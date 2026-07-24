"""
FFmpeg filter_complex compositor — replaces MoviePy for all video compositing.

Builds an FFmpeg ``-filter_complex`` string from timeline data and renders
it in a single async subprocess.  This is 5–20× faster than MoviePy's
frame-by-frame Python/NumPy decode → process → encode loop.

Architecture
────────────
The :class:`FilterGraphBuilder` is a stateful builder that accumulates:

  • **inputs** — media files (``-i file.mp4``)
  • **filters** — filtergraph expressions linked by unique pad labels

After the full timeline is described, call :meth:`build_command` to get the
complete FFmpeg argument list, then :func:`render` to execute asynchronously.

Usage::

    from kinetograph.core.compositor import FilterGraphBuilder, render

    fg = FilterGraphBuilder()
    idx = fg.add_input("clip_a.mp4")
    v = fg.trim_video(idx, 1.0, 5.0)
    a = fg.trim_audio(idx, 1.0, 5.0)
    fg.set_outputs(v, a)
    await render(fg, "output.mp4")
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from kinetograph.core.hwaccel import hw
from kinetograph.observability import tool_span

logger = logging.getLogger(__name__)


def escape_ffmpeg_filter_path(path: str) -> str:
    """Escape a filesystem path for use as a value inside an FFmpeg filtergraph.

    Used for the ``ass``/``subtitles`` filter filename. The previous approach
    wrapped the path in single quotes and backslash-escaped the quote, which
    FFmpeg cannot parse (a backslash does not escape a quote *inside* single
    quotes) and which broke on Windows drive letters. Instead we do not quote,
    and backslash-escape the characters significant to the filtergraph parser.

    Backslashes are normalized to forward slashes first (accepted by FFmpeg on
    Windows), so the escape pass only has to handle the remaining specials.
    """
    p = str(path).replace("\\", "/")
    for ch in ("'", ":", "[", "]", ",", ";"):
        p = p.replace(ch, "\\" + ch)
    return p


# ─── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class SegmentResult:
    """Result of building one timeline segment in the filter graph."""
    video_label: str
    audio_label: str | None
    duration: float  # seconds


# ─── FilterGraphBuilder ─────────────────────────────────────────────────────────

class FilterGraphBuilder:
    """
    Incrementally build an FFmpeg ``-filter_complex`` filter graph.

    Every ``trim_*``, ``concat_*``, ``xfade``, etc. method appends filter
    expressions and returns a **pad label** (a short string like ``"tv3"``)
    that downstream methods can reference.

    Thread-safety: **not** thread-safe — create one builder per render.
    """

    def __init__(self, *, fps: int = 30) -> None:
        self._fps = fps
        self._inputs: list[str] = []          # file paths (order = input index)
        self._filters: list[str] = []         # filter expressions
        self._counter = 0                     # monotonic label counter
        # Track how many times each input stream has been referenced
        # so we know when to use split/asplit.
        self._input_vid_refs: dict[int, int] = {}  # input_idx → ref count
        self._input_aud_refs: dict[int, int] = {}
        # Deferred: split labels created lazily
        self._vid_split_labels: dict[int, list[str]] = {}
        self._aud_split_labels: dict[int, list[str]] = {}
        # Output labels
        self._out_video: str | None = None
        self._out_audio: str | None = None

    # ── Input Management ──────────────────────────────────────────────────

    def add_input(self, path: str) -> int:
        """Register an input file.  Returns its 0-based index."""
        idx = len(self._inputs)
        self._inputs.append(path)
        return idx

    # ── Label Factory ─────────────────────────────────────────────────────

    def _label(self, prefix: str = "v") -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    # ── Raw Input-Stream Accessors ────────────────────────────────────────
    # In FFmpeg filter_complex, each input pad ([N:v], [N:a]) can only be
    # consumed by ONE downstream filter.  If a single input needs to be
    # trimmed into multiple sub-clips, we must first ``split`` / ``asplit``
    # it.  The methods below handle this transparently.

    def _reserve_vid(self, input_idx: int) -> None:
        """Declare that input_idx:v will be consumed one more time."""
        self._input_vid_refs[input_idx] = self._input_vid_refs.get(input_idx, 0) + 1

    def _reserve_aud(self, input_idx: int) -> None:
        self._input_aud_refs[input_idx] = self._input_aud_refs.get(input_idx, 0) + 1

    def _emit_splits(self) -> None:
        """Emit split/asplit filters for every input that's referenced more
        than once.  Must be called ONCE, right before ``build_command()``.

        For singly-referenced inputs the raw ``[N:v]`` / ``[N:a]`` labels
        are used directly (no split filter emitted).
        """
        for idx, count in self._input_vid_refs.items():
            if count <= 1:
                self._vid_split_labels[idx] = [f"{idx}:v"]
            else:
                labels = [self._label("sv") for _ in range(count)]
                outs = "".join(f"[{l}]" for l in labels)
                self._filters.insert(0, f"[{idx}:v]split={count}{outs}")
                self._vid_split_labels[idx] = labels

        for idx, count in self._input_aud_refs.items():
            if count <= 1:
                self._aud_split_labels[idx] = [f"{idx}:a"]
            else:
                labels = [self._label("sa") for _ in range(count)]
                outs = "".join(f"[{l}]" for l in labels)
                self._filters.insert(0, f"[{idx}:a]asplit={count}{outs}")
                self._aud_split_labels[idx] = labels

    def _pop_vid(self, input_idx: int) -> str:
        """Return the next available split label for input_idx video."""
        return self._vid_split_labels[input_idx].pop(0)

    def _pop_aud(self, input_idx: int) -> str:
        return self._aud_split_labels[input_idx].pop(0)

    # ── Low-Level Filter Primitives ───────────────────────────────────────

    def trim_video(self, input_idx: int, start: float, end: float) -> str:
        """Trim video stream of *input_idx* to ``[start, end)`` seconds.

        Returns the output pad label.
        """
        lbl = self._label("tv")
        # Use a deferred placeholder — replaced by _emit_splits before build
        self._reserve_vid(input_idx)
        self._filters.append(
            f"__VID_{input_idx}__"
            f"trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
            f"fps={self._fps}[{lbl}]"
        )
        return lbl

    def trim_audio(self, input_idx: int, start: float, end: float) -> str:
        """Trim audio stream of *input_idx* to ``[start, end)`` seconds."""
        lbl = self._label("ta")
        self._reserve_aud(input_idx)
        self._filters.append(
            f"__AUD_{input_idx}__"
            f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[{lbl}]"
        )
        return lbl

    def null_audio(self, duration: float, rate: int = 48000) -> str:
        """Generate a silent audio pad of *duration* seconds.

        Uses the ``anullsrc`` virtual input routed through the filter graph.
        """
        lbl = self._label("sil")
        # anullsrc is a *source* filter — goes directly in the filtergraph
        self._filters.append(
            f"anullsrc=r={rate}:cl=stereo,atrim=duration={duration:.6f},"
            f"asetpts=PTS-STARTPTS[{lbl}]"
        )
        return lbl

    def concat_video(self, labels: list[str]) -> str:
        """Concatenate video-only streams."""
        if len(labels) == 1:
            return labels[0]
        out = self._label("cv")
        ins = "".join(f"[{l}]" for l in labels)
        self._filters.append(f"{ins}concat=n={len(labels)}:v=1:a=0[{out}]")
        return out

    def concat_audio(self, labels: list[str]) -> str:
        """Concatenate audio-only streams."""
        if len(labels) == 1:
            return labels[0]
        out = self._label("ca")
        ins = "".join(f"[{l}]" for l in labels)
        self._filters.append(f"{ins}concat=n={len(labels)}:v=0:a=1[{out}]")
        return out

    def concat_av(self, av_pairs: list[tuple[str, str]]) -> tuple[str, str]:
        """Concatenate audio+video stream pairs together.

        Each element of *av_pairs* is ``(video_label, audio_label)``.
        Returns ``(video_out, audio_out)``.
        """
        if len(av_pairs) == 1:
            return av_pairs[0]
        v_out = self._label("cav")
        a_out = self._label("caa")
        ins = "".join(f"[{v}][{a}]" for v, a in av_pairs)
        self._filters.append(
            f"{ins}concat=n={len(av_pairs)}:v=1:a=1[{v_out}][{a_out}]"
        )
        return v_out, a_out

    def xfade(self, a: str, b: str, duration: float, offset: float) -> str:
        """Video crossfade (``xfade``) between two pads."""
        out = self._label("xf")
        self._filters.append(
            f"[{a}][{b}]xfade=transition=fade:duration={duration:.6f}"
            f":offset={offset:.6f}[{out}]"
        )
        return out

    def acrossfade(self, a: str, b: str, duration: float) -> str:
        """Audio crossfade between two pads."""
        out = self._label("ax")
        self._filters.append(
            f"[{a}][{b}]acrossfade=d={duration:.6f}:c1=tri:c2=tri[{out}]"
        )
        return out

    def fade_video(
        self, label: str, total_dur: float,
        fade_in: float = 0.0, fade_out: float = 0.0,
    ) -> str:
        """Apply video fade-from-black / fade-to-black."""
        parts: list[str] = []
        if fade_in > 0:
            parts.append(f"fade=t=in:st=0:d={fade_in:.6f}")
        if fade_out > 0 and total_dur > fade_out:
            parts.append(f"fade=t=out:st={total_dur - fade_out:.6f}:d={fade_out:.6f}")
        if not parts:
            return label
        out = self._label("fd")
        self._filters.append(f"[{label}]{','.join(parts)}[{out}]")
        return out

    def fade_audio(
        self, label: str, total_dur: float,
        fade_in: float = 0.0, fade_out: float = 0.0,
    ) -> str:
        """Apply audio fade in/out."""
        parts: list[str] = []
        if fade_in > 0:
            parts.append(f"afade=t=in:st=0:d={fade_in:.6f}")
        if fade_out > 0 and total_dur > fade_out:
            parts.append(f"afade=t=out:st={total_dur - fade_out:.6f}:d={fade_out:.6f}")
        if not parts:
            return label
        out = self._label("af")
        self._filters.append(f"[{label}]{','.join(parts)}[{out}]")
        return out

    def scale(self, label: str, w: int, h: int) -> str:
        """Scale a video pad to exact dimensions."""
        out = self._label("sc")
        self._filters.append(f"[{label}]scale={w}:{h}:flags=lanczos[{out}]")
        return out

    def overlay(
        self, base: str, over: str,
        x: int, y: int,
        enable_start: float | None = None,
        enable_end: float | None = None,
        eof_action: str = "repeat",
    ) -> str:
        """Picture-in-picture overlay with optional enable window."""
        out = self._label("ov")
        enable = ""
        if enable_start is not None and enable_end is not None:
            enable = f":enable='between(t,{enable_start:.6f},{enable_end:.6f})'"
        self._filters.append(
            f"[{base}][{over}]overlay=x={x}:y={y}"
            f":format=auto:shortest=0:eof_action={eof_action}{enable}[{out}]"
        )
        return out

    def delay_pts(self, label: str, delay_seconds: float) -> str:
        """Shift video PTS forward by *delay_seconds*.

        Used to ensure overlay frames arrive at the correct timeline
        position so the ``overlay`` filter composites them at the
        intended time rather than consuming them during the initial
        (pre-enable) portion of the base stream.
        """
        if delay_seconds <= 0:
            return label
        out = self._label("dp")
        self._filters.append(
            f"[{label}]setpts=PTS+{delay_seconds:.6f}/TB[{out}]"
        )
        return out

    def setpts(self, label: str) -> str:
        """Reset PTS on a video stream (``setpts=PTS-STARTPTS``)."""
        out = self._label("sp")
        self._filters.append(f"[{label}]setpts=PTS-STARTPTS[{out}]")
        return out

    def ass_burn(self, video_label: str, ass_path: str) -> str:
        """Burn ASS subtitles into the video stream."""
        out = self._label("sub")
        escaped = escape_ffmpeg_filter_path(ass_path)
        self._filters.append(f"[{video_label}]ass={escaped}[{out}]")
        return out

    def atrim_to_duration(self, label: str, duration: float) -> str:
        """Hard-trim an audio pad to *duration* seconds."""
        out = self._label("at")
        self._filters.append(
            f"[{label}]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[{out}]"
        )
        return out

    # ── Output ────────────────────────────────────────────────────────────

    def set_outputs(self, video: str, audio: str | None = None) -> None:
        """Declare the final output pad labels."""
        self._out_video = video
        self._out_audio = audio

    # ── Build ─────────────────────────────────────────────────────────────

    def build_command(
        self,
        output_path: str,
        *,
        fps: int = 30,
        audio_codec: str = "aac",
        audio_bitrate: str = "192k",
        audio_rate: int = 48000,
        pix_fmt: str = "yuv420p",
    ) -> list[str]:
        """
        Compile the accumulated graph into a complete ``ffmpeg`` command.

        Returns the full argument list ready for ``subprocess`` /
        ``asyncio.create_subprocess_exec``.
        """
        # 1. Resolve split/asplit for multi-referenced inputs
        self._emit_splits()

        # 2. Replace deferred placeholders with real split labels
        resolved: list[str] = []
        vid_cursors: dict[int, int] = {k: 0 for k in self._vid_split_labels}
        aud_cursors: dict[int, int] = {k: 0 for k in self._aud_split_labels}

        for filt in self._filters:
            f = filt
            # Replace video placeholders
            for idx, labels in self._vid_split_labels.items():
                placeholder = f"__VID_{idx}__"
                while placeholder in f:
                    cursor = vid_cursors[idx]
                    real_label = labels[cursor] if cursor < len(labels) else f"{idx}:v"
                    f = f.replace(placeholder, f"[{real_label}]", 1)
                    vid_cursors[idx] = cursor + 1
            # Replace audio placeholders
            for idx, labels in self._aud_split_labels.items():
                placeholder = f"__AUD_{idx}__"
                while placeholder in f:
                    cursor = aud_cursors[idx]
                    real_label = labels[cursor] if cursor < len(labels) else f"{idx}:a"
                    f = f.replace(placeholder, f"[{real_label}]", 1)
                    aud_cursors[idx] = cursor + 1
            resolved.append(f)

        # Join with ';' only — embedding raw newlines can trip some FFmpeg
        # builds' filtergraph parser (a filter name beginning with whitespace).
        filter_complex = ";".join(resolved)

        # 3. Build the full command
        cmd: list[str] = ["ffmpeg", "-y", "-hide_banner"]

        # NOTE: we deliberately do NOT emit hardware *decode* flags here.
        # The entire filtergraph (trim/scale/overlay/xfade/concat) runs on CPU
        # filters, so forcing decoded frames into GPU memory (e.g.
        # "-hwaccel cuda -hwaccel_output_format cuda") made every filter fail on
        # NVIDIA/VAAPI. Hardware *encoding* (below, via hw.video_codec_flags)
        # accepts system-memory frames and is where the real speedup is.

        # Inputs
        for path in self._inputs:
            cmd.extend(["-i", path])

        # Filter graph
        cmd.extend(["-filter_complex", filter_complex])

        # Map outputs
        if self._out_video:
            cmd.extend(["-map", f"[{self._out_video}]"])
        if self._out_audio:
            cmd.extend(["-map", f"[{self._out_audio}]"])

        # Video codec (hardware-accelerated or software)
        cmd.extend(hw.video_codec_flags())
        cmd.extend(["-pix_fmt", pix_fmt])

        # Audio codec
        if self._out_audio:
            cmd.extend(["-c:a", audio_codec, "-b:a", audio_bitrate, "-ar", str(audio_rate)])

        # FPS (applied in output, not filter, for maximum compat)
        cmd.extend(["-r", str(fps)])

        cmd.append(output_path)
        return cmd

    # ── High-Level: Build a Segment ───────────────────────────────────────

    def build_segment(
        self,
        primary_input: int,
        p_in: float,
        p_out: float,
        skip_regions: list[tuple[float, float]],
        cutaway_inputs: list[tuple[int, float, float]],
        insert_point: float,
        has_audio: bool = True,
    ) -> SegmentResult:
        """
        Build one composited timeline segment.

        This replicates the Director's ``_build_segment_video()`` logic
        entirely within the FFmpeg filter graph — no MoviePy needed.

        Args:
            primary_input: Input index of the normalized primary clip.
            p_in: Start time in the primary source (seconds).
            p_out: End time in the primary source (seconds).
            skip_regions: List of ``(start_s, end_s)`` stutter/filler regions
                          (absolute source timestamps within ``[p_in, p_out]``).
            cutaway_inputs: List of ``(input_idx, in_s, out_s)`` for cutaway clips.
            insert_point: Seconds into the *clean* (de-stuttered) primary
                          where cutaway visuals begin.
            has_audio: Whether the primary clip has an audio stream.

        Returns:
            :class:`SegmentResult` with video/audio labels and total duration.
        """
        # ── 1. Compute clean ranges (non-skip portions of primary) ───────
        clean_ranges = _compute_clean_ranges(p_in, p_out, skip_regions)
        if not clean_ranges:
            # Entire clip is stutters — shouldn't happen but handle gracefully
            clean_ranges = [(p_in, p_out)]

        clean_duration = sum(e - s for s, e in clean_ranges)

        # ── 2. If no cutaways, just concat the clean primary ─────────────
        if not cutaway_inputs:
            v_labels = [self.trim_video(primary_input, s, e) for s, e in clean_ranges]
            seg_v = self.concat_video(v_labels)
            seg_a = None
            if has_audio:
                a_labels = [self.trim_audio(primary_input, s, e) for s, e in clean_ranges]
                seg_a = self.concat_audio(a_labels)
            return SegmentResult(seg_v, seg_a, clean_duration)

        # ── 3. With cutaways: split clean ranges at the insertion point ──
        cutaway_durs = [out_s - in_s for _, in_s, out_s in cutaway_inputs]
        total_cutaway = sum(cutaway_durs)

        # Cap cutaways so they don't exceed primary duration
        if total_cutaway > clean_duration:
            scale = (clean_duration * 0.9) / total_cutaway
            cutaway_inputs = [
                (idx, in_s, in_s + max(0.5, (out_s - in_s) * scale))
                for idx, in_s, out_s in cutaway_inputs
            ]
            cutaway_durs = [out_s - in_s for _, in_s, out_s in cutaway_inputs]
            total_cutaway = sum(cutaway_durs)

        # Clamp insert_point
        insert_point = max(0.2, min(insert_point, clean_duration - total_cutaway))
        insert_point = max(0.2, insert_point)

        before_ranges, after_ranges = _split_clean_ranges_at(
            clean_ranges, insert_point, total_cutaway,
        )

        # ── 4. Build video parts: [before] + [cutaways] + [after] ───────
        video_parts: list[str] = []

        # Before cutaway
        for s, e in before_ranges:
            video_parts.append(self.trim_video(primary_input, s, e))

        # Cutaway visuals (video only — no audio)
        for c_idx, c_in, c_out in cutaway_inputs:
            video_parts.append(self.trim_video(c_idx, c_in, c_out))

        # After cutaway
        for s, e in after_ranges:
            video_parts.append(self.trim_video(primary_input, s, e))

        seg_v = self.concat_video(video_parts)

        # ── 5. Audio: continuous primary audio under cutaway visuals ──
        # Traditional NLE behaviour: the primary's spoken audio plays
        # uninterrupted while cutaway *visuals* replace the primary video.
        # The audio from ALL clean ranges (before + gap + after) gives us
        # exactly the right duration because the gap consumed by
        # _split_clean_ranges_at equals total_cutaway by construction.
        seg_a = None
        if has_audio:
            a_labels = [self.trim_audio(primary_input, s, e)
                        for s, e in clean_ranges]
            seg_a = self.concat_audio(a_labels)

        return SegmentResult(seg_v, seg_a, clean_duration)

    def build_standalone_cutaway_segment(
        self,
        cutaway_inputs: list[tuple[int, float, float]],
    ) -> SegmentResult:
        """Build a segment from cutaway clips only (no primary audio)."""
        video_parts = []
        total_dur = 0.0
        for c_idx, c_in, c_out in cutaway_inputs:
            video_parts.append(self.trim_video(c_idx, c_in, c_out))
            total_dur += c_out - c_in

        seg_v = self.concat_video(video_parts)
        # Silent audio track so concat works uniformly
        seg_a = self.null_audio(total_dur)
        return SegmentResult(seg_v, seg_a, total_dur)

    # ── High-Level: Chain Segments with Crossfade ─────────────────────────

    def chain_segments(
        self,
        segments: list[SegmentResult],
        crossfade_dur: float = 0.2,
        bookend_fade: float = 0.3,
    ) -> tuple[str, str | None, float]:
        """
        Chain multiple segments with video crossfade transitions.

        Audio is chained with matching crossfade overlap so it stays
        perfectly aligned with the video timeline.

        Returns ``(video_label, audio_label, total_duration)``.
        """
        if not segments:
            raise ValueError("No segments to chain")

        if len(segments) == 1:
            s = segments[0]
            v = self.fade_video(s.video_label, s.duration, bookend_fade, bookend_fade)
            a = s.audio_label
            if a:
                a = self.fade_audio(a, s.duration, bookend_fade, bookend_fade)
            return v, a, s.duration

        # ── Video: iterative xfade chain ─────────────────────────────────
        xf = min(crossfade_dur, 0.5)
        current_v = segments[0].video_label
        cumulative_dur = segments[0].duration

        for seg in segments[1:]:
            offset = cumulative_dur - xf
            if offset < 0.01:
                # Segment too short for crossfade → just concat
                current_v = self.concat_video([current_v, seg.video_label])
                cumulative_dur += seg.duration
            else:
                current_v = self.xfade(current_v, seg.video_label, xf, offset)
                cumulative_dur = cumulative_dur + seg.duration - xf

        # Bookend fades
        current_v = self.fade_video(current_v, cumulative_dur, bookend_fade, bookend_fade)

        # ── Audio: crossfade chain matching video overlap ────────────────
        # Each video xfade overlaps by `xf` seconds, so audio must also
        # overlap by the same amount to stay aligned with the video.
        audio_labels = [s.audio_label for s in segments if s.audio_label]
        current_a: str | None = None
        if audio_labels:
            if len(audio_labels) == 1:
                current_a = audio_labels[0]
            else:
                current_a = audio_labels[0]
                a_dur = segments[0].duration
                for i, seg in enumerate(segments[1:], start=1):
                    if seg.audio_label is None:
                        continue
                    # Crossfade audio with the same overlap as video
                    if a_dur - xf >= 0.01 and seg.duration >= xf:
                        current_a = self.acrossfade(current_a, seg.audio_label, xf)
                        a_dur = a_dur + seg.duration - xf
                    else:
                        current_a = self.concat_audio([current_a, seg.audio_label])
                        a_dur += seg.duration
                # Safety trim if rounding leaves audio slightly long
                if a_dur > cumulative_dur + 0.01:
                    current_a = self.atrim_to_duration(current_a, cumulative_dur)
            current_a = self.fade_audio(current_a, cumulative_dur, bookend_fade, bookend_fade)

        return current_v, current_a, cumulative_dur

    # ── High-Level: PiP Overlay ───────────────────────────────────────────

    def add_pip_overlay(
        self,
        base_video: str,
        overlay_input: int,
        ov_in: float,
        ov_out: float,
        timeline_start: float,
        target_w: int,
        target_h: int,
        target_x: int,
        target_y: int,
        base_duration: float,
    ) -> str:
        """
        Composite a picture-in-picture overlay onto the base video.

        The overlay is trimmed, scaled, then its PTS is shifted forward by
        ``timeline_start`` so that frames arrive at the correct position
        on the base timeline.  ``eof_action=pass`` ensures the base video
        passes through cleanly once the overlay ends.  An ``enable``
        window is kept as a safety net.

        Returns the new video label with the overlay applied.
        """
        ov_v = self.trim_video(overlay_input, ov_in, ov_out)
        ov_v = self.scale(ov_v, target_w, target_h)

        # Delay overlay PTS so its frames arrive at the right timeline
        # position.  Without this, the overlay filter consumes frames
        # during the pre-enable window and only the last (repeated)
        # frame is visible when enable becomes true.
        ov_v = self.delay_pts(ov_v, timeline_start)

        overlay_dur = ov_out - ov_in
        enable_end = min(timeline_start + overlay_dur, base_duration)

        return self.overlay(
            base_video, ov_v,
            target_x, target_y,
            enable_start=timeline_start,
            enable_end=enable_end,
            eof_action="pass",
        )


# ─── Helper Functions ────────────────────────────────────────────────────────

def _compute_clean_ranges(
    p_in: float,
    p_out: float,
    skip_regions: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """
    Compute non-skip (clean) ranges within ``[p_in, p_out]``.

    Returns a sorted list of ``(start, end)`` tuples representing the
    portions of the clip that should be KEPT after removing stutters/fillers.
    """
    if not skip_regions:
        return [(p_in, p_out)]

    ranges: list[tuple[float, float]] = []
    current = p_in
    for skip_start, skip_end in sorted(skip_regions):
        if skip_start > current + 0.05:
            ranges.append((current, skip_start))
        current = max(current, skip_end)
    if current < p_out - 0.05:
        ranges.append((current, p_out))

    return ranges if ranges else [(p_in, p_out)]


def _split_clean_ranges_at(
    clean_ranges: list[tuple[float, float]],
    split_point: float,
    cutaway_duration: float,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """
    Split the clean ranges into two groups at *split_point* seconds,
    removing a gap of *cutaway_duration* from the "after" portion.

    *split_point* is in "clean time" (0-based, after stutter removal).
    Returns ``(before_ranges, after_ranges)`` in source-time coordinates.

    The "before" group covers ``[0, split_point)`` in clean-time.
    The "after" group covers ``[split_point + cutaway_duration, ...]``
    in clean-time, so the primary video + cutaway visuals together equal
    exactly ``clean_duration`` and the audio (which spans all clean ranges
    minus the gap) matches the video duration.
    """
    before: list[tuple[float, float]] = []
    after: list[tuple[float, float]] = []
    elapsed = 0.0  # accumulated "clean time"
    phase: str = "before"  # "before" → "gap" → "after"
    gap_remaining = cutaway_duration

    for start, end in clean_ranges:
        rng_dur = end - start

        if phase == "before":
            remaining_before = split_point - elapsed
            if remaining_before >= rng_dur:
                # Entire range goes to "before"
                before.append((start, end))
                elapsed += rng_dur
            elif remaining_before > 0.01:
                # Split within this range — first part goes to "before"
                before.append((start, start + remaining_before))
                # Remainder enters the "gap" phase
                leftover_start = start + remaining_before
                leftover_dur = rng_dur - remaining_before
                elapsed += remaining_before
                phase = "gap"
                # Consume gap from the leftover
                if leftover_dur <= gap_remaining + 0.001:
                    gap_remaining -= leftover_dur
                    elapsed += leftover_dur
                    if gap_remaining <= 0.01:
                        phase = "after"
                else:
                    # Gap ends within this range
                    after_start = leftover_start + gap_remaining
                    if after_start < end - 0.01:
                        after.append((after_start, end))
                    elapsed += leftover_dur
                    gap_remaining = 0
                    phase = "after"
            else:
                # split_point is essentially 0 — enter gap phase
                phase = "gap"
                if rng_dur <= gap_remaining + 0.001:
                    gap_remaining -= rng_dur
                    elapsed += rng_dur
                    if gap_remaining <= 0.01:
                        phase = "after"
                else:
                    after_start = start + gap_remaining
                    if after_start < end - 0.01:
                        after.append((after_start, end))
                    elapsed += rng_dur
                    gap_remaining = 0
                    phase = "after"

        elif phase == "gap":
            if rng_dur <= gap_remaining + 0.001:
                gap_remaining -= rng_dur
                elapsed += rng_dur
                if gap_remaining <= 0.01:
                    phase = "after"
            else:
                after_start = start + gap_remaining
                if after_start < end - 0.01:
                    after.append((after_start, end))
                elapsed += rng_dur
                gap_remaining = 0
                phase = "after"

        else:  # phase == "after"
            after.append((start, end))
            elapsed += rng_dur

    return before, after


# ─── Async Render ────────────────────────────────────────────────────────────

async def render(
    builder: FilterGraphBuilder,
    output_path: str | Path,
    *,
    fps: int = 30,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
    audio_rate: int = 48000,
    timeout: int = 1200,
) -> Path:
    """
    Execute a built filter graph asynchronously.

    Uses ``asyncio.create_subprocess_exec`` so the event loop is never blocked
    (unlike the old ``subprocess.run`` calls which froze the entire FastAPI
    server during rendering).

    Returns the output path on success; raises ``RuntimeError`` on failure.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = builder.build_command(
        str(out),
        fps=fps,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        audio_rate=audio_rate,
    )

    logger.info(f"🎬 Compositor: Rendering → {out.name}")
    logger.debug(f"🎬 Compositor: cmd = {shlex.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        # Reap the killed process so it doesn't linger as a zombie with
        # undrained pipes ("Exception ignored" transport warnings).
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        raise RuntimeError(f"FFmpeg render timed out after {timeout}s")

    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace")[-2000:]
        logger.error(f"🎬 Compositor: FFmpeg failed (rc={proc.returncode}):\n{err_text}")
        raise RuntimeError(f"FFmpeg render failed (rc={proc.returncode}): {err_text[-500:]}")

    logger.info(f"🎬 Compositor: ✓ Render complete → {out}")
    return out


async def run_ffmpeg_async(
    cmd: list[str],
    *,
    timeout: int = 600,
    description: str = "FFmpeg command",
) -> tuple[bytes, bytes]:
    """
    Run an arbitrary FFmpeg command asynchronously.

    Convenience wrapper used by the sound engineer and other agents
    that build their own FFmpeg commands.

    Returns ``(stdout, stderr)`` bytes on success.
    Raises ``RuntimeError`` on failure.
    """
    logger.debug(f"🎬 {description}: {shlex.join(cmd)}")

    with tool_span("ffmpeg", **{"ffmpeg.description": description}):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise RuntimeError(f"{description} timed out after {timeout}s")

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"{description} failed (rc={proc.returncode}): {err_text[-500:]}")

        return stdout, stderr
