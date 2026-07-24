"""
Media utilities — FFmpeg wrappers for extraction, normalization, and probing.

Functions come in two flavours:

  • **Synchronous** (e.g. ``normalize_clip``) — run inside ``ProcessPoolExecutor``
    workers during clip normalization.  Must stay synchronous because
    ``concurrent.futures`` workers can't run coroutines.
  • **Async** (e.g. ``extract_audio_async``) — for I/O-bound calls made from
    LangGraph agent nodes (Archivist, Sound Engineer).  Use
    ``asyncio.create_subprocess_exec`` so the FastAPI event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

from kinetograph.config import settings

logger = logging.getLogger(__name__)

# Supported media extensions
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}


def probe_media(file_path: str | Path) -> dict:
    """
    Probe a media file with ffprobe and return metadata.

    Returns dict with keys: duration_ms, width, height, fps, has_audio, codec, is_image.
    For images: duration_ms defaults to 5000 (5s), fps=0, has_audio=False.
    Raises RuntimeError on corrupt/unreadable files.
    """
    file_path = str(file_path)
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed for {file_path}: {result.stderr}")

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format", {})

        # Use .get() — some streams (data/attachment tracks) omit codec_type.
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        fps = 0.0
        width = 0
        height = 0
        if video_stream:
            width = int(video_stream.get("width", 0))
            height = int(video_stream.get("height", 0))
            # Parse r_frame_rate "30/1" or "30000/1001"
            r_fps = video_stream.get("r_frame_rate", "0/1")
            try:
                num, den = r_fps.split("/")
                fps = float(num) / float(den) if float(den) != 0 else 0.0
            except (ValueError, ZeroDivisionError):
                fps = 0.0

        # Many containers (mkv/webm, some mp4) don't carry format.duration —
        # fall back to per-stream duration (or nb_frames / fps) so real videos
        # aren't mistaken for stills.
        duration_s = _resolve_duration_s(fmt, video_stream, audio_stream, fps)

        suffix = Path(file_path).suffix.lower()
        is_image = suffix in IMAGE_EXTENSIONS or (video_stream is not None and duration_s <= 0)

        return {
            "duration_ms": int(duration_s * 1000) if duration_s > 0 else 5000,
            "width": width,
            "height": height,
            "fps": round(fps, 2),
            "has_audio": audio_stream is not None,
            "codec": video_stream.get("codec_name", "") if video_stream else "",
            "is_image": is_image,
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise RuntimeError(f"Failed to probe {file_path}: {exc}") from exc


def _resolve_duration_s(fmt: dict, video_stream: dict | None,
                        audio_stream: dict | None, fps: float) -> float:
    """Best-effort media duration in seconds.

    Tries format.duration, then each stream's duration, then nb_frames/fps for
    the video stream. Returns 0.0 when nothing usable is found (e.g. images).
    """
    def _as_float(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    d = _as_float(fmt.get("duration"))
    if d > 0:
        return d
    for stream in (video_stream, audio_stream):
        if stream:
            d = _as_float(stream.get("duration"))
            if d > 0:
                return d
    # Derive from frame count when duration metadata is missing entirely.
    if video_stream and fps > 0:
        nb = _as_float(video_stream.get("nb_frames"))
        if nb > 0:
            return nb / fps
    return 0.0


def extract_audio(video_path: str | Path, output_path: str | Path) -> Path:
    """
    Extract audio track from a video file as 16kHz mono WAV (optimal for STT).

    Returns the output path.
    """
    video_path = str(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",                    # no video
            "-acodec", "pcm_s16le",   # 16-bit PCM
            "-ar", "16000",           # 16 kHz
            "-ac", "1",               # mono
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {result.stderr[:500]}")
    return output_path


def extract_keyframes(
    video_path: str | Path,
    output_dir: str | Path,
    interval_sec: int | None = None,
) -> list[dict]:
    """
    Extract keyframes from a video at the given interval.

    Returns a list of {"timestamp_ms": int, "frame_path": str}.
    For videos longer than 1 hour, automatically increases interval to 3s.
    """
    video_path = str(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Probe duration to decide sampling rate
    meta = probe_media(video_path)
    duration_ms = meta["duration_ms"]

    if interval_sec is None:
        interval_sec = settings.keyframe_interval
        # Edge case: very long footage → sample less frequently
        if duration_ms > 3_600_000:  # > 1 hour
            interval_sec = max(interval_sec, 3)

    # Use ffmpeg to extract frames
    stem = Path(video_path).stem
    pattern = str(output_dir / f"{stem}_frame_%06d.jpg")

    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", f"fps=1/{interval_sec}",
            "-q:v", "2",             # JPEG quality
            "-frames:v", "500",      # safety cap: max 500 frames
            pattern,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Keyframe extraction failed: {result.stderr[:500]}")

    # Collect frame paths with timestamps
    frames = sorted(output_dir.glob(f"{stem}_frame_*.jpg"))
    keyframes = []
    for i, frame_path in enumerate(frames):
        ts_ms = i * interval_sec * 1000
        if ts_ms > duration_ms:
            break
        keyframes.append({
            "timestamp_ms": ts_ms,
            "frame_path": str(frame_path),
        })

    return keyframes


def _detect_shot_boundaries(video_path: str, duration_sec: float) -> list[float]:
    """Return sorted shot-cut timestamps (seconds) via ffmpeg scene detection.

    Uses the ``select='gt(scene,…)'`` + ``showinfo`` trick and parses
    ``pts_time`` from stderr. Best-effort: returns ``[]`` on any failure (caller
    falls back to uniform time-slicing).
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", video_path,
                "-filter:v", "select='gt(scene,0.3)',showinfo",
                "-f", "null", "-",
            ],
            capture_output=True, text=True,
            timeout=min(120, max(30, int(duration_sec))),
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    cuts: list[float] = []
    for m in re.finditer(r"pts_time:([0-9]+\.?[0-9]*)", result.stderr):
        try:
            cuts.append(float(m.group(1)))
        except ValueError:
            continue
    return sorted(t for t in cuts if 0.0 < t < duration_sec)


