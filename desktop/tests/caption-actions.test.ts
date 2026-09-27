import { beforeEach, expect, test, vi } from "vitest";
vi.mock("../src/lib/api", () => ({ KinetographAPI: { selectCaptionStyle: vi.fn() } }));
vi.mock("@/store/use-chat-store", () => ({ useChatStore: { getState: vi.fn() } }));
import { KinetographAPI } from "../src/lib/api";
import { useChatStore } from "../src/store/use-chat-store";
import { applyCaptionStyle } from "../src/lib/caption-actions";
const chat = {
  pipelineActive: false, isProcessing: false,
  setPipelineActive: vi.fn((value: boolean) => { chat.pipelineActive = value; }),
  setProcessing: vi.fn((value: boolean) => { chat.isProcessing = value; }),
  addUserMessage: vi.fn(),
};
beforeEach(() => {
  vi.clearAllMocks();
  chat.pipelineActive = false;
  chat.isProcessing = false;
  vi.mocked(useChatStore.getState).mockReturnValue(chat as unknown as ReturnType<typeof useChatStore.getState>);
});
test("applies a style atomically and stays busy until completion", async () => {
  vi.mocked(KinetographAPI.selectCaptionStyle).mockResolvedValue({ status: "started", style_id: "none", style_name: "None" });
  await applyCaptionStyle("none");
  expect(KinetographAPI.selectCaptionStyle).toHaveBeenCalledExactlyOnceWith("none", true);
  expect(chat.pipelineActive).toBe(true);
  await expect(applyCaptionStyle("bold-yellow")).rejects.toThrow("Wait");
  expect(KinetographAPI.selectCaptionStyle).toHaveBeenCalledTimes(1);
});
test("failed requests release the UI so the user can retry", async () => {
  vi.mocked(KinetographAPI.selectCaptionStyle).mockRejectedValue(new Error("Disconnected"));
  await expect(applyCaptionStyle("clean-white")).rejects.toThrow("Disconnected");
  expect(chat.pipelineActive).toBe(false);
  expect(chat.isProcessing).toBe(false);
  expect(chat.addUserMessage).not.toHaveBeenCalled();
});
