"use client"

import type { PlanDoc } from "@/stores/chatStore"
import { MarkdownView } from "@/components/common/MarkdownView"

interface ProposedPlanCardProps {
  plan: PlanDoc
  disabled?: boolean
  onExecute: () => void
}

function planMarkdown(plan: PlanDoc): string {
  return (plan.sections ?? [])
    .filter((section) => section.type === "markdown" && typeof section.content === "string")
    .map((section) => section.content?.trim())
    .filter(Boolean)
    .join("\n\n")
}

export function ProposedPlanCard({ plan, disabled, onExecute }: ProposedPlanCardProps) {
  const markdown = planMarkdown(plan)

  return (
    <div className="studio-action-card mb-2 overflow-hidden rounded-xl border border-sky-300/20 bg-sky-950/20 shadow-sm shadow-black/20">
      <div className="flex items-start justify-between gap-3 border-b border-sky-300/15 px-3 py-3">
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex items-center gap-2">
            <span className="rounded-full border border-sky-300/20 bg-sky-300/10 px-2 py-0.5 text-[10px] text-sky-200">
              Plan Mode
            </span>
          </div>
          <div className="truncate text-sm font-medium text-zinc-100">{plan.title || "计划"}</div>
          {plan.summary ? (
            <p className="mt-1 text-xs leading-relaxed text-zinc-400">{plan.summary}</p>
          ) : null}
        </div>
        <span className="rounded-full border border-sky-300/20 bg-sky-300/10 px-2 py-0.5 text-[10px] text-sky-200">
          只读
        </span>
      </div>
      {markdown ? (
        <div className="px-3 py-3 text-xs leading-relaxed text-zinc-300">
          <MarkdownView>{markdown}</MarkdownView>
        </div>
      ) : null}
      <div className="flex items-center justify-end gap-2 border-t border-sky-300/15 px-3 py-2.5">
        <button
          type="button"
          disabled={disabled}
          onClick={onExecute}
          className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-semibold text-zinc-950 transition-colors hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          执行计划
        </button>
      </div>
    </div>
  )
}
