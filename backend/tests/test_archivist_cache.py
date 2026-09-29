"""Incremental indexing and cancellation, with all provider calls mocked."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kinetograph.agents import archivist as A
from kinetograph.config import settings
from kinetograph.core.analysis_cache import AnalysisCache


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kinetograph_project_dir", str(tmp_path))
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fixture")
    assets = [
        {
            "file_path": str(source),
            "file_name": source.name,
            "has_audio": True,
            "is_image": False,
            "duration_ms": 8000,
        }
    ]
    segments = [
        {"start_ms": n, "end_ms": n + 4000, "frame_paths": ["frame.jpg"]} for n in (0, 4000)
    ]
    monkeypatch.setattr(A, "_discover_media_files", lambda: assets)
    monkeypatch.setattr(A, "extract_audio_async", AsyncMock(return_value=tmp_path / "audio.wav"))
    monkeypatch.setattr(A, "extract_video_segments_async", AsyncMock(return_value=segments))
    monkeypatch.setattr(
        A, "_transcribe_audio_async", AsyncMock(return_value={"text": "hello", "words": []})
    )

    async def describe(segment, *args, **kwargs):
        return {
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "description": "A scene",
            "clip_type": "SCENIC",
            "num_frames": 1,
        }

    monkeypatch.setattr(A, "_describe_segment_vlm", AsyncMock(side_effect=describe))
    return assets, source


@pytest.mark.asyncio
async def test_second_run_skips_providers_and_extraction(fixture):
    first = await A.archivist_node({})
    second = await A.archivist_node({})
    assert second["master_index"] == first["master_index"]
    assert second["analysis_stats"]["cache_hits"] == 1
    assert A._transcribe_audio_async.await_count == 1
    assert A.extract_video_segments_async.await_count == 1
    assert A._describe_segment_vlm.await_count == 2


@pytest.mark.asyncio
async def test_new_or_changed_video_only_reanalyzes_that_video(fixture):
    assets, source = fixture
    await A.archivist_node({})
    other = source.parent / "other.mp4"
    other.write_bytes(b"new")
    assets.append({**assets[0], "file_path": str(other), "file_name": other.name})
    result = await A.archivist_node({})
    assert result["analysis_stats"]["cache_hits"] == 1
    assert A._transcribe_audio_async.await_count == 2
    source.write_bytes(b"changed footage")
    result = await A.archivist_node({})
    assert result["analysis_stats"]["cache_hits"] == 1
    assert A._transcribe_audio_async.await_count == 3


@pytest.mark.asyncio
async def test_failed_window_is_retried_without_retranscribing(fixture, monkeypatch):
    original = A._describe_segment_vlm.side_effect

    async def partial(segment, *args, **kwargs):
        result = await original(segment)
        return {**result, "analysis_failed": segment["start_ms"] == 4000}

    A._describe_segment_vlm.side_effect = partial
    result = await A.archivist_node({})
    assert result["errors"]
    A._describe_segment_vlm.side_effect = original
    result = await A.archivist_node({})
    assert not result["errors"]
    assert A._transcribe_audio_async.await_count == 1
    assert A._describe_segment_vlm.await_count == 3


@pytest.mark.asyncio
async def test_stop_drains_children_and_keeps_completed_windows(fixture):
    _, source = fixture
    started = asyncio.Event()
    cancelled = asyncio.Event()
    original = A._describe_segment_vlm.side_effect

    async def slow(segment, *args, **kwargs):
        if segment["start_ms"] == 0:
            return await original(segment)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    A._describe_segment_vlm.side_effect = slow
    task = asyncio.create_task(A.archivist_node({}))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    cache = AnalysisCache(str(source))
    assert len(cache.data["visuals"]) == 1
    assert "transcript" in cache.data
    A._describe_segment_vlm.side_effect = original
    await A.archivist_node({})
    assert A._transcribe_audio_async.await_count == 1
    assert A._describe_segment_vlm.await_count == 3


@pytest.mark.asyncio
async def test_same_named_files_have_independent_cache_and_scratch_paths(fixture):
    assets, source = fixture
    second = source.parent / "second" / source.name
    second.parent.mkdir()
    second.write_bytes(b"other content")
    assets.append({**assets[0], "file_path": str(second)})
    result = await A.archivist_node({})
    assert result["analysis_stats"]["analyzed"] == 2
    paths = [call.args[1] for call in A.extract_audio_async.call_args_list]
    assert len(set(paths)) == 2


def test_model_change_and_corrupt_cache_invalidate(fixture, monkeypatch):
    _, source = fixture
    cache = AnalysisCache(str(source))
    cache.save()
    monkeypatch.setattr(settings, "vlm_model", "different-model")
    assert AnalysisCache(str(source)).key != cache.key
    cache.path.write_text("broken json")
    assert not AnalysisCache(str(source)).data["complete"]


@pytest.mark.asyncio
async def test_assets_overlap_but_stay_within_limit(fixture, monkeypatch):
    assets, source = fixture
    for n in range(3):
        other = source.parent / f"other-{n}.mp4"
        other.write_bytes(b"media")
        assets.append({**assets[0], "file_path": str(other), "file_name": other.name})
    monkeypatch.setattr(settings, "archivist_asset_concurrency", 2)
    active = peak = 0
    both_started = asyncio.Event()

    async def transcribe(*args):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 2)
        await asyncio.sleep(0.01)
        active -= 1
        return {"text": "", "words": []}

    A._transcribe_audio_async.side_effect = transcribe
    await A.archivist_node({})
    assert peak == 2


@pytest.mark.asyncio
async def test_one_pass_frame_extraction_with_real_ffmpeg(tmp_path):
    import shutil
    import subprocess
    from kinetograph.core.media import extract_video_segments_async

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    source = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=5",
            "-c:v",
            "libx264",
            str(source),
        ],
        capture_output=True,
        check=True,
    )
    segments = await extract_video_segments_async(source, tmp_path / "frames")
    assert [s["start_ms"] for s in segments] == [0, 4000]
    assert segments[-1]["end_ms"] == 5000
    assert len(segments[0]["frame_paths"]) == 8
    assert all(Path(path).is_file() for s in segments for path in s["frame_paths"])
