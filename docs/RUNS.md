# Run diagnostics and incremental indexing

## API keys

Save credentials in desktop Settings. Each provider shows whether its key is
present in the running engine; this is a presence check, not an authentication
test. Saving refreshes that status. Blank fields preserve existing keys.
A new scripting run reloads saved keys and rejects a missing Gemini key before
indexing footage. Saved keys take precedence over stale inherited environment
values on reload. The error identifies the environment file the engine reads.

## Stop

The AI assistant's square Stop button cancels the current run. Completed
Archivist work remains cached. In-flight vision/transcription requests are
cancelled locally; a provider may already have processed a submitted request.
FFmpeg extraction, normalization, rendering and caption processes are terminated
and reaped. The stopped run remains available in its log folder.

## Logs

Use the assistant's log icon or **Open run log** on a completion/error message.
Each project stores `logs/runs/<timestamp>_<run-id>/` containing:

- `run.json`: prompt, model names, key presence, node timings, errors and status.
- `events.jsonl`: pipeline events, including Archivist cache/timing statistics.
- `backend.log`: detailed execution and provider failure messages.

Configured secrets are redacted from logs, including structured error records.
Prompts, paths and transcripts can still be private: review logs before sharing.
The local API exposes `GET /api/runs` and `GET /api/runs/{run_id}`.

## Archivist cache and concurrency

`.cache/analysis/` maps each source to its transcript and successful visual
windows. File path, size, modification/change time, analysis version and model/
sampling settings form the cache identity. Unchanged videos skip extraction and
provider calls. New/changed videos are analyzed; incomplete videos reuse saved
transcription and retry missing windows. Same-named files have separate caches.

The first run with this cache format populates it. Legacy `master_index.json`
files lack source fingerprints and cannot safely prove that footage is unchanged.

Each video is decoded once for scene detection and sampled frames, scaled to at
most 768 pixels wide. Audio extraction/transcription overlaps frame extraction.
Defaults: two concurrent assets, two transcriptions, five vision requests across
all assets. Tune `ARCHIVIST_ASSET_CONCURRENCY`, `ARCHIVIST_STT_CONCURRENCY`, and
`VLM_CONCURRENCY` in the environment file, then restart the engine. Increasing
limits can trigger provider throttling or exhaust local resources.

Verification: `python -m pytest backend/tests/test_archivist_cache.py backend/tests/test_run_controls.py`.
These tests mock provider requests and exercise actual FFmpeg sampling and process
cancellation; no provider usage is charged.
