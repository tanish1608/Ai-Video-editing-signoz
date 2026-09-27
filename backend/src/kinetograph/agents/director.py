"""
Agent 5: The Director
──────────────────────
Translates the approved Paper Edit + assets into a rendered video.

Core compositing model (like a real NLE):
  - Primary clips carry both video AND audio (the narrative backbone)
  - Cutaway clips are VISUAL-ONLY overlays — their own audio is stripped.
    The primary audio continues playing underneath cutaway visuals.
  - The timeline is processed as "segments" grouped by primary clip.
    Each segment = [primary video+audio] with optional cutaway visuals on top.

The Director renders the entire timeline in a **single FFmpeg process** using
``-filter_complex``.  This replaces the old MoviePy frame-by-frame pipeline
and is 5–20× faster on most hardware (and fully hardware-acceleratable on
Apple Silicon / NVIDIA GPUs).

Handles: codec mismatch, vertical (9:16) content, stutter jump-cuts, PiP overlays.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from kinetograph.config import settings
from kinetograph.core.compositor import FilterGraphBuilder, SegmentResult
from kinetograph.core.compositor import render as render_fg
from kinetograph.core.media import (
    IMAGE_EXTENSIONS,
    normalize_clip,
    normalize_image_to_video,
    probe_media,
)
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


def _resolve_clip_path(clip: dict, synth_assets: list[dict]) -> str | None:
    """Resolve a Paper Edit clip to its actual file path."""
    if clip.get("clip_type") == "synth":
        for sa in synth_assets:
            if sa["clip_id"] == clip["clip_id"]:
                return sa["file_path"]
        return None
    src = clip.get("source_file", "")
    if src in ("SYNTHESIZE", "__SYNTH__", ""):
        return None
    return src


def _normalize_one_clip(
    source_path: str,
    output_path: str,
    color_grade: dict | None = None,
    width: int | None = None,
    height: int | None = None,
    quality_crf: int | None = None,
) -> str:
    """Normalize a single clip (runs in a worker thread). Handles images too."""
    if Path(source_path).suffix.lower() in IMAGE_EXTENSIONS:
        normalize_image_to_video(source_path, output_path, width=width, height=height)
    else:
        normalize_clip(
            source_path,
            output_path,
            color_grade=color_grade,
            width=width,
            height=height,
            crf=quality_crf,
        )
    return output_path


def _normalize_all_clips(
    approved_edit: dict,
    synth_assets: list[dict],
    temp_dir: Path,
    color_grade: dict | None = None,
    width: int | None = None,
    height: int | None = None,
    quality_crf: int | None = None,
) -> dict[str, str]:
    """Normalize ALL clips to the canonical format (vertical/horizontal per settings).

    De-duplicates by source file so we never re-encode the same media twice.
    Uses ThreadPoolExecutor to parallelize FFmpeg encoding across CPU cores.
    """
    normalized = {}
    source_cache: dict[str, str] = {}  # source_path → normalized_path
    clips = approved_edit.get("clips", [])

    # Build the work list, de-duplicating by source
    work: list[tuple[str, str, str]] = []  # (clip_id, source_path, output_path)
    for clip in clips:
        clip_id = clip["clip_id"]
        source_path = _resolve_clip_path(clip, synth_assets)

        if not source_path or not Path(source_path).exists():
            logger.warning(f"🎬 Director: Missing source for {clip_id}: {source_path}")
            continue

        if source_path in source_cache:
            logger.info(f"🎬 Director: Reusing normalized cache for {clip_id}")
            continue

        output_path = str(temp_dir / f"{clip_id}_normalized.mp4")
        source_cache[source_path] = output_path
        work.append((clip_id, source_path, output_path))

    if not work:
        return normalized

    # Parallelize FFmpeg normalization — use at most half the CPUs (FFmpeg itself is multi-threaded)
    max_workers = max(1, min(4, len(work), (os.cpu_count() or 2) // 2))
    logger.info(
        f"🎬 Director: Normalizing {len(work)} clips with {max_workers} parallel workers..."
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _normalize_one_clip, src, out, color_grade, width, height, quality_crf
            ): cid
            for cid, src, out in work
        }
        for future in as_completed(futures):
            clip_id = futures[future]
            try:
                result_path = future.result()
                normalized[clip_id] = result_path
                logger.info(f"🎬 Director: Normalized {clip_id}")
            except RuntimeError as exc:
                logger.error(f"🎬 Director: Failed to normalize {clip_id}: {exc}")

    # Fill in de-duplicated clip IDs that point to the same source
    for clip in clips:
        cid = clip["clip_id"]
        if cid not in normalized:
            src = _resolve_clip_path(clip, synth_assets)
            if src and src in source_cache and source_cache[src] in normalized.values():
                normalized[cid] = source_cache[src]

    return normalized


def _group_into_segments(clips_spec: list[dict]) -> list[dict]:
    """
    Group the flat clip list into primary segments with attached cutaway overlays.

    Each segment is:
      { "primary": <clip_dict>, "cutaways": [<clip_dict>, ...] }

    Cutaway/synth clips that appear between two primary clips are attached to
    the PRECEDING primary clip (their visuals play over its audio).
    """
    segments = []
    current_segment = None

    for clip in clips_spec:
        if clip.get("clip_type") == "primary":
            if current_segment is not None:
                segments.append(current_segment)
            current_segment = {"primary": clip, "cutaways": []}
        else:
            # Cutaway or synth → attach to current primary segment
            if current_segment is not None:
                current_segment["cutaways"].append(clip)
            else:
                # Cutaway before any primary → treat as standalone visual-only
                # Create a synthetic segment (no narrative audio)
                segments.append({"primary": None, "cutaways": [clip]})

    if current_segment is not None:
        segments.append(current_segment)

    return segments


def _get_skip_regions(
    clip_spec: dict,
    master_index: list[dict] | None,
) -> list[tuple[float, float]]:
    """
    Look up stutter / filler skip regions from master_index for a clip.

    Returns list of (start_sec, end_sec) regions to cut from the source file.
    """
    if not master_index:
        return []

    source = clip_spec.get("source_file", "")
    in_ms = clip_spec.get("in_ms", 0)
    out_ms = clip_spec.get("out_ms", 0)

    regions: list[tuple[float, float]] = []
    for entry in master_index:
        if entry.get("asset_file") != source:
            continue
        if entry.get("end_ms", 0) <= in_ms or entry.get("start_ms", 0) >= out_ms:
            continue
        for skip in entry.get("skip_regions", []):
            s, e = skip[0], skip[1]  # works for both tuples and lists
            s = max(s, in_ms)
            e = min(e, out_ms)
            if e > s + 50:  # at least 50 ms to be worth skipping
                regions.append((s / 1000.0, e / 1000.0))

    return sorted(set(regions))


def _find_best_cutaway_insert_point(
    primary_clip_spec: dict,
    master_index: list[dict] | None,
    primary_duration: float,
    min_gap_ms: int = 200,
) -> float:
    """
    Find the best moment to insert cutaway based on natural speech pauses.

    Scans the word-level timestamps from the master index for the primary
    clip and finds the largest gap between consecutive words.  The cutaway
    is inserted at the start of that gap, giving the edit a natural
    "breath" feeling rather than a mechanical 30% cut.

    Falls back to 30% of the primary duration if no word data is available.

    Args:
        primary_clip_spec: The primary clip dict from the Paper Edit.
        master_index: Full master index with word timestamps.
        primary_duration: Duration of the (possibly stutter-trimmed) primary clip in seconds.
        min_gap_ms: Minimum gap to consider a "pause" (default 200ms).

    Returns:
        Insertion point in seconds relative to the primary clip start (0-based).
    """
    fallback = min(primary_duration * 0.3, primary_duration * 0.8)
    fallback = max(0.2, fallback)

    if not master_index:
        return fallback

    source = primary_clip_spec.get("source_file", "")
    in_ms = primary_clip_spec.get("in_ms", 0)
    out_ms = primary_clip_spec.get("out_ms", 0)

    # Collect all words within this primary clip's range
    clip_words: list[dict] = []
    for entry in master_index:
        if entry.get("asset_file") != source:
            continue
        if entry.get("end_ms", 0) <= in_ms or entry.get("start_ms", 0) >= out_ms:
            continue
        for w in entry.get("words", []):
            ws = w.get("start_ms", 0)
            we = w.get("end_ms", 0)
            if ws >= in_ms and we <= out_ms and w.get("text", "").strip():
                clip_words.append(w)

    if len(clip_words) < 2:
        return fallback

    clip_words.sort(key=lambda w: w["start_ms"])

    # Find the largest gap between consecutive words
    best_gap_start_ms = -1
    best_gap_size = 0

    for i in range(len(clip_words) - 1):
        gap_start = clip_words[i]["end_ms"]
        gap_end = clip_words[i + 1]["start_ms"]
        gap_size = gap_end - gap_start

        if gap_size >= min_gap_ms and gap_size > best_gap_size:
            best_gap_size = gap_size
            best_gap_start_ms = gap_start

    if best_gap_start_ms < 0:
        return fallback

    # Convert to seconds relative to the trimmed primary clip (0-based)
    insert_sec = (best_gap_start_ms - in_ms) / 1000.0

    # Clamp: don't insert at the very start or very end
    insert_sec = max(0.2, min(insert_sec, primary_duration - 0.3))

    return insert_sec


def _build_segment_for_fg(
    fg: FilterGraphBuilder,
    segment: dict,
    normalized: dict[str, str],
    input_indices: dict[str, int],
    master_index: list[dict] | None = None,
) -> SegmentResult | None:
    """
    Build one composited segment inside the FilterGraphBuilder.

    Registers inputs as needed and returns a :class:`SegmentResult` with
    the video/audio labels and duration.  All heavy lifting happens inside
    FFmpeg's filter_complex — no MoviePy frame-by-frame processing.
    """
    primary_clip_spec = segment["primary"]
    cutaway_specs = segment["cutaways"]

    try:
        # ── Handle standalone cutaway (no primary) ──
        if primary_clip_spec is None:
            cutaway_inputs: list[tuple[int, float, float]] = []
            for cspec in cutaway_specs:
                norm_path = normalized.get(cspec["clip_id"])
                if not norm_path or not Path(norm_path).exists():
                    continue
                idx = _ensure_input(fg, norm_path, input_indices)
                meta = probe_media(norm_path)
                c_in = cspec["in_ms"] / 1000.0
                c_out = min(cspec["out_ms"] / 1000.0, meta["duration_ms"] / 1000.0)
                if c_out <= c_in:
                    continue
                cutaway_inputs.append((idx, c_in, c_out))
            if cutaway_inputs:
                return fg.build_standalone_cutaway_segment(cutaway_inputs)
            return None

        # ── Normal segment: primary + optional cutaway overlays ──
        primary_id = primary_clip_spec["clip_id"]
        primary_norm = normalized.get(primary_id)
        if not primary_norm or not Path(primary_norm).exists():
            logger.warning(f"🎬 Director: No normalized clip for primary {primary_id}")
            return None

        primary_idx = _ensure_input(fg, primary_norm, input_indices)
        primary_meta = probe_media(primary_norm)
        has_audio = primary_meta.get("has_audio", False)

        p_in = max(0, primary_clip_spec["in_ms"] / 1000.0)
        p_out = min(primary_clip_spec["out_ms"] / 1000.0, primary_meta["duration_ms"] / 1000.0)
        if p_out <= p_in:
            logger.warning(f"🎬 Director: Primary {primary_id} has zero duration")
            return None

        # ── Skip regions (stutter / filler removal) ──
        skip_regions = _get_skip_regions(primary_clip_spec, master_index) if master_index else []

        # ── Prepare cutaway inputs ──
        cutaway_inputs = []
        for cspec in cutaway_specs:
            norm_path = normalized.get(cspec["clip_id"])
            if not norm_path or not Path(norm_path).exists():
                logger.warning(f"🎬 Director: Missing cutaway {cspec['clip_id']}, skipping")
                continue
            c_idx = _ensure_input(fg, norm_path, input_indices)
            c_meta = probe_media(norm_path)
            c_in = max(0, cspec["in_ms"] / 1000.0)
            c_out = min(cspec["out_ms"] / 1000.0, c_meta["duration_ms"] / 1000.0)
            if c_out <= c_in:
                continue
            cutaway_inputs.append((c_idx, c_in, c_out))

        # ── Compute insertion point for cutaway ──
        from kinetograph.core.compositor import _compute_clean_ranges

        clean_ranges = _compute_clean_ranges(p_in, p_out, skip_regions)
        clean_duration = sum(e - s for s, e in clean_ranges)

        insert_point = (
            _find_best_cutaway_insert_point(
                primary_clip_spec,
                master_index,
                clean_duration,
            )
            if cutaway_inputs
            else 0.0
        )

        return fg.build_segment(
            primary_input=primary_idx,
            p_in=p_in,
            p_out=p_out,
            skip_regions=skip_regions,
            cutaway_inputs=cutaway_inputs,
            insert_point=insert_point,
            has_audio=has_audio,
        )

    except Exception as exc:
        logger.error(f"🎬 Director: Error building segment: {exc}")
        return None


def _ensure_input(
    fg: FilterGraphBuilder,
    path: str,
    cache: dict[str, int],
) -> int:
    """Add *path* to the filter graph's inputs (de-duplicated)."""
    if path not in cache:
        cache[path] = fg.add_input(path)
    return cache[path]


