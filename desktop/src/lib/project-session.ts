/**
 * Project-switch reset — the single routine that returns the renderer to a
 * clean slate before entering or switching projects.
 *
 * Used both by the Welcome screen (New/Open/Recent) and by the App-level
 * `project-opened` subscription (macOS File → Open Project), so opening a
 * project from the menu can no longer leave a stale timeline, asset list, or
 * undo stack from the previous project.
 */

import { disconnectProvider, resetDoc } from "@/lib/crdt";
import { useKinetographStore } from "@/store/use-kinetograph-store";

/**
 * Tear down the current project's client state:
 *  - disconnect the CRDT websocket provider,
 *  - wipe the Yjs doc + undo history (resetDoc clears the UndoManager),
 *  - reset the Zustand store (assets, selection, tracks, color grade, …).
 *
 * After this, re-mounting the Editor (or reconnecting the provider) pulls the
 * new project's state from the backend via sync-step-2.
 */
export function resetForProjectSwitch(): void {
	disconnectProvider();
	resetDoc();
	useKinetographStore.getState().resetProject();
}
