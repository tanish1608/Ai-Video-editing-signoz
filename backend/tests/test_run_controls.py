"""Saved credentials, redacted diagnostics and actual task cancellation."""

import asyncio
import json
import logging

import pytest

from kinetograph.config import Settings, settings
from kinetograph.runlog import RunLog


def test_saved_key_reload_beats_stale_inherited_key(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=saved-test-key\n")
    monkeypatch.setenv("GEMINI_API_KEY", "old-inherited-test-key")
    monkeypatch.setitem(Settings.model_config, "env_file", str(env))
    local = Settings(kinetograph_project_dir=str(tmp_path), output_fps=24)
    local.reload_secrets()
    assert local.gemini_api_key == "saved-test-key"
    assert local.output_fps == 24
    assert local.kinetograph_project_dir == str(tmp_path)
    env.write_text("GEMINI_API_KEY=replaced-test-key\n")
    local.reload_secrets()
    assert local.gemini_api_key == "replaced-test-key"


def test_logs_redact_json_events_and_tracebacks(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kinetograph_project_dir", str(tmp_path))
    monkeypatch.setattr(settings, "gemini_api_key", "private-test-secret")
    log = RunLog("test-redaction")
    log.start(prompt="Prompt", test="private-test-secret")
    with log.capture():
        logging.getLogger("kinetograph.test").error("Failure with private-test-secret")
    log.node_done("scripter", "error", [{"message": "private-test-secret"}])
    log.finish("error", error="private-test-secret")
    for path in log.dir.iterdir():
        assert "private-test-secret" not in path.read_text()
    assert json.loads((log.dir / "run.json").read_text())["api_keys"]["gemini"] is True


@pytest.mark.asyncio
async def test_stop_cancels_running_work_and_records_run(tmp_path, monkeypatch):
    from kinetograph import server

    monkeypatch.setattr(settings, "kinetograph_project_dir", str(tmp_path))
    monkeypatch.setattr(server, "sessions", server.SessionManager())
    messages = []

    async def broadcast(message):
        messages.append(message)

    monkeypatch.setattr(server, "_broadcast", broadcast)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Graph:
        async def astream(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            yield {}

    session = server.sessions.create()
    session.graph = Graph()
    session.pipeline_state = {"phase": "ingesting", "run_id": "stop-test"}
    session._running_task = asyncio.create_task(server._stream_pipeline(session, {}, "archivist"))
    await asyncio.wait_for(started.wait(), 2)
    result = await server.stop_pipeline()
    assert result["status"] == "stopped"
    assert cancelled.is_set()
    assert session.pipeline_state["phase"] == "idle"
    assert any(message["type"] == "pipeline_stopped" for message in messages)
    record = next((tmp_path / "logs/runs").glob("*/run.json"))
    assert json.loads(record.read_text())["status"] == "cancelled"
    assert (await server.stop_pipeline())["status"] == "not_running"


@pytest.mark.asyncio
async def test_stop_reaps_encoder_running_in_worker_thread(tmp_path):
    import os
    import sys
    from threading import Event
    from kinetograph.core.media import _run_normalization

    marker = tmp_path / "child.pid"
    signal = Event()
    script = "import os,pathlib,time; pathlib.Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    task = asyncio.create_task(
        asyncio.to_thread(
            _run_normalization,
            [sys.executable, "-c", script, str(marker)],
            70,
            signal,
        )
    )

    async def started():
        while not marker.exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(started(), 3)
        pid = int(marker.read_text())
        signal.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            await asyncio.wait_for(task, 3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        signal.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reading_settings_cannot_change_models_during_analysis(monkeypatch):
    from unittest.mock import Mock
    from kinetograph import server

    monkeypatch.setattr(server, "sessions", server.SessionManager())
    session = server.sessions.create()
    session._running_task = Mock(done=lambda: False)
    reload = Mock()
    monkeypatch.setattr(type(settings), "reload_secrets", reload)
    await server.get_config()
    reload.assert_not_called()
    session._running_task = None
    await server.get_config()
    reload.assert_called_once()
