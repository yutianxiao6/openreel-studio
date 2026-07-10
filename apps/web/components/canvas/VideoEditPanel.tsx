"use client"

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import type { DragEvent as ReactDragEvent, PointerEvent as ReactPointerEvent } from "react"
import { resolveMediaUrl, runProjectMediaOperation } from "@/lib/api"
import { cn } from "@/lib/utils"

export interface VideoEditPanelMediaNode {
  id: string
  title: string
  type: "video" | "audio" | "image"
  src: string
  sourceNodeId?: string
  synthetic?: boolean
  durationSeconds?: number
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
type TimelineTool = "select" | "blade"
type PreviewScale = "fit" | "50" | "75" | "100"

interface TimelineClipState {
  clipId: string
  mediaId: string
  start: number
  duration: number
  sourceOffset: number
  syncGroupId?: string
  fullSource?: boolean
}

const TRACK_LABEL_WIDTH = 76
const DEFAULT_CLIP_SECONDS = 4
const DEFAULT_TIMELINE_SECONDS = 12
const DEFAULT_PX_PER_SECOND = 84
const MIN_CLIP_SECONDS = 0.25
const SNAP_PIXELS = 10
const PLAYBACK_UI_FRAME_MS = 1000 / 20
const TIMELINE_FRAME_WIDTH = 96
const SPRITE_FRAME_STEPS = [6, 10, 14, 18, 24, 32, 40, 48] as const

const mediaDurationCache = new Map<string, number>()
const mediaDurationRequests = new Map<string, Promise<number>>()

function validDuration(value: unknown): number | null {
  const duration = Number(value)
  return Number.isFinite(duration) && duration > 0 ? duration : null
}

function loadMediaDuration(src: string, type: "video" | "audio"): Promise<number> {
  const cached = mediaDurationCache.get(src)
  if (cached) return Promise.resolve(cached)
  const pending = mediaDurationRequests.get(src)
  if (pending) return pending

  const request = new Promise<number>((resolve, reject) => {
    const media = document.createElement(type)
    media.preload = "metadata"
    media.onloadedmetadata = () => {
      const duration = validDuration(media.duration)
      media.removeAttribute("src")
      media.load()
      if (!duration) {
        reject(new Error("media duration unavailable"))
        return
      }
      mediaDurationCache.set(src, duration)
      resolve(duration)
    }
    media.onerror = () => reject(new Error("media metadata unavailable"))
    media.src = src
  }).finally(() => {
    mediaDurationRequests.delete(src)
  })
  mediaDurationRequests.set(src, request)
  return request
}

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

function clipsShareTimelineRange(a: TimelineClipState, b: TimelineClipState): boolean {
  return Boolean(a.syncGroupId && b.syncGroupId && a.syncGroupId === b.syncGroupId)
}

function createClipId(mediaId: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10)
  return `clip:${mediaId}:${Date.now().toString(36)}:${random}`
}

function createSyncGroupId(seed = "media"): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10)
  return `sync:${seed}:${Date.now().toString(36)}:${random}`
}

function createClip(
  mediaId: string,
  start: number,
  duration: number,
  sourceOffset = 0,
  syncGroupId?: string,
  fullSource = false,
): TimelineClipState {
  return {
    clipId: createClipId(mediaId),
    mediaId,
    start: Math.max(0, start),
    duration: Math.max(MIN_CLIP_SECONDS, duration),
    sourceOffset: Math.max(0, sourceOffset),
    syncGroupId,
    fullSource,
  }
}

function splitClipsAt(
  clips: TimelineClipState[],
  time: number,
  rightGroupIds: Map<string, string>,
  targetClipIds?: Set<string>,
) {
  let selectedRightClipId: string | null = null
  const nextClips = clips.flatMap((clip) => {
    if (targetClipIds && !targetClipIds.has(clip.clipId)) return [clip]
    if (time <= clip.start + MIN_CLIP_SECONDS || time >= clipEnd(clip) - MIN_CLIP_SECONDS) {
      return [clip]
    }
    const leftDuration = time - clip.start
    const rightDuration = clipEnd(clip) - time
    const groupKey = clip.syncGroupId || clip.clipId
    const rightSyncGroupId = rightGroupIds.get(groupKey) || createSyncGroupId(groupKey)
    rightGroupIds.set(groupKey, rightSyncGroupId)
    const rightClip = createClip(
      clip.mediaId,
      time,
      rightDuration,
      clip.sourceOffset + leftDuration,
      rightSyncGroupId,
    )
    selectedRightClipId = selectedRightClipId || rightClip.clipId
    return [
      { ...clip, duration: leftDuration, fullSource: false },
      rightClip,
    ]
  })
  return { clips: nextClips, selectedRightClipId }
}

function mediaTypeLabel(type: VideoEditPanelMediaNode["type"]): string {
  if (type === "video") return "视频"
  if (type === "audio") return "音频"
  return "图片"
}

function mediaSourceKey(item: Pick<VideoEditPanelMediaNode, "id" | "sourceNodeId">): string {
  return item.sourceNodeId || item.id
}

