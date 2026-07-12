# Development and testing

English · [简体中文](../zh-CN/development.md) · [Documentation home](../README.en.md)

## Development environment

Install Node.js, pnpm, Python, and uv as described in the [Quick start](./getting-started.md).

Common commands:

```bash
bash install.sh                 # Install and initialize
pnpm dev                        # Web development server
pnpm api:dev                    # API development server
pnpm api:init-db                # Initialize the database
pnpm -r typecheck               # TypeScript checks
git diff --check                # Patch and whitespace checks
```

Backend tests:

```bash
cd apps/api
PYTHONPATH=. uv run pytest -q
```

Frontend interaction changes require a real-browser pass and a screenshot of the modified state in addition to typecheck and build.

## Where changes belong

| Requirement | Preferred location |
| --- | --- |
| Pages and interaction | `apps/web` |
| API and persistence | `apps/api/app/api`, `services`, and `db` |
| Agent orchestration | `apps/api/app/agent` |
| Atomic tools | `apps/api/app/mcp_tools` |
| Video-production knowledge | Skills or workflow templates |
| Media HTTP protocols | `config/*_provider_protocols/catalog.json` |
| Reusable workflows | `workflow_templates/user` or built-in skill templates |

Business process and prompt-writing knowledge should live in skills rather than the stable per-turn system prompt. Rules that can be enforced by schemas, validators, or permission policy should be implemented and tested there.

## Before committing

1. Keep only task-related changes.
2. Run relevant backend tests, frontend checks, and browser verification.
3. Run `git diff --check`.
4. Inspect untracked files.
5. Scan for API keys, tokens, private keys, `.env` files, runtime data, and build output.
6. Confirm that `data/`, `storage/`, local screenshots, and user content are not staged.

## Documentation contributions

- Chinese documentation belongs in `docs/zh-CN/`; English documentation belongs in `docs/en/`.
- Root `README.md` is the Chinese product entry; `README.en.md` is the English product entry.
- User guides describe stable product behavior. Temporary implementation plans and debugging notes do not belong in the main navigation.
- Markdown under `apps/api/app/skills/` is runtime source. Changes alter agent behavior and require contract tests.
- Screenshots must come from the real product and must not expose keys, private conversations, identities, or unlicensed content.

## Pull request guidance

A pull request should state:

- the user-visible problem;
- the chosen solution and its boundaries;
- changed files;
- verification commands and results;
- real screenshots for UI changes;
- known limitations and follow-up work.

Do not attach full runtime databases, traces, raw model responses, or private configuration to a public pull request.
