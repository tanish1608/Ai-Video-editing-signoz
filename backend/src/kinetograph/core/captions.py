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
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from textwrap import dedent

from kinetograph.config import settings
from kinetograph.core.compositor import escape_ffmpeg_filter_path

logger = logging.getLogger(__name__)

# ─── ASS Style Constants ──────────────────────────────────────────────────────

# Colours in ASS format: &HAABBGGRR& (AA=alpha, BB=blue, GG=green, RR=red)
# 00 alpha = fully opaque, FF = fully transparent
_WHITE = "&H00FFFFFF"  # white
_YELLOW = "&H0000FFFF"  # yellow (highlight colour)
_OUTLINE = "&H00000000"  # black outline
_BOX_BG = "&HC0000000"  # semi-transparent black background (C0 = 75% transparent)

_FONT_NAME = "Arial"
_FONT_SIZE = 56  # Tuned for 1080px width vertical video
_OUTLINE_SIZE = 3
_SHADOW_SIZE = 0
_MARGIN_V = 200  # Push up from bottom edge (pixels)
_MARGIN_H = 50  # Left/right margins

# Maximum words per caption group
_MAX_WORDS_PER_GROUP = 4
# Minimum duration (seconds) a caption group stays on screen
_MIN_GROUP_DURATION = 0.4


# ─── Caption Style Presets ────────────────────────────────────────────────────

