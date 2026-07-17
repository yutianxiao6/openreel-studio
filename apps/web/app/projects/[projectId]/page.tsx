"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "next/navigation"
import { ChatPanel } from "@/components/chat/ChatPanel"
import WorkflowCanvas from "@/components/canvas/WorkflowCanvas"
import { useProjectStore, type ProjectRecord } from "@/stores/projectStore"
import { useChatStore } from "@/stores/chatStore"
import { useCanvasStore } from "@/stores/canvasStore"
import { useViewModeStore } from "@/stores/viewModeStore"
import { useBlueprintStore } from "@/stores/blueprintStore"
import { ProjectTitleEditor } from "@/components/project/ProjectTitleEditor"
import { ProjectSessionSidebar } from "@/components/project/ProjectSessionSidebar"
import { WorkspaceViewTabs, workspaceViewDescription, type WorkspaceView } from "@/components/workspace/WorkspaceViewTabs"
import { api } from "@/lib/api"

const LS_KEY = "drama.currentProjectId"
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
  const viewModeReadyRef = useRef(false)

  const switchWorkspaceView = useCallback((next: WorkspaceView) => {
    setWorkspaceView(next)
    setMobilePane("work")
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
    <div className="flex h-[100dvh] min-h-0 flex-col bg-[#0b0d10] text-zinc-100">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-white/10 bg-[#111318] px-3 py-2 shadow-sm shadow-black/30 sm:px-4 sm:py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-2 sm:gap-3">
          <span className="shrink-0 text-sm font-semibold tracking-wide text-zinc-100">OpenReel Studio</span>
          <span className="hidden h-4 w-px bg-white/10 sm:block" />
          <ProjectTitleEditor fallback="加载中..." />
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2 text-xs text-zinc-500">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-400" />
          已连接
        </div>
      </header>

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

      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        <ProjectSessionSidebar />
        <div className={`min-h-0 flex-col border-r border-white/10 md:flex md:w-[420px] md:shrink-0 ${mobilePane === "chat" ? "flex w-full" : "hidden"}`}>
          <ChatPanel />
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
