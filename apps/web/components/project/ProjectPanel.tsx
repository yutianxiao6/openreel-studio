"use client"

import { useMemo, useState } from "react"
import { useProjectStore } from "@/stores/projectStore"
import CharacterCard from "./CharacterCard"
import { EpisodeList } from "./EpisodeList"

type Tab = "characters" | "episodes" | "outline"

const TABS: { id: Tab; label: string }[] = [
  { id: "characters", label: "人物" },
  { id: "episodes", label: "剧集" },
  { id: "outline", label: "大纲" },
]

export function ProjectPanel() {
  const project = useProjectStore((s) => s.currentProject)
  const [tab, setTab] = useState<Tab>("characters")

  const state = useMemo(() => {
    if (!project) return null
    const raw = project.state_json
    if (!raw) return null
    if (typeof raw === "string") {
      try {
        return JSON.parse(raw) as Record<string, unknown>
      } catch {
        return null
      }
    }
    return raw as Record<string, unknown>
  }, [project])

  if (!project) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 text-sm">
        <p>暂无项目，请在聊天区创建项目</p>
      </div>
    )
  }

  const characters = (state?.characters as unknown[]) || []
  const outlineEpisodes =
    ((state?.outline as Record<string, unknown> | undefined)?.episodes as unknown[]) || []
  const hasBlueprint = Boolean(
    state?.project_blueprint ||
    state?.pending_blueprint_draft ||
    state?.pending_blueprint_review,
  )
  const metaParts = [
    project.genre || "未设定",
    hasBlueprint && project.episode_count ? `${project.episode_count}集` : "",
    project.format || "",
  ].filter(Boolean)

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gray-950">
      <div className="border-b border-gray-800 px-4 py-3">
        <h2 className="font-semibold text-sm truncate text-gray-100">{project.title}</h2>
        <p className="text-xs text-gray-500 mt-0.5">
          {metaParts.join(" · ")}
        </p>
      </div>

      <div className="flex border-b border-gray-800 px-2 pt-2 gap-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
              tab === t.id
                ? "bg-gray-800 text-gray-100"
                : "text-gray-500 hover:text-gray-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {tab === "characters" && (
          <div className="space-y-3">
            {characters.length > 0 ? (
              characters.map((char, i) => (
                <CharacterCard key={i} character={char as Record<string, unknown>} />
              ))
            ) : (
              <p className="text-xs text-gray-500 text-center py-8">
                暂无人物，通过聊天生成
              </p>
            )}
          </div>
        )}

        {tab === "episodes" && <EpisodeList />}

        {tab === "outline" && (
          <div>
            {outlineEpisodes.length > 0 ? (
              <div className="space-y-2">
                {outlineEpisodes.map((ep, i) => {
                  const e = ep as Record<string, unknown>
                  return (
                    <div
                      key={i}
                      className="rounded-md border border-gray-800 bg-gray-900 p-3 text-xs"
                    >
                      <div className="font-medium text-gray-200">
                        第{String(e.episode_number ?? i + 1)}集：{String(e.title ?? "")}
                      </div>
                      <div className="mt-1 text-gray-500 line-clamp-2">
                        {String(e.summary ?? "")}
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-xs text-gray-500 text-center py-8">
                暂无大纲，通过聊天生成
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
