# 桌面打包

[English](../DESKTOP_PACKAGING.md) · [中文文档首页](../README.md)

桌面版保留 Web/API 架构：Electron 管理窗口，启动 PyInstaller 打包的 FastAPI，并加载 Next.js standalone 服务。

## 构建平台

PyInstaller 产物与系统相关，每个平台应在目标系统构建。

| 目标 | 命令 | 构建机 |
| --- | --- | --- |
| Windows x64 | `pnpm desktop:package:win` | Windows x64 |
| Linux x64 | `pnpm desktop:package:linux` | Linux x64 |
| macOS | `pnpm desktop:package:mac` | macOS |

需要 Node.js 20+、pnpm 9+、Python 3.11+、uv 和 electron-builder 对应平台依赖。

Windows 第一次打包前运行：

```powershell
pnpm desktop:check:win
```

## 打包流程

平台脚本会：

1. 按 lockfile 安装 JavaScript 依赖；
2. 构建 Next.js standalone runtime；
3. 使用 PyInstaller 打包 FastAPI；
4. 检查提示词、协议目录和媒体子进程行为；
5. 把 API 和 Web 资源放入 Electron；
6. 使用 electron-builder 生成安装包。

产物位于 `dist/installers/`。

## 运行数据

桌面版会在应用数据根目录创建：

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

`OPENREEL_DATA_DIR` 可以覆盖数据根目录，`OPENREEL_SKILLS_DIR` 可以覆盖可编辑 Skill 目录。模型 Key 只属于运行配置，不会编译进安装包。

## 自动发布

`.github/workflows/release.yml` 在版本 tag 推送后并行构建 Windows、Linux 和 macOS。只有三个平台全部成功才发布 GitHub Release。

```bash
git tag v0.2.0
git push origin v0.2.0
```

推送或移动 tag 前必须先同步远程，比较完整文件树，检查未跟踪文件，并扫描密钥、运行数据、本地配置和构建产物。

npm 安装器由 `.github/workflows/npm-publish.yml` 发布。npm 已存在的版本不可覆盖，工作流会跳过重复版本。

## 安装后检查

1. Electron 只打开一个主窗口；
2. 本地 API 和 Web runtime 能启动；
3. 健康检查成功；
4. `config/` 下存在图片、视频和音频协议目录；
5. 设置页可以读取和保存 Provider；
6. Windows 媒体操作不会弹出命令行黑窗口；
7. 启动失败时 `logs/` 有可用日志。

## 签名状态

除非发布环境提供签名凭据，当前安装包默认未签名，系统可能提示发布者未验证。正式公开分发应增加 Windows Authenticode、Apple 签名与公证、SHA256 校验和以及明确的更新策略。
