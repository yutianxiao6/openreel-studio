const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  ipcMain,
  nativeImage,
  net: electronNet,
} = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { Readable } = require("node:stream");
const { pipeline } = require("node:stream/promises");
const { migrateAppDataBackToInstall } = require("./runtime_data.cjs");

const isWindows = process.platform === "win32";
const isPackaged = app.isPackaged;
const processes = [];

let mainWindow = null;
let tray = null;
let isQuitting = false;
let startupDirs = null;

function fixedUserDataDir() {
  if (process.platform === "win32" && process.env.APPDATA) {
    return path.join(process.env.APPDATA, "OpenReel Studio");
  }
  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Application Support", "OpenReel Studio");
  }
  return path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "OpenReel Studio");
}

function packagedInstallRoot() {
  if (!isPackaged) {
    return null;
  }
  if (process.platform === "darwin") {
    // .../OpenReel Studio.app/Contents/MacOS/OpenReel Studio -> parent of .app
    return path.dirname(path.dirname(path.dirname(path.dirname(process.execPath))));
  }
  if (process.platform === "linux" && process.env.APPIMAGE) {
    return path.dirname(process.env.APPIMAGE);
  }
  return path.dirname(process.execPath);
}

function desktopDataRoot() {
  if (process.env.OPENREEL_DATA_DIR) {
    return path.resolve(process.env.OPENREEL_DATA_DIR);
  }
  const installRoot = packagedInstallRoot();
  if (installRoot) {
    return installRoot;
  }
  return fixedUserDataDir();
}

function mkdirp(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function safeSuggestedFilename(value) {
  const clean = path.basename(String(value || "openreel-media"))
    .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "-")
    .replace(/\s+/g, " ")
    .trim();
  return clean.slice(0, 180) || "openreel-media";
}

function mediaDownloadPreferencesPath() {
  return path.join(fixedUserDataDir(), "media-download.json");
}

function readMediaDownloadDirectory() {
  try {
    const preferences = JSON.parse(fs.readFileSync(mediaDownloadPreferencesPath(), "utf8"));
    const directory = typeof preferences?.directory === "string" ? preferences.directory : "";
    return directory && fs.existsSync(directory) && fs.statSync(directory).isDirectory()
      ? directory
      : "";
  } catch {
    return "";
  }
}

function rememberMediaDownloadDirectory(directory) {
  const preferencesPath = mediaDownloadPreferencesPath();
  mkdirp(path.dirname(preferencesPath));
  fs.writeFileSync(
    preferencesPath,
    `${JSON.stringify({ directory }, null, 2)}\n`,
    "utf8",
  );
}

async function chooseMediaDownloadDirectory(owner) {
  const options = {
    title: "选择媒体下载文件夹",
    defaultPath: readMediaDownloadDirectory() || app.getPath("downloads"),
    properties: ["openDirectory", "createDirectory"],
  };
  const selected = owner
    ? await dialog.showOpenDialog(owner, options)
    : await dialog.showOpenDialog(options);
  if (selected.canceled || !selected.filePaths[0]) {
    return "";
  }
  const directory = selected.filePaths[0];
  rememberMediaDownloadDirectory(directory);
  return directory;
}

const activeMediaDownloadTargets = new Set();

function uniqueMediaDownloadPath(directory, filename) {
  const parsed = path.parse(safeSuggestedFilename(filename));
  const stem = parsed.name || "openreel-media";
  const extension = parsed.ext;
  for (let index = 0; index < 10000; index += 1) {
    const candidateName = index === 0
      ? `${stem}${extension}`
      : `${stem} (${index})${extension}`;
    const candidate = path.join(directory, candidateName);
    if (!fs.existsSync(candidate) && !activeMediaDownloadTargets.has(candidate)) {
      return candidate;
    }
  }
  return path.join(directory, `${stem}-${Date.now()}${extension}`);
}

ipcMain.handle("openreel:get-media-download-directory", () => ({
  ok: true,
  directory: readMediaDownloadDirectory(),
}));

