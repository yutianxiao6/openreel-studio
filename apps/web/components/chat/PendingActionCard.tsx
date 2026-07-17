import type { PendingActionPayload } from "@/stores/chatStore"
import { PendingDecisionActions } from "./PendingDecisionActions"

interface PendingActionCardProps {
  action: PendingActionPayload
  disabled?: boolean
  onResolve: (action: PendingActionPayload, decision: "confirm" | "cancel") => void
}

function riskLabel(risk?: string) {
  if (risk === "destructive") return "高风险"
  if (risk === "high") return "高风险"
  if (risk === "medium") return "需确认"
  if (risk === "low") return "低风险"
  return "待确认"
}

function riskClass(risk?: string) {
  if (risk === "destructive" || risk === "high") {
    return "border-red-400/30 bg-red-500/10 text-red-200"
  }
  if (risk === "medium") return "border-amber-400/30 bg-amber-400/10 text-amber-200"
  return "border-white/10 bg-white/[0.04] text-zinc-300"
}

export function PendingActionCard({ action, disabled, onResolve }: PendingActionCardProps) {
  const status = action.status ?? "pending"
  const locked = disabled || status !== "pending"

  return (
    <div className="studio-action-card mb-2 overflow-hidden rounded-xl border border-white/10 bg-[var(--studio-panel)] shadow-sm shadow-black/20">
      <div className="flex items-start justify-between gap-3 border-b border-white/10 px-3 py-3">
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex items-center gap-2">
            <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-zinc-400">
              操作确认
            </span>
            <span className={`rounded-full border px-2 py-0.5 text-[10px] ${riskClass(action.risk)}`}>
              {riskLabel(action.risk)}
            </span>
          </div>
          <div className="truncate text-sm font-medium text-zinc-100">{action.title}</div>
          {action.description ? (
            <p className="mt-1 text-xs leading-relaxed text-zinc-400">{action.description}</p>
          ) : null}
          {action.reason ? (
            <p className="mt-2 rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-[11px] leading-relaxed text-zinc-500">
              {action.reason}
            </p>
          ) : null}
        </div>
        {status !== "pending" ? (
          <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-zinc-400">
            {status === "confirmed" ? "已确认" : "已取消"}
          </span>
        ) : null}
      </div>

      {status === "pending" ? (
        <PendingDecisionActions
          confirmLabel={action.confirmLabel ?? "确认"}
          cancelLabel={action.cancelLabel ?? "取消"}
          disabled={locked}
          onConfirm={() => onResolve(action, "confirm")}
          onCancel={() => onResolve(action, "cancel")}
        />
      ) : null}
    </div>
  )
}
