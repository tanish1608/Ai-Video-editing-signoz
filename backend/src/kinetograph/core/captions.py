"""
Caption renderer — engaging word-by-word animated captions.

Generates ASS (Advanced SubStation Alpha) subtitle files from ElevenLabs
word-level timestamps.  The style mimics modern short-form video captions:
  - Words appear in groups of 3–5 (natural phrase chunks)
  - The ACTIVE word is highlighted in a bright accent colour (yellow)
  - Other words in the group are white
  - A semi-transparent dark pill/box sits behind the text
  - Positioned in the lower third of the frame

The ASS file is burned into the video via FFmpeg's libass filter.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from textwrap import dedent

from kinetograph.config import settings
from kinetograph.core.compositor import escape_ffmpeg_filter_path

logger = logging.getLogger(__name__)

# ─── ASS Style Constants ──────────────────────────────────────────────────────

# Colours in ASS format: &HAABBGGRR& (AA=alpha, BB=blue, GG=green, RR=red)
# 00 alpha = fully opaque, FF = fully transparent
_WHITE = "&H00FFFFFF"       # white
_YELLOW = "&H0000FFFF"     # yellow (highlight colour)
_OUTLINE = "&H00000000"    # black outline
_BOX_BG = "&HC0000000"     # semi-transparent black background (C0 = 75% opaque)

_FONT_NAME = "Arial"
_FONT_SIZE = 56             # Tuned for 1080px width vertical video
_OUTLINE_SIZE = 3
_SHADOW_SIZE = 0
_MARGIN_V = 200             # Push up from bottom edge (pixels)
_MARGIN_H = 50              # Left/right margins

# Maximum words per caption group
_MAX_WORDS_PER_GROUP = 4
# Minimum duration (seconds) a caption group stays on screen
_MIN_GROUP_DURATION = 0.4


# ─── Caption Style Presets ────────────────────────────────────────────────────

CAPTION_STYLE_PRESETS: dict[str, dict] = {
    "bold-yellow": {
        "id": "bold-yellow",
        "name": "Bold Yellow",
        "description": "TikTok-style — yellow highlight on active word, white others, dark pill background",
        "preview": "🟡 Bold yellow highlight",
        "font_name": "Arial",
        "font_size": 56,
        "active_color": "&H0000FFFF",   # yellow
        "inactive_color": "&H00FFFFFF", # white
        "outline_color": "&H00000000",  # black
        "bg_color": "&HC0000000",       # semi-transparent black
        "outline_size": 3,
        "position": "bottom",           # bottom | center | top
        "border_style": 4,              # 4 = opaque box
    },
    "clean-white": {
        "id": "clean-white",
        "name": "Clean White",
        "description": "Minimal white text with subtle outline, no background box",
        "preview": "⬜ Clean white minimal",
        "font_name": "Arial",
        "font_size": 52,
        "active_color": "&H00FFFFFF",   # white (bold)
        "inactive_color": "&H80FFFFFF", # semi-transparent white
        "outline_color": "&H00000000",  # black
        "bg_color": "&H00000000",       # transparent (no box)
        "outline_size": 4,
        "position": "bottom",
        "border_style": 1,              # 1 = outline + drop shadow
    },
    "neon-green": {
        "id": "neon-green",
        "name": "Neon Pop",
        "description": "Electric green highlight on active word, punchy and modern",
        "preview": "🟢 Neon green highlight",
        "font_name": "Arial",
        "font_size": 58,
        "active_color": "&H0000FF00",   # green
        "inactive_color": "&H00FFFFFF", # white
        "outline_color": "&H00000000",  # black
        "bg_color": "&HC0000000",       # semi-transparent black
        "outline_size": 3,
        "position": "bottom",
        "border_style": 4,
    },
    "subtitle-classic": {
        "id": "subtitle-classic",
        "name": "Classic Subtitles",
        "description": "Traditional TV subtitles — all white, no per-word highlight",
        "preview": "📺 Classic TV subtitles",
        "font_name": "Arial",
        "font_size": 48,
        "active_color": "&H00FFFFFF",   # white (same as inactive — no highlight)
        "inactive_color": "&H00FFFFFF", # white
        "outline_color": "&H00000000",  # black
        "bg_color": "&HC0000000",       # semi-transparent black
        "outline_size": 2,
        "position": "bottom",
        "border_style": 4,
    },
}


def _resolve_font(bold: bool = True) -> str:
    """Return an absolute path to a .ttf font file Pillow can open.

    Searches common macOS / Linux font dirs for Arial Bold, then Arial,
    then falls back to any available .ttf.  Returns the name string
    'Arial' as a last resort (may still fail on headless systems).
    """
    candidates: list[str] = []
    if bold:
        candidates += [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/LiberationSans-Bold.ttf",
        ]
    candidates += [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    # Last-ditch: scan /System/Library/Fonts for any .ttf
    for d in ["/System/Library/Fonts", "/usr/share/fonts"]:
        try:
            for p in Path(d).rglob("*.ttf"):
                return str(p)
        except OSError:
            continue
    return "Arial"  # fallback — may work if system fontconfig resolves it


# ─── ASS File Builder ─────────────────────────────────────────────────────────

def _ass_header(width: int, height: int, style: dict | None = None) -> str:
    """Generate the ASS file header with script info and styles."""
    s = style or {}
    font_name = s.get("font_name", _FONT_NAME)
    font_size = s.get("font_size", _FONT_SIZE)
    inactive_color = s.get("inactive_color", _WHITE)
    active_color = s.get("active_color", _YELLOW)
    outline_color = s.get("outline_color", _OUTLINE)
    bg_color = s.get("bg_color", _BOX_BG)
    outline_size = s.get("outline_size", _OUTLINE_SIZE)
    border_style = s.get("border_style", 4)
    position = s.get("position", "bottom")
    margin_v = {"top": 60, "center": 0, "bottom": _MARGIN_V}.get(position, _MARGIN_V)
    alignment = {"top": 8, "center": 5, "bottom": 2}.get(position, 2)  # ASS numpad alignment

    return dedent(f"""\
        [Script Info]
        Title: Kinetograph Captions
        ScriptType: v4.00+
        WrapStyle: 0
        ScaledBorderAndShadow: yes
        YCbCr Matrix: None
        PlayResX: {width}
        PlayResY: {height}

        [V4+ Styles]
        Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
        Style: Default,{font_name},{font_size},{inactive_color},{active_color},{outline_color},{bg_color},-1,0,0,0,100,100,1,0,{border_style},{outline_size},{_SHADOW_SIZE},{alignment},{_MARGIN_H},{_MARGIN_H},{margin_v}

        [Events]
        Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    """)


def _ms_to_ass_time(ms: int) -> str:
    """Convert milliseconds to ASS timestamp format H:MM:SS.cc (centiseconds)."""
    if ms < 0:
        ms = 0
    total_cs = ms // 10
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean_word(text: str) -> str:
    """Clean a word for display — strip edge whitespace but keep punctuation.

    Also neutralizes ASS override characters (``{`` ``}`` ``\\``) so a transcript
    token containing them can't corrupt the styling override block or start an
    unintended override.
    """
    return (
        text.strip()
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


# ─── Word Grouping ────────────────────────────────────────────────────────────

def _group_words(words: list[dict], max_per_group: int = _MAX_WORDS_PER_GROUP) -> list[list[dict]]:
    """
    Group words into caption chunks of max_per_group words.

    Splits on sentence-ending punctuation (. ! ?) to keep phrases natural.
    Each group is a list of word dicts with {text, start_ms, end_ms}.
    """
    if not words:
        return []

    groups: list[list[dict]] = []
    current_group: list[dict] = []

    for word in words:
        text = word.get("text", "").strip()
        if not text:
            continue

        current_group.append(word)

        # Split on sentence boundaries or when group is full
        is_sentence_end = bool(re.search(r'[.!?]$', text))
        is_full = len(current_group) >= max_per_group

        if is_sentence_end or is_full:
            groups.append(current_group)
            current_group = []

    # Don't forget the last group
    if current_group:
        groups.append(current_group)

    return groups


# ─── ASS Dialogue Events ──────────────────────────────────────────────────────

def _build_events(word_groups: list[list[dict]], style: dict | None = None) -> list[str]:
    """
    Generate ASS dialogue events for each word group.

    For each group, creates sub-events where each word takes its turn
    being highlighted. This produces the TikTok-style word-by-word
    animation effect.
    """
    s = style or {}
    active_color = s.get("active_color", _YELLOW)
    inactive_color = s.get("inactive_color", _WHITE)
    events: list[str] = []

    for group in word_groups:
        if not group:
            continue

        group_start_ms = group[0]["start_ms"]
        group_end_ms = group[-1]["end_ms"]

        # Ensure minimum duration
        if (group_end_ms - group_start_ms) < _MIN_GROUP_DURATION * 1000:
            group_end_ms = group_start_ms + int(_MIN_GROUP_DURATION * 1000)

        all_words = [_clean_word(w["text"]) for w in group]

        # Create a sub-event for each word's active period
        for i, word_dict in enumerate(group):
            word_start = word_dict["start_ms"]

            # Word end = start of next word (or group end for last word)
            if i + 1 < len(group):
                word_end = group[i + 1]["start_ms"]
            else:
                word_end = group_end_ms

            # Clamp
            word_start = max(word_start, group_start_ms)
            word_end = min(word_end, group_end_ms)
            if word_end <= word_start:
                word_end = word_start + 100  # at least 100ms

            # Build the styled text: all words shown, active one highlighted
            parts: list[str] = []
            for j, w_text in enumerate(all_words):
                if j == i:
                    # Active word — highlighted colour, slightly scaled up
                    parts.append(
                        f"{{\\c{active_color}\\fscx110\\fscy110\\b1}}{w_text}"
                        f"{{\\c{inactive_color}\\fscx100\\fscy100\\b0}}"
                    )
                else:
                    parts.append(w_text)

            styled_text = " ".join(parts)

            start_ts = _ms_to_ass_time(word_start)
            end_ts = _ms_to_ass_time(word_end)

            # Layer 0, Default style
            event = f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{styled_text}"
            events.append(event)

    return events


# ─── Timeline Mapping ─────────────────────────────────────────────────────────

def map_words_to_timeline(
    approved_edit: dict,
    master_index: list[dict],
) -> list[dict]:
    """
    Map word-level timestamps from source files to the rendered video timeline.

    The approved edit clips reference [in_ms, out_ms] ranges in source files.
    The rendered video concatenates these clips sequentially.  This function
    remaps each word's timing to the output timeline.

    Returns a flat list of word dicts with remapped {text, start_ms, end_ms}.
    """
    clips = approved_edit.get("clips", [])
    if not clips:
        return []

    # Build a lookup: source_file → list of words (from master_index)
    file_words: dict[str, list[dict]] = {}
    for entry in master_index:
        src = entry.get("asset_file", "")
        words = entry.get("words", [])
        if src and words:
            if src not in file_words:
                file_words[src] = []
            file_words[src].extend(words)

    # Sort each file's words by start_ms
    for src in file_words:
        file_words[src].sort(key=lambda w: w.get("start_ms", 0))

    # Walk through clips and remap words
    remapped_words: list[dict] = []
    cumulative_offset_ms = 0

    for clip in clips:
        clip_type = clip.get("clip_type", "")
        source_file = clip.get("source_file", "")
        in_ms = clip.get("in_ms", 0)
        out_ms = clip.get("out_ms", 0)
        clip_duration_ms = out_ms - in_ms

        if clip_duration_ms <= 0:
            continue

        # Only primary clips carry dialogue audio
        if clip_type != "primary":
            # Cutaway clips are visual only — no words, but still advances the timeline
            # Actually, cutaway visuals play OVER primary audio, so they don't
            # advance the timeline independently. Skip them.
            continue

        # Find words from this source file within [in_ms, out_ms]
        src_words = file_words.get(source_file, [])
        for word in src_words:
            w_start = word.get("start_ms", 0)
            w_end = word.get("end_ms", 0)
            w_text = word.get("text", "").strip()

            if not w_text:
                continue

            # Check if word falls within the clip's range (with some tolerance)
            if w_start >= in_ms - 50 and w_end <= out_ms + 50:
                remapped_words.append({
                    "text": w_text,
                    "start_ms": cumulative_offset_ms + max(0, w_start - in_ms),
                    "end_ms": cumulative_offset_ms + max(0, w_end - in_ms),
                })

        cumulative_offset_ms += clip_duration_ms

    return remapped_words


# ─── Public API ────────────────────────────────────────────────────────────────

def generate_ass_captions(
    approved_edit: dict,
    master_index: list[dict],
    output_path: Path,
    width: int | None = None,
    height: int | None = None,
    video_duration_ms: int | None = None,
    style: dict | None = None,
) -> Path | None:
    """
    Generate an ASS subtitle file with engaging word-by-word captions.

    Maps ElevenLabs word timestamps to the rendered video timeline,
    groups them into phrases, and creates highlight-per-word events.

    Parameters
    ----------
    video_duration_ms : int, optional
        The *actual* rendered video duration in milliseconds.  When provided,
        all mapped timestamps are scaled proportionally so that captions stay
        in sync even when Cloudinary cross-fade transitions compress the
        timeline.
    style : dict, optional
        Caption style overrides from a preset (font, colors, position, etc.).

    Returns the path to the .ass file, or None if no words available.
    """
    width = width or settings.output_width
    height = height or settings.output_height
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Map source file words → rendered video timeline
    timeline_words = map_words_to_timeline(approved_edit, master_index)

    if not timeline_words:
        logger.warning("📝 Captions: No words found to generate captions")
        return None

    logger.info(f"📝 Captions: Mapped {len(timeline_words)} words to rendered timeline")

    # ── Scale timestamps to match actual video duration ──────────────
    # Cloudinary cross-fade transitions overlap segments, so the rendered
    # video is shorter than the raw primary sum.  Proportionally scale every
    # timestamp so captions don't drift progressively later.
    if video_duration_ms and timeline_words:
        mapped_end_ms = max(w["end_ms"] for w in timeline_words)
        if mapped_end_ms > 0 and abs(mapped_end_ms - video_duration_ms) > 200:
            scale = video_duration_ms / mapped_end_ms
            logger.info(
                f"📝 Captions: Scaling timeline {mapped_end_ms}ms → "
                f"{video_duration_ms}ms (×{scale:.4f})"
            )
            for w in timeline_words:
                w["start_ms"] = int(w["start_ms"] * scale)
                w["end_ms"] = int(w["end_ms"] * scale)

    # Group into phrase chunks
    word_groups = _group_words(timeline_words, max_per_group=_MAX_WORDS_PER_GROUP)
    logger.info(f"📝 Captions: {len(word_groups)} caption groups")

    # Generate ASS events
    events = _build_events(word_groups, style=style)

    if not events:
        logger.warning("📝 Captions: No events generated")
        return None

    # Write ASS file
    ass_content = _ass_header(width, height, style=style) + "\n".join(events) + "\n"
    output_path.write_text(ass_content, encoding="utf-8")

    logger.info(f"📝 Captions: Generated ASS → {output_path}")
    return output_path


def burn_captions(
    video_path: str | Path,
    ass_path: str | Path,
    output_path: str | Path,
) -> Path:
    """
    Burn captions into a video using FFmpeg's ``ass`` filter.

    This is kept as a standalone utility for ad-hoc caption burns.
    In the normal pipeline, the Director merges the ``ass`` filter into
    its single-pass ``filter_complex`` render, avoiding an extra re-encode.
    """
    video_path = str(video_path)
    ass_path_str = str(ass_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # FFmpeg ass filter needs special chars escaped inside the filtergraph
    escaped = escape_ffmpeg_filter_path(ass_path_str)
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"ass={escaped}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        str(output_path),
    ]
    logger.info("📝 Captions: Burning via FFmpeg ass filter...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"Caption burn failed — FFmpeg ass filter error: {result.stderr[:500]}"
        )
    logger.info(f"📝 Captions: Burned → {output_path}")
    return output_path


def _ffmpeg_has_filter(name: str) -> bool:
    """Check if FFmpeg has a specific filter compiled in."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True, text=True, timeout=10,
        )
        return f" {name} " in result.stdout or f" {name}\n" in result.stdout
    except Exception:
        return False
