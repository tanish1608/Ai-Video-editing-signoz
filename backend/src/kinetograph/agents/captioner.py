"""
Agent 6: The Captioner
───────────────────────
Generates engaging word-by-word captions for the rendered video.

Uses ElevenLabs word-level timestamps (from the Archivist's transcription)
mapped to the rendered video's timeline.  Generates ASS subtitles with
TikTok-style active-word highlighting, then burns them into the video
via FFmpeg's ``ass`` filter.

Pipeline position: director → **captioner** → sound_engineer → export
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kinetograph.config import settings
from kinetograph.core.captions import burn_captions, generate_ass_captions
from kinetograph.core.media import probe_media_async
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


async def captioner_node(state: GraphState) -> dict:
    """
    LangGraph node — The Captioner.

    1. Generate ASS subtitle file from word-level timestamps
    2. Burn captions into the rendered video via FFmpeg ass filter
    """
    logger.info("📝 Captioner: Starting caption generation...")

    render_path = state.get("render_path")
    approved_edit = state.get("approved_edit")
    master_index = state.get("master_index", [])
    caption_style = state.get("caption_style")  # user-chosen style preset

    if not render_path or not Path(render_path).exists():
        return {
            "phase": Phase.ERROR,
            "errors": [{
                "agent": "captioner",
                "message": f"Rendered video not found: {render_path}",
                "phase": Phase.RENDERED,
                "recoverable": False,
            }],
        }

    if not approved_edit:
        logger.warning("📝 Captioner: No approved edit — skipping captions")
        return {"phase": Phase.RENDERED}

    try:
        render_p = Path(render_path)
        caption_dir = settings.state_dir / "captions"
        caption_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Generate ASS subtitle file
        ass_path = caption_dir / f"{render_p.stem}_captions.ass"

        # Get actual rendered video duration so caption timestamps
        # can be scaled to compensate for crossfade transition overlaps
        video_meta = await probe_media_async(render_path)
        video_duration_ms = video_meta.get("duration_ms")

        result = await asyncio.to_thread(
            generate_ass_captions,
            approved_edit=approved_edit,
            master_index=master_index,
            output_path=ass_path,
            video_duration_ms=video_duration_ms,
            style=caption_style,
        )

        if result is None:
            logger.warning("📝 Captioner: No captions generated (no word timestamps?) — passing through")
            return {"phase": Phase.RENDERED}

        logger.info(f"📝 Captioner: ✓ ASS file generated → {ass_path}")

        # Step 2: Burn captions into the rendered video.
        # The director runs BEFORE the captioner in the pipeline, so it
        # can only burn captions from a *previous* run.  For the current
        # run (including caption-only edits), we burn here.
        captioned_path = render_p.parent / f"{render_p.stem}_captioned.mp4"
        try:
            await asyncio.to_thread(burn_captions, render_path, ass_path, captioned_path)
            logger.info(f"📝 Captioner: ✓ Captions burned → {captioned_path}")
            final_render = str(captioned_path)
        except Exception as burn_exc:
            logger.error(f"📝 Captioner: burn_captions failed: {burn_exc}")
            # Fall back to un-captioned render so pipeline can continue
            final_render = render_path

        return {
            "phase": Phase.RENDERED,
            "render_path": final_render,
            "caption_path": str(ass_path),
        }

    except Exception as exc:
        logger.error(f"📝 Captioner: Failed: {exc}")
        # Non-fatal — continue pipeline without captions
        return {
            "phase": Phase.RENDERED,
            "errors": [{
                "agent": "captioner",
                "message": f"Caption generation failed (non-fatal): {exc}",
                "phase": Phase.RENDERED,
                "recoverable": True,
            }],
        }
