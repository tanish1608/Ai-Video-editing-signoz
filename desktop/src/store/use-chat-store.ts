import { create } from "zustand";
import type {
	ChatMessage,
	ChatMessageType,
	ChatRole,
	AgentActivity,
	EditType,
} from "@/types/chat";
import type { Phase, PaperEdit, PipelineError, CaptionStylePreset } from "@/types/kinetograph";

// ─── Unique ID generator ──────────────────────────────────────────────────────

let _seq = 0;
function chatId(): string {
	return `msg-${Date.now()}-${++_seq}`;
}

// ─── Store Interface ──────────────────────────────────────────────────────────

interface ChatState {
	// Messages
	messages: ChatMessage[];
	isOpen: boolean;
	isProcessing: boolean;
	threadId: string | null;

	// Current agent activity
	agentActivity: AgentActivity | null;

	// Pipeline state tracking
	pipelineActive: boolean;
	pipelinePhase: Phase | null;

	// Actions
	addMessage: (msg: Omit<ChatMessage, "id" | "timestamp">) => string;
	updateMessage: (id: string, updates: Partial<ChatMessage>) => void;
	removeMessage: (id: string) => void;

	setOpen: (open: boolean) => void;
	toggleOpen: () => void;
	setProcessing: (processing: boolean) => void;
	setThreadId: (id: string | null) => void;

	setAgentActivity: (activity: AgentActivity | null) => void;
	setPipelineActive: (active: boolean) => void;
	setPipelinePhase: (phase: Phase | null) => void;

	// Convenience message helpers
	addUserMessage: (content: string) => string;
	addSystemMessage: (content: string, type?: ChatMessageType) => string;
	addAgentUpdate: (agent: string, phase: Phase, content: string) => string;
	addApprovalRequest: (paperEdit: PaperEdit) => string;
	addPipelineComplete: (renderPath?: string, timelinePath?: string) => string;
	addPipelineError: (errors: PipelineError[], content: string) => string;
	addCaptionStyleRequest: (styles: CaptionStylePreset[]) => string;
	addLoadingMessage: () => string;
	removeLoadingMessages: () => void;

	clearMessages: () => void;
}

// ─── Store ────────────────────────────────────────────────────────────────────

export const useChatStore = create<ChatState>((set, get) => ({
	messages: [],
	isOpen: false,
	isProcessing: false,
	threadId: null,
	agentActivity: null,
	pipelineActive: false,
	pipelinePhase: null,

	addMessage: (msg) => {
		const id = chatId();
		const message: ChatMessage = { ...msg, id, timestamp: Date.now() };
		set((state) => ({ messages: [...state.messages, message] }));
		return id;
	},

	updateMessage: (id, updates) =>
		set((state) => ({
			messages: state.messages.map((m) =>
				m.id === id ? { ...m, ...updates } : m,
			),
		})),

	removeMessage: (id) =>
		set((state) => ({
			messages: state.messages.filter((m) => m.id !== id),
		})),

	setOpen: (open) => set({ isOpen: open }),
	toggleOpen: () => set((state) => ({ isOpen: !state.isOpen })),
	setProcessing: (processing) => set({ isProcessing: processing }),
	setThreadId: (id) => set({ threadId: id }),

	setAgentActivity: (activity) => set({ agentActivity: activity }),
	setPipelineActive: (active) => set({ pipelineActive: active }),
	setPipelinePhase: (phase) => set({ pipelinePhase: phase }),

	// ─── Convenience helpers ───────────────────────────────────────────

	addUserMessage: (content) => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "user" as ChatRole,
					type: "text" as ChatMessageType,
					content,
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	addSystemMessage: (content, type = "text") => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "assistant" as ChatRole,
					type,
					content,
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	addAgentUpdate: (agent, phase, content) => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "assistant" as ChatRole,
					type: "agent-update" as ChatMessageType,
					content,
					agent,
					phase,
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	addApprovalRequest: (paperEdit) => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "assistant" as ChatRole,
					type: "approval-request" as ChatMessageType,
					content:
						"Your paper edit is ready for review! Check the timeline and approve or request changes.",
					paperEdit,
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	addPipelineComplete: (renderPath, timelinePath) => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "assistant" as ChatRole,
					type: "pipeline-complete" as ChatMessageType,
					content:
						"🎉 Your video is ready! It's been loaded into the timeline. You can edit it manually or export it.",
					renderPath,
					timelinePath,
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	addPipelineError: (errors, content) => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "assistant" as ChatRole,
					type: "pipeline-error" as ChatMessageType,
					content,
					errors,
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	addCaptionStyleRequest: (styles) => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "assistant" as ChatRole,
					type: "caption-style-request" as ChatMessageType,
					content: "Choose a caption style for your video. The captioner will burn your selection into the final render.",
					captionStyles: styles,
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	addLoadingMessage: () => {
		const id = chatId();
		set((state) => ({
			messages: [
				...state.messages,
				{
					id,
					role: "assistant" as ChatRole,
					type: "loading" as ChatMessageType,
					content: "",
					timestamp: Date.now(),
				},
			],
		}));
		return id;
	},

	removeLoadingMessages: () =>
		set((state) => ({
			messages: state.messages.filter((m) => m.type !== "loading"),
		})),

	clearMessages: () =>
		set({
			messages: [],
			agentActivity: null,
			pipelineActive: false,
			pipelinePhase: null,
			threadId: null,
		}),
}));
