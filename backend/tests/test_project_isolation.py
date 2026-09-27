"""Project switching must not import CRDT deletion history from other projects."""

import asyncio
import os

from kinetograph import crdt
from kinetograph.server import _metadata_cache_key


def test_reopening_project_restores_saved_timeline(tmp_path):
    old_doc, old_path = crdt.doc, crdt._snapshot_path
    try:
        crdt.clear_doc()
        crdt.set_snapshot_path(tmp_path / 'a.yjs')
        crdt.load_paper_edit({'title': 'Project A', 'clips': [
            {'clip_id': 'a', 'source_file': 'a.mp4', 'in_ms': 0, 'out_ms': 1000},
        ]})
        crdt.save_snapshot()
        crdt.clear_doc()
        crdt.set_snapshot_path(tmp_path / 'b.yjs')
        crdt.load_paper_edit({'title': 'Project B', 'clips': []})
        crdt.save_snapshot()
        crdt.clear_doc()
        crdt.set_snapshot_path(tmp_path / 'a.yjs')
        assert crdt.load_snapshot()
        edit = crdt.paper_edit_from_doc()
        assert edit['title'] == 'Project A'
        assert [clip['clip_id'] for clip in edit['clips']] == ['a']
    finally:
        crdt.clear_doc()
        crdt._snapshot_path = old_path
        crdt.doc.apply_update(old_doc.get_update())


def test_deferred_old_project_update_is_not_broadcast(monkeypatch):
    async def scenario():
        previous = crdt.doc
        crdt.clear_doc()
        sent = []
        async def broadcast(update):
            sent.append(update)
        monkeypatch.setattr(crdt, 'broadcast_update', broadcast)
        await crdt._deferred_save_and_broadcast(b'old', source=previous)
        assert sent == []
    asyncio.run(scenario())


def test_metadata_cache_distinguishes_same_named_files(tmp_path):
    first, second = tmp_path / 'a' / 'clip.mp4', tmp_path / 'b' / 'clip.mp4'
    for file in (first, second):
        file.parent.mkdir()
        file.write_bytes(b'same size')
        os.utime(file, ns=(1000000000, 1000000000))
    assert _metadata_cache_key(first) != _metadata_cache_key(second)
