import { app, BrowserWindow, ipcMain, dialog, shell, Menu } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import http from "node:http";

// ─── Constants ──────────────────────────────────────────────────────────────────

const BACKEND_PORT = 8080;
const BACKEND_URL = `http://localhost:${BACKEND_PORT}`;
const HEALTH_ENDPOINT = `${BACKEND_URL}/api/health`;
const HEALTH_POLL_MS = 500;
const HEALTH_TIMEOUT_MS = 30_000;

const isDev = process.env.NODE_ENV === "development" || !app.isPackaged;

// ─── State ──────────────────────────────────────────────────────────────────────

let mainWindow: BrowserWindow | null = null;
let pythonProcess: ChildProcess | null = null;
let currentProjectDir: string | null = null;
let backendStatus: "starting" | "running" | "error" | "stopped" = "starting";

function isSafeExternalUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

// ─── Python Sidecar ─────────────────────────────────────────────────────────────

function getPythonCommand(): { cmd: string; args: string[] } {
  if (isDev) {
    // In development, use the venv python from the repo root
    const venvPython = path.join(__dirname, "..", "..", ".venv", "bin", "python3");
    if (fs.existsSync(venvPython)) {
      return {
        cmd: venvPython,
        args: ["-m", "uvicorn", "kinetograph.server:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)],
      };
    }
    // Fallback to system python
    return {
      cmd: "python3",
      args: ["-m", "uvicorn", "kinetograph.server:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)],
    };
  }

  // In production, use bundled PyInstaller binary
  const resourcePath = process.resourcesPath;
  const sidecarName = process.platform === "win32" ? "kinetograph-server.exe" : "kinetograph-server";
  const sidecarPath = path.join(resourcePath, "backend", sidecarName);

  if (fs.existsSync(sidecarPath)) {
    return { cmd: sidecarPath, args: [] };
  }

  throw new Error(`Bundled backend sidecar is missing: ${sidecarPath}`);
}

async function isBackendAlreadyRunning(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(HEALTH_ENDPOINT, (res) => {
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => { req.destroy(); resolve(false); });
  });
}

function startBackend(): void {
  if (pythonProcess) return;

  let command: { cmd: string; args: string[] };
  try {
    command = getPythonCommand();
  } catch (error) {
    console.error("[Kinetograph] Cannot start backend:", error);
    backendStatus = "error";
    mainWindow?.webContents.send("backend-status", "error");
    return;
  }
  const { cmd, args } = command;
  const cwd = isDev ? path.join(__dirname, "..", "..") : process.resourcesPath;
  const envPath = isDev ? path.join(__dirname, "..", "..", ".env") : path.join(app.getPath("userData"), ".env");

  console.log(`[Kinetograph] Starting backend: ${cmd} ${args.join(" ")}`);
  console.log(`[Kinetograph] CWD: ${cwd}`);

  const env: Record<string, string> = {
    ...process.env as Record<string, string>,
    PYTHONDONTWRITEBYTECODE: "1",
    KINETOGRAPH_ENV_FILE: envPath,
  };

  // If a project dir is set, pass it to the backend
  if (currentProjectDir) {
    env.KINETOGRAPH_PROJECT_DIR = currentProjectDir;
  }

  pythonProcess = spawn(cmd, args, {
    cwd,
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });

  pythonProcess.stdout?.on("data", (data: Buffer) => {
    const msg = data.toString().trim();
    if (msg) console.log(`[Backend] ${msg}`);
    mainWindow?.webContents.send("backend-log", msg);
  });

  pythonProcess.stderr?.on("data", (data: Buffer) => {
    const msg = data.toString().trim();
    if (msg) console.error(`[Backend] ${msg}`);
    mainWindow?.webContents.send("backend-log", msg);
  });

  pythonProcess.on("exit", (code, signal) => {
    console.log(`[Kinetograph] Backend exited: code=${code}, signal=${signal}`);
    pythonProcess = null;
    mainWindow?.webContents.send("backend-status", "stopped");
  });

  pythonProcess.on("error", (err) => {
    console.error(`[Kinetograph] Failed to start backend:`, err);
    pythonProcess = null;
    mainWindow?.webContents.send("backend-status", "error");
  });
}

function stopBackend(): void {
  if (!pythonProcess) return;
  console.log("[Kinetograph] Stopping backend...");
  // Capture THIS specific child so a later restart (which reassigns
  // pythonProcess to a new child) is never SIGKILLed by this timer.
  const proc = pythonProcess;
  proc.kill("SIGTERM");
  const forceKill = setTimeout(() => {
    proc.kill("SIGKILL");
  }, 5000);
  // If it exits cleanly first, cancel the force-kill and clear the ref.
  proc.once("exit", () => {
    clearTimeout(forceKill);
    if (pythonProcess === proc) pythonProcess = null;
  });
}

function waitForBackend(): Promise<boolean> {
  return new Promise((resolve) => {
    const start = Date.now();

    const poll = () => {
      if (Date.now() - start > HEALTH_TIMEOUT_MS) {
        console.error("[Kinetograph] Backend health check timed out");
        resolve(false);
        return;
      }

      const req = http.get(HEALTH_ENDPOINT, (res) => {
        if (res.statusCode === 200) {
          resolve(true);
        } else {
          setTimeout(poll, HEALTH_POLL_MS);
        }
      });

      req.on("error", () => {
        setTimeout(poll, HEALTH_POLL_MS);
      });

      req.setTimeout(5000, () => {
        req.destroy();
        setTimeout(poll, HEALTH_POLL_MS);
      });
    };

    poll();
  });
}

// ─── Window ─────────────────────────────────────────────────────────────────────

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    title: "Kinetograph",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    trafficLightPosition: { x: 12, y: 10 },
    backgroundColor: "#0c0c0e",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });

  // Show window when ready to avoid flash
  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
  });

  if (isDev) {
    mainWindow.loadURL("http://localhost:5173");
    mainWindow.webContents.openDevTools({ mode: "detach" });
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  // Open external links in browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeExternalUrl(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
}

