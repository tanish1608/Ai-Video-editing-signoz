import { currentRenderPath } from "@/lib/render-output";
import { useEffect, useRef, useCallback, useState } from "react";
import { useKinetographStore } from "@/store/use-kinetograph-store";
import { useChatStore } from "@/store/use-chat-store";
import { WSEvent, Phase, OverlayClip, OVERLAY_PRESETS, OverlayPreset } from "@/types/kinetograph";
import { PHASE_DESCRIPTIONS, NODE_TO_AGENT } from "@/types/chat";
import { KinetographAPI } from "@/lib/api";
import { getBackendUrlSync, getWebSocketUrl } from "@/lib/backend";

// ─── Shared V2-clips-from-backend mapper ────────────────────────────────────
function mapOverlayClips(raw: Record<string, unknown>[], idPrefix: string): OverlayClip[] {
	const store = useKinetographStore.getState();
	return raw.map((oc, i) => {
		const preset = (oc.overlay_preset as OverlayPreset) || "pip-br";
		const transform = OVERLAY_PRESETS[preset] || OVERLAY_PRESETS["pip-br"];
		const sourceFile = typeof oc.source_file === "string" ? oc.source_file : "";
		return {
			id: `${idPrefix}-${i}`,
			sourceAssetId: store.assets.find((a) => sourceFile.includes(a.file_name))?.id ?? "",
			sourceFile: sourceFile.split("/").pop() || sourceFile,
			inMs: (oc.in_ms as number) || 0,
			outMs: (oc.out_ms as number) || 3000,
			timelineStartMs: (oc.timeline_start_ms as number) || 0,
			// Pipeline-produced overlays (Scripter's PiP A-roll-over-B-roll) live
			// on the V2 track. A track_id from the backend overrides this.
			trackId: typeof oc.track_id === "string" ? oc.track_id : "V2",
			transform: { ...transform },
			preset,
		} satisfies OverlayClip;
	});
}

// Exponential backoff for reconnection: 1s, 2s, 4s … capped at 30s, so a
// backend that is down for a while doesn't get hammered with a flat 3s retry.
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

