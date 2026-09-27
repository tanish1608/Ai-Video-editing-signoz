"""
Soundstripe music integration — fetch royalty-free background music by vibe.

Uses Gemini LLM to analyze video content and pick appropriate mood/energy/genre
filters, then queries the Soundstripe REST API to find matching songs.
Downloads the WAV audio file for high-quality mixing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
from google import genai
from google.genai import types

from kinetograph.config import settings

logger = logging.getLogger(__name__)

_SOUNDSTRIPE_BASE = "https://api.soundstripe.com/v1"

# ─── Vibe Picker (Gemini LLM) ─────────────────────────────────────────────────

_VIBE_SYSTEM_PROMPT = """\
You are a Grammy-nominated music supervisor who specialises in scoring short-form \
vertical video content (TikTok, Reels, Shorts). You have an instinct for matching \
sonic textures to visual storytelling.

TASK: Given a description of the video's content and mood, select the single best \
combination of Soundstripe API filters to find the perfect background track.

────────────────────────────────────────────────
AVAILABLE FILTERS
────────────────────────────────────────────────
• energy:        "very_low" | "low" | "medium" | "high"
• mood:          e.g. "Happy", "Inspirational", "Chill", "Dramatic", "Suspenseful", \
"Upbeat", "Romantic", "Melancholic", "Energetic", "Peaceful", "Dark", "Whimsical", "Epic"
• genre:         e.g. "Pop", "Hip Hop", "Electronic", "Acoustic", "Cinematic", \
"Lo-Fi", "R&B", "Rock", "Jazz", "Classical", "Ambient", "Indie", "Folk"
• characteristic: e.g. "Minimal", "Driving", "Atmospheric", "Groovy", "Percussive", \
"Dreamy", "Warm", "Bright", "Smooth"
• instrumental:  true | false
• bpm_min / bpm_max: optional BPM range (60-180)
• search_query:  optional free-text search term

────────────────────────────────────────────────
DECISION RULES
────────────────────────────────────────────────
1. ALWAYS set instrumental = true. Background music must never have vocals competing \
   with the narrator's voice.
2. Pick exactly ONE mood, ONE genre, ONE energy level, and optionally ONE characteristic.
3. This is BACKGROUND music — it supports the story, it is not the star. Err on the \
   side of subtle and unobtrusive.
4. Match the emotional arc:
   - Talking-head / narration / interview → very_low or low energy, Ambient or Lo-Fi
   - Upbeat montage / hackathon recap → medium energy, Electronic or Pop
   - Dramatic / inspiring story → low-to-medium energy, Cinematic
   - Food / lifestyle content → low energy, Acoustic or Jazz
5. If the video mentions a specific vibe ("hype", "chill", "epic"), honour that intent.
6. Use search_query ONLY when the video has a very specific theme that the other \
   filters cannot capture (e.g. "retro 8-bit" or "tropical").

────────────────────────────────────────────────
OUTPUT
────────────────────────────────────────────────
Output ONLY valid JSON. No markdown, no explanation, no code fences.

