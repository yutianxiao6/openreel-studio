const fs = require("node:fs");

if (process.platform === "win32") {
  const originalSymlink = fs.symlink;
  const originalSymlinkSync = fs.symlinkSync;
  const originalPromisesSymlink = fs.promises.symlink;

  function isDirectorySync(target) {
    try {
      return fs.statSync(target).isDirectory();
    } catch {
      return false;
    }
  }

  async function isDirectory(target) {
    try {
      return (await fs.promises.stat(target)).isDirectory();
    } catch {
      return false;
    }
  }

  fs.symlink = function symlinkWithJunctionFallback(target, path, type, callback) {
    if (typeof type === "function") {
      callback = type;
      type = undefined;
    }
    const nextType = type ?? (isDirectorySync(target) ? "junction" : undefined);
    return originalSymlink.call(this, target, path, nextType, callback);
  };

  fs.symlinkSync = function symlinkSyncWithJunctionFallback(target, path, type) {
    const nextType = type ?? (isDirectorySync(target) ? "junction" : undefined);
    return originalSymlinkSync.call(this, target, path, nextType);
  };

  fs.promises.symlink = async function promisesSymlinkWithJunctionFallback(target, path, type) {
    const nextType = type ?? ((await isDirectory(target)) ? "junction" : undefined);
    return originalPromisesSymlink.call(this, target, path, nextType);
  };
}
