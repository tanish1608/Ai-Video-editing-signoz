# 🎬 Kinetograph — Production TODO

> ⚠️ **Partially stale.** An architecture audit (2026-07) addressed several items
> below — notably: backend now binds to `127.0.0.1`, CORS is locked to the
> renderer origins, WebSocket reconnection uses exponential backoff, path
> traversal is closed on the asset/output endpoints, and the pipeline no longer
> reports success on a failed render. It also fixed bugs not listed here
> (CRDT double-broadcast, event-loop blocking, hardware-decode render failures,
> caption/loudness issues). References to `pipeline.py` are stale — that module
> no longer exists (see `orchestrator.py`). Still open from this list:
> `webSecurity:false`/CSP, the 3-min stuck-agent band-aid, non-GET API retries,
> and most Missing-Features/Packaging items.
>
> Tracking all bugs, missing features, and production-readiness items.
> Checked items `[x]` are done. Unchecked `[ ]` are pending.

---

## 🔴 Bugs & Broken

- [ ] **Import Media does nothing** — Electron menu `File → Import Media` (⌘I) triggers the file picker but files are only logged, never uploaded to the backend or added to the asset store.
  - File: `desktop/src/pages/Editor.tsx` (line ~330, `// TODO: Upload files via API`)
  - Fix: Call `KinetographAPI.uploadAssets()` with the selected files, then refresh asset list.

- [ ] **Asset delete is client-only** — Deleting an asset in the Media Bin only removes it from the Zustand store. No backend API call is made and no file is removed from disk. Assets reappear on refresh.
  - File: `desktop/src/components/asset-dropzone.tsx`
  - Fix: Add a `DELETE /api/assets/{id}` endpoint on the backend, call it from the frontend.

- [ ] **Duplicate NVIDIA_API_KEY in generated .env** — `writeEnvFile()` in the Electron main process writes `NVIDIA_API_KEY` twice (under "AI Keys" and "Model Configuration"). Second value overrides first.
  - File: `desktop/electron/main.ts` → `writeEnvFile()`

---

## 🟡 Missing Features

- [ ] **Backend: DELETE /api/assets/{id} endpoint** — No way to delete assets from disk via the API. Needed for the asset delete UI to actually work.
  - File: `backend/src/kinetograph/server.py`

- [ ] **Render progress feedback** — Export panel fires the render then does a blind 2-second `setTimeout` before polling outputs. No progress bar, percentage, or streaming status. Long renders show nothing.
  - File: `desktop/src/components/export-panel.tsx`
  - Fix: Add a `/api/render/status` endpoint or stream progress via WebSocket. Show a progress bar in the UI.

- [ ] **Native "Save As" for rendered files** — Download links use `<a href=...>` which may not work correctly inside Electron. Should use `dialog.showSaveDialog()` and stream the file to the chosen location.
  - File: `desktop/src/components/chat-panel.tsx` (render download links)
  - File: `desktop/src/components/export-panel.tsx`

- [ ] **Undo/redo for timeline operations** — Zustand store has `undo()`/`redo()` but not all timeline mutations (drag reorder, trim, delete clip) push to the undo stack consistently.
  - File: `desktop/src/store/use-kinetograph-store.ts`

- [ ] **Multiple project support** — Welcome screen shows "Recent Projects" but the backend only supports a single global project directory (media_drop/output/state at repo root). Need per-project isolation.
  - File: `backend/src/kinetograph/config.py` — honor `KINETOGRAPH_PROJECT_DIR` env var
  - File: `desktop/electron/main.ts` — pass project dir to backend on startup

- [ ] **Auto-save project state** — No persistence of timeline state between sessions. Closing and reopening loses the current edit.

- [ ] **Waveform display on audio tracks** — Timeline shows audio clips but no waveform visualization.

- [ ] **Clip trimming in timeline** — Clips can be dragged/reordered but not trimmed (adjusting in/out points by dragging edges).

- [ ] **Transitions UI** — Transitions exist in the data model but there's no UI to add/change/remove transitions between clips.

---

## 🟠 Code Quality

- [ ] **Silent `.catch(() => {})` swallowing errors** — 20+ occurrences across the frontend. API calls to save paper edits, reorder clips, run pipeline, etc. silently discard failures. Users think changes saved when they didn't.
  - Affected files: `export-panel.tsx`, `asset-dropzone.tsx`, `chat-panel.tsx`, `timeline-editor.tsx`, `Editor.tsx`, `use-kinetograph-store.ts`, `use-video-player.ts`, and more.
  - Fix: Add error toasts for user-facing actions. Use a global error boundary.

- [ ] **Console.log throughout renderer** — `console.log`/`console.warn`/`console.error` used for logging in production-facing code. Should use a structured logger or strip in prod builds.
  - Key offenders: `use-kinetograph-ws.ts`, `Editor.tsx`, `use-video-player.ts`

- [ ] **Empty catch blocks** — Multiple `try {} catch {}` with no error handling in `use-video-player.ts`, `Editor.tsx`, `local-asset.ts`.

