"""
Hardware-accelerated encoding detection.

Probes the system FFmpeg install once at import time and exposes the best
available H.264 encoder + decode flags.  Falls back to libx264 software.

Enterprise NLEs (Premiere, Resolve) always prefer hardware encoding.
On Apple Silicon `h264_videotoolbox` is 5-10× faster than libx264.
On NVIDIA GPUs `h264_nvenc` is 3-8× faster.

Usage::

    from kinetograph.core.hwaccel import hw

    # In any FFmpeg command, replace "-c:v libx264" with:
    cmd = ["ffmpeg", "-y", *hw.decode_flags, "-i", input_path,
           "-c:v", hw.encoder, *hw.encode_flags, ...]
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Ordered by preference: fastest / best quality first.
_ENCODER_PROBES: list[dict] = [
    {
        "name": "h264_videotoolbox",
        "os": "darwin",
        "encode_flags": ["-q:v", "65"],           # VT quality (0-100, 65 ≈ CRF 18)
        "decode_flags": ["-hwaccel", "videotoolbox"],
    },
    {
        "name": "h264_nvenc",
        "os": None,                                 # any OS with NVIDIA GPU
        "encode_flags": ["-preset", "p4", "-rc", "vbr", "-cq", "20", "-b:v", "0"],
        "decode_flags": ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"],
    },
    {
        "name": "h264_qsv",
        "os": None,
        "encode_flags": ["-preset", "medium", "-global_quality", "20"],
        "decode_flags": ["-hwaccel", "qsv"],
    },
    {
        "name": "h264_vaapi",
        "os": "linux",
        "encode_flags": ["-qp", "20"],
        "decode_flags": ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"],
    },
]


@dataclass(frozen=True)
class HWAccelProfile:
    """Immutable hardware-acceleration profile for the current system."""

    encoder: str = "libx264"
    encode_flags: tuple[str, ...] = ("-preset", "fast", "-crf", "18")
    decode_flags: tuple[str, ...] = ()
    is_hardware: bool = False
    # Software-only fallback flags (always available)
    sw_encoder: str = "libx264"
    sw_encode_flags: tuple[str, ...] = ("-preset", "fast", "-crf", "18")

    def video_codec_flags(self, *, quality: str = "normal") -> list[str]:
        """Return ``-c:v <encoder> <flags>`` ready to splice into a command.

        *quality* can be ``"normal"`` (default) or ``"fast"`` (preview/proxy).
        """
        if quality == "fast" and self.is_hardware:
            # Hardware encoder in fast mode — lower quality, higher speed
            return ["-c:v", self.encoder, *self.encode_flags]
        if quality == "fast":
            return ["-c:v", self.sw_encoder, "-preset", "ultrafast", "-crf", "28"]
        return ["-c:v", self.encoder, *self.encode_flags]

    def sw_video_codec_flags(self) -> list[str]:
        """Always return software encoder flags (for normalization in workers)."""
        return ["-c:v", self.sw_encoder, *self.sw_encode_flags]


# ─── Detection ──────────────────────────────────────────────────────────────────

def _detect_available_encoders() -> set[str]:
    """Query FFmpeg for available H.264 encoders."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        encoders: set[str] = set()
        for line in result.stdout.splitlines():
            if "h264" not in line.lower() or line.startswith("---"):
                continue
            parts = line.split()
            # Encoder lines look like " V..... h264_nvenc  NVIDIA NVENC ..."
            # A blank/short line must not raise IndexError.
            if len(parts) >= 2:
                encoders.add(parts[1])
        return encoders
    except Exception as exc:
        logger.warning(f"⚡ HWAccel: Could not query FFmpeg encoders: {exc}")
        return set()


def _test_encoder(encoder_name: str) -> bool:
    """Smoke-test an encoder with a 1-frame black video to confirm it actually works."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.04",
                "-c:v", encoder_name, "-frames:v", "1",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _probe() -> HWAccelProfile:
    """Detect the best available hardware encoder."""
    import sys

    available = _detect_available_encoders()
    current_os = sys.platform  # "darwin", "linux", "win32"

    for probe in _ENCODER_PROBES:
        name = probe["name"]
        if name not in available:
            continue
        if probe["os"] and probe["os"] != current_os:
            continue
        if _test_encoder(name):
            profile = HWAccelProfile(
                encoder=name,
                encode_flags=tuple(probe["encode_flags"]),
                decode_flags=tuple(probe.get("decode_flags", ())),
                is_hardware=True,
            )
            logger.info(f"⚡ HWAccel: Using hardware encoder → {name}")
            return profile

    logger.info("⚡ HWAccel: No hardware encoder available → libx264 software")
    return HWAccelProfile()


# ─── Singleton ──────────────────────────────────────────────────────────────────

hw: HWAccelProfile = _probe()
"""Global hardware-acceleration profile.  Import this everywhere::

    from kinetograph.core.hwaccel import hw
"""
