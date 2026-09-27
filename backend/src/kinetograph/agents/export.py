"""
Export Agent — Final stage of the pipeline.
Builds OTIO timeline alongside the rendered video.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kinetograph.config import settings
from kinetograph.core.timeline import build_otio_timeline, export_otio
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


async def export_node(state: GraphState) -> dict:
    """
    LangGraph node — Export.

    Generates an OTIO timeline file from the approved edit.
    """
    logger.info("📦 Export: Building timeline files...")

    approved_edit = state.get("approved_edit")
    normalized_clips = state.get("normalized_clips", {})
    render_path = state.get("render_path")

    if not approved_edit:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "export",
                    "message": "No approved edit for timeline export",
                    "phase": Phase.EXPORTING,
                    "recoverable": False,
                }
            ],
        }

    # Guard against reporting success when the upstream render never produced a
    # valid file (post-approval nodes swallow failures into Phase.ERROR dicts).
    if not render_path or not Path(render_path).is_file():
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "export",
                    "message": f"No valid rendered video to export (render_path={render_path!r})",
                    "phase": Phase.EXPORTING,
                    "recoverable": False,
                }
            ],
        }

    try:
        # Build clip path mapping
        clip_paths = {}
        for clip in approved_edit.get("clips", []):
            cid = clip["clip_id"]
            if cid in normalized_clips and Path(normalized_clips[cid]).is_file():
                clip_paths[cid] = normalized_clips[cid]
            else:
                clip_paths[cid] = clip.get("source_file", "")

        # Build OTIO timeline
        timeline = build_otio_timeline(
            paper_edit=approved_edit,
            clip_paths=clip_paths,
            fps=float(settings.output_fps),
        )

        # Export OTIO timeline
        title = approved_edit.get("title", "output").replace(" ", "_")
        otio_path = Path(render_path).parent / f"{title}.otio"

        export_otio(timeline, otio_path)
        logger.info(f"📦 Export: OTIO → {otio_path}")

        logger.info("📦 Export: Complete!")
        logger.info(f"   🎬 Video:    {render_path}")
        logger.info(f"   📋 Timeline: {otio_path}")

        # Retain clean picture/audio baselines for reversible caption and sound edits.
        # Filename heuristics cannot distinguish intermediates from user exports.

        return {
            "phase": Phase.COMPLETE,
            "timeline_path": str(otio_path),
        }

    except Exception as exc:
        logger.error(f"📦 Export: Failed: {exc}")
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "export",
                    "message": f"Timeline export failed: {exc}",
                    "phase": Phase.EXPORTING,
                    "recoverable": True,
                }
            ],
        }
