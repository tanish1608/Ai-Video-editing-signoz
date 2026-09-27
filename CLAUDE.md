# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Kinetograph — an Electron desktop video editor. A React frontend (`desktop/`) talks over HTTP + WebSocket to a Python FastAPI backend (`backend/`) that runs a LangGraph multi-agent pipeline. The backend renders video through FFmpeg `filter_complex` (single-pass, hardware-accelerated), not MoviePy/frame-by-frame. In dev, Electron spawns the backend as a child "sidecar" process on port 8080; in production it runs a PyInstaller-bundled binary.

## Commands

Everything is driven from `./dev.sh` (preflight-checks venv, backend install, node_modules, FFmpeg, `.env`):

```bash
./dev.sh              # backend + Electron together
./dev.sh backend      # backend only (uvicorn --reload on :8080)
./dev.sh desktop      # frontend only; sets KINETOGRAPH_EXTERNAL_BACKEND=1 so Electron won't spawn its own backend
```

Manual / first-time setup:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend/[dev]"      # editable install with pytest/ruff
cd desktop && npm install
cp .env.example .env                # then fill in API keys
```

Backend alone (from repo root, venv active):
```bash
uvicorn kinetograph.server:app --host 127.0.0.1 --port 8080 --reload --reload-dir backend/src
kinetograph run "<prompt>" --project <name>   # headless pipeline via CLI (python -m kinetograph)
kinetograph runs [<run_id>] --project <dir>    # list / inspect logged pipeline runs
```

Tests / lint / typecheck:
```bash
cd backend && pytest                          # all tests (pytest-asyncio)
cd backend && pytest tests/test_archivist_nemotron.py::<name>   # single test
cd backend && ruff check src/                 # line-length 100, rules E/F/I/W
cd desktop && npm run typecheck               # tsc --noEmit
```

Distribution builds: `cd desktop && npm run build` (add `-- --win` / `-- --linux`); output in `desktop/release/`. `build` first runs `build:sidecar` (`backend/scripts/build_sidecar.py`), which PyInstaller-bundles `kinetograph/sidecar.py` as the backend binary — see `desktop/package.json` → `build.extraResources`.

## Backend architecture

The pipeline is a **deterministic LangGraph StateGraph** (`orchestrator.py`, `build_graph()`), not LLM-routed. Routing is by rule-based conditional edges:

```
archivist → scripter ⇄ critic → human_review → [synthesizer?] → director → captioner → sound_engineer → export → END
                                     ↑ reject → scripter
