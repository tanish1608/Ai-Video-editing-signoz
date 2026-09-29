"""
FastAPI server — headless backend API for the Kinetograph video orchestration engine.

Exposes REST + WebSocket endpoints for a separate TypeScript frontend
to control the pipeline, browse assets, edit timelines, and stream status.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import subprocess
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from kinetograph import __version__, runlog
from kinetograph import crdt as crdt_mod
from kinetograph.config import settings
from kinetograph.core.media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, probe_media
from kinetograph.security import LocalAccessMiddleware
from kinetograph.state import Phase

_checkpoint_connection = None  # aiosqlite.Connection | None
_checkpoint_project: Path | None = None


async def _project_checkpointer():
    """Return the durable async LangGraph checkpointer for the active project.

    The server drives the graph with ``astream`` (async), so the checkpointer
    MUST be async-capable — a plain ``SqliteSaver`` raises NotImplementedError on
    ``aget_tuple``. We cache one long-lived ``aiosqlite`` connection per project
    so the human-review interrupt/resume flow persists across requests.
    """
    global _checkpoint_connection, _checkpoint_project
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    project = settings.state_dir.resolve()
    if _checkpoint_connection is not None and _checkpoint_project == project:
        return AsyncSqliteSaver(_checkpoint_connection)

    await _close_project_checkpointer()
    project.mkdir(parents=True, exist_ok=True)
    _checkpoint_connection = await aiosqlite.connect(
        str(project / "pipeline_checkpoints.sqlite"), check_same_thread=False
    )
    _checkpoint_project = project
    return AsyncSqliteSaver(_checkpoint_connection)


async def _close_project_checkpointer() -> None:
    global _checkpoint_connection, _checkpoint_project
    if _checkpoint_connection is not None:
        try:
            await _checkpoint_connection.close()
        except Exception:
            pass
    _checkpoint_connection = None
    _checkpoint_project = None


def _phase_val(phase) -> str:
    """Extract the plain string value from a Phase enum (or pass through strings).

    Python 3.11+ changed ``str(SomeStrEnum.MEMBER)`` to return
    ``'ClassName.MEMBER'`` instead of the *value*.  This helper always
    returns the underlying value string (e.g. ``'complete'``).
    """
    if isinstance(phase, Phase):
        return phase.value
    if hasattr(phase, "value"):
        return str(phase.value)
    return str(phase) if phase else ""


def _safe_update(update) -> dict:
    """Safely coerce a LangGraph stream update into a plain dict.

    LangGraph node outputs are *usually* dicts but can occasionally be
    tuples, ReducerValues, or other wrapper types.  This ensures we
    always hand a plain ``dict`` to ``session.pipeline_state.update()``.
    """
    if isinstance(update, dict):
        return update
    if hasattr(update, "items"):  # dict-like
        return dict(update)
    return {}


def _merge_state(target: dict, update) -> None:
    """Merge a LangGraph node update into the running pipeline state.

    List-valued keys (like ``errors``, ``raw_assets``, ``synth_assets``)
    use *extend* semantics to match the ``operator.add`` reducer that
    the ``GraphState`` TypedDict declares.
    """
    safe = _safe_update(update)
    _LIST_KEYS = {"raw_assets", "synth_assets", "errors", "render_history", "completed_agents"}
    for key, value in safe.items():
        if key in _LIST_KEYS and isinstance(value, list):
            existing = target.get(key)
            if isinstance(existing, list):
                existing.extend(value)
                continue
        target[key] = value


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle (replaces the deprecated on_event hooks).

    Startup: ensure cache dirs, load media refs + asset index, wire up and load
    the CRDT snapshot. Shutdown: cancel any in-flight pipeline task and flush the
    CRDT snapshot so unsaved timeline edits survive a restart.
    """
    # ── Startup ──
    from kinetograph.observability import init_telemetry

    init_telemetry()

    for d in (
        settings.cache_dir,
        settings.thumbnail_cache_dir,
        settings.waveform_cache_dir,
        settings.metadata_cache_dir,
        settings.conformed_audio_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    _load_media_refs()
    _rebuild_asset_index()

    crdt_mod.set_snapshot_path(settings.state_dir / "crdt_snapshot.yjs")
    crdt_mod.load_snapshot()
    await _restore_persisted_session()

    yield

    # ── Shutdown ──
    try:
        active = sessions.active
        if active and active._running_task and not active._running_task.done():
            active._running_task.cancel()
    except Exception:
        logger.warning("Shutdown: failed to cancel running pipeline task", exc_info=True)
    try:
        crdt_mod.save_snapshot()
    except Exception:
        logger.warning("Shutdown: failed to flush CRDT snapshot", exc_info=True)
    await _close_project_checkpointer()


app = FastAPI(
    title="Kinetograph API",
    version=__version__,
    description="Autonomous Multi-Agent Video Orchestration Engine — Backend API",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — restricted to the local renderer origins. The backend is bound to
# loopback and uses no cookies/credentials, so we allow the Vite dev server and
# the Electron production renderer (file:// → Origin "null") explicitly rather
# than the wildcard + credentials combo (which invites DNS-rebinding).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "null",  # Electron file:// renderer in production
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "X-Pipeline-Phase"],
)

app.add_middleware(LocalAccessMiddleware)

# ═══════════════════════════════════════════════════════════════════════════════
#  SESSION MANAGEMENT  (multi-tenant — each pipeline run gets its own session)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class PipelineSession:
    """State for a single pipeline run."""

    thread_id: str
    pipeline_state: dict
    graph: object = None  # compiled LangGraph
    config: dict = field(default_factory=dict)
    graph_start: str = "archivist"
    websockets: list[WebSocket] = field(default_factory=list)
    _running_task: asyncio.Task | None = field(default=None)
    # Caption style gate — pipeline waits here until user picks a style


class SessionManager:
    """Owns the single active project session.

    The backend has mutable project-wide settings, caches, CRDT state, and
    FFmpeg scratch paths.  Allowing several sessions to mutate those objects
    concurrently is not safe, so this deliberately enforces one run at a time.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, PipelineSession] = {}
        self._active_id: str | None = None  # the most recent session (default)
        self._global_websockets: list[WebSocket] = []

    def create(self, thread_id: str | None = None) -> PipelineSession:
        active = self.active
        if active and active._running_task and not active._running_task.done():
            raise RuntimeError("A pipeline job is already running")
        tid = thread_id or str(uuid.uuid4())
        session = PipelineSession(
            thread_id=tid,
            pipeline_state={"phase": Phase.IDLE},
            config={"configurable": {"thread_id": tid}},
        )
        self._sessions[tid] = session
        self._active_id = tid
        return session

    def get(self, thread_id: str | None = None) -> PipelineSession | None:
        if thread_id:
            return self._sessions.get(thread_id)
        if self._active_id:
            return self._sessions.get(self._active_id)
        return None

    @property
    def active(self) -> PipelineSession | None:
        return self.get()

    def add_global_ws(self, ws: WebSocket) -> None:
        self._global_websockets.append(ws)

    def remove_global_ws(self, ws: WebSocket) -> None:
        if ws in self._global_websockets:
            self._global_websockets.remove(ws)

    def reset_active(self) -> None:
        """Clear the active session (e.g. on project switch)."""
        self._sessions.clear()
        self._active_id = None

    def restore(self, session: PipelineSession) -> None:
        """Make a restored, persisted session the active project session."""
        self._sessions = {session.thread_id: session}
        self._active_id = session.thread_id

    @property
    def all_websockets(self) -> list[WebSocket]:
        """All global + session-specific websockets."""
        ws = list(self._global_websockets)
        for s in self._sessions.values():
            ws.extend(s.websockets)
        return ws


sessions = SessionManager()


def _session_record_path() -> Path:
    return settings.state_dir / "active_session.json"


def _persist_session(session: PipelineSession) -> None:
    """Persist enough context to reconnect a durable graph checkpoint on boot."""
    record = {
        "thread_id": session.thread_id,
        "pipeline_state": session.pipeline_state,
        "config": session.config,
        "graph_start": session.graph_start,
    }
    path = _session_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w") as f:
        json.dump(record, f, default=_json_default)
    temporary.replace(path)


async def _restore_persisted_session() -> None:
    """Restore the active project session after a backend restart, if present."""
    path = _session_record_path()
    if not path.exists():
        return
    try:
        with open(path) as f:
            record = json.load(f)
        thread_id = record["thread_id"]
        graph_start = record.get("graph_start", "archivist")
        from kinetograph.orchestrator import compile_graph

        session = PipelineSession(
            thread_id=thread_id,
            pipeline_state=record.get("pipeline_state", {"phase": Phase.IDLE}),
            graph=compile_graph(start_from=graph_start, checkpointer=await _project_checkpointer()),
            config=record.get("config", {"configurable": {"thread_id": thread_id}}),
            graph_start=graph_start,
        )
        sessions.restore(session)
        logger.info("Restored persisted pipeline session %s", thread_id)
    except Exception:
        logger.warning("Failed to restore persisted pipeline session", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════


from kinetograph.core.editing_options import (  # noqa: E402
    EditingOptions,
    caption_style,
    load_options,
    pipeline_options,
    save_options,
)


@app.get("/api/project/editing-options", tags=["Project"])
async def get_editing_options():
    return load_options().model_dump()


@app.put("/api/project/editing-options", tags=["Project"])
async def update_editing_options(request: EditingOptions):
    active = sessions.active
    if active and active._running_task and not active._running_task.done():
        raise HTTPException(409, "Wait for the current edit to finish before changing options.")
    try:
        caption_style(request.caption_style_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    save_options(request)
    return request.model_dump()


_pipeline_start_lock = asyncio.Lock()


def _serialize_start(function):
    """Reserve startup while checkpoint I/O yields, before a running task exists."""
    from functools import wraps

    @wraps(function)
    async def wrapped(*args, **kwargs):
        if _pipeline_start_lock.locked():
            raise HTTPException(409, "A pipeline job is starting. Wait for it to finish.")
        async with _pipeline_start_lock:
            return await function(*args, **kwargs)

    return wrapped


def _require_gemini_key() -> None:
    """Fail fast (before any work) when the Scripter/Critic would have no Gemini key."""
    settings.reload_secrets()
    if not settings.gemini_api_key:
        raise HTTPException(
            412,
            "Gemini API key is not configured. Open Settings → API Keys, paste your "
            "Gemini key and click Save, then try again. "
            f"(The engine reads keys from {settings.model_config.get('env_file')})",
        )


class RunRequest(BaseModel):
    prompt: str = Field(
        ..., min_length=1, max_length=10_000, description="Natural language creative brief"
    )
    project_name: str = Field(
        "untitled", min_length=1, max_length=160, description="Project name for output files"
    )


class ApprovalRequest(BaseModel):
    action: Literal["approve", "reject"] = Field(..., description="'approve' or 'reject'")
    paper_edit: Optional[dict] = Field(
        None, description="Modified Paper Edit (if user made changes)"
    )
    reason: Optional[str] = Field(None, description="Rejection reason (if action='reject')")


# NOTE: ClipUpdateRequest and ReorderRequest removed — granular per-clip
# endpoints have been consolidated into PUT /api/paper-edit (full save).


class EditInstructionRequest(BaseModel):
    """Post-pipeline edit request — natural language instruction to modify the video."""

    instruction: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description=(
            "Natural language edit instruction (e.g., 'change the music to something upbeat')"
        ),
    )
    edit_type: Optional[
        Literal["rescript", "resynthesize", "rerender", "audio", "captions", "general"]
    ] = Field(
        None,
        description=(
            "Hint: 'rescript', 'resynthesize', 'rerender', 'audio', or "
            "'general'. Auto-detected if omitted."
        ),
    )


class CaptionStyleRequest(BaseModel):
    """User's chosen caption style for the Captioner agent."""

    apply: bool = False

    style_id: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="Caption style preset ID (e.g. 'bold-yellow', 'clean-white')",
    )


