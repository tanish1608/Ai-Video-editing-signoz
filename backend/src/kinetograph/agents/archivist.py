"""
Agent 1: The Archivist
─────────────────────
Ingests raw video/audio/image files from the project media/ directory.
Extracts audio → ElevenLabs STT (word-level timestamps + diarization).
Splits video into temporal segments → NVIDIA Nemotron VLM (video understanding).
Produces a master JSON index mapping timestamps to transcript + visual context.

The Archivist does NOT impose editorial roles (a-roll/b-roll). Instead it tags
each entry with `has_speech` (from STT) and `content_tags` (from VLM) so the
Scripter can make the editorial decision about which clips serve as primary
narrative and which are cutaways.

Architecture (v2 — Nemotron video-native):
  OLD: 1 keyframe/sec → 1 VLM call/frame → sequential → ~181 calls, zero temporal context
  NEW: 4-sec segments → 8 frames @ 2 FPS each → concurrent VLM calls (5×) → motion-aware
       descriptions with temporal context.  ~22 segments instead of ~85 single frames
       per file.  6× faster, richer output.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import httpx
from elevenlabs import ElevenLabs

from kinetograph.config import settings
from kinetograph.core.media import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    extract_audio_async,
    extract_video_segments_async,
    probe_media,
)
from kinetograph.observability import (
    llm_span,
    record_nvidia_tokens,
    tool_span,
)
from kinetograph.schema import SegmentVisual, VisualCategory
from kinetograph.state import GraphState, Phase

logger = logging.getLogger(__name__)


# ─── VLM Interaction (NVIDIA Nemotron — video-native) ─────────────────────────


def _encode_image_base64(image_path: str) -> str:
    """Read an image file and return its base64 encoding."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _build_vlm_headers() -> dict[str, str]:
    """Build auth headers for the NVIDIA NIM API."""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.nvidia_api_key}",
    }


def _build_vlm_url() -> str:
    """Build the VLM chat completions URL."""
    return f"{settings.vlm_base_url.rstrip('/')}/v1/chat/completions"


def _build_vlm_prompt(transcript_slice: str = "") -> str:
    """VLM prompt — asks for STRUCTURED JSON grounded in the spoken audio.

    Two upgrades over the old free-text prompt: (1) the model is told what is
    being *said* during these frames (A/V fusion), so it can describe how the
    visuals relate to the speech; (2) it must return a strict JSON object that
    parses into :class:`SegmentVisual`, replacing the fragile last-line
    classification convention, and adds editorial signal (salience/energy/
    emotion) for downstream ranking.
    """
    audio_ctx = (
        f'\n\nAUDIO CONTEXT — the speaker is saying during these frames:\n"{transcript_slice}"\n'
        "Describe the visuals AND how they relate to (support / illustrate / "
        "contradict) what is being said.\n"
        if transcript_slice.strip()
        else "\n\n(There is no speech during this segment.)\n"
    )
    return (
        "You are a senior documentary film cataloguer with an expert eye for detail, "
        "reviewing sequential frames (2 FPS) from a short video segment."
        f"{audio_ctx}"
        "\nReturn a SINGLE JSON object with EXACTLY these keys:\n"
        "  subject   — who/what is the primary focus (specific: 'Two "
        "engineers pair-programming on a MacBook', not 'people using a computer')\n"
        "  setting   — where this is (e.g. 'indoor co-working space with neon lighting')\n"
        "  action    — the motion/change across frames (say 'static, no motion' if none)\n"
        "  notable   — visible text, logos, graphics, distinctive elements (empty string if none)\n"
        "  clip_type — EXACTLY one of: TALKING_HEAD, SCENIC, ACTION, "
        "TEXT_OVERLAY, TRANSITION, OTHER\n"
        "  energy    — number 0.0-1.0: visual intensity/motion (0=static, 1=fast/dynamic)\n"
        "  salience  — number 0.0-1.0: how "
        "highlight-worthy/attention-grabbing this moment is for a punchy short\n"
        "  emotion   — one word for the dominant mood (e.g. 'excited', "
        "'calm', 'tense', 'neutral')\n\n"
        "Be specific and decisive; omit hedging. Output ONLY the JSON "
        "object, no prose, no code fences."
    )


