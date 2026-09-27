"""
Agent 2: The Scripter
─────────────────────
Reads the user prompt + the Archivist's master index.
Uses Gemini to generate a structured "Paper Edit" —
a JSON DAG dictating the exact sequence of clips for the final video.

Clip roles are content-based (not directory-based):
  - "primary"  → carries BOTH video and audio (narrative backbone)
  - "cutaway"  → visual-only overlay; audio stripped (visual variety)
  - "synth"    → stock footage from Pexels
  - "overlay"  → PiP composite (V2 track)
"""

from __future__ import annotations

import json
import logging

from google import genai
from google.genai import types

from kinetograph.config import settings
from kinetograph.observability import llm_span, record_gemini_tokens, record_retry
from kinetograph.schema import ClipType, OverlayPreset, TransitionType
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


# ─── Visual-clip enrichment helpers ─────────────────────────────────────────────

# Generic English stopwords for keyword extraction (no domain-specific terms —
# the previous _TAG_VOCABULARY hardcoded event terms like "mistral"/"nvidia"/
# "hackathon" and only worked for that one shoot). With the Archivist now
# emitting rich structured visual descriptions, we derive tags from the text
# itself so semantic matching generalises to ANY footage.
_STOPWORDS: frozenset[str] = frozenset(
    "the a an and or but of to in on at for with from by is are was were be been "
    "being this that these those it its as into over under between out up down off "
    "then than so such very more most some any all no not can will would could may "
    "who whom which what when where why how there here their they them he she his "
    "her you your we our us i me my one two three shows show showing display "
    "displays captures features depicts video clip frame frames scene segment "
    "across appears visible seen looking".split()
)


def _extract_visual_tags(visual_texts: list[str], transcript: str) -> list[str]:
    """Derive semantic keyword tags from visual descriptions + transcript.

    Content-word tokenisation (stopwords + short tokens removed), ranked by
    frequency — no hardcoded taxonomy, so it works for any footage/topic.
    """
    import re as _re
    from collections import Counter

    blob = (" ".join(visual_texts) + " " + transcript).lower()
    tokens = _re.findall(r"[a-z][a-z\-]{2,}", blob)
    counts = Counter(t for t in tokens if t not in _STOPWORDS and len(t) > 2)
    # Top content words as tags (frequency-ranked, capped for token budget).
    return [w for w, _ in counts.most_common(8)]


def _summarize_visuals(visual_descriptions: list[str]) -> str:
    """Combine all VDs for a visual clip into a single concise summary.

    Strips filler like 'The video shows/displays/captures...' and
    deduplicates overlapping descriptions.
    """
    import re

    if not visual_descriptions:
        return ""

    # Strip common VLM filler prefixes
    cleaned = []
    for vd in visual_descriptions:
        vd = re.sub(
            r"^(The video (shows?|displays?|captures?|features?|depicts?)\s+)",
            "",
            vd,
            flags=re.IGNORECASE,
        )
        # Capitalize first char
        if vd:
            vd = vd[0].upper() + vd[1:]
        cleaned.append(vd)

    # If multiple VDs, combine unique sentences (first 2 VDs usually cover it)
    combined = " ".join(cleaned[:2])
    # Cap at ~200 chars to avoid token bloat
    if len(combined) > 200:
        combined = combined[:197] + "..."
    return combined


# ─── System Prompt ─────────────────────────────────────────────────────────────

