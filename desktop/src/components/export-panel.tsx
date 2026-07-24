import { useKinetographStore } from "@/store/use-kinetograph-store";
import { isProviderSynced } from "@/lib/crdt";
import { KinetographAPI } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { OutputFile } from "@/types/kinetograph";
import { AnimatePresence, motion } from "framer-motion";
import {
	Download,
	FileVideo,
	FileText,
	FileType,
	Loader2,
	RefreshCw,
	X,
	AlertCircle,
	HardDrive,
	Save,
	FileJson,
	Film,
	Monitor,
	Smartphone,
	FolderOpen,
	Subtitles,
	FileCode,
	CheckCircle,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

interface ExportPanelProps {
	onClose: () => void;
}

const FILE_ICON: Record<string, React.ElementType> = {
	video: FileVideo,
	fcpxml: FileText,
	srt: FileType,
	vtt: FileType,
	captions: FileType,
};

function iconForFile(f: OutputFile) {
	for (const [key, Icon] of Object.entries(FILE_ICON)) {
		if (f.type?.includes(key) || f.file_name?.includes(key)) return Icon;
	}
	return HardDrive;
}

function formatBytes(bytes: number): string {
	if (bytes === 0) return "0 B";
	const k = 1024;
	const sizes = ["B", "KB", "MB", "GB"];
	const i = Math.floor(Math.log(bytes) / Math.log(k));
	return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatDuration(ms: number): string {
	const totalSec = Math.floor(ms / 1000);
	const min = Math.floor(totalSec / 60);
	const sec = totalSec % 60;
	return `${min}:${sec.toString().padStart(2, "0")}`;
}

function formatTC(ms: number): string {
	const totalFrames = Math.max(0, Math.floor((ms / 1000) * 30));
	const frames = totalFrames % 30;
	const totalSeconds = Math.floor(totalFrames / 30);
	const seconds = totalSeconds % 60;
	const minutes = Math.floor(totalSeconds / 60) % 60;
	const hours = Math.floor(totalSeconds / 3600);
	return `${hours.toString().padStart(2, "0")}:${minutes.toString().padStart(2, "0")}:${seconds.toString().padStart(2, "0")}:${frames.toString().padStart(2, "0")}`;
}

function formatSRTTime(ms: number): string {
	const hours = Math.floor(ms / 3600000);
	const minutes = Math.floor((ms % 3600000) / 60000);
	const seconds = Math.floor((ms % 60000) / 1000);
	const millis = Math.floor(ms % 1000);
	return `${hours.toString().padStart(2, "0")}:${minutes.toString().padStart(2, "0")}:${seconds.toString().padStart(2, "0")},${millis.toString().padStart(3, "0")}`;
}

function formatVTTTime(ms: number): string {
	const hours = Math.floor(ms / 3600000);
	const minutes = Math.floor((ms % 3600000) / 60000);
	const seconds = Math.floor((ms % 60000) / 1000);
	const millis = Math.floor(ms % 1000);
	return `${hours.toString().padStart(2, "0")}:${minutes.toString().padStart(2, "0")}:${seconds.toString().padStart(2, "0")}.${millis.toString().padStart(3, "0")}`;
}

function downloadText(content: string, filename: string, mimeType: string) {
	const blob = new Blob([content], { type: mimeType });
	const url = URL.createObjectURL(blob);
	const a = document.createElement("a");
	a.href = url;
	a.download = filename;
	a.click();
	URL.revokeObjectURL(url);
}

const RESOLUTION_PRESETS = [
	{ label: "Full HD", width: 1920, height: 1080, aspect: "16:9", orientation: "horizontal" as const },
	{ label: "HD", width: 1280, height: 720, aspect: "16:9", orientation: "horizontal" as const },
	{ label: "SD", width: 854, height: 480, aspect: "16:9", orientation: "horizontal" as const },
	{ label: "Full HD", width: 1080, height: 1920, aspect: "9:16", orientation: "vertical" as const },
	{ label: "HD", width: 720, height: 1280, aspect: "9:16", orientation: "vertical" as const },
	{ label: "SD", width: 480, height: 854, aspect: "9:16", orientation: "vertical" as const },
];

export function ExportPanel({ onClose }: ExportPanelProps) {
	const paperEdit = useKinetographStore((s) => s.paperEdit);
	const assets = useKinetographStore((s) => s.assets);

	const [activeTab, setActiveTab] = useState<"save" | "render">("save");
	const [files, setFiles] = useState<OutputFile[]>([]);
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const [saveStatus, setSaveStatus] = useState<string | null>(null);
	const [selectedResolution, setSelectedResolution] = useState(0);
	const [renderQuality, setRenderQuality] = useState<"high" | "medium" | "low">("high");
	const [isRendering, setIsRendering] = useState(false);
	const [renderProgress, setRenderProgress] = useState(0);
	const [renderStage, setRenderStage] = useState<string | null>(null);
	const renderPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

	const sequenceDurationMs =
		paperEdit?.clips.reduce((s, c) => s + (c.out_ms - c.in_ms), 0) ?? 0;

	const fetchOutputs = useCallback(async () => {
		setLoading(true);
		setError(null);
		try {
			const res = await KinetographAPI.getOutputs();
			setFiles(res.files ?? []);
		} catch (err) {
			setError(
				err instanceof Error ? err.message : "Failed to fetch outputs",
			);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		if (activeTab === "render") fetchOutputs();
	}, [activeTab, fetchOutputs]);

	const handleSaveProject = useCallback(async () => {
		if (!paperEdit) return;
		setSaveStatus("saving");
		try {
			// Yjs CRDT syncs continuously — just verify connection is live
			if (!isProviderSynced()) {
				// Provider not synced, fallback to JSON download
				handleDownloadProject();
				setSaveStatus(null);
				return;
			}
			setSaveStatus("saved");
			setTimeout(() => setSaveStatus(null), 2000);
		} catch {
			handleDownloadProject();
			setSaveStatus(null);
		}
	}, [paperEdit]);

	const handleDownloadProject = useCallback(() => {
		if (!paperEdit) return;
		const data = JSON.stringify(paperEdit, null, 2);
		const blob = new Blob([data], { type: "application/json" });
		const url = URL.createObjectURL(blob);
		const a = document.createElement("a");
		a.href = url;
		a.download = `${(paperEdit.title || "project").replace(/[^a-zA-Z0-9_-]/g, "_")}.json`;
		a.click();
		URL.revokeObjectURL(url);
		setSaveStatus("downloaded");
		setTimeout(() => setSaveStatus(null), 2000);
	}, [paperEdit]);

	const handleExportEDL = useCallback(() => {
		if (!paperEdit) return;
		let edl = `TITLE: ${paperEdit.title}\nFCM: NON-DROP FRAME\n\n`;
		let editNum = 1;
		let recIn = 0;
		for (const clip of paperEdit.clips) {
			const dur = clip.out_ms - clip.in_ms;
			const srcIn = formatTC(clip.in_ms);
			const srcOut = formatTC(clip.out_ms);
			const recInTC = formatTC(recIn);
			const recOutTC = formatTC(recIn + dur);
			const name = clip.source_file.padEnd(32).slice(0, 32);
			edl += `${String(editNum).padStart(3, "0")}  ${name} V     C        ${srcIn} ${srcOut} ${recInTC} ${recOutTC}\n`;
			edl += `* FROM CLIP NAME: ${clip.source_file}\n`;
			if (clip.description) edl += `* COMMENT: ${clip.description}\n`;
			edl += `\n`;
			editNum++;
			recIn += dur;
		}
		const blob = new Blob([edl], { type: "text/plain" });
		const url = URL.createObjectURL(blob);
		const a = document.createElement("a");
		a.href = url;
		a.download = `${(paperEdit.title || "project").replace(/[^a-zA-Z0-9_-]/g, "_")}.edl`;
		a.click();
		URL.revokeObjectURL(url);
	}, [paperEdit]);

	const handleExportFCPXML = useCallback(() => {
		if (!paperEdit) return;
		const FPS = 30;
		const title = paperEdit.title || "Untitled";
		const safeName = title.replace(/[^a-zA-Z0-9_-]/g, "_");
		const totalDurFrames = paperEdit.clips.reduce((s, c) => s + Math.round(((c.out_ms - c.in_ms) / 1000) * FPS), 0);

		let xml = `<?xml version="1.0" encoding="UTF-8"?>\n`;
		xml += `<!DOCTYPE fcpxml>\n`;
		xml += `<fcpxml version="1.10">\n`;
		xml += `  <resources>\n`;
		xml += `    <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/${FPS}s" width="1920" height="1080"/>\n`;

		// Collect unique source files
		const sourceFiles = [...new Set(paperEdit.clips.map((c) => c.source_file))];
		sourceFiles.forEach((file, i) => {
			xml += `    <asset id="a${i + 1}" name="${file}" src="file://./${encodeURIComponent(file)}" start="0s" duration="0s" hasVideo="1" hasAudio="1" format="r1"/>\n`;
		});
		xml += `  </resources>\n`;

		xml += `  <library>\n`;
		xml += `    <event name="${title}">\n`;
		xml += `      <project name="${title}">\n`;
		xml += `        <sequence format="r1" duration="${totalDurFrames}/${FPS}s" tcStart="0s" tcFormat="NDF">\n`;
		xml += `          <spine>\n`;

		for (const clip of paperEdit.clips) {
			const dur = clip.out_ms - clip.in_ms;
			const durFrames = Math.round((dur / 1000) * FPS);
			const inFrames = Math.round((clip.in_ms / 1000) * FPS);
			const outFrames = Math.round((clip.out_ms / 1000) * FPS);
			const assetIdx = sourceFiles.indexOf(clip.source_file);
			const clipName = clip.source_file.replace(/\.[^.]+$/, "");

			xml += `            <asset-clip ref="a${assetIdx + 1}" name="${clipName}" offset="0s" duration="${durFrames}/${FPS}s" start="${inFrames}/${FPS}s" tcFormat="NDF">\n`;
			if (clip.description) {
				xml += `              <note>${clip.description.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")}</note>\n`;
			}
			xml += `            </asset-clip>\n`;
		}

		xml += `          </spine>\n`;
		xml += `        </sequence>\n`;
		xml += `      </project>\n`;
		xml += `    </event>\n`;
		xml += `  </library>\n`;
		xml += `</fcpxml>\n`;

		const blob = new Blob([xml], { type: "application/xml" });
		const url = URL.createObjectURL(blob);
		const a = document.createElement("a");
		a.href = url;
		a.download = `${safeName}.fcpxml`;
		a.click();
		URL.revokeObjectURL(url);
	}, [paperEdit]);

	const handleExportSRT = useCallback(() => {
		if (!paperEdit) return;
		let srt = "";
		let recIn = 0;
		paperEdit.clips.forEach((clip, i) => {
			const dur = clip.out_ms - clip.in_ms;
			const start = recIn;
			const end = recIn + dur;
			srt += `${i + 1}\n`;
			srt += `${formatSRTTime(start)} --> ${formatSRTTime(end)}\n`;
			srt += `${clip.description || clip.source_file}\n\n`;
			recIn += dur;
		});
		downloadText(srt, `${(paperEdit.title || "project").replace(/[^a-zA-Z0-9_-]/g, "_")}.srt`, "text/srt");
	}, [paperEdit]);

	const handleExportVTT = useCallback(() => {
		if (!paperEdit) return;
		let vtt = "WEBVTT\n\n";
		let recIn = 0;
		paperEdit.clips.forEach((clip, i) => {
			const dur = clip.out_ms - clip.in_ms;
			const start = recIn;
			const end = recIn + dur;
			vtt += `${i + 1}\n`;
			vtt += `${formatVTTTime(start)} --> ${formatVTTTime(end)}\n`;
			vtt += `${clip.description || clip.source_file}\n\n`;
			recIn += dur;
		});
		downloadText(vtt, `${(paperEdit.title || "project").replace(/[^a-zA-Z0-9_-]/g, "_")}.vtt`, "text/vtt");
	}, [paperEdit]);

	const handleShowInFolder = useCallback((filePath: string) => {
		// Electron shell.showItemInFolder — the preload exposes the API as
		// `window.electron` (not `electronAPI`), which is why this was dead.
		window.electron?.showItemInFolder(filePath);
	}, []);

	const handleStartRender = useCallback(async () => {
		setIsRendering(true);
		setError(null);
		setRenderProgress(0);
		setRenderStage("Starting render…");
		const preset = RESOLUTION_PRESETS[selectedResolution];
		try {
			await KinetographAPI.startRender({
				width: preset.width,
				height: preset.height,
				quality: renderQuality,
			});

			// Poll pipeline status for progress
			renderPollRef.current = setInterval(async () => {
				try {
					const status = await KinetographAPI.getStatus();
					if (status.stage) setRenderStage(status.stage);
					if (typeof status.progress === "number") setRenderProgress(Math.round(status.progress * 100));
					if (status.phase === "error") {
						if (renderPollRef.current) clearInterval(renderPollRef.current);
						renderPollRef.current = null;
						setIsRendering(false);
						setError(status.errors.at(-1)?.message || "Render failed");
						return;
					}
					if (status.phase === "complete") {
						if (renderPollRef.current) clearInterval(renderPollRef.current);
						renderPollRef.current = null;
						setIsRendering(false);
						setRenderStage("Complete!");
						setRenderProgress(100);
						await fetchOutputs();
						setTimeout(() => { setRenderStage(null); setRenderProgress(0); }, 3000);
					}
				} catch {
					// Ignore poll errors
				}
			}, 1500);

			// Safety timeout — stop polling after 10 minutes
			setTimeout(() => {
				if (renderPollRef.current) {
					clearInterval(renderPollRef.current);
					renderPollRef.current = null;
				}
				setIsRendering(false);
			}, 600_000);
		} catch (err) {
			setError(err instanceof Error ? err.message : "Render failed");
			setIsRendering(false);
			setRenderStage(null);
		}
	}, [selectedResolution, renderQuality, fetchOutputs]);

	// Cleanup poll on unmount
	useEffect(() => {
		return () => { if (renderPollRef.current) clearInterval(renderPollRef.current); };
	}, []);

	return (
		<AnimatePresence>
			<motion.div
				initial={{ opacity: 0 }}
				animate={{ opacity: 1 }}
				exit={{ opacity: 0 }}
				className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
				onClick={(e) => {
					if (e.target === e.currentTarget) onClose();
				}}
			>
				<motion.div
					initial={{ opacity: 0, y: 24, scale: 0.96 }}
					animate={{ opacity: 1, y: 0, scale: 1 }}
					exit={{ opacity: 0, y: 24, scale: 0.96 }}
					transition={{ type: "spring", damping: 26, stiffness: 300 }}
					className="relative w-full max-w-lg rounded-lg border border-zinc-800 bg-zinc-900 shadow-2xl"
				>
					{/* Header */}
					<div className="flex items-center justify-between border-b border-zinc-800 px-5 py-3.5">
						<div className="flex items-center gap-2">
							<Film className="h-4 w-4 text-blue-500" />
							<h2 className="text-sm font-semibold text-zinc-100">
								Project
							</h2>
						</div>
						<button
							type="button"
							onClick={onClose}
							className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300 transition-colors"
						>
							<X className="h-3.5 w-3.5" />
						</button>
					</div>

					{/* Tabs */}
					<div className="flex border-b border-zinc-800">
						{(
							[
								["save", "Save & Export"],
								["render", "Render"],
							] as const
						).map(([key, label]) => (
							<button
								key={key}
								onClick={() => setActiveTab(key)}
								className={cn(
									"flex-1 py-2.5 text-xs font-medium transition-colors border-b-2",
									activeTab === key
										? "border-blue-500 text-blue-400"
										: "border-transparent text-zinc-500 hover:text-zinc-300",
								)}
							>
								{label}
							</button>
						))}
					</div>

					{/* Body */}
					<div className="px-5 py-4">
						{activeTab === "save" && (
							<div className="flex flex-col gap-4">
								{/* Project summary */}
								{paperEdit && (
									<div className="rounded border border-zinc-800 bg-zinc-800/30 p-3">
										<h3 className="text-xs font-semibold text-zinc-200 mb-2">
											{paperEdit.title || "Untitled Sequence"}
										</h3>
										<div className="grid grid-cols-3 gap-3 text-[10px]">
											<div className="flex flex-col gap-0.5">
												<span className="text-zinc-500">Clips</span>
												<span className="text-zinc-200 font-mono">
													{paperEdit.clips.length}
												</span>
											</div>
											<div className="flex flex-col gap-0.5">
												<span className="text-zinc-500">Duration</span>
												<span className="text-zinc-200 font-mono">
													{formatDuration(sequenceDurationMs)}
												</span>
											</div>
											<div className="flex flex-col gap-0.5">
												<span className="text-zinc-500">Media</span>
												<span className="text-zinc-200 font-mono">
													{assets.length} files
												</span>
											</div>
										</div>
									</div>
								)}

								{!paperEdit && (
									<div className="flex flex-col items-center gap-2 py-6 text-zinc-500">
										<Film className="h-6 w-6" />
										<span className="text-xs">
											No sequence to export. Add clips to the timeline first.
										</span>
									</div>
								)}

								{paperEdit && (
									<div className="flex flex-col gap-2">
										<button
											onClick={handleSaveProject}
											className="flex items-center justify-center gap-2 rounded bg-blue-600 px-4 py-2.5 text-xs font-semibold text-white hover:bg-blue-500 transition-colors disabled:opacity-50"
											disabled={saveStatus === "saving"}
										>
											{saveStatus === "saving" ? (
												<Loader2 className="h-3.5 w-3.5 animate-spin" />
											) : (
												<Save className="h-3.5 w-3.5" />
											)}
											{saveStatus === "saved"
												? "Saved!"
												: saveStatus === "saving"
													? "Saving\u2026"
													: "Save Project"}
										</button>
										<div className="flex gap-2">
											<button
												onClick={handleDownloadProject}
												className="flex-1 flex items-center justify-center gap-2 rounded border border-zinc-700 bg-zinc-800 px-3 py-2 text-xs font-medium text-zinc-300 hover:bg-zinc-700 transition-colors"
											>
												<FileJson className="h-3.5 w-3.5" />
												JSON
											</button>
											<button
												onClick={handleExportEDL}
												className="flex-1 flex items-center justify-center gap-2 rounded border border-zinc-700 bg-zinc-800 px-3 py-2 text-xs font-medium text-zinc-300 hover:bg-zinc-700 transition-colors"
											>
												<FileText className="h-3.5 w-3.5" />
												EDL
											</button>
											<button
												onClick={handleExportFCPXML}
												className="flex-1 flex items-center justify-center gap-2 rounded border border-zinc-700 bg-zinc-800 px-3 py-2 text-xs font-medium text-zinc-300 hover:bg-zinc-700 transition-colors"
											>
												<FileCode className="h-3.5 w-3.5" />
												FCPXML
											</button>
										</div>

										{/* Caption exports */}
										<div className="border-t border-zinc-800 pt-2 mt-1">
											<span className="text-[10px] font-medium text-zinc-500 mb-1.5 block">Caption / Subtitle Export</span>
											<div className="flex gap-2">
												<button
													onClick={handleExportSRT}
													className="flex-1 flex items-center justify-center gap-2 rounded border border-zinc-700 bg-zinc-800 px-3 py-2 text-xs font-medium text-zinc-300 hover:bg-zinc-700 transition-colors"
												>
													<Subtitles className="h-3.5 w-3.5" />
													SRT
												</button>
												<button
													onClick={handleExportVTT}
													className="flex-1 flex items-center justify-center gap-2 rounded border border-zinc-700 bg-zinc-800 px-3 py-2 text-xs font-medium text-zinc-300 hover:bg-zinc-700 transition-colors"
												>
													<Subtitles className="h-3.5 w-3.5" />
													WebVTT
												</button>
											</div>
										</div>
									</div>
								)}
							</div>
						)}

						{activeTab === "render" && (
							<div className="flex flex-col gap-4">
								{/* Resolution Presets */}
								<div className="flex flex-col gap-2">
									<label className="text-[10px] font-medium text-zinc-400">Resolution</label>
									<div className="flex flex-col gap-1.5">
										<span className="text-[9px] text-zinc-500 font-medium flex items-center gap-1.5"><Monitor className="h-3 w-3" /> Horizontal (16:9)</span>
										<div className="grid grid-cols-3 gap-1.5">
											{RESOLUTION_PRESETS.filter((p) => p.orientation === "horizontal").map((preset) => {
												const idx = RESOLUTION_PRESETS.indexOf(preset);
												return (
													<button key={idx} onClick={() => setSelectedResolution(idx)} className={cn("flex flex-col items-center gap-1 rounded border p-2 transition-all", selectedResolution === idx ? "border-blue-500 bg-blue-500/10 text-blue-400" : "border-zinc-800 bg-zinc-800/30 text-zinc-400 hover:border-zinc-700")}>
														<div className="w-10 h-6 rounded bg-zinc-700/50 border border-zinc-600/30 flex items-center justify-center"><Monitor className="h-3 w-3 opacity-50" /></div>
														<span className="text-[9px] font-semibold">{preset.width}\u00d7{preset.height}</span>
														<span className="text-[7px] text-zinc-500">{preset.label}</span>
													</button>
												);
											})}
										</div>
									</div>
									<div className="flex flex-col gap-1.5">
										<span className="text-[9px] text-zinc-500 font-medium flex items-center gap-1.5"><Smartphone className="h-3 w-3" /> Vertical (9:16)</span>
										<div className="grid grid-cols-3 gap-1.5">
											{RESOLUTION_PRESETS.filter((p) => p.orientation === "vertical").map((preset) => {
												const idx = RESOLUTION_PRESETS.indexOf(preset);
												return (
													<button key={idx} onClick={() => setSelectedResolution(idx)} className={cn("flex flex-col items-center gap-1 rounded border p-2 transition-all", selectedResolution === idx ? "border-blue-500 bg-blue-500/10 text-blue-400" : "border-zinc-800 bg-zinc-800/30 text-zinc-400 hover:border-zinc-700")}>
														<div className="w-5 h-8 rounded bg-zinc-700/50 border border-zinc-600/30 flex items-center justify-center"><Smartphone className="h-3 w-3 opacity-50" /></div>
														<span className="text-[9px] font-semibold">{preset.width}\u00d7{preset.height}</span>
														<span className="text-[7px] text-zinc-500">{preset.label}</span>
													</button>
												);
											})}
										</div>
									</div>
								</div>

								{/* Quality */}
								<div className="flex flex-col gap-1.5">
									<label className="text-[10px] font-medium text-zinc-400">Quality</label>
									<div className="flex gap-px bg-zinc-800 p-0.5 rounded">
										{(["high", "medium", "low"] as const).map((q) => (
											<button key={q} onClick={() => setRenderQuality(q)} className={cn("flex-1 py-1.5 text-[9px] font-medium rounded capitalize transition-all", renderQuality === q ? "bg-zinc-600 text-white" : "hover:bg-zinc-700 text-zinc-500")}>{q}</button>
										))}
									</div>
								</div>

								{/* Summary */}
								<div className="flex items-center justify-between text-[9px] text-zinc-500 bg-zinc-800/30 rounded px-3 py-2 font-mono">
									<span>MP4 (H.264)</span>
									<span>30 FPS</span>
									<span>{RESOLUTION_PRESETS[selectedResolution].width}\u00d7{RESOLUTION_PRESETS[selectedResolution].height}</span>
									<span className="capitalize">{renderQuality}</span>
								</div>

								{/* Render */}
								<button onClick={handleStartRender} disabled={isRendering || !paperEdit || paperEdit.clips.length === 0} className="flex items-center justify-center gap-2 rounded bg-blue-600 px-4 py-2.5 text-xs font-semibold text-white hover:bg-blue-500 transition-colors disabled:opacity-50">
									{isRendering ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileVideo className="h-3.5 w-3.5" />}
									{isRendering ? "Rendering\u2026" : "Start Render"}
								</button>

								{/* Render progress */}
								{(isRendering || renderStage) && (
									<div className="rounded border border-zinc-800 bg-zinc-800/30 p-3 flex flex-col gap-2">
										<div className="flex items-center justify-between">
											<span className="text-[10px] font-medium text-zinc-300 flex items-center gap-1.5">
												{renderStage === "Complete!" ? <CheckCircle className="h-3 w-3 text-green-400" /> : <Loader2 className="h-3 w-3 animate-spin text-blue-400" />}
												{renderStage || "Processing…"}
											</span>
											<span className="text-[9px] font-mono text-zinc-500">{renderProgress}%</span>
										</div>
										<div className="w-full h-1.5 bg-zinc-800 rounded-full overflow-hidden">
											<div
												className={cn(
													"h-full rounded-full transition-all duration-500",
													renderStage === "Complete!" ? "bg-green-500" : "bg-blue-500",
												)}
												style={{ width: `${renderProgress}%` }}
											/>
										</div>
									</div>
								)}

								{/* Rendered Files */}
								<div className="border-t border-zinc-800" />
								<div className="flex items-center justify-between">
									<span className="text-[10px] text-zinc-500 font-medium">
										Rendered Files
									</span>
									<button
										onClick={fetchOutputs}
										disabled={loading}
										className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300 transition-colors disabled:opacity-40"
									>
										<RefreshCw
											className={cn(
												"h-3 w-3",
												loading && "animate-spin",
											)}
										/>
									</button>
								</div>

								{loading && (
									<div className="flex flex-col items-center gap-2 py-6 text-zinc-500">
										<Loader2 className="h-5 w-5 animate-spin" />
										<span className="text-xs">Loading\u2026</span>
									</div>
								)}

								{error && !loading && (
									<div className="flex flex-col items-center gap-2 py-6 text-zinc-500">
										<AlertCircle className="h-5 w-5 text-zinc-600" />
										<span className="text-xs text-zinc-500">{error}</span>
									</div>
								)}

								{!loading && !error && files.length === 0 && (
									<div className="flex flex-col items-center gap-2 py-6 text-zinc-500">
										<HardDrive className="h-5 w-5" />
										<span className="text-xs">
											No rendered outputs yet.
										</span>
									</div>
								)}

								{!loading && !error && files.length > 0 && (
									<ul className="space-y-2 max-h-60 overflow-y-auto">
										{files.map((file) => {
											const Icon = iconForFile(file);
											return (
												<li
													key={file.file_name}
													className="group flex items-center justify-between rounded-md border border-zinc-800 bg-zinc-800/40 px-3 py-2.5 transition-colors hover:border-zinc-700"
												>
													<div className="flex items-center gap-3 min-w-0">
														<Icon className="h-4 w-4 shrink-0 text-zinc-500 group-hover:text-zinc-300 transition-colors" />
														<div className="min-w-0">
															<p className="truncate text-xs font-medium text-zinc-200">
																{file.file_name}
															</p>
															<p className="text-[10px] text-zinc-500">
																{file.type} \u00b7{" "}
																{formatBytes(file.size_bytes)}
															</p>
														</div>
													</div>
													<a
														href={file.download_url}
														download={file.file_name}
														className="ml-3 shrink-0 rounded bg-blue-600/10 px-2.5 py-1 text-[10px] font-semibold text-blue-400 hover:bg-blue-600/20 transition-colors"
													>
														Download
													</a>
													{file.download_url && (
														<button
															onClick={() => handleShowInFolder(file.download_url)}
															className="ml-1 shrink-0 rounded bg-zinc-700/30 px-2 py-1 text-[10px] font-medium text-zinc-400 hover:bg-zinc-700/50 hover:text-zinc-200 transition-colors"
															title="Show in Finder"
														>
															<FolderOpen className="h-3 w-3" />
														</button>
													)}
												</li>
											);
										})}
									</ul>
								)}
							</div>
						)}
					</div>

					{/* Footer */}
					<div className="border-t border-zinc-800 px-5 py-3 flex items-center justify-between">
						<span className="text-[10px] text-zinc-600">
							\u2318S to save \u00b7 Esc to close
						</span>
						<button
							onClick={onClose}
							className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-[10px] font-medium text-zinc-400 hover:bg-zinc-700 transition-colors"
						>
							Close
						</button>
					</div>
				</motion.div>
			</motion.div>
		</AnimatePresence>
	);
}
