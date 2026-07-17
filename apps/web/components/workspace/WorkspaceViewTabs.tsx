"use client"

import { cn } from "@/lib/utils"
import { motion } from "framer-motion"

export type WorkspaceView = "canvas" | "workflow"

interface WorkspaceViewTabsProps {
  value: WorkspaceView
  onChange: (value: WorkspaceView) => void
}

const WORKSPACE_VIEWS: Array<{ value: WorkspaceView; label: string; icon: "canvas" | "workflow" }> = [
  { value: "canvas", label: "创作画布", icon: "canvas" },
  { value: "workflow", label: "流程面板", icon: "workflow" },
]

function ViewIcon({ type }: { type: "canvas" | "workflow" }) {
  if (type === "workflow") {
    return (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true" className="h-3.5 w-3.5">
        <rect x="1.5" y="2" width="4" height="3.5" rx="1" /><rect x="10.5" y="10.5" width="4" height="3.5" rx="1" /><path d="M5.5 3.75h2.2v8.5h2.8" />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true" className="h-3.5 w-3.5">
      <rect x="1.5" y="2" width="13" height="11.5" rx="2" /><path d="M5.5 6.2h5M5.5 9.3h3.2" />
    </svg>
  )
}

export function workspaceViewDescription(value: WorkspaceView): string {
  return value === "workflow"
    ? "工作流模板、步骤、输入和插件节点在这里编排运行"
    : "任务驱动的 text / image / video / audio 节点实时展示"
}

export function WorkspaceViewTabs({ value, onChange }: WorkspaceViewTabsProps) {
  return (
    <div className="studio-workspace-switcher flex items-center gap-1 rounded-xl border border-white/[0.08] bg-black/30 p-1 shadow-inner shadow-black/20 backdrop-blur-md">
      {WORKSPACE_VIEWS.map((item) => (
        <button
          key={item.value}
          type="button"
          onClick={() => onChange(item.value)}
          aria-pressed={value === item.value}
          data-workspace-view={item.value}
          className={cn(
            "studio-workspace-switcher-button relative flex h-7 items-center gap-1.5 rounded-lg px-3 text-[11px] font-medium transition-colors",
            value === item.value
              ? "text-white"
              : "text-zinc-400 hover:bg-white/[0.08] hover:text-zinc-100",
          )}
        >
          {value === item.value && (
            <motion.span
              layoutId="openreel-workspace-active-tab"
              className="absolute inset-0 rounded-lg border border-violet-300/20 bg-gradient-to-r from-violet-500/80 to-cyan-400/65 shadow-[0_8px_20px_rgba(76,61,190,0.24),inset_0_1px_rgba(255,255,255,0.2)]"
              transition={{ type: "spring", stiffness: 420, damping: 32 }}
            />
          )}
          <span className="relative z-10"><ViewIcon type={item.icon} /></span>
          <span className="relative z-10">{item.label}</span>
        </button>
      ))}
    </div>
  )
}
