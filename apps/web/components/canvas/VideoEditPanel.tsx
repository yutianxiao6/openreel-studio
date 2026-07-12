"use client"

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import type { DragEvent as ReactDragEvent, PointerEvent as ReactPointerEvent } from "react"
import { runProjectMediaOperation } from "@/lib/api"
import {
  cancelVideoEditorSequenceRender,
  getLatestVideoEditorSequenceRender,
  getVideoEditorFrameTileUrl,
  getVideoEditorMediaIndex,
  getVideoEditorSequenceRender,
  getVideoEditorSequence,
  getVideoEditorWaveformManifest,
  getVideoEditorWaveformPage,
  renderVideoEditorSequence,
  saveVideoEditorSequence,
  type VideoEditorMediaIndex,
  type VideoEditorSequenceRenderJob,
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

type BusyAction = "frame" | "tail" | "split" | "trim" | "concat-video" | "concat-audio" | "render" | null
type TimelineTool = "select" | "blade"
type TrimMode = "normal" | "ripple" | "rolling"
type PreviewScale = "fit" | "50" | "75" | "100"
type PlaybackResolution = "full" | "half" | "quarter"

interface VisualTransformState {
  fit: "contain" | "cover"
  positionX: number
  positionY: number
  scale: number
  rotationDeg: number
  opacity: number
  cropLeft: number
  cropTop: number
  cropRight: number
  cropBottom: number
}

interface TimelineClipState {
  clipId: string
  mediaId: string
  trackId: string
  startFrame: number
  durationFrames: number
  sourceInFrame: number
  syncGroupId?: string
  fullSource?: boolean
  gainDb?: number
  muted?: boolean
  fadeInFrames?: number
  fadeOutFrames?: number
  visualTransform: VisualTransformState
}

interface TimelineTrackState {
  id: string
  kind: "video" | "audio"
  name: string
  order: number
  locked: boolean
  syncLocked: boolean
  visible: boolean
  muted: boolean
  solo: boolean
  gainDb: number
  height: number
}

interface TimelineMarkerState {
  id: string
  frame: number
  label: string
}

type TimelineTransitionKind = "video_cross_dissolve" | "audio_constant_power"

interface TimelineTransitionState {
  id: string
  kind: TimelineTransitionKind
  trackId: string
  outgoingClipId: string
  incomingClipId: string
  durationFrames: number
}

interface EditorSnapshot {
  videoClips: TimelineClipState[]
  audioClips: TimelineClipState[]
  tracks: TimelineTrackState[]
  markers: TimelineMarkerState[]
  transitions: TimelineTransitionState[]
}

interface TimelineViewport {
  startFrame: number
  endFrame: number
}

interface SourceMarkState {
  inFrame: number
  outFrame: number
}

interface MarqueeState {
  left: number
  top: number
  width: number
  height: number
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
const DEFAULT_TRACK_HEIGHT = 76
const MIN_TRACK_HEIGHT = 64
const MAX_TRACK_HEIGHT = 180
const MIN_CLIP_GAIN_DB = -60
const MAX_CLIP_GAIN_DB = 12

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
  | "ripple"
  | "rolling"
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
      {name === "ripple" && <><path d="M4 4v12M8 7l-3 3 3 3M11 6h5M11 10h5M11 14h5" /></>}
      {name === "rolling" && <><path d="M10 3v14M3 7h5M5.5 4.5 8 7 5.5 9.5M17 13h-5M14.5 10.5 12 13l2.5 2.5" /></>}
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

function nominalFramesPerSecond(framesPerSecond: number): number {
  return Math.max(1, Math.round(framesPerSecond))
}

function formatFrameTimecode(frame: number, framesPerSecond: number): string {
  const nominalFps = nominalFramesPerSecond(framesPerSecond)
  const safeFrame = Math.max(0, Math.round(frame))
  const frames = safeFrame % nominalFps
  const totalSeconds = Math.floor(safeFrame / nominalFps)
  const seconds = totalSeconds % 60
  const totalMinutes = Math.floor(totalSeconds / 60)
  const minutes = totalMinutes % 60
  const hours = Math.floor(totalMinutes / 60)
  return [hours, minutes, seconds, frames]
    .map((part) => String(part).padStart(2, "0"))
    .join(":")
}

function parseFrameTimecode(value: string, framesPerSecond: number): number | null {
  const nominalFps = nominalFramesPerSecond(framesPerSecond)
  const parts = value.trim().replaceAll(";", ":").split(":")
  if (parts.length < 2 || parts.length > 4 || parts.some((part) => !/^\d+$/.test(part))) return null
  const values = parts.map(Number)
  const padded = [...Array(4 - values.length).fill(0), ...values]
  const [hours, minutes, seconds, frames] = padded
  if (minutes > 59 || seconds > 59 || frames >= nominalFps) return null
  return (((hours * 60 + minutes) * 60 + seconds) * nominalFps) + frames
}

function FrameTimecodeInput({
  frame,
  framesPerSecond,
  ariaLabel,
  onFocus,
  onCommit,
}: {
  frame: number
  framesPerSecond: number
  ariaLabel: string
  onFocus: () => void
  onCommit: (frame: number) => void
}) {
  const formatted = formatFrameTimecode(frame, framesPerSecond)
  const [draft, setDraft] = useState(formatted)
  const [invalid, setInvalid] = useState(false)

  useEffect(() => {
    setDraft(formatted)
    setInvalid(false)
  }, [formatted])

  const commit = () => {
    const parsed = parseFrameTimecode(draft, framesPerSecond)
    if (parsed === null) {
      setDraft(formatted)
      setInvalid(true)
      return
    }
    setInvalid(false)
    setDraft(formatFrameTimecode(parsed, framesPerSecond))
    onCommit(parsed)
  }

  return (
    <input
      type="text"
      inputMode="numeric"
      spellCheck={false}
      value={draft}
      onFocus={onFocus}
      onChange={(event) => {
        setDraft(event.target.value)
        setInvalid(false)
      }}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur()
        if (event.key === "Escape") {
          setDraft(formatted)
          setInvalid(false)
          event.currentTarget.blur()
        }
      }}
      aria-label={ariaLabel}
      aria-invalid={invalid}
      className={cn(
        "h-5 w-full rounded-[2px] border bg-[#24272c] px-1 text-center font-mono text-[8px] tabular-nums text-[#d5d9de] outline-none focus:border-[#579bd3]",
        invalid ? "border-[#a95356]" : "border-[#3a3f46]",
      )}
    />
  )
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(Math.max(value, min), max)
}

function clipEndFrame(clip: TimelineClipState): number {
  return clip.startFrame + clip.durationFrames
}

function transitionFrameRange(transition: TimelineTransitionState, cutFrame: number) {
  const outgoingFrames = Math.floor(transition.durationFrames / 2)
  return {
    startFrame: cutFrame - outgoingFrames,
    endFrame: cutFrame + transition.durationFrames - outgoingFrames,
  }
}

function clipsShareTimelineRange(a: TimelineClipState, b: TimelineClipState): boolean {
  return Boolean(a.syncGroupId && b.syncGroupId && a.syncGroupId === b.syncGroupId)
}

function defaultVisualTransform(): VisualTransformState {
  return {
    fit: "contain",
    positionX: 0,
    positionY: 0,
    scale: 1,
    rotationDeg: 0,
    opacity: 1,
    cropLeft: 0,
    cropTop: 0,
    cropRight: 0,
    cropBottom: 0,
  }
}

function normalizedVisualTransform(value?: Partial<VisualTransformState> | null): VisualTransformState {
  const cropLeft = clamp(Number(value?.cropLeft || 0), 0, 0.95)
  const cropRight = clamp(Number(value?.cropRight || 0), 0, 0.95 - cropLeft)
  const cropTop = clamp(Number(value?.cropTop || 0), 0, 0.95)
  const cropBottom = clamp(Number(value?.cropBottom || 0), 0, 0.95 - cropTop)
  return {
    fit: value?.fit === "cover" ? "cover" : "contain",
    positionX: clamp(Number(value?.positionX || 0), -2, 2),
    positionY: clamp(Number(value?.positionY || 0), -2, 2),
    scale: clamp(Number(value?.scale || 1), 0.1, 4),
    rotationDeg: clamp(Number(value?.rotationDeg || 0), -360, 360),
    opacity: clamp(Number(value?.opacity ?? 1), 0, 1),
    cropLeft,
    cropTop,
    cropRight,
    cropBottom,
  }
}

function cloneTimelineClip(clip: TimelineClipState): TimelineClipState {
  return {
    ...clip,
    visualTransform: { ...clip.visualTransform },
  }
}

function visualTransformStyle(value: VisualTransformState) {
  return {
    objectFit: value.fit,
    opacity: value.opacity,
    clipPath: `inset(${value.cropTop * 100}% ${value.cropRight * 100}% ${value.cropBottom * 100}% ${value.cropLeft * 100}%)`,
    transform: `translate(${value.positionX * 100}%, ${value.positionY * 100}%) scale(${value.scale}) rotate(${value.rotationDeg}deg)`,
    transformOrigin: "center center",
  } as const
}

function drawProgramFrame(
  context: CanvasRenderingContext2D,
  video: HTMLVideoElement,
  width: number,
  height: number,
  value: VisualTransformState,
) {
  const sourceWidth = video.videoWidth
  const sourceHeight = video.videoHeight
  if (!sourceWidth || !sourceHeight) return
  const fitScale = value.fit === "cover"
    ? Math.max(width / sourceWidth, height / sourceHeight)
    : Math.min(width / sourceWidth, height / sourceHeight)
  const renderedWidth = sourceWidth * fitScale * value.scale
  const renderedHeight = sourceHeight * fitScale * value.scale
  const sourceX = sourceWidth * value.cropLeft
  const sourceY = sourceHeight * value.cropTop
  const sourceCropWidth = sourceWidth * (1 - value.cropLeft - value.cropRight)
  const sourceCropHeight = sourceHeight * (1 - value.cropTop - value.cropBottom)
  const destinationX = -renderedWidth / 2 + renderedWidth * value.cropLeft
  const destinationY = -renderedHeight / 2 + renderedHeight * value.cropTop
  const destinationWidth = renderedWidth * (1 - value.cropLeft - value.cropRight)
  const destinationHeight = renderedHeight * (1 - value.cropTop - value.cropBottom)
  context.save()
  context.clearRect(0, 0, width, height)
  context.globalAlpha = value.opacity
  context.translate(width / 2 + value.positionX * width, height / 2 + value.positionY * height)
  context.rotate(value.rotationDeg * Math.PI / 180)
  context.drawImage(
    video,
    sourceX,
    sourceY,
    sourceCropWidth,
    sourceCropHeight,
    destinationX,
    destinationY,
    destinationWidth,
    destinationHeight,
  )
  context.restore()
}

function createClipId(mediaId: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10)
  return `clip:${mediaId}:${Date.now().toString(36)}:${random}`
}

function createMarkerId(): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10)
  return `marker:${Date.now().toString(36)}:${random}`
}

function createTransitionId(kind: TimelineTransitionKind): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10)
  return `transition:${kind}:${Date.now().toString(36)}:${random}`
}

function createSyncGroupId(seed = "media"): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(36).slice(2, 10)
  return `sync:${seed}:${Date.now().toString(36)}:${random}`
}

function createClip(
  mediaId: string,
  trackId: string,
  startFrame: number,
  durationFrames: number,
  sourceInFrame = 0,
  syncGroupId?: string,
  fullSource = false,
): TimelineClipState {
  return {
    clipId: createClipId(mediaId),
    mediaId,
    trackId,
    startFrame: Math.max(0, Math.round(startFrame)),
    durationFrames: Math.max(1, Math.round(durationFrames)),
    sourceInFrame: Math.max(0, Math.round(sourceInFrame)),
    syncGroupId,
    fullSource,
    gainDb: 0,
    muted: false,
    fadeInFrames: 0,
    fadeOutFrames: 0,
    visualTransform: defaultVisualTransform(),
  }
}

function defaultTimelineTracks(): TimelineTrackState[] {
  return [
    {
      id: "v1",
      kind: "video",
      name: "视频 1",
      order: 0,
      locked: false,
      syncLocked: true,
      visible: true,
      muted: false,
      solo: false,
      gainDb: 0,
      height: DEFAULT_TRACK_HEIGHT,
    },
    {
      id: "a1",
      kind: "audio",
      name: "音频 1",
      order: 0,
      locked: false,
      syncLocked: true,
      visible: true,
      muted: false,
      solo: false,
      gainDb: 0,
      height: DEFAULT_TRACK_HEIGHT,
    },
  ]
}

function timelineGaps(clips: TimelineClipState[], endFrame: number): Array<{ startFrame: number; durationFrames: number }> {
  if (clips.length === 0 || endFrame <= 0) return []
  const sorted = [...clips].sort((left, right) => left.startFrame - right.startFrame)
  const gaps: Array<{ startFrame: number; durationFrames: number }> = []
  let cursor = 0
  for (const clip of sorted) {
    if (clip.startFrame > cursor) {
      gaps.push({ startFrame: cursor, durationFrames: clip.startFrame - cursor })
    }
    cursor = Math.max(cursor, clipEndFrame(clip))
  }
  if (cursor < endFrame) gaps.push({ startFrame: cursor, durationFrames: endFrame - cursor })
  return gaps
}

function clipRightSegment(
  clip: TimelineClipState,
  startFrame: number,
  groupIds: Map<string, string>,
): TimelineClipState {
  const sourceDelta = startFrame - clip.startFrame
  const groupKey = clip.syncGroupId || clip.clipId
  const syncGroupId = groupIds.get(groupKey) || createSyncGroupId(groupKey)
  groupIds.set(groupKey, syncGroupId)
  return {
    ...clip,
    clipId: createClipId(clip.mediaId),
    startFrame,
    durationFrames: clipEndFrame(clip) - startFrame,
    sourceInFrame: clip.sourceInFrame + sourceDelta,
    syncGroupId,
    fullSource: false,
    fadeInFrames: 0,
  }
}

function insertGapIntoClips(
  clips: TimelineClipState[],
  insertionFrame: number,
  durationFrames: number,
  affectedTrackIds: Set<string>,
  rightGroupIds: Map<string, string>,
): TimelineClipState[] {
  return clips.flatMap((clip) => {
    if (!affectedTrackIds.has(clip.trackId) || clipEndFrame(clip) <= insertionFrame) return [clip]
    if (clip.startFrame >= insertionFrame) {
      return [{ ...clip, startFrame: clip.startFrame + durationFrames }]
    }
    const leftDuration = insertionFrame - clip.startFrame
    const right = clipRightSegment(clip, insertionFrame, rightGroupIds)
    return [
      {
        ...clip,
        durationFrames: leftDuration,
        fullSource: false,
        fadeOutFrames: 0,
      },
      {
        ...right,
        startFrame: insertionFrame + durationFrames,
        fadeOutFrames: Math.min(clip.fadeOutFrames || 0, right.durationFrames),
      },
    ]
  })
}

