"""A tiny real FFmpeg render catches filter-graph and normalization regressions."""

import asyncio
import shutil
import subprocess

import pytest

from kinetograph.config import settings
from kinetograph.state import Phase


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_director_renders_synthetic_clip_without_blocking_loop(tmp_path, monkeypatch):
    from kinetograph.agents.director import director_node
    from kinetograph.core.media import probe_media
    monkeypatch.setattr(settings, 'kinetograph_project_dir', str(tmp_path))
    source = tmp_path / 'input.mp4'
    subprocess.run([
        'ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:r=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source),
    ], check=True, timeout=30)
    state = {
        'approved_edit': {'title': 'Smoke render', 'clips': [{
            'clip_id': 'one', 'source_file': str(source), 'in_ms': 0, 'out_ms': 2000,
            'clip_type': 'primary', 'description': 'Synthetic test',
        }]},
        'render_settings': {'width': 320, 'height': 240, 'crf': 28},
    }
    async def scenario():
        ticks = 0
        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)
        timer = asyncio.create_task(heartbeat())
        try:
            result = await director_node(state)
        finally:
            timer.cancel()
        assert ticks > 1
        return result
    result = asyncio.run(scenario())
    assert result['phase'] == Phase.RENDERED, result.get('errors')
    metadata = probe_media(result['render_path'])
    assert (metadata['width'], metadata['height']) == (320, 240)
    assert metadata['duration_ms'] >= 1000
    assert metadata['has_audio']