class ProjectSettings(BaseModel):
    """Configurable project settings exposed to the frontend."""

    output_width: Optional[int] = Field(None, ge=64, le=7680)
    output_height: Optional[int] = Field(None, ge=64, le=7680)
    output_orientation: Optional[Literal["portrait", "landscape"]] = None
    output_fps: Optional[int] = Field(None, ge=1, le=120)
    output_audio_rate: Optional[int] = Field(None, ge=8_000, le=192_000)
    keyframe_interval: Optional[int] = Field(None, ge=1, le=30)


class RenderRequest(BaseModel):
    """Re-render with custom resolution & quality."""

    width: int = Field(1080, ge=64, le=7680, description="Output width in pixels")
    height: int = Field(1920, ge=64, le=7680, description="Output height in pixels")
    quality: Literal["high", "medium", "low"] = Field("high", description="Encoding quality")


class ColorGradeRequest(BaseModel):
    """Color grading parameters — all values are offsets from neutral."""

    brightness: float = Field(0.0, ge=-1.0, le=1.0, description="Brightness offset (-1..1)")
    contrast: float = Field(
        1.0, ge=0.0, le=3.0, description="Contrast multiplier (0..3, 1=neutral)"
    )
    saturation: float = Field(
        1.0, ge=0.0, le=3.0, description="Saturation multiplier (0..3, 1=neutral)"
    )
    gamma: float = Field(1.0, ge=0.1, le=5.0, description="Gamma (0.1..5, 1=neutral)")
    temperature: float = Field(
        0.0, ge=-1.0, le=1.0, description="Warm/cool shift (-1=cool, 0=neutral, 1=warm)"
    )
    tint: float = Field(
        0.0, ge=-1.0, le=1.0, description="Green/magenta tint (-1=green, 0=neutral, 1=magenta)"
    )
    shadows: float = Field(0.0, ge=-1.0, le=1.0, description="Shadow lift/crush (-1..1)")
    highlights: float = Field(0.0, ge=-1.0, le=1.0, description="Highlight lift/crush (-1..1)")


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET BROADCAST
# ═══════════════════════════════════════════════════════════════════════════════


def _json_default(obj):
    """Custom JSON default serializer — handles Phase enums safely."""
    if isinstance(obj, Phase):
        return obj.value
    if hasattr(obj, "value"):  # other enums
        return obj.value
    return str(obj)


async def _broadcast(event: dict):
    """Broadcast a JSON event to all connected WebSocket clients."""
    message = json.dumps(event, default=_json_default)
    disconnected = []
    for ws in sessions.all_websockets:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        sessions.remove_global_ws(ws)


# ─── Timeline Extras (V2 overlays, music — not in CRDT) ──────────────────────

_timeline_extras: dict = {}  # in-memory cache of the latest extras


def _save_timeline_extras(overlay_clips: list | None = None, music_path: str | None = None) -> None:
    """Persist non-CRDT timeline data to the project's state directory."""
    global _timeline_extras
    extras = {}
    if overlay_clips:
        extras["overlay_clips"] = overlay_clips
    if music_path:
        extras["music_path"] = music_path
    _timeline_extras = extras
    try:
        p = settings.state_dir / "timeline_extras.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(extras, f, indent=2)
    except Exception:
        logger.warning("Failed to save timeline extras", exc_info=True)


def _load_timeline_extras() -> dict:
    """Load non-CRDT timeline data from the project's state directory."""
    global _timeline_extras
    try:
        p = settings.state_dir / "timeline_extras.json"
        if p.exists():
            with open(p) as f:
                _timeline_extras = json.load(f)
                return _timeline_extras
    except Exception:
        logger.warning("Failed to load timeline extras", exc_info=True)
    _timeline_extras = {}
    return _timeline_extras


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH & INFO
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/health", tags=["System"])
async def health_check():
    """Health check — returns server version, uptime status, and config.

    This endpoint MUST be fast and never block on pipeline work.
    """
    try:
        phase = _phase_val(
            sessions.active.pipeline_state.get("phase", Phase.IDLE)
            if sessions.active
            else Phase.IDLE
        )
    except Exception:
        phase = "idle"
    return {
        "status": "ok",
        "version": __version__,
        "phase": phase,
    }


@app.get("/api/config", tags=["System"])
async def get_config():
    """Get current project configuration without changing an in-flight analysis."""
    active = sessions.active
    if not (active and active._running_task and not active._running_task.done()):
        settings.reload_secrets()
    return {
        "output_width": settings.output_width,
        "output_height": settings.output_height,
        "output_orientation": settings.output_orientation,
        "output_fps": settings.output_fps,
        "output_audio_rate": settings.output_audio_rate,
        "keyframe_interval": settings.keyframe_interval,
        "vlm_model": settings.vlm_model,
        "gemini_model": settings.gemini_model,
        "api_keys": runlog.key_status(),
        "color_grade": _color_grade,
        "project_dir": str(settings._project_root),
    }


class SetProjectDirRequest(BaseModel):
    project_dir: str


@app.post("/api/project/set-dir", tags=["System"])
async def set_project_dir(request: SetProjectDirRequest):
    """
    Set the active project directory at runtime.

    Called by Electron when the user opens or creates a project.
    Switches all backend paths (media, state, output, cache) to the
    new project directory and ensures the directory structure exists.
    """
    project_path = Path(request.project_dir).expanduser().resolve()
    if not project_path.is_dir():
        raise HTTPException(400, f"Directory does not exist: {request.project_dir}")

    active = sessions.active
    if active and active._running_task and not active._running_task.done():
        raise HTTPException(409, "Cannot switch projects while a pipeline job is running")

    # Save the outgoing project's CRDT before changing the path it targets.
    crdt_mod.save_snapshot()
    await crdt_mod.disconnect_clients()
    crdt_mod.clear_doc()
    await _close_project_checkpointer()

    # Update the settings singleton — all derived paths (media_dir, state_dir, etc.)
    # automatically point to the new project directory.
    settings.kinetograph_project_dir = str(project_path)

    # Ensure project structure exists
    for sub in (
        "media",
        "media/.synth",
        "state",
        "output",
        ".cache",
        ".cache/thumbnails",
        ".cache/waveforms",
        ".cache/metadata",
        ".cache/audio",
    ):
        (project_path / sub).mkdir(parents=True, exist_ok=True)

    # ── Reload CRDT for the new project ──────────────────────────
    # Point persistence at the new project directory and load only its state.
    crdt_mod.set_snapshot_path(settings.state_dir / "crdt_snapshot.yjs")
    # 4. Load the new project's snapshot (no-op if it doesn't exist yet)
    crdt_mod.load_snapshot()
    # 5. Load non-CRDT timeline extras (V2 overlays, music)
    _load_timeline_extras()

    # Reset every in-memory cache/config whose scope is the old project before
    # loading the new one.  In particular, an empty media_refs.json must not
    # inherit refs from the previous project.
    _reset_project_runtime()
    _load_media_refs()
    _rebuild_asset_index()

    # Restore a paused/completed session belonging to this project, if any.
    sessions.reset_active()
    await _restore_persisted_session()

    logger.info(f"📂 Project directory set to: {project_path}")

    return {
        "status": "ok",
        "project_dir": str(project_path),
        "media_dir": str(settings.media_dir),
        "state_dir": str(settings.state_dir),
        "output_dir": str(settings.output_dir),
    }


# ─── Mutable runtime config ────────────────────────────────────────────────────
_DEFAULT_COLOR_GRADE: dict = {
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "gamma": 1.0,
    "temperature": 0.0,
    "tint": 0.0,
    "shadows": 0.0,
    "highlights": 0.0,
}
_color_grade: dict = _DEFAULT_COLOR_GRADE.copy()


def _reset_project_runtime() -> None:
    """Drop mutable process state that must never cross project boundaries."""
    global _color_grade, _asset_type_overrides, _media_refs, _asset_index
    _color_grade = _DEFAULT_COLOR_GRADE.copy()
    _asset_type_overrides = {}
    _media_refs = {}
    _asset_index = {}
    settings.__dict__.pop("_custom_width", None)
    settings.__dict__.pop("_custom_height", None)


