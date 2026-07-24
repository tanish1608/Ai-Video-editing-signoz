"""Observability instrumentation tests — assert the span tree with an
in-memory exporter (no SigNoz collector or API keys required).

Run: cd backend && pytest tests/test_observability.py
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

# Install an in-memory tracer provider at import time (before anything else sets
# one). init_telemetry() is never called in unit tests, so the default is no-op
# until we override it here.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)

import kinetograph.observability as obs  # noqa: E402

obs._tracer = None  # force it to pick up the provider we just set


@pytest.fixture(autouse=True)
def _clear_spans():
    _EXPORTER.clear()
    yield


def _span_names():
    return [s.name for s in _EXPORTER.get_finished_spans()]


def test_agent_span_emitted_with_attributes():
    with obs.agent_span("scripter", phase="scripting") as span:
        span.set_attribute("kinetograph.clip_count", 5)
    spans = _EXPORTER.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "agent.scripter"
    assert s.attributes["agent.name"] == "scripter"
    assert s.attributes["kinetograph.phase"] == "scripting"
    assert s.attributes["kinetograph.clip_count"] == 5


def test_llm_span_sets_genai_attributes_and_tokens():
    with obs.llm_span("gemini", "gemini-2.5-flash") as span:
        obs.record_tokens(span, input_tokens=100, output_tokens=42,
                          system="gemini", model="gemini-2.5-flash")
    s = _EXPORTER.get_finished_spans()[0]
    assert s.name == "llm.gemini"
    assert s.attributes[obs.GEN_AI_SYSTEM] == "gemini"
    assert s.attributes[obs.GEN_AI_REQUEST_MODEL] == "gemini-2.5-flash"
    assert s.attributes[obs.GEN_AI_USAGE_INPUT_TOKENS] == 100
    assert s.attributes[obs.GEN_AI_USAGE_OUTPUT_TOKENS] == 42


def test_tool_span_records_error_and_reraises():
    with pytest.raises(ValueError):
        with obs.tool_span("ffmpeg"):
            raise ValueError("boom")
    s = _EXPORTER.get_finished_spans()[0]
    assert s.name == "tool.ffmpeg"
    assert s.status.status_code == StatusCode.ERROR


def test_nested_span_tree_parent_child():
    # A tool span opened inside an agent span should be a child of it.
    with obs.agent_span("director"):
        with obs.tool_span("ffmpeg"):
            pass
    spans = {s.name: s for s in _EXPORTER.get_finished_spans()}
    agent = spans["agent.director"]
    tool = spans["tool.ffmpeg"]
    assert tool.parent is not None
    assert tool.parent.span_id == agent.context.span_id


def test_make_node_wraps_agent_in_span():
    # The orchestrator's node factory is the choke point that instruments every
    # agent. A fake node run through it must emit an agent.<name> span.
    from kinetograph.orchestrator import _make_node
    from kinetograph.state import Phase

    async def fake_agent(state):
        return {"phase": Phase.SCRIPTED, "paper_edit": {"clips": [1, 2, 3]}}

    node = _make_node(fake_agent, "scripter")
    result = asyncio.run(node({"phase": Phase.INGESTING}))

    assert result["completed_agents"] == ["scripter"]
    s = next(s for s in _EXPORTER.get_finished_spans() if s.name == "agent.scripter")
    assert s.attributes["agent.name"] == "scripter"
    assert s.attributes["kinetograph.phase.out"] == "scripted"
    assert s.attributes["kinetograph.clip_count"] == 3


def test_make_node_records_agent_error():
    from kinetograph.orchestrator import _make_node

    async def failing_agent(state):
        raise RuntimeError("agent crashed")

    node = _make_node(failing_agent, "director")
    with pytest.raises(RuntimeError):
        asyncio.run(node({}))
    s = next(s for s in _EXPORTER.get_finished_spans() if s.name == "agent.director")
    assert s.status.status_code == StatusCode.ERROR


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