ipcMain.handle("openreel:choose-media-download-directory", async (event) => {
  const owner = BrowserWindow.fromWebContents(event.sender) || mainWindow || undefined;
  try {
    const directory = await chooseMediaDownloadDirectory(owner);
    return { ok: true, canceled: !directory, directory };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, canceled: false, directory: "", error: message };
  }
});

function decodeDataUrl(value) {
  const match = /^data:([^;,]*)(;base64)?,(.*)$/s.exec(value);
  if (!match) {
    throw new Error("Unsupported data URL");
  }
  return match[2]
    ? Buffer.from(match[3], "base64")
    : Buffer.from(decodeURIComponent(match[3]), "utf8");
}

async function writeMediaUrlToFile(url, targetPath) {
  const parsed = new URL(url);
  const tempPath = `${targetPath}.openreel-${process.pid}-${Date.now()}.part`;
  try {
    if (parsed.protocol === "data:") {
      await fs.promises.writeFile(tempPath, decodeDataUrl(url));
    } else {
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        throw new Error(`Unsupported media URL protocol: ${parsed.protocol}`);
      }
      const response = await electronNet.fetch(url, { redirect: "follow" });
      if (!response.ok) {
        throw new Error(`Media download failed: HTTP ${response.status}`);
      }
      if (!response.body) {
        throw new Error("Media download returned an empty response");
      }
      await pipeline(
        Readable.fromWeb(response.body),
        fs.createWriteStream(tempPath, { flags: "wx" }),
      );
    }
    await fs.promises.copyFile(tempPath, targetPath);
  } finally {
    await fs.promises.rm(tempPath, { force: true }).catch(() => {});
  }
}

ipcMain.handle("openreel:save-media", async (event, request) => {
  const url = typeof request?.url === "string" ? request.url.trim() : "";
  if (!url) {
    return { ok: false, error: "没有可保存的媒体地址" };
  }
  const suggestedName = safeSuggestedFilename(request?.suggestedName);
  const owner = BrowserWindow.fromWebContents(event.sender) || mainWindow || undefined;
  let directory = readMediaDownloadDirectory();
  if (!directory) {
    directory = await chooseMediaDownloadDirectory(owner);
    if (!directory) {
      return { ok: true, canceled: true };
    }
  }
  const targetPath = uniqueMediaDownloadPath(directory, suggestedName);
  activeMediaDownloadTargets.add(targetPath);
  try {
    await writeMediaUrlToFile(url, targetPath);
    logDesktop(`saved media url=${new URL(url).protocol} target=${targetPath}`);
    return { ok: true, canceled: false, path: targetPath };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    logDesktop(`failed to save media: ${message}`);
    return { ok: false, canceled: false, error: message };
  } finally {
    activeMediaDownloadTargets.delete(targetPath);
  }
});

function copyMissingDirectoryEntries(sourceRoot, targetRoot) {
  if (!sourceRoot || !fs.existsSync(sourceRoot)) {
    return;
  }
  mkdirp(targetRoot);
  for (const entry of fs.readdirSync(sourceRoot, { withFileTypes: true })) {
    const source = path.join(sourceRoot, entry.name);
    const target = path.join(targetRoot, entry.name);
    if (entry.isDirectory()) {
      if (fs.existsSync(target) && !fs.statSync(target).isDirectory()) {
        continue;
      }
      copyMissingDirectoryEntries(source, target);
      continue;
    }
    if (fs.existsSync(target)) {
      continue;
    }
    fs.copyFileSync(source, target);
  }
}

function repoRoot() {
  return path.resolve(__dirname, "..", "..", "..");
}

function desktopSkillsRoot(root) {
  if (process.env.OPENREEL_SKILLS_DIR) {
    return path.resolve(process.env.OPENREEL_SKILLS_DIR);
  }
  if (!isPackaged) {
    return path.join(repoRoot(), "skills");
  }
  return path.join(root, "skills");
}

function bundledDefaultRoot(name) {
  if (!isPackaged) {
    return null;
  }
  const target = path.join(process.resourcesPath, "defaults", name);
  return fs.existsSync(target) ? target : null;
}