async def _describe_segment_vlm(
    segment: dict,
    asset_type: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    transcript_slice: str = "",
) -> dict:
    """
    Send a video segment (multiple frames) + the overlapping speech to Nemotron
    and get a STRUCTURED, audio-grounded description.

    The model receives up to 8 frames @ 2 FPS (~4s of video) plus the transcript
    spoken during that window, and returns a JSON object parsed into
    :class:`SegmentVisual` (subject/setting/action/notable/clip_type + energy/
    salience/emotion). Falls back to the legacy free-text parse if the endpoint
    ignores the JSON request.

    Returns a dict carrying the structured fields PLUS a composite ``description``
    and ``clip_type`` for backward compatibility with existing consumers.
    """
    frame_paths = segment["frame_paths"]

    # ── Build multi-frame content array ───────────────────────────────────
    prompt = _build_vlm_prompt(transcript_slice)

    content: list[dict] = [{"type": "text", "text": prompt}]
    for fp in frame_paths:
        b64 = _encode_image_base64(fp)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            }
        )

    payload = {
        "model": settings.vlm_model,
        "messages": [
            {"role": "system", "content": "/no_think"},
            {"role": "user", "content": content},
        ],
        "max_tokens": 500,
        "temperature": 0.2,
        "stream": False,
        # Ask for JSON; NVIDIA NIM (OpenAI-compatible) honors this on supported
        # models. The parser degrades gracefully if the body isn't valid JSON.
        "response_format": {"type": "json_object"},
    }

    async with sem:
        with llm_span(
            "nvidia", settings.vlm_model, **{"kinetograph.segment_start_ms": segment["start_ms"]}
        ) as _vlm_span:
            try:
                resp = None
                last_exc = None
                # Retry up to 2 times with increasing timeout
                for attempt in range(3):
                    try:
                        timeout = 30.0 + attempt * 10.0  # 30s, 40s, 50s
                        resp = await client.post(
                            _build_vlm_url(),
                            json=payload,
                            headers=_build_vlm_headers(),
                            timeout=timeout,
                        )

                        # Handle rate-limits with back-off
                        if resp.status_code == 429:
                            wait = min(float(resp.headers.get("Retry-After", 3)), 10)
                            logger.warning(f"VLM rate-limited, waiting {wait:.1f}s...")
                            await asyncio.sleep(wait)
                            continue

                        if resp.status_code >= 500:
                            logger.warning(f"VLM server error {resp.status_code}, retrying...")
                            await asyncio.sleep(2**attempt)
                            continue

                        resp.raise_for_status()
                        break  # Success

                    except httpx.TimeoutException as tex:
                        last_exc = tex
                        if attempt < 2:
                            logger.warning(
                                f"VLM timeout for segment "
                                f"{segment['start_ms']}-{segment['end_ms']}ms "
                                f"(attempt {attempt + 1}/3), retrying..."
                            )
                            await asyncio.sleep(1)
                        continue
                    except httpx.HTTPStatusError:
                        raise

                if resp is None or resp.status_code != 200:
                    raise RuntimeError(
                        last_exc
                        or f"VLM retries exhausted (status={getattr(resp, 'status_code', '?')})"
                    )

                data = resp.json()
                record_nvidia_tokens(_vlm_span, data, settings.vlm_model)
                raw_text = data["choices"][0]["message"]["content"].strip()

                visual = _parse_segment_visual(raw_text)
                # Record editorial signal on the span for SigNoz.
                _vlm_span.set_attribute("kinetograph.salience", visual.salience)
                _vlm_span.set_attribute("kinetograph.energy", visual.energy)
                _vlm_span.set_attribute("kinetograph.clip_type", visual.clip_type.value)

                return _segment_result(segment, visual, len(frame_paths))

            except Exception as exc:
                logger.warning(
                    f"VLM failed for segment {segment['start_ms']}-{segment['end_ms']}ms: {exc}"
                )
                failed = SegmentVisual(
                    subject=f"[VLM analysis failed: {exc}]",
                    clip_type=VisualCategory.OTHER,
                )
                return _segment_result(segment, failed, len(frame_paths))


