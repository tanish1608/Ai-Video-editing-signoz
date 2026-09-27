/**
 * CRDT layer — shared Yjs document for the Paper Edit.
 *
 * The Y.Doc is the single source of truth for timeline editing.
 * A WebsocketProvider (y-websocket) syncs it with the backend's pycrdt Doc,
 * giving us real-time AI co-editing, persistence, and offline-resilient sync.
 *
 * Schema:
 *   doc.getArray('clips')   → Y.Array<Y.Map>  (ordered timeline clips)
 *   doc.getMap('meta')       → Y.Map            (title, music_prompt, music_path)
 */

import * as Y from "yjs";
import { WebsocketProvider } from "y-websocket";
import type {
	PaperEdit,
	PaperEditClip,
	TransitionType,
	ClipType,
	OverlayClip,
	AudioClip,
	OverlayTransform,
	OverlayPreset,
} from "@/types/kinetograph";
import { getBackendUrlSync } from "./backend";

// ─── Shared document ──────────────────────────────────────────────────────────

export let ydoc = new Y.Doc();

/** Ordered timeline clips — each element is a Y.Map with PaperEditClip fields. */
export let yClips = ydoc.getArray<Y.Map<unknown>>("clips");

/** Sequence metadata: title, music_prompt, music_path. */
export let yMeta = ydoc.getMap<unknown>("meta");

/** V2/V3… video overlay clips (PiP), each a Y.Map with a `trackId`. */
export let yOverlays = ydoc.getArray<Y.Map<unknown>>("overlays");

/** A2/A3… audio clips, each a Y.Map with a `trackId`. */
export let yAudio = ydoc.getArray<Y.Map<unknown>>("audio");

// ─── Undo / Redo ──────────────────────────────────────────────────────────────

/**
 * UndoManager scoped to clips + meta + overlays + audio, tracking only
 * user-originated changes. AI pipeline changes arrive from the backend
 * (different client-id) and are excluded automatically. Use origin = 'user'
 * for local edits.
 */
export let undoManager = new Y.UndoManager([yClips, yMeta, yOverlays, yAudio], {
	trackedOrigins: new Set(["user"]),
	captureTimeout: 500, // group changes within 500 ms
});

// ─── WebSocket Provider ───────────────────────────────────────────────────────

let _provider: WebsocketProvider | null = null;

/**
 * Connect the y-websocket provider to the backend CRDT endpoint.
 * Safe to call multiple times — returns the existing provider if already connected.
 */
export function connectProvider(): WebsocketProvider {
	if (_provider) return _provider;

	const wsUrl = getBackendUrlSync().replace(/^http/, "ws");

	_provider = new WebsocketProvider(
		`${wsUrl}/ws/crdt`,
		"paper-edit",
		ydoc,
		{
			connect: true,
			disableBc: true, // single-tab desktop app
			resyncInterval: 5000, // safety-net re-sync
		},
	);

	const provider = _provider;
	provider.on("connection-close", (event: CloseEvent | null) => {
		if (event?.code === 4001) provider.disconnect();
	});
	return provider;
}

/** Disconnect and destroy the provider (e.g. on unmount / page leave). */
export function disconnectProvider(): void {
	if (_provider) {
		_provider.disconnect();
		_provider.destroy();
		_provider = null;
	}
}

/**
 * Clear all CRDT data (clips + metadata) from the local Yjs document.
 *
 * Called before switching projects so the old timeline doesn't bleed
 * into the new project.  After clearing, ``connectProvider()`` will
 * pull the correct state from the backend via sync-step-2.
 */
const documentListeners = new Set<() => void>();

function notifyDocumentListeners(): void {
	for (const listener of documentListeners) listener();
}
ydoc.on("afterTransaction", notifyDocumentListeners);

export function observeDocument(listener: () => void): () => void {
	documentListeners.add(listener);
	return () => documentListeners.delete(listener);
}

export function resetDoc(): void {
	disconnectProvider();
	undoManager.destroy();
	ydoc.destroy();
	ydoc = new Y.Doc();
	yClips = ydoc.getArray<Y.Map<unknown>>("clips");
	yMeta = ydoc.getMap<unknown>("meta");
	yOverlays = ydoc.getArray<Y.Map<unknown>>("overlays");
	yAudio = ydoc.getArray<Y.Map<unknown>>("audio");
	undoManager = new Y.UndoManager([yClips, yMeta, yOverlays, yAudio], {
		trackedOrigins: new Set(["user"]),
		captureTimeout: 500,
	});
	ydoc.on("afterTransaction", notifyDocumentListeners);
	notifyDocumentListeners();
}

