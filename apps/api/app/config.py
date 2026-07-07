from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path


# Repository root = two levels above this file (apps/api/app/config.py -> repo root)
REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_sqlite_url(url: str) -> str:
    """Resolve a relative sqlite URL against the repo root so the same DB is
    used no matter the CWD where uvicorn is launched from."""
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return url
    rest = url[len(prefix):]
    if rest.startswith("/"):
        return url
    target = (REPO_ROOT / rest).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return f"{prefix}{target.as_posix()}"


class Settings(BaseSettings):
    APP_NAME: str = "OpenReel Studio"
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    WEB_PORT: int = 3000

    DATABASE_URL: str = "sqlite+aiosqlite:///./data/app.db"

    PROJECT_ROOT: str = str(REPO_ROOT)
    STORAGE_DRIVER: str = "local"
    STORAGE_PATH: str = "./storage"
    STORAGE_DIR: str = "./storage"

    LITELLM_ENABLED: bool = True
    DEFAULT_TEXT_MODEL: str = "deepseek/deepseek-chat"
    DEFAULT_FAST_MODEL: str = "deepseek/deepseek-chat"
    DEFAULT_SCRIPT_MODEL: str = "deepseek/deepseek-chat"
    DEFAULT_REVIEW_MODEL: str = "deepseek/deepseek-chat"

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    DASHSCOPE_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    MCP_ENABLED: bool = True

    SECRET_KEY: str = "change-me"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8000,http://localhost:8001,http://localhost:8002,http://localhost:8003,http://localhost:8004,http://localhost:8005"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def storage_path_resolved(self) -> Path:
        path = Path(self.STORAGE_PATH).expanduser()
        if not path.is_absolute():
            path = Path(self.PROJECT_ROOT).expanduser().resolve() / path
        return path.resolve()

    class Config:
        # Only load env files that actually exist (.env mechanism is deprecated;
        # API keys are managed via config/runtime.jsonc ConfigStore)
        env_file = tuple(
            str(p) for p in ((REPO_ROOT / ".env.local"), (REPO_ROOT / ".env")) if p.exists()
        )
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.DATABASE_URL = _resolve_sqlite_url(s.DATABASE_URL)
    return s


settings = get_settings()