CAPTION_STYLE_PRESETS: dict[str, dict] = {
    "bold-yellow": {
        "id": "bold-yellow",
        "name": "Bold Yellow",
        "description": "TikTok-style — yellow highlight on active word, "
        "white others, dark pill background",
        "preview": "🟡 Bold yellow highlight",
        "font_name": "Arial",
        "font_size": 56,
        "active_color": "&H0000FFFF",  # yellow
        "inactive_color": "&H00FFFFFF",  # white
        "outline_color": "&H00000000",  # black
        "bg_color": "&HC0000000",  # semi-transparent black
        "outline_size": 3,
        "position": "bottom",  # bottom | center | top
        "border_style": 3,  # 3 = opaque box
    },
    "clean-white": {
        "id": "clean-white",
        "name": "Clean White",
        "description": "Minimal white text with subtle outline, no background box",
        "preview": "⬜ Clean white minimal",
        "font_name": "Arial",
        "font_size": 52,
        "active_color": "&H00FFFFFF",  # white (bold)
        "inactive_color": "&H80FFFFFF",  # semi-transparent white
        "outline_color": "&H00000000",  # black
        "bg_color": "&H00000000",  # transparent (no box)
        "outline_size": 4,
        "position": "bottom",
        "border_style": 1,  # 1 = outline + drop shadow
    },
    "neon-green": {
        "id": "neon-green",
        "name": "Neon Pop",
        "description": "Electric green highlight on active word, punchy and modern",
        "preview": "🟢 Neon green highlight",
        "font_name": "Arial",
        "font_size": 58,
        "active_color": "&H0000FF00",  # green
        "inactive_color": "&H00FFFFFF",  # white
        "outline_color": "&H00000000",  # black
        "bg_color": "&HC0000000",  # semi-transparent black
        "outline_size": 3,
        "position": "bottom",
        "border_style": 3,
    },
    "subtitle-classic": {
        "id": "subtitle-classic",
        "name": "Classic Subtitles",
        "description": "Traditional TV subtitles — all white, no per-word highlight",
        "preview": "📺 Classic TV subtitles",
        "font_name": "Arial",
        "font_size": 48,
        "active_color": "&H00FFFFFF",  # white (same as inactive — no highlight)
        "inactive_color": "&H00FFFFFF",  # white
        "outline_color": "&H00000000",  # black
        "bg_color": "&HC0000000",  # semi-transparent black
        "outline_size": 2,
        "position": "bottom",
        "border_style": 3,
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
    scale = min(width, height) / 1080
    font_size = max(10, round(s.get("font_size", _FONT_SIZE) * scale))
    inactive_color = s.get("inactive_color", _WHITE)
    active_color = s.get("active_color", _YELLOW)
    outline_color = s.get("outline_color", _OUTLINE)
    bg_color = s.get("bg_color", _BOX_BG)
    outline_size = max(1, s.get("outline_size", _OUTLINE_SIZE) * scale)
    border_style = s.get("border_style", 3)
    position = s.get("position", "bottom")
    margin_v = {"top": 60, "center": 0, "bottom": _MARGIN_V}.get(position, _MARGIN_V)
    margin_v = round(margin_v * scale)
    margin_h = round(_MARGIN_H * scale)
    alignment = {"top": 8, "center": 5, "bottom": 2}.get(position, 2)  # ASS numpad alignment

    style_line = (
        f"Style: Default,{font_name},{font_size},{inactive_color},"
        f"{active_color},{outline_color},{bg_color},-1,0,0,0,100,100,1,0,"
        f"{border_style},{outline_size},{_SHADOW_SIZE},{alignment},{margin_h},"
        f"{margin_h},{margin_v}"
    )
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV"
    )
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
        {style_format}
        {style_line}

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
    return text.strip().replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


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
        is_sentence_end = bool(re.search(r"[.!?]$", text))
        is_full = len(current_group) >= max_per_group

        if is_sentence_end or is_full:
            groups.append(current_group)
            current_group = []

    # Don't forget the last group
    if current_group:
        groups.append(current_group)

    return groups


# ─── ASS Dialogue Events ──────────────────────────────────────────────────────


def _color_override(color: str) -> str:
    value = color.replace("&H", "").replace("&", "").zfill(8)
    return f"\\c&H{value[-6:]}&\\1a&H{value[-8:-6]}&"


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
                        f"{{{_color_override(active_color)}\\fscx110\\fscy110\\b1}}{w_text}"
                        f"{{{_color_override(inactive_color)}\\fscx100\\fscy100\\b1}}"
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
    mapping = approved_edit.get("render_map")
    if mapping is None:
        # Draft/legacy fallback. New renders always provide the exact source map.
        mapping = []
        offset = 0
        has_primary = False
        for clip in approved_edit.get("clips", []):
            primary = clip.get("clip_type") == "primary"
            if clip.get("clip_type") == "overlay" or (has_primary and not primary):
                continue
            duration = clip.get("out_ms", 0) - clip.get("in_ms", 0)
            if duration <= 0:
                continue
            mapping.append(
                {
                    "source_file": clip.get("source_file"),
                    "source_start_ms": clip.get("in_ms", 0),
                    "source_end_ms": clip.get("out_ms", 0),
                    "timeline_start_ms": offset,
                    "has_dialogue": primary,
                }
            )
            offset += duration
            has_primary |= primary
    file_words: dict[str, dict[tuple, dict]] = {}
    for entry in master_index:
        words = file_words.setdefault(entry.get("asset_file", ""), {})
        for word in entry.get("words", []):
            key = (word.get("start_ms", 0), word.get("end_ms", 0), word.get("text", ""))
            words[key] = word
    result = []
    for region in mapping:
        if not region.get("has_dialogue"):
            continue
        start, end = region["source_start_ms"], region["source_end_ms"]
        for word in file_words.get(region["source_file"], {}).values():
            ws, we = word.get("start_ms", 0), word.get("end_ms", 0)
            if start <= ws < we <= end and word.get("text", "").strip():
                result.append(
                    {
                        "text": word["text"].strip(),
                        "start_ms": region["timeline_start_ms"] + ws - start,
                        "end_ms": region["timeline_start_ms"] + we - start,
                    }
                )
    return sorted(result, key=lambda w: w["start_ms"])


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
        The actual rendered duration, used only to clip events at the media end.
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

    # Clip at the media boundary; never stretch speech into trailing silence.
    if video_duration_ms is not None:
        timeline_words = [
            {**w, "end_ms": min(w["end_ms"], video_duration_ms)}
            for w in timeline_words
            if w["start_ms"] < video_duration_ms
        ]

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

    Audio is stream-copied from the clean master so style changes preserve it.
    """
    video_path = str(video_path)
    ass_path_str = str(ass_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    executable = caption_ffmpeg()
    # FFmpeg ass filter needs special chars escaped inside the filtergraph
    escaped = escape_ffmpeg_filter_path(ass_path_str)
    cmd = [
        executable,
        "-hide_banner",
        "-y",
        "-i",
        video_path,
        "-vf",
        f"ass={escaped}",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        str(output_path),
    ]
    logger.info("📝 Captions: Burning via FFmpeg ass filter...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"Caption burn failed — FFmpeg ass filter error: {result.stderr[-1500:]}"
        )
    logger.info(f"📝 Captions: Burned → {output_path}")
    return output_path


def _ffmpeg_has_filter(name: str) -> bool:
    """Check if FFmpeg has a specific filter compiled in."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return f" {name} " in result.stdout or f" {name}\n" in result.stdout
    except Exception:
        return False


@lru_cache(maxsize=1)
def caption_ffmpeg() -> str:
    """Find a subtitle-capable build without replacing the system FFmpeg."""
    candidates = [
        os.environ.get("KINETOGRAPH_CAPTION_FFMPEG"),
        shutil.which("ffmpeg"),
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    ]
    import imageio_ffmpeg

    candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    for candidate in dict.fromkeys(c for c in candidates if c):
        if not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run(
                [candidate, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10
            )
            if " ass " in result.stdout:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError(
        "Captions require FFmpeg with libass. On macOS install ffmpeg-full; "
        "or set KINETOGRAPH_CAPTION_FFMPEG to a subtitle-capable FFmpeg executable."
    )
