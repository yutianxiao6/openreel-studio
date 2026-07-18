# Quick start

English · [简体中文](../zh-CN/getting-started.md) · [Documentation home](../README.en.md)

## Choose a runtime

| Option | Best for |
| --- | --- |
| Desktop package | Using the product without modifying code. |
| Source checkout | Development, debugging, and customization. |
| Docker | Servers, long-running deployments, and team access. |

## Desktop package

Download the package for your platform from [GitHub Releases](https://github.com/yutianxiao6/openreel-studio/releases/latest):

- Windows: `OpenReel.Studio-Setup-*.exe`
- Linux: `*.AppImage` or `*.deb`
- macOS: `*.dmg` or `*.zip`

The installer CLI can also select the latest package:

```bash
npx openreel-studio-installer
```

The first launch creates local database, configuration, asset, and log directories. You still need to configure your own model accounts in Settings.

## Run from source

### Requirements

- Node.js 20 or later
- pnpm 9 or later
- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)

### Install

```bash
git clone https://github.com/yutianxiao6/openreel-studio.git
cd openreel-studio
bash install.sh
```

The installer resolves frontend and backend dependencies, creates runtime directories, and initializes SQLite.

### Start

Open two terminals:

```bash
# Terminal 1: API
pnpm api:dev
```

```bash
# Terminal 2: Web
pnpm dev
```

Open `http://localhost:3000`. The API listens on `http://localhost:8000` by default.

### Configure the first models

1. Open Settings from the application header.
2. Add at least one LLM provider, choose its tier, and set the tier default.
3. Add image, video, or audio providers as needed.
4. Select the protocol that matches the media provider HTTP API. Add a Catalog protocol first when no matching option exists.
5. Save, create a project, verify the Agent with a small request, then verify each external media service with a minimal node run.

Local `config/runtime.jsonc` is the configuration source of truth. Prefer the Settings UI and never commit real API keys.

Settings has no separate connection-test button. Saving confirms local validation; a minimal node run confirms end-to-end provider access. See [Model configuration and provider protocols](./model-providers.md) for fields, Base URL rules, and complete image/video/audio protocol examples.

## Docker

For a local development-style deployment:

```bash
docker compose up -d --build
```

Inspect containers and logs:

```bash
docker compose ps
docker compose logs -f api web
```

For the production overlay:

```bash
cp .env.production.example .env.production
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The production overlay adds a Caddy gateway and the `/studio` base path. Configure the domain, certificates, authentication, and `.env.production` before exposing it publicly.

## Verify the installation

```bash
curl http://localhost:8000/api/health
```

The response should include `"status":"ok"`. Then verify the product path:

1. Create a project.
2. Send a normal chat message.
3. Create and run a text node.
4. Open Settings and confirm provider configuration is readable.
5. If a media provider is configured, run one minimal image or video node.

## Troubleshooting

### Chat works, but image or video generation does not

LLM and media providers are configured separately. Check that the media provider is enabled, its model name is correct, and its protocol ID exists. Run a node with the smallest supported parameters and inspect the node error.

### The page loads, but API calls fail

Check the API port, `NEXT_PUBLIC_API_BASE_URL`, reverse-proxy path, and CORS configuration. In production Docker mode, the browser should enter through the `/studio` gateway.

### A desktop build reports missing protocols

Upgrade to the latest release. Protocol catalogs are bundled with current installers and should not require manual copying.

### Where is runtime data stored?

Source and Docker deployments use `data/`, `storage/`, `assets/`, and `config/` by default. Desktop paths are platform-specific; see [Desktop packaging](../DESKTOP_PACKAGING.md).

Continue with the [User guide](./user-guide.md).
