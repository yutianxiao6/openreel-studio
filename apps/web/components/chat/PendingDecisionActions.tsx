"use client"

import type { ReactNode } from "react"

interface PendingDecisionActionsProps {
  confirmLabel: string
  cancelLabel: string
  disabled?: boolean
  busyLabel?: string
  hint?: string
  extraActions?: ReactNode
  onConfirm: () => void
  onCancel: () => void
}

export function PendingDecisionActions({
  confirmLabel,
  cancelLabel,
  disabled,
  busyLabel = "处理中",
  hint = "确认前不会执行该操作",
  extraActions,
  onConfirm,
  onCancel,
}: PendingDecisionActionsProps) {
  return (
    <div className="flex items-center gap-2 border-t border-white/10 px-3 py-3">
      <button
        onClick={onConfirm}
        disabled={disabled}
        className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-950 transition-colors hover:bg-white disabled:opacity-40"
      >
        {disabled ? busyLabel : confirmLabel}
      </button>
      <button
        onClick={onCancel}
        disabled={disabled}
        className="rounded-md border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-zinc-300 transition-colors hover:bg-white/[0.07] disabled:opacity-40"
      >
        {cancelLabel}
      </button>
      {extraActions}
      {hint ? <span className="ml-auto text-[10px] text-zinc-500">{hint}</span> : null}
    </div>
  )
}
