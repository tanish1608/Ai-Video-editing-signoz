Kinetograph's first public alpha: an AI-assisted desktop video editor with reviewable edits, a timeline, and FFmpeg export.

- Fixed project switching that could erase saved timelines through CRDT deletion history.
- Kept normalization and media probing off the API event loop; bounded parallel encodes.
- Added per-launch local API authentication and corrected packaged asset/workspace paths.
- Updated desktop dependencies and the retired Gemini model default.
- Added backend/frontend regression checks, packaged backend smoke tests, and native packaging workflows.

**Requirements:** install FFmpeg and ffprobe separately (including H.264, AAC, and libass); configure your own API keys in Settings. AI features use external providers and may incur charges.

**Limitations:** unsigned alpha installers; no signing/notarization or automatic updates. Keep project backups. Build/smoke checks do not validate AI output quality or every platform's UI. Only download artifacts matching your OS and CPU architecture. SHA256SUMS.txt contains checksums.