@app.post("/api/config", tags=["System"])
async def update_config(body: ProjectSettings):
    """
    Update output resolution and settings at runtime.

    Only non-null fields are applied.  This mutates the global ``settings``
    singleton so subsequent pipeline runs pick up the new values.
    """
    if body.output_orientation is not None:
        settings.output_orientation = body.output_orientation
        # An explicit orientation selection returns to its canonical size.
        if body.output_width is None and body.output_height is None:
            settings.__dict__.pop("_custom_width", None)
            settings.__dict__.pop("_custom_height", None)
    if body.output_width is not None and body.output_height is not None:
        # Override orientation based on dimensions
        settings.output_orientation = (
            "portrait" if body.output_height > body.output_width else "landscape"
        )
        # Store as custom overrides
        settings.__dict__["_custom_width"] = body.output_width
        settings.__dict__["_custom_height"] = body.output_height
    if body.output_fps is not None:
        settings.output_fps = body.output_fps
    if body.output_audio_rate is not None:
        settings.output_audio_rate = body.output_audio_rate
    if body.keyframe_interval is not None:
        settings.keyframe_interval = body.keyframe_interval

    return {
        "status": "ok",
        "output_width": settings.output_width,
        "output_height": settings.output_height,
        "output_orientation": settings.output_orientation,
        "output_fps": settings.output_fps,
    }


@app.post("/api/config/color-grade", tags=["System"])
async def set_color_grade(body: ColorGradeRequest):
    """
    Set global color-grading parameters.

    These are applied as FFmpeg ``eq`` + ``colorbalance`` filters when the
    Director normalises / re-renders clips.
    """
    global _color_grade
    _color_grade = body.model_dump()
    return {"status": "ok", "color_grade": _color_grade}


@app.get("/api/config/color-grade", tags=["System"])
async def get_color_grade():
    """Return the current colour-grading parameters."""
    return _color_grade


_QUALITY_CRF = {"high": "18", "medium": "23", "low": "28"}


@app.post("/api/render", tags=["Pipeline"])
@_serialize_start
async def start_render(request: RenderRequest):
    """
    Re-render the current project with custom resolution and quality.

    Uses the existing approved Paper Edit and normalized clips.
    This does NOT re-run the full AI pipeline — it only re-composites
    and encodes.
    """
    session = sessions.active
    if session is None:
        raise HTTPException(400, "No active pipeline session.")

    ps = session.pipeline_state
    # Prefer the latest CRDT doc (includes user edits), then fall back to session state
    approved = crdt_mod.paper_edit_from_doc() or ps.get("approved_edit") or ps.get("paper_edit")
    if not approved:
        raise HTTPException(400, "No paper edit — run the pipeline first.")

    approved = {**(ps.get("approved_edit") or ps.get("paper_edit") or {}), **approved}
    normalized = ps.get("normalized_clips", {})
    if not normalized:
        raise HTTPException(400, "No normalized clips — run the pipeline first.")

    if session._running_task and not session._running_task.done():
        raise HTTPException(409, "Pipeline already running. Wait for it to finish.")

    # A re-render is a first-class graph run, not a Director-only shortcut:
    # captions, mastering, and OTIO export must process the new artifact too.
    from kinetograph.orchestrator import compile_graph

    settings.reload_secrets()
    run_id = uuid.uuid4().hex
    render_state = dict(ps)
    render_state.update(pipeline_options())
    render_state.update(
        {
            "phase": Phase.IDLE,
            "approved_edit": approved,
            "normalized_clips": normalized,
            "errors": [],
            "completed_agents": [],
            "run_id": run_id,
            "render_settings": {
                "width": request.width,
                "height": request.height,
                "crf": int(_QUALITY_CRF[request.quality]),
            },
            "color_grade": _color_grade,
        }
    )
    session.graph = compile_graph(start_from="director", checkpointer=await _project_checkpointer())
    session.config = {"configurable": {"thread_id": f"{session.thread_id}-render-{run_id}"}}
    session.graph_start = "director"
    session.pipeline_state = render_state
    _persist_session(session)
    await _broadcast({"type": "pipeline_started", "thread_id": session.thread_id})
    session._running_task = asyncio.create_task(
        _stream_pipeline(session, render_state, first_node="director")
    )
    return {
        "status": "started",
        "message": f"Re-rendering at {request.width}×{request.height} "
        f"({request.quality}). Listen on WebSocket for updates.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  PIPELINE CONTROL
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/pipeline/status", tags=["Pipeline"])
async def get_pipeline_status():
    """
    Get full pipeline status including current phase, project name,
    active agent, error list, and output paths.
    """
    s = sessions.active
    ps = s.pipeline_state if s else {"phase": Phase.IDLE}
    phase = _phase_val(ps.get("phase", Phase.IDLE))
    progress_by_phase = {
        "idle": 0.0,
        "normalizing": 0.1,
        "rendering": 0.35,
        "rendered": 0.55,
        "captioning": 0.65,
        "mastering": 0.78,
        "mastered": 0.88,
        "exporting": 0.94,
        "complete": 1.0,
    }
    return {
        "phase": phase,
        "stage": phase,
        "progress": progress_by_phase.get(phase, 0.0),
        "project_name": ps.get("project_name", ""),
        "user_prompt": ps.get("user_prompt", ""),
        "created_at": ps.get("created_at", ""),
        "asset_count": len(ps.get("raw_assets", [])),
        "index_count": len(ps.get("master_index", [])),
        "synth_count": len(ps.get("synth_assets", [])),
        "render_path": ps.get("render_path"),
        "timeline_path": ps.get("timeline_path"),
        "errors": ps.get("errors", []),
    }


@app.post("/api/pipeline/run", tags=["Pipeline"])
@_serialize_start
async def run_pipeline(request: RunRequest):
    """
    Start a new pipeline run.

    Returns immediately with a thread_id. The pipeline runs asynchronously
    in a background task.  Connect to the WebSocket at /ws to receive
    real-time phase updates. The pipeline will pause at 'awaiting_approval'
    for human review of the Paper Edit.
    """

    from kinetograph.orchestrator import compile_graph

    active = sessions.active
    if active and active._running_task and not active._running_task.done():
        raise HTTPException(409, "Pipeline already running. Wait for it to finish.")
    _require_gemini_key()
    try:
        session = sessions.create()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.graph = compile_graph(checkpointer=await _project_checkpointer())
    session.graph_start = "archivist"

    initial_state = {
        **pipeline_options(),
        "phase": Phase.IDLE,
        "user_prompt": request.prompt,
        "project_name": request.project_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": uuid.uuid4().hex,
        "raw_assets": [],
        "master_index": [],
        "synth_assets": [],
        "errors": [],
        "normalized_clips": {},
        "render_history": [],
        "completed_agents": [],
        "color_grade": _color_grade
        if any(
            abs(v - (1.0 if k in ("contrast", "saturation", "gamma") else 0.0)) > 0.001
            for k, v in _color_grade.items()
        )
        else None,
    }

    session.pipeline_state = initial_state.copy()
    _persist_session(session)

    await _broadcast({"type": "pipeline_started", "thread_id": session.thread_id})

    # Fire-and-forget: run pipeline in background so the HTTP response returns instantly.
    session._running_task = asyncio.create_task(
        _stream_pipeline(session, initial_state, first_node="archivist")
    )

    return {
        "status": "started",
        "thread_id": session.thread_id,
        "message": "Pipeline started. Listen on WebSocket for updates.",
    }


# Deterministic next-node map for broadcasting "in-progress" phases.
# After node A completes, we broadcast node B's starting phase so the
# frontend can show a spinner.  Conditional edges (human_review) are
# handled specially inside _stream_pipeline.
_DETERMINISTIC_NEXT: dict[str, str] = {
    "archivist": "scripter",
    "scripter": "critic",
    # human_review → handled by conditional check (scripter or synthesizer/director)
    "synthesizer": "director",
    "director": "captioner",
    "captioner": "sound_engineer",
    "sound_engineer": "export",
}


async def _stream_pipeline(session: "PipelineSession", input_data, first_node: str | None = None):
    """Unified background coroutine — streams LangGraph events and broadcasts via WS.

    Used by every pipeline flow (run, approve, edit, render).  The pipeline is
    deterministic-sequential, so we know which node comes next and can
    broadcast its starting phase proactively.  Each call is recorded in the
    run's log folder (see kinetograph.runlog).
    """
    ps = session.pipeline_state
    run_log = runlog.RunLog(ps.get("run_id") or session.thread_id)
    run_log.start(
        thread_id=session.thread_id,
        project_name=ps.get("project_name"),
        prompt=ps.get("user_prompt"),
        edit_instruction=ps.get("edit_instruction"),
        start_from=first_node or "resume",
    )
    with run_log.capture():
        await _stream_pipeline_logged(session, input_data, first_node, run_log)


async def _stream_pipeline_logged(
    session: "PipelineSession",
    input_data,
    first_node: str | None,
    run_log: runlog.RunLog,
):
    log_dir = str(run_log.dir)
    try:
        if first_node:
            await _broadcast_starting_phase(first_node)

        async for event in session.graph.astream(input_data, session.config, stream_mode="updates"):
            for node_name, update in event.items():
                if node_name.startswith("__"):  # skip __interrupt__ etc.
                    continue
                update = _safe_update(update)
                _merge_state(session.pipeline_state, update)
                _persist_session(session)
                phase = update.get("phase", "")
                phase_str = _phase_val(phase)
                run_log.node_done(node_name, phase_str, update.get("errors", []))
                if update.get("analysis_stats"):
                    run_log.analysis_finished(update["analysis_stats"])
                await _broadcast(
                    {
                        "type": "phase_update",
                        "node": node_name,
                        "phase": phase_str,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "errors": update.get("errors", []),
                    }
                )

                # Broadcast starting phase for the next node in the sequence
                if phase_str != "error":
                    next_node = _DETERMINISTIC_NEXT.get(node_name)
                    if next_node:
                        await _broadcast_starting_phase(next_node)

        # Check if paused at interrupt (human approval gate). Use the ASYNC
        # state getter — the async SqliteSaver rejects sync calls from the loop.
        snapshot = await session.graph.aget_state(session.config)
        if snapshot.next:
            session.pipeline_state["phase"] = Phase.AWAITING_APPROVAL
            # Push paper edit into CRDT doc — auto-syncs to frontend
            pe = session.pipeline_state.get("paper_edit")
            if pe:
                crdt_mod.load_paper_edit(pe)
            _persist_session(session)
            run_log.finish("awaiting_approval")
            await _broadcast(
                {
                    "type": "awaiting_approval",
                    "paper_edit": pe,
                }
            )
            return

        # Pipeline finished — push final paper edit into CRDT doc
        final_pe = session.pipeline_state.get("approved_edit") or session.pipeline_state.get(
            "paper_edit"
        )
        if final_pe:
            crdt_mod.load_paper_edit(final_pe)

        # Pipeline finished — broadcast completion (with phase so frontend can distinguish
        # success/error)
        final_phase = _phase_val(session.pipeline_state.get("phase", ""))

        # Persist non-CRDT timeline data (V2 overlays, music) for project reload
        _save_timeline_extras(
            overlay_clips=session.pipeline_state.get("overlay_clips"),
            music_path=session.pipeline_state.get("music_path"),
        )
        _persist_session(session)
        run_log.finish(
            final_phase or "complete", render_path=session.pipeline_state.get("render_path")
        )

        await _broadcast(
            {
                "type": "pipeline_complete",
                "phase": final_phase,
                "render_path": session.pipeline_state.get("render_path"),
                "timeline_path": session.pipeline_state.get("timeline_path"),
                "music_path": session.pipeline_state.get("music_path"),
                "overlay_clips": session.pipeline_state.get("overlay_clips", []),
                "log_dir": log_dir,
            }
        )

    except asyncio.CancelledError:
        # Raised into the task by /api/pipeline/stop (or server shutdown).
        # Swallowed deliberately so the task ends cleanly and clients are told.
        logger.info("Pipeline stopped by user")
        session.pipeline_state["phase"] = Phase.IDLE
        _persist_session(session)
        run_log.finish("cancelled")
        await _broadcast({"type": "pipeline_stopped", "log_dir": log_dir})

    except Exception as exc:
        logger.error(f"Pipeline error: {exc}", exc_info=True)
        session.pipeline_state["phase"] = Phase.ERROR
        _persist_session(session)
        run_log.finish("error", error=str(exc))
        await _broadcast(
            {
                "type": "phase_update",
                "node": "error_handler",
                "phase": "error",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "errors": [
                    {
                        "agent": "pipeline",
                        "message": str(exc),
                        "phase": "error",
                        "recoverable": False,
                    }
                ],
            }
        )
        # Broadcast pipeline_complete with error phase so frontend shows error, not success
        await _broadcast(
            {
                "type": "pipeline_complete",
                "phase": "error",
                "render_path": session.pipeline_state.get("render_path"),
                "timeline_path": session.pipeline_state.get("timeline_path"),
                "log_dir": log_dir,
            }
        )


@app.post("/api/pipeline/approve", tags=["Pipeline"])
@_serialize_start
async def approve_pipeline(request: ApprovalRequest):
    """
    Approve or reject the Paper Edit and resume the pipeline.

    Returns immediately. The pipeline resumes asynchronously in a background task.
    - action='approve': continues to Synthesizer → Director → Sound Engineer → Export
    - action='reject': re-routes back to the Scripter for a new Paper Edit

    Optionally include a modified paper_edit if the user edited clips in the UI.
    """

    session = sessions.active
    if session is None or session.graph is None:
        raise HTTPException(400, "No active pipeline to approve/reject")

    from langgraph.types import Command

    decision = {"action": request.action, "reason": request.reason}
    if request.paper_edit:
        decision["paper_edit"] = request.paper_edit

    # Fire-and-forget: resume pipeline in background
    if session._running_task and not session._running_task.done():
        raise HTTPException(409, "Pipeline already running. Wait for it to finish.")
    settings.reload_secrets()
    session._running_task = asyncio.create_task(_stream_pipeline(session, Command(resume=decision)))

    return {
        "status": "started",
        "thread_id": session.thread_id,
        "message": f"Pipeline resumed with action='{request.action}'. "
        "Listen on WebSocket for updates.",
    }


@app.post("/api/pipeline/stop", tags=["Pipeline"])
async def stop_pipeline():
    """Stop the running pipeline task (run, resume, edit or render).

    Cancels the background task; in-flight async FFmpeg subprocesses are killed.
    Clients receive a ``pipeline_stopped`` WebSocket event. Idempotent.
    """
    session = sessions.active
    task = session._running_task if session else None
    if task is None or task.done():
        return {"status": "not_running"}
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=15)
    return {"status": "stopped" if done else "stopping"}


