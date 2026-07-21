"""OpenReel host bridge for Universal Model Adapter media backends."""

from __future__ import annotations

import asyncio
import base64
import inspect
import mimetypes
import shutil
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from universal_model_adapter import (
    AudioOutput,
    FileOutput,
    ImageOutput,
    InvocationHandle,
    InvocationResult,
    MediaInput,
    VideoOutput,
)

from app.config import settings
from app.services.universal_adapter_config import (
    UniversalAdapterBinding,
    create_universal_adapter_binding,
    universal_adapter_cache_key,
)


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
_TERMINAL_EVENT_TYPES = {
    "invocation.completed",
    "invocation.failed",
    "invocation.cancelled",
}
_INTERNAL_EXTRA_KEYS = {
    "uma",
    "_poll_interval_seconds",
    "_poll_timeout_seconds",
    "public_base_url",
    "image_transport",
}


@dataclass
class _AdapterJob:
    binding: UniversalAdapterBinding
    handle: InvocationHandle
    project_id: str
    save_locally: bool
    last_sequence: int = 0
    result: dict[str, Any] | None = None
    poll_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _without_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _mapped_values(
    values: Mapping[str, Any],
    mapping: Mapping[str, str],
) -> dict[str, Any]:
    return {mapping.get(key, key): value for key, value in values.items() if value is not None}