SCRIPTER_SYSTEM_PROMPT = """You edit real footage into a coherent, economical video.
Return only a JSON edit decision list grounded in the supplied master index.

EDITORIAL CONTRACT
- Obey the selected editing mode and the user's requested tone and duration.
- Narration: open with a self-contained, relevant thought, develop it with evidence,
  and finish with a payoff. Preserve negation, attribution, and the speaker's meaning.
  Never manufacture a quotation or combine fragments into a claim they did not make.
  Select complete speech segments at their supplied boundaries. Prefer hard cuts.
- Highlights: build a visual progression (establish, develop, peak, resolve), varying
  shot scale and movement. Use primary clips for sequential shots, even without speech.
  Do not force a talking-head hook. Use source ambience where useful; music carries pace.
  Make purposeful cuts, not an arbitrary cut every two seconds. Crossfades are optional.
- A primary clip adds time and carries source audio. A cutaway/synth AFTER a primary
  replaces its picture only; it adds NO time. Keep total attached B-roll within that
  primary's duration. Before the first primary, visual clips play sequentially in silence.
- Cutaways usually last 2–4 seconds. Match what is being discussed literally; do not
  substitute stock metaphors for a specific person, event, location or product.
  Prefer supplied footage. Request stock only when it fills a real visual gap.
- Avoid duplicate speech, mid-word cuts, gratuitous PiP, and transitions over words.
  Use source_file exactly as indexed. All times are integer milliseconds within bounds.
- Give each clip a short description and editorial_purpose explaining its contribution.
  scratchpad is a brief edit synopsis for the reviewer, not extended reasoning.
- Music: describe instrumental texture, tempo feel, energy progression and ending.
  No vocals under dialogue. Do not request imitation of named artists or songs.
- sound_effects: default []; at most 3 subtle cues, only for meaningful visual events.
  Each cue anchors to a PRIMARY clip_id with offset_ms, duration_ms (500–3000), prompt.
  Avoid effects over important words. Effects are optional, not decoration on every cut.
- For revision, preserve successful choices and change only what the request/critique needs.

JSON SHAPE
{"title":"Title", "scratchpad":"Brief editorial synopsis", "total_duration_ms":10000,
 "music":{"vibe":"warm", "genre":"ambient", "energy_curve":{"opening":"quiet",
 "middle":"gentle build", "ending":"resolve"}, "prompt":"Soft instrumental texture"},
 "clips":[{"clip_id":"clip_001", "source_file":"/indexed/file.mp4", "in_ms":0,
 "out_ms":10000, "clip_type":"primary", "transition":"cut",
 "description":"What is seen/heard", "editorial_purpose":"Establish the subject"}],
 "overlay_clips":[], "sound_effects":[]}
Clip types: primary, cutaway, synth, overlay. Transitions: cut, crossfade.
For synth, provide search_query and source_file="__SYNTH__".
Only add PiP if the brief requires it: list once in overlay_clips with clip_type="overlay",
source_file, in_ms, out_ms, timeline_start_ms, and overlay_preset="pip-br".
"""


# ─── Validation ────────────────────────────────────────────────────────────────

# Cutaway duration guardrails (ms) — the prompt asks for 2–4s cutaways.
_CUTAWAY_MIN_MS = 2000
_CUTAWAY_MAX_MS = 4000
_SNAP_TOLERANCE_MS = 600  # snap a primary edge to a segment boundary within this


def _snap(value: int, candidates: list[int]) -> int:
    """Snap *value* to the nearest candidate within tolerance, else leave it."""
    if not candidates:
        return value
    nearest = min(candidates, key=lambda c: abs(c - value))
    return nearest if abs(nearest - value) <= _SNAP_TOLERANCE_MS else value


def _compose_music_prompt(paper_edit: dict) -> str | None:
    """Compose a Soundstripe search hint from the structured `music` spec.

    Prefers the rich MusicSpec (vibe/genre/energy/prompt); falls back to the
    legacy flat `music_prompt` string if the model only produced that.
    """
    music = paper_edit.get("music")
    if isinstance(music, dict):
        parts = [
            music.get("vibe", ""),
            music.get("genre", ""),
            music.get("prompt", ""),
        ]
        curve = music.get("energy_curve")
        if isinstance(curve, dict) and curve:
            parts.append("energy: " + ", ".join(f"{k} {v}" for k, v in curve.items()))
        composed = " · ".join(p for p in parts if p).strip(" ·")
        if composed:
            return composed
    return paper_edit.get("music_prompt")


