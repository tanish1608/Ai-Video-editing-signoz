"""
Agent 2.5: The Editorial Critic (QA)
─────────────────────────────────────
Reviews the Scripter's Paper Edit against the creative brief and the editorial
rules the Scripter is *supposed* to follow, then returns structured feedback.
The orchestrator uses that feedback to drive a bounded review→revise loop
(Scripter → Critic → Scripter → human gate) — a genuine multi-agent handoff
that is also the marquee trace in SigNoz.

This is the self-repair loop elevated to a first-class, observable agent. It is
LLM-powered (Gemini) but strictly bounded: at most `CRITIC_MAX_ITERATIONS`
revise cycles, and it degrades to "approved" on any error so it can never block
the pipeline.
"""

from __future__ import annotations

import json
import logging

from google import genai
from google.genai import types

from kinetograph.config import settings
from kinetograph.observability import llm_span, record_gemini_tokens
from kinetograph.schema import CriticFeedback
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)

CRITIC_MAX_ITERATIONS = 2  # max Scripter→Critic revise cycles


CRITIC_SYSTEM_PROMPT = """You are a ruthless but fair EDITORIAL QA reviewer for short-form
video in the selected project mode. \
An editor has produced a "paper edit" (an ordered list of clips) from a creative brief and
available footage. \
Your job is to critique it against professional standards and the brief, and decide whether it
needs revision.

REVIEW CRITERIA (score each mentally, then give an overall 0-10):
1. OPENING — Does the first shot/thought orient the viewer and serve the chosen tone?
2. BRIEF ALIGNMENT — Does the edit actually deliver what the brief asked for (topic, length, tone)?
3. PROGRESSION — Narration needs a coherent thought and payoff; highlights need a visual arc.
4. PACING — Does shot duration suit the brief? Avoid redundant shots and forced alternation.
5. CUTAWAY RELEVANCE — Do cutaways semantically match what's being said underneath them?
6. DURATION — Does rendered duration land within ±15% of the requested length?
7. TECHNICAL SANITY — Valid clip types, no obviously broken in/out points, cutaways not longer
than the primary they cover. Check source evidence: no fabricated quotes or changed meanings.

SEVERITY:
- "blocker": must be fixed before rendering (weak hook, wrong length, irrelevant cutaways, broken
structure).
- "warning": should be fixed (minor pacing/relevance issues).
- "note": optional polish.

Set approved=false if there is ANY blocker. Be specific: reference the clip_id and give a concrete
fix.
Do NOT rewrite the edit — only critique it. Output MUST match the requested JSON schema exactly."""


def _condense_edit_for_review(paper_edit: dict) -> dict:
    """Trim the edit to the fields the critic needs (keeps the prompt small)."""
    clips = []
    for c in paper_edit.get("clips", []):
        clips.append(
            {
                "clip_id": c.get("clip_id"),
                "clip_type": c.get("clip_type"),
                "source_file": c.get("source_file"),
                "in_ms": c.get("in_ms"),
                "out_ms": c.get("out_ms"),
                "duration_ms": (c.get("out_ms", 0) - c.get("in_ms", 0)),
                "description": c.get("description", ""),
                "act": c.get("act"),
                "editorial_purpose": c.get("editorial_purpose", ""),
            }
        )
    return {
        "title": paper_edit.get("title", ""),
        "total_duration_ms": paper_edit.get("total_duration_ms", 0),
        "scratchpad": paper_edit.get("scratchpad", ""),
        "clips": clips,
    }


async def critic_node(state: GraphState) -> dict:
    """LangGraph node — reviews the current paper edit and returns feedback.

    Writes `critic_feedback` (a CriticFeedback dict) and bumps `critic_iteration`.
    Never raises — on any failure it approves so the pipeline proceeds.
    """
    paper_edit = state.get("paper_edit")
    iteration = state.get("critic_iteration", 0) + 1

    if not paper_edit or not paper_edit.get("clips"):
        # Nothing to review — approve and move on.
        return {
            "critic_iteration": iteration,
            "critic_feedback": CriticFeedback(
                approved=True, summary="No edit to review."
            ).model_dump(),
        }

    user_prompt = state.get("user_prompt", "")
    review_payload = _condense_edit_for_review(paper_edit)

    evidence = [
        {
            key: entry.get(key)
            for key in (
                "asset_file",
                "start_ms",
                "end_ms",
                "transcript",
                "visual_descriptions",
            )
        }
        for entry in state.get("master_index", [])
    ]
    user_message = (
        f"EDITING MODE: {state.get('editing_mode', 'narration')}\n"
        "Judge highlights by visual progression, not a mandatory spoken hook.\n"
        f"SOURCE EVIDENCE: {json.dumps(evidence, default=str)}\n"
        f"CREATIVE BRIEF:\n{user_prompt}\n\n"
        f"PAPER EDIT UNDER REVIEW:\n{json.dumps(review_payload, indent=2)}\n\n"
        f"This is review iteration {iteration} of at most {CRITIC_MAX_ITERATIONS}. "
        f"Critique it now."
    )

    logger.info(
        "🧐 Critic: Reviewing edit (%d clips, iteration %d)...",
        len(paper_edit.get("clips", [])),
        iteration,
    )

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        with llm_span(
            "gemini", settings.gemini_model, **{"llm.role": "critic", "critic.iteration": iteration}
        ) as span:
            response = await client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(text=f"{CRITIC_SYSTEM_PROMPT}\n\n{user_message}"),
                        ],
                    ),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4096,
                    response_mime_type="application/json",
                    response_schema=CriticFeedback,
                ),
            )
            record_gemini_tokens(span, response, settings.gemini_model)

        feedback = CriticFeedback.model_validate_json(response.text)
        logger.info(
            "🧐 Critic: score=%.1f approved=%s blockers=%d issues=%d",
            feedback.overall_score,
            feedback.approved,
            len(feedback.blockers),
            len(feedback.issues),
        )
        return {
            "critic_iteration": iteration,
            "critic_feedback": feedback.model_dump(),
            "phase": Phase.SCRIPTED,
        }
    except Exception as exc:
        # Degrade gracefully — a critic failure must never block the pipeline.
        logger.warning("🧐 Critic: review failed (%s) — approving to proceed", exc)
        return {
            "critic_iteration": iteration,
            "critic_feedback": CriticFeedback(
                approved=True,
                summary=f"Critic unavailable ({exc}); proceeding without review.",
            ).model_dump(),
        }
