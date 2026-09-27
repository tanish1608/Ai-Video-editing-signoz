"""
Backend CRDT layer — shared pycrdt document for the Paper Edit.

This module mirrors the frontend's crdt.ts.  A single Y.Doc is the
authoritative copy of the timeline. The FastAPI WebSocket at /ws/crdt
syncs this document with the frontend's Yjs doc via the standard
y-protocols sync protocol.

Schema (matches frontend):
    doc.get("clips", type=Array)   → Array[Map]   (ordered timeline clips)
    doc.get("meta",  type=Map)     → Map           (title, music_prompt, music_path)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from pycrdt import Array, Doc, Map

logger = logging.getLogger(__name__)

# ── Shared document ─────────────────────────────────────────────────────────

doc = Doc()
y_clips: Array = doc.get("clips", type=Array)
y_meta: Map = doc.get("meta", type=Map)
# V2/V3… overlay clips and A2/A3… audio clips (each carries a `trackId`).
# The frontend owns these; the backend just syncs + persists them as part of
# the shared doc so they survive reloads and participate in undo.
y_overlays: Array = doc.get("overlays", type=Array)
y_audio: Array = doc.get("audio", type=Array)


# ── y-protocols constants & lib0 varint helpers ────────────────────────────

# Outer message types (y-websocket)
MSG_SYNC = 0
MSG_AWARENESS = 1
MSG_AUTH = 2
MSG_QUERY_AWARENESS = 3

# Sync sub-types (y-protocols/sync)
SYNC_STEP1 = 0
SYNC_STEP2 = 1
SYNC_UPDATE = 2


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Read a lib0 variable-length unsigned integer from *data* at *offset*."""
    result = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, offset


def read_var_bytes(data: bytes, offset: int) -> tuple[bytes, int]:
    """Read a lib0 varint-length-prefixed byte array."""
    length, offset = read_varint(data, offset)
    end = offset + length
    return data[offset:end], end


def write_varint(value: int) -> bytes:
    """Encode a lib0 variable-length unsigned integer."""
    buf = bytearray()
    while value > 0x7F:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value & 0x7F)
    return bytes(buf)


def write_var_bytes(data: bytes) -> bytes:
    """Encode a lib0 varint-length-prefixed byte array."""
    return write_varint(len(data)) + data


def encode_sync_step2(update: bytes) -> bytes:
    """Wrap a raw CRDT update as a y-protocols sync-step-2 message."""
    return bytes([MSG_SYNC, SYNC_STEP2]) + write_var_bytes(update)


def encode_sync_update(update: bytes) -> bytes:
    """Wrap a raw CRDT update as a y-protocols sync-update message."""
    return bytes([MSG_SYNC, SYNC_UPDATE]) + write_var_bytes(update)


# Sentinel origin tagged onto updates that arrived from a WebSocket client.
# The observer uses it to skip re-broadcasting those updates: the WS handler
# already relays them to the *other* clients (with the originator excluded).
# transaction.origin() returns hash(origin) for hashable origins, stable within
# the process, so we compare against the precomputed hash.
WS_ORIGIN = "ws-relay"
_WS_ORIGIN_ID = hash(WS_ORIGIN)


def apply_ws_update(update: bytes) -> None:
    """Apply an update received from a WebSocket client, tagged as WS-originated."""
    with doc.transaction(origin=WS_ORIGIN):
        doc.apply_update(update)


# ── JSON → CRDT helpers ────────────────────────────────────────────────────


def _clip_to_map(clip: dict) -> Map:
    """Convert a plain dict clip to a pycrdt Map with prelim data."""
    m = Map(
        {
            k: clip[k]
            for k in (
                "clip_id",
                "source_file",
                "in_ms",
                "out_ms",
                "clip_type",
                "description",
                "transition",
                "transition_duration_ms",
                "overlay_text",
                "search_query",
            )
            if clip.get(k) is not None
        }
    )
    return m


def load_paper_edit(pe: dict, *, origin: Any = None) -> None:
    """
    Overwrite the CRDT document with a full Paper Edit snapshot.

    This is used when the AI pipeline produces a new edit, or on project load.
    """
    with doc.transaction(origin=origin):
        # Clear
        if len(y_clips) > 0:
            del y_clips[0 : len(y_clips)]

        # Meta
        y_meta["title"] = pe.get("title", "Untitled Sequence")
        for key in ("music_prompt", "music_path"):
            if pe.get(key) is not None:
                y_meta[key] = pe[key]
            elif key in y_meta:
                # A replacement snapshot must not retain metadata from the
                # previous sequence (for example, an old music asset).
                del y_meta[key]

        # Clips
        for clip in pe.get("clips", []):
            y_clips.append(_clip_to_map(clip))


def paper_edit_from_doc() -> dict | None:
    """
    Derive a PaperEdit JSON snapshot from the current CRDT document.
    Returns None if the document is empty.
    """
    if len(y_clips) == 0 and "title" not in y_meta:
        return None

    clips = []
    for i in range(len(y_clips)):
        m = y_clips[i]
        clip = {}
        for key in (
            "clip_id",
            "source_file",
            "in_ms",
            "out_ms",
            "clip_type",
            "description",
            "transition",
            "transition_duration_ms",
            "overlay_text",
            "search_query",
        ):
            if key in m:
                clip[key] = m[key]
        clips.append(clip)

    total_duration_ms = sum((c.get("out_ms", 0) - c.get("in_ms", 0)) for c in clips)

    result: dict[str, Any] = {
        "title": y_meta.get("title", "Untitled Sequence"),
        "total_duration_ms": total_duration_ms,
        "clips": clips,
    }
    if "music_prompt" in y_meta:
        result["music_prompt"] = y_meta["music_prompt"]
    if "music_path" in y_meta:
        result["music_path"] = y_meta["music_path"]
    return result


