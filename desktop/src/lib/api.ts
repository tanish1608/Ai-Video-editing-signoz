export interface EditingOptions {
  editing_mode: "narration" | "highlights";
  audio_provider: "elevenlabs" | "soundstripe" | "none";
  sound_effects_enabled: boolean;
  caption_style_id: string;
}

import ky, { HTTPError } from "ky";
import { getBackendUrlSync } from "./backend";
import {
  AssetsResponse,
  PipelineStatus,
  RunRequest,
  RunResponse,
  PaperEdit,
  ApprovalRequest,
  OutputResponse,
  MasterIndexResponse,
  RawAsset,
  EditRequest,
  EditResponse,
  CaptionStylePreset,
  RenderRequest,
  RenderResponse,
  ColorGrade,
  PipelineError,
} from "@/types/kinetograph";

function createApi() {
  return ky.create({
    prefixUrl: `${getBackendUrlSync()}/api`,
    timeout: 120_000,
    retry: { limit: 2, methods: ["get"] },
  });
}

// Lazy-initialized api instance — recreated if backend URL changes
let _api: ReturnType<typeof ky.create> | null = null;
function api() {
  if (!_api) _api = createApi();
  return _api;
}

/** Best human-readable message for a failed request — prefers FastAPI's `detail`. */
export async function apiErrorMessage(err: unknown, fallback: string): Promise<string> {
  if (err instanceof HTTPError) {
    try {
      const body = (await err.response.clone().json()) as { detail?: unknown };
      if (typeof body.detail === "string") return body.detail;
    } catch {
      // not JSON — fall through
    }
  }
  return err instanceof Error ? err.message : fallback;
}

export interface RunNode {
  node: string;
  phase: string;
  duration_s: number;
  finished_at: string;
}

export interface RunSummary {
  run_id: string;
  status: "running" | "awaiting_approval" | "complete" | "error" | "cancelled" | string;
  started_at?: string;
  ended_at?: string;
  duration_s?: number;
  prompt?: string;
  nodes: RunNode[];
  errors: PipelineError[];
  error?: string;
  api_keys?: Record<string, boolean>;
  log_dir: string;
}

/** Call this after backend URL is resolved to reinitialize the API client */
export function reinitializeApi(): void {
  _api = null;
}

export const KinetographAPI = {
  getHealth: () =>
    api().get("health").json<{ status: string; version: string }>(),
  getConfig: () => api().get("config").json<Record<string, string | number>>(),

  getAssets: () =>
    api()
      .get("assets")
      .json<AssetsResponse>(),

  uploadAsset: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return api()
      .post("assets/upload", {
        body: formData,
      })
      .json<RawAsset>();
  },

  /**
   * Register media files by their absolute paths — reference-based import.
   * Files are NOT copied; the backend stores a reference + creates a symlink.
   */
  registerAssets: (filePaths: string[]) =>
    api()
      .post("assets/register", {
        json: { file_paths: filePaths },
      })
      .json<{
        status: string;
        registered: number;
        results: Array<RawAsset & { status?: string; error?: string }>;
      }>(),

  /** Delete a media asset — removes the reference/file from the backend. */
  deleteAsset: (assetId: string) =>
    api()
      .delete(`assets/${assetId}`)
      .json<{ status: string; asset_id: string; original_path?: string }>(),

  /** Purge the media cache (thumbnails, waveforms, metadata, etc.) */
  purgeCache: (targets = "all") =>
    api()
      .delete(`cache?targets=${encodeURIComponent(targets)}`)
      .json<{ status: string; details: Record<string, number> }>(),

  /** Get cache disk usage statistics */
  getCacheStats: () =>
    api()
      .get("cache/stats")
      .json<Record<string, { files: number; bytes: number } | number | string>>(),

  getStatus: () => api().get("pipeline/status").json<PipelineStatus>(),

  runPipeline: (request: RunRequest) =>
    api().post("pipeline/run", { json: request }).json<RunResponse>(),

  approvePipeline: (request: ApprovalRequest) =>
    api().post("pipeline/approve", { json: request }).json<RunResponse>(),

  stopPipeline: () =>
    api()
      .post("pipeline/stop", { timeout: 30_000 })
      .json<{ status: "stopped" | "stopping" | "not_running" }>(),

  getRuns: (limit = 50) =>
    api().get(`runs?limit=${limit}`).json<{ runs: RunSummary[] }>(),

  // NOTE: getPaperEdit and savePaperEdit removed — paper edit is now synced
  // in real-time via Yjs CRDT over /ws/crdt WebSocket connection.

  getMasterIndex: () => api().get("master-index").json<MasterIndexResponse>(),

  getOutputs: () => api().get("output").json<OutputResponse>(),

  editPipeline: (request: EditRequest) =>
    api().post("pipeline/edit", { json: request }).json<EditResponse>(),

  getEditingOptions: () => api().get("project/editing-options").json<EditingOptions>(),
  saveEditingOptions: (options: EditingOptions) =>
    api().put("project/editing-options", { json: options }).json<EditingOptions>(),

  getCaptionStyles: () =>
    api().get("pipeline/caption-styles").json<{ styles: CaptionStylePreset[]; selected_style_id: string }>(),

  selectCaptionStyle: (styleId: string, apply = false) =>
    api()
      .post("pipeline/caption-style", { json: { style_id: styleId, apply } })
      .json<{ status: string; style_id: string; style_name: string }>(),



  startRender: (request: RenderRequest) =>
    api().post("render", { json: request }).json<RenderResponse>(),

  updateConfig: (config: {
    output_width?: number;
    output_height?: number;
    output_orientation?: string;
    output_fps?: number;
  }) =>
    api()
      .post("config", { json: config })
      .json<{ status: string; output_width: number; output_height: number }>(),

  updateAssetType: (assetId: string, assetType: string) =>
    api()
      .patch(`assets/${assetId}/type`, { json: { asset_type: assetType } })
      .json<{ status: string; asset_id: string; asset_type: string }>(),

  getColorGrade: () => api().get("config/color-grade").json<ColorGrade>(),

  setColorGrade: (grade: ColorGrade) =>
    api()
      .post("config/color-grade", { json: grade })
      .json<{ status: string; color_grade: ColorGrade }>(),
};
