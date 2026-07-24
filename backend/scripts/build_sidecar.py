"""Build the self-contained Python sidecar consumed by Electron Builder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = BACKEND_ROOT / "src" / "kinetograph" / "sidecar.py"


def main() -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "kinetograph-server",
        "--paths",
        str(BACKEND_ROOT / "src"),
        "--collect-submodules",
        "kinetograph",
        str(ENTRYPOINT),
    ]
    subprocess.run(command, cwd=BACKEND_ROOT, check=True)


if __name__ == "__main__":
    main()