# ── Document Reset ──────────────────────────────────────────────────────────


def clear_doc() -> None:
    """Start an independent document, discarding the outgoing project's history."""
    global doc, y_clips, y_meta, y_overlays, y_audio, _save_pending
    doc = Doc()
    y_clips = doc.get("clips", type=Array)
    y_meta = doc.get("meta", type=Map)
    y_overlays = doc.get("overlays", type=Array)
    y_audio = doc.get("audio", type=Array)
    _save_pending = False
    doc.observe(_on_doc_update)


async def disconnect_clients() -> None:
    """Detach old-project clients before replacing the shared document."""
    async with _crdt_lock:
        clients = list(_crdt_clients)
        _crdt_clients.clear()
    for client in clients:
        try:
            await client.close(code=4001, reason="Project changed")
        except Exception:
            logger.debug("Project client already disconnected", exc_info=True)


# ── Persistence ─────────────────────────────────────────────────────────────

_snapshot_path: Path | None = None


def set_snapshot_path(path: Path) -> None:
    """Set the path where CRDT snapshots are persisted."""
    global _snapshot_path
    _snapshot_path = path
    path.parent.mkdir(parents=True, exist_ok=True)


def save_snapshot() -> None:
    """Persist the current CRDT state to disk (binary Yjs update format)."""
    if _snapshot_path is None:
        return
    try:
        update = doc.get_update()
        temporary = _snapshot_path.with_suffix(".tmp")
        temporary.write_bytes(update)
        temporary.replace(_snapshot_path)
    except Exception:
        logger.warning("Failed to save CRDT snapshot", exc_info=True)


def load_snapshot() -> bool:
    """Load a previously persisted CRDT snapshot.  Returns True if loaded."""
    if _snapshot_path is None or not _snapshot_path.exists():
        return False
    try:
        data = _snapshot_path.read_bytes()
        doc.apply_update(data)
        logger.info("Loaded CRDT snapshot from %s (%d bytes)", _snapshot_path, len(data))
        return True
    except Exception:
        logger.warning("Failed to load CRDT snapshot", exc_info=True)
        return False


# ── WebSocket sync protocol ────────────────────────────────────────────────

# Connected WebSocket clients for CRDT sync
_crdt_clients: list[Any] = []  # list of FastAPI WebSocket objects
_crdt_lock = asyncio.Lock()


async def add_client(ws: Any) -> None:
    async with _crdt_lock:
        _crdt_clients.append(ws)


async def remove_client(ws: Any) -> None:
    async with _crdt_lock:
        if ws in _crdt_clients:
            _crdt_clients.remove(ws)


async def broadcast_update(update: bytes, *, exclude: Any = None) -> None:
    """Broadcast a raw CRDT update to all connected clients.

    The update is automatically wrapped in y-protocols sync-update encoding
    before sending.
    """
    msg = encode_sync_update(update)
    await broadcast_raw(msg, exclude=exclude)


async def broadcast_raw(msg: bytes, *, exclude: Any = None) -> None:
    """Broadcast a pre-encoded y-protocols message to all connected clients."""
    disconnected = []
    async with _crdt_lock:
        for client in _crdt_clients:
            if client is exclude:
                continue
            try:
                await client.send_bytes(msg)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            _crdt_clients.remove(client)


_save_pending = False


def _on_doc_update(event) -> None:
    """Called whenever the local pycrdt doc changes (e.g. from pipeline write).
    The observer fires during the transaction, so we can't save/broadcast here.
    Instead, mark as dirty and let the event loop handle it on the next tick.

    We capture the transaction origin *synchronously* here (it is only valid
    during the observer callback) and pass it through, so the deferred broadcast
    decision doesn't depend on a racy module-level flag read a tick later.
    """
    global _save_pending
    _save_pending = True
    update: bytes = event.update
    try:
        origin_id = event.transaction.origin()
    except Exception:
        origin_id = None
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon(
            lambda u=update, o=origin_id, source=doc: asyncio.ensure_future(
                _deferred_save_and_broadcast(u, o, source)
            )
        )
    except RuntimeError:
        # No event loop — will be saved on next explicit save_snapshot() call
        pass


async def _deferred_save_and_broadcast(
    update: bytes, origin_id: Any = None, source: Doc | None = None
) -> None:
    """Runs on the next event loop tick — after the transaction has committed."""
    global _save_pending
    if source is not None and source is not doc:
        return
    if _save_pending:
        _save_pending = False
        save_snapshot()
    # Updates that arrived from a WebSocket client are re-broadcast (with the
    # originator excluded) by the WS handler itself. Server-originated updates
    # (pipeline writes, REST edits, origin=None) go to every connected client.
    if origin_id != _WS_ORIGIN_ID:
        await broadcast_update(update)


# Subscribe to document changes
doc.observe(_on_doc_update)