/** Whether the initial CRDT sync with the backend has completed. */
export function isProviderSynced(): boolean {
	return _provider?.synced ?? false;
}

// ─── Yjs → JSON helpers ──────────────────────────────────────────────────────

/** Convert a single Y.Map clip to a plain PaperEditClip object. */
export function clipFromYMap(m: Y.Map<unknown>): PaperEditClip {
	return {
		clip_id: (m.get("clip_id") as string) ?? "",
		source_file: (m.get("source_file") as string) ?? "",
		in_ms: (m.get("in_ms") as number) ?? 0,
		out_ms: (m.get("out_ms") as number) ?? 0,
		clip_type: (m.get("clip_type") as ClipType) ?? "cutaway",
		description: (m.get("description") as string) ?? "",
		transition: (m.get("transition") as TransitionType) ?? "cut",
		transition_duration_ms: m.get("transition_duration_ms") as
			| number
			| undefined,
		overlay_text: m.get("overlay_text") as string | undefined,
		search_query: m.get("search_query") as string | undefined,
	};
}

/**
 * Derive a PaperEdit JSON snapshot from the current Yjs document.
 * Returns null if the document is empty (no clips AND no title).
 */
export function paperEditFromDoc(): PaperEdit | null {
	if (yClips.length === 0 && !yMeta.get("title")) return null;

	const clips: PaperEditClip[] = [];
	for (let i = 0; i < yClips.length; i++) {
		clips.push(clipFromYMap(yClips.get(i)));
	}

	const totalDurationMs = clips.reduce(
		(sum, c) => sum + (c.out_ms - c.in_ms),
		0,
	);

	return {
		title: (yMeta.get("title") as string) ?? "Untitled Sequence",
		total_duration_ms: totalDurationMs,
		clips,
		music_prompt: yMeta.get("music_prompt") as string | undefined,
		music_path: yMeta.get("music_path") as string | undefined,
	};
}

// ─── JSON → Yjs helpers ──────────────────────────────────────────────────────

/** Create a Y.Map representing a single PaperEditClip. */
export function clipToYMap(clip: PaperEditClip): Y.Map<unknown> {
	const m = new Y.Map<unknown>();
	m.set("clip_id", clip.clip_id);
	m.set("source_file", clip.source_file);
	m.set("in_ms", clip.in_ms);
	m.set("out_ms", clip.out_ms);
	m.set("clip_type", clip.clip_type);
	m.set("description", clip.description);
	if (clip.transition) m.set("transition", clip.transition);
	if (clip.transition_duration_ms !== undefined)
		m.set("transition_duration_ms", clip.transition_duration_ms);
	if (clip.overlay_text) m.set("overlay_text", clip.overlay_text);
	if (clip.search_query) m.set("search_query", clip.search_query);
	return m;
}

/**
 * Overwrite the Yjs document with a full PaperEdit snapshot.
 * Used when the AI pipeline produces a new edit or when loading a project.
 *
 * @param origin - Yjs transaction origin.  Use 'user' to make it undoable,
 *                 'remote' (default) for AI / load operations.
 */
export function loadPaperEditIntoDoc(
	pe: PaperEdit,
	origin: string = "remote",
): void {
	ydoc.transact(() => {
		// Clear existing clips
		if (yClips.length > 0) yClips.delete(0, yClips.length);

		// Metadata
		yMeta.set("title", pe.title);
		if (pe.music_prompt !== undefined) yMeta.set("music_prompt", pe.music_prompt);
		else yMeta.delete("music_prompt");
		if (pe.music_path !== undefined) yMeta.set("music_path", pe.music_path);
		else yMeta.delete("music_path");

		// Clips
		for (const clip of pe.clips) {
			yClips.push([clipToYMap(clip)]);
		}
	}, origin);
}

// ─── Overlay (V2/V3…) clip converters ─────────────────────────────────────────

