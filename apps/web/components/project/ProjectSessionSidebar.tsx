"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { useRouter } from "next/navigation"
import { api } from "@/lib/api"
import { useProjectStore, type ProjectRecord } from "@/stores/projectStore"

const LS_CURRENT_PROJECT = "drama.currentProjectId"

function SidebarIcon({ name }: { name: "menu" | "collapse" | "plus" | "project" | "trash" | "refresh" | "multi" }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="h-4 w-4">
      {name === "menu" && <><path d="M4 5h12M4 10h12M4 15h12" /></>}
      {name === "collapse" && <><path d="M4 5h12M4 10h7M4 15h12" /><path d="m14 8 2 2-2 2" /></>}
      {name === "plus" && <path d="M10 4v12M4 10h12" />}
      {name === "project" && <><path d="M3.5 5.5h5l1.5 1.7h6.5v8.3h-13z" /><path d="M3.5 8h13" /></>}
      {name === "trash" && <><path d="M5.5 6.5h9M8 3.8h4M7 6.5l.6 9h4.8l.6-9" /></>}
      {name === "refresh" && <><path d="M15 6V3.5L12.5 6" /><path d="M15 5.8A6 6 0 1 0 16 12" /></>}
      {name === "multi" && <><rect x="3.5" y="4" width="5" height="5" rx="1" /><rect x="11.5" y="4" width="5" height="5" rx="1" /><rect x="3.5" y="12" width="5" height="5" rx="1" /><path d="m12 14.5 1.5 1.5 3-3.5" /></>}
    </svg>
  )
}

function projectTimeLabel(value?: string): string {
  if (!value) return ""
  const time = new Date(value)
  if (Number.isNaN(time.getTime())) return ""
  const now = new Date()
  if (time.toDateString() === now.toDateString()) {
    return time.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
  }
  if (time.getFullYear() === now.getFullYear()) {
    return time.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })
  }
  return time.toLocaleDateString("zh-CN", { year: "2-digit", month: "numeric", day: "numeric" })
}

function sortedProjects(projects: ProjectRecord[]): ProjectRecord[] {
  return [...projects].sort((left, right) => {
    const rightTime = Date.parse(right.updated_at || right.created_at || "") || 0
    const leftTime = Date.parse(left.updated_at || left.created_at || "") || 0
    return rightTime - leftTime
  })
}

