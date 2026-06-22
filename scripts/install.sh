#!/usr/bin/env bash
set -euo pipefail

repo="yutianxiao6/openreel-studio"
api="https://api.github.com/repos/${repo}/releases/latest"
download_dir="${OPENREEL_DOWNLOAD_DIR:-${HOME}/Downloads}"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "$1 is required. $2" >&2
    exit 1
  fi
}

need curl "Install curl and run this command again."

os="$(uname -s)"
case "$os" in
  Linux)
    platform="linux"
    ;;
  Darwin)
    platform="macos"
    ;;
  *)
    echo "Unsupported OS: $os. Use Windows PowerShell with scripts/install.ps1, Linux, or macOS." >&2
    exit 1
    ;;
esac

echo "Fetching latest OpenReel Studio release..."
json="$(curl -fsSL -H "User-Agent: openreel-installer" "$api")"

if command -v python3 >/dev/null 2>&1; then
  url="$(
    RELEASE_JSON="$json" PLATFORM="$platform" python3 - <<'PY'
import json
import os

release = json.loads(os.environ["RELEASE_JSON"])
platform = os.environ["PLATFORM"]
assets = release.get("assets", [])

def pick_linux():
    for suffix in (".AppImage", ".deb"):
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(suffix):
                return asset.get("browser_download_url", "")
    return ""

def pick_macos():
    for suffix in (".dmg", ".zip"):
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(suffix):
                return asset.get("browser_download_url", "")
    return ""

print(pick_linux() if platform == "linux" else pick_macos())
PY
  )"
else
  if [ "$platform" = "linux" ]; then
    url="$(printf "%s" "$json" | grep -Eo '"browser_download_url":[[:space:]]*"[^"]+\.(AppImage|deb)"' | head -n 1 | sed -E 's/.*"([^"]+)"/\1/')"
  else
    url="$(printf "%s" "$json" | grep -Eo '"browser_download_url":[[:space:]]*"[^"]+\.(dmg|zip)"' | head -n 1 | sed -E 's/.*"([^"]+)"/\1/')"
  fi
fi

if [ -z "${url:-}" ]; then
  echo "No ${platform} installer asset was found in the latest release." >&2
  exit 1
fi

mkdir -p "$download_dir"
filename="${url##*/}"
filename="${filename%%\?*}"
if command -v python3 >/dev/null 2>&1; then
  filename="$(FILENAME="$filename" python3 - <<'PY'
import os
import urllib.parse
print(urllib.parse.unquote(os.environ["FILENAME"]))
PY
)"
fi
out="${download_dir}/${filename}"

echo "Downloading ${filename}..."
curl -fL "$url" -o "$out"

if [[ "$out" == *.AppImage ]]; then
  chmod +x "$out"
fi

echo "Downloaded: $out"

if [ "${OPENREEL_NO_OPEN:-0}" != "1" ]; then
  case "$platform" in
    linux)
      echo "Run it with: $out"
      ;;
    macos)
      if command -v open >/dev/null 2>&1; then
        open "$out"
      fi
      ;;
  esac
fi
