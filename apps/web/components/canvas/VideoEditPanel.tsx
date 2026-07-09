"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type { DragEvent as ReactDragEvent, PointerEvent as ReactPointerEvent } from "react"
import { runProjectMediaOperation } from "@/lib/api"
import { cn } from "@/lib/utils"

export interface VideoEditPanelMediaNode {
  id: string
  title: string
  type: "video" | "audio" | "image"
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
type PreviewScale = "fit" | "50" | "75" | "100"

interface TimelineClipState {
  id: string
  start: number
  duration: number
}

const TRACK_LABEL_WIDTH = 76
const DEFAULT_CLIP_SECONDS = 4
const DEFAULT_TIMELINE_SECONDS = 12
const DEFAULT_PX_PER_SECOND = 84

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

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(Math.max(value, min), max)
}

function clipEnd(clip: TimelineClipState): number {
  return clip.start + clip.duration
}

function mediaTypeLabel(type: VideoEditPanelMediaNode["type"]): string {
  if (type === "video") return "视频"
  if (type === "audio") return "音频"
  return "图片"
}

function waveformBars(seed: string, count = 72): number[] {
  let value = seed.split("").reduce((sum, char) => sum + char.charCodeAt(0), 37)
  return Array.from({ length: count }, () => {
    value = (value * 1664525 + 1013904223) % 4294967296
    return 0.18 + (value / 4294967296) * 0.78
  })
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

function timeFromPointer(
  event: { clientX: number },
  container: HTMLElement,
  pxPerSecond: number,
): number {
  const rect = container.getBoundingClientRect()
  const scrollLeft = container.scrollLeft || 0
  return Math.max(0, (event.clientX - rect.left + scrollLeft - TRACK_LABEL_WIDTH) / pxPerSecond)
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
          video.onseeked = () => resolve()
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
            className="h-full min-w-0 flex-1 object-cover opacity-90"
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
        "inline-flex h-7 items-center gap-1 rounded-md border px-2 text-[11px] font-semibold transition",
        active
          ? "border-cyan-200/45 bg-cyan-300/16 text-cyan-100"
          : "border-white/10 bg-white/[0.035] text-zinc-300 hover:bg-white/[0.08]",
      )}
    >
      <span className="text-[10px] text-zinc-500">{glyph}</span>
      {label}
    </button>
  )
}

function ActionButton({
  children,
  disabled,
  active,
  onClick,
}: {
  children: React.ReactNode
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
        "h-8 rounded-md border px-2.5 text-[11px] font-semibold transition",
        active
          ? "border-cyan-200/45 bg-cyan-200 text-cyan-950"
          : "border-white/10 bg-white/[0.045] text-zinc-200 hover:bg-white/[0.085]",
        disabled && "cursor-not-allowed opacity-40 hover:bg-white/[0.045]",
      )}
    >
      {children}
    </button>
  )
}

function MediaBinItem({
  item,
  onInsert,
}: {
  item: VideoEditPanelMediaNode
  onInsert: (item: VideoEditPanelMediaNode) => void
}) {
  return (
    <div
      draggable
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "copy"
        event.dataTransfer.setData("openreel/media-id", item.id)
      }}
      onDoubleClick={() => onInsert(item)}
      className="group flex cursor-grab items-center gap-2 rounded-md border border-white/10 bg-white/[0.035] p-2 active:cursor-grabbing"
    >
      <div className="relative h-10 w-14 shrink-0 overflow-hidden rounded bg-black">
        {item.type === "video" ? (
          <video src={item.src} muted preload="metadata" className="h-full w-full object-cover opacity-85" />
        ) : item.type === "image" ? (
          <img src={item.src} alt="" className="h-full w-full object-cover opacity-90" draggable={false} />
        ) : (
          <div className="flex h-full items-end justify-center gap-0.5 px-2 py-2">
            {waveformBars(item.id, 12).map((height, index) => (
              <span
                key={index}
                className="w-1 rounded-full bg-amber-200/80"
                style={{ height: `${height * 100}%` }}
              />
            ))}
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[11px] font-medium text-zinc-100">{item.title || "未命名素材"}</div>
        <div className="mt-0.5 text-[10px] text-zinc-500">{mediaTypeLabel(item.type)} · 双击插入</div>
      </div>
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation()
          onInsert(item)
        }}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-zinc-100 text-xs font-black text-zinc-950 opacity-0 transition group-hover:opacity-100"
        title="插入轨道"
        aria-label="插入轨道"
      >
        +
      </button>
    </div>
  )
}

