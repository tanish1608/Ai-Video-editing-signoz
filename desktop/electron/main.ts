import { app, BrowserWindow, ipcMain, dialog, shell, Menu, session } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import http from "node:http";
import { randomBytes } from "node:crypto";

// ─── Constants ──────────────────────────────────────────────────────────────────

const BACKEND_PORT = 8080;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const HEALTH_ENDPOINT = `${BACKEND_URL}/api/health`;
const HEALTH_POLL_MS = 500;
const HEALTH_TIMEOUT_MS = 30_000;

const isDev = !app.isPackaged;
const externalBackend = isDev && process.env.KINETOGRAPH_EXTERNAL_BACKEND === "1";
const backendToken = externalBackend ? (process.env.KINETOGRAPH_API_TOKEN ?? "") : randomBytes(32).toString("hex");
const backendHeaders = { "X-Kinetograph-Token": backendToken };

function setBackendStatus(status: typeof backendStatus): void {
  backendStatus = status;
  mainWindow?.webContents.send("backend-status", status);
}

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
    const venvPython = path.join(__dirname, "..", "..", ".venv", ...(process.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python3"]));
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
    const req = http.get(HEALTH_ENDPOINT, { headers: backendHeaders }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => { req.destroy(); resolve(false); });
  });
}

function startBackend(): void {
  if (pythonProcess || externalBackend) return;
  setBackendStatus("starting");

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
  const cwd = isDev ? path.join(__dirname, "..", "..") : app.getPath("userData");
  const envPath = isDev ? path.join(__dirname, "..", "..", ".env") : path.join(app.getPath("userData"), ".env");

  console.log(`[Kinetograph] Starting backend: ${cmd} ${args.join(" ")}`);
  console.log(`[Kinetograph] CWD: ${cwd}`);

  const env: Record<string, string> = {
    ...process.env as Record<string, string>,
    PYTHONDONTWRITEBYTECODE: "1",
    KINETOGRAPH_ENV_FILE: envPath,
    KINETOGRAPH_API_TOKEN: backendToken,
    API_HOST: "127.0.0.1",
    API_PORT: String(BACKEND_PORT),
    // GUI launches on macOS do not inherit the user's shell PATH.
    PATH: [process.env.PATH ?? "", "/opt/homebrew/bin", "/usr/local/bin"].join(path.delimiter),
  };

  // If a project dir is set, pass it to the backend
  if (currentProjectDir || !isDev) {
    env.KINETOGRAPH_PROJECT_DIR = currentProjectDir ?? path.join(app.getPath("userData"), "workspace");
    fs.mkdirSync(env.KINETOGRAPH_PROJECT_DIR, { recursive: true });
  }

  const child = spawn(cmd, args, {
    cwd,
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });

  pythonProcess = child;
  child.stdout?.on("data", (data: Buffer) => {
    const msg = data.toString().trim();
    if (msg) console.log(`[Backend] ${msg}`);
    mainWindow?.webContents.send("backend-log", msg);
  });

  child.stderr?.on("data", (data: Buffer) => {
    const msg = data.toString().trim();
    if (msg) console.error(`[Backend] ${msg}`);
    mainWindow?.webContents.send("backend-log", msg);
  });

  child.on("exit", (code, signal) => {
    console.log(`[Kinetograph] Backend exited: code=${code}, signal=${signal}`);
    if (pythonProcess === child) {
      pythonProcess = null;
      setBackendStatus(code ? "error" : "stopped");
    }
  });

  child.on("error", (err) => {
    console.error(`[Kinetograph] Failed to start backend:`, err);
    if (pythonProcess === child) {
      pythonProcess = null;
      setBackendStatus("error");
    }
  });
}

async function stopBackend(): Promise<void> {
  const child = pythonProcess;
  if (!child) return;
  await new Promise<void>((resolve) => {
    const forceKill = setTimeout(() => child.kill("SIGKILL"), 5000);
    child.once("exit", () => { clearTimeout(forceKill); resolve(); });
    child.kill("SIGTERM");
  });
}

async function waitForBackend(): Promise<boolean> {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await isBackendAlreadyRunning()) {
      setBackendStatus("running");
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, HEALTH_POLL_MS));
  }
  setBackendStatus("error");
  return false;
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
      sandbox: true,
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

  mainWindow.webContents.on("will-navigate", (event) => event.preventDefault());

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
          click: () => shell.openExternal("https://github.com/tanish1608/Ai-Video-editing-signoz"),
        },
        {
          label: "Report Issue",
          click: () => shell.openExternal("https://github.com/tanish1608/Ai-Video-editing-signoz/issues"),
        },
      ],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ─── IPC Handlers ───────────────────────────────────────────────────────────────

/** Tell the running backend to switch all paths to a project directory. */
let switchingProject = false;

