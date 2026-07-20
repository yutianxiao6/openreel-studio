"""Codex app-server bridge for the OpenReel embedded chat surface.

Codex remains the reasoning/orchestration agent.  OpenReel exposes a narrow set
of project-scoped dynamic tools and executes them through the existing atomic
node/skill services; this module never calls the OpenReel Agent Loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import settings
from app.mcp_tools import canvas_tools, config_tools
from app.mcp_tools.registry import registry


logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30.0
THREAD_REQUEST_TIMEOUT_SECONDS = 60.0
TURN_REQUEST_TIMEOUT_SECONDS = 1200.0


class CodexBridgeError(RuntimeError):
    """A user-visible Codex bridge failure."""


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": _object_schema(properties, required),
    }


OPENREEL_DYNAMIC_TOOLS: list[dict[str, Any]] = [
    _function_tool(
        "openreel_project_state",
        "Read the selected OpenReel project's current state, task summary, and node summary before deciding what to do.",
        {},
    ),
    _function_tool(
        "openreel_list_nodes",
        "List user-visible text, image, video, or audio nodes on the selected OpenReel canvas.",
        {
            "type": {"type": "string", "enum": ["text", "image", "video", "audio"]},
            "status": {"type": "string"},
            "surface": {"type": "string"},
            "query": {"type": "string"},
            "regex": {"type": "string"},
            "case_sensitive": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 0},
        },
    ),
    _function_tool(
        "openreel_get_nodes",
        "Read one or several detailed OpenReel nodes by visible id, or find details by text search.",
        {
            "node_id": {"type": "string"},
            "node_ids": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"},
            "regex": {"type": "string"},
            "case_sensitive": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 0},
        },
    ),
    _function_tool(
        "openreel_create_nodes",
        "Create one or a batch of OpenReel text/image/video/audio nodes. Use fields.references for creative dependencies.",
        {
            "type": {"type": "string", "enum": ["text", "image", "video", "audio"]},
            "fields": {"type": "object", "additionalProperties": True},
            "name": {"type": "string"},
            "prompt": {"type": "string"},
            "parent_node_id": {"type": "string"},
            "nodes": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "object", "additionalProperties": True},
            },
        },
    ),
    _function_tool(
        "openreel_update_nodes",
        "Patch one or several existing OpenReel nodes in place. Preserve ids and unrelated fields.",
        {
            "node_id": {"type": "string"},
            "patch": {"type": "object", "additionalProperties": True},
            "node_ids": {"type": "array", "items": {"type": "string"}},
            "updates": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "object", "additionalProperties": True},
            },
        },
    ),
    _function_tool(
        "openreel_run_node",
        "Run an existing OpenReel node through its configured model/provider. This does not invoke the OpenReel chat agent.",
        {
            "node_id": {"type": "string"},
            "action": {"type": "string", "enum": ["run", "render", "force"]},
            "extra_fields": {"type": "object", "additionalProperties": True},
            "hidden_extra_field_keys": {"type": "array", "items": {"type": "string"}},
        },
        ["node_id"],
    ),
    _function_tool(
        "openreel_move_node",
        "Set an existing node's canvas position without changing its creative content.",
        {
            "node_id": {"type": "string"},
            "x": {"type": "number"},
            "y": {"type": "number"},
        },
        ["node_id", "x", "y"],
    ),
    _function_tool(
        "openreel_connect_nodes",
        "Create a canvas dependency edge. Prefer node fields.references when the edge represents a production dependency.",
        {
            "source_node_id": {"type": "string"},
            "target_node_id": {"type": "string"},
            "label": {"type": "string"},
        },
        ["source_node_id", "target_node_id"],
    ),
    _function_tool(
        "openreel_delete_nodes",
        "Permanently delete selected nodes. Use only when the latest user message explicitly asks for deletion and pass confirm=true.",
        {
            "node_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "confirm": {"type": "boolean"},
        },
        ["node_ids", "confirm"],
    ),
    _function_tool(
        "openreel_search_skills",
        "Search OpenReel workflow, prompt, or review production knowledge before authoring an unfamiliar workflow.",
        {
            "query": {"type": "string"},
            "queries": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string", "enum": ["workflow", "prompt", "review"]},
            "scope": {"type": "string", "enum": ["user", "builtin"]},
            "regex": {"type": "string"},
            "case_sensitive": {"type": "boolean", "default": False},
        },
        ["category"],
    ),
    _function_tool(
        "openreel_get_skill",
        "Read one OpenReel production skill by exact name.",
        {
            "name": {"type": "string"},
            "category": {"type": "string", "enum": ["workflow", "prompt", "review"]},
            "scope": {"type": "string", "enum": ["user", "builtin"]},
            "detail": {"type": "string", "enum": ["summary", "full"]},
        },
        ["name"],
    ),
    _function_tool(
        "openreel_get_model_config",
        "Read the selected OpenReel installation's parsed model/provider configuration with secrets masked.",
        {},
    ),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_codex_bins() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/opt/homebrew/bin/codex"),
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    ]
    app_data = os.environ.get("APPDATA")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if app_data:
        candidates.extend([
            Path(app_data) / "npm" / "codex.cmd",
            Path(app_data) / "npm" / "codex.exe",
        ])
    if local_app_data:
        candidates.extend([
            Path(local_app_data) / "Programs" / "Codex" / "codex.exe",
            Path(local_app_data) / "Programs" / "codex" / "codex.exe",
        ])
    return candidates


def find_codex_binary() -> str | None:
    explicit = str(os.environ.get("OPENREEL_CODEX_BIN") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return str(path) if path.exists() else None
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    for candidate in _candidate_codex_bins():
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def _codex_command(binary: str, *args: str) -> list[str]:
    suffix = Path(binary).suffix.lower()
    if sys.platform == "win32" and suffix in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", binary, *args]
    return [binary, *args]


def _clean_args(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if item is not None}


class CodexAppServerBridge:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._thread_projects: dict[str, str] = {}
        self._resumed_threads: set[str] = set()
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._active_turns: dict[str, tuple[str, str]] = {}
        self._initialized = False
        self._authenticated = False
        self._external_plugin_installed = False
        self._binary: str | None = None
        self._user_agent = ""
        self._connected_at: str | None = None
        self._last_error: str | None = None
        self._stderr_tail: list[str] = []

    def project_lock(self, project_id: str) -> asyncio.Lock:
        return self._project_locks.setdefault(project_id, asyncio.Lock())

    async def start(self, *, restart: bool = False) -> dict[str, Any]:
        async with self._start_lock:
            if restart:
                await self._stop_unlocked()
            if self._process is not None and self._process.returncode is None and self._initialized:
                await self._refresh_connection_metadata()
                return self.status_snapshot()

            binary = find_codex_binary()
            self._binary = binary
            if not binary:
                self._last_error = (
                    "未找到 Codex CLI。请先安装并登录 Codex，或用 OPENREEL_CODEX_BIN 指定可执行文件。"
                )
                return self.status_snapshot(state="missing_cli")

            self._last_error = None
            self._stderr_tail = []
            command = _codex_command(binary, "app-server", "--stdio")
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(Path(settings.PROJECT_ROOT).expanduser().resolve()),
                    env=dict(os.environ),
                    limit=4 * 1024 * 1024,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._reader_task = asyncio.create_task(self._reader_loop())
                self._stderr_task = asyncio.create_task(self._stderr_loop())
                initialized = await self.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "openreel_studio",
                            "title": "OpenReel Studio",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    require_initialized=False,
                )
                self._user_agent = str(initialized.get("userAgent") or "") if isinstance(initialized, dict) else ""
                await self.notify("initialized", {})
                self._initialized = True
                await self._refresh_connection_metadata()
                if self._authenticated:
                    self._connected_at = _now_iso()
            except Exception as exc:
                self._last_error = str(exc)
                await self._stop_unlocked(preserve_error=True)
                return self.status_snapshot(state="error")
            return self.status_snapshot()

    async def _refresh_connection_metadata(self) -> None:
        if not self._initialized:
            return
        try:
            account = await self.request("account/read", {}, timeout=REQUEST_TIMEOUT_SECONDS)
            self._authenticated = bool(
                isinstance(account, dict)
                and (account.get("account") is not None or account.get("requiresOpenaiAuth") is False)
            )
        except Exception as exc:
            self._authenticated = False
            self._last_error = f"Codex 登录状态读取失败：{exc}"
        # Embedded OpenReel sessions use client-provided dynamic tools and do
        # not depend on the optional standalone plugin. Avoid plugin/list here:
        # a remote catalog can make that response several megabytes large.
        self._external_plugin_installed = False

    def status_snapshot(self, *, state: str | None = None) -> dict[str, Any]:
        running = self._process is not None and self._process.returncode is None
        if state is None:
            if not self._binary:
                state = "missing_cli"
            elif not running or not self._initialized:
                state = "error" if self._last_error else "disconnected"
            elif not self._authenticated:
                state = "login_required"
            else:
                state = "connected"
        connected = state == "connected"
        messages = {
            "connected": "Codex 已连接",
            "login_required": "Codex 已启动，但尚未登录",
            "missing_cli": "未检测到 Codex CLI",
            "disconnected": "Codex 未连接",
            "error": "Codex 连接失败",
        }
        return {
            "ok": connected,
            "connected": connected,
            "state": state,
            "label": messages.get(state, "Codex 未连接"),
            "detail": self._last_error,
            "binary_found": bool(self._binary),
            "binary": self._binary,
            "authenticated": self._authenticated,
            "app_server_running": running,
            "control_mode": "embedded_dynamic_tools",
            "openreel_agent_used": False,
            "external_plugin_installed": self._external_plugin_installed,
            "user_agent": self._user_agent,
            "connected_at": self._connected_at,
        }

    async def ensure_connected(self) -> dict[str, Any]:
        status = await self.start()
        if not status.get("connected"):
            raise CodexBridgeError(str(status.get("detail") or status.get("label") or "Codex 未连接"))
        return status

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        require_initialized: bool = True,
    ) -> Any:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise CodexBridgeError("Codex app-server 未运行")
        if require_initialized and not self._initialized:
            raise CodexBridgeError("Codex app-server 尚未初始化")
        loop = asyncio.get_running_loop()
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CodexBridgeError(f"Codex 请求超时：{method}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None) -> None:
        await self._write({"method": method, "params": params or {}})

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise CodexBridgeError("Codex app-server 连接已断开")
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" not in message:
                    future = self._pending.get(message.get("id"))
                    if future is not None and not future.done():
                        if message.get("error") is not None:
                            error = message.get("error") or {}
                            future.set_exception(CodexBridgeError(str(error.get("message") or error)))
                        else:
                            future.set_result(message.get("result"))
                    continue
                if "id" in message and "method" in message:
                    task = asyncio.create_task(self._handle_server_request(message))
                    self._server_request_tasks.add(task)
                    task.add_done_callback(self._server_request_tasks.discard)
                    continue
                if message.get("method"):
                    await self._broadcast_notification(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex app-server stdout reader failed")
        finally:
            if self._process is process and process.returncode is not None:
                self._last_error = self._last_error or f"Codex app-server 已退出（code={process.returncode}）"
            error = CodexBridgeError(self._last_error or "Codex app-server 连接已关闭")
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(error)

    async def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_tail.append(line[:1000])
                    self._stderr_tail = self._stderr_tail[-20:]
        except asyncio.CancelledError:
            raise

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        try:
            if method == "item/tool/call":
                result = await self._execute_dynamic_tool(message.get("params") or {})
                await self._write({"id": request_id, "result": result})
                return
            await self._write({
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"OpenReel Codex client does not support server request: {method}",
                },
            })
        except Exception as exc:
            try:
                await self._write({
                    "id": request_id,
                    "result": {
                        "success": False,
                        "contentItems": [{"type": "inputText", "text": json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)}],
                    },
                })
            except Exception:
                logger.exception("Failed to return Codex dynamic tool error")

    async def _execute_dynamic_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(params.get("threadId") or "")
        project_id = self._thread_projects.get(thread_id)
        if not project_id:
            raise CodexBridgeError("Codex thread is not bound to an OpenReel project")
        tool = str(params.get("tool") or "")
        arguments = _clean_args(params.get("arguments"))
        result = await self._dispatch_openreel_tool(project_id, tool, arguments)
        success = not (isinstance(result, dict) and (result.get("ok") is False or result.get("error")))
        return {
            "success": success,
            "contentItems": [{
                "type": "inputText",
                "text": json.dumps(result, ensure_ascii=False, default=str),
            }],
        }

    async def _dispatch_openreel_tool(
        self,
        project_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> Any:
        if tool == "openreel_project_state":
            return await registry.call("project.get_state", project_id=project_id)
        if tool == "openreel_list_nodes":
            return await registry.call("node.list", project_id=project_id, **arguments)
        if tool == "openreel_get_nodes":
            return await registry.call("node.get", project_id=project_id, **arguments)
        if tool == "openreel_create_nodes":
            return await registry.call("node.create", project_id=project_id, **arguments)
        if tool == "openreel_update_nodes":
            return await registry.call("node.update", project_id=project_id, **arguments)
        if tool == "openreel_run_node":
            return await registry.call("node.run", project_id=project_id, **arguments)
        if tool == "openreel_search_skills":
            return await registry.call("skill.search", **arguments)
        if tool == "openreel_get_skill":
            detail = arguments.get("detail")
            if detail == "summary":
                arguments["detail"] = ""
            return await registry.call("skill.get", **arguments)
        if tool == "openreel_get_model_config":
            return {
                "ok": True,
                "config": await config_tools.config_read(mask_secrets=True),
                "secrets_masked": True,
            }
        if tool == "openreel_move_node":
            internal_id = await self._resolve_internal_node_id(project_id, arguments.get("node_id"))
            x = float(arguments.get("x"))
            y = float(arguments.get("y"))
            updated = await canvas_tools.update_node(internal_id, {"position_x": x, "position_y": y})
            payload = {"id": internal_id, "position": {"x": x, "y": y}, **updated}
            await self._emit_canvas(project_id, "update_node", payload)
            return {"ok": True, **payload}
        if tool == "openreel_connect_nodes":
            source_id = await self._resolve_internal_node_id(project_id, arguments.get("source_node_id"))
            target_id = await self._resolve_internal_node_id(project_id, arguments.get("target_node_id"))
            edge = await canvas_tools.connect_nodes(
                project_id=project_id,
                source_node_id=source_id,
                target_node_id=target_id,
                label=arguments.get("label"),
            )
            await self._emit_canvas(project_id, "add_edge", edge)
            return {"ok": True, "edge": edge}
        if tool == "openreel_delete_nodes":
            if arguments.get("confirm") is not True:
                return {"ok": False, "error": "confirm must be true after explicit user authorization"}
            node_ids = arguments.get("node_ids")
            if not isinstance(node_ids, list) or not node_ids:
                return {"ok": False, "error": "node_ids must be a non-empty array"}
            result = await canvas_tools.delete_nodes(project_id, [str(item) for item in node_ids])
            for internal_id in result.get("_canvas_deleted_node_ids") or []:
                await self._emit_canvas(project_id, "delete_node", {"id": internal_id})
            return result
        raise CodexBridgeError(f"Unsupported OpenReel dynamic tool: {tool}")

    async def _resolve_internal_node_id(self, project_id: str, node_id: Any) -> str:
        value = str(node_id or "").strip()
        if not value:
            raise CodexBridgeError("node_id is required")
        node = await registry.call("node.get", project_id=project_id, node_id=value)
        if isinstance(node, dict) and node.get("ok") is False:
            raise CodexBridgeError(str(node.get("error") or "Node not found"))
        internal_id = str(
            (node or {}).get("_canvas_node_id")
            or (node or {}).get("_canvas_id")
            or (node or {}).get("id")
            or ""
        )
        if not internal_id:
            raise CodexBridgeError(f"Cannot resolve node id: {value}")
        return internal_id

    async def _emit_canvas(self, project_id: str, action: str, payload: dict[str, Any]) -> None:
        try:
            from app.agent.orchestrator import emit_canvas_event
            await emit_canvas_event(
                {"type": "canvas_action", "action": action, "payload": payload},
                project_id=project_id,
            )
        except Exception:
            logger.exception("Codex bridge canvas event failed: %s", action)

    async def _broadcast_notification(self, message: dict[str, Any]) -> None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = str(params.get("threadId") or "")
        if thread_id:
            queues = list(self._subscribers.get(thread_id) or [])
        else:
            queues = [queue for group in self._subscribers.values() for queue in group]
        for queue in queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    @asynccontextmanager
    async def subscribe(self, thread_id: str) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.setdefault(thread_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._subscribers.get(thread_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(thread_id, None)

    def _thread_instructions(self, project_id: str) -> str:
        return (
            "You are Codex embedded in the OpenReel Studio chat panel. You are the sole reasoning and orchestration agent; "
            "do not call, delegate to, or imitate the OpenReel Agent Loop. The selected OpenReel project id is "
            f"{project_id}. Use the openreel_* dynamic tools to read and change that project. "
            "Start stateful work by reading project state and nodes. Persist user-visible creative truth as text/image/video/audio nodes, "
            "use fields.references for production dependencies, and run nodes only after their model-facing fields are ready. "
            "Do not use shell commands or edit application source files to control OpenReel. "
            "Delete nodes only when the latest user message explicitly requests deletion and then pass confirm=true. "
            "For image nodes write an exact resolution matching aspect_ratio; read OpenReel model configuration rather than inventing model ids."
        )

    async def ensure_thread(self, project_id: str, existing_thread_id: str | None = None) -> tuple[str, bool]:
        await self.ensure_connected()
        thread_id = str(existing_thread_id or "").strip()
        if thread_id and thread_id not in self._resumed_threads:
            try:
                await self.request(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "cwd": str(Path(settings.PROJECT_ROOT).expanduser().resolve()),
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "developerInstructions": self._thread_instructions(project_id),
                    },
                    timeout=THREAD_REQUEST_TIMEOUT_SECONDS,
                )
                self._resumed_threads.add(thread_id)
                self._thread_projects[thread_id] = project_id
                return thread_id, False
            except Exception:
                logger.warning("Could not resume Codex thread %s; starting a new thread", thread_id)

        if thread_id and thread_id in self._resumed_threads:
            self._thread_projects[thread_id] = project_id
            return thread_id, False

        response = await self.request(
            "thread/start",
            {
                "cwd": str(Path(settings.PROJECT_ROOT).expanduser().resolve()),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "developerInstructions": self._thread_instructions(project_id),
                "dynamicTools": OPENREEL_DYNAMIC_TOOLS,
                "ephemeral": False,
            },
            timeout=THREAD_REQUEST_TIMEOUT_SECONDS,
        )
        thread_id = str((response or {}).get("thread", {}).get("id") or "")
        if not thread_id:
            raise CodexBridgeError("Codex thread/start did not return a thread id")
        self._resumed_threads.add(thread_id)
        self._thread_projects[thread_id] = project_id
        return thread_id, True

    async def start_turn(
        self,
        project_id: str,
        thread_id: str,
        message: str,
        *,
        client_user_message_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": message}],
                "clientUserMessageId": client_user_message_id,
            },
            timeout=TURN_REQUEST_TIMEOUT_SECONDS,
        )
        turn = (response or {}).get("turn") if isinstance(response, dict) else None
        turn_id = str((turn or {}).get("id") or "")
        if not turn_id:
            raise CodexBridgeError("Codex turn/start did not return a turn id")
        self._active_turns[project_id] = (thread_id, turn_id)
        return response

    async def interrupt_project(self, project_id: str) -> bool:
        active = self._active_turns.get(project_id)
        if not active:
            return False
        thread_id, turn_id = active
        await self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return True

    def clear_active_turn(self, project_id: str, turn_id: str | None = None) -> None:
        active = self._active_turns.get(project_id)
        if active is None:
            return
        if turn_id is None or active[1] == turn_id:
            self._active_turns.pop(project_id, None)

    async def stop(self) -> None:
        async with self._start_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self, *, preserve_error: bool = False) -> None:
        process = self._process
        self._process = None
        current = asyncio.current_task()
        for task in [self._reader_task, self._stderr_task, *self._server_request_tasks]:
            if task is not None and task is not current and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        self._server_request_tasks.clear()
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(CodexBridgeError("Codex app-server stopped"))
        self._pending.clear()
        self._initialized = False
        self._authenticated = False
        self._connected_at = None
        self._subscribers.clear()
        self._thread_projects.clear()
        self._resumed_threads.clear()
        self._active_turns.clear()
        if not preserve_error:
            self._last_error = None


codex_app_server = CodexAppServerBridge()
