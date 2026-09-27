import { KinetographAPI } from "./api";
import { useChatStore } from "@/store/use-chat-store";

/** Keep the UI busy until the pipeline completion event, not just the HTTP response. */
export async function applyCaptionStyle(styleId: string): Promise<void> {
  const chat = useChatStore.getState();
  if (chat.pipelineActive) throw new Error("Wait for the current edit to finish.");
  chat.setPipelineActive(true);
  chat.setProcessing(true);
  try {
    await KinetographAPI.selectCaptionStyle(styleId, true);
    chat.addUserMessage("Apply caption style: " + styleId);
  } catch (error) {
    chat.setPipelineActive(false);
    chat.setProcessing(false);
    throw error;
  }
}
