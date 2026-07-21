"""Translate one OpenReel provider row into an immutable UMA client binding.

The provider row contains connection/model configuration only. Protocol
documents stay in Universal Model Adapter's bundled catalog or the project
protocol directory and are never embedded in runtime.jsonc.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from universal_model_adapter import (
    AdapterConfig,
    AsyncClient,
    LogicalModel,
    ProviderConnection,
    ProviderModelTarget,
    Route,
    TargetOperation,
    common_protocol_path,
)

from app.config import settings


UNIVERSAL_ADAPTER_API_FORMAT = "universal_adapter"
_KIND_DEFAULT_OPERATIONS = {
    "image": "image.generate",
    "video": "video.generate",
    "audio": "audio.speech",
    "llm": "llm.chat",
}
_ID_RE = re.compile(r"[^a-z0-9._-]+")


class UniversalAdapterProviderOptions(BaseModel):
    """OpenReel-owned routing and target settings for one UMA provider."""

    model_config = ConfigDict(extra="forbid")

    protocol_id: str = Field(min_length=1)
    operation: str | None = None
    base_slot: str = Field(default="api", min_length=1)
    credential_slot: str | None = "api_key"
    bases: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    provider_parameters: dict[str, Any] = Field(default_factory=dict)
    target_parameters: dict[str, Any] = Field(default_factory=dict)
    target_defaults: dict[str, Any] = Field(default_factory=dict)
    logical_defaults: dict[str, Any] = Field(default_factory=dict)
    request_schema: dict[str, Any] = Field(default_factory=dict)
    variants: dict[str, Any] = Field(default_factory=dict)
    accepted_media_roles: tuple[str, ...] = ()
    input_map: dict[str, str] = Field(default_factory=dict)
    parameter_map: dict[str, str] = Field(default_factory=dict)
    static_input: dict[str, Any] = Field(default_factory=dict)
    static_parameters: dict[str, Any] = Field(default_factory=dict)
    pass_extra_parameters: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0)
    poll_interval_seconds: float = Field(default=1, ge=0)
    poll_max_interval_seconds: float = Field(default=10, gt=0)
    task_timeout_seconds: float = Field(default=1800, gt=0)
    task_success_grace_polls: int = Field(default=2, ge=0, le=20)
    max_concurrency: int = Field(default=8, ge=1)
    max_media_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    max_output_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)

    @field_validator("protocol_id", "operation", "base_slot", "credential_slot")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("accepted_media_roles")
    @classmethod
    def unique_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(role.strip() for role in value if role.strip())
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("accepted_media_roles must not contain duplicates")
        return cleaned

    @model_validator(mode="after")
    def valid_poll_policy(self) -> UniversalAdapterProviderOptions:
        if not self.protocol_id:
            raise ValueError("protocol_id must not be blank")
        if not self.base_slot:
            raise ValueError("base_slot must not be blank")
        if self.operation == "":
            raise ValueError("operation must not be blank")
        if self.credential_slot == "":
            raise ValueError("credential_slot must be null or a non-empty slot name")
        if self.poll_max_interval_seconds < self.poll_interval_seconds:
            raise ValueError("poll_max_interval_seconds must be at least poll_interval_seconds")
        for mapping_name, mapping in (
            ("input_map", self.input_map),
            ("parameter_map", self.parameter_map),
        ):
            if any(
                not str(source).strip() or not str(target).strip()
                for source, target in mapping.items()
            ):
                raise ValueError(f"{mapping_name} keys and values must not be empty")
        return self

    def operation_for(self, kind: str) -> str:
        operation = self.operation or _KIND_DEFAULT_OPERATIONS.get(kind)
        if not operation:
            raise ValueError(f"no default UMA operation exists for provider kind {kind!r}")
        prefix, separator, name = operation.partition(".")
        if not separator or prefix != kind or not name:
            raise ValueError(f"UMA operation {operation!r} must use the {kind!r} modality prefix")
        return operation


@dataclass(frozen=True)
class UniversalAdapterBinding:
    client: AsyncClient
    provider_name: str
    remote_model: str
    logical_model: str
    kind: str
    operation: str
    options: UniversalAdapterProviderOptions
    cache_key: str


def is_universal_adapter_provider(provider: Any) -> bool:
    return str(getattr(provider, "api_format", "") or "").strip() == UNIVERSAL_ADAPTER_API_FORMAT


def parse_universal_adapter_options(
    provider: Any,
    provider_params: dict[str, Any],
) -> UniversalAdapterProviderOptions:
    if not is_universal_adapter_provider(provider):
        raise ValueError("provider does not use api_format='universal_adapter'")
    raw = provider_params.get("uma")
    if not isinstance(raw, dict):
        raise ValueError("universal_adapter provider requires params.uma configuration")
    forbidden = sorted(key for key in ("protocol", "protocol_document", "operations") if key in raw)
    if forbidden:
        raise ValueError(
            "params.uma contains inline protocol fields; keep protocol documents in the protocol catalog"
        )
    return UniversalAdapterProviderOptions.model_validate(raw)


def universal_adapter_protocol_paths() -> tuple[Path, ...]:
    paths: list[Path] = [common_protocol_path()]
    project_catalog = (
        Path(settings.PROJECT_ROOT).expanduser().resolve()
        / "config"
        / "universal_model_adapter"
        / "protocols"
    )
    if project_catalog.exists():
        paths.append(project_catalog)
    for raw in os.getenv("OPENREEL_UMA_PROTOCOLS", "").split(os.pathsep):
        text = raw.strip()
        if not text:
            continue
        path = Path(text).expanduser().resolve()
        if path.exists() and path not in paths:
            paths.append(path)
    return tuple(paths)


def _safe_id(value: str, *, fallback: str) -> str:
    normalized = _ID_RE.sub("-", value.strip().lower()).strip("-._")
    return normalized or fallback


def _protocol_revision(paths: tuple[Path, ...]) -> list[tuple[str, int, int]]:
    revisions: list[tuple[str, int, int]] = []
    for path in paths:
        candidates = sorted(path.rglob("*.json")) if path.is_dir() else [path]
        for candidate in candidates:
            try:
                stat = candidate.stat()
            except OSError:
                continue
            revisions.append((str(candidate), stat.st_mtime_ns, stat.st_size))
    return revisions


def universal_adapter_cache_key(
    provider: Any,
    provider_params: dict[str, Any],
) -> str:
    options = parse_universal_adapter_options(provider, provider_params)
    paths = universal_adapter_protocol_paths()
    payload = {
        "name": str(getattr(provider, "name", "") or ""),
        "kind": str(getattr(provider, "kind", "") or ""),
        "base_url": str(getattr(provider, "base_url", "") or ""),
        "api_key": str(getattr(provider, "api_key", "") or ""),
        "model_name": str(getattr(provider, "model_name", "") or ""),
        "params": provider_params,
        "options": options.model_dump(mode="json"),
        "protocol_revision": _protocol_revision(paths),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_universal_adapter_binding(
    provider: Any,
    provider_params: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> UniversalAdapterBinding:
    options = parse_universal_adapter_options(provider, provider_params)
    kind = str(getattr(provider, "kind", "") or "").strip()
    operation = options.operation_for(kind)
    provider_name = str(getattr(provider, "name", "") or "").strip()
    remote_model = str(getattr(provider, "model_name", "") or "").strip()
    base_url = str(getattr(provider, "base_url", "") or "").strip()
    api_key = str(getattr(provider, "api_key", "") or "").strip()
    if not provider_name or not remote_model:
        raise ValueError("universal_adapter provider requires name and model_name")

    provider_id = f"openreel-{_safe_id(kind, fallback='model')}-{_safe_id(provider_name, fallback='provider')}"
    target_id = f"{provider_id}/target"
    logical_model = f"openreel-{_safe_id(provider_name, fallback='model')}"
    bases = dict(options.bases)
    if base_url:
        bases.setdefault(options.base_slot, base_url)
    credentials: dict[str, Any] = {}
    if api_key and options.credential_slot:
        credentials[options.credential_slot] = {"value": api_key}

    target_operation = TargetOperation.model_validate(
        {
            "request_schema": options.request_schema,
            "defaults": options.target_defaults,
            "variants": options.variants,
        }
    )
    paths = universal_adapter_protocol_paths()
    config = AdapterConfig(
        protocol_paths=paths,
        providers=(
            ProviderConnection(
                id=provider_id,
                protocol=options.protocol_id,
                bases=bases,
                credentials=credentials,
                headers=options.headers,
                parameters=options.provider_parameters,
                timeout_seconds=options.timeout_seconds,
                poll_interval_seconds=options.poll_interval_seconds,
                poll_max_interval_seconds=options.poll_max_interval_seconds,
                task_timeout_seconds=options.task_timeout_seconds,
                task_success_grace_polls=options.task_success_grace_polls,
                max_concurrency=options.max_concurrency,
            ),
        ),
        targets=(
            ProviderModelTarget(
                id=target_id,
                provider=provider_id,
                remote_model=remote_model,
                kind=kind,
                operations={operation: target_operation},
                parameters=options.target_parameters,
            ),
        ),
        models=(
            LogicalModel(
                id=logical_model,
                kind=kind,
                routes=(Route(target=target_id),),
                defaults=options.logical_defaults,
                metadata={"openreel_provider": provider_name},
            ),
        ),
        allowed_media_roots=(
            Path(settings.PROJECT_ROOT).expanduser().resolve(),
            settings.storage_path_resolved,
        ),
        max_media_bytes=options.max_media_bytes,
    )
    return UniversalAdapterBinding(
        client=AsyncClient(config, transport=transport),
        provider_name=provider_name,
        remote_model=remote_model,
        logical_model=logical_model,
        kind=kind,
        operation=operation,
        options=options,
        cache_key=universal_adapter_cache_key(provider, provider_params),
    )
