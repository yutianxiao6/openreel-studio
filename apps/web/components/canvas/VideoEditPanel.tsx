"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react"
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
type TimelineTool = "select" | "blade" | "trim"

function formatTime(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "0:00"
  const total = Math.max(0, Math.floor(value))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, "0")}`
}

function formatTimePrecise(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "00:00.00"
  const minutes = Math.floor(value / 60)
  const seconds = value % 60
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(2).padStart(5, "0")}`
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

function timelineTicks(duration: number): number[] {
  const max = Number.isFinite(duration) && duration > 0 ? duration : 30
  const count = 7
  return Array.from({ length: count }, (_, index) => (max / (count - 1)) * index)
}

function waveformBars(seed: string, count = 52): number[] {
  let value = seed.split("").reduce((sum, char) => sum + char.charCodeAt(0), 23)
  return Array.from({ length: count }, () => {
    value = (value * 1664525 + 1013904223) % 4294967296
    return 0.22 + (value / 4294967296) * 0.72
  })
}

function trackInsertOrder(order: string[], id: string, beforeId?: string): string[] {
  const clean = order.filter((item) => item !== id)
  if (!beforeId) return [...clean, id]
  const index = clean.indexOf(beforeId)
  if (index < 0) return [...clean, id]
  return [...clean.slice(0, index), id, ...clean.slice(index)]
}

function videoFrameTimes(duration: number, count: number): number[] {
  if (!Number.isFinite(duration) || duration <= 0) return []
  const safeCount = Math.max(1, count)
  if (safeCount === 1) return [Math.min(duration * 0.5, Math.max(duration - 0.05, 0))]
  return Array.from({ length: safeCount }, (_, index) => {
    const ratio = index / (safeCount - 1)
    return Math.min(Math.max(duration * ratio, 0), Math.max(duration - 0.05, 0))
  })
}

function VideoThumbnailStrip({
  src,
  count = 10,
}: {
  src: string
  count?: number
}) {
  const [frames, setFrames] = useState<string[]>([])
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    setFrames([])
    setFailed(false)
    if (!src) return

    const capture = async () => {
      const video = document.createElement("video")
      video.crossOrigin = "anonymous"
      video.muted = true
      video.preload = "auto"
      video.playsInline = true
      video.src = src
      const canvas = document.createElement("canvas")
      canvas.width = 160
      canvas.height = 90
      const context = canvas.getContext("2d")
      if (!context) throw new Error("canvas unavailable")

      await new Promise<void>((resolve, reject) => {
        video.onloadedmetadata = () => resolve()
        video.onerror = () => reject(new Error("video metadata unavailable"))
      })
      const duration = Number(video.duration || 0)
      const times = videoFrameTimes(duration, count)
      const nextFrames: string[] = []
      for (const time of times) {
        if (cancelled) return
        await new Promise<void>((resolve, reject) => {
          const finish = () => resolve()
          video.onseeked = finish
          video.onerror = () => reject(new Error("video seek unavailable"))
          video.currentTime = time
        })
        context.drawImage(video, 0, 0, canvas.width, canvas.height)
        nextFrames.push(canvas.toDataURL("image/jpeg", 0.72))
      }
      if (!cancelled) setFrames(nextFrames)
    }

    capture().catch(() => {
      if (!cancelled) setFailed(true)
    })
    return () => {
      cancelled = true
    }
  }, [count, src])

  if (frames.length > 0) {
    return (
      <div className="absolute inset-0 flex">
        {frames.map((frame, index) => (
          <img
            key={`${frame.slice(0, 24)}-${index}`}
            src={frame}
            alt=""
            className="h-full min-w-0 flex-1 object-cover opacity-88"
            draggable={false}
          />
        ))}
      </div>
    )
  }

  return (
    <div className="absolute inset-0 grid grid-cols-8 gap-px">
      {Array.from({ length: 8 }, (_, index) => (
        <div key={index} className="relative overflow-hidden bg-cyan-950/45">
          <video
            src={src}
            muted
            preload="metadata"
            className={cn("h-full w-full object-cover opacity-70", failed && "opacity-55")}
          />
          <div className="absolute inset-0 bg-white/[0.025]" />
        </div>
      ))}
    </div>
  )
}

