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
            if cid in normalized_clips:
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

        # Clean up *intermediate* MP4s from the output directory. Only remove
        # files whose name carries a known pipeline-stage marker — never an
        # arbitrary .mp4, so a user's earlier final renders are preserved.
        if render_path:
            final_render = Path(render_path).resolve()
            _INTERMEDIATE_MARKERS = (
                "_captioned",
                "_mixed",
                "_denoised",
                "_normalized",
                "_master",
                "_raw",
                "_temp",
                "_render",
            )
            for f in final_render.parent.iterdir():
                if (
                    f.suffix.lower() == ".mp4"
                    and f.resolve() != final_render
                    and not f.name.startswith(".")
                    and any(marker in f.stem for marker in _INTERMEDIATE_MARKERS)
                ):
                    try:
                        f.unlink()
                        logger.info(f"📦 Export: Removed intermediate: {f.name}")
                    except OSError:
                        pass

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