function createMenu(): void {
  const template: Electron.MenuItemConstructorOptions[] = [
    ...(process.platform === "darwin"
      ? [
          {
            label: app.name,
            submenu: [
              { role: "about" as const },
              { type: "separator" as const },
              {
                label: "Settings…",
                accelerator: "CmdOrCtrl+,",
                click: () => mainWindow?.webContents.send("navigate", "settings"),
              },
              { type: "separator" as const },
              { role: "hide" as const },
              { role: "hideOthers" as const },
              { role: "unhide" as const },
              { type: "separator" as const },
              { role: "quit" as const },
            ],
          } as Electron.MenuItemConstructorOptions,
        ]
      : []),
    {
      label: "File",
      submenu: [
        {
          label: "Open Project…",
          accelerator: "CmdOrCtrl+O",
          click: () => handleOpenProject(),
        },
        {
          label: "New Project…",
          accelerator: "CmdOrCtrl+N",
          click: () => handleNewProject(),
        },
        { type: "separator" },
        {
          label: "Import Media…",
          accelerator: "CmdOrCtrl+I",
          click: () => mainWindow?.webContents.send("import-media"),
        },
        { type: "separator" },
        {
          label: "Export…",
          accelerator: "CmdOrCtrl+E",
          click: () => mainWindow?.webContents.send("navigate", "export"),
        },
        { type: "separator" },
        process.platform === "darwin" ? { role: "close" } : { role: "quit" },
      ],
    },
    {
      label: "Edit",
      submenu: [
        { label: "Undo", accelerator: "CmdOrCtrl+Z", click: () => mainWindow?.webContents.send("action", "undo") },
        { label: "Redo", accelerator: "CmdOrCtrl+Shift+Z", click: () => mainWindow?.webContents.send("action", "redo") },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "GitHub Repository",
          click: () => shell.openExternal("https://github.com/kinetograph/kinetograph"),
        },
        {
          label: "Report Issue",
          click: () => shell.openExternal("https://github.com/kinetograph/kinetograph/issues"),
        },
      ],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ─── IPC Handlers ───────────────────────────────────────────────────────────────

/** Tell the running backend to switch all paths to a project directory. */
async function notifyBackendProjectDir(projectDir: string): Promise<void> {
  try {
    const res = await new Promise<boolean>((resolve) => {
      const postData = JSON.stringify({ project_dir: projectDir });
      const req = http.request(
        `${BACKEND_URL}/api/project/set-dir`,
        { method: "POST", headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(postData) } },
        (res) => resolve(res.statusCode === 200),
      );
      req.on("error", () => resolve(false));
      req.write(postData);
      req.end();
    });
    if (res) {
      console.log(`[Kinetograph] Backend switched to project: ${projectDir}`);
    } else {
      console.warn(`[Kinetograph] Backend failed to switch project dir`);
    }
  } catch (err) {
    console.warn("[Kinetograph] Could not notify backend of project dir:", err);
  }
}

async function handleOpenProject(): Promise<string | null> {
  const result = await dialog.showOpenDialog(mainWindow!, {
    title: "Open Project",
    properties: ["openDirectory", "createDirectory"],
    buttonLabel: "Open Project",
  });

  if (result.canceled || result.filePaths.length === 0) return null;

  const projectDir = result.filePaths[0];
  currentProjectDir = projectDir;

  // Create project structure if it doesn't exist
  ensureProjectStructure(projectDir);

  // Tell the running backend to switch to this project directory
  await notifyBackendProjectDir(projectDir);

  mainWindow?.webContents.send("project-opened", projectDir);
  return projectDir;
}

async function handleNewProject(): Promise<string | null> {
  const result = await dialog.showSaveDialog(mainWindow!, {
    title: "New Project",
    buttonLabel: "Create Project",
    defaultPath: path.join(app.getPath("documents"), "Untitled Project"),
  });

  if (result.canceled || !result.filePath) return null;

  const projectDir = result.filePath;
  fs.mkdirSync(projectDir, { recursive: true });

  currentProjectDir = projectDir;
  ensureProjectStructure(projectDir);

  // Tell the running backend to switch to this project directory
  await notifyBackendProjectDir(projectDir);

  mainWindow?.webContents.send("project-opened", projectDir);
  return projectDir;
}

function ensureProjectStructure(dir: string): void {
  const dirs = [
    "media",
    "media/.synth",
    "state",
    "output",
    ".cache",
    ".cache/thumbnails",
    ".cache/waveforms",
    ".cache/metadata",
    ".cache/audio",
  ];
  for (const d of dirs) {
    fs.mkdirSync(path.join(dir, d), { recursive: true });
  }

  // Ensure project manifest exists
  const manifestPath = path.join(dir, "state", "project.json");
  if (!fs.existsSync(manifestPath)) {
    const manifest = {
      name: path.basename(dir),
      created: new Date().toISOString(),
      modified: new Date().toISOString(),
      version: "2.0.0",
      media_refs: {},
      settings: {},
    };
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
  }
}

async function handleImportMedia(): Promise<string[]> {
  const result = await dialog.showOpenDialog(mainWindow!, {
    title: "Import Media",
    properties: ["openFile", "multiSelections"],
    filters: [
      { name: "Video Files", extensions: ["mp4", "mov", "avi", "mkv", "webm", "m4v", "mts"] },
      { name: "Audio Files", extensions: ["mp3", "wav", "aac", "m4a", "flac", "ogg"] },
      { name: "All Files", extensions: ["*"] },
    ],
  });

  if (result.canceled) return [];
  return result.filePaths;
}

function setupIPC(): void {
  ipcMain.handle("open-project", handleOpenProject);
  ipcMain.handle("new-project", handleNewProject);
  ipcMain.handle("import-media", handleImportMedia);

  ipcMain.handle("get-backend-url", () => BACKEND_URL);
  ipcMain.handle("get-backend-status", () => backendStatus);
  ipcMain.handle("get-project-dir", () => currentProjectDir);
  ipcMain.handle("is-dev", () => isDev);

  ipcMain.handle("get-app-version", () => app.getVersion());
  ipcMain.handle("get-user-data-path", () => app.getPath("userData"));

  ipcMain.handle("show-item-in-folder", (_event, filePath: unknown) => {
    if (typeof filePath === "string" && path.isAbsolute(filePath) && fs.existsSync(filePath)) {
      shell.showItemInFolder(filePath);
    }
  });

  ipcMain.handle("open-external", (_event, url: unknown) => {
    if (isSafeExternalUrl(url)) return shell.openExternal(url);
    throw new Error("Only http(s) URLs may be opened externally");
  });

  // Settings persistence
  ipcMain.handle("get-settings", () => {
    const settingsPath = path.join(app.getPath("userData"), "settings.json");
    if (fs.existsSync(settingsPath)) {
      return JSON.parse(fs.readFileSync(settingsPath, "utf-8"));
    }
    return {};
  });

  ipcMain.handle("save-settings", (_event, settings: Record<string, unknown>) => {
    const settingsPath = path.join(app.getPath("userData"), "settings.json");
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2));

    // Also write API keys to .env format for the Python backend
    writeEnvFile(settings);
  });

  ipcMain.handle("restart-backend", async () => {
    stopBackend();
    await new Promise((r) => setTimeout(r, 2000));
    startBackend();
    return waitForBackend();
  });

  // Recent projects
  ipcMain.handle("get-recent-projects", () => {
    const recentsPath = path.join(app.getPath("userData"), "recent-projects.json");
    if (fs.existsSync(recentsPath)) {
      return JSON.parse(fs.readFileSync(recentsPath, "utf-8"));
    }
    return [];
  });

  ipcMain.handle("add-recent-project", (_event, projectPath: string) => {
    const recentsPath = path.join(app.getPath("userData"), "recent-projects.json");
    let recents: string[] = [];
    if (fs.existsSync(recentsPath)) {
      recents = JSON.parse(fs.readFileSync(recentsPath, "utf-8"));
    }
    recents = [projectPath, ...recents.filter((p) => p !== projectPath)].slice(0, 10);
    fs.writeFileSync(recentsPath, JSON.stringify(recents, null, 2));
    return recents;
  });
}

