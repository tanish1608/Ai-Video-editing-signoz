# Local backend API

The backend serves one active desktop project on `http://127.0.0.1:8080`. Run `./dev.sh backend` and open `/docs` or `/openapi.json` for the authoritative schemas. Endpoints below are a map, not a substitute for generated request/response definitions.

## Access

Electron injects an `X-Kinetograph-Token` header into HTTP, media, and WebSocket requests. Its random value is passed to the child process as `KINETOGRAPH_API_TOKEN` and changes on each app launch. Do not put it in URLs. Standalone development may omit the token; only local Vite browser origins and non-browser clients are allowed. Host headers must identify loopback. Never expose the development API publicly.

## Endpoint groups

| Purpose | Endpoints |
| --- | --- |
| Readiness/configuration | `GET /api/health`, `GET/POST /api/config`, `GET/POST /api/config/color-grade` |
| Active project | `POST /api/project/set-dir` with `{"project_dir": "/absolute/path"}` |
| Pipeline | `POST /api/pipeline/run`, `/approve`, `/edit`; `GET /api/pipeline/status` |
| Captions | `GET /api/pipeline/caption-styles`, `POST /api/pipeline/caption-style` |
| Render | `POST /api/render` |
| Media | `GET /api/assets`, `POST /api/assets/register`, `POST /api/assets/upload` |
| Individual asset | `DELETE /api/assets/{asset_id}`, `PATCH /api/assets/{asset_id}/type` |
| Previews | `GET /api/assets/{asset_id}/thumbnail`, `/waveform`, `/stream` |
| Path-based playback | `GET /api/assets/stream?path=...` (project/registered paths only) |
| Index | `GET /api/master-index`, `GET /api/master-index/search` |
| Timeline snapshot | `GET/PUT /api/paper-edit` (compatibility endpoints) |
| Exports | `GET /api/output`, `GET /api/output/{filename}` |
| Cache | `GET /api/cache/stats`, `DELETE /api/cache` |

## Synchronization and project switching

`/ws` carries JSON pipeline events, including `connected`, `phase_update`, `awaiting_approval`, and `pipeline_complete`. `/ws/crdt/paper-edit` carries binary Yjs sync messages for timeline state. A client must replace its Y.Doc when changing projects; deleting the old document's contents propagates tombstones and can corrupt reloaded snapshots.

A project switch saves the outgoing snapshot, disconnects CRDT clients with close code `4001`, replaces the backend document, and restores the selected project's state. Clients should wait for the project-switch response before reconnecting with a fresh document. Switching while a pipeline job is active returns `409`.

## Rendering and approval

Pipeline runs pause at human review; `/api/pipeline/approve` resumes the saved graph. Editing content re-enters planning and approval. Render and audio changes can start later in the workflow. Check the response status and subsequent events: a started background job can still fail, and `pipeline_complete` includes its final phase.

Provider requests belong to the backend. The renderer should use `desktop/src/lib/api.ts` for REST calls and the CRDT provider for timeline edits.

## Run diagnostics

`POST /api/pipeline/stop` cancels active work. `GET /api/runs` lists project runs;
`GET /api/runs/{run_id}` returns a summary, events and log tail.
See [Run diagnostics and incremental indexing](RUNS.md) for cache and key handling.