function writeLogStream(logDir, name) {
  mkdirp(logDir);
  return fs.createWriteStream(path.join(logDir, `${name}.log`), { flags: "a" });
}

function appendLog(stream, chunk) {
  stream.write(chunk);
}

function writeLogLine(logDir, name, line) {
  const stream = writeLogStream(logDir, name);
  stream.write(`[${new Date().toISOString()}] ${line}\n`);
  stream.end();
}

function writeStartupLog(line) {
  const targets = [
    path.join(desktopDataRoot(), "logs"),
    path.join(os.tmpdir(), "OpenReel Studio", "logs"),
  ].filter(Boolean);

  for (const logDir of targets) {
    try {
      writeLogLine(logDir, "startup", line);
    } catch {
      // Startup logging is best effort because it runs before Electron is fully ready.
    }
  }
}

function logDesktop(line) {
  writeStartupLog(line);
  if (!startupDirs) {
    return;
  }
  writeLogLine(startupDirs.logs, "desktop", line);
}

process.on("uncaughtException", (error) => {
  writeStartupLog(`uncaughtException: ${error.stack || error.message || String(error)}`);
});

process.on("unhandledRejection", (reason) => {
  const message = reason instanceof Error ? reason.stack || reason.message : String(reason);
  writeStartupLog(`unhandledRejection: ${message}`);
});

function findPort(start) {
  return new Promise((resolve, reject) => {
    const tryPort = (port) => {
      const server = net.createServer();
      server.once("error", () => tryPort(port + 1));
      server.once("listening", () => {
        server.close(() => resolve(port));
      });
      server.listen(port, "127.0.0.1");
    };
    try {
      tryPort(start);
    } catch (error) {
      reject(error);
    }
  });
}

function probeHttp(url, { timeoutMs = 2000 } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode || 0);
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error("timeout"));
    });
    req.on("error", reject);
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForHttp(
  url,
  {
    timeoutMs = 1200000,
    intervalMs = 700,
    acceptStatus = (statusCode) => statusCode >= 200 && statusCode < 400,
  } = {},
) {
  const startedAt = Date.now();
  let lastError = null;
  while (Date.now() - startedAt <= timeoutMs) {
    try {
      const statusCode = await probeHttp(url);
      if (acceptStatus(statusCode)) {
        return statusCode;
      }
      lastError = new Error(`HTTP ${statusCode}`);
    } catch (error) {
      lastError = error;
    }
    await sleep(intervalMs);
  }
  throw new Error(
    `Timed out waiting for ${url}${lastError ? ` (${lastError.message})` : ""}`,
  );
}

async function waitForWebApp(webBase, { timeoutMs = 1200000, intervalMs = 700 } = {}) {
  const candidates = [`${webBase}/`, `${webBase}/studio`];
  const startedAt = Date.now();
  let lastError = null;
  while (Date.now() - startedAt <= timeoutMs) {
    for (const url of candidates) {
      try {
        const statusCode = await probeHttp(url);
        if (statusCode >= 200 && statusCode < 400) {
          return url;
        }
        lastError = new Error(`${url} returned HTTP ${statusCode}`);
      } catch (error) {
        lastError = error;
      }
    }
    await sleep(intervalMs);
  }
  throw new Error(
    `Timed out waiting for web app at ${candidates.join(" or ")}${
      lastError ? ` (${lastError.message})` : ""
    }`,
  );
}

function spawnLogged(command, args, options, logDir, name) {
  const out = writeLogStream(logDir, name);
  out.write(`\n[${new Date().toISOString()}] ${command} ${args.join(" ")}\n`);
  const child = spawn(command, args, {
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    ...options,
  });
  child.stdout.on("data", (chunk) => appendLog(out, chunk));
  child.stderr.on("data", (chunk) => appendLog(out, chunk));
  child.on("exit", (code, signal) => {
    out.write(`[${new Date().toISOString()}] exited code=${code} signal=${signal}\n`);
    out.end();
  });
  processes.push(child);
  return child;
}

function executableName(base) {
  return isWindows ? `${base}.exe` : base;
}

