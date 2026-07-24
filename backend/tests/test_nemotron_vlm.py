"""
Test script: NVIDIA Nemotron Nano 12B V2 VL  (via NVIDIA NIM Cloud API)
────────────────────────────────────────────────────────────────────────
Endpoint : https://integrate.api.nvidia.com/v1/chat/completions
Model    : nvidia/nemotron-nano-12b-v2-vl
Auth     : Bearer <NVIDIA_API_KEY>

Tests:
  1. Single image description
  2. Multi-image comparison
  3. Video frames (pre-extracted at 2 FPS) — model's video mode
  4. A-roll frame + transcript context (our pipeline use-case)

Usage:
    export NVIDIA_API_KEY=nvapi-...
    python tests/test_nemotron_vlm.py
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

MODEL = "nvidia/nemotron-nano-12b-v2-vl"
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# Grab sample media files for testing
MEDIA_DIR = Path(__file__).parent.parent / "media_drop"
SAMPLE_VIDEOS = list(MEDIA_DIR.rglob("*.mp4"))


def encode_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def extract_frames(video_path: str, num_frames: int = 8, fps: float = 2.0) -> list[str]:
    """
    Extract frames from a video at the given FPS.
    Nemotron supports 2 FPS, min 8 frames, max 128 frames.
    Returns list of frame file paths.
    """
    tmpdir = tempfile.mkdtemp(prefix="nemotron_frames_")
    out_pattern = os.path.join(tmpdir, "frame_%04d.jpg")

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"fps={fps}",
        "-frames:v", str(num_frames),
        "-q:v", "2",
        out_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"  ⚠ FFmpeg error: {result.stderr[:200]}")
        return []

    frames = sorted(Path(tmpdir).glob("*.jpg"))
    return [str(f) for f in frames]


def call_nvidia_api(messages: list[dict], max_tokens: int = 512) -> str:
    """Call NVIDIA NIM Cloud API (OpenAI-compatible)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }

    print(f"  → Calling NVIDIA NIM API ({MODEL})...")
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(API_URL, json=payload, headers=headers)

    if resp.status_code != 200:
        return f"[ERROR {resp.status_code}]: {resp.text[:800]}"

    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# ── Test 1: Single Image ─────────────────────────────────────────────────────

def test_single_image():
    print("\n" + "=" * 60)
    print("TEST 1: Single Image Description")
    print("=" * 60)

    if not SAMPLE_VIDEOS:
        print("  ⚠ No sample videos found in media_drop/")
        return

    video = str(SAMPLE_VIDEOS[0])
    print(f"  Source: {Path(video).name}")

    frames = extract_frames(video, num_frames=1, fps=0.5)
    if not frames:
        print("  ⚠ Failed to extract frames")
        return

    b64 = encode_image_b64(frames[0])
    print(f"  Frame: {frames[0]} ({len(b64) // 1024}KB base64)")

    messages = [
        {"role": "system", "content": "/no_think"},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Describe this video frame in 2-3 concise sentences. "
                           "Focus on: who/what is visible, the setting, actions, mood.",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        }
    ]

    result = call_nvidia_api(messages)
    print(f"\n  📝 Response:\n  {result}\n")


# ── Test 2: Multi-Image (compare two B-roll clips) ───────────────────────────

def test_multi_image():
    print("\n" + "=" * 60)
    print("TEST 2: Multi-Image Comparison")
    print("=" * 60)

    if len(SAMPLE_VIDEOS) < 2:
        print("  ⚠ Need at least 2 videos for multi-image test")
        return

    frames1 = extract_frames(str(SAMPLE_VIDEOS[0]), num_frames=1, fps=0.5)
    frames2 = extract_frames(str(SAMPLE_VIDEOS[1]), num_frames=1, fps=0.5)
    if not frames1 or not frames2:
        print("  ⚠ Failed to extract frames")
        return

    b64_1 = encode_image_b64(frames1[0])
    b64_2 = encode_image_b64(frames2[0])
    print(f"  Image 1: {Path(SAMPLE_VIDEOS[0]).name}")
    print(f"  Image 2: {Path(SAMPLE_VIDEOS[1]).name}")

    messages = [
        {"role": "system", "content": "/no_think"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Compare these two images. What's different? What mood does each convey?"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_1}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_2}"}},
            ],
        }
    ]

    result = call_nvidia_api(messages, max_tokens=600)
    print(f"\n  📝 Response:\n  {result}\n")


