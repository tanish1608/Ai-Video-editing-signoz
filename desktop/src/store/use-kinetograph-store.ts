import { create } from "zustand";
import { persist } from "zustand/middleware";
import * as Y from "yjs";
import {
	Phase,
	RawAsset,
	PaperEdit,
	PipelineStatus,
	PipelineError,
	PaperEditClip,
	Track,
	TrackType,
	OverlayClip,
	AudioClip,
	OverlayTransform,
	OverlayPreset,
	OVERLAY_PRESETS,
	PreviewResolution,
	ColorGrade,
	DEFAULT_COLOR_GRADE,
} from "@/types/kinetograph";
import { revokeLocalAssetUrl } from "@/lib/local-asset";
import { resolveBackendUrl } from "@/lib/backend";
import {
	ydoc,
	yClips,
	yMeta,
	yOverlays,
	yAudio,
	undoManager,
	observeDocument,
	paperEditFromDoc,
	loadPaperEditIntoDoc,
	findClipIndex,
	findClipMap,
	clipFromYMap,
	clipToYMap,
	overlayToYMap,
	audioToYMap,
	overlaysFromDoc,
	audioFromDoc,
	loadOverlaysIntoDoc,
	findOverlayMap,
	findOverlayIndex,
	findAudioIndex,
} from "@/lib/crdt";

// ─── Default tracks ───────────────────────────────────────────────────────────

const DEFAULT_TRACKS: Track[] = [
	{ id: "V2", label: "V2", type: "video", muted: false, solo: false, volume: 1, locked: false, height: 48 },
	{ id: "V1", label: "V1", type: "video", muted: false, solo: false, volume: 1, locked: false, height: 64 },
	{ id: "A1", label: "A1", type: "audio", muted: false, solo: false, volume: 1, locked: false, height: 36 },
	{ id: "A2", label: "A2", type: "audio", muted: false, solo: false, volume: 0.35, locked: false, height: 36 },
];

interface KinetographState {
	// Core State
	phase: Phase;
	assets: RawAsset[];
	paperEdit: PaperEdit | null;
	pipelineStatus: PipelineStatus | null;
	errors: PipelineError[];

	// Multi-track
	tracks: Track[];
	musicPath: string | null;
	v2Clips: OverlayClip[];
	a2Clips: AudioClip[];

	// UI State
	selectedAssetId: string | null;
	selectedAssetIds: Set<string>;
	selectedClipId: string | null;
	selectedClipIds: Set<string>;
	selectedV2ClipId: string | null;
	playheadMs: number;
	renderUrl: string | null;
	previewResolution: PreviewResolution;
	colorGrade: ColorGrade;

	// Actions
	setPhase: (phase: Phase) => void;
	setRenderUrl: (url: string | null) => void;
	setAssets: (assets: RawAsset[]) => void;
	addAssets: (assets: RawAsset[]) => void;
	renameAsset: (assetId: string, fileName: string) => void;
	deleteAsset: (assetId: string) => void;
	setPaperEdit: (paperEdit: PaperEdit | null) => void;
	setPipelineStatus: (status: PipelineStatus | null) => void;
	addError: (error: PipelineError) => void;
	clearErrors: () => void;

	// Asset Selection
	toggleAssetSelected: (assetId: string, multi: boolean) => void;
	selectAllByType: (type: RawAsset["asset_type"]) => void;
	clearAssetSelection: () => void;
	toggleAssetType: (assetId: string) => void;
	setSelectedAssetIds: (ids: Set<string>) => void;
	deleteSelectedAssets: () => Promise<void>;

	// Timeline Clip Selection
	toggleClipSelected: (clipId: string, multi: boolean) => void;
	setSelectedClipIds: (ids: Set<string>) => void;
	clearClipSelection: () => void;
	deleteSelectedClips: () => void;

	// Timeline Actions (CRDT-backed)
	updateClip: (clipId: string, updates: Partial<PaperEditClip>) => void;
	reorderClips: (clipIds: string[]) => void;
	deleteClip: (clipId: string) => void;

	// Track Actions
	setTrackVolume: (trackId: string, volume: number) => void;
	toggleTrackMute: (trackId: string) => void;
	toggleTrackSolo: (trackId: string) => void;
	toggleTrackLock: (trackId: string) => void;
	setTrackHeight: (trackId: string, height: number) => void;
	addTrack: (type: TrackType) => string;
	removeTrack: (trackId: string) => void;
	setMusicPath: (path: string | null) => void;

