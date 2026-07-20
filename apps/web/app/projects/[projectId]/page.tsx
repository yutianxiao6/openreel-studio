"use client"

import { useCallback, useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react"
import { useParams } from "next/navigation"
import { ChatPanel } from "@/components/chat/ChatPanel"
import WorkflowCanvas from "@/components/canvas/WorkflowCanvas"
import { useProjectStore, type ProjectRecord } from "@/stores/projectStore"
import { useChatStore } from "@/stores/chatStore"
import { useCanvasStore } from "@/stores/canvasStore"
import { useViewModeStore } from "@/stores/viewModeStore"
import { useBlueprintStore } from "@/stores/blueprintStore"
import { ProjectSessionSidebar } from "@/components/project/ProjectSessionSidebar"
import { WorkspaceViewTabs, workspaceViewDescription, type WorkspaceView } from "@/components/workspace/WorkspaceViewTabs"
import { StudioHeader } from "@/components/workspace/StudioHeader"
import { StudioAtmosphere } from "@/components/workspace/StudioAtmosphere"
import { SettingsModal } from "@/components/settings/SettingsModal"
import { api } from "@/lib/api"

const LS_KEY = "drama.currentProjectId"
const LS_CHAT_WIDTH = "drama.chatWidth"
const CHAT_MIN = 320
const CHAT_MAX = 720
const CHAT_DEFAULT = 420
const WORKSPACE_MIN = 440
type MobilePane = "chat" | "work"

export default function ProjectWorkspacePage() {
  const params = useParams()
  const projectId = params.projectId as string
  const { currentProject, setProject } = useProjectStore()
  const loadHistory = useChatStore((s) => s.loadHistory)
  const loadNodes = useCanvasStore((s) => s.loadNodes)
  const loadBlueprint = useBlueprintStore((s) => s.load)
  const viewMode = useViewModeStore((s) => s.mode)
  const [mobilePane, setMobilePane] = useState<MobilePane>("chat")
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>("canvas")
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [chatWidth, setChatWidth] = useState(CHAT_DEFAULT)
  const viewModeReadyRef = useRef(false)
  const chatPaneRef = useRef<HTMLDivElement | null>(null)
  const chatWidthRef = useRef(CHAT_DEFAULT)
  const resizingChatRef = useRef(false)

  const switchWorkspaceView = useCallback((next: WorkspaceView) => {
    setWorkspaceView(next)
    setMobilePane("work")
  }, [])

  useEffect(() => {
    const stored = window.localStorage.getItem(LS_CHAT_WIDTH)
    const value = stored ? Number.parseInt(stored, 10) : CHAT_DEFAULT
    if (!Number.isFinite(value)) return
    const next = Math.max(CHAT_MIN, Math.min(CHAT_MAX, value))
    chatWidthRef.current = next
    setChatWidth(next)
  }, [])

  const finishChatResize = useCallback((event?: ReactPointerEvent<HTMLDivElement>) => {
    if (!resizingChatRef.current) return
    resizingChatRef.current = false
    if (event?.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    document.body.style.cursor = ""
    document.body.style.userSelect = ""
    window.localStorage.setItem(LS_CHAT_WIDTH, String(Math.round(chatWidthRef.current)))
  }, [])

  const startChatResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    event.stopPropagation()
    resizingChatRef.current = true
    event.currentTarget.setPointerCapture(event.pointerId)
    document.body.style.cursor = "col-resize"
    document.body.style.userSelect = "none"
  }, [])

  const moveChatResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (!resizingChatRef.current) return
    event.preventDefault()
    const chatLeft = chatPaneRef.current?.getBoundingClientRect().left || 0
    const viewportMaximum = Math.max(CHAT_MIN, window.innerWidth - chatLeft - WORKSPACE_MIN)
    const next = Math.max(CHAT_MIN, Math.min(CHAT_MAX, viewportMaximum, event.clientX - chatLeft))
    chatWidthRef.current = next
    setChatWidth(next)
  }, [])

  useEffect(() => () => {
    document.body.style.cursor = ""
    document.body.style.userSelect = ""
  }, [])

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    Promise.all([
      api.getProject(projectId),
      api.getProjectMessages(projectId),
      api.getProjectNodes(projectId),
    ])
      .then(([project, historyRes, nodesRes]) => {
        if (cancelled) return
        setProject(project as unknown as ProjectRecord)
        const record = project as unknown as ProjectRecord
        void loadBlueprint(record.id)
        if (typeof window !== "undefined") {
          window.localStorage.setItem(LS_KEY, projectId)
        }
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
      })
      .catch(console.error)
    return () => {
      cancelled = true
    }
  }, [loadHistory, loadNodes, loadBlueprint, projectId, setProject])

  useEffect(() => {
    if (!viewModeReadyRef.current) {
      viewModeReadyRef.current = true
      return
    }
    setMobilePane("work")
  }, [viewMode])

  return (
    <div className="studio-shell flex h-[100dvh] min-h-0 flex-col text-zinc-100">
      <StudioAtmosphere />
      <StudioHeader connected={Boolean(currentProject)} projectFallback="加载中..." onOpenSettings={() => setSettingsOpen(true)} />
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <div className="studio-mobile-tabs grid shrink-0 grid-cols-2 gap-1 p-1 md:hidden">
        <button
          onClick={() => setMobilePane("chat")}
          className={`rounded-md px-3 py-2 text-xs font-medium transition-colors ${
            mobilePane === "chat" ? "is-active" : "text-zinc-400 hover:bg-white/10"
          }`}
        >
          聊天
        </button>
        <button
          onClick={() => setMobilePane("work")}
          className={`rounded-md px-3 py-2 text-xs font-medium transition-colors ${
            mobilePane === "work" ? "is-active" : "text-zinc-400 hover:bg-white/10"
          }`}
        >
          工作区
        </button>
      </div>

      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        <ProjectSessionSidebar />
        <div
          ref={chatPaneRef}
          className={`studio-chat-pane min-h-0 flex-col md:flex md:shrink-0 ${mobilePane === "chat" ? "flex w-full" : "hidden"} md:[width:var(--chat-width)]`}
          style={{ "--chat-width": `${chatWidth}px` } as CSSProperties}
        >
          <ChatPanel />
        </div>
        <div
          role="separator"
          aria-label="调整聊天区宽度"
          aria-orientation="vertical"
          aria-valuemin={CHAT_MIN}
          aria-valuemax={CHAT_MAX}
          aria-valuenow={Math.round(chatWidth)}
          onPointerDown={startChatResize}
          onPointerMove={moveChatResize}
          onPointerUp={finishChatResize}
          onPointerCancel={finishChatResize}
          onDoubleClick={() => {
            chatWidthRef.current = CHAT_DEFAULT
            setChatWidth(CHAT_DEFAULT)
            window.localStorage.setItem(LS_CHAT_WIDTH, String(CHAT_DEFAULT))
          }}
          className="studio-resizer group relative hidden w-1 shrink-0 touch-none cursor-col-resize md:block"
          title="拖动调整聊天区宽度 · 双击恢复默认"
        >
          <div className="absolute inset-y-0 -left-2 -right-2" />
        </div>
        <div className={`studio-workspace-pane min-h-0 flex-1 flex-col overflow-hidden md:flex ${mobilePane === "work" ? "flex" : "hidden"}`}>
          <div className="studio-workbar flex shrink-0 items-center gap-1 px-3 py-2">
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
