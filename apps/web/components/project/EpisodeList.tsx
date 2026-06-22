"use client"

import { useMemo, useState } from "react"
import { useProjectStore } from "@/stores/projectStore"

interface EpisodeData {
  id: string
  episode_number: number
  title?: string
  hook?: string
  summary?: string
  cliffhanger?: string
  script?: string
  status?: string
}

function parseEpisodes(project: ReturnType<typeof useProjectStore.getState>["currentProject"]): EpisodeData[] {
  if (!project) return []
  const raw = project.state_json
  let state: Record<string, unknown> | null = null
  if (typeof raw === "string") {
    try {
      state = JSON.parse(raw)
    } catch {
      state = null
    }
  } else if (raw) {
    state = raw as Record<string, unknown>
  }
  if (!state) return []

  const episodesField = state.episodes
  if (Array.isArray(episodesField)) {
    return episodesField as EpisodeData[]
  }
  if (episodesField && typeof episodesField === "object") {
    return Object.entries(episodesField as Record<string, unknown>).map(
      ([key, value]) => ({
        id: `ep-${key}`,
        episode_number: Number(key),
        ...(value as Record<string, unknown>),
      } as EpisodeData),
    )
  }
  return []
}

export function EpisodeList() {
  const project = useProjectStore((s) => s.currentProject)
  const [expanded, setExpanded] = useState<string | null>(null)

  const episodes = useMemo(() => parseEpisodes(project), [project])

  if (!project) {
    return (
      <div className="p-4 text-sm text-gray-500 text-center">暂无分集信息</div>
    )
  }

  if (episodes.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500 text-center">
        <p>还没有分集大纲</p>
        <p className="mt-1 text-xs">在聊天中说&quot;生成大纲&quot;来创建</p>
      </div>
    )
  }

  return (
    <div className="divide-y divide-gray-800">
      {episodes.map((ep) => (
        <div key={ep.id} className="py-3">
          <button
            className="w-full text-left flex items-center justify-between"
            onClick={() => setExpanded(expanded === ep.id ? null : ep.id)}
          >
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono bg-indigo-900/40 text-indigo-300 px-1.5 py-0.5 rounded">
                EP{ep.episode_number}
              </span>
              <span className="text-sm font-medium text-gray-200 truncate max-w-[160px]">
                {ep.title || `第${ep.episode_number}集`}
              </span>
            </div>
            {ep.status && <StatusBadge status={ep.status} />}
          </button>

          {expanded === ep.id && (
            <div className="mt-2 space-y-1.5 pl-2 border-l-2 border-indigo-500/40">
              {ep.summary && (
                <div>
                  <span className="text-xs font-medium text-gray-500">简介：</span>
                  <p className="text-xs text-gray-400 line-clamp-3">{ep.summary}</p>
                </div>
              )}
              {ep.cliffhanger && (
                <div>
                  <span className="text-xs font-medium text-purple-400">悬念：</span>
                  <p className="text-xs text-gray-400">{ep.cliffhanger}</p>
                </div>
              )}
              {ep.script && (
                <div className="mt-1">
                  <span className="text-xs bg-green-900/40 text-green-300 px-1.5 py-0.5 rounded">
                    剧本已生成
                  </span>
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    draft: { label: "草稿", cls: "bg-gray-800 text-gray-400" },
    pending: { label: "待生成", cls: "bg-gray-800 text-gray-400" },
    generating: { label: "生成中", cls: "bg-blue-900/40 text-blue-300" },
    done: { label: "完成", cls: "bg-green-900/40 text-green-300" },
    failed: { label: "失败", cls: "bg-red-900/40 text-red-300" },
  }
  const s = map[status] || { label: status, cls: "bg-gray-800 text-gray-400" }
  return <span className={`text-xs px-1.5 py-0.5 rounded ${s.cls}`}>{s.label}</span>
}
