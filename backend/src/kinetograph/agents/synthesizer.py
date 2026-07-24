"""
Agent 4: The Synthesizer
─────────────────────────
Identifies visual gaps in the approved Paper Edit where user-provided cutaway
footage is insufficient.  Downloads missing stock footage from Pexels via Python httpx.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from kinetograph.config import settings
from kinetograph.core.media import probe_media
from kinetograph.observability import tool_span
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


# ─── Pexels Search ────────────────────────────────────────────────────────────

async def _search_pexels(query: str, orientation: str | None = None) -> list[dict]:
    """
    Search Pexels for stock video clips.
    Returns list of {id, url, download_url, width, height, duration, photographer}.

    Orientation defaults to settings.output_orientation so Pexels results
    match the project canvas (portrait / landscape).
    """
    if orientation is None:
        orientation = getattr(settings, "output_orientation", "portrait")

    # tool_span is a SYNC context manager — keep it in a plain `with` nested
    # inside the async client (mixing it into `async with` fails: a sync CM has
    # no __aenter__).
    with tool_span("pexels_search", **{"pexels.query": query, "pexels.orientation": orientation}):
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.pexels.com/videos/search",
                params={
                    "query": query,
                    "orientation": orientation,
                    "size": "medium",
                    "per_page": 5,
                },
                headers={"Authorization": settings.pexels_api_key},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

    results = []
    for video in data.get("videos", []):
        best_file = None
        for vf in video.get("video_files", []):
            if vf.get("file_type") == "video/mp4" and vf.get("quality") in ("hd", "sd"):
                if best_file is None or vf.get("width", 0) > best_file.get("width", 0):
                    best_file = vf

        if best_file:
            results.append({
                "id": video["id"],
                "url": video.get("url", ""),
                "download_url": best_file["link"],
                "width": best_file.get("width", 0),
                "height": best_file.get("height", 0),
                "duration": video.get("duration", 0),
                "photographer": video.get("user", {}).get("name", "Unknown"),
            })

    return results


async def _download_clip(url: str, output_path: Path) -> Path:
    """Download a video clip from URL to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, timeout=120.0)
        response.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(response.content)
    return output_path


# ─── Agent Entry Point ────────────────────────────────────────────────────────

_DOWNLOAD_CONCURRENCY = 3  # Max parallel Pexels downloads


async def _fetch_one_clip(clip: dict, sem: asyncio.Semaphore) -> dict | None:
    """Search + download a single synth clip, respecting concurrency limit."""
    clip_id = clip["clip_id"]
    query = clip.get("search_query", clip.get("description", "stock footage"))
    duration_needed = clip.get("out_ms", 3000) - clip.get("in_ms", 0)

    async with sem:
        logger.info(f"🎨 Synthesizer: Searching Pexels for '{query}'")
        results = await _search_pexels(query)

        if not results:
            logger.warning(f"🎨 Synthesizer: No results for '{query}'")
            return None

        best = min(results, key=lambda r: abs(r["duration"] * 1000 - duration_needed))

        output_path = settings.synth_cache_dir / f"{clip_id}_{best['id']}.mp4"
        await _download_clip(best["download_url"], output_path)

        try:
            meta = probe_media(output_path)
            logger.info(
                f"🎨 Synthesizer: Downloaded {clip_id} — "
                f"{meta['width']}x{meta['height']}, {meta['duration_ms']}ms"
            )
            return {
                "clip_id": clip_id,
                "file_path": str(output_path),
                "source_url": best["url"],
                "pexels_id": best["id"],
                "photographer": best["photographer"],
                "duration_ms": meta["duration_ms"],
            }
        except RuntimeError:
            logger.error(f"🎨 Synthesizer: Downloaded file is corrupt: {output_path}")
            output_path.unlink(missing_ok=True)
            return None


async def synthesizer_node(state: GraphState) -> dict:
    """
    LangGraph node — The Synthesizer.

    1. Parse approved edit for "synth" clips
    2. Search + download from Pexels in parallel (max 3 concurrent)
    3. Validate downloaded files
    """
    approved_edit = state.get("approved_edit")

    if not approved_edit:
        return {
            "phase": Phase.ERROR,
            "errors": [{
                "agent": "synthesizer",
                "message": "No approved edit found",
                "phase": Phase.SYNTHESIZING,
                "recoverable": False,
            }],
        }

    clips = approved_edit.get("clips", [])
    synth_clips = [c for c in clips if c.get("clip_type") == "synth"]

    if not synth_clips:
        logger.info("🎨 Synthesizer: No synthetic clips needed — skipping")
        return {"phase": Phase.SYNTHESIZED}

    logger.info(f"🎨 Synthesizer: Need to find {len(synth_clips)} stock clips (parallel)")

    sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

    # Launch all search+download tasks in parallel
    tasks = [_fetch_one_clip(clip, sem) for clip in synth_clips]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    synth_assets = []
    errors = []

    for clip, result in zip(synth_clips, results):
        if isinstance(result, Exception):
            logger.error(f"🎨 Synthesizer: Failed for {clip['clip_id']}: {result}")
            errors.append({
                "agent": "synthesizer",
                "message": f"Pexels search failed for {clip['clip_id']}: {result}",
                "phase": Phase.SYNTHESIZING,
                "recoverable": True,
            })
        elif result is None:
            errors.append({
                "agent": "synthesizer",
                "message": f"No Pexels results for clip {clip['clip_id']}",
                "phase": Phase.SYNTHESIZING,
                "recoverable": True,
            })
        else:
            synth_assets.append(result)

    logger.info(f"🎨 Synthesizer: Acquired {len(synth_assets)}/{len(synth_clips)} assets")

    result_state: dict = {
        "phase": Phase.SYNTHESIZED,
        "synth_assets": synth_assets,
    }
    if errors:
        result_state["errors"] = errors

    return result_state
