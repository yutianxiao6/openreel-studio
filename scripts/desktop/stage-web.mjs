import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const webDir = path.join(root, "apps", "web");
const standaloneDir = path.join(webDir, ".next", "standalone");
const staticDir = path.join(webDir, ".next", "static");
const publicDir = path.join(webDir, "public");
const targetDir = path.join(root, "apps", "desktop", "dist", "resources", "web");
const targetStaticDir = path.join(targetDir, "apps", "web", ".next", "static");
const targetPublicDir = path.join(targetDir, "apps", "web", "public");
const targetWebNodeModulesDir = path.join(targetDir, "apps", "web", "node_modules");
const targetPnpmDir = path.join(targetDir, "node_modules", ".pnpm");

function assertDir(dir, message) {
  if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) {
    throw new Error(message);
  }
}

assertDir(standaloneDir, "Next standalone output missing. Run `pnpm --filter web build` first.");
assertDir(staticDir, "Next static output missing. Run `pnpm --filter web build` first.");

fs.rmSync(targetDir, { recursive: true, force: true });
fs.mkdirSync(targetDir, { recursive: true });
fs.cpSync(standaloneDir, targetDir, { recursive: true });
fs.rmSync(targetStaticDir, { recursive: true, force: true });
fs.cpSync(staticDir, targetStaticDir, { recursive: true });

if (fs.existsSync(publicDir)) {
  fs.rmSync(targetPublicDir, { recursive: true, force: true });
  fs.cpSync(publicDir, targetPublicDir, { recursive: true });
}

function copyPackage(packageDir, scope, name) {
  const packageJson = path.join(packageDir, "package.json");
  if (!fs.existsSync(packageJson)) {
    return;
  }
  const destinationDir = scope
    ? path.join(targetWebNodeModulesDir, scope, name)
    : path.join(targetWebNodeModulesDir, name);
  if (fs.existsSync(destinationDir)) {
    return;
  }
  fs.mkdirSync(path.dirname(destinationDir), { recursive: true });
  fs.cpSync(packageDir, destinationDir, { recursive: true, dereference: true });
}

function flattenPnpmRuntimePackages() {
  if (!fs.existsSync(targetPnpmDir)) {
    return;
  }
  for (const entry of fs.readdirSync(targetPnpmDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name === "node_modules") {
      continue;
    }
    const packageNodeModulesDir = path.join(targetPnpmDir, entry.name, "node_modules");
    if (!fs.existsSync(packageNodeModulesDir)) {
      continue;
    }
    for (const packageEntry of fs.readdirSync(packageNodeModulesDir, { withFileTypes: true })) {
      const packagePath = path.join(packageNodeModulesDir, packageEntry.name);
      if (packageEntry.name.startsWith("@")) {
        for (const scopedEntry of fs.readdirSync(packagePath, { withFileTypes: true })) {
          if (scopedEntry.isDirectory() || scopedEntry.isSymbolicLink()) {
            copyPackage(path.join(packagePath, scopedEntry.name), packageEntry.name, scopedEntry.name);
          }
        }
      } else if (packageEntry.isDirectory() || packageEntry.isSymbolicLink()) {
        copyPackage(packagePath, null, packageEntry.name);
      }
    }
  }
}

flattenPnpmRuntimePackages();

console.log(`Staged Next standalone runtime at ${path.relative(root, targetDir)}`);
