"""Reference imports work without symlinks, and uploads never replace originals."""

import asyncio
import io
import json

import pytest
from fastapi import HTTPException, UploadFile

from kinetograph.config import settings


def test_discovery_reads_references_without_symlinks(tmp_path, monkeypatch):
    from kinetograph.agents import archivist
    monkeypatch.setattr(settings, 'kinetograph_project_dir', str(tmp_path / 'project'))
    source = tmp_path / 'external.mp4'
    source.write_bytes(b'test')
    settings.state_dir.mkdir(parents=True)
    settings.media_refs_path.write_text(json.dumps({'external': str(source)}))
    monkeypatch.setattr(archivist, 'probe_media', lambda _: {
        'duration_ms': 1000, 'width': 320, 'height': 240, 'fps': 30, 'has_audio': False,
    })
    assets = archivist._discover_media_files()
    assert len(assets) == 1
    assert assets[0]['file_path'] == str(source)


def test_duplicate_normalization_failure_has_no_phantom_output(tmp_path, monkeypatch):
    from kinetograph.agents import director
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'test')
    def fail(*args):
        raise RuntimeError('Encoding failed')
    monkeypatch.setattr(director, '_normalize_one_clip', fail)
    edit = {'clips': [{'clip_id': name, 'source_file': str(source)} for name in ['a', 'b']]}
    assert director._normalize_all_clips(edit, [], tmp_path) == {}


def test_duplicate_upload_preserves_existing_file(tmp_path, monkeypatch):
    from kinetograph.server import upload_asset
    monkeypatch.setattr(settings, 'kinetograph_project_dir', str(tmp_path))
    settings.media_dir.mkdir()
    original = settings.media_dir / 'clip.mp4'
    original.write_bytes(b'original')
    upload = UploadFile(filename='clip.mp4', file=io.BytesIO(b'replacement'))
    with pytest.raises(HTTPException) as error:
        asyncio.run(upload_asset(upload))
    assert error.value.status_code == 409
    assert original.read_bytes() == b'original'
