"""Normalize original audio, generate a score/effects, and retain a caption-free master.

All FFmpeg operations run asynchronously. Caption edits skip this stage entirely.
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
)
from kinetograph.core.music import (
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
    """Normalize loudness without destructive gates, fixed EQ or stacked denoisers."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Preserve the source timbre; aggressive gates/EQ damaged quiet speech and ambience.
    audio_filter = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-af",
        audio_filter,
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        str(out),
    ]

    logger.info("Sound Engineer: Normalize source audio while preserving timbre")
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
        ramp = max(fade_sec, 0.01)
        parts.append(
            f"min(clip((t-{duck_start:.3f})/{ramp:.3f},0,1),"
            f"clip(({duck_end:.3f}-t)/{ramp:.3f},0,1))"
        )

    speech_expr = "+".join(parts)

    # When speech active → ducked_vol, else → normal_vol
    # FFmpeg volume filter: volume='if(expr, ducked, normal)'
    vol_expr = (
        f"volume='{ducked_vol}+({normal_vol}-{ducked_vol})*(1-min(1,{speech_expr}))':eval=frame"
    )
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
            f"[1:a]atrim=duration={video_duration_s:.3f},"
            f"asetpts=N/SR/TB,loudnorm=I=-18:TP=-2:LRA=11,{ducking_expr},"
            f"afade=t=in:d=0.25,afade=t=out:st={max(0, video_duration_s - 1):.3f}:d=1[music];"
            f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=3:normalize=0,alimiter=limit=0.891:level=false:latency=true[out]"
        )
    else:
        logger.info("🔊 Sound Engineer: Video has no audio — using music only")
        filter_complex = (
            f"[1:a]atrim=duration={video_duration_s:.3f},"
            f"asetpts=N/SR/TB,loudnorm=I=-18:TP=-2:LRA=11,{ducking_expr},"
            f"afade=t=in:d=0.25,afade=t=out:st={max(0, video_duration_s - 1):.3f}:d=1[out]"
        )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-stream_loop",
        "-1",
        "-i",
        music_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v",
        "-map",
        "[out]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out),
    ]

    logger.info(
        f"🔊 Sound Engineer: Mixing background music (vol={music_volume}, "
        f"ducked={music_ducked_volume}, speech_windows={len(speech_windows)})..."
    )
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

    render_path = state.get("picture_path")
    master_index = state.get("master_index", [])
    approved_edit = state.get("approved_edit")
    edit_instruction = state.get("edit_instruction")
    state_music_prompt = state.get("music_prompt")
    existing_music = state.get("music_path")
    provider = state.get("audio_provider", "elevenlabs")
    warnings = []

    if not render_path or not Path(render_path).exists():
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "sound_engineer",
                    "message": f"Rendered video not found: {render_path}",
                    "phase": Phase.MASTERING,
                    "recoverable": False,
                }
            ],
        }

    try:
        render_p = Path(render_path)
        # Sanitize stem: colons are interpreted as protocol separators by FFmpeg
        safe_stem = re.sub(r"[^\w\s\-.]", "_", render_p.stem)
        work_dir = settings.state_dir / "sound_engineer" / state.get("run_id", "audio")
        work_dir.mkdir(parents=True, exist_ok=True)

        current_path = render_path

        # Check if the rendered video actually has an audio stream
        render_meta = await probe_media_async(current_path)
        has_audio = render_meta.get("has_audio", False)

        # ── Step 1: Source loudness normalization ────────────────
        if has_audio:
            denoised_path = str(work_dir / f"{safe_stem}_denoised.mp4")
            try:
                await _denoise_and_normalize(current_path, denoised_path)
                current_path = denoised_path
                logger.info("🔊 Sound Engineer: ✓ Denoise + LUFS complete (single pass)")
            except Exception as exc:
                logger.warning(f"🔊 Sound Engineer: Denoise+LUFS failed ({exc}), continuing...")
        else:
            logger.warning(
                "🔊 Sound Engineer: Input video has NO audio stream — skipping "
                "denoise & normalization"
            )

        # ── Step 3: Fetch Background Music ─────────────────────
        music_path: str | None = None

        # Re-use existing music file if available (not cleared by edit agent)
        if provider != "none" and existing_music and Path(existing_music).exists():
            music_path = existing_music
            logger.info(
                f"🔊 Sound Engineer: ♻️ Re-using existing music → {Path(existing_music).name}"
            )
        elif provider == "elevenlabs":
            from kinetograph.core.generated_audio import generate_audio

            prompt = state_music_prompt or "Subtle instrumental cinematic background score"
            if edit_instruction:
                prompt += f". Requested direction: {edit_instruction}"
            try:
                music_path = str(await generate_audio(prompt, render_meta["duration_ms"]))
            except Exception as exc:
                logger.warning("ElevenLabs music generation failed: %s", exc)
                warnings.append(
                    {
                        "agent": "sound_engineer",
                        "recoverable": True,
                        "message": "ElevenLabs music generation failed. Check your key, "
                        "plan and connection, then retry the audio edit.",
                    }
                )
        elif provider == "soundstripe" and soundstripe_configured():
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
                    logger.info(
                        f"🔊 Sound Engineer: ✓ Background music fetched → {music_file.name}"
                    )
            except Exception as exc:
                logger.warning(
                    f"🔊 Sound Engineer: Music fetch failed ({exc}), continuing without music..."
                )
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
                    music_volume=0.55 if state.get("editing_mode") == "highlights" else 0.28,
                    music_ducked_volume=0.10,
                )
                current_path = mixed_path
                logger.info("🔊 Sound Engineer: ✓ Music mixed with speech ducking")
            except Exception as exc:
                logger.warning("Music mixing failed: %s", exc)
                warnings.append(
                    {
                        "agent": "sound_engineer",
                        "recoverable": True,
                        "message": "Music could not be mixed into this render.",
                    }
                )

        # Optional sparse effects are anchored to rendered clip starts, not guessed timestamps.
        if provider == "elevenlabs" and state.get("sound_effects_enabled", True):
            from kinetograph.core.generated_audio import generate_audio

            cues = (approved_edit or {}).get("sound_effects", [])[:3]
            mapping = (approved_edit or {}).get("render_map", [])
            for i, cue in enumerate(cues):
                regions = [r for r in mapping if r["clip_id"] == cue.get("clip_id")]
                if not regions or not cue.get("prompt"):
                    continue
                start_ms = min(r["timeline_start_ms"] for r in regions) + max(
                    0, min(3000, int(cue.get("offset_ms", 0)))
                )
                duration = min(3000, max(500, int(cue.get("duration_ms", 1000))))
                if start_ms + duration > render_meta["duration_ms"]:
                    continue
                try:
                    effect = await generate_audio(cue["prompt"], duration, kind="effect")
                    output = str(work_dir / f"effect-{i}.mp4")
                    await _mix_effect(current_path, str(effect), output, start_ms, duration)
                    current_path = output
                except Exception as exc:
                    logger.warning("Sound effect generation/mix failed: %s", exc)
                    warnings.append(
                        {
                            "agent": "sound_engineer",
                            "recoverable": True,
                            "message": "A requested sound effect could not be generated.",
                        }
                    )

        # ── Step 5: Final Master ───────────────────────────────
        # Strip existing _mastered suffix to prevent _mastered_mastered
        stem = safe_stem
        if stem.endswith("_mastered"):
            stem = stem[: -len("_mastered")]
        output_dir = settings.output_dir / "runs" / state.get("run_id", "audio")
        output_dir.mkdir(parents=True, exist_ok=True)
        mastered_path = output_dir / f"{stem}_mastered{render_p.suffix}"

        if current_path != str(mastered_path):
            # Copy/remux to final output location (async)
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                current_path,
                "-c",
                "copy",
                str(mastered_path),
            ]
            try:
                await run_ffmpeg_async(cmd, timeout=300, description="Final master (stream copy)")
            except RuntimeError:
                # If stream copy fails, re-encode (async)
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    current_path,
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-preset",
                    "medium",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
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
            "caption_source_path": str(mastered_path),
            "caption_path": None,
            "music_path": music_path,
            "errors": warnings,
            "render_history": [str(mastered_path)],
        }
        if music_path:
            return_state["music_path"] = music_path

        return return_state

    except Exception as exc:
        logger.error(f"🔊 Sound Engineer: Mastering failed: {exc}")
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "sound_engineer",
                    "message": f"Audio mastering failed: {exc}",
                    "phase": Phase.MASTERING,
                    "recoverable": True,
                }
            ],
        }


async def _mix_effect(video: str, effect: str, output: str, start_ms: int, duration_ms: int):
    """A restrained effect over the master, limited and trimmed to picture length."""
    meta = await probe_media_async(video)
    duration = meta["duration_ms"] / 1000
    base = "[0:a]" if meta.get("has_audio") else "[silence]"
    silence = (
        ""
        if meta.get("has_audio")
        else (f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}[silence];")
    )
    filters = (
        silence + f"[1:a]atrim=duration={duration_ms / 1000},asetpts=PTS-STARTPTS,"
        f"loudnorm=I=-24:TP=-6:LRA=7,volume=0.3,adelay={start_ms}:all=1[fx];"
        f"{base}[fx]amix=inputs=2:duration=first:normalize=0,"
        "alimiter=limit=0.891:level=false:latency=true[out]"
    )
    await run_ffmpeg_async(
        [
            "ffmpeg",
            "-y",
            "-i",
            video,
            "-i",
            effect,
            "-filter_complex",
            filters,
            "-map",
            "0:v",
            "-map",
            "[out]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-t",
            str(duration),
            output,
        ],
        timeout=600,
        description="Mix sound effect",
    )
