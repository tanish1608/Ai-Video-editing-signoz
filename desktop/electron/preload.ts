import { contextBridge, ipcRenderer } from "electron";

// ─── Expose a safe API to the renderer process ─────────────────────────────────

const electronAPI = {
  // Project management
  openProject: () => ipcRenderer.invoke("open-project") as Promise<string | null>,
  newProject: () => ipcRenderer.invoke("new-project") as Promise<string | null>,
  importMedia: () => ipcRenderer.invoke("import-media") as Promise<string[]>,
  getProjectDir: () => ipcRenderer.invoke("get-project-dir") as Promise<string | null>,

  // Backend
  getBackendUrl: () => ipcRenderer.invoke("get-backend-url") as Promise<string>,
  getBackendStatus: () => ipcRenderer.invoke("get-backend-status") as Promise<string>,
  restartBackend: () => ipcRenderer.invoke("restart-backend") as Promise<boolean>,

  // App info
  getAppVersion: () => ipcRenderer.invoke("get-app-version") as Promise<string>,
  getUserDataPath: () => ipcRenderer.invoke("get-user-data-path") as Promise<string>,
  isDev: () => ipcRenderer.invoke("is-dev") as Promise<boolean>,

  // Settings
  getSettings: () => ipcRenderer.invoke("get-settings") as Promise<Record<string, unknown>>,
  saveSettings: (settings: Record<string, unknown>) => ipcRenderer.invoke("save-settings", settings),

  // Recent projects
  getRecentProjects: () => ipcRenderer.invoke("get-recent-projects") as Promise<string[]>,
  addRecentProject: (projectPath: string) => ipcRenderer.invoke("add-recent-project", projectPath) as Promise<string[]>,

  // Shell
  showItemInFolder: (filePath: string) => ipcRenderer.invoke("show-item-in-folder", filePath),
  openExternal: (url: string) => ipcRenderer.invoke("open-external", url),

  // Events from main process
  onBackendStatus: (callback: (status: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, status: string) => callback(status);
    ipcRenderer.on("backend-status", handler);
    return () => ipcRenderer.removeListener("backend-status", handler);
  },

  onBackendLog: (callback: (log: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, log: string) => callback(log);
    ipcRenderer.on("backend-log", handler);
    return () => ipcRenderer.removeListener("backend-log", handler);
  },

  onNavigate: (callback: (route: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, route: string) => callback(route);
    ipcRenderer.on("navigate", handler);
    return () => ipcRenderer.removeListener("navigate", handler);
  },

  onAction: (callback: (action: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, action: string) => callback(action);
    ipcRenderer.on("action", handler);
    return () => ipcRenderer.removeListener("action", handler);
  },

  onProjectOpened: (callback: (path: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, projectPath: string) => callback(projectPath);
    ipcRenderer.on("project-opened", handler);
    return () => ipcRenderer.removeListener("project-opened", handler);
  },

  onImportMedia: (callback: () => void) => {
    const handler = () => callback();
    ipcRenderer.on("import-media", handler);
    return () => ipcRenderer.removeListener("import-media", handler);
  },
};

contextBridge.exposeInMainWorld("electron", electronAPI);

// ─── Type declaration for the renderer ──────────────────────────────────────────

export type ElectronAPI = typeof electronAPI;