async function openProjectDirectory(projectDir: string): Promise<string | null> {
  if (switchingProject) return null;
  switchingProject = true;
  try {
    ensureProjectStructure(projectDir);
    await new Promise<void>((resolve, reject) => {
      const postData = JSON.stringify({ project_dir: projectDir });
      const req = http.request(`${BACKEND_URL}/api/project/set-dir`, {
        method: "POST",
        headers: { ...backendHeaders, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(postData) },
      }, (res) => {
        let body = "";
        res.on("data", (chunk) => { body += chunk; });
        res.on("end", () => {
          if (res.statusCode === 200) resolve();
          else {
            let detail = `Engine returned HTTP ${res.statusCode}`;
            try { detail = JSON.parse(body).detail ?? detail; } catch { /* non-JSON error */ }
            reject(new Error(detail));
          }
        });
      });
      req.on("error", reject);
      req.setTimeout(10_000, () => req.destroy(new Error("Project switch timed out")));
      req.end(postData);
    });
    currentProjectDir = projectDir;
    mainWindow?.webContents.send("project-opened", projectDir);
    return projectDir;
  } catch (error) {
    await dialog.showMessageBox(mainWindow!, {
      type: "error", message: "Could not open project", detail: String(error),
    });
    return null;
  } finally {
    switchingProject = false;
  }
}

async function handleOpenProject(): Promise<string | null> {
  const result = await dialog.showOpenDialog(mainWindow!, {
    title: "Open Project",
    properties: ["openDirectory", "createDirectory"],
    buttonLabel: "Open Project",
  });

  if (result.canceled || result.filePaths.length === 0) return null;

  return openProjectDirectory(result.filePaths[0]);
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

  return openProjectDirectory(projectDir);
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
  ipcMain.handle("open-recent-project", (_event, projectDir: unknown) => {
    if (typeof projectDir !== "string" || !path.isAbsolute(projectDir) || !fs.existsSync(projectDir)) {
      throw new Error("Project directory does not exist");
    }
    return openProjectDirectory(projectDir);
  });
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
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2), { mode: 0o600 });

    // Also write API keys to .env format for the Python backend
    writeEnvFile(settings);
  });

  ipcMain.handle("restart-backend", async () => {
    if (externalBackend) {
      await dialog.showMessageBox(mainWindow!, {
        type: "info", message: "Restart the external engine",
        detail: "The engine was started by dev.sh. Restart dev.sh to reload saved API settings.",
      });
      return false;
    }
    await stopBackend();
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

// Parse KEY=VALUE lines of an existing env file (comments/blank lines ignored).
function readEnvFile(envPath: string): Map<string, string> {
  const values = new Map<string, string>();
  if (!fs.existsSync(envPath)) return values;
  for (const line of fs.readFileSync(envPath, "utf-8").split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$/);
    if (match) values.set(match[1], match[2]);
  }
  return values;
}

function writeEnvFile(settings: Record<string, unknown>): void {
  const envPath = isDev
    ? path.join(__dirname, "..", "..", ".env")
    : path.join(app.getPath("userData"), ".env");

  // Never let an empty Settings field erase a key already in the file (e.g. a
  // hand-edited repo .env in dev), and keep variables the app doesn't manage.
  const existing = readEnvFile(envPath);
  const secret = (name: string, value: unknown) =>
    `${name}=${envValue(value) || existing.get(name) || ""}`;

  const lines: string[] = [
    "# Kinetograph Configuration (managed by the app)",
    "",
    "# ── AI / LLM Keys ──────────────────────────────────────",
    secret("GEMINI_API_KEY", settings.geminiApiKey),
    secret("HF_TOKEN", settings.hfToken),
    secret("ELEVENLABS_API_KEY", settings.elevenlabsApiKey),
    secret("NVIDIA_API_KEY", settings.nvidiaApiKey),
    "",
    "# ── Stock & Music ──────────────────────────────────────",
    secret("PEXELS_API_KEY", settings.pexelsApiKey),
    secret("SOUNDSTRIPE_API_KEY", settings.soundstripeApiKey),
    "",
    "# ── Model Configuration ───────────────────────────────",
    `GEMINI_MODEL=${envValue(settings.geminiModel, "gemini-3.8-flash")}`,
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

  const managed = new Set(lines.map((line) => line.split("=")[0]));
  const preserved = [...existing].filter(([name]) => !managed.has(name));
  if (preserved.length > 0) {
    lines.push("", "# ── Other (preserved) ──────────────────────────────────");
    for (const [name, value] of preserved) lines.push(`${name}=${value}`);
  }

  fs.writeFileSync(envPath, lines.join("\n") + "\n", { mode: 0o600 });
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
  session.defaultSession.webRequest.onBeforeSendHeaders({
    urls: [`${BACKEND_URL}/*`, `ws://127.0.0.1:${BACKEND_PORT}/*`],
  }, (details, callback) => {
    callback({ requestHeaders: { ...details.requestHeaders, ...backendHeaders } });
  });
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  setupIPC();
  createMenu();
  createWindow();

  // Sync saved settings → .env before starting backend
  ensureEnvFromSettings();

  // Start backend sidecar (skip if already running, e.g. from dev.sh)
  const alreadyRunning = externalBackend && await isBackendAlreadyRunning();
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
