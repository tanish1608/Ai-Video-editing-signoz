import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import {
	DndContext,
	closestCenter,
	PointerSensor,
	KeyboardSensor,
	useSensor,
	useSensors,
	DragEndEvent,
	DragOverlay,
	DragStartEvent,
} from "@dnd-kit/core";
import {
	arrayMove,
	SortableContext,
	sortableKeyboardCoordinates,
	horizontalListSortingStrategy,
} from "@dnd-kit/sortable";
import { useKinetographStore } from "@/store/use-kinetograph-store";
import { useMarqueeSelect } from "@/hooks/use-marquee-select";
import { TimelineClip } from "./timeline-clip";
import { PaperEditClip, PaperEdit, TransitionType, OverlayClip, OverlayPreset, OVERLAY_PRESETS, Track } from "@/types/kinetograph";
import { loadPaperEditIntoDoc } from "@/lib/crdt";
import {
	Save,
	Trash2,
	Loader2,
	ZoomIn,
	ZoomOut,
	Scissors,
	XCircle,
	MousePointerClick,
	Volume2,
	VolumeX,
	Layers,
	Plus,
	Lock,
	Unlock,
	Eye,
	EyeOff,
	ChevronDown,
	Crop,
	GripVertical,
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";

// ─── Constants ──────────────────────────────────────────────────────────────────

const FPS = 30;
const BASE_PIXELS_PER_SECOND = 14;
const MIN_ZOOM_LEVEL = 0.25;
const MAX_ZOOM_LEVEL = 6;
const MIN_LABEL_SPACING_PX = 90;
const MIN_TIMELINE_SECONDS = 60;
const TRAILING_PADDING_SECONDS = 30;
const TRACK_HEADER_WIDTH_PX = 64;
const DEFAULT_TRANSITION_DURATION_MS = 200;

const TRANSITION_OPTIONS: { value: TransitionType; label: string; shortLabel: string }[] = [
	{ value: "cut", label: "Cut", shortLabel: "Cut" },
	{ value: "crossfade", label: "Crossfade", shortLabel: "XFade" },
	{ value: "dissolve", label: "Dissolve", shortLabel: "Diss" },
	{ value: "fade-to-black", label: "Fade to Black", shortLabel: "F→B" },
	{ value: "fade-to-white", label: "Fade to White", shortLabel: "F→W" },
	{ value: "wipe-left", label: "Wipe Left", shortLabel: "W←" },
	{ value: "wipe-right", label: "Wipe Right", shortLabel: "W→" },
	{ value: "slide-left", label: "Slide Left", shortLabel: "S←" },
	{ value: "slide-right", label: "Slide Right", shortLabel: "S→" },
];

const OVERLAY_PRESET_OPTIONS: { value: OverlayPreset; label: string; icon: string }[] = [
	{ value: "pip-br", label: "PiP Bottom-Right", icon: "◳" },
	{ value: "pip-bl", label: "PiP Bottom-Left", icon: "◲" },
	{ value: "pip-tr", label: "PiP Top-Right", icon: "◱" },
	{ value: "pip-tl", label: "PiP Top-Left", icon: "◰" },
	{ value: "pip-center", label: "PiP Center", icon: "◻" },
	{ value: "side-by-side", label: "Side by Side", icon: "▐" },
	{ value: "custom", label: "Custom", icon: "✦" },
];

const TRANSITION_CUT_PX = 12;
const TRANSITION_ACTIVE_PX = 40;
const TRANSITION_SELECTED_PX = 52;

// ─── Utility functions ──────────────────────────────────────────────────────────

function getTransitionWidthPx(
	clip: PaperEditClip,
	index: number,
	selectedTransitionClipId: string | null,
): number {
	if (index <= 0) return 0;
	if (selectedTransitionClipId === clip.clip_id) return TRANSITION_SELECTED_PX;
	if (clip.transition && clip.transition !== "cut") return TRANSITION_ACTIVE_PX;
	return TRANSITION_CUT_PX;
}

function getTransitionOffsetAtMs(
	playheadMs: number,
	clips: PaperEditClip[],
	selectedTransitionClipId: string | null,
): number {
	let cursor = 0;
	let offsetPx = 0;
	for (let i = 0; i < clips.length; i++) {
		const trPx = getTransitionWidthPx(clips[i], i, selectedTransitionClipId);
		const dur = clips[i].out_ms - clips[i].in_ms;
		if (playheadMs <= cursor) break;
		offsetPx += trPx;
		cursor += dur;
	}
	return offsetPx;
}

function getTotalTransitionPx(
	clips: PaperEditClip[],
	selectedTransitionClipId: string | null,
): number {
	let total = 0;
	for (let i = 1; i < clips.length; i++) {
		total += getTransitionWidthPx(clips[i], i, selectedTransitionClipId);
	}
	return total;
}

function pixelToMs(
	xPx: number,
	pxPerSecond: number,
	clips: PaperEditClip[],
	selectedTransitionClipId: string | null,
): number {
	let cursor = 0;
	let pixelCursor = 0;
	for (let i = 0; i < clips.length; i++) {
		const trPx = getTransitionWidthPx(clips[i], i, selectedTransitionClipId);
		if (xPx < pixelCursor + trPx) return cursor;
		pixelCursor += trPx;
		const dur = clips[i].out_ms - clips[i].in_ms;
		const clipPx = (dur / 1000) * pxPerSecond;
		if (xPx < pixelCursor + clipPx) {
			return cursor + ((xPx - pixelCursor) / pxPerSecond) * 1000;
		}
		pixelCursor += clipPx;
		cursor += dur;
	}
	return cursor + ((xPx - pixelCursor) / pxPerSecond) * 1000;
}

function clamp(value: number, min: number, max: number) {
	return Math.min(max, Math.max(min, value));
}

function formatTimecode(ms: number) {
	const totalFrames = Math.max(0, Math.floor((ms / 1000) * FPS));
	const frames = totalFrames % FPS;
	const totalSeconds = Math.floor(totalFrames / FPS);
	const seconds = totalSeconds % 60;
	const minutes = Math.floor(totalSeconds / 60) % 60;
	const hours = Math.floor(totalSeconds / 3600);
	return `${hours.toString().padStart(2, "0")}:${minutes.toString().padStart(2, "0")}:${seconds
		.toString()
		.padStart(2, "0")}:${frames.toString().padStart(2, "0")}`;
}

function getTickStepSeconds(pxPerSecond: number) {
	const options = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
	return options.find((s) => s * pxPerSecond >= MIN_LABEL_SPACING_PX) ?? options[options.length - 1];
}

// ─── Sub-components ─────────────────────────────────────────────────────────────

/** Deterministic pseudo-waveform bars for audio tracks */
function AudioWaveformBars({ clipId, width }: { clipId: string; width: number }) {
	const bars = useMemo(() => {
		let hash = 0;
		for (let i = 0; i < clipId.length; i++) {
			hash = ((hash << 5) - hash + clipId.charCodeAt(i)) | 0;
		}
		const count = Math.max(6, Math.min(Math.floor(width / 3), 120));
		return Array.from({ length: count }, (_, i) => {
			const s = Math.abs((hash * (i + 1) * 2654435761) | 0) % 1000;
			return 0.12 + (s / 1000) * 0.88;
		});
	}, [clipId, width]);

	return (
		<>
			{bars.map((h, i) => (
				<div
					key={i}
					className="w-[2px] rounded-full shrink-0 bg-teal-400/30 group-hover:bg-teal-400/50 transition-colors"
					style={{ height: `${h * 100}%` }}
				/>
			))}
		</>
	);
}

/** Track header with label, mute, solo, lock controls */
function TrackHeader({
	track,
	onToggleMute,
	onToggleSolo,
	onToggleLock,
	onVolumeChange,
	onRemove,
	canRemove,
}: {
	track: Track;
	onToggleMute: () => void;
	onToggleSolo: () => void;
	onToggleLock: () => void;
	onVolumeChange: (v: number) => void;
	onRemove: () => void;
	canRemove: boolean;
}) {
	const [showVolumeSlider, setShowVolumeSlider] = useState(false);
	const isAudio = track.type === "audio";
	const labelColor = track.type === "video"
		? (track.id === "V1" ? "text-blue-400" : "text-amber-400")
		: (track.id === "A1" ? "text-teal-400" : "text-violet-400");

	return (
		<div
			className="flex flex-col justify-center gap-0.5 px-1.5 border-r border-zinc-800 bg-zinc-900/80 select-none relative group/header"
			style={{ width: `${TRACK_HEADER_WIDTH_PX}px`, minWidth: `${TRACK_HEADER_WIDTH_PX}px` }}
			onMouseEnter={() => isAudio && setShowVolumeSlider(true)}
			onMouseLeave={() => setShowVolumeSlider(false)}
		>
			{/* Track label + remove */}
			<div className="flex items-center gap-1">
				<span className={cn("text-[9px] font-bold tracking-wide", labelColor)}>
					{track.label}
				</span>
				{canRemove && (
					<button
						onClick={onRemove}
						className="opacity-0 group-hover/header:opacity-100 p-0.5 text-zinc-600 hover:text-red-400 transition-all ml-auto"
						title="Remove track"
					>
						<XCircle className="h-2.5 w-2.5" />
					</button>
				)}
			</div>

			{/* Controls: M S L */}
			<div className="flex items-center gap-0.5">
				<button
					onClick={onToggleMute}
					className={cn(
						"w-4 h-3.5 rounded flex items-center justify-center transition-colors text-[7px] font-bold",
						track.muted
							? "text-red-400 bg-red-400/15"
							: "text-zinc-600 hover:text-zinc-400 hover:bg-zinc-800",
					)}
					title={track.muted ? "Unmute" : "Mute"}
				>
					M
				</button>
				<button
					onClick={onToggleSolo}
					className={cn(
						"w-4 h-3.5 rounded flex items-center justify-center transition-colors text-[7px] font-bold",
						track.solo
							? "text-yellow-400 bg-yellow-400/15"
							: "text-zinc-600 hover:text-zinc-400 hover:bg-zinc-800",
					)}
					title={track.solo ? "Unsolo" : "Solo"}
				>
					S
				</button>
				<button
					onClick={onToggleLock}
					className={cn(
						"w-4 h-3.5 rounded flex items-center justify-center transition-colors",
						track.locked
							? "text-orange-400 bg-orange-400/15"
							: "text-zinc-600 hover:text-zinc-400 hover:bg-zinc-800",
					)}
					title={track.locked ? "Unlock" : "Lock"}
				>
					{track.locked ? <Lock className="h-2 w-2" /> : <Unlock className="h-2 w-2" />}
				</button>
			</div>

			{/* Volume slider popover for audio tracks */}
			{isAudio && showVolumeSlider && (
				<div
					className="absolute left-full top-1/2 -translate-y-1/2 ml-1 z-50 bg-zinc-900 border border-zinc-700 rounded-md p-2 shadow-xl flex items-center gap-2 min-w-[120px]"
					onPointerDown={(e) => e.stopPropagation()}
				>
					<input
						type="range"
						min={0}
						max={100}
						step={1}
						value={Math.round(track.volume * 100)}
						onChange={(e) => onVolumeChange(Number(e.target.value) / 100)}
						className="w-16 accent-blue-500 h-1 bg-zinc-800 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-2.5 [&::-webkit-slider-thumb]:h-2.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-blue-500"
					/>
					<span className="text-[8px] font-mono text-zinc-400 min-w-[28px] text-right">
						{Math.round(track.volume * 100)}%
					</span>
				</div>
			)}
		</div>
	);
}

/** Timecode ruler */
function TimeRuler({
	timelineWidthPx,
	pxPerSecond,
	tickStepSeconds,
	onSeek,
	clips,
	selectedTransitionClipId,
}: {
	timelineWidthPx: number;
	pxPerSecond: number;
	tickStepSeconds: number;
	onSeek?: (ms: number) => void;
	clips?: PaperEditClip[];
	selectedTransitionClipId?: string | null;
}) {
	const ticks = useMemo(() => {
		const totalSeconds = timelineWidthPx / pxPerSecond;
		const count = Math.floor(totalSeconds / tickStepSeconds) + 1;
		return Array.from({ length: count }, (_, i) => i * tickStepSeconds);
	}, [pxPerSecond, tickStepSeconds, timelineWidthPx]);

	const handleClick = (e: React.MouseEvent<HTMLDivElement>) => {
		if (!onSeek) return;
		const rect = e.currentTarget.getBoundingClientRect();
		const x = e.clientX - rect.left - TRACK_HEADER_WIDTH_PX;
		const ms = clips && clips.length > 0
			? pixelToMs(x, pxPerSecond, clips, selectedTransitionClipId ?? null)
			: (x / pxPerSecond) * 1000;
		onSeek(Math.max(0, ms));
	};

	return (
		<div
			className="relative h-6 border-b border-zinc-800 bg-zinc-900/30 min-w-full cursor-pointer sticky top-0 z-20"
			style={{ width: `${TRACK_HEADER_WIDTH_PX + timelineWidthPx}px` }}
			onClick={handleClick}
		>
			{/* Header corner */}
			<div
				className="absolute top-0 left-0 bottom-0 bg-zinc-900/80 border-r border-zinc-800 z-10"
				style={{ width: `${TRACK_HEADER_WIDTH_PX}px` }}
			/>
			{ticks.map((seconds) => {
				const trOffsetPx = clips && clips.length > 0
					? getTransitionOffsetAtMs(seconds * 1000, clips, selectedTransitionClipId ?? null)
					: 0;
				return (
					<div
						key={seconds}
						className="absolute top-0 bottom-0 border-l border-zinc-800/50 pl-1"
						style={{ left: `${TRACK_HEADER_WIDTH_PX + seconds * pxPerSecond + trOffsetPx}px` }}
					>
						<span className="text-[8px] font-mono text-zinc-600">
							{formatTimecode(seconds * 1000)}
						</span>
					</div>
				);
			})}
		</div>
	);
}

// ─── Main component ─────────────────────────────────────────────────────────────

interface TimelineEditorProps {
	onSeek?: (ms: number) => void;
}

export function TimelineEditor({ onSeek }: TimelineEditorProps) {
	const paperEdit = useKinetographStore((s) => s.paperEdit);
	const reorderClips = useKinetographStore((s) => s.reorderClips);
	const addAssetToTimeline = useKinetographStore((s) => s.addAssetToTimeline);
	const addAssetsToTimeline = useKinetographStore((s) => s.addAssetsToTimeline);
	const selectedClipId = useKinetographStore((s) => s.selectedClipId);
	const selectedClipIds = useKinetographStore((s) => s.selectedClipIds);
	const setSelectedClip = useKinetographStore((s) => s.setSelectedClip);
	const toggleClipSelected = useKinetographStore((s) => s.toggleClipSelected);
	const setSelectedClipIds = useKinetographStore((s) => s.setSelectedClipIds);
	const deleteSelectedClips = useKinetographStore((s) => s.deleteSelectedClips);
	const deleteClip = useKinetographStore((s) => s.deleteClip);
	const updateClip = useKinetographStore((s) => s.updateClip);
	const playheadMs = useKinetographStore((s) => s.playheadMs);
	const tracks = useKinetographStore((s) => s.tracks);
	const musicPath = useKinetographStore((s) => s.musicPath);
	const setTrackVolume = useKinetographStore((s) => s.setTrackVolume);
	const toggleTrackMute = useKinetographStore((s) => s.toggleTrackMute);
	const toggleTrackSolo = useKinetographStore((s) => s.toggleTrackSolo);
	const toggleTrackLock = useKinetographStore((s) => s.toggleTrackLock);
	const addTrack = useKinetographStore((s) => s.addTrack);
	const removeTrack = useKinetographStore((s) => s.removeTrack);

	// V2 overlay
	const v2Clips = useKinetographStore((s) => s.v2Clips);
	const removeV2Clip = useKinetographStore((s) => s.removeV2Clip);
	const selectedV2ClipId = useKinetographStore((s) => s.selectedV2ClipId);
	const setSelectedV2Clip = useKinetographStore((s) => s.setSelectedV2Clip);
	const updateV2ClipTransform = useKinetographStore((s) => s.updateV2ClipTransform);
	const setV2ClipPreset = useKinetographStore((s) => s.setV2ClipPreset);

	// A2
	const a2Clips = useKinetographStore((s) => s.a2Clips);

	// ── UI State ───────────────────────────────────────────────────
	const [zoomLevel, setZoomLevel] = useState(1);
	const trackViewportRef = useRef<HTMLDivElement>(null);
	const v1ContainerRef = useRef<HTMLDivElement>(null);
	const [trackViewportWidth, setTrackViewportWidth] = useState(0);
	const [isDraggingPlayhead, setIsDraggingPlayhead] = useState(false);
	const [activeDragId, setActiveDragId] = useState<string | null>(null);
	const [selectedTransitionClipId, setSelectedTransitionClipId] = useState<string | null>(null);
	const [showAddTrackMenu, setShowAddTrackMenu] = useState(false);
	const [dropTargetTrackId, setDropTargetTrackId] = useState<string | null>(null);
	const lastClickedClipRef = useRef<string | null>(null);

	// ── Enterprise playhead: direct DOM refs ──────────────────────
	const playheadElRef = useRef<HTMLDivElement>(null);
	const isDraggingPlayheadRef = useRef(false);

	// Inspector
	const [inspectorDescription, setInspectorDescription] = useState("");
	const [inspectorTransition, setInspectorTransition] = useState<TransitionType>("cut");
	const [isSavingInspector, setIsSavingInspector] = useState(false);

	const sensors = useSensors(
		useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
		useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
	);

	const selectedClip = paperEdit?.clips.find((c) => c.clip_id === selectedClipId);
	const selectedV2Clip = v2Clips.find((c) => c.id === selectedV2ClipId);
	const transitionClip = selectedTransitionClipId ? paperEdit?.clips.find((c) => c.clip_id === selectedTransitionClipId) ?? null : null;
	const transitionClipIndex = selectedTransitionClipId && paperEdit ? paperEdit.clips.findIndex((c) => c.clip_id === selectedTransitionClipId) : -1;
	const prevTransitionClip = transitionClipIndex > 0 && paperEdit ? paperEdit.clips[transitionClipIndex - 1] ?? null : null;

	// Split tracks into video (top) and audio (bottom)
	const videoTracks = useMemo(() => tracks.filter((t) => t.type === "video"), [tracks]);
	const audioTracks = useMemo(() => tracks.filter((t) => t.type === "audio"), [tracks]);

	// ── Marquee selection on V1 track ──────────────────────────────
	const { marquee: v1Marquee, handlers: v1MarqueeHandlers } = useMarqueeSelect({
		containerRef: v1ContainerRef,
		itemSelector: "[data-clip-id]",
		idAttribute: "data-clip-id",
		onSelectionChange: setSelectedClipIds,
		shouldStart: (e) => {
			const t = e.target as HTMLElement;
			return !t.closest("button") && !t.closest("[data-trim-handle]");
		},
	});

	// ── Clip click with shift-range + cmd-multi ────────────────────
	const handleClipClick = useCallback(
		(e: React.MouseEvent, clipId: string) => {
			setSelectedTransitionClipId(null);
			setSelectedV2Clip(null);

			if (e.shiftKey && lastClickedClipRef.current && paperEdit) {
				const lastIdx = paperEdit.clips.findIndex((c) => c.clip_id === lastClickedClipRef.current);
				const thisIdx = paperEdit.clips.findIndex((c) => c.clip_id === clipId);
				if (lastIdx >= 0 && thisIdx >= 0) {
					const lo = Math.min(lastIdx, thisIdx);
					const hi = Math.max(lastIdx, thisIdx);
					const rangeIds = new Set(
						paperEdit.clips.slice(lo, hi + 1).map((c) => c.clip_id),
					);
					if (e.metaKey || e.ctrlKey) {
						const merged = new Set(selectedClipIds);
						for (const id of rangeIds) merged.add(id);
						setSelectedClipIds(merged);
					} else {
						setSelectedClipIds(rangeIds);
					}
					return;
				}
			}
			lastClickedClipRef.current = clipId;
			toggleClipSelected(clipId, e.metaKey || e.ctrlKey);
		},
		[paperEdit, selectedClipIds, setSelectedClipIds, toggleClipSelected, setSelectedV2Clip],
	);

	useEffect(() => {
		if (selectedClip) {
			setInspectorDescription(selectedClip.description || "");
			setInspectorTransition(selectedClip.transition || "cut");
		}
	}, [selectedClip?.clip_id]); // eslint-disable-line react-hooks/exhaustive-deps

	// ── DnD ────────────────────────────────────────────────────────
	const handleDragStart = useCallback((event: DragStartEvent) => {
		setActiveDragId(event.active.id as string);
	}, []);

	const handleDragEnd = useCallback((event: DragEndEvent) => {
		setActiveDragId(null);
		if (!paperEdit) return;
		const { active, over } = event;
		if (over && active.id !== over.id) {
			const oldIndex = paperEdit.clips.findIndex((c) => c.clip_id === active.id);
			const newIndex = paperEdit.clips.findIndex((c) => c.clip_id === over.id);
			const newClips = arrayMove(paperEdit.clips, oldIndex, newIndex);
			reorderClips(newClips.map((c) => c.clip_id));
		}
	}, [paperEdit, reorderClips]);

	const handleApplyEdits = useCallback(() => {
		if (!selectedClipId) return;
		setIsSavingInspector(true);
		updateClip(selectedClipId, {
			description: inspectorDescription,
			transition: inspectorTransition,
		});
		setTimeout(() => setIsSavingInspector(false), 300);
	}, [selectedClipId, inspectorDescription, inspectorTransition, updateClip]);

	const handleTransitionToggle = useCallback(
		(t: TransitionType) => {
			setInspectorTransition(t);
			if (selectedClipId) updateClip(selectedClipId, { transition: t });
		},
		[selectedClipId, updateClip],
	);

	const handleTransitionTypeChange = useCallback(
		(clipId: string, type: TransitionType) => updateClip(clipId, { transition: type }),
		[updateClip],
	);

	const handleTransitionDurationChange = useCallback(
		(clipId: string, durationMs: number) => updateClip(clipId, { transition_duration_ms: durationMs }),
		[updateClip],
	);

	const handleTrimClip = useCallback(
		(clipId: string, edge: "in" | "out", deltaMs: number) => {
			const clip = paperEdit?.clips.find((c) => c.clip_id === clipId);
			if (!clip) return;
			const MIN_CLIP_MS = 200;
			let newIn = clip.in_ms;
			let newOut = clip.out_ms;
			if (edge === "in") {
				newIn = Math.max(0, clip.in_ms + deltaMs);
				if (newOut - newIn < MIN_CLIP_MS) newIn = newOut - MIN_CLIP_MS;
			} else {
				newOut = Math.max(newIn + MIN_CLIP_MS, clip.out_ms + deltaMs);
			}
			updateClip(clipId, { in_ms: newIn, out_ms: newOut });
		},
		[paperEdit?.clips, updateClip],
	);

	// ── Split tool ─────────────────────────────────────────────────
	const handleSplitAtPlayhead = useCallback(() => {
		if (!paperEdit || !selectedClipId) return;
		const clip = paperEdit.clips.find((c) => c.clip_id === selectedClipId);
		if (!clip) return;

		// Read playhead from store imperatively to avoid stale closure / re-render deps
		const currentPlayheadMs = useKinetographStore.getState().playheadMs;

		let clipStart = 0;
		for (const c of paperEdit.clips) {
			if (c.clip_id === selectedClipId) break;
			clipStart += c.out_ms - c.in_ms;
		}
		const clipEnd = clipStart + (clip.out_ms - clip.in_ms);
		if (currentPlayheadMs <= clipStart || currentPlayheadMs >= clipEnd) return;

		const splitPoint = clip.in_ms + (currentPlayheadMs - clipStart);
		const MIN_SPLIT = 100;
		if (splitPoint - clip.in_ms < MIN_SPLIT || clip.out_ms - splitPoint < MIN_SPLIT) return;

		const clipA: PaperEditClip = { ...clip, out_ms: splitPoint };
		const clipB: PaperEditClip = {
			...clip,
			clip_id: `clip-${crypto.randomUUID()}`,
			in_ms: splitPoint,
			description: clip.description + " (split)",
		};

		const newClips = paperEdit.clips.flatMap((c) =>
			c.clip_id === selectedClipId ? [clipA, clipB] : [c],
		);

		loadPaperEditIntoDoc(
			{ ...paperEdit, clips: newClips, total_duration_ms: newClips.reduce((s, c) => s + (c.out_ms - c.in_ms), 0) },
			"user",
		);
		useKinetographStore.setState({ selectedClipId: clipB.clip_id });
	}, [paperEdit, selectedClipId]);

	// ── Zoom ────────────────────────────────────────────────────────
	const applyZoom = useCallback(
		(nextZoomLevel: number, focusClientX?: number) => {
			const clampedZoom = clamp(nextZoomLevel, MIN_ZOOM_LEVEL, MAX_ZOOM_LEVEL);
			if (clampedZoom === zoomLevel) return;
			const viewport = trackViewportRef.current;
			if (!viewport) { setZoomLevel(clampedZoom); return; }
			const previousPPS = BASE_PIXELS_PER_SECOND * zoomLevel;
			const rect = viewport.getBoundingClientRect();
			const focusX = focusClientX !== undefined ? focusClientX - rect.left : viewport.clientWidth / 2;
			const focusTime = (viewport.scrollLeft + focusX) / previousPPS;
			setZoomLevel(clampedZoom);
			requestAnimationFrame(() => {
				const nextPPS = BASE_PIXELS_PER_SECOND * clampedZoom;
				viewport.scrollLeft = Math.max(0, focusTime * nextPPS - focusX);
			});
		},
		[zoomLevel],
	);

	const zoomIn = useCallback(() => applyZoom(zoomLevel * 1.25), [applyZoom, zoomLevel]);
	const zoomOut = useCallback(() => applyZoom(zoomLevel / 1.25), [applyZoom, zoomLevel]);

	// ── Generic drop handler factory ────────────────────────────────
	const makeTrackDragHandlers = useCallback((trackId: string) => ({
		onDragOver: (event: React.DragEvent) => {
			const types = event.dataTransfer.types;
			if (!types.includes("application/x-kinetograph-asset-id") && !types.includes("application/x-kinetograph-asset-ids")) return;
			event.preventDefault();
			event.dataTransfer.dropEffect = "copy";
			setDropTargetTrackId(trackId);
		},
		onDragEnter: (event: React.DragEvent) => {
			const types = event.dataTransfer.types;
			if (!types.includes("application/x-kinetograph-asset-id") && !types.includes("application/x-kinetograph-asset-ids")) return;
			event.preventDefault();
			setDropTargetTrackId(trackId);
		},
		onDragLeave: () => setDropTargetTrackId(null),
		onDrop: (event: React.DragEvent) => {
			event.preventDefault();
			setDropTargetTrackId(null);
			// Main video track (V1) lays clips out sequentially; overlay/audio
			// tracks position the clip at the drop point on the timeline.
			const isMain = trackId === "V1";
			const pps = BASE_PIXELS_PER_SECOND * zoomLevel;
			const rect = event.currentTarget.getBoundingClientRect();
			const dropMs = isMain ? 0 : Math.max(0, ((event.clientX - rect.left) / pps) * 1000);
			const addAsset = useKinetographStore.getState().addAssetToTimeline;
			const addAssets = useKinetographStore.getState().addAssetsToTimeline;
			const idsJson = event.dataTransfer.getData("application/x-kinetograph-asset-ids");
			if (idsJson) {
				try {
					const ids: string[] = JSON.parse(idsJson);
					const clipIds = addAssets(ids, trackId, dropMs);
					if (clipIds.length > 0 && isMain) setSelectedClip(clipIds[clipIds.length - 1]);
					return;
				} catch { /* fall through */ }
			}
			const assetId = event.dataTransfer.getData("application/x-kinetograph-asset-id");
			if (!assetId) return;
			const clipId = addAsset(assetId, trackId, dropMs);
			if (clipId && isMain) setSelectedClip(clipId);
		},
	}), [setSelectedClip, zoomLevel]);

	const handleTrackWheel = (event: React.WheelEvent<HTMLDivElement>) => {
		if (!event.ctrlKey && !event.metaKey) return;
		event.preventDefault();
		applyZoom(zoomLevel * (event.deltaY < 0 ? 1.1 : 0.9), event.clientX);
	};

	// ── Playhead ────────────────────────────────────────────────────
	const handlePlayheadPointerDown = (e: React.PointerEvent) => {
		e.preventDefault();
		e.stopPropagation();
		setIsDraggingPlayhead(true);
		isDraggingPlayheadRef.current = true;
		(e.target as HTMLElement).setPointerCapture(e.pointerId);
	};

	const handlePlayheadPointerMove = useCallback(
		(e: React.PointerEvent) => {
			if (!isDraggingPlayhead || !trackViewportRef.current) return;
			const rect = trackViewportRef.current.getBoundingClientRect();
			const x = e.clientX - rect.left + trackViewportRef.current.scrollLeft - TRACK_HEADER_WIDTH_PX;
			const currentClips = paperEdit?.clips ?? [];
			const ms = currentClips.length > 0
				? pixelToMs(x, BASE_PIXELS_PER_SECOND * zoomLevel, currentClips, selectedTransitionClipId)
				: (x / (BASE_PIXELS_PER_SECOND * zoomLevel)) * 1000;
			onSeek?.(Math.max(0, ms));
		},
		[isDraggingPlayhead, zoomLevel, onSeek, paperEdit?.clips, selectedTransitionClipId],
	);

	const handlePlayheadPointerUp = useCallback(() => {
		setIsDraggingPlayhead(false);
		isDraggingPlayheadRef.current = false;
	}, []);

	// ── Resize observer ─────────────────────────────────────────────
	useEffect(() => {
		const viewport = trackViewportRef.current;
		if (!viewport) return;
		const obs = new ResizeObserver((entries) => setTrackViewportWidth(entries[0]?.contentRect.width ?? 0));
		obs.observe(viewport);
		return () => obs.disconnect();
	}, []);

	// ── Keyboard shortcuts ──────────────────────────────────────────
	useEffect(() => {
		const onKeyDown = (event: KeyboardEvent) => {
			const target = event.target as HTMLElement | null;
			if (target?.tagName === "INPUT" || target?.tagName === "TEXTAREA" || target?.isContentEditable) return;

			if (event.key === "Escape") {
				setSelectedTransitionClipId(null);
				setSelectedClip(null);
				setSelectedV2Clip(null);
			}

			// Cmd/Ctrl+A → select all V1 clips
			if ((event.metaKey || event.ctrlKey) && event.key === "a" && paperEdit && paperEdit.clips.length > 0) {
				event.preventDefault();
				setSelectedClipIds(new Set(paperEdit.clips.map((c) => c.clip_id)));
				return;
			}

			if (event.key === "Delete" || event.key === "Backspace") {
				if (selectedClipIds.size > 0) {
					event.preventDefault();
					deleteSelectedClips();
				} else if (selectedV2ClipId) {
					event.preventDefault();
					removeV2Clip(selectedV2ClipId);
				}
			}
			if (!selectedClipId) return;
			if (event.key === "s" && !event.metaKey && !event.ctrlKey) handleSplitAtPlayhead();
		};
		window.addEventListener("keydown", onKeyDown);
		return () => window.removeEventListener("keydown", onKeyDown);
	}, [deleteSelectedClips, selectedClipId, selectedClipIds, selectedV2ClipId, removeV2Clip, handleSplitAtPlayhead, setSelectedClip, setSelectedV2Clip, paperEdit, setSelectedClipIds]);

	// ── Timeline metrics ────────────────────────────────────────────
	const sequenceDurationMs = useMemo(
		() => paperEdit?.clips.reduce((t, c) => t + (c.out_ms - c.in_ms), 0) ?? 0,
		[paperEdit],
	);
	const effectiveDurationMs = Math.max(paperEdit?.total_duration_ms ?? 0, sequenceDurationMs);
	const pxPerSecond = BASE_PIXELS_PER_SECOND * zoomLevel;

	const timelineDurationSeconds = useMemo(() => {
		const fromClips = effectiveDurationMs / 1000 + TRAILING_PADDING_SECONDS;
		const fromViewport = trackViewportWidth > 0 ? trackViewportWidth / pxPerSecond : 0;
		return Math.max(MIN_TIMELINE_SECONDS, fromClips, fromViewport);
	}, [effectiveDurationMs, pxPerSecond, trackViewportWidth]);

	const totalTransitionPx = useMemo(
		() => paperEdit ? getTotalTransitionPx(paperEdit.clips, selectedTransitionClipId) : 0,
		[paperEdit, selectedTransitionClipId],
	);

	const timelineWidthPx = Math.max(trackViewportWidth, Math.ceil(timelineDurationSeconds * pxPerSecond) + totalTransitionPx);
	const tickStepSeconds = getTickStepSeconds(pxPerSecond);
	// playheadLeftPx no longer used for rendering — the enterprise playhead effect
	// updates the DOM directly via transform. Kept only for backward compat if needed.
	const activeDragClip = activeDragId ? paperEdit?.clips.find((c) => c.clip_id === activeDragId) : null;

	// ── Enterprise playhead: direct DOM updates via store subscription ──
	// Bypasses React render cycle for smooth 60fps playhead motion + auto-scroll.
	useEffect(() => {
		const el = playheadElRef.current;
		if (!el) return;

		const computeLeft = (ms: number) => {
			const clips = useKinetographStore.getState().paperEdit?.clips ?? [];
			const trOffset = clips.length > 0
				? getTransitionOffsetAtMs(ms, clips, selectedTransitionClipId)
				: 0;
			return (ms / 1000) * pxPerSecond + trOffset;
		};

		// Set initial position
		el.style.transform = `translateX(${computeLeft(useKinetographStore.getState().playheadMs)}px)`;

		const unsub = useKinetographStore.subscribe((state, prev) => {
			if (state.playheadMs === prev.playheadMs && state.paperEdit === prev.paperEdit) return;
			const leftPx = computeLeft(state.playheadMs);
			el.style.transform = `translateX(${leftPx}px)`;

			// Auto-scroll timeline to keep playhead visible during continuous playback
			const viewport = trackViewportRef.current;
			if (viewport && !isDraggingPlayheadRef.current) {
				const delta = Math.abs(state.playheadMs - prev.playheadMs);
				if (delta > 0 && delta < 200) {
					const viewWidth = viewport.clientWidth - TRACK_HEADER_WIDTH_PX;
					const scrollRight = viewport.scrollLeft + viewWidth;
					if (leftPx > scrollRight - 60) {
						viewport.scrollLeft = leftPx - viewWidth * 0.25;
					}
				}
			}
		});

		return unsub;
	}, [pxPerSecond, selectedTransitionClipId]);

	// ── Track renderers ─────────────────────────────────────────────

	/** Video overlay track (V2, V3, …) — absolute-positioned clips */
	const renderOverlayTrack = (track: Track) => {
		const isDropTarget = dropTargetTrackId === track.id;
		const handlers = makeTrackDragHandlers(track.id);
		// Only this track's overlay clips (multi-track: V2, V3, …).
		const trackClips = v2Clips.filter((c) => c.trackId === track.id);
		return (
			<div key={track.id} className="flex items-stretch group" style={{ height: `${track.height}px` }}>
				<TrackHeader
					track={track}
					onToggleMute={() => toggleTrackMute(track.id)}
					onToggleSolo={() => toggleTrackSolo(track.id)}
					onToggleLock={() => toggleTrackLock(track.id)}
					onVolumeChange={(v) => setTrackVolume(track.id, v)}
					onRemove={() => removeTrack(track.id)}
					canRemove
				/>
				<div className={cn("flex-1 relative", track.locked && "opacity-50 pointer-events-none")} {...handlers}>
					{trackClips.length > 0 ? (
						<div className="relative h-full">
							{trackClips.map((clip) => {
								const dur = clip.outMs - clip.inMs;
								const w = Math.max(60, (dur / 1000) * pxPerSecond);
								const leftPx = (clip.timelineStartMs / 1000) * pxPerSecond;
								const presetLabel = OVERLAY_PRESET_OPTIONS.find((o) => o.value === clip.preset)?.icon ?? "✦";
								return (
									<div
										key={clip.id}
										onClick={() => { setSelectedV2Clip(clip.id); setSelectedClip(null); setSelectedTransitionClipId(null); }}
										className={cn(
											"absolute top-0.5 bottom-0.5 rounded border cursor-pointer overflow-hidden transition-all group/v2clip",
											selectedV2ClipId === clip.id
												? "bg-amber-500/15 ring-1 ring-amber-500/50 border-amber-500/30"
												: "bg-zinc-800/50 border-zinc-700/30 hover:bg-zinc-800/70 hover:border-zinc-600/40",
										)}
										style={{ width: `${w}px`, left: `${leftPx}px` }}
									>
										<div className="flex h-full items-center px-1.5 gap-1.5">
											<div className={cn(
												"shrink-0 w-5 h-5 rounded flex items-center justify-center text-[9px]",
												selectedV2ClipId === clip.id ? "bg-amber-500/30 text-amber-300" : "bg-amber-500/10 text-amber-500/60",
											)}>
												{presetLabel}
											</div>
											<div className="flex flex-col min-w-0 gap-0.5">
												<span className="text-[7px] font-medium text-zinc-400 truncate">{clip.sourceFile.replace(/\.[^.]+$/, "")}</span>
												<span className="text-[6px] font-mono text-zinc-600">{(dur / 1000).toFixed(1)}s · {OVERLAY_PRESET_OPTIONS.find((o) => o.value === clip.preset)?.label ?? "Custom"}</span>
											</div>
											{/* Quick delete */}
											<button
												onClick={(e) => { e.stopPropagation(); removeV2Clip(clip.id); }}
												className="ml-auto opacity-0 group-hover/v2clip:opacity-100 p-0.5 text-zinc-600 hover:text-red-400 transition-all shrink-0"
												title="Remove overlay"
											>
												<XCircle className="h-2.5 w-2.5" />
											</button>
										</div>
										<div className="absolute bottom-0 left-0 right-0 h-[2px] bg-amber-500/30" />
									</div>
								);
							})}
						</div>
					) : (
						<div className={cn(
							"h-full border border-dashed rounded flex items-center justify-center transition-colors",
							isDropTarget ? "border-amber-500/50 bg-amber-500/5" : "border-zinc-800/30 bg-transparent",
						)}>
							<span className="text-[7px] font-medium text-zinc-700">{isDropTarget ? "Drop to add overlay" : "Drop video for overlay"}</span>
						</div>
					)}
				</div>
			</div>
		);
	};

	/** V1 primary track — sequential DnD clips */
	const renderV1Track = (track: Track) => {
		const isDropTarget = dropTargetTrackId === track.id;
		const handlers = makeTrackDragHandlers(track.id);
		return (
			<div key={track.id} className="flex items-stretch group" style={{ height: `${track.height}px` }}>
				<TrackHeader
					track={track}
					onToggleMute={() => toggleTrackMute(track.id)}
					onToggleSolo={() => toggleTrackSolo(track.id)}
					onToggleLock={() => toggleTrackLock(track.id)}
					onVolumeChange={(v) => setTrackVolume(track.id, v)}
					onRemove={() => {}}
					canRemove={false}
				/>
				<div
					className={cn(
						"flex-1 touch-none",
						isDropTarget && "ring-1 ring-blue-500/30 bg-blue-500/5 rounded",
						track.locked && "opacity-50 pointer-events-none",
					)}
					{...handlers}
				>
					{paperEdit && paperEdit.clips.length > 0 ? (
						<DndContext sensors={sensors} collisionDetection={closestCenter} onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
							<SortableContext items={paperEdit.clips.map((c) => c.clip_id)} strategy={horizontalListSortingStrategy}>
								<div ref={v1ContainerRef} className="flex items-center h-full relative" {...v1MarqueeHandlers}>
									{paperEdit.clips.flatMap((clip, i) => [
										...(i > 0 ? [
											<button
												key={`tr-${clip.clip_id}`}
												onClick={(e) => {
													e.stopPropagation();
													setSelectedTransitionClipId(selectedTransitionClipId === clip.clip_id ? null : clip.clip_id);
													setSelectedClip(null);
												}}
												className={cn(
													"shrink-0 flex flex-col items-center justify-center cursor-pointer transition-all rounded-sm h-full group/tr",
													selectedTransitionClipId === clip.clip_id
														? "bg-purple-500/20 border border-purple-500/40"
														: clip.transition && clip.transition !== "cut"
															? "bg-gradient-to-b from-purple-500/10 to-transparent border border-purple-500/15 hover:border-purple-500/30"
															: "hover:bg-zinc-800/40 border border-transparent hover:border-zinc-700/30",
												)}
												style={{ width: `${getTransitionWidthPx(clip, i, selectedTransitionClipId)}px` }}
												title={`${TRANSITION_OPTIONS.find((t) => t.value === (clip.transition || "cut"))?.label ?? "Cut"}${clip.transition_duration_ms ? ` · ${clip.transition_duration_ms}ms` : ""}`}
											>
												{clip.transition && clip.transition !== "cut" ? (
													<div className="flex flex-col items-center gap-0.5 overflow-hidden">
														<div className={cn("w-5 h-5 rounded flex items-center justify-center", selectedTransitionClipId === clip.clip_id ? "bg-purple-500/30" : "bg-purple-500/15")}>
															<span className={cn("text-[9px]", selectedTransitionClipId === clip.clip_id ? "text-purple-300" : "text-purple-400/70")}>✦</span>
														</div>
														<span className={cn("text-[7px] font-bold whitespace-nowrap leading-none", selectedTransitionClipId === clip.clip_id ? "text-purple-300" : "text-purple-400/60")}>
															{TRANSITION_OPTIONS.find((t) => t.value === (clip.transition || "cut"))?.shortLabel ?? ""}
														</span>
													</div>
												) : (
													<div className="flex flex-col items-center gap-0.5">
														<div className={cn(
															"w-4 h-4 rounded flex items-center justify-center transition-colors",
															selectedTransitionClipId === clip.clip_id ? "bg-zinc-600" : "bg-zinc-800/50 group-hover/tr:bg-zinc-700/60",
														)}>
															<span className={cn("text-[7px] font-bold", selectedTransitionClipId === clip.clip_id ? "text-zinc-300" : "text-zinc-600 group-hover/tr:text-zinc-400")}>⋮</span>
														</div>
													</div>
												)}
											</button>,
										] : []),
										<TimelineClip
											key={clip.clip_id}
											clip={clip}
											isSelected={selectedClipIds.has(clip.clip_id) || selectedClipId === clip.clip_id}
											pxPerSecond={pxPerSecond}
											onClick={(e) => { handleClipClick(e, clip.clip_id); }}
											onDelete={() => deleteClip(clip.clip_id)}
											onTrim={(edge, deltaMs) => handleTrimClip(clip.clip_id, edge, deltaMs)}
										/>,
									])}
									{/* Append area — drop target for adding clips at the end */}
									<div
										className={cn(
											"shrink-0 h-full min-w-[60px] flex-1 flex items-center justify-center",
											isDropTarget && "bg-blue-500/10 border border-dashed border-blue-500/30 rounded",
										)}
									>
										{isDropTarget && (
											<span className="text-[8px] text-blue-400/70 font-medium whitespace-nowrap">+ Drop here</span>
										)}
									</div>

									{/* Marquee overlay */}
									{v1Marquee && (
										<div
											className="absolute border-2 border-blue-400/70 bg-blue-500/20 pointer-events-none z-50 rounded-sm"
											style={{
												left: v1Marquee.x,
												top: v1Marquee.y,
												width: v1Marquee.width,
												height: v1Marquee.height,
											}}
										/>
									)}
								</div>
							</SortableContext>
							<DragOverlay dropAnimation={null}>
								{activeDragClip && (
									<div className="h-16 bg-blue-500/20 border border-blue-500/40 rounded opacity-80"
										style={{ width: `${Math.max(40, ((activeDragClip.out_ms - activeDragClip.in_ms) / 1000) * pxPerSecond)}px` }}>
										<div className="p-1 text-[8px] text-blue-300 truncate">{activeDragClip.source_file}</div>
									</div>
								)}
							</DragOverlay>
						</DndContext>
					) : (
						<div className="flex h-full w-full items-center justify-center rounded border border-dashed border-zinc-800 bg-zinc-900/20">
							<div className="flex items-center gap-2 text-zinc-600">
								<MousePointerClick className="h-3.5 w-3.5" />
								<span className="text-[9px] font-medium">Drag media from the bin to start editing</span>
							</div>
						</div>
					)}
				</div>
			</div>
		);
	};

	/** A1 dialogue track — mirrors V1 layout */
	const renderA1Track = (track: Track) => (
		<div key={track.id} className="flex items-stretch group" style={{ height: `${track.height}px` }}>
			<TrackHeader
				track={track}
				onToggleMute={() => toggleTrackMute(track.id)}
				onToggleSolo={() => toggleTrackSolo(track.id)}
				onToggleLock={() => toggleTrackLock(track.id)}
				onVolumeChange={(v) => setTrackVolume(track.id, v)}
				onRemove={() => {}}
				canRemove={false}
			/>
			<div className={cn("flex-1", track.locked && "opacity-50 pointer-events-none")}>
				{paperEdit && paperEdit.clips.length > 0 ? (
					<div className="flex items-center h-full">
						{paperEdit.clips.flatMap((clip, i) => {
							const dur = clip.out_ms - clip.in_ms;
							const w = Math.max(40, (dur / 1000) * pxPerSecond);
							const trW = i > 0 ? getTransitionWidthPx(clip, i, selectedTransitionClipId) : 0;
							return [
								...(i > 0 ? [<div key={`atr-${clip.clip_id}`} className="shrink-0" style={{ width: `${trW}px` }} />] : []),
								<div
									key={`a-${clip.clip_id}`}
									onClick={(e) => { handleClipClick(e, clip.clip_id); }}
									className={cn(
										"shrink-0 border-r border-black/30 cursor-pointer overflow-hidden transition-colors h-full",
										track.muted ? "opacity-40" : "",
										selectedClipIds.has(clip.clip_id) || selectedClipId === clip.clip_id
											? "bg-teal-500/20 ring-1 ring-teal-500/50"
											: "bg-teal-500/10 border-teal-500/10 hover:bg-teal-500/15",
									)}
									style={{ width: `${w}px` }}
								>
									<div className="flex items-end justify-center h-full px-0.5 py-1 gap-[1px]">
										<AudioWaveformBars clipId={clip.clip_id} width={w} />
									</div>
								</div>,
							];
						})}
					</div>
				) : (
					<div className="h-full border border-dashed border-zinc-900/50 bg-zinc-900/10 rounded flex items-center justify-center">
						<span className="text-[7px] font-medium text-zinc-700">Dialogue</span>
					</div>
				)}
			</div>
		</div>
	);

	/** A2+ audio track — absolute positioned clips */
	const renderAudioTrack = (track: Track) => {
		const isDropTarget = dropTargetTrackId === track.id;
		const handlers = makeTrackDragHandlers(track.id);
		// Only this track's audio clips (multi-track: A2, A3, …).
		const trackAudioClips = a2Clips.filter((c) => c.trackId === track.id);
		return (
			<div key={track.id} className="flex items-stretch group" style={{ height: `${track.height}px` }}>
				<TrackHeader
					track={track}
					onToggleMute={() => toggleTrackMute(track.id)}
					onToggleSolo={() => toggleTrackSolo(track.id)}
					onToggleLock={() => toggleTrackLock(track.id)}
					onVolumeChange={(v) => setTrackVolume(track.id, v)}
					onRemove={() => removeTrack(track.id)}
					canRemove={track.id !== "A1"}
				/>
				<div className={cn("flex-1 relative", track.locked && "opacity-50 pointer-events-none")} {...handlers}>
					{track.id === "A2" && musicPath ? (() => {
						// Use the timeline's effective duration (matches V1 track width)
						const w = Math.max(80, (effectiveDurationMs / 1000) * pxPerSecond);
						const fileName = musicPath.split("/").pop() ?? "Background Music";
						return (
							<div className={cn("rounded border overflow-hidden transition-colors h-full", track.muted ? "opacity-40" : "", "bg-violet-500/15 border-violet-500/30")} style={{ width: `${w}px` }}>
								<div className="flex items-center h-full px-1.5 gap-1">
									<Volume2 className="h-2.5 w-2.5 text-violet-400/70 shrink-0" />
									<span className="text-[7px] font-medium text-violet-300/80 truncate">{fileName}</span>
								</div>
							</div>
						);
					})() : trackAudioClips.length > 0 ? (
						<div className="relative h-full">
							{trackAudioClips.map((clip) => {
								const dur = clip.outMs - clip.inMs;
								const w = Math.max(40, (dur / 1000) * pxPerSecond);
								const leftPx = (clip.timelineStartMs / 1000) * pxPerSecond;
								return (
									<div
										key={clip.id}
										className={cn(
											"absolute top-0.5 bottom-0.5 rounded border cursor-pointer overflow-hidden transition-colors",
											track.muted ? "opacity-40" : "",
											"bg-violet-500/10 border-violet-500/20 hover:bg-violet-500/15",
										)}
										style={{ width: `${w}px`, left: `${leftPx}px` }}
									>
										<div className="flex items-end justify-center h-full px-0.5 py-1 gap-[1px]">
											<AudioWaveformBars clipId={clip.id} width={w} />
										</div>
									</div>
								);
							})}
						</div>
					) : (
						<div className={cn(
							"h-full border border-dashed rounded flex items-center justify-center transition-colors",
							isDropTarget ? "border-violet-500/50 bg-violet-500/5" : "border-zinc-800/30 bg-transparent",
						)}>
							<span className="text-[7px] font-medium text-zinc-700">{isDropTarget ? "Drop audio here" : "Drop audio for background"}</span>
						</div>
					)}
				</div>
			</div>
		);
	};

	const renderTrack = (track: Track) => {
		if (track.id === "V1") return renderV1Track(track);
		if (track.id === "A1") return renderA1Track(track);
		if (track.type === "video") return renderOverlayTrack(track);
		return renderAudioTrack(track);
	};

	// ═════════════════════════════════════════════════════════════════
	// RENDER
	// ═════════════════════════════════════════════════════════════════

	return (
		<div className="flex w-full flex-col gap-2">
			{/* ── Toolbar ────────────────────────────────────────── */}
			<div className="flex items-center justify-between h-7">
				<div className="flex items-center gap-2">
					<span className="text-[10px] font-medium text-zinc-400">{paperEdit?.title || "Untitled Sequence"}</span>
					{paperEdit && (
						<span className="text-[9px] font-mono text-zinc-600">
							{(sequenceDurationMs / 1000).toFixed(1)}s · {paperEdit.clips.length} clips
						</span>
					)}
				</div>
				<div className="flex items-center gap-1">
					{selectedClipIds.size > 1 && (
						<>
							<span className="text-[9px] font-mono text-blue-400/80 mr-1">{selectedClipIds.size} selected</span>
							<button onClick={deleteSelectedClips} className="flex h-6 items-center gap-1 rounded border border-red-800/40 bg-red-600/15 px-2 text-[9px] font-medium text-red-300 hover:bg-red-600/25 transition-colors" title={`Delete ${selectedClipIds.size} clips`}>
								<Trash2 className="h-3 w-3" /> Delete {selectedClipIds.size}
							</button>
						</>
					)}
					{selectedClipIds.size <= 1 && selectedClipId && (
						<>
							<button onClick={handleSplitAtPlayhead} className="flex h-6 items-center gap-1 rounded border border-zinc-800 bg-zinc-900/50 px-2 text-[9px] font-medium text-zinc-400 hover:bg-zinc-800 hover:text-white transition-colors" title="Split at playhead (S)">
								<Scissors className="h-3 w-3" /> Split
							</button>
							<button onClick={deleteSelectedClips} className="flex h-6 items-center gap-1 rounded border border-zinc-800 bg-zinc-900/50 px-2 text-[9px] font-medium text-zinc-400 hover:bg-zinc-800 hover:text-red-400 transition-colors" title="Delete clip (Del)">
								<Trash2 className="h-3 w-3" />
							</button>
						</>
					)}
					{selectedV2ClipId && (
						<button onClick={() => removeV2Clip(selectedV2ClipId)} className="flex h-6 items-center gap-1 rounded border border-zinc-800 bg-zinc-900/50 px-2 text-[9px] font-medium text-zinc-400 hover:bg-zinc-800 hover:text-red-400 transition-colors" title="Delete overlay (Del)">
							<Trash2 className="h-3 w-3" /> <span>Remove Overlay</span>
						</button>
					)}

					{/* Add Track dropdown */}
					<div className="relative">
						<button
							onClick={() => setShowAddTrackMenu(!showAddTrackMenu)}
							className="flex h-6 items-center gap-1 rounded border border-zinc-800 bg-zinc-900/50 px-2 text-[9px] font-medium text-zinc-400 hover:bg-zinc-800 hover:text-white transition-colors"
							title="Add track"
						>
							<Plus className="h-3 w-3" /> Track <ChevronDown className="h-2.5 w-2.5" />
						</button>
						{showAddTrackMenu && (
							<>
								<div className="fixed inset-0 z-40" onClick={() => setShowAddTrackMenu(false)} />
								<div className="absolute right-0 top-full mt-1 z-50 bg-zinc-900 border border-zinc-700 rounded-md shadow-xl overflow-hidden min-w-[120px]">
									<button onClick={() => { addTrack("video"); setShowAddTrackMenu(false); }} className="w-full flex items-center gap-2 px-3 py-2 text-[10px] text-zinc-300 hover:bg-zinc-800 transition-colors">
										<Eye className="h-3 w-3 text-blue-400" /> Video Track
									</button>
									<button onClick={() => { addTrack("audio"); setShowAddTrackMenu(false); }} className="w-full flex items-center gap-2 px-3 py-2 text-[10px] text-zinc-300 hover:bg-zinc-800 transition-colors">
										<Volume2 className="h-3 w-3 text-teal-400" /> Audio Track
									</button>
								</div>
							</>
						)}
					</div>

					{/* Zoom */}
					<div className="flex h-6 items-center gap-0.5 rounded border border-zinc-800 bg-zinc-900/50 px-0.5 ml-1">
						<button onClick={zoomOut} className="rounded p-0.5 text-zinc-500 hover:bg-zinc-800 hover:text-white disabled:opacity-30" disabled={zoomLevel <= MIN_ZOOM_LEVEL}>
							<ZoomOut className="h-3 w-3" />
						</button>
						<span className="text-[9px] font-mono text-zinc-500 px-1 min-w-[32px] text-center">{Math.round(zoomLevel * 100)}%</span>
						<button onClick={zoomIn} className="rounded p-0.5 text-zinc-500 hover:bg-zinc-800 hover:text-white disabled:opacity-30" disabled={zoomLevel >= MAX_ZOOM_LEVEL}>
							<ZoomIn className="h-3 w-3" />
						</button>
					</div>
				</div>
			</div>

			{/* ── Track Viewport ──────────────────────────────────── */}
			<div
				ref={trackViewportRef}
				onWheel={handleTrackWheel}
				onPointerMove={isDraggingPlayhead ? handlePlayheadPointerMove : undefined}
				onPointerUp={isDraggingPlayhead ? handlePlayheadPointerUp : undefined}
				className="relative w-full rounded border overflow-x-auto overflow-y-hidden custom-scrollbar border-zinc-800 bg-zinc-950/50 transition-colors"
			>
				{/* Ruler */}
				<TimeRuler
					timelineWidthPx={timelineWidthPx}
					pxPerSecond={pxPerSecond}
					tickStepSeconds={tickStepSeconds}
					onSeek={onSeek}
					clips={paperEdit?.clips}
					selectedTransitionClipId={selectedTransitionClipId}
				/>

				{/* Playhead line — positioned via direct DOM manipulation (enterprise pattern) */}
				<div
					ref={playheadElRef}
					className="absolute top-0 bottom-0 z-30"
					style={{ left: `${TRACK_HEADER_WIDTH_PX}px`, willChange: 'transform' }}
				>
					<div
						className={cn(
							"w-px h-full pointer-events-none",
							isDraggingPlayhead ? "bg-blue-400 shadow-[0_0_8px_rgba(59,130,246,0.6)]" : "bg-blue-500 shadow-[0_0_4px_rgba(59,130,246,0.4)]",
						)}
					/>
					<div
						onPointerDown={handlePlayheadPointerDown}
						className="absolute top-0 left-1/2 -translate-x-1/2 w-3 h-3 bg-blue-500 rotate-45 -translate-y-1/2 border border-black/20 cursor-grab active:cursor-grabbing hover:bg-blue-400 z-40 pointer-events-auto"
					/>
				</div>

				{/* Track stack */}
				<div className="relative flex flex-col min-w-full" style={{ width: `${TRACK_HEADER_WIDTH_PX + timelineWidthPx}px` }}>
					{/* Video tracks (highest overlay first → V1 last) */}
					{videoTracks.map((track) => (
						<div key={track.id} className="border-b border-zinc-800/40">{renderTrack(track)}</div>
					))}

					{/* V/A separator */}
					<div className="h-px bg-zinc-700/50" />

					{/* Audio tracks */}
					{audioTracks.map((track) => (
						<div key={track.id} className="border-b border-zinc-800/40">{renderTrack(track)}</div>
					))}
				</div>
			</div>

			{/* ═══ Inspectors ═══════════════════════════════════════ */}

			{/* V2 Overlay Inspector */}
			<AnimatePresence>
				{selectedV2ClipId && selectedV2Clip && (
					<motion.div
						initial={{ opacity: 0, y: 6 }}
						animate={{ opacity: 1, y: 0 }}
						exit={{ opacity: 0, y: 6 }}
						className="rounded border border-zinc-800 bg-zinc-900 p-3"
					>
						<div className="flex items-center justify-between mb-3">
							<div className="flex items-center gap-3">
								<Layers className="h-3.5 w-3.5 text-amber-500" />
								<h3 className="text-[10px] font-semibold text-zinc-200">Overlay: {selectedV2Clip.sourceFile}</h3>
								<span className="text-[9px] font-mono text-zinc-500">{((selectedV2Clip.outMs - selectedV2Clip.inMs) / 1000).toFixed(2)}s</span>
							</div>
							<button onClick={() => setSelectedV2Clip(null)} className="text-zinc-600 hover:text-zinc-300 transition-colors">
								<XCircle className="h-3.5 w-3.5" />
							</button>
						</div>
						<div className="grid grid-cols-3 gap-4">
							{/* Preset picker */}
							<div className="flex flex-col gap-1.5">
								<label className="text-[9px] font-medium text-zinc-500">Layout Preset</label>
								<div className="grid grid-cols-4 gap-1">
									{OVERLAY_PRESET_OPTIONS.map((opt) => (
										<button key={opt.value} onClick={() => setV2ClipPreset(selectedV2ClipId, opt.value)}
											className={cn("rounded border px-1 py-1.5 text-[9px] font-medium transition-all text-center",
												selectedV2Clip.preset === opt.value ? "border-amber-500 bg-amber-500/15 text-amber-400" : "border-zinc-800 bg-zinc-800/30 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300")}
											title={opt.label}>{opt.icon}</button>
									))}
								</div>
							</div>
							{/* Transform */}
							<div className="flex flex-col gap-1.5">
								<label className="text-[9px] font-medium text-zinc-500">Position & Size</label>
								<div className="grid grid-cols-2 gap-1.5">
									{(["x", "y", "width", "height"] as const).map((prop) => (
										<div key={prop} className="flex flex-col gap-0.5">
											<span className="text-[7px] font-mono text-zinc-600 uppercase">{prop}</span>
											<input type="number" min={0} max={100} value={Math.round(selectedV2Clip.transform[prop])}
												onChange={(e) => updateV2ClipTransform(selectedV2ClipId, { [prop]: Number(e.target.value) })}
												className="w-full bg-black/30 border border-zinc-800 rounded px-1.5 py-1 text-[9px] font-mono text-zinc-300 focus:border-amber-500/50 outline-none" />
										</div>
									))}
								</div>
							</div>
							{/* Opacity + radius */}
							<div className="flex flex-col gap-1.5">
								<label className="text-[9px] font-medium text-zinc-500">Appearance</label>
								<div className="flex flex-col gap-1.5">
									<div className="flex items-center gap-2">
										<span className="text-[7px] font-mono text-zinc-600 w-12">Opacity</span>
										<input type="range" min={0} max={100} value={Math.round(selectedV2Clip.transform.opacity * 100)}
											onChange={(e) => updateV2ClipTransform(selectedV2ClipId, { opacity: Number(e.target.value) / 100 })}
											className="flex-1 accent-amber-500 h-1 bg-zinc-800 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-2.5 [&::-webkit-slider-thumb]:h-2.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-amber-500" />
										<span className="text-[8px] font-mono text-zinc-400 w-8 text-right">{Math.round(selectedV2Clip.transform.opacity * 100)}%</span>
									</div>
									<div className="flex items-center gap-2">
										<span className="text-[7px] font-mono text-zinc-600 w-12">Radius</span>
										<input type="range" min={0} max={50} value={selectedV2Clip.transform.borderRadius}
											onChange={(e) => updateV2ClipTransform(selectedV2ClipId, { borderRadius: Number(e.target.value) })}
											className="flex-1 accent-amber-500 h-1 bg-zinc-800 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-2.5 [&::-webkit-slider-thumb]:h-2.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-amber-500" />
										<span className="text-[8px] font-mono text-zinc-400 w-8 text-right">{selectedV2Clip.transform.borderRadius}%</span>
									</div>
								</div>
								<button onClick={() => removeV2Clip(selectedV2ClipId)} className="mt-1 flex items-center gap-1.5 text-zinc-600 hover:text-red-400 transition-colors text-[10px] font-medium">
									<Trash2 className="h-3 w-3" /> Remove Overlay
								</button>
							</div>
						</div>
						{/* Visual preview */}
						<div className="mt-3 flex items-center gap-3">
							<div className="relative w-32 bg-zinc-800 rounded border border-zinc-700 overflow-hidden" style={{ aspectRatio: "16/9" }}>
								<div className="absolute inset-0 flex items-center justify-center text-[6px] text-zinc-600">V1</div>
								<div className="absolute bg-amber-500/30 border border-amber-500/60 rounded-sm"
									style={{ left: `${selectedV2Clip.transform.x}%`, top: `${selectedV2Clip.transform.y}%`, width: `${selectedV2Clip.transform.width}%`, height: `${selectedV2Clip.transform.height}%`, opacity: selectedV2Clip.transform.opacity, borderRadius: `${selectedV2Clip.transform.borderRadius}%` }}>
									<div className="flex items-center justify-center h-full text-[5px] text-amber-300 font-medium">V2</div>
								</div>
							</div>
							<span className="text-[8px] text-zinc-600">{OVERLAY_PRESET_OPTIONS.find((o) => o.value === selectedV2Clip.preset)?.label ?? "Custom"}</span>
						</div>
					</motion.div>
				)}
			</AnimatePresence>

			{/* Clip Inspector */}
			<AnimatePresence>
				{selectedClipId && selectedClip && selectedClipIds.size <= 1 && !selectedV2ClipId && (
					<motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 6 }} className="rounded border border-zinc-800 bg-zinc-900 p-3">
						<div className="flex items-center justify-between mb-3">
							<div className="flex items-center gap-3">
								<h3 className="text-[10px] font-semibold text-zinc-200">{selectedClip.source_file}</h3>
								<div className="flex items-center gap-2 text-[9px] font-mono text-zinc-500">
									<span>IN: {(selectedClip.in_ms / 1000).toFixed(2)}s</span>
									<span className="opacity-30">→</span>
									<span>OUT: {(selectedClip.out_ms / 1000).toFixed(2)}s</span>
									<span className="opacity-30">|</span>
									<span className="text-blue-400/70">{((selectedClip.out_ms - selectedClip.in_ms) / 1000).toFixed(2)}s</span>
								</div>
							</div>
							<button onClick={() => setSelectedClip(null)} className="text-zinc-600 hover:text-zinc-300 transition-colors"><XCircle className="h-3.5 w-3.5" /></button>
						</div>
						<div className="grid grid-cols-4 gap-4">
							<div className="col-span-2 flex flex-col gap-1.5">
								<label className="text-[9px] font-medium text-zinc-500">Description</label>
								<textarea
									className="bg-black/30 border border-zinc-800 rounded p-2 text-xs text-zinc-300 min-h-[48px] focus:border-blue-500/50 outline-none transition-colors resize-none"
									value={inspectorDescription}
									onChange={(e) => setInspectorDescription(e.target.value)}
									onBlur={() => { if (selectedClipId && inspectorDescription !== selectedClip.description) updateClip(selectedClipId, { description: inspectorDescription }); }}
								/>
							</div>
							<div className="flex flex-col gap-3">
								<div className="flex flex-col gap-1.5">
									<label className="text-[9px] font-medium text-zinc-500">Transition (In)</label>
									<div className="grid grid-cols-3 gap-1">
										{TRANSITION_OPTIONS.map((opt) => (
											<button
												key={opt.value}
												onClick={() => handleTransitionToggle(opt.value)}
												className={cn(
													"rounded border px-1 py-1 text-[8px] font-semibold transition-all text-center",
													inspectorTransition === opt.value
														? "border-purple-500 bg-purple-500/15 text-purple-400"
														: "border-zinc-800 bg-zinc-800/30 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300",
												)}
											>
												{opt.shortLabel}
											</button>
										))}
									</div>
								</div>
								<div className="flex flex-col gap-1">
									<label className="text-[9px] font-medium text-zinc-500">Source</label>
									<span className="text-[9px] font-mono text-zinc-500 truncate">{selectedClip.source_file}</span>
								</div>
							</div>
							<div className="flex flex-col justify-end items-end gap-2">
								<button onClick={() => deleteClip(selectedClipId)} className="flex items-center gap-1.5 text-zinc-600 hover:text-red-400 transition-colors text-[10px] font-medium">
									<Trash2 className="h-3 w-3" /> Remove
								</button>
								<button onClick={handleApplyEdits} disabled={isSavingInspector} className="flex items-center gap-1.5 rounded bg-zinc-100 px-3 py-1.5 text-[10px] font-semibold text-black hover:bg-white transition-all disabled:opacity-50">
									{isSavingInspector ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />} Apply
								</button>
							</div>
						</div>
					</motion.div>
				)}
			</AnimatePresence>

			{/* Transition Inspector */}
			<AnimatePresence>
				{selectedTransitionClipId && transitionClip && (
					<motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 6 }} className="rounded border border-zinc-800 bg-zinc-900 p-3">
						<div className="flex items-center justify-between mb-3">
							<div className="flex items-center gap-3">
								<div className="w-2 h-2 rounded-sm rotate-45 bg-purple-500/60 border border-purple-400" />
								<h3 className="text-[10px] font-semibold text-zinc-200">
									Transition{prevTransitionClip ? `: ${prevTransitionClip.source_file} → ${transitionClip.source_file}` : ""}
								</h3>
								<span className="text-[9px] font-mono text-zinc-500">
									{TRANSITION_OPTIONS.find((t) => t.value === (transitionClip.transition || "cut"))?.label ?? "Cut"}
									{transitionClip.transition && transitionClip.transition !== "cut" && ` · ${transitionClip.transition_duration_ms ?? DEFAULT_TRANSITION_DURATION_MS}ms`}
								</span>
							</div>
							<button onClick={() => setSelectedTransitionClipId(null)} className="text-zinc-600 hover:text-zinc-300 transition-colors"><XCircle className="h-3.5 w-3.5" /></button>
						</div>
						<div className="grid grid-cols-2 gap-4">
							<div className="flex flex-col gap-1.5">
								<label className="text-[9px] font-medium text-zinc-500">Type</label>
								<div className="grid grid-cols-3 gap-1">
									{TRANSITION_OPTIONS.map((opt) => (
										<button key={opt.value} onClick={() => handleTransitionTypeChange(selectedTransitionClipId, opt.value)}
											className={cn("rounded border px-1.5 py-1.5 text-[8px] font-semibold transition-all text-center",
												(transitionClip.transition || "cut") === opt.value ? "border-blue-500 bg-blue-500/15 text-blue-400" : "border-zinc-800 bg-zinc-800/30 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300")}>
											{opt.label}
										</button>
									))}
								</div>
							</div>
							<div className="flex flex-col gap-1.5">
								<label className="text-[9px] font-medium text-zinc-500">Duration <span className="ml-1 font-mono text-zinc-400">{transitionClip.transition_duration_ms ?? DEFAULT_TRANSITION_DURATION_MS}ms</span></label>
								<input type="range" min={100} max={2000} step={50} value={transitionClip.transition_duration_ms ?? DEFAULT_TRANSITION_DURATION_MS}
									onChange={(e) => handleTransitionDurationChange(selectedTransitionClipId, Number(e.target.value))}
									className="w-full accent-blue-500 h-1.5 bg-zinc-800 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-blue-500"
									disabled={!transitionClip.transition || transitionClip.transition === "cut"} />
								<div className="flex justify-between text-[7px] text-zinc-600 font-mono">
									<span>100ms</span><span>1000ms</span><span>2000ms</span>
								</div>
								{(!transitionClip.transition || transitionClip.transition === "cut") && (
									<p className="text-[8px] text-zinc-600 italic">Duration only applies to non-cut transitions</p>
								)}
							</div>
						</div>
					</motion.div>
				)}
			</AnimatePresence>
		</div>
	);
}
