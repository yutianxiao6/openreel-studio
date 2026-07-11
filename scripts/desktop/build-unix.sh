#!/usr/bin/env bash
set -euo pipefail

target="${1:-}"
if [[ "$target" != "linux" && "$target" != "mac" ]]; then
  echo "Usage: scripts/desktop/build-unix.sh linux|mac" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
api_dist="$root/dist/openreel-api"
api_stage="$root/apps/desktop/dist/resources/api/openreel-api"
web_stage="$root/apps/desktop/dist/resources/web"
installer_dir="$root/dist/installers"
spec="$root/packaging/pyinstaller/openreel-api.spec"

case "$(uname -s)" in
  Linux) host="linux" ;;
  Darwin) host="mac" ;;
  *)
    echo "Unsupported host OS: $(uname -s). Use Windows, Linux, or macOS." >&2
    exit 1
    ;;
esac

if [[ "$host" != "$target" ]]; then
  echo "Cannot build $target desktop package on $host. PyInstaller must run on the target OS." >&2
  exit 1
fi

step() {
  echo
  echo "==> $*"
}

require_command() {
  local name="$1"
  local hint="$2"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "$name was not found. $hint" >&2
    exit 1
  fi
}

check_node() {
  local version major
  version="$(node --version)"
  major="${version#v}"
  major="${major%%.*}"
  if [[ "$major" -lt 20 ]]; then
    echo "Node.js 20+ is required. Current version: $version" >&2
    exit 1
  fi
  echo "[check] Node.js $version"
}

check_python() {
  local version
  version="$(python3 --version 2>&1)"
  case "$version" in
    "Python 3."*) ;;
    *)
      echo "Python 3.11+ is required. Current version: $version" >&2
      exit 1
      ;;
  esac
  echo "[check] $version"
}

step "Preflight"
require_command node "Install Node.js 20 LTS."
check_node
require_command pnpm "Install pnpm with: npm install -g pnpm"
echo "[check] pnpm $(pnpm --version)"
require_command python3 "Install Python 3.11+."
check_python
require_command uv "Install uv from https://docs.astral.sh/uv/."
echo "[check] $(uv --version)"

cd "$root"

export NEXT_PUBLIC_BASE_PATH=""
export NEXT_PUBLIC_API_BASE_URL=""
export INTERNAL_API_BASE_URL="http://127.0.0.1:8000"

if [[ "${SKIP_INSTALL:-0}" != "1" ]]; then
  step "Install JavaScript dependencies"
  pnpm install --frozen-lockfile
fi

step "Build Next.js standalone web runtime"
pnpm --filter web build

step "Stage web runtime for Electron"
pnpm desktop:stage:web
if [[ ! -f "$web_stage/apps/web/server.js" ]]; then
  echo "Staged web server.js was not found under apps/desktop/dist/resources/web." >&2
  exit 1
fi

step "Package FastAPI runtime with PyInstaller"
rm -rf "$api_dist"
uv run --project "$root/apps/api" --with pyinstaller pyinstaller "$spec" --noconfirm

step "Stage API runtime for Electron"
rm -rf "$api_stage"
mkdir -p "$(dirname "$api_stage")"
cp -R "$api_dist" "$api_stage"
if [[ ! -f "$api_stage/openreel-api" ]]; then
  echo "Staged API executable was not found under apps/desktop/dist/resources/api/openreel-api." >&2
  exit 1
fi

step "Smoke-test packaged API resources"
smoke_root="$(mktemp -d)"
if ! OPENREEL_USER_DATA_DIR="$smoke_root" \
  PROJECT_ROOT="$smoke_root" \
  OPENREEL_PACKAGING_SMOKE=1 \
  "$api_stage/openreel-api"; then
  rm -rf "$smoke_root"
  echo "Packaged API smoke test failed." >&2
  exit 1
fi
for protocol_dir_name in image_provider_protocols video_provider_protocols audio_provider_protocols; do
  if [[ ! -f "$smoke_root/config/$protocol_dir_name/catalog.json" ]]; then
    rm -rf "$smoke_root"
    echo "Packaged protocol catalog was not installed: config/$protocol_dir_name/catalog.json" >&2
    exit 1
  fi
done
rm -rf "$smoke_root"

step "Build $target desktop package"
pnpm --filter desktop "package:$target"

step "Installer output"
if [[ -d "$installer_dir" ]]; then
  find "$installer_dir" -maxdepth 1 -type f -printf "%f %s bytes\n" 2>/dev/null || ls -lh "$installer_dir"
else
  echo "Installer output directory was not created: $installer_dir" >&2
  exit 1
fi
