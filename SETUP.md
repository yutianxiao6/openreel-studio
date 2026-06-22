# OpenReel Studio - 快速开始

## 环境要求

- **Node.js** >= 20
- **Python** >= 3.11
- **pnpm** (前端包管理)
- **uv** (Python 包管理)

## 安装步骤

### 1. 克隆仓库

```bash
git clone <repository-url>
cd openreel-studio
```

### 2. 配置环境变量

```bash
# 复制示例配置
cp .env.example .env
cp .env.local.example .env.local

# 编辑 .env.local 填入你的 API keys
# 注意:.env.local 不会被提交到 git
```

### 3. 安装依赖

```bash
# 前端依赖
pnpm install

# 后端依赖
cd apps/api
uv sync
cd ../..
```

### 4. 启动服务

打开两个终端分别启动 API 和 Web。

#### 终端 1: API

```bash
pnpm api:dev
```

#### 终端 2: Web

```bash
pnpm dev
```

### 5. 访问应用

- **Web 界面**: http://localhost:3000
- **API 文档**: http://localhost:8000/docs
- **健康检查**: http://localhost:8000/api/health

## 端口配置

默认端口:

- API: `8000`
- Web: `3000`

## 常见问题

### 端口被占用

如果 8000 端口被占用，可以手动指定 API 端口:

```bash
cd apps/api
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

同时需要设置前端环境变量(创建 `apps/web/.env.local`):

```env
NEXT_PUBLIC_API_BASE_URL=http://localhost:8001
```

### CORS 错误

确保 `.env` 中的 `CORS_ORIGINS` 包含你的前端地址:

```env
CORS_ORIGINS=http://localhost:3000,http://localhost:8000
```

### API Key 配置

**重要**: 不要在 `.env` 中填写真实的 API keys!

所有敏感信息应该放在 `.env.local` 中(已在 `.gitignore` 中排除):

```env
# .env.local
DEEPSEEK_API_KEY=<your-deepseek-api-key>
OPENAI_API_KEY=<your-openai-api-key>
```

## 数据库

默认使用 SQLite,数据库文件位于 `data/app.db`。

如需使用 PostgreSQL,修改 `.env`:

```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/drama_agent
```

## 开发模式

### 单独启动服务

```bash
# 只启动 API
pnpm api:dev

# 只启动 Web
pnpm dev
```

### 查看日志

API 和 Web 日志分别输出在对应终端中。

## 生产部署

生产环境使用 `docker-compose.prod.yml`。默认只向宿主机暴露 HTTPS 网关，
Web 页面和 API 都由 Caddy Basic Auth 保护；API 容器不直接暴露到公网。

Linux 服务器可以直接使用一条命令部署：

```bash
curl -fsSL https://raw.githubusercontent.com/yutianxiao6/openreel-studio/main/scripts/install-server.sh | bash
```

脚本会克隆或更新仓库、创建 `.env.production`、生成登录密码哈希并启动
Docker 服务。API 首次启动时会根据已有的 `*_API_KEY` 环境变量自动生成
`config/runtime.jsonc`。

### 1. 创建生产配置

```bash
cp .env.production.example .env.production
docker run --rm caddy:2-alpine caddy hash-password --plaintext '替换为你的密码'
```

将生成的哈希填入 `.env.production` 的 `AUTH_PASSWORD_HASH`，并设置
`AUTH_USER`。不要把明文密码写入文件。

如果不想手动编辑 `config/runtime.jsonc`，可以只把 API Key 写入
`.env.production`。服务首次启动时会按环境变量自动生成默认运行时配置；
也可以复制无密钥模板：

```bash
cp config/runtime.example.jsonc config/runtime.jsonc
```

真实 Key 不要写进仓库，只写入 `.env.production` 或让
`config/runtime.jsonc` 使用 `${ENV_VAR}` 引用。

### 2. 启动服务

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
```

Caddy 默认使用内部 TLS 证书。直接用服务器 IP 访问时，浏览器首次访问会提示证书风险，需要手动确认。

默认访问地址：

```text
https://服务器公网IP:3100
```

### 3. 限制公网访问

在云服务器安全组中，仅允许你自己的公网 IP 访问 TCP `3100`。
例如来源填写 `203.0.113.10/32`，不要使用 `0.0.0.0/0`。这样即使其他人
知道服务器地址，也无法连接到登录页面。

### 4. 常用命令

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml ps
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f
docker compose --env-file .env.production -f docker-compose.prod.yml down
```

## 配置模型 API Key

应用支持在页面中动态配置模型，不需要重启服务：

1. 登录应用，点击右上角的设置按钮。
2. 打开 `LLM 模型`，点击 `添加 Provider`。
3. 填写名称、Provider 前缀、模型名和 API Key。
4. 官方接口的 Base URL 可以留空；中转接口填写对应地址。
5. 勾选 `设为默认` 和 `启用`，然后保存。

LLM Provider 里还可以填写模型元数据，这些字段用于上下文监控、压缩率估算、
默认输出长度和能力判断：

- `上下文窗口 tokens`：模型完整上下文窗口大小。
- `最大输入 tokens`：可用于输入的上限；服务商预留输出空间时可小于上下文窗口。
- `最大输出 tokens`：该 Provider 默认输出上限；任务没有单独配置时使用。
- `Prompt Cache`：模型或中转站是否支持缓存统计/计费。
- `视觉输入`：聊天接口是否支持图片输入。
- `Tokenizer`：token 估算器标记，例如 `o200k_base`、`cl100k_base` 或 `provider`。
- `扩展参数 JSON`：模型私有元数据，原样保存到 `config/runtime.jsonc`。

这些字段留空时，系统不会按模型名猜测真实上下文，只会用本地压缩阈值做压力提示。

例如 DeepSeek 官方接口可以填写：

```text
名称：deepseek-default
Provider 前缀：deepseek
模型名：deepseek-chat
Base URL：留空
API Key：你的 DeepSeek Key
```

图片和视频模型分别在 `图片 Provider`、`视频 Provider` 中添加。配置保存后
会写入本机的 `config/runtime.jsonc`。该文件已被 `.gitignore` 忽略，不会
提交到仓库。

生产环境也可以将密钥放入 `.env.production`，再在
`config/runtime.jsonc` 中通过环境变量引用，避免把真实 Key 写入配置文件：

```env
DEEPSEEK_API_KEY=你的真实Key
```

```json
{
  "name": "deepseek-default",
  "provider": "deepseek",
  "model_name": "deepseek-chat",
  "base_url": null,
  "api_key": "${DEEPSEEK_API_KEY}",
  "is_default": true,
  "enabled": true
}
```

如果修改了 `.env.production`，需要重新创建 API 容器使环境变量生效：

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --force-recreate api
```