export function useKinetographWS() {
	const ws = useRef<WebSocket | null>(null);
	const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
	const reconnectAttempts = useRef(0);
	const [isConnected, setIsConnected] = useState(false);

	// ── Message handler in a ref — always reads latest store actions ──
	const onMessage = useCallback((event: MessageEvent) => {
		try {
			const data: WSEvent = JSON.parse(event.data);
			const chat = useChatStore.getState();
			const store = useKinetographStore.getState();

			switch (data.type) {
				case "connected":
					store.setPhase(data.phase);
					if (
						data.phase === Phase.IDLE ||
						data.phase === Phase.COMPLETE ||
						data.phase === Phase.ERROR
					) {
						chat.setAgentActivity(null);
						chat.setProcessing(false);
						chat.setPipelineActive(false);
					}

					// Music is restored from persisted extras. Overlay (V2/V3…)
					// clips are NOT restored here anymore — they now live in the
					// CRDT doc and are restored via the /ws/crdt sync-step-2 on
					// connect, so restoring them from timeline_extras too would
					// race and duplicate.
					if (data.music_path) {
						store.setMusicPath(data.music_path);
					}

					// Restore rendered video URL if this project was previously completed
					if (data.phase === Phase.COMPLETE || data.phase === Phase.IDLE) {
						const backendUrl = getBackendUrlSync();
						KinetographAPI.getOutputs()
							.then((out) => {
								const renderPath = currentRenderPath(out.files, data.render_path);
								if (renderPath) {
									store.setRenderUrl(
										`${backendUrl}/api/assets/stream?path=${encodeURIComponent(renderPath)}`,
									);
								}
							})
							.catch(() => {});
					}
					break;

				case "pipeline_started":
					chat.removeLoadingMessages();
					chat.setThreadId(data.thread_id);
					store.setRenderUrl(null);
					chat.addSystemMessage(
						`🔗 Pipeline session started (${data.thread_id.slice(0, 8)}...)`,
					);
					break;

				case "phase_update": {
					const phaseErrors = data.errors ?? [];
					store.setPhase(data.phase);
					if (phaseErrors.length > 0) phaseErrors.forEach(store.addError);

					const agentName = NODE_TO_AGENT[data.node] || data.node;
					const description =
						PHASE_DESCRIPTIONS[data.phase as Phase] || `${agentName} → ${data.phase}`;

					const phaseStr = data.phase.toString();
					const isCompleted =
						phaseStr.endsWith("ed") ||
						data.phase === Phase.COMPLETE ||
						data.phase === Phase.ERROR ||
						data.phase === Phase.AWAITING_APPROVAL;

					if (isCompleted) {
						chat.setAgentActivity(null);
					} else {
						chat.setAgentActivity({
							agent: agentName,
							phase: data.phase as Phase,
							description,
							startedAt: Date.now(),
							isActive: true,
						});
					}

					chat.removeLoadingMessages();
					if (isCompleted) {
						const msgs = useChatStore.getState().messages;
						const lastInProgress = [...msgs]
							.reverse()
							.find(
								(m) =>
									m.type === "agent-update" &&
									m.agent === agentName &&
									!m.phase?.toString().endsWith("ed") &&
									m.phase !== "complete",
							);
						if (lastInProgress) {
							chat.updateMessage(lastInProgress.id, {
								phase: data.phase as Phase,
								content: description,
							});
						} else {
							chat.addAgentUpdate(agentName, data.phase as Phase, description);
						}
					} else {
						chat.addAgentUpdate(agentName, data.phase as Phase, description);
					}

					if (phaseErrors.length > 0) {
						chat.addPipelineError(phaseErrors, `⚠️ ${agentName} encountered errors.`);
					}

					if (data.phase === Phase.SCRIPTED || data.phase === Phase.RENDERED) {
						// Paper edit is synced via Yjs CRDT — no need to fetch via REST
						chat.setPipelineActive(false);
						chat.setAgentActivity(null);
					}

					if (data.phase === Phase.ERROR) {
						chat.setProcessing(false);
						chat.setPipelineActive(false);
						chat.setAgentActivity(null);
					}
					break;
				}

				case "awaiting_approval":
					store.setPhase(Phase.AWAITING_APPROVAL);
					store.setPaperEdit(data.paper_edit);
					chat.removeLoadingMessages();
					chat.setAgentActivity(null);
					chat.addApprovalRequest(data.paper_edit);
					break;

				case "caption_style_options":
					break;

				case "pipeline_complete": {
					const completedPhase = data.phase || Phase.COMPLETE;
					store.setPhase(completedPhase);
					chat.setProcessing(false);
					chat.setPipelineActive(false);
					chat.setAgentActivity(null);
					chat.removeLoadingMessages();

					if (completedPhase === Phase.ERROR) {
						chat.addPipelineError(
							[],
							"❌ Pipeline encountered an error and could not complete. Open the run log for details, or try again.",
							data.log_dir,
						);
						break;
					}

					if (data.music_path) store.setMusicPath(data.music_path);

					if (data.overlay_clips?.length) {
						store.setV2Clips(mapOverlayClips(data.overlay_clips, "v2-pipeline"));
					}

					KinetographAPI.getAssets()
						.then((res) => store.setAssets(res.assets))
						.catch(() => {});

					const backendUrl = getBackendUrlSync();
					KinetographAPI.getOutputs()
						.then((out) => {
							const renderPath = currentRenderPath(out.files, data.render_path);
							const timeline = out.files.find((f) => f.type === "otio");

							if (renderPath) {
								// Cache-bust so the <video> element reloads even if the filename is the same
								store.setRenderUrl(
									`${backendUrl}/api/assets/stream?path=${encodeURIComponent(renderPath)}&t=${Date.now()}`,
								);
							}

							chat.addPipelineComplete(
								renderPath || data.render_path,
								timeline?.file_path || data.timeline_path,
							);
						})
						.catch(() => {
							chat.addPipelineComplete(data.render_path, data.timeline_path);
						});
					break;
				}

				case "pipeline_stopped":
					store.setPhase(Phase.IDLE);
					chat.setProcessing(false);
					chat.setPipelineActive(false);
					chat.setAgentActivity(null);
					chat.removeLoadingMessages();
					chat.addMessage({
						role: "assistant",
						type: "text",
						content: "⏹️ Pipeline stopped. You can send a new prompt or edit when ready.",
						logDir: data.log_dir,
					});
					break;

				case "pong":
					break;
			}
		} catch (err) {
			console.error("WS parse error:", err);
		}
	}, []);

	const onMessageRef = useRef(onMessage);
	onMessageRef.current = onMessage;

	// ── connect: always clears reconnect timer & old socket first ──
	const connect = useCallback(() => {
		// Cancel any pending reconnect
		if (reconnectTimer.current) {
			clearTimeout(reconnectTimer.current);
			reconnectTimer.current = null;
		}

		// Tear down previous socket cleanly (null onclose first to prevent
		// the onclose handler from scheduling yet another reconnect).
		if (ws.current) {
			ws.current.onclose = null;
			ws.current.close();
			ws.current = null;
		}

		const wsUrl = getWebSocketUrl(getBackendUrlSync());
		const socket = new WebSocket(wsUrl);

		socket.onopen = () => {
			setIsConnected(true);
			reconnectAttempts.current = 0; // reset backoff on a successful connect
		};
		socket.onclose = () => {
			setIsConnected(false);
			// Only reconnect if this socket is still the active one
			// (prevents stale sockets from spawning extra connections).
			if (ws.current === socket) {
				ws.current = null;
				const delay = Math.min(
					RECONNECT_MAX_MS,
					RECONNECT_BASE_MS * 2 ** reconnectAttempts.current,
				);
				reconnectAttempts.current += 1;
				console.log(`WS disconnected. Reconnecting in ${delay / 1000}s...`);
				reconnectTimer.current = setTimeout(() => connect(), delay);
			}
		};

		// Delegate to the ref so the handler is always up-to-date
		// without needing to recreate connect().
		socket.onmessage = (ev) => onMessageRef.current(ev);

		ws.current = socket;
	}, []); // stable — no deps

	// ── Open exactly one connection on mount, tear down on unmount ──
	useEffect(() => {
		connect();
		return () => {
			if (reconnectTimer.current) {
				clearTimeout(reconnectTimer.current);
				reconnectTimer.current = null;
			}
			if (ws.current) {
				ws.current.onclose = null;
				ws.current.close();
				ws.current = null;
			}
		};
	}, [connect]);

	// ── Keepalive ping ──
	useEffect(() => {
		const interval = setInterval(() => {
			if (ws.current?.readyState === WebSocket.OPEN) {
				ws.current.send(JSON.stringify({ type: "ping" }));
			}
		}, 20000);
		return () => clearInterval(interval);
	}, []);

	// ── Auto-clear stuck agent activity after 3 min ──
	useEffect(() => {
		const interval = setInterval(() => {
			const { agentActivity, setAgentActivity, setProcessing, setPipelineActive } =
				useChatStore.getState();
			if (agentActivity?.isActive && agentActivity.startedAt) {
				const elapsed = Date.now() - agentActivity.startedAt;
				if (elapsed > 3 * 60 * 1000) {
					console.warn("Agent activity stuck for >3min, auto-clearing spinner");
					setAgentActivity(null);
					setProcessing(false);
					setPipelineActive(false);
				}
			}
		}, 10000);
		return () => clearInterval(interval);
	}, []);

	return { isConnected };
}
