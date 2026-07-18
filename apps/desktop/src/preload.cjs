const { contextBridge, ipcRenderer } = require("electron");

function readArg(name) {
  const prefix = `--${name}=`;
  const match = process.argv.find((arg) => arg.startsWith(prefix));
  return match ? match.slice(prefix.length) : "";
}

contextBridge.exposeInMainWorld("openReelDesktop", {
  apiBase: readArg("openreel-api-base"),
  webBase: readArg("openreel-web-base"),
  platform: process.platform,
  getMediaDownloadDirectory: () => ipcRenderer.invoke("openreel:get-media-download-directory"),
  chooseMediaDownloadDirectory: () => ipcRenderer.invoke("openreel:choose-media-download-directory"),
  saveMedia: (request) => ipcRenderer.invoke("openreel:save-media", request),
});