	// V2 Overlay Actions
	addV2Clip: (assetId: string, trackId: string, timelineStartMs: number) => string | null;
	removeV2Clip: (clipId: string) => void;
	updateV2ClipTransform: (clipId: string, transform: Partial<OverlayTransform>) => void;
	setV2ClipPreset: (clipId: string, preset: OverlayPreset) => void;
	reorderV2Clips: (clipIds: string[]) => void;
	setSelectedV2Clip: (clipId: string | null) => void;
	trimV2Clip: (clipId: string, edge: "in" | "out", deltaMs: number) => void;
	setV2Clips: (clips: OverlayClip[]) => void;

	// A2 Audio Actions
	addA2Clip: (assetId: string, trackId: string, timelineStartMs: number) => string | null;
	removeA2Clip: (clipId: string) => void;

	// History (Yjs UndoManager)
	undo: () => void;
	redo: () => void;

	// Player Actions
	setPlayhead: (ms: number) => void;
	setSelectedAsset: (assetId: string | null) => void;
	setSelectedClip: (clipId: string | null) => void;
	setPreviewResolution: (res: PreviewResolution) => void;
	setColorGrade: (grade: Partial<ColorGrade>) => void;
	addAssetToTimeline: (assetId: string, targetTrackId?: string, timelineStartMs?: number) => string | null;
	addAssetsToTimeline: (assetIds: string[], targetTrackId?: string, timelineStartMs?: number) => string[];

	// Project lifecycle
	resetProject: () => void;
}

