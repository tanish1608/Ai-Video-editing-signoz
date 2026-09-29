"""Burn captions from the retained, mastered, caption-free video."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kinetograph.config import settings
from kinetograph.core.captions import burn_captions_async, generate_ass_captions
from kinetograph.core.media import probe_media_async
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


async def captioner_node(state: GraphState) -> dict:
    source = state.get("caption_source_path") or state.get("picture_path")
    # Old projects must re-render once, rather than burn over existing subtitles.
    if not source or not Path(source).is_file():
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "captioner",
                    "message": "Re-render this project to create a clean caption source.",
                    "recoverable": True,
                }
            ],
        }
    style = state.get("caption_style")
    if style and style.get("id") == "none":
        return {"phase": Phase.RENDERED, "render_path": source, "caption_path": None}
    try:
        run_id = state.get("run_id", "captions")
        output_dir = settings.output_dir / "runs" / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        ass_path = output_dir / "captions.ass"
        meta = await probe_media_async(source)
        result = await asyncio.to_thread(
            generate_ass_captions,
            approved_edit=state.get("approved_edit") or {},
            master_index=state.get("master_index", []),
            output_path=ass_path,
            video_duration_ms=meta.get("duration_ms"),
            width=meta.get("width"),
            height=meta.get("height"),
            style=style,
        )
        if result is None:
            return {"phase": Phase.RENDERED, "render_path": source, "caption_path": None}
        output = output_dir / "captioned.mp4"
        await burn_captions_async(source, ass_path, output)
        return {
            "phase": Phase.RENDERED,
            "render_path": str(output),
            "caption_path": str(ass_path),
            "render_history": [str(output)],
        }
    except Exception as exc:
        logger.exception("Caption generation failed")
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "captioner",
                    "message": f"Caption generation failed: {exc}",
                    "recoverable": True,
                }
            ],
        }