function waveformBars(seed: string, count = 72): number[] {
  let value = seed.split("").reduce((sum, char) => sum + char.charCodeAt(0), 37)
  return Array.from({ length: count }, () => {
    value = (value * 1664525 + 1013904223) % 4294967296
    return 0.18 + (value / 4294967296) * 0.78
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
  projectId,
  nodeId,
  sourceOffset,
  clipDuration,
  sourceDuration,
  width,
  pxPerSecond,
}: {
  projectId: string
  nodeId: string
  sourceOffset: number
  clipDuration: number
  sourceDuration: number
  width: number
  pxPerSecond: number
}) {
  const desiredFrames = Math.max(6, Math.min(48, Math.ceil(sourceDuration * pxPerSecond / TIMELINE_FRAME_WIDTH)))
  const frameCount = SPRITE_FRAME_STEPS.find((value) => value >= desiredFrames) || 48
  const displayCount = Math.max(1, Math.ceil(width / TIMELINE_FRAME_WIDTH))
  const spriteUrl = resolveMediaUrl(
    `/api/video-editor/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/timeline-sprite` +
    `?frame_count=${frameCount}&duration_seconds=${sourceDuration.toFixed(3)}&frame_width=128&frame_height=72`,
  )
  const frameIndexes = Array.from({ length: displayCount }, (_, index) => {
    const sourceTime = sourceOffset + ((index + 0.5) / displayCount) * clipDuration
    return Math.max(0, Math.min(frameCount - 1, Math.floor((sourceTime / sourceDuration) * frameCount)))
  })

  return (
    <div className="absolute inset-0 flex overflow-hidden bg-cyan-950/70" data-openreel-frame-strip="true">
      {frameIndexes.map((frameIndex, index) => (
        <span
          key={`${index}-${frameIndex}`}
          data-openreel-timeline-frame="true"
          data-frame-index={frameIndex}
          className="h-full min-w-0 flex-1 border-r border-black/20 bg-cover bg-no-repeat last:border-r-0"
          style={{
            backgroundImage: `url(${spriteUrl})`,
            backgroundPosition: frameCount > 1 ? `${(frameIndex / (frameCount - 1)) * 100}% center` : "center",
            backgroundSize: `${frameCount * 100}% 100%`,
          }}
        />
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

const MediaBinItem = memo(function MediaBinItem({
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
})

const TimelineClip = memo(function TimelineClip({
  projectId,
  clip,
  item,
  kind,
  activeTool,
  pxPerSecond,
  sourceDuration,
  selected,
  onSelect,
  onDragStartTime,
  onResizeEdge,
  onCutAtTime,
}: {
  projectId: string
  clip: TimelineClipState
  item: VideoEditPanelMediaNode
  kind: "video" | "audio"
  activeTool: TimelineTool
  pxPerSecond: number
  sourceDuration: number | null
  selected?: boolean
  onSelect: (clipId: string) => void
  onDragStartTime: (kind: "video" | "audio", clipId: string, start: number) => void
  onResizeEdge: (kind: "video" | "audio", clipId: string, edge: "start" | "end", edgeTime: number) => void
  onCutAtTime: (time: number, clipId: string) => void
}) {
  const [dragging, setDragging] = useState(false)
  const left = clip.start * pxPerSecond
  const width = Math.max(18, clip.duration * pxPerSecond)

  const beginMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement | null
    if (target?.dataset.edgeHandle) return
    event.preventDefault()
    event.stopPropagation()
    onSelect(clip.clipId)
    if (activeTool === "blade") {
      const rect = event.currentTarget.getBoundingClientRect()
      const localTime = clamp((event.clientX - rect.left) / pxPerSecond, 0, clip.duration)
      onCutAtTime(clip.start + localTime, clip.clipId)
      return
    }
    const startX = event.clientX
    const initialStart = clip.start
    setDragging(true)

    const onMove = (moveEvent: PointerEvent) => {
      const delta = (moveEvent.clientX - startX) / pxPerSecond
      onDragStartTime(kind, clip.clipId, Math.max(0, initialStart + delta))
    }
    const onEnd = () => {
      setDragging(false)
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }

  const beginResize = (edge: "start" | "end", event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    onSelect(clip.clipId)
    const startX = event.clientX
    const initialEdgeTime = edge === "start" ? clip.start : clipEnd(clip)
    const onMove = (moveEvent: PointerEvent) => {
      const delta = (moveEvent.clientX - startX) / pxPerSecond
      onResizeEdge(kind, clip.clipId, edge, initialEdgeTime + delta)
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
      data-clip-kind={kind}
      data-clip-id={clip.clipId}
      data-sync-group-id={clip.syncGroupId || ""}
      data-start={clip.start.toFixed(6)}
      data-duration={clip.duration.toFixed(6)}
      data-source-offset={clip.sourceOffset.toFixed(6)}
      data-source-duration={sourceDuration?.toFixed(6) || ""}
      onPointerDown={beginMove}
      className={cn(
        "absolute top-2 h-[76px] overflow-hidden rounded-md border shadow-sm",
        kind === "video"
          ? "border-cyan-200/30 bg-cyan-300/10"
          : "border-amber-200/30 bg-amber-300/10",
        selected && "ring-1 ring-cyan-100/80",
        activeTool === "blade" && "cursor-crosshair",
        dragging && "cursor-grabbing opacity-85",
        !dragging && activeTool !== "blade" && "cursor-grab",
      )}
      style={{ left, width }}
    >
      {kind === "video" ? (
        <>
          {item.type === "image" ? (
            <img src={item.src} alt="" className="absolute inset-0 h-full w-full object-cover opacity-90" draggable={false} />
          ) : sourceDuration ? (
            <VideoThumbnailStrip
              projectId={projectId}
              nodeId={item.id}
              sourceOffset={clip.sourceOffset}
              clipDuration={clip.duration}
              sourceDuration={sourceDuration}
              width={width}
              pxPerSecond={pxPerSecond}
            />
          ) : (
            <div className="absolute inset-0 animate-pulse bg-[linear-gradient(110deg,rgba(8,47,73,.75),rgba(14,116,144,.28),rgba(8,47,73,.75))]" />
          )}
          <div className="absolute inset-0 bg-gradient-to-r from-black/10 via-transparent to-black/20" />
        </>
      ) : (
        <div className="absolute inset-x-2 bottom-2 flex h-11 items-center gap-[3px]">
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
      <div className="absolute bottom-1 right-2 rounded bg-black/55 px-1.5 py-0.5 text-[9px] font-medium tabular-nums text-zinc-200">
        源 {formatTimePrecise(clip.sourceOffset)}–{formatTimePrecise(clip.sourceOffset + clip.duration)}
      </div>
      <button
        type="button"
        data-edge-handle="start"
        onPointerDown={(event) => beginResize("start", event)}
        className={cn(
          "absolute bottom-0 left-0 top-0 z-10 w-3 cursor-ew-resize rounded-l border-l-2 transition",
          kind === "video"
            ? "border-cyan-100/90 bg-cyan-100/10 hover:bg-cyan-100/28"
            : "border-amber-100/90 bg-amber-100/10 hover:bg-amber-100/28",
        )}
        title="收放起点"
        aria-label="收放起点"
      />
      <button
        type="button"
        data-edge-handle="end"
        onPointerDown={(event) => beginResize("end", event)}
        className={cn(
          "absolute bottom-0 right-0 top-0 z-10 w-3 cursor-ew-resize rounded-r border-r-2 transition",
          kind === "video"
            ? "border-cyan-100/90 bg-cyan-100/10 hover:bg-cyan-100/28"
            : "border-amber-100/90 bg-amber-100/10 hover:bg-amber-100/28",
        )}
        title="收放终点"
        aria-label="收放终点"
      />
    </div>
  )
})

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
  const playheadRef = useRef<HTMLDivElement | null>(null)
  const currentTimeRef = useRef(0)
  const pxPerSecondRef = useRef(DEFAULT_PX_PER_SECOND)
  const pendingZoomRef = useRef<{ anchorTime: number; localX: number } | null>(null)
  const initializedNodeRef = useRef<string | null>(null)
  const [sourceDurations, setSourceDurations] = useState<Record<string, number>>({})
  const [currentTime, setCurrentTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [tool, setTool] = useState<TimelineTool>("select")
  const [pxPerSecond, setPxPerSecond] = useState(DEFAULT_PX_PER_SECOND)
  const [previewScale, setPreviewScale] = useState<PreviewScale>("fit")
  const [busy, setBusy] = useState<BusyAction>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null)
  const [videoClips, setVideoClips] = useState<TimelineClipState[]>([])
  const [audioClips, setAudioClips] = useState<TimelineClipState[]>([])
  currentTimeRef.current = currentTime

  const visualItems = useMemo(
    () => mediaNodes.filter((item) => (item.type === "video" || item.type === "image") && item.src),
    [mediaNodes],
  )
  const videoItems = useMemo(() => mediaNodes.filter((item) => item.type === "video" && item.src), [mediaNodes])
  const audioItems = useMemo(() => mediaNodes.filter((item) => item.type === "audio" && item.src), [mediaNodes])
  const sourceVideoItem = useMemo(
    () => videoItems.find((item) => item.id === nodeId) || videoItems[0],
    [nodeId, videoItems],
  )
  const primarySourceKey = sourceVideoItem ? mediaSourceKey(sourceVideoItem) : undefined
  const primarySyncGroupId = primarySourceKey ? `sync:${primarySourceKey}` : undefined
  const embeddedAudioItems = useMemo<VideoEditPanelMediaNode[]>(() => (
    videoItems.map((item) => ({
      id: `embedded-audio:${item.id}`,
      sourceNodeId: mediaSourceKey(item),
      synthetic: true,
      type: "audio",
      title: `${item.title || "视频"} 原声`,
      src: item.src,
    }))
  ), [videoItems])
  const embeddedAudioByVideoId = useMemo(
    () => new Map(embeddedAudioItems.map((item) => [item.id.replace(/^embedded-audio:/, ""), item])),
    [embeddedAudioItems],
  )
  const explicitAudioBySourceKey = useMemo(() => {
    const entries = audioItems
      .filter((item) => Boolean(item.sourceNodeId))
      .map((item) => [mediaSourceKey(item), item] as const)
    return new Map(entries)
  }, [audioItems])
  const audioTimelineItems = useMemo(
    () => [...audioItems, ...embeddedAudioItems],
    [audioItems, embeddedAudioItems],
  )
  const mediaById = useMemo(() => {
    const entries = [...mediaNodes]
    entries.push(...embeddedAudioItems)
    return new Map(entries.map((item) => [item.id, item]))
  }, [embeddedAudioItems, mediaNodes])
  const audioItemForVideo = useCallback((item: VideoEditPanelMediaNode | undefined) => {
    if (!item || item.type !== "video") return undefined
    return explicitAudioBySourceKey.get(mediaSourceKey(item)) || embeddedAudioByVideoId.get(item.id)
  }, [embeddedAudioByVideoId, explicitAudioBySourceKey])
  const registerSourceDuration = useCallback((src: string, value: unknown) => {
    const duration = validDuration(value)
    if (!src || !duration) return
    setSourceDurations((current) => (
      Math.abs((current[src] || 0) - duration) < 0.001
        ? current
        : { ...current, [src]: duration }
    ))
  }, [])
  const sourceDurationForItem = useCallback((item: VideoEditPanelMediaNode | undefined): number | null => {
    if (!item || item.type === "image") return null
    return validDuration(sourceDurations[item.src]) || validDuration(item.durationSeconds)
  }, [sourceDurations])
  const sourceDurationForClip = useCallback((clip: TimelineClipState): number | null => (
    sourceDurationForItem(mediaById.get(clip.mediaId))
  ), [mediaById, sourceDurationForItem])
  const selectedTimelineClip = useMemo(() => (
    [...videoClips, ...audioClips].find((clip) => clip.clipId === selectedClipId)
  ), [audioClips, selectedClipId, videoClips])
  const selectedSyncGroupId = selectedTimelineClip?.syncGroupId
  const selectedVideoClip = useMemo(() => (
    videoClips.find((clip) => clip.clipId === selectedClipId) ||
    (selectedSyncGroupId ? videoClips.find((clip) => clip.syncGroupId === selectedSyncGroupId) : undefined) ||
    videoClips[0]
  ), [selectedClipId, selectedSyncGroupId, videoClips])
  const currentVideoClip = useMemo(
    () => videoClips.find((clip) => currentTime >= clip.start && currentTime < clipEnd(clip)),
    [currentTime, videoClips],
  )
  const currentAudioClip = useMemo(
    () => audioClips.find((clip) => currentTime >= clip.start && currentTime < clipEnd(clip)),
    [audioClips, currentTime],
  )
  const currentVideoItem = currentVideoClip ? mediaById.get(currentVideoClip.mediaId) : undefined
  const currentAudioItem = currentAudioClip ? mediaById.get(currentAudioClip.mediaId) : undefined
  const selectedVideoItem = selectedVideoClip ? mediaById.get(selectedVideoClip.mediaId) : undefined
  const playAudioThroughVideo = Boolean(
    currentVideoClip &&
    currentAudioClip &&
    currentVideoItem?.type === "video" &&
    currentAudioItem?.synthetic &&
    currentVideoItem.src === currentAudioItem.src &&
    currentVideoClip.syncGroupId &&
    currentVideoClip.syncGroupId === currentAudioClip.syncGroupId,
  )
  const playbackEnd = useMemo(() => (
    Math.max(0, ...videoClips.map(clipEnd), ...audioClips.map(clipEnd))
  ), [audioClips, videoClips])
  const timelineDuration = useMemo(() => {
    const lastClipEnd = Math.max(
      0,
      ...videoClips.map(clipEnd),
      ...audioClips.map(clipEnd),
    )
    return Math.max(DEFAULT_TIMELINE_SECONDS, Math.ceil(lastClipEnd + 2))
  }, [audioClips, videoClips])
  const timelineWidth = timelineDuration * pxPerSecond
  const ticks = useMemo(() => {
    const step = pxPerSecond >= 120 ? 1 : pxPerSecond >= 72 ? 2 : 5
    const count = Math.floor(timelineDuration / step) + 1
    return Array.from({ length: count }, (_, index) => index * step)
  }, [pxPerSecond, timelineDuration])

  useEffect(() => {
    let cancelled = false
    for (const item of mediaNodes) {
      if (item.type === "image" || !item.src) continue
      const declaredDuration = validDuration(item.durationSeconds)
      if (declaredDuration) registerSourceDuration(item.src, declaredDuration)
      void loadMediaDuration(item.src, item.type).then((duration) => {
        if (!cancelled) registerSourceDuration(item.src, duration)
      }).catch(() => undefined)
    }
    return () => {
      cancelled = true
    }
  }, [mediaNodes, registerSourceDuration])

  useEffect(() => {
    if (initializedNodeRef.current === nodeId) return
    const primary = videoItems.find((item) => item.id === nodeId) || visualItems[0]
    if (!primary) return
    initializedNodeRef.current = nodeId
    const duration = sourceDurationForItem(primary) || DEFAULT_CLIP_SECONDS
    const syncGroupId = primary.type === "video" && mediaSourceKey(primary) === primarySourceKey
      ? primarySyncGroupId
      : undefined
    setVideoClips([createClip(primary.id, 0, duration, 0, syncGroupId, primary.type === "video")])
    const primaryAudio = primary.type === "video" ? audioItemForVideo(primary) : undefined
    setAudioClips(primaryAudio
      ? [createClip(primaryAudio.id, 0, duration, 0, syncGroupId, true)]
      : [])
    setSelectedClipId(null)
    setCurrentTime(0)
    setPlaying(false)
    setError(null)
  }, [audioItemForVideo, nodeId, primarySourceKey, primarySyncGroupId, sourceDurationForItem, videoItems, visualItems])

  useEffect(() => {
    setVideoClips((current) => current
      .filter((clip) => visualItems.some((item) => item.id === clip.mediaId))
      .map((clip) => {
        const duration = sourceDurationForClip(clip)
        return clip.fullSource && duration && Math.abs(clip.duration - duration) > 0.001
          ? { ...clip, duration }
          : clip
      }))
    setAudioClips((current) => current
      .filter((clip) => audioTimelineItems.some((item) => item.id === clip.mediaId))
      .map((clip) => {
        const duration = sourceDurationForClip(clip)
        return clip.fullSource && duration && Math.abs(clip.duration - duration) > 0.001
          ? { ...clip, duration }
          : clip
      }))
  }, [audioTimelineItems, sourceDurationForClip, visualItems])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !currentVideoClip || playing) return
    const item = mediaById.get(currentVideoClip.mediaId)
    if (item?.type !== "video") {
      video.pause()
      return
    }
    const localTime = currentVideoClip.sourceOffset + clamp(currentTime - currentVideoClip.start, 0, currentVideoClip.duration)
    if (Math.abs((video.currentTime || 0) - localTime) > 0.08) {
      video.currentTime = localTime
    }
    video.pause()
  }, [currentTime, currentVideoClip, mediaById, playing])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !currentAudioClip || !currentAudioItem || playAudioThroughVideo) {
      audio?.pause()
      return
    }
    if (playing) return
    const localTime = currentAudioClip.sourceOffset + clamp(currentTime - currentAudioClip.start, 0, currentAudioClip.duration)
    if (Math.abs((audio.currentTime || 0) - localTime) > 0.08) {
      audio.currentTime = localTime
    }
    audio.pause()
  }, [currentAudioClip, currentAudioItem, currentTime, playAudioThroughVideo, playing])

  useEffect(() => {
    const video = videoRef.current
    const audio = audioRef.current
    if (!playing) {
      video?.pause()
      audio?.pause()
      return
    }
    const timelineTime = currentTimeRef.current
    const mediaStarts: Promise<void>[] = []
    if (video && currentVideoClip && currentVideoItem?.type === "video") {
      const localTime = currentVideoClip.sourceOffset + clamp(timelineTime - currentVideoClip.start, 0, currentVideoClip.duration)
      if (Math.abs((video.currentTime || 0) - localTime) > 0.15) video.currentTime = localTime
      mediaStarts.push(video.play())
    }
    if (audio && currentAudioClip && currentAudioItem && !playAudioThroughVideo) {
      const localTime = currentAudioClip.sourceOffset + clamp(timelineTime - currentAudioClip.start, 0, currentAudioClip.duration)
      if (Math.abs((audio.currentTime || 0) - localTime) > 0.15) audio.currentTime = localTime
      mediaStarts.push(audio.play())
    }
    void Promise.all(mediaStarts).catch(() => undefined)
  }, [currentAudioClip, currentAudioItem, currentVideoClip, currentVideoItem, playAudioThroughVideo, playing])

  const seekTo = useCallback((time: number) => {
    const nextTime = clamp(time, 0, timelineDuration)
    currentTimeRef.current = nextTime
    setCurrentTime(nextTime)
  }, [timelineDuration])

  const zoomTimelineAt = useCallback((nextValue: number, clientX?: number) => {
    const next = clamp(nextValue, 42, 220)
    const currentScale = pxPerSecondRef.current
    const container = timelineRef.current
    if (!container || clientX == null) {
      pxPerSecondRef.current = next
      setPxPerSecond(next)
      return
    }
    const rect = container.getBoundingClientRect()
    const localX = clientX - rect.left
    const anchorTime = Math.max(0, (localX + container.scrollLeft - TRACK_LABEL_WIDTH) / currentScale)
    pendingZoomRef.current = { anchorTime, localX }
    pxPerSecondRef.current = next
    setPxPerSecond(next)
  }, [])

  useLayoutEffect(() => {
    const container = timelineRef.current
    const pending = pendingZoomRef.current
    if (!container || !pending) return
    container.scrollLeft = Math.max(0, pending.anchorTime * pxPerSecond + TRACK_LABEL_WIDTH - pending.localX)
    pendingZoomRef.current = null
  }, [pxPerSecond])

  useEffect(() => {
    const container = timelineRef.current
    if (!container) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      event.stopPropagation()
      const delta = event.deltaY || event.deltaX
      const factor = Math.exp(-delta * 0.0018)
      zoomTimelineAt(pxPerSecondRef.current * factor, event.clientX)
    }
    container.addEventListener("wheel", onWheel, { passive: false })
    return () => container.removeEventListener("wheel", onWheel)
  }, [zoomTimelineAt])

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
    const nextStart = playbackEnd > 0 && currentTime >= playbackEnd - 0.02 ? 0 : currentTime
    if (playbackEnd <= 0 || nextStart >= playbackEnd) return
    if (nextStart !== currentTime) {
      currentTimeRef.current = nextStart
      setCurrentTime(nextStart)
    }
    setPlaying(true)
  }, [currentTime, playbackEnd, playing])

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
    if (!playing) return
    if (playbackEnd <= 0) {
      setPlaying(false)
      return
    }
    let frame = 0
    let last = performance.now()
    let lastUiCommit = last
    let timelineTime = currentTimeRef.current
    const tick = (now: number) => {
      const deltaSeconds = (now - last) / 1000
      last = now
      timelineTime += deltaSeconds
      if (timelineTime >= playbackEnd - 0.015) {
        videoRef.current?.pause()
        audioRef.current?.pause()
        currentTimeRef.current = playbackEnd
        if (playheadRef.current) playheadRef.current.style.left = `${TRACK_LABEL_WIDTH + playbackEnd * pxPerSecond}px`
        setCurrentTime(playbackEnd)
        setPlaying(false)
        return
      }
      currentTimeRef.current = timelineTime
      if (playheadRef.current) playheadRef.current.style.left = `${TRACK_LABEL_WIDTH + timelineTime * pxPerSecond}px`
      if (now - lastUiCommit >= PLAYBACK_UI_FRAME_MS) {
        lastUiCommit = now
        setCurrentTime(timelineTime)
      }
      frame = window.requestAnimationFrame(tick)
    }
    frame = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame)
  }, [playbackEnd, playing, pxPerSecond])

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

  const snapTimeToBoundaries = useCallback((value: number, excludeClipIds?: Iterable<string>): number => {
    const excluded = new Set(excludeClipIds || [])
    const threshold = Math.max(0.05, SNAP_PIXELS / pxPerSecond)
    const targets = [...videoClips, ...audioClips]
      .filter((clip) => !excluded.has(clip.clipId))
      .flatMap((clip) => [clip.start, clipEnd(clip)])
    const closest = targets.reduce<{ value: number; distance: number } | null>((best, target) => {
      const distance = Math.abs(value - target)
      if (distance > threshold) return best
      if (!best || distance < best.distance) return { value: target, distance }
      return best
    }, null)
    return closest ? closest.value : value
  }, [audioClips, pxPerSecond, videoClips])

  const snapClipStart = useCallback((clip: TimelineClipState, start: number, excludeClipIds?: Iterable<string>): number => {
    const rawStart = Math.max(0, start)
    const excluded = new Set(excludeClipIds || [clip.clipId])
    excluded.add(clip.clipId)
    const snappedStart = snapTimeToBoundaries(rawStart, excluded)
    const snappedEndStart = snapTimeToBoundaries(rawStart + clip.duration, excluded) - clip.duration
    const startDistance = Math.abs(snappedStart - rawStart)
    const endDistance = Math.abs(snappedEndStart - rawStart)
    return Math.max(0, endDistance < startDistance ? snappedEndStart : snappedStart)
  }, [snapTimeToBoundaries])

  const insertMediaItem = useCallback((item: VideoEditPanelMediaNode, startAt?: number) => {
    const duration = item.type === "image"
      ? DEFAULT_CLIP_SECONDS
      : sourceDurationForItem(item) || DEFAULT_CLIP_SECONDS
    const start = snapTimeToBoundaries(Math.max(0, startAt ?? currentTimeRef.current))
    if (item.type === "video") {
      const syncGroupId = createSyncGroupId(mediaSourceKey(item))
      const clip = createClip(item.id, start, duration, 0, syncGroupId, true)
      setVideoClips((current) => [...current, clip])
      const linkedAudio = audioItemForVideo(item)
      if (linkedAudio) {
        setAudioClips((current) => [...current, createClip(linkedAudio.id, start, duration, 0, syncGroupId, true)])
      }
      setSelectedClipId(clip.clipId)
      return
    }
    const clip = createClip(item.id, start, duration, 0, undefined, item.type === "audio")
    if (item.type === "image") {
      setVideoClips((current) => [...current, clip])
      setSelectedClipId(clip.clipId)
      return
    }
    setAudioClips((current) => [...current, clip])
  }, [audioItemForVideo, snapTimeToBoundaries, sourceDurationForItem])

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

  const updateClipStart = useCallback((kind: "video" | "audio", clipId: string, start: number) => {
    if (kind === "video") {
      const original = videoClips.find((clip) => clip.clipId === clipId)
      if (!original) return
      const linkedAudioBaselines = new Map(
        audioClips
          .filter((clip) => mediaById.get(original.mediaId)?.type === "video" && clipsShareTimelineRange(clip, original))
          .map((clip) => [clip.clipId, clip]),
      )
      const linkedClipIds = new Set([original.clipId, ...linkedAudioBaselines.keys()])
      const nextStart = snapClipStart(original, start, linkedClipIds)
      setVideoClips((clips) => (
        clips.map((clip) => clip.clipId === clipId ? { ...clip, start: nextStart } : clip)
      ))
      if (linkedAudioBaselines.size > 0) {
        setAudioClips((clips) => (
          clips.map((clip) => {
            const baseline = linkedAudioBaselines.get(clip.clipId)
            return baseline
              ? {
                  ...clip,
                  start: nextStart,
                  duration: original.duration,
                  sourceOffset: original.sourceOffset,
                  fullSource: original.fullSource,
                }
              : clip
          })
        ))
      }
      return
    }
    const original = audioClips.find((clip) => clip.clipId === clipId)
    if (!original) return
    const linkedVideoBaselines = new Map(
      videoClips
        .filter((clip) => clipsShareTimelineRange(clip, original))
        .map((clip) => [clip.clipId, clip]),
    )
    const linkedClipIds = new Set([original.clipId, ...linkedVideoBaselines.keys()])
    const nextStart = snapClipStart(original, start, linkedClipIds)
    setAudioClips((clips) => (
      clips.map((clip) => clip.clipId === clipId ? { ...clip, start: nextStart } : clip)
    ))
    if (linkedVideoBaselines.size > 0) {
      setVideoClips((clips) => (
        clips.map((clip) => {
          const baseline = linkedVideoBaselines.get(clip.clipId)
          return baseline
            ? {
                ...clip,
                start: nextStart,
                duration: original.duration,
                sourceOffset: original.sourceOffset,
                fullSource: original.fullSource,
              }
            : clip
        })
      ))
    }
  }, [audioClips, mediaById, snapClipStart, videoClips])

  const resizeClipEdge = useCallback((
    kind: "video" | "audio",
    clipId: string,
    edge: "start" | "end",
    edgeTime: number,
  ) => {
    const primaryClips = kind === "video" ? videoClips : audioClips
    const linkedClips = kind === "video" ? audioClips : videoClips
    const original = primaryClips.find((clip) => clip.clipId === clipId)
    if (!original) return
    const linked = linkedClips.filter((clip) => clipsShareTimelineRange(clip, original))
    const group = [original, ...linked]
    const groupIds = new Set(group.map((clip) => clip.clipId))
    const snappedEdge = snapTimeToBoundaries(edgeTime, groupIds)

    let nextStart = original.start
    let nextDuration = original.duration
    let nextSourceOffset = original.sourceOffset
    if (edge === "start") {
      const earliestStart = Math.max(0, ...group.map((clip) => clip.start - clip.sourceOffset))
      const latestStart = Math.min(...group.map((clip) => clipEnd(clip) - MIN_CLIP_SECONDS))
      nextStart = clamp(snappedEdge, earliestStart, latestStart)
      const delta = nextStart - original.start
      nextDuration = Math.max(MIN_CLIP_SECONDS, original.duration - delta)
      nextSourceOffset = Math.max(0, original.sourceOffset + delta)
    } else {
      const minimumEnd = Math.max(...group.map((clip) => clip.start + MIN_CLIP_SECONDS))
      const boundedEnds = group.map((clip) => {
        const item = mediaById.get(clip.mediaId)
        if (item?.type === "image") return Number.POSITIVE_INFINITY
        const sourceDuration = sourceDurationForClip(clip)
        const safeSourceDuration = sourceDuration || clip.sourceOffset + clip.duration
        return clip.start + Math.max(MIN_CLIP_SECONDS, safeSourceDuration - clip.sourceOffset)
      })
      const maximumEnd = Math.min(...boundedEnds)
      const nextEnd = clamp(snappedEdge, minimumEnd, maximumEnd)
      nextDuration = Math.max(MIN_CLIP_SECONDS, nextEnd - original.start)
    }

    const applyRange = (clip: TimelineClipState): TimelineClipState => ({
      ...clip,
      start: nextStart,
      duration: nextDuration,
      sourceOffset: nextSourceOffset,
      fullSource: false,
    })
    if (kind === "video") {
      setVideoClips((clips) => clips.map((clip) => clip.clipId === clipId ? applyRange(clip) : clip))
      if (linked.length > 0) {
        const linkedIds = new Set(linked.map((clip) => clip.clipId))
        setAudioClips((clips) => clips.map((clip) => linkedIds.has(clip.clipId) ? applyRange(clip) : clip))
      }
      return
    }
    setAudioClips((clips) => clips.map((clip) => clip.clipId === clipId ? applyRange(clip) : clip))
    if (linked.length > 0) {
      const linkedIds = new Set(linked.map((clip) => clip.clipId))
      setVideoClips((clips) => clips.map((clip) => linkedIds.has(clip.clipId) ? applyRange(clip) : clip))
    }
  }, [audioClips, mediaById, snapTimeToBoundaries, sourceDurationForClip, videoClips])

  const splitTimelineAt = useCallback((time: number, targetClipId?: string) => {
    const target = targetClipId
      ? [...videoClips, ...audioClips].find((clip) => clip.clipId === targetClipId)
      : undefined
    const targetClipIds = target
      ? new Set([...videoClips, ...audioClips]
          .filter((clip) => clip.clipId === target.clipId || clipsShareTimelineRange(clip, target))
          .map((clip) => clip.clipId))
      : undefined
    const rightGroupIds = new Map<string, string>()
    const videoResult = splitClipsAt(videoClips, time, rightGroupIds, targetClipIds)
    const audioResult = splitClipsAt(audioClips, time, rightGroupIds, targetClipIds)
    setVideoClips(videoResult.clips)
    setAudioClips(audioResult.clips)
    if (videoResult.selectedRightClipId || audioResult.selectedRightClipId) {
      setSelectedClipId(videoResult.selectedRightClipId || audioResult.selectedRightClipId)
    }
    seekTo(time)
  }, [audioClips, seekTo, videoClips])

  const handleTimelineBackgroundDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null
    const container = timelineRef.current
    if (!container) return
    const time = timeFromPointer(event, container, pxPerSecond)
    if (target?.closest("[data-openreel-timeline-clip]")) return
    if (tool === "blade") {
      splitTimelineAt(time)
      return
    }
    beginPlayheadDrag(event)
  }

  const canTrim = Boolean(selectedVideoClip && selectedVideoItem?.type === "video" && selectedVideoClip.duration > MIN_CLIP_SECONDS)
  const selectedSourceDuration = selectedVideoClip ? sourceDurationForClip(selectedVideoClip) : null
  const videoConcatIds = videoClips.map((clip) => clip.mediaId).filter((id) => videoItems.some((item) => item.id === id))
  const audioConcatIds = audioClips.map((clip) => clip.mediaId).filter((id) => audioItems.some((item) => item.id === id))
  const isBusy = Boolean(busy)
  const previewScaleStyle = previewScale === "fit"
    ? { height: "min(100%, 280px)", width: "auto", maxWidth: "100%" }
    : { width: `${previewScale}%`, maxWidth: "640px" }

  return (
    <div
      className="openreel-video-edit-panel nodrag nowheel fixed inset-x-3 bottom-3 top-4 z-[94] overflow-hidden rounded-lg border border-white/10 bg-[#070b10]/98 text-zinc-100 shadow-[0_28px_90px_rgba(0,0,0,0.68)] backdrop-blur-xl"
      data-openreel-workflow-ui="true"
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      onWheel={(event) => event.stopPropagation()}
    >
      <div className="flex h-9 items-center justify-between border-b border-white/10 bg-[#0b0f16] px-3">
        <div className="flex min-w-0 items-center gap-2">
          <div className="max-w-[280px] truncate text-xs font-semibold text-zinc-200">{title || "视频剪辑"}</div>
          <div className="text-[10px] tabular-nums text-zinc-500">{formatTimePrecise(currentTime)} / {formatTimePrecise(playbackEnd)}</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="h-7 rounded-md border border-white/10 px-3 text-xs text-zinc-300 transition hover:bg-white/[0.07]"
        >
          关闭
        </button>
      </div>

      <div className="grid h-[calc(100%-2.25rem)] w-full min-w-0 grid-rows-[minmax(245px,40%)_minmax(320px,1fr)] bg-[#070b10]">
        <div className="grid w-full min-w-0 min-h-0 grid-cols-[220px_minmax(420px,1fr)_300px] border-b border-white/10 max-xl:grid-cols-[200px_minmax(360px,1fr)_280px] max-lg:grid-cols-1 max-lg:overflow-y-auto">
          <aside data-openreel-media-bin="true" className="flex min-h-0 flex-col border-r border-white/10 bg-[#0b1017]">
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

          <main data-openreel-preview-pane="true" className="flex min-h-0 min-w-0 flex-col bg-[#0b1017]">
            <div className="flex min-h-0 flex-1 items-center justify-center bg-[radial-gradient(circle_at_center,rgba(39,52,68,.42),rgba(7,11,16,.96)_72%)] p-2">
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
                      data-openreel-preview-video="true"
                      src={currentVideoItem.src || videoUrl}
                      muted={Boolean(currentAudioItem) && !playAudioThroughVideo}
                      preload="metadata"
                      className="h-full w-full object-contain [color-scheme:dark]"
                      onLoadedMetadata={(event) => {
                        const nextDuration = Number(event.currentTarget.duration || 0)
                        registerSourceDuration(currentVideoItem.src, nextDuration)
                      }}
                    />
                  )
                ) : (
                  <div className="text-xs text-zinc-500">播放头不在视频片段上</div>
                )}
              </div>
              {currentAudioItem && !playAudioThroughVideo && (
                <audio ref={audioRef} src={currentAudioItem.src} preload="metadata" />
              )}
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
                <ActionButton
                  disabled={isBusy || currentVideoItem?.type !== "video"}
                  onClick={() => void runOperation("frame", {
                    operation: "video.export_frame",
                    source_node_id: currentVideoClip?.mediaId || nodeId,
                    frame_mode: "time",
                    time_seconds: Math.max(0, (currentVideoClip?.sourceOffset || 0) + currentTime - (currentVideoClip?.start || 0)),
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
              </div>
            </div>
          </main>

          <aside data-openreel-inspector-pane="true" className="min-h-0 border-l border-white/10 bg-[#0c1118]">
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
                      source_node_id: selectedVideoClip?.mediaId || nodeId,
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
                      source_node_id: selectedVideoClip?.mediaId || nodeId,
                      range: {
                        start_seconds: Math.max(0, selectedVideoClip?.sourceOffset || 0),
                        end_seconds: Math.max(
                          MIN_CLIP_SECONDS,
                          (selectedVideoClip?.sourceOffset || 0) + (selectedVideoClip?.duration || MIN_CLIP_SECONDS),
                        ),
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
                  <div className="text-[11px] font-semibold text-zinc-300">轨道编辑</div>
                  <div className="text-[10px] text-zinc-500">吸附 {SNAP_PIXELS}px</div>
                </div>
                {selectedVideoClip && (
                  <div className="mb-2 rounded border border-white/[0.07] bg-black/20 px-2 py-1.5 text-[10px] tabular-nums text-zinc-400">
                    片段 {formatTimePrecise(selectedVideoClip.duration)} · 源 {formatTimePrecise(selectedVideoClip.sourceOffset)}–{formatTimePrecise(selectedVideoClip.sourceOffset + selectedVideoClip.duration)}
                    {selectedSourceDuration ? ` / ${formatTimePrecise(selectedSourceDuration)}` : ""}
                  </div>
                )}
                <div className="text-[11px] leading-5 text-zinc-500">
                  视频和音频收放不会超出源素材。选择“切割”后点击片段，会把画面及绑定音轨同步切成前后两段。
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

        <section className="flex w-full min-h-0 min-w-0 flex-col bg-[#080c12]">
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-white/[0.08] bg-[#0d1219] px-2.5">
            <div className="flex items-center gap-2">
              <span className="px-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-500">时间线 · 2 轨</span>
              <ToolButton label="选择" glyph="S" active={tool === "select"} onClick={() => setTool("select")} />
              <ToolButton label="切割" glyph="C" active={tool === "blade"} onClick={() => setTool("blade")} />
              <span className="hidden text-[10px] text-zinc-600 md:inline">滚轮以指针位置缩放</span>
            </div>
            <div className="flex items-center gap-1.5 text-[10px] text-zinc-500">
              <button
                type="button"
                onClick={() => zoomTimelineAt(pxPerSecond - 14)}
                className="h-7 w-7 rounded border border-white/10 text-sm text-zinc-300 hover:bg-white/[0.07]"
                title="缩小时间线"
                aria-label="缩小时间线"
              >
                −
              </button>
              <span className="min-w-[58px] rounded border border-white/10 px-1.5 py-1 text-center tabular-nums">{Math.round(pxPerSecond)} px/s</span>
              <button
                type="button"
                onClick={() => zoomTimelineAt(pxPerSecond + 14)}
                className="h-7 w-7 rounded border border-white/10 text-sm text-zinc-300 hover:bg-white/[0.07]"
                title="放大时间线"
                aria-label="放大时间线"
              >
                +
              </button>
            </div>
          </div>
          <div
            ref={timelineRef}
            data-openreel-timeline-scroll="true"
            data-px-per-second={pxPerSecond.toFixed(4)}
            data-track-label-width={TRACK_LABEL_WIDTH}
            className="relative w-full min-h-0 min-w-0 flex-1 overflow-auto bg-[#080c12]"
            onPointerDown={handleTimelineBackgroundDown}
          >
            <div className="relative min-h-full" style={{ width: TRACK_LABEL_WIDTH + timelineWidth }}>
            <div className="sticky top-0 z-20 grid h-7 border-b border-white/[0.07] bg-[#0d1118]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="sticky left-0 z-30 border-r border-white/[0.07] bg-[#0d1118]" />
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

            <div className="relative grid h-[92px] border-b border-white/[0.07]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="sticky left-0 z-10 flex flex-col justify-center border-r border-white/[0.07] bg-[#0d1118] px-3">
                <div className="text-[11px] font-semibold text-zinc-200">V1</div>
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
                  const item = mediaById.get(clip.mediaId)
                  if (!item || (item.type !== "video" && item.type !== "image")) return null
                  return (
                    <TimelineClip
                      key={clip.clipId}
                      projectId={projectId}
                      clip={clip}
                      item={item}
                      kind="video"
                      activeTool={tool}
                      pxPerSecond={pxPerSecond}
                      sourceDuration={sourceDurationForClip(clip)}
                      selected={clip.clipId === selectedClipId || Boolean(selectedSyncGroupId && clip.syncGroupId === selectedSyncGroupId)}
                      onSelect={setSelectedClipId}
                      onDragStartTime={updateClipStart}
                      onResizeEdge={resizeClipEdge}
                      onCutAtTime={splitTimelineAt}
                    />
                  )
                })}
              </div>
            </div>

            <div className="relative grid h-[92px] border-b border-white/[0.07]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="sticky left-0 z-10 flex flex-col justify-center border-r border-white/[0.07] bg-[#0d1118] px-3">
                <div className="text-[11px] font-semibold text-zinc-200">A1</div>
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
                  const item = mediaById.get(clip.mediaId)
                  if (!item || item.type !== "audio") return null
                  return (
                    <TimelineClip
                      key={clip.clipId}
                      projectId={projectId}
                      clip={clip}
                      item={item}
                      kind="audio"
                      activeTool={tool}
                      pxPerSecond={pxPerSecond}
                      sourceDuration={sourceDurationForClip(clip)}
                      selected={clip.clipId === selectedClipId || Boolean(selectedSyncGroupId && clip.syncGroupId === selectedSyncGroupId)}
                      onSelect={setSelectedClipId}
                      onDragStartTime={updateClipStart}
                      onResizeEdge={resizeClipEdge}
                      onCutAtTime={splitTimelineAt}
                    />
                  )
                })}
              </div>
            </div>

            <div
              ref={playheadRef}
              className="absolute bottom-0 top-0 z-30 w-px cursor-ew-resize bg-cyan-100 shadow-[0_0_0_1px_rgba(34,211,238,0.28),0_0_18px_rgba(34,211,238,0.45)]"
              style={{ left: TRACK_LABEL_WIDTH + currentTime * pxPerSecond }}
              onPointerDown={beginPlayheadDrag}
            >
              <div className="-ml-1.5 h-3 w-3 rounded-sm bg-cyan-100" />
            </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