function packagedApiExecutable() {
  const exe = path.join(
    process.resourcesPath,
    "api",
    "openreel-api",
    executableName("openreel-api"),
  );
  if (!fs.existsSync(exe)) {
    throw new Error(`Packaged API executable not found: ${exe}`);
  }
  return exe;
}

function packagedWebServer() {
  const server = path.join(process.resourcesPath, "web", "apps", "web", "server.js");
  if (!fs.existsSync(server)) {
    throw new Error(`Packaged Next server not found: ${server}`);
  }
  return server;
}

function appIconPath() {
  const iconName = isWindows ? "icon.ico" : "icon.png";
  const icon = path.join(__dirname, "..", "build", iconName);
  return fs.existsSync(icon) ? icon : undefined;
}

function trayIcon() {
  const icon = appIconPath();
  if (icon) {
    const image = nativeImage.createFromPath(icon);
    if (!image.isEmpty()) {
      return image;
    }
  }
  return nativeImage.createFromDataURL(
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAI0lEQVR4AWMY+f//PwMlgImBQjDqgFEHjDpg1AGjDgAABYcEAvJAAE4AAAAASUVORK5CYII=",
  );
}

function desktopDirs() {
  const root = desktopDataRoot();
  if (isPackaged && !process.env.OPENREEL_DATA_DIR) {
    const migration = migrateAppDataBackToInstall(fixedUserDataDir(), root);
    if (migration.status === "completed") {
      writeStartupLog(
        `returned AppData runtime files to install root; copied=${migration.stats.copied} ` +
        `replaced=${migration.stats.replaced} kept=${migration.stats.kept} ` +
        `backup=${migration.recovery_root || "none"}`,
      );
    }
  }
  const skills = desktopSkillsRoot(root);
  const dirs = {
    root,
    userData: path.join(root, "data", "electron"),
    data: path.join(root, "data"),
    storage: path.join(root, "storage"),
    assets: path.join(root, "assets"),
    config: path.join(root, "config"),
    plugins: path.join(root, "plugins"),
    workflowTemplates: path.join(root, "workflow_templates"),
    logs: path.join(root, "logs"),
    skills,
    skillWorkflows: path.join(skills, "workflows"),
    skillPrompts: path.join(skills, "prompts"),
    skillReview: path.join(skills, "review"),
  };
  Object.values(dirs).forEach(mkdirp);
  copyMissingDirectoryEntries(bundledDefaultRoot("config"), dirs.config);
  copyMissingDirectoryEntries(bundledDefaultRoot("plugins"), dirs.plugins);
  copyMissingDirectoryEntries(bundledDefaultRoot("workflow_templates"), dirs.workflowTemplates);
  app.setPath("userData", dirs.userData);
  return dirs;
}

function buildRuntimeEnv({ apiPort, webPort, dirs }) {
  const apiBase = `http://127.0.0.1:${apiPort}`;
  const webBase = `http://127.0.0.1:${webPort}`;
  return {
    ...process.env,
    APP_ENV: "desktop",
    APP_HOST: "127.0.0.1",
    APP_PORT: String(apiPort),
    WEB_PORT: String(webPort),
    PROJECT_ROOT: dirs.root,
    DATABASE_URL: `sqlite+aiosqlite:///${path.join(dirs.data, "app.db").replace(/\\/g, "/")}`,
    STORAGE_PATH: dirs.storage,
    STORAGE_DIR: dirs.storage,
    CORS_ORIGINS: `${webBase},${apiBase}`,
    OPENREEL_DESKTOP: "1",
    OPENREEL_USER_DATA_DIR: dirs.root,
    OPENREEL_SKILLS_DIR: dirs.skills,
  };
}