# ─── MoviePy crossfade concatenation ──────────────────────────────────────────

_CROSSFADE_SEC = 0.2  # Short visual-only dissolve between segments
_BOOKEND_FADE_SEC = 0.3  # Fade-in from black / fade-out to black duration


# ─── Agent Entry Point ────────────────────────────────────────────────────────


async def director_node(state: GraphState) -> dict:
    """
    LangGraph node — The Director.

    1. Normalize all clips to canonical format (vertical 1080×1920)
    2. Group clips into primary segments with cutaway overlays
    3. Build the entire timeline in a single FFmpeg filter_complex graph
    4. Apply PiP overlays and caption burn-in
    5. Render in one async FFmpeg process (hardware-accelerated when available)
    """
    logger.info("🎬 Director: Starting composited render (FFmpeg filter_complex)...")

    approved_edit = state.get("approved_edit")
    synth_assets = state.get("synth_assets", [])
    master_index = state.get("master_index", [])

    if not approved_edit:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "director",
                    "message": "No approved edit to render",
                    "phase": Phase.RENDERING,
                    "recoverable": False,
                }
            ],
        }

    clips_spec = approved_edit.get("clips", [])
    if not clips_spec:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "director",
                    "message": "Approved edit has no clips",
                    "phase": Phase.RENDERING,
                    "recoverable": False,
                }
            ],
        }

    render_settings = state.get("render_settings") or {}
    width = int(render_settings.get("width", settings.output_width))
    height = int(render_settings.get("height", settings.output_height))
    quality_crf = int(render_settings.get("crf", 18))
    run_id = str(state.get("run_id") or uuid.uuid4().hex)
    temp_dir = settings.state_dir / "director_temp" / run_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Normalize all clips
    logger.info(f"🎬 Director: Normalizing {len(clips_spec)} clips to {width}×{height}...")
    color_grade = state.get("color_grade")
    normalized = await asyncio.to_thread(
        _normalize_all_clips,
        approved_edit,
        synth_assets,
        temp_dir,
        color_grade=color_grade,
        width=width,
        height=height,
        quality_crf=quality_crf,
    )

    if not normalized:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "director",
                    "message": "No clips could be normalized",
                    "phase": Phase.NORMALIZING,
                    "recoverable": False,
                }
            ],
        }

    # Step 2: Group into primary segments
    segments = _group_into_segments(clips_spec)
    logger.info(
        f"🎬 Director: {len(segments)} segments "
        f"({sum(1 for s in segments if s['primary'])} primary + cutaway groups)"
    )

    # Step 3: Build filter graph
    fg = FilterGraphBuilder(fps=settings.output_fps)
    input_indices: dict[str, int] = {}  # path → input index (de-duplicated)

    segment_results: list[SegmentResult] = []
    errors: list[dict] = []

    for i, segment in enumerate(segments):
        primary_id = segment["primary"]["clip_id"] if segment["primary"] else "standalone"
        cutaway_count = len(segment["cutaways"])
        logger.info(
            f"🎬 Director: Segment {i + 1}: primary={primary_id}, cutaway overlays={cutaway_count}"
        )

        # Offload the (blocking, probe-heavy) graph-building helper off the loop.
        result = await asyncio.to_thread(
            _build_segment_for_fg, fg, segment, normalized, input_indices, master_index
        )
        if result:
            segment_results.append(result)
        else:
            errors.append(
                {
                    "agent": "director",
                    "message": f"Segment {i + 1} ({primary_id}) could not be built",
                    "phase": Phase.RENDERING,
                    "recoverable": True,
                }
            )

    if not segment_results:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "director",
                    "message": "No valid segments",
                    "phase": Phase.RENDERING,
                    "recoverable": False,
                }
            ],
        }

    # Step 4: Chain segments with crossfade transitions
    logger.info(f"🎬 Director: Chaining {len(segment_results)} segments (FFmpeg xfade)...")

    final_v, final_a, total_dur = fg.chain_segments(
        segment_results,
        crossfade_dur=_CROSSFADE_SEC,
        bookend_fade=_BOOKEND_FADE_SEC,
    )

    # Step 5: PiP overlays
    overlay_clips = state.get("overlay_clips", []) or approved_edit.get("overlay_clips", [])
    if overlay_clips:
        logger.info(f"🎬 Director: Compositing {len(overlay_clips)} PiP overlays...")
        final_v = await asyncio.to_thread(
            _apply_overlays_in_fg,
            fg,
            final_v,
            overlay_clips,
            normalized,
            synth_assets,
            input_indices,
            total_dur,
            width,
            height,
        )

    # Note: caption burn-in is handled by the Captioner node (runs after
    # the Director), so we don't burn captions here.

    fg.set_outputs(final_v, final_a)

    # Step 7: Render
    raw_title = approved_edit.get("title", "output")
    # Sanitize title: replace colons and other FS-unfriendly chars.
    # FFmpeg interprets colons as protocol separators on all platforms.
    title = re.sub(r"[^\w\s\-.]", "_", raw_title).replace(" ", "_")
    output_path = settings.output_dir / "runs" / run_id / f"{title}.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        logger.info(f"🎬 Director: Rendering to {output_path}...")
        await render_fg(
            fg,
            str(output_path),
            fps=settings.output_fps,
        )

        logger.info(f"🎬 Director: ✓ Render complete → {output_path}")

        # Update paper_edit transitions to reflect the crossfades we applied
        if len(segment_results) > 1:
            seg_boundary_ids = set()
            for i, seg in enumerate(segments):
                if i > 0 and seg["primary"] is not None:
                    seg_boundary_ids.add(seg["primary"]["clip_id"])
            default_crossfade_ms = int(_CROSSFADE_SEC * 1000)
            for clip in clips_spec:
                if clip["clip_id"] in seg_boundary_ids:
                    if not clip.get("transition") or clip["transition"] == "cut":
                        clip["transition"] = "crossfade"
                    if not clip.get("transition_duration_ms"):
                        clip["transition_duration_ms"] = default_crossfade_ms

        result_state: dict = {
            "phase": Phase.RENDERED,
            "normalized_clips": normalized,
            "render_path": str(output_path),
            "render_history": [str(output_path)],
            "approved_edit": approved_edit,
        }
        if errors:
            result_state["errors"] = errors

        # Cleanup temp clips
        import shutil as _shutil

        if temp_dir.exists():
            try:
                _shutil.rmtree(temp_dir)
                logger.info(f"🎬 Director: Cleaned up temp dir: {temp_dir}")
            except OSError as exc:
                logger.warning(f"🎬 Director: Failed to clean temp dir: {exc}")

        return result_state

    except Exception as exc:
        logger.error(f"🎬 Director: Render failed: {exc}")
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "director",
                    "message": f"Render failed: {exc}",
                    "phase": Phase.RENDERING,
                    "recoverable": True,
                }
            ],
        }


