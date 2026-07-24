"""Typed EDL + Critic routing tests — no API keys or collector required.

Run: cd backend && pytest tests/test_schema.py
"""

from __future__ import annotations

import pytest

from kinetograph.schema import (
    Act,
    ClipType,
    CriticFeedback,
    CriticIssue,
    EditClip,
    EditorialDecisionList,
    Severity,
    TransitionType,
)


# ── EDL round-trip + coercion ─────────────────────────────────────────────────

def test_editclip_defaults_and_duration():
    c = EditClip(clip_id="c1", source_file="/m/a.mp4", in_ms=1000, out_ms=4000)
    assert c.clip_type == ClipType.PRIMARY
    assert c.transition == TransitionType.CUT
    assert c.confidence == 1.0
    assert c.duration_ms == 3000


def test_editclip_enum_coercion_from_strings():
    c = EditClip(
        clip_id="c1", source_file="x", in_ms=0, out_ms=1000,
        clip_type="cutaway", transition="crossfade", act="hook",
    )
    assert c.clip_type is ClipType.CUTAWAY
    assert c.transition is TransitionType.CROSSFADE
    assert c.act is Act.HOOK


def test_editclip_confidence_bounds_enforced():
    with pytest.raises(ValueError):
        EditClip(clip_id="c1", source_file="x", in_ms=0, out_ms=1, confidence=1.5)


def test_edl_full_roundtrip():
    edl = EditorialDecisionList(
        title="Demo",
        scratchpad="hook->body->conclusion",
        clips=[
            EditClip(clip_id="c1", source_file="/m/a.mp4", in_ms=0, out_ms=3000,
                     act=Act.HOOK, rationale="strong opening line", confidence=0.9),
            EditClip(clip_id="c2", source_file="__SYNTH__", in_ms=0, out_ms=2000,
                     clip_type=ClipType.SYNTH, search_query="hands typing on laptop"),
        ],
    )
    dumped = edl.model_dump()
    restored = EditorialDecisionList.model_validate(dumped)
    assert restored.title == "Demo"
    assert restored.scratchpad == "hook->body->conclusion"
    assert [c.clip_id for c in restored.clips] == ["c1", "c2"]
    assert restored.clips[0].act is Act.HOOK
    assert restored.clips[1].clip_type is ClipType.SYNTH


def test_edl_validates_json_from_model():
    payload = '{"title":"T","clips":[{"clip_id":"c1","source_file":"x","in_ms":0,"out_ms":1000}]}'
    edl = EditorialDecisionList.model_validate_json(payload)
    assert edl.clips[0].clip_id == "c1"


# ── Critic feedback semantics ─────────────────────────────────────────────────

def test_critic_needs_revision_on_blocker():
    fb = CriticFeedback(
        approved=False, overall_score=4.0,
        issues=[CriticIssue(severity=Severity.BLOCKER, clip_id="c1", message="weak hook")],
    )
    assert fb.blockers and fb.needs_revision() is True


def test_critic_no_revision_when_approved_and_only_notes():
    fb = CriticFeedback(
        approved=True, overall_score=8.5,
        issues=[CriticIssue(severity=Severity.NOTE, message="minor polish")],
    )
    assert fb.needs_revision() is False


# ── _route_after_critic ───────────────────────────────────────────────────────

def test_route_after_critic_revises_under_cap():
    from kinetograph.orchestrator import _route_after_critic
    from kinetograph.agents.critic import CRITIC_MAX_ITERATIONS
    state = {
        "critic_iteration": 1,
        "critic_feedback": CriticFeedback(
            approved=False,
            issues=[CriticIssue(severity=Severity.BLOCKER, message="fix")],
        ).model_dump(),
    }
    assert CRITIC_MAX_ITERATIONS >= 2
    assert _route_after_critic(state) == "scripter"


def test_route_after_critic_proceeds_when_approved():
    from kinetograph.orchestrator import _route_after_critic
    state = {
        "critic_iteration": 1,
        "critic_feedback": CriticFeedback(approved=True, overall_score=9.0).model_dump(),
    }
    assert _route_after_critic(state) == "human_review"


def test_route_after_critic_proceeds_at_iteration_cap():
    from kinetograph.orchestrator import _route_after_critic
    from kinetograph.agents.critic import CRITIC_MAX_ITERATIONS
    # Even with blockers, once we hit the cap we must stop revising.
    state = {
        "critic_iteration": CRITIC_MAX_ITERATIONS,
        "critic_feedback": CriticFeedback(
            approved=False,
            issues=[CriticIssue(severity=Severity.BLOCKER, message="still bad")],
        ).model_dump(),
    }
    assert _route_after_critic(state) == "human_review"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