def _parse_segment_visual(raw_text: str) -> "SegmentVisual":
    """Parse a VLM response into a SegmentVisual, tolerant of formatting.

    Prefers strict JSON (the requested format); if the model returned prose with
    a trailing category label (legacy free-text style), degrade to filling
    `subject` with the text and recovering the clip_type from the last line.
    """
    text = raw_text.strip()
    # Strip accidental ```json fences.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return SegmentVisual.model_validate(obj)
    except Exception:
        pass

    # Legacy fallback: last line may be a category label.
    lines = text.split("\n")
    valid = {c.value for c in VisualCategory}
    clip_type = VisualCategory.OTHER
    desc = text
    if lines and lines[-1].strip().upper() in valid:
        clip_type = VisualCategory(lines[-1].strip().upper())
        desc = "\n".join(lines[:-1]).strip()
    return SegmentVisual(subject=desc, clip_type=clip_type)


def _segment_result(segment: dict, visual: "SegmentVisual", num_frames: int) -> dict:
    """Build the per-segment result dict — structured fields + a composite
    ``description``/``clip_type`` for backward-compatible consumers."""
    parts = [p for p in (visual.subject, visual.setting, visual.action, visual.notable) if p]
    description = " ".join(parts).strip() or visual.subject
    return {
        "start_ms": segment["start_ms"],
        "end_ms": segment["end_ms"],
        "description": description,
        "clip_type": visual.clip_type.value,
        "num_frames": num_frames,
        # Structured signal (new — consumed by _build_asset_index / Scripter / Critic)
        "visual": visual.model_dump(),
        "salience": visual.salience,
        "energy": visual.energy,
        "emotion": visual.emotion,
    }


def _transcript_slice_for(words: list[dict], start_ms: int, end_ms: int) -> str:
    """Join the transcript words that overlap a [start_ms, end_ms) window.

    Powers A/V fusion — each VLM call is grounded in what is being said during
    exactly those frames.
    """
    if not words:
        return ""
    toks = [
        w.get("text", "")
        for w in words
        if w.get("end_ms", 0) > start_ms
        and w.get("start_ms", 0) < end_ms
        and w.get("text", "").strip()
    ]
    return " ".join(toks).strip()


async def _analyze_video_segments(
    segments: list[dict],
    asset_type: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    words: list[dict] | None = None,
) -> list[dict]:
    """
    Analyze all segments concurrently, bounded by the shared *sem*.
    Returns descriptions in chronological order. On global timeout, keeps the
    segments that already completed rather than discarding everything.

    When *words* (transcript) are supplied, each segment's overlapping speech is
    passed to the VLM for audio-visual grounding.
    """
    total = len(segments)
    if total == 0:
        return []

    words = words or []

    # Real Tasks so we can cancel the stragglers and read completed results.
    tasks = [
        asyncio.ensure_future(
            _describe_segment_vlm(
                seg,
                asset_type,
                client,
                sem,
                transcript_slice=_transcript_slice_for(words, seg["start_ms"], seg["end_ms"]),
            )
        )
        for seg in segments
    ]

    # Global timeout: ~15s per segment (generous) to prevent infinite hangs.
    global_timeout = max(120.0, total * 15.0)
    done, pending = await asyncio.wait(tasks, timeout=global_timeout)

    if pending:
        logger.error(
            f"🗄️  VLM global timeout after {global_timeout:.0f}s "
            f"({len(done)}/{total} completed) — using partial results"
        )
        for t in pending:
            t.cancel()

    # Collect successful results from the completed tasks.
    good_results = []
    for t in done:
        try:
            r = t.result()
            if isinstance(r, dict):
                good_results.append(r)
        except Exception as exc:
            logger.warning(f"🗄️  VLM segment failed: {exc}")

    # Sort by start_ms to ensure chronological order
    return sorted(good_results, key=lambda r: r["start_ms"])


