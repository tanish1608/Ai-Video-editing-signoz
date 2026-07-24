# 🎬 Kinetograph

**AI-powered autonomous video editor — describe your vision, get a broadcast-ready video.**

Kinetograph is an open-source desktop application that combines a **multi-agent AI pipeline** with a professional **non-linear editor (NLE)**. Drop in raw footage, describe what you want in plain English, and 8 specialised AI agents collaborate to produce a finished video — complete with cutaway synthesis, captions, sound design, and colour grading — all reviewable and editable through an integrated timeline.

<p align="center">
  <img src="docs/diagram-export-3-1-2026-2_17_56-PM.png" alt="Kinetograph Editor" width="800" />
</p>

---

## ✨ Features

- **Natural-language editing** — chat with the AI to create, revise, and refine your video
- **Deterministic multi-agent pipeline** — a fixed-order LangGraph: Archivist → Scripter → **human review** → (Synthesizer) → Director → Captioner → Sound Engineer → Export
- **Professional NLE timeline** — multi-track drag-and-drop editor with transitions, overlays, and keyboard shortcuts
- **Real-time preview** — dual-video player with crossfade transitions and V2 overlay support
- **Cutaway synthesis** — AI-sourced stock footage from Pexels, synced to your script
- **Automatic captions** — ElevenLabs STT with styled burn-in
- **Sound design** — Soundstripe background music with AI-powered vibe matching
- **Colour grading** — real-time preview with FFmpeg eq + colorbalance rendering
- **Desktop app** — native Electron shell with macOS/Windows/Linux support
- **Configurable models** — Gemini, NVIDIA Nemotron VLM, ElevenLabs, and more

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────┐
│                 Electron Shell                    │
│  ┌───────────────────────────────────────────┐  │
│  │           React + Vite Frontend           │  │
│  │  Timeline │ Viewer │ Chat │ Media Bin     │  │
│  └─────────────────┬─────────────────────────┘  │
│                    │ HTTP / WebSocket             │
│  ┌─────────────────▼─────────────────────────┐  │
│  │         Python Backend (Sidecar)           │  │
│  │  FastAPI + LangGraph Multi-Agent Pipeline  │  │
│  │  Fixed-order graph + human-review gate     │  │
│  │  FFmpeg · Gemini · NVIDIA NIM             │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

| Layer             | Tech                                                                               |
| ----------------- | ---------------------------------------------------------------------------------- |
| **Desktop shell** | Electron 33, `vite-plugin-electron`                                                |
| **Frontend**      | React 19, Vite 6, Tailwind CSS v4, Zustand 5                                       |
| **Backend**       | Python 3.11+, FastAPI, LangGraph, FFmpeg, PyAV                                     |
| **Rendering**     | FFmpeg `filter_complex` (single-pass), hardware-accelerated (VideoToolbox / NVENC) |
| **AI models**     | Gemini 2.5 Flash (LLM), NVIDIA Nemotron (VLM), ElevenLabs (STT)                    |
| **Timeline**      | `react-resizable-panels`, `@dnd-kit`, `framer-motion`                              |

---

## 📁 Project Structure