function OperationButton({
  children,
  disabled,
  active,
  tone = "neutral",
  onClick,
}: {
  children: ReactNode
  disabled?: boolean
  active?: boolean
  tone?: "neutral" | "primary"
  onClick: () => void
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 items-center justify-center rounded-md border px-2.5 text-[11px] font-semibold transition",
        tone === "primary"
          ? "border-cyan-200/40 bg-cyan-200 text-cyan-950 shadow-[0_10px_24px_rgba(103,232,249,0.16)] hover:bg-cyan-100"
          : "border-white/10 bg-white/[0.045] text-zinc-200 hover:border-white/18 hover:bg-white/[0.085]",
        active && tone !== "primary" && "border-cyan-200/35 bg-cyan-300/12 text-cyan-100",
        disabled && "cursor-not-allowed opacity-40 hover:border-white/10 hover:bg-white/[0.045]",
      )}
    >
      {children}
    </button>
  )
}

function ToolButton({
  label,
  glyph,
  active,
  onClick,
}: {
  label: string
  glyph: string
  active?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className={cn(
        "flex h-8 items-center justify-center gap-1 rounded-md border px-2.5 text-[11px] font-semibold transition",
        active
          ? "border-cyan-200/40 bg-cyan-300/16 text-cyan-100"
          : "border-white/10 bg-white/[0.035] text-zinc-300 hover:bg-white/[0.08]",
      )}
    >
      <span className="text-[10px] text-zinc-500">{glyph}</span>
      <span>{label}</span>
    </button>
  )
}

function AssetCard({
  item,
  active,
  onInsert,
}: {
  item: VideoEditPanelMediaNode
  active?: boolean
  onInsert: (item: VideoEditPanelMediaNode) => void
}) {
  return (
    <div
      draggable
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "copyMove"
        event.dataTransfer.setData("openreel/media-id", item.id)
        event.dataTransfer.setData("text/plain", item.id)
      }}
      onDoubleClick={() => onInsert(item)}
      className={cn(
        "group overflow-hidden rounded-md border bg-[#111720] transition",
        active ? "border-cyan-200/50 shadow-[0_0_0_1px_rgba(103,232,249,0.18)]" : "border-white/10 hover:border-white/18",
      )}
    >
      <div className="relative aspect-video overflow-hidden bg-black">
        {item.type === "video" ? (
          <video src={item.src} muted preload="metadata" className="h-full w-full object-cover opacity-90 transition group-hover:opacity-100" />
        ) : (
          <div className="flex h-full items-end justify-center gap-1 px-4 py-4">
            {waveformBars(item.id, 14).map((height, index) => (
              <span
                key={index}
                className="w-1.5 rounded-full bg-amber-200/80"
                style={{ height: `${height * 100}%` }}
              />
            ))}
          </div>
        )}
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            onInsert(item)
          }}
          className="absolute right-1.5 top-1.5 flex h-6 w-6 items-center justify-center rounded bg-zinc-100 text-xs font-black text-zinc-950 opacity-0 shadow-lg transition group-hover:opacity-100"
          title="插入轨道"
          aria-label="插入轨道"
        >
          +
        </button>
      </div>
      <div className="px-2 py-1.5">
        <div className="truncate text-[11px] font-medium text-zinc-100">{item.title || "未命名"}</div>
        <div className="mt-0.5 text-[10px] text-zinc-500">{item.type === "video" ? "视频素材" : "音频素材"}</div>
      </div>
    </div>
  )
}

