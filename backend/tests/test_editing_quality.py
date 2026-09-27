"""Editorial and render regressions. Provider calls are mocked; FFmpeg is real."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from kinetograph.config import settings
from kinetograph.core.captions import (
    CAPTION_STYLE_PRESETS,
    generate_ass_captions,
    map_words_to_timeline,
)
from kinetograph.core.compositor import FilterGraphBuilder, SegmentResult, segment_overlaps
from kinetograph.agents.scripter import _validate_paper_edit


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kinetograph_project_dir", str(tmp_path))
    return tmp_path


def clip(cid="one", start=0, end=4000, kind="primary"):
    return {
        "clip_id": cid,
        "source_file": "source.mp4",
        "in_ms": start,
        "out_ms": end,
        "clip_type": kind,
        "transition": "cut",
    }


def index():
    return [
        {
            "asset_file": "source.mp4",
            "start_ms": 0,
            "end_ms": 4000,
            "words": [
                {"text": "hello", "start_ms": 200, "end_ms": 500},
                {"text": "removed", "start_ms": 1300, "end_ms": 1600},
                {"text": "world", "start_ms": 2200, "end_ms": 2500},
            ],
        }
    ]


def test_captions_follow_actual_render_map_and_dedupe_words():
    edit = {
        "render_map": [
            {
                "clip_id": "one",
                "source_file": "source.mp4",
                "source_start_ms": 0,
                "source_end_ms": 1000,
                "timeline_start_ms": 2000,
                "has_dialogue": True,
            },
            {
                "clip_id": "one",
                "source_file": "source.mp4",
                "source_start_ms": 2000,
                "source_end_ms": 4000,
                "timeline_start_ms": 3000,
                "has_dialogue": True,
            },
        ]
    }
    assert map_words_to_timeline(edit, index() * 2) == [
        {"text": "hello", "start_ms": 2200, "end_ms": 2500},
        {"text": "world", "start_ms": 3200, "end_ms": 3500},
    ]


def test_captions_do_not_stretch_into_trailing_silence(project):
    output = generate_ass_captions(
        {"clips": [clip()]}, index(), project / "captions.ass", video_duration_ms=9000
    )
    text = output.read_text()
    assert "0:00:00.20" in text
    assert "0:00:02.20" in text
    assert "0:00:07.92" not in text


def test_leading_visual_advances_caption_timeline():
    words = map_words_to_timeline({"clips": [clip("intro", 0, 2000, "cutaway"), clip()]}, index())
    assert words[0]["start_ms"] == 2200


def test_transition_video_audio_have_same_bounded_overlap():
    segments = [
        SegmentResult("v0", "a0", 1),
        SegmentResult("v1", None, 0.1),
        SegmentResult("v2", "a2", 2),
    ]
    assert segment_overlaps(segments, transitions=[0.5, 0]) == [0.05, 0]
    graph = FilterGraphBuilder()
    _, _, duration = graph.chain_segments(segments, transitions=[0.5, 0], bookend_fade=0)
    assert duration == pytest.approx(3.05)
    filters = ";".join(graph._filters)
    assert "xfade=transition=fade:duration=0.050000" in filters
    assert "acrossfade=d=0.050000" in filters
    assert "anullsrc" in filters


def test_script_runtime_excludes_broll_and_pip():
    edit = {"clips": [clip(), clip("b", 0, 3000, "cutaway"), clip("p", 0, 1000, "overlay")]}
    assert not _validate_paper_edit(edit, index())
    assert edit["total_duration_ms"] == 4000


def test_visual_highlights_do_not_snap_to_speech_edges():
    edit = {"clips": [clip(start=300, end=3700)]}
    assert not _validate_paper_edit(edit, index(), "highlights")
    assert edit["clips"][0]["in_ms"] == 300
    assert edit["clips"][0]["out_ms"] == 3700


def test_script_rejects_duplicate_speech_and_invalid_effects():
    edit = {
        "clips": [clip(), clip("two")],
        "sound_effects": [{"clip_id": "missing", "prompt": "bang", "duration_ms": 99999}],
    }
    errors = _validate_paper_edit(edit, index())
    assert any("duplicate" in e for e in errors)
    assert any("sound effect" in e for e in errors)


def test_project_preferences_do_not_leak(project, monkeypatch):
    from kinetograph.core.editing_options import EditingOptions, load_options, save_options

    save_options(EditingOptions(editing_mode="highlights", caption_style_id="none"))
    monkeypatch.setattr(settings, "kinetograph_project_dir", str(project / "second"))
    assert load_options().editing_mode == "narration"
    assert load_options().caption_style_id == "bold-yellow"
    monkeypatch.setattr(settings, "kinetograph_project_dir", str(project))
    assert load_options().editing_mode == "highlights"
    assert load_options().caption_style_id == "none"


@pytest.mark.asyncio
async def test_generation_cache_and_provider_payload(project, monkeypatch):
    from kinetograph.core import generated_audio as audio

    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    monkeypatch.setattr(
        audio, "probe_media_async", AsyncMock(return_value={"has_audio": True, "duration_ms": 4000})
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"mock-audio")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await audio.generate_audio("gentle score", 4000, client=client)
        assert await audio.generate_audio("gentle score", 4000, client=client) == first
        await audio.generate_audio("soft whoosh", 1000, kind="effect", client=client)
    assert len(requests) == 2
    assert json.loads(requests[0].content)["force_instrumental"] is True
    assert json.loads(requests[0].content)["music_length_ms"] == 4000
    assert json.loads(requests[1].content)["duration_seconds"] == 1
    assert requests[1].url.path == "/v1/sound-generation"


@pytest.mark.asyncio
async def test_provider_failure_is_not_cached_or_retried(project, monkeypatch):
    from kinetograph.core.generated_audio import generate_audio

    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await generate_audio("score", 4000, client=client)
    assert count == 1
    assert not list(project.rglob("*.mp3"))


@pytest.mark.asyncio
async def test_caption_apply_busy_does_not_change_preference(project, monkeypatch):
    from fastapi import HTTPException
    from kinetograph import server
    from kinetograph.core.editing_options import load_options

    monkeypatch.setattr(server, "sessions", server.SessionManager())
    session = server.sessions.create()
    session._running_task = asyncio.create_task(asyncio.sleep(60))
    try:
        with pytest.raises(HTTPException) as error:
            await server.select_caption_style(
                server.CaptionStyleRequest(style_id="none", apply=True)
            )
        assert error.value.status_code == 409
        assert load_options().caption_style_id == "bold-yellow"
    finally:
        session._running_task.cancel()
        await asyncio.gather(session._running_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_caption_only_graph_skips_audio():
    from kinetograph.orchestrator import build_graph

    graph = build_graph("captioner")
    route = next(iter(graph.branches["captioner"].values())).path
    assert route.invoke({"phase": "rendered"}) == "export"
    assert route.invoke({"phase": "error"}) == "error_handler"


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
async def test_real_caption_switch_keeps_clean_picture_and_identical_audio(project):
    from kinetograph.agents.captioner import captioner_node
    from kinetograph.agents.sound_engineer import _mix_music, _mix_effect

    source = project / "source.mp4"
    music = project / "music.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:r=30:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=1",
            str(music),
        ],
        check=True,
        capture_output=True,
    )
    master = project / "master.mp4"
    await _mix_music(str(source), str(music), str(master), [(0.2, 2.5)])
    effect_master = project / "effect.mp4"
    await _mix_effect(str(master), str(music), str(effect_master), 1000, 700)
    state = {
        "caption_source_path": str(effect_master),
        "approved_edit": {"clips": [clip()]},
        "master_index": index(),
        "run_id": "style-a",
        "caption_style": CAPTION_STYLE_PRESETS["bold-yellow"],
    }
    first = await captioner_node(state)
    assert first["phase"] == "rendered", first
    second = await captioner_node(
        {
            **state,
            **first,
            "run_id": "style-b",
            "caption_style": CAPTION_STYLE_PRESETS["clean-white"],
        }
    )
    assert second["phase"] == "rendered", second

    def audio_hash(path):
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a",
                "-c",
                "copy",
                "-f",
                "adts",
                "-",
            ],
            check=True,
            capture_output=True,
        )
        return hashlib.sha256(result.stdout).hexdigest()

    assert audio_hash(first["render_path"]) == audio_hash(effect_master)
    assert audio_hash(second["render_path"]) == audio_hash(effect_master)
    assert "PlayResX: 320" in Path(second["caption_path"]).read_text()
    removed = await captioner_node({**state, **second, "caption_style": {"id": "none"}})
    assert removed["render_path"] == str(effect_master)
    assert removed["caption_path"] is None
    assert source.is_file() and effect_master.is_file()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
async def test_director_records_skip_cuts_and_transition_overlap(project):
    from kinetograph.agents.director import director_node
    from kinetograph.core.media import probe_media_async

    source = project / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:r=30:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    clips = [clip("intro", 0, 500, "cutaway"), clip(), clip("end", 0, 100, "primary")]
    for c in clips:
        c["source_file"] = str(source)
    clips[1]["transition"] = "crossfade"
    clips[1]["transition_duration_ms"] = 200
    clips[2]["transition"] = "crossfade"
    clips[2]["transition_duration_ms"] = 500
    entries = index()
    entries[0]["asset_file"] = str(source)
    entries[0]["skip_regions"] = [[1000, 2000]]
    result = await director_node(
        {
            "approved_edit": {"title": "test", "clips": clips},
            "master_index": entries,
            "run_id": "picture",
            "render_settings": {"width": 320, "height": 240},
        }
    )
    assert result["phase"] == "rendered", result
    # .5 intro + 3s clean primary + .1 outro - .2 overlap - .05 short overlap
    assert result["approved_edit"]["total_duration_ms"] == 3350
    words = map_words_to_timeline(result["approved_edit"], entries)
    assert [(w["text"], w["start_ms"]) for w in words] == [("hello", 500), ("world", 1500)]
    metadata = await probe_media_async(result["render_path"])
    assert abs(metadata["duration_ms"] - 3350) < 100
    assert result["picture_path"] == result["render_path"]