{
  "energy": "low",
  "mood": "Chill",
  "genre": "Lo-Fi",
  "characteristic": "Warm",
  "instrumental": true,
  "bpm_min": null,
  "bpm_max": null,
  "search_query": null,
  "reasoning": "Brief 1-sentence explanation of why this vibe fits the video"
}
"""


def _pick_vibe(video_description: str) -> dict:
    """
    Ask Gemini to choose Soundstripe filters based on the video's content.

    Returns a dict with filter parameters.
    Falls back to generic chill/lo-fi on failure.
    """
    fallback = {
        "energy": "low",
        "mood": "Chill",
        "genre": "Lo-Fi",
        "instrumental": True,
    }

    if not settings.gemini_api_key:
        logger.warning("🎵 Music: No Gemini key — using default vibe (Chill / Lo-Fi)")
        return fallback

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=f"{_VIBE_SYSTEM_PROMPT}\n\nVideo description:\n{video_description}"
                        ),
                    ],
                ),
            ],
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=512,
                response_mime_type="application/json",
            ),
        )

        raw = resp.text.strip()
        data = json.loads(raw)
        logger.info(
            f"🎵 Music: LLM picked vibe → {data.get('mood', '?')} / "
            f"{data.get('genre', '?')} / energy={data.get('energy', '?')} "
            f"({data.get('reasoning', '')})"
        )
        return data

    except Exception as exc:
        logger.warning(f"🎵 Music: Vibe picker failed ({exc}), using defaults")
        return fallback


# ─── Soundstripe API Client ───────────────────────────────────────────────────


def _search_songs(filters: dict, max_results: int = 5) -> list[dict]:
    """
    Query the Soundstripe API for songs matching the given filters.

    Returns a list of song objects with their included audio_files.
    """
    headers = {
        "Authorization": f"Token {settings.soundstripe_api_key}",
        "Accept": "application/json",
    }

    params: dict[str, str] = {
        "page[size]": str(max_results),
        "include": "audio_files",
    }

    # Map our filter dict to Soundstripe query params
    if filters.get("energy"):
        params["filter[energy]"] = filters["energy"]
    if filters.get("mood"):
        params["filter[tags][mood]"] = filters["mood"]
    if filters.get("genre"):
        params["filter[tags][genre]"] = filters["genre"]
    if filters.get("characteristic"):
        params["filter[tags][characteristic]"] = filters["characteristic"]
    if filters.get("instrumental") is True:
        params["filter[instrumental]"] = "true"
    if filters.get("bpm_min"):
        params["filter[bpm][min]"] = str(filters["bpm_min"])
    if filters.get("bpm_max"):
        params["filter[bpm][max]"] = str(filters["bpm_max"])
    if filters.get("search_query"):
        params["filter[q]"] = filters["search_query"]

    try:
        resp = httpx.get(
            f"{_SOUNDSTRIPE_BASE}/songs",
            headers=headers,
            params=params,
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()

        songs = payload.get("data", [])
        included = payload.get("included", [])

        # Build a lookup of audio_files by ID
        audio_lookup: dict[str, dict] = {}
        for item in included:
            if item.get("type") == "audio_files":
                audio_lookup[item["id"]] = item.get("attributes", {})

        # Attach audio_file data to each song
        results = []
        for song in songs:
            audio_refs = song.get("relationships", {}).get("audio_files", {}).get("data", [])
            audio_files = []
            for ref in audio_refs:
                af = audio_lookup.get(ref.get("id"))
                if af:
                    audio_files.append(af)

            results.append(
                {
                    "id": song["id"],
                    "title": song.get("attributes", {}).get("title", "Unknown"),
                    "bpm": song.get("attributes", {}).get("bpm"),
                    "tags": song.get("attributes", {}).get("tags", {}),
                    "audio_files": audio_files,
                }
            )

        return results

    except httpx.HTTPStatusError as exc:
        logger.error(
            f"🎵 Music: Soundstripe API error {exc.response.status_code}: {exc.response.text[:300]}"
        )
        return []
    except Exception as exc:
        logger.error(f"🎵 Music: Soundstripe search failed: {exc}")
        return []


def _download_audio(url: str, output_path: Path) -> Path:
    """Download an audio file from Soundstripe."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=8192):
                f.write(chunk)

    logger.info(f"🎵 Music: Downloaded → {output_path.name}")
    return output_path


# ─── Public API ────────────────────────────────────────────────────────────────


def is_configured() -> bool:
    """Check if Soundstripe API is configured."""
    return bool(settings.soundstripe_api_key)


