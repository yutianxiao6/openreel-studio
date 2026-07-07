"use client"

import { cn } from "@/lib/utils"

export type WorkspaceView = "canvas" | "workflow"

interface WorkspaceViewTabsProps {
  value: WorkspaceView
  onChange: (value: WorkspaceView) => void
}

const WORKSPACE_VIEWS: Array<{ value: WorkspaceView; label: string }> = [
  { value: "canvas", label: "创作画布" },
  { value: "workflow", label: "流程面板" },
]

export function workspaceViewDescription(value: WorkspaceView): string {
  return value === "workflow"
    ? "工作流模板、步骤、输入和插件节点在这里编排运行"
    : "任务驱动的 text / image / video / audio 节点实时展示"
}

export function WorkspaceViewTabs({ value, onChange }: WorkspaceViewTabsProps) {
  return (
    <div className="flex items-center gap-1 rounded-md border border-white/10 bg-black/24 p-1">
      {WORKSPACE_VIEWS.map((item) => (
        <button
          key={item.value}
          type="button"
          onClick={() => onChange(item.value)}
          className={cn(
            "h-7 rounded px-3 text-xs font-medium transition-colors",
            value === item.value
              ? "bg-zinc-100 text-zinc-950"
              : "text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100",
          )}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}