function startApi({ apiPort, webPort, dirs }) {
  const env = buildRuntimeEnv({ apiPort, webPort, dirs });
  if (isPackaged) {
    return spawnLogged(packagedApiExecutable(), [], { cwd: dirs.root, env }, dirs.logs, "api");
  }
  const apiDir = path.resolve(__dirname, "..", "..", "api");
  return spawnLogged(
    "uv",
    ["run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(apiPort)],
    { cwd: apiDir, env },
    dirs.logs,
    "api",
  );
}

function startWeb({ apiPort, webPort, dirs }) {
  const apiBase = `http://127.0.0.1:${apiPort}`;
  const env = {
    ...process.env,
    NODE_ENV: "production",
    HOSTNAME: "127.0.0.1",
    PORT: String(webPort),
    NEXT_PUBLIC_API_BASE_URL: apiBase,
    INTERNAL_API_BASE_URL: apiBase,
    OPENREEL_DESKTOP: "1",
  };
  if (isPackaged) {
    const server = packagedWebServer();
    Object.assign(process.env, env);
    writeLogLine(dirs.logs, "web", `starting in-process Next server: ${server}`);
    try {
      require(server);
    } catch (error) {
      writeLogLine(dirs.logs, "web", error.stack || error.message || String(error));
      throw error;
    }
    return null;
  }
  const repoRoot = path.resolve(__dirname, "..", "..", "..");
  return spawnLogged(
    "pnpm",
    ["--filter", "web", "exec", "next", "dev", "-p", String(webPort), "-H", "127.0.0.1"],
    { cwd: repoRoot, env: { ...env, NODE_ENV: "development" } },
    dirs.logs,
    "web",
  );
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  if (tray) {
    return;
  }
  tray = new Tray(trayIcon());
  tray.setToolTip("OpenReel Studio");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "打开 OpenReel Studio", click: showMainWindow },
      { type: "separator" },
      {
        label: "退出",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on("click", showMainWindow);
}

function createWindow({ apiPort, webPort }) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    return mainWindow;
  }
  const apiBase = `http://127.0.0.1:${apiPort}`;
  const webBase = `http://127.0.0.1:${webPort}`;
  const icon = appIconPath();
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1120,
    minHeight: 720,
    show: false,
    autoHideMenuBar: true,
    ...(icon ? { icon } : {}),
    backgroundColor: "#f8fafc",
    title: "OpenReel Studio",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      additionalArguments: [
        `--openreel-api-base=${apiBase}`,
        `--openreel-web-base=${webBase}`,
      ],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.setMenuBarVisibility(false);
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown" || input.isAutoRepeat) {
      return;
    }
    const key = String(input.key || "").toLowerCase();
    const isReloadShortcut = key === "f5" || ((input.control || input.meta) && !input.alt && key === "r");
    if (!isReloadShortcut) {
      return;
    }
    event.preventDefault();
    mainWindow.webContents.reload();
  });
  mainWindow.on("close", (event) => {
    if (isQuitting) {
      return;
    }
    event.preventDefault();
    mainWindow.hide();
  });
  return mainWindow;
}

async function openAppWindow({ appUrl, apiPort, webPort }) {
  const win = createWindow({ apiPort, webPort });
  await win.loadURL(appUrl);
  showMainWindow();
}

function logStartupError(error) {
  logDesktop(error.stack || error.message || String(error));
}

async function boot() {
  writeStartupLog("Electron main process entered boot().");
  const dirs = desktopDirs();
  startupDirs = dirs;
  createTray();

  const apiPort = await findPort(7860);
  const webPort = await findPort(apiPort + 1);
  logDesktop(`starting desktop runtime apiPort=${apiPort} webPort=${webPort}`);

  try {
    startApi({ apiPort, webPort, dirs });
    await waitForHttp(`http://127.0.0.1:${apiPort}/api/health`);
    startWeb({ apiPort, webPort, dirs });
    const appUrl = await waitForWebApp(`http://127.0.0.1:${webPort}`);
    await openAppWindow({ appUrl, apiPort, webPort });
  } catch (error) {
    logStartupError(error);
  }
}

function stopChildren() {
  for (const child of processes.splice(0)) {
    if (!child.killed) {
      child.kill();
    }
  }
}

app.whenReady().then(() => {
  app.setName("OpenReel Studio");
  Menu.setApplicationMenu(null);
  writeStartupLog("Electron app ready.");
  boot().catch(logStartupError);
});

app.on("before-quit", () => {
  isQuitting = true;
  stopChildren();
});
app.on("window-all-closed", () => {});
app.on("activate", showMainWindow);
