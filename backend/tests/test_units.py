"""Unit tests for pure logic — no API keys or media files required.

These cover the risk-carrying helpers surfaced by the audit: the edit
classifier, FFmpeg path escaping, media-duration resolution, path-traversal
guard, error routing, and the CRDT round-trip. Run with:

    cd backend && pytest tests/test_units.py
"""

from __future__ import annotations

import pytest

from kinetograph.core.compositor import escape_ffmpeg_filter_path
from kinetograph.core.media import _resolve_duration_s


# ── FFmpeg path escaping (P1.7) ───────────────────────────────────────────────

def test_escape_plain_path_unchanged():
    assert escape_ffmpeg_filter_path("/media/clip.ass") == "/media/clip.ass"


def test_escape_colon_and_backslash_windows():
    # Windows drive letter: backslashes → forward slashes, colon escaped.
    assert escape_ffmpeg_filter_path("C:\\videos\\a.ass") == "C\\:/videos/a.ass"


def test_escape_single_quote():
    # A quote must be backslash-escaped (the old single-quote wrapping broke).
    assert escape_ffmpeg_filter_path("/x/o'brien.ass") == "/x/o\\'brien.ass"


def test_escape_filtergraph_specials():
    out = escape_ffmpeg_filter_path("/a[b],c;d.ass")
    assert out == "/a\\[b\\]\\,c\\;d.ass"


# ── Media duration resolution (P1.4) ──────────────────────────────────────────

def test_duration_from_format():
    assert _resolve_duration_s({"duration": "12.5"}, None, None, 30.0) == 12.5


def test_duration_falls_back_to_stream_when_format_missing():
    # mkv/webm often omit format.duration — must use the stream's.
    assert _resolve_duration_s({}, {"duration": "8.0"}, None, 30.0) == 8.0


def test_duration_from_nb_frames_over_fps():
    assert _resolve_duration_s({}, {"nb_frames": "300"}, None, 30.0) == 10.0


def test_duration_zero_for_image_like_input():
    assert _resolve_duration_s({}, None, None, 0.0) == 0.0


# ── Edit classifier (server _classify_edit) ───────────────────────────────────

def test_classify_edit_routes_by_keyword():
    from kinetograph.server import _classify_edit
    assert _classify_edit("make the background music louder") == "sound_engineer"
    assert _classify_edit("change the caption font") == "captioner"
    assert _classify_edit("increase the brightness and re-render") == "director"


def test_classify_edit_defaults_to_scripter():
    from kinetograph.server import _classify_edit
    assert _classify_edit("make it more dramatic and cut the boring parts") == "scripter"


def test_classify_edit_word_boundary_avoids_false_substring():
    from kinetograph.server import _classify_edit
    # "context" must not match the "text" keyword; "upgrade" must not match "grade".
    assert _classify_edit("add more context about the founding story") == "scripter"
    assert _classify_edit("upgrade the intro to be punchier") == "scripter"


def test_classify_edit_scoring_prefers_stronger_signal():
    from kinetograph.server import _classify_edit
    # Two caption hits ("caption", "text") beat one audio hit ("louder").
    assert _classify_edit("make the caption text louder") == "captioner"


# ── Path-traversal guard (P0.2) ───────────────────────────────────────────────

def test_safe_within_blocks_traversal(tmp_path):
    from kinetograph.server import _safe_within
    root = tmp_path / "output"
    root.mkdir()
    assert _safe_within(root, root / "render.mp4") is True
    assert _safe_within(root, root / ".." / ".." / "etc" / "passwd") is False


# ── Error routing (P0.1) ──────────────────────────────────────────────────────

def test_route_on_error_directs_error_phase_to_handler():
    from kinetograph.orchestrator import _route_on_error
    router = _route_on_error("captioner")
    assert router({"phase": "error"}) == "error_handler"
    assert router({"phase": "rendered"}) == "captioner"


def test_is_error_phase_accepts_enum_and_string():
    from kinetograph.orchestrator import _is_error_phase
    from kinetograph.state import Phase
    assert _is_error_phase({"phase": Phase.ERROR}) is True
    assert _is_error_phase({"phase": "error"}) is True
    assert _is_error_phase({"phase": Phase.RENDERED}) is False


# ── CRDT round-trip (P0.7/P0.8 support) ───────────────────────────────────────

def test_crdt_load_and_read_roundtrip():
    from kinetograph import crdt
    crdt.clear_doc()
    pe = {
        "title": "Test Sequence",
        "clips": [
            {
                "clip_id": "c1",
                "source_file": "/m/a.mp4",
                "in_ms": 0,
                "out_ms": 2000,
                "clip_type": "a-roll",
                "description": "opening",
            },
            {
                "clip_id": "c2",
                "source_file": "/m/b.mp4",
                "in_ms": 500,
                "out_ms": 3500,
                "clip_type": "b-roll",
                "description": "cutaway",
            },
        ],
        "music_prompt": "ambient",
    }
    crdt.load_paper_edit(pe)
    out = crdt.paper_edit_from_doc()
    assert out is not None
    assert out["title"] == "Test Sequence"
    assert [c["clip_id"] for c in out["clips"]] == ["c1", "c2"]
    assert out["total_duration_ms"] == (2000 - 0) + (3500 - 500)
    assert out["music_prompt"] == "ambient"
    crdt.clear_doc()


def test_crdt_replacement_removes_stale_music_metadata():
    from kinetograph import crdt

    crdt.clear_doc()
    crdt.load_paper_edit({
        "title": "First",
        "clips": [],
        "music_prompt": "ambient",
        "music_path": "/music/old.mp3",
    })
    crdt.load_paper_edit({"title": "Second", "clips": []})
    out = crdt.paper_edit_from_doc()
    assert out is not None
    assert "music_prompt" not in out
    assert "music_path" not in out
    crdt.clear_doc()


def test_session_manager_rejects_new_run_while_active_task_is_running():
    import asyncio

    from kinetograph.server import SessionManager

    async def scenario():
        manager = SessionManager()
        first = manager.create("one")
        first._running_task = asyncio.create_task(asyncio.sleep(1))
        with pytest.raises(RuntimeError, match="already running"):
            manager.create("two")
        first._running_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first._running_task

    asyncio.run(scenario())


def test_persisted_session_restores_with_project_checkpointer(tmp_path, monkeypatch):
    import asyncio

    from kinetograph.config import settings
    from kinetograph import server

    async def _run():
        monkeypatch.setattr(settings, "kinetograph_project_dir", str(tmp_path))
        await server._close_project_checkpointer()
        server.sessions.reset_active()
        original = server.PipelineSession(
            thread_id="persisted-run",
            pipeline_state={"phase": "awaiting_approval", "project_name": "Test"},
            config={"configurable": {"thread_id": "persisted-run"}},
            graph_start="archivist",
        )
        server._persist_session(original)
        # The checkpointer is now async (AsyncSqliteSaver over aiosqlite), so the
        # aiosqlite connection must be created within a running event loop.
        await server._restore_persisted_session()
        restored = server.sessions.active
        assert restored is not None
        assert restored.thread_id == "persisted-run"
        assert restored.pipeline_state["phase"] == "awaiting_approval"
        assert restored.graph is not None
        await server._close_project_checkpointer()
        server.sessions.reset_active()

    asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