# ─── PiP Overlay Compositing ────────────────────────────────────────────────


def _apply_overlays_in_fg(
    fg: FilterGraphBuilder,
    base_video: str,
    overlay_clips: list[dict],
    normalized: dict[str, str],
    synth_assets: list[dict],
    input_indices: dict[str, int],
    base_duration: float,
    base_w: int | None = None,
    base_h: int | None = None,
) -> str:
    """
    Composite V2 overlay clips (picture-in-picture) into the filter graph.

    Each overlay_clip dict has:
      - source_file / clip_id: identifies the video to overlay
      - in_ms, out_ms: which portion of the overlay source to use
      - timeline_start_ms: when on the main timeline the overlay appears
      - transform: {x, y, width, height, opacity} (% of frame)
    """
    base_w = base_w or settings.output_width
    base_h = base_h or settings.output_height
    current = base_video

    for ov in overlay_clips:
        try:
            clip_id = ov.get("clip_id", "")

            # Resolve overlay source file
            ov_path = None
            if clip_id in normalized:
                ov_path = normalized[clip_id]
            else:
                src = ov.get("source_file", "")
                if src and src not in ("SYNTHESIZE", "__SYNTH__", "") and Path(src).exists():
                    ov_path = src
                elif ov.get("clip_type") == "synth":
                    for sa in synth_assets:
                        if sa.get("clip_id") == clip_id:
                            ov_path = sa.get("file_path")
                            break

            if not ov_path or not Path(ov_path).exists():
                logger.warning(f"🎬 Director: Overlay source missing for {clip_id}")
                continue

            ov_idx = _ensure_input(fg, ov_path, input_indices)
            ov_meta = probe_media(ov_path)
            ov_in = max(0, ov.get("in_ms", 0) / 1000.0)
            ov_out = min(
                ov.get("out_ms", ov_meta["duration_ms"]) / 1000.0,
                ov_meta["duration_ms"] / 1000.0,
            )
            if ov_out <= ov_in:
                continue

            # Parse transform (percentage → pixel)
            transform = ov.get("transform", {})
            target_w = int(base_w * transform.get("width", 30) / 100)
            target_h = int(base_h * transform.get("height", 30) / 100)
            target_x = int(base_w * transform.get("x", 65) / 100)
            target_y = int(base_h * transform.get("y", 60) / 100)

            timeline_start_s = ov.get("timeline_start_ms", 0) / 1000.0
            if timeline_start_s >= base_duration:
                continue

            current = fg.add_pip_overlay(
                base_video=current,
                overlay_input=ov_idx,
                ov_in=ov_in,
                ov_out=ov_out,
                timeline_start=timeline_start_s,
                target_w=target_w,
                target_h=target_h,
                target_x=target_x,
                target_y=target_y,
                base_duration=base_duration,
            )
            logger.info(
                f"🎬 Director: Added PiP overlay {clip_id} at {timeline_start_s:.1f}s "
                f"({transform.get('width', 30)}% @ {target_x},{target_y})"
            )

        except Exception as exc:
            logger.error(
                f"🎬 Director: Failed to composite overlay {ov.get('clip_id', '?')}: {exc}"
            )

    return current
