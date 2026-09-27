import type { OutputFile } from "@/types/kinetograph";

/** The pipeline owns the current render; filenames cannot identify the active revision. */
export function currentRenderPath(files: OutputFile[], renderPath?: string | null): string | undefined {
  if (renderPath) return renderPath;
  const videos = files.filter((file) => file.type === "mp4");
  return videos.find((file) => file.file_name.includes("captioned"))?.file_path
    ?? videos.find((file) => file.file_name.includes("mastered"))?.file_path
    ?? videos.at(-1)?.file_path;
}
