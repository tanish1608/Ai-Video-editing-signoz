"""
Agent 7: The Sound Engineer
────────────────────────────
Full audio mastering pipeline on the rendered video:

  1. **Single-pass noise removal + LUFS normalization** — FFmpeg afftdn
     adaptive filter chain → loudnorm in one command.
  2. **Background music** — Fetches vibe-matched music from Soundstripe via
     Gemini LLM analysis, downloads the track.
  3. **Music mixing with ducking** — Mixes background music at low volume
     (~0.35) with automatic ducking during speech segments (drops to ~0.15)
     so dialogue always cuts through cleanly.
  4. **Final master** — Outputs the polished video ready for export.

All FFmpeg commands run via ``asyncio.create_subprocess_exec`` so the
FastAPI event loop is never blocked (unlike the old ``subprocess.run``
calls which froze the entire server during mastering).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from kinetograph.config import settings
from kinetograph.core.compositor import run_ffmpeg_async
from kinetograph.core.media import probe_media_async
from kinetograph.core.music import (
    build_video_description,
    fetch_background_music,
    is_configured as soundstripe_configured,
)
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


# ─── Step 1: Single-Pass Noise Removal + LUFS Normalization ───────────────────

async def _denoise_and_normalize(
    input_path: str,
    output_path: str,
    target_lufs: float = -14.0,
) -> Path:
    """
    Remove background noise AND normalize loudness in a **single FFmpeg pass**.

    Old pipeline ran 3 passes: denoise → LUFS measure → LUFS apply.
    This consolidates into 1 pass (single-pass loudnorm is ~95% as accurate
    as two-pass and eliminates an entire re-encode).

    Filter chain:
      highpass → afftdn → EQ → anlmdn → gate → speechnorm → compressor → loudnorm
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    audio_filter = ",".join([
        # 1. Bandpass — kill sub-bass rumble and high-freq hiss
        "highpass=f=120:poles=2",
        "lowpass=f=9500",
        # 2. Adaptive FFT denoiser — removes steady-state hiss / hum
        "afftdn=nf=-20:tn=1:om=o",
        # 3. EQ cut at room-resonance frequencies (200–600 Hz)
        "equalizer=f=350:t=q:w=1.2:g=-4",
        # 4. Non-local-means denoiser — cleans residual echo smear
        "anlmdn=s=10:p=0.002:r=0.002:m=20",
        # 5. Noise gate — silence reverb tails between phrases
        "agate=threshold=0.03:ratio=4:attack=0.3:release=60:range=0.02",
        # 6. Speech normalizer — evens out sentence-level loudness
        "speechnorm=e=6:c=6:t=0.03:r=0.002:f=0.002",
        # 7. Compressor — tame remaining peaks
        "acompressor=threshold=0.089:ratio=4:attack=5:release=100:makeup=1",
        # 8. LUFS normalization (single-pass — avoids a second encode)
        f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
    ])

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", audio_filter,
        "-map", "0:v",
        "-map", "0:a",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        str(out),
    ]

    logger.info(
        "🔊 Sound Engineer: Single-pass denoise + LUFS "
        "(highpass → afftdn → EQ → anlmdn → gate → speechnorm → compressor → loudnorm)..."
    )
    await run_ffmpeg_async(cmd, timeout=600, description="Denoise+LUFS")
    return out


# ─── Step 3: Speech Window Detection ──────────────────────────────────────────