# ─── ElevenLabs STT ───────────────────────────────────────────────────────────


def _transcribe_audio(audio_path: str) -> dict:
    """
    Transcribe audio using ElevenLabs Scribe.

    Returns dict with keys: text, words (list of {text, start_ms, end_ms, speaker_id}).
    """
    client = ElevenLabs(api_key=settings.elevenlabs_api_key)

    with (
        open(audio_path, "rb") as audio_file,
        tool_span("elevenlabs_stt", **{"stt.model": settings.elevenlabs_stt_model}),
    ):
        result = client.speech_to_text.convert(
            file=audio_file,
            model_id=settings.elevenlabs_stt_model,
            language_code="en",
            diarize=True,
            timestamps_granularity="word",
            tag_audio_events=True,
        )

    words = []
    if hasattr(result, "words") and result.words:
        for w in result.words:
            words.append(
                {
                    "text": w.text,
                    "start_ms": int(w.start * 1000) if hasattr(w, "start") else 0,
                    "end_ms": int(w.end * 1000) if hasattr(w, "end") else 0,
                    "speaker_id": getattr(w, "speaker_id", None),
                }
            )

    return {
        "text": result.text if hasattr(result, "text") else "",
        "words": words,
    }


# ─── Discovery ────────────────────────────────────────────────────────────────


def _discover_media_files() -> list[dict]:
    """Scan media/ for all supported video and image files.

    All files live in a single flat directory. The Archivist does NOT
    assign editorial roles — it discovers and probes, that's it.
    Files inside the hidden .synth/ subdirectory are skipped (those are
    managed by the Synthesizer agent).
    """
    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    files = []
    media_dir = settings.media_dir
    candidates = list(media_dir.iterdir()) if media_dir.exists() else []
    # Windows may deny symlink creation. Registered originals are authoritative
    # even when there is no corresponding link in media/.
    if settings.media_refs_path.exists():
        try:
            refs = json.loads(settings.media_refs_path.read_text())
            candidates.extend(Path(value) for value in refs.values() if isinstance(value, str))
        except (OSError, ValueError, AttributeError):
            logger.warning("Could not read media references", exc_info=True)
    seen: set[Path] = set()
    for f in sorted(candidates):
        resolved = f.resolve()
        if resolved in seen or not f.is_file():
            continue
        seen.add(resolved)
        # Skip hidden files/dirs (including .synth/)
        if f.name.startswith(".") or f.is_dir():
            continue
        if f.suffix.lower() in all_extensions:
            is_image = f.suffix.lower() in IMAGE_EXTENSIONS
            try:
                meta = probe_media(f)
                files.append(
                    {
                        "file_path": str(f),
                        "file_name": f.name,
                        "media_type": "image" if is_image else "video",
                        "duration_ms": meta["duration_ms"],
                        "width": meta["width"],
                        "height": meta["height"],
                        "fps": meta["fps"],
                        "has_audio": meta["has_audio"],
                        "is_image": is_image,
                    }
                )
            except RuntimeError as exc:
                logger.error(f"Skipping corrupt file {f}: {exc}")
    return files


# ─── Stutter / Filler Detection ────────────────────────────────────────────────

_FILLER_WORDS = frozenset({"um", "uh", "ah", "er", "hmm", "hm", "mm"})