function TimelineClip({
  clip,
  item,
  kind,
  pxPerSecond,
  selected,
  trimStart,
  trimEnd,
  currentTime,
  onSelect,
  onDragStartTime,
  onTrimEdge,
}: {
  clip: TimelineClipState
  item: VideoEditPanelMediaNode
  kind: "video" | "audio"
  pxPerSecond: number
  selected?: boolean
  trimStart?: number
  trimEnd?: number
  currentTime: number
  onSelect: () => void
  onDragStartTime: (start: number) => void
  onTrimEdge?: (edge: "start" | "end", seconds: number) => void
}) {
  const [dragging, setDragging] = useState(false)
  const left = clip.start * pxPerSecond
  const width = Math.max(54, clip.duration * pxPerSecond)
  const localTrimStart = trimStart != null ? clamp(trimStart - clip.start, 0, clip.duration) : 0
  const localTrimEnd = trimEnd != null ? clamp(trimEnd - clip.start, 0, clip.duration) : clip.duration
  const localPlayhead = clamp(currentTime - clip.start, 0, clip.duration)

  const beginMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement | null
    if (target?.dataset.trimHandle) return
    event.preventDefault()
    event.stopPropagation()
    onSelect()
    const startX = event.clientX
    const initialStart = clip.start
    setDragging(true)

    const onMove = (moveEvent: PointerEvent) => {
      const delta = (moveEvent.clientX - startX) / pxPerSecond
      onDragStartTime(Math.max(0, initialStart + delta))
    }
    const onEnd = () => {
      setDragging(false)
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }

  const beginTrim = (edge: "start" | "end", event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!onTrimEdge) return
    event.preventDefault()
    event.stopPropagation()
    onSelect()
    const startX = event.clientX
    const initial = edge === "start" ? localTrimStart : localTrimEnd
    const onMove = (moveEvent: PointerEvent) => {
      const delta = (moveEvent.clientX - startX) / pxPerSecond
      onTrimEdge(edge, clip.start + clamp(initial + delta, 0, clip.duration))
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
      onPointerDown={beginMove}
      className={cn(
        "absolute top-2 h-[58px] overflow-hidden rounded-md border shadow-sm",
        kind === "video"
          ? "border-cyan-200/30 bg-cyan-300/10"
          : "border-amber-200/30 bg-amber-300/10",
        selected && "ring-1 ring-cyan-100/80",
        dragging && "cursor-grabbing opacity-85",
        !dragging && "cursor-grab",
      )}
      style={{ left, width }}
    >
      {kind === "video" ? (
        <>
          {item.type === "image" ? (
            <img src={item.src} alt="" className="absolute inset-0 h-full w-full object-cover opacity-90" draggable={false} />
          ) : (
            <VideoThumbnailStrip src={item.src} count={Math.max(4, Math.min(12, Math.round(width / 70)))} />
          )}
          <div className="absolute inset-0 bg-gradient-to-r from-black/10 via-transparent to-black/20" />
          {selected && item.type === "video" && (
            <div
              className="absolute bottom-0 top-0 border-x border-emerald-100/90 bg-emerald-300/16"
              style={{
                left: `${(localTrimStart / clip.duration) * 100}%`,
                width: `${Math.max(0, ((localTrimEnd - localTrimStart) / clip.duration) * 100)}%`,
              }}
            >
              <button
                type="button"
                data-trim-handle="start"
                onPointerDown={(event) => beginTrim("start", event)}
                className="absolute -left-1 top-0 h-full w-2 cursor-ew-resize rounded bg-emerald-100"
                title="裁剪起点"
                aria-label="裁剪起点"
              />
              <button
                type="button"
                data-trim-handle="end"
                onPointerDown={(event) => beginTrim("end", event)}
                className="absolute -right-1 top-0 h-full w-2 cursor-ew-resize rounded bg-emerald-100"
                title="裁剪终点"
                aria-label="裁剪终点"
              />
            </div>
          )}
        </>
      ) : (
        <div className="absolute inset-x-2 bottom-2 flex h-8 items-center gap-[3px]">
          {waveformBars(item.id, Math.max(24, Math.min(92, Math.round(width / 6)))).map((height, index) => (
            <span
              key={index}
              className="w-1 rounded-full bg-amber-100/80"
              style={{ height: `${height * 100}%` }}
            />
          ))}
        </div>
      )}
      <div className="absolute left-2 top-1.5 flex max-w-[calc(100%-1rem)] items-center gap-1.5">
        <span className={cn(
          "rounded px-1.5 py-0.5 text-[10px] font-bold",
          kind === "video" ? "bg-cyan-100 text-cyan-950" : "bg-amber-100 text-amber-950",
        )}>
          {kind === "video" ? (item.type === "image" ? "I" : "V") : "A"}
        </span>
        <span className="truncate text-[11px] font-semibold text-zinc-50">{item.title || "素材"}</span>
      </div>
      {kind === "video" && selected && (
        <div
          className="absolute bottom-0 top-0 w-px bg-white/85"
          style={{ left: `${(localPlayhead / clip.duration) * 100}%` }}
        />
      )}
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
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const timelineRef = useRef<HTMLDivElement | null>(null)
  const [sourceDuration, setSourceDuration] = useState(DEFAULT_CLIP_SECONDS)
  const [currentTime, setCurrentTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [tool, setTool] = useState<TimelineTool>("select")
  const [pxPerSecond, setPxPerSecond] = useState(DEFAULT_PX_PER_SECOND)
  const [previewScale, setPreviewScale] = useState<PreviewScale>("fit")
  const [busy, setBusy] = useState<BusyAction>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedClipId, setSelectedClipId] = useState(nodeId)
  const [videoClips, setVideoClips] = useState<TimelineClipState[]>([])
  const [audioClips, setAudioClips] = useState<TimelineClipState[]>([])
  const [trimStart, setTrimStart] = useState(0)
  const [trimEnd, setTrimEnd] = useState(DEFAULT_CLIP_SECONDS)

  const visualItems = useMemo(
    () => mediaNodes.filter((item) => (item.type === "video" || item.type === "image") && item.src),
    [mediaNodes],
  )
  const videoItems = useMemo(() => mediaNodes.filter((item) => item.type === "video" && item.src), [mediaNodes])
  const audioItems = useMemo(() => mediaNodes.filter((item) => item.type === "audio" && item.src), [mediaNodes])
  const mediaById = useMemo(() => new Map(mediaNodes.map((item) => [item.id, item])), [mediaNodes])
  const selectedVideoClip = useMemo(
    () => videoClips.find((clip) => clip.id === selectedClipId) || videoClips[0],
    [selectedClipId, videoClips],
  )
  const currentVideoClip = useMemo(
    () => videoClips.find((clip) => currentTime >= clip.start && currentTime <= clipEnd(clip)) || selectedVideoClip,
    [currentTime, selectedVideoClip, videoClips],
  )
  const currentAudioClip = useMemo(
    () => audioClips.find((clip) => currentTime >= clip.start && currentTime <= clipEnd(clip)),
    [audioClips, currentTime],
  )
  const currentVideoItem = currentVideoClip ? mediaById.get(currentVideoClip.id) : undefined
  const currentAudioItem = currentAudioClip ? mediaById.get(currentAudioClip.id) : undefined
  const selectedVideoItem = selectedVideoClip ? mediaById.get(selectedVideoClip.id) : undefined
  const timelineDuration = useMemo(() => {
    const lastClipEnd = Math.max(
      0,
      ...videoClips.map(clipEnd),
      ...audioClips.map(clipEnd),
      sourceDuration,
    )
    return Math.max(DEFAULT_TIMELINE_SECONDS, Math.ceil(lastClipEnd + 2))
  }, [audioClips, sourceDuration, videoClips])
  const timelineWidth = timelineDuration * pxPerSecond
  const ticks = useMemo(() => {
    const step = pxPerSecond >= 120 ? 1 : pxPerSecond >= 72 ? 2 : 5
    const count = Math.floor(timelineDuration / step) + 1
    return Array.from({ length: count }, (_, index) => index * step)
  }, [pxPerSecond, timelineDuration])

  useEffect(() => {
    setVideoClips((current) => {
      const valid = current.filter((clip) => visualItems.some((item) => item.id === clip.id))
      if (valid.length > 0) {
        return valid.map((clip) => (
          clip.id === nodeId
            ? { ...clip, duration: sourceDuration || clip.duration || DEFAULT_CLIP_SECONDS }
            : clip
        ))
      }
      const primary = videoItems.find((item) => item.id === nodeId) || visualItems[0]
      return primary
        ? [{ id: primary.id, start: 0, duration: sourceDuration || DEFAULT_CLIP_SECONDS }]
        : []
    })
  }, [nodeId, sourceDuration, videoItems, visualItems])

  useEffect(() => {
    setAudioClips((current) => {
      const valid = current.filter((clip) => audioItems.some((item) => item.id === clip.id))
      if (valid.length > 0) return valid
      const primary = audioItems[0]
      return primary
        ? [{ id: primary.id, start: 0, duration: sourceDuration || DEFAULT_CLIP_SECONDS }]
        : []
    })
  }, [audioItems, sourceDuration])

  useEffect(() => {
    setSelectedClipId(nodeId)
    setCurrentTime(0)
    setTrimStart(0)
    setTrimEnd(sourceDuration || DEFAULT_CLIP_SECONDS)
    setPlaying(false)
    setError(null)
  }, [nodeId, sourceDuration])

  useEffect(() => {
    if (!currentVideoClip) return
    const nextStart = clamp(trimStart, currentVideoClip.start, clipEnd(currentVideoClip) - 0.05)
    const nextEnd = clamp(trimEnd, nextStart + 0.05, clipEnd(currentVideoClip))
    if (nextStart !== trimStart) setTrimStart(nextStart)
    if (nextEnd !== trimEnd) setTrimEnd(nextEnd)
  }, [currentVideoClip, trimEnd, trimStart])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !currentVideoClip) return
    const item = mediaById.get(currentVideoClip.id)
    if (item?.type !== "video") return
    const localTime = clamp(currentTime - currentVideoClip.start, 0, currentVideoClip.duration)
    if (Math.abs((video.currentTime || 0) - localTime) > 0.08) {
      video.currentTime = localTime
    }
  }, [currentTime, currentVideoClip, mediaById])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !currentAudioClip) {
      audio?.pause()
      return
    }
    const localTime = clamp(currentTime - currentAudioClip.start, 0, currentAudioClip.duration)
    if (Math.abs((audio.currentTime || 0) - localTime) > 0.08) {
      audio.currentTime = localTime
    }
    if (playing) {
      void audio.play().catch(() => undefined)
    } else {
      audio.pause()
    }
  }, [currentAudioClip, currentTime, playing])

  const seekTo = (time: number) => {
    setCurrentTime(clamp(time, 0, timelineDuration))
  }

  const zoomTimelineAt = useCallback((nextValue: number, clientX?: number) => {
    const next = clamp(nextValue, 42, 220)
    const container = timelineRef.current
    if (!container || clientX == null) {
      setPxPerSecond(next)
      return
    }
    const rect = container.getBoundingClientRect()
    const localX = clientX - rect.left
    const anchorTime = Math.max(0, (localX + container.scrollLeft - TRACK_LABEL_WIDTH) / pxPerSecond)
    setPxPerSecond(next)
    window.requestAnimationFrame(() => {
      container.scrollLeft = Math.max(0, anchorTime * next + TRACK_LABEL_WIDTH - localX)
    })
  }, [pxPerSecond])

  const beginPlayheadDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const container = timelineRef.current
    if (!container) return
    event.preventDefault()
    const update = (pointerEvent: PointerEvent | ReactPointerEvent) => {
      seekTo(timeFromPointer(pointerEvent, container, pxPerSecond))
    }
    update(event)
    const onMove = (moveEvent: PointerEvent) => update(moveEvent)
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }

  const togglePlayback = useCallback(() => {
    const video = videoRef.current
    if (playing) {
      video?.pause()
      audioRef.current?.pause()
      setPlaying(false)
      return
    }
    const clip = currentVideoClip
    if (!clip && !currentAudioClip) return
    const item = clip ? mediaById.get(clip.id) : undefined
    if (video && clip && item?.type === "video") {
      const localTime = clamp(currentTime - clip.start, 0, clip.duration)
      video.currentTime = localTime
      void video.play()
    }
    if (currentAudioClip) {
      const audio = audioRef.current
      if (audio) {
        audio.currentTime = clamp(currentTime - currentAudioClip.start, 0, currentAudioClip.duration)
        void audio.play().catch(() => undefined)
      }
    }
    setPlaying(true)
  }, [currentAudioClip, currentTime, currentVideoClip, mediaById, playing])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const isTyping = ["input", "textarea", "select"].includes(target?.tagName?.toLowerCase() || "") || target?.isContentEditable
      if (isTyping) return
      if (event.code === "Space" || event.key === " ") {
        event.preventDefault()
        togglePlayback()
        return
      }
      if (!(event.ctrlKey || event.metaKey)) return
      if (event.key === "=" || event.key === "+") {
        event.preventDefault()
        zoomTimelineAt(pxPerSecond + 14)
      }
      if (event.key === "-") {
        event.preventDefault()
        zoomTimelineAt(pxPerSecond - 14)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [pxPerSecond, togglePlayback, zoomTimelineAt])

  useEffect(() => {
    if (!playing || currentVideoItem?.type === "video") return
    let frame = 0
    let last = performance.now()
    const tick = (now: number) => {
      const deltaSeconds = (now - last) / 1000
      last = now
      setCurrentTime((value) => {
        const next = clamp(value + deltaSeconds, 0, timelineDuration)
        if (next >= timelineDuration) {
          setPlaying(false)
          return timelineDuration
        }
        return next
      })
      frame = window.requestAnimationFrame(tick)
    }
    frame = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame)
  }, [currentVideoItem?.type, playing, timelineDuration])

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

  const insertMediaItem = (item: VideoEditPanelMediaNode, startAt = currentTime) => {
    const duration = item.type === "image"
      ? DEFAULT_CLIP_SECONDS
      : Math.max(sourceDuration || DEFAULT_CLIP_SECONDS, DEFAULT_CLIP_SECONDS)
    const start = Math.max(0, startAt)
    if (item.type === "video" || item.type === "image") {
      setVideoClips((current) => {
        const exists = current.some((clip) => clip.id === item.id)
        return exists
          ? current.map((clip) => clip.id === item.id ? { ...clip, start } : clip)
          : [...current, { id: item.id, start, duration }]
      })
      setSelectedClipId(item.id)
      return
    }
    setAudioClips((current) => {
      const exists = current.some((clip) => clip.id === item.id)
      return exists
        ? current.map((clip) => clip.id === item.id ? { ...clip, start } : clip)
        : [...current, { id: item.id, start, duration }]
    })
  }

  const handleTrackDrop = (kind: "video" | "audio", event: ReactDragEvent<HTMLDivElement>) => {
    event.preventDefault()
    event.stopPropagation()
    const id = event.dataTransfer.getData("openreel/media-id")
    const item = mediaById.get(id)
    const container = timelineRef.current
    if (!item || !container) return
    if (kind === "video" && item.type === "audio") return
    if (kind === "audio" && item.type !== "audio") return
    insertMediaItem(item, timeFromPointer(event, container, pxPerSecond))
  }

  const updateClipStart = (kind: "video" | "audio", id: string, start: number) => {
    const updater = (clips: TimelineClipState[]) => (
      clips.map((clip) => clip.id === id ? { ...clip, start: Math.max(0, start) } : clip)
    )
    if (kind === "video") {
      setVideoClips(updater)
      return
    }
    setAudioClips(updater)
  }

  const setTrimEdge = (edge: "start" | "end", seconds: number) => {
    if (!selectedVideoClip) return
    if (edge === "start") {
      setTrimStart(clamp(seconds, selectedVideoClip.start, trimEnd - 0.05))
      return
    }
    setTrimEnd(clamp(seconds, trimStart + 0.05, clipEnd(selectedVideoClip)))
  }

  const trimAroundTime = (time: number) => {
    if (!selectedVideoClip) return
    const center = clamp(time, selectedVideoClip.start, clipEnd(selectedVideoClip))
    const span = Math.min(Math.max(selectedVideoClip.duration * 0.25, 1.2), selectedVideoClip.duration)
    let start = clamp(center - span / 2, selectedVideoClip.start, clipEnd(selectedVideoClip))
    let end = clamp(start + span, selectedVideoClip.start, clipEnd(selectedVideoClip))
    start = Math.max(selectedVideoClip.start, end - span)
    setTrimStart(start)
    setTrimEnd(end)
    seekTo(start)
  }

  const handleTimelineBackgroundDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null
    if (target?.closest("[data-openreel-timeline-clip]")) return
    const container = timelineRef.current
    if (!container) return
    const time = timeFromPointer(event, container, pxPerSecond)
    if (tool === "trim") {
      trimAroundTime(time)
      return
    }
    beginPlayheadDrag(event)
  }

  const canTrim = selectedVideoClip && selectedVideoItem?.type === "video" && trimEnd > trimStart + 0.05
  const videoConcatIds = videoClips.map((clip) => clip.id).filter((id) => videoItems.some((item) => item.id === id))
  const audioConcatIds = audioClips.map((clip) => clip.id).filter((id) => audioItems.some((item) => item.id === id))
  const isBusy = Boolean(busy)
  const previewScaleStyle = previewScale === "fit"
    ? { width: "min(100%, 680px)" }
    : { width: `${previewScale}%`, maxWidth: "780px" }

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
            <div className="text-[10px] text-zinc-500">{formatTimePrecise(currentTime)} / {formatTimePrecise(timelineDuration)}</div>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="h-8 rounded-md border border-white/10 px-3 text-xs text-zinc-300 transition hover:bg-white/[0.07]"
        >
          关闭
        </button>
      </div>

      <div className="grid h-[calc(100%-2.75rem)] grid-rows-[minmax(0,1fr)_190px] bg-[#070b10]">
        <div className="grid min-h-0 grid-cols-[220px_minmax(420px,1fr)_300px] border-b border-white/10 max-xl:grid-cols-[200px_minmax(360px,1fr)_280px] max-lg:grid-cols-1 max-lg:overflow-y-auto">
          <aside className="flex min-h-0 flex-col border-r border-white/10 bg-[#0b1017]">
            <div className="flex h-10 items-center justify-between border-b border-white/10 px-3">
              <div className="text-[12px] font-semibold text-zinc-100">项目素材</div>
              <div className="text-[10px] text-zinc-500">{mediaNodes.length}</div>
            </div>
            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
              {mediaNodes.map((item) => (
                <MediaBinItem key={item.id} item={item} onInsert={insertMediaItem} />
              ))}
              {mediaNodes.length === 0 && (
                <div className="rounded-md border border-dashed border-white/10 px-3 py-5 text-center text-xs leading-5 text-zinc-500">
                  当前项目里的图片、视频和音频会出现在这里
                </div>
              )}
            </div>
          </aside>

          <main className="flex min-h-0 flex-col bg-[#090d13]">
            <div className="flex min-h-0 flex-1 items-center justify-center bg-black p-5">
              <div
                className="relative flex aspect-video max-h-full items-center justify-center overflow-hidden rounded-md border border-white/10 bg-black shadow-inner"
                style={previewScaleStyle}
              >
                {currentVideoItem ? (
                  currentVideoItem.type === "image" ? (
                    <img src={currentVideoItem.src} alt="" className="h-full w-full object-contain" draggable={false} />
                  ) : (
                    <video
                      ref={videoRef}
                      src={currentVideoItem.src || videoUrl}
                      muted={audioClips.length > 0}
                      preload="metadata"
                      className="h-full w-full object-contain [color-scheme:dark]"
                      onLoadedMetadata={(event) => {
                        const nextDuration = Number(event.currentTarget.duration || 0)
                        const safeDuration = Number.isFinite(nextDuration) && nextDuration > 0 ? nextDuration : DEFAULT_CLIP_SECONDS
                        if (currentVideoItem.id === nodeId) {
                          setSourceDuration(safeDuration)
                          setTrimEnd(safeDuration)
                        }
                      }}
                      onTimeUpdate={(event) => {
                        if (!currentVideoClip || !playing) return
                        const next = currentVideoClip.start + event.currentTarget.currentTime
                        setCurrentTime(clamp(next, 0, timelineDuration))
                      }}
                      onPlay={() => setPlaying(true)}
                      onPause={() => setPlaying(false)}
                      onEnded={() => {
                        setPlaying(false)
                        audioRef.current?.pause()
                      }}
                    />
                  )
                ) : (
                  <div className="text-xs text-zinc-500">播放头不在视频片段上</div>
                )}
                <div className="pointer-events-none absolute left-3 top-3 rounded bg-black/55 px-2 py-1 text-[10px] font-medium text-zinc-300">
                  {currentVideoItem?.title || selectedVideoItem?.title || "当前画面"}
                </div>
              </div>
              {currentAudioItem && <audio ref={audioRef} src={currentAudioItem.src} preload="metadata" />}
            </div>

            <div className="flex h-12 items-center justify-between border-t border-white/10 bg-[#0d121a] px-3">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={togglePlayback}
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-zinc-100 text-[12px] font-black text-zinc-950 transition hover:bg-white"
                  title={playing ? "暂停" : "播放"}
                  aria-label={playing ? "暂停" : "播放"}
                >
                  {playing ? "II" : "▶"}
                </button>
                <span className="w-[76px] text-[11px] font-semibold text-cyan-100">{formatTimePrecise(currentTime)}</span>
                <ToolButton label="选择" glyph="S" active={tool === "select"} onClick={() => setTool("select")} />
                <ToolButton label="切割" glyph="B" active={tool === "blade"} onClick={() => setTool("blade")} />
                <ToolButton label="裁剪" glyph="T" active={tool === "trim"} onClick={() => setTool("trim")} />
                <ActionButton
                  disabled={isBusy || currentVideoItem?.type !== "video"}
                  onClick={() => void runOperation("frame", {
                    operation: "video.export_frame",
                    source_node_id: currentVideoClip?.id || nodeId,
                    frame_mode: "time",
                    time_seconds: Math.max(0, currentTime - (currentVideoClip?.start || 0)),
                    title: `${title || "视频"} ${formatTime(currentTime)} 画面`,
                  })}
                >
                  定格
                </ActionButton>
              </div>
              <div className="hidden items-center gap-2 text-[10px] text-zinc-500 sm:flex">
                <span>空格播放</span>
                <select
                  value={previewScale}
                  onChange={(event) => setPreviewScale(event.target.value as PreviewScale)}
                  className="h-7 rounded-md border border-white/10 bg-[#141a22] px-2 text-[11px] font-semibold text-zinc-200 outline-none"
                  title="视频缩放"
                  aria-label="视频缩放"
                >
                  <option value="fit">适合</option>
                  <option value="50">50%</option>
                  <option value="75">75%</option>
                  <option value="100">100%</option>
                </select>
                <span className="rounded border border-white/10 px-1.5 py-0.5">{Math.round(pxPerSecond)} px/s</span>
              </div>
            </div>
          </main>

          <aside className="min-h-0 border-l border-white/10 bg-[#0c1118]">
            <div className="flex h-10 items-center justify-between border-b border-white/10 px-3">
              <div className="text-[12px] font-semibold text-zinc-100">功能区</div>
              <div className={cn("h-2 w-2 rounded-full", isBusy ? "bg-cyan-300" : "bg-emerald-300/80")} />
            </div>
            <div className="space-y-4 overflow-y-auto p-3">
              <section className="rounded-md border border-white/10 bg-white/[0.035] p-3">
                <div className="mb-3 text-[11px] font-semibold text-zinc-300">生成到画布</div>
                <div className="grid grid-cols-2 gap-2">
                  <ActionButton
                    active={busy === "tail"}
                    disabled={isBusy || selectedVideoItem?.type !== "video"}
                    onClick={() => void runOperation("tail", {
                      operation: "video.export_frame",
                      source_node_id: selectedVideoClip?.id || nodeId,
                      frame_mode: "tail",
                      title: `${title || "视频"} 尾帧`,
                    })}
                  >
                    尾帧
                  </ActionButton>
                  <ActionButton
                    active={busy === "split"}
                    disabled={isBusy}
                    onClick={() => void runOperation("split", {
                      operation: "video.split_tracks",
                      source_node_id: nodeId,
                    })}
                  >
                    分音轨
                  </ActionButton>
                  <ActionButton
                    active={busy === "trim"}
                    disabled={isBusy || !canTrim}
                    onClick={() => void runOperation("trim", {
                      operation: "video.trim",
                      source_node_id: selectedVideoClip?.id || nodeId,
                      range: {
                        start_seconds: Math.max(0, trimStart - (selectedVideoClip?.start || 0)),
                        end_seconds: Math.max(0.05, trimEnd - (selectedVideoClip?.start || 0)),
                      },
                      title: `${selectedVideoItem?.title || title || "视频"} 片段`,
                    })}
                  >
                    导出片段
                  </ActionButton>
                  <ActionButton
                    active={busy === "concat-video"}
                    disabled={isBusy || videoConcatIds.length < 2}
                    onClick={() => void runOperation("concat-video", {
                      operation: "video.concat",
                      source_node_ids: videoConcatIds,
                      title: "拼接视频",
                    })}
                  >
                    拼接视频
                  </ActionButton>
                  <ActionButton
                    active={busy === "concat-audio"}
                    disabled={isBusy || audioConcatIds.length < 2}
                    onClick={() => void runOperation("concat-audio", {
                      operation: "audio.concat",
                      source_node_ids: audioConcatIds,
                      title: "拼接音频",
                    })}
                  >
                    拼接音频
                  </ActionButton>
                </div>
              </section>

              <section className="rounded-md border border-white/10 bg-white/[0.035] p-3">
                <div className="mb-3 flex items-center justify-between">
                  <div className="text-[11px] font-semibold text-zinc-300">当前裁剪</div>
                  <div className="text-[10px] text-zinc-500">{formatTimePrecise(trimStart)} - {formatTimePrecise(trimEnd)}</div>
                </div>
                <div className="text-[11px] leading-5 text-zinc-500">
                  选中“裁剪”后点击画面轴可设定范围，也可以拖动画面片段上的浅绿色手柄微调。
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

        <section
          ref={timelineRef}
          className="relative min-h-0 overflow-auto bg-[#080c12]"
          onPointerDown={handleTimelineBackgroundDown}
          onWheel={(event) => {
            event.preventDefault()
            event.stopPropagation()
            const factor = event.deltaY < 0 ? 1.12 : 0.88
            zoomTimelineAt(pxPerSecond * factor, event.clientX)
          }}
        >
          <div className="relative min-h-full" style={{ width: TRACK_LABEL_WIDTH + timelineWidth }}>
            <div className="sticky top-0 z-20 grid h-7 border-b border-white/[0.07] bg-[#0d1118]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="border-r border-white/[0.07]" />
              <div className="relative">
                {ticks.map((tick) => (
                  <div
                    key={tick}
                    className="absolute top-0 h-full border-l border-white/[0.08] pl-1 text-[10px] leading-7 text-zinc-500"
                    style={{ left: tick * pxPerSecond }}
                  >
                    {formatTime(tick)}
                  </div>
                ))}
              </div>
            </div>

            <div className="relative grid h-[76px] border-b border-white/[0.07]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="flex flex-col justify-center border-r border-white/[0.07] bg-[#0d1118] px-3">
                <div className="text-[11px] font-semibold text-zinc-200">画面轴</div>
                <div className="mt-1 text-[10px] text-zinc-500">拖动定位</div>
              </div>
              <div
                className="relative bg-[#090d13]"
                onDragOver={(event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = "copy"
                }}
                onDrop={(event) => handleTrackDrop("video", event)}
              >
                {videoClips.map((clip) => {
                  const item = mediaById.get(clip.id)
                  if (!item || (item.type !== "video" && item.type !== "image")) return null
                  return (
                    <TimelineClip
                      key={clip.id}
                      clip={clip}
                      item={item}
                      kind="video"
                      pxPerSecond={pxPerSecond}
                      selected={clip.id === selectedClipId}
                      trimStart={clip.id === selectedClipId ? trimStart : undefined}
                      trimEnd={clip.id === selectedClipId ? trimEnd : undefined}
                      currentTime={currentTime}
                      onSelect={() => {
                        setSelectedClipId(clip.id)
                        setTrimStart(clip.start)
                        setTrimEnd(clipEnd(clip))
                      }}
                      onDragStartTime={(start) => updateClipStart("video", clip.id, start)}
                      onTrimEdge={item.type === "video" ? setTrimEdge : undefined}
                    />
                  )
                })}
              </div>
            </div>

            <div className="relative grid h-[76px] border-b border-white/[0.07]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="flex flex-col justify-center border-r border-white/[0.07] bg-[#0d1118] px-3">
                <div className="text-[11px] font-semibold text-zinc-200">音频轴</div>
                <div className="mt-1 text-[10px] text-zinc-500">拖动对齐</div>
              </div>
              <div
                className="relative bg-[#090d13]"
                onDragOver={(event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = "copy"
                }}
                onDrop={(event) => handleTrackDrop("audio", event)}
              >
                {audioClips.length === 0 && (
                  <div className="absolute inset-2 flex items-center justify-center rounded-md border border-dashed border-white/10 text-xs text-zinc-600">
                    将音频拖到这里对齐画面
                  </div>
                )}
                {audioClips.map((clip) => {
                  const item = mediaById.get(clip.id)
                  if (!item || item.type !== "audio") return null
                  return (
                    <TimelineClip
                      key={clip.id}
                      clip={clip}
                      item={item}
                      kind="audio"
                      pxPerSecond={pxPerSecond}
                      currentTime={currentTime}
                      onSelect={() => undefined}
                      onDragStartTime={(start) => updateClipStart("audio", clip.id, start)}
                    />
                  )
                })}
              </div>
            </div>

            <div
              className="absolute bottom-0 top-0 z-30 w-px cursor-ew-resize bg-cyan-100 shadow-[0_0_0_1px_rgba(34,211,238,0.28),0_0_18px_rgba(34,211,238,0.45)]"
              style={{ left: TRACK_LABEL_WIDTH + currentTime * pxPerSecond }}
              onPointerDown={beginPlayheadDrag}
            >
              <div className="-ml-1.5 h-3 w-3 rounded-sm bg-cyan-100" />
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
