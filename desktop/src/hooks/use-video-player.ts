import { useRef, useCallback, useEffect, useState } from "react";
import { useKinetographStore } from "@/store/use-kinetograph-store";
import { getBackendUrlSync, resolveBackendUrl } from "@/lib/backend";
import type { PaperEditClip, TransitionType } from "@/types/kinetograph";

export type PlaybackState = "idle" | "playing" | "paused";

/** Info about an in-progress visual transition */
export interface TransitionState {
	active: boolean;
	type: TransitionType;
	/** 0 → 1 over the transition duration */
	progress: number;
	durationMs: number;
}

const EMPTY_TRANSITION: TransitionState = {
	active: false,
	type: "cut",
	progress: 0,
	durationMs: 0,
};

function resolveClipUrl(
	clip: PaperEditClip,
	assets: { file_name: string; file_path: string; stream_url: string }[],
): string | null {
	// Try exact match first
	const match =
		assets.find((a) => a.file_name === clip.source_file) ??
		assets.find((a) => a.file_path === clip.source_file);
	if (match) return resolveBackendUrl(match.stream_url);

	// Try matching by basename (source_file may be a full path)
	const baseName = clip.source_file.split("/").pop() ?? clip.source_file;
	const baseMatch = assets.find((a) => a.file_name === baseName);
	if (baseMatch) return resolveBackendUrl(baseMatch.stream_url);

	// Fallback: stream by absolute path
	if (clip.source_file.startsWith("/")) {
		return `${getBackendUrlSync()}/api/assets/stream?path=${encodeURIComponent(clip.source_file)}`;
	}
	return null;
}

function resolvePlayheadToClip(
	playheadMs: number,
	clips: PaperEditClip[],
): { clip: PaperEditClip; offsetMs: number; clipIndex: number } | null {
	let cursor = 0;
	for (let i = 0; i < clips.length; i++) {
		const dur = clips[i].out_ms - clips[i].in_ms;
		if (playheadMs < cursor + dur) {
			return {
				clip: clips[i],
				offsetMs: clips[i].in_ms + (playheadMs - cursor),
				clipIndex: i,
			};
		}
		cursor += dur;
	}
	return null;
}

function loadVideoSource(
	video: HTMLVideoElement,
	clip: PaperEditClip,
	assets: { file_name: string; file_path: string; stream_url: string }[],
	seekMs: number,
): Promise<boolean> {
	return new Promise((resolve) => {
		const url = resolveClipUrl(clip, assets);
		if (!url) { resolve(false); return; }

		const onReady = () => {
			video.removeEventListener("canplay", onReady);
			video.removeEventListener("error", onErr);
			video.currentTime = seekMs / 1000;
			resolve(true);
		};
		const onErr = () => {
			video.removeEventListener("canplay", onReady);
			video.removeEventListener("error", onErr);
			resolve(false);
		};

		// If same source, just seek
		if (video.src && video.src === url) {
			video.currentTime = seekMs / 1000;
			resolve(true);
			return;
		}

		video.addEventListener("canplay", onReady);
		video.addEventListener("error", onErr);
		video.src = url;
		video.load();
	});
}

