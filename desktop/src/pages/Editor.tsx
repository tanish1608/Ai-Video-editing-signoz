import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import {
  Group,
  Panel,
  Separator,
  PanelImperativeHandle,
} from "react-resizable-panels";
import { useKinetographStore } from "@/store/use-kinetograph-store";
import { useChatStore } from "@/store/use-chat-store";
import { useVideoPlayer } from "@/hooks/use-video-player";
import { useKinetographWS } from "@/hooks/use-kinetograph-ws";
import { KinetographAPI } from "@/lib/api";
import { getBackendUrlSync, resolveBackendUrl } from "@/lib/backend";
import { connectProvider, disconnectProvider } from "@/lib/crdt";
import { AssetDropzone } from "@/components/asset-dropzone";
import { TimelineEditor } from "@/components/timeline-editor";
import { ExportPanel } from "@/components/export-panel";
import { ChatPanel } from "@/components/chat-panel";
import { ColorGradePanel } from "@/components/color-grade-panel";
import { CaptionsPanel } from "@/components/captions-panel";
import {
  Film,
  Layout,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Pause,
  Square,
  SkipBack,
  SkipForward,
  Volume2,
  VolumeX,
  FolderOpen,
  Sparkles,
  Palette,
  Type,
  Settings,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { PREVIEW_RESOLUTIONS, PreviewResolution } from "@/types/kinetograph";

const FPS = 30;

function formatTimecode(ms: number) {
  const totalFrames = Math.max(0, Math.floor((ms / 1000) * FPS));
  const frames = totalFrames % FPS;
  const totalSeconds = Math.floor(totalFrames / FPS);
  const seconds = totalSeconds % 60;
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  return `${hours.toString().padStart(2, "0")}:${minutes
    .toString()
    .padStart(2, "0")}:${seconds.toString().padStart(2, "0")}:${frames
    .toString()
    .padStart(2, "0")}`;
}

const PLAYBACK_RATES = [0.25, 0.5, 1, 1.5, 2, 4];

export function Editor() {
  const [isSidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [showExportPanel, setShowExportPanel] = useState(false);
  const [sidebarTab, setSidebarTab] = useState<"media" | "color" | "captions">("media");
  const leftPanelRef = useRef<PanelImperativeHandle>(null);

  const setAssets = useKinetographStore((s) => s.setAssets);
  const assets = useKinetographStore((s) => s.assets);
  const undo = useKinetographStore((s) => s.undo);
  const redo = useKinetographStore((s) => s.redo);
  const renderUrl = useKinetographStore((s) => s.renderUrl);
  const setPlayhead = useKinetographStore((s) => s.setPlayhead);
  const paperEdit = useKinetographStore((s) => s.paperEdit);
  const v2Clips = useKinetographStore((s) => s.v2Clips);
  const tracks = useKinetographStore((s) => s.tracks);
  const musicPath = useKinetographStore((s) => s.musicPath);
  const previewResolution = useKinetographStore((s) => s.previewResolution);
  const setPreviewResolution = useKinetographStore((s) => s.setPreviewResolution);
  const colorGrade = useKinetographStore((s) => s.colorGrade);

  const isChatOpen = useChatStore((s) => s.isOpen);
  const setChatOpen = useChatStore((s) => s.setOpen);
  const toggleChat = useChatStore((s) => s.toggleOpen);
  const pipelineActive = useChatStore((s) => s.pipelineActive);

  // CSS filter for real-time color-grade preview
  const colorGradeFilter = useMemo(() => {
    const parts: string[] = [];
    if (Math.abs(colorGrade.brightness) > 0.001)
      parts.push(`brightness(${1 + colorGrade.brightness})`);
    if (Math.abs(colorGrade.contrast - 1) > 0.001)
      parts.push(`contrast(${colorGrade.contrast})`);
    if (Math.abs(colorGrade.saturation - 1) > 0.001)
      parts.push(`saturate(${colorGrade.saturation})`);
    if (Math.abs(colorGrade.gamma - 1) > 0.05) {
      const gammaCorrection = 1 / colorGrade.gamma;
      parts.push(`brightness(${gammaCorrection})`);
    }
    if (Math.abs(colorGrade.temperature) > 0.01) {
      if (colorGrade.temperature > 0) {
        parts.push(`sepia(${colorGrade.temperature * 0.3})`);
      } else {
        parts.push(`hue-rotate(${colorGrade.temperature * 30}deg)`);
      }
    }
    return parts.length > 0 ? parts.join(" ") : "none";
  }, [colorGrade]);

  const { isConnected } = useKinetographWS();

  // Connect the Yjs CRDT provider for real-time paper-edit sync
  useEffect(() => {
    connectProvider();
    return () => disconnectProvider();
  }, []);

  const {
    videoARef,
    videoBRef,
    activeSlot,
    transitionState,
    playbackState,
    currentTimeDisplay,
    totalDurationMs,
    volume,
    playbackRate,
    togglePlayPause,
    stop,
    seekTo,
    setVolume,
    setPlaybackRate,
  } = useVideoPlayer();

  // ── Rendered video mode ──
  const renderedVideoRef = useRef<HTMLVideoElement>(null);
  const musicAudioRef = useRef<HTMLAudioElement>(null);
  const isRenderedMode = !!renderUrl;
  const [rvTime, setRvTime] = useState(0);
  const [rvDuration, setRvDuration] = useState(0);
  const [rvPlaying, setRvPlaying] = useState(false);

  // ── Track volumes ──
  const a1Track = tracks.find((t) => t.id === "A1");
  const a2Track = tracks.find((t) => t.id === "A2");
  const a1Volume = a1Track?.muted ? 0 : (a1Track?.volume ?? 1);
  const a2Volume = a2Track?.muted ? 0 : (a2Track?.volume ?? 0.35);

  // ── V2 Overlay ──
  const v2VideoRef = useRef<HTMLVideoElement>(null);
  const loadedV2SrcRef = useRef<string | null>(null);
  const [v2Ready, setV2Ready] = useState(false);
  const effTimeRef = useRef(0); // updated below after effTime is computed

  // Find the V2 clip whose timeline range contains the current playhead
  // Only in editor mode — rendered video already has overlays composited.
  const visibleV2Clip = useMemo(() => {
    if (isRenderedMode) return null;
    if (v2Clips.length === 0) return null;
    const currentMs = currentTimeDisplay;
    return v2Clips.find(clip => {
      const clipEnd = clip.timelineStartMs + (clip.outMs - clip.inMs);
      return currentMs >= clip.timelineStartMs && currentMs < clipEnd;
    }) ?? null;
  }, [v2Clips, isRenderedMode, currentTimeDisplay]);

  const v2StreamUrl = useMemo(() => {
    if (!visibleV2Clip) return null;
    const asset = assets.find((a) => a.id === visibleV2Clip.sourceAssetId);
    return asset?.stream_url ?? null;
  }, [visibleV2Clip, assets]);

  useEffect(() => {
    const video = v2VideoRef.current;
    if (!video) return;
    if (!v2StreamUrl) {
      video.pause();
      video.removeAttribute("src");
      video.load();
      loadedV2SrcRef.current = null;
      setV2Ready(false);
      return;
    }
    let fullUrl: string;
    try { fullUrl = new URL(v2StreamUrl, getBackendUrlSync()).href; } catch { fullUrl = v2StreamUrl; }
    if (loadedV2SrcRef.current === fullUrl) return;

    setV2Ready(false);
    loadedV2SrcRef.current = fullUrl;
    // Compute seek position: source inMs + offset within clip based on current playhead
    const currentMs = effTimeRef.current;
    const clipOffset = visibleV2Clip ? Math.max(0, currentMs - visibleV2Clip.timelineStartMs) : 0;
    const seekTime = ((visibleV2Clip?.inMs ?? 0) + clipOffset) / 1000;
    video.src = fullUrl;
    video.load();
    const onCanPlay = () => {
      try {
        video.currentTime = seekTime;
        setV2Ready(true);
        if (playbackState === "playing") video.play().catch(() => {});
      } catch { /* */ }
    };
    const onError = () => {
      setV2Ready(false);
      loadedV2SrcRef.current = null;
    };
    video.addEventListener("canplay", onCanPlay, { once: true });
    video.addEventListener("error", onError, { once: true });
    return () => {
      video.removeEventListener("canplay", onCanPlay);
      video.removeEventListener("error", onError);
    };
  }, [v2StreamUrl, visibleV2Clip?.inMs, playbackState]);

  // V2 overlay: keep seek position in sync with main playhead
  useEffect(() => {
    const video = v2VideoRef.current;
    if (!video || !visibleV2Clip || !v2Ready) return;

    const unsub = useKinetographStore.subscribe((state, prev) => {
      if (state.playheadMs === prev.playheadMs) return;
      const currentMs = state.playheadMs;
      const clipEnd = visibleV2Clip.timelineStartMs + (visibleV2Clip.outMs - visibleV2Clip.inMs);
      if (currentMs >= visibleV2Clip.timelineStartMs && currentMs < clipEnd) {
        const expected = (visibleV2Clip.inMs + (currentMs - visibleV2Clip.timelineStartMs)) / 1000;
        // Only re-seek if drift exceeds 150ms to avoid constant seeking
        if (Math.abs(video.currentTime - expected) > 0.15) {
          video.currentTime = expected;
        }
        // Ensure video is actually playing during active playback
        if (video.paused && playbackState === "playing") {
          video.play().catch(() => {});
        }
      }
    });

    // Handle video ending (source file shorter than clip range) —
    // hold last frame and keep video element in paused state.
    const onEnded = () => {
      if (video.duration > 0) {
        video.currentTime = Math.max(0, video.duration - 0.01);
      }
    };
    video.addEventListener("ended", onEnded);

    return () => {
      unsub();
      video.removeEventListener("ended", onEnded);
    };
  }, [visibleV2Clip, v2Ready, playbackState]);

  useEffect(() => {
    const video = v2VideoRef.current;
    if (!video || !v2StreamUrl || !video.src || !v2Ready) return;
    try {
      if (playbackState === "playing") video.play().catch(() => {});
      else if (video.readyState >= 2) video.pause();
    } catch { /* */ }
  }, [playbackState, v2StreamUrl, v2Ready]);

  useEffect(() => {
    const v = renderedVideoRef.current;
    if (!v || !renderUrl) return;
    setRvTime(0); setRvDuration(0); setRvPlaying(false);
    v.load();
    const onTime = () => setRvTime(v.currentTime * 1000);
    const onDur = () => { if (v.duration && isFinite(v.duration)) setRvDuration(v.duration * 1000); };
    const onPlay = () => setRvPlaying(true);
    const onPause = () => setRvPlaying(false);
    v.addEventListener("timeupdate", onTime);
    v.addEventListener("loadedmetadata", onDur);
    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    return () => {
      v.removeEventListener("timeupdate", onTime);
      v.removeEventListener("loadedmetadata", onDur);
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
    };
  }, [renderUrl]);

  const sequenceDurationMs = useMemo(() => {
    if (!paperEdit) return 0;
    return paperEdit.clips.reduce((s, c) => s + (c.out_ms - c.in_ms), 0);
  }, [paperEdit]);

  useEffect(() => {
    if (isRenderedMode && rvDuration > 0) {
      const progress = rvTime / rvDuration;
      const mappedMs = progress * (sequenceDurationMs || rvDuration);
      setPlayhead(mappedMs);
    }
  }, [isRenderedMode, rvTime, rvDuration, sequenceDurationMs, setPlayhead]);

  const effTime = isRenderedMode ? rvTime : currentTimeDisplay;
  const effDuration = isRenderedMode ? rvDuration : totalDurationMs;
  const effPlaying = isRenderedMode ? rvPlaying : playbackState === "playing";
  effTimeRef.current = effTime;

  // Apply track volumes
  useEffect(() => {
    const effectiveA1 = a1Volume * volume;
    if (videoARef.current) videoARef.current.volume = effectiveA1;
    if (videoBRef.current) videoBRef.current.volume = effectiveA1;
    if (renderedVideoRef.current) renderedVideoRef.current.volume = effectiveA1;
  }, [a1Volume, volume, videoARef, videoBRef]);

  useEffect(() => {
    if (musicAudioRef.current) musicAudioRef.current.volume = a2Volume * volume;
  }, [a2Volume, volume]);

  // Music playback
  const musicStreamUrl = useMemo(() => {
    if (!musicPath) return null;
    return `${getBackendUrlSync()}/api/assets/stream?path=${encodeURIComponent(musicPath)}`;
  }, [musicPath]);

  useEffect(() => {
    const audio = musicAudioRef.current;
    if (!audio) return;
    if (!musicStreamUrl) { audio.pause(); audio.removeAttribute("src"); audio.load(); return; }
    if (audio.src && audio.src.includes(encodeURIComponent(musicPath || ""))) return;
    audio.src = musicStreamUrl;
    audio.loop = true;
    audio.volume = a2Volume * volume;
    audio.load();
  }, [musicStreamUrl, musicPath, a2Volume, volume]);

  useEffect(() => {
    const audio = musicAudioRef.current;
    if (!audio || !musicStreamUrl) return;
    if (effPlaying) audio.play().catch(() => {});
    else audio.pause();
  }, [effPlaying, musicStreamUrl]);

  useEffect(() => {
    const audio = musicAudioRef.current;
    if (!audio || !musicStreamUrl || !audio.duration) return;
    const musicSec = (effTime / 1000) % (audio.duration || 1);
    if (Math.abs(audio.currentTime - musicSec) > 0.5) audio.currentTime = musicSec;
  }, [effTime, musicStreamUrl]);

  const handlePlayPause = useCallback(() => {
    if (isRenderedMode && renderedVideoRef.current) {
      const v = renderedVideoRef.current;
      if (v.paused) v.play().catch(() => {}); else v.pause();
    } else { togglePlayPause(); }
  }, [isRenderedMode, togglePlayPause]);

  const handleStop = useCallback(() => {
    if (isRenderedMode && renderedVideoRef.current) {
      const v = renderedVideoRef.current;
      v.pause(); v.currentTime = 0; setRvTime(0);
    } else { stop(); }
  }, [isRenderedMode, stop]);

  const handleSeekTo = useCallback((ms: number) => {
    if (isRenderedMode && renderedVideoRef.current) {
      const v = renderedVideoRef.current;
      const totalClipMs = sequenceDurationMs || rvDuration;
      const progress = totalClipMs > 0 ? ms / totalClipMs : 0;
      const videoSec = (progress * rvDuration) / 1000;
      v.currentTime = Math.max(0, Math.min(videoSec, v.duration || 0));
    } else { seekTo(ms); }
    const v2 = v2VideoRef.current;
    if (v2 && v2.src) {
      const v2State = useKinetographStore.getState().v2Clips;
      for (const clip of v2State) {
        const clipEnd = clip.timelineStartMs + (clip.outMs - clip.inMs);
        if (ms >= clip.timelineStartMs && ms < clipEnd) {
          v2.currentTime = (clip.inMs + (ms - clip.timelineStartMs)) / 1000;
          return;
        }
      }
    }
  }, [isRenderedMode, seekTo, sequenceDurationMs, rvDuration]);

  const handleSetVolume = useCallback((v: number) => {
    setVolume(v);
    if (renderedVideoRef.current) renderedVideoRef.current.volume = v;
  }, [setVolume]);

  const handleSetPlaybackRate = useCallback((rate: number) => {
    setPlaybackRate(rate);
    if (renderedVideoRef.current) renderedVideoRef.current.playbackRate = rate;
  }, [setPlaybackRate]);

  useEffect(() => {
    KinetographAPI.getAssets().then((res) => setAssets(res.assets)).catch(() => undefined);
  }, [setAssets]);

  // Native media import from Electron menu — reference-based (no copy)
  useEffect(() => {
    if (!window.electron) return;
    const unsub = window.electron.onImportMedia(async () => {
      const filePaths = await window.electron.importMedia();
      if (filePaths.length === 0) return;
      try {
        await KinetographAPI.registerAssets(filePaths);
        const updated = await KinetographAPI.getAssets();
        setAssets(updated.assets);
      } catch (err) {
        console.error("Failed to register media:", err);
      }
    });
    return () => { unsub(); };
  }, [setAssets]);

  // Keyboard shortcuts
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;

      if ((e.metaKey || e.ctrlKey) && e.key === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      else if ((e.metaKey || e.ctrlKey) && e.key === "z" && e.shiftKey) { e.preventDefault(); redo(); }
      else if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); setShowExportPanel(true); }
      else if ((e.metaKey || e.ctrlKey) && e.key === "l") { e.preventDefault(); toggleChat(); }
      else if (e.key === " ") { e.preventDefault(); handlePlayPause(); }
      else if (e.key === "Escape") { handleStop(); setShowExportPanel(false); }
      else if (e.key === "j") { handleSeekTo(Math.max(0, effTime - 5000)); }
      else if (e.key === "k") { handlePlayPause(); }
      else if (e.key === "l" && !(e.metaKey || e.ctrlKey)) { handleSeekTo(effTime + 5000); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); handleSeekTo(Math.max(0, effTime - (e.shiftKey ? 1000 : 1000 / FPS))); }
      else if (e.key === "ArrowRight") { e.preventDefault(); handleSeekTo(effTime + (e.shiftKey ? 1000 : 1000 / FPS)); }
      else if (e.key === "Home") { handleSeekTo(0); }
      else if (e.key === "End") { handleSeekTo(effDuration); }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [handlePlayPause, handleStop, handleSeekTo, effTime, effDuration, undo, redo, toggleChat]);

  const toggleSidebar = () => {
    const panel = leftPanelRef.current;
    if (panel) {
      if (isSidebarCollapsed) panel.expand(); else panel.collapse();
      setSidebarCollapsed(!isSidebarCollapsed);
    }
  };

  return (
    <div className="flex h-screen w-full flex-col bg-[#0c0c0e] text-[#d1d1d1] overflow-hidden selection:bg-blue-500/30">
      {showExportPanel && <ExportPanel onClose={() => setShowExportPanel(false)} />}
      <audio ref={musicAudioRef} preload="auto" className="hidden" />

      {/* Top Bar — with drag region for macOS */}
      <header className="flex h-9 items-center justify-between border-b border-zinc-800 bg-[#1a1a1e] px-3 z-30">
        {/* Drag region + left content */}
        <div className="flex items-center gap-4">
          {/* macOS traffic lights spacer */}
          <div className="drag-region w-16 h-full" />
          <div className="flex items-center gap-2 no-drag">
            <Film className="h-3.5 w-3.5 text-blue-500" />
            <span className="text-[11px] font-semibold tracking-wide text-zinc-200">Kinetograph</span>
          </div>
        </div>

        <div className="flex items-center gap-2 no-drag">
          {/* Connection indicator */}
          <div className="flex items-center gap-1 px-1.5" title={isConnected ? "Backend connected" : "Backend disconnected"}>
            <div className={cn("w-1.5 h-1.5 rounded-full", isConnected ? "bg-emerald-400" : "bg-red-400 animate-pulse")} />
            <span className="text-[9px] text-zinc-600">{isConnected ? "Live" : "Offline"}</span>
          </div>

          {/* Transport */}
          <div className="flex items-center gap-0.5 border-x border-zinc-800 px-2 h-9">
            <button onClick={() => handleSeekTo(0)} className="p-1 hover:bg-zinc-800 rounded text-zinc-500 hover:text-white transition-colors" title="Go to start (Home)">
              <SkipBack className="h-3 w-3" />
            </button>
            <button
              onClick={handlePlayPause}
              className={cn(
                "p-1 rounded transition-colors",
                effPlaying
                  ? "bg-blue-600/20 text-blue-400 hover:bg-blue-600/30"
                  : "hover:bg-zinc-800 text-zinc-400 hover:text-white",
              )}
              title={effPlaying ? "Pause (K/Space)" : "Play (K/Space)"}
            >
              {effPlaying ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3 fill-current" />}
            </button>
            <button onClick={handleStop} className="p-1 hover:bg-zinc-800 rounded text-zinc-500 hover:text-white transition-colors" title="Stop (Esc)">
              <Square className="h-2.5 w-2.5" />
            </button>
            <button onClick={() => handleSeekTo(effDuration)} className="p-1 hover:bg-zinc-800 rounded text-zinc-500 hover:text-white transition-colors" title="Go to end (End)">
              <SkipForward className="h-3 w-3" />
            </button>
          </div>

          {/* Timecode */}
          <div className="flex items-center gap-1.5 px-2">
            <span className="text-[11px] font-mono tabular text-blue-400/90">{formatTimecode(effTime)}</span>
            <span className="text-[9px] font-mono tabular text-zinc-600">/ {formatTimecode(effDuration)}</span>
          </div>

          {/* Volume */}
          <div className="flex items-center gap-1 border-l border-zinc-800 pl-2">
            <button onClick={() => handleSetVolume(volume > 0 ? 0 : 1)} className="p-1 hover:bg-zinc-800 rounded text-zinc-500 hover:text-white transition-colors">
              {volume > 0 ? <Volume2 className="h-3 w-3" /> : <VolumeX className="h-3 w-3" />}
            </button>
            <input type="range" min={0} max={1} step={0.05} value={volume} onChange={(e) => handleSetVolume(Number(e.target.value))} className="w-14 h-1 accent-blue-500 cursor-pointer" />
          </div>

          {/* Speed */}
          <select
            value={playbackRate}
            onChange={(e) => handleSetPlaybackRate(Number(e.target.value))}
            className="bg-zinc-900 border border-zinc-800 rounded text-[10px] text-zinc-400 px-1 py-0.5 cursor-pointer outline-none"
          >
            {PLAYBACK_RATES.map((r) => (<option key={r} value={r}>{r}×</option>))}
          </select>

          {/* AI Chat Toggle */}
          <div className="border-l border-zinc-800 pl-2">
            <button
              onClick={toggleChat}
              className={cn(
                "flex items-center gap-1.5 px-2 py-1 rounded-md text-[10px] font-medium transition-all",
                isChatOpen
                  ? "bg-purple-600/20 text-purple-400 border border-purple-500/30"
                  : "hover:bg-zinc-800 text-zinc-500 hover:text-zinc-300 border border-transparent",
                pipelineActive && !isChatOpen && "text-purple-400 animate-pulse",
              )}
              title="AI Assistant (⌘L)"
            >
              <Sparkles className="h-3 w-3" />
              <span>AI</span>
              {pipelineActive && <span className="h-1.5 w-1.5 rounded-full bg-purple-400 animate-pulse" />}
            </button>
          </div>

          {/* Settings */}
          <button
            onClick={() => document.dispatchEvent(new CustomEvent("navigate-settings"))}
            className="p-1 hover:bg-zinc-800 rounded text-zinc-500 hover:text-white transition-colors"
            title="Settings (⌘,)"
          >
            <Settings className="h-3 w-3" />
          </button>
        </div>
      </header>

      {/* Main workspace */}
      <main className="flex-1 flex overflow-hidden">
        <div className="flex-1 min-w-0">
          <Group orientation="horizontal" className="h-full">
            {/* Left sidebar */}
            <Panel
              panelRef={leftPanelRef}
              defaultSize={20}
              minSize={12}
              collapsible
              className={cn("flex flex-col border-r border-zinc-800 bg-[#121215]", isSidebarCollapsed && "hidden")}
            >
              <div className="flex items-center h-7 border-b border-zinc-800 bg-zinc-900/50">
                <button
                  onClick={() => setSidebarTab("media")}
                  className={cn(
                    "flex items-center gap-1.5 flex-1 justify-center h-full text-[10px] font-medium transition-colors border-b-2",
                    sidebarTab === "media" ? "border-blue-500 text-zinc-300" : "border-transparent text-zinc-500 hover:text-zinc-400",
                  )}
                >
                  <FolderOpen className="h-3 w-3" />
                  Media
                  <span className="text-[8px] font-mono text-zinc-600">{assets.length}</span>
                </button>
                <button
                  onClick={() => setSidebarTab("color")}
                  className={cn(
                    "flex items-center gap-1.5 flex-1 justify-center h-full text-[10px] font-medium transition-colors border-b-2",
                    sidebarTab === "color" ? "border-blue-500 text-zinc-300" : "border-transparent text-zinc-500 hover:text-zinc-400",
                  )}
                >
                  <Palette className="h-3 w-3" />
                  Color
                </button>
                <button
                  onClick={() => setSidebarTab("captions")}
                  className={cn(
                    "flex items-center gap-1.5 flex-1 justify-center h-full text-[10px] font-medium transition-colors border-b-2",
                    sidebarTab === "captions" ? "border-blue-500 text-zinc-300" : "border-transparent text-zinc-500 hover:text-zinc-400",
                  )}
                >
                  <Type className="h-3 w-3" />
                  CC
                </button>
              </div>
              <div className="flex-1 min-h-0 p-2">
                {sidebarTab === "media" && <AssetDropzone />}
                {sidebarTab === "color" && (
                  <div className="h-full overflow-y-auto custom-scrollbar">
                    <ColorGradePanel />
                  </div>
                )}
                {sidebarTab === "captions" && (
                  <div className="h-full overflow-y-auto custom-scrollbar">
                    <CaptionsPanel />
                  </div>
                )}
              </div>
            </Panel>

            <div>
              <button onClick={toggleSidebar} className="p-1.5 hover:bg-zinc-800 rounded text-zinc-500 hover:text-zinc-300 transition-colors" title={isSidebarCollapsed ? "Show sidebar" : "Hide sidebar"}>
                {isSidebarCollapsed ? <PanelLeftOpen className="h-3.5 w-3.5" /> : <PanelLeftClose className="h-3.5 w-3.5" />}
              </button>
            </div>

            <Separator className="w-0.5 bg-black/40 hover:bg-blue-500/20 transition-colors cursor-col-resize" />

            {/* Center: Viewer + Timeline */}
            <Panel defaultSize={80}>
              <Group orientation="vertical">
                {/* Viewer */}
                <Panel defaultSize={60} minSize={25}>
                  <div className="flex h-full flex-col overflow-hidden bg-[#0e0e10]">
                    <div className="flex flex-1 items-center justify-center p-4 relative">
                      {/* Preview resolution picker */}
                      <div className="absolute top-2 right-2 z-40">
                        <select
                          value={previewResolution}
                          onChange={(e) => {
                            const res = e.target.value as PreviewResolution;
                            setPreviewResolution(res);
                            const dims = PREVIEW_RESOLUTIONS[res];
                            KinetographAPI.updateConfig({
                              output_width: dims.width,
                              output_height: dims.height,
                              output_orientation: dims.height > dims.width ? "portrait" : "landscape",
                            }).catch(() => {});
                          }}
                          className="bg-zinc-900/80 border border-zinc-700 text-zinc-300 text-[9px] rounded px-1.5 py-0.5 cursor-pointer hover:bg-zinc-800 focus:outline-none focus:ring-1 focus:ring-blue-500/50 backdrop-blur-sm"
                        >
                          {(Object.entries(PREVIEW_RESOLUTIONS) as [PreviewResolution, { label: string }][]).map(([key, val]) => (
                            <option key={key} value={key}>{val.label}</option>
                          ))}
                        </select>
                      </div>
                      <div
                        className="relative overflow-hidden border border-zinc-800/50 bg-black"
                        style={{
                          aspectRatio: `${PREVIEW_RESOLUTIONS[previewResolution].width} / ${PREVIEW_RESOLUTIONS[previewResolution].height}`,
                          height: "100%",
                          maxWidth: "100%",
                          filter: colorGradeFilter,
                        }}
                      >
                        {isRenderedMode ? (
                          <>
                            <video ref={renderedVideoRef} src={renderUrl!} className="absolute inset-0 h-full w-full object-contain" style={{ zIndex: 2 }} playsInline preload="auto" />
                            <div className="absolute top-2 left-2 z-20 flex items-center gap-1 bg-emerald-600/60 backdrop-blur-sm px-1.5 py-0.5 rounded pointer-events-none">
                              <div className="h-1.5 w-1.5 rounded-full bg-emerald-300" />
                              <span className="text-[8px] font-medium text-white">Final Render</span>
                            </div>
                          </>
                        ) : (!paperEdit || paperEdit.clips.length === 0) ? (
                          <div className="absolute inset-0 flex flex-col items-center justify-center text-zinc-600 z-10">
                            <Film className="h-8 w-8 mb-2 text-zinc-700" />
                            <span className="text-[11px] font-medium">No clips in timeline</span>
                            <span className="text-[9px] text-zinc-700 mt-0.5">Drag media to the timeline to start editing</span>
                          </div>
                        ) : (
                          <>
                            <video
                              ref={videoARef}
                              className="absolute inset-0 h-full w-full object-contain"
                              style={{
                                zIndex: activeSlot.current === "A" ? 2 : 1,
                                opacity: transitionState.active
                                  ? (activeSlot.current === "A" ? 1 - transitionState.progress : transitionState.progress)
                                  : (activeSlot.current === "A" ? 1 : 0),
                              }}
                              playsInline preload="metadata"
                            />
                            <video
                              ref={videoBRef}
                              className="absolute inset-0 h-full w-full object-contain"
                              style={{
                                zIndex: activeSlot.current === "B" ? 2 : 1,
                                opacity: transitionState.active
                                  ? (activeSlot.current === "B" ? 1 - transitionState.progress : transitionState.progress)
                                  : (activeSlot.current === "B" ? 1 : 0),
                              }}
                              playsInline preload="metadata"
                            />

                            {transitionState.active && (transitionState.type === "fade-to-black" || transitionState.type === "fade-to-white") && (
                              <div className="absolute inset-0 z-10 pointer-events-none" style={{
                                background: transitionState.type === "fade-to-black" ? "black" : "white",
                                opacity: transitionState.progress < 0.5 ? transitionState.progress * 2 : 2 - transitionState.progress * 2,
                              }} />
                            )}

                            {transitionState.active && (transitionState.type === "wipe-left" || transitionState.type === "wipe-right") && (
                              <div className="absolute inset-0 z-10 pointer-events-none overflow-hidden">
                                <div className="absolute inset-0" style={{
                                  background: "black", opacity: 0.08,
                                  clipPath: transitionState.type === "wipe-right"
                                    ? `inset(0 ${(1 - transitionState.progress) * 100}% 0 0)`
                                    : `inset(0 0 0 ${(1 - transitionState.progress) * 100}%)`,
                                }} />
                              </div>
                            )}

                            {transitionState.active && (
                              <div className="absolute top-2 right-2 z-20 flex items-center gap-1 bg-black/60 backdrop-blur-sm px-1.5 py-0.5 rounded pointer-events-none">
                                <div className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />
                                <span className="text-[8px] font-medium text-zinc-300 capitalize">{transitionState.type.replace(/-/g, " ")}</span>
                              </div>
                            )}

                            {playbackState === "playing" && !transitionState.active && (
                              <div className="absolute top-2 left-2 z-20 flex items-center gap-1 bg-black/60 backdrop-blur-sm px-1.5 py-0.5 rounded pointer-events-none">
                                <div className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
                                <span className="text-[8px] font-medium text-zinc-300">PLAY</span>
                              </div>
                            )}
                          </>
                        )}

                        {/* V2 Overlay */}
                        <div
                          className="absolute overflow-hidden pointer-events-none z-10"
                          style={{
                            left: visibleV2Clip ? `${visibleV2Clip.transform.x}%` : 0,
                            top: visibleV2Clip ? `${visibleV2Clip.transform.y}%` : 0,
                            width: visibleV2Clip ? `${visibleV2Clip.transform.width}%` : 0,
                            height: visibleV2Clip ? `${visibleV2Clip.transform.height}%` : 0,
                            opacity: (visibleV2Clip && v2Ready) ? visibleV2Clip.transform.opacity : 0,
                            borderRadius: visibleV2Clip ? `${visibleV2Clip.transform.borderRadius}px` : 0,
                            transition: "opacity 0.5s ease-in-out",
                          }}
                        >
                          <video ref={v2VideoRef} className="h-full w-full object-cover" playsInline muted preload="auto" />
                        </div>
                      </div>
                    </div>
                  </div>
                </Panel>

                <Separator className="h-0.5 bg-black/40 hover:bg-blue-500/20 transition-colors cursor-row-resize" />

                {/* Timeline */}
                <Panel defaultSize={40} minSize={20}>
                  <div className="h-full flex flex-col bg-[#121215]">
                    <div className="flex h-7 items-center justify-between border-b border-zinc-800 bg-zinc-900/50 px-3">
                      <div className="flex items-center gap-2">
                        <Layout className="h-3 w-3 text-zinc-500" />
                        <span className="text-[10px] font-medium text-zinc-400">Timeline</span>
                      </div>
                      <span className="text-[9px] font-mono text-zinc-600">{FPS} FPS</span>
                    </div>
                    <div className="flex-1 p-3 overflow-y-auto custom-scrollbar">
                      <TimelineEditor onSeek={handleSeekTo} />
                    </div>
                  </div>
                </Panel>
              </Group>
            </Panel>
          </Group>
        </div>

        {/* AI Chat Panel */}
        {isChatOpen && (
          <div className="w-[340px] shrink-0 border-l border-zinc-800" style={{ animation: "slideInRight 0.15s ease-out" }}>
            <ChatPanel onClose={() => setChatOpen(false)} />
          </div>
        )}
      </main>
    </div>
  );
}
