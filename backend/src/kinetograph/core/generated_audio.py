"""ElevenLabs audio generation with project-local, content-addressed caching."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import httpx

from kinetograph.config import settings
from kinetograph.core.media import probe_media_async


async def generate_audio(
    prompt: str,
    duration_ms: int,
    *,
    kind: str = "music",
    client: httpx.AsyncClient | None = None,
) -> Path:
    if not settings.elevenlabs_api_key:
        raise ValueError("Configure an ElevenLabs API key in Settings to generate audio.")
    if kind == "music":
        endpoint = "music"
        payload = {
            "prompt": prompt[:4100],
            "music_length_ms": max(3000, min(600000, duration_ms)),
            "force_instrumental": True,
            "model_id": "music_v1",
        }
    elif kind == "effect":
        endpoint = "sound-generation"
        payload = {
            "text": prompt[:2000],
            "duration_seconds": max(0.5, min(3, duration_ms / 1000)),
            "prompt_influence": 0.3,
            "model_id": "eleven_text_to_sound_v2",
        }
    else:
        raise ValueError(f"Unknown audio kind: {kind}")
    key = hashlib.sha256(json.dumps([endpoint, payload], sort_keys=True).encode()).hexdigest()
    cache = settings._project_root / ".cache" / "audio" / "generated"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{kind}-{key}.mp3"
    if destination.is_file() and destination.stat().st_size:
        return destination
    temporary = cache / f"{key}-{uuid.uuid4().hex}.mp3"
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(240, connect=15))
    try:
        # No automatic POST retries: a timeout may already have incurred generation costs.
        async with client.stream(
            "POST",
            f"https://api.elevenlabs.io/v1/{endpoint}",
            json=payload,
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": settings.elevenlabs_api_key},
        ) as response:
            response.raise_for_status()
            with temporary.open("wb") as file:
                async for chunk in response.aiter_bytes():
                    file.write(chunk)
        meta = await probe_media_async(str(temporary))
        if not meta.get("has_audio") or meta.get("duration_ms", 0) <= 0:
            raise ValueError("ElevenLabs returned invalid or empty audio")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
        if owned:
            await client.aclose()