@app.get("/api/runs", tags=["Runs"])
async def list_runs(limit: int = Query(50, ge=1, le=500)):
    """Summaries of past pipeline runs in the active project, newest first."""
    return {"runs": runlog.list_runs(limit)}


@app.get("/api/runs/{run_id}", tags=["Runs"])
async def get_run(run_id: str, tail: int = Query(200, ge=0, le=5000)):
    """One run's summary, event timeline and the tail of its backend.log."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", run_id):
        raise HTTPException(400, "Invalid run id")
    run = runlog.read_run(run_id, tail)
    if run is None:
        raise HTTPException(404, f"No log for run {run_id}")
    return run


# ── Node → "in-progress" phase mapping for synthetic WS broadcasts ────────────
# When a node starts executing, we broadcast its starting phase so the frontend
# can show agent activity (spinning indicator + chat message).
# The graph's astream only emits AFTER a node completes.

_NODE_STARTING_PHASE: dict[str, str] = {
    "archivist": "ingesting",
    "scripter": "scripting",
    "human_review": "awaiting_approval",
    "synthesizer": "synthesizing",
    "director": "normalizing",
    "captioner": "captioning",
    "sound_engineer": "mastering",
    "export": "exporting",
}


async def _broadcast_starting_phase(node_name: str) -> None:
    """Broadcast a synthetic 'in-progress' phase update for a node that is about to start."""
    phase = _NODE_STARTING_PHASE.get(node_name)
    if phase:
        await _broadcast(
            {
                "type": "phase_update",
                "node": node_name,
                "phase": phase,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "errors": [],
            }
        )


# ─── POST-PIPELINE EDIT (Deterministic) ────────────────────────────────────────

# Rule-based edit classifier — determines which agent to start from based on
# the user's natural-language instruction.  No LLM call needed.
_EDIT_KEYWORDS: list[tuple[list[str], str]] = [
    # Audio / music only
    (
        [
            "music",
            "audio",
            "sound",
            "volume",
            "louder",
            "quieter",
            "bgm",
            "background music",
            "ducking",
            "noise",
        ],
        "sound_engineer",
    ),
    # Captions only
    (["caption", "subtitle", "text", "font", "word"], "captioner"),
    # Re-render only (no content change)
    (
        [
            "render",
            "quality",
            "resolution",
            "export",
            "color",
            "grade",
            "brightness",
            "contrast",
            "saturation",
        ],
        "director",
    ),
]


def _classify_edit(instruction: str) -> str:
    """Determine which agent to start from based on the edit instruction.

    Returns one of: 'scripter', 'director', 'captioner', 'sound_engineer'.
    Content-level edits (default) route through scripter → approval → full chain.

    Uses **word-boundary** matching (so "text" no longer matches "context" and
    "grade" no longer matches "upgrade") and **scoring** rather than first-list-
    wins (so "make the caption text louder" routes to the captioner — 2 keyword
    hits — instead of the sound engineer). Ties fall back to keyword-list order;
    no matches default to the scripter.
    """
    lower = instruction.lower()
    best_node = "scripter"
    best_score = 0
    for keywords, start_node in _EDIT_KEYWORDS:
        score = sum(1 for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", lower))
        if score > best_score:
            best_score = score
            best_node = start_node
    return best_node


@app.post("/api/pipeline/edit", tags=["Pipeline"])
@_serialize_start
async def edit_pipeline(request: EditInstructionRequest):
    """
    Post-pipeline edit endpoint.

    Accepts a natural language instruction to modify the current video.
    A rule-based classifier determines which agent to start from — content
    edits go through scripter + approval; audio/caption/render edits skip
    directly to the relevant agent.

    Runs in background; listen on WebSocket for updates.
    """
    session = sessions.active
    if session is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No active pipeline session. Use /api/pipeline/run to start a new pipeline first."
            ),
        )
    if session._running_task and not session._running_task.done():
        raise HTTPException(409, "Pipeline already running. Wait for it to finish.")

    from kinetograph.orchestrator import compile_graph

    # Classify the edit to determine starting agent
    explicit = {
        "rescript": "scripter",
        "resynthesize": "synthesizer",
        "rerender": "director",
        "audio": "sound_engineer",
        "captions": "captioner",
    }
    start_from = explicit.get(request.edit_type) or _classify_edit(request.instruction)
    # Legacy renders have no clean baseline; rebuild once before changing sound/text.
    baseline = "caption_source_path" if start_from == "captioner" else "picture_path"
    if start_from in {"captioner", "sound_engineer"} and not (
        session.pipeline_state.get(baseline) and Path(session.pipeline_state[baseline]).is_file()
    ):
        start_from = "director"
    logger.info("Edit classified: '%s' → start_from=%s", request.instruction[:80], start_from)
    if start_from == "scripter":
        _require_gemini_key()
    else:
        settings.reload_secrets()

    # Build edit state from current pipeline state
    original_prompt = session.pipeline_state.get("user_prompt", "")
    edit_state = dict(session.pipeline_state)
    edit_state["phase"] = Phase.IDLE
    edit_state["errors"] = []
    edit_state["completed_agents"] = []
    edit_state["edit_instruction"] = request.instruction
    edit_state["user_prompt"] = f"{original_prompt}\n\n[EDIT REQUEST]: {request.instruction}"
    edit_state["run_id"] = uuid.uuid4().hex

    # Inject current colour-grading settings
    edit_state["color_grade"] = (
        _color_grade
        if any(
            abs(v - (1.0 if k in ("contrast", "saturation", "gamma") else 0.0)) > 0.001
            for k, v in _color_grade.items()
        )
        else None
    )

    edit_state.update(pipeline_options())
    if start_from == "sound_engineer":
        # An explicit music edit requests a new composition, never another music layer.
        edit_state["music_path"] = None

    # Compile a new graph starting from the classified agent
    graph = compile_graph(start_from=start_from, checkpointer=await _project_checkpointer())
    config = {"configurable": {"thread_id": f"{session.thread_id}-edit-{uuid.uuid4().hex[:8]}"}}

    # Store on session so approve endpoint can resume if needed
    session.graph = graph
    session.config = config
    session.graph_start = start_from
    session.pipeline_state = edit_state
    _persist_session(session)

    # Broadcast pipeline_started so the frontend clears the old rendered video
    await _broadcast({"type": "pipeline_started", "thread_id": session.thread_id})

    session._running_task = asyncio.create_task(
        _stream_pipeline(session, edit_state, first_node=start_from)
    )

    return {
        "status": "started",
        "message": f"Edit started from {start_from}. Listen on WebSocket for updates.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  CAPTION STYLE PICKER (user selects before captioner runs)
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/pipeline/caption-styles", tags=["Pipeline"])
async def get_caption_styles():
    """Return all available caption style presets."""
    from kinetograph.core.captions import CAPTION_STYLE_PRESETS

    return {
        "styles": list(CAPTION_STYLE_PRESETS.values()),
        "selected_style_id": load_options().caption_style_id,
    }


@app.post("/api/pipeline/caption-style", tags=["Pipeline"])
async def select_caption_style(request: CaptionStyleRequest):
    """Persist a project preference and optionally apply it in a single request."""
    try:
        style = caption_style(request.style_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session = sessions.active
    if _pipeline_start_lock.locked() or (
        session and session._running_task and not session._running_task.done()
    ):
        raise HTTPException(409, "Wait for the current edit to finish before changing captions.")
    if request.apply and (not session or not session.pipeline_state.get("render_path")):
        raise HTTPException(409, "Render a video before applying captions.")
    options = load_options()
    options.caption_style_id = request.style_id
    save_options(options)
    if request.apply:
        await edit_pipeline(
            EditInstructionRequest(
                instruction=f"Apply caption style {request.style_id}",
                edit_type="captions",
            )
        )
    elif session:
        session.pipeline_state["caption_style"] = style
        _persist_session(session)
    return {
        "status": "started" if request.apply else "ok",
        "style_id": request.style_id,
        "style_name": style["name"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  ASSET MANAGEMENT — Enterprise Media Cache Architecture
#
#  Design goals (like Adobe Premiere Pro / DaVinci Resolve):
#    • Reference-based import — original files stay in place, never copied
#    • Persistent metadata cache — ffprobe results saved as JSON (keyed by mtime+size)
#    • Disk-cached thumbnails & waveforms — generated once, served forever
#    • In-memory asset index — O(1) lookups by asset_id
#    • Streaming upload — chunked I/O, never reads entire file into RAM
#    • Graceful cache invalidation — stale caches auto-purged on mtime/size change
# ═══════════════════════════════════════════════════════════════════════════════

# In-memory map of asset_id → overridden asset_type (deprecated, kept for compat)
_asset_type_overrides: dict[str, str] = {}

# ── Media References Registry ──────────────────────────────────────────────────
# Maps asset_id → absolute path to the ORIGINAL source file (reference-based import).
# Persisted to state/media_refs.json — never copies the source file.
_media_refs: dict[str, str] = {}
_media_refs_lock = asyncio.Lock()


def _load_media_refs() -> None:
    """Load the media references map from disk (called once at startup)."""
    global _media_refs
    _media_refs = {}
    refs_path = settings.media_refs_path
    if refs_path.exists():
        try:
            with open(refs_path) as f:
                _media_refs = json.load(f)
            logger.info(f"📂 Loaded {len(_media_refs)} media references from {refs_path}")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"📂 Failed to load media refs: {exc}")
            _media_refs = {}


def _save_media_refs() -> None:
    """Persist the media references map to disk."""
    refs_path = settings.media_refs_path
    refs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(refs_path, "w") as f:
        json.dump(_media_refs, f, indent=2)


# ── In-memory Asset Index (O(1) lookup) ───────────────────────────────────────
# Rebuilt on startup and after every import/delete.  Maps asset_id → Path.
_asset_index: dict[str, Path] = {}


def _rebuild_asset_index() -> None:
    """Scan media dirs + media refs to build the in-memory asset lookup index."""
    global _asset_index
    idx: dict[str, Path] = {}
    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS

    # 1. Scan physical directories (media/ and .synth/)
    for folder in (settings.media_dir, settings.synth_cache_dir):
        if not folder.exists():
            continue
        for f in sorted(folder.iterdir()):
            if f.is_dir() or f.name.startswith("."):
                continue
            if f.suffix.lower() in all_extensions:
                idx[f.stem] = f

    # 2. Add referenced (external) files — these take precedence over copies
    for asset_id, ref_path_str in _media_refs.items():
        ref_path = Path(ref_path_str)
        if ref_path.exists():
            idx[asset_id] = ref_path

    _asset_index = idx
    logger.info(f"📂 Asset index rebuilt: {len(idx)} assets")


def _safe_within(root: Path, candidate: Path) -> bool:
    """True if `candidate` resolves inside `root` (blocks path traversal)."""
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _is_registerable_path(candidate: Path) -> bool:
    """Allow reference-import only from user-owned media locations.

    Blocks registering (and later streaming) sensitive system files such as
    /etc/passwd while still permitting media anywhere the user normally keeps
    it: their home directory, mounted/removable volumes, or the project dir.
    """
    roots = [Path.home(), settings.media_dir, settings.output_dir, settings.state_dir]
    roots += [Path(p) for p in ("/Volumes", "/media", "/mnt")]
    return any(_safe_within(root, candidate) for root in roots if root.exists())


def _find_asset(asset_id: str) -> Path | None:
    """O(1) asset lookup by ID from the in-memory index.

    Falls back to a linear scan if the index misses (handles race conditions
    where a file was added between index rebuilds).
    """
    # Fast path: index hit
    path = _asset_index.get(asset_id)
    if path and path.exists():
        return path

    # Slow fallback: linear scan (auto-repairs the index)
    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    for folder in (settings.media_dir, settings.synth_cache_dir):
        if not folder.exists():
            continue
        for f in folder.iterdir():
            if f.stem == asset_id and f.suffix.lower() in all_extensions:
                _asset_index[asset_id] = f  # repair index
                return f

    # Check media refs
    ref = _media_refs.get(asset_id)
    if ref:
        p = Path(ref)
        if p.exists():
            _asset_index[asset_id] = p
            return p

    return None


# ── Metadata Cache (persistent ffprobe results) ───────────────────────────────


def _metadata_cache_key(file_path: Path) -> str:
    """Generate a cache key from asset stem + file mtime + file size.

    This means the cache auto-invalidates when the file is modified or replaced.
    """
    stat = file_path.stat()
    raw = f"{file_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cached_probe_media(file_path: Path) -> dict:
    """Probe a media file, returning cached results when available.

    First checks the persistent JSON cache in .cache/metadata/.
    On miss, runs ffprobe, caches the result, and returns it.
    """
    cache_dir = settings.metadata_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        cache_key = _metadata_cache_key(file_path)
    except OSError:
        # File doesn't exist or stat failed — fall back to uncached probe
        return probe_media(file_path)

    cache_file = cache_dir / f"{cache_key}.json"

    # Cache hit — return immediately (no subprocess)
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            cache_file.unlink(missing_ok=True)  # corrupt cache, regenerate

    # Cache miss — probe and persist
    meta = probe_media(file_path)
    try:
        with open(cache_file, "w") as f:
            json.dump(meta, f)
    except OSError:
        pass  # non-fatal: cache write failed, will re-probe next time

    return meta


async def _cached_probe_media_async(file_path: Path) -> dict:
    """Async wrapper — runs the (blocking) ffprobe off the event loop.

    ffprobe can take up to 30s per uncached file; calling the sync version
    directly inside an async handler froze the event loop (health checks and
    all websockets) for the duration. Offload to a worker thread instead.
    """
    return await asyncio.to_thread(_cached_probe_media, file_path)


# ── Startup hooks ──────────────────────────────────────────────────────────────


@app.patch("/api/assets/{asset_id}/type", tags=["Assets"])
async def update_asset_type(
    asset_id: str,
    body: dict,
):
    """
    Override the asset_type for a given asset (deprecated).

    Body: {"asset_type": "primary" | "cutaway"}
    The override is kept in-memory for the lifetime of the server.
    """
    new_type = body.get("asset_type")
    if new_type not in ("primary", "cutaway", "synth"):
        raise HTTPException(status_code=422, detail=f"Invalid asset_type: {new_type}")
    _asset_type_overrides[asset_id] = new_type
    logger.info(f"Asset type override: {asset_id} → {new_type}")
    return {"status": "ok", "asset_id": asset_id, "asset_type": new_type}


@app.get("/api/assets", tags=["Assets"])
async def list_assets():
    """
    List all media assets in the project.

    Uses the persistent metadata cache — first request after import probes
    each file once, subsequent requests are instant (no subprocesses).
    """
    assets = []

    # 1. Scan physical directories (local copies + synth cache)
    scan_dirs = {
        "media": settings.media_dir,
        "synth": settings.synth_cache_dir,
    }
    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    seen_ids: set[str] = set()

    for source_label, folder in scan_dirs.items():
        if not folder.exists():
            continue
        for f in sorted(folder.iterdir()):
            if f.is_dir() or f.name.startswith("."):
                continue
            if f.suffix.lower() not in all_extensions:
                continue
            try:
                meta = await _cached_probe_media_async(f)
                asset_type = "synth" if source_label == "synth" else "media"
                assets.append(
                    {
                        "id": f.stem,
                        "file_name": f.name,
                        "file_path": str(f),
                        "asset_type": _asset_type_overrides.get(f.stem, asset_type),
                        "duration_ms": meta["duration_ms"],
                        "width": meta["width"],
                        "height": meta["height"],
                        "fps": meta["fps"],
                        "has_audio": meta["has_audio"],
                        "codec": meta["codec"],
                        "thumbnail_url": f"/api/assets/{f.stem}/thumbnail",
                        "waveform_url": f"/api/assets/{f.stem}/waveform"
                        if meta["has_audio"]
                        else None,
                        "stream_url": f"/api/assets/{f.stem}/stream",
                    }
                )
                seen_ids.add(f.stem)
            except RuntimeError:
                assets.append(
                    {
                        "id": f.stem,
                        "file_name": f.name,
                        "file_path": str(f),
                        "asset_type": _asset_type_overrides.get(f.stem, "media"),
                        "error": "Failed to probe file — may be corrupt",
                    }
                )
                seen_ids.add(f.stem)

    # 2. Referenced (external) files — not physically in project dirs
    for asset_id, ref_path_str in _media_refs.items():
        if asset_id in seen_ids:
            continue  # already listed from physical scan (symlink or copy)
        ref_path = Path(ref_path_str)
        if not ref_path.exists():
            assets.append(
                {
                    "id": asset_id,
                    "file_name": ref_path.name,
                    "file_path": ref_path_str,
                    "asset_type": "media",
                    "error": f"Media offline — original file not found: {ref_path_str}",
                }
            )
            continue
        try:
            meta = await _cached_probe_media_async(ref_path)
            assets.append(
                {
                    "id": asset_id,
                    "file_name": ref_path.name,
                    "file_path": ref_path_str,
                    "asset_type": _asset_type_overrides.get(asset_id, "media"),
                    "duration_ms": meta["duration_ms"],
                    "width": meta["width"],
                    "height": meta["height"],
                    "fps": meta["fps"],
                    "has_audio": meta["has_audio"],
                    "codec": meta["codec"],
                    "thumbnail_url": f"/api/assets/{asset_id}/thumbnail",
                    "waveform_url": f"/api/assets/{asset_id}/waveform"
                    if meta["has_audio"]
                    else None,
                    "stream_url": f"/api/assets/{asset_id}/stream",
                }
            )
        except RuntimeError:
            assets.append(
                {
                    "id": asset_id,
                    "file_name": ref_path.name,
                    "file_path": ref_path_str,
                    "asset_type": "media",
                    "error": "Failed to probe file — may be corrupt",
                }
            )

    return {"assets": assets, "total": len(assets)}


@app.get("/api/assets/{asset_id}/thumbnail", tags=["Assets"])
async def get_asset_thumbnail(
    asset_id: str,
    t: float = Query(0.5, description="Timestamp in seconds to capture thumbnail"),
):
    """
    Get a JPEG thumbnail for an asset at a specific timestamp.

    Enterprise behaviour: generated once, then served from disk cache forever.
    Cache key = asset_id + timestamp → .cache/thumbnails/<key>.jpg
    """
    video_path = _find_asset(asset_id)
    if not video_path:
        raise HTTPException(404, f"Asset not found: {asset_id}")

    # Disk cache lookup
    cache_dir = settings.thumbnail_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"{asset_id}_{t:.2f}"
    cache_file = cache_dir / f"{cache_key}.jpg"

    if cache_file.exists():
        return FileResponse(
            str(cache_file),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # Cache miss — generate via FFmpeg
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(t),
                "-i",
                str(video_path),
                "-vframes",
                "1",
                "-vf",
                "scale=320:-1",
                "-f",
                "image2",
                "-c:v",
                "mjpeg",
                str(cache_file),
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0 or not cache_file.exists():
            cache_file.unlink(missing_ok=True)
            raise HTTPException(500, "Thumbnail generation failed")

        return FileResponse(
            str(cache_file),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    except subprocess.TimeoutExpired:
        cache_file.unlink(missing_ok=True)
        raise HTTPException(500, "Thumbnail generation timed out")


@app.get("/api/assets/{asset_id}/waveform", tags=["Assets"])
async def get_asset_waveform(
    asset_id: str,
    width: int = Query(800, description="Waveform image width in pixels"),
    height: int = Query(120, description="Waveform image height in pixels"),
):
    """
    Get an audio waveform PNG for an asset.

    Enterprise behaviour: generated once, then served from disk cache.
    Cache key = asset_id + dimensions → .cache/waveforms/<key>.png
    """
    video_path = _find_asset(asset_id)
    if not video_path:
        raise HTTPException(404, f"Asset not found: {asset_id}")

    # Disk cache lookup
    cache_dir = settings.waveform_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"{asset_id}_{width}x{height}"
    cache_file = cache_dir / f"{cache_key}.png"

    if cache_file.exists():
        return FileResponse(
            str(cache_file),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # Cache miss — generate via FFmpeg
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-filter_complex",
                f"aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors=#00d4ff",
                "-frames:v",
                "1",
                "-f",
                "image2",
                "-c:v",
                "png",
                str(cache_file),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not cache_file.exists():
            cache_file.unlink(missing_ok=True)
            raise HTTPException(500, "Waveform generation failed")

        return FileResponse(
            str(cache_file),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    except subprocess.TimeoutExpired:
        cache_file.unlink(missing_ok=True)
        raise HTTPException(500, "Waveform generation timed out")


@app.get("/api/assets/{asset_id}/stream", tags=["Assets"])
async def stream_asset(asset_id: str):
    """
    Stream a video asset file for playback in the frontend preview player.

    Returns the raw video file with proper Content-Type and Content-Disposition.
    Supports range requests for seeking.
    """
    video_path = _find_asset(asset_id)
    if not video_path:
        raise HTTPException(404, f"Asset not found: {asset_id}")

    mime, _ = mimetypes.guess_type(str(video_path))
    return FileResponse(str(video_path), media_type=mime or "video/mp4")


@app.post("/api/assets/register", tags=["Assets"])
async def register_asset(body: dict):
    """
    Register a media file by its absolute path — Adobe Premiere-style reference import.

    The file is NOT copied. Instead, we store a reference to its original location
    and create a symlink in the project's media/ directory for pipeline compatibility.
    Returns full metadata (dimensions, duration, etc.).

    Body: {"file_paths": ["/absolute/path/to/video.mp4", ...]}
    """
    file_paths = body.get("file_paths", [])
    if not file_paths:
        raise HTTPException(400, "No file_paths provided")

    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    results = []

    for file_path_str in file_paths:
        file_path = Path(file_path_str)

        if not file_path.exists():
            results.append({"file_path": file_path_str, "error": "File not found"})
            continue

        ext = file_path.suffix.lower()
        if ext not in all_extensions:
            results.append({"file_path": file_path_str, "error": f"Unsupported file type: {ext}"})
            continue

        if not _is_registerable_path(file_path):
            results.append({"file_path": file_path_str, "error": "Path not allowed"})
            continue

        asset_id = file_path.stem

        # De-duplicate: if this exact path is already registered, skip
        if asset_id in _media_refs and _media_refs[asset_id] == file_path_str:
            try:
                meta = await _cached_probe_media_async(file_path)
                results.append(
                    {
                        "status": "already_registered",
                        "id": asset_id,
                        "file_name": file_path.name,
                        "file_path": file_path_str,
                        "asset_type": "media",
                        **meta,
                    }
                )
            except RuntimeError as exc:
                results.append({"file_path": file_path_str, "error": str(exc)})
            continue

        # Handle name collisions: append a suffix
        if asset_id in _media_refs or asset_id in _asset_index:
            suffix = 1
            while f"{asset_id}_{suffix}" in _media_refs or f"{asset_id}_{suffix}" in _asset_index:
                suffix += 1
            asset_id = f"{asset_id}_{suffix}"

        # Store the reference
        _media_refs[asset_id] = file_path_str

        # Create a symlink in media/ for pipeline compatibility
        # (archivist, director, etc. expect files in media_dir)
        symlink_dest = settings.media_dir / file_path.name
        settings.media_dir.mkdir(parents=True, exist_ok=True)

        # Handle collision in symlink filename
        if symlink_dest.exists() and not symlink_dest.is_symlink():
            # A real file already exists — don't overwrite, use the original
            pass
        elif not symlink_dest.exists():
            try:
                symlink_dest.symlink_to(file_path)
            except OSError:
                # Symlink creation failed (e.g. cross-device on some filesystems)
                # Fall back to no symlink — the asset index has the original path
                logger.warning(
                    f"📂 Symlink creation failed for {file_path.name}, using reference only"
                )

        try:
            meta = await _cached_probe_media_async(file_path)
            results.append(
                {
                    "status": "registered",
                    "id": asset_id,
                    "file_name": file_path.name,
                    "file_path": file_path_str,
                    "asset_type": "media",
                    "thumbnail_url": f"/api/assets/{asset_id}/thumbnail",
                    "waveform_url": f"/api/assets/{asset_id}/waveform"
                    if meta.get("has_audio")
                    else None,
                    "stream_url": f"/api/assets/{asset_id}/stream",
                    **meta,
                }
            )
        except RuntimeError as exc:
            _media_refs.pop(asset_id, None)
            results.append({"file_path": file_path_str, "error": f"Corrupt file: {exc}"})

    # Persist refs and rebuild index
    _save_media_refs()
    _rebuild_asset_index()

    return {
        "status": "ok",
        "registered": len([r for r in results if "error" not in r]),
        "results": results,
    }


@app.post("/api/assets/upload", tags=["Assets"])
async def upload_asset(file: UploadFile):
    """
    Upload a new media asset to the project via streaming I/O.

    For large files this streams chunks to disk — never holds the entire
    file in RAM at once. Prefer POST /api/assets/register for local files.
    """
    folder = settings.media_dir

    if not file.filename:
        raise HTTPException(400, "No filename provided")

    # Strip any directory components from the client-supplied name so a value
    # like "../../evil.mp4" cannot escape the media directory.
    safe_name = Path(file.filename).name
    ext = Path(safe_name).suffix.lower()
    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    if ext not in all_extensions:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    dest = folder / safe_name
    folder.mkdir(parents=True, exist_ok=True)
    if not _safe_within(folder, dest):
        raise HTTPException(400, "Invalid filename")

    # Streaming write — 1 MB chunks, never loads entire file into RAM
    CHUNK_SIZE = 1024 * 1024  # 1 MB
    MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB cap (disk-fill guard)
    written = 0
    try:
        with open(dest, "xb") as f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise ValueError("File exceeds maximum upload size")
                f.write(chunk)
    except FileExistsError:
        raise HTTPException(409, "A file with this name already exists; rename it before uploading")
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(413, str(exc))
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"Upload failed: {exc}")
    finally:
        await file.close()

    try:
        meta = await _cached_probe_media_async(dest)
        _rebuild_asset_index()
        return {
            "status": "uploaded",
            "id": dest.stem,
            "file_name": safe_name,
            "file_path": str(dest),
            "asset_type": "media",
            "thumbnail_url": f"/api/assets/{dest.stem}/thumbnail",
            "waveform_url": f"/api/assets/{dest.stem}/waveform" if meta.get("has_audio") else None,
            "stream_url": f"/api/assets/{dest.stem}/stream",
            **meta,
        }
    except RuntimeError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Uploaded file appears corrupt: {exc}")


@app.delete("/api/assets/{asset_id}", tags=["Assets"])
async def delete_asset(asset_id: str):
    """
    Delete a media asset from the project.

    If the asset is reference-based, removes the reference and symlink but
    NEVER deletes the original source file. If it's a physical copy or synth
    file, the file itself is deleted.
    """
    # Check if this is a referenced asset
    if asset_id in _media_refs:
        original_path = _media_refs.pop(asset_id)
        _save_media_refs()

        # Remove the symlink in media/ if it points to this file
        for f in settings.media_dir.iterdir():
            if f.is_symlink() and f.stem == asset_id:
                f.unlink()
                break
            if f.is_symlink():
                try:
                    if str(f.resolve()) == original_path:
                        f.unlink()
                        break
                except OSError:
                    pass

        _asset_type_overrides.pop(asset_id, None)
        _asset_index.pop(asset_id, None)

        # Clean up cached thumbnails, waveforms, metadata for this asset
        _purge_asset_caches(asset_id)

        logger.info(
            f"📂 Unlinked referenced asset: {asset_id} (original untouched: {original_path})"
        )
        _rebuild_asset_index()
        return {"status": "unlinked", "asset_id": asset_id, "original_path": original_path}

    # Physical file — find and delete
    video_path = _find_asset(asset_id)
    if not video_path:
        raise HTTPException(404, f"Asset not found: {asset_id}")

    # If it's a symlink, remove the symlink (don't follow to delete the original)
    if video_path.is_symlink():
        video_path.unlink()
    else:
        video_path.unlink()

    _asset_type_overrides.pop(asset_id, None)
    _asset_index.pop(asset_id, None)
    _purge_asset_caches(asset_id)

    logger.info(f"📂 Deleted asset: {asset_id} ({video_path})")
    _rebuild_asset_index()
    return {"status": "deleted", "asset_id": asset_id}


def _purge_asset_caches(asset_id: str) -> None:
    """Remove all cached thumbnails, waveforms, and metadata for a given asset."""
    for cache_dir, prefix, ext in [
        (settings.thumbnail_cache_dir, asset_id, ".jpg"),
        (settings.waveform_cache_dir, asset_id, ".png"),
    ]:
        if not cache_dir.exists():
            continue
        for f in cache_dir.iterdir():
            if f.name.startswith(prefix) and f.suffix == ext:
                f.unlink(missing_ok=True)

    # Metadata cache uses hash keys, so we purge ALL metadata for this asset
    # by checking if any cache file's original stem matches. For efficiency,
    # we skip this (metadata files are tiny) and let them age out naturally.


# ── Cache Management Endpoint ──────────────────────────────────────────────────


@app.delete("/api/cache", tags=["System"])
async def purge_cache(
    targets: str = Query(
        "all",
        description="Comma-separated cache targets: thumbnails, waveforms, metadata, audio, all",
    ),
):
    """
    Purge the media cache — like Adobe Premiere's 'Clean Cache' button.

    Targets: thumbnails, waveforms, metadata, audio, all.
    Safe to call at any time — everything regenerates on demand.
    """
    target_set = {t.strip().lower() for t in targets.split(",")}
    purged = {}

    cache_map = {
        "thumbnails": settings.thumbnail_cache_dir,
        "waveforms": settings.waveform_cache_dir,
        "metadata": settings.metadata_cache_dir,
        "audio": settings.conformed_audio_dir,
    }

    if "all" in target_set:
        target_set = set(cache_map.keys())

    for name, cache_dir in cache_map.items():
        if name not in target_set:
            continue
        if not cache_dir.exists():
            purged[name] = 0
            continue

        count = 0
        for f in cache_dir.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
                count += 1
        purged[name] = count

    # Also clean up pipeline temp dirs if requested
    if "all" in {t.strip().lower() for t in targets.split(",")}:
        for temp_name in ("archivist_temp", "director_temp"):
            temp_dir = settings.state_dir / temp_name
            if temp_dir.exists():
                count = 0
                for f in temp_dir.rglob("*"):
                    if f.is_file():
                        f.unlink(missing_ok=True)
                        count += 1
                purged[temp_name] = count

    logger.info(f"🧹 Cache purged: {purged}")
    return {"status": "purged", "details": purged}


@app.get("/api/cache/stats", tags=["System"])
async def cache_stats():
    """
    Get cache disk usage statistics — like Adobe's cache size display.

    Returns byte counts for each cache category.
    """
    stats = {}
    cache_map = {
        "thumbnails": settings.thumbnail_cache_dir,
        "waveforms": settings.waveform_cache_dir,
        "metadata": settings.metadata_cache_dir,
        "audio": settings.conformed_audio_dir,
    }

    total = 0
    for name, cache_dir in cache_map.items():
        if not cache_dir.exists():
            stats[name] = {"files": 0, "bytes": 0}
            continue
        files = [f for f in cache_dir.iterdir() if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        stats[name] = {"files": len(files), "bytes": size}
        total += size

    # Pipeline temp dirs
    for temp_name in ("archivist_temp", "director_temp"):
        temp_dir = settings.state_dir / temp_name
        if temp_dir.exists():
            files = list(temp_dir.rglob("*"))
            file_list = [f for f in files if f.is_file()]
            size = sum(f.stat().st_size for f in file_list)
            stats[temp_name] = {"files": len(file_list), "bytes": size}
            total += size
        else:
            stats[temp_name] = {"files": 0, "bytes": 0}

    stats["total_bytes"] = total
    stats["total_human"] = _human_size(total)
    return stats


def _human_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


# ═══════════════════════════════════════════════════════════════════════════════
#  MASTER INDEX (transcript + visual context)
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/master-index", tags=["Index"])
async def get_master_index():
    """
    Get the full master index — transcript + visual descriptions for every segment.

    Each entry maps a time range in a source file to its spoken words
    (with word-level timestamps) and VLM-generated scene descriptions.
    """
    index_path = settings.state_dir / "master_index.json"
    if index_path.exists():
        with open(index_path) as f:
            data = json.load(f)
        return {"entries": data, "total": len(data)}
    raise HTTPException(404, "Master index not yet generated — run the pipeline first")


@app.get("/api/master-index/search", tags=["Index"])
async def search_master_index(
    q: str = Query(..., description="Search term to find in transcript or visual descriptions"),
):
    """
    Search the master index by keyword.

    Returns segments where the transcript or visual description contains the query.
    Useful for the frontend's clip search / cutaway suggestion feature.
    """
    index_path = settings.state_dir / "master_index.json"
    if not index_path.exists():
        raise HTTPException(404, "Master index not yet generated")

    with open(index_path) as f:
        data = json.load(f)

    q_lower = q.lower()
    results = []
    for entry in data:
        transcript = entry.get("transcript", "").lower()
        visuals = " ".join(entry.get("visual_descriptions", [])).lower()
        if q_lower in transcript or q_lower in visuals:
            results.append(entry)

    return {"results": results, "total": len(results), "query": q}


# ═══════════════════════════════════════════════════════════════════════════════
#  PAPER EDIT (timeline CRUD)
# ═══════════════════════════════════════════════════════════════════════════════


def _resolve_synth_source_files(edit: dict, synth_assets: list[dict]) -> None:
    """Replace __SYNTH__ source_file with the actual file path from synth_assets."""
    if not synth_assets:
        return
    sa_map = {
        sa["clip_id"]: sa["file_path"]
        for sa in synth_assets
        if "clip_id" in sa and "file_path" in sa
    }
    for clip in edit.get("clips", []):
        if clip.get("source_file") in ("__SYNTH__", "SYNTHESIZE", ""):
            real_path = sa_map.get(clip["clip_id"])
            if real_path and Path(real_path).exists():
                clip["source_file"] = real_path


def _resolve_synth_source_files_from_disk(edit: dict) -> None:
    """Resolve __SYNTH__ clips by scanning the .synth/ cache directory for matching clip IDs."""
    synth_dir = settings.synth_cache_dir
    if not synth_dir.exists():
        return
    # Build map: clip_id prefix → file path  (e.g. clip_001_15051649.mp4 → clip_001)
    synth_files: dict[str, Path] = {}
    for f in synth_dir.iterdir():
        if f.suffix.lower() in VIDEO_EXTENSIONS and not f.name.startswith("."):
            # Extract clip_id prefix (everything before the last _XXXXX part)
            parts = f.stem.rsplit("_", 1)
            if parts:
                synth_files[parts[0]] = f
    for clip in edit.get("clips", []):
        if clip.get("source_file") in ("__SYNTH__", "SYNTHESIZE", ""):
            match = synth_files.get(clip["clip_id"])
            if match:
                clip["source_file"] = str(match)


@app.get("/api/paper-edit", tags=["Paper Edit"])
async def get_paper_edit():
    """
    Get the current Paper Edit.

    Prefers the CRDT document (real-time, includes user edits), then falls
    back to the pipeline session state, and finally to file on disk.
    """
    # Prefer CRDT doc — this has the latest user edits synced via Yjs
    crdt_pe = crdt_mod.paper_edit_from_doc()
    if crdt_pe and crdt_pe.get("clips"):
        # Resolve synth source files if needed
        session = sessions.active if sessions else None
        if session:
            synth_assets = session.pipeline_state.get("synth_assets", []) or []
            music_path = session.pipeline_state.get("music_path")
            _resolve_synth_source_files(crdt_pe, synth_assets)
            if music_path and "music_path" not in crdt_pe:
                crdt_pe["music_path"] = music_path
        return crdt_pe

    # Fall back to session state
    session = sessions.active if sessions else None
    if session:
        music_path = session.pipeline_state.get("music_path")
        synth_assets = session.pipeline_state.get("synth_assets", []) or []
        approved = session.pipeline_state.get("approved_edit")
        if approved:
            result = dict(approved)
            if music_path:
                result["music_path"] = music_path
            _resolve_synth_source_files(result, synth_assets)
            return result
        paper = session.pipeline_state.get("paper_edit")
        if paper:
            result = dict(paper)
            if music_path:
                result["music_path"] = music_path
            _resolve_synth_source_files(result, synth_assets)
            return result

    # Fall back to file on disk
    review_path = settings.state_dir / "paper_edit_review.json"
    if review_path.exists():
        with open(review_path) as f:
            data = json.load(f)
        _resolve_synth_source_files_from_disk(data)
        return data
    raise HTTPException(404, "No Paper Edit available — run the pipeline first")


@app.put("/api/paper-edit", tags=["Paper Edit"])
async def save_paper_edit(edit: dict):
    """
    Save the entire Paper Edit (full replacement).

    With Yjs CRDT sync, the frontend normally does NOT need to call this —
    edits are synced in real-time via the /ws/crdt WebSocket.
    This endpoint is kept for backwards compatibility and as a fallback.
    """
    # Write to CRDT doc so it syncs to connected clients
    crdt_mod.load_paper_edit(edit)
    # Also save to disk for backwards compat
    review_path = settings.state_dir / "paper_edit_review.json"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with open(review_path, "w") as f:
        json.dump(edit, f, indent=2)
    return {"status": "saved"}


# NOTE: Editing is now handled via Yjs CRDT sync over /ws/crdt.
# The frontend Y.Doc is the source of truth — changes are synced in real-time
# to the backend pycrdt Doc which persists to crdt_snapshot.yjs.
# The Y.UndoManager on the frontend replaces the old structuredClone undo/redo stack.


# ═══════════════════════════════════════════════════════════════════════════════
#  OUTPUT FILES
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/output", tags=["Output"])
async def list_output_files():
    """
    List all output files (rendered videos, timelines).

    Returns download URLs for each output file.
    """
    output_dir = settings.output_dir
    if not output_dir.exists():
        return {"files": []}

    files = []
    for f in sorted(output_dir.rglob("*")):
        if not f.is_file() or f.name == ".gitkeep":
            continue
        relative_name = f.relative_to(output_dir).as_posix()
        files.append(
            {
                "file_name": relative_name,
                "file_path": str(f),
                "size_bytes": f.stat().st_size,
                "download_url": f"/api/output/{relative_name}",
                "type": f.suffix.lstrip("."),
            }
        )

    return {"files": files, "total": len(files)}


@app.get("/api/output/{filename:path}", tags=["Output"])
async def download_output(filename: str):
    """Download a rendered output file (video, timeline, etc.)."""
    file_path = settings.output_dir / filename
    # Block path traversal (e.g. filename="../../etc/passwd").
    if not _safe_within(settings.output_dir, file_path):
        raise HTTPException(403, "Access denied — path outside output directory")
    if not file_path.exists():
        raise HTTPException(404, f"Output file not found: {filename}")

    mime, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        str(file_path),
        media_type=mime or "application/octet-stream",
        filename=file_path.name,
    )


@app.get("/api/assets/stream", tags=["Assets"])
async def stream_asset_by_path(
    path: str = Query(..., description="Absolute path to the media file"),
):
    """
    Stream any media file by its absolute path.

    Used as a fallback when clip source_file doesn't match any catalogued asset.
    Only allows files within the project's media_drop or output directories.
    """
    file_path = Path(path)
    # Security: allow files under media, output, state, or any registered media ref
    media_root = settings.media_dir.resolve()
    output_root = settings.output_dir.resolve()
    state_root = settings.state_dir.resolve()
    registered_paths = set(_media_refs.values())
    try:
        resolved = file_path.resolve()
    except Exception:
        raise HTTPException(400, "Invalid path")

    allowed = (
        resolved.is_relative_to(media_root)
        or resolved.is_relative_to(output_root)
        or resolved.is_relative_to(state_root)
        or str(resolved) in registered_paths
    )
    if not allowed:
        raise HTTPException(403, "Access denied — path outside allowed directories")

    if not file_path.exists():
        raise HTTPException(404, f"File not found: {path}")

    mime, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        str(file_path),
        media_type=mime or "application/octet-stream",
        filename=file_path.name,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET (real-time pipeline events)
# ═══════════════════════════════════════════════════════════════════════════════


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket endpoint for real-time pipeline events.

    Events pushed from server → client:

    - {"type": "pipeline_started", "thread_id": "..."}
    - {"type": "phase_update", "node": "archivist", "phase": "indexed", "timestamp": "...",
    "errors": [...]}
    - {"type": "awaiting_approval", "paper_edit": {...}}

    Client → server messages:

    - {"type": "ping"} → server responds with {"type": "pong"}
    """
    await ws.accept()
    sessions.add_global_ws(ws)

    # Send current state on connect (includes timeline extras for project reload)
    s = sessions.active
    extras = _timeline_extras or {}
    await ws.send_text(
        json.dumps(
            {
                "type": "connected",
                "phase": _phase_val(s.pipeline_state.get("phase", Phase.IDLE) if s else Phase.IDLE),
                "version": __version__,
                "render_path": s.pipeline_state.get("render_path") if s else None,
                "overlay_clips": extras.get("overlay_clips", []),
                "music_path": extras.get("music_path"),
            },
            default=_json_default,
        )
    )

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        sessions.remove_global_ws(ws)