def _shot_windows(duration_sec: float, cuts: list[float],
                  target_sec: float, min_sec: float = 1.2) -> list[tuple[float, float]]:
    """Turn shot-cut points into [start, end) windows for VLM analysis.

    Windows never straddle a shot boundary and never exceed *target_sec* (a long
    take is subdivided). Windows shorter than *min_sec* are merged forward so a
    flurry of quick cuts doesn't spawn dozens of frame-starved calls.
    """
    # Boundaries = 0, each cut, and the end — de-duplicated + sorted.
    bounds = sorted({0.0, duration_sec, *[c for c in cuts if 0 < c < duration_sec]})
    windows: list[tuple[float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        # Subdivide a long shot into ≤ target_sec chunks.
        start = a
        while b - start > target_sec + 0.01:
            windows.append((start, start + target_sec))
            start += target_sec
        if b - start >= 0.5:
            windows.append((start, b))

    # Merge tiny windows forward.
    merged: list[tuple[float, float]] = []
    for w in windows:
        if merged and (w[1] - merged[-1][0]) <= target_sec and (w[0] - merged[-1][0]) < min_sec:
            merged[-1] = (merged[-1][0], w[1])
        else:
            merged.append(w)
    return merged


def extract_video_segments(
    video_path: str | Path,
    output_dir: str | Path,
    segment_sec: float | None = None,
    fps: float | None = None,
    max_frames: int | None = None,
) -> list[dict]:
    """
    Split a video into temporal segments and extract frames from each.

    Instead of 1 keyframe per second (old approach), this groups the video
    into *segment_sec*-long windows and pulls *max_frames* frames at *fps*
    from each window.  This lets a video-capable VLM see motion and temporal
    context per segment rather than isolated stills.

    Returns a list of segments::

        [
            {
                "segment_index": 0,
                "start_ms": 0,
                "end_ms": 4000,
                "frame_paths": ["/.../seg00_frame_0001.jpg", ...],
            },
            ...
        ]
    """
    video_path = str(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    segment_sec = segment_sec or settings.vlm_segment_sec
    fps = fps or settings.vlm_segment_fps
    max_frames = max_frames or settings.vlm_max_frames

    meta = probe_media(video_path)
    duration_ms = meta["duration_ms"]
    duration_sec = duration_ms / 1000.0

    # Shot-boundary-aware windows: detect cuts, then build windows that never
    # straddle a cut (so one VLM call = one coherent shot) and never exceed
    # segment_sec. Falls back to uniform slicing if detection finds nothing.
    cuts = _detect_shot_boundaries(video_path, duration_sec)
    windows = _shot_windows(duration_sec, cuts, target_sec=segment_sec)
    if not windows:
        windows = [
            (t, min(t + segment_sec, duration_sec))
            for t in _frange(0.0, duration_sec, segment_sec)
        ]
    logger.info(
        "🗄️  Segmentation: %d shot cuts → %d windows (%s)",
        len(cuts), len(windows), "shot-aware" if cuts else "uniform-fallback",
    )

    segments: list[dict] = []
    for seg_idx, (seg_start, seg_end) in enumerate(windows):
        seg_dur = seg_end - seg_start
        if seg_dur < 0.5:  # skip tiny trailing windows
            continue

        seg_dir = output_dir / f"seg{seg_idx:03d}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(seg_dir / "frame_%04d.jpg")

        # Extract frames from [seg_start, seg_end] at the desired FPS.
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{seg_start:.3f}",
            "-i", video_path,
            "-t", f"{seg_dur:.3f}",
            "-vf", f"fps={fps}",
            "-frames:v", str(max_frames),
            "-q:v", "2",
            pattern,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            continue  # non-fatal — skip this window

        frame_paths = sorted(str(p) for p in seg_dir.glob("frame_*.jpg"))
        if not frame_paths:
            continue

        segments.append({
            "segment_index": seg_idx,
            "start_ms": int(seg_start * 1000),
            "end_ms": int(seg_end * 1000),
            "frame_paths": frame_paths,
        })

    return segments


def _frange(start: float, stop: float, step: float):
    """float range generator (for uniform-slice fallback)."""
    t = start
    while t < stop:
        yield t
        t += step


def normalize_image_to_video(
    input_path: str | Path,
    output_path: str | Path,
    duration_s: float = 5.0,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    audio_rate: int | None = None,
) -> Path:
    """
    Convert a still image to a video clip of the given duration.

    Uses FFmpeg loop + scale + pad to produce a video identical in format to
    normalize_clip output, so the Director can treat it like any other clip.
    """
    width = width or settings.output_width
    height = height or settings.output_height
    fps = fps or settings.output_fps
    audio_rate = audio_rate or settings.output_audio_rate

    input_path = str(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", input_path,
        "-f", "lavfi", "-i", f"anullsrc=r={audio_rate}:cl=stereo",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={fps}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", str(audio_rate),
        "-ac", "2",
        "-t", str(duration_s),
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"Image-to-video failed for {input_path}: {result.stderr[:500]}")

    return output_path


def normalize_clip(
    input_path: str | Path,
    output_path: str | Path,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    audio_rate: int | None = None,
    color_grade: dict | None = None,
    crf: int | None = None,
) -> Path:
    """
    Transcode a clip to the canonical project format.

    Handles codec mismatch, resolution/FPS normalization, audio sync,
    and optional colour grading via FFmpeg eq + colorbalance filters.
    """
    width = width or settings.output_width
    height = height or settings.output_height
    fps = fps or settings.output_fps
    audio_rate = audio_rate or settings.output_audio_rate

    input_path = str(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if input has audio
    meta = probe_media(input_path)
    has_audio = meta["has_audio"]

    # Build video filter chain
    vf_parts = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
        f"fps={fps}",
    ]

    # Append colour-grading filters if provided and non-neutral
    if color_grade:
        vf_parts.extend(_build_color_grade_filters(color_grade))

    vf = ",".join(vf_parts)

    # Build command — inputs first, then filters, then codecs
    cmd = ["ffmpeg", "-y", "-i", input_path]

    if not has_audio:
        # Add a silent audio source as second input — MUST come before filters
        cmd.extend(["-f", "lavfi", "-i", f"anullsrc=r={audio_rate}:cl=stereo"])

    cmd.extend([
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", str(crf or 18),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", str(audio_rate),
        "-ac", "2",
    ])

    if not has_audio:
        cmd.extend(["-shortest"])

    cmd.append(str(output_path))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Normalization failed for {input_path}: {result.stderr[:500]}")

    return output_path


def _build_color_grade_filters(cg: dict) -> list[str]:
    """
    Convert a colour-grade dict into FFmpeg filter strings.

    Uses the ``eq`` filter for brightness/contrast/saturation/gamma
    and ``colorbalance`` for temperature/tint/shadows/highlights.
    Returns an empty list if all values are neutral.
    """
    filters: list[str] = []

    # ── eq filter (brightness, contrast, saturation, gamma) ──
    brightness = cg.get("brightness", 0.0)
    contrast = cg.get("contrast", 1.0)
    saturation = cg.get("saturation", 1.0)
    gamma = cg.get("gamma", 1.0)

    eq_needs = (
        abs(brightness) > 0.001
        or abs(contrast - 1.0) > 0.001
        or abs(saturation - 1.0) > 0.001
        or abs(gamma - 1.0) > 0.001
    )
    if eq_needs:
        filters.append(
            f"eq=brightness={brightness:.3f}:contrast={contrast:.3f}"
            f":saturation={saturation:.3f}:gamma={gamma:.3f}"
        )

    # ── colorbalance filter (temperature, tint, shadows, highlights) ──
    temperature = cg.get("temperature", 0.0)
    tint = cg.get("tint", 0.0)
    shadows = cg.get("shadows", 0.0)
    highlights = cg.get("highlights", 0.0)

    cb_needs = (
        abs(temperature) > 0.001
        or abs(tint) > 0.001
        or abs(shadows) > 0.001
        or abs(highlights) > 0.001
    )
    if cb_needs:
        # Temperature: positive = warm (add red shadows, blue highlights inverted)
        # Tint: positive = magenta (add red + blue midtones)
        rs = shadows * 0.3 + temperature * 0.15
        gs = shadows * 0.3 - tint * 0.15
        bs = shadows * 0.3 - temperature * 0.15

        rh = highlights * 0.3 - temperature * 0.1
        gh = highlights * 0.3 - tint * 0.1
        bh = highlights * 0.3 + temperature * 0.1

        rm = temperature * 0.2 + tint * 0.1
        gm = -tint * 0.2
        bm = -temperature * 0.2 + tint * 0.1

        filters.append(
            f"colorbalance="
            f"rs={rs:.3f}:gs={gs:.3f}:bs={bs:.3f}:"
            f"rm={rm:.3f}:gm={gm:.3f}:bm={bm:.3f}:"
            f"rh={rh:.3f}:gh={gh:.3f}:bh={bh:.3f}"
        )

    return filters


def normalize_audio_lufs(
    input_path: str | Path,
    output_path: str | Path,
    target_lufs: float = -14.0,
    output_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> Path:
    """
    Normalize audio loudness to target LUFS using FFmpeg's loudnorm filter.

    Two-pass: first measure, then apply with linear correction.
    Falls back to single-pass if measurement parsing fails.

    Args:
        output_codec: Audio codec for the output (e.g. "aac", "pcm_s16le").
        audio_bitrate: Bitrate for lossy codecs (ignored for pcm).
    """
    input_path = str(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build audio codec args
    codec_args = ["-c:a", output_codec]
    if output_codec not in ("pcm_s16le", "pcm_s24le", "flac"):
        codec_args += ["-b:a", audio_bitrate]

    # Pass 1: Measure
    measure_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    result = subprocess.run(measure_cmd, capture_output=True, text=True, timeout=300)

    # Parse the loudnorm stats from stderr
    stderr = result.stderr
    json_start = stderr.rfind("{")
    json_end = stderr.rfind("}") + 1

    if json_start == -1 or json_end <= 0:
        # Fallback: single-pass normalization (less precise but functional)
        logger.warning("LUFS measurement parse failed — falling back to single-pass")
        fallback_cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
            "-c:v", "copy",
            *codec_args,
            str(output_path),
        ]
        result = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"LUFS normalization failed: {result.stderr[:500]}")
        return output_path

    stats = json.loads(stderr[json_start:json_end])

    # Pass 2: Apply with measured values (linear mode for clean correction)
    apply_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", (
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
            f"measured_I={stats['input_i']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:"
            f"linear=true:print_format=summary"
        ),
        "-c:v", "copy",
        *codec_args,
        str(output_path),
    ]
    result = subprocess.run(apply_cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"LUFS normalization failed: {result.stderr[:500]}")

    return output_path


# ─── Async Wrappers ──────────────────────────────────────────────────────────
# For agent nodes that run on the async event loop, these wrappers avoid
# blocking.  Heavy CPU-bound work (normalize_clip, normalize_image_to_video)
# stays synchronous because it runs in ProcessPoolExecutor workers.

async def probe_media_async(file_path: str | Path) -> dict:
    """Async version of :func:`probe_media`."""
    return await asyncio.to_thread(probe_media, file_path)


async def extract_audio_async(
    video_path: str | Path, output_path: str | Path,
) -> Path:
    """Async version of :func:`extract_audio`."""
    return await asyncio.to_thread(extract_audio, video_path, output_path)


async def extract_keyframes_async(
    video_path: str | Path,
    output_dir: str | Path,
    interval_sec: int | None = None,
) -> list[dict]:
    """Async version of :func:`extract_keyframes`."""
    return await asyncio.to_thread(extract_keyframes, video_path, output_dir, interval_sec)


async def extract_video_segments_async(
    video_path: str | Path,
    output_dir: str | Path,
    segment_sec: float | None = None,
    fps: float | None = None,
    max_frames: int | None = None,
) -> list[dict]:
    """Async version of :func:`extract_video_segments`."""
    return await asyncio.to_thread(
        extract_video_segments, video_path, output_dir, segment_sec, fps, max_frames,
    )


async def normalize_audio_lufs_async(
    input_path: str | Path,
    output_path: str | Path,
    target_lufs: float = -14.0,
    output_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> Path:
    """Async version of :func:`normalize_audio_lufs`."""
    return await asyncio.to_thread(
        normalize_audio_lufs, input_path, output_path, target_lufs, output_codec, audio_bitrate,
    )