export function overlayToYMap(clip: OverlayClip): Y.Map<unknown> {
	const m = new Y.Map<unknown>();
	m.set("id", clip.id);
	m.set("sourceAssetId", clip.sourceAssetId);
	m.set("sourceFile", clip.sourceFile);
	m.set("inMs", clip.inMs);
	m.set("outMs", clip.outMs);
	m.set("timelineStartMs", clip.timelineStartMs);
	m.set("trackId", clip.trackId);
	m.set("preset", clip.preset);
	// transform is stored as a plain object value (updated atomically).
	m.set("transform", { ...clip.transform });
	return m;
}

export function overlayFromYMap(m: Y.Map<unknown>): OverlayClip {
	return {
		id: (m.get("id") as string) ?? "",
		sourceAssetId: (m.get("sourceAssetId") as string) ?? "",
		sourceFile: (m.get("sourceFile") as string) ?? "",
		inMs: (m.get("inMs") as number) ?? 0,
		outMs: (m.get("outMs") as number) ?? 0,
		timelineStartMs: (m.get("timelineStartMs") as number) ?? 0,
		trackId: (m.get("trackId") as string) ?? "V2",
		preset: (m.get("preset") as OverlayPreset) ?? "pip-br",
		transform: (m.get("transform") as OverlayTransform),
	};
}

export function overlaysFromDoc(): OverlayClip[] {
	const out: OverlayClip[] = [];
	for (let i = 0; i < yOverlays.length; i++) out.push(overlayFromYMap(yOverlays.get(i)));
	return out;
}

/** Replace all overlay clips in the doc (used by AI/restore). */
export function loadOverlaysIntoDoc(clips: OverlayClip[], origin: string = "remote"): void {
	ydoc.transact(() => {
		if (yOverlays.length > 0) yOverlays.delete(0, yOverlays.length);
		for (const c of clips) yOverlays.push([overlayToYMap(c)]);
	}, origin);
}

// ─── Audio (A2/A3…) clip converters ───────────────────────────────────────────

export function audioToYMap(clip: AudioClip): Y.Map<unknown> {
	const m = new Y.Map<unknown>();
	m.set("id", clip.id);
	m.set("sourceAssetId", clip.sourceAssetId);
	m.set("sourceFile", clip.sourceFile);
	m.set("inMs", clip.inMs);
	m.set("outMs", clip.outMs);
	m.set("timelineStartMs", clip.timelineStartMs);
	m.set("trackId", clip.trackId);
	return m;
}

export function audioFromYMap(m: Y.Map<unknown>): AudioClip {
	return {
		id: (m.get("id") as string) ?? "",
		sourceAssetId: (m.get("sourceAssetId") as string) ?? "",
		sourceFile: (m.get("sourceFile") as string) ?? "",
		inMs: (m.get("inMs") as number) ?? 0,
		outMs: (m.get("outMs") as number) ?? 0,
		timelineStartMs: (m.get("timelineStartMs") as number) ?? 0,
		trackId: (m.get("trackId") as string) ?? "A2",
	};
}

export function audioFromDoc(): AudioClip[] {
	const out: AudioClip[] = [];
	for (let i = 0; i < yAudio.length; i++) out.push(audioFromYMap(yAudio.get(i)));
	return out;
}

// ─── Lookup helpers ───────────────────────────────────────────────────────────

/** Find the array index of a clip by its clip_id. Returns -1 if not found. */
export function findClipIndex(clipId: string): number {
	for (let i = 0; i < yClips.length; i++) {
		if (yClips.get(i).get("clip_id") === clipId) return i;
	}
	return -1;
}

/** Find the Y.Map for a clip by its clip_id. Returns null if not found. */
export function findClipMap(clipId: string): Y.Map<unknown> | null {
	const idx = findClipIndex(clipId);
	return idx >= 0 ? yClips.get(idx) : null;
}

/** Find the Y.Map for an overlay/audio clip by its id in the given array. */
function _findById(arr: Y.Array<Y.Map<unknown>>, id: string): Y.Map<unknown> | null {
	for (let i = 0; i < arr.length; i++) {
		if (arr.get(i).get("id") === id) return arr.get(i);
	}
	return null;
}

export function findOverlayMap(id: string): Y.Map<unknown> | null {
	return _findById(yOverlays, id);
}

export function findOverlayIndex(id: string): number {
	for (let i = 0; i < yOverlays.length; i++) if (yOverlays.get(i).get("id") === id) return i;
	return -1;
}

export function findAudioIndex(id: string): number {
	for (let i = 0; i < yAudio.length; i++) if (yAudio.get(i).get("id") === id) return i;
	return -1;
}
