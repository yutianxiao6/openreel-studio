"""Strict public Workflow Spec v2 contract and deterministic plan compiler.

The reusable document is the only public workflow truth source. Runtime state,
canvas ids, provider selection, and execution-runner details never belong in
this schema.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


WORKFLOW_SPEC_VERSION = "openreel.workflow.v2"
WORKFLOW_PLAN_VERSION = "openreel.workflow.execution-plan.v2"

_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,100}$"
_STEP_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])steps\.([A-Za-z][A-Za-z0-9_-]*)\.(?:output|outputs)(?:\.[A-Za-z0-9_-]+|\[\])*(?![A-Za-z0-9_])"
)
_INPUT_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])inputs\.([A-Za-z][A-Za-z0-9_-]*)(?:\.[A-Za-z0-9_-]+|\[\])*(?![A-Za-z0-9_])"
)

WorkflowStepKind = Literal[
    "text",
    "object",
    "collection",
    "image",
    "video",
    "audio",
    "loop",
    "plugin",
]
WorkflowInputType = Literal[
    "text",
    "long_text",
    "number",
    "integer",
    "boolean",
    "enum",
    "image",
    "video",
    "audio",
    "json",
]
WorkflowFieldType = Literal["string", "number", "integer", "boolean", "object", "array"]
WorkflowUseRole = Literal["vision", "reference", "source"]
WorkflowConditionOperator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "empty", "not_empty"]


class WorkflowSpecError(ValueError):
    """Raised when a public v2 workflow document is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WorkflowInputOption(_StrictModel):
    value: str | int | float | bool
    label: str = ""