function TimelineClip({
  item,
  index,
  total,
  selected,
  draggedId,
  tool,
  duration,
  trimStart,
  trimEnd,
  onDragStart,
  onDropOn,
  onInsertBefore,
  onSeekPercent,
  onTrimAroundPercent,
  onTrimEdgePercent,
}: {
  item: VideoEditPanelMediaNode
  index: number
  total: number
  selected?: boolean
  draggedId: string | null
  tool: TimelineTool
  duration: number
  trimStart: number
  trimEnd: number
  onDragStart: (id: string | null) => void
  onDropOn: (id: string) => void
  onInsertBefore: (id: string, beforeId: string) => void
  onSeekPercent: (percent: number) => void
  onTrimAroundPercent: (percent: number) => void
  onTrimEdgePercent: (edge: "start" | "end", percent: number) => void
}) {
  const widthPercent = Math.max(18, 100 / Math.max(total, 1))
  const trimLeft = duration > 0 ? Math.max(0, Math.min(100, (trimStart / duration) * 100)) : 0
  const trimRight = duration > 0 ? Math.max(0, Math.min(100, (trimEnd / duration) * 100)) : 100
  const pointerPercent = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (rect.width <= 0) return 0
    return Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
  }
  const beginTrimEdgeDrag = (edge: "start" | "end", event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    const clip = event.currentTarget.closest("[data-openreel-timeline-clip]") as HTMLElement | null
    if (!clip) return
    const rect = clip.getBoundingClientRect()
    const onMove = (moveEvent: PointerEvent) => {
      if (rect.width <= 0) return
      const percent = Math.max(0, Math.min(1, (moveEvent.clientX - rect.left) / rect.width))
      onTrimEdgePercent(edge, percent)
    }
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }
  return (
    <div
      data-openreel-timeline-clip="true"
      draggable
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "move"
        event.dataTransfer.setData("text/plain", item.id)
        event.dataTransfer.setData("openreel/media-id", item.id)
        onDragStart(item.id)
      }}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault()
        const mediaId = event.dataTransfer.getData("openreel/media-id") || event.dataTransfer.getData("text/plain")
        if (mediaId && mediaId !== item.id) {
          onInsertBefore(mediaId, item.id)
          return
        }
        onDropOn(item.id)
      }}
      onDragEnd={() => onDragStart(null)}
      onPointerDown={(event) => {
        if (item.type !== "video") return
        const percent = pointerPercent(event)
        if (tool === "trim") {
          onTrimAroundPercent(percent)
          return
        }
        onSeekPercent(percent)
      }}
      className={cn(
        "relative h-full min-w-[150px] cursor-grab overflow-hidden rounded-md border active:cursor-grabbing",
        item.type === "video" ? "border-cyan-200/35 bg-cyan-300/13" : "border-amber-200/35 bg-amber-300/13",
        selected && "ring-1 ring-cyan-100/70",
        draggedId === item.id && "opacity-55",
      )}
      style={{ width: `${widthPercent}%` }}
    >
      {item.type === "video" ? (
        <>
          <VideoThumbnailStrip src={item.src} count={12} />
          <div className="absolute inset-0 bg-gradient-to-r from-black/12 via-transparent to-black/18" />
          {selected && (
            <div
              className="absolute bottom-0 top-0 border-x border-emerald-100/90 bg-emerald-300/16 shadow-[0_0_18px_rgba(52,211,153,0.18)]"
              style={{
                left: `${trimLeft}%`,
                width: `${Math.max(0, trimRight - trimLeft)}%`,
              }}
            >
              <button
                type="button"
                onPointerDown={(event) => beginTrimEdgeDrag("start", event)}
                className="absolute -left-1.5 top-0 h-full w-3 cursor-ew-resize rounded bg-emerald-100/95"
                title="拖动裁剪起点"
                aria-label="拖动裁剪起点"
              />
              <button
                type="button"
                onPointerDown={(event) => beginTrimEdgeDrag("end", event)}
                className="absolute -right-1.5 top-0 h-full w-3 cursor-ew-resize rounded bg-emerald-100/95"
                title="拖动裁剪终点"
                aria-label="拖动裁剪终点"
              />
            </div>
          )}
          <div className="absolute inset-x-2 top-2 flex items-center gap-1.5">
            <span className="rounded bg-cyan-100/90 px-1.5 py-0.5 text-[10px] font-bold text-cyan-950">V{index + 1}</span>
            <span className="min-w-0 truncate text-[11px] font-semibold text-cyan-50">{item.title || "视频片段"}</span>
          </div>
          <div className="absolute inset-x-2 bottom-2 flex items-center justify-between text-[10px] text-cyan-50/80">
            <span>{formatTime(0)}</span>
            <span>{duration > 0 ? formatTime(duration) : "素材"}</span>
          </div>
        </>
      ) : (
        <>
          <div className="absolute inset-x-2 top-2 flex items-center gap-1.5">
            <span className="rounded bg-amber-100/90 px-1.5 py-0.5 text-[10px] font-bold text-amber-950">A{index + 1}</span>
            <span className="min-w-0 truncate text-[11px] font-semibold text-amber-50">{item.title || "音频片段"}</span>
          </div>
          <div className="absolute inset-x-2 bottom-2 flex h-8 items-center gap-[3px]">
            {waveformBars(item.id).map((height, bar) => (
              <span
                key={bar}
                className="w-1 rounded-full bg-amber-100/80"
                style={{ height: `${height * 100}%` }}
              />
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function TimelineTrack({
  label,
  type,
  items,
  order,
  draggedId,
  activeId,
  tool,
  duration,
  trimStart,
  trimEnd,
  onDragStart,
  onDropOn,
  onAppendItem,
  onInsertBefore,
  onSeekPercent,
  onTrimAroundPercent,
  onTrimEdgePercent,
}: {
  label: string
  type: "video" | "audio"
  items: VideoEditPanelMediaNode[]
  order: string[]
  draggedId: string | null
  activeId?: string
  tool: TimelineTool
  duration: number
  trimStart: number
  trimEnd: number
  onDragStart: (id: string | null) => void
  onDropOn: (id: string) => void
  onAppendItem: (id: string) => void
  onInsertBefore: (id: string, beforeId: string) => void
  onSeekPercent: (percent: number) => void
  onTrimAroundPercent: (percent: number) => void
  onTrimEdgePercent: (edge: "start" | "end", percent: number) => void
}) {
  const itemMap = useMemo(() => new Map(items.map((item) => [item.id, item])), [items])
  const orderedItems = order.map((id) => itemMap.get(id)).filter((item): item is VideoEditPanelMediaNode => Boolean(item))
  return (
    <div className="grid min-h-[76px] grid-cols-[84px_minmax(0,1fr)] border-t border-white/[0.07]">
      <div className="flex flex-col justify-center gap-1 border-r border-white/[0.07] bg-[#0d1118] px-3">
        <div className="text-[11px] font-semibold text-zinc-200">{label}</div>
        <div className="flex items-center gap-1 text-[10px] text-zinc-500">
          <span className="h-2 w-2 rounded-sm bg-white/20" />
          <span>{type === "video" ? "可见" : "启用"}</span>
        </div>
      </div>
      <div
        className="relative flex min-w-0 gap-2 overflow-x-auto bg-[#090d13] p-2"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault()
          const mediaId = event.dataTransfer.getData("openreel/media-id") || event.dataTransfer.getData("text/plain")
          if (mediaId) onAppendItem(mediaId)
        }}
      >
        {orderedItems.length === 0 ? (
          <div className="flex h-full w-full items-center justify-center rounded-md border border-dashed border-white/10 text-xs text-zinc-600">
            {type === "video" ? "把视频片段放到这里拼接" : "分离音频后可在这里拼接"}
          </div>
        ) : (
          orderedItems.map((item, index) => (
            <TimelineClip
              key={item.id}
              item={item}
              index={index}
              total={orderedItems.length}
              selected={item.id === activeId}
              draggedId={draggedId}
              tool={tool}
              duration={duration}
              trimStart={trimStart}
              trimEnd={trimEnd}
              onDragStart={onDragStart}
              onDropOn={onDropOn}
              onInsertBefore={onInsertBefore}
              onSeekPercent={onSeekPercent}
              onTrimAroundPercent={onTrimAroundPercent}
              onTrimEdgePercent={onTrimEdgePercent}
            />
          ))
        )}
      </div>
    </div>
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
  const [timelineTool, setTimelineTool] = useState<TimelineTool>("select")
  const [playing, setPlaying] = useState(false)

  const videoItems = useMemo(() => mediaNodes.filter((item) => item.type === "video" && item.src), [mediaNodes])
  const audioItems = useMemo(() => mediaNodes.filter((item) => item.type === "audio" && item.src), [mediaNodes])
  const activeVideo = useMemo(
    () => videoItems.find((item) => item.id === nodeId) || videoItems[0],
    [nodeId, videoItems],
  )
  const ticks = useMemo(() => timelineTicks(duration), [duration])
  const playheadPercent = duration > 0 ? Math.min(100, Math.max(0, (currentTime / duration) * 100)) : 0

  useEffect(() => {
    setVideoOrder((current) => {
      const ids = videoItems.map((item) => item.id)
      const preserved = current.filter((id) => ids.includes(id))
      if (preserved.length > 0) {
        return preserved.includes(nodeId) || !ids.includes(nodeId) ? preserved : [nodeId, ...preserved]
      }
      return ids.includes(nodeId) ? [nodeId] : ids.slice(0, 1)
    })
  }, [nodeId, videoItems])

  useEffect(() => {
    setAudioOrder((current) => {
      const ids = audioItems.map((item) => item.id)
      return current.filter((id) => ids.includes(id))
    })
  }, [audioItems])

  useEffect(() => {
    setError(null)
    setDuration(0)
    setCurrentTime(0)
    setStartSeconds(0)
    setEndSeconds(0)
    setPlaying(false)
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

  const togglePlayback = () => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) {
      void video.play()
      setPlaying(true)
    } else {
      video.pause()
      setPlaying(false)
    }
  }

  const insertMediaItem = (item: VideoEditPanelMediaNode) => {
    if (item.type === "video") {
      setVideoOrder((current) => trackInsertOrder(current, item.id))
      return
    }
    setAudioOrder((current) => trackInsertOrder(current, item.id))
  }

  const appendTrackItem = (type: "video" | "audio", id: string) => {
    const exists = type === "video"
      ? videoItems.some((item) => item.id === id)
      : audioItems.some((item) => item.id === id)
    if (!exists) return
    if (type === "video") {
      setVideoOrder((current) => trackInsertOrder(current, id))
      return
    }
    setAudioOrder((current) => trackInsertOrder(current, id))
  }

  const insertTrackItemBefore = (type: "video" | "audio", id: string, beforeId: string) => {
    const exists = type === "video"
      ? videoItems.some((item) => item.id === id)
      : audioItems.some((item) => item.id === id)
    if (!exists) return
    if (type === "video") {
      setVideoOrder((current) => trackInsertOrder(current, id, beforeId))
      return
    }
    setAudioOrder((current) => trackInsertOrder(current, id, beforeId))
  }

  const seekPercent = (percent: number) => {
    if (duration <= 0) return
    seekTo(duration * Math.max(0, Math.min(1, percent)))
  }

  const trimAroundPercent = (percent: number) => {
    if (duration <= 0) return
    const center = duration * Math.max(0, Math.min(1, percent))
    const span = Math.min(Math.max(duration * 0.22, 1.2), 6)
    let start = Math.max(0, center - span / 2)
    let end = Math.min(duration, start + span)
    start = Math.max(0, end - span)
    setStartSeconds(start)
    setEndSeconds(end)
    seekTo(start)
  }

  const setTrimEdgePercent = (edge: "start" | "end", percent: number) => {
    if (duration <= 0) return
    const time = duration * Math.max(0, Math.min(1, percent))
    if (edge === "start") {
      updateStart(Math.min(time, endSeconds - 0.1))
      return
    }
    updateEnd(Math.max(time, startSeconds + 0.1))
  }

  const canTrim = endSeconds > startSeconds + 0.05
  const videoConcatIds = videoOrder.filter((id) => videoItems.some((item) => item.id === id))
  const audioConcatIds = audioOrder.filter((id) => audioItems.some((item) => item.id === id))
  const isBusy = Boolean(busy)

  return (
    <div
      className="openreel-video-edit-panel nodrag nowheel fixed inset-x-3 bottom-3 top-4 z-[94] overflow-hidden rounded-lg border border-white/10 bg-[#070b10]/98 text-zinc-100 shadow-[0_28px_90px_rgba(0,0,0,0.68)] backdrop-blur-xl"
      data-openreel-workflow-ui="true"
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      onWheel={(event) => event.stopPropagation()}
    >
      <div className="flex h-11 items-center justify-between border-b border-white/10 bg-[#0b0f16] px-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-cyan-300 text-[11px] font-black text-cyan-950">VE</div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-zinc-100">{title || "视频剪辑"}</div>
            <div className="text-[10px] text-zinc-500">{formatTimePrecise(currentTime)} / {formatTimePrecise(duration)}</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <ToolButton label="选择" glyph="S" active={timelineTool === "select"} onClick={() => setTimelineTool("select")} />
          <ToolButton label="切割" glyph="B" active={timelineTool === "blade"} onClick={() => setTimelineTool("blade")} />
          <ToolButton label="裁剪" glyph="T" active={timelineTool === "trim"} onClick={() => setTimelineTool("trim")} />
        </div>
        <button
          type="button"
          onClick={onClose}
          className="h-8 rounded-md border border-white/10 px-3 text-xs text-zinc-300 transition hover:bg-white/[0.07]"
        >
          关闭
        </button>
      </div>

      <div className="grid h-[calc(100%-3rem)] grid-rows-[minmax(0,1fr)_260px] bg-[#070b10]">
        <div className="grid min-h-0 grid-cols-[268px_minmax(380px,1fr)_304px] border-b border-white/10 max-xl:grid-cols-[230px_minmax(340px,1fr)_280px] max-lg:grid-cols-1 max-lg:overflow-y-auto">
          <aside className="min-h-0 border-r border-white/10 bg-[#0c1118]">
            <div className="flex h-10 items-center justify-between border-b border-white/10 px-3">
              <div className="text-[12px] font-semibold text-zinc-100">素材</div>
              <div className="rounded bg-white/[0.055] px-1.5 py-0.5 text-[10px] text-zinc-400">{mediaNodes.length}</div>
            </div>
            <div className="flex h-[calc(100%-2.5rem)] flex-col overflow-hidden">
              <div className="grid grid-cols-2 gap-2 overflow-y-auto p-3">
                {videoItems.map((item) => (
                  <AssetCard key={item.id} item={item} active={item.id === nodeId} onInsert={insertMediaItem} />
                ))}
                {audioItems.map((item) => (
                  <AssetCard key={item.id} item={item} onInsert={insertMediaItem} />
                ))}
                {mediaNodes.length === 0 && (
                  <div className="col-span-2 rounded-md border border-dashed border-white/10 px-3 py-8 text-center text-xs text-zinc-500">
                    画布上的视频和音频会出现在这里
                  </div>
                )}
              </div>
            </div>
          </aside>

          <main className="flex min-h-0 flex-col bg-[#090d13]">
            <div className="flex h-10 items-center justify-between border-b border-white/10 px-3">
              <div className="text-[12px] font-semibold text-zinc-100">播放器</div>
              <div className="flex items-center gap-2 text-[10px] text-zinc-500">
                <span>原始比例</span>
                <span className="rounded border border-white/10 px-1.5 py-0.5">Fit</span>
              </div>
            </div>
            <div className="flex min-h-0 flex-1 items-center justify-center bg-black p-4">
              <div className="relative flex h-full max-h-full w-full items-center justify-center overflow-hidden rounded-md border border-white/10 bg-black shadow-inner">
                <video
                  ref={videoRef}
                  src={videoUrl}
                  preload="metadata"
                  className="h-full w-full object-contain [color-scheme:dark]"
                  onLoadedMetadata={(event) => {
                    const nextDuration = Number(event.currentTarget.duration || 0)
                    setDuration(Number.isFinite(nextDuration) ? nextDuration : 0)
                    setEndSeconds(Number.isFinite(nextDuration) && nextDuration > 0 ? nextDuration : 0)
                  }}
                  onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                  onPlay={() => setPlaying(true)}
                  onPause={() => setPlaying(false)}
                  onEnded={() => setPlaying(false)}
                />
                <div className="pointer-events-none absolute left-3 top-3 rounded bg-black/55 px-2 py-1 text-[10px] font-medium text-zinc-300">
                  {activeVideo?.title || "当前视频"}
                </div>
              </div>
            </div>
            <div className="border-t border-white/10 bg-[#0d121a] px-3 py-2">
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={togglePlayback}
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-zinc-100 text-[13px] font-black text-zinc-950 transition hover:bg-white"
                  title={playing ? "暂停" : "播放"}
                  aria-label={playing ? "暂停" : "播放"}
                >
                  {playing ? "II" : "▶"}
                </button>
                <div className="w-[72px] text-[11px] font-semibold text-cyan-100">{formatTimePrecise(currentTime)}</div>
                <input
                  type="range"
                  min={0}
                  max={Math.max(duration, 0.1)}
                  step={0.01}
                  value={clampTime(currentTime, duration)}
                  onChange={(event) => seekTo(Number(event.target.value))}
                  className="min-w-0 flex-1 accent-cyan-200"
                />
                <div className="w-[72px] text-right text-[11px] text-zinc-500">{formatTimePrecise(duration)}</div>
              </div>
            </div>
          </main>

          <aside className="min-h-0 border-l border-white/10 bg-[#0c1118]">
            <div className="flex h-10 items-center justify-between border-b border-white/10 px-3">
              <div className="text-[12px] font-semibold text-zinc-100">检查器</div>
              <div className={cn("h-2 w-2 rounded-full", isBusy ? "bg-cyan-300" : "bg-emerald-300/80")} />
            </div>
            <div className="space-y-4 overflow-y-auto p-3">
              <section className="rounded-md border border-white/10 bg-white/[0.035] p-3">
                <div className="mb-3 text-[11px] font-semibold text-zinc-300">生成到画布</div>
                <div className="grid grid-cols-2 gap-2">
                  <OperationButton
                    active={busy === "frame"}
                    disabled={isBusy}
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
                    disabled={isBusy}
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
                    active={busy === "split"}
                    disabled={isBusy}
                    onClick={() => void runOperation("split", {
                      operation: "video.split_tracks",
                      source_node_id: nodeId,
                    })}
                  >
                    分音轨
                  </OperationButton>
                  <OperationButton
                    active={busy === "trim"}
                    disabled={isBusy || !canTrim}
                    tone="primary"
                    onClick={() => void runOperation("trim", {
                      operation: "video.trim",
                      source_node_id: nodeId,
                      range: { start_seconds: startSeconds, end_seconds: endSeconds },
                      title: `${title || "视频"} 片段`,
                    })}
                  >
                    导出片段
                  </OperationButton>
                </div>
              </section>

              <section className="rounded-md border border-white/10 bg-white/[0.035] p-3">
                <div className="mb-3 flex items-center justify-between">
                  <div className="text-[11px] font-semibold text-zinc-300">裁剪范围</div>
                  <div className="text-[10px] text-zinc-500">{formatTime(startSeconds)} - {formatTime(endSeconds)}</div>
                </div>
                <div className="space-y-3">
                  <label className="block">
                    <span className="mb-1 block text-[10px] text-zinc-500">起点</span>
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
                  <label className="block">
                    <span className="mb-1 block text-[10px] text-zinc-500">终点</span>
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
              </section>

              <section className="rounded-md border border-white/10 bg-white/[0.035] p-3">
                <div className="mb-3 text-[11px] font-semibold text-zinc-300">轨道合成</div>
                <div className="grid gap-2">
                  <OperationButton
                    active={busy === "concat-video"}
                    disabled={isBusy || videoConcatIds.length < 2}
                    onClick={() => void runOperation("concat-video", {
                      operation: "video.concat",
                      source_node_ids: videoConcatIds,
                      title: "拼接视频",
                    })}
                  >
                    拼接视频轨
                  </OperationButton>
                  <OperationButton
                    active={busy === "concat-audio"}
                    disabled={isBusy || audioConcatIds.length < 2}
                    onClick={() => void runOperation("concat-audio", {
                      operation: "audio.concat",
                      source_node_ids: audioConcatIds,
                      title: "拼接音频",
                    })}
                  >
                    拼接音频轨
                  </OperationButton>
                </div>
              </section>

              {error && (
                <div className="rounded-md border border-red-400/25 bg-red-950/45 p-3 text-xs leading-5 text-red-100">
                  {error}
                </div>
              )}
              {busy && (
                <div className="flex items-center gap-2 rounded-md border border-cyan-200/20 bg-cyan-300/10 p-3 text-xs text-cyan-100">
                  <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-cyan-100 border-t-transparent" />
                  处理中...
                </div>
              )}
            </div>
          </aside>
        </div>

        <section className="min-h-0 bg-[#080c12]">
          <div className="flex h-10 items-center justify-between border-b border-white/10 bg-[#0b0f16] px-3">
            <div className="flex items-center gap-1.5">
              <ToolButton label="选择" glyph="S" active={timelineTool === "select"} onClick={() => setTimelineTool("select")} />
              <ToolButton label="切割" glyph="B" active={timelineTool === "blade"} onClick={() => setTimelineTool("blade")} />
              <ToolButton label="裁剪" glyph="T" active={timelineTool === "trim"} onClick={() => setTimelineTool("trim")} />
              <div className="ml-2 h-5 w-px bg-white/10" />
              <OperationButton
                disabled={isBusy}
                onClick={() => void runOperation("frame", {
                  operation: "video.export_frame",
                  source_node_id: nodeId,
                  frame_mode: "time",
                  time_seconds: currentTime,
                  title: `${title || "视频"} ${formatTime(currentTime)} 画面`,
                })}
              >
                定格画面
              </OperationButton>
            </div>
            <div className="flex items-center gap-2 text-[11px] text-zinc-500">
              <span>吸附</span>
              <span className="rounded bg-cyan-300/14 px-1.5 py-0.5 text-cyan-100">开</span>
              <span className="ml-2">缩放</span>
              <input type="range" min={0} max={100} defaultValue={42} className="w-24 accent-cyan-200" />
            </div>
          </div>

          <div className="relative h-[calc(100%-2.5rem)] overflow-hidden">
            <div className="grid h-7 grid-cols-[84px_minmax(0,1fr)] border-b border-white/[0.07] bg-[#0d1118]">
              <div className="border-r border-white/[0.07]" />
              <div className="relative">
                {ticks.map((tick) => (
                  <div
                    key={tick}
                    className="absolute top-0 h-full border-l border-white/[0.08] pl-1 text-[10px] leading-7 text-zinc-500"
                    style={{ left: `${duration > 0 ? (tick / duration) * 100 : 0}%` }}
                  >
                    {formatTime(tick)}
                  </div>
                ))}
              </div>
            </div>
            <div className="relative h-[calc(100%-1.75rem)] overflow-y-auto">
              <div className="pointer-events-none absolute bottom-0 top-0 z-20 w-px bg-cyan-100 shadow-[0_0_0_1px_rgba(34,211,238,0.28),0_0_18px_rgba(34,211,238,0.45)]" style={{ left: `calc(84px + ${playheadPercent}% * (100% - 84px) / 100)` }}>
                <div className="-ml-1.5 h-3 w-3 rounded-sm bg-cyan-100" />
              </div>
              <TimelineTrack
                label="V1"
                type="video"
                items={videoItems}
                order={videoOrder}
                draggedId={draggedVideoId}
                activeId={nodeId}
                tool={timelineTool}
                duration={duration}
                trimStart={startSeconds}
                trimEnd={endSeconds}
                onDragStart={setDraggedVideoId}
                onDropOn={(id) => {
                  if (!draggedVideoId) return
                  setVideoOrder((current) => reorder(current, draggedVideoId, id))
                  setDraggedVideoId(null)
                }}
                onAppendItem={(id) => appendTrackItem("video", id)}
                onInsertBefore={(id, beforeId) => insertTrackItemBefore("video", id, beforeId)}
                onSeekPercent={seekPercent}
                onTrimAroundPercent={trimAroundPercent}
                onTrimEdgePercent={setTrimEdgePercent}
              />
              <TimelineTrack
                label="A1"
                type="audio"
                items={audioItems}
                order={audioOrder}
                draggedId={draggedAudioId}
                tool={timelineTool}
                duration={duration}
                trimStart={0}
                trimEnd={duration}
                onDragStart={setDraggedAudioId}
                onDropOn={(id) => {
                  if (!draggedAudioId) return
                  setAudioOrder((current) => reorder(current, draggedAudioId, id))
                  setDraggedAudioId(null)
                }}
                onAppendItem={(id) => appendTrackItem("audio", id)}
                onInsertBefore={(id, beforeId) => insertTrackItemBefore("audio", id, beforeId)}
                onSeekPercent={seekPercent}
                onTrimAroundPercent={trimAroundPercent}
                onTrimEdgePercent={setTrimEdgePercent}
              />
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