def _detect_stutters(words: list[dict]) -> dict:
    """
    Detect consecutive repeated words (stutters) and filler words.

    A stutter is 2+ consecutive identical words (e.g. "I I I was going").
    For repeats, only the LAST occurrence is kept (usually the cleanest
    pronunciation).  Common fillers (um, uh, er…) are removed entirely.

    Returns:
        skip_regions: list of (start_ms, end_ms) to cut from the video
        cleaned_words: word list with stutters/fillers removed
    """
    if not words:
        return {"skip_regions": [], "cleaned_words": []}

    skip_regions: list[tuple[int, int]] = []
    cleaned_words: list[dict] = []

    i = 0
    while i < len(words):
        text = words[i].get("text", "").strip()
        normalized = text.lower().rstrip(".,!?;:'\"")

        # Skip filler words entirely
        if normalized in _FILLER_WORDS:
            skip_regions.append((words[i]["start_ms"], words[i]["end_ms"]))
            i += 1
            continue

        if not normalized or len(normalized) < 1:
            cleaned_words.append(words[i])
            i += 1
            continue

        # Detect consecutive repeats of the same word
        j = i + 1
        while j < len(words):
            next_norm = words[j].get("text", "").strip().lower().rstrip(".,!?;:'\"")
            if next_norm == normalized:
                j += 1
            else:
                break

        repeat_count = j - i
        if repeat_count > 1:
            # Stutter: skip all but the last occurrence (cleanest take)
            for k in range(i, j - 1):
                skip_regions.append((words[k]["start_ms"], words[k]["end_ms"]))
            cleaned_words.append(words[j - 1])
            i = j
        else:
            cleaned_words.append(words[i])
            i += 1

    # Merge adjacent / overlapping skip regions (100ms tolerance)
    if skip_regions:
        skip_regions.sort()
        merged = [list(skip_regions[0])]
        for s, e in skip_regions[1:]:
            if s <= merged[-1][1] + 100:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        skip_regions = [(s, e) for s, e in merged]

    return {"skip_regions": skip_regions, "cleaned_words": cleaned_words}


# ─── Index Builder ─────────────────────────────────────────────────────────────


