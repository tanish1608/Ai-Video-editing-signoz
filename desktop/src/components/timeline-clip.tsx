import { useRef, useCallback, useState, useEffect } from "react";
import { PaperEditClip } from "@/types/kinetograph";
import { useKinetographStore } from "@/store/use-kinetograph-store";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { GripVertical, Scissors, Volume2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { resolveBackendUrl } from "@/lib/backend";

interface TimelineClipProps {
	clip: PaperEditClip;
	isSelected: boolean;
	pxPerSecond: number;
	onClick: (e: React.MouseEvent) => void;
	onDelete: () => void;
	onTrim?: (edge: "in" | "out", deltaMs: number) => void;
}

function generateClipThumbnail(streamUrl: string, seekTime: number): Promise<string> {
	return new Promise((resolve) => {
		const video = document.createElement("video");
		video.preload = "auto";
		video.muted = true;
		video.crossOrigin = "anonymous";
		video.src = streamUrl;

		const canvas = document.createElement("canvas");

		video.onloadeddata = () => {
			video.currentTime = seekTime;
		};

		video.onseeked = () => {
			canvas.width = video.videoWidth || 320;
			canvas.height = video.videoHeight || 180;
			const ctx = canvas.getContext("2d");
			if (ctx) {
				ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
				resolve(canvas.toDataURL("image/jpeg", 0.6));
			} else {
				resolve("");
			}
			video.removeAttribute("src");
			video.load();
		};

		video.onerror = () => resolve("");
	});
}

export function TimelineClip({
	clip,
	isSelected,
	pxPerSecond,
	onClick,
	onDelete,
	onTrim,
}: TimelineClipProps) {
	const assets = useKinetographStore((s) => s.assets);
	const [thumbnailUrl, setThumbnailUrl] = useState<string>("");

	const {
		attributes,
		listeners,
		setNodeRef,
		transform,
		transition,
		isDragging,
	} = useSortable({ id: clip.clip_id });

	const trimStartXRef = useRef<number>(0);
	const trimEdgeRef = useRef<"in" | "out">("in");

	// Generate thumbnail for this clip
	useEffect(() => {
		const asset = assets.find(
			(a) => a.file_name === clip.source_file || a.file_path === clip.source_file,
		);
		if (!asset) return;

		// If asset has a thumbnail, use it directly
		if (asset.thumbnail_url) {
			setThumbnailUrl(resolveBackendUrl(asset.thumbnail_url) ?? asset.thumbnail_url);
			return;
		}

		// Otherwise generate from the clip's in-point. clip.in_ms changes on every
		// trim pointermove, so debounce the (expensive full-video) decode until the
		// drag settles and drop stale/unmounted results.
		let cancelled = false;
		const seekSec = clip.in_ms / 1000;
		const streamUrl = resolveBackendUrl(asset.stream_url) ?? asset.stream_url;
		const timer = setTimeout(() => {
			generateClipThumbnail(streamUrl, seekSec).then((url) => {
				if (!cancelled && url) setThumbnailUrl(url);
			});
		}, 200);
		return () => {
			cancelled = true;
			clearTimeout(timer);
		};
	}, [clip.source_file, clip.in_ms, assets]);

	const handleTrimPointerDown = useCallback(
		(e: React.PointerEvent, edge: "in" | "out") => {
			e.preventDefault();
			e.stopPropagation();
			trimStartXRef.current = e.clientX;
			trimEdgeRef.current = edge;
			(e.target as HTMLElement).setPointerCapture(e.pointerId);
		},
		[],
	);

	const handleTrimPointerMove = useCallback(
		(e: React.PointerEvent) => {
			if (!onTrim) return;
			if (!(e.target as HTMLElement).hasPointerCapture(e.pointerId)) return;
			const deltaX = e.clientX - trimStartXRef.current;
			if (Math.abs(deltaX) < 2) return;
			const deltaMs = (deltaX / pxPerSecond) * 1000;
			trimStartXRef.current = e.clientX;
			onTrim(trimEdgeRef.current, deltaMs);
		},
		[onTrim, pxPerSecond],
	);

	const handleTrimPointerUp = useCallback((e: React.PointerEvent) => {
		(e.target as HTMLElement).releasePointerCapture(e.pointerId);
	}, []);

	const style = {
		transform: CSS.Transform.toString(transform),
		transition,
	};

	const duration = Math.max(0, clip.out_ms - clip.in_ms);
	const width = Math.max(40, (duration / 1000) * pxPerSecond);

	const clipColor =
		clip.clip_type === "primary"
			? "border-l-blue-500"
			: clip.clip_type === "cutaway"
				? "border-l-emerald-500"
				: "border-l-purple-500";

	return (
		<div
			ref={setNodeRef}
			style={{ ...style, width: `${width}px` }}
			onClick={onClick}
			data-clip-id={clip.clip_id}
			{...attributes}
			{...listeners}
			className={cn(
				"relative flex h-16 shrink-0 flex-col border-r border-black/40 transition-all select-none group overflow-hidden border-l-2 cursor-grab active:cursor-grabbing",
				clipColor,
				isSelected
					? "bg-blue-500/15 ring-1 ring-blue-500/50 z-10"
					: "bg-zinc-800/80 hover:bg-zinc-750/80",
				isDragging && "opacity-40 z-50",
			)}
		>
			{/* Thumbnail background */}
			{thumbnailUrl && (
				<div
					className="absolute inset-0 bg-cover bg-center opacity-30 group-hover:opacity-40 transition-opacity"
					style={{ backgroundImage: `url(${thumbnailUrl})` }}
				/>
			)}

			{/* Header */}
			<div
				className="relative flex h-4 items-center justify-between px-1.5 bg-black/40 border-b border-black/30"
			>
				<div className="flex items-center gap-1">
					<GripVertical className="h-2 w-2 text-zinc-500" />
					<span className="text-[8px] font-medium text-zinc-400 truncate max-w-[80px]">
						{clip.source_file.replace(/\.[^.]+$/, "")}
					</span>
				</div>
				{clip.transition === "crossfade" && (
					<Scissors className="h-2 w-2 text-zinc-500" />
				)}
			</div>

			{/* Body */}
			<div className="relative flex flex-1 flex-col justify-end p-1.5 overflow-hidden">
				<div className="flex items-center justify-between text-[8px] font-mono text-zinc-500">
					<span className={isSelected ? "text-blue-400/80" : ""}>
						{(clip.in_ms / 1000).toFixed(1)}s
					</span>
					<span className="text-zinc-600">
						{(duration / 1000).toFixed(1)}s
					</span>
					<span>{(clip.out_ms / 1000).toFixed(1)}s</span>
				</div>
			</div>

			{/* Trim handles */}
			<div
				data-trim-handle
				onPointerDown={(e) => handleTrimPointerDown(e, "in")}
				onPointerMove={handleTrimPointerMove}
				onPointerUp={handleTrimPointerUp}
				className="absolute left-0 inset-y-4 w-1.5 bg-transparent hover:bg-blue-500/50 cursor-ew-resize transition-colors touch-none z-20"
				title="Trim In"
			/>
			<div
				data-trim-handle
				onPointerDown={(e) => handleTrimPointerDown(e, "out")}
				onPointerMove={handleTrimPointerMove}
				onPointerUp={handleTrimPointerUp}
				className="absolute right-0 inset-y-4 w-1.5 bg-transparent hover:bg-blue-500/50 cursor-ew-resize transition-colors touch-none z-20"
				title="Trim Out"
			/>

			{/* Selection highlight */}
			{isSelected && (
				<div className="absolute inset-0 border border-blue-500/40 pointer-events-none" />
			)}
		</div>
	);
}
