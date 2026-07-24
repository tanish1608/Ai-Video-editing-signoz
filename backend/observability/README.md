# Kinetograph Observability → SigNoz

The pipeline emits OpenTelemetry **traces** (one trace per run, nested agent →
LLM/tool spans) and **metrics** (per-agent latency, LLM tokens, retries, errors)
over OTLP/HTTP. Point them at SigNoz and you get a live, debuggable view of the
multi-agent graph.

## What gets instrumented

| Span | Emitted by | Key attributes |
|------|-----------|----------------|
| `agent.<name>` | `orchestrator._make_node` (all 8 agents + critic) | `agent.name`, `kinetograph.phase`, `kinetograph.phase.out`, `kinetograph.clip_count`, `kinetograph.index_size`, `kinetograph.error_count` |
| `llm.gemini` | Scripter, Critic | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `llm.attempt` |
| `llm.nvidia` | Archivist VLM (per segment → shows fan-out) | same GenAI attrs + `kinetograph.segment_start_ms` |
| `tool.elevenlabs_stt` | Archivist STT | `stt.model` |
| `tool.pexels_search` | Synthesizer | `pexels.query`, `pexels.orientation` |
| `tool.ffmpeg` | Director / Sound Engineer / Captioner | `ffmpeg.description` |

Metrics (instrument names): `kinetograph.agent.latency` (histogram, ms),
`kinetograph.llm.tokens` (counter, by system/model/direction),
`kinetograph.llm.calls`, `kinetograph.agent.errors`, `kinetograph.agent.retries`.

## Run SigNoz (self-hosted)

```bash
git clone -b main https://github.com/SigNoz/signoz.git
cd signoz/deploy/docker
docker compose up -d          # UI on http://localhost:3301, OTLP on :4317/:4318
```

Then in the repo `.env`:

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

(SigNoz Cloud instead: set the endpoint to your ingest URL and
`OTEL_EXPORTER_OTLP_HEADERS=signoz-ingestion-key=<KEY>`.)

Run the pipeline (`./dev.sh backend` then trigger a run, or
`kinetograph run "<brief>" --project demo`). Open SigNoz → **Traces** and you'll
see one trace per run; expand it for the `Scripter → Critic → Scripter → human`
handoff and the per-segment VLM fan-out under the Archivist.

## Dashboard & alert — PROVISIONED (us2 tenant `kind-quetzal`)

Both were created live via the SigNoz MCP against this pipeline's traces:

- **Dashboard** "Kinetograph — Multi-Agent Pipeline" (id `019f8557-8598-7f02-81af-a15035521e3b`)
  — 4 trace-based panels: Agent latency (P95) by `agent.name`, LLM calls by
  `gen_ai.request.model`, Agents runs & avg latency (table), Errored agent spans (list).
- **Email channel** `kinetograph-email` → tanish.vadhineni@gmail.com (test delivered).
- **Alert** "Kinetograph agent error" (id `019f855d-87bd-727f-83cd-fc3820025a73`)
  — traces threshold: `service.name = 'kinetograph-pipeline' AND has_error = true`,
  count > 0, at_least_once over a 5m rolling window (1m frequency) → email channel.

These are trace-based (robust: traces land immediately). The metric instruments
(`kinetograph.agent.latency`, `.llm.tokens`, `.llm.calls`, `.agent.retries`,
`.agent.errors`) also flow and can back additional metric panels/alerts.

> `dashboard.json` / `alert.md` in this dir are the portable specs for re-import
> on another tenant; the live versions above were pushed via MCP.