def _detect_speech_windows(
    approved_edit: dict,
    master_index: list[dict],
    merge_gap_ms: int = 300,
) -> list[tuple[float, float]]:
    """
    Extract speech windows mapped to the RENDERED video timeline.

    Remaps word timestamps from source files through the approved edit's
    clip ordering so that music ducking aligns with actual dialogue
    in the rendered output (not the original source-file times).
    """
    from kinetograph.core.captions import map_words_to_timeline

    timeline_words = map_words_to_timeline(approved_edit, master_index)

    if not timeline_words:
        return []

    raw_windows: list[tuple[int, int]] = []
    for w in timeline_words:
        s = w.get("start_ms", 0)
        e = w.get("end_ms", 0)
        if e > s:
            raw_windows.append((s, e))

    if not raw_windows:
        return []

    # Sort and merge overlapping / close-together windows
    raw_windows.sort()
    merged: list[tuple[int, int]] = [raw_windows[0]]

    for start, end in raw_windows[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + merge_gap_ms:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    # Convert to seconds
    return [(s / 1000.0, e / 1000.0) for s, e in merged]


# ─── Step 4: Music Mixing with Ducking ────────────────────────────────────────

def _build_ducking_volume_expr(
    speech_windows: list[tuple[float, float]],
    normal_vol: float = 0.9,
    ducked_vol: float = 0.75,
    fade_sec: float = 0.3,
) -> str:
    """
    Build an FFmpeg volume expression that ducks music during speech.

    The expression uses between() to detect speech regions and smoothly
    ramps the volume down/up with a fade envelope.

    Returns an FFmpeg -af filter string for the music input.
    """
    if not speech_windows:
        return f"volume={normal_vol}"

    # Build enable expressions for speech regions (with fade margins)
    parts = []
    for start_s, end_s in speech_windows:
        duck_start = max(0, start_s - fade_sec)
        duck_end = end_s + fade_sec
        parts.append(f"between(t,{duck_start:.3f},{duck_end:.3f})")

    speech_expr = "+".join(parts)

    # When speech active → ducked_vol, else → normal_vol
    # FFmpeg volume filter: volume='if(expr, ducked, normal)'
    vol_expr = f"volume='{ducked_vol}+({normal_vol}-{ducked_vol})*(1-min(1,{speech_expr}))':eval=frame"
    return vol_expr


async def _mix_music(
    video_path: str,
    music_path: str,
    output_path: str,
    speech_windows: list[tuple[float, float]],
    music_volume: float = 0.35,
    music_ducked_volume: float = 0.15,
) -> Path:
    """
    Mix background music into the video with automatic speech ducking.

    Uses FFmpeg's filter_complex (async) to:
    1. Take the video's audio as the primary track
    2. Load the music file, trim to video duration, apply ducking volume
    3. Mix both audio streams together
    4. Output with the original video track intact (stream-copied)
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    meta = await probe_media_async(video_path)
    video_duration_s = meta["duration_ms"] / 1000.0

    ducking_expr = _build_ducking_volume_expr(
        speech_windows,
        normal_vol=music_volume,
        ducked_vol=music_ducked_volume,
    )

    has_video_audio = meta.get("has_audio", False)

    if has_video_audio:
        # normalize=0 is essential: without it amix scales every input by 1/n
        # (0.5 here), halving the dialogue on top of the music ducking. With it,
        # dialogue [0:a] stays at unity and music sits at the ducking_expr level.
        filter_complex = (
            f"[1:a]aloop=loop=-1:size=2e+09,atrim=duration={video_duration_s:.3f},"
            f"asetpts=N/SR/TB,{ducking_expr}[music];"
            f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=3:normalize=0[out]"
        )
    else:
        logger.info("🔊 Sound Engineer: Video has no audio — using music only")
        filter_complex = (
            f"[1:a]aloop=loop=-1:size=2e+09,atrim=duration={video_duration_s:.3f},"
            f"asetpts=N/SR/TB,{ducking_expr}[out]"
        )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", music_path,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[out]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        str(out),
    ]

    logger.info(f"🔊 Sound Engineer: Mixing background music (vol={music_volume}, "
                f"ducked={music_ducked_volume}, speech_windows={len(speech_windows)})...")
    await run_ffmpeg_async(cmd, timeout=600, description="Music mix")
    return out


# ─── Intermediate file cleanup ─────────────────────────────────────────────────

def _cleanup_intermediates(work_dir: Path, keep: set[str] | None = None) -> None:
    """
    Remove intermediate files from the sound engineer work directory.

    Keeps files whose names are in *keep* (e.g. the final master).
    Removes _denoised, _normalized, _mixed temporaries + downloaded music.
    """
    keep = keep or set()
    suffixes = {".mp4", ".wav", ".mp3", ".aac", ".m4a"}
    removed = 0
    freed = 0

    for f in work_dir.iterdir():
        if f.is_file() and f.suffix.lower() in suffixes and f.name not in keep:
            try:
                size = f.stat().st_size
                f.unlink()
                removed += 1
                freed += size
            except OSError:
                pass

    if removed:
        logger.info(
            f"🔊 Sound Engineer: Cleaned up {removed} intermediate files "
            f"({freed / 1_048_576:.1f} MB freed)"
        )


# ─── Agent Entry Point ────────────────────────────────────────────────────────

async def sound_engineer_node(state: GraphState) -> dict:
    """
    LangGraph node — The Sound Engineer.

    Full audio mastering pipeline:
      1. Remove background noise (afftdn + bandpass)
      2. Normalize dialogue to −14 LUFS (two-pass loudnorm)
      3. Fetch background music from Soundstripe (LLM picks the vibe)
      4. Mix music at low volume with speech ducking
      5. Output the mastered video
    """
    logger.info("🔊 Sound Engineer: Starting audio mastering pipeline...")

    render_path = state.get("render_path")
    master_index = state.get("master_index", [])
    approved_edit = state.get("approved_edit")
    edit_instruction = state.get("edit_instruction")
    state_music_prompt = state.get("music_prompt")
    existing_music = state.get("music_path")

    if not render_path or not Path(render_path).exists():
        return {
            "phase": Phase.ERROR,
            "errors": [{
                "agent": "sound_engineer",
                "message": f"Rendered video not found: {render_path}",
                "phase": Phase.MASTERING,
                "recoverable": False,
            }],
        }

    try:
        render_p = Path(render_path)
        # Sanitize stem: colons are interpreted as protocol separators by FFmpeg
        safe_stem = re.sub(r'[^\w\s\-.]', '_', render_p.stem)
        work_dir = settings.state_dir / "sound_engineer"
        work_dir.mkdir(parents=True, exist_ok=True)

        current_path = render_path

        # Check if the rendered video actually has an audio stream
        render_meta = await probe_media_async(current_path)
        has_audio = render_meta.get("has_audio", False)

        # ── Step 1: Single-pass Denoise + LUFS ────────────────
        if has_audio:
            denoised_path = str(work_dir / f"{safe_stem}_denoised.mp4")
            try:
                await _denoise_and_normalize(current_path, denoised_path)
                current_path = denoised_path
                logger.info("🔊 Sound Engineer: ✓ Denoise + LUFS complete (single pass)")
            except Exception as exc:
                logger.warning(f"🔊 Sound Engineer: Denoise+LUFS failed ({exc}), continuing...")
        else:
            logger.warning("🔊 Sound Engineer: Input video has NO audio stream — skipping denoise & normalization")

        # ── Step 3: Fetch Background Music ─────────────────────
        music_path: str | None = None

        # Re-use existing music file if available (not cleared by edit agent)
        if existing_music and Path(existing_music).exists():
            music_path = existing_music
            logger.info(f"🔊 Sound Engineer: ♻️ Re-using existing music → {Path(existing_music).name}")
        elif soundstripe_configured():
            try:
                video_desc = build_video_description(approved_edit, master_index)
                # Append state-level music_prompt and edit_instruction for richer context
                if state_music_prompt:
                    video_desc = f"{video_desc}\nMUSIC HINT: {state_music_prompt}"
                if edit_instruction:
                    video_desc = f"{video_desc}\nUSER EDIT REQUEST: {edit_instruction}"

                video_meta = await probe_media_async(current_path)
                video_duration = video_meta["duration_ms"] / 1000.0

                # fetch_background_music does a blocking LLM call + HTTP search +
                # full WAV download — run it off the event loop.
                music_file = await asyncio.to_thread(
                    fetch_background_music,
                    video_description=video_desc,
                    output_dir=work_dir,
                    target_duration_sec=video_duration,
                )
                if music_file:
                    music_path = str(music_file)
                    logger.info(f"🔊 Sound Engineer: ✓ Background music fetched → {music_file.name}")
            except Exception as exc:
                logger.warning(f"🔊 Sound Engineer: Music fetch failed ({exc}), continuing without music...")
        else:
            logger.info("🔊 Sound Engineer: Soundstripe not configured — skipping background music")

        # ── Step 4: Mix Music with Ducking ─────────────────────
        if music_path:
            speech_windows = _detect_speech_windows(approved_edit or {}, master_index)
            mixed_path = str(work_dir / f"{safe_stem}_mixed.mp4")
            try:
                await _mix_music(
                    video_path=current_path,
                    music_path=music_path,
                    output_path=mixed_path,
                    speech_windows=speech_windows,
                    music_volume=0.35,        # 35% volume normally
                    music_ducked_volume=0.15,  # 15% during speech
                )
                current_path = mixed_path
                logger.info("🔊 Sound Engineer: ✓ Music mixed with speech ducking")
            except Exception as exc:
                logger.warning(f"🔊 Sound Engineer: Music mixing failed ({exc}), continuing without music...")

        # ── Step 5: Final Master ───────────────────────────────
        # Strip existing _mastered suffix to prevent _mastered_mastered
        stem = safe_stem
        if stem.endswith("_mastered"):
            stem = stem[:-len("_mastered")]
        mastered_path = render_p.parent / f"{stem}_mastered{render_p.suffix}"

        if current_path != str(mastered_path):
            # Copy/remux to final output location (async)
            cmd = [
                "ffmpeg", "-y",
                "-i", current_path,
                "-c", "copy",
                str(mastered_path),
            ]
            try:
                await run_ffmpeg_async(cmd, timeout=300, description="Final master (stream copy)")
            except RuntimeError:
                # If stream copy fails, re-encode (async)
                cmd = [
                    "ffmpeg", "-y",
                    "-i", current_path,
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                    "-c:a", "aac", "-b:a", "192k",
                    str(mastered_path),
                ]
                await run_ffmpeg_async(cmd, timeout=600, description="Final master (re-encode)")

        logger.info(f"🔊 Sound Engineer: ✓ Mastering complete → {mastered_path}")

        # ── Cleanup intermediate files to save disk ────────────
        keep_files = {mastered_path.name}
        if music_path:
            keep_files.add(Path(music_path).name)
        _cleanup_intermediates(work_dir, keep=keep_files)

        return_state: dict = {
            "phase": Phase.MASTERED,
            "render_path": str(mastered_path),
            "render_history": [str(mastered_path)],
        }
        if music_path:
            return_state["music_path"] = music_path

        return return_state

    except Exception as exc:
        logger.error(f"🔊 Sound Engineer: Mastering failed: {exc}")
        return {
            "phase": Phase.ERROR,
            "errors": [{
                "agent": "sound_engineer",
                "message": f"Audio mastering failed: {exc}",
                "phase": Phase.MASTERING,
                "recoverable": True,
            }],
        }
