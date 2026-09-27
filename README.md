# Kinetograph

A desktop video editor that turns footage and a written brief into an editable timeline. Review the proposed edit before rendering, adjust clips in the timeline, and export video and OpenTimelineIO files.

**Status: public alpha.** Expect rough edges. Keep original footage and a backup of projects. AI-assisted editing uses your own provider accounts; it is not an offline AI model.

## Install the desktop app

Download an available installer from [Releases](https://github.com/tanish1608/Ai-Video-editing-signoz/releases). The first release uses unsigned builds; macOS and Windows may require an explicit security approval to open the app. Only install artifacts from this repository. Platform/architecture support is listed on each release.

The app bundles Python, but **FFmpeg and ffprobe must be installed separately and available on PATH**. On macOS, `brew install ffmpeg` installs both; the app also checks standard Homebrew locations. On Windows and Linux, install an FFmpeg build with H.264, AAC, and libass support. Restart the app after installing it.

1. Open Settings and enter your provider keys.
2. Create or open a project folder and import footage.
3. Describe the desired edit. Review and approve the paper edit.
4. Choose captions, render, and export.

Gemini powers edit planning and review; ElevenLabs provides transcription. NVIDIA NIM adds visual analysis, Pexels supplies optional stock footage, and Soundstripe is optional music integration. Provider usage may incur charges. Footage, extracted frames, transcripts, and prompts may be sent to the providers used by the selected features. Telemetry is off by default.

## Run from source

Requires Python 3.11+ (3.12 tested), Node.js 24+, and FFmpeg/ffprobe. On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e 'backend/[dev,build]'
(cd desktop && npm ci)
cp .env.example .env
./dev.sh
```

Fill in `.env` before using the AI pipeline. `./dev.sh backend` runs the API; `./dev.sh desktop` connects Electron to an already running API. Restart `dev.sh` after changing provider settings. The development script never terminates an unrelated process using port 8080.

On Windows, activate `.venv\Scripts\Activate.ps1`, install the same dependencies, and run `npm run dev` from `desktop/`; Electron starts the Python backend. `dev.sh` requires a POSIX shell.

## Checks and packaging

```bash
python -m pytest backend/tests -q
python -m ruff check backend/src
(cd desktop && npm test)
(cd desktop && npm run typecheck)
(cd desktop && npm run build)
```

Run packaging with the virtual environment active. Installers appear in `desktop/release/`. Builds target the host OS and architecture; use the release workflow for other platforms. Live NVIDIA diagnostic scripts require credentials and sample media and are separate from offline checks. See [release instructions](docs/RELEASING.md).

## Architecture

- `desktop/src/`: React UI, Zustand view state, and Yjs timeline/undo.
- `desktop/electron/`: filesystem dialogs, settings, and local backend lifecycle.
- `backend/src/kinetograph/`: FastAPI, a deterministic LangGraph workflow, and FFmpeg rendering.
- `backend/tests/` and `desktop/tests/`: regression tests.
- `backend/observability/`: optional OpenTelemetry/SigNoz integration.

The workflow is `index → plan ⇄ critique → human approval → optional stock → render → captions → audio → export`. “Agents” are workflow stages, not independent autonomous services. Normalization, captions, and audio can require additional encoding passes.

See [architecture and tradeoffs](docs/ARCHITECTURE.md), [API reference](docs/API.md), and [contributor guidelines](AGENTS.md).

## Project data and security

Media imports reference originals. Project folders contain `media/`, `state/`, `output/`, and `.cache/`. Keep originals accessible; deleting a cache is safe, deleting `state/` loses project data. The packaged default workspace lives in Electron's user-data directory.

The backend listens on loopback. Packaged Electron launches authenticate HTTP, media, and WebSocket requests with a random per-launch token. Do not expose the development API through a public tunnel. API keys are stored locally in `.env` and app settings; do not include them or project files in issues.

The repository previously tracked a sample CRDT snapshot and SQLite journal files. They have been removed from the current tree but remain in historical commits; avoid copying that pattern into contributions.

## License

[MIT](LICENSE). External services, media, codecs, and dependencies have their own terms.
