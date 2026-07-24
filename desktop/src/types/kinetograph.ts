// 🎬 Kinetograph TypeScript Interfaces
// Based on API.md and the "Hollywood Swarm" 8-agent architecture

export enum Phase {
  IDLE = 'idle',
  INGESTING = 'ingesting',
  INDEXED = 'indexed',
  SCRIPTING = 'scripting',
  SCRIPTED = 'scripted',
  AWAITING_APPROVAL = 'awaiting_approval',
  APPROVED = 'approved',
  SYNTHESIZING = 'synthesizing',
  SYNTHESIZED = 'synthesized',
  NORMALIZING = 'normalizing',
  NORMALIZED = 'normalized',
  RENDERING = 'rendering',
  RENDERED = 'rendered',
  CAPTIONING = 'captioning',
  MASTERING = 'mastering',
  MASTERED = 'mastered',
  EXPORTING = 'exporting',
  COMPLETE = 'complete',
  ERROR = 'error',
}

// Map of phases to the agent currently "waking up"
export const AgentMap: Record<Phase, string | null> = {
  [Phase.IDLE]: null,
  [Phase.INGESTING]: 'Archivist',
  [Phase.INDEXED]: 'Archivist',
  [Phase.SCRIPTING]: 'Scripter',
  [Phase.SCRIPTED]: 'Scripter',
  [Phase.AWAITING_APPROVAL]: 'Review (Human)',
  [Phase.APPROVED]: 'Review (Human)',
  [Phase.SYNTHESIZING]: 'Synthesizer',
  [Phase.SYNTHESIZED]: 'Synthesizer',
  [Phase.NORMALIZING]: 'Director',
  [Phase.NORMALIZED]: 'Director',
  [Phase.RENDERING]: 'Director',
  [Phase.RENDERED]: 'Motion Grapher',
  [Phase.CAPTIONING]: 'Captioner',
  [Phase.MASTERING]: 'Sound Engineer',
  [Phase.MASTERED]: 'Sound Engineer',
  [Phase.EXPORTING]: 'QA Lead',
  [Phase.COMPLETE]: 'QA Lead',
  [Phase.ERROR]: 'QA Lead',
};

export type ClipType = 'primary' | 'cutaway' | 'synth';
export type TransitionType = 'cut' | 'crossfade' | 'dissolve' | 'fade-to-black' | 'fade-to-white' | 'wipe-left' | 'wipe-right' | 'slide-left' | 'slide-right';

// ─── Preview Resolution ────────────────────────────────────────────────────────

export type PreviewResolution = '16:9' | '9:16' | '4:3' | '1:1';

export const PREVIEW_RESOLUTIONS: Record<PreviewResolution, { label: string; width: number; height: number }> = {
  '16:9': { label: '16:9 Landscape', width: 1920, height: 1080 },
  '9:16': { label: '9:16 Vertical', width: 1080, height: 1920 },
  '4:3':  { label: '4:3 Standard',  width: 1440, height: 1080 },
  '1:1':  { label: '1:1 Square',    width: 1080, height: 1080 },
};

// ─── Color Grading ─────────────────────────────────────────────────────────────

export interface ColorGrade {
  brightness: number;   // -1..1,  0 = neutral
  contrast: number;     //  0..3,  1 = neutral
  saturation: number;   //  0..3,  1 = neutral
  gamma: number;        // 0.1..5, 1 = neutral
  temperature: number;  // -1..1,  0 = neutral
  tint: number;         // -1..1,  0 = neutral
  shadows: number;      // -1..1,  0 = neutral
  highlights: number;   // -1..1,  0 = neutral
}

export const DEFAULT_COLOR_GRADE: ColorGrade = {
  brightness: 0, contrast: 1, saturation: 1, gamma: 1,
  temperature: 0, tint: 0, shadows: 0, highlights: 0,
};

// ─── Render Request ────────────────────────────────────────────────────────────

export interface RenderRequest {
  width: number;
  height: number;
  quality: 'high' | 'medium' | 'low';
}

export interface RenderResponse {
  status: string;
  message: string;
}

// ─── Overlay (V2) clip types ───────────────────────────────────────────────────

export type OverlayPreset = 'pip-br' | 'pip-bl' | 'pip-tr' | 'pip-tl' | 'pip-center' | 'side-by-side' | 'custom';

export interface OverlayTransform {
  x: number;          // 0..100 — percentage of frame width for left edge
  y: number;          // 0..100 — percentage of frame height for top edge
  width: number;      // 0..100 — percentage of frame width
  height: number;     // 0..100 — percentage of frame height
  opacity: number;    // 0..1
  borderRadius: number; // px
}

export interface OverlayClip {
  id: string;
  sourceAssetId: string;
  sourceFile: string;
  inMs: number;
  outMs: number;
  timelineStartMs: number; // where on the timeline this overlay begins
  trackId: string;         // which video overlay track (V2, V3, …) it lives on
  transform: OverlayTransform;
  preset: OverlayPreset;
}

export interface AudioClip {
  id: string;
  sourceAssetId: string;
  sourceFile: string;
  inMs: number;
  outMs: number;
  timelineStartMs: number;
  trackId: string;         // which audio track (A2, A3, …) it lives on
}