// Sanitize a settings value before writing it into the .env file. Strips CR/LF
// (and anything after) so a value pasted into a free-text field like
// vlmBaseUrl can't inject additional env vars or corrupt the file.
function envValue(value: unknown, fallback: string | number = ""): string {
  const raw = value === undefined || value === null || value === "" ? fallback : value;
  return String(raw).split(/[\r\n]/)[0];
}

function writeEnvFile(settings: Record<string, unknown>): void {
  const envPath = isDev
    ? path.join(__dirname, "..", "..", ".env")
    : path.join(app.getPath("userData"), ".env");

  const lines: string[] = [
    "# Kinetograph Configuration (managed by the app)",
    "",
    "# ── AI / LLM Keys ──────────────────────────────────────",
    `GEMINI_API_KEY=${envValue(settings.geminiApiKey)}`,
    `HF_TOKEN=${envValue(settings.hfToken)}`,
    `ELEVENLABS_API_KEY=${envValue(settings.elevenlabsApiKey)}`,
    `NVIDIA_API_KEY=${envValue(settings.nvidiaApiKey)}`,
    "",
    "# ── Stock & Music ──────────────────────────────────────",
    `PEXELS_API_KEY=${envValue(settings.pexelsApiKey)}`,
    `SOUNDSTRIPE_API_KEY=${envValue(settings.soundstripeApiKey)}`,
    "",
    "# ── Model Configuration ───────────────────────────────",
    `GEMINI_MODEL=${envValue(settings.geminiModel, "gemini-2.5-flash-preview-05-20")}`,
    `VLM_MODEL=${envValue(settings.vlmModel, "nvidia/nemotron-nano-12b-v2-vl")}`,
    `VLM_BASE_URL=${envValue(settings.vlmBaseUrl, "https://integrate.api.nvidia.com")}`,
    "",
    "# ── Media Settings ─────────────────────────────────────",
    `OUTPUT_WIDTH=${envValue(settings.outputWidth, 1080)}`,
    `OUTPUT_HEIGHT=${envValue(settings.outputHeight, 1920)}`,
    `OUTPUT_FPS=${envValue(settings.outputFps, 30)}`,
    "",
    "# ── Server ─────────────────────────────────────────────",
    "API_HOST=127.0.0.1",
    `API_PORT=${BACKEND_PORT}`,
  ];

  fs.writeFileSync(envPath, lines.join("\n") + "\n");
}

