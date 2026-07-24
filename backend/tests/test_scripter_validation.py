"""Phase 3 Scripter tests — enforce+clamp validation, music composition, and
the de-hardcoded keyword extractor. No API keys required.

Run: cd backend && pytest tests/test_scripter_validation.py
"""

from __future__ import annotations

from kinetograph.agents import scripter as S


def _index():
    return [
        {"asset_file": "/m/a.mp4", "start_ms": 0, "end_ms": 4000},
        {"asset_file": "/m/a.mp4", "start_ms": 4000, "end_ms": 8000},
        {"asset_file": "/m/b.mp4", "start_ms": 0, "end_ms": 3000},
    ]


# ── enforce + clamp ───────────────────────────────────────────────────────────

def test_invalid_clip_type_corrected_to_primary():
    pe = {"clips": [{"clip_id": "c1", "source_file": "/m/a.mp4", "in_ms": 0,
                     "out_ms": 4000, "clip_type": "banana"}]}
    errors = S._validate_paper_edit(pe, _index())
    assert not errors
    assert pe["clips"][0]["clip_type"] == "primary"


def test_invalid_transition_corrected_to_cut():
    pe = {"clips": [{"clip_id": "c1", "source_file": "/m/a.mp4", "in_ms": 0,
                     "out_ms": 4000, "clip_type": "primary", "transition": "wipe"}]}
    S._validate_paper_edit(pe, _index())
    assert pe["clips"][0]["transition"] == "cut"


def test_duplicate_clip_id_auto_suffixed():
    pe = {"clips": [
        {"clip_id": "c1", "source_file": "/m/a.mp4", "in_ms": 0, "out_ms": 4000, "clip_type": "primary"},
        {"clip_id": "c1", "source_file": "/m/b.mp4", "in_ms": 0, "out_ms": 3000, "clip_type": "primary"},
    ]}
    errors = S._validate_paper_edit(pe, _index())
    ids = [c["clip_id"] for c in pe["clips"]]
    assert ids[0] == "c1" and ids[1] == "c1_1"
    assert not any("Duplicate" in e for e in errors)


def test_primary_edge_snaps_to_segment_boundary():
    # in_ms 4200 → snaps to 4000; out_ms 7900 → snaps to 8000 (within 600ms tol)
    pe = {"clips": [{"clip_id": "c1", "source_file": "/m/a.mp4", "in_ms": 4200,
                     "out_ms": 7900, "clip_type": "primary"}]}
    S._validate_paper_edit(pe, _index())
    assert pe["clips"][0]["in_ms"] == 4000
    assert pe["clips"][0]["out_ms"] == 8000


def test_cutaway_duration_clamped_to_4s():
    pe = {"clips": [{"clip_id": "c1", "source_file": "/m/a.mp4", "in_ms": 0,
                     "out_ms": 7000, "clip_type": "cutaway"}]}  # 7s → clamp to 4s
    S._validate_paper_edit(pe, _index())
    assert pe["clips"][0]["out_ms"] - pe["clips"][0]["in_ms"] == 4000


def test_inverted_range_is_unfixable_error():
    pe = {"clips": [{"clip_id": "c1", "source_file": "/m/a.mp4", "in_ms": 5000,
                     "out_ms": 5000, "clip_type": "primary"}]}
    errors = S._validate_paper_edit(pe, _index())
    assert any("must be >" in e for e in errors)


def test_missing_source_is_error():
    pe = {"clips": [{"clip_id": "c1", "source_file": "/m/ghost.mp4", "in_ms": 0,
                     "out_ms": 2000, "clip_type": "primary"}]}
    errors = S._validate_paper_edit(pe, _index())
    assert any("not found" in e for e in errors)


def test_synth_requires_search_query():
    pe = {"clips": [{"clip_id": "c1", "source_file": "__SYNTH__", "in_ms": 0,
                     "out_ms": 2000, "clip_type": "synth"}]}
    errors = S._validate_paper_edit(pe, _index())
    assert any("search_query" in e for e in errors)


def test_total_duration_recomputed():
    pe = {"clips": [
        {"clip_id": "c1", "source_file": "/m/a.mp4", "in_ms": 0, "out_ms": 4000, "clip_type": "primary"},
        {"clip_id": "c2", "source_file": "/m/b.mp4", "in_ms": 0, "out_ms": 3000, "clip_type": "primary"},
    ], "total_duration_ms": 999}
    S._validate_paper_edit(pe, _index())
    assert pe["total_duration_ms"] == 7000


# ── music composition ─────────────────────────────────────────────────────────

def test_compose_music_from_structured_spec():
    pe = {"music": {"vibe": "uplifting", "genre": "lo-fi", "prompt": "chill beats",
                    "energy_curve": {"hook": "punchy"}}}
    out = S._compose_music_prompt(pe)
    assert "uplifting" in out and "lo-fi" in out and "chill beats" in out and "punchy" in out


def test_compose_music_falls_back_to_flat_prompt():
    assert S._compose_music_prompt({"music_prompt": "epic trailer"}) == "epic trailer"
    assert S._compose_music_prompt({}) is None


# ── keyword extraction (de-hardcoded) ─────────────────────────────────────────

def test_extract_tags_are_content_words_not_fixed_taxonomy():
    tags = S._extract_visual_tags(
        ["A chef plating pasta in a busy restaurant kitchen"],
        "the food was absolutely incredible tonight",
    )
    # generalises to any domain — surfaces content words, not a fixed 13-tag map
    assert "restaurant" in tags or "kitchen" in tags or "food" in tags
    # stopwords excluded
    assert "the" not in tags and "was" not in tags


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
