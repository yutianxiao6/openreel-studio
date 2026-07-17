"use client"

import { ProjectTitleEditor } from "@/components/project/ProjectTitleEditor"

interface StudioHeaderProps {
  connected: boolean
  projectFallback: string
  onOpenSettings: () => void
}

function SettingsIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true" className="h-4 w-4">
      <circle cx="10" cy="10" r="2.4" />
      <path d="M16 11.5v-3l-1.8-.5a6.6 6.6 0 0 0-.7-1.6l.9-1.6-2.2-2.1-1.6.9A6.8 6.8 0 0 0 9 3L8.5 1.3h-3L5 3a6.6 6.6 0 0 0-1.6.7l-1.6-.9-2.1 2.1.9 1.6A6.8 6.8 0 0 0 0 8.1l-1.7.5v3l1.7.5a6.8 6.8 0 0 0 .6 1.6l-.9 1.6 2.1 2.1 1.6-.9a6.6 6.6 0 0 0 1.6.7l.5 1.7h3l.5-1.7a6.8 6.8 0 0 0 1.6-.6l1.6.9 2.2-2.1-.9-1.6a6.6 6.6 0 0 0 .5-1.7l1.8-.5Z" transform="translate(3.5 .1) scale(.65)" />
    </svg>
  )
}

export function StudioHeader({ connected, projectFallback, onOpenSettings }: StudioHeaderProps) {
  return (
    <header className="studio-topbar">
      <div className="flex min-w-0 flex-1 items-center gap-2.5 sm:gap-3.5">
        <div className="studio-brand-mark" aria-hidden="true">
          <span className="studio-brand-mark-core">O</span>
        </div>
        <div className="hidden shrink-0 items-baseline gap-1.5 sm:flex">
          <span className="studio-brand-name">OPENREEL</span>
          <span className="studio-brand-edition">STUDIO</span>
        </div>
        <span className="hidden h-5 w-px bg-gradient-to-b from-transparent via-white/15 to-transparent sm:block" />
        <div className="flex min-w-0 items-center gap-2">
          <span className="hidden rounded-full border border-white/[0.08] bg-white/[0.035] px-2 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] text-zinc-500 lg:inline">Project</span>
          <ProjectTitleEditor fallback={projectFallback} />
        </div>
      </div>

      <div className="ml-auto flex shrink-0 items-center gap-2">
        <div className={`studio-connection-pill ${connected ? "is-online" : "is-connecting"}`}>
          <span className="studio-connection-indicator"><span /></span>
          <span className="hidden sm:inline">{connected ? "Agent Online" : "Connecting"}</span>
        </div>
        <button
          type="button"
          onClick={onOpenSettings}
          className="studio-icon-button group"
          title="系统设置"
          aria-label="打开设置"
        >
          <SettingsIcon />
          <span className="hidden text-[10px] font-medium sm:inline">设置</span>
        </button>
      </div>
    </header>
  )
}
