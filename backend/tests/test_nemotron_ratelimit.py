"""
Stress test: NVIDIA NIM API rate limits & throughput
────────────────────────────────────────────────────
Simulates the Archivist workload: rapid sequential VLM calls
(one per keyframe, ~85–200 keyframes per project).

Measures:
  - Requests per minute achievable
  - Rate-limit (429) behaviour
  - Latency distribution (p50, p90, p99)
  - Error rate

Usage:
    export NVIDIA_API_KEY=nvapi-...
    python tests/test_nemotron_ratelimit.py
"""

import asyncio
import base64
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

MODEL = "nvidia/nemotron-nano-12b-v2-vl"
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

MEDIA_DIR = Path(__file__).parent.parent / "media_drop"
SAMPLE_VIDEOS = list(MEDIA_DIR.rglob("*.mp4"))

# How many requests to fire
TOTAL_REQUESTS = 30          # Start with 30 to test limits
CONCURRENCY = 5              # Parallel requests at a time
RETRY_AFTER_DEFAULT = 2.0    # Default backoff on 429 (seconds)
MAX_RETRIES = 3


def encode_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def extract_one_frame(video_path: str) -> str | None:
    """Extract a single frame from a video."""
    tmpdir = tempfile.mkdtemp(prefix="nemotron_rl_")
    out = os.path.join(tmpdir, "frame.jpg")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", "fps=0.5", "-frames:v", "1", "-q:v", "2", out,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return out if result.returncode == 0 and os.path.exists(out) else None


def build_payload(b64_img: str) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "/no_think"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Describe this video frame in 1-2 concise sentences. "
                               "Focus on: who/what is visible, the setting, actions.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
                    },
                ],
            },
        ],
        "max_tokens": 150,
        "temperature": 0.2,
        "stream": False,
    }


# ── Single request with retry ────────────────────────────────────────────────

async def send_request(
    client: httpx.AsyncClient,
    payload: dict,
    headers: dict,
    request_id: int,
) -> dict:
    """Send one request, retry on 429 with backoff. Returns timing info."""
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            resp = await client.post(
                API_URL, json=payload, headers=headers, timeout=120.0,
            )
            elapsed = time.monotonic() - t0

            if resp.status_code == 200:
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                return {
                    "id": request_id,
                    "status": 200,
                    "elapsed": elapsed,
                    "attempt": attempt,
                    "text": text[:80],
                }

            elif resp.status_code == 429:
                retry_after = float(
                    resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT)
                )
                print(
                    f"  ⚠ #{request_id} rate-limited (429) — "
                    f"waiting {retry_after:.1f}s (attempt {attempt}/{MAX_RETRIES})"
                )
                await asyncio.sleep(retry_after)
                continue

            else:
                return {
                    "id": request_id,
                    "status": resp.status_code,
                    "elapsed": elapsed,
                    "attempt": attempt,
                    "text": resp.text[:200],
                }

        except Exception as exc:
            elapsed = time.monotonic() - t0
            if attempt < MAX_RETRIES:
                print(f"  ⚠ #{request_id} error: {exc} — retrying...")
                await asyncio.sleep(1)
                continue
            return {
                "id": request_id,
                "status": -1,
                "elapsed": elapsed,
                "attempt": attempt,
                "text": str(exc)[:200],
            }

    return {
        "id": request_id,
        "status": 429,
        "elapsed": 0,
        "attempt": MAX_RETRIES,
        "text": "Exhausted retries on 429",
    }


# ── Throughput test: sequential ───────────────────────────────────────────────

async def run_sequential(payload: dict, headers: dict, n: int):
    """Fire N requests one after another (simulates current Archivist loop)."""
    print(f"\n{'='*60}")
    print(f"TEST A: Sequential — {n} requests, one at a time")
    print(f"{'='*60}")

    results = []
    async with httpx.AsyncClient() as client:
        t_start = time.monotonic()
        for i in range(n):
            r = await send_request(client, payload, headers, i + 1)
            results.append(r)
            status_icon = "✅" if r["status"] == 200 else "❌"
            print(f"  {status_icon} #{r['id']:>3d}  {r['elapsed']:.2f}s  HTTP {r['status']}")
        t_total = time.monotonic() - t_start

    return results, t_total


# ── Throughput test: concurrent (batched) ─────────────────────────────────────