def _validate_paper_edit(
    paper_edit: dict,
    master_index: list[dict],
    editing_mode: str = "narration",
) -> list[str]:
    """
    Validate + CORRECT a Paper Edit against the master index.

    Repair safe enum/range drift; reject missing sources and impossible ranges.
    The Scripter retries with the previous candidate and concrete errors.
    """
    errors: list[str] = []

    if not paper_edit.get("clips"):
        errors.append("Paper Edit has no clips")
        return errors

    valid_types = {c.value for c in ClipType}
    valid_transitions = {t.value for t in TransitionType}
    valid_presets = {p.value for p in OverlayPreset}

    # Per-source available bounds + exact segment start/end boundaries (for snapping).
    available: dict[str, dict] = {}
    starts: dict[str, list[int]] = {}
    ends: dict[str, list[int]] = {}
    for entry in master_index:
        f = entry["asset_file"]
        available.setdefault(f, {"min_ms": entry["start_ms"], "max_ms": entry["end_ms"]})
        available[f]["min_ms"] = min(available[f]["min_ms"], entry["start_ms"])
        available[f]["max_ms"] = max(available[f]["max_ms"], entry["end_ms"])
        starts.setdefault(f, []).append(entry["start_ms"])
        ends.setdefault(f, []).append(entry["end_ms"])

    seen_ids: set[str] = set()
    source_use: dict[str, int] = {}
    total_ms = 0
    primary_seen = False
    used_ranges = set()

    for i, clip in enumerate(paper_edit["clips"]):
        cid = clip.get("clip_id") or f"clip_{i:03d}"

        # 1. Enum correction (silent) — invalid values snap to safe defaults.
        ctype = clip.get("clip_type")
        if ctype not in valid_types:
            clip["clip_type"] = ClipType.PRIMARY.value
            ctype = clip["clip_type"]
        if clip.get("transition") not in valid_transitions:
            clip["transition"] = TransitionType.CUT.value
        if ctype == ClipType.OVERLAY.value and clip.get("overlay_preset") not in valid_presets:
            clip["overlay_preset"] = OverlayPreset.PIP_BR.value

        # 2. Duplicate clip_id → auto-suffix (silent).
        if cid in seen_ids:
            suffix = 1
            while f"{cid}_{suffix}" in seen_ids:
                suffix += 1
            cid = f"{cid}_{suffix}"
        clip["clip_id"] = cid
        seen_ids.add(cid)

        # 3. Duration sanity — inverted range can't be safely fixed → error.
        in_ms = clip.get("in_ms", 0)
        out_ms = clip.get("out_ms", 0)
        if out_ms <= in_ms:
            errors.append(f"{cid}: out_ms ({out_ms}) must be > in_ms ({in_ms})")
            continue

        if ctype == ClipType.SYNTH.value:
            if not clip.get("search_query"):
                errors.append(f"{cid}: synth clip missing search_query")
        else:
            src = clip.get("source_file", "")
            if src not in available:
                errors.append(f"{cid}: source_file '{src}' not found in master index")
            else:
                source_use[src] = source_use.get(src, 0) + 1
                if ctype == ClipType.PRIMARY.value and editing_mode == "narration":
                    # Snap primary edges to exact segment boundaries (silent) —
                    # the prompt requires primaries use exact index timestamps.
                    in_ms = _snap(in_ms, starts.get(src, []))
                    out_ms = _snap(out_ms, ends.get(src, []))
                elif ctype == ClipType.CUTAWAY.value:
                    # Clamp cutaway to 2–4s (silent).
                    dur = out_ms - in_ms
                    dur = max(_CUTAWAY_MIN_MS, min(_CUTAWAY_MAX_MS, dur))
                    out_ms = in_ms + dur
                # Clamp to the source's real available window (silent).
                b = available[src]
                in_ms = max(b["min_ms"], in_ms)
                out_ms = min(b["max_ms"], out_ms)
                if out_ms <= in_ms:  # clamp collapsed it → unfixable
                    errors.append(f"{cid}: no valid range within source bounds")
                    continue
                clip["in_ms"], clip["out_ms"] = in_ms, out_ms

        if ctype == "primary":
            identity = (clip.get("source_file"), in_ms, out_ms)
            if identity in used_ranges:
                errors.append(f"{cid}: duplicate primary range; choose a distinct moment")
            used_ranges.add(identity)
            primary_seen = True
            total_ms += out_ms - in_ms
        elif ctype != "overlay" and not primary_seen:
            total_ms += out_ms - in_ms

    # Warn (non-blocking) about heavily reused sources.
    for src, n in source_use.items():
        if n >= 4:
            logger.info("📝 Scripter: source reused %d times: %s", n, src)

    primary_ids = {c["clip_id"] for c in paper_edit["clips"] if c["clip_type"] == "primary"}
    cues = paper_edit.get("sound_effects", [])
    if not isinstance(cues, list) or len(cues) > 3:
        errors.append("sound_effects must be an array with at most 3 cues")
    else:
        for cue in cues:
            if (
                not isinstance(cue, dict)
                or cue.get("clip_id") not in primary_ids
                or not isinstance(cue.get("prompt"), str)
                or not cue["prompt"].strip()
                or not isinstance(cue.get("offset_ms", 0), int)
                or not 0 <= cue.get("offset_ms", 0) <= 3000
                or not isinstance(cue.get("duration_ms"), int)
                or not 500 <= cue["duration_ms"] <= 3000
            ):
                errors.append(
                    "Invalid sound effect: require a primary anchor, prompt and 500–3000ms"
                )
    # Correct overlap duration as the renderer will, rather than counting B-roll twice.
    from kinetograph.agents.director import _group_into_segments
    from kinetograph.core.compositor import SegmentResult, segment_overlaps

    segments = _group_into_segments(paper_edit["clips"])
    heads = [seg["primary"] or seg["cutaways"][0] for seg in segments]
    durations = [SegmentResult("", None, max(0, c["out_ms"] - c["in_ms"]) / 1000) for c in heads]
    transitions = [
        min(500, max(0, c.get("transition_duration_ms", 200))) / 1000
        if c.get("transition") == "crossfade"
        else 0
        for c in heads[1:]
    ]
    total_ms -= round(sum(segment_overlaps(durations, transitions=transitions)) * 1000)
    paper_edit["total_duration_ms"] = total_ms
    return errors


