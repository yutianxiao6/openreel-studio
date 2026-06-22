#!/usr/bin/env node
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import process from "node:process";

const repo = "yutianxiao6/openreel-studio";
const latestReleaseApi = `https://api.github.com/repos/${repo}/releases/latest`;
const serverInstallUrl = `https://raw.githubusercontent.com/${repo}/main/scripts/install-server.sh`;
const userAgent = "openreel-studio-installer";

const args = process.argv.slice(2);

function usage() {
  console.log(`OpenReel Studio installer

Usage:
  openreel-studio [download] [--dir <path>] [--no-open] [--print-url]
  openreel-studio server

Commands:
  download    Download the latest desktop installer for this OS. This is the default.
  server      Bootstrap a Linux server deployment with Docker.

Options:
  --dir PATH  Download directory. Defaults to ~/Downloads.
  --no-open   Do not start/open the downloaded installer.
  --print-url Print the selected release asset URL without downloading.
  --help      Show this help.
  --version   Show CLI version.
`);
}

function readPackageVersion() {
  const packagePath = new URL("../package.json", import.meta.url);
  return JSON.parse(fs.readFileSync(packagePath, "utf8")).version;
}

function parseArgs(rawArgs) {
  const opts = {
    command: "download",
    dir: process.env.OPENREEL_DOWNLOAD_DIR || path.join(os.homedir(), "Downloads"),
    open: true,
    printUrl: false,
  };

  const rest = [...rawArgs];
  if (rest[0] === "download" || rest[0] === "desktop") {
    opts.command = "download";
    rest.shift();
  } else if (rest[0] === "server") {
    opts.command = "server";
    rest.shift();
  }

  for (let index = 0; index < rest.length; index += 1) {
    const arg = rest[index];
    if (arg === "--help" || arg === "-h") {
      opts.help = true;
    } else if (arg === "--version" || arg === "-v") {
      opts.version = true;
    } else if (arg === "--no-open") {
      opts.open = false;
    } else if (arg === "--print-url") {
      opts.printUrl = true;
    } else if (arg === "--dir") {
      index += 1;
      if (!rest[index]) throw new Error("--dir requires a path.");
      opts.dir = path.resolve(rest[index]);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return opts;
}

function requestJson(url) {
  return new Promise((resolve, reject) => {
    requestBuffer(url)
      .then((buffer) => resolve(JSON.parse(buffer.toString("utf8"))))
      .catch(reject);
  });
}

function requestBuffer(url, redirects = 0) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const transport = parsed.protocol === "http:" ? http : https;
    const req = transport.request(
      parsed,
      {
        headers: {
          "User-Agent": userAgent,
          Accept: "application/json, application/octet-stream",
        },
      },
      (res) => {
        const status = res.statusCode || 0;
        const location = res.headers.location;
        if (status >= 300 && status < 400 && location) {
          res.resume();
          if (redirects >= 10) {
            reject(new Error("Too many redirects."));
            return;
          }
          requestBuffer(new URL(location, parsed).toString(), redirects + 1).then(resolve, reject);
          return;
        }
        if (status < 200 || status >= 300) {
          res.resume();
          reject(new Error(`HTTP ${status} for ${url}`));
          return;
        }
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => resolve(Buffer.concat(chunks)));
      },
    );
    req.setTimeout(60_000, () => {
      req.destroy(new Error(`Request timed out: ${url}`));
    });
    req.on("error", reject);
    req.end();
  });
}

function downloadFile(url, target, redirects = 0) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const transport = parsed.protocol === "http:" ? http : https;
    const req = transport.request(
      parsed,
      {
        headers: {
          "User-Agent": userAgent,
          Accept: "application/octet-stream",
        },
      },
      (res) => {
        const status = res.statusCode || 0;
        const location = res.headers.location;
        if (status >= 300 && status < 400 && location) {
          res.resume();
          if (redirects >= 10) {
            reject(new Error("Too many redirects."));
            return;
          }
          downloadFile(new URL(location, parsed).toString(), target, redirects + 1).then(resolve, reject);
          return;
        }
        if (status < 200 || status >= 300) {
          res.resume();
          reject(new Error(`HTTP ${status} for ${url}`));
          return;
        }
        const file = fs.createWriteStream(target);
        res.pipe(file);
        file.on("finish", () => file.close(resolve));
        file.on("error", reject);
      },
    );
    req.setTimeout(300_000, () => {
      req.destroy(new Error(`Download timed out: ${url}`));
    });
    req.on("error", reject);
    req.end();
  });
}

function selectAsset(assets) {
  const platform = process.platform;
  const namesByPlatform = {
    win32: [/Setup.*\.exe$/i, /\.exe$/i],
    darwin: [/\.dmg$/i, /\.zip$/i],
    linux: [/\.AppImage$/i, /\.deb$/i],
  };
  const patterns = namesByPlatform[platform];
  if (!patterns) {
    throw new Error(`Unsupported OS: ${platform}. Use Windows, Linux, or macOS.`);
  }
  for (const pattern of patterns) {
    const asset = assets.find((item) => pattern.test(item.name || ""));
    if (asset?.browser_download_url) return asset;
  }
  throw new Error(`No desktop installer asset found for ${platform}.`);
}

function openInstaller(filePath) {
  if (process.platform === "win32") {
    spawn("cmd", ["/c", "start", "", filePath], { detached: true, stdio: "ignore" }).unref();
  } else if (process.platform === "darwin") {
    spawn("open", [filePath], { detached: true, stdio: "ignore" }).unref();
  } else {
    console.log(`Run it with: ${filePath}`);
  }
}

async function downloadDesktop(opts) {
  console.log("Fetching latest OpenReel Studio release...");
  const release = await requestJson(latestReleaseApi);
  const asset = selectAsset(release.assets || []);
  if (opts.printUrl) {
    console.log(asset.browser_download_url);
    return;
  }

  fs.mkdirSync(opts.dir, { recursive: true });
  const target = path.join(opts.dir, asset.name);
  console.log(`Downloading ${asset.name}...`);
  await downloadFile(asset.browser_download_url, target);
  if (/\.AppImage$/i.test(target)) {
    fs.chmodSync(target, 0o755);
  }
  console.log(`Downloaded: ${target}`);
  if (opts.open) openInstaller(target);
}

function runServerInstaller() {
  if (process.platform === "win32") {
    throw new Error("Server deployment is for Linux servers. Use the Windows desktop installer on Windows.");
  }
  const command = `curl -fsSL ${serverInstallUrl} | bash`;
  const child = spawn("bash", ["-lc", command], { stdio: "inherit" });
  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code ?? 1);
  });
}

async function main() {
  const opts = parseArgs(args);
  if (opts.help) {
    usage();
    return;
  }
  if (opts.version) {
    console.log(readPackageVersion());
    return;
  }
  if (opts.command === "server") {
    runServerInstaller();
    return;
  }
  await downloadDesktop(opts);
}

main().catch((error) => {
  console.error(`Error: ${error.message}`);
  process.exit(1);
});