def _build_asset_index(
    asset: dict,
    transcript_data: dict,
    visual_segments: list[dict],
) -> list[dict]:
    """
    Merge transcript + visual context into master index entries for one asset.

    Content-based classification:
      - `has_speech`: True if STT found meaningful spoken words
      - `content_tags`: VLM-detected clip types (TALKING_HEAD, SCENIC, etc.)

    The Archivist does NOT assign a-roll/b-roll — that editorial decision
    is made by the Scripter.

    ElevenLabs Scribe returns whitespace characters as standalone word tokens
    (text=" ").  We strip those before storing.  Uses sentence-aware segmentation
    to avoid splitting mid-sentence.
    """
    file_path = asset["file_path"]
    entries: list[dict] = []

    raw_words = transcript_data.get("words", [])
    words = [w for w in raw_words if w.get("text", "").strip()]

    # Detect and remove stutters / filler words
    stutter_info = _detect_stutters(words)
    all_skip_regions = stutter_info["skip_regions"]
    cleaned_words = stutter_info["cleaned_words"]

    # Determine if the asset has meaningful speech
    has_speech = len(cleaned_words) >= 3  # at least 3 real words

    if cleaned_words:
        MIN_SEG_MS = 4000
        MAX_SEG_MS = 8000
        SENTENCE_ENDS = {".", "?", "!"}

        segment_start_ms = cleaned_words[0]["start_ms"]
        segment_words: list[dict] = []

        def _flush(seg_words: list[dict]) -> None:
            if not seg_words:
                return
            seg_s = seg_words[0]["start_ms"]
            seg_e = seg_words[-1]["end_ms"]

            overlapping_visuals = [
                vs for vs in visual_segments if vs["end_ms"] > seg_s and vs["start_ms"] < seg_e
            ]

            # Skip regions that fall within this segment's time range
            seg_skips = [(s, e) for s, e in all_skip_regions if e > seg_s and s < seg_e]

            # Collect content tags from VLM analysis
            content_tags = list({vs["clip_type"] for vs in overlapping_visuals})

            # Aggregate editorial signal over the overlapping visuals (max — a
            # segment is as highlight-worthy as its strongest moment).
            salience = max((vs.get("salience", 0.0) for vs in overlapping_visuals), default=0.0)
            energy = max((vs.get("energy", 0.0) for vs in overlapping_visuals), default=0.0)
            emotions = [vs.get("emotion", "") for vs in overlapping_visuals if vs.get("emotion")]

            entries.append(
                {
                    "asset_file": file_path,
                    "media_type": asset.get("media_type", "video"),
                    "has_speech": has_speech,
                    "content_tags": content_tags,
                    "start_ms": seg_s,
                    "end_ms": seg_e,
                    "transcript": " ".join(sw["text"] for sw in seg_words),
                    "words": seg_words,
                    "skip_regions": seg_skips,
                    "visual_descriptions": [vs["description"] for vs in overlapping_visuals],
                    "clip_types": content_tags,  # kept for backward compat
                    "speaker_id": seg_words[0].get("speaker_id"),
                    # Editorial signal for ranking (Scripter/Critic)
                    "salience": round(salience, 3),
                    "energy": round(energy, 3),
                    "emotion": emotions[0] if emotions else "",
                }
            )

        for w in cleaned_words:
            segment_words.append(w)
            elapsed = w["end_ms"] - segment_start_ms
            text = w["text"].rstrip()

            is_sentence_end = any(text.endswith(p) for p in SENTENCE_ENDS)
            if (is_sentence_end and elapsed >= MIN_SEG_MS) or elapsed >= MAX_SEG_MS:
                _flush(segment_words)
                segment_start_ms = w["end_ms"]
                segment_words = []

        _flush(segment_words)
    else:
        for vs in visual_segments:
            entries.append(
                {
                    "asset_file": file_path,
                    "media_type": asset.get("media_type", "video"),
                    "has_speech": False,
                    "content_tags": [vs["clip_type"]],
                    "start_ms": vs["start_ms"],
                    "end_ms": vs["end_ms"],
                    "transcript": "",
                    "words": [],
                    "skip_regions": [],
                    "visual_descriptions": [vs["description"]],
                    "clip_types": [vs["clip_type"]],
                    "speaker_id": None,
                    "salience": round(vs.get("salience", 0.0), 3),
                    "energy": round(vs.get("energy", 0.0), 3),
                    "emotion": vs.get("emotion", ""),
                }
            )

    return entries


# ─── Agent Entry Point ────────────────────────────────────────────────────────