- [ ] **API client retry only on GET** — `ky` is configured with `retry: { limit: 2, methods: ["get"] }`. POST/PATCH/DELETE calls get zero retries, meaning save failures are permanent.
  - File: `desktop/src/lib/api.ts`

- [ ] **3-minute stuck-agent auto-clear is a band-aid** — If `pipelineActive` is true for >3 min, it's force-cleared. This masks real backend hangs instead of notifying the user.
  - File: `desktop/src/store/use-chat-store.ts`

- [ ] **Hardcoded `pipeline.py` review URL** — `http://localhost:{port}/review` in the orchestrator. Would break in any non-local deployment.
  - File: `backend/src/kinetograph/pipeline.py`

---

## 🔵 Production Readiness

### Security

- [ ] **`webSecurity: false` in Electron** — Disables same-origin policy and CORS in the renderer. This is a significant security risk in production builds.
  - File: `desktop/electron/main.ts` → `BrowserWindow` options
  - Fix: Use a custom `file://` or `app://` protocol, or proxy media through the backend. Remove `webSecurity: false`.

- [ ] **CORS is wide open on the backend** — `allow_origins=["*"]` in FastAPI CORS middleware.
  - File: `backend/src/kinetograph/server.py`
  - Fix: Restrict to `http://localhost:5173` (dev) and the Electron app origin in production.

- [ ] **Backend binds to `0.0.0.0`** — Exposes the API on all network interfaces. Anyone on the same LAN can access the backend.
  - File: `backend/src/kinetograph/config.py` (default `api_host = "0.0.0.0"`)
  - Fix: Default to `127.0.0.1`. Only bind `0.0.0.0` if explicitly configured.

- [ ] **No API rate limiting** — `/api/pipeline/start` and `/api/render` are expensive operations with no rate limiting.
  - File: `backend/src/kinetograph/server.py`

- [ ] **No validation of required API keys at startup** — Backend starts fine with empty API keys, then fails at runtime when agents try to use them. Should fail fast with a clear error message.
  - File: `backend/src/kinetograph/config.py`

### Stability

- [ ] **WebSocket reconnection has no backoff** — Reconnects with a flat 3-second delay forever. No exponential backoff, no max-retry limit, no user notification of persistent disconnection.
  - File: `desktop/src/hooks/use-kinetograph-ws.ts`

- [ ] **Backend health check runs forever** — Electron polls `/api/health` every N seconds indefinitely. Previous session showed it timing out after 30s and stopping the backend. Should have better lifecycle management.
  - File: `desktop/electron/main.ts`

- [ ] **No graceful shutdown on backend errors** — If the Python process crashes, Electron shows "Backend stopped" but doesn't offer a retry button or helpful error message.

### Packaging & Distribution

- [ ] **PyInstaller bundling** — Production builds need the Python backend bundled as a standalone binary. Currently only works in dev mode with the venv.
  - Need: PyInstaller spec file, GitHub Actions CI for multi-platform builds.

- [ ] **App icons** — `build/icon.icns` (macOS), `build/icon.ico` (Windows), `build/icon.png` (Linux) referenced in electron-builder config but don't exist yet.
  - File: `desktop/package.json` → `build.mac.icon`, `build.win.icon`, `build.linux.icon`

- [ ] **Code signing** — macOS requires notarization for distribution. Windows needs Authenticode signing. Neither is configured.
  - File: `desktop/package.json` → `build.mac.hardenedRuntime` is set but no signing identity.

- [ ] **Auto-updater** — No auto-update mechanism. Users would need to manually download new versions.
  - Consider: `electron-updater` with GitHub Releases.

- [ ] **Crash reporting** — No crash/error reporting in production. Backend errors and Electron renderer crashes go unnoticed.

### UX Polish

- [ ] **Loading states** — Many operations (upload, render, pipeline) lack proper loading indicators.

- [ ] **Onboarding flow** — No first-run tutorial or guided setup for API keys.

- [ ] **Error messages** — Most errors are silent or show generic toasts. Should have contextual, actionable error messages.

- [ ] **Responsive layout** — UI doesn't handle very small or very large window sizes well.

- [ ] **Accessibility** — No ARIA labels, keyboard navigation is limited to shortcuts, no screen reader support.

- [ ] **Dark/Light theme** — Currently dark-only. Consider adding theme support.

---

## 📋 Priority Order

### P0 — Must fix before any release
1. Fix Import Media flow (actually upload files)
2. Fix asset delete (add backend endpoint + call it)
3. Remove `webSecurity: false` from Electron
4. Bind backend to `127.0.0.1` by default
5. Lock down CORS origins
6. Add proper error handling (replace silent `.catch(() => {})`)

### P1 — Should fix for beta
7. Render progress feedback
8. Native Save As for exports
9. WebSocket reconnection backoff
10. API key validation at startup
11. PyInstaller bundling for production builds
12. App icons

### P2 — Nice to have for v1.0
13. Multiple project support
14. Auto-save project state
15. Undo/redo consistency
16. Clip trimming in timeline
17. Transitions UI
18. Waveform display
19. Code signing + auto-updater
20. Onboarding flow

---

*Last updated: March 2, 2026*










