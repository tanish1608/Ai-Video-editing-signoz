"""
Observability — OpenTelemetry traces + metrics exported to SigNoz (OTLP).

This is the instrumentation layer that turns the pipeline into a *legible*
multi-agent system: every agent node, LLM call, and tool call becomes a span,
nested under one trace per pipeline run, with GenAI semantic-convention
attributes (model, token usage, latency) plus domain attributes (agent name,
clip counts, confidence, retry #). Metrics (per-agent latency, tokens, errors,
retries) feed SigNoz dashboards + alerts.

Design notes:
- **Safe by default.** If :func:`init_telemetry` is never called (e.g. in unit
  tests) the OpenTelemetry API returns no-op tracers/meters, so every helper
  here is a cheap no-op. Nothing in the pipeline depends on a collector being
  up — a down SigNoz just means spans are dropped, never a pipeline failure.
- **One choke point.** Wrapping the orchestrator's node factory instruments all
  agents at once; the LLM/tool helpers are dropped in at the call sites.
- Endpoint + headers come from the standard OTel env vars
  (``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_HEADERS``); default is a
  local SigNoz OTLP/HTTP collector at ``http://localhost:4318``.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)

SERVICE_NAME = "kinetograph-pipeline"

# GenAI semantic-convention attribute keys (stable subset).
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

_initialized = False
_tracer: Optional[trace.Tracer] = None

# Metric instruments (created on init; no-op meter otherwise).
_agent_latency_ms = None
_llm_tokens = None
_agent_errors = None
_agent_retries = None
_llm_calls = None


def _tracer_() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("kinetograph")
    return _tracer


def init_telemetry(service_name: str = SERVICE_NAME) -> None:
    """Configure the global OTel tracer + meter with an OTLP→SigNoz exporter.

    Idempotent and best-effort: any failure (missing collector, bad env) is
    logged and swallowed so the pipeline still runs. Honors ``OTEL_SDK_DISABLED``.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        logger.info("📡 Observability: OTEL_SDK_DISABLED=true — telemetry off")
        return

    # Opt-in: telemetry is off unless explicitly enabled, so normal dev runs
    # aren't spammed with OTLP connection retries when no collector is up. For
    # the SigNoz demo, set KINETOGRAPH_TELEMETRY=1 (and the OTLP endpoint).
    if os.getenv("KINETOGRAPH_TELEMETRY", "").lower() not in ("1", "true", "on"):
        logger.info("📡 Observability: disabled (set KINETOGRAPH_TELEMETRY=1 to enable → SigNoz)")
        return

    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": os.getenv("KINETOGRAPH_VERSION", "0.1.0"),
            }
        )

        # Traces
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

        # Metrics
        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)

        _init_instruments()

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        logger.info("📡 Observability: OTel initialized → %s (service=%s)", endpoint, service_name)
    except Exception:
        logger.warning("📡 Observability: failed to init telemetry (continuing without)", exc_info=True)


def _init_instruments() -> None:
    global _agent_latency_ms, _llm_tokens, _agent_errors, _agent_retries, _llm_calls
    meter = metrics.get_meter("kinetograph")
    _agent_latency_ms = meter.create_histogram(
        "kinetograph.agent.latency", unit="ms", description="Per-agent execution time",
    )
    _llm_tokens = meter.create_counter(
        "kinetograph.llm.tokens", unit="token", description="LLM tokens used",
    )
    _llm_calls = meter.create_counter(
        "kinetograph.llm.calls", unit="1", description="LLM calls made",
    )
    _agent_errors = meter.create_counter(
        "kinetograph.agent.errors", unit="1", description="Agent errors",
    )
    _agent_retries = meter.create_counter(
        "kinetograph.agent.retries", unit="1", description="Agent retry attempts",
    )


# ── Span helpers ──────────────────────────────────────────────────────────────

@contextmanager
def agent_span(agent_name: str, phase: Optional[str] = None, **attrs: Any) -> Iterator[Span]:
    """Wrap an agent node. Records latency + errors as metrics + span status."""
    start = time.perf_counter()
    with _tracer_().start_as_current_span(f"agent.{agent_name}") as span:
        span.set_attribute("agent.name", agent_name)
        if phase:
            span.set_attribute("kinetograph.phase", phase)
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
        try:
            yield span
        except Exception as exc:
            _record_error(span, agent_name, exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if _agent_latency_ms is not None:
                _agent_latency_ms.record(elapsed_ms, {"agent": agent_name})


@contextmanager
def tool_span(tool_name: str, **attrs: Any) -> Iterator[Span]:
    """Wrap an external tool/side-effect call (STT, Pexels, ffmpeg, …)."""
    with _tracer_().start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute("tool.name", tool_name)
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


@contextmanager
def llm_span(system: str, model: str, **attrs: Any) -> Iterator[Span]:
    """Wrap an LLM/VLM call with GenAI semantic-convention attributes.

    The caller should call :func:`record_tokens` on the yielded span once the
    response is available.
    """
    with _tracer_().start_as_current_span(f"llm.{system}") as span:
        span.set_attribute(GEN_AI_SYSTEM, system)
        span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
        if _llm_calls is not None:
            _llm_calls.add(1, {"system": system, "model": model})
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def record_tokens(span: Span, *, input_tokens: int = 0, output_tokens: int = 0,
                  system: str = "", model: str = "") -> None:
    """Attach token usage to an LLM span + the tokens counter."""
    if input_tokens:
        span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, int(input_tokens))
    if output_tokens:
        span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, int(output_tokens))
    if _llm_tokens is not None:
        base = {"system": system, "model": model}
        if input_tokens:
            _llm_tokens.add(int(input_tokens), {**base, "direction": "input"})
        if output_tokens:
            _llm_tokens.add(int(output_tokens), {**base, "direction": "output"})


def record_gemini_tokens(span: Span, response: Any, model: str) -> None:
    """Extract token usage from a google-genai response onto the span + metrics."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    record_tokens(
        span,
        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        system="gemini",
        model=model,
    )


def record_nvidia_tokens(span: Span, data: dict, model: str = "") -> None:
    """Extract token usage from an OpenAI-compatible (NVIDIA NIM) response body."""
    usage = (data or {}).get("usage") or {}
    record_tokens(
        span,
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        system="nvidia",
        model=model,
    )


def record_retry(agent_name: str) -> None:
    if _agent_retries is not None:
        _agent_retries.add(1, {"agent": agent_name})


def record_agent_error(agent_name: str) -> None:
    """Increment the agent-error counter for a node that FAILED by returning an
    error phase (rather than raising — those are counted in agent_span)."""
    if _agent_errors is not None:
        _agent_errors.add(1, {"agent": agent_name})


def _record_error(span: Span, agent_name: str, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))
    if _agent_errors is not None:
        _agent_errors.add(1, {"agent": agent_name})
