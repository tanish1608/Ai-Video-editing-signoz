const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const venv = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python3");
const python = process.env.KINETOGRAPH_PYTHON || (fs.existsSync(venv) ? venv : (process.platform === "win32" ? "python" : "python3"));
const result = spawnSync(python, [path.join(root, "backend/scripts/build_sidecar.py")], { stdio: "inherit" });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
