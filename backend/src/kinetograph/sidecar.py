"""Packaged desktop entry point for the local Kinetograph API sidecar."""

from __future__ import annotations

import uvicorn

from kinetograph.config import settings


def main() -> None:
    """Run the local-only API server used by the Electron application."""
    uvicorn.run("kinetograph.server:app", host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