class WorkflowInputSpec(_StrictModel):
    type: WorkflowInputType
    label: str
    description: str = ""
    required: bool = False
    default: Any | None = None
    min: float | None = None
    max: float | None = None
    options: list[WorkflowInputOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_options(self) -> "WorkflowInputSpec":
        if self.type == "enum" and not self.options:
            raise ValueError("enum input requires non-empty options")
        if self.type != "enum" and self.options:
            raise ValueError("options are only valid for enum inputs")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("input min cannot exceed max")
        return self


class WorkflowPrompt(_StrictModel):
    role: str = ""
    task: str
    output: str = ""
    check: str = ""

    @model_validator(mode="after")
    def _validate_task(self) -> "WorkflowPrompt":
        if not self.task.strip():
            raise ValueError("prompt.task cannot be empty")
        return self


class WorkflowOutputField(_StrictModel):
    id: str = Field(pattern=_ID_PATTERN)
    type: WorkflowFieldType = "string"
    label: str = ""
    description: str = ""
    required: bool = False
    fields: list["WorkflowOutputField"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_nested_fields(self) -> "WorkflowOutputField":
        if self.fields and self.type not in {"object", "array"}:
            raise ValueError("nested fields require type object or array")
        return self


class WorkflowOutputSchema(_StrictModel):
    fields: list[WorkflowOutputField]
    allow_extra: bool = False

    @model_validator(mode="after")
    def _validate_fields(self) -> "WorkflowOutputSchema":
        if not self.fields:
            raise ValueError("output schema requires at least one field")
        ids = [field.id for field in self.fields]
        if len(ids) != len(set(ids)):
            raise ValueError("output schema field ids must be unique")
        return self


class WorkflowOutput(_StrictModel):
    canvas: bool | None = None
    schema_: WorkflowOutputSchema | None = Field(default=None, alias="schema")


class WorkflowReferenceSelection(_StrictModel):
    values: str
    by: list[str]

    @model_validator(mode="after")
    def _validate_selection(self) -> "WorkflowReferenceSelection":
        if not self.values.strip():
            raise ValueError("reference selection values path cannot be empty")
        cleaned = [value.strip() for value in self.by if value.strip()]
        if not cleaned:
            raise ValueError("reference selection requires at least one match field")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("reference selection match fields must be unique")
        self.by = cleaned
        return self


class WorkflowUse(_StrictModel):
    from_: str = Field(alias="from", pattern=_ID_PATTERN)
    as_: list[WorkflowUseRole] = Field(alias="as", min_length=1)
    select: WorkflowReferenceSelection | None = None

    @model_validator(mode="after")
    def _validate_roles(self) -> "WorkflowUse":
        if len(self.as_) != len(set(self.as_)):
            raise ValueError("reference roles must be unique")
        if "source" in self.as_ and len(self.as_) != 1:
            raise ValueError("source cannot be combined with vision or reference")
        return self


class WorkflowCondition(_StrictModel):
    path: str
    op: WorkflowConditionOperator
    value: Any | None = None

    @model_validator(mode="after")
    def _validate_value(self) -> "WorkflowCondition":
        if not self.path.strip():
            raise ValueError("condition path cannot be empty")
        if self.op in {"empty", "not_empty"} and self.value is not None:
            raise ValueError(f"condition operator {self.op} does not accept value")
        if self.op not in {"empty", "not_empty"} and self.value is None:
            raise ValueError(f"condition operator {self.op} requires value")
        return self


class WorkflowForeach(_StrictModel):
    items: str | None = None
    count: str | int | None = None
    as_: str = Field(alias="as", pattern=_ID_PATTERN)
    key: str | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> "WorkflowForeach":
        if (self.items is None) == (self.count is None):
            raise ValueError("foreach requires exactly one of items or count")
        if isinstance(self.items, str) and not self.items.strip():
            raise ValueError("foreach.items cannot be empty")
        if isinstance(self.count, str) and not self.count.strip():
            raise ValueError("foreach.count cannot be empty")
        if isinstance(self.count, int) and self.count < 1:
            raise ValueError("foreach.count must be positive")
        return self


class WorkflowPlugin(_StrictModel):
    id: str
    action: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_identity(self) -> "WorkflowPlugin":
        if not self.id.strip() or "." not in self.id:
            raise ValueError("plugin.id must be a namespaced id")
        if not self.action.strip():
            raise ValueError("plugin.action cannot be empty")
        return self


class WorkflowStep(_StrictModel):
    id: str = Field(pattern=_ID_PATTERN)
    title: str
    kind: WorkflowStepKind
    description: str = ""
    needs: list[str] = Field(default_factory=list)
    prompt: WorkflowPrompt | None = None
    output: WorkflowOutput | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    uses: list[WorkflowUse] = Field(default_factory=list)
    when: WorkflowCondition | None = None
    execution: Literal["auto", "manual"] = "auto"
    on_error: Literal["stop", "continue"] = "stop"
    foreach: WorkflowForeach | None = None
    steps: list["WorkflowStep"] = Field(default_factory=list)
    plugin: WorkflowPlugin | None = None
    ui: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_kind_contract(self) -> "WorkflowStep":
        if not self.title.strip():
            raise ValueError("step title cannot be empty")
        if len(self.needs) != len(set(self.needs)):
            raise ValueError("step needs must be unique")
        for dependency in self.needs:
            if not re.fullmatch(_ID_PATTERN, dependency):
                raise ValueError(f"invalid dependency id: {dependency!r}")

        if self.kind == "loop":
            if self.foreach is None or not self.steps:
                raise ValueError("loop step requires foreach and nested steps")
            if self.prompt or self.uses or self.plugin or self.fields or self.output:
                raise ValueError("loop step accepts only control fields and nested steps")
            return self

        if self.foreach is not None or self.steps:
            raise ValueError("foreach and nested steps are only valid for loop steps")

        if self.kind == "plugin":
            if self.plugin is None:
                raise ValueError("plugin step requires plugin configuration")
            if self.prompt is not None:
                raise ValueError("plugin step cannot contain an LLM prompt")
        elif self.plugin is not None:
            raise ValueError("plugin configuration is only valid for plugin steps")

        if self.kind in {"text", "object", "collection"} and self.prompt is None:
            raise ValueError(f"{self.kind} step requires prompt")
        if self.kind in {"object", "collection"}:
            if self.output is None or self.output.schema_ is None:
                raise ValueError(f"{self.kind} step requires output.schema")
        if self.kind == "text" and self.output and self.output.schema_:
            raise ValueError("text step cannot declare structured output schema")

        if self.kind in {"image", "video", "audio"}:
            source_uses = [use for use in self.uses if "source" in use.as_]
            if source_uses:
                if len(source_uses) != 1 or len(self.uses) != 1:
                    raise ValueError("direct source media requires exactly one source use")
                if self.prompt is not None:
                    raise ValueError("direct source media does not accept prompt")
            elif self.prompt is None:
                raise ValueError(f"{self.kind} step requires prompt or one direct source")
        return self


class WorkflowSpec(_StrictModel):
    schema_: Literal[WORKFLOW_SPEC_VERSION] = Field(alias="schema")
    id: str = Field(pattern=_ID_PATTERN)
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    inputs: dict[str, WorkflowInputSpec] = Field(default_factory=dict)
    steps: list[WorkflowStep] = Field(min_length=1)
    ui: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_root(self) -> "WorkflowSpec":
        if not self.title.strip():
            raise ValueError("workflow title cannot be empty")
        for input_id in self.inputs:
            if not re.fullmatch(_ID_PATTERN, input_id):
                raise ValueError(f"invalid input id: {input_id!r}")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("workflow tags must be unique")
        for extension_id in self.extensions:
            if "." not in extension_id:
                raise ValueError("extension keys must be namespaced")
        return self


WorkflowOutputField.model_rebuild()
WorkflowStep.model_rebuild()


def parse_workflow_spec(value: Any) -> WorkflowSpec:
    """Parse only the public v2 contract; no legacy aliases are accepted."""
    try:
        return WorkflowSpec.model_validate(value)
    except ValidationError as exc:
        raise WorkflowSpecError(str(exc)) from exc


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, BaseModel):
        yield from _iter_strings(value.model_dump(by_alias=True, exclude_none=True))
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _path_dependencies(value: Any) -> tuple[set[str], set[str]]:
    step_ids: set[str] = set()
    input_ids: set[str] = set()
    for text in _iter_strings(value):
        step_ids.update(match.group(1) for match in _STEP_PATH_RE.finditer(text))
        input_ids.update(match.group(1) for match in _INPUT_PATH_RE.finditer(text))
    return step_ids, input_ids


def _flatten_steps(
    steps: list[WorkflowStep],
    *,
    parent_loop_id: str | None = None,
) -> tuple[list[WorkflowStep], dict[str, str | None]]:
    flattened: list[WorkflowStep] = []
    parent_by_id: dict[str, str | None] = {}
    for step in steps:
        flattened.append(step)
        parent_by_id[step.id] = parent_loop_id
        if step.kind == "loop":
            children, child_parents = _flatten_steps(step.steps, parent_loop_id=step.id)
            flattened.extend(children)
            parent_by_id.update(child_parents)
    return flattened, parent_by_id


def _step_dependency_payload(step: WorkflowStep) -> dict[str, Any]:
    payload = step.model_dump(by_alias=True, exclude_none=True)
    for key in ("id", "title", "description", "kind", "needs", "output", "execution", "on_error", "ui", "steps"):
        payload.pop(key, None)
    return payload


def _validate_and_derive_dependencies(spec: WorkflowSpec) -> dict[str, list[str]]:
    flattened, parent_by_id = _flatten_steps(spec.steps)
    all_ids = [step.id for step in flattened]
    if len(all_ids) != len(set(all_ids)):
        duplicates = sorted({step_id for step_id in all_ids if all_ids.count(step_id) > 1})
        raise WorkflowSpecError("workflow step ids must be globally unique: " + ", ".join(duplicates))
    known_steps = set(all_ids)
    known_inputs = set(spec.inputs)
    dependencies: dict[str, list[str]] = {}

    for step in flattened:
        derived_steps, referenced_inputs = _path_dependencies(_step_dependency_payload(step))
        derived_steps.update(use.from_ for use in step.uses)
        explicit = set(step.needs)
        parent = parent_by_id.get(step.id)
        if parent:
            explicit.add(parent)
        unknown_steps = sorted((explicit | derived_steps) - known_steps)
        if unknown_steps:
            raise WorkflowSpecError(
                f"step {step.id!r} references unknown steps: {', '.join(unknown_steps)}"
            )
        unknown_inputs = sorted(referenced_inputs - known_inputs)
        if unknown_inputs:
            raise WorkflowSpecError(
                f"step {step.id!r} references unknown inputs: {', '.join(unknown_inputs)}"
            )
        resolved = sorted(explicit | derived_steps)
        if step.id in resolved:
            raise WorkflowSpecError(f"step {step.id!r} cannot depend on itself")
        dependencies[step.id] = resolved

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str, trail: list[str]) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            cycle_start = trail.index(step_id) if step_id in trail else 0
            cycle = [*trail[cycle_start:], step_id]
            raise WorkflowSpecError("workflow dependency cycle: " + " -> ".join(cycle))
        visiting.add(step_id)
        for dependency in dependencies.get(step_id, []):
            visit(dependency, [*trail, step_id])
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in all_ids:
        visit(step_id, [])
    return dependencies