// ─── App Lifecycle ──────────────────────────────────────────────────────────────

/**
 * Ensure the .env file reflects the latest saved settings.
 * Called on startup so that settings from previous sessions are
 * honoured without requiring an explicit "Save & Restart Engine".
 */
function ensureEnvFromSettings(): void {
  const settingsPath = path.join(app.getPath("userData"), "settings.json");
  if (!fs.existsSync(settingsPath)) return;
  try {
    const settings = JSON.parse(fs.readFileSync(settingsPath, "utf-8"));
    if (settings && typeof settings === "object" && Object.keys(settings).length > 0) {
      writeEnvFile(settings);
      console.log("[Kinetograph] .env synced from saved settings");
    }
  } catch (err) {
    console.warn("[Kinetograph] Could not read settings.json:", err);
  }
}

app.whenReady().then(async () => {
  setupIPC();
  createMenu();
  createWindow();

  // Sync saved settings → .env before starting backend
  ensureEnvFromSettings();

  // Start backend sidecar (skip if already running, e.g. from dev.sh)
  const alreadyRunning = await isBackendAlreadyRunning();
  if (alreadyRunning) {
    console.log("[Kinetograph] Backend already running — skipping sidecar spawn");
  } else {
    startBackend();
  }

  const backendReady = await waitForBackend();
  if (backendReady) {
    console.log("[Kinetograph] Backend is ready!");
    backendStatus = "running";
    mainWindow?.webContents.send("backend-status", "running");
  } else {
    console.error("[Kinetograph] Backend failed to start");
    backendStatus = "error";
    mainWindow?.webContents.send("backend-status", "error");
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  stopBackend();
});

// Handle second instance (single-instance lock)
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}