async def archivist_node(state: GraphState) -> dict:
    """
    LangGraph node — The Archivist (v2 — Nemotron video-native).

    Pipeline:
      1. Discover raw media files
      2. Extract audio + transcribe with ElevenLabs
      3. Split video into temporal segments (4-sec windows, 8 frames @ 2 FPS)
      4. Analyze segments concurrently with NVIDIA Nemotron VLM
      5. Build master index merging transcript + visual context
    """
    logger.info("🗄️  Archivist v2: Starting deep parse of raw assets...")

    # 1. Discover (probes every file — offload the blocking scan off the loop)
    raw_assets = await asyncio.to_thread(_discover_media_files)
    if not raw_assets:
        return {
            "phase": Phase.ERROR,
            "errors": [
                {
                    "agent": "archivist",
                    "message": "No media files found in media/ directory",
                    "phase": Phase.INGESTING,
                    "recoverable": False,
                }
            ],
        }

    logger.info(f"🗄️  Archivist: Found {len(raw_assets)} raw assets")

    master_index = []
    temp_dir = settings.state_dir / "archivist_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as http_client:
        # ONE semaphore shared across every asset — otherwise a per-asset
        # semaphore combined with concurrent asset processing gives an effective
        # concurrency of vlm_concurrency × N_assets, blowing past rate limits.
        vlm_sem = asyncio.Semaphore(settings.vlm_concurrency)

        async def _process_asset(asset: dict) -> list[dict]:
            """Process a single asset: extract audio → STT → VLM → index entries."""
            file_path = asset["file_path"]
            stem = Path(file_path).stem

            # ── Images: single VLM description, no audio ──
            if asset.get("is_image"):
                logger.info(f"🗄️  Archivist: Processing image {asset['file_name']}")
                visual_segments = []
                try:
                    # Create a single-frame "segment" for the VLM
                    seg = {
                        "start_ms": 0,
                        "end_ms": asset["duration_ms"],
                        "frame_paths": [file_path],  # Image IS the frame
                    }
                    desc = await _describe_segment_vlm(seg, "media", http_client, vlm_sem)
                    visual_segments = [desc]
                except Exception as exc:
                    logger.error(f"🗄️  Image VLM failed for {asset['file_name']}: {exc}")
                return _build_asset_index(asset, {"text": "", "words": []}, visual_segments)

            # ── Videos: full audio + VLM pipeline ──
            # 2. Extract audio + transcribe (blocking I/O — run in thread)
            transcript_data = {"text": "", "words": []}
            if asset["has_audio"]:
                try:
                    audio_path = await extract_audio_async(
                        file_path,
                        temp_dir / f"{stem}.wav",
                    )
                    # ElevenLabs SDK is sync — run in a thread to avoid blocking
                    loop = asyncio.get_running_loop()
                    transcript_data = await loop.run_in_executor(
                        None,
                        _transcribe_audio,
                        str(audio_path),
                    )
                except Exception as exc:
                    logger.error(f"🗄️  Transcription failed for {asset['file_name']}: {exc}")

            # 3. Split video into temporal segments
            visual_segments = []
            try:
                segments = await extract_video_segments_async(
                    file_path,
                    temp_dir / f"{stem}_segments",
                )

                # 4. Analyze all segments concurrently (shared VLM semaphore),
                #    grounding each in the speech spoken during it (A/V fusion).
                visual_segments = await _analyze_video_segments(
                    segments,
                    "media",
                    http_client,
                    vlm_sem,
                    words=transcript_data.get("words", []),
                )
            except Exception as exc:
                logger.error(f"🗄️  Segment analysis failed for {asset['file_name']}: {exc}")

            return _build_asset_index(asset, transcript_data, visual_segments)

        # Process all assets concurrently (STT calls + VLM calls run in parallel)
        asset_tasks = [_process_asset(asset) for asset in raw_assets]
        asset_results = await asyncio.gather(*asset_tasks, return_exceptions=True)

        for asset, result in zip(raw_assets, asset_results):
            if isinstance(result, Exception):
                logger.error(f"🗄️  Asset processing failed for {asset['file_name']}: {result}")
            else:
                master_index.extend(result)

    # Persist index to disk for debugging
    index_path = settings.state_dir / "master_index.json"
    with open(index_path, "w") as f:
        json.dump(master_index, f, indent=2)

    # ── Cleanup temp files (WAVs, extracted frames) ───────────────────
    # Extracted audio and keyframes are only needed during indexing.
    # Clean them up to avoid unbounded disk growth across pipeline runs.
    import shutil as _shutil

    if temp_dir.exists():
        try:
            _shutil.rmtree(temp_dir)
            logger.info(f"🗄️  Archivist: Cleaned up temp dir: {temp_dir}")
        except OSError as exc:
            logger.warning(f"🗄️  Archivist: Failed to clean temp dir: {exc}")

    logger.info(f"🗄️  Archivist: Built master index with {len(master_index)} entries")

    return {
        "phase": Phase.INDEXED,
        "raw_assets": raw_assets,
        "master_index": master_index,
    }
