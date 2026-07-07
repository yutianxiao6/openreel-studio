"""Turn-level tool-call budgets for the Agent Loop.

OpenReel keeps turn accounting in the tool runtime/registry layer instead of
burying every guard inside the prompt. This module classifies a tool call,
records the attempt, and returns a terminal decision when the current turn is no
longer making bounded progress.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_TOTAL_TOOL_CALL_BUDGET = 0

GENERAL_PHASE = "general"


def _budget_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _deferred_target(tool_name: str, tool_args: dict[str, Any] | None) -> str:
    if tool_name != "tool.execute" or not isinstance(tool_args, dict):
        return ""
    raw = tool_args.get("name") or tool_args.get("tool") or tool_args.get("target")
    return str(raw or "").strip()


def classify_tool_phase(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    state: dict[str, Any] | None,
) -> str:
    return GENERAL_PHASE


@dataclass(frozen=True)
class TurnBudgetLimits:
    total_tool_calls: int = DEFAULT_TOTAL_TOOL_CALL_BUDGET

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "TurnBudgetLimits":
        settings = settings or {}
        return cls(
            total_tool_calls=_budget_int(
                settings.get("tool_call_budget"),
                DEFAULT_TOTAL_TOOL_CALL_BUDGET,
            ),
        )


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    phase: str
    reason: str = ""
    count: int = 0
    limit: int = 0
    tool_name: str = ""
    deferred_tool_name: str = ""

    def to_tool_result(self) -> dict[str, Any]:
        if self.reason == "total_tool_call_budget_exceeded":
            scope = "当前 run 的总工具调用"
        else:
            scope = f"{self.phase} 阶段"
        return {
            "ok": False,
            "error": (
                f"{scope}已达到 {self.limit} 次调用预算，"
                "本轮停止，避免继续纠错式重复调用。"
            ),
            "error_kind": "turn_budget_exceeded",
            "stop_reason": "run_budget_exceeded",
            "budget_reason": self.reason,
            "phase": self.phase,
            "tool": self.tool_name,
            "_deferred_tool": self.deferred_tool_name,
            "count": self.count,
            "limit": self.limit,
            "suggested_next": "ask_or_wait_for_user",
            "hint": "请检查 trace 中最近的工具错误和节点状态；必要时简化需求或向用户补问信息后重试。",
        }


@dataclass
class TurnBudgetState:
    limits: TurnBudgetLimits
    total_tool_calls: int = 0
    phase_tool_calls: dict[str, int] = field(default_factory=dict)
    tool_attempts: dict[str, int] = field(default_factory=dict)

    def before_model_call(self, state: dict[str, Any] | None) -> BudgetDecision:
        return BudgetDecision(True, GENERAL_PHASE)

    def before_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        state: dict[str, Any] | None,
    ) -> BudgetDecision:
        phase = classify_tool_phase(tool_name, tool_args, state)
        target = _deferred_target(tool_name, tool_args)
        self.total_tool_calls += 1
        self.phase_tool_calls[phase] = self.phase_tool_calls.get(phase, 0) + 1
        self.tool_attempts[tool_name] = self.tool_attempts.get(tool_name, 0) + 1

        if self.limits.total_tool_calls > 0 and self.total_tool_calls > self.limits.total_tool_calls:
            return BudgetDecision(
                False,
                phase,
                reason="total_tool_call_budget_exceeded",
                count=self.total_tool_calls,
                limit=self.limits.total_tool_calls,
                tool_name=tool_name,
                deferred_tool_name=target,
            )

        phase_count = self.phase_tool_calls[phase]

        return BudgetDecision(
            True,
            phase,
            count=phase_count,
            limit=self.limits.total_tool_calls,
            tool_name=tool_name,
            deferred_tool_name=target,
        )
