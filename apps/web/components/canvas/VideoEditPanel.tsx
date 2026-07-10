"use client"

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import type { DragEvent as ReactDragEvent, PointerEvent as ReactPointerEvent } from "react"
import { runProjectMediaOperation } from "@/lib/api"
import {
  getVideoEditorFrameTileUrl,
  getVideoEditorMediaIndex,
  getVideoEditorSequence,
  getVideoEditorWaveformManifest,
  getVideoEditorWaveformPage,
  saveVideoEditorSequence,
  type VideoEditorMediaIndex,
  type VideoEditorSequenceSpec,
  type VideoEditorWaveformManifest,
  type VideoEditorWaveformPage,
} from "@/lib/videoEditorApi"
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
  startFrame: number
  durationFrames: number
  sourceInFrame: number
  syncGroupId?: string
  fullSource?: boolean
  gainDb?: number
  muted?: boolean
  fadeInFrames?: number
  fadeOutFrames?: number
}

interface EditorSnapshot {
  videoClips: TimelineClipState[]
  audioClips: TimelineClipState[]
  audioTrackMuted: boolean
  audioTrackSolo: boolean
  audioTrackGainDb: number
}

interface TimelineViewport {
  startFrame: number
  endFrame: number
}

const TRACK_LABEL_WIDTH = 112
const DEFAULT_CLIP_SECONDS = 4
const DEFAULT_TIMELINE_SECONDS = 12
const DEFAULT_PX_PER_SECOND = 84
const MIN_CLIP_SECONDS = 0.25
const SNAP_PIXELS = 10
const PLAYBACK_UI_FRAME_MS = 1000 / 20
const TIMELINE_FRAME_WIDTH = 96
const FRAME_TILE_COLUMNS = 8
const FRAME_TILE_ROWS = 4
const FRAMES_PER_TILE = FRAME_TILE_COLUMNS * FRAME_TILE_ROWS
const FRAME_DETAIL_WIDTH = 72
const DEFAULT_FRAME_RATE = 24

const mediaDurationCache = new Map<string, number>()
const mediaDurationRequests = new Map<string, Promise<number>>()

type EditorIconName =
  | "audio"
  | "blade"
  | "close"
  | "film"
  | "frame"
  | "image"
  | "minus"
  | "pause"
  | "play"
  | "plus"
  | "pointer"
  | "redo"
  | "step-back"
  | "step-forward"
  | "undo"

function EditorIcon({ name, className }: { name: EditorIconName; className?: string }) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={cn("h-3.5 w-3.5", className)}
    >
      {name === "pointer" && <path d="M4 2.8 15.6 9l-5.1 1.45-2.2 5.2L4 2.8Z" />}
      {name === "blade" && <><circle cx="6" cy="6" r="2.4" /><circle cx="6" cy="14" r="2.4" /><path d="m8 7.2 8 4.8M8 12.8 16 8" /></>}
      {name === "play" && <path fill="currentColor" stroke="none" d="m7 4 8 6-8 6V4Z" />}
      {name === "pause" && <><path strokeWidth="2.4" d="M7 4.5v11M13 4.5v11" /></>}
      {name === "step-back" && <><path d="M6 4.5v11" /><path fill="currentColor" stroke="none" d="m14.5 4.5-7 5.5 7 5.5v-11Z" /></>}
      {name === "step-forward" && <><path d="M14 4.5v11" /><path fill="currentColor" stroke="none" d="m5.5 4.5 7 5.5-7 5.5v-11Z" /></>}
      {name === "film" && <><rect x="2.5" y="4" width="15" height="12" rx="1" /><path d="M6 4v12M14 4v12M2.5 8h3.5M14 8h3.5M2.5 12h3.5M14 12h3.5" /></>}
      {name === "audio" && <><path d="M3 10h2M7 6v8M10 3.5v13M13 6v8M16 8v4" /></>}
      {name === "image" && <><rect x="2.5" y="3.5" width="15" height="13" rx="1" /><circle cx="7" cy="8" r="1.3" /><path d="m4.5 14 4-4 2.5 2 2-2 2.5 4" /></>}
      {name === "frame" && <><path d="M3 7V3h4M13 3h4v4M17 13v4h-4M7 17H3v-4" /><circle cx="10" cy="10" r="2" /></>}
      {name === "minus" && <path d="M4 10h12" />}
      {name === "plus" && <path d="M4 10h12M10 4v12" />}
      {name === "close" && <path d="m5 5 10 10M15 5 5 15" />}
      {name === "undo" && <><path d="M6.5 6H3v-3.5" /><path d="M3.2 5.7A7 7 0 1 1 4.6 14" /></>}
      {name === "redo" && <><path d="M13.5 6H17v-3.5" /><path d="M16.8 5.7A7 7 0 1 0 15.4 14" /></>}
    </svg>
  )
}

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

function clipEndFrame(clip: TimelineClipState): number {
  return clip.startFrame + clip.durationFrames
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
  startFrame: number,
  durationFrames: number,
  sourceInFrame = 0,
  syncGroupId?: string,
  fullSource = false,
): TimelineClipState {
  return {
    clipId: createClipId(mediaId),
    mediaId,
    startFrame: Math.max(0, Math.round(startFrame)),
    durationFrames: Math.max(1, Math.round(durationFrames)),
    sourceInFrame: Math.max(0, Math.round(sourceInFrame)),
    syncGroupId,
    fullSource,
    gainDb: 0,
    muted: false,
    fadeInFrames: 0,
    fadeOutFrames: 0,
  }
}

