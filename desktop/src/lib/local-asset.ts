import { RawAsset } from "@/types/kinetograph";

const DEFAULT_DURATION_MS = 1000;
const DEFAULT_WIDTH = 1920;
const DEFAULT_HEIGHT = 1080;
const DEFAULT_FPS = 30;

interface VideoMetadata {
	durationMs: number;
	width: number;
	height: number;
	hasAudio: boolean;
	thumbnailUrl: string;
}

function createLocalId() {
	if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
		return `local-${crypto.randomUUID()}`;
	}
	return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function inferAssetType(_fileName: string): RawAsset["asset_type"] {
	return "media";
}

function inferCodec(file: File) {
	const [mime] = file.type.split(";");
	const codec = mime?.replace("video/", "").trim();
	return codec || "unknown";
}

function generateThumbnail(video: HTMLVideoElement, seekTime: number): Promise<string> {
	return new Promise((resolve) => {
		const canvas = document.createElement("canvas");
		const w = video.videoWidth || 320;
		const h = video.videoHeight || 180;
		// Use a smaller canvas for thumbnails
		const scale = Math.min(1, 320 / w);
		canvas.width = Math.round(w * scale);
		canvas.height = Math.round(h * scale);

		const onSeeked = () => {
			video.removeEventListener("seeked", onSeeked);
			const ctx = canvas.getContext("2d");
			if (ctx) {
				ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
				resolve(canvas.toDataURL("image/jpeg", 0.7));
			} else {
				resolve("");
			}
		};

		video.addEventListener("seeked", onSeeked);
		video.currentTime = seekTime;
	});
}

function loadMetadata(streamUrl: string): Promise<VideoMetadata> {
	return new Promise((resolve) => {
		const video = document.createElement("video");
		video.preload = "auto";
		video.muted = true;
		video.src = streamUrl;

		const finish = (metadata: VideoMetadata) => {
			video.removeAttribute("src");
			video.load();
			resolve(metadata);
		};

		video.onloadeddata = async () => {
			const durationSeconds = Number.isFinite(video.duration) ? video.duration : 1;
			const hasAudio = (video as any).mozHasAudio === true ||
				(typeof (video as any).webkitAudioDecodedByteCount === "number" &&
					(video as any).webkitAudioDecodedByteCount > 0);

			// Generate thumbnail from 0.5s or 10% into the video
			const seekTime = Math.min(0.5, durationSeconds * 0.1);
			let thumbnailUrl = "";
			try {
				thumbnailUrl = await generateThumbnail(video, seekTime);
			} catch {
				// thumbnail generation failed, use empty
			}

			finish({
				durationMs: Math.max(DEFAULT_DURATION_MS, Math.round(durationSeconds * 1000)),
				width: video.videoWidth || DEFAULT_WIDTH,
				height: video.videoHeight || DEFAULT_HEIGHT,
				hasAudio,
				thumbnailUrl,
			});
		};

		video.onerror = () => {
			finish({
				durationMs: DEFAULT_DURATION_MS,
				width: DEFAULT_WIDTH,
				height: DEFAULT_HEIGHT,
				hasAudio: true,
				thumbnailUrl: "",
			});
		};
	});
}

// Track active blob URLs for lifecycle management — prevents memory leaks
const _activeBlobUrls = new Map<string, string>();

export function isVideoFile(file: File) {
	return file.type.startsWith("video/");
}

/** Revoke a blob URL tracked by createLocalAsset to free memory. */
export function revokeLocalAssetUrl(assetId: string): void {
	const url = _activeBlobUrls.get(assetId);
	if (url) {
		URL.revokeObjectURL(url);
		_activeBlobUrls.delete(assetId);
	}
}

/** Revoke ALL tracked blob URLs (e.g. on project close / asset refresh). */
export function revokeAllLocalAssetUrls(): void {
	for (const url of _activeBlobUrls.values()) {
		URL.revokeObjectURL(url);
	}
	_activeBlobUrls.clear();
}

export async function createLocalAsset(file: File): Promise<RawAsset> {
	const streamUrl = URL.createObjectURL(file);
	const metadata = await loadMetadata(streamUrl);
	const id = createLocalId();

	// Track for lifecycle management
	_activeBlobUrls.set(id, streamUrl);

	return {
		id,
		file_name: file.name,
		file_path: (file as File & { path?: string }).path || file.name,
		asset_type: inferAssetType(file.name),
		duration_ms: metadata.durationMs,
		width: metadata.width,
		height: metadata.height,
		fps: DEFAULT_FPS,
		has_audio: metadata.hasAudio,
		codec: inferCodec(file),
		thumbnail_url: metadata.thumbnailUrl,
		waveform_url: null,
		stream_url: streamUrl,
	};
}
