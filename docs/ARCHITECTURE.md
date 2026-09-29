# Architecture and tradeoffs

## Assessment

The Electron / Python / FFmpeg split is appropriate: browser technology serves the editing UI, Python integrates AI providers, and FFmpeg handles media. Replacing this stack would be a large rewrite with little immediate benefit.

The workflow is deterministic. LangGraph earns its place through durable checkpoints and the pause for human approval. Keep routing explicit; adding an LLM supervisor would increase cost and make failures harder to reproduce.

## State ownership

- The active project's CRDT document owns the timeline and undoable edits. React derives view state through Zustand.
- LangGraph/SQLite stores pipeline progress and approval checkpoints.
- Electron owns which project is open and which backend process it started.
- A single backend serves one active project. It is not a multi-tenant web service.

Each project gets a fresh CRDT document in both processes. Clearing the contents of the old document is insufficient: CRDT deletions survive synchronization and can erase data when old snapshots are reloaded. Project changes close old sockets, replace document identities, and discard deferred events from the previous document.

## Remaining complexity

`server.py` combines routing, pipeline sessions, asset indexing, and persistence. The next structural refactor should extract project/session and media services behind tested interfaces, then split FastAPI routers. Moving functions alone would spread the same global state across more files.

The timeline currently uses Yjs/pycrdt, a hand-written sync protocol, Zustand, and compatibility JSON snapshots. CRDT is useful for granular undo and synchronized timeline changes, but expensive for a single-window product. Retain it for this alpha; decide whether collaboration is a product requirement before replacing it. Gradually retire compatibility snapshots once project migrations are defined.

## Rendering and performance

FFmpeg performs actual encoding. A bounded thread pool launches normalization subprocesses; another Python process pool adds startup and packaging costs without speeding up FFmpeg. Blocking normalization waits and media probes must stay off the FastAPI event loop.

The compositor combines timeline operations in one filter graph, but the complete workflow still normalizes footage and may re-encode for captions and audio. Do not advertise the whole pipeline as single-pass. Reusing normalized files across unchanged renders and folding caption/audio work into final composition are future optimizations requiring output-equivalence tests.

Metadata caches include the resolved source path, modification time, and size. Same-named files from different directories must not share probe results.

## Public alpha boundaries

Local API access is protected by an Electron-injected per-launch token; the token does not appear in media URLs or logs. Standalone development additionally restricts browser origins and Host headers. The desktop does not host a publicly accessible server.

FFmpeg is an external prerequisite. Installers are unsigned until signing identities and notarization are configured. Offline tests and packaged backend smoke checks do not establish AI output quality or replace platform UI testing.

The model default was updated because the old preview was retired; model availability remains configurable. See [Google's deprecation schedule](https://ai.google.dev/gemini-api/docs/deprecations). Electron was updated following its [security guidance](https://www.electronjs.org/docs/latest/tutorial/security).

## Incremental ingestion

Archivist checkpoints transcription and individual visual windows in a project-local
cache keyed by source identity and analysis settings. Bounded asset and transcription
workers share one vision-request semaphore. Sampled frames and scene detection share
a decoder; transcripts and frame extraction run concurrently. Cancellation drains
child tasks and encoder processes before a replacement run starts. See [RUNS.md](RUNS.md).
