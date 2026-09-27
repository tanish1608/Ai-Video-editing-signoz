"""
Per-run logs — one folder per pipeline run, for analysing runs after the fact.

Layout (inside the active project)::

    logs/runs/<YYYYmmdd-HHMMSS>_<run_id[:12]>/
        run.json       summary: prompt, status, per-node timings, errors, models, key presence
        events.jsonl   timeline: one JSON object per pipeline event
        backend.log    every log record emitted while the run was executing

A run spans several background tasks (run → approval pause → resume), so the
folder is looked up by ``run_id`` and appended to. Secrets are redacted from
``backend.log`` before they hit disk.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from kinetograph import __version__
from kinetograph.config import settings

logger = logging.getLogger(__name__)

_SECRET_FIELDS = (
    "gemini_api_key",
    "elevenlabs_api_key",
    "nvidia_api_key",
    "pexels_api_key",
    "soundstripe_api_key",
    "hf_token",
    "kinetograph_api_token",
)

KEY_FIELDS = {
    "gemini": "gemini_api_key",
    "elevenlabs": "elevenlabs_api_key",
    "nvidia": "nvidia_api_key",
    "pexels": "pexels_api_key",
    "soundstripe": "soundstripe_api_key",
}


def key_status() -> dict[str, bool]:
    """Which API keys the backend can currently see (presence only, never values)."""
    return {name: bool(getattr(settings, attr, "")) for name, attr in KEY_FIELDS.items()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def runs_dir() -> Path:
    return settings.logs_dir / "runs"


def _find_dir(run_id: str) -> Path | None:
    root = runs_dir()
    if not root.is_dir():
        return None
    matches = sorted(root.glob(f"*_{run_id[:12]}"))
    return matches[-1] if matches else None


class _RedactingFormatter(logging.Formatter):
    """Replace any configured API key value (message or traceback) with ***."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for field_name in _SECRET_FIELDS:
            secret = getattr(settings, field_name, "") or ""
            if len(secret) >= 8:
                text = text.replace(secret, "***")
        return text


class RunLog:
    """Append-only log for one pipeline run (see module docstring)."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.dir = _find_dir(run_id) or (
            runs_dir() / f"{datetime.now():%Y%m%d-%H%M%S}_{run_id[:12]}"
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        self._summary_path = self.dir / "run.json"
        self._events_path = self.dir / "events.jsonl"
        self.summary: dict[str, Any] = self._load_summary()
        self._mark = time.monotonic()

    # ── summary ──────────────────────────────────────────────────────────

    def _load_summary(self) -> dict[str, Any]:
        try:
            return json.loads(self._summary_path.read_text())
        except (OSError, ValueError):
            return {"run_id": self.run_id, "nodes": [], "errors": []}

    def _save_summary(self) -> None:
        tmp = self._summary_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.summary, indent=2, default=str))
        tmp.replace(self._summary_path)

    def start(self, **meta: Any) -> None:
        """Record run metadata. Called once per background task (run, resume, edit)."""
        if "started_at" not in self.summary:
            self.summary.update(
                started_at=_now(),
                version=__version__,
                project_dir=str(settings._project_root),
                models={"gemini": settings.gemini_model, "vlm": settings.vlm_model},
                api_keys=key_status(),
            )
        self.summary.update({k: v for k, v in meta.items() if v is not None})
        self.summary["status"] = "running"
        self._mark = time.monotonic()
        self._save_summary()
        self.event("task_started", **meta)

    def event(self, kind: str, **data: Any) -> None:
        line = {"ts": _now(), "event": kind, **data}
        with open(self._events_path, "a") as f:
            f.write(json.dumps(line, default=str) + "\n")

    def node_done(self, node: str, phase: str, errors: list | None = None) -> None:
        """Record a node completion; duration is time since the previous node finished."""
        now = time.monotonic()
        duration = round(now - self._mark, 2)
        self._mark = now
        errors = errors or []
        self.summary["nodes"].append(
            {"node": node, "phase": phase, "duration_s": duration, "finished_at": _now()}
        )
        self.summary["errors"].extend(errors)
        self._save_summary()
        self.event("node_done", node=node, phase=phase, duration_s=duration, errors=errors)

    def finish(self, status: str, **extra: Any) -> None:
        """Close out this task. ``status``: complete | error | cancelled | awaiting_approval."""
        self.summary["status"] = status
        self.summary.update({k: v for k, v in extra.items() if v is not None})
        if status != "awaiting_approval":
            self.summary["ended_at"] = _now()
            try:
                start = datetime.fromisoformat(self.summary["started_at"])
                self.summary["duration_s"] = round(
                    (datetime.now(timezone.utc) - start).total_seconds(), 2
                )
            except (KeyError, ValueError):
                pass
        self._save_summary()
        self.event("task_finished", status=status, **extra)

    # ── log capture ─────────────────────────────────────────────────────

    @contextmanager
    def capture(self) -> Iterator[None]:
        """Tee every log record (kinetograph INFO+, libraries WARNING+) into backend.log."""
        handler = logging.FileHandler(self.dir / "backend.log", encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            _RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        pkg = logging.getLogger("kinetograph")
        old_level = pkg.level
        if pkg.getEffectiveLevel() > logging.INFO:
            pkg.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            yield
        finally:
            root.removeHandler(handler)
            pkg.setLevel(old_level)
            handler.close()


# ── Reading runs back ────────────────────────────────────────────────────────


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Run summaries for the active project, newest first."""
    root = runs_dir()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        try:
            summary = json.loads((d / "run.json").read_text())
        except (OSError, ValueError):
            continue
        summary["log_dir"] = str(d)
        out.append(summary)
        if len(out) >= limit:
            break
    return out


def read_run(run_id: str, tail: int = 200) -> dict[str, Any] | None:
    """Summary + events + the last ``tail`` lines of backend.log for one run."""
    d = _find_dir(run_id)
    if d is None:
        return None
    try:
        summary = json.loads((d / "run.json").read_text())
    except (OSError, ValueError):
        summary = {"run_id": run_id}
    events = []
    try:
        for line in (d / "events.jsonl").read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    try:
        log_lines = (d / "backend.log").read_text(errors="replace").splitlines()[-tail:]
    except OSError:
        log_lines = []
    return {**summary, "log_dir": str(d), "events": events, "log_tail": log_lines}
