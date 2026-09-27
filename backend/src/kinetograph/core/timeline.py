"""
Timeline export utilities — OpenTimelineIO + manual FCPXML generation.

OTIO is optional (may not build on all Python versions).
Falls back to manual XML generation if unavailable.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import opentimelineio as otio
    from opentimelineio.opentime import RationalTime, TimeRange

    OTIO_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    OTIO_AVAILABLE = False


def _file_uri(file_path: str) -> str:
    """Build a properly-escaped ``file://`` URI (handles spaces/special chars)."""
    try:
        return Path(file_path).absolute().as_uri()
    except (ValueError, OSError):
        # Non-absolute or otherwise unconvertible — best-effort percent-encode.
        return "file://" + quote(str(file_path))


def build_otio_timeline(
    paper_edit: dict,
    clip_paths: dict[str, str],
    fps: float = 30.0,
) -> Any:
    """
    Build an OTIO Timeline from an approved PaperEdit + resolved clip paths.

    If OTIO is unavailable, returns a plain dict representation.
    """
    if not OTIO_AVAILABLE:
        return {
            "name": paper_edit.get("title", "Kinetograph Export"),
            "fps": fps,
            "clips": [
                {
                    "clip_id": c["clip_id"],
                    "file_path": clip_paths.get(c["clip_id"], c.get("source_file", "")),
                    "in_ms": c["in_ms"],
                    "out_ms": c["out_ms"],
                }
                for c in paper_edit.get("clips", [])
            ],
        }

    timeline = otio.schema.Timeline(name=paper_edit.get("title", "Kinetograph Export"))
    video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    audio_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)

    for clip_data in paper_edit.get("clips", []):
        clip_id = clip_data["clip_id"]
        in_ms = clip_data["in_ms"]
        out_ms = clip_data["out_ms"]
        duration_ms = out_ms - in_ms
        file_path = clip_paths.get(clip_id, clip_data.get("source_file", ""))

        media_ref = otio.schema.ExternalReference(
            target_url=_file_uri(file_path),
            # available_range must *contain* the source_range. The source range
            # starts at in_ms, so the available range has to span at least
            # [0, out_ms] — not just the clip's used duration, or any clip with
            # in_ms > 0 produces an invalid (out-of-bounds) media reference.
            available_range=TimeRange(
                start_time=RationalTime(0, fps),
                duration=RationalTime(out_ms / 1000.0 * fps, fps),
            ),
        )
        source_range = TimeRange(
            start_time=RationalTime(in_ms / 1000.0 * fps, fps),
            duration=RationalTime(duration_ms / 1000.0 * fps, fps),
        )

        video_track.append(
            otio.schema.Clip(
                name=clip_id,
                media_reference=media_ref,
                source_range=source_range,
            )
        )
        audio_track.append(
            otio.schema.Clip(
                name=f"{clip_id}_audio",
                media_reference=media_ref,
                source_range=source_range,
            )
        )

    timeline.tracks.append(video_track)
    timeline.tracks.append(audio_track)
    return timeline


def export_otio(timeline: Any, output_path: str | Path) -> Path:
    """Export timeline as native .otio file (or .json fallback)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if OTIO_AVAILABLE and not isinstance(timeline, dict):
        otio.adapters.write_to_file(timeline, str(output_path))
    else:
        output_path = output_path.with_suffix(".json")
        with open(output_path, "w") as f:
            json.dump(timeline, f, indent=2)

    return output_path


def _media_rep(file_path: str) -> ET.Element:
    """Create an FCPXML media-rep element."""
    return ET.Element("media-rep", kind="original-media", src=_file_uri(file_path))


def export_fcpxml(timeline: Any, output_path: str | Path) -> Path:
    """
    Export timeline as FCPXML.
    Uses OTIO adapter if available, otherwise generates manually.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if OTIO_AVAILABLE and not isinstance(timeline, dict):
        otio.adapters.write_to_file(timeline, str(output_path), adapter_name="fcp_xml")
        return output_path

    # Manual FCPXML generation
    clips = timeline.get("clips", []) if isinstance(timeline, dict) else []
    fps = timeline.get("fps", 30.0) if isinstance(timeline, dict) else 30.0
    name = timeline.get("name", "Kinetograph") if isinstance(timeline, dict) else "Kinetograph"

    fcpxml = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(resources, "format", id="r0", name=f"FFVideoFormat1080p{int(fps)}")

    for i, clip in enumerate(clips):
        duration_ms = clip["out_ms"] - clip["in_ms"]
        duration_s = f"{duration_ms / 1000:.3f}s"
        asset = ET.SubElement(
            resources,
            "asset",
            id=f"r{i + 1}",
            start="0s",
            duration=duration_s,
            hasVideo="1",
            hasAudio="1",
            format="r0",
            audioSources="1",
            audioChannels="2",
            audioRate="48000",
        )
        asset.append(_media_rep(clip["file_path"]))

    event = ET.SubElement(fcpxml, "event", name=name)
    project = ET.SubElement(event, "project", name=name)
    sequence = ET.SubElement(project, "sequence", format="r0")
    spine = ET.SubElement(sequence, "spine")

    for i, clip in enumerate(clips):
        in_s = f"{clip['in_ms'] / 1000:.3f}s"
        duration_ms = clip["out_ms"] - clip["in_ms"]
        duration_s = f"{duration_ms / 1000:.3f}s"
        ET.SubElement(
            spine,
            "asset-clip",
            name=clip["clip_id"],
            ref=f"r{i + 1}",
            start=in_s,
            duration=duration_s,
            audioRole="dialogue",
        )

    tree = ET.ElementTree(fcpxml)
    ET.indent(tree, space="  ")
    tree.write(str(output_path), encoding="unicode", xml_declaration=True)
    return output_path