function createClipId() {
	if (
		typeof crypto !== "undefined" &&
		typeof crypto.randomUUID === "function"
	) {
		return `clip-${crypto.randomUUID()}`;
	}
	return `clip-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export const useKinetographStore = create<KinetographState>()(
	persist(
	(set, get) => ({
	phase: Phase.IDLE,
	assets: [],
	paperEdit: null,
	pipelineStatus: null,
	errors: [],
	tracks: DEFAULT_TRACKS,
	musicPath: null,
	v2Clips: [],
	a2Clips: [],
	selectedAssetId: null,
	selectedAssetIds: new Set<string>(),
	selectedClipId: null,
	selectedClipIds: new Set<string>(),
	selectedV2ClipId: null,
	playheadMs: 0,
	renderUrl: null,
	previewResolution: "9:16" as PreviewResolution,
	colorGrade: { ...DEFAULT_COLOR_GRADE },

	setPhase: (phase) => set({ phase }),
	setRenderUrl: (url) => set({ renderUrl: url }),
	setAssets: (assets) =>
		set((state) => ({
			assets: assets.map((a) => ({
				...a,
				stream_url: resolveBackendUrl(a.stream_url) ?? a.stream_url,
				thumbnail_url: resolveBackendUrl(a.thumbnail_url) ?? a.thumbnail_url,
			})),
			selectedAssetId:
				state.selectedAssetId &&
				!assets.some((asset) => asset.id === state.selectedAssetId)
					? null
					: state.selectedAssetId,
		})),
	addAssets: (newAssets) =>
		set((state) => ({
			assets: [...state.assets, ...newAssets.map((a) => ({
				...a,
				stream_url: resolveBackendUrl(a.stream_url) ?? a.stream_url,
				thumbnail_url: resolveBackendUrl(a.thumbnail_url) ?? a.thumbnail_url,
			}))],
		})),
	renameAsset: (assetId, fileName) =>
		set((state) => ({
			assets: state.assets.map((asset) =>
				asset.id === assetId
					? {
							...asset,
							file_name: fileName,
							file_path: fileName,
						}
					: asset,
			),
		})),
	deleteAsset: (assetId) =>
		set((state) => {
			const assetToDelete = state.assets.find((asset) => asset.id === assetId);
			if (assetToDelete?.stream_url.startsWith("blob:")) {
				revokeLocalAssetUrl(assetId);
			}

			const remainingAssets = state.assets.filter(
				(asset) => asset.id !== assetId,
			);
			return {
				assets: remainingAssets,
				selectedAssetId:
					state.selectedAssetId === assetId ? null : state.selectedAssetId,
			};
		}),

	// ─── Paper Edit (CRDT bridge) ──────────────────────────────────
	// Called when the AI pipeline or remote source produces a new paper edit.
	// Writes the full snapshot into the Yjs doc with origin='remote' so the
	// UndoManager does NOT track it.  The Yjs→Zustand observer below will
	// pick up the change and update the Zustand state reactively.
	setPaperEdit: (paperEdit) => {
		if (paperEdit) {
			loadPaperEditIntoDoc(paperEdit, "remote");
		} else {
			// Clear the doc
			ydoc.transact(() => {
				if (yClips.length > 0) yClips.delete(0, yClips.length);
				yMeta.delete("title");
				yMeta.delete("music_prompt");
				yMeta.delete("music_path");
			}, "remote");
		}
		// Eagerly update Zustand (observer will also fire, but this avoids a tick delay)
		set({
			paperEdit,
			musicPath: paperEdit?.music_path ?? get().musicPath,
		});
	},

	setPipelineStatus: (status) =>
		set({ pipelineStatus: status, phase: status?.phase ?? Phase.IDLE }),
	addError: (error) => set((state) => ({ errors: [...state.errors, error] })),
	clearErrors: () => set({ errors: [] }),

	// ─── Track Actions ─────────────────────────────────────────────
	setTrackVolume: (trackId, volume) =>
		set((state) => ({
			tracks: state.tracks.map((t) =>
				t.id === trackId ? { ...t, volume: Math.max(0, Math.min(1, volume)) } : t,
			),
		})),

	toggleTrackMute: (trackId) =>
		set((state) => ({
			tracks: state.tracks.map((t) =>
				t.id === trackId ? { ...t, muted: !t.muted } : t,
			),
		})),

	toggleTrackSolo: (trackId) =>
		set((state) => ({
			tracks: state.tracks.map((t) =>
				t.id === trackId ? { ...t, solo: !t.solo } : t,
			),
		})),

	toggleTrackLock: (trackId) =>
		set((state) => ({
			tracks: state.tracks.map((t) =>
				t.id === trackId ? { ...t, locked: !t.locked } : t,
			),
		})),

	setTrackHeight: (trackId, height) =>
		set((state) => ({
			tracks: state.tracks.map((t) =>
				t.id === trackId ? { ...t, height: Math.max(24, Math.min(200, height)) } : t,
			),
		})),

	addTrack: (type) => {
		const state = get();
		const prefix = type === "video" ? "V" : "A";
		const existing = state.tracks.filter((t) => t.type === type);
		const maxNum = existing.reduce((max, t) => {
			const n = parseInt(t.id.replace(prefix, ""), 10);
			return isNaN(n) ? max : Math.max(max, n);
		}, 0);
		const nextNum = maxNum + 1;
		const id = `${prefix}${nextNum}`;
		const newTrack: Track = {
			id,
			label: id,
			type,
			muted: false,
			solo: false,
			volume: type === "audio" ? 0.5 : 1,
			locked: false,
			height: type === "video" ? 48 : 36,
		};
		// Insert in order: video tracks at top (highest number first), audio tracks at bottom
		const videoTracks = [...state.tracks.filter((t) => t.type === "video")];
		const audioTracks = [...state.tracks.filter((t) => t.type === "audio")];
		if (type === "video") {
			videoTracks.unshift(newTrack); // Add at top (highest layer)
		} else {
			audioTracks.push(newTrack); // Add at bottom
		}
		set({ tracks: [...videoTracks, ...audioTracks] });
		return id;
	},

	removeTrack: (trackId) => {
		// Don't allow removing V1 or A1 (primary tracks)
		if (trackId === "V1" || trackId === "A1") return;
		// Delete this track's clips from the CRDT doc (undoable + persisted).
		// The Yjs→Zustand observer refreshes v2Clips/a2Clips.
		ydoc.transact(() => {
			for (let i = yOverlays.length - 1; i >= 0; i--) {
				if (yOverlays.get(i).get("trackId") === trackId) yOverlays.delete(i, 1);
			}
			for (let i = yAudio.length - 1; i >= 0; i--) {
				if (yAudio.get(i).get("trackId") === trackId) yAudio.delete(i, 1);
			}
		}, "user");
		set((state) => ({ tracks: state.tracks.filter((t) => t.id !== trackId) }));
	},

	setMusicPath: (path) => set({ musicPath: path }),

	// ─── V2 Overlay Actions (CRDT-backed — undoable + persisted) ────
	addV2Clip: (assetId, trackId, timelineStartMs) => {
		const state = get();
		const asset = state.assets.find((a) => a.id === assetId);
		if (!asset) return null;
		const id = `v2-${createClipId()}`;
		const clip: OverlayClip = {
			id,
			sourceAssetId: assetId,
			sourceFile: asset.file_name,
			inMs: 0,
			outMs: asset.duration_ms,
			timelineStartMs,
			trackId,
			transform: { ...OVERLAY_PRESETS["pip-br"] },
			preset: "pip-br",
		};
		ydoc.transact(() => { yOverlays.push([overlayToYMap(clip)]); }, "user");
		set({ selectedV2ClipId: id });
		return id;
	},

	removeV2Clip: (clipId) => {
		const idx = findOverlayIndex(clipId);
		if (idx >= 0) ydoc.transact(() => { yOverlays.delete(idx, 1); }, "user");
		set((s) => ({
			selectedV2ClipId: s.selectedV2ClipId === clipId ? null : s.selectedV2ClipId,
		}));
	},

	updateV2ClipTransform: (clipId, transform) => {
		const m = findOverlayMap(clipId);
		if (!m) return;
		ydoc.transact(() => {
			const current = (m.get("transform") as OverlayTransform) ?? OVERLAY_PRESETS["custom"];
			m.set("transform", { ...current, ...transform });
			m.set("preset", "custom");
		}, "user");
	},

	setV2ClipPreset: (clipId, preset) => {
		const m = findOverlayMap(clipId);
		if (!m) return;
		ydoc.transact(() => {
			m.set("preset", preset);
			m.set("transform", { ...OVERLAY_PRESETS[preset] });
		}, "user");
	},

	reorderV2Clips: (clipIds) => {
		ydoc.transact(() => {
			const byId = new Map(overlaysFromDoc().map((c) => [c.id, c]));
			if (yOverlays.length > 0) yOverlays.delete(0, yOverlays.length);
			for (const id of clipIds) {
				const c = byId.get(id);
				if (c) yOverlays.push([overlayToYMap(c)]);
			}
		}, "user");
	},

	setSelectedV2Clip: (clipId) => set({ selectedV2ClipId: clipId }),

	// Full replace (AI pipeline / project restore) — origin 'remote' so it is
	// not tracked by the UndoManager.
	setV2Clips: (clips) => {
		loadOverlaysIntoDoc(clips, "remote");
		set({ selectedV2ClipId: null });
	},

	trimV2Clip: (clipId, edge, deltaMs) => {
		const m = findOverlayMap(clipId);
		if (!m) return;
		ydoc.transact(() => {
			const inMs = (m.get("inMs") as number) ?? 0;
			const outMs = (m.get("outMs") as number) ?? 0;
			const MIN = 200;
			if (edge === "in") {
				const newIn = Math.max(0, inMs + deltaMs);
				m.set("inMs", outMs - newIn < MIN ? outMs - MIN : newIn);
			} else {
				m.set("outMs", Math.max(inMs + MIN, outMs + deltaMs));
			}
		}, "user");
	},

	// ─── A2 Audio Actions (CRDT-backed) ───────────────────────────
	addA2Clip: (assetId, trackId, timelineStartMs) => {
		const state = get();
		const asset = state.assets.find((a) => a.id === assetId);
		if (!asset) return null;
		const id = `a2-${createClipId()}`;
		const clip: AudioClip = {
			id,
			sourceAssetId: assetId,
			sourceFile: asset.file_name,
			inMs: 0,
			outMs: asset.duration_ms,
			timelineStartMs,
			trackId,
		};
		ydoc.transact(() => { yAudio.push([audioToYMap(clip)]); }, "user");
		return id;
	},

	removeA2Clip: (clipId) => {
		const idx = findAudioIndex(clipId);
		if (idx >= 0) ydoc.transact(() => { yAudio.delete(idx, 1); }, "user");
	},

	// ─── History (Yjs UndoManager) ────────────────────────────────
	undo: () => undoManager.undo(),
	redo: () => undoManager.redo(),

	// ─── Timeline Editing (CRDT-backed) ───────────────────────────
	updateClip: (clipId, updates) => {
		const clipMap = findClipMap(clipId);
		if (!clipMap) return;
		ydoc.transact(() => {
			for (const [key, value] of Object.entries(updates)) {
				clipMap.set(key, value);
			}
		}, "user");
		// Zustand state is updated reactively by the Yjs→Zustand observer below
	},

	reorderClips: (clipIds) => {
		ydoc.transact(() => {
			// Snapshot the current clips as plain objects
			const clipMap = new Map<string, PaperEditClip>();
			for (let i = 0; i < yClips.length; i++) {
				const c = clipFromYMap(yClips.get(i));
				clipMap.set(c.clip_id, c);
			}
			// Delete all clips
			if (yClips.length > 0) yClips.delete(0, yClips.length);
			// Re-insert in the requested order
			for (const id of clipIds) {
				const c = clipMap.get(id);
				if (!c) continue;
				yClips.push([clipToYMap(c)]);
			}
		}, "user");
	},

	deleteClip: (clipId) => {
		const idx = findClipIndex(clipId);
		if (idx < 0) return;
		ydoc.transact(() => {
			yClips.delete(idx, 1);
		}, "user");
		set((state) => {
			const nextIds = new Set(state.selectedClipIds);
			nextIds.delete(clipId);
			return {
				selectedClipId: state.selectedClipId === clipId ? null : state.selectedClipId,
				selectedClipIds: nextIds,
			};
		});
	},

	setPlayhead: (ms) => set({ playheadMs: ms }),
	setSelectedAsset: (assetId) => set({ selectedAssetId: assetId, selectedAssetIds: assetId ? new Set([assetId]) : new Set() }),
	setSelectedClip: (clipId) => set({ selectedClipId: clipId, selectedClipIds: clipId ? new Set([clipId]) : new Set() }),
	setPreviewResolution: (res) => set({ previewResolution: res }),
	setColorGrade: (grade) => set((s) => ({ colorGrade: { ...s.colorGrade, ...grade } })),

	toggleAssetSelected: (assetId, multi) =>
		set((s) => {
			if (!multi) return { selectedAssetId: assetId, selectedAssetIds: new Set([assetId]) };
			const next = new Set(s.selectedAssetIds);
			if (next.has(assetId)) { next.delete(assetId); } else { next.add(assetId); }
			return { selectedAssetId: assetId, selectedAssetIds: next };
		}),
	selectAllByType: (type) =>
		set((s) => {
			const ids = new Set(s.assets.filter((a) => a.asset_type === type).map((a) => a.id));
			return { selectedAssetIds: ids, selectedAssetId: [...ids][0] ?? null };
		}),
	clearAssetSelection: () => set({ selectedAssetIds: new Set(), selectedAssetId: null }),
	setSelectedAssetIds: (ids) => set({ selectedAssetIds: ids, selectedAssetId: [...ids][0] ?? null }),
	deleteSelectedAssets: async () => {
		const state = get();
		const ids = [...state.selectedAssetIds];
		for (const id of ids) {
			try { await (await import("@/lib/api")).KinetographAPI.deleteAsset(id); } catch { /* still remove locally */ }
			const asset = state.assets.find((a) => a.id === id);
			if (asset?.stream_url.startsWith("blob:")) revokeLocalAssetUrl(id);
		}
		set((s) => ({
			assets: s.assets.filter((a) => !state.selectedAssetIds.has(a.id)),
			selectedAssetId: null,
			selectedAssetIds: new Set(),
		}));
	},
	// asset_type is content-based (not user-togglable). Kept for interface
	// compatibility, but a genuine no-op — previously it called set({assets})
	// which notified every subscriber for nothing.
	toggleAssetType: (_assetId) => {},

	// ─── Timeline Clip Selection ──────────────────────────────────
	toggleClipSelected: (clipId, multi) =>
		set((s) => {
			if (!multi) return { selectedClipId: clipId, selectedClipIds: new Set([clipId]) };
			const next = new Set(s.selectedClipIds);
			if (next.has(clipId)) { next.delete(clipId); } else { next.add(clipId); }
			return { selectedClipId: clipId, selectedClipIds: next };
		}),
	setSelectedClipIds: (ids) => set({ selectedClipIds: ids, selectedClipId: [...ids][0] ?? null }),
	clearClipSelection: () => set({ selectedClipIds: new Set(), selectedClipId: null }),
	deleteSelectedClips: () => {
		const state = get();
		const ids = [...state.selectedClipIds];
		if (ids.length === 0) return;
		ydoc.transact(() => {
			// Delete in reverse index order to avoid shifting issues
			const indices = ids
				.map((id) => findClipIndex(id))
				.filter((i) => i >= 0)
				.sort((a, b) => b - a);
			for (const idx of indices) yClips.delete(idx, 1);
		}, "user");
		set({ selectedClipId: null, selectedClipIds: new Set() });
	},

	addAssetToTimeline: (assetId, targetTrackId, timelineStartMs = 0) => {
		const state = get();
		const asset = state.assets.find((item) => item.id === assetId);
		if (!asset) return null;

		// Route to a video overlay track (V2, V3, …)
		if (targetTrackId && targetTrackId.startsWith("V") && targetTrackId !== "V1") {
			return get().addV2Clip(assetId, targetTrackId, timelineStartMs);
		}

		// Route to an audio track (A2, A3, …)
		if (targetTrackId && targetTrackId.startsWith("A") && targetTrackId !== "A1") {
			return get().addA2Clip(assetId, targetTrackId, timelineStartMs);
		}

		const clipType: PaperEditClip["clip_type"] =
			asset.asset_type === "synth" ? "synth" : "cutaway";

		const clipId = createClipId();

		// Write to Yjs doc — the observer will update Zustand state
		ydoc.transact(() => {
			const m = new Y.Map<unknown>();
			m.set("clip_id", clipId);
			m.set("source_file", asset.file_name);
			m.set("in_ms", 0);
			m.set("out_ms", asset.duration_ms);
			m.set("clip_type", clipType);
			m.set("description", asset.file_name);
			m.set("transition", "cut");
			yClips.push([m]);

			// Ensure metadata exists
			if (!yMeta.get("title")) {
				yMeta.set("title", "Untitled Sequence");
			}
		}, "user");

		return clipId;
	},

	addAssetsToTimeline: (assetIds, targetTrackId, timelineStartMs = 0) => {
		const results: string[] = [];
		for (const assetId of assetIds) {
			const id = get().addAssetToTimeline(assetId, targetTrackId, timelineStartMs);
			if (id) results.push(id);
		}
		return results;
	},

	resetProject: () => {
		// Revoke any object URLs to avoid memory leaks
		const currentAssets = get().assets;
		for (const a of currentAssets) {
			revokeLocalAssetUrl(a.id);
		}
		set({
			phase: Phase.IDLE,
			assets: [],
			paperEdit: null,
			pipelineStatus: null,
			errors: [],
			tracks: DEFAULT_TRACKS,
			musicPath: null,
			v2Clips: [],
			a2Clips: [],
			selectedAssetId: null,
			selectedAssetIds: new Set<string>(),
			selectedClipId: null,
			selectedClipIds: new Set<string>(),
			selectedV2ClipId: null,
			playheadMs: 0,
			renderUrl: null,
			previewResolution: "9:16" as PreviewResolution,
			colorGrade: { ...DEFAULT_COLOR_GRADE },
		});
	},
}),
	{
		name: "kinetograph-timeline",
		// Persist only UI preferences that are NOT project-specific.
		// V1 clips / paperEdit are handled by the Yjs CRDT WebSocket sync.
		// V2 clips, music, etc. are now persisted per-project on the backend
		// (timeline_extras.json) and restored via the WS connected event.
		partialize: (state) => ({
			tracks: state.tracks,
			colorGrade: state.colorGrade,
			previewResolution: state.previewResolution,
		}),
	},
));


// ─── Yjs → Zustand reactive bridge ────────────────────────────────────────────
// Whenever the Yjs document changes (local edit, remote sync, undo/redo),
// derive a fresh PaperEdit snapshot and push it into Zustand so React re-renders.

function _syncYjsToZustand() {
	const pe = paperEditFromDoc();
	const updates: Partial<KinetographState> = {
		paperEdit: pe,
		musicPath: (yMeta.get("music_path") as string) ?? null,
		v2Clips: overlaysFromDoc(),
		a2Clips: audioFromDoc(),
	};
	// When all clips are deleted, clear the stale rendered video so the
	// preview doesn't show the last played frame.
	if (!pe || pe.clips.length === 0) {
		updates.renderUrl = null;
	}
	useKinetographStore.setState(updates);
}

observeDocument(_syncYjsToZustand);
