"""Opt-in live evaluation using invented footage descriptions, never user media.

Run: python backend/scripts/evaluate_editing.py --live [--audio]
Writes drafts and generated samples to a temporary project; calls may incur provider usage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from kinetograph.agents.scripter import scripter_node
from kinetograph.config import settings
from kinetograph.core.generated_audio import generate_audio


async def evaluate(include_audio: bool):
    directory = Path(tempfile.mkdtemp(prefix="kinetograph-edit-eval-"))
    settings.kinetograph_project_dir = str(directory)
    entries = [
        {"asset_file": "/synthetic/interview.mp4", "start_ms": 0, "end_ms": 4000,
         "has_speech": True, "transcript": "We almost closed the bakery last winter.",
         "visual_descriptions": ["Baker at the counter, medium shot"], "salience": .9},
        {"asset_file": "/synthetic/interview.mp4", "start_ms": 4000, "end_ms": 8000,
         "has_speech": True, "transcript": "Our neighbors started ordering bread for each other.",
         "visual_descriptions": ["Baker smiles, hands on counter"], "salience": .8},
        {"asset_file": "/synthetic/interview.mp4", "start_ms": 8000, "end_ms": 12000,
         "has_speech": True, "transcript": "Now we bake an extra batch for the community every Friday.",
         "visual_descriptions": ["Baker holds up a fresh loaf"], "salience": .9},
        {"asset_file": "/synthetic/bakery.mp4", "start_ms": 0, "end_ms": 4000,
         "has_speech": False, "visual_descriptions": ["Wide shot of bakery opening at dawn"]},
        {"asset_file": "/synthetic/dough.mp4", "start_ms": 0, "end_ms": 4000,
         "has_speech": False, "visual_descriptions": ["Close-up hands kneading dough"]},
        {"asset_file": "/synthetic/neighbors.mp4", "start_ms": 0, "end_ms": 4000,
         "has_speech": False, "visual_descriptions": ["Neighbors share bread at a long outdoor table"]},
    ]
    report = {}
    for mode, brief in [
        ("narration", "Make a warm 12-second social story about how neighbors saved this bakery. "
         "Use the three complete spoken thoughts in order, with relevant B-roll. No stock footage."),
        ("highlights", "Make a calm 9–12 second cinematic bakery highlight. No interview speech; "
         "progress from opening the shop to baking to sharing. No stock footage."),
    ]:
        result = await scripter_node({"master_index": entries, "user_prompt": brief,
                                     "editing_mode": mode})
        (directory / f"{mode}.json").write_text(json.dumps(result, indent=2, default=str))
        edit = result.get("paper_edit") or {}
        report[mode] = {"phase": result["phase"], "duration_ms": edit.get("total_duration_ms"),
                        "clips": len(edit.get("clips", [])), "errors": result.get("errors", [])}
    if include_audio:
        for kind, prompt, duration in [
            ("music", "Warm acoustic instrumental background for a neighborhood bakery, "
             "soft plucked strings, gentle pulse, resolved ending, no vocals", 8000),
            ("effect", "One soft airy whoosh, restrained transition, no voice or music", 700),
        ]:
            try:
                output = await generate_audio(prompt, duration, kind=kind)
                report[kind] = {"path": str(output), "bytes": output.stat().st_size}
            except Exception as exc:
                report[kind] = {"error": str(exc)}
    (directory / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({"directory": str(directory), "results": report}, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow calls to configured providers")
    parser.add_argument("--audio", action="store_true", help="Also generate an 8s score and one effect")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to explicitly enable provider calls.")
    asyncio.run(evaluate(args.audio))
