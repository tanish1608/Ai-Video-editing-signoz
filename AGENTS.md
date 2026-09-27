# Repository Guidelines

## Project Structure & Module Organization

Kinetograph is an Electron video editor with a React/TypeScript frontend and Python FastAPI backend.

- `backend/src/kinetograph/`: API server, LangGraph orchestration, schemas, and configuration; `agents/` contains pipeline stages and `core/` contains FFmpeg/media utilities.
- `backend/tests/`: Python tests and live API diagnostic scripts; `backend/observability/`: SigNoz dashboards and smoke tests.
- `desktop/src/`: React components, pages, hooks, Zustand stores, and API/CRDT clients; `desktop/electron/`: main process and preload bridge.
- `docs/`: API reference, manual test prompts, and diagrams. Runtime media, exports, state, and caches belong in project directories, outside source code.

## Build, Test, and Development Commands

Use Python 3.11+, Node.js 24+, and FFmpeg on `PATH`. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e 'backend/[dev]'
(cd desktop && npm install)
cp .env.example .env  # First setup only; configure API keys locally.
./dev.sh             # Launch backend and Electron.
./dev.sh backend     # Backend with reload on port 8080.
./dev.sh desktop     # Desktop using an external backend.
(cd backend && pytest)
(cd backend && ruff check src/)
(cd desktop && npm test)
(cd desktop && npm run typecheck)
```

For distribution, install `pip install -e 'backend/[build]'`, then run `(cd desktop && npm run build)`. This bundles the Python sidecar and writes installers to `desktop/release/`.

## Coding Style & Naming Conventions

Use four-space Python indentation, type annotations, and `snake_case` functions/modules. Ruff targets Python 3.11 with a 100-character limit and E/F/I/W checks. TypeScript uses strict checking; follow each file’s indentation, quotes, and semicolons. Use PascalCase React exports and kebab-case component/hook filenames, following nearby code. No ESLint or Prettier configuration is present.

## Testing Guidelines

Use pytest and pytest-asyncio; name files `test_*.py` and tests `test_<behavior>`. Add focused regression tests for changed behavior, mocking external services. Run individual files with `(cd backend && pytest tests/test_units.py)`. Live NVIDIA diagnostic scripts require credentials and sample media. Frontend regression tests use Vitest in `desktop/tests/*.test.ts`. No coverage threshold is configured; also manually verify UI changes using `docs/test-prompts.md`.

## Commit & Pull Request Guidelines

History uses short, plain-language subjects such as `Update README`; no formal convention is established. Write concise imperative subjects. PRs should explain behavior changes, list validation performed, link relevant issues, and include screenshots for UI changes.

## Security & Configuration

Keep API keys in local `.env` files. Never commit credentials, generated media, caches, or SQLite checkpoints.
