# 快速开始

[English](../en/getting-started.md) · [中文文档首页](../README.md)

## 选择运行方式

| 方式 | 适合场景 |
| --- | --- |
| 桌面安装包 | 想直接使用，不需要修改代码。 |
| 源码运行 | 开发、调试或二次开发。 |
| Docker | 自有服务器、长期运行或团队访问。 |

## 桌面版

从 [GitHub Releases](https://github.com/yutianxiao6/openreel-studio/releases/latest) 下载当前平台的安装包：

- Windows：`OpenReel.Studio-Setup-*.exe`
- Linux：`*.AppImage` 或 `*.deb`
- macOS：`*.dmg` 或 `*.zip`

也可以使用安装器 CLI：

```bash
npx openreel-studio-installer
```

桌面版第一次启动会在本机创建数据库、配置、资产和日志目录。模型账号仍需由你在设置面板中配置。

## 从源码运行

### 环境要求

- Node.js 20 或更高版本
- pnpm 9 或更高版本
- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)

### 安装

```bash
git clone https://github.com/yutianxiao6/openreel-studio.git
cd openreel-studio
bash install.sh
```

安装脚本会安装前后端依赖、创建运行目录并初始化 SQLite 数据库。

### 启动

打开两个终端：

```bash
# 终端 1：API
pnpm api:dev
```

```bash
# 终端 2：Web
pnpm dev
```

然后访问 `http://localhost:3000`。API 默认运行在 `http://localhost:8000`。

### 第一次配置模型

1. 打开右上角设置面板。
2. 在 LLM 页面添加至少一个文本模型并设置任务映射。
3. 按需在媒体页面添加图片、视频或音频服务。
4. 使用测试按钮确认 Base URL、API Key、模型名和协议匹配。
5. 保存后新建项目，用一句简单请求验证 Agent 能正常响应。

配置真相源是本地 `config/runtime.jsonc`。优先通过设置面板修改，不要把真实 API Key 提交到 Git。

## Docker

开发型本地容器：

```bash
docker compose up -d --build
```

查看状态和日志：

```bash
docker compose ps
docker compose logs -f api web
```

生产覆盖配置：

```bash
cp .env.production.example .env.production
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

生产配置默认增加 Caddy 网关和 `/studio` Base Path。部署前请配置域名、证书、鉴权和 `.env.production`，不要直接使用示例密钥。

## 验证安装

```bash
curl http://localhost:8000/api/health
```

预期返回包含 `"status":"ok"` 的 JSON。随后在浏览器中完成以下检查：

1. 创建项目；
2. 发送一条普通消息；
3. 创建文本节点并运行；
4. 打开设置页确认模型配置可读；
5. 如果配置了媒体模型，再运行一个最小图片或视频节点。

## 常见问题

### Agent 可以聊天，但不能生成图片或视频

LLM 与媒体服务是独立配置。检查对应媒体 Provider 是否启用、模型名是否正确、协议 ID 是否存在，并使用设置页测试连接。

### 页面能打开，但请求 API 失败

确认 API 端口、`NEXT_PUBLIC_API_BASE_URL`、反向代理路径和 CORS 配置一致。Docker 生产模式下，浏览器应通过 `/studio` 网关访问。

### 安装版找不到协议

先升级到最新 Release。协议目录应由安装包自动提供，不需要手动复制到程序目录。

### 数据放在哪里

源码和 Docker 默认使用仓库下的 `data/`、`storage/`、`assets/` 和 `config/`。桌面版使用平台对应的应用数据目录；具体位置见 [桌面打包说明](./desktop-packaging.md)。

下一步阅读 [使用指南](./user-guide.md)。
