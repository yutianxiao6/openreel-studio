"use client"

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react"
import { ChatPanel } from "@/components/chat/ChatPanel"
import WorkflowCanvas from "@/components/canvas/WorkflowCanvas"
import { useProjectStore, type ProjectRecord } from "@/stores/projectStore"
import { useChatStore } from "@/stores/chatStore"
import { useCanvasStore } from "@/stores/canvasStore"
import { useViewModeStore } from "@/stores/viewModeStore"
import { useBlueprintStore } from "@/stores/blueprintStore"
import { SettingsModal } from "@/components/settings/SettingsModal"
import { ProjectTitleEditor } from "@/components/project/ProjectTitleEditor"
import { WorkspaceViewTabs, workspaceViewDescription, type WorkspaceView } from "@/components/workspace/WorkspaceViewTabs"
import { api } from "@/lib/api"

const LS_KEY = "drama.currentProjectId"
const LS_CHAT_WIDTH = "drama.chatWidth"
const CHAT_MIN = 320
const CHAT_MAX = 720
const CHAT_DEFAULT = 460
type MobilePane = "chat" | "work"

async function ensureCurrentProject(): Promise<ProjectRecord> {
  const stored = typeof window !== "undefined" ? localStorage.getItem(LS_KEY) : null
  if (stored) {
    try {
      return (await api.getProject(stored)) as unknown as ProjectRecord
    } catch {
      // fall through to create a new one
    }
  }
  const created = (await api.createProject({
    title: "未命名项目",
    genre: "",
    episode_count: 1,
    budget_level: "low",
  })) as unknown as ProjectRecord
  if (typeof window !== "undefined") {
    localStorage.setItem(LS_KEY, created.id)
  }
  return created
}