export function useVideoPlayer() {
	// Two video elements for seamless transitions
	const videoARef = useRef<HTMLVideoElement>(null);
	const videoBRef = useRef<HTMLVideoElement>(null);
	/** Which element is currently "active" (foreground) */
	const activeSlotRef = useRef<"A" | "B">("A");

	const rafRef = useRef<number>(0);
	const activeClipIdRef = useRef<string | null>(null);
	const isPlayingRef = useRef(false);
	const clipsRef = useRef<PaperEditClip[]>([]);
	/** Guard: true while we're loading the next clip (prevents re-triggering advance) */
	const isAdvancingRef = useRef(false);

	// Transition state
	const transitionRef = useRef<{
		active: boolean;
		type: TransitionType;
		durationMs: number;
		startedAt: number; // performance.now()
		seqMsAtStart: number; // timeline ms when transition began
	} | null>(null);

	const [playbackState, setPlaybackState] = useState<PlaybackState>("idle");
	const [currentTimeDisplay, setCurrentTimeDisplay] = useState(0);
	const [volume, setVolumeState] = useState(1);
	const [playbackRate, setPlaybackRateState] = useState(1);
	const [transitionState, setTransitionState] = useState<TransitionState>(EMPTY_TRANSITION);

	const assets = useKinetographStore((s) => s.assets);
	const paperEdit = useKinetographStore((s) => s.paperEdit);
	const selectedClipId = useKinetographStore((s) => s.selectedClipId);
	const setPlayhead = useKinetographStore((s) => s.setPlayhead);
	const setSelectedClip = useKinetographStore((s) => s.setSelectedClip);

	const clips = paperEdit?.clips ?? [];

	useEffect(() => { clipsRef.current = clips; }, [clips]);

	const totalDurationMs = clips.reduce((sum, c) => sum + (c.out_ms - c.in_ms), 0);

	/** Get the currently-active video element */
	const getActiveVideo = useCallback(() => {
		return activeSlotRef.current === "A" ? videoARef.current : videoBRef.current;
	}, []);

	/** Get the inactive (standby) video element */
	const getStandbyVideo = useCallback(() => {
		return activeSlotRef.current === "A" ? videoBRef.current : videoARef.current;
	}, []);

	/** Swap active/standby */
	const swapSlots = useCallback(() => {
		activeSlotRef.current = activeSlotRef.current === "A" ? "B" : "A";
	}, []);

	/** Load a clip into a specific video element */
	const loadClipInto = useCallback(
		(video: HTMLVideoElement, clip: PaperEditClip, seekMs: number) => {
			return loadVideoSource(video, clip, assets, seekMs);
		},
		[assets],
	);

	const seekToClip = useCallback(
		(clipId: string) => {
			const video = getActiveVideo();
			if (!video || clips.length === 0) return;
			let offset = 0;
			for (const c of clips) {
				if (c.clip_id === clipId) break;
				offset += c.out_ms - c.in_ms;
			}
			const target = resolvePlayheadToClip(offset, clips);
			if (!target) return;
			activeClipIdRef.current = target.clip.clip_id;
			loadClipInto(video, target.clip, target.offsetMs);
			setPlayhead(offset);
			setCurrentTimeDisplay(offset);
		},
		[clips, loadClipInto, setPlayhead, getActiveVideo],
	);

	useEffect(() => {
		if (selectedClipId && !isPlayingRef.current) {
			seekToClip(selectedClipId);
		}
	}, [selectedClipId, seekToClip]);

	// ── Main rAF tick ────────────────────────────────────────────────
	const tickPlayhead = useCallback(() => {
		const activeVideo = getActiveVideo();
		if (!activeVideo) return;

		// ─── During a visual transition ────────────────────────────
		const tr = transitionRef.current;
		if (tr && tr.active) {
			const elapsed = performance.now() - tr.startedAt;
			const progress = Math.min(1, elapsed / tr.durationMs);
			const seqMs = tr.seqMsAtStart + elapsed * (playbackRate || 1);

			setPlayhead(seqMs);
			setCurrentTimeDisplay(seqMs);
			setTransitionState({
				active: true,
				type: tr.type,
				progress,
				durationMs: tr.durationMs,
			});

			if (progress >= 1) {
				// Transition complete → swap standby to foreground
				const standby = getStandbyVideo();
				if (standby) {
					// Pause outgoing video
					activeVideo.pause();
				}
				swapSlots();
				transitionRef.current = null;
				setTransitionState(EMPTY_TRANSITION);
			}

			if (isPlayingRef.current) {
				rafRef.current = requestAnimationFrame(tickPlayhead);
			}
			return;
		}

		// ─── Normal playback (no transition) ───────────────────────
		// If the active video is paused (loading), keep the loop alive
		if (activeVideo.paused && !activeVideo.ended) {
			if (isPlayingRef.current) {
				rafRef.current = requestAnimationFrame(tickPlayhead);
			}
			return;
		}

		const currentClips = clipsRef.current;
		let clipStartMs = 0;
		for (const c of currentClips) {
			if (c.clip_id === activeClipIdRef.current) break;
			clipStartMs += c.out_ms - c.in_ms;
		}
		const activeClip = currentClips.find(
			(c) => c.clip_id === activeClipIdRef.current,
		);
		if (!activeClip) {
			if (isPlayingRef.current) rafRef.current = requestAnimationFrame(tickPlayhead);
			return;
		}

		const currentVideoMs = activeVideo.currentTime * 1000;
		const seqMs = clipStartMs + (currentVideoMs - activeClip.in_ms);
		setPlayhead(seqMs);
		setCurrentTimeDisplay(seqMs);

		// ─── Check if clip is ending → advance to next ─────────────
		if ((currentVideoMs >= activeClip.out_ms - 16 || activeVideo.ended) && !isAdvancingRef.current) {
			const idx = currentClips.findIndex(
				(c) => c.clip_id === activeClipIdRef.current,
			);
			const nextClip = currentClips[idx + 1];
			if (nextClip) {
				const nextUrl = resolveClipUrl(nextClip, assets);
				if (nextUrl) {
					const transType = nextClip.transition ?? "cut";
					const transDur = transType !== "cut"
						? (nextClip.transition_duration_ms ?? 500)
						: 0;

					// Lock: prevent re-triggering while the standby loads
					isAdvancingRef.current = true;
					// Pause the outgoing video immediately so it doesn't play
					// past out_ms and show unintended frames
					if (transDur <= 0) {
						activeVideo.pause();
					}

					// Load next clip into the standby video element
					const standby = getStandbyVideo();
					if (standby) {
						loadClipInto(standby, nextClip, nextClip.in_ms).then((ok) => {
							if (!ok || !isPlayingRef.current) {
								isAdvancingRef.current = false;
								return;
							}
							standby.volume = volume;
							standby.playbackRate = playbackRate;
							standby.play().catch(() => {});

							// Now safe to update the active clip ID
							activeClipIdRef.current = nextClip.clip_id;
							setSelectedClip(nextClip.clip_id);

							if (transDur > 0) {
								// Start visual transition
								transitionRef.current = {
									active: true,
									type: transType,
									durationMs: transDur,
									startedAt: performance.now(),
									seqMsAtStart: seqMs,
								};
							} else {
								// Instant cut → swap immediately
								swapSlots();
							}
							isAdvancingRef.current = false;
						});
					} else {
						isAdvancingRef.current = false;
					}
				}
			} else {
				// Last clip → stop playback
				activeVideo.pause();
				isPlayingRef.current = false;
				setPlaybackState("paused");
			}
		}

		if (isPlayingRef.current) {
			rafRef.current = requestAnimationFrame(tickPlayhead);
		}
	}, [assets, setPlayhead, setSelectedClip, getActiveVideo, getStandbyVideo, swapSlots, loadClipInto, volume, playbackRate]);

	const play = useCallback(async () => {
		const video = getActiveVideo();
		if (!video || clips.length === 0) return;

		// Cancel any in-progress transition
		transitionRef.current = null;
		setTransitionState(EMPTY_TRANSITION);
		isAdvancingRef.current = false;

		const currentMs = useKinetographStore.getState().playheadMs;
		const target = resolvePlayheadToClip(currentMs, clips);

		if (!target) {
			const first = clips[0];
			activeClipIdRef.current = first.clip_id;
			const ok = await loadClipInto(video, first, first.in_ms);
			if (!ok) return;
			setSelectedClip(first.clip_id);
			setPlayhead(0);
			setCurrentTimeDisplay(0);
		} else {
			activeClipIdRef.current = target.clip.clip_id;
			const ok = await loadClipInto(video, target.clip, target.offsetMs);
			if (!ok) return;
			setSelectedClip(target.clip.clip_id);
		}

		video.volume = volume;
		video.playbackRate = playbackRate;
		try {
			await video.play();
			isPlayingRef.current = true;
			setPlaybackState("playing");
			rafRef.current = requestAnimationFrame(tickPlayhead);
		} catch {
			/* autoplay blocked */
		}
	}, [clips, loadClipInto, tickPlayhead, setSelectedClip, setPlayhead, volume, playbackRate, getActiveVideo]);

	const pause = useCallback(() => {
		const active = getActiveVideo();
		const standby = getStandbyVideo();
		active?.pause();
		standby?.pause();
		isPlayingRef.current = false;
		isAdvancingRef.current = false;
		transitionRef.current = null;
		setTransitionState(EMPTY_TRANSITION);
		setPlaybackState("paused");
		cancelAnimationFrame(rafRef.current);
	}, [getActiveVideo, getStandbyVideo]);

	const stop = useCallback(() => {
		const active = getActiveVideo();
		const standby = getStandbyVideo();
		active?.pause();
		standby?.pause();
		isPlayingRef.current = false;
		isAdvancingRef.current = false;
		transitionRef.current = null;
		setTransitionState(EMPTY_TRANSITION);
		setPlaybackState("idle");
		cancelAnimationFrame(rafRef.current);
		setPlayhead(0);
		setCurrentTimeDisplay(0);
		activeClipIdRef.current = null;
		activeSlotRef.current = "A";
	}, [setPlayhead, getActiveVideo, getStandbyVideo]);

	const togglePlayPause = useCallback(() => {
		if (playbackState === "playing") pause();
		else play();
	}, [playbackState, play, pause]);

	const seekTo = useCallback(
		async (ms: number) => {
			const video = getActiveVideo();
			if (!video || clips.length === 0) return;

			// Cancel any transition
			transitionRef.current = null;
			setTransitionState(EMPTY_TRANSITION);
			isAdvancingRef.current = false;
			getStandbyVideo()?.pause();

			const clamped = Math.max(0, Math.min(ms, totalDurationMs));
			const target = resolvePlayheadToClip(clamped, clips);
			if (!target) return;
			activeClipIdRef.current = target.clip.clip_id;
			const ok = await loadClipInto(video, target.clip, target.offsetMs);
			if (!ok) return;
			setSelectedClip(target.clip.clip_id);
			setPlayhead(clamped);
			setCurrentTimeDisplay(clamped);
			if (isPlayingRef.current) {
				video.volume = volume;
				video.playbackRate = playbackRate;
				video.play().catch(() => {});
			}
		},
		[clips, totalDurationMs, loadClipInto, setPlayhead, setSelectedClip, volume, playbackRate, getActiveVideo, getStandbyVideo],
	);

	const setVolume = useCallback((v: number) => {
		setVolumeState(v);
		if (videoARef.current) videoARef.current.volume = v;
		if (videoBRef.current) videoBRef.current.volume = v;
	}, []);

	const setPlaybackRate = useCallback((rate: number) => {
		setPlaybackRateState(rate);
		if (videoARef.current) videoARef.current.playbackRate = rate;
		if (videoBRef.current) videoBRef.current.playbackRate = rate;
	}, []);

	useEffect(() => {
		return () => cancelAnimationFrame(rafRef.current);
	}, []);

	return {
		videoARef,
		videoBRef,
		activeSlot: activeSlotRef,
		transitionState,
		playbackState,
		currentTimeDisplay,
		totalDurationMs,
		volume,
		playbackRate,
		play,
		pause,
		stop,
		togglePlayPause,
		seekTo,
		seekToClip,
		setVolume,
		setPlaybackRate,
	};
}
