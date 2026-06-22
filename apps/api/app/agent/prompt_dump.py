"""把每次 LLM 调用前组装好的 system / messages / tools 落盘,排查提示词问题用。

默认写到 apps/api/data/prompts/{project_id}/{run_id}.jsonl。生产环境可通过
DRAMA_PROMPT_DUMP_DIR=/workspace/data/prompt_dumps 写到宿主机挂载目录。
同一次 stream() 调用的所有 iteration 用同一个 run_id,顺序追加成 JSONL。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.agent.vision_context import redact_image_data_urls

logger = logging.getLogger(__name__)

_DUMP_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "prompts"
_CHARS_PER_TOKEN = 3.5
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password)([\"'\s:=]+)([^\"'\s,}]+)"
)


def prompt_dumps_root() -> Path:
    configured = os.getenv("DRAMA_PROMPT_DUMP_DIR", "").strip()
    root = Path(configured) if configured else _DUMP_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _redact(value: Any) -> Any:
    value = redact_image_data_urls(value)
    if isinstance(value, dict):
        return {
            key: (
                _redact(item)
                if str(key).lower() == "cache_key"
                else "<redacted>" if re.search(r"(?i)(key|token|secret|password|authorization)", str(key))
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_RE.sub(r"\1\2<redacted>", value)
    return value


def _prune_old_dumps(out_dir: Path) -> None:
    keep_days = max(1, int(os.getenv("DRAMA_PROMPT_DUMP_RETENTION_DAYS", "7")))
    cutoff = datetime.now() - timedelta(days=keep_days)
    for path in out_dir.glob("*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except OSError:
            logger.warning("prompt_dump prune failed: %s", path)


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _estimate_text_tokens(text: str) -> int:
    return max(0, int((len(text or "") + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN))


def _estimate_payload_tokens(payload: Any) -> int:
    safe_payload = _redact(payload)
    return _estimate_text_tokens(json.dumps(safe_payload, ensure_ascii=False, default=str))


def _section_token_estimates(prompt_assembly: dict | None) -> dict[str, int]:
    estimates = {
        "stable_section_tokens": 0,
        "history_section_tokens": 0,
        "dynamic_section_tokens": 0,
        "guide_section_tokens": 0,
    }
    if not isinstance(prompt_assembly, dict):
        return estimates
    sections = prompt_assembly.get("sections")
    if not isinstance(sections, list):
        return estimates
    for section in sections:
        if not isinstance(section, dict):
            continue
        tokens = _estimate_text_tokens("x" * int(section.get("chars") or 0))
        source = str(section.get("source") or "")
        tier = str(section.get("tier") or "")
        if source == "guide":
            estimates["guide_section_tokens"] += tokens
        elif source in {"factory", "state"}:
            estimates["dynamic_section_tokens"] += tokens
        elif tier == "s":
            estimates["stable_section_tokens"] += tokens
        else:
            estimates["history_section_tokens"] += tokens
    return estimates


def dump_llm_request(
    project_id: str,
    run_id: str,
    iteration: int,
    system: str,
    messages: list[dict],
    tools: list[dict],
    user_message: str | None = None,
    prompt_assembly: dict | None = None,
) -> None:
    if not _truthy_env("DRAMA_PROMPT_DUMP_ENABLED"):
        return
    # iteration=0 写完整 tools schema(几十 KB),之后只写 name 列表,避免每行重复
    # DRAMA_PROMPT_DUMP_FULL=true 时每轮都写完整 tools 和 API 顺序 messages,
    # 临时用于排查 provider prompt-cache 前缀断点。
    try:
        full_dump = _truthy_env("DRAMA_PROMPT_DUMP_FULL")
        out_dir = prompt_dumps_root() / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        _prune_old_dumps(out_dir)
        path = out_dir / f"{run_id}.jsonl"

        if full_dump or iteration == 0:
            tools_payload: Any = tools
        else:
            tools_payload = [t.get("function", {}).get("name", "?") for t in tools]
        api_messages = [{"role": "system", "content": system}, *messages] if system else list(messages)

        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "iteration": iteration,
            "dump_mode": "full" if full_dump else "compact",
            "user_message": _redact(user_message),
            "system_len": len(system or ""),
            "messages_count": len(messages),
            "tools_count": len(tools),
            "system": _redact(system),
            "messages": _redact(messages),
            "tools": _redact(tools_payload),
            "token_estimate": {
                "system_tokens": _estimate_text_tokens(system or ""),
                "messages_tokens": _estimate_payload_tokens(messages),
                "tool_schema_tokens": _estimate_payload_tokens(tools),
                "total_input_tokens": (
                    _estimate_text_tokens(system or "")
                    + _estimate_payload_tokens(messages)
                    + _estimate_payload_tokens(tools)
                ),
                **_section_token_estimates(prompt_assembly),
            },
        }
        if full_dump:
            record["api_request"] = _redact({
                "messages": api_messages,
                "tools": tools,
            })
        if prompt_assembly is not None:
            record["prompt_assembly"] = _redact(prompt_assembly)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        logger.info(
            "prompt_dump written: project=%s run=%s iter=%d → %s",
            project_id, run_id, iteration, path,
        )
    except Exception:
        logger.exception("dump_llm_request failed")