# ═══════════════════════════════════════════════════════════════════════════════
#  CRDT WEBSOCKET (Yjs document sync)
# ═══════════════════════════════════════════════════════════════════════════════


@app.websocket("/ws/crdt/{room}")
async def crdt_websocket_endpoint(ws: WebSocket, room: str = "default"):
    """
    Yjs binary sync protocol — keeps the frontend Y.Doc and the backend
    pycrdt Doc in lock-step.

    y-websocket uses a two-level encoding (lib0 varints):
        byte 0 = outer message type:
            0 = sync, 1 = awareness, 3 = queryAwareness
        For sync messages (outer=0):
            byte 1 = sync sub-type:
                0 = sync-step-1, 1 = sync-step-2, 2 = update
            remaining bytes = varint-length-prefixed payload

    On connect the server sends its full state as a sync-step-2 so the
    client catches up.  After that, incremental updates flow via sync-update
    messages.
    """
    await ws.accept()
    await crdt_mod.add_client(ws)
    connected_doc = crdt_mod.doc

    try:
        # Send the server's full state as sync-step-2 so the client catches up.
        full_update = crdt_mod.doc.get_update()
        await ws.send_bytes(crdt_mod.encode_sync_step2(full_update))

        while True:
            data = await ws.receive_bytes()
            if connected_doc is not crdt_mod.doc:
                break
            if len(data) < 1:
                continue

            outer_type, offset = crdt_mod.read_varint(data, 0)

            if outer_type == crdt_mod.MSG_SYNC:
                if offset >= len(data):
                    continue
                sync_type, offset = crdt_mod.read_varint(data, offset)

                if sync_type == crdt_mod.SYNC_STEP1:
                    # Client sends its state vector; reply with diff.
                    try:
                        state_vector, _ = crdt_mod.read_var_bytes(data, offset)
                        diff = crdt_mod.doc.get_update(state_vector)
                        await ws.send_bytes(crdt_mod.encode_sync_step2(diff))
                    except Exception:
                        # State vector invalid — send full state.
                        full = crdt_mod.doc.get_update()
                        await ws.send_bytes(crdt_mod.encode_sync_step2(full))

                elif sync_type == crdt_mod.SYNC_STEP2:
                    # Client sends its diff — apply and relay to other clients.
                    try:
                        update, _ = crdt_mod.read_var_bytes(data, offset)
                        crdt_mod.apply_ws_update(update)
                        await crdt_mod.broadcast_update(update, exclude=ws)
                    except Exception:
                        logger.warning("Failed to apply CRDT sync-step-2", exc_info=True)

                elif sync_type == crdt_mod.SYNC_UPDATE:
                    # Incremental update — apply and relay to other clients.
                    try:
                        update, _ = crdt_mod.read_var_bytes(data, offset)
                        crdt_mod.apply_ws_update(update)
                        await crdt_mod.broadcast_update(update, exclude=ws)
                    except Exception:
                        logger.warning("Failed to apply CRDT update", exc_info=True)

            elif outer_type == crdt_mod.MSG_AWARENESS:
                # Awareness — forward to other clients as-is.
                await crdt_mod.broadcast_raw(data, exclude=ws)

            # MSG_QUERY_AWARENESS / MSG_AUTH — silently ignore.

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning("CRDT WebSocket error", exc_info=True)
    finally:
        await crdt_mod.remove_client(ws)