function overwriteClipsInRange(
  clips: TimelineClipState[],
  startFrame: number,
  durationFrames: number,
  targetTrackIds: Set<string>,
  rightGroupIds: Map<string, string>,
): TimelineClipState[] {
  const endFrame = startFrame + durationFrames
  return clips.flatMap((clip) => {
    const oldEndFrame = clipEndFrame(clip)
    if (!targetTrackIds.has(clip.trackId) || oldEndFrame <= startFrame || clip.startFrame >= endFrame) {
      return [clip]
    }
    const next: TimelineClipState[] = []
    if (clip.startFrame < startFrame) {
      next.push({
        ...clip,
        durationFrames: startFrame - clip.startFrame,
        fullSource: false,
        fadeOutFrames: 0,
      })
    }
    if (oldEndFrame > endFrame) {
      const right = clipRightSegment(clip, endFrame, rightGroupIds)
      next.push({
        ...right,
        fadeOutFrames: Math.min(clip.fadeOutFrames || 0, right.durationFrames),
      })
    }
    return next
  })
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

function canRoutePreviewAudio(sourceUrl: string): boolean {
  if (typeof window === "undefined" || !sourceUrl) return false
  try {
    const parsed = new URL(sourceUrl, window.location.href)
    if (["blob:", "data:"].includes(parsed.protocol) || parsed.origin === window.location.origin) return true
    const loopbackHosts = new Set(["127.0.0.1", "localhost", "[::1]"])
    return loopbackHosts.has(parsed.hostname) && loopbackHosts.has(window.location.hostname)
  } catch {
    return false
  }
}

function previewMediaCrossOrigin(sourceUrl: string): "anonymous" | undefined {
  if (typeof window === "undefined" || !canRoutePreviewAudio(sourceUrl)) return undefined
  try {
    return new URL(sourceUrl, window.location.href).origin === window.location.origin ? undefined : "anonymous"
  } catch {
    return undefined
  }
}

function requiresCanvasVideoPreview(): boolean {
  if (typeof navigator === "undefined") return false
  const userAgent = navigator.userAgent || ""
  return /Windows/i.test(userAgent) && /Electron/i.test(userAgent)
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
  height,
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
  height: number
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
    const cssHeight = Math.max(24, Math.round(height))
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
  }, [clipDuration, fadeInSeconds, fadeOutSeconds, gainDb, height, muted, page, width])

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
      className="absolute inset-x-0 bottom-1 w-full"
      style={{ width: Math.max(1, width), height: Math.max(24, height) }}
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
  onOverwrite,
  onSelect,
  selected,
}: {
  item: VideoEditPanelMediaNode
  onInsert: (item: VideoEditPanelMediaNode) => void
  onOverwrite: (item: VideoEditPanelMediaNode) => void
  onSelect: (item: VideoEditPanelMediaNode) => void
  selected?: boolean
}) {
  return (
    <div
      draggable
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "copy"
        event.dataTransfer.setData("openreel/media-id", item.id)
      }}
      onClick={() => onSelect(item)}
      onDoubleClick={() => onInsert(item)}
      data-openreel-media-item="true"
      data-media-id={item.id}
      data-media-type={item.type}
      className={cn(
        "group min-w-0 cursor-grab border bg-[#1b1e22] p-1 transition hover:border-[#4b515a] hover:bg-[#22262b] active:cursor-grabbing",
        selected ? "border-[#5596c5] bg-[#222d36]" : "border-transparent",
      )}
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
        <div className="absolute bottom-1 right-1 flex gap-0.5 opacity-0 transition group-hover:opacity-100">
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              onSelect(item)
              onInsert(item)
            }}
            className="flex h-5 min-w-5 items-center justify-center border border-white/20 bg-black/80 px-1 font-mono text-[8px] text-white hover:bg-[#315f83]"
            title="插入到目标轨道 (,)"
            aria-label={`插入素材 ${item.title}`}
          >
            ,
          </button>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              onSelect(item)
              onOverwrite(item)
            }}
            className="flex h-5 min-w-5 items-center justify-center border border-white/20 bg-black/80 px-1 font-mono text-[8px] text-white hover:bg-[#7b5f35]"
            title="覆盖到目标轨道 (.)"
            aria-label={`覆盖素材 ${item.title}`}
          >
            .
          </button>
        </div>
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
  trimMode,
  pxPerSecond,
  sequenceFps,
  trackHeight,
  viewport,
  sourceDuration,
  mediaIndex,
  waveformNodeId,
  trackGainDb,
  trackMuted,
  disabled,
  selected,
  onBeginEdit,
  onEditEnd,
  onSelect,
  onDragStartFrame,
  onResizeEdge,
  onAudioGainChange,
  onAudioFadeChange,
  onCutAtFrame,
}: {
  projectId: string
  clip: TimelineClipState
  item: VideoEditPanelMediaNode
  kind: "video" | "audio"
  activeTool: TimelineTool
  trimMode: TrimMode
  pxPerSecond: number
  sequenceFps: number
  trackHeight: number
  viewport: TimelineViewport
  sourceDuration: number | null
  mediaIndex?: VideoEditorMediaIndex | null
  waveformNodeId?: string
  trackGainDb?: number
  trackMuted?: boolean
  disabled?: boolean
  selected?: boolean
  onBeginEdit: () => void
  onEditEnd: () => void
  onSelect: (clipId: string, options: { additive: boolean; independent: boolean }) => void
  onDragStartFrame: (kind: "video" | "audio", clipId: string, startFrame: number, trackId: string) => void
  onResizeEdge: (kind: "video" | "audio", clipId: string, edge: "start" | "end", edgeFrame: number) => void
  onAudioGainChange: (clipId: string, gainDb: number) => void
  onAudioFadeChange: (clipId: string, edge: "in" | "out", frames: number) => void
  onCutAtFrame: (frame: number, clipId: string, independent?: boolean) => void
}) {
  const [dragging, setDragging] = useState(false)
  const left = clip.startFrame / sequenceFps * pxPerSecond
  const width = Math.max(18, clip.durationFrames / sequenceFps * pxPerSecond)
  const clipHeight = Math.max(52, trackHeight - 12)
  const clipDurationSeconds = clip.durationFrames / sequenceFps
  const sourceInSeconds = clip.sourceInFrame / sequenceFps
  const audioEnvelopeTop = Math.max(34, Math.round(clipHeight * 0.6))
  const audioEnvelopeBottom = Math.max(audioEnvelopeTop + 8, clipHeight - 8)
  const clipGainDb = clamp(clip.gainDb || 0, MIN_CLIP_GAIN_DB, MAX_CLIP_GAIN_DB)
  const gainLineY = audioEnvelopeTop + (
    (MAX_CLIP_GAIN_DB - clipGainDb) / (MAX_CLIP_GAIN_DB - MIN_CLIP_GAIN_DB)
  ) * (audioEnvelopeBottom - audioEnvelopeTop)
  const fadeInX = clamp((clip.fadeInFrames || 0) / clip.durationFrames * width, 0, width)
  const fadeOutX = clamp(width - (clip.fadeOutFrames || 0) / clip.durationFrames * width, 0, width)
  const fadeHandleInset = Math.min(10, width / 2)
  const fadeInHandleX = clamp(fadeInX, fadeHandleInset, width - fadeHandleInset)
  const fadeOutHandleX = clamp(fadeOutX, fadeHandleInset, width - fadeHandleInset)

  const beginMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || disabled) return
    const target = event.target as HTMLElement | null
    if (target?.dataset.edgeHandle) return
    event.preventDefault()
    event.stopPropagation()
    onSelect(clip.clipId, {
      additive: event.ctrlKey || event.metaKey || event.shiftKey,
      independent: event.altKey,
    })
    if (activeTool === "blade") {
      onBeginEdit()
      const rect = event.currentTarget.getBoundingClientRect()
      const localFrame = Math.round(clamp((event.clientX - rect.left) / pxPerSecond * sequenceFps, 0, clip.durationFrames))
      onCutAtFrame(clip.startFrame + localFrame, clip.clipId, event.altKey)
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
      const targetRow = document.elementFromPoint(moveEvent.clientX, moveEvent.clientY)
        ?.closest<HTMLElement>(`[data-openreel-track-kind="${kind}"]`)
      const targetTrackId = targetRow?.dataset.openreelTrackId || clip.trackId
      onDragStartFrame(kind, clip.clipId, Math.max(0, initialStartFrame + deltaFrames), targetTrackId)
    }
    const onEnd = () => {
      setDragging(false)
      onEditEnd()
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }

  const beginResize = (edge: "start" | "end", event: ReactPointerEvent<HTMLButtonElement>) => {
    if (disabled) return
    event.preventDefault()
    event.stopPropagation()
    onSelect(clip.clipId, {
      additive: event.ctrlKey || event.metaKey || event.shiftKey,
      independent: event.altKey,
    })
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
      onEditEnd()
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }

  const beginAudioGainAdjust = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (kind !== "audio" || disabled) return
    event.preventDefault()
    event.stopPropagation()
    onSelect(clip.clipId, { additive: false, independent: true })
    onBeginEdit()
    const startY = event.clientY
    const initialGainDb = clipGainDb
    const updateGain = (clientY: number, fineAdjustment: boolean) => {
      const dbPerPixel = fineAdjustment ? 0.1 : 0.5
      const gainDb = Math.round(clamp(
        initialGainDb - (clientY - startY) * dbPerPixel,
        MIN_CLIP_GAIN_DB,
        MAX_CLIP_GAIN_DB,
      ) * 2) / 2
      onAudioGainChange(clip.clipId, gainDb)
    }
    const onMove = (moveEvent: PointerEvent) => updateGain(moveEvent.clientY, moveEvent.shiftKey)
    const onEnd = () => {
      onEditEnd()
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }

  const beginAudioFadeAdjust = (edge: "in" | "out", event: ReactPointerEvent<HTMLButtonElement>) => {
    if (kind !== "audio" || disabled) return
    event.preventDefault()
    event.stopPropagation()
    onSelect(clip.clipId, { additive: false, independent: true })
    onBeginEdit()
    const clipRect = event.currentTarget.closest<HTMLElement>("[data-openreel-timeline-clip]")?.getBoundingClientRect()
    if (!clipRect) return
    const updateFade = (clientX: number) => {
      const localX = clamp(clientX - clipRect.left, 0, clipRect.width)
      const frames = edge === "in"
        ? Math.round(localX / clipRect.width * clip.durationFrames)
        : Math.round((clipRect.width - localX) / clipRect.width * clip.durationFrames)
      onAudioFadeChange(clip.clipId, edge, frames)
    }
    updateFade(event.clientX)
    const onMove = (moveEvent: PointerEvent) => updateFade(moveEvent.clientX)
    const onEnd = () => {
      onEditEnd()
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
      data-media-id={clip.mediaId}
      data-track-id={clip.trackId}
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
      data-trim-mode={trimMode}
      data-selected={selected ? "true" : "false"}
      onPointerDown={beginMove}
      className={cn(
        "group/clip absolute top-1.5 overflow-hidden rounded-[2px] border shadow-[0_1px_2px_rgba(0,0,0,.55)]",
        kind === "video"
          ? "border-[#315f80] bg-[#254d69]"
          : "border-[#32664f] bg-[#24523e]",
        selected && "border-[#a9cdec] ring-1 ring-[#6ca9d8]",
        activeTool === "blade" && "cursor-crosshair",
        dragging && "cursor-grabbing opacity-85",
        !dragging && activeTool !== "blade" && "cursor-grab",
        disabled && "cursor-not-allowed opacity-60",
      )}
      style={{ left, width, height: clipHeight }}
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
        <>
          {waveformNodeId ? (
            <RealAudioWaveform
              projectId={projectId}
              nodeId={waveformNodeId}
              sourceOffset={sourceInSeconds}
              clipDuration={clipDurationSeconds}
              width={width}
              height={clipHeight - 24}
              gainDb={(clip.gainDb || 0) + (trackGainDb || 0)}
              muted={Boolean(clip.muted || trackMuted)}
              fadeInSeconds={(clip.fadeInFrames || 0) / sequenceFps}
              fadeOutSeconds={(clip.fadeOutFrames || 0) / sequenceFps}
            />
          ) : null}
          <svg
            viewBox={`0 0 ${width} ${clipHeight}`}
            preserveAspectRatio="none"
            className="pointer-events-none absolute inset-0 z-[11] h-full w-full overflow-visible"
            aria-hidden="true"
          >
            <path
              d={`M 0 ${audioEnvelopeBottom} L ${fadeInX} ${gainLineY} L ${fadeOutX} ${gainLineY} L ${width} ${audioEnvelopeBottom}`}
              fill="none"
              stroke="rgba(219,239,228,.9)"
              strokeWidth="1"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
          <button
            type="button"
            data-openreel-audio-rubber-band="true"
            data-gain-db={clipGainDb.toFixed(1)}
            onPointerDown={beginAudioGainAdjust}
            className="absolute left-2 right-2 z-[12] h-2 -translate-y-1/2 cursor-ns-resize bg-transparent before:absolute before:inset-x-0 before:top-1/2 before:h-px before:bg-[#dbeee4]/90 hover:before:h-0.5 hover:before:bg-white"
            style={{ top: gainLineY }}
            title={`片段音量 ${clipGainDb.toFixed(1)} dB · 上下拖动`}
            aria-label={`时间轴片段音量 ${item.title}`}
          />
          <button
            type="button"
            data-openreel-audio-fade-handle="true"
            data-fade-edge="in"
            onPointerDown={(event) => beginAudioFadeAdjust("in", event)}
            className="absolute z-[14] h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rotate-45 cursor-ew-resize border border-[#e8fff3] bg-[#5d9879] shadow-[0_1px_2px_rgba(0,0,0,.7)]"
            style={{ left: fadeInHandleX, top: gainLineY }}
            title={`直接调整淡入 · ${clip.fadeInFrames || 0}f`}
            aria-label={`直接调整淡入 ${item.title}`}
          />
          <button
            type="button"
            data-openreel-audio-fade-handle="true"
            data-fade-edge="out"
            onPointerDown={(event) => beginAudioFadeAdjust("out", event)}
            className="absolute z-[14] h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rotate-45 cursor-ew-resize border border-[#e8fff3] bg-[#5d9879] shadow-[0_1px_2px_rgba(0,0,0,.7)]"
            style={{ left: fadeOutHandleX, top: gainLineY }}
            title={`直接调整淡出 · ${clip.fadeOutFrames || 0}f`}
            aria-label={`直接调整淡出 ${item.title}`}
          />
        </>
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
          trimMode === "ripple" && "border-[#e2b96f] bg-[#c38b32]/15",
          trimMode === "rolling" && "border-[#c19be6] bg-[#8b63b2]/15",
        )}
        title={trimMode === "normal" ? "收放起点" : trimMode === "ripple" ? "波纹裁剪起点" : "滚动编辑起点"}
        aria-label={trimMode === "normal" ? "收放起点" : trimMode === "ripple" ? "波纹裁剪起点" : "滚动编辑起点"}
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
          trimMode === "ripple" && "border-[#e2b96f] bg-[#c38b32]/15",
          trimMode === "rolling" && "border-[#c19be6] bg-[#8b63b2]/15",
        )}
        title={trimMode === "normal" ? "收放终点" : trimMode === "ripple" ? "波纹裁剪终点" : "滚动编辑终点"}
        aria-label={trimMode === "normal" ? "收放终点" : trimMode === "ripple" ? "波纹裁剪终点" : "滚动编辑终点"}
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
  const programCanvasRef = useRef<HTMLCanvasElement | null>(null)
  const transitionVideoRef = useRef<HTMLVideoElement | null>(null)
  const transitionProgramCanvasRef = useRef<HTMLCanvasElement | null>(null)
  const transitionOutgoingAudioRef = useRef<HTMLAudioElement | null>(null)
  const transitionIncomingAudioRef = useRef<HTMLAudioElement | null>(null)
  const previewAudioContextRef = useRef<AudioContext | null>(null)
  const previewGainNodesRef = useRef(new WeakMap<HTMLMediaElement, GainNode>())
  const previewGainValuesRef = useRef(new WeakMap<HTMLMediaElement, number>())
  const decodedVideoClockRef = useRef<{ mediaTime: number; observedAt: number } | null>(null)
  const suppressMediaClockUntilRef = useRef(0)
  const sourcePreviewRef = useRef<HTMLVideoElement | null>(null)
  const timelineRef = useRef<HTMLDivElement | null>(null)
  const playheadRef = useRef<HTMLDivElement | null>(null)
  const currentTimeRef = useRef(0)
  const pxPerSecondRef = useRef(DEFAULT_PX_PER_SECOND)
  const pendingZoomRef = useRef<{ anchorTime: number; localX: number } | null>(null)
  const initializedNodeRef = useRef<string | null>(null)
  const sequenceRevisionRef = useRef(0)
  const lastSavedSequenceRef = useRef("")
  const sequenceSaveChainRef = useRef<Promise<void>>(Promise.resolve())
  const renderPollTokenRef = useRef(0)
  const undoStackRef = useRef<EditorSnapshot[]>([])
  const redoStackRef = useRef<EditorSnapshot[]>([])
  const videoClipsRef = useRef<TimelineClipState[]>([])
  const audioClipsRef = useRef<TimelineClipState[]>([])
  const tracksRef = useRef<TimelineTrackState[]>(defaultTimelineTracks())
  const markersRef = useRef<TimelineMarkerState[]>([])
  const transitionsRef = useRef<TimelineTransitionState[]>([])
  const selectedClipIdsRef = useRef<Set<string>>(new Set())
  const [sourceDurations, setSourceDurations] = useState<Record<string, number>>({})
  const [mediaIndexes, setMediaIndexes] = useState<Record<string, VideoEditorMediaIndex>>({})
  const [sequenceLoaded, setSequenceLoaded] = useState(false)
  const [sequenceRevision, setSequenceRevision] = useState(0)
  const [sequenceFrameRate, setSequenceFrameRate] = useState({ numerator: DEFAULT_FRAME_RATE, denominator: 1 })
  const [currentTime, setCurrentTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [playbackDirection, setPlaybackDirection] = useState<1 | -1>(1)
  const [tool, setTool] = useState<TimelineTool>("select")
  const [trimMode, setTrimMode] = useState<TrimMode>("normal")
  const [snappingEnabled, setSnappingEnabled] = useState(true)
  const [snapGuideFrame, setSnapGuideFrame] = useState<number | null>(null)
  const [pxPerSecond, setPxPerSecond] = useState(DEFAULT_PX_PER_SECOND)
  const [previewScale, setPreviewScale] = useState<PreviewScale>("fit")
  const [playbackResolution, setPlaybackResolution] = useState<PlaybackResolution>("full")
  const canvasVideoPreview = useMemo(requiresCanvasVideoPreview, [])
  const [loopEnabled, setLoopEnabled] = useState(false)
  const [loopRange, setLoopRange] = useState({ inFrame: 0, outFrame: 0 })
  const [tracks, setTracks] = useState<TimelineTrackState[]>(defaultTimelineTracks)
  const [markers, setMarkers] = useState<TimelineMarkerState[]>([])
  const [transitions, setTransitions] = useState<TimelineTransitionState[]>([])
  const [selectedMarkerId, setSelectedMarkerId] = useState<string | null>(null)
  const [activeVideoTrackId, setActiveVideoTrackId] = useState("v1")
  const [activeAudioTrackId, setActiveAudioTrackId] = useState("a1")
  const [selectedMediaId, setSelectedMediaId] = useState(nodeId)
  const [sourceMarks, setSourceMarks] = useState<Record<string, SourceMarkState>>({})
  const [sourceCursorFrame, setSourceCursorFrame] = useState(0)
  const [timelineViewport, setTimelineViewport] = useState<TimelineViewport>({
    startFrame: 0,
    endFrame: DEFAULT_TIMELINE_SECONDS * DEFAULT_FRAME_RATE,
  })
  const [busy, setBusy] = useState<BusyAction>(null)
  const [error, setError] = useState<string | null>(null)
  const [renderNotice, setRenderNotice] = useState<string | null>(null)
  const [renderJob, setRenderJob] = useState<VideoEditorSequenceRenderJob | null>(null)
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null)
  const [selectedClipIds, setSelectedClipIds] = useState<Set<string>>(() => new Set())
  const [marquee, setMarquee] = useState<MarqueeState | null>(null)
  const [videoClips, setVideoClips] = useState<TimelineClipState[]>([])
  const [audioClips, setAudioClips] = useState<TimelineClipState[]>([])
  const [historyDepth, setHistoryDepth] = useState({ undo: 0, redo: 0 })
  currentTimeRef.current = currentTime
  videoClipsRef.current = videoClips
  audioClipsRef.current = audioClips
  tracksRef.current = tracks
  markersRef.current = markers
  transitionsRef.current = transitions
  selectedClipIdsRef.current = selectedClipIds
  const framesPerSecond = sequenceFrameRate.numerator / sequenceFrameRate.denominator
  const trackById = useMemo(() => new Map(tracks.map((track) => [track.id, track])), [tracks])
  const videoTracks = useMemo(() => tracks
    .filter((track) => track.kind === "video")
    .sort((left, right) => right.order - left.order), [tracks])
  const audioTracks = useMemo(() => tracks
    .filter((track) => track.kind === "audio")
    .sort((left, right) => left.order - right.order), [tracks])

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
  const selectedMediaItem = mediaById.get(selectedMediaId) || mediaById.get(nodeId) || mediaNodes[0]
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
  const selectedSourceFrameCount = selectedMediaItem?.type === "image"
    ? Math.max(1, Math.round(DEFAULT_CLIP_SECONDS * framesPerSecond))
    : sourceFrameCountForItem(selectedMediaItem) || Math.max(1, Math.round(DEFAULT_CLIP_SECONDS * framesPerSecond))
  const selectedSourceMark = sourceMarks[selectedMediaItem?.id || ""] || {
    inFrame: 0,
    outFrame: selectedSourceFrameCount,
  }
  const updateSelectedSourceMark = useCallback((patch: Partial<SourceMarkState>) => {
    if (!selectedMediaItem) return
    setSourceMarks((current) => {
      const existing = current[selectedMediaItem.id] || { inFrame: 0, outFrame: selectedSourceFrameCount }
      const requestedIn = patch.inFrame ?? existing.inFrame
      const requestedOut = patch.outFrame ?? existing.outFrame
      const inFrame = Math.round(clamp(requestedIn, 0, selectedSourceFrameCount - 1))
      const outFrame = Math.round(clamp(requestedOut, inFrame + 1, selectedSourceFrameCount))
      return { ...current, [selectedMediaItem.id]: { inFrame, outFrame } }
    })
  }, [selectedMediaItem, selectedSourceFrameCount])
  useEffect(() => {
    setSourceCursorFrame((current) => Math.round(clamp(
      Number.isFinite(current) ? current : selectedSourceMark.inFrame,
      selectedSourceMark.inFrame,
      Math.max(selectedSourceMark.inFrame, selectedSourceMark.outFrame - 1),
    )))
  }, [selectedMediaId, selectedSourceMark.inFrame, selectedSourceMark.outFrame])
  useEffect(() => {
    const preview = sourcePreviewRef.current
    if (!preview || selectedMediaItem?.type !== "video") return
    const nextTime = sourceCursorFrame / framesPerSecond
    if (Math.abs((preview.currentTime || 0) - nextTime) > 0.02) preview.currentTime = nextTime
    preview.pause()
  }, [framesPerSecond, selectedMediaItem, sourceCursorFrame])
  const selectedTimelineClip = useMemo(() => (
    [...videoClips, ...audioClips].find((clip) => clip.clipId === selectedClipId)
  ), [audioClips, selectedClipId, videoClips])
  const selectTimelineClip = useCallback((
    clipId: string,
    options: { additive: boolean; independent: boolean },
  ) => {
    const allClips = [...videoClips, ...audioClips]
    const target = allClips.find((clip) => clip.clipId === clipId)
    if (!target) return
    setSelectedMarkerId(null)
    const targetIds = new Set(allClips
      .filter((clip) => clip.clipId === clipId || (!options.independent && clipsShareTimelineRange(clip, target)))
      .map((clip) => clip.clipId))
    const removing = options.additive && [...targetIds].every((id) => selectedClipIds.has(id))
    const next = options.additive ? new Set(selectedClipIds) : new Set<string>()
    if (!options.additive) {
      targetIds.forEach((id) => next.add(id))
    } else {
      const remove = [...targetIds].every((id) => next.has(id))
      for (const id of targetIds) {
        if (remove) next.delete(id)
        else next.add(id)
      }
    }
    selectedClipIdsRef.current = next
    setSelectedClipIds(next)
    setSelectedClipId(removing
      ? [...selectedClipIds].find((id) => !targetIds.has(id)) || null
      : clipId)
  }, [audioClips, selectedClipIds, videoClips])
  const clearTimelineSelection = useCallback(() => {
    setSelectedClipId(null)
    selectedClipIdsRef.current = new Set()
    setSelectedClipIds(new Set())
    setSelectedMarkerId(null)
  }, [])
  const captureEditorSnapshot = useCallback((): EditorSnapshot => ({
    videoClips: videoClipsRef.current.map(cloneTimelineClip),
    audioClips: audioClipsRef.current.map(cloneTimelineClip),
    tracks: tracksRef.current.map((track) => ({ ...track })),
    markers: markersRef.current.map((marker) => ({ ...marker })),
    transitions: transitionsRef.current.map((transition) => ({ ...transition })),
  }), [])
  const updateHistoryDepth = useCallback(() => {
    setHistoryDepth({
      undo: undoStackRef.current.length,
      redo: redoStackRef.current.length,
    })
  }, [])
  const applyEditorSnapshot = useCallback((snapshot: EditorSnapshot) => {
    const nextVideoClips = snapshot.videoClips.map(cloneTimelineClip)
    const nextAudioClips = snapshot.audioClips.map(cloneTimelineClip)
    const nextTracks = snapshot.tracks.map((track) => ({ ...track }))
    const nextMarkers = snapshot.markers.map((marker) => ({ ...marker }))
    const nextTransitions = snapshot.transitions.map((transition) => ({ ...transition }))
    videoClipsRef.current = nextVideoClips
    audioClipsRef.current = nextAudioClips
    tracksRef.current = nextTracks
    markersRef.current = nextMarkers
    transitionsRef.current = nextTransitions
    setVideoClips(nextVideoClips)
    setAudioClips(nextAudioClips)
    setTracks(nextTracks)
    setMarkers(nextMarkers)
    setTransitions(nextTransitions)
    setActiveVideoTrackId((current) => nextTracks.some((track) => track.id === current && track.kind === "video")
      ? current
      : nextTracks.find((track) => track.kind === "video")?.id || "v1")
    setActiveAudioTrackId((current) => nextTracks.some((track) => track.id === current && track.kind === "audio")
      ? current
      : nextTracks.find((track) => track.kind === "audio")?.id || "a1")
    setSelectedClipId(null)
    setSelectedClipIds(new Set())
    setSelectedMarkerId(null)
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
  const selectedVideoOutgoingClip = useMemo(() => (
    selectedVideoClip
      ? videoClips.find((clip) => (
          clip.trackId === selectedVideoClip.trackId &&
          clipEndFrame(clip) === selectedVideoClip.startFrame &&
          clip.clipId !== selectedVideoClip.clipId
        ))
      : undefined
  ), [selectedVideoClip, videoClips])
  const selectedAudioOutgoingClip = useMemo(() => (
    selectedAudioClip
      ? audioClips.find((clip) => (
          clip.trackId === selectedAudioClip.trackId &&
          clipEndFrame(clip) === selectedAudioClip.startFrame &&
          clip.clipId !== selectedAudioClip.clipId
        ))
      : undefined
  ), [audioClips, selectedAudioClip])
  const selectedVideoTransition = useMemo(() => (
    selectedVideoClip && selectedVideoOutgoingClip
      ? transitions.find((transition) => (
          transition.kind === "video_cross_dissolve" &&
          transition.outgoingClipId === selectedVideoOutgoingClip.clipId &&
          transition.incomingClipId === selectedVideoClip.clipId
        ))
      : undefined
  ), [selectedVideoClip, selectedVideoOutgoingClip, transitions])
  const selectedAudioTransition = useMemo(() => (
    selectedAudioClip && selectedAudioOutgoingClip
      ? transitions.find((transition) => (
          transition.kind === "audio_constant_power" &&
          transition.outgoingClipId === selectedAudioOutgoingClip.clipId &&
          transition.incomingClipId === selectedAudioClip.clipId
        ))
      : undefined
  ), [selectedAudioClip, selectedAudioOutgoingClip, transitions])
  const maxTransitionDuration = useCallback((
    outgoing: TimelineClipState,
    incoming: TimelineClipState,
    excludeTransitionId?: string,
  ) => {
    const outgoingSourceFrames = sourceFrameCountForClip(outgoing)
    const outgoingTailHandle = outgoingSourceFrames === null
      ? 2_400
      : Math.max(0, outgoingSourceFrames - outgoing.sourceInFrame - outgoing.durationFrames)
    const incomingSourceFrames = sourceFrameCountForClip(incoming)
    const incomingHeadHandle = incomingSourceFrames === null ? 2_400 : incoming.sourceInFrame
    let maxOutgoingSide = Math.min(outgoing.durationFrames, incomingHeadHandle)
    let maxIncomingSide = Math.min(incoming.durationFrames, outgoingTailHandle)
    const allClips = new Map([...videoClips, ...audioClips].map((clip) => [clip.clipId, clip]))
    for (const transition of transitions) {
      if (transition.id === excludeTransitionId || transition.trackId !== incoming.trackId) continue
      const existingIncoming = allClips.get(transition.incomingClipId)
      if (!existingIncoming) continue
      const range = transitionFrameRange(transition, existingIncoming.startFrame)
      if (range.endFrame <= incoming.startFrame) {
        maxOutgoingSide = Math.min(maxOutgoingSide, incoming.startFrame - range.endFrame)
      } else if (range.startFrame >= incoming.startFrame) {
        maxIncomingSide = Math.min(maxIncomingSide, range.startFrame - incoming.startFrame)
      } else {
        return 0
      }
    }
    const durationFrames = Math.min(2_400, maxOutgoingSide * 2 + 1, maxIncomingSide * 2)
    return durationFrames >= 2 ? durationFrames : 0
  }, [audioClips, sourceFrameCountForClip, transitions, videoClips])
  const setCutTransition = useCallback((
    kind: TimelineTransitionKind,
    outgoing: TimelineClipState | undefined,
    incoming: TimelineClipState | undefined,
    existing: TimelineTransitionState | undefined,
  ) => {
    if (existing) {
      recordUndoSnapshot()
      setTransitions((current) => current.filter((transition) => transition.id !== existing.id))
      return
    }
    if (!outgoing || !incoming) {
      setError("请选择剪切点右侧的相邻片段")
      return
    }
    const maxDuration = maxTransitionDuration(outgoing, incoming)
    if (maxDuration < 2) {
      setError("素材把手不足：请先向外裁剪前后片段，为转场保留源画面")
      return
    }
    recordUndoSnapshot()
    setTransitions((current) => [...current, {
      id: createTransitionId(kind),
      kind,
      trackId: incoming.trackId,
      outgoingClipId: outgoing.clipId,
      incomingClipId: incoming.clipId,
      durationFrames: Math.min(24, maxDuration),
    }])
    setError(null)
  }, [maxTransitionDuration, recordUndoSnapshot])
  const updateCutTransitionDuration = useCallback((
    transition: TimelineTransitionState,
    outgoing: TimelineClipState,
    incoming: TimelineClipState,
    value: number,
  ) => {
    const maxDuration = maxTransitionDuration(outgoing, incoming, transition.id)
    const durationFrames = Math.round(clamp(value, 2, maxDuration))
    setTransitions((current) => current.map((candidate) => (
      candidate.id === transition.id ? { ...candidate, durationFrames } : candidate
    )))
  }, [maxTransitionDuration])
  const updateSelectedVisualTransform = useCallback((patch: Partial<VisualTransformState>) => {
    if (!selectedVideoClip) return
    if (currentTimeRef.current * framesPerSecond < selectedVideoClip.startFrame ||
        currentTimeRef.current * framesPerSecond >= clipEndFrame(selectedVideoClip)) {
      const previewFrame = selectedVideoClip.startFrame + Math.min(
        Math.max(0, Math.floor(selectedVideoClip.durationFrames / 2)),
        selectedVideoClip.durationFrames - 1,
      )
      const previewTime = previewFrame / framesPerSecond
      currentTimeRef.current = previewTime
      setCurrentTime(previewTime)
    }
    setVideoClips((clips) => clips.map((clip) => (
      clip.clipId === selectedVideoClip.clipId
        ? {
            ...clip,
            visualTransform: normalizedVisualTransform({ ...clip.visualTransform, ...patch }),
          }
        : clip
    )))
  }, [framesPerSecond, selectedVideoClip])
  const resetSelectedVisualTransform = useCallback(() => {
    if (!selectedVideoClip) return
    recordUndoSnapshot()
    updateSelectedVisualTransform(defaultVisualTransform())
  }, [recordUndoSnapshot, selectedVideoClip, updateSelectedVisualTransform])
  const currentFrame = Math.round(currentTime * framesPerSecond)
  const currentVideoClip = useMemo(() => {
    for (const track of videoTracks) {
      if (!track.visible) continue
      const clip = videoClips.find((candidate) => (
        candidate.trackId === track.id &&
        currentFrame >= candidate.startFrame &&
        currentFrame < clipEndFrame(candidate)
      ))
      if (clip) return clip
    }
    return undefined
  }, [currentFrame, videoClips, videoTracks])
  const currentAudioClip = useMemo(() => {
    const hasSolo = audioTracks.some((track) => track.solo)
    for (const track of audioTracks) {
      if (track.muted || (hasSolo && !track.solo)) continue
      const clip = audioClips.find((candidate) => (
        candidate.trackId === track.id &&
        currentFrame >= candidate.startFrame &&
        currentFrame < clipEndFrame(candidate)
      ))
      if (clip && !clip.muted) return clip
    }
    return undefined
  }, [audioClips, audioTracks, currentFrame])
  const currentVideoItem = currentVideoClip ? mediaById.get(currentVideoClip.mediaId) : undefined
  const currentAudioItem = currentAudioClip ? mediaById.get(currentAudioClip.mediaId) : undefined
  const currentAudioTrack = currentAudioClip ? trackById.get(currentAudioClip.trackId) : undefined
  const activeVideoTransition = useMemo(() => {
    for (const track of videoTracks) {
      if (!track.visible) continue
      for (const transition of transitions) {
        if (transition.kind !== "video_cross_dissolve" || transition.trackId !== track.id) continue
        const outgoing = videoClips.find((clip) => clip.clipId === transition.outgoingClipId)
        const incoming = videoClips.find((clip) => clip.clipId === transition.incomingClipId)
        if (!outgoing || !incoming) continue
        const range = transitionFrameRange(transition, incoming.startFrame)
        if (currentFrame >= range.startFrame && currentFrame < range.endFrame) {
          return {
            transition,
            outgoing,
            incoming,
            range,
            progress: clamp((currentFrame - range.startFrame) / transition.durationFrames, 0, 1),
          }
        }
      }
    }
    return undefined
  }, [currentFrame, transitions, videoClips, videoTracks])
  const activeAudioTransition = useMemo(() => {
    const hasSolo = audioTracks.some((track) => track.solo)
    for (const track of audioTracks) {
      if (track.muted || (hasSolo && !track.solo)) continue
      for (const transition of transitions) {
        if (transition.kind !== "audio_constant_power" || transition.trackId !== track.id) continue
        const outgoing = audioClips.find((clip) => clip.clipId === transition.outgoingClipId)
        const incoming = audioClips.find((clip) => clip.clipId === transition.incomingClipId)
        if (!outgoing || !incoming) continue
        const range = transitionFrameRange(transition, incoming.startFrame)
        if (currentFrame >= range.startFrame && currentFrame < range.endFrame) {
          const progress = clamp((currentFrame - range.startFrame) / transition.durationFrames, 0, 1)
          return {
            transition,
            outgoing,
            incoming,
            outgoingItem: mediaById.get(outgoing.mediaId),
            incomingItem: mediaById.get(incoming.mediaId),
            track,
            range,
            progress,
            outgoingPower: Math.cos(progress * Math.PI / 2),
            incomingPower: Math.sin(progress * Math.PI / 2),
          }
        }
      }
    }
    return undefined
  }, [audioClips, audioTracks, currentFrame, mediaById, transitions])
  const videoTransitionSingleSource = Boolean(
    activeVideoTransition &&
    mediaById.get(activeVideoTransition.outgoing.mediaId)?.src === mediaById.get(activeVideoTransition.incoming.mediaId)?.src &&
    activeVideoTransition.outgoing.sourceInFrame - activeVideoTransition.outgoing.startFrame ===
      activeVideoTransition.incoming.sourceInFrame - activeVideoTransition.incoming.startFrame &&
    JSON.stringify(activeVideoTransition.outgoing.visualTransform) === JSON.stringify(activeVideoTransition.incoming.visualTransform),
  )
  const audioTransitionSingleSource = Boolean(
    activeAudioTransition &&
    activeAudioTransition.outgoingItem?.src === activeAudioTransition.incomingItem?.src &&
    activeAudioTransition.outgoing.sourceInFrame - activeAudioTransition.outgoing.startFrame ===
      activeAudioTransition.incoming.sourceInFrame - activeAudioTransition.incoming.startFrame,
  )
  const audioTransitionThroughVideo = Boolean(
    audioTransitionSingleSource &&
    currentVideoItem?.type === "video" &&
    activeAudioTransition?.outgoingItem?.synthetic &&
    activeAudioTransition.outgoingItem.src === currentVideoItem.src,
  )
  const transitionVideoClip = activeVideoTransition && !videoTransitionSingleSource
    ? (currentVideoClip?.clipId === activeVideoTransition.outgoing.clipId
        ? activeVideoTransition.incoming
        : activeVideoTransition.outgoing)
    : undefined
  const transitionVideoItem = transitionVideoClip ? mediaById.get(transitionVideoClip.mediaId) : undefined
  const transitionVisualTransform = useMemo(
    () => normalizedVisualTransform(transitionVideoClip?.visualTransform),
    [transitionVideoClip?.visualTransform],
  )
  const selectedVideoItem = selectedVideoClip ? mediaById.get(selectedVideoClip.mediaId) : undefined
  const currentVisualTransform = useMemo(
    () => normalizedVisualTransform(currentVideoClip?.visualTransform),
    [currentVideoClip?.visualTransform],
  )
  const currentVisualTransformStyle = useMemo(
    () => visualTransformStyle(currentVisualTransform),
    [currentVisualTransform],
  )
  const primaryMediaIndex = sourceVideoItem ? mediaIndexes[sourceVideoItem.id] : undefined
  const maxPxPerSecond = Math.max(220, framesPerSecond * FRAME_DETAIL_WIDTH)
  const playAudioThroughVideo = Boolean(
    !activeAudioTransition &&
    currentVideoClip &&
    currentAudioClip &&
    currentVideoItem?.type === "video" &&
    currentAudioItem?.synthetic &&
    currentVideoItem.src === currentAudioItem.src &&
    currentVideoClip.syncGroupId &&
    currentVideoClip.syncGroupId === currentAudioClip.syncGroupId,
  )
  const setPreviewMediaGain = useCallback((element: HTMLMediaElement | null, amplitude: number) => {
    if (!element) return
    const safeAmplitude = clamp(amplitude, 0, gainAmplitude(MAX_CLIP_GAIN_DB * 2))
    previewGainValuesRef.current.set(element, safeAmplitude)
    const gainNode = previewGainNodesRef.current.get(element)
    if (gainNode) {
      gainNode.gain.value = safeAmplitude
      element.volume = 1
    } else {
      element.volume = Math.min(1, safeAmplitude)
    }
  }, [])
  const ensurePreviewAudioGraph = useCallback(async () => {
    const AudioContextConstructor = window.AudioContext
    if (!AudioContextConstructor) return
    let context = previewAudioContextRef.current
    if (!context || context.state === "closed") {
      context = new AudioContextConstructor()
      previewAudioContextRef.current = context
    }
    const elements: Array<HTMLMediaElement | null> = [
      videoRef.current,
      audioRef.current,
      transitionOutgoingAudioRef.current,
      transitionIncomingAudioRef.current,
    ]
    for (const element of elements) {
      if (!element || previewGainNodesRef.current.has(element)) continue
      const sourceUrl = element.currentSrc || element.src
      try {
        if (!canRoutePreviewAudio(sourceUrl)) continue
        const source = context.createMediaElementSource(element)
        const gainNode = context.createGain()
        gainNode.gain.value = previewGainValuesRef.current.get(element) ?? element.volume
        source.connect(gainNode).connect(context.destination)
        previewGainNodesRef.current.set(element, gainNode)
        element.volume = 1
      } catch {
        // Cross-origin media without Web Audio permission keeps native volume.
      }
    }
    if (context.state === "suspended") await context.resume()
  }, [])
  useEffect(() => () => {
    const context = previewAudioContextRef.current
    previewAudioContextRef.current = null
    if (context && context.state !== "closed") void context.close()
  }, [])
  useEffect(() => {
    if (!playing) return
    void ensurePreviewAudioGraph()
  }, [activeAudioTransition?.transition.id, currentAudioItem?.id, currentVideoItem?.id, ensurePreviewAudioGraph, playing])
  const programVideoGap = !currentVideoClip
  const programAudioGap = !currentAudioClip
  const sequenceEndFrame = useMemo(() => (
    Math.max(0, ...videoClips.map(clipEndFrame), ...audioClips.map(clipEndFrame))
  ), [audioClips, videoClips])
  const playbackEnd = sequenceEndFrame / framesPerSecond
  const effectiveLoopOutFrame = loopRange.outFrame > loopRange.inFrame
    ? Math.min(sequenceEndFrame, loopRange.outFrame)
    : sequenceEndFrame
  const playbackClockSource = playbackDirection < 0
    ? "timeline"
    : audioTransitionThroughVideo && currentVideoClip && currentVideoItem?.type === "video"
      ? "video-pts"
      : activeAudioTransition?.outgoingItem || activeAudioTransition?.incomingItem
      ? "audio-transition"
      : currentAudioClip && currentAudioItem && !playAudioThroughVideo
      ? "audio"
      : currentVideoClip && currentVideoItem?.type === "video"
        ? "video-pts"
        : "timeline"

  useEffect(() => {
    setLoopRange((current) => {
      if (sequenceEndFrame <= 0) return { inFrame: 0, outFrame: 0 }
      const inFrame = Math.min(current.inFrame, sequenceEndFrame - 1)
      const outFrame = current.outFrame > inFrame
        ? Math.min(current.outFrame, sequenceEndFrame)
        : sequenceEndFrame
      return inFrame === current.inFrame && outFrame === current.outFrame ? current : { inFrame, outFrame }
    })
  }, [sequenceEndFrame])
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
    const clipSpec = (clip: TimelineClipState) => ({
      id: clip.clipId,
      track_id: clip.trackId,
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
      visual_transform: {
        fit: clip.visualTransform.fit,
        position_x: clip.visualTransform.positionX,
        position_y: clip.visualTransform.positionY,
        scale: clip.visualTransform.scale,
        rotation_deg: clip.visualTransform.rotationDeg,
        opacity: clip.visualTransform.opacity,
        crop_left: clip.visualTransform.cropLeft,
        crop_top: clip.visualTransform.cropTop,
        crop_right: clip.visualTransform.cropRight,
        crop_bottom: clip.visualTransform.cropBottom,
      },
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
      tracks: tracks.map((track) => ({
        id: track.id,
        kind: track.kind,
        name: track.name,
        order: track.order,
        locked: track.locked,
        sync_locked: track.syncLocked,
        visible: track.visible,
        muted: track.muted,
        solo: track.solo,
        gain_db: track.gainDb,
        height_px: track.height,
      })),
      clips: [
        ...videoClips.map(clipSpec),
        ...audioClips.map(clipSpec),
      ],
      markers: markers.map((marker) => ({
        id: marker.id,
        frame: marker.frame,
        label: marker.label,
      })),
      transitions: transitions.map((transition) => ({
        id: transition.id,
        kind: transition.kind,
        track_id: transition.trackId,
        outgoing_clip_id: transition.outgoingClipId,
        incoming_clip_id: transition.incomingClipId,
        duration_frames: transition.durationFrames,
      })),
    }
  }, [audioClips, markers, primaryMediaIndex, sequenceFrameRate, sourceFrameCountForClip, tracks, transitions, videoClips])

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
    renderPollTokenRef.current += 1
    setRenderJob(null)
    setRenderNotice(null)
    setTool("select")
    setTrimMode("normal")
    setSnappingEnabled(true)
    setSnapGuideFrame(null)
    setPlaybackDirection(1)
    setPlaybackResolution("full")
    setLoopEnabled(false)
    setLoopRange({ inFrame: 0, outFrame: 0 })
    decodedVideoClockRef.current = null
    setSourceMarks({})
    setSourceCursorFrame(0)
    setSelectedClipId(null)
    setSelectedClipIds(new Set())
    setMarkers([])
    setTransitions([])
    setSelectedMarkerId(null)
    setVideoClips([])
    setAudioClips([])
    setTracks(defaultTimelineTracks())
    setActiveVideoTrackId("v1")
    setActiveAudioTrackId("a1")
    setSelectedMediaId(nodeId)
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
        trackId: clip.track_id,
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
        visualTransform: normalizedVisualTransform({
          fit: clip.visual_transform?.fit,
          positionX: clip.visual_transform?.position_x,
          positionY: clip.visual_transform?.position_y,
          scale: clip.visual_transform?.scale,
          rotationDeg: clip.visual_transform?.rotation_deg,
          opacity: clip.visual_transform?.opacity,
          cropLeft: clip.visual_transform?.crop_left,
          cropTop: clip.visual_transform?.crop_top,
          cropRight: clip.visual_transform?.crop_right,
          cropBottom: clip.visual_transform?.crop_bottom,
        }),
      }))
      const restoredTracks = document.spec.tracks.map((track): TimelineTrackState => ({
        id: track.id,
        kind: track.kind,
        name: track.name,
        order: track.order,
        locked: track.locked,
        syncLocked: track.sync_locked,
        visible: track.visible,
        muted: track.muted,
        solo: track.solo,
        gainDb: track.gain_db,
        height: track.height_px || DEFAULT_TRACK_HEIGHT,
      }))
      setSequenceFrameRate(document.spec.settings.frame_rate)
      setTracks(restoredTracks)
      setMarkers(document.spec.markers || [])
      setTransitions((document.spec.transitions || []).map((transition) => ({
        id: transition.id,
        kind: transition.kind,
        trackId: transition.track_id,
        outgoingClipId: transition.outgoing_clip_id,
        incomingClipId: transition.incoming_clip_id,
        durationFrames: transition.duration_frames,
      })))
      setVideoClips(restored.filter((clip) => trackKinds.get(
        document.spec.clips.find((item) => item.id === clip.clipId)?.track_id || "",
      ) === "video"))
      setAudioClips(restored.filter((clip) => trackKinds.get(
        document.spec.clips.find((item) => item.id === clip.clipId)?.track_id || "",
      ) === "audio"))
      setActiveVideoTrackId(restoredTracks.find((track) => track.kind === "video")?.id || "v1")
      setActiveAudioTrackId(restoredTracks.find((track) => track.kind === "audio")?.id || "a1")
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
    setVideoClips([createClip(primary.id, "v1", 0, durationFrames, 0, syncGroupId, primary.type === "video")])
    const primaryAudio = primary.type === "video" ? audioItemForVideo(primary) : undefined
    setAudioClips(primaryAudio
      ? [createClip(primaryAudio.id, "a1", 0, durationFrames, 0, syncGroupId, true)]
      : [])
    setTransitions([])
    setSelectedClipId(null)
    setSelectedClipIds(new Set())
    setCurrentTime(0)
    setPlaying(false)
    setTracks(defaultTimelineTracks())
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
    if (!sequenceLoaded) return
    const clipsById = new Map([...videoClips, ...audioClips].map((clip) => [clip.clipId, clip]))
    setTransitions((current) => {
      const valid = current.filter((transition) => {
        const outgoing = clipsById.get(transition.outgoingClipId)
        const incoming = clipsById.get(transition.incomingClipId)
        if (!outgoing || !incoming || outgoing.trackId !== transition.trackId || incoming.trackId !== transition.trackId) return false
        if (clipEndFrame(outgoing) !== incoming.startFrame) return false
        return transition.durationFrames <= maxTransitionDuration(outgoing, incoming, transition.id)
      })
      return valid.length === current.length ? current : valid
    })
  }, [audioClips, maxTransitionDuration, sequenceLoaded, videoClips])

  const persistSequenceNow = useCallback(async (): Promise<number> => {
    if (!sequenceLoaded || initializedNodeRef.current !== nodeId) {
      throw new Error("剪辑序列尚未加载完成")
    }
    const payloadKey = JSON.stringify(sequenceSpec)
    if (payloadKey === lastSavedSequenceRef.current) return sequenceRevisionRef.current
    const savePromise = sequenceSaveChainRef.current
      .catch(() => undefined)
      .then(async () => {
        if (payloadKey === lastSavedSequenceRef.current) return sequenceRevisionRef.current
        const document = await saveVideoEditorSequence(
          projectId,
          nodeId,
          sequenceRevisionRef.current,
          sequenceSpec,
        )
        sequenceRevisionRef.current = document.revision
        lastSavedSequenceRef.current = JSON.stringify(document.spec)
        setSequenceRevision(document.revision)
        return document.revision
      })
    sequenceSaveChainRef.current = savePromise.then(() => undefined)
    return savePromise
  }, [nodeId, projectId, sequenceLoaded, sequenceSpec])

  useEffect(() => {
    if (!sequenceLoaded || initializedNodeRef.current !== nodeId) return
    const payloadKey = JSON.stringify(sequenceSpec)
    if (payloadKey === lastSavedSequenceRef.current) return
    const timer = window.setTimeout(() => {
      void persistSequenceNow().catch((reason) => {
        setError(reason instanceof Error ? reason.message : "剪辑序列自动保存失败")
      })
    }, 650)
    return () => window.clearTimeout(timer)
  }, [nodeId, persistSequenceNow, sequenceLoaded, sequenceSpec])

  const monitorRenderJob = useCallback(async (initialJob: VideoEditorSequenceRenderJob) => {
    const token = renderPollTokenRef.current + 1
    renderPollTokenRef.current = token
    let current = initialJob
    setRenderJob(current)
    setBusy("render")
    try {
      while (["queued", "running", "cancelling"].includes(current.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, 650))
        if (renderPollTokenRef.current !== token) return
        current = await getVideoEditorSequenceRender(projectId, nodeId, current.id)
        if (renderPollTokenRef.current !== token) return
        setRenderJob(current)
      }
      if (current.status === "completed") {
        const render = current.result?.render
        setRenderNotice(render
          ? `已导出 r${current.sequence_revision} · ${render.width}×${render.height} · ${render.duration_frames} 帧`
          : `已导出序列 r${current.sequence_revision}`)
        await onCommitted()
      } else if (current.status === "failed") {
        setError(current.error_message || "时间线导出失败")
      }
    } catch (reason) {
      if (renderPollTokenRef.current === token) {
        setError(reason instanceof Error ? reason.message : "无法读取导出进度")
      }
    } finally {
      if (renderPollTokenRef.current === token) setBusy(null)
    }
  }, [nodeId, onCommitted, projectId])

  useEffect(() => {
    if (!sequenceLoaded || initializedNodeRef.current !== nodeId) return
    let cancelled = false
    void getLatestVideoEditorSequenceRender(projectId, nodeId).then((job) => {
      if (cancelled || !job) return
      if (["queued", "running", "cancelling"].includes(job.status)) {
        void monitorRenderJob(job)
        return
      }
      if (["failed", "cancelled"].includes(job.status)) {
        setRenderJob(job)
        if (job.status === "failed") setError(job.error_message || "上一次时间线导出失败")
      }
    }).catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [monitorRenderJob, nodeId, projectId, sequenceLoaded])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !currentVideoClip || (playing && playbackDirection > 0)) return
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
  }, [currentTime, currentVideoClip, framesPerSecond, mediaById, playbackDirection, playing])

  useEffect(() => {
    const video = transitionVideoRef.current
    if (!video || !transitionVideoClip || transitionVideoItem?.type !== "video") {
      video?.pause()
      return
    }
    const sourceFrameCount = sourceFrameCountForClip(transitionVideoClip)
    const localFrame = clamp(
      currentFrame - transitionVideoClip.startFrame + transitionVideoClip.sourceInFrame,
      0,
      Math.max(0, (sourceFrameCount || Number.MAX_SAFE_INTEGER) - 1),
    )
    const localTime = localFrame / framesPerSecond
    if (playing && playbackDirection > 0) {
      if (video.paused) {
        if (Math.abs((video.currentTime || 0) - localTime) > 0.15) video.currentTime = localTime
        void video.play().catch(() => undefined)
      }
    } else {
      if (Math.abs((video.currentTime || 0) - localTime) > 0.04) video.currentTime = localTime
      video.pause()
    }
  }, [currentFrame, framesPerSecond, playbackDirection, playing, sourceFrameCountForClip, transitionVideoClip, transitionVideoItem])

  useEffect(() => {
    if (!currentAudioClip) return
    const localFrame = clamp(currentFrame - currentAudioClip.startFrame, 0, currentAudioClip.durationFrames)
    const fadeInFrames = currentAudioClip.fadeInFrames || 0
    const fadeOutFrames = currentAudioClip.fadeOutFrames || 0
    const fadeIn = fadeInFrames > 0 ? Math.min(1, localFrame / fadeInFrames) : 1
    const remainingFrames = Math.max(0, currentAudioClip.durationFrames - localFrame)
    const fadeOut = fadeOutFrames > 0 ? Math.min(1, remainingFrames / fadeOutFrames) : 1
    const amplitude = gainAmplitude((currentAudioClip.gainDb || 0) + (currentAudioTrack?.gainDb || 0)) * Math.min(fadeIn, fadeOut)
    if (playAudioThroughVideo) setPreviewMediaGain(videoRef.current, amplitude)
    setPreviewMediaGain(audioRef.current, amplitude)
  }, [currentAudioClip, currentAudioTrack?.gainDb, currentFrame, playAudioThroughVideo, setPreviewMediaGain])

  useEffect(() => {
    const outgoingAudio = transitionOutgoingAudioRef.current
    const incomingAudio = transitionIncomingAudioRef.current
    if (!activeAudioTransition) {
      outgoingAudio?.pause()
      incomingAudio?.pause()
      return
    }
    if (audioTransitionThroughVideo) {
      outgoingAudio?.pause()
      incomingAudio?.pause()
      const outgoingAmplitude = activeAudioTransition.outgoing.muted
        ? 0
        : gainAmplitude((activeAudioTransition.outgoing.gainDb || 0) + activeAudioTransition.track.gainDb) * activeAudioTransition.outgoingPower
      const incomingAmplitude = activeAudioTransition.incoming.muted
        ? 0
        : gainAmplitude((activeAudioTransition.incoming.gainDb || 0) + activeAudioTransition.track.gainDb) * activeAudioTransition.incomingPower
      setPreviewMediaGain(videoRef.current, Math.hypot(outgoingAmplitude, incomingAmplitude))
      return
    }
    const syncAudio = (
      audio: HTMLAudioElement | null,
      clip: TimelineClipState,
      power: number,
    ) => {
      if (!audio) return
      const sourceFrameCount = sourceFrameCountForClip(clip)
      const localFrame = clamp(
        currentFrame - clip.startFrame + clip.sourceInFrame,
        0,
        Math.max(0, (sourceFrameCount || Number.MAX_SAFE_INTEGER) - 1),
      )
      const localTime = localFrame / framesPerSecond
      const clipAmplitude = clip.muted
        ? 0
        : gainAmplitude((clip.gainDb || 0) + activeAudioTransition.track.gainDb) * power
      const pairedAmplitude = audioTransitionSingleSource && clip.clipId === activeAudioTransition.outgoing.clipId
        ? (activeAudioTransition.incoming.muted
            ? 0
            : gainAmplitude((activeAudioTransition.incoming.gainDb || 0) + activeAudioTransition.track.gainDb) * activeAudioTransition.incomingPower)
        : 0
      setPreviewMediaGain(audio, audioTransitionSingleSource ? Math.hypot(clipAmplitude, pairedAmplitude) : clipAmplitude)
      if (playing && playbackDirection > 0) {
        if (audio.paused) {
          if (Math.abs((audio.currentTime || 0) - localTime) > 0.15) audio.currentTime = localTime
          void audio.play().catch(() => undefined)
        }
      } else {
        if (Math.abs((audio.currentTime || 0) - localTime) > 0.04) audio.currentTime = localTime
        audio.pause()
      }
    }
    syncAudio(outgoingAudio, activeAudioTransition.outgoing, activeAudioTransition.outgoingPower)
    if (audioTransitionSingleSource) incomingAudio?.pause()
    else syncAudio(incomingAudio, activeAudioTransition.incoming, activeAudioTransition.incomingPower)
  }, [activeAudioTransition, audioTransitionSingleSource, audioTransitionThroughVideo, currentFrame, framesPerSecond, playbackDirection, playing, setPreviewMediaGain, sourceFrameCountForClip])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !currentAudioClip || !currentAudioItem || playAudioThroughVideo) {
      audio?.pause()
      return
    }
    if (playing && playbackDirection > 0) return
    const localTime = currentAudioClip.sourceInFrame / framesPerSecond + clamp(
      currentTime - currentAudioClip.startFrame / framesPerSecond,
      0,
      currentAudioClip.durationFrames / framesPerSecond,
    )
    if (Math.abs((audio.currentTime || 0) - localTime) > 0.08) {
      audio.currentTime = localTime
    }
    audio.pause()
  }, [currentAudioClip, currentAudioItem, currentTime, framesPerSecond, playbackDirection, playAudioThroughVideo, playing])

  useEffect(() => {
    const video = videoRef.current
    const audio = audioRef.current
    if (!playing) {
      video?.pause()
      audio?.pause()
      return
    }
    if (playbackDirection < 0) {
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
  }, [currentAudioClip, currentAudioItem, currentVideoClip, currentVideoItem, framesPerSecond, playbackDirection, playAudioThroughVideo, playing])

  useEffect(() => {
    const video = videoRef.current
    decodedVideoClockRef.current = null
    if (!video || currentVideoItem?.type !== "video" || typeof video.requestVideoFrameCallback !== "function") return
    let callbackId = 0
    let cancelled = false
    const observe = (now: number, metadata: VideoFrameCallbackMetadata) => {
      if (cancelled) return
      decodedVideoClockRef.current = { mediaTime: metadata.mediaTime, observedAt: now }
      callbackId = video.requestVideoFrameCallback(observe)
    }
    callbackId = video.requestVideoFrameCallback(observe)
    return () => {
      cancelled = true
      if (callbackId) video.cancelVideoFrameCallback(callbackId)
      decodedVideoClockRef.current = null
    }
  }, [currentVideoClip?.clipId, currentVideoItem])

  useEffect(() => {
    const video = videoRef.current
    const canvas = programCanvasRef.current
    if (!video || !canvas || currentVideoItem?.type !== "video" || (playbackResolution === "full" && !canvasVideoPreview)) return
    const factor = playbackResolution === "full" ? 1 : playbackResolution === "half" ? 0.5 : 0.25
    const width = Math.max(16, Math.round(sequenceSpec.settings.width * factor))
    const height = Math.max(16, Math.round(sequenceSpec.settings.height * factor))
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext("2d", { alpha: true })
    if (!context) return
    let callbackId = 0
    let animationFrame = 0
    let cancelled = false
    let renderedFrames = 0
    const draw = () => {
      if (cancelled || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return
      drawProgramFrame(context, video, width, height, currentVisualTransform)
      renderedFrames += 1
      canvas.dataset.renderedFrames = String(renderedFrames)
      canvas.dataset.visualSignature = [
        currentVisualTransform.fit,
        currentVisualTransform.positionX,
        currentVisualTransform.positionY,
        currentVisualTransform.scale,
        currentVisualTransform.rotationDeg,
        currentVisualTransform.opacity,
        currentVisualTransform.cropTop,
        currentVisualTransform.cropRight,
        currentVisualTransform.cropBottom,
        currentVisualTransform.cropLeft,
      ].join(":")
    }
    const drawFrame = () => {
      draw()
      if (typeof video.requestVideoFrameCallback === "function") {
        callbackId = video.requestVideoFrameCallback(drawFrame)
      } else {
        animationFrame = window.requestAnimationFrame(drawFrame)
      }
    }
    video.addEventListener("loadeddata", draw)
    video.addEventListener("seeked", draw)
    drawFrame()
    return () => {
      cancelled = true
      video.removeEventListener("loadeddata", draw)
      video.removeEventListener("seeked", draw)
      if (callbackId) video.cancelVideoFrameCallback(callbackId)
      if (animationFrame) window.cancelAnimationFrame(animationFrame)
    }
  }, [canvasVideoPreview, currentVideoClip?.clipId, currentVideoItem, currentVisualTransform, playbackResolution, sequenceSpec.settings.height, sequenceSpec.settings.width])

  useEffect(() => {
    const video = transitionVideoRef.current
    const canvas = transitionProgramCanvasRef.current
    if (!video || !canvas || transitionVideoItem?.type !== "video" || (playbackResolution === "full" && !canvasVideoPreview)) return
    const factor = playbackResolution === "full" ? 1 : playbackResolution === "half" ? 0.5 : 0.25
    const width = Math.max(16, Math.round(sequenceSpec.settings.width * factor))
    const height = Math.max(16, Math.round(sequenceSpec.settings.height * factor))
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext("2d", { alpha: true })
    if (!context) return
    let callbackId = 0
    let animationFrame = 0
    let cancelled = false
    let renderedFrames = 0
    const draw = () => {
      if (cancelled || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return
      drawProgramFrame(context, video, width, height, transitionVisualTransform)
      renderedFrames += 1
      canvas.dataset.renderedFrames = String(renderedFrames)
    }
    const drawFrame = () => {
      draw()
      if (typeof video.requestVideoFrameCallback === "function") {
        callbackId = video.requestVideoFrameCallback(drawFrame)
      } else {
        animationFrame = window.requestAnimationFrame(drawFrame)
      }
    }
    video.addEventListener("loadeddata", draw)
    video.addEventListener("seeked", draw)
    drawFrame()
    return () => {
      cancelled = true
      video.removeEventListener("loadeddata", draw)
      video.removeEventListener("seeked", draw)
      if (callbackId) video.cancelVideoFrameCallback(callbackId)
      if (animationFrame) window.cancelAnimationFrame(animationFrame)
    }
  }, [canvasVideoPreview, playbackResolution, sequenceSpec.settings.height, sequenceSpec.settings.width, transitionVideoClip?.clipId, transitionVideoItem, transitionVisualTransform])

  const timeToFrame = useCallback((time: number) => (
    Math.max(0, Math.round(time * framesPerSecond))
  ), [framesPerSecond])
  const frameToTime = useCallback((frame: number) => Math.max(0, Math.round(frame)) / framesPerSecond, [framesPerSecond])

  const seekTo = useCallback((time: number) => {
    const nextTime = clamp(frameToTime(timeToFrame(time)), 0, timelineDuration)
    currentTimeRef.current = nextTime
    setCurrentTime(nextTime)
  }, [frameToTime, timeToFrame, timelineDuration])

  const setProgramLoopIn = useCallback(() => {
    if (sequenceEndFrame <= 1) return
    const inFrame = Math.round(clamp(timeToFrame(currentTimeRef.current), 0, sequenceEndFrame - 1))
    setLoopRange((current) => ({
      inFrame,
      outFrame: current.outFrame > inFrame ? current.outFrame : Math.min(sequenceEndFrame, inFrame + 1),
    }))
  }, [sequenceEndFrame, timeToFrame])

  const setProgramLoopOut = useCallback(() => {
    if (sequenceEndFrame <= 1) return
    const requested = timeToFrame(currentTimeRef.current) + 1
    setLoopRange((current) => ({
      inFrame: Math.min(current.inFrame, sequenceEndFrame - 1),
      outFrame: Math.round(clamp(requested, current.inFrame + 1, sequenceEndFrame)),
    }))
  }, [sequenceEndFrame, timeToFrame])

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
    event.stopPropagation()
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
      transitionVideoRef.current?.pause()
      transitionOutgoingAudioRef.current?.pause()
      transitionIncomingAudioRef.current?.pause()
      setPlaying(false)
      return
    }
    void ensurePreviewAudioGraph()
    setPlaybackDirection(1)
    const nextStart = playbackEnd > 0 && currentTime >= playbackEnd - 0.02 ? 0 : currentTime
    if (playbackEnd <= 0 || nextStart >= playbackEnd) return
    if (nextStart !== currentTime) {
      currentTimeRef.current = nextStart
      setCurrentTime(nextStart)
    }
    setPlaying(true)
  }, [currentTime, ensurePreviewAudioGraph, playbackEnd, playing])

  const shuttlePlayback = useCallback((direction: 1 | -1) => {
    const nextStart = direction > 0 && playbackEnd > 0 && currentTimeRef.current >= playbackEnd - 0.02
      ? 0
      : currentTimeRef.current
    if ((direction < 0 && nextStart <= 0) || playbackEnd <= 0) {
      setPlaying(false)
      return
    }
    if (nextStart !== currentTimeRef.current) {
      currentTimeRef.current = nextStart
      setCurrentTime(nextStart)
    }
    void ensurePreviewAudioGraph()
    setPlaybackDirection(direction)
    setPlaying(true)
  }, [ensurePreviewAudioGraph, playbackEnd])

  const activeTransitionAudioClockClip = activeAudioTransition?.outgoing
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
      if (playbackDirection < 0) {
        timelineTime += deltaSeconds * playbackDirection
      } else {
        let sampledMediaClock = false
        if (now >= suppressMediaClockUntilRef.current && playbackClockSource === "audio-transition" && activeTransitionAudioClockClip) {
          const audio = transitionOutgoingAudioRef.current
          if (audio && !audio.paused && audio.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
            const sampledTimelineTime = activeTransitionAudioClockClip.startFrame / framesPerSecond + (
              audio.currentTime - activeTransitionAudioClockClip.sourceInFrame / framesPerSecond
            )
            timelineTime = Math.max(timelineTime, sampledTimelineTime)
            sampledMediaClock = true
          }
        } else if (now >= suppressMediaClockUntilRef.current && playbackClockSource === "audio" && currentAudioClip) {
          const audio = audioRef.current
          if (audio && !audio.paused && audio.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
            const sampledTimelineTime = currentAudioClip.startFrame / framesPerSecond + Math.max(
              0,
              audio.currentTime - currentAudioClip.sourceInFrame / framesPerSecond,
            )
            timelineTime = Math.max(timelineTime, sampledTimelineTime)
            sampledMediaClock = true
          }
        } else if (now >= suppressMediaClockUntilRef.current && playbackClockSource === "video-pts" && currentVideoClip) {
          const video = videoRef.current
          if (video && !video.paused && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
            const decoded = decodedVideoClockRef.current
            const mediaTime = decoded && now - decoded.observedAt < 250 ? decoded.mediaTime : video.currentTime
            const sampledTimelineTime = currentVideoClip.startFrame / framesPerSecond + Math.max(
              0,
              mediaTime - currentVideoClip.sourceInFrame / framesPerSecond,
            )
            timelineTime = Math.max(timelineTime, sampledTimelineTime)
            sampledMediaClock = true
          }
        }
        if (!sampledMediaClock) timelineTime += deltaSeconds
      }
      if (
        playbackDirection > 0 &&
        loopEnabled &&
        effectiveLoopOutFrame > loopRange.inFrame &&
        timelineTime >= effectiveLoopOutFrame / framesPerSecond - 0.5 / framesPerSecond
      ) {
        const loopInTime = loopRange.inFrame / framesPerSecond
        const video = videoRef.current
        const audio = audioRef.current
        timelineTime = loopInTime
        decodedVideoClockRef.current = null
        if (
          video &&
          currentVideoClip &&
          loopRange.inFrame >= currentVideoClip.startFrame &&
          loopRange.inFrame < clipEndFrame(currentVideoClip)
        ) {
          video.currentTime = currentVideoClip.sourceInFrame / framesPerSecond + loopInTime - currentVideoClip.startFrame / framesPerSecond
          void video.play().catch(() => undefined)
        } else {
          video?.pause()
        }
        if (
          audio &&
          currentAudioClip &&
          loopRange.inFrame >= currentAudioClip.startFrame &&
          loopRange.inFrame < clipEndFrame(currentAudioClip) &&
          !playAudioThroughVideo
        ) {
          audio.currentTime = currentAudioClip.sourceInFrame / framesPerSecond + loopInTime - currentAudioClip.startFrame / framesPerSecond
          void audio.play().catch(() => undefined)
        } else {
          audio?.pause()
        }
        suppressMediaClockUntilRef.current = now + 120
        currentTimeRef.current = timelineTime
        if (playheadRef.current) playheadRef.current.style.left = `${TRACK_LABEL_WIDTH + timelineTime * pxPerSecond}px`
        if (timelineRef.current) timelineRef.current.dataset.currentFrame = String(Math.round(timelineTime * framesPerSecond))
        setCurrentTime(timelineTime)
        frame = window.requestAnimationFrame(tick)
        return
      }
      if (playbackDirection > 0 && timelineTime >= playbackEnd - 0.015) {
        videoRef.current?.pause()
        audioRef.current?.pause()
        transitionVideoRef.current?.pause()
        transitionOutgoingAudioRef.current?.pause()
        transitionIncomingAudioRef.current?.pause()
        currentTimeRef.current = playbackEnd
        if (playheadRef.current) playheadRef.current.style.left = `${TRACK_LABEL_WIDTH + playbackEnd * pxPerSecond}px`
        if (timelineRef.current) timelineRef.current.dataset.currentFrame = String(Math.round(playbackEnd * framesPerSecond))
        setCurrentTime(playbackEnd)
        setPlaying(false)
        return
      }
      if (playbackDirection < 0 && timelineTime <= 0) {
        videoRef.current?.pause()
        audioRef.current?.pause()
        transitionVideoRef.current?.pause()
        transitionOutgoingAudioRef.current?.pause()
        transitionIncomingAudioRef.current?.pause()
        currentTimeRef.current = 0
        if (playheadRef.current) playheadRef.current.style.left = `${TRACK_LABEL_WIDTH}px`
        if (timelineRef.current) timelineRef.current.dataset.currentFrame = "0"
        setCurrentTime(0)
        setPlaying(false)
        return
      }
      currentTimeRef.current = timelineTime
      if (playheadRef.current) playheadRef.current.style.left = `${TRACK_LABEL_WIDTH + timelineTime * pxPerSecond}px`
      if (timelineRef.current) timelineRef.current.dataset.currentFrame = String(Math.round(timelineTime * framesPerSecond))
      if (now - lastUiCommit >= PLAYBACK_UI_FRAME_MS) {
        lastUiCommit = now
        setCurrentTime(timelineTime)
      }
      frame = window.requestAnimationFrame(tick)
    }
    frame = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame)
  }, [activeTransitionAudioClockClip, currentAudioClip, currentVideoClip, effectiveLoopOutFrame, framesPerSecond, loopEnabled, loopRange.inFrame, playAudioThroughVideo, playbackClockSource, playbackDirection, playbackEnd, playing, pxPerSecond])

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

  const renderSequence = async () => {
    if (busy || !sequenceLoaded || sequenceEndFrame <= 0) return
    setBusy("render")
    setError(null)
    setRenderNotice(null)
    try {
      const revision = await persistSequenceNow()
      const result = await renderVideoEditorSequence(
        projectId,
        nodeId,
        revision,
        `${title || "视频"} · 时间线成片`,
      )
      await monitorRenderJob(result)
    } catch (err) {
      setError(err instanceof Error ? err.message : "时间线导出失败")
      setBusy(null)
    }
  }

  const cancelSequenceRender = async () => {
    if (!renderJob || !["queued", "running"].includes(renderJob.status)) return
    setError(null)
    setRenderJob((current) => current ? { ...current, status: "cancelling", phase: "正在取消" } : current)
    try {
      const cancelled = await cancelVideoEditorSequenceRender(projectId, nodeId, renderJob.id)
      setRenderJob(cancelled)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消导出失败")
    }
  }

  const snapFrameToBoundaries = useCallback((
    valueFrame: number,
    excludeClipIds?: Iterable<string>,
    showGuide = true,
  ): number => {
    if (!snappingEnabled) {
      if (showGuide) setSnapGuideFrame(null)
      return Math.max(0, Math.round(valueFrame))
    }
    const excluded = new Set(excludeClipIds || [])
    const thresholdFrames = Math.max(1, Math.ceil(SNAP_PIXELS / pxPerSecond * framesPerSecond))
    const clipTargets = [...videoClips, ...audioClips]
      .filter((clip) => !excluded.has(clip.clipId))
      .flatMap((clip) => [clip.startFrame, clipEndFrame(clip)])
    const targets = [
      0,
      timeToFrame(currentTimeRef.current),
      Math.max(0, ...videoClips.map(clipEndFrame), ...audioClips.map(clipEndFrame)),
      ...markers.map((marker) => marker.frame),
      ...clipTargets,
    ]
    const closest = targets.reduce<{ value: number; distance: number } | null>((best, target) => {
      const distance = Math.abs(valueFrame - target)
      if (distance > thresholdFrames) return best
      if (!best || distance < best.distance) return { value: target, distance }
      return best
    }, null)
    if (showGuide) setSnapGuideFrame(closest?.value ?? null)
    return Math.max(0, Math.round(closest ? closest.value : valueFrame))
  }, [audioClips, framesPerSecond, markers, pxPerSecond, snappingEnabled, timeToFrame, videoClips])

  const snapClipStartFrame = useCallback((clip: TimelineClipState, startFrame: number, excludeClipIds?: Iterable<string>): number => {
    const rawStartFrame = Math.max(0, Math.round(startFrame))
    const excluded = new Set(excludeClipIds || [clip.clipId])
    excluded.add(clip.clipId)
    const snappedStartFrame = snapFrameToBoundaries(rawStartFrame, excluded, false)
    const snappedEndFrame = snapFrameToBoundaries(rawStartFrame + clip.durationFrames, excluded, false)
    const snappedEndStartFrame = snappedEndFrame - clip.durationFrames
    const startDistance = Math.abs(snappedStartFrame - rawStartFrame)
    const endDistance = Math.abs(snappedEndStartFrame - rawStartFrame)
    const startSnapped = snappedStartFrame !== rawStartFrame
    const endSnapped = snappedEndFrame !== rawStartFrame + clip.durationFrames
    const useEnd = endSnapped && (!startSnapped || endDistance < startDistance)
    const result = Math.max(0, Math.round(
      useEnd ? snappedEndStartFrame : startSnapped ? snappedStartFrame : rawStartFrame,
    ))
    setSnapGuideFrame(startSnapped || endSnapped ? (useEnd ? snappedEndFrame : snappedStartFrame) : null)
    return result
  }, [snapFrameToBoundaries])

  const updateTrack = useCallback((trackId: string, patch: Partial<TimelineTrackState>) => {
    setTracks((current) => current.map((track) => track.id === trackId ? { ...track, ...patch } : track))
  }, [])

  const addSequenceMarker = useCallback(() => {
    const frame = timeToFrame(currentTimeRef.current)
    const existing = markers.find((marker) => marker.frame === frame)
    if (existing) {
      clearTimelineSelection()
      setSelectedMarkerId(existing.id)
      return
    }
    recordUndoSnapshot()
    const marker: TimelineMarkerState = {
      id: createMarkerId(),
      frame,
      label: `M${markers.length + 1}`,
    }
    setMarkers((current) => [...current, marker].sort((left, right) => left.frame - right.frame))
    setSelectedMarkerId(marker.id)
    setSelectedClipId(null)
    selectedClipIdsRef.current = new Set()
    setSelectedClipIds(new Set())
  }, [clearTimelineSelection, markers, recordUndoSnapshot, timeToFrame])

  const deleteSelectedMarker = useCallback(() => {
    if (!selectedMarkerId) return
    recordUndoSnapshot()
    setMarkers((current) => current.filter((marker) => marker.id !== selectedMarkerId))
    setSelectedMarkerId(null)
  }, [recordUndoSnapshot, selectedMarkerId])

  const beginTrackResize = useCallback((trackId: string, event: ReactPointerEvent<HTMLButtonElement>) => {
    const track = trackById.get(trackId)
    if (!track) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.focus({ preventScroll: true })
    recordUndoSnapshot()
    const startY = event.clientY
    const initialHeight = track.height
    const onMove = (moveEvent: PointerEvent) => {
      const height = Math.round(clamp(initialHeight + moveEvent.clientY - startY, MIN_TRACK_HEIGHT, MAX_TRACK_HEIGHT))
      updateTrack(trackId, { height })
    }
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }, [recordUndoSnapshot, trackById, updateTrack])

  const addTrack = useCallback((kind: "video" | "audio") => {
    recordUndoSnapshot()
    const prefix = kind === "video" ? "v" : "a"
    const sameKind = tracks.filter((track) => track.kind === kind)
    const nextNumber = Math.max(0, ...sameKind.map((track) => Number(track.id.match(/\d+$/)?.[0] || 0))) + 1
    const nextTrack: TimelineTrackState = {
      id: `${prefix}${nextNumber}`,
      kind,
      name: `${kind === "video" ? "视频" : "音频"} ${nextNumber}`,
      order: Math.max(-1, ...sameKind.map((track) => track.order)) + 1,
      locked: false,
      syncLocked: true,
      visible: true,
      muted: false,
      solo: false,
      gainDb: 0,
      height: DEFAULT_TRACK_HEIGHT,
    }
    setTracks((current) => [...current, nextTrack])
    if (kind === "video") setActiveVideoTrackId(nextTrack.id)
    else setActiveAudioTrackId(nextTrack.id)
  }, [recordUndoSnapshot, tracks])

  const deleteTrack = useCallback((trackId: string) => {
    const track = trackById.get(trackId)
    if (!track) return
    if (tracks.filter((candidate) => candidate.kind === track.kind).length <= 1) {
      setError("每种轨道至少保留一条")
      return
    }
    if ([...videoClips, ...audioClips].some((clip) => clip.trackId === trackId)) {
      setError("轨道中仍有片段，请先移动或删除片段")
      return
    }
    recordUndoSnapshot()
    const nextTracks = tracks
      .filter((candidate) => candidate.id !== trackId)
      .map((candidate) => candidate.kind === track.kind && candidate.order > track.order
        ? { ...candidate, order: candidate.order - 1 }
        : candidate)
    setTracks(nextTracks)
    if (track.kind === "video" && activeVideoTrackId === trackId) {
      setActiveVideoTrackId(nextTracks.find((candidate) => candidate.kind === "video")?.id || "v1")
    }
    if (track.kind === "audio" && activeAudioTrackId === trackId) {
      setActiveAudioTrackId(nextTracks.find((candidate) => candidate.kind === "audio")?.id || "a1")
    }
    setError(null)
  }, [activeAudioTrackId, activeVideoTrackId, audioClips, recordUndoSnapshot, trackById, tracks, videoClips])

  const reorderTrack = useCallback((trackId: string, direction: -1 | 1) => {
    const track = trackById.get(trackId)
    if (!track) return
    const sameKind = tracks.filter((candidate) => candidate.kind === track.kind).sort((left, right) => left.order - right.order)
    const index = sameKind.findIndex((candidate) => candidate.id === trackId)
    const swap = sameKind[index + direction]
    if (!swap) return
    recordUndoSnapshot()
    setTracks((current) => current.map((candidate) => {
      if (candidate.id === track.id) return { ...candidate, order: swap.order }
      if (candidate.id === swap.id) return { ...candidate, order: track.order }
      return candidate
    }))
  }, [recordUndoSnapshot, trackById, tracks])

  const placeMediaItem = useCallback((
    item: VideoEditPanelMediaNode,
    mode: "insert" | "overwrite",
    startAt?: number,
    explicitTrackId?: string,
  ) => {
    const sourceFrameCount = item.type === "image"
      ? Math.round(DEFAULT_CLIP_SECONDS * framesPerSecond)
      : sourceFrameCountForItem(item) || Math.round(DEFAULT_CLIP_SECONDS * framesPerSecond)
    const mark = sourceMarks[item.id] || { inFrame: 0, outFrame: sourceFrameCount }
    const sourceInFrame = Math.round(clamp(mark.inFrame, 0, sourceFrameCount - 1))
    const sourceOutFrame = Math.round(clamp(mark.outFrame, sourceInFrame + 1, sourceFrameCount))
    const durationFrames = sourceOutFrame - sourceInFrame
    const fullSource = sourceInFrame === 0 && sourceOutFrame === sourceFrameCount
    const startFrame = snapFrameToBoundaries(timeToFrame(startAt ?? currentTimeRef.current))
    const visualTrackId = item.type !== "audio"
      ? (explicitTrackId && trackById.get(explicitTrackId)?.kind === "video" ? explicitTrackId : activeVideoTrackId)
      : undefined
    const audioTrackId = item.type === "audio"
      ? (explicitTrackId && trackById.get(explicitTrackId)?.kind === "audio" ? explicitTrackId : activeAudioTrackId)
      : activeAudioTrackId
    const linkedAudio = item.type === "video" ? audioItemForVideo(item) : undefined
    const targetTrackIds = new Set<string>([
      ...(visualTrackId ? [visualTrackId] : []),
      ...((item.type === "audio" || linkedAudio) && audioTrackId ? [audioTrackId] : []),
    ])
    const unavailable = [...targetTrackIds].find((trackId) => !trackById.has(trackId) || trackById.get(trackId)?.locked)
    if (unavailable) {
      setError(`目标轨道 ${unavailable.toUpperCase()} 已锁定或不存在`)
      return
    }
    recordUndoSnapshot()
    const rightGroupIds = new Map<string, string>()
    if (mode === "insert") {
      const affectedTrackIds = new Set(tracks
        .filter((track) => track.syncLocked && !track.locked)
        .map((track) => track.id))
      targetTrackIds.forEach((trackId) => affectedTrackIds.add(trackId))
      setVideoClips((current) => insertGapIntoClips(current, startFrame, durationFrames, affectedTrackIds, rightGroupIds))
      setAudioClips((current) => insertGapIntoClips(current, startFrame, durationFrames, affectedTrackIds, rightGroupIds))
    } else {
      setVideoClips((current) => overwriteClipsInRange(current, startFrame, durationFrames, targetTrackIds, rightGroupIds))
      setAudioClips((current) => overwriteClipsInRange(current, startFrame, durationFrames, targetTrackIds, rightGroupIds))
    }
    if (item.type === "video") {
      const syncGroupId = createSyncGroupId(mediaSourceKey(item))
      const clip = createClip(item.id, visualTrackId || activeVideoTrackId, startFrame, durationFrames, sourceInFrame, syncGroupId, fullSource)
      setVideoClips((current) => [...current, clip])
      const selectedIds = new Set([clip.clipId])
      if (linkedAudio) {
        const linkedAudioClip = createClip(linkedAudio.id, audioTrackId, startFrame, durationFrames, sourceInFrame, syncGroupId, fullSource)
        setAudioClips((current) => [...current, linkedAudioClip])
        selectedIds.add(linkedAudioClip.clipId)
      }
      setSelectedClipId(clip.clipId)
      setSelectedClipIds(selectedIds)
      setSnapGuideFrame(null)
      setError(null)
      return
    }
    const clip = createClip(
      item.id,
      item.type === "image" ? (visualTrackId || activeVideoTrackId) : audioTrackId,
      startFrame,
      durationFrames,
      sourceInFrame,
      undefined,
      fullSource,
    )
    if (item.type === "image") {
      setVideoClips((current) => [...current, clip])
      setSelectedClipId(clip.clipId)
      setSelectedClipIds(new Set([clip.clipId]))
      setSnapGuideFrame(null)
      setError(null)
      return
    }
    setAudioClips((current) => [...current, clip])
    setSelectedClipId(clip.clipId)
    setSelectedClipIds(new Set([clip.clipId]))
    setSnapGuideFrame(null)
    setError(null)
  }, [activeAudioTrackId, activeVideoTrackId, audioItemForVideo, framesPerSecond, recordUndoSnapshot, snapFrameToBoundaries, sourceFrameCountForItem, sourceMarks, timeToFrame, trackById, tracks])

  const insertMediaItem = useCallback((item: VideoEditPanelMediaNode) => {
    placeMediaItem(item, "insert")
  }, [placeMediaItem])

  const overwriteMediaItem = useCallback((item: VideoEditPanelMediaNode) => {
    placeMediaItem(item, "overwrite")
  }, [placeMediaItem])

  const handleTrackDrop = (track: TimelineTrackState, event: ReactDragEvent<HTMLDivElement>) => {
    event.preventDefault()
    event.stopPropagation()
    const id = event.dataTransfer.getData("openreel/media-id")
    const item = mediaById.get(id)
    const container = timelineRef.current
    if (!item || !container) return
    if (track.locked) return
    if (track.kind === "video" && item.type === "audio") return
    if (track.kind === "audio" && item.type !== "audio") return
    placeMediaItem(
      item,
      event.shiftKey ? "insert" : "overwrite",
      timeFromPointer(event, container, pxPerSecond),
      track.id,
    )
  }

  const updateClipStartFrame = useCallback((kind: "video" | "audio", clipId: string, startFrame: number, trackId: string) => {
    const allClips = [...videoClips, ...audioClips]
    const original = allClips.find((clip) => clip.clipId === clipId)
    const sourceTrack = original ? trackById.get(original.trackId) : undefined
    const targetTrack = trackById.get(trackId)
    if (!original || original.trackId !== sourceTrack?.id || sourceTrack.locked || !targetTrack || targetTrack.kind !== kind || targetTrack.locked) return
    const activeSelection = selectedClipIdsRef.current
    const moveIds = activeSelection.has(original.clipId)
      ? new Set(activeSelection)
      : new Set(allClips
          .filter((clip) => clip.clipId === original.clipId || clipsShareTimelineRange(clip, original))
          .map((clip) => clip.clipId))
    const moving = allClips.filter((clip) => moveIds.has(clip.clipId))
    if (moving.some((clip) => trackById.get(clip.trackId)?.locked)) return
    const baselines = new Map(moving.map((clip) => [clip.clipId, clip]))
    const minimumDelta = -Math.min(...moving.map((clip) => clip.startFrame))
    const boundedStartFrame = original.startFrame + Math.max(minimumDelta, Math.round(startFrame) - original.startFrame)
    const nextStartFrame = snapClipStartFrame(original, boundedStartFrame, moveIds)
    const deltaFrames = Math.max(minimumDelta, nextStartFrame - original.startFrame)
    const applyMove = (clip: TimelineClipState): TimelineClipState => {
      const baseline = baselines.get(clip.clipId)
      return baseline
        ? {
            ...clip,
            startFrame: baseline.startFrame + deltaFrames,
            trackId: clip.clipId === original.clipId ? trackId : baseline.trackId,
          }
        : clip
    }
    setVideoClips((clips) => clips.map(applyMove))
    setAudioClips((clips) => clips.map(applyMove))
  }, [audioClips, snapClipStartFrame, trackById, videoClips])

  const resizeClipEdge = useCallback((
    kind: "video" | "audio",
    clipId: string,
    edge: "start" | "end",
    edgeFrame: number,
  ) => {
    const primaryClips = kind === "video" ? videoClips : audioClips
    const allClips = [...videoClips, ...audioClips]
    const original = primaryClips.find((clip) => clip.clipId === clipId)
    if (!original || trackById.get(original.trackId)?.locked) return
    const linkedGroup = (clip: TimelineClipState) => {
      const activeSelection = selectedClipIdsRef.current
      const respectSelection = activeSelection.has(clip.clipId)
      return allClips.filter((candidate) => (
        candidate.clipId === clip.clipId || (
          clipsShareTimelineRange(candidate, clip) &&
          (!respectSelection || activeSelection.has(candidate.clipId))
        )
      ))
    }
    const group = linkedGroup(original)
    if (group.some((clip) => trackById.get(clip.trackId)?.locked)) return
    const groupIds = new Set(group.map((clip) => clip.clipId))
    const snappedEdgeFrame = snapFrameToBoundaries(edgeFrame, groupIds)
    const clampFades = (clip: TimelineClipState, durationFrames: number): TimelineClipState => {
      const fadeInFrames = Math.min(clip.fadeInFrames || 0, durationFrames)
      const fadeOutFrames = Math.min(clip.fadeOutFrames || 0, durationFrames - fadeInFrames)
      return { ...clip, durationFrames, fadeInFrames, fadeOutFrames, fullSource: false }
    }
    const sourceEndBoundary = (clip: TimelineClipState) => {
      const item = mediaById.get(clip.mediaId)
      if (item?.type === "image") return Number.POSITIVE_INFINITY
      const sourceFrameCount = sourceFrameCountForClip(clip) || clip.sourceInFrame + clip.durationFrames
      return clip.startFrame + Math.max(1, sourceFrameCount - clip.sourceInFrame)
    }
    const applyUpdates = (updates: Map<string, TimelineClipState>) => {
      setVideoClips((clips) => clips.map((clip) => updates.get(clip.clipId) || clip))
      setAudioClips((clips) => clips.map((clip) => updates.get(clip.clipId) || clip))
    }

    if (trimMode === "rolling") {
      const ordered = primaryClips
        .filter((clip) => clip.trackId === original.trackId)
        .sort((left, right) => left.startFrame - right.startFrame)
      const leftPrimary = edge === "end"
        ? original
        : ordered.find((clip) => clip.clipId !== original.clipId && clipEndFrame(clip) === original.startFrame)
      const rightPrimary = edge === "start"
        ? original
        : ordered.find((clip) => clip.clipId !== original.clipId && clip.startFrame === clipEndFrame(original))
      if (!leftPrimary || !rightPrimary) return
      const leftGroup = linkedGroup(leftPrimary)
      const rightGroup = linkedGroup(rightPrimary)
      const boundaryFrame = edge === "end" ? clipEndFrame(original) : original.startFrame
      const minimumBoundary = Math.max(
        ...leftGroup.map((clip) => clip.startFrame + 1),
        ...rightGroup.map((clip) => clip.startFrame - clip.sourceInFrame),
      )
      const maximumBoundary = Math.min(
        ...leftGroup.map(sourceEndBoundary),
        ...rightGroup.map((clip) => clipEndFrame(clip) - 1),
      )
      const nextBoundary = Math.round(clamp(edgeFrame, minimumBoundary, maximumBoundary))
      const deltaFrames = nextBoundary - boundaryFrame
      const updates = new Map<string, TimelineClipState>()
      for (const clip of leftGroup) {
        updates.set(clip.clipId, clampFades(clip, clip.durationFrames + deltaFrames))
      }
      for (const clip of rightGroup) {
        updates.set(clip.clipId, clampFades({
          ...clip,
          startFrame: clip.startFrame + deltaFrames,
          sourceInFrame: clip.sourceInFrame + deltaFrames,
        }, clip.durationFrames - deltaFrames))
      }
      applyUpdates(updates)
      return
    }

    if (trimMode === "ripple") {
      const oldEndFrame = clipEndFrame(original)
      let timelineDeltaFrames = 0
      const updates = new Map<string, TimelineClipState>()
      if (edge === "start") {
        const requestedDelta = snappedEdgeFrame - original.startFrame
        const minimumDelta = Math.max(...group.map((clip) => -clip.sourceInFrame))
        const maximumDelta = Math.min(...group.map((clip) => clip.durationFrames - 1))
        const sourceDeltaFrames = Math.round(clamp(requestedDelta, minimumDelta, maximumDelta))
        timelineDeltaFrames = -sourceDeltaFrames
        for (const clip of group) {
          updates.set(clip.clipId, clampFades({
            ...clip,
            sourceInFrame: clip.sourceInFrame + sourceDeltaFrames,
          }, clip.durationFrames - sourceDeltaFrames))
        }
      } else {
        const minimumEndFrame = Math.max(...group.map((clip) => clip.startFrame + 1))
        const maximumEndFrame = Math.min(...group.map(sourceEndBoundary))
        const nextEndFrame = Math.round(clamp(snappedEdgeFrame, minimumEndFrame, maximumEndFrame))
        timelineDeltaFrames = nextEndFrame - oldEndFrame
        for (const clip of group) {
          updates.set(clip.clipId, clampFades(clip, clip.durationFrames + timelineDeltaFrames))
        }
      }
      for (const clip of allClips) {
        const track = trackById.get(clip.trackId)
        if (!groupIds.has(clip.clipId) && clip.startFrame >= oldEndFrame && track?.syncLocked && !track.locked) {
          updates.set(clip.clipId, { ...clip, startFrame: Math.max(0, clip.startFrame + timelineDeltaFrames) })
        }
      }
      applyUpdates(updates)
      return
    }

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

    const updates = new Map<string, TimelineClipState>()
    for (const clip of group) {
      updates.set(clip.clipId, clampFades({
        ...clip,
        startFrame: nextStartFrame,
        sourceInFrame: nextSourceInFrame,
      }, nextDurationFrames))
    }
    applyUpdates(updates)
  }, [audioClips, mediaById, snapFrameToBoundaries, sourceFrameCountForClip, trackById, trimMode, videoClips])

  const updateSelectedClipFrameValue = useCallback((
    field: "startFrame" | "sourceInFrame" | "sourceOutFrame" | "durationFrames",
    rawValue: number,
  ) => {
    if (!selectedTimelineClip || !Number.isFinite(rawValue)) return
    if (trackById.get(selectedTimelineClip.trackId)?.locked) return
    const allClips = [...videoClips, ...audioClips]
    const group = allClips.filter((clip) => (
      clip.clipId === selectedTimelineClip.clipId || (
        clipsShareTimelineRange(clip, selectedTimelineClip) && selectedClipIds.has(clip.clipId)
      )
    ))
    const groupIds = new Set(group.map((clip) => clip.clipId))
    const value = Math.round(rawValue)
    let nextStartFrame = selectedTimelineClip.startFrame
    let nextSourceInFrame = selectedTimelineClip.sourceInFrame
    let nextDurationFrames = selectedTimelineClip.durationFrames
    if (field === "startFrame") {
      nextStartFrame = Math.max(0, value)
    } else if (field === "sourceInFrame") {
      const maximumSourceIn = Math.min(...group.map((clip) => {
        const sourceFrameCount = sourceFrameCountForClip(clip)
        return sourceFrameCount ? Math.max(0, sourceFrameCount - clip.durationFrames) : Number.MAX_SAFE_INTEGER
      }))
      nextSourceInFrame = Math.round(clamp(value, 0, maximumSourceIn))
    } else if (field === "durationFrames") {
      const maximumDuration = Math.min(...group.map((clip) => {
        const sourceFrameCount = sourceFrameCountForClip(clip)
        return sourceFrameCount ? Math.max(1, sourceFrameCount - clip.sourceInFrame) : Number.MAX_SAFE_INTEGER
      }))
      nextDurationFrames = Math.round(clamp(value, 1, maximumDuration))
    } else {
      const maximumSourceOut = Math.min(...group.map((clip) => (
        sourceFrameCountForClip(clip) || Number.MAX_SAFE_INTEGER
      )))
      const nextSourceOutFrame = Math.round(clamp(value, nextSourceInFrame + 1, maximumSourceOut))
      nextDurationFrames = nextSourceOutFrame - nextSourceInFrame
    }
    const applyValue = (clip: TimelineClipState): TimelineClipState => {
      const fadeInFrames = Math.min(clip.fadeInFrames || 0, nextDurationFrames)
      const fadeOutFrames = Math.min(clip.fadeOutFrames || 0, nextDurationFrames - fadeInFrames)
      return {
        ...clip,
        startFrame: nextStartFrame,
        sourceInFrame: nextSourceInFrame,
        durationFrames: nextDurationFrames,
        fadeInFrames,
        fadeOutFrames,
        fullSource: false,
      }
    }
    setVideoClips((clips) => clips.map((clip) => groupIds.has(clip.clipId) ? applyValue(clip) : clip))
    setAudioClips((clips) => clips.map((clip) => groupIds.has(clip.clipId) ? applyValue(clip) : clip))
  }, [audioClips, selectedClipIds, selectedTimelineClip, sourceFrameCountForClip, trackById, videoClips])

  const updateAudioClipGain = useCallback((clipId: string, rawGainDb: number) => {
    const gainDb = Math.round(clamp(rawGainDb, MIN_CLIP_GAIN_DB, MAX_CLIP_GAIN_DB) * 2) / 2
    setAudioClips((clips) => clips.map((clip) => (
      clip.clipId === clipId ? { ...clip, gainDb } : clip
    )))
  }, [])

  const updateAudioClipFade = useCallback((clipId: string, edge: "in" | "out", rawFrames: number) => {
    setAudioClips((clips) => clips.map((clip) => {
      if (clip.clipId !== clipId) return clip
      if (edge === "in") {
        const fadeInFrames = Math.round(clamp(rawFrames, 0, clip.durationFrames - (clip.fadeOutFrames || 0)))
        return { ...clip, fadeInFrames }
      }
      const fadeOutFrames = Math.round(clamp(rawFrames, 0, clip.durationFrames - (clip.fadeInFrames || 0)))
      return { ...clip, fadeOutFrames }
    }))
  }, [])

  const splitTimelineAtFrame = useCallback((splitFrame: number, targetClipId?: string, independent = false) => {
    const safeSplitFrame = Math.max(0, Math.round(splitFrame))
    const target = targetClipId
      ? [...videoClips, ...audioClips].find((clip) => clip.clipId === targetClipId)
      : undefined
    if (target && trackById.get(target.trackId)?.locked) return
    const targetClipIds = target
      ? new Set([...videoClips, ...audioClips]
          .filter((clip) => (
            (clip.clipId === target.clipId || (!independent && clipsShareTimelineRange(clip, target))) &&
            !trackById.get(clip.trackId)?.locked
          ))
          .map((clip) => clip.clipId))
      : new Set([...videoClips, ...audioClips]
          .filter((clip) => !trackById.get(clip.trackId)?.locked)
          .map((clip) => clip.clipId))
    const rightGroupIds = new Map<string, string>()
    const videoResult = splitClipsAt(videoClips, safeSplitFrame, rightGroupIds, targetClipIds)
    const audioResult = splitClipsAt(audioClips, safeSplitFrame, rightGroupIds, targetClipIds)
    setVideoClips(videoResult.clips)
    setAudioClips(audioResult.clips)
    if (videoResult.selectedRightClipId || audioResult.selectedRightClipId) {
      setSelectedClipId(videoResult.selectedRightClipId || audioResult.selectedRightClipId)
      setSelectedClipIds(new Set([
        ...(videoResult.selectedRightClipId ? [videoResult.selectedRightClipId] : []),
        ...(audioResult.selectedRightClipId ? [audioResult.selectedRightClipId] : []),
      ]))
    }
    seekTo(frameToTime(safeSplitFrame))
  }, [audioClips, frameToTime, seekTo, trackById, videoClips])

  const deleteSelectedClips = useCallback((ripple = false) => {
    if (!selectedTimelineClip) return
    const allClips = [...videoClips, ...audioClips]
    const requestedIds = selectedClipIds.size > 0
      ? selectedClipIds
      : new Set(allClips
          .filter((clip) => clip.clipId === selectedTimelineClip.clipId || clipsShareTimelineRange(clip, selectedTimelineClip))
          .map((clip) => clip.clipId))
    const deleteIds = new Set(allClips
      .filter((clip) => requestedIds.has(clip.clipId) && !trackById.get(clip.trackId)?.locked)
      .map((clip) => clip.clipId))
    const deleted = allClips.filter((clip) => deleteIds.has(clip.clipId))
    if (deleted.length === 0) return
    recordUndoSnapshot()
    const gapStartFrame = Math.min(...deleted.map((clip) => clip.startFrame))
    const gapEndFrame = Math.max(...deleted.map(clipEndFrame))
    const gapDurationFrames = Math.max(0, gapEndFrame - gapStartFrame)
    const applyDeletion = (clips: TimelineClipState[]) => clips
      .filter((clip) => !deleteIds.has(clip.clipId))
      .map((clip) => (
        ripple && clip.startFrame >= gapEndFrame && Boolean(trackById.get(clip.trackId)?.syncLocked) && !trackById.get(clip.trackId)?.locked
          ? { ...clip, startFrame: Math.max(gapStartFrame, clip.startFrame - gapDurationFrames) }
          : clip
      ))
    setVideoClips((clips) => applyDeletion(clips))
    setAudioClips((clips) => applyDeletion(clips))
    setSelectedClipId(null)
    setSelectedClipIds(new Set())
    seekTo(frameToTime(gapStartFrame))
  }, [audioClips, frameToTime, recordUndoSnapshot, seekTo, selectedClipIds, selectedTimelineClip, trackById, videoClips])

  const jumpToEditPoint = useCallback((direction: -1 | 1) => {
    const current = timeToFrame(currentTimeRef.current)
    const points = [...new Set([
      0,
      ...videoClips.flatMap((clip) => [clip.startFrame, clipEndFrame(clip)]),
      ...audioClips.flatMap((clip) => [clip.startFrame, clipEndFrame(clip)]),
      ...markers.map((marker) => marker.frame),
    ])].sort((left, right) => left - right)
    const target = direction > 0
      ? points.find((frame) => frame > current)
      : [...points].reverse().find((frame) => frame < current)
    if (target != null) seekTo(frameToTime(target))
  }, [audioClips, frameToTime, markers, seekTo, timeToFrame, videoClips])

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
        if (selectedMarkerId && selectedClipIdsRef.current.size === 0) deleteSelectedMarker()
        else deleteSelectedClips(event.shiftKey)
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
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault()
        jumpToEditPoint(event.key === "ArrowUp" ? -1 : 1)
        return
      }
      if (!(commandKey || event.altKey) && key === "v") {
        setTool("select")
        setTrimMode("normal")
        return
      }
      if (!(commandKey || event.altKey) && key === "c") {
        setTool("blade")
        return
      }
      if (!(commandKey || event.altKey) && key === "b") {
        event.preventDefault()
        setTool("select")
        setTrimMode("ripple")
        return
      }
      if (!(commandKey || event.altKey) && key === "n") {
        event.preventDefault()
        setTool("select")
        setTrimMode("rolling")
        return
      }
      if (!(commandKey || event.altKey) && key === "s") {
        event.preventDefault()
        setSnappingEnabled((value) => !value)
        setSnapGuideFrame(null)
        return
      }
      if (!(commandKey || event.altKey) && key === "m") {
        event.preventDefault()
        addSequenceMarker()
        return
      }
      if (!(commandKey || event.altKey) && event.key === "[") {
        event.preventDefault()
        setProgramLoopIn()
        return
      }
      if (!(commandKey || event.altKey) && event.key === "]") {
        event.preventDefault()
        setProgramLoopOut()
        return
      }
      if (!(commandKey || event.altKey) && key === "i") {
        event.preventDefault()
        updateSelectedSourceMark({ inFrame: sourceCursorFrame })
        return
      }
      if (!(commandKey || event.altKey) && key === "o") {
        event.preventDefault()
        updateSelectedSourceMark({ outFrame: sourceCursorFrame + 1 })
        return
      }
      if (!(commandKey || event.altKey) && key === "j") {
        event.preventDefault()
        shuttlePlayback(-1)
        return
      }
      if (!(commandKey || event.altKey) && key === "k") {
        event.preventDefault()
        videoRef.current?.pause()
        audioRef.current?.pause()
        transitionVideoRef.current?.pause()
        transitionOutgoingAudioRef.current?.pause()
        transitionIncomingAudioRef.current?.pause()
        setPlaying(false)
        return
      }
      if (!(commandKey || event.altKey) && key === "l") {
        event.preventDefault()
        shuttlePlayback(1)
        return
      }
      if (!(commandKey || event.altKey) && key === "g") {
        event.preventDefault()
        const gain = document.querySelector<HTMLInputElement>('[data-openreel-clip-gain="true"]')
        gain?.focus()
        return
      }
      if (!(commandKey || event.altKey) && event.key === "," && selectedMediaItem) {
        event.preventDefault()
        placeMediaItem(selectedMediaItem, "insert")
        return
      }
      if (!(commandKey || event.altKey) && event.key === "." && selectedMediaItem) {
        event.preventDefault()
        placeMediaItem(selectedMediaItem, "overwrite")
        return
      }
      if (commandKey && key === "k") {
        event.preventDefault()
        recordUndoSnapshot()
        splitTimelineAtFrame(timeToFrame(currentTimeRef.current))
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
  }, [addSequenceMarker, deleteSelectedClips, deleteSelectedMarker, framesPerSecond, jumpToEditPoint, placeMediaItem, pxPerSecond, recordUndoSnapshot, redoEditor, seekTo, selectedMarkerId, selectedMediaItem, setProgramLoopIn, setProgramLoopOut, shuttlePlayback, sourceCursorFrame, splitTimelineAtFrame, timeToFrame, togglePlayback, undoEditor, updateSelectedSourceMark, zoomTimelineAt])

  const handleTimelineBackgroundDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null
    const container = timelineRef.current
    if (!container) return
    if (target?.closest("[data-openreel-playhead]")) return
    const time = timeFromPointer(event, container, pxPerSecond)
    if (target?.closest("[data-openreel-timeline-clip]")) return
    if (target?.closest("[data-openreel-timeline-ruler]")) {
      clearTimelineSelection()
      beginPlayheadDrag(event)
      return
    }
    if (tool === "blade") {
      recordUndoSnapshot()
      splitTimelineAtFrame(timeToFrame(time))
      return
    }
    event.preventDefault()
    const rect = container.getBoundingClientRect()
    const startClientX = event.clientX
    const startClientY = event.clientY
    const startLeft = event.clientX - rect.left + container.scrollLeft
    const startTop = event.clientY - rect.top + container.scrollTop
    const additive = event.ctrlKey || event.metaKey || event.shiftKey
    const independent = event.altKey
    let moved = false
    const onMove = (moveEvent: PointerEvent) => {
      const deltaX = moveEvent.clientX - startClientX
      const deltaY = moveEvent.clientY - startClientY
      if (!moved && Math.hypot(deltaX, deltaY) < 4) return
      moved = true
      const currentLeft = moveEvent.clientX - rect.left + container.scrollLeft
      const currentTop = moveEvent.clientY - rect.top + container.scrollTop
      setMarquee({
        left: Math.min(startLeft, currentLeft),
        top: Math.min(startTop, currentTop),
        width: Math.abs(currentLeft - startLeft),
        height: Math.abs(currentTop - startTop),
      })
    }
    const onEnd = (upEvent: PointerEvent) => {
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onEnd)
      setMarquee(null)
      if (!moved) {
        clearTimelineSelection()
        seekTo(time)
        return
      }
      const selectionRect = {
        left: Math.min(startClientX, upEvent.clientX),
        right: Math.max(startClientX, upEvent.clientX),
        top: Math.min(startClientY, upEvent.clientY),
        bottom: Math.max(startClientY, upEvent.clientY),
      }
      const hitIds = new Set(Array.from(container.querySelectorAll<HTMLElement>("[data-openreel-timeline-clip]"))
        .filter((element) => {
          const box = element.getBoundingClientRect()
          return box.right >= selectionRect.left && box.left <= selectionRect.right &&
            box.bottom >= selectionRect.top && box.top <= selectionRect.bottom
        })
        .map((element) => element.dataset.clipId || "")
        .filter(Boolean))
      const allClips = [...videoClips, ...audioClips]
      if (!independent) {
        for (const clip of allClips) {
          if (allClips.some((hit) => hitIds.has(hit.clipId) && clipsShareTimelineRange(hit, clip))) {
            hitIds.add(clip.clipId)
          }
        }
      }
      setSelectedClipIds((current) => additive ? new Set([...current, ...hitIds]) : hitIds)
      setSelectedClipId([...hitIds][0] || null)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onEnd)
  }

  const canTrim = Boolean(selectedVideoClip && selectedVideoItem?.type === "video" && selectedVideoClip.durationFrames > 1)
  const selectedSourceDuration = selectedVideoClip ? sourceDurationForClip(selectedVideoClip) : null
  const videoConcatIds = videoClips.map((clip) => clip.mediaId).filter((id) => videoItems.some((item) => item.id === id))
  const audioConcatIds = audioClips.map((clip) => clip.mediaId).filter((id) => audioItems.some((item) => item.id === id))
  const isBusy = Boolean(busy)
  const previewScaleStyle = previewScale === "fit"
    ? { height: "100%", width: "auto", maxWidth: "100%" }
    : { width: `${previewScale}%`, maxWidth: "960px" }
  const visualTransitionProgress = activeVideoTransition?.progress || 0
  const currentVisualIsIncoming = Boolean(
    activeVideoTransition && currentVideoClip?.clipId === activeVideoTransition.incoming.clipId,
  )
  const transitionVisualIsIncoming = Boolean(
    activeVideoTransition && transitionVideoClip?.clipId === activeVideoTransition.incoming.clipId,
  )
  const currentVisualLayerOpacity = activeVideoTransition && !videoTransitionSingleSource && currentVisualIsIncoming
    ? visualTransitionProgress
    : 1
  const transitionVisualLayerOpacity = activeVideoTransition && transitionVisualIsIncoming
    ? visualTransitionProgress
    : 1
  const currentVisualLayerZ = currentVisualIsIncoming ? 20 : 10
  const transitionVisualLayerZ = transitionVisualIsIncoming ? 20 : 10

  const renderTimelineTrack = (track: TimelineTrackState) => {
    const kindClips = track.kind === "video" ? videoClips : audioClips
    const trackClips = kindClips.filter((clip) => clip.trackId === track.id)
    const trackTransitions = transitions.filter((transition) => transition.trackId === track.id)
    const gaps = timelineGaps(trackClips, sequenceEndFrame)
    const isActive = track.kind === "video" ? activeVideoTrackId === track.id : activeAudioTrackId === track.id
    const sameKindCount = tracks.filter((candidate) => candidate.kind === track.kind).length
    const canDelete = sameKindCount > 1 && trackClips.length === 0
    const activateTrack = () => {
      if (track.kind === "video") setActiveVideoTrackId(track.id)
      else setActiveAudioTrackId(track.id)
    }
    const toggleTrack = (patch: Partial<TimelineTrackState>) => {
      recordUndoSnapshot()
      updateTrack(track.id, patch)
    }
    const controlClass = (active = false) => cn(
      "flex h-4 min-w-4 items-center justify-center rounded-[2px] border px-0.5 text-[7px] font-semibold",
      active
        ? "border-[#5d91b8] bg-[#315f83] text-[#e4f4ff]"
        : "border-[#3b4148] bg-[#25282d] text-[#7b8189] hover:text-white",
    )
    return (
      <div
        key={track.id}
        data-openreel-track-row="true"
        data-openreel-track-id={track.id}
        data-openreel-track-kind={track.kind}
        data-track-locked={track.locked ? "true" : "false"}
        data-track-sync-locked={track.syncLocked ? "true" : "false"}
        data-track-visible={track.visible ? "true" : "false"}
        data-track-muted={track.muted ? "true" : "false"}
        data-track-active={isActive ? "true" : "false"}
        data-track-height={track.height}
        className="relative grid border-b border-[#30343a]"
        style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px`, height: track.height }}
      >
        <div className={cn(
          "sticky left-0 z-30 grid grid-cols-[30px_1fr] border-r bg-[#202328]",
          isActive ? "border-[#6c9fc4]" : "border-[#3a3f46]",
        )}>
          <button
            type="button"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={activateTrack}
            className={cn(
              "flex items-center justify-center border-r text-[10px] font-semibold",
              track.kind === "video"
                ? "border-[#3a3f46] bg-[#2b4f68] text-[#d8ecfa]"
                : "border-[#3a3f46] bg-[#28503e] text-[#d8f1e4]",
              isActive && "bg-[#477ca0] text-white",
            )}
            aria-label={`目标轨道 ${track.id.toUpperCase()}`}
            title="设为插入/覆盖目标轨道"
          >
            {track.id.toUpperCase()}
          </button>
          <div className="flex min-w-0 flex-col justify-center gap-1 px-1.5">
            <input
              value={track.name}
              maxLength={40}
              onPointerDown={(event) => event.stopPropagation()}
              onFocus={recordUndoSnapshot}
              onChange={(event) => updateTrack(track.id, { name: event.target.value || track.name })}
              className="h-4 min-w-0 border-0 bg-transparent px-0.5 text-[8px] font-medium text-[#d2d5d9] outline-none focus:bg-[#15171a] focus:text-white"
              aria-label={`重命名轨道 ${track.id.toUpperCase()}`}
            />
            <div className="flex items-center gap-0.5">
              <button type="button" className={controlClass()} onPointerDown={(event) => event.stopPropagation()} onClick={() => reorderTrack(track.id, track.kind === "video" ? 1 : -1)} title="上移轨道" aria-label={`上移轨道 ${track.id.toUpperCase()}`}>↑</button>
              <button type="button" className={controlClass()} onPointerDown={(event) => event.stopPropagation()} onClick={() => reorderTrack(track.id, track.kind === "video" ? -1 : 1)} title="下移轨道" aria-label={`下移轨道 ${track.id.toUpperCase()}`}>↓</button>
              <button type="button" className={controlClass(track.locked)} onPointerDown={(event) => event.stopPropagation()} onClick={() => toggleTrack({ locked: !track.locked })} title={track.locked ? "解锁轨道" : "锁定轨道"} aria-label={`${track.locked ? "解锁" : "锁定"}轨道 ${track.id.toUpperCase()}`}>L</button>
              <button type="button" className={controlClass(track.syncLocked)} onPointerDown={(event) => event.stopPropagation()} onClick={() => toggleTrack({ syncLocked: !track.syncLocked })} title={track.syncLocked ? "关闭同步锁" : "启用同步锁"} aria-label={`${track.syncLocked ? "关闭" : "启用"}同步锁 ${track.id.toUpperCase()}`}>⇄</button>
              {track.kind === "video" ? (
                <button type="button" className={controlClass(track.visible)} onPointerDown={(event) => event.stopPropagation()} onClick={() => toggleTrack({ visible: !track.visible })} title={track.visible ? "隐藏视频轨道" : "显示视频轨道"} aria-label={`${track.visible ? "隐藏" : "显示"}轨道 ${track.id.toUpperCase()}`}>V</button>
              ) : (
                <>
                  <button type="button" className={controlClass(track.solo)} onPointerDown={(event) => event.stopPropagation()} onClick={() => toggleTrack({ solo: !track.solo })} title={track.solo ? "取消独奏" : "独奏音频轨道"} aria-label={`${track.solo ? "取消独奏" : "独奏"}轨道 ${track.id.toUpperCase()}`} data-openreel-track-solo="true">S</button>
                  <button type="button" className={controlClass(track.muted)} onPointerDown={(event) => event.stopPropagation()} onClick={() => toggleTrack({ muted: !track.muted })} title={track.muted ? "取消静音" : "静音音频轨道"} aria-label={`${track.muted ? "取消静音" : "静音"}轨道 ${track.id.toUpperCase()}`}>M</button>
                </>
              )}
              <button type="button" disabled={!canDelete} className={cn(controlClass(), !canDelete && "cursor-not-allowed opacity-30")} onPointerDown={(event) => event.stopPropagation()} onClick={() => deleteTrack(track.id)} title={canDelete ? "删除空轨道" : "只能删除空的非末条轨道"} aria-label={`删除轨道 ${track.id.toUpperCase()}`}>×</button>
            </div>
            {track.kind === "audio" && (
              <div className="flex items-center gap-1 text-[#737983]">
                <input
                  type="range"
                  min="-60"
                  max={MAX_CLIP_GAIN_DB}
                  step="0.5"
                  value={track.gainDb}
                  onPointerDown={(event) => {
                    event.stopPropagation()
                    recordUndoSnapshot()
                  }}
                  onChange={(event) => updateTrack(track.id, { gainDb: Number(event.target.value) })}
                  className="h-1 w-10 min-w-0 accent-[#6fac8d]"
                  aria-label={`音频轨道音量 ${track.id.toUpperCase()}`}
                  data-openreel-track-gain="true"
                />
                <span className="font-mono text-[7px]">{track.gainDb.toFixed(0)}</span>
              </div>
            )}
          </div>
        </div>
        <div
          className={cn(
            "relative [background-image:linear-gradient(90deg,rgba(255,255,255,.025)_1px,transparent_1px)] [background-size:84px_100%]",
            track.locked ? "bg-[#1d1d20]" : "bg-[#17191d]",
          )}
          onPointerDown={activateTrack}
          onDragOver={(event) => {
            if (track.locked) return
            event.preventDefault()
            event.dataTransfer.dropEffect = event.shiftKey ? "copy" : "move"
          }}
          onDrop={(event) => handleTrackDrop(track, event)}
        >
          {gaps.map((gap) => (
            <div
              key={`${track.id}:${gap.startFrame}:${gap.durationFrames}`}
              data-openreel-sequence-gap="true"
              data-gap-kind={track.kind}
              data-gap-start-frame={gap.startFrame}
              data-gap-duration-frames={gap.durationFrames}
              className={cn(
                "pointer-events-none absolute bottom-1.5 top-1.5 overflow-hidden border border-dashed",
                track.kind === "video"
                  ? "border-[#3d4249] bg-[repeating-linear-gradient(135deg,#0d0f12_0,#0d0f12_6px,#14171b_6px,#14171b_12px)]"
                  : "border-[#33433b] bg-[#121816]",
              )}
              style={{
                left: gap.startFrame / framesPerSecond * pxPerSecond,
                width: Math.max(1, gap.durationFrames / framesPerSecond * pxPerSecond),
              }}
            >
              {gap.durationFrames / framesPerSecond * pxPerSecond >= 54 && (
                <span className="absolute inset-0 flex items-center justify-center font-mono text-[7px] tracking-[0.08em] text-[#596069]">
                  {track.kind === "video" ? "BLACK" : "SILENCE"}
                </span>
              )}
            </div>
          ))}
          {trackClips.length === 0 && track.kind === "audio" && (
            <div className="pointer-events-none absolute inset-2 flex items-center justify-center border border-dashed border-[#343940] text-[9px] text-[#5f656d]">
              {track.locked ? "轨道已锁定" : "拖入音频 · Shift 为插入"}
            </div>
          )}
          {trackClips.map((clip) => {
            const item = mediaById.get(clip.mediaId)
            if (!item) return null
            if (track.kind === "video" && item.type !== "video" && item.type !== "image") return null
            if (track.kind === "audio" && item.type !== "audio") return null
            return (
              <TimelineClip
                key={clip.clipId}
                projectId={projectId}
                clip={clip}
                item={item}
                kind={track.kind}
                activeTool={tool}
                trimMode={trimMode}
                pxPerSecond={pxPerSecond}
                sequenceFps={framesPerSecond}
                trackHeight={track.height}
                viewport={timelineViewport}
                sourceDuration={sourceDurationForClip(clip)}
                mediaIndex={track.kind === "video"
                  ? (item.type === "video" ? mediaIndexes[item.id] : null)
                  : mediaIndexes[mediaSourceKey(item)]}
                waveformNodeId={track.kind === "audio" ? mediaSourceKey(item) : undefined}
                trackGainDb={track.gainDb}
                trackMuted={track.muted}
                disabled={track.locked}
                selected={selectedClipIds.has(clip.clipId)}
                onBeginEdit={recordUndoSnapshot}
                onEditEnd={() => setSnapGuideFrame(null)}
                onSelect={(clipId, options) => {
                  activateTrack()
                  selectTimelineClip(clipId, options)
                }}
                onDragStartFrame={updateClipStartFrame}
                onResizeEdge={resizeClipEdge}
                onAudioGainChange={updateAudioClipGain}
                onAudioFadeChange={updateAudioClipFade}
                onCutAtFrame={splitTimelineAtFrame}
              />
            )
          })}
          {trackTransitions.map((transition) => {
            const incoming = trackClips.find((clip) => clip.clipId === transition.incomingClipId)
            if (!incoming) return null
            const range = transitionFrameRange(transition, incoming.startFrame)
            const width = Math.max(12, transition.durationFrames / framesPerSecond * pxPerSecond)
            return (
              <div
                key={transition.id}
                data-openreel-transition="true"
                data-transition-id={transition.id}
                data-transition-kind={transition.kind}
                data-transition-duration-frames={transition.durationFrames}
                data-transition-cut-frame={incoming.startFrame}
                className={cn(
                  "pointer-events-none absolute bottom-1.5 top-1.5 z-20 overflow-hidden border shadow-[0_1px_4px_rgba(0,0,0,.45)]",
                  transition.kind === "video_cross_dissolve"
                    ? "border-[#8fc8ed] bg-[linear-gradient(135deg,rgba(57,113,151,.92)_0%,rgba(105,165,204,.7)_49%,rgba(37,77,105,.94)_50%,rgba(66,130,170,.88)_100%)] text-[#e4f5ff]"
                    : "border-[#82c9a6] bg-[linear-gradient(155deg,rgba(32,92,65,.92)_0%,rgba(93,169,128,.82)_48%,rgba(32,92,65,.92)_100%)] text-[#e3f9ed]",
                )}
                style={{
                  left: range.startFrame / framesPerSecond * pxPerSecond,
                  width,
                }}
              >
                <span className="absolute inset-0 flex items-center justify-center truncate px-1 font-mono text-[6px] font-semibold tracking-[0.08em] drop-shadow">
                  {transition.kind === "video_cross_dissolve" ? "DISSOLVE" : "XFADE"}
                </span>
                <span className="absolute bottom-0 right-0 bg-black/45 px-0.5 font-mono text-[6px]">{transition.durationFrames}f</span>
              </div>
            )
          })}
        </div>
        <button
          type="button"
          data-openreel-track-resize-handle="true"
          onPointerDown={(event) => beginTrackResize(track.id, event)}
          className="absolute inset-x-0 bottom-0 z-40 h-1 cursor-row-resize border-0 bg-transparent after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-[#3b4148] hover:after:h-0.5 hover:after:bg-[#6b9fc3]"
          aria-label={`调整轨道高度 ${track.id.toUpperCase()}`}
          title={`拖动调整 ${track.id.toUpperCase()} 高度 · ${track.height}px`}
        />
      </div>
    )
  }

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

      <div className="grid h-[calc(100%-2rem)] w-full min-w-0 grid-rows-[minmax(360px,70%)_minmax(260px,30%)] bg-[#111316]">
        <div className="grid min-h-0 w-full min-w-0 grid-cols-[238px_minmax(420px,1fr)_284px] border-b border-[#34383f] max-xl:grid-cols-[210px_minmax(360px,1fr)_270px] max-lg:grid-cols-1 max-lg:overflow-y-auto">
          <aside data-openreel-media-bin="true" className="flex min-h-0 flex-col border-r border-[#34383f] bg-[#191b1f]">
            <div className="flex h-8 items-end justify-between border-b border-[#34383f] bg-[#202328] px-2.5">
              <div className="flex h-full items-center border-b-2 border-[#4d92c5] text-[10px] font-semibold text-[#e3e5e8]">媒体池</div>
              <div className="mb-2 font-mono text-[9px] text-[#777d86]">{mediaNodes.length} ITEMS</div>
            </div>
            <div
              data-openreel-source-monitor="true"
              data-source-in-frame={selectedSourceMark.inFrame}
              data-source-out-frame={selectedSourceMark.outFrame}
              data-source-cursor-frame={sourceCursorFrame}
              className="flex h-[78px] shrink-0 gap-2 border-b border-[#30343a] bg-[#15181c] p-2"
            >
              <div className="flex h-[46px] w-[72px] shrink-0 items-center justify-center overflow-hidden border border-[#30353b] bg-black">
                {selectedMediaItem?.type === "video" ? (
                  <video ref={sourcePreviewRef} src={selectedMediaItem.src} muted preload="metadata" className="h-full w-full object-cover" />
                ) : selectedMediaItem?.type === "image" ? (
                  <img src={selectedMediaItem.src} alt="" className="h-full w-full object-cover" draggable={false} />
                ) : (
                  <EditorIcon name="audio" className="h-5 w-5 text-[#77bd9c]" />
                )}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[8px] font-medium text-[#bdc2c8]">{selectedMediaItem?.title || "选择源素材"}</div>
                <input
                  type="range"
                  min="0"
                  max={Math.max(0, selectedSourceFrameCount - 1)}
                  step="1"
                  value={sourceCursorFrame}
                  onChange={(event) => setSourceCursorFrame(Number(event.target.value))}
                  className="mt-1 h-1 w-full accent-[#5a9dcc]"
                  aria-label="源监视器播放头"
                />
                <div className="mt-1 flex items-center gap-1">
                  <button type="button" onClick={() => updateSelectedSourceMark({ inFrame: sourceCursorFrame })} className="h-4 border border-[#3c5669] bg-[#243845] px-1 text-[7px] text-[#b9d8ed]" aria-label="设置源入点" title="设置源入点 (I)">I</button>
                  <input type="number" min="0" max={selectedSourceMark.outFrame - 1} value={selectedSourceMark.inFrame} onChange={(event) => updateSelectedSourceMark({ inFrame: Number(event.target.value) })} className="h-4 w-10 border border-[#343a41] bg-[#20242a] px-1 text-right font-mono text-[7px] text-[#d6d9de] outline-none" aria-label="源素材入点帧" />
                  <span className="text-[7px] text-[#666d75]">–</span>
                  <input type="number" min={selectedSourceMark.inFrame + 1} max={selectedSourceFrameCount} value={selectedSourceMark.outFrame} onChange={(event) => updateSelectedSourceMark({ outFrame: Number(event.target.value) })} className="h-4 w-10 border border-[#343a41] bg-[#20242a] px-1 text-right font-mono text-[7px] text-[#d6d9de] outline-none" aria-label="源素材出点帧" />
                  <button type="button" onClick={() => updateSelectedSourceMark({ outFrame: sourceCursorFrame + 1 })} className="h-4 border border-[#66563e] bg-[#403522] px-1 text-[7px] text-[#ead6b5]" aria-label="设置源出点" title="设置源出点 (O)">O</button>
                </div>
                <div className="mt-1 font-mono text-[7px] text-[#676e76]">{selectedSourceMark.outFrame - selectedSourceMark.inFrame}f · CUR {sourceCursorFrame}f</div>
              </div>
            </div>
            <div className="flex h-8 shrink-0 items-center justify-between border-b border-[#30343a] bg-[#1c1f23] px-2">
              <div className="min-w-0 truncate text-[9px] text-[#aeb3ba]">{selectedMediaItem?.title || "选择源素材"}</div>
              <div className="flex shrink-0 gap-1">
                <button
                  type="button"
                  disabled={!selectedMediaItem}
                  onClick={() => selectedMediaItem && placeMediaItem(selectedMediaItem, "insert")}
                  className="h-5 border border-[#3e4f5e] bg-[#263744] px-1.5 font-mono text-[8px] text-[#bcd9ee] hover:bg-[#315f83] disabled:opacity-35"
                  aria-label="插入所选素材"
                  title="插入 (,)"
                >
                  INSERT ,
                </button>
                <button
                  type="button"
                  disabled={!selectedMediaItem}
                  onClick={() => selectedMediaItem && placeMediaItem(selectedMediaItem, "overwrite")}
                  className="h-5 border border-[#594d3a] bg-[#3a3023] px-1.5 font-mono text-[8px] text-[#ead5b3] hover:bg-[#6b5330] disabled:opacity-35"
                  aria-label="覆盖所选素材"
                  title="覆盖 (.)"
                >
                  OVERWRITE .
                </button>
              </div>
            </div>
            <div className="grid min-h-0 flex-1 auto-rows-max grid-cols-2 content-start gap-1.5 overflow-y-auto p-2">
              {mediaNodes.map((item) => (
                <MediaBinItem
                  key={item.id}
                  item={item}
                  onInsert={insertMediaItem}
                  onOverwrite={overwriteMediaItem}
                  onSelect={(selected) => setSelectedMediaId(selected.id)}
                  selected={item.id === selectedMediaItem?.id}
                />
              ))}
              {mediaNodes.length === 0 && (
                <div className="col-span-2 border border-dashed border-[#3a3f46] px-3 py-6 text-center text-[10px] leading-5 text-[#777d86]">
                  当前项目里的图片、视频和音频会出现在这里
                </div>
              )}
            </div>
          </aside>

          <main data-openreel-preview-pane="true" data-playback-clock={playbackClockSource} className="flex min-h-0 min-w-0 flex-col bg-[#111316]">
            <div className="flex h-7 shrink-0 items-center justify-between border-b border-[#2f3339] bg-[#1d2024] px-2.5">
              <span className="text-[9px] font-medium text-[#aeb3ba]">时间线监看器</span>
              <span className="font-mono text-[8px] text-[#656b73]">{programVideoGap ? "BLACK" : programAudioGap ? "SILENCE" : `PROGRAM · ${playbackClockSource.toUpperCase()}`}</span>
            </div>
            <div className="flex min-h-0 flex-1 items-center justify-center bg-[#090a0c] p-1.5">
              <div
                className="relative flex max-h-full items-center justify-center overflow-hidden border border-[#292d32] bg-black shadow-[0_0_0_1px_rgba(0,0,0,.8)]"
                style={{
                  ...previewScaleStyle,
                  aspectRatio: `${sequenceSpec.settings.width} / ${sequenceSpec.settings.height}`,
                }}
                data-openreel-program-gap={programVideoGap || programAudioGap ? "true" : "false"}
                data-program-video-gap={programVideoGap ? "true" : "false"}
                data-program-audio-gap={programAudioGap ? "true" : "false"}
                data-visual-fit={currentVisualTransform.fit}
                data-visual-position-x={currentVisualTransform.positionX}
                data-visual-position-y={currentVisualTransform.positionY}
                data-visual-scale={currentVisualTransform.scale}
                data-visual-rotation={currentVisualTransform.rotationDeg}
                data-visual-opacity={currentVisualTransform.opacity}
                data-visual-crop={`${currentVisualTransform.cropTop},${currentVisualTransform.cropRight},${currentVisualTransform.cropBottom},${currentVisualTransform.cropLeft}`}
                data-active-video-transition={activeVideoTransition?.transition.id || ""}
                data-video-transition-progress={activeVideoTransition ? activeVideoTransition.progress.toFixed(4) : ""}
                data-video-transition-compositor={activeVideoTransition ? (videoTransitionSingleSource ? "single-source" : "dual-source") : ""}
                data-active-audio-transition={activeAudioTransition?.transition.id || ""}
                data-audio-transition-progress={activeAudioTransition ? activeAudioTransition.progress.toFixed(4) : ""}
                data-audio-transition-compositor={activeAudioTransition
                  ? (audioTransitionThroughVideo ? "video-source" : audioTransitionSingleSource ? "single-source" : "dual-source")
                  : ""}
                data-audio-outgoing-gain={activeAudioTransition ? activeAudioTransition.outgoingPower.toFixed(4) : ""}
                data-audio-incoming-gain={activeAudioTransition ? activeAudioTransition.incomingPower.toFixed(4) : ""}
              >
                {currentVideoItem ? (
                  currentVideoItem.type === "image" ? (
                    <img
                      src={currentVideoItem.src}
                      alt=""
                      className={cn("h-full w-full object-contain", activeVideoTransition && "absolute inset-0")}
                      style={{
                        ...currentVisualTransformStyle,
                        zIndex: currentVisualLayerZ,
                        opacity: currentVisualTransform.opacity * currentVisualLayerOpacity,
                      }}
                      draggable={false}
                      data-openreel-preview-visual="true"
                    />
                  ) : (
                    <video
                      ref={videoRef}
                      data-openreel-preview-video="true"
                      data-openreel-preview-visual="true"
                      src={currentVideoItem.src || videoUrl}
                      crossOrigin={previewMediaCrossOrigin(currentVideoItem.src || videoUrl)}
                      muted={audioTransitionThroughVideo
                        ? false
                        : Boolean(activeAudioTransition) || !playAudioThroughVideo || Boolean(currentAudioTrack?.muted) || Boolean(currentAudioClip?.muted)}
                      preload="metadata"
                      className={cn(
                        "object-contain [color-scheme:dark]",
                        playbackResolution === "full" && !canvasVideoPreview
                          ? cn("h-full w-full", activeVideoTransition && "absolute inset-0")
                          : "pointer-events-none absolute h-px w-px opacity-0",
                      )}
                      style={playbackResolution === "full" && !canvasVideoPreview
                        ? {
                            ...currentVisualTransformStyle,
                            zIndex: currentVisualLayerZ,
                            opacity: currentVisualTransform.opacity * currentVisualLayerOpacity,
                          }
                        : { ...currentVisualTransformStyle, opacity: 0 }}
                      onLoadedMetadata={(event) => {
                        const nextDuration = Number(event.currentTarget.duration || 0)
                        registerSourceDuration(currentVideoItem.src, nextDuration)
                      }}
                    />
                  )
                ) : (
                  <div className="absolute bottom-2 right-2 border border-white/10 bg-black/70 px-1.5 py-0.5 font-mono text-[7px] tracking-[0.08em] text-[#5f656d]">BLACK · GAP</div>
                )}
                {transitionVideoItem && (
                  transitionVideoItem.type === "image" ? (
                    <img
                      src={transitionVideoItem.src}
                      alt=""
                      className="absolute inset-0 h-full w-full object-contain"
                      style={{
                        ...visualTransformStyle(transitionVisualTransform),
                        zIndex: transitionVisualLayerZ,
                        opacity: transitionVisualTransform.opacity * transitionVisualLayerOpacity,
                      }}
                      draggable={false}
                      data-openreel-transition-visual="true"
                      data-transition-layer={transitionVisualIsIncoming ? "incoming" : "outgoing"}
                    />
                  ) : (
                    <video
                      ref={transitionVideoRef}
                      src={transitionVideoItem.src}
                      muted
                      preload="metadata"
                      className={cn(
                        "object-contain [color-scheme:dark]",
                        playbackResolution === "full" && !canvasVideoPreview
                          ? "absolute inset-0 h-full w-full"
                          : "pointer-events-none absolute h-px w-px opacity-0",
                      )}
                      style={playbackResolution === "full" && !canvasVideoPreview
                        ? {
                            ...visualTransformStyle(transitionVisualTransform),
                            zIndex: transitionVisualLayerZ,
                            opacity: transitionVisualTransform.opacity * transitionVisualLayerOpacity,
                          }
                        : { opacity: 0 }}
                      data-openreel-transition-video="true"
                      data-transition-layer={transitionVisualIsIncoming ? "incoming" : "outgoing"}
                      data-transition-layer-opacity={transitionVisualLayerOpacity.toFixed(4)}
                    />
                  )
                )}
                {currentVideoItem?.type === "video" && (playbackResolution !== "full" || canvasVideoPreview) && (
                  <canvas
                    ref={programCanvasRef}
                    data-openreel-program-canvas="true"
                    data-playback-resolution={playbackResolution}
                    className="absolute inset-0 h-full w-full object-contain"
                    style={{ zIndex: currentVisualLayerZ, opacity: currentVisualLayerOpacity }}
                  />
                )}
                {transitionVideoItem?.type === "video" && (playbackResolution !== "full" || canvasVideoPreview) && (
                  <canvas
                    ref={transitionProgramCanvasRef}
                    data-openreel-transition-program-canvas="true"
                    data-playback-resolution={playbackResolution}
                    className="absolute inset-0 h-full w-full object-contain"
                    style={{ zIndex: transitionVisualLayerZ, opacity: transitionVisualLayerOpacity }}
                  />
                )}
                {!programVideoGap && programAudioGap && (
                  <div className="pointer-events-none absolute bottom-2 right-2 border border-white/10 bg-black/70 px-1.5 py-0.5 font-mono text-[7px] tracking-[0.08em] text-[#8b918f]">SILENCE</div>
                )}
              </div>
              {currentAudioItem && !playAudioThroughVideo && !activeAudioTransition && (
                <audio data-openreel-preview-audio="true" ref={audioRef} src={currentAudioItem.src} crossOrigin={previewMediaCrossOrigin(currentAudioItem.src)} preload="metadata" muted={Boolean(currentAudioTrack?.muted) || Boolean(currentAudioClip?.muted)} />
              )}
              {activeAudioTransition?.outgoingItem && !audioTransitionThroughVideo && (
                <audio
                  data-openreel-transition-audio="outgoing"
                  data-transition-gain={(audioTransitionSingleSource
                    ? Math.hypot(
                        gainAmplitude((activeAudioTransition.outgoing.gainDb || 0) + activeAudioTransition.track.gainDb) * activeAudioTransition.outgoingPower,
                        gainAmplitude((activeAudioTransition.incoming.gainDb || 0) + activeAudioTransition.track.gainDb) * activeAudioTransition.incomingPower,
                      )
                    : activeAudioTransition.outgoingPower).toFixed(4)}
                  ref={transitionOutgoingAudioRef}
                  src={activeAudioTransition.outgoingItem.src}
                  crossOrigin={previewMediaCrossOrigin(activeAudioTransition.outgoingItem.src)}
                  preload="metadata"
                />
              )}
              {activeAudioTransition?.incomingItem && !audioTransitionSingleSource && !audioTransitionThroughVideo && (
                <audio
                  data-openreel-transition-audio="incoming"
                  data-transition-gain={activeAudioTransition.incomingPower.toFixed(4)}
                  ref={transitionIncomingAudioRef}
                  src={activeAudioTransition.incomingItem.src}
                  crossOrigin={previewMediaCrossOrigin(activeAudioTransition.incomingItem.src)}
                  preload="metadata"
                />
              )}
            </div>

            <div className="relative flex h-10 shrink-0 items-center justify-between border-t border-[#30343a] bg-[#1c1f23] px-2.5">
              <div className="flex min-w-[154px] items-center gap-2">
                <span className="font-mono text-[11px] font-medium tabular-nums text-[#d9dde2]" data-openreel-program-timecode="true">{formatFrameTimecode(currentFrame, framesPerSecond)}</span>
                <span className="font-mono text-[7px] tabular-nums text-[#6d737b]">{currentFrame}f</span>
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
                  data-openreel-export-frame-control="true"
                >
                  <EditorIcon name="frame" className="h-3 w-3" />
                </button>
                <button
                  type="button"
                  onClick={() => setLoopEnabled((value) => !value)}
                  disabled={sequenceEndFrame <= 1}
                  className={cn(
                    "h-5 border px-1 font-mono text-[7px]",
                    loopEnabled ? "border-[#8f7136] bg-[#5b4724] text-[#ffe0a0]" : "border-[#3a3f46] bg-[#25282d] text-[#7e848c]",
                  )}
                  aria-label={loopEnabled ? "关闭循环播放" : "开启循环播放"}
                  title="循环播放"
                  data-openreel-loop-enabled={loopEnabled ? "true" : "false"}
                >
                  LOOP
                </button>
                <button type="button" onClick={setProgramLoopIn} className="h-5 border border-[#3a3f46] bg-[#25282d] px-1 font-mono text-[7px] text-[#8e949c]" aria-label="设置节目循环入点" title="设置循环入点 ([)">[</button>
                <button type="button" onClick={setProgramLoopOut} className="h-5 border border-[#3a3f46] bg-[#25282d] px-1 font-mono text-[7px] text-[#8e949c]" aria-label="设置节目循环出点" title="设置循环出点 (])">]</button>
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
              <div className="hidden min-w-[220px] items-center justify-end gap-1.5 text-[9px] text-[#717780] sm:flex">
                <span className="font-mono text-[7px] tabular-nums text-[#666d75]" data-openreel-loop-range="true" data-loop-in-frame={loopRange.inFrame} data-loop-out-frame={effectiveLoopOutFrame}>{formatFrameTimecode(loopRange.inFrame, framesPerSecond)}–{formatFrameTimecode(effectiveLoopOutFrame, framesPerSecond)}</span>
                <select
                  value={playbackResolution}
                  onChange={(event) => setPlaybackResolution(event.target.value as PlaybackResolution)}
                  className="h-6 rounded-[2px] border border-[#353a41] bg-[#24272c] px-1 text-[8px] text-[#c8ccd1] outline-none"
                  title="回放分辨率"
                  aria-label="回放分辨率"
                >
                  <option value="full">FULL</option>
                  <option value="half">1/2</option>
                  <option value="quarter">1/4</option>
                </select>
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

          <aside data-openreel-inspector-pane="true" className="flex min-h-0 flex-col border-l border-[#34383f] bg-[#191b1f]">
            <div className="flex h-8 items-end justify-between border-b border-[#34383f] bg-[#202328] px-2.5">
              <div className="flex h-full items-center border-b-2 border-[#4d92c5] text-[10px] font-semibold text-[#e3e5e8]">检查器</div>
              <div className="mb-2 text-[8px] uppercase tracking-[0.12em] text-[#6e747d]">Clip</div>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <section className="border-b border-[#34383f] px-3 py-2.5">
                <div className="mb-2 flex items-center justify-between">
                  <div className="text-[9px] font-semibold uppercase tracking-[0.1em] text-[#b8bdc4]">输出与媒体操作</div>
                  <span className={cn("h-1.5 w-1.5 rounded-full", isBusy ? "bg-[#67a9d8]" : "bg-[#6b727b]")} />
                </div>
                <button
                  type="button"
                  onClick={() => void renderSequence()}
                  disabled={isBusy || !sequenceLoaded || sequenceEndFrame <= 0}
                  aria-label="导出时间线成片"
                  data-openreel-render-sequence="true"
                  className={cn(
                    "mb-1.5 flex h-9 w-full items-center justify-between rounded-[3px] border px-2.5 text-left transition",
                    busy === "render"
                      ? "border-[#5e9dca] bg-[#315f83] text-white"
                      : "border-[#477da4] bg-[#26465e] text-[#e3f1fb] hover:border-[#6aa8d4] hover:bg-[#315772]",
                    (isBusy || !sequenceLoaded || sequenceEndFrame <= 0) && "cursor-not-allowed opacity-40",
                  )}
                >
                  <span>
                    <span className="block text-[10px] font-semibold">
                      {busy === "render" ? `正在渲染时间线 ${renderJob?.progress || 0}%` : "导出时间线成片"}
                    </span>
                    <span className="mt-0.5 block font-mono text-[7px] text-[#9fc5df]">
                      H.264 · AAC · {sequenceSpec.settings.width}×{sequenceSpec.settings.height} · {framesPerSecond.toFixed(2)} FPS
                    </span>
                  </span>
                  <span className="font-mono text-[8px] text-[#9fc5df]">r{sequenceRevision}</span>
                </button>
                {renderJob && ["queued", "running", "cancelling"].includes(renderJob.status) && (
                  <div
                    className="mb-1.5 border border-[#355a74] bg-[#1e303d] px-2 py-1.5"
                    data-openreel-render-progress="true"
                    data-render-status={renderJob.status}
                    data-render-progress={renderJob.progress}
                  >
                    <div className="mb-1 flex items-center justify-between text-[8px] text-[#b9d7eb]">
                      <span>{renderJob.phase || "正在渲染"}</span>
                      <span className="font-mono">{renderJob.progress}%</span>
                    </div>
                    <div className="h-1 overflow-hidden bg-[#14232d]">
                      <div
                        className="h-full bg-[#62a9d8] transition-[width] duration-300"
                        style={{ width: `${Math.max(0, Math.min(100, renderJob.progress))}%` }}
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => void cancelSequenceRender()}
                      disabled={renderJob.status === "cancelling"}
                      className="mt-1.5 h-5 w-full border border-[#735050] bg-[#3b292b] text-[8px] text-[#e7bfc0] hover:border-[#996264] hover:bg-[#4a3033] disabled:cursor-wait disabled:opacity-50"
                      aria-label="取消时间线导出"
                      data-openreel-cancel-render="true"
                    >
                      {renderJob.status === "cancelling" ? "正在取消…" : "取消导出"}
                    </button>
                  </div>
                )}
                {renderJob?.status === "cancelled" && !renderNotice && (
                  <div className="mb-1.5 border border-[#75613d] bg-[#3d3321] px-2 py-1.5 text-[8px] text-[#e5d09b]" data-openreel-render-cancelled="true">
                    导出已取消，时间线和已有成片均未改变。
                  </div>
                )}
                {renderJob?.status === "failed" && (
                  <div className="mb-1.5 border border-[#7d4547] bg-[#3c2426] px-2 py-1.5 text-[8px] leading-3 text-[#f0c1c3]" data-openreel-render-failed="true">
                    {renderJob.error_message || "时间线导出失败"}
                  </div>
                )}
                {renderNotice && !error && (
                  <div
                    className="mb-1.5 border border-[#386b55] bg-[#203d31] px-2 py-1.5 text-[8px] leading-3 text-[#bde6d0]"
                    data-openreel-render-success="true"
                  >
                    {renderNotice}
                  </div>
                )}
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
                  <div className="font-mono text-[8px] text-[#707680]">{trimMode.toUpperCase()} · SNAP {snappingEnabled ? `${SNAP_PIXELS}px` : "OFF"} · {selectedClipIds.size} SEL</div>
                </div>
                {selectedTimelineClip && (
                  <div className="mb-2 space-y-1.5 border border-[#30343a] bg-[#17191d] p-2" data-openreel-frame-inspector="true">
                    <label className="flex items-center justify-between gap-2 text-[8px] text-[#7f858e]">
                      <span>时间线起始帧</span>
                      <span className="flex items-center gap-1">
                        <input
                          type="number"
                          min="0"
                          step="1"
                          value={selectedTimelineClip.startFrame}
                          onFocus={recordUndoSnapshot}
                          onChange={(event) => updateSelectedClipFrameValue("startFrame", Number(event.target.value))}
                          className="h-5 w-20 rounded-[2px] border border-[#3a3f46] bg-[#24272c] px-1.5 text-right font-mono text-[8px] text-[#d5d9de] outline-none focus:border-[#579bd3]"
                          aria-label="时间线起始帧"
                        />
                        <span>f</span>
                      </span>
                    </label>
                    <label className="flex items-center justify-between gap-2 text-[8px] text-[#7f858e]">
                      <span>源入点帧</span>
                      <span className="flex items-center gap-1">
                        <input
                          type="number"
                          min="0"
                          step="1"
                          value={selectedTimelineClip.sourceInFrame}
                          onFocus={recordUndoSnapshot}
                          onChange={(event) => updateSelectedClipFrameValue("sourceInFrame", Number(event.target.value))}
                          className="h-5 w-20 rounded-[2px] border border-[#3a3f46] bg-[#24272c] px-1.5 text-right font-mono text-[8px] text-[#d5d9de] outline-none focus:border-[#579bd3]"
                          aria-label="源入点帧"
                        />
                        <span>f</span>
                      </span>
                    </label>
                    <label className="flex items-center justify-between gap-2 text-[8px] text-[#7f858e]">
                      <span>片段持续帧</span>
                      <span className="flex items-center gap-1">
                        <input
                          type="number"
                          min="1"
                          step="1"
                          value={selectedTimelineClip.durationFrames}
                          onFocus={recordUndoSnapshot}
                          onChange={(event) => updateSelectedClipFrameValue("durationFrames", Number(event.target.value))}
                          className="h-5 w-20 rounded-[2px] border border-[#3a3f46] bg-[#24272c] px-1.5 text-right font-mono text-[8px] text-[#d5d9de] outline-none focus:border-[#579bd3]"
                          aria-label="片段持续帧"
                        />
                        <span>f</span>
                      </span>
                    </label>
                    <label className="flex items-center justify-between gap-2 text-[8px] text-[#7f858e]">
                      <span>源出点帧</span>
                      <span className="flex items-center gap-1">
                        <input
                          type="number"
                          min={selectedTimelineClip.sourceInFrame + 1}
                          step="1"
                          value={selectedTimelineClip.sourceInFrame + selectedTimelineClip.durationFrames}
                          onFocus={recordUndoSnapshot}
                          onChange={(event) => updateSelectedClipFrameValue("sourceOutFrame", Number(event.target.value))}
                          className="h-5 w-20 rounded-[2px] border border-[#3a3f46] bg-[#24272c] px-1.5 text-right font-mono text-[8px] text-[#d5d9de] outline-none focus:border-[#579bd3]"
                          aria-label="源出点帧"
                        />
                        <span>f</span>
                      </span>
                    </label>
                    <div className="border-t border-[#30343a] pt-1.5" data-openreel-timecode-inspector="true">
                      <div className="mb-1 flex items-center justify-between font-mono text-[7px] uppercase tracking-[0.08em] text-[#666d76]">
                        <span>Timecode</span>
                        <span>{nominalFramesPerSecond(framesPerSecond)} FPS NDF</span>
                      </div>
                      <div className="grid grid-cols-2 gap-1.5">
                        <label className="min-w-0 text-[7px] text-[#737a83]">
                          <span className="mb-0.5 block">时间线入点</span>
                          <FrameTimecodeInput
                            frame={selectedTimelineClip.startFrame}
                            framesPerSecond={framesPerSecond}
                            ariaLabel="时间线起始时间码"
                            onFocus={recordUndoSnapshot}
                            onCommit={(frame) => updateSelectedClipFrameValue("startFrame", frame)}
                          />
                        </label>
                        <label className="min-w-0 text-[7px] text-[#737a83]">
                          <span className="mb-0.5 block">持续时间</span>
                          <FrameTimecodeInput
                            frame={selectedTimelineClip.durationFrames}
                            framesPerSecond={framesPerSecond}
                            ariaLabel="片段持续时间码"
                            onFocus={recordUndoSnapshot}
                            onCommit={(frame) => updateSelectedClipFrameValue("durationFrames", frame)}
                          />
                        </label>
                        <label className="min-w-0 text-[7px] text-[#737a83]">
                          <span className="mb-0.5 block">源入点</span>
                          <FrameTimecodeInput
                            frame={selectedTimelineClip.sourceInFrame}
                            framesPerSecond={framesPerSecond}
                            ariaLabel="源入点时间码"
                            onFocus={recordUndoSnapshot}
                            onCommit={(frame) => updateSelectedClipFrameValue("sourceInFrame", frame)}
                          />
                        </label>
                        <label className="min-w-0 text-[7px] text-[#737a83]">
                          <span className="mb-0.5 block">源出点</span>
                          <FrameTimecodeInput
                            frame={selectedTimelineClip.sourceInFrame + selectedTimelineClip.durationFrames}
                            framesPerSecond={framesPerSecond}
                            ariaLabel="源出点时间码"
                            onFocus={recordUndoSnapshot}
                            onCommit={(frame) => updateSelectedClipFrameValue("sourceOutFrame", frame)}
                          />
                        </label>
                      </div>
                    </div>
                  </div>
                )}
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

              {selectedVideoClip && (
                <section className="border-b border-[#34383f] px-3 py-2.5" data-openreel-visual-inspector="true">
                  <div className="mb-2 flex items-center justify-between">
                    <div>
                      <div className="text-[9px] font-semibold uppercase tracking-[0.1em] text-[#b8bdc4]">画面</div>
                      <div className="mt-0.5 font-mono text-[7px] text-[#666d76]">MOTION · CROP · OPACITY</div>
                    </div>
                    <button
                      type="button"
                      onClick={resetSelectedVisualTransform}
                      className="h-5 border border-[#3b4148] bg-[#25282d] px-2 text-[8px] text-[#a6abb2] hover:border-[#56606a] hover:bg-[#30343a] hover:text-white"
                      aria-label="重置画面属性"
                      data-openreel-reset-visual="true"
                    >
                      重置
                    </button>
                  </div>
                  <div className="space-y-2 border border-[#30343a] bg-[#17191d] p-2">
                    <label className="flex items-center justify-between gap-2 text-[8px] text-[#7f858e]">
                      <span>适配</span>
                      <select
                        value={selectedVideoClip.visualTransform.fit}
                        onFocus={recordUndoSnapshot}
                        onChange={(event) => updateSelectedVisualTransform({ fit: event.target.value === "cover" ? "cover" : "contain" })}
                        className="h-5 w-24 border border-[#3a3f46] bg-[#24272c] px-1 font-mono text-[8px] text-[#d5d9de] outline-none focus:border-[#579bd3]"
                        aria-label="画面适配"
                      >
                        <option value="contain">适合</option>
                        <option value="cover">填充</option>
                      </select>
                    </label>
                    <div className="grid grid-cols-2 gap-1.5">
                      <label className="text-[7px] text-[#737a83]">
                        <span className="mb-0.5 block">位置 X</span>
                        <span className="flex items-center border border-[#3a3f46] bg-[#24272c] focus-within:border-[#579bd3]">
                          <input
                            type="number"
                            min="-200"
                            max="200"
                            step="0.5"
                            value={Number((selectedVideoClip.visualTransform.positionX * 100).toFixed(1))}
                            onFocus={recordUndoSnapshot}
                            onChange={(event) => updateSelectedVisualTransform({ positionX: Number(event.target.value) / 100 })}
                            className="h-5 min-w-0 flex-1 bg-transparent px-1 text-right font-mono text-[8px] text-[#d5d9de] outline-none"
                            aria-label="画面位置 X"
                          />
                          <span className="pr-1 text-[7px] text-[#69717a]">%</span>
                        </span>
                      </label>
                      <label className="text-[7px] text-[#737a83]">
                        <span className="mb-0.5 block">位置 Y</span>
                        <span className="flex items-center border border-[#3a3f46] bg-[#24272c] focus-within:border-[#579bd3]">
                          <input
                            type="number"
                            min="-200"
                            max="200"
                            step="0.5"
                            value={Number((selectedVideoClip.visualTransform.positionY * 100).toFixed(1))}
                            onFocus={recordUndoSnapshot}
                            onChange={(event) => updateSelectedVisualTransform({ positionY: Number(event.target.value) / 100 })}
                            className="h-5 min-w-0 flex-1 bg-transparent px-1 text-right font-mono text-[8px] text-[#d5d9de] outline-none"
                            aria-label="画面位置 Y"
                          />
                          <span className="pr-1 text-[7px] text-[#69717a]">%</span>
                        </span>
                      </label>
                    </div>
                    <label className="flex items-center gap-2">
                      <span className="w-10 text-[8px] text-[#777d86]">缩放</span>
                      <input
                        type="range"
                        min="10"
                        max="400"
                        step="1"
                        value={selectedVideoClip.visualTransform.scale * 100}
                        onFocus={recordUndoSnapshot}
                        onPointerDown={recordUndoSnapshot}
                        onChange={(event) => updateSelectedVisualTransform({ scale: Number(event.target.value) / 100 })}
                        className="h-1 min-w-0 flex-1 accent-[#659ac0]"
                        aria-label="画面缩放"
                      />
                      <span className="w-10 text-right font-mono text-[8px] text-[#c8ccd1]">{Math.round(selectedVideoClip.visualTransform.scale * 100)}%</span>
                    </label>
                    <label className="flex items-center gap-2">
                      <span className="w-10 text-[8px] text-[#777d86]">旋转</span>
                      <input
                        type="range"
                        min="-180"
                        max="180"
                        step="0.5"
                        value={selectedVideoClip.visualTransform.rotationDeg}
                        onFocus={recordUndoSnapshot}
                        onPointerDown={recordUndoSnapshot}
                        onChange={(event) => updateSelectedVisualTransform({ rotationDeg: Number(event.target.value) })}
                        className="h-1 min-w-0 flex-1 accent-[#659ac0]"
                        aria-label="画面旋转"
                      />
                      <span className="w-10 text-right font-mono text-[8px] text-[#c8ccd1]">{selectedVideoClip.visualTransform.rotationDeg.toFixed(1)}°</span>
                    </label>
                    <label className="flex items-center gap-2">
                      <span className="w-10 text-[8px] text-[#777d86]">不透明</span>
                      <input
                        type="range"
                        min="0"
                        max="100"
                        step="1"
                        value={selectedVideoClip.visualTransform.opacity * 100}
                        onFocus={recordUndoSnapshot}
                        onPointerDown={recordUndoSnapshot}
                        onChange={(event) => updateSelectedVisualTransform({ opacity: Number(event.target.value) / 100 })}
                        className="h-1 min-w-0 flex-1 accent-[#659ac0]"
                        aria-label="画面不透明度"
                      />
                      <span className="w-10 text-right font-mono text-[8px] text-[#c8ccd1]">{Math.round(selectedVideoClip.visualTransform.opacity * 100)}%</span>
                    </label>
                    <div className="border-t border-[#30343a] pt-2">
                      <div className="mb-1.5 flex items-center justify-between text-[7px] uppercase tracking-[0.08em] text-[#666d76]">
                        <span>矩形裁剪</span>
                        <span>%</span>
                      </div>
                      <div className="grid grid-cols-4 gap-1">
                        {([
                          ["左", "cropLeft", "画面裁剪左"],
                          ["上", "cropTop", "画面裁剪上"],
                          ["右", "cropRight", "画面裁剪右"],
                          ["下", "cropBottom", "画面裁剪下"],
                        ] as const).map(([label, key, ariaLabel]) => (
                          <label key={key} className="min-w-0 text-center text-[7px] text-[#737a83]">
                            <span className="mb-0.5 block">{label}</span>
                            <input
                              type="number"
                              min="0"
                              max="95"
                              step="0.5"
                              value={Number((selectedVideoClip.visualTransform[key] * 100).toFixed(1))}
                              onFocus={recordUndoSnapshot}
                              onChange={(event) => updateSelectedVisualTransform({ [key]: Number(event.target.value) / 100 })}
                              className="h-5 w-full border border-[#3a3f46] bg-[#24272c] px-1 text-center font-mono text-[8px] text-[#d5d9de] outline-none focus:border-[#579bd3]"
                              aria-label={ariaLabel}
                            />
                          </label>
                        ))}
                      </div>
                    </div>
                  </div>
                </section>
              )}

              {(selectedVideoOutgoingClip || selectedAudioOutgoingClip) && (
                <section className="border-b border-[#34383f] px-3 py-2.5" data-openreel-transition-inspector="true">
                  <div className="mb-2 flex items-center justify-between">
                    <div>
                      <div className="text-[9px] font-semibold uppercase tracking-[0.1em] text-[#b8bdc4]">转场</div>
                      <div className="mt-0.5 font-mono text-[7px] text-[#666d76]">CUT POINT · SOURCE HANDLES</div>
                    </div>
                    <span className="font-mono text-[7px] text-[#737a83]">{selectedTimelineClip?.startFrame || 0}f</span>
                  </div>
                  <div className="space-y-1.5">
                    {selectedVideoClip && selectedVideoOutgoingClip && (
                      <div className="border border-[#334653] bg-[#171b1f] p-1.5" data-transition-inspector-kind="video_cross_dissolve">
                        <div className="flex items-center justify-between gap-2">
                          <div className="min-w-0">
                            <div className="truncate text-[8px] font-medium text-[#c7dcea]">视频交叉叠化</div>
                            <div className="mt-0.5 font-mono text-[6px] text-[#647784]">LINEAR DISSOLVE</div>
                          </div>
                          <button
                            type="button"
                            onClick={() => setCutTransition(
                              "video_cross_dissolve",
                              selectedVideoOutgoingClip,
                              selectedVideoClip,
                              selectedVideoTransition,
                            )}
                            className={cn(
                              "h-5 shrink-0 border px-2 text-[7px]",
                              selectedVideoTransition
                                ? "border-[#72545a] bg-[#3a272b] text-[#d8a9af] hover:bg-[#4a2f35]"
                                : "border-[#456b84] bg-[#23445a] text-[#d5efff] hover:bg-[#2d5873]",
                            )}
                            aria-label={selectedVideoTransition ? "删除视频交叉叠化" : "添加视频交叉叠化"}
                          >
                            {selectedVideoTransition ? "移除" : "添加"}
                          </button>
                        </div>
                        {selectedVideoTransition && (
                          <label className="mt-1.5 flex items-center justify-between gap-2 border-t border-[#2d3941] pt-1.5 text-[7px] text-[#788892]">
                            <span>持续帧</span>
                            <span className="flex items-center gap-1">
                              <input
                                type="number"
                                min="2"
                                max={maxTransitionDuration(selectedVideoOutgoingClip, selectedVideoClip, selectedVideoTransition.id)}
                                step="1"
                                value={selectedVideoTransition.durationFrames}
                                onFocus={recordUndoSnapshot}
                                onChange={(event) => updateCutTransitionDuration(
                                  selectedVideoTransition,
                                  selectedVideoOutgoingClip,
                                  selectedVideoClip,
                                  Number(event.target.value),
                                )}
                                className="h-5 w-16 border border-[#3a4650] bg-[#242a2f] px-1 text-right font-mono text-[8px] text-[#d9e8f2] outline-none focus:border-[#579bd3]"
                                aria-label="视频交叉叠化时长帧"
                              />
                              <span>f</span>
                            </span>
                          </label>
                        )}
                      </div>
                    )}
                    {selectedAudioClip && selectedAudioOutgoingClip && (
                      <div className="border border-[#33483e] bg-[#171b19] p-1.5" data-transition-inspector-kind="audio_constant_power">
                        <div className="flex items-center justify-between gap-2">
                          <div className="min-w-0">
                            <div className="truncate text-[8px] font-medium text-[#c6e3d3]">音频恒功率交叉淡化</div>
                            <div className="mt-0.5 font-mono text-[6px] text-[#627a6d]">COS / SIN POWER</div>
                          </div>
                          <button
                            type="button"
                            onClick={() => setCutTransition(
                              "audio_constant_power",
                              selectedAudioOutgoingClip,
                              selectedAudioClip,
                              selectedAudioTransition,
                            )}
                            className={cn(
                              "h-5 shrink-0 border px-2 text-[7px]",
                              selectedAudioTransition
                                ? "border-[#72545a] bg-[#3a272b] text-[#d8a9af] hover:bg-[#4a2f35]"
                                : "border-[#426c57] bg-[#234938] text-[#d9f5e5] hover:bg-[#2e5b47]",
                            )}
                            aria-label={selectedAudioTransition ? "删除音频恒功率交叉淡化" : "添加音频恒功率交叉淡化"}
                          >
                            {selectedAudioTransition ? "移除" : "添加"}
                          </button>
                        </div>
                        {selectedAudioTransition && (
                          <label className="mt-1.5 flex items-center justify-between gap-2 border-t border-[#2d3b34] pt-1.5 text-[7px] text-[#778a7f]">
                            <span>持续帧</span>
                            <span className="flex items-center gap-1">
                              <input
                                type="number"
                                min="2"
                                max={maxTransitionDuration(selectedAudioOutgoingClip, selectedAudioClip, selectedAudioTransition.id)}
                                step="1"
                                value={selectedAudioTransition.durationFrames}
                                onFocus={recordUndoSnapshot}
                                onChange={(event) => updateCutTransitionDuration(
                                  selectedAudioTransition,
                                  selectedAudioOutgoingClip,
                                  selectedAudioClip,
                                  Number(event.target.value),
                                )}
                                className="h-5 w-16 border border-[#3b4841] bg-[#242a27] px-1 text-right font-mono text-[8px] text-[#dbece3] outline-none focus:border-[#5c9f7d]"
                                aria-label="音频恒功率交叉淡化时长帧"
                              />
                              <span>f</span>
                            </span>
                          </label>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="mt-1.5 text-[7px] leading-3 text-[#666d76]">转场居中于剪切点，并严格占用两侧源素材把手。</div>
                </section>
              )}

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
                      max={MAX_CLIP_GAIN_DB}
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
                  {busy === "render" ? "正在按时间线渲染，请保持编辑器打开…" : "处理中..."}
                </div>
              )}
            </div>
          </aside>
        </div>

        <section className="flex min-h-0 w-full min-w-0 flex-col bg-[#15171a]">
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-[#34383f] bg-[#202328] px-2">
            <div className="flex items-center gap-2">
              <div className="mr-1 flex h-9 items-center border-b-2 border-[#4d92c5] px-1 text-[10px] font-semibold text-[#e2e4e7]">时间线 1</div>
              <ToolButton label="选择 (V)" icon="pointer" active={tool === "select" && trimMode === "normal"} onClick={() => {
                setTool("select")
                setTrimMode("normal")
              }} />
              <ToolButton label="切割" icon="blade" active={tool === "blade"} onClick={() => setTool("blade")} />
              <ToolButton label="波纹裁剪 (B)" icon="ripple" active={tool === "select" && trimMode === "ripple"} onClick={() => {
                setTool("select")
                setTrimMode("ripple")
              }} />
              <ToolButton label="滚动编辑 (N)" icon="rolling" active={tool === "select" && trimMode === "rolling"} onClick={() => {
                setTool("select")
                setTrimMode("rolling")
              }} />
              <div className="ml-1 h-4 w-px bg-[#3b4047]" />
              <button
                type="button"
                onClick={() => {
                  setSnappingEnabled((value) => !value)
                  setSnapGuideFrame(null)
                }}
                className={cn(
                  "h-6 border px-1.5 text-[8px] font-semibold",
                  snappingEnabled
                    ? "border-[#4c88ad] bg-[#315f83] text-[#e0f3ff]"
                    : "border-[#3b4148] bg-[#25282d] text-[#7f858d]",
                )}
                aria-label={snappingEnabled ? "关闭吸附" : "开启吸附"}
                title="吸附 (S)"
              >
                SNAP S
              </button>
              <button type="button" onClick={addSequenceMarker} className="h-6 border border-[#66593e] bg-[#3b3426] px-1.5 text-[8px] font-semibold text-[#e5cf9b] hover:bg-[#594a2d]" aria-label="添加序列标记" title="在播放头添加标记 (M)">+M</button>
              <button type="button" disabled={!selectedMarkerId} onClick={deleteSelectedMarker} className="h-6 border border-[#4a4440] bg-[#2a2826] px-1 text-[8px] text-[#a9a19a] hover:bg-[#473a31] disabled:opacity-30" aria-label="删除所选序列标记" title="删除所选标记">−M</button>
              <button type="button" onClick={() => addTrack("video")} className="h-6 border border-[#3d4d59] bg-[#252b31] px-1.5 text-[8px] font-semibold text-[#b9d7eb] hover:bg-[#304a5c]" aria-label="添加视频轨道" title="添加视频轨道">+V</button>
              <button type="button" onClick={() => addTrack("audio")} className="h-6 border border-[#3d5148] bg-[#252d29] px-1.5 text-[8px] font-semibold text-[#b8dfca] hover:bg-[#305141]" aria-label="添加音频轨道" title="添加音频轨道">+A</button>
              <button type="button" disabled={!selectedMediaItem} onClick={() => selectedMediaItem && placeMediaItem(selectedMediaItem, "insert")} className="h-6 border border-[#3e4f5e] bg-[#263744] px-1.5 text-[8px] text-[#bcd9ee] hover:bg-[#315f83] disabled:opacity-35" aria-label="时间线插入编辑" title="插入所选素材 (,)">插入 ,</button>
              <button type="button" disabled={!selectedMediaItem} onClick={() => selectedMediaItem && placeMediaItem(selectedMediaItem, "overwrite")} className="h-6 border border-[#594d3a] bg-[#3a3023] px-1.5 text-[8px] text-[#ead5b3] hover:bg-[#6b5330] disabled:opacity-35" aria-label="时间线覆盖编辑" title="覆盖所选素材 (.)">覆盖 .</button>
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
            data-trim-mode={trimMode}
            data-snapping-enabled={snappingEnabled ? "true" : "false"}
            data-selected-clip-count={selectedClipIds.size}
            data-snap-guide-frame={snapGuideFrame ?? ""}
            data-current-frame={currentFrame}
            className="relative min-h-0 w-full min-w-0 flex-1 overflow-auto bg-[#15171a]"
            onPointerDown={handleTimelineBackgroundDown}
          >
            <div className="relative min-h-full" style={{ width: TRACK_LABEL_WIDTH + timelineWidth }}>
            <div data-openreel-timeline-ruler="true" className="sticky top-0 z-20 grid h-7 border-b border-[#353941] bg-[#1c1f23]" style={{ gridTemplateColumns: `${TRACK_LABEL_WIDTH}px ${timelineWidth}px` }}>
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
                {markers.map((marker) => (
                  <button
                    key={marker.id}
                    type="button"
                    data-openreel-sequence-marker="true"
                    data-marker-id={marker.id}
                    data-marker-frame={marker.frame}
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={(event) => {
                      event.stopPropagation()
                      clearTimelineSelection()
                      setSelectedMarkerId(marker.id)
                      seekTo(frameToTime(marker.frame))
                    }}
                    className={cn(
                      "absolute top-0 z-30 h-7 w-3 -translate-x-1/2 text-[#d4a84f]",
                      selectedMarkerId === marker.id && "text-[#ffd47a] drop-shadow-[0_0_4px_rgba(255,194,83,.8)]",
                    )}
                    style={{ left: marker.frame / framesPerSecond * pxPerSecond }}
                    title={`${marker.label} · ${formatFrameTimecode(marker.frame, framesPerSecond)}`}
                    aria-label={`序列标记 ${marker.label}`}
                  >
                    <span className="mx-auto block h-0 w-0 border-l-[5px] border-r-[5px] border-t-[7px] border-l-transparent border-r-transparent border-t-current" />
                    <span className="mx-auto block h-4 w-px bg-current" />
                  </button>
                ))}
              </div>
            </div>

            {videoTracks.map(renderTimelineTrack)}
            {audioTracks.map(renderTimelineTrack)}

            {snapGuideFrame != null && (
              <div
                data-openreel-snap-guide="true"
                className="pointer-events-none absolute bottom-0 top-0 z-40 w-px bg-[#48c7ff] shadow-[0_0_6px_rgba(72,199,255,.75)]"
                style={{ left: TRACK_LABEL_WIDTH + snapGuideFrame / framesPerSecond * pxPerSecond }}
              />
            )}
            {marquee && (
              <div
                data-openreel-marquee="true"
                className="pointer-events-none absolute z-50 border border-[#75b9e8] bg-[#4b96c8]/15"
                style={marquee}
              />
            )}

            <div
              ref={playheadRef}
              data-openreel-playhead="true"
              className="absolute bottom-0 top-0 z-50 w-3 -translate-x-1/2 touch-none cursor-ew-resize"
              style={{ left: TRACK_LABEL_WIDTH + currentTime * pxPerSecond }}
              onPointerDown={beginPlayheadDrag}
            >
              <div className="pointer-events-none absolute bottom-0 left-1/2 top-0 w-px -translate-x-1/2 bg-[#ff4d4f] shadow-[0_0_0_1px_rgba(255,77,79,.18)]" />
              <div className="pointer-events-none absolute left-1/2 top-0 h-0 w-0 -translate-x-1/2 border-l-[4px] border-r-[4px] border-t-[7px] border-l-transparent border-r-transparent border-t-[#ff4d4f]" />
            </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
