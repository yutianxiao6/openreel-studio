"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import type { ReactNode } from "react"
import { runProjectMediaOperation } from "@/lib/api"
import { cn } from "@/lib/utils"

export interface VideoEditPanelMediaNode {
  id: string
  title: string
  type: "video" | "audio"
  src: string
}

interface VideoEditPanelProps {
  projectId: string
  nodeId: string
  title: string
  videoUrl: string
  mediaNodes: VideoEditPanelMediaNode[]
  onClose: () => void
  onCommitted: () => Promise<void> | void
}

type BusyAction = "frame" | "tail" | "split" | "trim" | "concat-video" | "concat-audio" | null

function formatTime(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "0:00"
  const total = Math.max(0, Math.floor(value))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, "0")}`
}

function clampTime(value: number, duration: number): number {
  const max = Number.isFinite(duration) && duration > 0 ? duration : Math.max(value, 0)
  if (!Number.isFinite(value)) return 0
  return Math.min(Math.max(value, 0), max)
}

function reorder(ids: string[], draggedId: string, targetId: string): string[] {
  if (draggedId === targetId) return ids
  const from = ids.indexOf(draggedId)
  const to = ids.indexOf(targetId)
  if (from < 0 || to < 0) return ids
  const next = [...ids]
  const [item] = next.splice(from, 1)
  next.splice(to, 0, item)
  return next
}

function OperationButton({
  children,
  disabled,
  active,
  onClick,
}: {
  children: ReactNode
  disabled?: boolean
  active?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "h-9 rounded-md border px-3 text-xs font-semibold transition",
        active
          ? "border-cyan-200/35 bg-cyan-200 text-cyan-950 shadow-[0_10px_24px_rgba(103,232,249,0.18)]"
          : "border-white/10 bg-white/[0.055] text-zinc-100 hover:border-white/18 hover:bg-white/[0.09]",
        disabled && "cursor-not-allowed opacity-45 hover:border-white/10 hover:bg-white/[0.055]",
      )}
    >
      {children}
    </button>
  )
}

function TrackList({
  title,
  items,
  order,
  draggedId,
  onDragStart,
  onDropOn,
  emptyText,
}: {
  title: string
  items: VideoEditPanelMediaNode[]
  order: string[]
  draggedId: string | null
  onDragStart: (id: string | null) => void
  onDropOn: (id: string) => void
  emptyText: string
}) {
  const itemMap = useMemo(() => new Map(items.map((item) => [item.id, item])), [items])
  const orderedItems = order.map((id) => itemMap.get(id)).filter((item): item is VideoEditPanelMediaNode => Boolean(item))
  return (
    <section className="min-h-0 rounded-md border border-white/10 bg-black/18 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">{title}</div>
        <div className="text-[11px] text-zinc-500">{orderedItems.length}</div>
      </div>
      <div className="space-y-2 overflow-y-auto pr-1">
        {orderedItems.length === 0 ? (
          <div className="rounded-md border border-dashed border-white/10 px-3 py-5 text-center text-xs text-zinc-500">
            {emptyText}
          </div>
        ) : (
          orderedItems.map((item, index) => (
            <div
              key={item.id}
              draggable
              onDragStart={(event) => {
                event.dataTransfer.effectAllowed = "move"
                event.dataTransfer.setData("text/plain", item.id)
                onDragStart(item.id)
              }}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault()
                onDropOn(item.id)
              }}
              onDragEnd={() => onDragStart(null)}
              className={cn(
                "group flex cursor-grab items-center gap-2 rounded-md border border-white/10 bg-white/[0.045] p-2 active:cursor-grabbing",
                draggedId === item.id && "border-cyan-200/50 bg-cyan-300/10",
              )}
            >
              <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-black/35 text-[10px] font-semibold text-zinc-400">
                {index + 1}
              </div>
              <div className="h-10 w-14 shrink-0 overflow-hidden rounded border border-white/10 bg-black/35">
                {item.type === "video" ? (
                  <video src={item.src} muted preload="metadata" className="h-full w-full object-cover" />
                ) : (
                  <div className="flex h-full items-end justify-center gap-1 px-2 py-2">
                    {[0.3, 0.72, 0.48, 0.86, 0.52].map((height, barIndex) => (
                      <span
                        key={barIndex}
                        className="w-1 rounded-full bg-amber-200/75"
                        style={{ height: `${height * 100}%` }}
                      />
                    ))}
                  </div>
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium text-zinc-100">{item.title || "未命名"}</div>
                <div className="mt-0.5 text-[10px] text-zinc-500">{item.type === "video" ? "视频片段" : "音频片段"}</div>
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  )
}

export default function VideoEditPanel({
  projectId,
  nodeId,
  title,
  videoUrl,
  mediaNodes,
  onClose,
  onCommitted,
}: VideoEditPanelProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [duration, setDuration] = useState(0)
  const [currentTime, setCurrentTime] = useState(0)
  const [startSeconds, setStartSeconds] = useState(0)
  const [endSeconds, setEndSeconds] = useState(0)
  const [busy, setBusy] = useState<BusyAction>(null)
  const [error, setError] = useState<string | null>(null)
  const [videoOrder, setVideoOrder] = useState<string[]>([])
  const [audioOrder, setAudioOrder] = useState<string[]>([])
  const [draggedVideoId, setDraggedVideoId] = useState<string | null>(null)
  const [draggedAudioId, setDraggedAudioId] = useState<string | null>(null)

  const videoItems = useMemo(() => mediaNodes.filter((item) => item.type === "video" && item.src), [mediaNodes])
  const audioItems = useMemo(() => mediaNodes.filter((item) => item.type === "audio" && item.src), [mediaNodes])

  useEffect(() => {
    setVideoOrder((current) => {
      const ids = videoItems.map((item) => item.id)
      const preserved = current.filter((id) => ids.includes(id))
      const missing = ids.filter((id) => !preserved.includes(id))
      const activeFirst = [nodeId, ...preserved.filter((id) => id !== nodeId), ...missing.filter((id) => id !== nodeId)]
      return activeFirst.filter((id, index, all) => all.indexOf(id) === index && ids.includes(id))
    })
  }, [nodeId, videoItems])

  useEffect(() => {
    setAudioOrder((current) => {
      const ids = audioItems.map((item) => item.id)
      return [...current.filter((id) => ids.includes(id)), ...ids.filter((id) => !current.includes(id))]
    })
  }, [audioItems])

  useEffect(() => {
    setError(null)
    setDuration(0)
    setCurrentTime(0)
    setStartSeconds(0)
    setEndSeconds(0)
  }, [nodeId, videoUrl])

  const runOperation = async (action: BusyAction, input: Parameters<typeof runProjectMediaOperation>[1]) => {
    if (!action || busy) return
    setBusy(action)
    setError(null)
    try {
      await runProjectMediaOperation(projectId, input)
      await onCommitted()
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败")
    } finally {
      setBusy(null)
    }
  }

  const seekTo = (value: number) => {
    const next = clampTime(value, duration)
    setCurrentTime(next)
    if (videoRef.current) videoRef.current.currentTime = next
  }

  const updateStart = (value: number) => {
    const next = Math.min(clampTime(value, duration), Math.max(endSeconds - 0.1, 0))
    setStartSeconds(next)
    if (currentTime < next) seekTo(next)
  }

  const updateEnd = (value: number) => {
    const max = duration || Math.max(value, startSeconds + 0.1)
    const next = Math.max(clampTime(value, max), startSeconds + 0.1)
    setEndSeconds(next)
    if (currentTime > next) seekTo(next)
  }

  const canTrim = endSeconds > startSeconds + 0.05
  const videoConcatIds = videoOrder.filter((id) => videoItems.some((item) => item.id === id))
  const audioConcatIds = audioOrder.filter((id) => audioItems.some((item) => item.id === id))

  return (
    <div
      className="openreel-video-edit-panel nodrag nowheel fixed inset-x-3 bottom-3 top-[10vh] z-[94] overflow-hidden rounded-lg border border-white/10 bg-[#0a0e14]/96 text-zinc-100 shadow-[0_28px_90px_rgba(0,0,0,0.62)] backdrop-blur-xl"
      data-openreel-workflow-ui="true"
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      onWheel={(event) => event.stopPropagation()}
    >
      <div className="flex h-12 items-center justify-between border-b border-white/10 px-4">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-zinc-100">{title || "视频剪辑"}</div>
          <div className="text-[11px] text-zinc-500">轻量时间轴</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="h-8 rounded-md border border-white/10 px-3 text-xs text-zinc-300 transition hover:bg-white/[0.07]"
        >
          关闭
        </button>
      </div>

      <div className="grid h-[calc(100%-3rem)] grid-cols-[260px_minmax(0,1fr)_280px] gap-3 p-3 max-xl:grid-cols-[220px_minmax(0,1fr)_260px] max-lg:grid-cols-1 max-lg:overflow-y-auto">
        <aside className="grid min-h-0 grid-rows-2 gap-3">
          <TrackList
            title="视频轨"
            items={videoItems}
            order={videoOrder}
            draggedId={draggedVideoId}
            onDragStart={setDraggedVideoId}
            onDropOn={(id) => {
              if (!draggedVideoId) return
              setVideoOrder((current) => reorder(current, draggedVideoId, id))
              setDraggedVideoId(null)
            }}
            emptyText="画布上还没有可拼接视频"
          />
          <TrackList
            title="音频轨"
            items={audioItems}
            order={audioOrder}
            draggedId={draggedAudioId}
            onDragStart={setDraggedAudioId}
            onDropOn={(id) => {
              if (!draggedAudioId) return
              setAudioOrder((current) => reorder(current, draggedAudioId, id))
              setDraggedAudioId(null)
            }}
            emptyText="先从视频分离音频"
          />
        </aside>

        <main className="flex min-h-0 flex-col overflow-hidden rounded-md border border-white/10 bg-[#080b11]">
          <div className="min-h-0 flex-1 bg-black">
            <video
              ref={videoRef}
              src={videoUrl}
              controls
              preload="metadata"
              className="h-full w-full bg-black object-contain [color-scheme:dark]"
              onLoadedMetadata={(event) => {
                const nextDuration = Number(event.currentTarget.duration || 0)
                setDuration(Number.isFinite(nextDuration) ? nextDuration : 0)
                setEndSeconds(Number.isFinite(nextDuration) && nextDuration > 0 ? nextDuration : 0)
              }}
              onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
            />
          </div>
          <div className="border-t border-white/10 bg-[#0d121a] p-4">
            <div className="mb-3 flex items-center justify-between text-xs">
              <span className="font-semibold text-zinc-200">{formatTime(currentTime)}</span>
              <span className="text-zinc-500">{formatTime(duration)}</span>
            </div>
            <input
              type="range"
              min={0}
              max={Math.max(duration, 0.1)}
              step={0.01}
              value={clampTime(currentTime, duration)}
              onChange={(event) => seekTo(Number(event.target.value))}
              className="w-full accent-cyan-200"
            />
            <div className="mt-4 rounded-md border border-white/10 bg-black/28 p-3">
              <div className="mb-2 flex items-center justify-between text-[11px] text-zinc-500">
                <span>裁剪范围</span>
                <span>{formatTime(startSeconds)} - {formatTime(endSeconds)}</span>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <label className="space-y-1">
                  <span className="text-[10px] text-zinc-500">起点</span>
                  <input
                    type="range"
                    min={0}
                    max={Math.max(duration, 0.1)}
                    step={0.01}
                    value={clampTime(startSeconds, duration)}
                    onChange={(event) => updateStart(Number(event.target.value))}
                    className="w-full accent-emerald-200"
                  />
                </label>
                <label className="space-y-1">
                  <span className="text-[10px] text-zinc-500">终点</span>
                  <input
                    type="range"
                    min={0}
                    max={Math.max(duration, 0.1)}
                    step={0.01}
                    value={clampTime(endSeconds, duration)}
                    onChange={(event) => updateEnd(Number(event.target.value))}
                    className="w-full accent-rose-200"
                  />
                </label>
              </div>
            </div>
          </div>
        </main>

        <aside className="flex min-h-0 flex-col rounded-md border border-white/10 bg-black/18 p-3">
          <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">操作</div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <OperationButton
              active={busy === "frame"}
              disabled={Boolean(busy)}
              onClick={() => void runOperation("frame", {
                operation: "video.export_frame",
                source_node_id: nodeId,
                frame_mode: "time",
                time_seconds: currentTime,
                title: `${title || "视频"} ${formatTime(currentTime)} 画面`,
              })}
            >
              当前帧
            </OperationButton>
            <OperationButton
              active={busy === "tail"}
              disabled={Boolean(busy)}
              onClick={() => void runOperation("tail", {
                operation: "video.export_frame",
                source_node_id: nodeId,
                frame_mode: "tail",
                title: `${title || "视频"} 尾帧`,
              })}
            >
              尾帧
            </OperationButton>
            <OperationButton
              active={busy === "trim"}
              disabled={Boolean(busy) || !canTrim}
              onClick={() => void runOperation("trim", {
                operation: "video.trim",
                source_node_id: nodeId,
                range: { start_seconds: startSeconds, end_seconds: endSeconds },
                title: `${title || "视频"} 片段`,
              })}
            >
              截片段
            </OperationButton>
            <OperationButton
              active={busy === "split"}
              disabled={Boolean(busy)}
              onClick={() => void runOperation("split", {
                operation: "video.split_tracks",
                source_node_id: nodeId,
              })}
            >
              分音轨
            </OperationButton>
          </div>

          <div className="mt-5 space-y-2">
            <OperationButton
              active={busy === "concat-video"}
              disabled={Boolean(busy) || videoConcatIds.length < 2}
              onClick={() => void runOperation("concat-video", {
                operation: "video.concat",
                source_node_ids: videoConcatIds,
                title: "拼接视频",
              })}
            >
              拼接视频
            </OperationButton>
            <OperationButton
              active={busy === "concat-audio"}
              disabled={Boolean(busy) || audioConcatIds.length < 2}
              onClick={() => void runOperation("concat-audio", {
                operation: "audio.concat",
                source_node_ids: audioConcatIds,
                title: "拼接音频",
              })}
            >
              拼接音频
            </OperationButton>
          </div>

          <div className="mt-5 rounded-md border border-white/10 bg-white/[0.035] p-3 text-[11px] leading-5 text-zinc-400">
            视频和音频拼接按左侧轨道顺序执行。所有结果都会作为新节点放回画布，原节点保持不变。
          </div>
          {error && (
            <div className="mt-3 rounded-md border border-red-400/25 bg-red-950/45 p-3 text-xs leading-5 text-red-100">
              {error}
            </div>
          )}
          {busy && (
            <div className="mt-auto flex items-center gap-2 pt-4 text-xs text-cyan-100">
              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-cyan-100 border-t-transparent" />
              处理中...
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
