"""Phase 2 Archivist intelligence tests — structured VLM parsing, A/V-fusion
transcript slicing, and shot-boundary windowing. No API keys / ffmpeg needed.

Run: cd backend && pytest tests/test_archivist_phase2.py
"""

from __future__ import annotations

import json

from kinetograph.agents import archivist as A
from kinetograph.core import media as M
from kinetograph.schema import VisualCategory


# ── Structured VLM parsing ────────────────────────────────────────────────────

def test_parse_structured_json():
    raw = json.dumps({
        "subject": "Two engineers at a laptop", "setting": "co-working space",
        "action": "typing fast", "notable": "NVIDIA sticker", "clip_type": "ACTION",
        "energy": 0.7, "salience": 0.85, "emotion": "excited",
    })
    v = A._parse_segment_visual(raw)
    assert v.clip_type is VisualCategory.ACTION
    assert v.salience == 0.85 and v.energy == 0.7 and v.emotion == "excited"


def test_parse_strips_code_fences():
    raw = "```json\n" + json.dumps({"subject": "x", "clip_type": "SCENIC", "salience": 0.4}) + "\n```"
    v = A._parse_segment_visual(raw)
    assert v.clip_type is VisualCategory.SCENIC and v.salience == 0.4


def test_parse_legacy_freetext_fallback():
    v = A._parse_segment_visual("A person addresses the camera directly.\nTALKING_HEAD")
    assert v.clip_type is VisualCategory.TALKING_HEAD
    assert "camera" in v.subject


def test_parse_garbage_defaults_to_other():
    v = A._parse_segment_visual("...not json, no label...")
    assert v.clip_type is VisualCategory.OTHER


def test_segment_result_backward_compatible_shape():
    v = A._parse_segment_visual(json.dumps({
        "subject": "a", "setting": "b", "action": "c", "clip_type": "ACTION", "salience": 0.6,
    }))
    r = A._segment_result({"start_ms": 0, "end_ms": 4000}, v, 8)
    # legacy keys preserved
    assert r["description"] and r["clip_type"] == "ACTION"
    # new signal present
    assert r["salience"] == 0.6 and "visual" in r


# ── A/V fusion: transcript slicing ────────────────────────────────────────────

def test_transcript_slice_overlap():
    words = [
        {"text": "hello", "start_ms": 0, "end_ms": 500},
        {"text": "world", "start_ms": 500, "end_ms": 1000},
        {"text": "later", "start_ms": 5000, "end_ms": 5500},
    ]
    assert A._transcript_slice_for(words, 0, 1200) == "hello world"
    assert A._transcript_slice_for(words, 4800, 6000) == "later"
    assert A._transcript_slice_for(words, 2000, 3000) == ""
    assert A._transcript_slice_for([], 0, 1000) == ""


# ── Shot-boundary windowing ───────────────────────────────────────────────────

def test_windows_uniform_when_no_cuts():
    w = M._shot_windows(10.0, [], target_sec=4.0)
    assert w[0] == (0.0, 4.0)
    assert w[-1][1] == 10.0


def test_windows_never_straddle_a_cut():
    w = M._shot_windows(12.0, [3.0, 7.0], target_sec=4.0)
    for a, b in w:
        assert not (a < 3.0 < b)
        assert not (a < 7.0 < b)


def test_windows_subdivide_long_take():
    # 10s single shot, 4s target → 3 windows.
    assert len(M._shot_windows(10.0, [], target_sec=4.0)) == 3


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