function splitClipsAt(
  clips: TimelineClipState[],
  splitFrame: number,
  rightGroupIds: Map<string, string>,
  targetClipIds?: Set<string>,
) {
  let selectedRightClipId: string | null = null
  const nextClips = clips.flatMap((clip) => {
    if (targetClipIds && !targetClipIds.has(clip.clipId)) return [clip]
    if (splitFrame <= clip.startFrame || splitFrame >= clipEndFrame(clip)) {
      return [clip]
    }
    const leftDurationFrames = splitFrame - clip.startFrame
    const rightDurationFrames = clipEndFrame(clip) - splitFrame
    const groupKey = clip.syncGroupId || clip.clipId
    const rightSyncGroupId = rightGroupIds.get(groupKey) || createSyncGroupId(groupKey)
    rightGroupIds.set(groupKey, rightSyncGroupId)
    const rightClip: TimelineClipState = {
      ...clip,
      clipId: createClipId(clip.mediaId),
      startFrame: splitFrame,
      durationFrames: rightDurationFrames,
      sourceInFrame: clip.sourceInFrame + leftDurationFrames,
      syncGroupId: rightSyncGroupId,
      fullSource: false,
      fadeInFrames: 0,
      fadeOutFrames: Math.min(clip.fadeOutFrames || 0, rightDurationFrames),
    }
    selectedRightClipId = selectedRightClipId || rightClip.clipId
    return [
      {
        ...clip,
        durationFrames: leftDurationFrames,
        fullSource: false,
        fadeInFrames: Math.min(clip.fadeInFrames || 0, leftDurationFrames),
        fadeOutFrames: 0,
      },
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

function mediaFramesPerSecond(index: VideoEditorMediaIndex | undefined): number {
  if (!index || index.frame_rate.denominator <= 0) return DEFAULT_FRAME_RATE
  return index.frame_rate.numerator / index.frame_rate.denominator
}

function gainAmplitude(gainDb: number): number {
  return gainDb <= -120 ? 0 : Math.pow(10, gainDb / 20)
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
  clipStartFrame,
  sourceInFrame,
  clipDurationFrames,
  sequenceFps,
  mediaIndex,
  pxPerSecond,
  viewport,
}: {
  projectId: string
  nodeId: string
  clipStartFrame: number
  sourceInFrame: number
  clipDurationFrames: number
  sequenceFps: number
  mediaIndex: VideoEditorMediaIndex
  pxPerSecond: number
  viewport: TimelineViewport
}) {
  const pixelsPerFrame = pxPerSecond / sequenceFps
  const showEveryFrame = pixelsPerFrame >= 18
  const stepFrames = showEveryFrame
    ? 1
    : Math.max(1, Math.round(TIMELINE_FRAME_WIDTH / Math.max(0.01, pixelsPerFrame)))
  const localVisibleStart = Math.max(0, viewport.startFrame - clipStartFrame)
  const localVisibleEnd = Math.min(clipDurationFrames, viewport.endFrame - clipStartFrame)
  const firstLocalFrame = Math.max(0, Math.floor(localVisibleStart / stepFrames) * stepFrames)
  const renderedFrames: Array<{ localFrame: number; sourceFrame: number; spanFrames: number }> = []
  for (let localFrame = firstLocalFrame; localFrame < localVisibleEnd; localFrame += stepFrames) {
    const spanFrames = Math.min(stepFrames, clipDurationFrames - localFrame)
    const sampledLocalFrame = showEveryFrame
      ? localFrame
      : Math.min(clipDurationFrames - 1, localFrame + Math.floor(spanFrames / 2))
    const sourceFrame = Math.min(mediaIndex.frame_count - 1, sourceInFrame + sampledLocalFrame)
    if (sourceFrame >= 0) renderedFrames.push({ localFrame, sourceFrame, spanFrames })
  }

  return (
    <div
      className="absolute inset-0 overflow-hidden bg-[#1c3548]"
      data-openreel-frame-strip="true"
      data-every-frame={showEveryFrame ? "true" : "false"}
      data-virtualized="true"
      data-source-frame-count={mediaIndex.frame_count}
      data-total-clip-frames={clipDurationFrames}
      data-rendered-frame-count={renderedFrames.length}
    >
      {renderedFrames.map(({ localFrame, sourceFrame, spanFrames }) => {
        const tileIndex = Math.floor(sourceFrame / FRAMES_PER_TILE)
        const tileCell = sourceFrame % FRAMES_PER_TILE
        const column = tileCell % FRAME_TILE_COLUMNS
        const row = Math.floor(tileCell / FRAME_TILE_COLUMNS)
        return (
          <span
            key={`${localFrame}-${sourceFrame}`}
            data-openreel-timeline-frame="true"
            data-frame-index={sourceFrame}
            data-timeline-frame={clipStartFrame + localFrame}
            className="absolute bottom-0 top-0 border-r border-black/25 bg-no-repeat"
            style={{
              left: localFrame * pixelsPerFrame,
              width: Math.max(1, spanFrames * pixelsPerFrame),
              backgroundImage: `url(${getVideoEditorFrameTileUrl(projectId, nodeId, tileIndex)})`,
              backgroundPosition: `${(column / (FRAME_TILE_COLUMNS - 1)) * 100}% ${(row / (FRAME_TILE_ROWS - 1)) * 100}%`,
              backgroundSize: `${FRAME_TILE_COLUMNS * 100}% ${FRAME_TILE_ROWS * 100}%`,
            }}
          />
        )
      })}
    </div>
  )
}

function RealAudioWaveform({
  projectId,
  nodeId,
  sourceOffset,
  clipDuration,
  width,
  gainDb,
  muted,
  fadeInSeconds,
  fadeOutSeconds,
}: {
  projectId: string
  nodeId: string
  sourceOffset: number
  clipDuration: number
  width: number
  gainDb: number
  muted: boolean
  fadeInSeconds: number
  fadeOutSeconds: number
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [manifest, setManifest] = useState<VideoEditorWaveformManifest | null>(null)
  const [page, setPage] = useState<VideoEditorWaveformPage | null>(null)

  useEffect(() => {
    let cancelled = false
    setManifest(null)
    setPage(null)
    void getVideoEditorWaveformManifest(projectId, nodeId).then((result) => {
      if (!cancelled) setManifest(result)
    }).catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [nodeId, projectId])

  useEffect(() => {
    if (!manifest) return
    let cancelled = false
    const sourceSamples = Math.max(1, Math.ceil(clipDuration * manifest.sample_rate))
    const targetBuckets = Math.max(1, Math.ceil(width / 2))
    const levelInfo = manifest.levels.find((level) => sourceSamples / level.samples_per_bucket <= targetBuckets)
      || manifest.levels[manifest.levels.length - 1]
    const startBucket = Math.floor(sourceOffset * manifest.sample_rate / levelInfo.samples_per_bucket)
    const bucketCount = Math.max(1, Math.ceil(sourceSamples / levelInfo.samples_per_bucket) + 1)
    const timer = window.setTimeout(() => {
      void getVideoEditorWaveformPage(projectId, nodeId, {
        level: levelInfo.level,
        startBucket,
        limit: Math.min(10_000, bucketCount),
      }).then((result) => {
        if (!cancelled) setPage(result)
      }).catch(() => undefined)
    }, 100)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [clipDuration, manifest, nodeId, projectId, sourceOffset, width])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !page) return
    const cssWidth = Math.max(1, Math.round(width))
    const cssHeight = 40
    const ratio = Math.min(2, window.devicePixelRatio || 1)
    canvas.width = Math.round(cssWidth * ratio)
    canvas.height = Math.round(cssHeight * ratio)
    const context = canvas.getContext("2d")
    if (!context) return
    context.setTransform(ratio, 0, 0, ratio, 0, 0)
    context.clearRect(0, 0, cssWidth, cssHeight)
    const channels = Math.max(1, page.channels)
    const channelHeight = cssHeight / channels
    const bucketCount = Math.max(1, page.maximum.length)
    const amplitude = muted ? 0 : gainAmplitude(gainDb)
    context.fillStyle = muted ? "rgba(120,132,126,.45)" : "rgba(168,223,196,.92)"
    context.strokeStyle = muted ? "rgba(120,132,126,.35)" : "rgba(151,207,179,.55)"
    context.lineWidth = 1
    for (let channel = 0; channel < channels; channel += 1) {
      const top = channel * channelHeight
      const center = top + channelHeight / 2
      context.beginPath()
      context.moveTo(0, Math.round(center) + 0.5)
      context.lineTo(cssWidth, Math.round(center) + 0.5)
      context.stroke()
      for (let index = 0; index < bucketCount; index += 1) {
        const x0 = Math.floor(index / bucketCount * cssWidth)
        const x1 = Math.max(x0 + 1, Math.ceil((index + 1) / bucketCount * cssWidth))
        const localSeconds = (index + 0.5) / bucketCount * clipDuration
        const fadeIn = fadeInSeconds > 0 ? Math.min(1, localSeconds / fadeInSeconds) : 1
        const fadeOut = fadeOutSeconds > 0
          ? Math.min(1, Math.max(0, clipDuration - localSeconds) / fadeOutSeconds)
          : 1
        const envelope = Math.min(fadeIn, fadeOut)
        const positive = Math.min(1, Math.abs(page.maximum[index]?.[channel] || 0) * amplitude * envelope)
        const negative = Math.min(1, Math.abs(page.minimum[index]?.[channel] || 0) * amplitude * envelope)
        const y0 = center - positive * (channelHeight / 2 - 1)
        const y1 = center + negative * (channelHeight / 2 - 1)
        context.fillRect(x0, y0, Math.max(1, x1 - x0), Math.max(1, y1 - y0))
      }
    }
  }, [clipDuration, fadeInSeconds, fadeOutSeconds, gainDb, muted, page, width])

  return (
    <canvas
      ref={canvasRef}
      data-openreel-real-waveform="true"
      data-waveform-level={page?.level ?? ""}
      data-waveform-buckets={page?.maximum.length || 0}
      data-waveform-gain-db={gainDb.toFixed(2)}
      data-waveform-muted={muted ? "true" : "false"}
      data-waveform-fade-in={fadeInSeconds.toFixed(6)}
      data-waveform-fade-out={fadeOutSeconds.toFixed(6)}
      className="absolute inset-x-0 bottom-1 h-10 w-full"
      style={{ width: Math.max(1, width) }}
    />
  )
}

function ToolButton({
  label,
  icon,
  active,
  disabled,
  onClick,
}: {
  label: string
  icon: EditorIconName
  active?: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-[3px] border text-[11px] transition disabled:cursor-not-allowed disabled:opacity-35",
        active
          ? "border-[#579bd3] bg-[#315f83] text-white shadow-[inset_0_1px_rgba(255,255,255,.08)]"
          : "border-[#353a41] bg-[#24272c] text-[#b8bdc5] hover:border-[#4a5059] hover:bg-[#2d3137] hover:text-white",
      )}
    >
      <EditorIcon name={icon} />
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
        "h-7 rounded-[3px] border px-2.5 text-[10px] font-medium transition",
        active
          ? "border-[#579bd3] bg-[#315f83] text-white"
          : "border-[#3a3f47] bg-[#272a30] text-[#d2d5da] hover:border-[#515862] hover:bg-[#30343a]",
        disabled && "cursor-not-allowed opacity-35 hover:border-[#3a3f47] hover:bg-[#272a30]",
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
      className="group min-w-0 cursor-grab border border-transparent bg-[#1b1e22] p-1 transition hover:border-[#4b515a] hover:bg-[#22262b] active:cursor-grabbing"
    >
      <div className="relative aspect-video w-full overflow-hidden bg-[#090a0c]">
        {item.type === "video" ? (
          <video src={item.src} muted preload="metadata" className="h-full w-full object-cover" />
        ) : item.type === "image" ? (
          <img src={item.src} alt="" className="h-full w-full object-cover" draggable={false} />
        ) : (
          <div className="flex h-full items-center justify-center bg-[#17332b] text-[#8bd2b3]">
            <EditorIcon name="audio" className="h-5 w-5" />
          </div>
        )}
        <div className="absolute bottom-1 left-1 flex h-4 w-4 items-center justify-center bg-black/70 text-[#d7dadd]">
          <EditorIcon name={item.type === "video" ? "film" : item.type === "audio" ? "audio" : "image"} className="h-2.5 w-2.5" />
        </div>
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            onInsert(item)
          }}
          className="absolute bottom-1 right-1 flex h-5 w-5 items-center justify-center border border-white/20 bg-black/75 text-white opacity-0 transition hover:bg-[#315f83] group-hover:opacity-100"
          title="插入轨道"
          aria-label="插入轨道"
        >
          <EditorIcon name="plus" className="h-3 w-3" />
        </button>
      </div>
      <div className="min-w-0 px-0.5 pb-0.5 pt-1">
        <div className="truncate text-[10px] font-medium text-[#d7dadd]">{item.title || "未命名素材"}</div>
        <div className="mt-0.5 text-[9px] text-[#777d86]">{mediaTypeLabel(item.type)}</div>
      </div>
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
  sequenceFps,
  viewport,
  sourceDuration,
  mediaIndex,
  waveformNodeId,
  trackGainDb,
  trackMuted,
  selected,
  onBeginEdit,
  onSelect,
  onDragStartFrame,
  onResizeEdge,
  onCutAtFrame,
}: {
  projectId: string
  clip: TimelineClipState
  item: VideoEditPanelMediaNode
  kind: "video" | "audio"
  activeTool: TimelineTool
  pxPerSecond: number
  sequenceFps: number
  viewport: TimelineViewport
  sourceDuration: number | null
  mediaIndex?: VideoEditorMediaIndex | null
  waveformNodeId?: string
  trackGainDb?: number
  trackMuted?: boolean
  selected?: boolean
  onBeginEdit: () => void
  onSelect: (clipId: string) => void
  onDragStartFrame: (kind: "video" | "audio", clipId: string, startFrame: number) => void
  onResizeEdge: (kind: "video" | "audio", clipId: string, edge: "start" | "end", edgeFrame: number) => void
  onCutAtFrame: (frame: number, clipId: string) => void
}) {
  const [dragging, setDragging] = useState(false)
  const left = clip.startFrame / sequenceFps * pxPerSecond
  const width = Math.max(18, clip.durationFrames / sequenceFps * pxPerSecond)
  const clipDurationSeconds = clip.durationFrames / sequenceFps
  const sourceInSeconds = clip.sourceInFrame / sequenceFps

  const beginMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement | null
    if (target?.dataset.edgeHandle) return
    event.preventDefault()
    event.stopPropagation()
    onSelect(clip.clipId)
    if (activeTool === "blade") {
      onBeginEdit()
      const rect = event.currentTarget.getBoundingClientRect()
      const localFrame = Math.round(clamp((event.clientX - rect.left) / pxPerSecond * sequenceFps, 0, clip.durationFrames))
      onCutAtFrame(clip.startFrame + localFrame, clip.clipId)
      return
    }
    const startX = event.clientX
    const initialStartFrame = clip.startFrame
    let historyRecorded = false
    setDragging(true)

    const onMove = (moveEvent: PointerEvent) => {
      if (!historyRecorded) {
        onBeginEdit()
        historyRecorded = true
      }
      const deltaFrames = Math.round((moveEvent.clientX - startX) / pxPerSecond * sequenceFps)
      onDragStartFrame(kind, clip.clipId, Math.max(0, initialStartFrame + deltaFrames))
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
    const initialEdgeFrame = edge === "start" ? clip.startFrame : clipEndFrame(clip)
    let historyRecorded = false
    const onMove = (moveEvent: PointerEvent) => {
      if (!historyRecorded) {
        onBeginEdit()
        historyRecorded = true
      }
      const deltaFrames = Math.round((moveEvent.clientX - startX) / pxPerSecond * sequenceFps)
      onResizeEdge(kind, clip.clipId, edge, initialEdgeFrame + deltaFrames)
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
      data-start={(clip.startFrame / sequenceFps).toFixed(6)}
      data-duration={clipDurationSeconds.toFixed(6)}
      data-source-offset={sourceInSeconds.toFixed(6)}
      data-source-duration={sourceDuration?.toFixed(6) || ""}
      data-start-frame={clip.startFrame}
      data-duration-frames={clip.durationFrames}
      data-source-in-frame={clip.sourceInFrame}
      data-gain-db={(clip.gainDb || 0).toFixed(2)}
      data-muted={clip.muted ? "true" : "false"}
      data-fade-in-frames={clip.fadeInFrames || 0}
      data-fade-out-frames={clip.fadeOutFrames || 0}
      onPointerDown={beginMove}
      className={cn(
        "group/clip absolute top-1.5 h-[64px] overflow-hidden rounded-[2px] border shadow-[0_1px_2px_rgba(0,0,0,.55)]",
        kind === "video"
          ? "border-[#315f80] bg-[#254d69]"
          : "border-[#32664f] bg-[#24523e]",
        selected && "border-[#a9cdec] ring-1 ring-[#6ca9d8]",
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
          ) : mediaIndex ? (
            <VideoThumbnailStrip
              projectId={projectId}
              nodeId={item.id}
              clipStartFrame={clip.startFrame}
              sourceInFrame={clip.sourceInFrame}
              clipDurationFrames={clip.durationFrames}
              sequenceFps={sequenceFps}
              mediaIndex={mediaIndex}
              pxPerSecond={pxPerSecond}
              viewport={viewport}
            />
          ) : (
            <div className="absolute inset-0 animate-pulse bg-[linear-gradient(110deg,#203746,#31566f,#203746)]" />
          )}
          <div className="absolute inset-0 bg-gradient-to-b from-black/20 via-transparent to-black/25" />
        </>
      ) : (
        waveformNodeId ? (
          <RealAudioWaveform
            projectId={projectId}
            nodeId={waveformNodeId}
            sourceOffset={sourceInSeconds}
            clipDuration={clipDurationSeconds}
            width={width}
            gainDb={(clip.gainDb || 0) + (trackGainDb || 0)}
            muted={Boolean(clip.muted || trackMuted)}
            fadeInSeconds={(clip.fadeInFrames || 0) / sequenceFps}
            fadeOutSeconds={(clip.fadeOutFrames || 0) / sequenceFps}
          />
        ) : null
      )}
      <div className={cn(
        "absolute inset-x-0 top-0 flex h-[18px] items-center gap-1 border-b px-1.5",
        kind === "video" ? "border-[#4b7897]/60 bg-[#244861]/92" : "border-[#4a765f]/60 bg-[#214735]/94",
      )}>
        <EditorIcon name={kind === "video" ? (item.type === "image" ? "image" : "film") : "audio"} className="h-2.5 w-2.5 shrink-0 text-white/75" />
        <span className="truncate text-[9px] font-medium text-white/90">{item.title || "素材"}</span>
      </div>
      <div className="absolute bottom-0.5 right-1 bg-black/60 px-1 py-px font-mono text-[8px] tabular-nums text-white/75">
        {formatTimePrecise(sourceInSeconds)}–{formatTimePrecise(sourceInSeconds + clipDurationSeconds)}
      </div>
      <button
        type="button"
        data-edge-handle="start"
        onPointerDown={(event) => beginResize("start", event)}
        className={cn(
          "absolute bottom-0 left-0 top-0 z-10 w-2 cursor-ew-resize border-l-2 opacity-0 transition group-hover/clip:opacity-100",
          kind === "video"
            ? "border-[#d5ebff] bg-[#8bc8f5]/10 hover:bg-[#8bc8f5]/25"
            : "border-[#d4f4e4] bg-[#8fd8b6]/10 hover:bg-[#8fd8b6]/25",
          selected && "opacity-100",
        )}
        title="收放起点"
        aria-label="收放起点"
      />
      <button
        type="button"
        data-edge-handle="end"
        onPointerDown={(event) => beginResize("end", event)}
        className={cn(
          "absolute bottom-0 right-0 top-0 z-10 w-2 cursor-ew-resize border-r-2 opacity-0 transition group-hover/clip:opacity-100",
          kind === "video"
            ? "border-[#d5ebff] bg-[#8bc8f5]/10 hover:bg-[#8bc8f5]/25"
            : "border-[#d4f4e4] bg-[#8fd8b6]/10 hover:bg-[#8fd8b6]/25",
          selected && "opacity-100",
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
  const sequenceRevisionRef = useRef(0)
  const lastSavedSequenceRef = useRef("")
  const sequenceSaveChainRef = useRef<Promise<void>>(Promise.resolve())
  const undoStackRef = useRef<EditorSnapshot[]>([])
  const redoStackRef = useRef<EditorSnapshot[]>([])
  const videoClipsRef = useRef<TimelineClipState[]>([])
  const audioClipsRef = useRef<TimelineClipState[]>([])
  const audioTrackMutedRef = useRef(false)
  const audioTrackSoloRef = useRef(false)
  const audioTrackGainDbRef = useRef(0)
  const [sourceDurations, setSourceDurations] = useState<Record<string, number>>({})
  const [mediaIndexes, setMediaIndexes] = useState<Record<string, VideoEditorMediaIndex>>({})
  const [sequenceLoaded, setSequenceLoaded] = useState(false)
  const [sequenceRevision, setSequenceRevision] = useState(0)
  const [sequenceFrameRate, setSequenceFrameRate] = useState({ numerator: DEFAULT_FRAME_RATE, denominator: 1 })
  const [currentTime, setCurrentTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [tool, setTool] = useState<TimelineTool>("select")
  const [pxPerSecond, setPxPerSecond] = useState(DEFAULT_PX_PER_SECOND)
  const [previewScale, setPreviewScale] = useState<PreviewScale>("fit")
  const [audioTrackMuted, setAudioTrackMuted] = useState(false)
  const [audioTrackSolo, setAudioTrackSolo] = useState(false)
  const [audioTrackGainDb, setAudioTrackGainDb] = useState(0)
  const [timelineViewport, setTimelineViewport] = useState<TimelineViewport>({
    startFrame: 0,
    endFrame: DEFAULT_TIMELINE_SECONDS * DEFAULT_FRAME_RATE,
  })
  const [busy, setBusy] = useState<BusyAction>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null)
  const [videoClips, setVideoClips] = useState<TimelineClipState[]>([])
  const [audioClips, setAudioClips] = useState<TimelineClipState[]>([])
  const [historyDepth, setHistoryDepth] = useState({ undo: 0, redo: 0 })
  currentTimeRef.current = currentTime
  videoClipsRef.current = videoClips
  audioClipsRef.current = audioClips
  audioTrackMutedRef.current = audioTrackMuted
  audioTrackSoloRef.current = audioTrackSolo
  audioTrackGainDbRef.current = audioTrackGainDb
  const framesPerSecond = sequenceFrameRate.numerator / sequenceFrameRate.denominator

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
    const index = mediaIndexes[item.type === "video" ? item.id : mediaSourceKey(item)]
    if (index) return index.duration_seconds
    return validDuration(sourceDurations[item.src]) || validDuration(item.durationSeconds)
  }, [mediaIndexes, sourceDurations])
  const sourceDurationForClip = useCallback((clip: TimelineClipState): number | null => (
    sourceDurationForItem(mediaById.get(clip.mediaId))
  ), [mediaById, sourceDurationForItem])
  const sourceFrameCountForItem = useCallback((item: VideoEditPanelMediaNode | undefined): number | null => {
    if (!item || item.type === "image") return null
    const index = mediaIndexes[item.type === "video" ? item.id : mediaSourceKey(item)]
    if (index) return index.frame_count
    const duration = sourceDurationForItem(item)
    return duration ? Math.max(1, Math.round(duration * framesPerSecond)) : null
  }, [framesPerSecond, mediaIndexes, sourceDurationForItem])
  const sourceFrameCountForClip = useCallback((clip: TimelineClipState): number | null => (
    sourceFrameCountForItem(mediaById.get(clip.mediaId))
  ), [mediaById, sourceFrameCountForItem])
  const selectedTimelineClip = useMemo(() => (
    [...videoClips, ...audioClips].find((clip) => clip.clipId === selectedClipId)
  ), [audioClips, selectedClipId, videoClips])
  const captureEditorSnapshot = useCallback((): EditorSnapshot => ({
    videoClips: videoClipsRef.current.map((clip) => ({ ...clip })),
    audioClips: audioClipsRef.current.map((clip) => ({ ...clip })),
    audioTrackMuted: audioTrackMutedRef.current,
    audioTrackSolo: audioTrackSoloRef.current,
    audioTrackGainDb: audioTrackGainDbRef.current,
  }), [])
  const updateHistoryDepth = useCallback(() => {
    setHistoryDepth({
      undo: undoStackRef.current.length,
      redo: redoStackRef.current.length,
    })
  }, [])
  const applyEditorSnapshot = useCallback((snapshot: EditorSnapshot) => {
    const nextVideoClips = snapshot.videoClips.map((clip) => ({ ...clip }))
    const nextAudioClips = snapshot.audioClips.map((clip) => ({ ...clip }))
    videoClipsRef.current = nextVideoClips
    audioClipsRef.current = nextAudioClips
    audioTrackMutedRef.current = snapshot.audioTrackMuted
    audioTrackSoloRef.current = snapshot.audioTrackSolo
    audioTrackGainDbRef.current = snapshot.audioTrackGainDb
    setVideoClips(nextVideoClips)
    setAudioClips(nextAudioClips)
    setAudioTrackMuted(snapshot.audioTrackMuted)
    setAudioTrackSolo(snapshot.audioTrackSolo)
    setAudioTrackGainDb(snapshot.audioTrackGainDb)
    setSelectedClipId(null)
    setPlaying(false)
  }, [])
  const recordUndoSnapshot = useCallback(() => {
    const snapshot = captureEditorSnapshot()
    const serialized = JSON.stringify(snapshot)
    const previous = undoStackRef.current.at(-1)
    if (!previous || JSON.stringify(previous) !== serialized) {
      undoStackRef.current.push(snapshot)
      if (undoStackRef.current.length > 100) undoStackRef.current.shift()
    }
    redoStackRef.current = []
    updateHistoryDepth()
  }, [captureEditorSnapshot, updateHistoryDepth])
  const undoEditor = useCallback(() => {
    const current = captureEditorSnapshot()
    const currentKey = JSON.stringify(current)
    let previous = undoStackRef.current.pop()
    while (previous && JSON.stringify(previous) === currentKey) {
      previous = undoStackRef.current.pop()
    }
    if (!previous) {
      updateHistoryDepth()
      return
    }
    redoStackRef.current.push(current)
    applyEditorSnapshot(previous)
    updateHistoryDepth()
  }, [applyEditorSnapshot, captureEditorSnapshot, updateHistoryDepth])
  const redoEditor = useCallback(() => {
    const current = captureEditorSnapshot()
    const currentKey = JSON.stringify(current)
    let next = redoStackRef.current.pop()
    while (next && JSON.stringify(next) === currentKey) {
      next = redoStackRef.current.pop()
    }
    if (!next) {
      updateHistoryDepth()
      return
    }
    undoStackRef.current.push(current)
    applyEditorSnapshot(next)
    updateHistoryDepth()
  }, [applyEditorSnapshot, captureEditorSnapshot, updateHistoryDepth])
  const selectedSyncGroupId = selectedTimelineClip?.syncGroupId
  const selectedVideoClip = useMemo(() => (
    videoClips.find((clip) => clip.clipId === selectedClipId) ||
    (selectedSyncGroupId ? videoClips.find((clip) => clip.syncGroupId === selectedSyncGroupId) : undefined) ||
    videoClips[0]
  ), [selectedClipId, selectedSyncGroupId, videoClips])
  const selectedAudioClip = useMemo(() => (
    audioClips.find((clip) => clip.clipId === selectedClipId) ||
    (selectedSyncGroupId ? audioClips.find((clip) => clip.syncGroupId === selectedSyncGroupId) : undefined) ||
    audioClips[0]
  ), [audioClips, selectedClipId, selectedSyncGroupId])
  const currentFrame = Math.round(currentTime * framesPerSecond)
  const currentVideoClip = useMemo(
    () => videoClips.find((clip) => currentFrame >= clip.startFrame && currentFrame < clipEndFrame(clip)),
    [currentFrame, videoClips],
  )
  const currentAudioClip = useMemo(
    () => audioClips.find((clip) => currentFrame >= clip.startFrame && currentFrame < clipEndFrame(clip)),
    [audioClips, currentFrame],
  )
  const currentVideoItem = currentVideoClip ? mediaById.get(currentVideoClip.mediaId) : undefined
  const currentAudioItem = currentAudioClip ? mediaById.get(currentAudioClip.mediaId) : undefined
  const selectedVideoItem = selectedVideoClip ? mediaById.get(selectedVideoClip.mediaId) : undefined
  const primaryMediaIndex = sourceVideoItem ? mediaIndexes[sourceVideoItem.id] : undefined
  const maxPxPerSecond = Math.max(220, framesPerSecond * FRAME_DETAIL_WIDTH)
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
    Math.max(0, ...videoClips.map(clipEndFrame), ...audioClips.map(clipEndFrame)) / framesPerSecond
  ), [audioClips, framesPerSecond, videoClips])
  const timelineDuration = useMemo(() => {
    const lastClipEndFrame = Math.max(
      0,
      ...videoClips.map(clipEndFrame),
      ...audioClips.map(clipEndFrame),
    )
    return Math.max(DEFAULT_TIMELINE_SECONDS, Math.ceil(lastClipEndFrame / framesPerSecond + 2))
  }, [audioClips, framesPerSecond, videoClips])
  const timelineWidth = timelineDuration * pxPerSecond
  const ticks = useMemo(() => {
    const step = pxPerSecond >= 120 ? 1 : pxPerSecond >= 72 ? 2 : 5
    const count = Math.floor(timelineDuration / step) + 1
    return Array.from({ length: count }, (_, index) => index * step)
  }, [pxPerSecond, timelineDuration])
  const sequenceSpec = useMemo<VideoEditorSequenceSpec>(() => {
    const frameRate = sequenceFrameRate
    const clipSpec = (clip: TimelineClipState, trackId: "v1" | "a1") => ({
      id: clip.clipId,
      track_id: trackId,
      media_id: clip.mediaId,
      timeline_start_frame: clip.startFrame,
      duration_frames: clip.durationFrames,
      source_in_frame: clip.sourceInFrame,
      source_frame_count: sourceFrameCountForClip(clip),
      linked_group_id: clip.syncGroupId || null,
      gain_db: clip.gainDb || 0,
      muted: Boolean(clip.muted),
      fade_in_frames: clip.fadeInFrames || 0,
      fade_out_frames: clip.fadeOutFrames || 0,
    })
    return {
      schema_version: "openreel.video_sequence.v1",
      settings: {
        frame_rate: frameRate,
        width: primaryMediaIndex?.width || 1280,
        height: primaryMediaIndex?.height || 720,
        audio_sample_rate: primaryMediaIndex?.audio.sample_rate || 48_000,
        audio_channels: primaryMediaIndex?.audio.channels || 2,
      },
      tracks: [
        {
          id: "v1",
          kind: "video",
          name: "视频 1",
          order: 0,
          locked: false,
          sync_locked: true,
          visible: true,
          muted: false,
          solo: false,
          gain_db: 0,
        },
        {
          id: "a1",
          kind: "audio",
          name: "音频 1",
          order: 0,
          locked: false,
          sync_locked: true,
          visible: true,
          muted: audioTrackMuted,
          solo: audioTrackSolo,
          gain_db: audioTrackGainDb,
        },
      ],
      clips: [
        ...videoClips.map((clip) => clipSpec(clip, "v1")),
        ...audioClips.map((clip) => clipSpec(clip, "a1")),
      ],
    }
  }, [audioClips, audioTrackGainDb, audioTrackMuted, audioTrackSolo, primaryMediaIndex, sequenceFrameRate, sourceFrameCountForClip, videoClips])

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
    let cancelled = false
    initializedNodeRef.current = null
    sequenceRevisionRef.current = 0
    lastSavedSequenceRef.current = ""
    undoStackRef.current = []
    redoStackRef.current = []
    setHistoryDepth({ undo: 0, redo: 0 })
    setSequenceRevision(0)
    setSequenceLoaded(false)
    setVideoClips([])
    setAudioClips([])
    setAudioTrackMuted(false)
    setAudioTrackSolo(false)
    setAudioTrackGainDb(0)
    void getVideoEditorSequence(projectId, nodeId).then((document) => {
      if (cancelled) return
      if (!document) {
        setSequenceLoaded(true)
        return
      }
      const trackKinds = new Map(document.spec.tracks.map((track) => [track.id, track.kind]))
      const restored = document.spec.clips.map((clip): TimelineClipState => ({
        clipId: clip.id,
        mediaId: clip.media_id,
        startFrame: clip.timeline_start_frame,
        durationFrames: clip.duration_frames,
        sourceInFrame: clip.source_in_frame,
        syncGroupId: clip.linked_group_id || undefined,
        fullSource: Boolean(
          clip.source_frame_count &&
          clip.source_in_frame === 0 &&
          clip.duration_frames === clip.source_frame_count
        ),
        gainDb: clip.gain_db,
        muted: clip.muted,
        fadeInFrames: clip.fade_in_frames,
        fadeOutFrames: clip.fade_out_frames,
      }))
      setSequenceFrameRate(document.spec.settings.frame_rate)
      setVideoClips(restored.filter((clip) => trackKinds.get(
        document.spec.clips.find((item) => item.id === clip.clipId)?.track_id || "",
      ) === "video"))
      setAudioClips(restored.filter((clip) => trackKinds.get(
        document.spec.clips.find((item) => item.id === clip.clipId)?.track_id || "",
      ) === "audio"))
      const audioTrack = document.spec.tracks.find((track) => track.id === "a1")
      setAudioTrackMuted(Boolean(audioTrack?.muted))
      setAudioTrackSolo(Boolean(audioTrack?.solo))
      setAudioTrackGainDb(audioTrack?.gain_db || 0)
      sequenceRevisionRef.current = document.revision
      lastSavedSequenceRef.current = JSON.stringify(document.spec)
      setSequenceRevision(document.revision)
      initializedNodeRef.current = nodeId
      setSequenceLoaded(true)
    }).catch((reason) => {
      if (cancelled) return
      setError(reason instanceof Error ? reason.message : "无法读取剪辑序列")
    })
    return () => {
      cancelled = true
    }
  }, [nodeId, projectId])

  useEffect(() => {
    const requiredIds = new Set<string>([nodeId])
    for (const clip of videoClips) {
      const item = mediaById.get(clip.mediaId)
      if (item?.type === "video") requiredIds.add(item.id)
    }
    const missingIds = [...requiredIds].filter((id) => (
      videoItems.some((item) => item.id === id) && !mediaIndexes[id]
    ))
    if (missingIds.length === 0) return
    let cancelled = false
    for (const id of missingIds) {
      void getVideoEditorMediaIndex(projectId, id).then((index) => {
        if (cancelled) return
        setMediaIndexes((current) => current[id] ? current : { ...current, [id]: index })
        const item = mediaById.get(id)
        if (item?.src) registerSourceDuration(item.src, index.duration_seconds)
        if (id === nodeId && sequenceRevisionRef.current === 0) {
          setSequenceFrameRate(index.frame_rate)
        }
      }).catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "视频逐帧索引失败")
      })
    }
    return () => {
      cancelled = true
    }
  }, [mediaById, mediaIndexes, nodeId, projectId, registerSourceDuration, videoClips, videoItems])

  useEffect(() => {
    if (!sequenceLoaded) return
    if (initializedNodeRef.current === nodeId) return
    const primary = videoItems.find((item) => item.id === nodeId) || visualItems[0]
    if (!primary) return
    initializedNodeRef.current = nodeId
    const duration = sourceDurationForItem(primary) || DEFAULT_CLIP_SECONDS
    const durationFrames = sourceFrameCountForItem(primary) || Math.max(1, Math.round(duration * framesPerSecond))
    const syncGroupId = primary.type === "video" && mediaSourceKey(primary) === primarySourceKey
      ? primarySyncGroupId
      : undefined
    setVideoClips([createClip(primary.id, 0, durationFrames, 0, syncGroupId, primary.type === "video")])
    const primaryAudio = primary.type === "video" ? audioItemForVideo(primary) : undefined
    setAudioClips(primaryAudio
      ? [createClip(primaryAudio.id, 0, durationFrames, 0, syncGroupId, true)]
      : [])
    setSelectedClipId(null)
    setCurrentTime(0)
    setPlaying(false)
    setAudioTrackMuted(false)
    setAudioTrackSolo(false)
    setAudioTrackGainDb(0)
    setError(null)
  }, [audioItemForVideo, framesPerSecond, nodeId, primarySourceKey, primarySyncGroupId, sequenceLoaded, sourceDurationForItem, sourceFrameCountForItem, videoItems, visualItems])

  useEffect(() => {
    setVideoClips((current) => current
      .filter((clip) => visualItems.some((item) => item.id === clip.mediaId))
      .map((clip) => {
        const frameCount = sourceFrameCountForClip(clip)
        return clip.fullSource && frameCount && clip.durationFrames !== frameCount
          ? { ...clip, durationFrames: frameCount }
          : clip
      }))
    setAudioClips((current) => current
      .filter((clip) => audioTimelineItems.some((item) => item.id === clip.mediaId))
      .map((clip) => {
        const frameCount = sourceFrameCountForClip(clip)
        return clip.fullSource && frameCount && clip.durationFrames !== frameCount
          ? { ...clip, durationFrames: frameCount }
          : clip
      }))
  }, [audioTimelineItems, sourceFrameCountForClip, visualItems])

  useEffect(() => {
    if (!sequenceLoaded || initializedNodeRef.current !== nodeId) return
    const payloadKey = JSON.stringify(sequenceSpec)
    if (payloadKey === lastSavedSequenceRef.current) return
    const timer = window.setTimeout(() => {
      sequenceSaveChainRef.current = sequenceSaveChainRef.current
        .catch(() => undefined)
        .then(async () => {
          const document = await saveVideoEditorSequence(
            projectId,
            nodeId,
            sequenceRevisionRef.current,
            sequenceSpec,
          )
          sequenceRevisionRef.current = document.revision
          lastSavedSequenceRef.current = JSON.stringify(document.spec)
          setSequenceRevision(document.revision)
        })
        .catch((reason) => {
          setError(reason instanceof Error ? reason.message : "剪辑序列自动保存失败")
        })
    }, 650)
    return () => window.clearTimeout(timer)
  }, [nodeId, projectId, sequenceLoaded, sequenceSpec])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !currentVideoClip || playing) return
    const item = mediaById.get(currentVideoClip.mediaId)
    if (item?.type !== "video") {
      video.pause()
      return
    }
    const localTime = currentVideoClip.sourceInFrame / framesPerSecond + clamp(
      currentTime - currentVideoClip.startFrame / framesPerSecond,
      0,
      currentVideoClip.durationFrames / framesPerSecond,
    )
    if (Math.abs((video.currentTime || 0) - localTime) > 0.08) {
      video.currentTime = localTime
    }
    video.pause()
  }, [currentTime, currentVideoClip, framesPerSecond, mediaById, playing])

  useEffect(() => {
    if (!currentAudioClip) return
    const localFrame = clamp(currentFrame - currentAudioClip.startFrame, 0, currentAudioClip.durationFrames)
    const fadeInFrames = currentAudioClip.fadeInFrames || 0
    const fadeOutFrames = currentAudioClip.fadeOutFrames || 0
    const fadeIn = fadeInFrames > 0 ? Math.min(1, localFrame / fadeInFrames) : 1
    const remainingFrames = Math.max(0, currentAudioClip.durationFrames - localFrame)
    const fadeOut = fadeOutFrames > 0 ? Math.min(1, remainingFrames / fadeOutFrames) : 1
    const amplitude = Math.min(1, gainAmplitude((currentAudioClip.gainDb || 0) + audioTrackGainDb) * Math.min(fadeIn, fadeOut))
    if (videoRef.current && playAudioThroughVideo) videoRef.current.volume = amplitude
    if (audioRef.current) audioRef.current.volume = amplitude
  }, [audioTrackGainDb, currentAudioClip, currentFrame, playAudioThroughVideo])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !currentAudioClip || !currentAudioItem || playAudioThroughVideo) {
      audio?.pause()
      return
    }
    if (playing) return
    const localTime = currentAudioClip.sourceInFrame / framesPerSecond + clamp(
      currentTime - currentAudioClip.startFrame / framesPerSecond,
      0,
      currentAudioClip.durationFrames / framesPerSecond,
    )
    if (Math.abs((audio.currentTime || 0) - localTime) > 0.08) {
      audio.currentTime = localTime
    }
    audio.pause()
  }, [currentAudioClip, currentAudioItem, currentTime, framesPerSecond, playAudioThroughVideo, playing])

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
      const localTime = currentVideoClip.sourceInFrame / framesPerSecond + clamp(
        timelineTime - currentVideoClip.startFrame / framesPerSecond,
        0,
        currentVideoClip.durationFrames / framesPerSecond,
      )
      if (Math.abs((video.currentTime || 0) - localTime) > 0.15) video.currentTime = localTime
      mediaStarts.push(video.play())
    }
    if (audio && currentAudioClip && currentAudioItem && !playAudioThroughVideo) {
      const localTime = currentAudioClip.sourceInFrame / framesPerSecond + clamp(
        timelineTime - currentAudioClip.startFrame / framesPerSecond,
        0,
        currentAudioClip.durationFrames / framesPerSecond,
      )
      if (Math.abs((audio.currentTime || 0) - localTime) > 0.15) audio.currentTime = localTime
      mediaStarts.push(audio.play())
    }
    void Promise.all(mediaStarts).catch(() => undefined)
  }, [currentAudioClip, currentAudioItem, currentVideoClip, currentVideoItem, framesPerSecond, playAudioThroughVideo, playing])

  const timeToFrame = useCallback((time: number) => (
    Math.max(0, Math.round(time * framesPerSecond))
  ), [framesPerSecond])
  const frameToTime = useCallback((frame: number) => Math.max(0, Math.round(frame)) / framesPerSecond, [framesPerSecond])

  const seekTo = useCallback((time: number) => {
    const nextTime = clamp(frameToTime(timeToFrame(time)), 0, timelineDuration)
    currentTimeRef.current = nextTime
    setCurrentTime(nextTime)
  }, [frameToTime, timeToFrame, timelineDuration])

  const zoomTimelineAt = useCallback((nextValue: number, clientX?: number) => {
    const next = clamp(nextValue, 42, maxPxPerSecond)
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
  }, [maxPxPerSecond])

  useLayoutEffect(() => {
    const container = timelineRef.current
    const pending = pendingZoomRef.current
    if (!container || !pending) return
    container.scrollLeft = Math.max(0, pending.anchorTime * pxPerSecond + TRACK_LABEL_WIDTH - pending.localX)
    pendingZoomRef.current = null
  }, [pxPerSecond])

  useLayoutEffect(() => {
    const container = timelineRef.current
    if (!container) return
    const updateViewport = () => {
      const visibleStartSeconds = Math.max(0, (container.scrollLeft - TRACK_LABEL_WIDTH) / pxPerSecond)
      const visibleEndSeconds = Math.max(visibleStartSeconds, (
        container.scrollLeft + container.clientWidth - TRACK_LABEL_WIDTH
      ) / pxPerSecond)
      const visibleFrames = Math.max(1, Math.ceil((visibleEndSeconds - visibleStartSeconds) * framesPerSecond))
      const overscanFrames = Math.max(Math.ceil(framesPerSecond), visibleFrames)
      const next = {
        startFrame: Math.max(0, Math.floor(visibleStartSeconds * framesPerSecond) - overscanFrames),
        endFrame: Math.ceil(visibleEndSeconds * framesPerSecond) + overscanFrames,
      }
      setTimelineViewport((current) => (
        current.startFrame === next.startFrame && current.endFrame === next.endFrame ? current : next
      ))
    }
    updateViewport()
    container.addEventListener("scroll", updateViewport, { passive: true })
    const observer = new ResizeObserver(updateViewport)
    observer.observe(container)
    return () => {
      container.removeEventListener("scroll", updateViewport)
      observer.disconnect()
    }
  }, [framesPerSecond, pxPerSecond])

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

  const snapFrameToBoundaries = useCallback((valueFrame: number, excludeClipIds?: Iterable<string>): number => {
    const excluded = new Set(excludeClipIds || [])
    const thresholdFrames = Math.max(1, Math.ceil(SNAP_PIXELS / pxPerSecond * framesPerSecond))
    const targets = [...videoClips, ...audioClips]
      .filter((clip) => !excluded.has(clip.clipId))
      .flatMap((clip) => [clip.startFrame, clipEndFrame(clip)])
    const closest = targets.reduce<{ value: number; distance: number } | null>((best, target) => {
      const distance = Math.abs(valueFrame - target)
      if (distance > thresholdFrames) return best
      if (!best || distance < best.distance) return { value: target, distance }
      return best
    }, null)
    return Math.max(0, Math.round(closest ? closest.value : valueFrame))
  }, [audioClips, framesPerSecond, pxPerSecond, videoClips])

  const snapClipStartFrame = useCallback((clip: TimelineClipState, startFrame: number, excludeClipIds?: Iterable<string>): number => {
    const rawStartFrame = Math.max(0, Math.round(startFrame))
    const excluded = new Set(excludeClipIds || [clip.clipId])
    excluded.add(clip.clipId)
    const snappedStartFrame = snapFrameToBoundaries(rawStartFrame, excluded)
    const snappedEndStartFrame = snapFrameToBoundaries(rawStartFrame + clip.durationFrames, excluded) - clip.durationFrames
    const startDistance = Math.abs(snappedStartFrame - rawStartFrame)
    const endDistance = Math.abs(snappedEndStartFrame - rawStartFrame)
    return Math.max(0, Math.round(endDistance < startDistance ? snappedEndStartFrame : snappedStartFrame))
  }, [snapFrameToBoundaries])

  const insertMediaItem = useCallback((item: VideoEditPanelMediaNode, startAt?: number) => {
    recordUndoSnapshot()
    const durationFrames = item.type === "image"
      ? Math.round(DEFAULT_CLIP_SECONDS * framesPerSecond)
      : sourceFrameCountForItem(item) || Math.round(DEFAULT_CLIP_SECONDS * framesPerSecond)
    const startFrame = snapFrameToBoundaries(timeToFrame(startAt ?? currentTimeRef.current))
    if (item.type === "video") {
      const syncGroupId = createSyncGroupId(mediaSourceKey(item))
      const clip = createClip(item.id, startFrame, durationFrames, 0, syncGroupId, true)
      setVideoClips((current) => [...current, clip])
      const linkedAudio = audioItemForVideo(item)
      if (linkedAudio) {
        setAudioClips((current) => [...current, createClip(linkedAudio.id, startFrame, durationFrames, 0, syncGroupId, true)])
      }
      setSelectedClipId(clip.clipId)
      return
    }
    const clip = createClip(item.id, startFrame, durationFrames, 0, undefined, item.type === "audio")
    if (item.type === "image") {
      setVideoClips((current) => [...current, clip])
      setSelectedClipId(clip.clipId)
      return
    }
    setAudioClips((current) => [...current, clip])
  }, [audioItemForVideo, framesPerSecond, recordUndoSnapshot, snapFrameToBoundaries, sourceFrameCountForItem, timeToFrame])

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

  const updateClipStartFrame = useCallback((kind: "video" | "audio", clipId: string, startFrame: number) => {
    if (kind === "video") {
      const original = videoClips.find((clip) => clip.clipId === clipId)
      if (!original) return
      const linkedAudioBaselines = new Map(
        audioClips
          .filter((clip) => mediaById.get(original.mediaId)?.type === "video" && clipsShareTimelineRange(clip, original))
          .map((clip) => [clip.clipId, clip]),
      )
      const linkedClipIds = new Set([original.clipId, ...linkedAudioBaselines.keys()])
      const nextStartFrame = snapClipStartFrame(original, startFrame, linkedClipIds)
      setVideoClips((clips) => (
        clips.map((clip) => clip.clipId === clipId ? { ...clip, startFrame: nextStartFrame } : clip)
      ))
      if (linkedAudioBaselines.size > 0) {
        setAudioClips((clips) => (
          clips.map((clip) => {
            const baseline = linkedAudioBaselines.get(clip.clipId)
            return baseline
              ? {
                  ...clip,
                  startFrame: nextStartFrame,
                  durationFrames: original.durationFrames,
                  sourceInFrame: original.sourceInFrame,
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
    const nextStartFrame = snapClipStartFrame(original, startFrame, linkedClipIds)
    setAudioClips((clips) => (
      clips.map((clip) => clip.clipId === clipId ? { ...clip, startFrame: nextStartFrame } : clip)
    ))
    if (linkedVideoBaselines.size > 0) {
      setVideoClips((clips) => (
        clips.map((clip) => {
          const baseline = linkedVideoBaselines.get(clip.clipId)
          return baseline
            ? {
                ...clip,
                startFrame: nextStartFrame,
                durationFrames: original.durationFrames,
                sourceInFrame: original.sourceInFrame,
                fullSource: original.fullSource,
              }
            : clip
        })
      ))
    }
  }, [audioClips, mediaById, snapClipStartFrame, videoClips])

  const resizeClipEdge = useCallback((
    kind: "video" | "audio",
    clipId: string,
    edge: "start" | "end",
    edgeFrame: number,
  ) => {
    const primaryClips = kind === "video" ? videoClips : audioClips
    const linkedClips = kind === "video" ? audioClips : videoClips
    const original = primaryClips.find((clip) => clip.clipId === clipId)
    if (!original) return
    const linked = linkedClips.filter((clip) => clipsShareTimelineRange(clip, original))
    const group = [original, ...linked]
    const groupIds = new Set(group.map((clip) => clip.clipId))
    const snappedEdgeFrame = snapFrameToBoundaries(edgeFrame, groupIds)

    let nextStartFrame = original.startFrame
    let nextDurationFrames = original.durationFrames
    let nextSourceInFrame = original.sourceInFrame
    if (edge === "start") {
      const earliestStartFrame = Math.max(0, ...group.map((clip) => clip.startFrame - clip.sourceInFrame))
      const latestStartFrame = Math.min(...group.map((clip) => clipEndFrame(clip) - 1))
      nextStartFrame = Math.round(clamp(snappedEdgeFrame, earliestStartFrame, latestStartFrame))
      const deltaFrames = nextStartFrame - original.startFrame
      nextDurationFrames = Math.max(1, original.durationFrames - deltaFrames)
      nextSourceInFrame = Math.max(0, original.sourceInFrame + deltaFrames)
    } else {
      const minimumEndFrame = Math.max(...group.map((clip) => clip.startFrame + 1))
      const boundedEnds = group.map((clip) => {
        const item = mediaById.get(clip.mediaId)
        if (item?.type === "image") return Number.POSITIVE_INFINITY
        const sourceFrameCount = sourceFrameCountForClip(clip) || clip.sourceInFrame + clip.durationFrames
        return clip.startFrame + Math.max(1, sourceFrameCount - clip.sourceInFrame)
      })
      const maximumEndFrame = Math.min(...boundedEnds)
      const nextEndFrame = Math.round(clamp(snappedEdgeFrame, minimumEndFrame, maximumEndFrame))
      nextDurationFrames = Math.max(1, nextEndFrame - original.startFrame)
    }

    const applyRange = (clip: TimelineClipState): TimelineClipState => {
      const fadeInFrames = Math.min(clip.fadeInFrames || 0, nextDurationFrames)
      const fadeOutFrames = Math.min(clip.fadeOutFrames || 0, nextDurationFrames - fadeInFrames)
      return {
        ...clip,
        startFrame: nextStartFrame,
        durationFrames: nextDurationFrames,
        sourceInFrame: nextSourceInFrame,
        fadeInFrames,
        fadeOutFrames,
        fullSource: false,
      }
    }
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
  }, [audioClips, mediaById, snapFrameToBoundaries, sourceFrameCountForClip, videoClips])

  const splitTimelineAtFrame = useCallback((splitFrame: number, targetClipId?: string) => {
    const safeSplitFrame = Math.max(0, Math.round(splitFrame))
    const target = targetClipId
      ? [...videoClips, ...audioClips].find((clip) => clip.clipId === targetClipId)
      : undefined
    const targetClipIds = target
      ? new Set([...videoClips, ...audioClips]
          .filter((clip) => clip.clipId === target.clipId || clipsShareTimelineRange(clip, target))
          .map((clip) => clip.clipId))
      : undefined
    const rightGroupIds = new Map<string, string>()
    const videoResult = splitClipsAt(videoClips, safeSplitFrame, rightGroupIds, targetClipIds)
    const audioResult = splitClipsAt(audioClips, safeSplitFrame, rightGroupIds, targetClipIds)
    setVideoClips(videoResult.clips)
    setAudioClips(audioResult.clips)
    if (videoResult.selectedRightClipId || audioResult.selectedRightClipId) {
      setSelectedClipId(videoResult.selectedRightClipId || audioResult.selectedRightClipId)
    }
    seekTo(frameToTime(safeSplitFrame))
  }, [audioClips, frameToTime, seekTo, videoClips])

  const deleteSelectedClips = useCallback((ripple = false) => {
    if (!selectedTimelineClip) return
    const allClips = [...videoClips, ...audioClips]
    const deleteIds = new Set(
      allClips
        .filter((clip) => (
          clip.clipId === selectedTimelineClip.clipId ||
          Boolean(selectedTimelineClip.syncGroupId && clip.syncGroupId === selectedTimelineClip.syncGroupId)
        ))
        .map((clip) => clip.clipId),
    )
    const deleted = allClips.filter((clip) => deleteIds.has(clip.clipId))
    if (deleted.length === 0) return
    recordUndoSnapshot()
    const gapStartFrame = Math.min(...deleted.map((clip) => clip.startFrame))
    const gapEndFrame = Math.max(...deleted.map(clipEndFrame))
    const gapDurationFrames = Math.max(0, gapEndFrame - gapStartFrame)
    const applyDeletion = (clips: TimelineClipState[]) => clips
      .filter((clip) => !deleteIds.has(clip.clipId))
      .map((clip) => (
        ripple && clip.startFrame >= gapEndFrame
          ? { ...clip, startFrame: Math.max(gapStartFrame, clip.startFrame - gapDurationFrames) }
          : clip
      ))
    setVideoClips((clips) => applyDeletion(clips))
    setAudioClips((clips) => applyDeletion(clips))
    setSelectedClipId(null)
    seekTo(frameToTime(gapStartFrame))
  }, [audioClips, frameToTime, recordUndoSnapshot, seekTo, selectedTimelineClip, videoClips])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const isTyping = ["input", "textarea", "select"].includes(target?.tagName?.toLowerCase() || "") || target?.isContentEditable
      if (isTyping) return
      const commandKey = event.ctrlKey || event.metaKey
      const key = event.key.toLowerCase()
      if (commandKey && key === "z") {
        event.preventDefault()
        if (event.shiftKey) redoEditor()
        else undoEditor()
        return
      }
      if (commandKey && key === "y") {
        event.preventDefault()
        redoEditor()
        return
      }
      if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault()
        deleteSelectedClips(event.shiftKey)
        return
      }
      if (event.code === "Space" || event.key === " ") {
        event.preventDefault()
        togglePlayback()
        return
      }
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault()
        seekTo(currentTimeRef.current + (event.key === "ArrowLeft" ? -1 : 1) / framesPerSecond)
        return
      }
      if (!(commandKey || event.altKey) && key === "v") {
        setTool("select")
        return
      }
      if (!(commandKey || event.altKey) && key === "c") {
        setTool("blade")
        return
      }
      if (!commandKey) return
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
  }, [deleteSelectedClips, framesPerSecond, pxPerSecond, redoEditor, seekTo, togglePlayback, undoEditor, zoomTimelineAt])

  const handleTimelineBackgroundDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null
    const container = timelineRef.current
    if (!container) return
    const time = timeFromPointer(event, container, pxPerSecond)
    if (target?.closest("[data-openreel-timeline-clip]")) return
    if (tool === "blade") {
      recordUndoSnapshot()
      splitTimelineAtFrame(timeToFrame(time))
      return
    }
    beginPlayheadDrag(event)
  }

  const canTrim = Boolean(selectedVideoClip && selectedVideoItem?.type === "video" && selectedVideoClip.durationFrames > 1)
  const selectedSourceDuration = selectedVideoClip ? sourceDurationForClip(selectedVideoClip) : null
  const videoConcatIds = videoClips.map((clip) => clip.mediaId).filter((id) => videoItems.some((item) => item.id === id))
  const audioConcatIds = audioClips.map((clip) => clip.mediaId).filter((id) => audioItems.some((item) => item.id === id))
  const isBusy = Boolean(busy)
  const previewScaleStyle = previewScale === "fit"
    ? { height: "min(100%, 280px)", width: "auto", maxWidth: "100%" }
    : { width: `${previewScale}%`, maxWidth: "640px" }

  return (
    <div
      className="openreel-video-edit-panel nodrag nowheel fixed inset-2 z-[94] overflow-hidden rounded-[4px] border border-[#34383f] bg-[#111316] text-[#d7d9dc] shadow-[0_24px_80px_rgba(0,0,0,0.72)]"
      data-openreel-workflow-ui="true"
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      onWheel={(event) => event.stopPropagation()}
    >
      <div className="flex h-8 items-center justify-between border-b border-[#34383f] bg-[#202328] px-2.5 shadow-[inset_0_1px_rgba(255,255,255,.025)]">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="border-r border-[#454a52] pr-2.5 text-[9px] font-semibold uppercase tracking-[0.16em] text-[#8d939c]">OpenReel Edit</div>
          <div className="max-w-[340px] truncate text-[11px] font-medium text-[#e1e3e6]">{title || "未命名时间线"}</div>
          <div className="font-mono text-[9px] tabular-nums text-[#777d86]">{formatTimePrecise(currentTime)} / {formatTimePrecise(playbackEnd)}</div>
          <div className="font-mono text-[8px] text-[#626871]">{framesPerSecond.toFixed(3).replace(/\.000$/, "")} fps · r{sequenceRevision}</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="flex h-6 w-7 items-center justify-center rounded-[2px] text-[#9ca1a9] transition hover:bg-[#353940] hover:text-white"
          title="关闭编辑器"
          aria-label="关闭编辑器"
        >
          <EditorIcon name="close" />
        </button>
      </div>

      <div className="grid h-[calc(100%-2rem)] w-full min-w-0 grid-rows-[minmax(220px,36%)_minmax(380px,1fr)] bg-[#111316]">
        <div className="grid min-h-0 w-full min-w-0 grid-cols-[238px_minmax(420px,1fr)_284px] border-b border-[#34383f] max-xl:grid-cols-[210px_minmax(360px,1fr)_270px] max-lg:grid-cols-1 max-lg:overflow-y-auto">
          <aside data-openreel-media-bin="true" className="flex min-h-0 flex-col border-r border-[#34383f] bg-[#191b1f]">
            <div className="flex h-8 items-end justify-between border-b border-[#34383f] bg-[#202328] px-2.5">
              <div className="flex h-full items-center border-b-2 border-[#4d92c5] text-[10px] font-semibold text-[#e3e5e8]">媒体池</div>
              <div className="mb-2 font-mono text-[9px] text-[#777d86]">{mediaNodes.length} ITEMS</div>
            </div>
            <div className="grid min-h-0 flex-1 auto-rows-max grid-cols-2 content-start gap-1.5 overflow-y-auto p-2">
              {mediaNodes.map((item) => (
                <MediaBinItem key={item.id} item={item} onInsert={insertMediaItem} />
              ))}
              {mediaNodes.length === 0 && (
                <div className="col-span-2 border border-dashed border-[#3a3f46] px-3 py-6 text-center text-[10px] leading-5 text-[#777d86]">
                  当前项目里的图片、视频和音频会出现在这里
                </div>
              )}
            </div>
          </aside>

          <main data-openreel-preview-pane="true" className="flex min-h-0 min-w-0 flex-col bg-[#111316]">
            <div className="flex h-7 shrink-0 items-center justify-between border-b border-[#2f3339] bg-[#1d2024] px-2.5">
              <span className="text-[9px] font-medium text-[#aeb3ba]">时间线监看器</span>
              <span className="font-mono text-[8px] text-[#656b73]">PROGRAM</span>
            </div>
            <div className="flex min-h-0 flex-1 items-center justify-center bg-[#090a0c] p-1.5">
              <div
                className="relative flex aspect-video max-h-full items-center justify-center overflow-hidden border border-[#292d32] bg-black shadow-[0_0_0_1px_rgba(0,0,0,.8)]"
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
                      muted={audioTrackMuted || Boolean(currentAudioClip?.muted) || (Boolean(currentAudioItem) && !playAudioThroughVideo)}
                      preload="metadata"
                      className="h-full w-full object-contain [color-scheme:dark]"
                      onLoadedMetadata={(event) => {
                        const nextDuration = Number(event.currentTarget.duration || 0)
                        registerSourceDuration(currentVideoItem.src, nextDuration)
                      }}
                    />
                  )
                ) : (
                  <div className="text-[10px] text-[#686e76]">播放头不在视频片段上</div>
                )}
              </div>
              {currentAudioItem && !playAudioThroughVideo && (
                <audio ref={audioRef} src={currentAudioItem.src} preload="metadata" muted={audioTrackMuted || Boolean(currentAudioClip?.muted)} />
              )}
            </div>

            <div className="relative flex h-10 shrink-0 items-center justify-between border-t border-[#30343a] bg-[#1c1f23] px-2.5">
              <div className="flex min-w-[154px] items-center gap-2">
                <span className="font-mono text-[11px] font-medium tabular-nums text-[#d9dde2]">{formatTimePrecise(currentTime)}</span>
                <button
                  type="button"
                  disabled={isBusy || currentVideoItem?.type !== "video"}
                  onClick={() => void runOperation("frame", {
                    operation: "video.export_frame",
                    source_node_id: currentVideoClip?.mediaId || nodeId,
                    frame_mode: "time",
                    time_seconds: Math.max(0,
                      (currentVideoClip?.sourceInFrame || 0) / framesPerSecond +
                      currentTime -
                      (currentVideoClip?.startFrame || 0) / framesPerSecond,
                    ),
                    title: `${title || "视频"} ${formatTime(currentTime)} 画面`,
                  })}
                  className="flex h-6 w-6 items-center justify-center rounded-[2px] text-[#9298a1] transition hover:bg-[#30343a] hover:text-white disabled:opacity-30"
                  title="导出当前帧"
                  aria-label="导出当前帧"
                >
                  <EditorIcon name="frame" className="h-3 w-3" />
                </button>
              </div>
              <div className="absolute left-1/2 flex -translate-x-1/2 items-center gap-1">
                <button
                  type="button"
                  onClick={() => seekTo(Math.max(0, currentTime - 1 / framesPerSecond))}
                  className="flex h-7 w-7 items-center justify-center text-[#a8adb5] hover:text-white"
                  title="后退一帧"
                  aria-label="后退一帧"
                >
                  <EditorIcon name="step-back" />
                </button>
                <button
                  type="button"
                  onClick={togglePlayback}
                  className="flex h-7 w-8 shrink-0 items-center justify-center rounded-[2px] text-[#e2e5e8] transition hover:bg-[#30343a] hover:text-white"
                  title={playing ? "暂停" : "播放"}
                  aria-label={playing ? "暂停" : "播放"}
                >
                  <EditorIcon name={playing ? "pause" : "play"} />
                </button>
                <button
                  type="button"
                  onClick={() => seekTo(Math.min(playbackEnd, currentTime + 1 / framesPerSecond))}
                  className="flex h-7 w-7 items-center justify-center text-[#a8adb5] hover:text-white"
                  title="前进一帧"
                  aria-label="前进一帧"
                >
                  <EditorIcon name="step-forward" />
                </button>
              </div>
              <div className="hidden min-w-[154px] items-center justify-end gap-2 text-[9px] text-[#717780] sm:flex">
                <select
                  value={previewScale}
                  onChange={(event) => setPreviewScale(event.target.value as PreviewScale)}
                  className="h-6 rounded-[2px] border border-[#353a41] bg-[#24272c] px-2 text-[9px] text-[#c8ccd1] outline-none"
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

          <aside data-openreel-inspector-pane="true" className="min-h-0 border-l border-[#34383f] bg-[#191b1f]">
            <div className="flex h-8 items-end justify-between border-b border-[#34383f] bg-[#202328] px-2.5">
              <div className="flex h-full items-center border-b-2 border-[#4d92c5] text-[10px] font-semibold text-[#e3e5e8]">检查器</div>
              <div className="mb-2 text-[8px] uppercase tracking-[0.12em] text-[#6e747d]">Clip</div>
            </div>
            <div className="min-h-0 overflow-y-auto">
              <section className="border-b border-[#34383f] px-3 py-2.5">
                <div className="mb-2 flex items-center justify-between">
                  <div className="text-[9px] font-semibold uppercase tracking-[0.1em] text-[#b8bdc4]">输出与媒体操作</div>
                  <span className={cn("h-1.5 w-1.5 rounded-full", isBusy ? "bg-[#67a9d8]" : "bg-[#6b727b]")} />
                </div>
                <div className="grid grid-cols-2 gap-1.5">
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
                        start_seconds: Math.max(0, (selectedVideoClip?.sourceInFrame || 0) / framesPerSecond),
                        end_seconds: Math.max(
                          MIN_CLIP_SECONDS,
                          ((selectedVideoClip?.sourceInFrame || 0) + (selectedVideoClip?.durationFrames || 1)) / framesPerSecond,
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

              <section className="border-b border-[#34383f] px-3 py-2.5">
                <div className="mb-2 flex items-center justify-between">
                  <div className="text-[9px] font-semibold uppercase tracking-[0.1em] text-[#b8bdc4]">片段属性</div>
                  <div className="font-mono text-[8px] text-[#707680]">SNAP {SNAP_PIXELS}px</div>
                </div>
                {selectedVideoClip && (
                  <div className="mb-2 divide-y divide-[#30343a] border-y border-[#30343a] text-[9px]">
                    <div className="flex items-center justify-between py-1.5">
                      <span className="text-[#777d86]">片段时长</span>
                      <span className="font-mono text-[#d0d4d9]">{formatTimePrecise(selectedVideoClip.durationFrames / framesPerSecond)} · {selectedVideoClip.durationFrames}f</span>
                    </div>
                    <div className="flex items-center justify-between py-1.5">
                      <span className="text-[#777d86]">源入点</span>
                      <span className="font-mono text-[#d0d4d9]">{formatTimePrecise(selectedVideoClip.sourceInFrame / framesPerSecond)} · {selectedVideoClip.sourceInFrame}f</span>
                    </div>
                    <div className="flex items-center justify-between py-1.5">
                      <span className="text-[#777d86]">源出点</span>
                      <span className="font-mono text-[#d0d4d9]">{formatTimePrecise((selectedVideoClip.sourceInFrame + selectedVideoClip.durationFrames) / framesPerSecond)} · {selectedVideoClip.sourceInFrame + selectedVideoClip.durationFrames}f</span>
                    </div>
                    {selectedSourceDuration && (
                      <div className="flex items-center justify-between py-1.5">
                        <span className="text-[#777d86]">源总长</span>
                        <span className="font-mono text-[#d0d4d9]">{formatTimePrecise(selectedSourceDuration)}</span>
                      </div>
                    )}
                  </div>
                )}
                <div className="text-[9px] leading-4 text-[#737983]">
                  边缘修剪受源素材范围约束。切割会同步处理已链接的画面与音频。
                </div>
              </section>

              {selectedAudioClip && (
                <section className="border-b border-[#34383f] px-3 py-2.5">
                  <div className="mb-2 flex items-center justify-between">
                    <div className="text-[9px] font-semibold uppercase tracking-[0.1em] text-[#b8bdc4]">音频片段</div>
                    <button
                      type="button"
                      onClick={() => {
                        recordUndoSnapshot()
                        setAudioClips((clips) => clips.map((clip) => (
                          clip.clipId === selectedAudioClip.clipId ? { ...clip, muted: !clip.muted } : clip
                        )))
                      }}
                      className={cn(
                        "h-5 rounded-[2px] border px-2 text-[8px]",
                        selectedAudioClip.muted
                          ? "border-[#b88a4f] bg-[#6b4b24] text-[#ffe1a8]"
                          : "border-[#3b4148] bg-[#25282d] text-[#9da3ab]",
                      )}
                    >
                      {selectedAudioClip.muted ? "已静音" : "静音"}
                    </button>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="w-9 text-[8px] text-[#777d86]">音量</span>
                    <input
                      type="range"
                      min="-60"
                      max="0"
                      step="0.5"
                      value={selectedAudioClip.gainDb || 0}
                      onPointerDown={recordUndoSnapshot}
                      onChange={(event) => {
                        const gainDb = Number(event.target.value)
                        setAudioClips((clips) => clips.map((clip) => (
                          clip.clipId === selectedAudioClip.clipId ? { ...clip, gainDb } : clip
                        )))
                      }}
                      className="h-1 min-w-0 flex-1 accent-[#6fac8d]"
                      aria-label="片段音量"
                      data-openreel-clip-gain="true"
                    />
                    <span className="w-12 text-right font-mono text-[8px] text-[#c8ccd1]">{(selectedAudioClip.gainDb || 0).toFixed(1)} dB</span>
                  </div>
                  <div className="mt-3 space-y-2 border-t border-[#30343a] pt-2">
                    <div className="flex items-center gap-2">
                      <span className="w-9 text-[8px] text-[#777d86]">淡入</span>
                      <input
                        type="range"
                        min="0"
                        max={selectedAudioClip.durationFrames}
                        step="1"
                        value={selectedAudioClip.fadeInFrames || 0}
                        onPointerDown={recordUndoSnapshot}
                        onChange={(event) => {
                          const fadeInFrames = Math.round(clamp(Number(event.target.value), 0, selectedAudioClip.durationFrames))
                          setAudioClips((clips) => clips.map((clip) => (
                            clip.clipId === selectedAudioClip.clipId
                              ? {
                                  ...clip,
                                  fadeInFrames,
                                  fadeOutFrames: Math.min(clip.fadeOutFrames || 0, clip.durationFrames - fadeInFrames),
                                }
                              : clip
                          )))
                        }}
                        className="h-1 min-w-0 flex-1 accent-[#6fac8d]"
                        aria-label="淡入时长"
                        data-openreel-fade-in="true"
                      />
                      <span className="w-12 text-right font-mono text-[8px] text-[#c8ccd1]">{formatTimePrecise((selectedAudioClip.fadeInFrames || 0) / framesPerSecond)}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="w-9 text-[8px] text-[#777d86]">淡出</span>
                      <input
                        type="range"
                        min="0"
                        max={selectedAudioClip.durationFrames}
                        step="1"
                        value={selectedAudioClip.fadeOutFrames || 0}
                        onPointerDown={recordUndoSnapshot}
                        onChange={(event) => {
                          const fadeOutFrames = Math.round(clamp(Number(event.target.value), 0, selectedAudioClip.durationFrames))
                          setAudioClips((clips) => clips.map((clip) => (
                            clip.clipId === selectedAudioClip.clipId
                              ? {
                                  ...clip,
                                  fadeInFrames: Math.min(clip.fadeInFrames || 0, clip.durationFrames - fadeOutFrames),
                                  fadeOutFrames,
                                }
                              : clip
                          )))
                        }}
                        className="h-1 min-w-0 flex-1 accent-[#6fac8d]"
                        aria-label="淡出时长"
                        data-openreel-fade-out="true"
                      />
                      <span className="w-12 text-right font-mono text-[8px] text-[#c8ccd1]">{formatTimePrecise((selectedAudioClip.fadeOutFrames || 0) / framesPerSecond)}</span>
                    </div>
                  </div>
                </section>
              )}

              {error && (
                <div className="m-3 border border-[#7d4547] bg-[#3c2426] p-2.5 text-[10px] leading-4 text-[#f0c1c3]">
                  {error}
                </div>
              )}
              {busy && (
                <div className="m-3 flex items-center gap-2 border border-[#355a74] bg-[#213746] p-2.5 text-[10px] text-[#c0ddf2]">
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-[#9dc9e8] border-t-transparent" />
                  处理中...
                </div>
              )}
            </div>
          </aside>
        </div>

        <section className="flex min-h-0 w-full min-w-0 flex-col bg-[#15171a]">
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-[#34383f] bg-[#202328] px-2">
            <div className="flex items-center gap-2">
              <div className="mr-1 flex h-9 items-center border-b-2 border-[#4d92c5] px-1 text-[10px] font-semibold text-[#e2e4e7]">时间线 1</div>
              <ToolButton label="选择" icon="pointer" active={tool === "select"} onClick={() => setTool("select")} />
              <ToolButton label="切割" icon="blade" active={tool === "blade"} onClick={() => setTool("blade")} />
              <div className="ml-1 h-4 w-px bg-[#3b4047]" />
              <ToolButton label="撤销 (Ctrl+Z)" icon="undo" disabled={historyDepth.undo === 0} onClick={undoEditor} />
              <ToolButton label="重做 (Ctrl+Shift+Z)" icon="redo" disabled={historyDepth.redo === 0} onClick={redoEditor} />
              <div className="ml-1 h-4 w-px bg-[#3b4047]" />
              <span className="hidden font-mono text-[9px] tabular-nums text-[#777d86] md:inline">{formatTimePrecise(currentTime)}</span>
            </div>
            <div className="flex items-center gap-1.5 text-[9px] text-[#777d86]">
              <button
                type="button"
                onClick={() => zoomTimelineAt(pxPerSecond / 1.25)}
                className="flex h-7 w-7 items-center justify-center rounded-[2px] text-[#aeb3ba] hover:bg-[#30343a] hover:text-white"
                title="缩小时间线"
                aria-label="缩小时间线"
              >
                <EditorIcon name="minus" />
              </button>
              <input
                type="range"
                min="42"
                max={maxPxPerSecond}
                step="1"
                value={pxPerSecond}
                onChange={(event) => zoomTimelineAt(Number(event.target.value))}
                className="h-1 w-20 cursor-pointer accent-[#629dcc]"
                title="时间线缩放"
                aria-label="时间线缩放"
              />
              <button
                type="button"
                onClick={() => zoomTimelineAt(pxPerSecond * 1.25)}
                className="flex h-7 w-7 items-center justify-center rounded-[2px] text-[#aeb3ba] hover:bg-[#30343a] hover:text-white"
                title="放大时间线"
                aria-label="放大时间线"
              >
                <EditorIcon name="plus" />
              </button>
              <span className="w-12 text-right font-mono text-[8px] text-[#676d75]">{Math.round(pxPerSecond)} px/s</span>
            </div>
          </div>
          <div
            ref={timelineRef}
            data-openreel-timeline-scroll="true"
            data-px-per-second={pxPerSecond.toFixed(4)}
            data-track-label-width={TRACK_LABEL_WIDTH}
            className="relative min-h-0 w-full min-w-0 flex-1 overflow-auto bg-[#15171a]"
            onPointerDown={handleTimelineBackgroundDown}
          >
            <div className="relative min-h-full" style={{ width: TRACK_LABEL_WIDTH + timelineWidth }}>
            <div className="sticky top-0 z-20 grid h-7 border-b border-[#353941] bg-[#1c1f23]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="sticky left-0 z-30 flex items-center border-r border-[#3a3f46] bg-[#1f2227] px-2 font-mono text-[8px] text-[#666c74]">TC</div>
              <div className="relative">
                {ticks.map((tick) => (
                  <div
                    key={tick}
                    className="absolute top-0 h-full border-l border-[#3b4048] pl-1 font-mono text-[8px] leading-7 text-[#777d86]"
                    style={{ left: tick * pxPerSecond }}
                  >
                    {formatTime(tick)}
                  </div>
                ))}
              </div>
            </div>

            <div className="relative grid h-[76px] border-b border-[#30343a]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="sticky left-0 z-10 grid grid-cols-[30px_1fr] border-r border-[#3a3f46] bg-[#202328]">
                <div className="flex items-center justify-center border-r border-[#3a3f46] bg-[#2b4f68] text-[10px] font-semibold text-[#d8ecfa]">V1</div>
                <div className="flex min-w-0 flex-col justify-center px-2">
                  <div className="truncate text-[9px] font-medium text-[#d2d5d9]">视频 1</div>
                  <div className="mt-1 flex items-center gap-1.5 text-[#737983]"><EditorIcon name="film" className="h-2.5 w-2.5" /><span className="text-[8px]">VIDEO</span></div>
                </div>
              </div>
              <div
                className="relative bg-[#17191d] [background-image:linear-gradient(90deg,rgba(255,255,255,.025)_1px,transparent_1px)] [background-size:84px_100%]"
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
                      sequenceFps={framesPerSecond}
                      viewport={timelineViewport}
                      sourceDuration={sourceDurationForClip(clip)}
                      mediaIndex={item.type === "video" ? mediaIndexes[item.id] : null}
                      selected={clip.clipId === selectedClipId || Boolean(selectedSyncGroupId && clip.syncGroupId === selectedSyncGroupId)}
                      onBeginEdit={recordUndoSnapshot}
                      onSelect={setSelectedClipId}
                      onDragStartFrame={updateClipStartFrame}
                      onResizeEdge={resizeClipEdge}
                      onCutAtFrame={splitTimelineAtFrame}
                    />
                  )
                })}
              </div>
            </div>

            <div className="relative grid h-[76px] border-b border-[#30343a]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
              <div className="sticky left-0 z-10 grid grid-cols-[30px_1fr] border-r border-[#3a3f46] bg-[#202328]">
                <div className="flex items-center justify-center border-r border-[#3a3f46] bg-[#28503e] text-[10px] font-semibold text-[#d8f1e4]">A1</div>
                <div className="flex min-w-0 flex-col justify-center px-2">
                  <div className="flex items-center justify-between gap-1">
                    <div className="truncate text-[9px] font-medium text-[#d2d5d9]">音频 1</div>
                    <div className="flex gap-0.5">
                      <button
                        type="button"
                        onPointerDown={(event) => event.stopPropagation()}
                        onClick={() => {
                          recordUndoSnapshot()
                          setAudioTrackSolo((value) => !value)
                        }}
                        className={cn(
                          "flex h-4 w-4 items-center justify-center rounded-[2px] border text-[8px] font-semibold",
                          audioTrackSolo
                            ? "border-[#5d91b8] bg-[#315f83] text-[#dcefff]"
                            : "border-[#3b4148] bg-[#25282d] text-[#7b8189] hover:text-white",
                        )}
                        title={audioTrackSolo ? "取消独奏" : "独奏音频轨道"}
                        aria-label={audioTrackSolo ? "取消独奏" : "独奏音频轨道"}
                        data-openreel-track-solo="true"
                      >
                        S
                      </button>
                      <button
                        type="button"
                        onPointerDown={(event) => event.stopPropagation()}
                        onClick={() => {
                          recordUndoSnapshot()
                          setAudioTrackMuted((value) => !value)
                        }}
                        className={cn(
                          "flex h-4 w-4 items-center justify-center rounded-[2px] border text-[8px] font-semibold",
                          audioTrackMuted
                            ? "border-[#b88a4f] bg-[#6b4b24] text-[#ffe1a8]"
                            : "border-[#3b4148] bg-[#25282d] text-[#7b8189] hover:text-white",
                        )}
                        title={audioTrackMuted ? "取消静音" : "静音音频轨道"}
                        aria-label={audioTrackMuted ? "取消静音" : "静音音频轨道"}
                      >
                        M
                      </button>
                    </div>
                  </div>
                  <div className="mt-1 flex items-center gap-1 text-[#737983]">
                    <EditorIcon name="audio" className="h-2.5 w-2.5 shrink-0" />
                    <input
                      type="range"
                      min="-60"
                      max="0"
                      step="0.5"
                      value={audioTrackGainDb}
                      onPointerDown={(event) => {
                        event.stopPropagation()
                        recordUndoSnapshot()
                      }}
                      onChange={(event) => setAudioTrackGainDb(Number(event.target.value))}
                      className="h-1 w-9 min-w-0 accent-[#6fac8d]"
                      aria-label="音频轨道音量"
                      data-openreel-track-gain="true"
                    />
                    <span className="font-mono text-[7px]">{audioTrackGainDb.toFixed(0)}</span>
                  </div>
                </div>
              </div>
              <div
                className="relative bg-[#17191d] [background-image:linear-gradient(90deg,rgba(255,255,255,.025)_1px,transparent_1px)] [background-size:84px_100%]"
                onDragOver={(event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = "copy"
                }}
                onDrop={(event) => handleTrackDrop("audio", event)}
              >
                {audioClips.length === 0 && (
                  <div className="absolute inset-2 flex items-center justify-center border border-dashed border-[#343940] text-[9px] text-[#5f656d]">
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
                      sequenceFps={framesPerSecond}
                      viewport={timelineViewport}
                      sourceDuration={sourceDurationForClip(clip)}
                      mediaIndex={mediaIndexes[mediaSourceKey(item)]}
                      waveformNodeId={mediaSourceKey(item)}
                      trackGainDb={audioTrackGainDb}
                      trackMuted={audioTrackMuted}
                      selected={clip.clipId === selectedClipId || Boolean(selectedSyncGroupId && clip.syncGroupId === selectedSyncGroupId)}
                      onBeginEdit={recordUndoSnapshot}
                      onSelect={setSelectedClipId}
                      onDragStartFrame={updateClipStartFrame}
                      onResizeEdge={resizeClipEdge}
                      onCutAtFrame={splitTimelineAtFrame}
                    />
                  )
                })}
              </div>
            </div>

            <div
              ref={playheadRef}
              className="absolute bottom-0 top-0 z-30 w-px cursor-ew-resize bg-[#ff4d4f] shadow-[0_0_0_1px_rgba(255,77,79,.18)]"
              style={{ left: TRACK_LABEL_WIDTH + currentTime * pxPerSecond }}
              onPointerDown={beginPlayheadDrag}
            >
              <div className="-ml-[4px] h-0 w-0 border-l-[4px] border-r-[4px] border-t-[7px] border-l-transparent border-r-transparent border-t-[#ff4d4f]" />
            </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
