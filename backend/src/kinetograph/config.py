"""
Core configuration — loaded from .env and environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = (
    Path(__file__).resolve().parents[3]
)  # repo root: backend/src/kinetograph/config.py → src → backend → repo root
_ENV_FILE = os.environ.get("KINETOGRAPH_ENV_FILE", str(_ROOT / ".env"))


class Settings(BaseSettings):
    """All runtime configuration — populated from .env at the repository root."""

    model_config = SettingsConfigDict(
        # The packaged desktop app keeps its editable settings in its user-data
        # directory.  Development continues to use the repository .env file.
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    # ── AI / LLM Keys ──────────────────────────────────
    gemini_api_key: str = ""
    hf_token: str = ""
    elevenlabs_api_key: str = ""
    nvidia_api_key: str = ""

    # ── Soundstripe (background music) ─────────────────
    soundstripe_api_key: str = ""

    # ── Stock Footage ──────────────────────────────────
    pexels_api_key: str = ""

    # ── Strip whitespace from API keys (.env values may have leading spaces) ──
    @field_validator(
        "gemini_api_key",
        "hf_token",
        "elevenlabs_api_key",
        "nvidia_api_key",
        "soundstripe_api_key",
        "pexels_api_key",
        mode="before",
    )
    @classmethod
    def _strip_api_keys(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    # ── Model Configuration ────────────────────────────
    gemini_model: str = "gemini-3.8-flash"
    vlm_model: str = "nvidia/nemotron-nano-12b-v2-vl"
    vlm_base_url: str = "https://integrate.api.nvidia.com"
    elevenlabs_stt_model: str = "scribe_v2"

    @field_validator("gemini_model")
    @classmethod
    def _migrate_retired_model(cls, value: str) -> str:
        if value == "gemini-2.5-flash-preview-05-20":
            return "gemini-3.8-flash"
        return value

    # ── VLM Pipeline Tuning ────────────────────────────
    vlm_concurrency: int = Field(5, ge=1, le=16)
    archivist_asset_concurrency: int = Field(2, ge=1, le=8)
    archivist_stt_concurrency: int = Field(2, ge=1, le=8)
    vlm_segment_sec: float = 4.0  # seconds per video segment
    vlm_segment_fps: float = 2.0  # frame extraction rate (model spec)
    vlm_max_frames: int = 8  # max frames per segment (model min)

    # ── Media Settings ─────────────────────────────────
    output_orientation: str = "portrait"  # "portrait" (9:16) or "landscape" (16:9)
    output_width_override: Optional[int] = Field(None, validation_alias="OUTPUT_WIDTH", gt=0)
    output_height_override: Optional[int] = Field(None, validation_alias="OUTPUT_HEIGHT", gt=0)
    output_fps: int = 30
    output_audio_rate: int = 48000
    keyframe_interval: int = 1  # seconds

    @property
    def output_width(self) -> int:
        """Derive width from orientation, or use custom override."""
        if self.output_width_override:
            return self.output_width_override
        if "_custom_width" in self.__dict__:
            return self.__dict__["_custom_width"]
        return 1080 if self.output_orientation == "portrait" else 1920

    @property
    def output_height(self) -> int:
        """Derive height from orientation, or use custom override."""
        if self.output_height_override:
            return self.output_height_override
        if "_custom_height" in self.__dict__:
            return self.__dict__["_custom_height"]
        return 1920 if self.output_orientation == "portrait" else 1080

    # ── Server ─────────────────────────────────────────
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    kinetograph_api_token: str = ""

    # ── Project Directory ──────────────────────────────
    # Set by Electron via KINETOGRAPH_PROJECT_DIR env var when a user
    # opens/creates a project. When unset, falls back to repo root paths.
    kinetograph_project_dir: Optional[str] = None

    # ── Paths (derived) ───────────────────────────────
    @property
    def _project_root(self) -> Path:
        """Active project root — either the user's project dir or repo root."""
        if self.kinetograph_project_dir:
            return Path(self.kinetograph_project_dir).expanduser().resolve()
        return _ROOT

    @property
    def root_dir(self) -> Path:
        return _ROOT

    @property
    def media_dir(self) -> Path:
        """Single flat media directory — all user-imported clips live here."""
        return self._project_root / "media"

    @property
    def synth_cache_dir(self) -> Path:
        """Hidden cache for Pexels stock downloads (auto-managed by Synthesizer)."""
        return self._project_root / "media" / ".synth"

    @property
    def output_dir(self) -> Path:
        return self._project_root / "output"

    @property
    def state_dir(self) -> Path:
        return self._project_root / "state"

    @property
    def logs_dir(self) -> Path:
        """Per-run logs (see kinetograph.runlog)."""
        return self._project_root / "logs"

    def reload_secrets(self) -> None:
        """Re-read API keys and model names from the env file.

        The desktop Settings page rewrites the env file on Save; without this the
        running backend would keep the keys it loaded at startup until restarted.
        Runtime-mutated fields (project dir, output size) are left untouched.
        """
        from dotenv import dotenv_values

        names = (
            "gemini_api_key",
            "hf_token",
            "elevenlabs_api_key",
            "nvidia_api_key",
            "soundstripe_api_key",
            "pexels_api_key",
            "gemini_model",
            "vlm_model",
            "vlm_base_url",
        )
        # Saved desktop settings are authoritative on reload, including when a
        # launcher inherited an older nonempty key. Never mutate os.environ.
        values = dotenv_values(self.model_config.get("env_file"))
        overrides = {
            name: values[name.upper()] for name in names if (values.get(name.upper()) or "").strip()
        }
        fresh = Settings(**overrides)
        for name in names:
            setattr(self, name, getattr(fresh, name))

    # ── Media Cache (Adobe-style persistent cache) ────────────────
    @property
    def cache_dir(self) -> Path:
        """Top-level media cache — thumbnails, waveforms, probe metadata.

        Analogous to Adobe Premiere Pro's *Media Cache* folder.
        Safe to delete at any time; everything is regenerated on demand.
        """
        return self._project_root / ".cache"

    @property
    def thumbnail_cache_dir(self) -> Path:
        """Disk-cached JPEG thumbnails keyed by <asset_id>_<timestamp>.jpg."""
        return self.cache_dir / "thumbnails"

    @property
    def waveform_cache_dir(self) -> Path:
        """Disk-cached PNG waveform images keyed by <asset_id>_<w>x<h>.png."""
        return self.cache_dir / "waveforms"

    @property
    def metadata_cache_dir(self) -> Path:
        """Disk-cached ffprobe JSON keyed by <asset_id>_<mtime>_<size>.json."""
        return self.cache_dir / "metadata"

    @property
    def conformed_audio_dir(self) -> Path:
        """Conformed 16 kHz mono WAVs (like Adobe's conformed audio cache)."""
        return self.cache_dir / "audio"

    @property
    def project_manifest_path(self) -> Path:
        """Single project manifest — media references, pipeline state, settings."""
        return self.state_dir / "project.json"

    @property
    def media_refs_path(self) -> Path:
        """JSON map of asset_id → original source path (reference-based import)."""
        return self.state_dir / "media_refs.json"


# Singleton — importable everywhere
settings = Settings()
