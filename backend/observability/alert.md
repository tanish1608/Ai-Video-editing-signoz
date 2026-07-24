# Suggested SigNoz alerts

Create these in SigNoz → **Alerts → New Alert → Metrics based**.

## 1. Pipeline agent errors

- **Metric:** `kinetograph.agent.errors`
- **Aggregation:** sum, grouped by `agent`
- **Condition:** value **> 0** over the last **5 minutes**
- **Severity:** warning
- **Why:** any agent recording an error (VLM failure, render failure, Gemini
  error) fires immediately — the fastest way to catch a broken run in the demo.

## 2. LLM cost / token spike per run

- **Metric:** `kinetograph.llm.tokens`
- **Aggregation:** sum, grouped by `system`
- **Condition:** value **>** your token budget (e.g. **200000**) over **10 minutes**
- **Severity:** info
- **Why:** flags an unusually expensive run (e.g. a critic revise-loop that
  didn't converge, or an oversized master index blowing the Scripter context).

## 3. Trace-based: slow agent (optional)

- Use a **Trace based** alert on span `agent.scripter` (or `agent.director`)
  with p95 duration **>** a threshold to catch latency regressions per agent.

> Metric instrument names map 1:1 to the query builder — see
> `observability/README.md` for the full list.
