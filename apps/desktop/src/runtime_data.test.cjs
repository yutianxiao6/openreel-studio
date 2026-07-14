const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  APPDATA_RETURN_MARKER,
  APPDATA_RETURN_RECOVERY_DIR,
  migrateAppDataBackToInstall,
} = require("./runtime_data.cjs");

function writeFile(root, relativePath, content, mtimeMs) {
  const target = path.join(root, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
  const timestamp = new Date(mtimeMs);
  fs.utimesSync(target, timestamp, timestamp);
  return target;
}

test("returns AppData runtime files to the install root without deleting either copy", () => {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "openreel-runtime-data-"));
  const source = path.join(workspace, "appdata");
  const target = path.join(workspace, "install");
  try {
    writeFile(target, "data/app.db", "install-old", 1_000);
    writeFile(source, "data/app.db", "appdata-new", 3_000);
    writeFile(target, "config/runtime.jsonc", "install-new", 4_000);
    writeFile(source, "config/runtime.jsonc", "appdata-old", 2_000);
    writeFile(source, "storage/project/video.mp4", "video", 3_000);

    const result = migrateAppDataBackToInstall(source, target);

    assert.equal(result.status, "completed");
    assert.equal(fs.readFileSync(path.join(target, "data/app.db"), "utf8"), "appdata-new");
    assert.equal(fs.readFileSync(path.join(target, "config/runtime.jsonc"), "utf8"), "install-new");
    assert.equal(fs.readFileSync(path.join(target, "storage/project/video.mp4"), "utf8"), "video");
    assert.equal(fs.readFileSync(path.join(source, "data/app.db"), "utf8"), "appdata-new");
    assert.equal(
      fs.readFileSync(path.join(target, APPDATA_RETURN_RECOVERY_DIR, "data/app.db"), "utf8"),
      "install-old",
    );
    assert.ok(fs.existsSync(path.join(target, APPDATA_RETURN_MARKER)));
    assert.deepEqual(result.stats, {
      copied: 1,
      replaced: 1,
      kept: 1,
      backed_up: 1,
      skipped: 0,
    });

    writeFile(source, "data/app.db", "ignored-after-marker", 5_000);
    assert.equal(migrateAppDataBackToInstall(source, target).status, "already_complete");
    assert.equal(fs.readFileSync(path.join(target, "data/app.db"), "utf8"), "appdata-new");
  } finally {
    fs.rmSync(workspace, { recursive: true, force: true });
  }
});

test("does not write a completion marker when AppData has no runtime directories", () => {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "openreel-runtime-empty-"));
  const source = path.join(workspace, "appdata");
  const target = path.join(workspace, "install");
  try {
    fs.mkdirSync(source, { recursive: true });
    const result = migrateAppDataBackToInstall(source, target);
    assert.equal(result.status, "no_source_data");
    assert.equal(fs.existsSync(path.join(target, APPDATA_RETURN_MARKER)), false);
  } finally {
    fs.rmSync(workspace, { recursive: true, force: true });
  }
});

test("does not mark a partial merge complete when a file copy fails", () => {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "openreel-runtime-failure-"));
  const source = path.join(workspace, "appdata");
  const target = path.join(workspace, "install");
  const originalCopyFileSync = fs.copyFileSync;
  try {
    writeFile(source, "data/app.db", "source", 3_000);
    fs.copyFileSync = (sourcePath, targetPath, ...args) => {
      if (targetPath === path.join(target, "data/app.db")) {
        throw new Error("simulated copy failure");
      }
      return originalCopyFileSync(sourcePath, targetPath, ...args);
    };

    assert.throws(
      () => migrateAppDataBackToInstall(source, target),
      /simulated copy failure/,
    );
    assert.equal(fs.existsSync(path.join(target, APPDATA_RETURN_MARKER)), false);
    assert.equal(fs.readFileSync(path.join(source, "data/app.db"), "utf8"), "source");
  } finally {
    fs.copyFileSync = originalCopyFileSync;
    fs.rmSync(workspace, { recursive: true, force: true });
  }
});
