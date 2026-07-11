"use client"

import { resolveMediaUrl, type ProjectMediaHistoryItem } from "@/lib/api"

export type MediaHistoryFilter = "all" | "text" | "image" | "video" | "audio"

export const MEDIA_HISTORY_LABEL: Record<ProjectMediaHistoryItem["kind"], string> = {
  text: "文本",
  image: "图片",
  video: "视频",
  audio: "音频",
}

function formatMediaHistoryTime(value?: string | null): string {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}

function formatMediaHistorySize(value?: number | null): string {
  if (!value || !Number.isFinite(value) || value <= 0) return ""
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`
  return `${(value / 1024 / 1024).toFixed(value < 10 * 1024 * 1024 ? 1 : 0)} MB`
}

function mediaHistoryMimeType(item: ProjectMediaHistoryItem): string {
  if (item.mime_type) return item.mime_type
  const lower = (item.filename || "").toLowerCase()
  if (lower.endsWith(".webm")) return "video/webm"
  if (lower.endsWith(".mov")) return "video/quicktime"
  if (lower.endsWith(".wav")) return "audio/wav"
  if (lower.endsWith(".m4a")) return "audio/mp4"
  if (lower.endsWith(".aac")) return "audio/aac"
  if (lower.endsWith(".ogg")) return "audio/ogg"
  if (lower.endsWith(".flac")) return "audio/flac"
  if (item.kind === "video") return "video/mp4"
  if (item.kind === "audio") return "audio/mpeg"
  return "image/png"
}

function MediaHistoryPreview({ item }: { item: ProjectMediaHistoryItem }) {
  if (item.kind === "text") {
    return (
      <div className="flex h-full w-full flex-col justify-between bg-zinc-950 p-2 text-zinc-200">
        <span className="text-[10px] font-semibold tracking-[0.18em] text-sky-200">TEXT</span>
        <span className="line-clamp-4 text-[11px] leading-4 text-zinc-300">{item.content || item.prompt || "文本结果"}</span>
      </div>
    )
  }
  const src = resolveMediaUrl(item.url)
  if (item.kind === "image") {
    return <img src={src} alt={item.title || item.filename || ""} className="h-full w-full object-cover" loading="lazy" />
  }
  if (item.kind === "video") {
    return (
      <video muted playsInline preload="metadata" controls={false} className="h-full w-full object-cover">
        <source src={src} type={mediaHistoryMimeType(item)} />
      </video>
    )
  }
  return (
    <div className="flex h-full w-full flex-col justify-center gap-1 bg-zinc-950 p-2 text-zinc-200">
      <span className="text-[10px] font-semibold tracking-[0.18em] text-amber-200">AUDIO</span>
      <audio controls preload="metadata" className="w-full scale-[0.92] origin-left">
        <source src={src} type={mediaHistoryMimeType(item)} />
      </audio>
    </div>
  )
}

export default function MediaHistoryDrawer({
  open,
  items,
  filter,
  loading,
  error,
  restoringId,
  deletingId,
  onToggle,
  onFilterChange,
  onRefresh,
  onRestore,
  onDelete,
}: {
  open: boolean
  items: ProjectMediaHistoryItem[]
  filter: MediaHistoryFilter
  loading: boolean
  error: string | null
  restoringId: string | null
  deletingId: string | null
  onToggle: () => void
  onFilterChange: (filter: MediaHistoryFilter) => void
  onRefresh: () => void
  onRestore: (item: ProjectMediaHistoryItem) => void
  onDelete: (item: ProjectMediaHistoryItem) => void
}) {
  const visibleItems = filter === "all" ? items : items.filter((item) => item.kind === filter)
  const counts = {
    all: items.length,
    text: items.filter((item) => item.kind === "text").length,
    image: items.filter((item) => item.kind === "image").length,
    video: items.filter((item) => item.kind === "video").length,
    audio: items.filter((item) => item.kind === "audio").length,
  }
  const tabs: Array<{ id: MediaHistoryFilter; label: string }> = [
    { id: "all", label: "全部" },
    { id: "text", label: "文本" },
    { id: "image", label: "图片" },
    { id: "video", label: "视频" },
    { id: "audio", label: "音频" },
  ]

  return (
    <>
      <button
        type="button"
        onClick={onToggle}
        className={`absolute right-4 top-4 z-30 rounded-md border px-3 py-2 text-xs font-medium shadow-xl backdrop-blur transition ${
          open
            ? "border-cyan-200/30 bg-cyan-300/12 text-cyan-100"
            : "border-white/10 bg-[#11151d]/92 text-zinc-200 hover:bg-white/[0.08]"
        }`}
      >
        历史 {items.length > 0 ? items.length : ""}
      </button>
      <div
        className={`absolute bottom-0 right-0 top-0 z-40 flex w-[min(390px,calc(100vw-18px))] flex-col border-l border-white/10 bg-[#0d1118]/96 shadow-2xl shadow-black/55 backdrop-blur transition-transform duration-200 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <div>
            <div className="text-sm font-semibold text-zinc-100">生成历史</div>
            <div className="text-[11px] text-zinc-500">当前项目 · {items.length} 条记录</div>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={onRefresh}
              disabled={loading}
              className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-300 hover:bg-white/[0.06] disabled:opacity-50"
            >
              {loading ? "刷新中" : "刷新"}
            </button>
            <button
              type="button"
              onClick={onToggle}
              className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]"
            >
              收起
            </button>
          </div>
        </div>
        <div className="border-b border-white/10 px-3 py-2">
          <div className="grid grid-cols-5 gap-1 rounded-md bg-black/24 p-1">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => onFilterChange(tab.id)}
                className={`rounded px-2 py-1.5 text-[11px] transition ${
                  filter === tab.id
                    ? "bg-zinc-100 text-zinc-950"
                    : "text-zinc-400 hover:bg-white/[0.06] hover:text-zinc-200"
                }`}
              >
                {tab.label} {counts[tab.id] || ""}
              </button>
            ))}
          </div>
        </div>
        {error ? (
          <div className="mx-3 mt-3 rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">
            {error}
          </div>
        ) : null}
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
          {visibleItems.length === 0 ? (
            <div className="mt-14 text-center text-xs text-zinc-500">
              {loading ? "正在读取生成历史..." : "暂无生成历史"}
            </div>
          ) : (
            <div className="space-y-2.5">
              {visibleItems.map((item) => {
                const restoring = restoringId === item.id
                const deleting = deletingId === item.id
                return (
                  <div key={item.id} className="overflow-hidden rounded-lg border border-white/[0.08] bg-white/[0.035]">
                    <div className="flex gap-2.5 p-2.5">
                      <div className="h-20 w-28 shrink-0 overflow-hidden rounded-md bg-black">
                        <MediaHistoryPreview item={item} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="rounded border border-white/10 bg-black/24 px-1.5 py-0.5 text-[10px] text-zinc-300">
                            {MEDIA_HISTORY_LABEL[item.kind]}
                          </span>
                          <span className="truncate text-xs font-medium text-zinc-100">{item.title || item.filename || "文本结果"}</span>
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-zinc-500">
                          <span>{formatMediaHistoryTime(item.created_at) || "未知时间"}</span>
                          {formatMediaHistorySize(item.size) ? <span>{formatMediaHistorySize(item.size)}</span> : null}
                          {item.source_node_title ? <span className="truncate">来自 {item.source_node_title}</span> : null}
                        </div>
                        {item.prompt ? (
                          <div className="mt-1.5 line-clamp-2 text-[11px] leading-relaxed text-zinc-400">
                            {item.prompt}
                          </div>
                        ) : null}
                        {item.kind === "text" && item.content ? (
                          <div className="mt-1.5 line-clamp-3 text-[11px] leading-relaxed text-zinc-200">
                            {item.content}
                          </div>
                        ) : null}
                      </div>
                    </div>
                    <div className="flex justify-end gap-1.5 border-t border-white/[0.06] bg-black/12 px-2.5 py-2">
                      {item.kind === "text" ? (
                        <span className="rounded-md border border-white/10 px-2.5 py-1 text-[11px] text-zinc-400">只读记录</span>
                      ) : (
                        <>
                          <a
                            href={resolveMediaUrl(item.url)}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-md border border-white/10 px-2.5 py-1 text-[11px] text-zinc-300 hover:bg-white/[0.06]"
                          >
                            查看
                          </a>
                          <button
                            type="button"
                            onClick={() => onRestore(item)}
                            disabled={restoring || deleting}
                            className="rounded-md bg-zinc-100 px-2.5 py-1 text-[11px] font-medium text-zinc-950 disabled:opacity-50"
                          >
                            {restoring ? "恢复中" : "恢复"}
                          </button>
                          <button
                            type="button"
                            onClick={() => onDelete(item)}
                            disabled={restoring || deleting}
                            className="rounded-md border border-red-300/20 bg-red-500/10 px-2.5 py-1 text-[11px] text-red-200 hover:bg-red-500/18 disabled:opacity-50"
                          >
                            {deleting ? "删除中" : "删除"}
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </>
  )
}
