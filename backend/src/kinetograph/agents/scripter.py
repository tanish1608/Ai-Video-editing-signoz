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
import uuid

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
    counts = Counter(
        t for t in tokens if t not in _STOPWORDS and len(t) > 2
    )
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
            "", vd, flags=re.IGNORECASE,
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

SCRIPTER_SYSTEM_PROMPT = """\
You are an elite, award-winning documentary editor with decades of experience cutting \
short-form vertical (9:16) content that has earned billions of views across TikTok, \
Reels, and Shorts. You construct compelling, narrative-driven video sequences from \
raw footage.

You will receive:
1. A **Creative Brief** from the director (desired length, tone, key moments).
2. A **Master Index** of every available clip segment, each tagged with:
   - `has_speech` (true if the speaker is talking)
   - `content_tags` (VLM-detected: TALKING_HEAD, SCENIC, ACTION, etc.)
   - `transcript` (what is being said, if any)
   - `visual_summary` / `tags` (for non-speech clips)

────────────────────────────────────────────────────────────────────────────────
## YOUR TWO-STEP PROCESS
────────────────────────────────────────────────────────────────────────────────

### STEP 1 — The Narrative Scratchpad (MANDATORY)
Before you write a single clip, you MUST plan the edit inside a `"scratchpad"` field \
in your JSON output. In this field you will:
  - Outline the **3-act structure** of the video (Hook → Body → Conclusion) \
based on the Creative Brief.
  - Scan the Master Index and hand-pick the most powerful **primary clips** \
(has_speech=true) to build each act.
  - Identify which **cutaway clips** (has_speech=false) visually match \
the subjects mentioned in each primary segment.
  - Do a **math check**: sum the selected primary durations and verify they \
hit the requested length (±10 %).

Example scratchpad value:
  "HOOK: I need a punchy opening — Clip at 5460-9480ms is perfect ('energy here \
is crazy'). BODY: Use the 3 strongest clips about building, then the food \
montage. CUTAWAY MAPPING: During 'wrote so much code' → overlay laptops footage. \
MATH: 12.4 + 8.1 + 6.3 + ... ≈ 58s → fits 60s brief."

### STEP 2 — The JSON Paper Edit
After your scratchpad, populate the `"clips"` array with the final timeline.

────────────────────────────────────────────────────────────────────────────────
## RULES FOR THE EDIT
────────────────────────────────────────────────────────────────────────────────

### Primary clips — The narration backbone (carries BOTH video AND audio)

Use clips where `has_speech` is true. Each entry in the Master Index is already \
a natural sentence or thought. You MUST use the EXACT `start_ms` and `end_ms` from \
the index as your `in_ms` and `out_ms`. Never trim a segment shorter — that \
slices sentences in half and sounds terrible.

Good: index has {start_ms: 5460, end_ms: 9480} → your clip is {in_ms: 5460, out_ms: 9480}
Bad:  {in_ms: 5460, out_ms: 7460} ← WRONG, cuts the sentence short.

Skip entries whose transcript is only "[background chatter]", \
"[background noise]", or inarticulate fragments.

Set `clip_type` to `"primary"` for these clips.

### Cutaway clips — Visual variety (NO audio in the final edit)
Use clips where `has_speech` is false (or any clip used purely for visuals).
- Cutaway clips play OVER the preceding primary clip's audio for visual variety.
- Clips should be short: **2–4 seconds** (e.g. in_ms: 0, out_ms: 3000).
- `in_ms`/`out_ms` refer to the source file's timestamps.
- Total cutaway duration after a primary clip must NOT exceed that primary's duration.
- Vary your selections — never reuse the same source file twice if possible.

Set `clip_type` to `"cutaway"` for these clips.

### Match the Topic — SEMANTIC MATCHING IS CRITICAL
Read the primary clip's transcript, identify the subject, then pick cutaway \
clips whose `tags` or `visual_summary` relate directly to that subject.

Matching examples:
| Primary says…                    | Cutaway tags to pick       |
|----------------------------------|----------------------------|
| "We wrote so much code"          | laptops, coding            |
| "the food was incredible"        | food, drinks               |
| "this painting blew my mind"     | painting, art              |
| "people flew from all over"      | crowd, teamwork            |
| "energy here is crazy"           | hackathon, crowd           |
| "build agents"                   | laptops, coding            |

### Stock Footage (Synth Clips) — When User Cutaways Are Insufficient
If no cutaway in the Master Index matches a topic the speaker mentions, you \
can request **stock footage from Pexels** by creating a synth clip:
- Set `clip_type` to `"synth"` (NOT `"cutaway"`)
- Set `source_file` to `"__SYNTH__"`
- Provide a **specific, vivid** `search_query` (e.g. "close up of hands typing \
on a laptop keyboard" — NOT just "typing")
- `in_ms` = 0, `out_ms` = desired length in ms (e.g. 3000 for 3 s)
- The Synthesizer agent will automatically search Pexels and download the footage.
- **Prefer real cutaway clips** from the index when a reasonable match exists. \
Use synth clips only when nothing in the index fits.

### Overlay Clips (V2 — Picture-in-Picture Compositing)
You can create **overlay clips** that appear as a picture-in-picture (PiP) on \
top of the main timeline. This is useful for:
- Showing the speaker (primary) in a small PiP window while cutaway plays fullscreen

To create an overlay clip:
- Set `clip_type` to `"overlay"`
- `source_file` must be a real asset from the Master Index
- `in_ms` / `out_ms` are timestamps within the SOURCE file
- `timeline_start_ms` (REQUIRED) — when on the final timeline this overlay appears
- `overlay_preset` — one of: `"pip-br"` (bottom-right), `"pip-bl"` (bottom-left), \
`"pip-tr"` (top-right), `"pip-tl"` (top-left), `"pip-center"`, `"side-by-side"`
- Default preset is `"pip-br"` if omitted

**STRICT overlay rules (MUST follow):**
- **ONLY use overlays on cutaway segments that are ≥ 5 seconds long.** Short cutaway \
  clips (< 5 s) must NEVER have an overlay — it looks clumsy and jarring.
- Use **at most 1–2 overlay clips per video**. Less is more.
- Overlays add a fade-in/fade-out so the overlay duration must be ≥ 2 s to look good.
- **Do NOT overlay on every cutaway** — only the single most impactful moment.
- If the video is ≤ 15 seconds total, do NOT use any overlays at all.
- Keep overlay duration ≤ the cutaway segment it covers minus 1 s (leave breathing room).
## CRITICAL RULE: METAPHORS, SLANG, AND IDIOMS (DO NOT BE LITERAL)
Human speakers use metaphors and slang. You MUST evaluate the business and emotional context of the transcript before selecting cutaway footage or generating a search query for the Synthesizer. DO NOT take metaphors literally. 
- If the speaker says "this idea is fire" or "the energy is fire", DO NOT show literal flames. Show an "excited crowd", "celebration", or "high energy teamwork".
- If the speaker says "we were putting out fires", DO NOT show burning buildings. Show "stressed developers typing", "fast-paced office", or "team collaboration".
- If the speaker says "we are swimming in data", DO NOT show oceans or water. Show "server racks", "abstract data visualizations", or "scrolling code".
Always deduce the UNDERLYING MEANING of the sentence before choosing the visual.

### Duration Constraint
Total video duration is determined ONLY by primary clips. Cutaways add visual \
variety, not extra time. If the brief asks for 60 seconds, your primary clips \
must sum to ~55–65 s.

### Story Structure
Order clips to tell a coherent story:
  1. **Hook** (first 3–5 s) — the single most attention-grabbing statement. \
     Each segment carries a `salience` score (0-1) from footage analysis — \
     STRONGLY prefer the highest-salience speech segment for the hook.
  2. **Body** — the core narrative arc; alternate between talking heads and \
     cutaway clips to maintain visual energy. Use `salience`/`energy`/`emotion` \
     to prioritise the best moments and drop low-salience filler.
  3. **Conclusion** — a memorable closing line or call-to-action.

### Editorial fields (populate these per clip so the QA reviewer can audit your reasoning)
For every clip also set: `act` ("hook" | "body" | "conclusion"), `beat` \
(one-line role in the story), `editorial_purpose` (why it earns its place), and \
`confidence` (0-1, how sure you are it belongs).

────────────────────────────────────────────────────────────────────────────────
## HOW THE DIRECTOR PROCESSES YOUR EDIT
────────────────────────────────────────────────────────────────────────────────

Timeline model:
```
AUDIO:  |---- primary clip_001 audio ----|---- primary clip_003 audio ----|
VIDEO:  |primary 001|cutaway 002|primary 001|cutaway 004|primary 003 cont|
```
- **Primary clip** → video + audio both play.
- **Cutaway immediately after primary** → its video replaces the screen, but \
the preceding primary clip's audio continues underneath. Cutaway audio is discarded.
- Multiple cutaway clips can follow one primary clip.

────────────────────────────────────────────────────────────────────────────────
## OUTPUT FORMAT
────────────────────────────────────────────────────────────────────────────────

Output ONLY valid JSON — no markdown, no explanation, no code fences.
Every clip must reference a real asset_file from the Master Index with valid timestamps.
clip_id must be unique ("clip_001", "clip_002", …).
Prefer "cut" transitions.

{
    "scratchpad": "HOOK: ... BODY: ... CUTAWAY MAPPING: ... MATH CHECK: ...",
    "title": "string",
    "total_duration_ms": integer,
    "music_prompt": "string or null",
    "music": {
        "vibe": "e.g. uplifting, tense, playful (matches the story's emotion)",
        "genre": "e.g. lo-fi hip hop, cinematic orchestral, electronic",
        "energy_curve": {"hook": "punchy", "body": "steady build", "conclusion": "resolve"},
        "prompt": "one concrete search phrase for a music library"
    },
    "clips": [
        {
            "clip_id": "clip_001",
            "source_file": "/absolute/path.mp4",
            "in_ms": 5460,
            "out_ms": 9480,
            "clip_type": "primary",
            "overlay_text": null,
            "transition": "cut",
            "search_query": null,
            "description": "Speaker: 'So I'm here at the hackathon and the energy is crazy'"
        },
        {
            "clip_id": "clip_002",
            "source_file": "/absolute/path/scenic.mp4",
            "in_ms": 1000,
            "out_ms": 3000,
            "clip_type": "cutaway",
            "overlay_text": null,
            "transition": "cut",
            "search_query": null,
            "description": "Cutaway: Hackathon banners and event signage"
        },
        {
            "clip_id": "clip_003",
            "source_file": "/absolute/path/speaker.mp4",
            "in_ms": 5460,
            "out_ms": 8460,
            "clip_type": "overlay",
            "overlay_text": null,
            "transition": "cut",
            "search_query": null,
            "timeline_start_ms": 9480,
            "overlay_preset": "pip-br",
            "description": "PiP: Speaker face during cutaway"
        }
    ],
    "overlay_clips": [
        {
            "clip_id": "clip_003",
            "source_file": "/absolute/path/speaker.mp4",
            "in_ms": 5460,
            "out_ms": 8460,
            "clip_type": "overlay",
            "timeline_start_ms": 9480,
            "overlay_preset": "pip-br",
            "description": "PiP: Speaker face during cutaway"
        }
    ]
}
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


def _validate_paper_edit(paper_edit: dict, master_index: list[dict]) -> list[str]:
    """
    Validate + CORRECT a Paper Edit against the master index.

    Philosophy: fix what is safe to fix silently (enum typos, out-of-range enum
    values, duplicate ids, cutaway durations, small primary-edge drift) and only
    return *errors* for things that can't be safely repaired (missing source,
    inverted in/out, missing synth query). The Scripter retries while errors
    remain, and on the last attempt returns the corrected-but-imperfect edit.
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
                if ctype == ClipType.PRIMARY.value:
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

        total_ms += out_ms - in_ms

    # Warn (non-blocking) about heavily reused sources.
    for src, n in source_use.items():
        if n >= 4:
            logger.info("📝 Scripter: source reused %d times: %s", n, src)

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
            "errors": [{
                "agent": "scripter",
                "message": "No user prompt provided",
                "phase": Phase.SCRIPTING,
                "recoverable": False,
            }],
        }

    if not master_index:
        return {
            "phase": Phase.ERROR,
            "errors": [{
                "agent": "scripter",
                "message": "Master index is empty — Archivist may have failed",
                "phase": Phase.SCRIPTING,
                "recoverable": False,
            }],
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
        if not has_speech:
            # For non-speech entries, add tags and visual summary for matching
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
        f"CREATIVE BRIEF:\n{user_prompt}\n\n"
        f"AVAILABLE FOOTAGE SUMMARY:\n"
        f"- Speech clips (potential primary): {len(speech_entries)} segments, {speech_total_ms / 1000:.0f}s total\n"
        f"- Visual clips (potential cutaway): {len(visual_entries)} clips, {visual_total_ms / 1000:.0f}s total\n\n"
        f"MASTER INDEX ({len(condensed_index)} segments):\n"
        f"{json.dumps(condensed_index, indent=2)}"
    )

    # For edits, include the existing paper edit and switch to an edit-specific prompt
    if is_edit:
        user_message += (
            f"\n\n═══ CURRENT PAPER EDIT (modify this) ═══\n"
            f"{json.dumps(existing_paper_edit, indent=2)}\n\n"
            f"IMPORTANT: The [EDIT REQUEST] above describes what the user wants changed.\n"
            f"You MUST start from the CURRENT PAPER EDIT above and apply ONLY the requested changes.\n"
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
    if not is_edit and existing_paper_edit and critic_feedback and not critic_feedback.get("approved", True):
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
                        types.Content(role="user", parts=[
                            types.Part.from_text(text=f"{SCRIPTER_SYSTEM_PROMPT}\n\n{attempt_message}"),
                        ]),
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
            validation_errors = _validate_paper_edit(paper_edit, master_index)
            if validation_errors:
                logger.warning(
                    f"📝 Scripter: Validation errors on attempt {attempt}: "
                    f"{validation_errors}"
                )
                last_error = "; ".join(validation_errors)
                # Retry on earlier attempts; on the last attempt fall through and
                # return the (imperfect) edit so the user can fix it in the
                # human-review gate — a usable draft beats a hard pipeline error.
                if attempt < max_attempts:
                    # Self-repair: tell the model exactly what to fix next time.
                    repair_note = (
                        "\n\n═══ FIX THESE VALIDATION ERRORS ═══\n"
                        "Your previous attempt had these problems — return a "
                        "corrected edit that resolves ALL of them:\n"
                        + "\n".join(f"- {e}" for e in validation_errors)
                    )
                    continue
                logger.warning("📝 Scripter: Returning Paper Edit with warnings")

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
        "errors": [{
            "agent": "scripter",
            "message": f"Failed to generate valid Paper Edit after {max_attempts} attempts: {last_error}",
            "phase": Phase.SCRIPTING,
            "recoverable": True,
        }],
    }