def _compiled_output(step: WorkflowStep) -> dict[str, Any]:
    output = step.output.model_dump(by_alias=True, exclude_none=True) if step.output else {}
    if "canvas" not in output:
        output["canvas"] = step.kind in {"image", "video", "audio"}
    if step.kind == "text":
        output["shape"] = "text"
    elif step.kind == "object":
        output["shape"] = "object"
    elif step.kind == "collection":
        output["shape"] = "array"
    elif step.kind in {"image", "video", "audio"}:
        output["shape"] = step.kind
    elif step.kind == "plugin":
        output["shape"] = "plugin"
    return output


def _compile_step(step: WorkflowStep, dependencies: dict[str, list[str]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": step.id,
        "title": step.title,
        "kind": step.kind,
        "depends_on": dependencies[step.id],
        "execution": step.execution,
        "on_error": step.on_error,
    }
    if step.description:
        result["description"] = step.description
    if step.kind == "loop":
        result["operation"] = "loop"
        result["foreach"] = step.foreach.model_dump(by_alias=True, exclude_none=True) if step.foreach else {}
        result["steps"] = [_compile_step(child, dependencies) for child in step.steps]
    elif step.kind == "plugin":
        result["operation"] = "plugin"
        result["plugin"] = step.plugin.model_dump(by_alias=True, exclude_none=True) if step.plugin else {}
        result["output"] = _compiled_output(step)
    else:
        result["operation"] = "media" if step.kind in {"image", "video", "audio"} else "llm"
        if step.prompt:
            result["prompt"] = step.prompt.model_dump(by_alias=True, exclude_none=True)
        result["output"] = _compiled_output(step)
    for key, value in (
        ("fields", step.fields),
        ("uses", [use.model_dump(by_alias=True, exclude_none=True) for use in step.uses]),
        ("when", step.when.model_dump(by_alias=True, exclude_none=True) if step.when else None),
        ("ui", step.ui),
    ):
        if value not in (None, {}, []):
            result[key] = value
    return result


def _requirements(spec: WorkflowSpec) -> dict[str, Any]:
    flattened, _ = _flatten_steps(spec.steps)
    media = sorted({step.kind for step in flattened if step.kind in {"image", "video", "audio"}})
    plugins = sorted({step.plugin.id for step in flattened if step.plugin is not None})
    needs_llm = any(
        step.kind in {"text", "object", "collection"}
        or (step.kind in {"image", "video", "audio"} and step.prompt is not None)
        for step in flattened
    )
    needs_vision = any("vision" in use.as_ for step in flattened for use in step.uses)
    return {
        "llm": needs_llm,
        "vision": needs_vision,
        "media": media,
        "plugins": plugins,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def compile_workflow_spec(value: Any) -> dict[str, Any]:
    """Compile a v2 public spec into a deterministic private execution plan."""
    spec = value if isinstance(value, WorkflowSpec) else parse_workflow_spec(value)
    dependencies = _validate_and_derive_dependencies(spec)
    plan: dict[str, Any] = {
        "schema": WORKFLOW_PLAN_VERSION,
        "workflow": {
            "id": spec.id,
            "title": spec.title,
            "description": spec.description,
            "tags": spec.tags,
        },
        "inputs": {
            key: item.model_dump(by_alias=True, exclude_none=True)
            for key, item in spec.inputs.items()
        },
        "steps": [_compile_step(step, dependencies) for step in spec.steps],
        "requirements": _requirements(spec),
        "ui": spec.ui,
        "extensions": spec.extensions,
    }
    plan["plan_hash"] = hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()
    return plan


def workflow_spec_payload(value: Any) -> dict[str, Any]:
    """Return the canonical public v2 document without runtime data."""
    spec = value if isinstance(value, WorkflowSpec) else parse_workflow_spec(value)
    return spec.model_dump(by_alias=True, exclude_none=True)
