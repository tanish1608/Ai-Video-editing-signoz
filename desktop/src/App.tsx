import { useState, useEffect } from "react";
import { getBackendUrl } from "@/lib/backend";
import { reinitializeApi } from "@/lib/api";
import { resetForProjectSwitch } from "@/lib/project-session";
import { Editor } from "@/pages/Editor";
import { Welcome } from "@/pages/Welcome";
import { Settings } from "@/pages/Settings";

type View = "loading" | "welcome" | "editor" | "settings";

export function App() {
  const [view, setView] = useState<View>("loading");
  const [backendStatus, setBackendStatus] = useState<"starting" | "running" | "error" | "stopped">("starting");
  // Bumped whenever a different project is opened so the Editor fully remounts
  // (re-connecting the CRDT provider and re-fetching assets for the new project).
  const [projectKey, setProjectKey] = useState(0);

  // Initialize backend URL and listen for main process events
  useEffect(() => {
    const unsubs: Array<() => void> = [];

    async function init() {
      // Resolve backend URL from Electron main process
      await getBackendUrl();
      reinitializeApi();

      // Listen for backend status from main process
      if (window.electron) {
        unsubs.push(window.electron.onBackendStatus((status) => {
          setBackendStatus(status as typeof backendStatus);
        }));

        // Query current status (fixes race condition: main may have sent
        // 'running' before this listener was registered)
        try {
          const currentStatus = await window.electron.getBackendStatus();
          if (currentStatus) setBackendStatus(currentStatus as typeof backendStatus);
        } catch { /* ignore if not available */ }

        // Listen for navigation from menu
        unsubs.push(window.electron.onNavigate((route) => {
          if (route === "settings") setView("settings");
          if (route === "export") setView("editor");
        }));

        // macOS File → Open/New Project switches the backend project dir; the
        // renderer must fully reset (timeline, assets, undo) and remount the
        // Editor for the new project — otherwise the previous project's state
        // bleeds through.
        unsubs.push(window.electron.onProjectOpened(() => {
          resetForProjectSwitch();
          setProjectKey((k) => k + 1);
          setView("editor");
        }));
      }

      // Show welcome/editor after init
      setView("welcome");
    }

    init();

    // Listen for navigate-settings events from Editor's settings button
    const onNavSettings = () => setView("settings");
    document.addEventListener("navigate-settings", onNavSettings);

    return () => {
      for (const unsub of unsubs) unsub();
      document.removeEventListener("navigate-settings", onNavSettings);
    };
  }, []);

  // Loading screen
  if (view === "loading") {
    return (
      <div className="flex h-screen items-center justify-center bg-[#0c0c0e]">
        <div className="flex flex-col items-center gap-4">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-zinc-700 border-t-blue-500" />
          <p className="text-sm text-zinc-500">Starting Kinetograph…</p>
        </div>
      </div>
    );
  }

  if (view === "settings") {
    return <Settings onBack={() => setView("editor")} />;
  }

  if (view === "welcome") {
    return (
      <Welcome
        backendStatus={backendStatus}
        onOpenEditor={() => setView("editor")}
      />
    );
  }

  return <Editor key={projectKey} />;
}