def _media_source(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("data:"):
        return {"type": "data_url", "data_url": text}
    if text.startswith(("http://", "https://")):
        return {"type": "url", "url": text}
    path = Path(text).expanduser()
    if path.exists():
        return {"type": "path", "path": path.resolve()}
    raise ValueError(
        "UMA media input must be a data URL, HTTP(S) URL, or readable local path; "
        f"received {text[:120]!r}"
    )


def _media_inputs(
    binding: UniversalAdapterBinding,
    values: list[tuple[str, str]],
) -> list[MediaInput]:
    if not values:
        return []
    accepted = set(binding.options.accepted_media_roles)
    roles = {role for role, _ in values}
    if not accepted:
        raise ValueError(
            "this UMA target does not declare accepted_media_roles; refusing to drop media inputs"
        )
    unsupported = sorted(roles - accepted)
    if unsupported:
        raise ValueError("UMA target does not accept media roles: " + ", ".join(unsupported))
    return [
        MediaInput(
            id=f"{role}-{index + 1}",
            role=role,
            source=_media_source(value),
        )
        for index, (role, value) in enumerate(values)
    ]


def _request_payload(
    binding: UniversalAdapterBinding,
    *,
    input_values: Mapping[str, Any],
    parameter_values: Mapping[str, Any],
    extra: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    options = binding.options
    request_input = {
        **options.static_input,
        **_mapped_values(_without_none(input_values), options.input_map),
    }
    parameters = {
        **options.static_parameters,
        **_mapped_values(_without_none(parameter_values), options.parameter_map),
    }
    if options.pass_extra_parameters:
        for key, value in (extra or {}).items():
            if key in _INTERNAL_EXTRA_KEYS or key.startswith("_") or value is None:
                continue
            parameters[options.parameter_map.get(key, key)] = value
    return request_input, parameters


def _result_error(result: InvocationResult) -> dict[str, Any]:
    error = result.error
    route = result.route
    return {
        "ok": False,
        "status": result.status,
        "error": error.message if error else "Universal Model Adapter invocation failed",
        "error_kind": error.code if error else "adapter_failed",
        "provider_msg": error.provider_code if error else None,
        "http_code": error.provider_status if error else None,
        "error_stage": error.stage if error else None,
        "error_field_path": error.field_path if error else None,
        "retryable": bool(error.retryable) if error else False,
        "provider_request_id": route.provider_request_id if route else None,
        "adapter_route": route.model_dump(mode="json") if route else None,
    }


def _result_metadata(
    binding: UniversalAdapterBinding,
    result: InvocationResult,
) -> dict[str, Any]:
    route = result.route
    return {
        "provider": binding.provider_name,
        "model": binding.remote_model,
        "usage": result.usage.model_dump(mode="json") if result.usage else None,
        "provider_request_id": route.provider_request_id if route else None,
        "adapter_route": route.model_dump(mode="json") if route else None,
        "adapter_invocation_id": result.id,
    }


def _image_result(
    binding: UniversalAdapterBinding,
    result: InvocationResult,
) -> dict[str, Any]:
    if not result.succeeded:
        return {**_result_error(result), **_result_metadata(binding, result)}
    images: list[dict[str, Any]] = []
    for output in result.outputs:
        if not isinstance(output, ImageOutput):
            continue
        item: dict[str, Any] = {
            "url": output.url,
            "width": output.width,
            "height": output.height,
            "mime_type": output.mime_type,
            "seed": output.seed,
        }
        if output.data is not None:
            item["b64"] = base64.b64encode(output.data).decode("ascii")
        elif output.path is not None:
            try:
                item["b64"] = base64.b64encode(output.path.read_bytes()).decode("ascii")
            except OSError as exc:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"cannot read UMA image output: {exc}",
                    "error_kind": "artifact_read_failed",
                    **_result_metadata(binding, result),
                }
        images.append(item)
    if not images:
        return {
            "ok": False,
            "status": "failed",
            "error": "UMA image invocation completed without an image output",
            "error_kind": "protocol_mismatch",
            **_result_metadata(binding, result),
        }
    return {
        "ok": True,
        "status": "completed",
        "images": images,
        **_result_metadata(binding, result),
    }


def _suffix_for_output(kind: str, output: VideoOutput | AudioOutput | FileOutput) -> str:
    suffix = Path(urlparse(output.url or "").path).suffix.lower()
    if suffix and len(suffix) <= 10:
        return suffix
    guessed = mimetypes.guess_extension(str(output.mime_type or "").split(";", 1)[0].strip())
    if guessed:
        return guessed
    return ".mp4" if kind == "video" else ".mp3" if kind == "audio" else ".bin"


class UniversalAdapterService:
    """Long-lived UMA clients and in-process job handles owned by OpenReel."""

    def __init__(self, *, download_client: httpx.AsyncClient | None = None) -> None:
        self._bindings: dict[str, UniversalAdapterBinding] = {}
        self._jobs: OrderedDict[str, _AdapterJob] = OrderedDict()
        self._download_client = download_client or httpx.AsyncClient(
            timeout=httpx.Timeout(600, connect=60),
            follow_redirects=True,
        )
        self._owns_download_client = download_client is None

    async def _binding(
        self,
        provider: Any,
        provider_params: dict[str, Any],
    ) -> UniversalAdapterBinding:
        key = universal_adapter_cache_key(provider, provider_params)
        existing = self._bindings.get(key)
        if existing is not None:
            return existing
        binding = create_universal_adapter_binding(provider, provider_params)
        self._bindings[key] = binding
        return binding

    async def generate_image(
        self,
        *,
        provider: Any,
        provider_params: dict[str, Any],
        project_id: str,
        prompt: str,
        negative_prompt: str | None,
        size: str | None,
        quality: str | None,
        count: int,
        reference_images: list[str] | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            binding = await self._binding(provider, provider_params)
            request_input, parameters = _request_payload(
                binding,
                input_values={"prompt": prompt},
                parameter_values={
                    "negative_prompt": negative_prompt,
                    "size": size,
                    "quality": quality,
                    "count": count,
                },
                extra=extra,
            )
            media = _media_inputs(
                binding,
                [("reference_image", value) for value in (reference_images or [])],
            )
            result = await binding.client.images.invoke(
                operation=binding.operation,
                model=binding.logical_model,
                input=request_input,
                parameters=parameters,
                media=media,
                metadata={"project_id": project_id, "openreel_provider": binding.provider_name},
            )
            return _image_result(binding, result)
        except Exception as exc:
            return self._configuration_failure(provider, exc)

    async def inspect_provider(
        self,
        *,
        provider: Any,
        provider_params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            binding = await self._binding(provider, provider_params)
            binding.client.inspect_model(binding.logical_model)
            return {
                "ok": True,
                "provider": binding.provider_name,
                "model": binding.remote_model,
                "adapter": "universal_adapter",
                "protocol_id": binding.options.protocol_id,
                "operation": binding.operation,
                "accepted_media_roles": list(binding.options.accepted_media_roles),
                "check": "configuration_only",
                "adapter_resume_supported": False,
            }
        except Exception as exc:
            return self._configuration_failure(provider, exc)

    async def submit_video(
        self,
        *,
        provider: Any,
        provider_params: dict[str, Any],
        project_id: str,
        prompt: str,
        first_frame_url: str | None,
        last_frame_url: str | None,
        duration_seconds: int,
        reference_images: list[str] | None,
        extra: dict[str, Any] | None,
        save_locally: bool,
        wait_for_completion: bool,
    ) -> dict[str, Any]:
        input_values = {"prompt": prompt}
        parameters = {
            "duration_seconds": duration_seconds,
            "aspect_ratio": (extra or {}).get("aspect_ratio"),
            "resolution": (extra or {}).get("resolution"),
        }
        media_values = []
        if first_frame_url:
            media_values.append(("first_frame", first_frame_url))
        if last_frame_url:
            media_values.append(("last_frame", last_frame_url))
        media_values.extend(("reference_image", value) for value in (reference_images or []))
        return await self._submit_media(
            provider=provider,
            provider_params=provider_params,
            project_id=project_id,
            kind="video",
            input_values=input_values,
            parameter_values=parameters,
            media_values=media_values,
            extra=extra,
            save_locally=save_locally,
            wait_for_completion=wait_for_completion,
        )

    async def submit_audio(
        self,
        *,
        provider: Any,
        provider_params: dict[str, Any],
        project_id: str,
        prompt: str,
        title: str | None,
        style: str | None,
        instrumental: bool | None,
        extra: dict[str, Any] | None,
        save_locally: bool,
        wait_for_completion: bool,
    ) -> dict[str, Any]:
        operation = ""
        try:
            binding = await self._binding(provider, provider_params)
            operation = binding.operation
        except Exception as exc:
            return self._configuration_failure(provider, exc)
        input_values = {
            "prompt": prompt,
            "text": prompt if operation == "audio.speech" else None,
            "title": title,
            "style": style,
            "instrumental": instrumental,
        }
        return await self._submit_media(
            provider=provider,
            provider_params=provider_params,
            project_id=project_id,
            kind="audio",
            input_values=input_values,
            parameter_values={
                "voice": (extra or {}).get("voice"),
                "format": (extra or {}).get("format") or (extra or {}).get("audio_format"),
                "duration_seconds": (extra or {}).get("duration_seconds"),
            },
            media_values=[],
            extra=extra,
            save_locally=save_locally,
            wait_for_completion=wait_for_completion,
            binding=binding,
        )

    async def _submit_media(
        self,
        *,
        provider: Any,
        provider_params: dict[str, Any],
        project_id: str,
        kind: str,
        input_values: Mapping[str, Any],
        parameter_values: Mapping[str, Any],
        media_values: list[tuple[str, str]],
        extra: dict[str, Any] | None,
        save_locally: bool,
        wait_for_completion: bool,
        binding: UniversalAdapterBinding | None = None,
    ) -> dict[str, Any]:
        try:
            binding = binding or await self._binding(provider, provider_params)
            request_input, parameters = _request_payload(
                binding,
                input_values=input_values,
                parameter_values=parameter_values,
                extra=extra,
            )
            media = _media_inputs(binding, media_values)
            backend = binding.client.backends.for_kind(kind)
            handle = await backend.submit(
                operation=binding.operation,
                model=binding.logical_model,
                input=request_input,
                parameters=parameters,
                media=media,
                metadata={"project_id": project_id, "openreel_provider": binding.provider_name},
            )
            job = _AdapterJob(
                binding=binding,
                handle=handle,
                project_id=project_id,
                save_locally=save_locally,
            )
            self._jobs[handle.id] = job
            self._jobs.move_to_end(handle.id)
            while len(self._jobs) > 256:
                oldest_id, oldest = next(iter(self._jobs.items()))
                if not (await oldest.handle.status()).terminal:
                    break
                self._jobs.pop(oldest_id, None)
            if wait_for_completion:
                return await self.poll(
                    provider=provider,
                    job_id=handle.id,
                    kind=kind,
                )
            snapshot = await handle.snapshot()
            route = snapshot.route
            return {
                "ok": True,
                "status": snapshot.status.value,
                "job_id": handle.id,
                "provider_task_id": snapshot.provider_task_id,
                "provider": binding.provider_name,
                "model": binding.remote_model,
                "progress": snapshot.progress,
                "adapter_route": route.model_dump(mode="json") if route else None,
                "adapter_resume_supported": False,
            }
        except Exception as exc:
            return self._configuration_failure(provider, exc)

    async def poll(
        self,
        *,
        provider: Any,
        job_id: str,
        kind: str,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {
                "ok": False,
                "status": "failed",
                "error": (
                    "UMA invocation is not available in this process; restart recovery is not "
                    "supported for universal_adapter jobs yet"
                ),
                "error_kind": "adapter_job_unavailable",
                "provider": str(getattr(provider, "name", "") or ""),
                "job_id": job_id,
                "adapter_resume_supported": False,
            }
        if job.binding.kind != kind:
            return {
                "ok": False,
                "status": "failed",
                "error": f"UMA job {job_id!r} belongs to {job.binding.kind!r}, not {kind!r}",
                "error_kind": "adapter_job_kind_mismatch",
                "job_id": job_id,
            }
        async with job.poll_lock:
            if job.result is not None:
                return dict(job.result)
            async for event in job.handle.events(after_sequence=job.last_sequence):
                job.last_sequence = event.sequence
                if progress_callback is not None:
                    update = self._progress_update(event, job_id)
                    if update is not None:
                        maybe_awaitable = progress_callback(update)
                        if inspect.isawaitable(maybe_awaitable):
                            await maybe_awaitable
                if event.type in _TERMINAL_EVENT_TYPES:
                    break
            result = await job.handle.result()
            mapped = await self._media_result(job, result)
            mapped["job_id"] = job_id
            mapped["adapter_resume_supported"] = False
            job.result = mapped
            return dict(mapped)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {
                "ok": False,
                "status": "failed",
                "error": "UMA invocation is not available in this process",
                "error_kind": "adapter_job_unavailable",
                "job_id": job_id,
            }
        result = await job.handle.cancel()
        mapped = await self._media_result(job, result)
        mapped["job_id"] = job_id
        job.result = mapped
        return dict(mapped)

    @staticmethod
    def _progress_update(event: Any, job_id: str) -> dict[str, Any] | None:
        if event.type == "progress":
            return {
                "job_id": job_id,
                "status": "running",
                "progress": event.value,
                "stage": event.stage,
                "updated_at": event.created_at.isoformat(),
            }
        if event.type == "stage.changed":
            return {
                "job_id": job_id,
                "status": event.status.value,
                "stage": event.stage,
                "updated_at": event.created_at.isoformat(),
            }
        return None

    async def _media_result(
        self,
        job: _AdapterJob,
        result: InvocationResult,
    ) -> dict[str, Any]:
        binding = job.binding
        if not result.succeeded:
            return {**_result_error(result), **_result_metadata(binding, result)}
        expected_type = VideoOutput if binding.kind == "video" else AudioOutput
        outputs = [output for output in result.outputs if isinstance(output, expected_type)]
        if not outputs:
            return {
                "ok": False,
                "status": "failed",
                "error": f"UMA {binding.kind} invocation completed without a {binding.kind} output",
                "error_kind": "protocol_mismatch",
                **_result_metadata(binding, result),
            }
        materialized = [
            await self._materialize_output(
                project_id=job.project_id,
                kind=binding.kind,
                output=output,
                save_locally=job.save_locally,
                limit=binding.options.max_output_bytes,
            )
            for output in outputs
        ]
        primary = materialized[0]
        response = {
            "ok": True,
            "status": "completed",
            "url": primary.get("url"),
            "local_url": primary.get("local_url"),
            "local_path": primary.get("local_path"),
            "remote_url": primary.get("remote_url"),
            "mime_type": primary.get("mime_type"),
            "duration": getattr(outputs[0], "duration_seconds", None),
            **_result_metadata(binding, result),
        }
        if binding.kind == "audio":
            response["audios"] = materialized
        return response

    async def _materialize_output(
        self,
        *,
        project_id: str,
        kind: str,
        output: VideoOutput | AudioOutput,
        save_locally: bool,
        limit: int,
    ) -> dict[str, Any]:
        remote_url = output.url
        base = {
            "url": remote_url or (str(output.path) if output.path else None),
            "remote_url": remote_url,
            "local_url": None,
            "local_path": str(output.path) if output.path else None,
            "mime_type": output.mime_type,
            "duration": output.duration_seconds,
        }
        if not save_locally:
            return base

        suffix = _suffix_for_output(kind, output)
        directory_name = "generated_videos" if kind == "video" else "generated_audio"
        destination_dir = settings.storage_path_resolved / project_id / directory_name
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{uuid.uuid4().hex[:12]}{suffix}"
        try:
            if output.data is not None:
                if len(output.data) > limit:
                    raise ValueError(f"UMA output exceeds configured {limit}-byte limit")
                destination.write_bytes(output.data)
            elif output.path is not None:
                source = output.path.expanduser().resolve()
                if source.stat().st_size > limit:
                    raise ValueError(f"UMA output exceeds configured {limit}-byte limit")
                shutil.copyfile(source, destination)
            elif remote_url:
                await self._download(remote_url, destination, limit=limit)
            else:
                raise ValueError("UMA media output has no materializable location")
        except Exception as exc:
            destination.unlink(missing_ok=True)
            return {**base, "download_error": str(exc)}
        local_url = f"/api/media/{project_id}/{directory_name}/{destination.name}"
        return {
            **base,
            "url": local_url,
            "local_url": local_url,
            "local_path": str(destination),
        }

    async def _download(self, url: str, destination: Path, *, limit: int) -> None:
        received = 0
        async with self._download_client.stream("GET", url) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > limit:
                raise ValueError(f"UMA output exceeds configured {limit}-byte limit")
            with destination.open("wb") as file:
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > limit:
                        file.close()
                        destination.unlink(missing_ok=True)
                        raise ValueError(f"UMA output exceeds configured {limit}-byte limit")
                    file.write(chunk)

    @staticmethod
    def _configuration_failure(provider: Any, exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "error_kind": "adapter_configuration_error",
            "provider": str(getattr(provider, "name", "") or ""),
            "model": str(getattr(provider, "model_name", "") or ""),
        }

    async def aclose(self) -> None:
        for job in self._jobs.values():
            if not (await job.handle.status()).terminal:
                await job.handle.cancel()
        for binding in self._bindings.values():
            await binding.client.aclose()
        self._bindings.clear()
        self._jobs.clear()
        if self._owns_download_client:
            await self._download_client.aclose()


universal_adapter_service = UniversalAdapterService()
