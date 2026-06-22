const { contextBridge } = require("electron");

function readArg(name) {
  const prefix = `--${name}=`;
  const match = process.argv.find((arg) => arg.startsWith(prefix));
  return match ? match.slice(prefix.length) : "";
}

contextBridge.exposeInMainWorld("openReelDesktop", {
  apiBase: readArg("openreel-api-base"),
  webBase: readArg("openreel-web-base"),
  platform: process.platform,
});
