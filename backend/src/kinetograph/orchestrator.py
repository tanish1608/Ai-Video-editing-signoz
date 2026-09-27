"""
The Orchestrator — Deterministic Sequential LangGraph StateGraph.

Fixed-order pipeline with conditional edges.  No LLM-powered routing.
Each agent runs in a known sequence, with human-in-the-loop approval
between scripting and rendering.

Graph topology (new video):
  archivist → scripter → human_review → [synthesizer?] → director
            → captioner → sound_engineer → export → END

Edit pipelines start from the appropriate agent (scripter, director,
captioner, or sound_engineer) determined by a simple rule-based
classifier in the server endpoint — no LLM routing needed.
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy
from opentelemetry.trace import Status, StatusCode

from kinetograph.agents.archivist import archivist_node
from kinetograph.agents.captioner import captioner_node
from kinetograph.agents.critic import critic_node
from kinetograph.agents.director import director_node
from kinetograph.agents.export import export_node
from kinetograph.agents.producer import human_review_node
from kinetograph.agents.scripter import scripter_node
from kinetograph.agents.sound_engineer import sound_engineer_node
from kinetograph.agents.synthesizer import synthesizer_node
from kinetograph.observability import agent_span, record_agent_error
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


# ── Retry Policy ──────────────────────────────────────────────────────────────

_api_retry = RetryPolicy(
    max_attempts=3,
    initial_interval=2.0,
    backoff_factor=2.0,
)


# ── Phase Normalization ──────────────────────────────────────────────────────


def _normalize_phase(result: dict) -> dict:
    """Convert Phase enum values to plain strings for checkpoint serialization.

    Prevents 'Deserializing unregistered type kinetograph.state.Phase'
    warnings from LangGraph's JsonPlusSerializer.
    """
    phase = result.get("phase")
    if isinstance(phase, Phase):
        result["phase"] = phase.value
    for err in result.get("errors", []):
        if isinstance(err, dict) and isinstance(err.get("phase"), Phase):
            err["phase"] = err["phase"].value
    return result


# ── Node Wrappers ────────────────────────────────────────────────────────────


def _make_node(agent_fn, agent_name: str):
    """Wrap an agent node: OTel agent span + completion tracking + Phase norm.

    This is the single choke point that makes every agent legible in SigNoz —
    one span per agent per run, tagged with the phase it produced and a few
    key output counts, with errors recorded on the span.
    """

    async def wrapped(state: GraphState) -> dict:
        with agent_span(agent_name, phase=_phase_str(state.get("phase"))) as span:
            result = await agent_fn(state)
            result["completed_agents"] = [agent_name]
            result = _normalize_phase(result)
            # Attach a few cheap, high-signal output attributes for the trace.
            out_phase = _phase_str(result.get("phase"))
            span.set_attribute("kinetograph.phase.out", out_phase)
            if "master_index" in result:
                span.set_attribute("kinetograph.index_size", len(result["master_index"] or []))
            pe = result.get("paper_edit") or result.get("approved_edit")
            if isinstance(pe, dict) and "clips" in pe:
                span.set_attribute("kinetograph.clip_count", len(pe.get("clips") or []))
            errs = result.get("errors")
            if errs:
                span.set_attribute("kinetograph.error_count", len(errs))
            # Agents signal failure by RETURNING {"phase": "error"} rather than
            # raising, so mark the span as errored here — otherwise a failed
            # agent shows up green in SigNoz. Include the latest error message.
            if out_phase == "error":
                msg = errs[-1].get("message", "agent error") if errs else "agent error"
                span.set_status(Status(StatusCode.ERROR, msg))
                record_agent_error(agent_name)
            return result

    wrapped.__name__ = agent_name
    wrapped.__qualname__ = agent_name
    return wrapped


def _phase_str(phase) -> str:
    """Plain string form of a Phase enum-or-string (for span attributes)."""
    if isinstance(phase, Phase):
        return phase.value
    return str(phase) if phase else ""


# ── Deterministic Routing Functions ──────────────────────────────────────────


def _route_after_archivist(state: GraphState) -> Literal["scripter", "error_handler"]:
    """Archivist → scripter (or error_handler on failure)."""
    phase = state.get("phase")
    if phase in (Phase.ERROR, Phase.ERROR.value, "error"):
        return "error_handler"
    return "scripter"


def _is_error_phase(state: GraphState) -> bool:
    """True when a node has signalled a terminal error via its returned phase."""
    phase = state.get("phase")
    return phase in (Phase.ERROR, Phase.ERROR.value, "error")


def _route_on_error(next_node: str):
    """Route to error_handler when the previous node errored, else to next_node.

    Post-approval agents (director/captioner/sound_engineer/export) swallow
    exceptions into {"phase": Phase.ERROR} dicts rather than raising, so without
    these guards the graph would march on and export would report COMPLETE on a
    failed render.
    """

    def _router(state: GraphState) -> str:
        if _is_error_phase(state):
            return "error_handler"
        return next_node

    _router.__name__ = f"_route_on_error_to_{next_node}"
    return _router


def _route_after_critic(state: GraphState) -> Literal["scripter", "human_review"]:
    """After the Critic reviews the edit: revise (→ scripter) or proceed (→ human).

    Revises only when the Critic flags a blocking problem AND we're under the
    iteration cap — a bounded self-repair loop, never an infinite debate.
    """
    from kinetograph.agents.critic import CRITIC_MAX_ITERATIONS
    from kinetograph.schema import CriticFeedback

    if _is_error_phase(state):
        return "human_review"  # let the user see the (imperfect) edit rather than dead-end

    fb_dict = state.get("critic_feedback") or {}
    iteration = state.get("critic_iteration", 0)
    try:
        feedback = CriticFeedback.model_validate(fb_dict)
    except Exception:
        return "human_review"

    if feedback.needs_revision() and iteration < CRITIC_MAX_ITERATIONS:
        return "scripter"
    return "human_review"


def _route_after_review(
    state: GraphState,
) -> Literal["scripter", "synthesizer", "director"]:
    """After human review: rejected → scripter, approved → synth check.

    If the approved edit contains synth clips (clip_type='synth'), route
    to the synthesizer first; otherwise skip straight to director.
    """
    phase = state.get("phase")
    if phase not in (Phase.APPROVED, Phase.APPROVED.value, "approved"):
        # Rejected or needs revision — loop back to scripter
        return "scripter"

    # Approved — check for synth clips
    approved = state.get("approved_edit") or {}
    clips = approved.get("clips", [])
    if any(c.get("clip_type") == "synth" for c in clips):
        return "synthesizer"
    return "director"


# ── Error Handler ────────────────────────────────────────────────────────────


async def error_handler_node(state: GraphState) -> dict:
    """Global error handler — logs errors and terminates gracefully."""
    errors = state.get("errors", [])
    if errors:
        latest = errors[-1]
        logger.error(
            "Pipeline Error [%s] at phase %s: %s",
            latest.get("agent", "?"),
            latest.get("phase", "?"),
            latest.get("message", "Unknown error"),
        )
    return {"phase": "error"}


# ── Graph Builder ────────────────────────────────────────────────────────────


def build_graph(start_from: str = "archivist") -> StateGraph:
    """Build the deterministic sequential LangGraph StateGraph.

    Args:
        start_from: Entry-point node name.
            "archivist"      — full pipeline (new video creation).
            "scripter"       — content edits (skip ingestion).
            "director"       — re-render (skip scripting + approval).
            "captioner"      — captions-only edit.
            "sound_engineer" — audio/music-only edit.

    All nodes are registered regardless of start_from so that edges
    always resolve.  The entry point controls where execution begins.
    """
    builder = StateGraph(GraphState)

    # ── Nodes ────────────────────────────────────────────────────────────
    builder.add_node("archivist", _make_node(archivist_node, "archivist"), retry_policy=_api_retry)
    # No node-level retry for the scripter: it already runs its own 3-attempt
    # loop internally (JSON + validation + API errors), so a node retry would
    # stack to up to 9 Gemini calls for one node.
    builder.add_node("scripter", _make_node(scripter_node, "scripter"))
    builder.add_node("critic", _make_node(critic_node, "critic"))  # editorial QA
    builder.add_node("human_review", human_review_node)  # uses interrupt()
    builder.add_node(
        "synthesizer", _make_node(synthesizer_node, "synthesizer"), retry_policy=_api_retry
    )
    builder.add_node("director", _make_node(director_node, "director"))
    builder.add_node("captioner", _make_node(captioner_node, "captioner"))
    builder.add_node("sound_engineer", _make_node(sound_engineer_node, "sound_engineer"))
    builder.add_node("export", _make_node(export_node, "export"))
    builder.add_node("error_handler", error_handler_node)

    # ── Entry Point ──────────────────────────────────────────────────────
    builder.set_entry_point(start_from)

    # ── Edges — deterministic sequential flow ────────────────────────────

    # archivist → scripter (or error)
    builder.add_conditional_edges("archivist", _route_after_archivist)

    # scripter → critic (editorial QA reviews every fresh/ revised edit)
    builder.add_conditional_edges("scripter", _route_on_error("critic"))

    # critic → scripter (revise, bounded) | human_review (proceed)
    # This is the visible multi-agent handoff: Scripter → Critic → Scripter → human.
    builder.add_conditional_edges("critic", _route_after_critic)

    # human_review → scripter (rejected) | synthesizer/director (approved)
    builder.add_conditional_edges("human_review", _route_after_review)

    # synthesizer → director (or error)
    builder.add_conditional_edges("synthesizer", _route_on_error("director"))

    # director → sound_engineer → captioner → export → END
    # Each edge is conditional so a Phase.ERROR from any render-stage agent is
    # routed to the error_handler instead of silently continuing to success.
    builder.add_conditional_edges("director", _route_on_error("sound_engineer"))
    builder.add_conditional_edges("captioner", _route_on_error("export"))
    builder.add_conditional_edges("sound_engineer", _route_on_error("captioner"))
    builder.add_conditional_edges("export", _route_on_error(END))

    # error handler terminates
    builder.add_edge("error_handler", END)

    return builder


# ── Compilation Helpers ──────────────────────────────────────────────────────


def compile_graph(start_from: str = "archivist", checkpointer=None):
    """Compile the graph with an in-memory checkpointer.

    Args:
        start_from: archivist | scripter | director | captioner | sound_engineer
        checkpointer: LangGraph checkpointer (defaults to InMemorySaver).
    """
    if checkpointer is None:
        checkpointer = InMemorySaver()

    builder = build_graph(start_from)
    return builder.compile(checkpointer=checkpointer)