def fetch_background_music(
    video_description: str,
    output_dir: Path,
    target_duration_sec: float | None = None,
) -> Path | None:
    """
    Fetch background music from Soundstripe based on the video's vibe.

    1. Uses Gemini LLM to analyze content and pick mood/energy/genre filters
    2. Queries Soundstripe API for matching songs
    3. Downloads the best matching WAV file
    4. Returns the path to the downloaded music file, or None on failure

    Args:
        video_description: Text description of the video content and mood
        output_dir: Directory to save the downloaded music file
        target_duration_sec: Optional target duration to prefer songs close to this length

    Returns:
        Path to downloaded music file, or None if unavailable
    """
    if not is_configured():
        logger.info("🎵 Music: Soundstripe not configured — skipping background music")
        return None

    # Step 1: Pick vibe via LLM
    filters = _pick_vibe(video_description)

    # Step 2: Search Soundstripe
    songs = _search_songs(filters, max_results=10)

    if not songs:
        # Retry with broader filters (just instrumental + energy)
        logger.info("🎵 Music: No songs found, retrying with broader filters...")
        broad_filters = {
            "energy": filters.get("energy", "low"),
            "instrumental": True,
        }
        songs = _search_songs(broad_filters, max_results=10)

    if not songs:
        logger.warning("🎵 Music: No songs found on Soundstripe")
        return None

    # Step 3: Pick the best song
    # Prefer instrumental tracks with duration close to target
    best_song = None
    best_audio = None
    best_score = -1

    for song in songs:
        for af in song.get("audio_files", []):
            # Prefer instrumental audio files
            is_instrumental = af.get("instrumental", False)
            duration = af.get("duration", 0)
            has_wav = bool(af.get("versions", {}).get("wav"))

            score = 0
            if is_instrumental:
                score += 10
            if has_wav:
                score += 5

            # Prefer songs that are at least as long as the video
            if target_duration_sec and duration >= target_duration_sec:
                score += 3
            elif target_duration_sec and duration >= target_duration_sec * 0.7:
                score += 1

            if score > best_score:
                best_score = score
                best_song = song
                best_audio = af

    if not best_song or not best_audio:
        logger.warning("🎵 Music: No suitable audio files found")
        return None

    # Step 4: Download
    versions = best_audio.get("versions", {})
    # Prefer WAV for quality, fallback to MP3
    url = versions.get("wav") or versions.get("mp3")

    if not url:
        logger.warning("🎵 Music: No download URL available")
        return None

    ext = ".wav" if versions.get("wav") == url else ".mp3"
    filename = f"bg_music_{best_song['id']}{ext}"
    output_path = output_dir / filename

    # Skip download if already cached
    if output_path.exists():
        logger.info(f"🎵 Music: Using cached → {output_path.name}")
        return output_path

    try:
        logger.info(
            f"🎵 Music: Downloading '{best_song['title']}' "
            f"(BPM={best_song.get('bpm')}, "
            f"mood={best_song.get('tags', {}).get('mood', '?')})"
        )
        return _download_audio(url, output_path)
    except Exception as exc:
        logger.error(f"🎵 Music: Download failed: {exc}")
        return None


def build_video_description(
    approved_edit: dict | None,
    master_index: list[dict],
) -> str:
    """
    Build a text description of the video content for the vibe picker.

    Combines the paper edit title/clips and transcript excerpts.
    If the scripter provided a music_prompt, it gets top billing
    as a direct creative directive from the editor.
    """
    parts: list[str] = []

    if approved_edit:
        # Music prompt is the scripter's explicit creative direction — prioritise it
        music_prompt = approved_edit.get("music_prompt")
        if music_prompt:
            parts.append(f"EDITOR'S MUSIC DIRECTION (highest priority): {music_prompt}")

        title = approved_edit.get("title", "")
        if title:
            parts.append(f"Video title: {title}")

        clips = approved_edit.get("clips", [])
        descriptions = [c.get("description", "") for c in clips if c.get("description")]
        if descriptions:
            parts.append("Clip descriptions: " + " | ".join(descriptions[:8]))

    # Add transcript excerpt
    transcript_parts = []
    for entry in master_index[:5]:  # First 5 entries for context
        text = entry.get("transcript", "")
        if text:
            transcript_parts.append(text[:200])

    if transcript_parts:
        parts.append("Transcript excerpt: " + " ... ".join(transcript_parts))

    return "\n".join(parts) if parts else "General video content"
