"""
Agent: Human Review Gate

Pauses the pipeline for user approval of the Paper Edit before rendering.
Uses LangGraph interrupt() for human-in-the-loop semantics.

In the deterministic pipeline architecture this is the ONLY place the
pipeline halts and waits for user input.  After the scripter generates
(or revises) a Paper Edit, execution flows here automatically:

  scripter → human_review → [approved] → synthesizer/director
                           → [rejected] → scripter (loop)
"""

from __future__ import annotations

import json
import logging

from langgraph.types import interrupt

from kinetograph.config import settings
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


# ── Human Review Node ────────────────────────────────────────────────────────


async def human_review_node(state: GraphState) -> dict:
    """LangGraph node — Human-in-the-loop approval gate.

    Reads the Paper Edit from state (set by the scripter), presents it
    to the user via interrupt(), and processes their approve/reject decision.

    Returns:
        On approve: ``{phase: "approved", approved_edit: ...}``
        On reject:  ``{phase: "scripted", edit_instruction: reason, errors: [...]}``
    """
    paper_edit = state.get("paper_edit")

    if not paper_edit:
        return {
            "phase": Phase.ERROR.value,
            "errors": [
                {
                    "agent": "human_review",
                    "message": "No Paper Edit to review",
                    "phase": "awaiting_approval",
                    "recoverable": False,
                }
            ],
        }

    logger.info(
        "Human Review: Paper Edit ready — %d clips, %.1fs",
        len(paper_edit.get("clips", [])),
        paper_edit.get("total_duration_ms", 0) / 1000.0,
    )

    # Persist for the web UI to read
    review_path = settings.state_dir / "paper_edit_review.json"
    with open(review_path, "w") as f:
        json.dump(paper_edit, f, indent=2)

    # ── INTERRUPT — pipeline halts here until resumed via /api/pipeline/approve ──
    decision = interrupt(
        {
            "type": "paper_edit_review",
            "message": "Paper Edit ready for your review",
            "paper_edit": paper_edit,
        }
    )

    # ── Process the human's decision ─────────────────────────────────────

    if isinstance(decision, dict):
        action = decision.get("action", "approve")

        if action == "reject":
            reason = decision.get("reason", "User rejected the Paper Edit")
            logger.info("Human Review: REJECTED — %s", reason)
            return {
                "phase": Phase.SCRIPTED.value,
                "edit_instruction": reason,
                "errors": [
                    {
                        "agent": "human_review",
                        "message": reason,
                        "phase": "awaiting_approval",
                        "recoverable": True,
                    }
                ],
            }

        # Approve (possibly with user-edited paper_edit from the timeline)
        approved = {**paper_edit, **decision.get("paper_edit", {})}
        logger.info(
            "Human Review: APPROVED — %d clips",
            len(approved.get("clips", [])),
        )

        approved_path = settings.state_dir / "approved_edit.json"
        with open(approved_path, "w") as f:
            json.dump(approved, f, indent=2)

        return {
            "phase": Phase.APPROVED.value,
            "approved_edit": approved,
        }

    # Simple string approval
    if isinstance(decision, str) and decision.lower() in (
        "approve",
        "yes",
        "ok",
        "go",
    ):
        logger.info("Human Review: APPROVED (simple)")
        approved_path = settings.state_dir / "approved_edit.json"
        with open(approved_path, "w") as f:
            json.dump(paper_edit, f, indent=2)
        return {
            "phase": Phase.APPROVED.value,
            "approved_edit": paper_edit,
        }

    # Unknown response — default to approval
    logger.warning("Human Review: Unknown decision %r — defaulting to approve", decision)
    return {
        "phase": Phase.APPROVED.value,
        "approved_edit": paper_edit,
    }
