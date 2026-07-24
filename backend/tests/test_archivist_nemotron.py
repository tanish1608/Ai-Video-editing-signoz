"""Quick integration test: run the Archivist with Nemotron VLM."""

import asyncio
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")

from kinetograph.agents.archivist import archivist_node
from kinetograph.state import GraphState, Phase


async def main():
    state = GraphState(
        phase=Phase.INGESTING,
        project_name="nemotron-test",
        user_prompt="test",
    )
    result = await archivist_node(state)

    print(f"\n{'='*60}")
    print(f"RESULT")
    print(f"{'='*60}")
    print(f"Phase        : {result['phase']}")
    print(f"Assets       : {len(result.get('raw_assets', []))}")
    print(f"Index entries: {len(result.get('master_index', []))}")

    for i, entry in enumerate(result.get("master_index", [])[:5]):
        print(f"\n--- Entry {i} ---")
        fname = entry["asset_file"].split("/")[-1]
        print(f"  File      : {fname}")
        print(f"  Type      : {entry.get('asset_type', '?')}")
        print(f"  Time      : {entry['start_ms']}ms - {entry['end_ms']}ms")
        if entry["transcript"]:
            print(f"  Transcript: {entry['transcript'][:80]}...")
        else:
            print(f"  Transcript: (none)")
        if entry["visual_descriptions"]:
            print(f"  Visual    : {entry['visual_descriptions'][0][:120]}...")
        else:
            print(f"  Visual    : (none)")
        print(f"  Clip types: {entry.get('clip_types', [])}")

    print(f"\n{'='*60}")
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
