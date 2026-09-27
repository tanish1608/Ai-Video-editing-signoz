"""
Typed Editorial Decision List (EDL) — the contract between the pipeline's
*intelligence* (Archivist, Scripter, Critic) and its *production* stack
(Director, Captioner, Sound Engineer).

Historically `master_index`, `paper_edit`, and `overlay_clips` flowed through
`GraphState` as loosely-typed `dict`/`list[dict]`, which caused silent field
drops and made editorial reasoning invisible downstream. These Pydantic v2
models give:

  * a validated schema the Critic agent can reason over,
  * clean, enumerable attributes for OTel spans,
  * a place to persist the editorial *why* (act / beat / rationale / confidence)
    that the Scripter's prompt already solicits but never recorded.

Integration is incremental: agents keep writing plain dicts into `GraphState`,
but validate at the boundaries via ``EditorialDecisionList.model_validate`` and
``.model_dump()``. Nothing in the render stack has to change at once.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# ── Enums ─────────────────────────────────────────────────────────────────────


class ClipType(str, Enum):
    PRIMARY = "primary"  # video + audio narrative backbone
    CUTAWAY = "cutaway"  # visual-only overlay over preceding primary audio
    SYNTH = "synth"  # Pexels stock B-roll
    OVERLAY = "overlay"  # PiP composite (A-roll over B-roll)


class TransitionType(str, Enum):
    CUT = "cut"
    CROSSFADE = "crossfade"


class Act(str, Enum):
    HOOK = "hook"
    BODY = "body"
    CONCLUSION = "conclusion"


class OverlayPreset(str, Enum):
    PIP_BR = "pip-br"
    PIP_BL = "pip-bl"
    PIP_TR = "pip-tr"
    PIP_TL = "pip-tl"
    PIP_CENTER = "pip-center"
    SIDE_BY_SIDE = "side-by-side"
    CUSTOM = "custom"


class VisualCategory(str, Enum):
    TALKING_HEAD = "TALKING_HEAD"
    SCENIC = "SCENIC"
    ACTION = "ACTION"
    TEXT_OVERLAY = "TEXT_OVERLAY"
    TRANSITION = "TRANSITION"
    OTHER = "OTHER"


# ── Archivist output (analysis) ───────────────────────────────────────────────


class Word(BaseModel):
    text: str
    start_ms: int
    end_ms: int
    speaker_id: Optional[str] = None


class SegmentVisual(BaseModel):
    """Structured VLM description of a video segment (replaces free-text parsing)."""

    subject: str = ""
    setting: str = ""
    action: str = ""
    notable: str = ""
    clip_type: VisualCategory = VisualCategory.OTHER
    energy: float = Field(0.0, ge=0.0, le=1.0)  # motion/intensity 0-1
    salience: float = Field(0.0, ge=0.0, le=1.0)  # how highlight-worthy 0-1
    emotion: str = ""  # e.g. "excited", "calm"


class AnalyzedSegment(BaseModel):
    """One master_index entry — uniform across speech and no-speech branches."""

    asset_file: str
    media_type: str = "video"
    start_ms: int
    end_ms: int
    has_speech: bool = False
    transcript: str = ""
    words: list[Word] = Field(default_factory=list)
    skip_regions: list[tuple[int, int]] = Field(default_factory=list)
    visual: Optional[SegmentVisual] = None
    content_tags: list[str] = Field(default_factory=list)
    speaker_id: Optional[str] = None


# ── Scripter output (the EDL) ─────────────────────────────────────────────────


class EditClip(BaseModel):
    """A single timeline clip — mechanical fields + editorial reasoning."""

    # Mechanical
    clip_id: str
    source_file: str
    in_ms: int
    out_ms: int
    clip_type: ClipType = ClipType.PRIMARY
    transition: TransitionType = TransitionType.CUT
    transition_duration_ms: Optional[int] = None
    search_query: Optional[str] = None  # synth clips
    overlay_text: Optional[str] = None
    timeline_start_ms: Optional[int] = None  # overlay clips
    overlay_preset: Optional[OverlayPreset] = None
    description: str = ""
    # Editorial (the "why" — persisted + surfaced, reasoned over by the Critic)
    act: Optional[Act] = None
    beat: str = ""
    editorial_purpose: str = ""
    rationale: str = ""
    confidence: float = Field(1.0, ge=0.0, le=1.0)

    @property
    def duration_ms(self) -> int:
        return max(0, self.out_ms - self.in_ms)


class MusicSpec(BaseModel):
    """Structured music direction (replaces the bare `music_prompt` string)."""

    vibe: str = ""
    genre: str = ""
    energy_curve: dict[str, str] = Field(default_factory=dict)  # act -> energy word
    prompt: str = ""


class SoundEffectCue(BaseModel):
    clip_id: str
    offset_ms: int = Field(0, ge=0, le=3000)
    duration_ms: int = Field(1000, ge=500, le=3000)
    prompt: str = Field(..., min_length=1, max_length=2000)


class EditorialDecisionList(BaseModel):
    """The full paper edit — the intelligence→production contract."""

    title: str = "Untitled Sequence"
    total_duration_ms: int = 0
    scratchpad: str = ""  # the narrative plan (now persisted)
    music: Optional[MusicSpec] = None
    sound_effects: list[SoundEffectCue] = Field(default_factory=list, max_length=3)
    clips: list[EditClip] = Field(default_factory=list)
    overlay_clips: list[EditClip] = Field(default_factory=list)


# ── Critic output ─────────────────────────────────────────────────────────────


class Severity(str, Enum):
    BLOCKER = "blocker"  # must fix — forces a revision
    WARNING = "warning"  # should fix
    NOTE = "note"  # nice-to-have


class CriticIssue(BaseModel):
    severity: Severity = Severity.WARNING
    clip_id: Optional[str] = None
    message: str = ""
    fix: str = ""  # concrete suggested change


class CriticFeedback(BaseModel):
    """Structured QA review of an EDL against the brief + constraints."""

    approved: bool = True
    overall_score: float = Field(0.0, ge=0.0, le=10.0)
    summary: str = ""
    issues: list[CriticIssue] = Field(default_factory=list)

    @property
    def blockers(self) -> list[CriticIssue]:
        return [i for i in self.issues if i.severity == Severity.BLOCKER]

    def needs_revision(self) -> bool:
        """Revise only when the critic disapproves or raised blocking issues."""
        return (not self.approved) or bool(self.blockers)