async def run_concurrent(payload: dict, headers: dict, n: int, concurrency: int):
    """Fire N requests in batches of `concurrency`."""
    print(f"\n{'='*60}")
    print(f"TEST B: Concurrent — {n} requests, {concurrency} at a time")
    print(f"{'='*60}")

    results = []
    sem = asyncio.Semaphore(concurrency)

    async def bounded(client, i):
        async with sem:
            return await send_request(client, payload, headers, i)

    async with httpx.AsyncClient() as client:
        t_start = time.monotonic()
        tasks = [bounded(client, i + 1) for i in range(n)]
        batch_results = await asyncio.gather(*tasks)
        t_total = time.monotonic() - t_start

    results = list(batch_results)
    for r in sorted(results, key=lambda x: x["id"]):
        status_icon = "✅" if r["status"] == 200 else "❌"
        print(f"  {status_icon} #{r['id']:>3d}  {r['elapsed']:.2f}s  HTTP {r['status']}")

    return results, t_total


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(label: str, results: list[dict], total_time: float):
    print(f"\n{'─'*60}")
    print(f"📊 {label} Results")
    print(f"{'─'*60}")

    ok = [r for r in results if r["status"] == 200]
    errs = [r for r in results if r["status"] != 200]
    rate_limited = [r for r in results if r["status"] == 429]

    print(f"  Total requests : {len(results)}")
    print(f"  Successes      : {len(ok)}")
    print(f"  Failures       : {len(errs)}")
    print(f"  Rate-limited   : {len(rate_limited)}")
    print(f"  Total time     : {total_time:.1f}s")

    if ok:
        latencies = [r["elapsed"] for r in ok]
        retries = [r["attempt"] for r in ok if r["attempt"] > 1]
        rps = len(ok) / total_time

        print(f"  Throughput     : {rps:.2f} req/s  ({rps*60:.0f} req/min)")
        print(f"  Latency p50   : {statistics.median(latencies):.2f}s")
        print(f"  Latency p90   : {sorted(latencies)[int(len(latencies)*0.9)]:.2f}s")
        print(f"  Latency p99   : {sorted(latencies)[min(int(len(latencies)*0.99), len(latencies)-1)]:.2f}s")
        print(f"  Latency min   : {min(latencies):.2f}s")
        print(f"  Latency max   : {max(latencies):.2f}s")
        print(f"  Needed retries: {len(retries)}/{len(ok)}")
    else:
        print("  ⚠ No successful requests!")

    if errs:
        print(f"\n  Error details:")
        for r in errs[:5]:
            print(f"    #{r['id']} HTTP {r['status']}: {r['text'][:120]}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not NVIDIA_API_KEY:
        print("❌ NVIDIA_API_KEY not set")
        sys.exit(1)

    if not SAMPLE_VIDEOS:
        print("❌ No videos in media_drop/")
        sys.exit(1)

    print(f"🧪 Rate Limit Stress Test: {MODEL}")
    print(f"🌐 {API_URL}")
    print(f"🔑 {NVIDIA_API_KEY[:12]}...{NVIDIA_API_KEY[-4:]}")
    print(f"📋 {TOTAL_REQUESTS} requests × 2 modes (sequential + concurrent)")

    # Prepare a single frame payload to reuse
    frame = extract_one_frame(str(SAMPLE_VIDEOS[0]))
    if not frame:
        print("❌ Failed to extract test frame")
        sys.exit(1)

    b64 = encode_image_b64(frame)
    payload = build_payload(b64)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
    }

    # Test A: Sequential (matches current Archivist pattern)
    seq_results, seq_time = await run_sequential(payload, headers, TOTAL_REQUESTS)
    print_report("Sequential", seq_results, seq_time)

    # Brief pause between tests
    print("\n  ⏳ Cooling down 5s before concurrent test...")
    await asyncio.sleep(5)

    # Test B: Concurrent (potential optimization)
    conc_results, conc_time = await run_concurrent(
        payload, headers, TOTAL_REQUESTS, CONCURRENCY,
    )
    print_report(f"Concurrent (×{CONCURRENCY})", conc_results, conc_time)

    # Summary
    seq_ok = len([r for r in seq_results if r["status"] == 200])
    conc_ok = len([r for r in conc_results if r["status"] == 200])
    print(f"\n{'='*60}")
    print(f"📋 SUMMARY")
    print(f"{'='*60}")
    print(f"  Sequential : {seq_ok}/{TOTAL_REQUESTS} ok in {seq_time:.1f}s")
    print(f"  Concurrent : {conc_ok}/{TOTAL_REQUESTS} ok in {conc_time:.1f}s")
    print(f"  Speed-up   : {seq_time/conc_time:.1f}× with concurrency={CONCURRENCY}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