# ─── Agent Entry Point ────────────────────────────────────────────────────────


async def scripter_node(state: GraphState) -> dict:
    """
    LangGraph node — The Scripter.

    Takes the user prompt + master index → generates a Paper Edit via Gemini.
    Validates the output and retries up to 3 times on malformed JSON.
    """
    logger.info("📝 Scripter: Drafting Paper Edit...")

    user_prompt = state.get("user_prompt", "")
    master_index = state.get("master_index", [])

    if not user_prompt:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "scripter",
                    "message": "No user prompt provided",
                    "phase": Phase.SCRIPTING,
                    "recoverable": False,
                }
            ],
        }

    if not master_index:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "scripter",
                    "message": "Master index is empty — Archivist may have failed",
                    "phase": Phase.SCRIPTING,
                    "recoverable": False,
                }
            ],
        }

    # Prepare a condensed version of the master index for the prompt.
    # Key optimisations:
    #  - Entries with speech: include transcript, skip verbose VLM descriptions
    #  - Entries without speech: include tags + visual summary for semantic matching
    condensed_index = []
    for entry in master_index:
        has_speech = entry.get("has_speech", bool(entry.get("transcript", "").strip()))
        content_tags = entry.get("content_tags", entry.get("clip_types", []))
        item: dict = {
            "asset_file": entry["asset_file"],
            "has_speech": has_speech,
            "content_tags": content_tags,
            "start_ms": entry["start_ms"],
            "end_ms": entry["end_ms"],
            "duration_ms": entry["end_ms"] - entry["start_ms"],
            "transcript": entry.get("transcript", ""),
            # Editorial ranking signal from the Archivist (0-1). Higher salience
            # = more highlight-worthy; use it to pick the hook + best moments.
            "salience": entry.get("salience", 0.0),
            "energy": entry.get("energy", 0.0),
            "emotion": entry.get("emotion", ""),
        }
        vds = entry.get("visual_descriptions", [])
        item["tags"] = _extract_visual_tags(vds, item["transcript"])
        item["visual_summary"] = _summarize_visuals(vds)
        condensed_index.append(item)

    # Build a short summary so the LLM can plan duration before reading all entries
    speech_entries = [e for e in condensed_index if e.get("has_speech")]
    visual_entries = [e for e in condensed_index if not e.get("has_speech")]
    speech_total_ms = sum(e["duration_ms"] for e in speech_entries)
    visual_total_ms = sum(e["duration_ms"] for e in visual_entries)

    # Check if this is an edit request (has existing paper_edit + [EDIT REQUEST] tag)
    existing_paper_edit = state.get("paper_edit")
    is_edit = "[EDIT REQUEST]" in user_prompt and existing_paper_edit

    user_message = (
        f"EDITING MODE: {state.get('editing_mode', 'narration')}\n"
        f"CREATIVE BRIEF:\n{user_prompt}\n\n"
        f"AVAILABLE FOOTAGE SUMMARY:\n"
        f"- Speech clips (potential primary): {len(speech_entries)} "
        f"segments, {speech_total_ms / 1000:.0f}s total\n"
        f"- Visual clips (potential cutaway): {len(visual_entries)} clips, "
        f"{visual_total_ms / 1000:.0f}s total\n\n"
        f"MASTER INDEX ({len(condensed_index)} segments):\n"
        f"{json.dumps(condensed_index, indent=2)}"
    )

    # For edits, include the existing paper edit and switch to an edit-specific prompt
    if is_edit:
        user_message += (
            f"\n\n═══ CURRENT PAPER EDIT (modify this) ═══\n"
            f"{json.dumps(existing_paper_edit, indent=2)}\n\n"
            f"IMPORTANT: The [EDIT REQUEST] above describes what the user wants changed.\n"
            f"You MUST start from the CURRENT PAPER EDIT above and apply ONLY "
            f"the requested changes.\n"
            f"Do NOT regenerate from scratch. Preserve all clips that the user did not mention.\n"
            f"If the user asks to remove something, remove those specific clips.\n"
            f"If the user asks to add something, add clips while keeping existing ones.\n"
            f"If the user asks to replace something, swap only the relevant clips.\n"
            f"Return the full updated paper edit JSON with all clips (modified + unchanged)."
        )
        logger.info("📝 Scripter: Edit mode — including existing paper edit in context")

    # Critic revise loop: if the Editorial Critic sent back the edit with issues,
    # include the previous edit + the specific problems so the Scripter repairs
    # them instead of regenerating blind.
    critic_feedback = state.get("critic_feedback")
    if (
        not is_edit
        and existing_paper_edit
        and critic_feedback
        and not critic_feedback.get("approved", True)
    ):
        issues = critic_feedback.get("issues", [])
        issue_lines = "\n".join(
            f"- [{i.get('severity', 'warning')}] "
            f"{('clip ' + i['clip_id'] + ': ') if i.get('clip_id') else ''}"
            f"{i.get('message', '')} → FIX: {i.get('fix', '')}"
            for i in issues
        )
        user_message += (
            f"\n\n═══ EDITORIAL QA FEEDBACK (revise to fix these) ═══\n"
            f"Overall score: {critic_feedback.get('overall_score', 0)}/10. "
            f"{critic_feedback.get('summary', '')}\n"
            f"Issues to fix:\n{issue_lines}\n\n"
            f"═══ YOUR PREVIOUS EDIT (fix it, don't restart) ═══\n"
            f"{json.dumps(existing_paper_edit, indent=2)}\n\n"
            f"Apply the fixes above while preserving everything that already works. "
            f"Return the full corrected paper edit JSON."
        )
        logger.info("📝 Scripter: Revise mode — incorporating %d critic issues", len(issues))

    client = genai.Client(api_key=settings.gemini_api_key)

    # Retry loop — LLM may produce slightly malformed JSON
    max_attempts = 3
    last_error = ""
    repair_note = ""  # self-repair: validation errors fed back on the next attempt

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"📝 Scripter: Attempt {attempt}/{max_attempts}")
            if attempt > 1:
                record_retry("scripter")

            attempt_message = user_message + repair_note
            with llm_span("gemini", settings.gemini_model, **{"llm.attempt": attempt}) as span:
                response = await client.aio.models.generate_content(
                    model=settings.gemini_model,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(
                                    text=f"{SCRIPTER_SYSTEM_PROMPT}\n\n{attempt_message}"
                                ),
                            ],
                        ),
                    ],
                    config=types.GenerateContentConfig(
                        temperature=0.4,
                        max_output_tokens=16384,
                        response_mime_type="application/json",
                    ),
                )
                record_gemini_tokens(span, response, settings.gemini_model)

            raw_content = response.text.strip()

            # Parse JSON
            paper_edit = json.loads(raw_content)

            # Validate + correct against master index
            validation_errors = _validate_paper_edit(
                paper_edit, master_index, state.get("editing_mode", "narration")
            )
            if validation_errors:
                logger.warning(
                    f"📝 Scripter: Validation errors on attempt {attempt}: {validation_errors}"
                )
                last_error = "; ".join(validation_errors)
                # Retry with actionable feedback, never hand off unrenderable drafts.
                if attempt < max_attempts:
                    # Self-repair: tell the model exactly what to fix next time.
                    repair_note = (
                        "\n\n═══ FIX THESE VALIDATION ERRORS ═══\n"
                        "Your previous attempt had these problems — return a "
                        "corrected edit that resolves ALL of them:\n"
                        + "\n".join(f"- {e}" for e in validation_errors)
                        + "\nPREVIOUS CANDIDATE:\n"
                        + json.dumps(paper_edit)
                    )
                    continue
                continue  # Never approve a draft with unrenderable source references.

            # Success (or last attempt with warnings).
            # Extract overlay clips from the paper edit (if any)
            overlay_clips = paper_edit.get("overlay_clips", [])
            # Also pull overlay-type clips from the main clips array
            for clip in paper_edit.get("clips", []):
                if clip.get("clip_type") == "overlay":
                    # Avoid duplicates (if already in overlay_clips array)
                    if not any(oc.get("clip_id") == clip.get("clip_id") for oc in overlay_clips):
                        overlay_clips.append(clip)

            logger.info(
                f"📝 Scripter: Paper Edit ready — "
                f"{len(paper_edit.get('clips', []))} clips, "
                f"{len(overlay_clips)} overlays, "
                f"{paper_edit.get('total_duration_ms', 0) / 1000:.1f}s total"
            )

            # Persist for debugging
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            edit_path = settings.state_dir / "paper_edit.json"
            with open(edit_path, "w") as f:
                json.dump(paper_edit, f, indent=2)

            return {
                "phase": Phase.SCRIPTED,
                "paper_edit": paper_edit,
                "overlay_clips": overlay_clips,
                # Structured music → a composed hint the Sound Engineer consumes.
                "music_prompt": _compose_music_prompt(paper_edit),
                # Clear any prior critic feedback — this edit hasn't been reviewed
                # yet, so the critic must not see stale approval/issues.
                "critic_feedback": None,
                "music_path": None,
            }

        except json.JSONDecodeError as exc:
            last_error = f"Invalid JSON from Gemini: {exc}"
            logger.warning(f"📝 Scripter: {last_error} (attempt {attempt})")
        except Exception as exc:
            last_error = f"Gemini API error: {exc}"
            logger.error(f"📝 Scripter: {last_error} (attempt {attempt})")

    # All attempts failed
    return {
        "phase": Phase.ERROR,
        "errors": [
            {
                "agent": "scripter",
                "message": "Failed to generate valid Paper Edit after "
                f"{max_attempts} attempts: {last_error}",
                "phase": Phase.SCRIPTING,
                "recoverable": True,
            }
        ],
    }