any node with phase == "error" → error_handler → END
```

- `critic` (`agents/critic.py`) is a Gemini editorial-QA pass on every fresh/revised edit. It sends the edit back to `scripter` only when feedback `needs_revision()` and `critic_iteration < CRITIC_MAX_ITERATIONS` (2) — a bounded loop; otherwise (or on critic error) it proceeds to `human_review`.
- Post-approval agents **swallow exceptions** and return `{"phase": Phase.ERROR}` instead of raising; `_route_on_error` guards each edge so a failed render doesn't reach `export` and report COMPLETE. Preserve this pattern when adding nodes.
- `schema.py` — Pydantic v2 Editorial Decision List (EDL) models (`EditorialDecisionList`, `CriticFeedback`, …). Agents still write plain dicts into `GraphState` but validate at boundaries via `model_validate` / `model_dump`.
- `observability.py` — OpenTelemetry traces + metrics to SigNoz over OTLP (`OTEL_EXPORTER_OTLP_ENDPOINT`, default `http://localhost:4318`). The orchestrator's `_make_node` wrapper instruments every agent; `init_telemetry()` is called by the server and CLI. Without it (e.g. tests) all helpers are no-ops, and a down collector never fails the pipeline. Dashboard/alert defs live in `backend/observability/`.
- `runlog.py` — every `_stream_pipeline` call writes `<project>/logs/runs/<ts>_<run_id>/` (`run.json` summary with per-node timings + errors + key presence, `events.jsonl`, `backend.log` with secrets redacted). Exposed via `GET /api/runs[/{run_id}]` and `kinetograph runs`. `POST /api/pipeline/stop` cancels the running task; `_stream_pipeline` swallows `CancelledError` and broadcasts `pipeline_stopped`, and `compositor.py` kills FFmpeg on cancel (work inside `asyncio.to_thread` can't be interrupted).
- API keys: the packaged app reads `KINETOGRAPH_ENV_FILE` (`<userData>/.env`, written by the Settings page), **not** the repo `.env`. `settings.reload_secrets()` re-reads keys at each run start so Settings → Save applies without restarting; `/api/pipeline/run` returns 412 when the Gemini key is missing.
- `state.py` — `GraphState` is a `TypedDict` (LangGraph requires TypedDict, not a Pydantic model). List fields use `operator.add` reducers → append semantics. `Phase` is a str-enum; `_normalize_phase` in the orchestrator converts enums to plain strings before checkpoint serialization to avoid LangGraph deserialization warnings.
- `human_review` uses LangGraph `interrupt()` — the user **must** approve the paper edit before rendering. Approval resumes via `/api/pipeline/approve`. A rejection loops back to `scripter`. REST endpoints are documented in `docs/API.md`.
- Synthesizer runs only if the approved edit contains clips with `clip_type == "synth"` (Pexels stock B-roll).
- **Edits** (post-pipeline tweaks) don't re-run the whole graph. `/api/pipeline/edit` calls `_classify_edit()` in `server.py`, a keyword rule-based classifier that picks a `start_from` node: music/audio → `sound_engineer`, captions/text → `captioner`, render/color/quality → `director`, else (content change) → `scripter` (which goes through approval again). The graph is recompiled with that entry point.
- `agents/` — one module per pipeline node. Note `producer.py` only contains `human_review_node`; the "Producer as LLM orchestrator" description in `README.md` is outdated — there is no LLM routing.
- `core/` — shared rendering: `compositor.py`'s `FilterGraphBuilder` compiles the whole timeline (trim/concat/xfade/PiP/caption burn-in) into a single `ffmpeg -filter_complex` command; `hwaccel.py` smoke-tests encoders at startup (videotoolbox → nvenc → qsv → vaapi → libx264); `media.py` does ffprobe + processing (all FFmpeg calls are `asyncio.create_subprocess_exec` so the event loop never blocks).
- `config.py` — `Settings` (pydantic-settings) loads `.env` from repo root; `settings` is an importable singleton. All project paths derive from `_project_root`, which is `KINETOGRAPH_PROJECT_DIR` when set, else repo root.
- `server.py` (~2400 lines) is the single FastAPI app: pipeline control, asset management (import-by-reference, thumbnails, waveforms, cached ffprobe), and WebSockets. Routes are tagged `System` / `Pipeline` / `Assets`.

## Frontend ↔ backend contract

- `desktop/electron/main.ts` owns the backend lifecycle: spawns the sidecar (skips if `/api/health` already answers, e.g. under `dev.sh`), and switches projects by POSTing `/api/project/set-dir` (the backend re-points all paths live). It also writes API keys from the Settings UI into `.env` (`writeEnvFile`).
- `desktop/src/lib/api.ts` — `KinetographAPI` (ky-based HTTP client) is the only place REST calls live.
- Real-time state uses **two** WebSockets: `/ws` for pipeline events, and `/ws/crdt` for the timeline. The timeline is a **CRDT** (Yjs on the frontend, `pycrdt` in `backend/crdt.py`) — the backend Y.Doc is the authoritative copy and persists to `state/crdt_snapshot.yjs`. When editing timeline behavior, changes must round-trip through the CRDT, not plain REST.
- State stores: `desktop/src/store/use-kinetograph-store.ts` and `use-chat-store.ts` (Zustand).

## Project directory layout

A "project" is any user-chosen directory. Electron scaffolds it (`ensureProjectStructure`): `media/` (imported clips, referenced by path — not copied — via `state/media_refs.json`), `media/.synth/` (Pexels cache), `output/`, `state/` (`project.json` manifest + CRDT snapshot), `.cache/` (thumbnails/waveforms/metadata/conformed-audio — Adobe-style, safe to delete and regenerated on demand).

## Config keys

Required: `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`. Recommended: `NVIDIA_API_KEY` (Nemotron VLM), `PEXELS_API_KEY`. Optional: `SOUNDSTRIPE_API_KEY`, `HF_TOKEN`. Models default to `gemini-2.5-flash-preview-05-20` and `nvidia/nemotron-nano-12b-v2-vl` (overridable via env or the in-app Settings page).
