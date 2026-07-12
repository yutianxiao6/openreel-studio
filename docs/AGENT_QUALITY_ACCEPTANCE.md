# Agent quality acceptance

English · [简体中文](./zh-CN/agent-quality.md) · [Documentation home](./README.en.md)

A live or E2E run that passes assertions proves feasibility, not product quality. Real agent validation should grade each dimension as `pass`, `warning`, or `fail` with trace-backed evidence.

## Dimensions

1. **Task completion**: the user's end goal is completed, persisted, and inspectable after the turn.
2. **Task correctness**: outputs, prompts, references, dependencies, templates, and exports satisfy every material user constraint.
3. **Cost control**: LLM calls, tool calls, tokens, cache use, elapsed time, and repeated reads are proportionate to the task.
4. **Flexibility**: nearby inputs, local edits, template reuse, and alternate skills work without hard-coded phrases or template IDs.
5. **User experience**: the agent asks only blocking questions, reports understandable progress, and keeps internal bookkeeping out of normal replies.
6. **Observability and safety**: traces, tool results, node state, token records, artifacts, failures, retries, and destructive confirmations agree.

## Required evidence

A report should include:

- the exact user goal and acceptance criteria;
- final persisted nodes, assets, workflows, or exports;
- relevant tool and trace events;
- token/cache and elapsed-time measurements;
- screenshots for changed user-facing states;
- failure and retry explanations;
- confirmation records for destructive actions.

If completion and correctness pass but cost, flexibility, or user experience remains a warning, the conclusion is:

```text
Feasible, but not yet good enough.
```

Do not label a path production-ready until all dimensions pass or the release explicitly accepts the remaining warnings.
