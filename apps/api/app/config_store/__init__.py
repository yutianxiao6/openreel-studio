"""ConfigStore — runtime config.jsonc 是真相源，DB 是物化缓存。

Architecture:
    config/runtime.jsonc  ─load─►  ConfigStore (mem cache)  ─sync─►  DB tables

Writers (Agent / UI / 编辑器手改) 永远走 file 入口；DB 永远只被 store.load() 写。
"""
from app.config_store.schema import RuntimeConfig
from app.config_store.store import ConfigStore, get_store

__all__ = ["RuntimeConfig", "ConfigStore", "get_store"]