export function ProjectSessionSidebar() {
  const router = useRouter()
  const currentProject = useProjectStore((state) => state.currentProject)
  const projects = useProjectStore((state) => state.projects)
  const setProjects = useProjectStore((state) => state.setProjects)
  const setCurrentProject = useProjectStore((state) => state.setCurrentProject)
  const [expanded, setExpanded] = useState(false)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
  const [multiSelect, setMultiSelect] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [batchDeleteConfirm, setBatchDeleteConfirm] = useState(false)
  const [batchDeleting, setBatchDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const orderedProjects = useMemo(() => sortedProjects(projects), [projects])

  const refreshProjects = useCallback(async () => {
    setLoading(true)
    try {
      const items = await api.listProjects({ compact: true })
      const records = items as ProjectRecord[]
      setProjects(records)
      const availableIds = new Set(records.map((project) => project.id))
      setSelectedIds((current) => new Set([...current].filter((id) => availableIds.has(id))))
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "项目列表加载失败")
    } finally {
      setLoading(false)
    }
  }, [setProjects])

  useEffect(() => {
    void refreshProjects()
    const onProjectsChanged = () => void refreshProjects()
    window.addEventListener("openreel:projects-changed", onProjectsChanged)
    return () => window.removeEventListener("openreel:projects-changed", onProjectsChanged)
  }, [refreshProjects])

  const toggleExpanded = () => {
    setExpanded((current) => {
      if (current) {
        setMultiSelect(false)
        setSelectedIds(new Set())
        setBatchDeleteConfirm(false)
      }
      return !current
    })
  }

  const toggleMultiSelect = () => {
    setMultiSelect((current) => {
      if (current) {
        setSelectedIds(new Set())
        setBatchDeleteConfirm(false)
      }
      return !current
    })
  }

  const toggleProjectSelection = (projectId: string) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(projectId)) next.delete(projectId)
      else next.add(projectId)
      return next
    })
    setBatchDeleteConfirm(false)
  }

  const toggleSelectAll = () => {
    setSelectedIds((current) => {
      if (current.size === orderedProjects.length) return new Set()
      return new Set(orderedProjects.map((project) => project.id))
    })
    setBatchDeleteConfirm(false)
  }

  const navigateToProject = (project: ProjectRecord, replace = false) => {
    window.localStorage.setItem(LS_CURRENT_PROJECT, project.id)
    setCurrentProject(project)
    setPendingDeleteId(null)
    const path = `/projects/${encodeURIComponent(project.id)}`
    if (replace) router.replace(path)
    else router.push(path)
    if (window.innerWidth < 768) {
      setExpanded(false)
    }
  }

  const createProject = async () => {
    if (creating) return
    setCreating(true)
    setError(null)
    try {
      const created = await api.createProject({
        title: "未命名项目",
        genre: "",
        episode_count: 1,
        budget_level: "low",
      }) as unknown as ProjectRecord
      setProjects([created, ...projects.filter((project) => project.id !== created.id)])
      setMultiSelect(false)
      setSelectedIds(new Set())
      setBatchDeleteConfirm(false)
      window.dispatchEvent(new CustomEvent("openreel:projects-changed"))
      navigateToProject(created)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "新建项目失败")
    } finally {
      setCreating(false)
    }
  }

  const deleteSelectedProjects = async () => {
    if (batchDeleting || selectedIds.size === 0) return
    setBatchDeleting(true)
    setError(null)
    const deletedIds = new Set<string>()
    const failedIds = new Set<string>()
    for (const projectId of selectedIds) {
      try {
        await api.deleteProject(projectId)
        deletedIds.add(projectId)
      } catch {
        failedIds.add(projectId)
      }
    }

    try {
      let remaining = projects.filter((project) => !deletedIds.has(project.id))
      if (remaining.length === 0) {
        const created = await api.createProject({
          title: "未命名项目",
          genre: "",
          episode_count: 1,
          budget_level: "low",
        }) as unknown as ProjectRecord
        remaining = [created]
      }
      setProjects(remaining)
      setSelectedIds(failedIds)
      setBatchDeleteConfirm(false)
      if (failedIds.size === 0) setMultiSelect(false)
      if (failedIds.size > 0) setError(`${failedIds.size} 个项目删除失败，请重试`)
      if (currentProject?.id && deletedIds.has(currentProject.id)) {
        navigateToProject(sortedProjects(remaining)[0], true)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除项目后创建新项目失败")
      await refreshProjects()
    } finally {
      setBatchDeleting(false)
    }
  }

  const deleteProject = async (project: ProjectRecord) => {
    if (deletingId) return
    setDeletingId(project.id)
    setError(null)
    try {
      await api.deleteProject(project.id)
      let remaining = projects.filter((item) => item.id !== project.id)
      setPendingDeleteId(null)
      if (currentProject?.id !== project.id) {
        setProjects(remaining)
        return
      }
      if (remaining.length === 0) {
        const created = await api.createProject({
          title: "未命名项目",
          genre: "",
          episode_count: 1,
          budget_level: "low",
        }) as unknown as ProjectRecord
        remaining = [created]
      }
      setProjects(remaining)
      navigateToProject(sortedProjects(remaining)[0], true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除项目失败")
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className={`relative z-40 w-12 shrink-0 transition-[width] duration-200 md:z-auto ${expanded ? "md:w-72" : "md:w-12"}`} data-openreel-project-sidebar="true" data-sidebar-expanded={expanded ? "true" : "false"}>
      {expanded && (
        <button
          type="button"
          className="fixed inset-0 z-40 bg-black/45 md:hidden"
          onClick={toggleExpanded}
          aria-label="收起项目栏"
        />
      )}
      <aside className={`absolute inset-y-0 left-0 z-50 flex h-full flex-col overflow-hidden border-r border-white/10 bg-[#101217] shadow-2xl shadow-black/40 transition-[width] duration-200 md:relative md:shadow-none ${expanded ? "w-72" : "w-12"}`}>
        <div className={`flex h-12 shrink-0 items-center border-b border-white/10 ${expanded ? "justify-between px-3" : "justify-center"}`}>
          {expanded && (
            <div className="min-w-0">
              <div className="text-[11px] font-semibold tracking-[0.08em] text-zinc-200">项目会话</div>
              <div className="mt-0.5 text-[9px] text-zinc-600">{orderedProjects.length} 个项目</div>
            </div>
          )}
          <button
            type="button"
            onClick={toggleExpanded}
            className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition hover:bg-white/10 hover:text-zinc-100"
            title={expanded ? "收起项目栏" : "展开项目栏"}
            aria-label={expanded ? "收起项目栏" : "展开项目栏"}
            aria-expanded={expanded}
          >
            <SidebarIcon name={expanded ? "collapse" : "menu"} />
          </button>
        </div>

        {expanded && (
          <div className="flex shrink-0 gap-1.5 p-2">
            <button
              type="button"
              onClick={() => void createProject()}
              disabled={creating}
              className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border border-white/10 bg-white/[0.035] px-2.5 text-zinc-300 transition hover:border-white/20 hover:bg-white/[0.08] hover:text-white disabled:cursor-wait disabled:opacity-50"
              aria-label="新建项目"
              title="新建项目并立即切换"
              data-openreel-project-create="true"
            >
              <SidebarIcon name="plus" />
              <span className="truncate text-[11px] font-medium">{creating ? "正在新建…" : "新建项目"}</span>
            </button>
            <button
              type="button"
              onClick={toggleMultiSelect}
              className={`flex h-9 items-center gap-1.5 rounded-md border px-2.5 text-[10px] transition ${multiSelect ? "border-[#577c99] bg-[#29445a] text-[#c8e2f4]" : "border-white/10 bg-white/[0.035] text-zinc-500 hover:border-white/20 hover:bg-white/[0.08] hover:text-zinc-200"}`}
              aria-label={multiSelect ? "退出多选" : "多选项目"}
              aria-pressed={multiSelect}
              data-openreel-project-multi-select="true"
            >
              <SidebarIcon name="multi" />
              <span>{multiSelect ? "完成" : "多选"}</span>
            </button>
          </div>
        )}

        {expanded ? (
          <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3" data-openreel-project-session-list="true">
            {loading && orderedProjects.length === 0 ? (
              <div className="px-2 py-6 text-center text-[10px] text-zinc-600">正在读取项目…</div>
            ) : orderedProjects.length === 0 ? (
              <div className="px-2 py-6 text-center text-[10px] leading-5 text-zinc-600">暂无项目<br />点击上方按钮开始</div>
            ) : (
              <div className="space-y-1">
                {orderedProjects.map((project) => {
                  const active = currentProject?.id === project.id
                  const confirming = pendingDeleteId === project.id
                  const deleting = deletingId === project.id
                  const selected = selectedIds.has(project.id)
                  return (
                    <div
                      key={project.id}
                      data-openreel-project-session="true"
                      data-project-id={project.id}
                      data-current-project={active ? "true" : "false"}
                      className={`group rounded-md border transition ${selected ? "border-cyan-400/40 bg-cyan-400/[0.08]" : active ? "border-[#43617b] bg-[#1d2a35]" : "border-transparent bg-transparent hover:border-white/[0.06] hover:bg-white/[0.045]"}`}
                    >
                      <div className="flex items-start gap-1 p-1">
                        <button
                          type="button"
                          onClick={() => multiSelect ? toggleProjectSelection(project.id) : navigateToProject(project)}
                          className="flex min-w-0 flex-1 items-start gap-2 rounded px-1.5 py-1.5 text-left"
                          aria-label={multiSelect ? `${selected ? "取消选择" : "选择"}项目 ${project.title || "未命名项目"}` : `切换到项目 ${project.title || "未命名项目"}`}
                        >
                          <span className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded border ${selected ? "border-cyan-300/60 bg-cyan-400/20 text-cyan-100" : active ? "border-[#577c99] bg-[#29445a] text-[#b7d7ee]" : "border-white/10 bg-white/[0.03] text-zinc-600"}`}>
                            {multiSelect ? <span className="text-[11px] font-semibold">{selected ? "✓" : ""}</span> : <SidebarIcon name="project" />}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className={`block truncate text-[11px] ${active ? "font-medium text-zinc-100" : "text-zinc-400 group-hover:text-zinc-200"}`}>{project.title || "未命名项目"}</span>
                            <span className="mt-0.5 flex items-center gap-1.5 text-[8px] text-zinc-600">
                              {active && <span className="text-[#79a9cc]">当前</span>}
                              <span>{projectTimeLabel(project.updated_at || project.created_at)}</span>
                            </span>
                          </span>
                        </button>
                        {!multiSelect && (
                          <button
                            type="button"
                            onClick={() => setPendingDeleteId(confirming ? null : project.id)}
                            disabled={deleting}
                            className={`mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded text-zinc-600 transition hover:bg-red-500/10 hover:text-red-300 ${confirming ? "bg-red-500/10 text-red-300" : "opacity-0 group-hover:opacity-100 focus:opacity-100"}`}
                            aria-label={`删除项目 ${project.title || "未命名项目"}`}
                            title="删除项目"
                          >
                            <SidebarIcon name="trash" />
                          </button>
                        )}
                      </div>
                      {confirming && (
                        <div className="mx-2 mb-2 flex items-center gap-1.5 border-t border-red-400/10 pt-2" data-openreel-project-delete-confirm="true">
                          <span className="min-w-0 flex-1 truncate text-[9px] text-red-200/80">确认删除？</span>
                          <button type="button" onClick={() => setPendingDeleteId(null)} className="rounded px-2 py-1 text-[9px] text-zinc-500 hover:bg-white/10 hover:text-zinc-200">取消</button>
                          <button type="button" onClick={() => void deleteProject(project)} disabled={deleting} className="rounded bg-red-500/15 px-2 py-1 text-[9px] text-red-200 hover:bg-red-500/25 disabled:opacity-50">{deleting ? "删除中" : "删除"}</button>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        ) : (
          <div className="min-h-0 flex-1" />
        )}

        {expanded && (
          <div className="shrink-0 border-t border-white/10 p-2">
            {error ? <div className="mb-1.5 break-words rounded bg-red-500/10 px-2 py-1.5 text-[9px] leading-4 text-red-200">{error}</div> : null}
            {multiSelect && (
              <div className="mb-1.5 rounded-md border border-white/10 bg-white/[0.025] p-1.5" data-openreel-project-multi-actions="true">
                {batchDeleteConfirm ? (
                  <div className="flex items-center gap-1.5">
                    <span className="min-w-0 flex-1 text-[9px] text-red-200/80">确认删除 {selectedIds.size} 个项目？</span>
                    <button type="button" onClick={() => setBatchDeleteConfirm(false)} className="rounded px-2 py-1 text-[9px] text-zinc-500 hover:bg-white/10 hover:text-zinc-200">取消</button>
                    <button type="button" onClick={() => void deleteSelectedProjects()} disabled={batchDeleting} className="rounded bg-red-500/15 px-2 py-1 text-[9px] text-red-200 hover:bg-red-500/25 disabled:opacity-50">{batchDeleting ? "删除中" : "删除"}</button>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5">
                    <button type="button" onClick={toggleSelectAll} className="rounded px-2 py-1.5 text-[9px] text-zinc-500 hover:bg-white/10 hover:text-zinc-200">{selectedIds.size === orderedProjects.length ? "取消全选" : "全选"}</button>
                    <span className="min-w-0 flex-1 text-right text-[9px] text-zinc-600">已选 {selectedIds.size} 项</span>
                    <button type="button" onClick={() => setBatchDeleteConfirm(true)} disabled={selectedIds.size === 0} className="flex items-center gap-1 rounded bg-red-500/10 px-2 py-1.5 text-[9px] text-red-300 hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-30"><SidebarIcon name="trash" />删除所选</button>
                  </div>
                )}
              </div>
            )}
            <button
              type="button"
              onClick={() => void refreshProjects()}
              disabled={loading}
              className="flex h-8 w-full items-center gap-2 rounded px-2 text-zinc-600 transition hover:bg-white/10 hover:text-zinc-200 disabled:opacity-40"
              aria-label="刷新项目列表"
              title="刷新项目列表"
            >
              <SidebarIcon name="refresh" />
              <span className="text-[10px]">刷新项目列表</span>
            </button>
          </div>
        )}
      </aside>
    </div>
  )
}
