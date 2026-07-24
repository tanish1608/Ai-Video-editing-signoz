"""
SigNoz smoke test — emit a representative multi-agent pipeline trace WITHOUT
running the real pipeline (no Gemini/ElevenLabs/NVIDIA/Pexels keys or media).

Use it to validate the whole OpenTelemetry → SigNoz path end-to-end: run this,
then query SigNoz (UI or MCP) for the `kinetograph-pipeline` service and you
should see one trace with the agent → llm/tool span tree, including the
Scripter → Critic → Scripter revise loop.

Usage:
    KINETOGRAPH_TELEMETRY=1 \
    OTEL_EXPORTER_OTLP_ENDPOINT=<your-signoz-otlp-http-endpoint> \
    OTEL_EXPORTER_OTLP_HEADERS=signoz-ingestion-key=<INGESTION_KEY> \
    .venv/bin/python backend/observability/smoke_test.py

For self-hosted SigNoz the endpoint is usually http://localhost:4318 and no
headers are needed. For SigNoz Cloud use the ingest URL + access-token header.
NOTE: the ingestion key is DIFFERENT from the API key used to *query* SigNoz.
"""

from __future__ import annotations

import time

from opentelemetry import trace

from kinetograph.observability import (
    agent_span,
    init_telemetry,
    llm_span,
    record_retry,
    record_tokens,
    tool_span,
)


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000.0)


def main() -> None:
    init_telemetry()

    tracer = trace.get_tracer("kinetograph.smoke")
    # One root span = one trace, so the whole run groups under a single entry.
    with tracer.start_as_current_span("pipeline.run") as root:
        root.set_attribute("kinetograph.project", "smoke-demo")
        root.set_attribute("kinetograph.brief", "20s hackathon highlight reel")

        # 1) Archivist — STT tool + a VLM fan-out (per-segment), with salience.
        with agent_span("archivist", phase="ingesting") as a:
            with tool_span("elevenlabs_stt", **{"stt.model": "scribe_v2"}):
                _sleep(40)
            for i, sal in enumerate((0.82, 0.35, 0.91, 0.6)):
                with llm_span("nvidia", "nvidia/nemotron-nano-12b-v2-vl",
                              **{"kinetograph.segment_start_ms": i * 4000}) as v:
                    record_tokens(v, input_tokens=1200, output_tokens=90,
                                  system="nvidia", model="nemotron")
                    v.set_attribute("kinetograph.salience", sal)
                    v.set_attribute("kinetograph.energy", 0.5 + i * 0.1)
                    v.set_attribute("kinetograph.clip_type", "TALKING_HEAD" if i % 2 else "ACTION")
                    _sleep(15)
            a.set_attribute("kinetograph.index_size", 12)

        # 2) Scripter (first pass) → 3) Critic (finds a blocker) → revise loop.
        with agent_span("scripter", phase="scripting") as s1:
            with llm_span("gemini", "gemini-2.5-flash", **{"llm.attempt": 1}) as g:
                record_tokens(g, input_tokens=8200, output_tokens=1400,
                              system="gemini", model="gemini-2.5-flash")
                _sleep(30)
            s1.set_attribute("kinetograph.clip_count", 9)

        with agent_span("critic", phase="scripting") as c1:
            with llm_span("gemini", "gemini-2.5-flash",
                          **{"llm.role": "critic", "critic.iteration": 1}) as g:
                record_tokens(g, input_tokens=2100, output_tokens=320,
                              system="gemini", model="gemini-2.5-flash")
                _sleep(20)
            c1.set_attribute("critic.approved", False)
            c1.set_attribute("critic.overall_score", 5.5)
            c1.set_attribute("critic.blockers", 1)

        # Scripter revise (self-repair) → Critic approves.
        with agent_span("scripter", phase="scripting") as s2:
            record_retry("scripter")
            with llm_span("gemini", "gemini-2.5-flash", **{"llm.attempt": 1, "llm.revise": True}) as g:
                record_tokens(g, input_tokens=9000, output_tokens=1500,
                              system="gemini", model="gemini-2.5-flash")
                _sleep(30)
            s2.set_attribute("kinetograph.clip_count", 8)

        with agent_span("critic", phase="scripting") as c2:
            with llm_span("gemini", "gemini-2.5-flash",
                          **{"llm.role": "critic", "critic.iteration": 2}) as g:
                record_tokens(g, input_tokens=2000, output_tokens=180,
                              system="gemini", model="gemini-2.5-flash")
                _sleep(20)
            c2.set_attribute("critic.approved", True)
            c2.set_attribute("critic.overall_score", 8.5)

        # 4) Render stack — ffmpeg tool spans.
        for node, phase in (("director", "rendering"),
                            ("captioner", "captioning"),
                            ("sound_engineer", "mastering"),
                            ("export", "exporting")):
            with agent_span(node, phase=phase):
                with tool_span("ffmpeg", **{"ffmpeg.description": f"{node} render"}):
                    _sleep(25)

        root.set_attribute("kinetograph.phase.out", "complete")

    # Flush the batch exporter before the process exits.
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
    print("✅ Smoke trace emitted. Look for service 'kinetograph-pipeline' in SigNoz.")


if __name__ == "__main__":
    main()