export default function HomePage() {
  const { currentProject, setProject } = useProjectStore()
  const [error, setError] = useState<string | null>(null)
  const loadHistory = useChatStore((s) => s.loadHistory)
  const loadNodes = useCanvasStore((s) => s.loadNodes)
  const loadBlueprint = useBlueprintStore((s) => s.load)
  const viewMode = useViewModeStore((s) => s.mode)

  const [chatWidth, setChatWidth] = useState<number>(CHAT_DEFAULT)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [mobilePane, setMobilePane] = useState<MobilePane>("chat")
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>("canvas")
  const draggingRef = useRef(false)
  const viewModeReadyRef = useRef(false)

  const switchWorkspaceView = useCallback((next: WorkspaceView) => {
    setWorkspaceView(next)
    setMobilePane("work")
  }, [])

  useEffect(() => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(LS_CHAT_WIDTH) : null
    if (stored) {
      const n = parseInt(stored, 10)
      if (!Number.isNaN(n) && n >= CHAT_MIN && n <= CHAT_MAX) setChatWidth(n)
    }
  }, [])

  const startDrag = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    draggingRef.current = true
    document.body.style.cursor = "col-resize"
    document.body.style.userSelect = "none"
  }, [])

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!draggingRef.current) return
      const next = Math.max(CHAT_MIN, Math.min(CHAT_MAX, e.clientX))
      setChatWidth(next)
    }
    const onUp = () => {
      if (!draggingRef.current) return
      draggingRef.current = false
      document.body.style.cursor = ""
      document.body.style.userSelect = ""
      if (typeof window !== "undefined") {
        window.localStorage.setItem(LS_CHAT_WIDTH, String(Math.round(chatWidth)))
      }
    }
    window.addEventListener("mousemove", onMove)
    window.addEventListener("mouseup", onUp)
    return () => {
      window.removeEventListener("mousemove", onMove)
      window.removeEventListener("mouseup", onUp)
    }
  }, [chatWidth])

  useEffect(() => {
    let cancelled = false
    ensureCurrentProject()
      .then(async (p) => {
        if (cancelled) return
        setProject(p)
        void loadBlueprint(p.id)
        try {
          const [historyRes, nodesRes] = await Promise.all([
            api.getProjectMessages(p.id),
            api.getProjectNodes(p.id),
          ])
          if (cancelled) return
          if (Array.isArray(historyRes)) {
            loadHistory(historyRes as { id: string; role: string; content: string; created_at: string }[])
          }
          const nr = nodesRes as { nodes?: unknown[]; edges?: unknown[] }
          if (nr.nodes && Array.isArray(nr.nodes)) {
            loadNodes(
              nr.nodes as {
                id: string
                type: string
                title: string
                status: string
                position_x: number
                position_y: number
                version?: number
                supersedes_id?: string | null
                output_json?: string | null
                input_json?: string | null
                model_config_json?: string | null
                surface?: string | null
              }[],
              (nr.edges ?? []) as { id: string; source_node_id: string; target_node_id: string; label?: string | null }[],
            )
          }
        } catch {
          // Non-fatal: history restore failed, user can still chat
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
  }, [setProject, loadBlueprint, loadHistory, loadNodes])

  useEffect(() => {
    if (!viewModeReadyRef.current) {
      viewModeReadyRef.current = true
      return
    }
    setMobilePane("work")
  }, [viewMode])

  return (
    <div className="flex h-[100dvh] min-h-0 flex-col bg-[#0b0d10] text-zinc-100">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-white/10 bg-[#111318] px-3 py-2 shadow-sm shadow-black/30 sm:px-4 sm:py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-2 sm:gap-3">
          <span className="shrink-0 text-sm font-semibold tracking-wide text-zinc-100">OpenReel Studio</span>
          <span className="hidden h-4 w-px bg-white/10 sm:block" />
          <ProjectTitleEditor fallback={error ? `连接失败：${error}` : "准备中…"} />
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2 text-xs text-zinc-500">
          <button
            onClick={() => setSettingsOpen(true)}
            className="rounded-md px-2 py-1 text-[10px] font-semibold tracking-tight text-zinc-400 transition-colors hover:bg-white/10 hover:text-zinc-100"
            title="设置"
            aria-label="打开设置"
          >
            SET
          </button>
          <span className={`w-2 h-2 rounded-full inline-block ${currentProject ? "bg-emerald-400" : "bg-amber-400"}`} />
          {currentProject ? "已连接" : "连接中"}
        </div>
      </header>

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <div className="grid shrink-0 grid-cols-2 gap-1 border-b border-white/10 bg-[#111318] p-1 md:hidden">
        <button
          onClick={() => setMobilePane("chat")}
          className={`rounded-md px-3 py-2 text-xs font-medium transition-colors ${
            mobilePane === "chat" ? "bg-zinc-100 text-zinc-950" : "text-zinc-400 hover:bg-white/10"
          }`}
        >
          聊天
        </button>
        <button
          onClick={() => setMobilePane("work")}
          className={`rounded-md px-3 py-2 text-xs font-medium transition-colors ${
            mobilePane === "work" ? "bg-zinc-100 text-zinc-950" : "text-zinc-400 hover:bg-white/10"
          }`}
        >
          工作区
        </button>
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <div
          className={`min-h-0 flex-col border-r border-white/10 md:flex md:shrink-0 ${
            mobilePane === "chat" ? "flex w-full" : "hidden"
          } md:[width:var(--chat-width)]`}
          style={{ "--chat-width": `${chatWidth}px` } as CSSProperties}
        >
          <ChatPanel />
        </div>
        <div
          onMouseDown={startDrag}
          className="group relative hidden w-1 shrink-0 cursor-col-resize bg-white/10 transition-colors hover:bg-indigo-500/60 active:bg-indigo-500 md:block"
          title="拖动调整聊天框宽度"
        >
          <div className="absolute inset-y-0 -left-1 -right-1" />
        </div>
        <div className={`min-h-0 flex-1 flex-col overflow-hidden md:flex ${mobilePane === "work" ? "flex" : "hidden"}`}>
          <div className="flex shrink-0 items-center gap-1 border-b border-white/10 bg-[#111318]/80 px-3 py-2">
            <WorkspaceViewTabs value={workspaceView} onChange={switchWorkspaceView} />
            <span className="ml-auto hidden truncate text-[10px] text-zinc-600 sm:block">
              {workspaceViewDescription(workspaceView)}
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-hidden">
            <WorkflowCanvas workspaceView={workspaceView} onWorkspaceViewChange={switchWorkspaceView} />
          </div>
        </div>
      </div>
    </div>
  )
}
