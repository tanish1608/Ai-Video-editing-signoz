# Releasing Kinetograph

1. Update `desktop/package.json`, its lockfile, `backend/pyproject.toml`, and `backend/src/kinetograph/__init__.py` to corresponding versions (npm uses `0.1.0-alpha.1`, Python uses `0.1.0a1`).
2. Run CI checks and `npm run build` on the target platform with the Python environment active. The desktop wrapper locates `.venv` or uses `KINETOGRAPH_PYTHON`.
3. Run `python backend/scripts/smoke_sidecar.py backend/dist/kinetograph-server` (add `.exe` on Windows). This checks startup, authentication, writable project paths, and project switching without provider credentials.
4. Exercise the packaged UI: launch, open/reopen projects, import media, undo/redo, restart the engine, render, export, and quit. Test AI features with your own accounts and non-sensitive sample media.
5. Update `docs/RELEASE_NOTES.md`, push a version tag, and wait for the Desktop release workflow. It builds on macOS, Windows, and Linux and creates a **draft prerelease** with SHA-256 checksums only when every build and backend smoke check succeeds.
6. Review artifacts and known limitations before publishing the draft. Never claim platform UI validation solely from a successful package build.

The initial builds are unsigned and do not bundle FFmpeg. Do not market them as production-ready. Configure signing/notarization and licensed FFmpeg distribution before changing those claims. To test packaging without creating a release, dispatch the workflow manually.

Secrets belong in repository Actions secrets or local environment variables, never committed configuration. Keep runtime state, original footage, and credentials out of packages and Git. Historical sample state remains in old commits; history rewriting requires a separate coordinated migration.
