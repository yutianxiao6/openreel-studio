const fs = require("node:fs");
const path = require("node:path");

const RUNTIME_DATA_DIR_NAMES = Object.freeze([
  "data",
  "storage",
  "assets",
  "config",
  "logs",
  "plugins",
  "skills",
  "workflow_templates",
]);

const APPDATA_RETURN_MARKER = ".appdata-return-migration-v1.json";
const APPDATA_RETURN_RECOVERY_DIR = path.join(
  ".openreel-data-recovery",
  "appdata-return-v1",
);

function mkdirp(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function copyFilePreservingTimes(source, target, sourceStat = fs.statSync(source)) {
  mkdirp(path.dirname(target));
  fs.copyFileSync(source, target);
  try {
    fs.chmodSync(target, sourceStat.mode);
  } catch {
    // Windows may reject POSIX mode updates; copied content is still valid.
  }
  fs.utimesSync(target, sourceStat.atime, sourceStat.mtime);
}

function backupTargetFile(target, relativePath, recoveryRoot, stats) {
  const backup = path.join(recoveryRoot, relativePath);
  if (!fs.existsSync(backup)) {
    copyFilePreservingTimes(target, backup);
    stats.backed_up += 1;
  }
}

function mergeDirectoryPreferNewer(sourceRoot, targetRoot, recoveryRoot, stats, relativeRoot) {
  if (!fs.existsSync(sourceRoot) || !fs.statSync(sourceRoot).isDirectory()) {
    return;
  }
  mkdirp(targetRoot);
  for (const entry of fs.readdirSync(sourceRoot, { withFileTypes: true })) {
    const source = path.join(sourceRoot, entry.name);
    const target = path.join(targetRoot, entry.name);
    const relativePath = path.join(relativeRoot, entry.name);

    if (entry.isSymbolicLink()) {
      stats.skipped += 1;
      continue;
    }
    if (entry.isDirectory()) {
      if (fs.existsSync(target) && !fs.statSync(target).isDirectory()) {
        stats.skipped += 1;
        continue;
      }
      mergeDirectoryPreferNewer(source, target, recoveryRoot, stats, relativePath);
      continue;
    }
    if (!entry.isFile()) {
      stats.skipped += 1;
      continue;
    }

    const sourceStat = fs.statSync(source);
    if (!fs.existsSync(target)) {
      copyFilePreservingTimes(source, target, sourceStat);
      stats.copied += 1;
      continue;
    }

    const targetStat = fs.statSync(target);
    if (!targetStat.isFile()) {
      stats.skipped += 1;
      continue;
    }
    if (sourceStat.mtimeMs <= targetStat.mtimeMs) {
      stats.kept += 1;
      continue;
    }

    backupTargetFile(target, relativePath, recoveryRoot, stats);
    copyFilePreservingTimes(source, target, sourceStat);
    stats.replaced += 1;
  }
}

function migrateAppDataBackToInstall(sourceRoot, targetRoot) {
  const source = path.resolve(sourceRoot);
  const target = path.resolve(targetRoot);
  if (source === target) {
    return { status: "same_root" };
  }

  const marker = path.join(target, APPDATA_RETURN_MARKER);
  if (fs.existsSync(marker)) {
    return { status: "already_complete", marker };
  }

  const availableDirs = RUNTIME_DATA_DIR_NAMES.filter((name) => {
    const candidate = path.join(source, name);
    return fs.existsSync(candidate) && fs.statSync(candidate).isDirectory();
  });
  if (availableDirs.length === 0) {
    return { status: "no_source_data" };
  }

  mkdirp(target);
  const recoveryRoot = path.join(target, APPDATA_RETURN_RECOVERY_DIR);
  const stats = {
    copied: 0,
    replaced: 0,
    kept: 0,
    backed_up: 0,
    skipped: 0,
  };
  for (const name of availableDirs) {
    mergeDirectoryPreferNewer(
      path.join(source, name),
      path.join(target, name),
      recoveryRoot,
      stats,
      name,
    );
  }

  const payload = {
    schema_version: "openreel.desktop_appdata_return.v1",
    source,
    target,
    recovery_root: stats.backed_up > 0 ? recoveryRoot : null,
    source_directories: availableDirs,
    stats,
    completed_at: new Date().toISOString(),
  };
  const temporaryMarker = `${marker}.tmp`;
  fs.writeFileSync(temporaryMarker, JSON.stringify(payload, null, 2));
  fs.renameSync(temporaryMarker, marker);
  return { status: "completed", marker, ...payload };
}

module.exports = {
  APPDATA_RETURN_MARKER,
  APPDATA_RETURN_RECOVERY_DIR,
  RUNTIME_DATA_DIR_NAMES,
  migrateAppDataBackToInstall,
};
