"""Persist completed Archivist work by source identity and analysis settings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kinetograph.config import settings

CACHE_VERSION = 1  # Bump when transcription, visual prompts or segmentation change.


class AnalysisCache:
    def __init__(self, source: str):
        path = Path(source).resolve()
        stat = path.stat()
        self.identity = {
            "version": CACHE_VERSION,
            "source": str(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "stt_model": settings.elevenlabs_stt_model,
            "language": "en",
            "vlm_model": settings.vlm_model,
            "vlm_url": settings.vlm_base_url,
            "segment_sec": settings.vlm_segment_sec,
            "fps": settings.vlm_segment_fps,
            "max_frames": settings.vlm_max_frames,
        }
        self.key = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.path = settings.cache_dir / "analysis" / f"{self.key}.json"
        self.data: dict = {"identity": self.identity, "visuals": [], "complete": False}
        try:
            saved = json.loads(self.path.read_text())
            if (
                saved.get("identity") == self.identity
                and isinstance(saved.get("visuals"), list)
                and all(isinstance(v, dict) and "start_ms" in v for v in saved["visuals"])
            ):
                self.data = saved
        except (OSError, ValueError, AttributeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False))
        temporary.replace(self.path)

    def save_visual(self, result: dict):
        if result.get("analysis_failed"):
            return  # A provider failure must remain retryable.
        key = (result["start_ms"], result["end_ms"])
        self.data["visuals"] = [
            v for v in self.data["visuals"] if (v["start_ms"], v["end_ms"]) != key
        ] + [result]
        self.save()
