# Desktop Packaging

OpenReel Studio keeps the existing web/API architecture in desktop builds:

- Electron launches the local services and owns the app window.
- FastAPI is packaged as a local executable.
- Next.js runs from standalone output.
- Runtime data is stored under the installation directory.

## 中文摘要

OpenReel Studio 桌面端保留现有 Web/API 架构：

- Electron 负责启动本地服务并打开桌面窗口。
- FastAPI 通过 PyInstaller 打包成本地可执行文件。
- Next.js 使用 standalone 输出作为本地 Web runtime。
- 用户数据、配置、日志和媒体文件写入安装目录下的固定子目录。

三平台入口：

| 目标平台 | 命令 | 要求 |
| --- | --- | --- |
| Windows | `pnpm desktop:package:win` 或 `package-windows.bat` | Windows x64 |
| Linux | `pnpm desktop:package:linux` | Linux x64 |
| macOS | `pnpm desktop:package:mac` | macOS，支持 x64/arm64 |

每个平台都应在目标系统上打包，因为 PyInstaller 生成的是平台相关的 API 可执行文件。

## Platform Build Machines

Build each desktop package on the target operating system. The API executable is
made by PyInstaller and should not be cross-built.

| Target | Command | Host requirement |
| --- | --- | --- |
| Windows | `pnpm desktop:package:win` or `package-windows.bat` | Windows x64 |
| Linux | `pnpm desktop:package:linux` | Linux x64 |
| macOS | `pnpm desktop:package:mac` | macOS, x64/arm64 capable |

Required tools:

- Node.js 20 LTS or newer
- pnpm 9 or newer
- Python 3.11 or newer, available as `python`
- uv, available as `uv`
- Git, if the source is cloned on the build machine

Install the common tools from an elevated PowerShell if needed:

```powershell
npm install -g pnpm
pip install uv
```

Run the Windows preflight check before the first Windows package:

```powershell
pnpm desktop:check:win
```

## One-command Package

## One-command Download

After a version tag creates a GitHub Release, users can download the latest
installer without cloning the repository.

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.ps1 | iex
```

Linux/macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install.sh | bash
```

The scripts query the latest GitHub Release and download the asset matching the
current operating system.

## Manual Package

Windows:

```powershell
pnpm desktop:package:win
```

or from `cmd.exe`:

```bat
package-windows.bat
```

Linux:

```bash
pnpm desktop:package:linux
```

macOS:

```bash
pnpm desktop:package:mac
```

The scripts perform these steps:

1. Checks Windows packaging prerequisites.
2. Installs pnpm dependencies with the lockfile.
3. Builds `apps/web` with Next standalone output.
4. Stages the standalone web runtime under `apps/desktop/dist/resources/web`.
5. Packages `apps/api` with PyInstaller into `dist/openreel-api`.
6. Stages the API runtime under `apps/desktop/dist/resources/api/openreel-api`.
7. Builds the target desktop artifact with electron-builder.

The installer is written to:

```text
dist/installers/
```

The expected artifact name is similar to:

```text
OpenReel Studio-Setup-0.0.1.exe
OpenReel Studio-0.0.1-x64.AppImage
OpenReel Studio-0.0.1-x64.dmg
```

Windows uses the assisted NSIS installer. It shows the installation directory
page, supports installing over an existing OpenReel Studio installation, and
keeps `data`, `storage`, `config`, and `logs` when upgrading. If the selected
directory is a drive root, NSIS creates the application subdirectory before
copying files.

## Automated Release Workflow

The release workflow is defined in `.github/workflows/release.yml`.

Push a version tag to build all desktop targets and publish a GitHub Release:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The workflow uploads every file from `dist/installers/` as a release asset.

## Build Options

Skip dependency install when the lockfile has already been installed:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\desktop\build-windows.ps1 -SkipInstall
```

Skip only the preflight check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\desktop\build-windows.ps1 -SkipPreflight
```

## Runtime Data

Desktop builds write mutable data under the installed application directory by default:

```text
Windows: <install-dir>\
macOS:   directory containing OpenReel Studio.app
Linux:   directory containing the AppImage/binary
```

The important subdirectories are:

```text
data/
storage/
config/
logs/
```

Set `OPENREEL_DATA_DIR` before launch to override this location.

Model keys and runtime provider settings belong in `config/runtime.jsonc` via the settings UI. They are not compiled into the installer.

## Development

To run the desktop shell against local dev services:

```bash
pnpm desktop:dev
```

The Electron shell starts:

- API via `uv run uvicorn app.main:app`
- Web via `next dev`

Both services bind to `127.0.0.1` with dynamically selected ports.

## Smoke Test After Installing

After installing the generated `.exe`, launch OpenReel Studio from the Start
Menu. The desktop app should:

1. Open one Electron window.
2. Start the local API on a random `127.0.0.1` port.
3. Start the local Next.js server on a second `127.0.0.1` port.
4. Create `data`, `storage`, `config`, and `logs` under the install directory.

If startup fails, inspect:

```text
<install-dir>\logs\api.log
<install-dir>\logs\web.log
```

Common failures:

- `openreel-api.exe` missing: rerun the package script and check the PyInstaller step.
- `server.js` missing: rerun the package script and check `pnpm --filter web build`.
- API key/model errors: open the app settings and configure providers; keys are runtime config, not installer contents.
- Windows Defender warning: the installer is unsigned until code signing is configured.

## Release Notes

Code signing is not configured yet. Before distributing outside internal testing, add:

- Windows Authenticode signing certificate.
- electron-builder signing configuration.
- SHA256 checksum generation.
- Optional auto-update channel.
