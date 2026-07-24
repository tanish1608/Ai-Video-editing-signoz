import { useState, useEffect, useCallback } from "react";
import { Film, FolderOpen, Plus, Settings, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import { resetForProjectSwitch } from "@/lib/project-session";

interface WelcomeProps {
  backendStatus: "starting" | "running" | "error" | "stopped";
  onOpenEditor: () => void;
}

export function Welcome({ backendStatus, onOpenEditor }: WelcomeProps) {
  const [recentProjects, setRecentProjects] = useState<string[]>([]);

  useEffect(() => {
    window.electron?.getRecentProjects().then(setRecentProjects).catch(() => {});
  }, []);

  /** Clean slate before entering the editor for a (potentially different) project. */
  const prepareAndOpen = useCallback(() => {
    // Disconnect any lingering CRDT provider & wipe the Yjs doc so the
    // backend's sync-step-2 repopulates with the correct project data.
    resetForProjectSwitch();
    onOpenEditor();
  }, [onOpenEditor]);

  const handleNewProject = async () => {
    const dir = await window.electron?.newProject();
    if (dir) {
      await window.electron?.addRecentProject(dir);
      prepareAndOpen();
    }
  };

  const handleOpenProject = async () => {
    const dir = await window.electron?.openProject();
    if (dir) {
      await window.electron?.addRecentProject(dir);
      prepareAndOpen();
    }
  };

  const handleOpenRecent = async (projectPath: string) => {
    // For recent projects we need to tell the backend which project to load.
    // openProject() shows a dialog; for recents we POST the dir directly.
    try {
      const backendUrl = await window.electron?.getBackendUrl();
      if (backendUrl) {
        await fetch(`${backendUrl}/api/project/set-dir`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_dir: projectPath }),
        });
      }
    } catch (e) {
      console.warn("Failed to set project dir on backend for recent project:", e);
    }
    await window.electron?.addRecentProject(projectPath);
    prepareAndOpen();
  };

  const statusColor = {
    starting: "text-yellow-400",
    running: "text-emerald-400",
    error: "text-red-400",
    stopped: "text-zinc-500",
  }[backendStatus];

  const statusText = {
    starting: "Starting engine…",
    running: "Engine ready",
    error: "Engine failed to start",
    stopped: "Engine stopped",
  }[backendStatus];

  return (
    <div className="flex h-screen flex-col bg-[#0c0c0e] text-zinc-100">
      {/* Drag region for macOS */}
      <div className="drag-region h-8 w-full shrink-0" />

      <div className="flex flex-1 items-center justify-center">
        <div className="flex flex-col items-center gap-10 max-w-lg w-full px-8">
          {/* Logo */}
          <div className="flex items-center gap-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-blue-600 to-purple-600">
              <Film className="h-6 w-6 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight">Kinetograph</h1>
              <p className="text-xs text-zinc-500">AI-Powered Video Editor</p>
            </div>
          </div>

          {/* Status indicator */}
          <div className="flex items-center gap-2">
            <div className={cn("h-2 w-2 rounded-full", statusColor, backendStatus === "starting" && "animate-pulse")} />
            <span className={cn("text-xs", statusColor)}>{statusText}</span>
          </div>

          {/* Actions */}
          <div className="flex gap-3 w-full">
            <button
              onClick={handleNewProject}
              disabled={backendStatus !== "running"}
              className="flex-1 flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-3 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <Plus className="h-4 w-4" />
              New Project
            </button>
            <button
              onClick={handleOpenProject}
              disabled={backendStatus !== "running"}
              className="flex-1 flex items-center justify-center gap-2 rounded-lg bg-zinc-800 px-4 py-3 text-sm font-medium text-zinc-300 hover:bg-zinc-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <FolderOpen className="h-4 w-4" />
              Open Project
            </button>
          </div>

          {/* Quick start — skip project selection, use default workspace */}
          <button
            onClick={onOpenEditor}
            disabled={backendStatus !== "running"}
            className="flex items-center gap-2 text-xs text-zinc-500 hover:text-zinc-300 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Zap className="h-3 w-3" />
            Quick start (use default workspace)
          </button>

          {/* Recent projects */}
          {recentProjects.length > 0 && (
            <div className="w-full">
              <h3 className="text-xs font-medium text-zinc-500 uppercase tracking-wider mb-2">Recent Projects</h3>
              <div className="flex flex-col gap-1">
                {recentProjects.map((project) => (
                  <button
                    key={project}
                    onClick={() => handleOpenRecent(project)}
                    disabled={backendStatus !== "running"}
                    className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200 transition-colors disabled:opacity-40"
                  >
                    <FolderOpen className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{project.split("/").pop()}</span>
                    <span className="ml-auto text-[10px] text-zinc-600 truncate max-w-[200px]">{project}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-center gap-4 pb-6 text-[10px] text-zinc-600">
        <span>v1.0.0</span>
        <span>•</span>
        <button
          onClick={() => window.electron?.openExternal("https://github.com/kinetograph/kinetograph")}
          className="hover:text-zinc-400 transition-colors"
        >
          GitHub
        </button>
      </div>
    </div>
  );
}