# ── Test 3: Video Frames ─────────────────────────────────────────────────────

def test_video_frames():
    print("\n" + "=" * 60)
    print("TEST 3: Video Understanding (multi-frame sequence)")
    print("=" * 60)

    if not SAMPLE_VIDEOS:
        print("  ⚠ No sample videos found")
        return

    # Pick a video and extract 8 frames at 2 FPS (model spec)
    video = str(SAMPLE_VIDEOS[0])
    print(f"  Source: {Path(video).name}")
    print(f"  Extracting 8 frames at 2 FPS...")

    frames = extract_frames(video, num_frames=8, fps=2.0)
    if len(frames) < 4:
        print(f"  ⚠ Only got {len(frames)} frames, need at least 4")
        return

    print(f"  Extracted {len(frames)} frames")

    # Send all frames as a sequence — Nemotron treats multiple images as video
    # For video inputs, reasoning is NOT supported, so use /no_think
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "These are sequential frames from a video clip extracted at 2 FPS. "
                "Describe what's happening in the video — the scene, any motion, "
                "the setting, and the overall mood. Be specific about what changes "
                "across the frames."
            ),
        }
    ]

    for i, frame_path in enumerate(frames):
        b64 = encode_image_b64(frame_path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    messages = [
        {"role": "system", "content": "/no_think"},
        {"role": "user", "content": content},
    ]

    result = call_nvidia_api(messages, max_tokens=800)
    print(f"\n  📝 Response:\n  {result}\n")


# ── Test 4: A-roll frame with transcript context ─────────────────────────────

def test_aroll_with_context():
    print("\n" + "=" * 60)
    print("TEST 4: A-roll Frame + Transcript Context")
    print("=" * 60)

    aroll_dir = MEDIA_DIR / "a-roll"
    aroll_videos = list(aroll_dir.glob("*.mp4")) if aroll_dir.exists() else []
    if not aroll_videos:
        print("  ⚠ No A-roll videos found in media_drop/a-roll/")
        return

    video = str(aroll_videos[0])
    print(f"  Source: {Path(video).name}")

    frames = extract_frames(video, num_frames=1, fps=0.2)  # ~1 frame from 5s
    if not frames:
        print("  ⚠ Failed to extract frame")
        return

    b64 = encode_image_b64(frames[0])

    messages = [
        {"role": "system", "content": "/no_think"},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "This is a frame from a solo travel vlog. The speaker says: "
                        "'But last year, I went to 11 countries.' "
                        "Describe what you see in the frame and how it relates to "
                        "the narrative. Is this A-roll (talking head) or B-roll (scenic cutaway)?"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        }
    ]

    result = call_nvidia_api(messages, max_tokens=400)
    print(f"\n  📝 Response:\n  {result}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not NVIDIA_API_KEY:
        print("❌ NVIDIA_API_KEY not set. Export it or add to .env")
        sys.exit(1)

    print(f"🧪 Testing: {MODEL}")
    print(f"🌐 Endpoint: {API_URL}")
    print(f"🔑 API Key: {NVIDIA_API_KEY[:12]}...{NVIDIA_API_KEY[-4:]}")
    print(f"📁 Media dir: {MEDIA_DIR}")
    print(f"🎬 Found {len(SAMPLE_VIDEOS)} video(s)")

    # Run all tests
    test_single_image()
    test_multi_image()
    test_video_frames()
    test_aroll_with_context()

    print("\n" + "=" * 60)
    print("✅ All tests complete!")
    print("=" * 60)