export const OVERLAY_PRESETS: Record<OverlayPreset, OverlayTransform> = {
  'pip-br':      { x: 65, y: 60, width: 30, height: 30, opacity: 1, borderRadius: 8 },
  'pip-bl':      { x: 5,  y: 60, width: 30, height: 30, opacity: 1, borderRadius: 8 },
  'pip-tr':      { x: 65, y: 5,  width: 30, height: 30, opacity: 1, borderRadius: 8 },
  'pip-tl':      { x: 5,  y: 5,  width: 30, height: 30, opacity: 1, borderRadius: 8 },
  'pip-center':  { x: 25, y: 25, width: 50, height: 50, opacity: 1, borderRadius: 0 },
  'side-by-side':{ x: 50, y: 0,  width: 50, height: 100,opacity: 1, borderRadius: 0 },
  'custom':      { x: 10, y: 10, width: 40, height: 40, opacity: 1, borderRadius: 0 },
};

export interface RawAsset {
  id: string;
  file_name: string;
  file_path: string;
  asset_type: 'media' | 'synth';
  duration_ms: number;
  width: number;
  height: number;
  fps: number;
  has_audio: boolean;
  codec: string;
  thumbnail_url: string;
  waveform_url: string | null;
  stream_url: string;
  error?: string;
}

export interface AssetsResponse {
  assets: RawAsset[];
  total: number;
}

export interface TranscriptWord {
  text: string;
  start_ms: number;
  end_ms: number;
  speaker_id: string | null;
}

export interface MasterIndexEntry {
  asset_file: string;
  start_ms: number;
  end_ms: number;
  transcript: string;
  words: TranscriptWord[];
  visual_descriptions: string[];
  speaker_id: string | null;
}

export interface MasterIndexResponse {
  entries: MasterIndexEntry[];
  total: number;
}

export interface PaperEditClip {
  clip_id: string;
  source_file: string;
  in_ms: number;
  out_ms: number;
  clip_type: ClipType;
  overlay_text?: string;
  transition?: TransitionType;
  transition_duration_ms?: number;
  search_query?: string;
  description: string;
}

export interface PaperEdit {
  title: string;
  total_duration_ms: number;
  clips: PaperEditClip[];
  music_prompt?: string;
  music_path?: string;    // path to background music file (from sound engineer)
}

// ─── Track types for multi-track timeline ──────────────────────────────────────

export type TrackType = 'video' | 'audio';

export interface Track {
  id: string;
  label: string;         // e.g. "V1", "V2", "A1", "A2"
  type: TrackType;
  muted: boolean;
  solo: boolean;
  volume: number;         // 0..1 — only meaningful for audio tracks
  locked: boolean;
  height: number;         // track height in px (default 48 for video, 32 for audio)
}

export interface PipelineError {
  agent: string;
  message: string;
  phase: string;
  recoverable: boolean;
  details?: string;
}

export interface PipelineStatus {
  phase: Phase;
  project_name: string;
  user_prompt: string;
  created_at: string; // ISO 8601
  asset_count: number;
  index_count: number;
  synth_count: number;
  render_path: string | null;
  timeline_path: string | null;
  errors: PipelineError[];
  /** Render stage label — present during active renders */
  stage?: string;
  /** Render progress 0-1 — present during active renders */
  progress?: number;
}

export interface RunRequest {
  prompt: string;
  project_name?: string;
}

export interface RunResponse {
  status: 'started' | 'awaiting_approval' | 'complete';
  thread_id?: string;
  message?: string;
  phase?: string;
  render_path?: string;
  timeline_path?: string;
}

export interface ApprovalRequest {
  action: 'approve' | 'reject';
  paper_edit?: PaperEdit;
  reason?: string;
}

export interface OutputFile {
  file_name: string;
  file_path: string;
  size_bytes: number;
  download_url: string;
  type: string;
}

export interface OutputResponse {
  files: OutputFile[];
  total: number;
}

export type WSEvent =
  | { type: 'connected'; phase: Phase; version: string; overlay_clips?: Record<string, unknown>[]; music_path?: string }
  | { type: 'pipeline_started'; thread_id: string }
  | { type: 'phase_update'; node: string; phase: Phase; timestamp: string; errors: PipelineError[] }
  | { type: 'awaiting_approval'; paper_edit: PaperEdit }
  | { type: 'pipeline_complete'; phase: Phase; render_path?: string; timeline_path?: string; music_path?: string; overlay_clips?: Record<string, unknown>[] }
  | { type: 'caption_style_options'; styles: CaptionStylePreset[] }
  | { type: 'pong' };

// ─── Caption style presets ─────────────────────────────────────────────────────

export interface CaptionStylePreset {
  id: string;
  name: string;
  description: string;
  preview: string;           // emoji + short label
  font_name: string;
  font_size: number;
  active_color: string;      // ASS colour
  inactive_color: string;
  outline_color: string;
  bg_color: string;
  outline_size: number;
  position: 'top' | 'center' | 'bottom';
  border_style: number;
}

// ─── Edit types (post-pipeline) ────────────────────────────────────────────────

export interface EditRequest {
  instruction: string;
  edit_type?: 'rescript' | 'resynthesize' | 'rerender' | 'audio' | 'general';
}

export interface EditResponse {
  status: 'awaiting_approval' | 'complete' | 'error';
  message: string;
  phase?: string;
  paper_edit?: PaperEdit;
  render_path?: string;
}
