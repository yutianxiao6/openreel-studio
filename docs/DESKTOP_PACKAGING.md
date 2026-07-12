# Desktop packaging

English · [简体中文](./zh-CN/desktop-packaging.md) · [Documentation home](./README.en.md)

OpenReel Studio desktop builds retain the web/API architecture: Electron owns the application window, starts a packaged FastAPI executable, and loads a standalone Next.js server on local ports.

## Build hosts

PyInstaller output is platform-specific. Build each package on its target operating system.

| Target | Command | Host |
| --- | --- | --- |
| Windows x64 | `pnpm desktop:package:win` | Windows x64 |
| Linux x64 | `pnpm desktop:package:linux` | Linux x64 |
| macOS | `pnpm desktop:package:mac` | macOS |

Requirements:

- Node.js 20 or later;
- pnpm 9 or later;
- Python 3.11 or later;
- uv;
- platform packaging tools required by electron-builder.

Run the Windows preflight before a first package:

```powershell
pnpm desktop:check:win
```

## Build pipeline

The platform scripts:

1. install locked JavaScript dependencies;
2. build the standalone Next.js runtime;
3. package FastAPI with PyInstaller;
4. verify bundled prompts, protocols, and media subprocess behavior;
5. stage API and Web resources for Electron;
6. build installer artifacts with electron-builder.

Output is written to `dist/installers/`.

## Runtime data

Desktop builds create these writable directories under the selected application data root:

```text
data/
storage/
assets/
config/
logs/
plugins/
skills/
workflow_templates/
```

Set `OPENREEL_DATA_DIR` to override the root and `OPENREEL_SKILLS_DIR` to override editable skills. Provider keys remain runtime configuration and are never compiled into an installer.

## Release workflow

`.github/workflows/release.yml` builds Windows, Linux, and macOS from a version tag. The release is published only after every platform succeeds.

```bash
git tag v0.2.0
git push origin v0.2.0
```

Before moving or publishing a tag, fetch the remote, compare the complete file tree, inspect untracked files, and scan for secrets, runtime data, local configuration, and build output.

The npm installer package is handled by `.github/workflows/npm-publish.yml`. An already published npm version is immutable and is skipped rather than overwritten.

## Installed smoke test

After installation, verify that:

1. one Electron window opens;
2. the packaged API and Web runtimes start on local ports;
3. the health endpoint succeeds;
4. image, video, and audio protocol catalogs exist under `config/`;
5. Settings can read and save runtime provider configuration;
6. media tools do not flash command windows on Windows;
7. logs are written under `logs/` when startup fails.

## Signing status

Release artifacts are currently unsigned unless the release environment supplies signing credentials. Operating systems may show an unverified-publisher warning. Public production distribution should add Windows Authenticode, Apple signing/notarization, checksums, and a documented update policy.