```
kinetograph/
├── backend/                 # Python AI pipeline + API server
│   ├── src/kinetograph/
│   │   ├── agents/          # 8 LangGraph agents
│   │   │   ├── archivist.py     # Media analysis (VLM)
│   │   │   ├── scripter.py      # Script generation (Gemini)
│   │   │   ├── producer.py      # Human-review gate (LangGraph interrupt)
│   │   │   ├── synthesizer.py   # Cutaway sourcing (Pexels)
│   │   │   ├── director.py      # Video compositing (FFmpeg filter_complex)
│   │   │   ├── captioner.py     # ASS caption generation (ElevenLabs STT)
│   │   │   ├── sound_engineer.py # Audio mastering (async FFmpeg)
│   │   │   └── export.py        # Final render + OTIO timeline
│   │   ├── core/            # Shared utilities
│   │   │   ├── compositor.py    # FilterGraphBuilder → FFmpeg filter_complex
│   │   │   ├── hwaccel.py       # Hardware encoder detection (VideoToolbox/NVENC)
│   │   │   ├── media.py         # FFmpeg probing & processing (sync + async)
│   │   │   ├── music.py         # Soundstripe integration
│   │   │   ├── captions.py      # ASS subtitle rendering
│   │   │   └── timeline.py      # OTIO timeline export
│   │   ├── config.py        # Pydantic settings (from .env)
│   │   ├── server.py        # FastAPI app + WebSocket
│   │   ├── orchestrator.py  # LangGraph graph wiring
│   │   ├── state.py         # Pipeline state schema
│   │   └── cli.py           # CLI entry point
│   ├── tests/
│   └── pyproject.toml       # Python dependencies
│
├── desktop/                 # Electron + React desktop app
│   ├── electron/
│   │   ├── main.ts          # Main process: window, sidecar, IPC
│   │   └── preload.ts       # Context bridge (renderer ↔ main)
│   ├── src/
│   │   ├── components/      # React UI components
│   │   ├── hooks/           # Custom React hooks
│   │   ├── store/           # Zustand state stores
│   │   ├── pages/           # Editor, Welcome, Settings views
│   │   ├── lib/             # API client, utilities
│   │   ├── types/           # TypeScript type definitions
│   │   ├── App.tsx          # Root component
│   │   └── main.tsx         # Entry point
│   ├── package.json
│   ├── vite.config.ts
│   └── tsconfig.json
│
├── docs/                    # Documentation
│   └── API.md               # Backend API reference
│
├── <project-dir>/           # Per-project (user-chosen directory)
│   ├── media/               # Imported media (symlinked references)
│   │   └── .synth/          # AI-sourced stock footage cache
│   ├── output/              # Rendered output
│   ├── state/               # Pipeline state, project manifest
│   │   └── project.json     # Project manifest
│   └── .cache/              # Thumbnails, waveforms, metadata
│
├── .env.example             # Environment template
├── .gitignore
└── README.md
```

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.11+** with `pip`
- **Node.js 20+** with `npm`
- **FFmpeg** installed and on `PATH`
- API keys (see [Configuration](#-configuration))

### 1. Clone the repo

```bash
git clone https://github.com/your-username/kinetograph.git
cd kinetograph
```

### 2. Set up the Python backend

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate    # macOS/Linux
# .venv\Scripts\activate     # Windows

# Install the backend in editable mode
pip install -e backend/
```

### 3. Set up the desktop app

```bash
cd desktop
npm install
cd ..
```

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys (see Configuration below)
```

### 5. Run the app

```bash
cd desktop
npm run dev
```

This starts both the Electron window and the Python backend automatically. The backend runs as a child process managed by Electron — no need to start it separately.

Create or open a project from the Welcome screen, then import your media files via **File → Import Media** (⌘I).

---

## ⚙️ Configuration

Copy `.env.example` to `.env` and fill in your API keys:

| Variable              | Required    | Description                                                                                 |
| --------------------- | ----------- | ------------------------------------------------------------------------------------------- |
| `GEMINI_API_KEY`      | **Yes**     | Google Gemini API key ([aistudio.google.com](https://aistudio.google.com))                  |
| `ELEVENLABS_API_KEY`  | **Yes**     | ElevenLabs API key for speech-to-text                                                       |
| `NVIDIA_API_KEY`      | Recommended | NVIDIA NIM API key for vision-language model ([build.nvidia.com](https://build.nvidia.com)) |
| `PEXELS_API_KEY`      | Recommended | Pexels API key for stock cutaway footage                                                    |
| `SOUNDSTRIPE_API_KEY` | Optional    | Soundstripe API key for background music                                                    |
| `HF_TOKEN`            | Optional    | Hugging Face token (alternative VLM hosting)                                                |

You can also configure these from the **Settings** page inside the app (⌘ + ,).

---

## ⌨️ Keyboard Shortcuts

| Key            | Action                                   |
| -------------- | ---------------------------------------- |
| `Space` / `K`  | Play / Pause                             |
| `J`            | Jump back 5 seconds                      |
| `L`            | Jump forward 5 seconds                   |
| `←` / `→`      | Step one frame / one second (with Shift) |
| `Home` / `End` | Jump to start / end                      |
| `Escape`       | Stop playback                            |
| `⌘Z` / `⌘⇧Z`   | Undo / Redo                              |
| `⌘S`           | Open export panel                        |
| `⌘L`           | Toggle AI chat                           |

---

## 🤖 Pipeline Agents

| #   | Agent              | Role                                                                       | Tech                                |
| --- | ------------------ | -------------------------------------------------------------------------- | ----------------------------------- |
| 1   | **Archivist**      | Analyse raw footage — extract keyframes, transcribe audio, describe scenes | NVIDIA Nemotron VLM, ElevenLabs STT |
| 2   | **Scripter**       | Generate a video script from the user's prompt + media analysis            | Gemini 2.5 Flash                    |
| —   | **Human review**   | The pipeline pauses (`interrupt()`) for the user to approve/edit the paper edit before rendering | — (LangGraph interrupt) |
| 3   | **Synthesizer**    | Source cutaway footage from Pexels (only if the edit contains synth clips) | Pexels API                          |
| 4   | **Director**       | Composite the final video — cuts, transitions, overlays                    | FFmpeg                              |
| 5   | **Captioner**      | Generate and burn-in styled captions                                       | ElevenLabs STT, FFmpeg              |
| 6   | **Sound Engineer** | Mix audio — normalize, EQ, add background music                            | FFmpeg                              |
| 7   | **Export**         | Final render with colour grading + OTIO timeline                           | FFmpeg, OpenTimelineIO              |

> The `producer.py` module now contains only the human-review gate — there is no LLM orchestrator that dynamically selects agents; the graph order is fixed (see `orchestrator.py`).

---

## ⚡ Rendering Architecture

Kinetograph renders video in a **single FFmpeg process** using `-filter_complex`, replacing the previous MoviePy frame-by-frame Python/NumPy pipeline. This is **5–20× faster** on most hardware.

### How it works

1. **`hwaccel.py`** — Probes the system FFmpeg at startup and smoke-tests hardware encoders in priority order: `h264_videotoolbox` (macOS) → `h264_nvenc` (NVIDIA) → `h264_qsv` (Intel) → `h264_vaapi` (Linux) → `libx264` (software fallback).

2. **`compositor.py`** — The `FilterGraphBuilder` class incrementally constructs an FFmpeg filter graph. The Director describes its entire timeline (trim, concat, xfade, PiP overlay, caption burn-in) as a DAG of filter expressions, then the builder compiles it into one `ffmpeg` command with hardware acceleration flags.

3. **Single-pass rendering** — Instead of the old 3-pass pipeline (Director render → Captioner re-encode → Sound Engineer re-encode), the Director now burns ASS captions directly into its filter graph. The Sound Engineer's denoise + LUFS normalization is also consolidated into a single FFmpeg pass.

4. **Async subprocess** — All FFmpeg commands use `asyncio.create_subprocess_exec`, so the FastAPI event loop is never blocked during rendering. The old synchronous `subprocess.run` calls froze the server.

```
Before (MoviePy):  decode → Python/NumPy → encode × 3 passes  = ~40s for 60s video
After  (FFmpeg):   filter_complex → single pass (hwaccel)     = ~4s for 60s video
```

---

## 🔌 API Reference

See [docs/API.md](docs/API.md) for the full backend REST + WebSocket API reference.

**Quick overview:**

| Endpoint                     | Method    | Description               |
| ---------------------------- | --------- | ------------------------- |
| `/api/health`                | GET       | Health check              |
| `/api/pipeline/start`        | POST      | Start the AI pipeline     |
| `/api/pipeline/stop`         | POST      | Stop the pipeline         |
| `/api/assets`                | GET       | List all media assets     |
| `/api/assets/upload`         | POST      | Upload media files        |
| `/api/paper-edit`            | GET       | Get the current timeline  |
| `/api/paper-edit/clips/{id}` | PATCH     | Update a timeline clip    |
| `/api/render`                | POST      | Trigger a final render    |
| `/ws`                        | WebSocket | Real-time pipeline events |

---

## 📦 Building for Distribution

### macOS (.dmg)

```bash
cd desktop
npm run build
```

Output will be in `desktop/release/`.

### Windows (.exe)

```bash
cd desktop
npm run build -- --win
```

### Linux (.AppImage)

```bash
cd desktop
npm run build -- --linux
```

> **Note:** Production builds bundle the Python backend using PyInstaller. See `desktop/package.json` → `build.extraResources` for the bundling configuration.

---

## 🛠 Development

### Backend only (for testing)

```bash
source .venv/bin/activate
uvicorn kinetograph.server:app --host 0.0.0.0 --port 8080 --reload
```

### Frontend only (Vite dev server)

```bash
cd desktop
npm run dev
```

### Type checking

```bash
cd desktop
npx tsc --noEmit
```

### Linting (Python)

```bash
cd backend
ruff check src/
```

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

<p align="center">
  Built with 🎬 by the Kinetograph team
</p>
