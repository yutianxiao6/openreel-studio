"use client"

import { useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import { useProjectStore, type ProjectRecord } from "@/stores/projectStore"

interface ProjectTitleEditorProps {
  fallback?: string
}

export function ProjectTitleEditor({ fallback = "准备中…" }: ProjectTitleEditorProps) {
  const currentProject = useProjectStore((s) => s.currentProject)
  const setCurrentProject = useProjectStore((s) => s.setCurrentProject)
  const updateCurrentProject = useProjectStore((s) => s.updateCurrentProject)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState("")
  const [saving, setSaving] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const title = currentProject?.title || fallback

  useEffect(() => {
    if (!editing) return
    setDraft(currentProject?.title || "")
    requestAnimationFrame(() => inputRef.current?.select())
  }, [currentProject?.title, editing])

  const save = async () => {
    if (saving) return
    if (!currentProject?.id) {
      setEditing(false)
      return
    }
    const nextTitle = draft.trim() || "未命名项目"
    if (nextTitle === currentProject.title) {
      setEditing(false)
      return
    }
    const previous = currentProject
    updateCurrentProject({ title: nextTitle })
    setSaving(true)
    try {
      const updated = await api.updateProject(currentProject.id, { title: nextTitle })
      setCurrentProject(updated as unknown as ProjectRecord)
      setEditing(false)
    } catch {
      setCurrentProject(previous)
    } finally {
      setSaving(false)
    }
  }

  if (!currentProject) {
    return <span className="min-w-0 flex-1 truncate text-xs text-zinc-500 sm:max-w-md">{title}</span>
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={draft}
        disabled={saving}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => void save()}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault()
            void save()
          }
          if (event.key === "Escape") {
            event.preventDefault()
            setDraft(currentProject.title || "")
            setEditing(false)
          }
        }}
        className="min-w-0 flex-1 rounded-lg border border-violet-300/20 bg-violet-400/[0.06] px-2 py-1 text-xs text-zinc-100 outline-none shadow-inner shadow-black/15 focus:border-violet-300/45 sm:max-w-md"
      />
    )
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="group min-w-0 flex-1 truncate rounded-lg px-1.5 py-1 text-left text-[11px] font-medium text-zinc-400 transition-all hover:bg-white/[0.04] hover:text-zinc-100 sm:max-w-md"
      title={currentProject.title}
    >
      {title}
    </button>
  )
}
