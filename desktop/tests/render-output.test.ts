import { expect, test } from "vitest";
import { currentRenderPath } from "../src/lib/render-output";
const files = [
  { file_name: "old_mastered.mp4", file_path: "/old_mastered.mp4", type: "mp4", size_bytes: 1, download_url: "" },
  { file_name: "captioned.mp4", file_path: "/new/captioned.mp4", type: "mp4", size_bytes: 1, download_url: "" },
];
test("shows the chosen caption revision rather than an older mastered output", () => {
  expect(currentRenderPath(files, "/new/captioned.mp4")).toBe("/new/captioned.mp4");
  expect(currentRenderPath(files, "/clean.mp4")).toBe("/clean.mp4");
});
