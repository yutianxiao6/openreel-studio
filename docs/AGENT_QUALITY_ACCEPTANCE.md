# Agent Quality Acceptance

This document defines how OpenReel Studio evaluates whether a natural-language
Agent path is actually useful. A live or E2E run that merely passes automated
assertions proves feasibility, not product quality.

Every real Agent validation report must score the run as `pass`, `warning`, or
`fail` on these dimensions, with trace-backed evidence.

## 中文摘要

本文定义 OpenReel Studio 如何判断一次自然语言 Agent 路径是否真正好用。
一次 live/E2E 运行只通过自动断言，只能证明“可行”，不能证明“产品体验达标”。

每份真实 Agent 验收报告都必须基于 trace、工具结果、节点状态和产物，从下面六个维度给出
`pass`、`warning` 或 `fail`：

1. 任务完成：用户目标是否端到端完成，并且结果已持久化。
2. 任务正确：结果是否真正满足用户约束和 skill 要求，而不只是表面有流程。
3. 成本控制：LLM 调用、工具调用、token、缓存和耗时是否合理。
4. 灵活性：相近输入、局部修改、模板复用和保存模板是否仍能工作。
5. 用户体验：是否只问阻塞问题，是否隐藏不必要的内部实现细节。
6. 可观测性与安全：trace、prompt dump、tool result、节点状态、产物和确认机制是否可解释。

如果任务完成和任务正确通过，但成本、灵活性或用户体验仍是 warning，结论只能是：
`Feasible, but not yet good enough.`

## Dimensions

1. Task completion

   The user's end goal is completed end to end. The final node, workflow,
   template, asset, generated media, or run result is persisted and can be
   inspected after the chat turn.

2. Task correctness

   The result matches the concrete user request and constraints. The Agent
   selects the intended node or template, avoids unrelated edits, does not
   mutate reusable source templates when asked for a local edit, and does not
   generate media types the user excluded. Correctness includes the actual
   output content, not only the surface flow: the report must compare the final
   node outputs, dependencies, prompts, saved templates, and exported package
   against every material requirement in the selected skill. If the requested
   flow exists but the produced content does not satisfy the skill, the result
   is not correct. Local prompt edits that request a rerun must show evidence
   of a new run using the updated prompt contract, not just a successful
   `node.update` call.

3. Cost control

   The report includes LLM call count, tool execution count, deferred tool
   count, token/cache usage, elapsed time, and repeated-read patterns. A local
   edit with an already clear target should be close to `node.update` plus an
   optional `node.run`; extra state or node reads require a concrete reason.
   Complex workflow setup can use more tools, but the report must explain why
   each discovery or ledger step was necessary.

4. Flexibility

   The path works for nearby variations, not only for the exact test phrase.
   The Agent should handle similar skills, changed inputs, local prompt edits,
   template reuse, and saving a new reusable template without hard-coded
   Chinese keywords, fixed template ids, or single-case shortcuts.

5. User experience

   The Agent asks only blocking questions, gives understandable progress and
   completion messages, and keeps internal ids, runtime bookkeeping, and tool
   implementation details out of the primary user interaction unless they are
   needed for diagnosis.

6. Observability and safety

   The report checks trace events, prompt dumps, tool results, node state,
   exported packages, token/cache records, and generated artifacts together.
   Destructive actions require structured confirmation. Failures, skips, and
   retries must be explainable from trace data.

## Reporting Rule

If task completion and task correctness pass but cost control, flexibility, or
user experience is only a warning, the conclusion is:

```text
Feasible, but not yet good enough.
```

Do not report the path as "good" or "production-quality" until all six
dimensions are pass or the remaining warnings are explicitly accepted for that
release.
