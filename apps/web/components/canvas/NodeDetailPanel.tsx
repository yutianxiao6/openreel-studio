"use client"

import { useEffect, useRef, useState, type CSSProperties, type MouseEvent } from "react"
import { motion } from "framer-motion"
import {
  callTool,
  getRuntimeConfigFile,
  getProjectNodeDetails,
  listProjectAssets,
  resolveMediaUrl,
  switchProjectNodeHistory,
  updateProjectNodeDetails,
  uploadFile,
} from "@/lib/api"
import {
  VIDEO_MODEL_OPTIONS,
  VIDEO_RESOLUTION_OPTIONS,
  defaultVideoResolutionForModel,
  videoSupportedResolutionsForModel,
} from "@/lib/videoModelOptions"
import { MarkdownView } from "@/components/common/MarkdownView"
import { useCanvasStore } from "@/stores/canvasStore"
import { useChatStore } from "@/stores/chatStore"
import { useProjectStore } from "@/stores/projectStore"
import { getNodeStyle } from "./nodeStyles"
import type { StageData } from "./SmartNode"

interface NodeFull {
  id: string
  type: string
  title: string
  status: string
  version?: number
  prompt?: string | null
  input?: unknown
  output?: unknown
  render_state?: string | null
  error_message?: string | null
  supersedes_id?: string | null
  position?: { x: number; y: number }
  creator?: "user" | "agent" | string | null
  changes?: Array<{ field: string; label: string; before: string; after: string }>
  created_at?: string
  updated_at?: string
}

interface EditableNodeDraft {
  title: string
  content: string
  prompt: string
  model: string
  style: string
  voice: string
  speed: string
  instructions: string
  format: string
  negative_tags: string
  aspect_ratio: string
  resolution: string
  quality: string
  duration_seconds: string
  instrumental: boolean
  custom_mode: boolean
  reference_images: string[]
}

interface AudioProviderOption {
  kind: string
  name: string
  model_name: string
  api_format: string
  is_active?: boolean
  enabled?: boolean
}

type AudioProviderMode = "tts" | "music" | "unknown"

const EDITABLE_NODE_TYPES = new Set(["text", "image", "video", "audio"])

const EMPTY_DRAFT: EditableNodeDraft = {
  title: "",
  content: "",
  prompt: "",
  model: "",
  style: "",
  voice: "",
  speed: "",
  instructions: "",
  format: "",
  negative_tags: "",
  aspect_ratio: "",
  resolution: "",
  quality: "",
  duration_seconds: "",
  instrumental: true,
  custom_mode: false,
  reference_images: [],
}

interface Props {
  nodeId: string
  projectId?: string | null
  onClose: () => void
  onRerun?: (nodeId: string) => void | Promise<void>
  onDelete?: (nodeId: string) => void | Promise<void>
  onSaved?: (node: NodeFull) => void | Promise<void>
  onRequestStoryRevision?: (nodeId: string) => void | Promise<void>
  actionDisabled?: boolean
  presentation?: "drawer" | "modal"
}

const STATUS_LABELS: Record<string, { label: string; cls: string }> = {
  idle: { label: "待运行", cls: "bg-gray-800 text-gray-400" },
  queued: { label: "排队中", cls: "bg-gray-800 text-gray-400" },
  running: { label: "运行中", cls: "bg-blue-900/40 text-blue-300" },
  completed: { label: "已完成", cls: "bg-green-900/40 text-green-300" },
  failed: { label: "失败", cls: "bg-red-900/40 text-red-300" },
  waiting_confirm: { label: "待确认", cls: "bg-yellow-900/40 text-yellow-300" },
}

function Lightbox({ src, alt, onClose }: { src: string; alt?: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])
  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/90 backdrop-blur-sm"
      onClick={onClose}
    >
      <img
        src={src}
        alt={alt ?? ""}
        className="max-w-[94vw] max-h-[94vh] object-contain rounded shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      />
      <button
        onClick={onClose}
        className="absolute top-4 right-4 px-3 py-1.5 bg-gray-800/80 hover:bg-gray-700 text-gray-200 rounded text-xs"
      >
        关闭 (Esc)
      </button>
    </div>
  )
}

interface MediaItem {
  kind: "image" | "video" | "audio"
  src: string
  poster?: string
  label?: string
  caption?: string
  prompt?: string
  width?: number
  height?: number
}

interface MediaHistoryEntry {
  id: string
  index: number
  created_at?: string
  type?: string
  prompt?: string
  output: unknown
  media: MediaItem[]
}

interface ImageGridCell {
  cell_id: string
  index?: number
  row?: number
  col?: number
  title?: string
  url?: string
  local_url?: string
  local_path?: string
  width?: number
  height?: number
}

interface ImageGridOutput {
  type: "image_grid"
  operation?: string
  grid?: { rows?: number; cols?: number }
  cells?: ImageGridCell[]
  url?: string
  local_url?: string
  composite_url?: string
  width?: number
  height?: number
}

interface InpaintPoint {
  x: number
  y: number
}

interface InpaintStroke {
  id: string
  brushSize: number
  points: InpaintPoint[]
}

interface ReferenceItem {
  kind: "url" | "file" | "reference" | "node" | "asset" | "text"
  value: string
  label: string
}

function asObj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function pickUrl(o: Record<string, unknown> | null): string | null {
  if (!o) return null
  const u = o.local_url || o.url || o.remote_url
  if (typeof u === "string" && u) return u
  return null
}

function isVideoSource(value: unknown): value is string {
  return typeof value === "string" && /\.(mp4|webm|mov)(\?|#|$)/i.test(value)
}

function isAudioSource(value: unknown): value is string {
  return typeof value === "string" && /\.(mp3|wav|m4a|aac|ogg|flac)(\?|#|$)/i.test(value)
}

function videoMimeType(src: string): string {
  const path = src.split(/[?#]/, 1)[0]?.toLowerCase() || ""
  if (path.endsWith(".webm")) return "video/webm"
  if (path.endsWith(".mov")) return "video/quicktime"
  return "video/mp4"
}

function audioMimeType(src: string): string {
  const path = src.split(/[?#]/, 1)[0]?.toLowerCase() || ""
  if (path.endsWith(".wav")) return "audio/wav"
  if (path.endsWith(".m4a")) return "audio/mp4"
  if (path.endsWith(".aac")) return "audio/aac"
  if (path.endsWith(".ogg")) return "audio/ogg"
  if (path.endsWith(".flac")) return "audio/flac"
  return "audio/mpeg"
}

function numericDimension(value: unknown): number | undefined {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? Math.round(n) : undefined
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function pointDistance(a: InpaintPoint, b: InpaintPoint): number {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

function strokePath(stroke: InpaintStroke): string {
  const points = stroke.points
  if (points.length === 0) return ""
  const first = points[0]
  if (points.length === 1) {
    return `M ${first.x * 1000} ${first.y * 1000} L ${first.x * 1000 + 0.1} ${first.y * 1000}`
  }
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x * 1000} ${point.y * 1000}`)
    .join(" ")
}

function normalizedBrushSize(brushSize: number, rect?: DOMRect | null): number {
  const shortSide = rect ? Math.min(rect.width, rect.height) : 720
  if (!Number.isFinite(shortSide) || shortSide <= 0) return 0.04
  return Number(Math.max(0.005, Math.min(0.2, brushSize / shortSide)).toFixed(4))
}

function imageGridFromOutput(output: unknown): ImageGridOutput | null {
  const obj = asObj(parseJson(output))
  if (!obj || obj.type !== "image_grid") return null
  const cells = Array.isArray(obj.cells)
    ? obj.cells.filter((item): item is ImageGridCell => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : []
  const grid = asObj(obj.grid) || {}
  return {
    type: "image_grid",
    operation: typeof obj.operation === "string" ? obj.operation : undefined,
    grid: {
      rows: typeof grid.rows === "number" ? grid.rows : Number(grid.rows || 0) || undefined,
      cols: typeof grid.cols === "number" ? grid.cols : Number(grid.cols || 0) || undefined,
    },
    cells,
    url: typeof obj.url === "string" ? obj.url : undefined,
    local_url: typeof obj.local_url === "string" ? obj.local_url : undefined,
    composite_url: typeof obj.composite_url === "string" ? obj.composite_url : undefined,
    width: numericDimension(obj.width),
    height: numericDimension(obj.height),
  }
}

function pickReferenceUrl(ref: unknown): string {
  const obj = asObj(ref)
  if (!obj) return ""
  const url = obj.local_url || obj.url || obj.remote_url
  return typeof url === "string" && url ? resolveMediaUrl(url) : ""
}

function referenceFileUrl(projectId: string | null | undefined, value: string): string {
  if (!projectId || !value) return ""
  if (/^(https?:|\/api\/|data:)/.test(value)) return resolveMediaUrl(value)
  if (value.startsWith("uploads/")) return resolveMediaUrl(`/api/uploads/${projectId}/file/${value}`)
  if (value.startsWith("generated_images/")) {
    return resolveMediaUrl(`/api/media/${projectId}/${value.replace(/^generated_images\//, "")}`)
  }
  if (value.startsWith("generated_audio/")) {
    return resolveMediaUrl(`/api/media/${projectId}/${value}`)
  }
  return ""
}

function normalizeReferenceValue(text: string, label: string, refId?: string): ReferenceItem | null {
  const value = text.trim()
  if (!value) return null
  if (/^(https?:|data:)/.test(value)) return { kind: "url", value, label }
  if (value.startsWith("/api/")) return { kind: "url", value, label }
  if (value.startsWith("/") && refId) return { kind: "reference", value: refId, label }
  if (value.startsWith("/")) return { kind: "url", value, label }
  if (value.startsWith("node:")) return { kind: "node", value: value.slice(5), label: "节点引用" }
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    return { kind: "node", value, label: "节点引用" }
  }
  if (value.startsWith("asset:")) return { kind: "asset", value: value.slice(6), label: "资产引用" }
  if (value.startsWith("uploads/") || value.startsWith("generated_images/") || value.startsWith("generated_audio/")) return { kind: "file", value, label }
  if (refId) return { kind: "reference", value: refId, label }
  return { kind: "text", value, label: "视觉锚点" }
}

function referenceIdentity(ref: ReferenceItem, projectId?: string | null): string {
  const value = ref.value.trim()
  if (!value) return ""
  if (ref.kind === "url") return `url:${resolveMediaUrl(value) || value}`
  if (ref.kind === "file") return `file:${referenceFileUrl(projectId, value) || value}`
  return `${ref.kind}:${value}`
}

function uniqueReferenceItems(items: ReferenceItem[], projectId?: string | null): ReferenceItem[] {
  const seen = new Set<string>()
  const unique: ReferenceItem[] = []
  for (const item of items) {
    const key = referenceIdentity(item, projectId)
    if (!key || seen.has(key)) continue
    seen.add(key)
    unique.push(item)
  }
  return unique
}

function normalizeReference(ref: unknown): ReferenceItem | null {
  if (typeof ref === "string") {
    return normalizeReferenceValue(ref, "引用图")
  }
  const obj = asObj(ref)
  if (!obj) return null
  const directUrl = pickReferenceUrl(ref)
  const label = String(obj.mention || obj.label || obj.name || obj.role || obj.type || "引用图")
  if (directUrl) return { kind: "url", value: directUrl, label }
  const refId = typeof obj.ref_id === "string" && obj.ref_id ? obj.ref_id : undefined
  const refValue = obj.ref || obj.reference || obj.reference_input || obj.rel_path || obj.path || obj.source_path
  if (typeof refValue === "string" && refValue) {
    const normalized = normalizeReferenceValue(refValue, label, refId)
    if (normalized) return normalized
  }
  const nodeId = obj.node_id || obj.nodeId || obj.source_node_id || obj.sourceNodeId
  if (typeof nodeId === "string" && nodeId) return { kind: "node", value: nodeId, label }
  const assetId = obj.asset_id || obj.assetId
  if (typeof assetId === "string" && assetId) return { kind: "asset", value: assetId, label }
  if (refId) return { kind: "reference", value: refId, label }
  const text = obj.description || obj.prompt || obj.id
  return typeof text === "string" && text ? { kind: "text", value: text, label } : null
}

function isVisualReferenceRole(ref: unknown): boolean {
  const obj = asObj(ref)
  if (!obj) return true
  const kind = String(obj.kind || "")
    .trim()
    .toLowerCase()
    .replace(/-/g, "_")
  if (kind.includes("text") || kind.includes("script") || kind.includes("context")) {
    return false
  }
  const role = String(obj.role || obj.usage || obj.purpose || "")
    .trim()
    .toLowerCase()
    .replace(/-/g, "_")
  if (!role) return true
  if (role.includes("text") || role.includes("script") || role.includes("context") || role.includes("brief")) {
    return false
  }
  return (
    role.includes("image") ||
    role.includes("visual") ||
    role.includes("frame") ||
    role.includes("style") ||
    role.includes("storyboard") ||
    role === "reference"
  )
}

interface VideoLightboxState {
  src: string
  poster?: string
  title?: string
}

function VideoLightbox({ src, poster, title, onClose }: VideoLightboxState & { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/92 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative w-[min(96vw,1180px)]"
        onClick={(e) => e.stopPropagation()}
      >
        <video
          poster={poster}
          controls
          autoPlay
          playsInline
          className="max-h-[86vh] w-full rounded-lg bg-black shadow-2xl"
        >
          <source src={src} type={videoMimeType(src)} />
        </video>
        <div className="mt-2 flex items-center justify-between gap-3 text-xs text-zinc-400">
          <span className="truncate">{title || "视频预览"}</span>
          <button
            onClick={onClose}
            className="rounded-md border border-white/10 bg-white/[0.06] px-3 py-1.5 text-zinc-100 transition hover:bg-white/[0.12]"
          >
            关闭 (Esc)
          </button>
        </div>
      </div>
    </div>
  )
}

function InlineVideoPreview({
  src,
  poster,
  title,
  className,
  onOpen,
}: {
  src: string
  poster?: string
  title?: string
  className: string
  onOpen: () => void
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [playing, setPlaying] = useState(false)

  useEffect(() => {
    setPlaying(false)
  }, [src])

  const togglePlayback = (event?: MouseEvent<HTMLButtonElement | HTMLVideoElement>) => {
    event?.preventDefault()
    event?.stopPropagation()
    const player = videoRef.current
    if (!player) return
    if (player.paused || player.ended) {
      void player.play()
        .then(() => setPlaying(true))
        .catch((error) => console.warn("Failed to play video preview", error))
      return
    }
    player.pause()
    setPlaying(false)
  }

  return (
    <div className="group/video relative overflow-hidden rounded-lg bg-black ring-1 ring-white/[0.08]">
      <video
        ref={videoRef}
        poster={poster}
        controls={false}
        disablePictureInPicture
        controlsList="nodownload nofullscreen noremoteplayback"
        playsInline
        preload="metadata"
        className={className}
        draggable={false}
        onClick={togglePlayback}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
      >
        <source src={src} type={videoMimeType(src)} />
      </video>
      <button
        type="button"
        aria-label={playing ? "暂停视频预览" : "播放视频预览"}
        onClick={togglePlayback}
        onPointerDown={(event) => event.stopPropagation()}
        className={`absolute left-1/2 top-1/2 z-20 flex h-12 w-12 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-white/18 bg-black/70 text-white shadow-2xl shadow-black/35 backdrop-blur transition hover:scale-105 hover:bg-black/85 ${playing ? "md:opacity-0 md:group-hover/video:opacity-100" : ""}`}
      >
        {playing ? (
          <span className="flex items-center gap-1">
            <span className="h-4 w-1.5 rounded-sm bg-white" />
            <span className="h-4 w-1.5 rounded-sm bg-white" />
          </span>
        ) : (
          <span className="ml-0.5 h-0 w-0 border-y-[9px] border-l-[14px] border-y-transparent border-l-white" />
        )}
      </button>
      <div className="absolute right-2 top-2 z-20">
        <button
          type="button"
          onClick={(event) => {
            event.preventDefault()
            event.stopPropagation()
            onOpen()
          }}
          onPointerDown={(event) => event.stopPropagation()}
          className="rounded-md border border-white/10 bg-black/70 px-2.5 py-1.5 text-[11px] font-semibold text-zinc-100 shadow-xl shadow-black/30 backdrop-blur transition hover:bg-black/85"
        >
          放大
        </button>
      </div>
      {title && <div className="sr-only">{title}</div>}
    </div>
  )
}

function InlineAudioPreview({
  src,
  title,
}: {
  src: string
  title?: string
}) {
  return (
    <div className="rounded-lg border border-white/[0.08] bg-black/35 p-3">
      <audio controls preload="metadata" className="w-full">
        <source src={src} type={audioMimeType(src)} />
      </audio>
      {title && <div className="mt-2 truncate text-[11px] text-zinc-500">{title}</div>}
    </div>
  )
}

/** Walk arbitrary output JSON and pick out images / videos / audio / fusion stages. */
function collectMedia(output: unknown): MediaItem[] {
  if (!output) return []
  const items: MediaItem[] = []
  const seen = new Set<string>()

  const pushImage = (src: string | null, label?: string, prompt?: string, width?: unknown, height?: unknown) => {
    if (!src) return
    const resolved = resolveMediaUrl(src)
    if (!resolved || seen.has(resolved)) return
    seen.add(resolved)
    items.push({ kind: "image", src: resolved, label, prompt, width: numericDimension(width), height: numericDimension(height) })
  }
  const pushVideo = (
    src: string | null,
    poster?: string,
    label?: string,
    prompt?: string,
    width?: unknown,
    height?: unknown,
  ) => {
    if (!src) return
    const resolved = resolveMediaUrl(src)
    if (!resolved || seen.has(resolved)) return
    seen.add(resolved)
    items.push({
      kind: "video",
      src: resolved,
      poster: poster ? resolveMediaUrl(poster) : undefined,
      label,
      prompt,
      width: numericDimension(width),
      height: numericDimension(height),
    })
  }
  const pushAudio = (src: string | null, label?: string, prompt?: string) => {
    if (!src) return
    const resolved = resolveMediaUrl(src)
    if (!resolved || seen.has(resolved)) return
    seen.add(resolved)
    items.push({ kind: "audio", src: resolved, label, prompt })
  }

  const obj = asObj(output)

  // fusion node — multi-stage payload
  if (obj && obj.type === "fusion" && Array.isArray(obj.stages)) {
    for (const s of obj.stages as StageData[]) {
      const src = s.local_url || s.url || s.remote_url || null
      if (!src) continue
      // crude detection: first/last frame & video-stage names
      const isVideo = /视频|video|clip/i.test(s.name) && typeof src === "string"
      const isAudio = /音频|audio|sound/i.test(s.name) && typeof src === "string"
      if (isVideo) pushVideo(src, undefined, s.name, s.prompt, s.width, s.height)
      else if (isAudio) pushAudio(src, s.name, s.prompt)
      else pushImage(src, s.name, s.prompt, s.width, s.height)
    }
    return items
  }

  if (!obj) return items

  // Direct image-shaped payload
  if (obj.type === "image") {
    pushImage(pickUrl(obj), "图片", undefined, obj.width, obj.height)
  }
  // Nested image
  const image = asObj(obj.image)
  if (image) pushImage(pickUrl(image), "图片", undefined, image.width, image.height)

  // Standalone url field
  if (typeof obj.url === "string" && /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(obj.url)) {
    pushImage(obj.url as string, "图片", undefined, obj.width, obj.height)
  }
  if (typeof obj.local_url === "string" && /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(obj.local_url)) {
    pushImage(obj.local_url as string, "图片", undefined, obj.width, obj.height)
  }

  // First / last frames
  const frames = asObj(obj.frames)
  if (frames) {
    const ff = asObj(frames.first) || (typeof frames.first === "string" ? { url: frames.first } : null)
    const lf = asObj(frames.last) || (typeof frames.last === "string" ? { url: frames.last } : null)
    pushImage(pickUrl(ff), "首帧")
    pushImage(pickUrl(lf), "尾帧")
  }
  const ff = asObj(obj.first_frame)
  if (ff) pushImage(pickUrl(ff), "首帧")
  const lf = asObj(obj.last_frame)
  if (lf) pushImage(pickUrl(lf), "尾帧")

  // Video
  const video = asObj(obj.video)
  if (video) {
    pushVideo(
      pickUrl(video),
      typeof video.poster === "string" ? video.poster : undefined,
      "视频",
      typeof video.prompt === "string" ? video.prompt : undefined,
      video.width,
      video.height,
    )
  }
  if (obj.type === "video") {
    pushVideo(
      pickUrl(obj),
      typeof obj.poster === "string" ? obj.poster : undefined,
      "视频",
      undefined,
      obj.width,
      obj.height,
    )
  }
  if (typeof obj.url === "string" && /\.(mp4|webm|mov)$/i.test(obj.url)) {
    pushVideo(obj.url as string, undefined, "视频")
  }
  const audio = asObj(obj.audio)
  if (audio) {
    pushAudio(
      pickUrl(audio),
      "音频",
      typeof audio.prompt === "string" ? audio.prompt : undefined,
    )
  }
  if (obj.type === "audio") {
    pushAudio(pickUrl(obj), "音频", typeof obj.prompt === "string" ? obj.prompt : undefined)
  }
  if (isAudioSource(obj.url)) {
    pushAudio(obj.url, "音频")
  }
  if (isAudioSource(obj.local_url)) {
    pushAudio(obj.local_url, "音频")
  }

  return items
}

function mediaHistoryEntriesFromOutput(output: unknown, kind: "image" | "video" | "audio"): MediaHistoryEntry[] {
  const obj = asObj(parseJson(output))
  const raw = obj?.history ?? obj?.media_history
  if (!Array.isArray(raw)) return []
  return raw
    .map((item, index): MediaHistoryEntry | null => {
      const entry = asObj(item)
      const entryOutput = entry && Object.prototype.hasOwnProperty.call(entry, "output")
        ? entry.output
        : item
      if (!isSuccessfulMediaHistoryOutput(entryOutput)) return null
      const media = collectMedia(entryOutput).filter((mediaItem) => mediaItem.kind === kind)
      if (media.length === 0) return null
      const id = typeof entry?.id === "string" && entry.id.trim()
        ? entry.id.trim()
        : `history-${index}`
      const outputObj = asObj(parseJson(entryOutput)) || {}
      const inputObj = asObj(entry?.input) || {}
      const prompt = typeof entry?.prompt === "string" && entry.prompt.trim()
        ? entry.prompt.trim()
        : pickPromptText("", inputObj, outputObj)
      return {
        id,
        index,
        created_at: typeof entry?.created_at === "string" ? entry.created_at : undefined,
        type: typeof entry?.type === "string" ? entry.type : undefined,
        prompt,
        output: entryOutput,
        media,
      }
    })
    .filter((item): item is MediaHistoryEntry => Boolean(item))
}

function isSuccessfulMediaHistoryOutput(output: unknown): boolean {
  const obj = asObj(parseJson(output))
  if (!obj) return collectMedia(output).length > 0
  if (obj.ok === false) return false
  const status = typeof obj.status === "string" ? obj.status.trim().toLowerCase() : ""
  if (["failed", "error", "cancelled", "canceled", "queued", "running"].includes(status)) return false
  if (typeof obj.error === "string" && obj.error.trim()) return false
  if (typeof obj.error_message === "string" && obj.error_message.trim()) return false
  if (obj.type === "fusion" && Array.isArray(obj.stages)) {
    let mediaStages = 0
    for (const stage of obj.stages) {
      const stageObj = asObj(stage)
      if (!stageObj || collectMedia(stageObj).length === 0) continue
      mediaStages += 1
      const stageStatus = typeof stageObj.status === "string" ? stageObj.status.trim().toLowerCase() : ""
      if (stageStatus && !["completed", "success", "succeeded", "done"].includes(stageStatus)) return false
      if (typeof stageObj.error === "string" && stageObj.error.trim()) return false
      if (typeof stageObj.error_message === "string" && stageObj.error_message.trim()) return false
    }
    return mediaStages > 0
  }
  return collectMedia(obj).length > 0
}

function formatHistoryTime(value?: string): string {
  if (!value) return ""
  const time = new Date(value)
  if (Number.isNaN(time.getTime())) return value
  return time.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}

const STORY_REVISION_NODE_TYPES = new Set(["text"])

const MEDIA_RERUN_NODE_TYPES = new Set(["image", "video", "audio"])

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="overflow-hidden rounded-lg border border-white/[0.08] bg-[#151923]/88 shadow-[0_18px_42px_rgba(0,0,0,0.24)]">
      <div className="border-b border-white/[0.07] bg-white/[0.025] px-4 py-2.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-400">
        {title}
      </div>
      <div className="p-3.5">{children}</div>
    </section>
  )
}

function PromptBlock({ children }: { children: string }) {
  return (
    <div className="max-h-[320px] overflow-y-auto rounded-lg bg-black/30 px-3.5 py-3 text-[13px] leading-6 text-zinc-200 shadow-inner shadow-black/25">
      <MarkdownView compact>{children}</MarkdownView>
    </div>
  )
}

function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? null, null, 2)
  } catch {
    return String(value)
  }
}

const FIELD_LABELS: Record<string, string> = {
  title: "标题",
  content: "内容",
  description: "描述",
  resolution: "分辨率",
  quality: "质量",
  aspect_ratio: "画幅",
  duration: "时长",
  duration_seconds: "时长",
  style: "风格",
  voice: "声音",
  speed: "语速",
  instructions: "TTS 指令",
  format: "格式",
  instrumental: "纯音乐",
  custom_mode: "高级模式",
  customMode: "高级模式",
  negative_tags: "负面标签",
  negativeTags: "负面标签",
  production_path: "制作方式",
  prompt_template: "提示词模板",
  references: "参考",
  depends_on: "依赖",
  blueprint_node_id: "蓝图节点",
  blueprint_node_type: "蓝图类型",
}

function formatFieldValue(value: unknown): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  if (Array.isArray(value)) return value.map((item) => formatFieldValue(item)).filter(Boolean).join("、")
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>
    for (const key of ["title", "name", "summary", "description", "content", "prompt"]) {
      const text = obj[key]
      if (typeof text === "string" && text.trim()) return text.trim()
    }
    return Object.entries(obj)
      .slice(0, 4)
      .map(([key, item]) => `${FIELD_LABELS[key] || key}: ${formatFieldValue(item)}`)
      .filter(Boolean)
      .join("；")
  }
  return ""
}

function FieldRows({ value }: { value: Record<string, unknown> }) {
  const rows = Object.entries(value)
    .map(([key, item]) => [FIELD_LABELS[key] || key, formatFieldValue(item)] as const)
    .filter(([, item]) => item.trim())
  if (rows.length === 0) return null
  return (
    <dl className="overflow-hidden rounded-lg bg-black/25 ring-1 ring-white/[0.06]">
      {rows.map(([label, item]) => (
        <div key={label} className="grid gap-1 border-b border-white/[0.06] px-3.5 py-2.5 text-[13px] last:border-b-0 sm:grid-cols-[104px_minmax(0,1fr)]">
          <dt className="text-zinc-500">{label}</dt>
          <dd className="whitespace-pre-wrap break-words leading-5 text-zinc-200">{item}</dd>
        </div>
      ))}
    </dl>
  )
}

function NodeDebugSection({ node }: { node: NodeFull }) {
  const [open, setOpen] = useState(false)
  return (
    <section className="rounded-lg border border-dashed border-amber-500/25 bg-amber-950/10">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-[12px] font-medium text-amber-100/90 hover:bg-amber-500/10"
      >
        <span>开发原始数据</span>
        <span className="text-[11px] text-amber-200/60">{open ? "收起" : "展开"}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-amber-500/15 p-3">
          <div className="grid gap-2 text-[11px] text-amber-100/70 sm:grid-cols-2">
            <span>节点 ID: {node.id}</span>
            {node.version != null && <span>版本: {node.version}</span>}
            {node.created_at && <span>创建: {node.created_at}</span>}
            {node.updated_at && <span>更新: {node.updated_at}</span>}
          </div>
          {[
            ["input", node.input],
            ["output", node.output],
            ["prompt", node.prompt],
          ].map(([label, value]) => (
            <div key={String(label)}>
              <div className="mb-1 text-[11px] uppercase tracking-wide text-amber-200/70">{String(label)}</div>
              <pre className="max-h-72 overflow-auto rounded-md border border-amber-500/15 bg-black/45 p-2 text-[11px] leading-5 text-amber-50/80">
                {prettyJson(value)}
              </pre>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/**
 * 12 类节点的人性化字段提取器。
 * 共同输入:已 parse 的 input/output 对象
 * 输出:一个用于 <Section> 排版的 JSX 树
 */
function parseJson(v: unknown): unknown {
  if (typeof v === "string") {
    try { return JSON.parse(v) } catch { return v }
  }
  return v
}

function errorTextFromUnknown(value: unknown): string {
  const parsed = parseJson(value)
  const obj = asObj(parsed)
  if (!obj) return ""
  const parts: string[] = []
  for (const key of ["error_message", "error", "provider_msg", "message", "detail"]) {
    const text = obj[key]
    if (typeof text === "string" && text.trim()) parts.push(text.trim())
  }
  const detail = asObj(obj.error_detail)
  if (detail) {
    for (const key of ["error", "provider_msg", "error_kind", "endpoint"]) {
      const text = detail[key]
      if (typeof text === "string" && text.trim()) parts.push(text.trim())
    }
  }
  const feedback = asObj(obj.model_feedback)
  if (feedback) {
    for (const key of ["what_went_wrong", "how_to_fix", "retry_policy"]) {
      const text = feedback[key]
      if (typeof text === "string" && text.trim()) parts.push(text.trim())
    }
  }
  const result = asObj(obj.result)
  if (result) {
    const nested = errorTextFromUnknown(result)
    if (nested) parts.push(nested)
  }
  if (Array.isArray(obj.stages)) {
    for (const stage of obj.stages) {
      const item = asObj(stage)
      const text = item && item.status === "failed" ? errorTextFromUnknown(item) : ""
      if (text) parts.push(text)
    }
  }
  return Array.from(new Set(parts)).join("\n")
}

function nodeDisplayError(node: NodeFull): string {
  const direct = typeof node.error_message === "string" ? node.error_message.trim() : ""
  return direct || errorTextFromUnknown(node.output)
}

/**
 * 抽出生图节点的"规格元信息"(分辨率/比例/画质/模型/provider)。
 * 优先级:output(实际出图后的真值)→ input(用户/Agent 创建时填的预设值)→ ""
 * 这样**节点还没 render 时**也能看到预设规格,render 之后再覆盖为真实值。
 */
function pickMediaSpec(
  output: Record<string, unknown>,
  input: Record<string, unknown>,
  imageStage?: Record<string, unknown>,
): {
  size: string
  aspect: string
  quality: string
  model: string
  provider: string
  downgraded: boolean
} {
  const pickFrom = (
    src: Record<string, unknown> | undefined,
    keys: string[],
  ): string => {
    if (!src) return ""
    for (const k of keys) {
      const v = src[k]
      if (typeof v === "string" && v) return v
    }
    return ""
  }
  // size 在 output 里可能叫 size 或 resolution
  const size =
    pickFrom(imageStage, ["size", "resolution"]) ||
    pickFrom(output, ["size", "resolution"]) ||
    pickFrom(input, ["resolution", "size"])
  const aspect =
    pickFrom(imageStage, ["aspect_ratio"]) ||
    pickFrom(output, ["aspect_ratio"]) ||
    pickFrom(input, ["aspect_ratio"])
  const quality =
    pickFrom(imageStage, ["quality"]) ||
    pickFrom(output, ["quality"]) ||
    pickFrom(input, ["quality"])
  const model =
    pickFrom(imageStage, ["model"]) ||
    pickFrom(output, ["model"]) ||
    pickFrom(input, ["model"])
  const provider =
    pickFrom(imageStage, ["provider"]) ||
    pickFrom(output, ["provider"])
  const downgraded = Boolean(
    (imageStage && imageStage.downgraded) || output.downgraded,
  )
  return { size, aspect, quality, model, provider, downgraded }
}

function MediaSpecBadges({
  spec,
  className = "",
}: {
  spec: ReturnType<typeof pickMediaSpec>
  className?: string
}) {
  const { size, aspect, quality, model, provider, downgraded } = spec
  if (!size && !aspect && !quality && !model && !provider) return null
  return (
    <div className={`flex flex-wrap gap-1.5 text-[11px] text-gray-400 ${className}`}>
      {size && <span className="px-1.5 py-0.5 rounded bg-black/30">分辨率 {size}</span>}
      {aspect && <span className="px-1.5 py-0.5 rounded bg-black/30">比例 {aspect}</span>}
      {quality && <span className="px-1.5 py-0.5 rounded bg-black/30">画质 {quality}</span>}
      {model && <span className="px-1.5 py-0.5 rounded bg-black/30">模型 {model}</span>}
      {provider && <span className="px-1.5 py-0.5 rounded bg-black/30">{provider}</span>}
      {downgraded && <span className="px-1.5 py-0.5 rounded bg-yellow-900/40 text-yellow-300">降级</span>}
    </div>
  )
}

function ImagePlaceholder({ label, busy = false }: { label: string; busy?: boolean }) {
  return (
    <div className="flex h-40 w-full items-center justify-center rounded-lg bg-black/30 text-[12px] text-zinc-500 ring-1 ring-white/[0.06]">
      <div className="flex flex-col items-center gap-2">
        {busy && <span className="h-5 w-5 rounded-full border-2 border-zinc-600 border-t-zinc-200 animate-spin" />}
        <span>{label}</span>
      </div>
    </div>
  )
}

function RefThumbnail({
  ref,
  projectId,
  setLightbox,
  compact = false,
}: {
  ref: ReferenceItem
  projectId?: string | null
  setLightbox: (v: { src: string; alt?: string } | null) => void
  compact?: boolean
}) {
  const token = `${ref.kind}:${ref.value}`
  // 异步把节点/资产引用解析到真实图片 url；失败时展示等待状态，不再调用不存在的 asset.get 工具。
  const [resolved, setResolved] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let cancelled = false
    setResolved(null)
    setFailed(false)
    ;(async () => {
      try {
        if (ref.kind === "url") {
          setResolved(resolveMediaUrl(ref.value) || ref.value)
          return
        }
        if (ref.kind === "file") {
          const url = referenceFileUrl(projectId, ref.value)
          if (url) setResolved(url)
          else setFailed(true)
          return
        }
        if (ref.kind === "reference") {
          if (!projectId) {
            setFailed(true)
            return
          }
          setResolved(resolveMediaUrl(`/api/uploads/${projectId}/reference/${ref.value}`))
          return
        }
        if (ref.kind === "text") {
          setFailed(true)
          return
        }
        if (ref.kind === "node") {
          let nodeId = ref.value
          if (projectId && nodeId && nodeId.length < 36) {
            const nodes = await callTool<Array<{ id?: string }>>("node.list", { project_id: projectId })
            if (cancelled) return
            if (Array.isArray(nodes)) {
              const matches = nodes
                .map((node) => String(node.id || ""))
                .filter((id) => id.startsWith(nodeId))
              if (matches.length === 1) nodeId = matches[0]
            }
          }
          const r = await callTool<Record<string, unknown>>("node.get", { project_id: projectId, node_id: nodeId })
          if (cancelled) return
          // output 可能是 fusion(stages 里挑图)、image、或直接顶层 url
          const out = r?.output as unknown
          let url: string | null = null
          const outObj = (out && typeof out === "object" && !Array.isArray(out))
            ? (out as Record<string, unknown>)
            : null
          if (outObj) {
            if (Array.isArray(outObj.stages)) {
              for (const s of outObj.stages as Record<string, unknown>[]) {
                const u = (s.local_url || s.url || s.remote_url) as string | undefined
                if (u) { url = u; break }
              }
            }
            if (!url) {
              const u = (outObj.local_url || outObj.url || outObj.remote_url) as string | undefined
              if (u) url = u
            }
          }
          if (url) setResolved(resolveMediaUrl(url) || url)
          else setFailed(true)
        } else if (ref.kind === "asset") {
          if (!projectId) {
            setFailed(true)
            return
          }
          const r = await listProjectAssets(projectId)
          if (cancelled) return
          const asset = r.assets.find((item) => item.id === ref.value)
          const u = asset?.url || asset?.path
          if (u) setResolved(resolveMediaUrl(u) || u)
          else setFailed(true)
        }
      } catch {
        if (!cancelled) setFailed(true)
      }
    })()
    return () => { cancelled = true }
  }, [ref.kind, ref.value, projectId])

  if (resolved) {
    return (
      <button
        onClick={() => setLightbox({ src: resolved, alt: ref.label })}
        className={compact
          ? "group relative block h-7 w-7 overflow-hidden rounded bg-black/40 ring-1 ring-white/[0.12] transition hover:ring-cyan-200/70"
          : "group relative block overflow-hidden rounded-lg bg-black/40 ring-1 ring-white/[0.08]"}
        title={token}
      >
        <img
          src={resolved}
          alt={ref.label}
          className={compact ? "h-full w-full object-cover" : "w-full h-20 object-cover"}
          onError={(e) => { (e.target as HTMLImageElement).style.opacity = "0.3" }}
        />
        {!compact && (
          <span className="absolute bottom-0 inset-x-0 bg-black/55 text-[10px] text-gray-100 px-1.5 py-0.5 truncate">
            {ref.label}
          </span>
        )}
      </button>
    )
  }
  if (compact) return null
  if (failed) {
    return (
      <span
        className="flex min-h-20 items-center rounded-lg bg-white/[0.03] px-2 py-1 text-[11px] text-gray-400 ring-1 ring-white/[0.08]"
        title={`${token} 暂无可预览图片`}
      >
        {ref.kind === "text" ? ref.value : "引用图等待产出"}
      </span>
    )
  }
  // loading
  return (
    <div className="flex h-20 items-center justify-center rounded-lg bg-black/40 ring-1 ring-white/[0.08]" title={token}>
      <div className="w-4 h-4 border-2 border-teal-400 border-t-transparent rounded-full animate-spin" />
    </div>
  )
}

function ReferenceThumbStrip({
  refs,
  projectId,
  setLightbox,
}: {
  refs: unknown[] | undefined
  projectId?: string | null
  setLightbox: (v: { src: string; alt?: string } | null) => void
}) {
  const normalized = uniqueReferenceItems((refs || [])
    .filter(isVisualReferenceRole)
    .map(normalizeReference)
    .filter((ref): ref is ReferenceItem => Boolean(ref && ref.kind !== "text")), projectId)
  if (normalized.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap gap-1.5" aria-label="引用图">
      {normalized.map((ref) => (
        <RefThumbnail
          key={referenceIdentity(ref, projectId)}
          ref={ref}
          projectId={projectId}
          setLightbox={setLightbox}
          compact
        />
      ))}
    </div>
  )
}

const TYPED_RENDERED_NODE_TYPES = new Set([
  "text",
  "image",
  "video",
  "audio",
  "character",
  "scene",
  "episode_script",
  "script_collection",
  "episode_segment_plan",
  "episode_cast_scene_plan",
  "segment_storyboard",
  "shot_first_frame",
  "shot_last_frame",
  "segment_story_template",
  "segment_video_prompt",
  "segment_video_clip",
])

function compactObject(value: Record<string, unknown>, excludedKeys: Set<string>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(value).filter(([key, item]) => item != null && item !== "" && !excludedKeys.has(key)),
  )
}

function pickPromptText(nodePrompt: string, input: Record<string, unknown>, output: Record<string, unknown>): string {
  const keys = ["prompt", "video_prompt", "image_prompt", "text", "content", "description", "summary"]
  for (const value of [nodePrompt, ...keys.map((key) => input[key]), ...keys.map((key) => output[key])]) {
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  return ""
}

function pickReadableText(input: Record<string, unknown>, output: Record<string, unknown>, nodePrompt = ""): string {
  const keys = ["content", "text", "summary", "description", "outline", "script", "prompt"]
  for (const value of [...keys.map((key) => output[key]), ...keys.map((key) => input[key]), nodePrompt]) {
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  return ""
}

function pickReferences(input: Record<string, unknown>, output: Record<string, unknown>): unknown[] | undefined {
  const inputFields = asObj(input.fields) || {}
  const values = [
    output.reference_images,
    output.references,
    output.reference_assets,
    input.reference_images,
    input.references,
    input.reference_assets,
    inputFields.reference_images,
    inputFields.references,
    inputFields.reference_assets,
  ]
  const refs: unknown[] = []
  const seen = new Set<string>()
  for (const value of values) {
    if (!Array.isArray(value) || value.length === 0) continue
    for (const item of value) {
      const key = typeof item === "string" ? item : JSON.stringify(item)
      if (seen.has(key)) continue
      seen.add(key)
      refs.push(item)
    }
  }
  return refs.length > 0 ? refs : undefined
}

function pickMediaInfo(input: Record<string, unknown>, output: Record<string, unknown>): Record<string, unknown> {
  const keys = [
    "duration",
    "duration_seconds",
    "aspect_ratio",
    "resolution",
    "size",
    "quality",
    "model",
    "provider",
    "style",
    "voice",
    "speed",
    "instructions",
    "format",
    "instrumental",
    "custom_mode",
    "customMode",
    "negative_tags",
    "negativeTags",
    "production_path",
  ]
  const entries = keys
    .map((key) => [key, output[key] ?? input[key]] as const)
    .filter(([, value]) => value != null && value !== "")
  return Object.fromEntries(entries)
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim()
    if (typeof value === "number" || typeof value === "boolean") return String(value)
  }
  return ""
}

function firstBool(defaultValue: boolean, ...values: unknown[]): boolean {
  for (const value of values) {
    if (typeof value === "boolean") return value
    if (typeof value === "number") return value !== 0
    if (typeof value !== "string") continue
    const text = value.trim().toLowerCase()
    if (["1", "true", "yes", "y", "on", "是"].includes(text)) return true
    if (["0", "false", "no", "n", "off", "否"].includes(text)) return false
  }
  return defaultValue
}

function audioProviderModeFromFormat(apiFormat?: string | null): AudioProviderMode {
  const format = String(apiFormat || "").trim().toLowerCase()
  if (["openai_tts", "tts", "openai_speech", "openai_audio_speech"].includes(format)) return "tts"
  if (["suno_compatible", "suno", "suno_api"].includes(format)) return "music"
  return "unknown"
}

function audioProviderTypeLabel(mode: AudioProviderMode): string {
  if (mode === "tts") return "TTS 语音"
  if (mode === "music") return "音乐生成"
  return "未知协议"
}

function resolveAudioProvider(
  value: string,
  providers: AudioProviderOption[],
): AudioProviderOption | undefined {
  const enabled = providers.filter((provider) => provider.enabled !== false)
  const selected = value.trim()
  if (selected) {
    return enabled.find((provider) => provider.name === selected || provider.model_name === selected)
  }
  return enabled.find((provider) => provider.is_active) || enabled[0]
}

function nodeInputFields(input: unknown): Record<string, unknown> {
  const inputObj = asObj(parseJson(input)) || {}
  const nestedFields = asObj(inputObj.fields)
  return nestedFields ? { ...inputObj, ...nestedFields } : inputObj
}

function normalizeRenderState(value: unknown): "stale" | "fresh" | string | undefined {
  if (typeof value !== "string") return undefined
  const text = value.trim()
  if (!text) return undefined
  if (["stale", "dirty", "outdated", "needs_render", "未更新"].includes(text)) return "stale"
  if (["fresh", "current", "latest", "最新"].includes(text)) return "fresh"
  return text
}

function renderStateFromNode(node?: NodeFull | null): string | undefined {
  if (!node || node.type !== "image") return undefined
  const direct = normalizeRenderState(node.render_state)
  if (direct) return direct
  const input = nodeInputFields(node.input)
  const fromInput = normalizeRenderState(input.render_state)
  if (fromInput) return fromInput
  return node.status === "completed" && node.output ? "fresh" : undefined
}

function stringArrayFromUnknown(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === "string") return item.trim()
      if (typeof item === "number" || typeof item === "boolean") return String(item)
      const obj = asObj(item)
      if (!obj) return ""
      const direct = obj.reference_input || obj.rel_path || obj.path || obj.source_path || obj.url || obj.local_url || obj.remote_url
      if (typeof direct === "string" && direct.trim()) return direct.trim()
      const ref = obj.ref || obj.reference
      if (typeof ref === "string" && ref.trim()) return ref.trim()
      const nodeId = obj.node_id || obj.nodeId || obj.source_node_id || obj.sourceNodeId
      if (typeof nodeId === "string" && nodeId.trim()) return `node:${nodeId.trim()}`
      const assetId = obj.asset_id || obj.assetId
      if (typeof assetId === "string" && assetId.trim()) return `asset:${assetId.trim()}`
      const id = obj.ref_id || obj.id
      return typeof id === "string" && id.trim() ? id.trim() : ""
    })
    .filter(Boolean)
}

function hasOwnKey(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function nodeRefIdFromUnknown(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    let text = String(value).trim()
    if (!text) return ""
    if (text.startsWith("@")) text = text.slice(1).trim()
    if (text.startsWith("node:")) return text.slice(5).trim()
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(text) ? text : ""
  }
  const obj = asObj(value)
  if (!obj) return ""
  for (const key of ["ref", "reference", "reference_input"]) {
    const ref = obj[key]
    const nodeId = nodeRefIdFromUnknown(ref)
    if (nodeId) return nodeId
  }
  for (const key of ["node_id", "nodeId", "source_node_id", "sourceNodeId"]) {
    const nodeId = obj[key]
    if (typeof nodeId === "string" && nodeId.trim()) return nodeId.trim()
  }
  return ""
}

function referenceListFromUnknown(value: unknown): unknown[] {
  if (Array.isArray(value)) return value
  return value == null || value === "" ? [] : [value]
}

function filterRemovedNodeReferences(value: unknown, removedNodeIds: Set<string>): unknown[] {
  return referenceListFromUnknown(value).filter((item) => {
    const nodeId = nodeRefIdFromUnknown(item)
    return !nodeId || !removedNodeIds.has(nodeId)
  })
}

function removeNodeReferencesFromContainer(
  container: Record<string, unknown>,
  removedNodeIds: Set<string>,
): { next: Record<string, unknown>; changed: boolean } {
  const next = { ...container }
  let changed = false
  for (const key of ["depends_on", "references"] as const) {
    if (!hasOwnKey(container, key)) continue
    const filtered = filterRemovedNodeReferences(container[key], removedNodeIds)
    if (JSON.stringify(filtered) === JSON.stringify(referenceListFromUnknown(container[key]))) continue
    next[key] = filtered
    changed = true
  }
  return { next, changed }
}

function normalizeVideoAspectRatio(value: string): string {
  return value === "9:16" ? "9:16" : "16:9"
}

function draftFromNode(node: NodeFull): EditableNodeDraft {
  const input = nodeInputFields(node.input)
  const output = asObj(parseJson(node.output)) || {}
  const nodePrompt = typeof node.prompt === "string" ? node.prompt : ""
  const inputReferenceImages = stringArrayFromUnknown(input.reference_images)
  const inputReferences = stringArrayFromUnknown(input.references)
  const referenceImages = (
    inputReferenceImages.length > 0
      ? inputReferenceImages
      : inputReferences.length > 0
        ? inputReferences
        : stringArrayFromUnknown(output.reference_images)
  )

  return {
    ...EMPTY_DRAFT,
    title: firstText(node.title, input.title, output.title),
    content: firstText(input.content, output.content, input.text, output.text, input.summary, output.summary),
    prompt: pickPromptText(nodePrompt, input, output),
    model: firstText(input.model, output.model),
    style: firstText(input.style, output.style),
    voice: firstText(input.voice, output.voice),
    speed: firstText(input.speed, output.speed),
    instructions: firstText(input.instructions, output.instructions),
    format: firstText(input.format, output.format),
    negative_tags: firstText(input.negative_tags, input.negativeTags, output.negative_tags, output.negativeTags),
    aspect_ratio: node.type === "video"
      ? normalizeVideoAspectRatio(firstText(input.aspect_ratio, output.aspect_ratio))
      : firstText(input.aspect_ratio, output.aspect_ratio) || (node.type === "image" ? "16:9" : ""),
    resolution: firstText(input.resolution, input.size, output.resolution, output.size)
      || (node.type === "image" ? "2560x1440" : node.type === "video" ? "720p" : ""),
    quality: firstText(input.quality, output.quality) || (node.type === "image" ? "high" : ""),
    duration_seconds: firstText(input.duration_seconds, input.duration, output.duration_seconds, output.duration) || (node.type === "video" ? "5" : ""),
    instrumental: firstBool(true, input.instrumental, output.instrumental),
    custom_mode: firstBool(false, input.custom_mode, input.customMode, output.custom_mode, output.customMode),
    reference_images: Array.from(new Set(referenceImages)),
  }
}

function payloadFromDraft(node: NodeFull, draft: EditableNodeDraft, audioMode: AudioProviderMode = "unknown"): {
  title: string
  prompt: string | null
  input: Record<string, unknown>
} {
  const current = nodeInputFields(node.input)
  const nextInput: Record<string, unknown> = { ...current }
  const title = draft.title.trim() || node.title || "未命名节点"
  const prompt = draft.prompt.trim()

  nextInput.title = title
  const currentFields = asObj(current.fields)
  const currentHasReferenceImages = hasOwnKey(current, "reference_images")
  const currentFieldsHasReferenceImages = currentFields ? hasOwnKey(currentFields, "reference_images") : false
  const referenceImages = Array.from(new Set(draft.reference_images.map((item) => item.trim()).filter(Boolean)))
  if (currentHasReferenceImages || referenceImages.length > 0) {
    nextInput.reference_images = referenceImages
  }
  const previousReferenceImages = stringArrayFromUnknown(current.reference_images)
  const previousEditableRefs = previousReferenceImages.length > 0
    ? previousReferenceImages
    : stringArrayFromUnknown(current.references)
  const nextReferenceNodeIds = new Set(referenceImages.map(nodeRefIdFromUnknown).filter(Boolean))
  const removedReferenceNodeIds = new Set(
    previousEditableRefs
      .map(nodeRefIdFromUnknown)
      .filter((nodeId) => nodeId && !nextReferenceNodeIds.has(nodeId)),
  )
  if (removedReferenceNodeIds.size > 0) {
    const cleaned = removeNodeReferencesFromContainer(nextInput, removedReferenceNodeIds)
    Object.assign(nextInput, cleaned.next)
  }
  if (currentFields) {
    const nextFields = { ...currentFields }
    if (currentFieldsHasReferenceImages) {
      nextFields.reference_images = referenceImages
    }
    const cleanedFields = removedReferenceNodeIds.size > 0
      ? removeNodeReferencesFromContainer(nextFields, removedReferenceNodeIds)
      : { next: nextFields, changed: false }
    if (currentFieldsHasReferenceImages || cleanedFields.changed) {
      nextInput.fields = cleanedFields.next
    }
  }

  if (node.type === "text") {
    nextInput.content = draft.content.trim()
    if (prompt) nextInput.prompt = prompt
    else delete nextInput.prompt
  } else {
    nextInput.prompt = prompt
  }

  if (node.type === "image") {
    nextInput.aspect_ratio = draft.aspect_ratio.trim()
    nextInput.resolution = draft.resolution.trim()
    nextInput.quality = draft.quality.trim()
  }

  if (node.type === "video") {
    nextInput.aspect_ratio = draft.aspect_ratio.trim()
    const model = draft.model.trim()
    if (model) nextInput.model = model
    else delete nextInput.model
    const resolution = draft.resolution.trim()
    if (resolution) nextInput.resolution = resolution
    else delete nextInput.resolution
    const duration = draft.duration_seconds.trim()
    nextInput.duration_seconds = duration && Number.isFinite(Number(duration)) ? Number(duration) : duration
  }

  if (node.type === "audio") {
    const model = draft.model.trim()
    if (model) nextInput.model = model
    else delete nextInput.model
    if (audioMode === "tts") {
      delete nextInput.style
      delete nextInput.duration_seconds
      delete nextInput.duration
      delete nextInput.negative_tags
      delete nextInput.negativeTags
      delete nextInput.instrumental
      delete nextInput.custom_mode
      delete nextInput.customMode
      const voice = draft.voice.trim()
      if (voice) nextInput.voice = voice
      else delete nextInput.voice
      const speed = draft.speed.trim()
      if (speed) nextInput.speed = Number.isFinite(Number(speed)) ? Number(speed) : speed
      else delete nextInput.speed
      const instructions = draft.instructions.trim()
      if (instructions) nextInput.instructions = instructions
      else delete nextInput.instructions
      const format = draft.format.trim()
      if (format) nextInput.format = format
      else delete nextInput.format
    } else if (audioMode === "music") {
      delete nextInput.voice
      delete nextInput.speed
      delete nextInput.instructions
      delete nextInput.format
      const style = draft.style.trim()
      if (style) nextInput.style = style
      else delete nextInput.style
      const duration = draft.duration_seconds.trim()
      if (duration) nextInput.duration_seconds = Number.isFinite(Number(duration)) ? Number(duration) : duration
      else delete nextInput.duration_seconds
      const negativeTags = draft.negative_tags.trim()
      if (negativeTags) nextInput.negative_tags = negativeTags
      else delete nextInput.negative_tags
      nextInput.instrumental = draft.instrumental
      nextInput.custom_mode = draft.custom_mode
    }
  }

  return { title, prompt: prompt || null, input: nextInput }
}

function previewPatchFromNode(node: NodeFull): Record<string, unknown> | undefined {
  const input = nodeInputFields(node.input)
  const output = asObj(parseJson(node.output)) || {}
  const grid = imageGridFromOutput(node.output)
  if (grid) {
    return {
      type: "image_grid",
      grid: grid.grid,
      cells: grid.cells,
      composite_url: grid.composite_url,
      local_url: grid.local_url || grid.composite_url || grid.url,
      url: grid.url || grid.local_url || grid.composite_url,
      width: grid.width,
      height: grid.height,
    }
  }
  if (node.type === "image" && output.type === "fusion" && Array.isArray(output.stages)) {
    return output
  }
  if (node.type === "image") {
    const url = pickUrl(output)
    if (url || output.local_url || output.remote_url) {
      return {
        type: "image",
        url: typeof output.url === "string" ? output.url : url || undefined,
        local_url: typeof output.local_url === "string" ? output.local_url : undefined,
        remote_url: typeof output.remote_url === "string" ? output.remote_url : undefined,
        width: numericDimension(output.width),
        height: numericDimension(output.height),
      }
    }
  }
  if (node.type === "video") {
    const videoPreview = videoPreviewPatchFromOutput(output)
    if (videoPreview) {
      return videoPreview
    }
  }
  const text = node.type === "text" ? pickReadableText(input, output, node.prompt || "") : ""
  if (text) return { type: "text", text }
  const prompt = pickPromptText(node.prompt || "", input, output)
  if (prompt && node.type === "image") return { type: "image_prompt", prompt }
  if (prompt && node.type === "video") return { type: "video_prompt", prompt }
  return undefined
}

function videoPreviewPatchFromOutput(output: Record<string, unknown>): Record<string, unknown> | undefined {
  if (output.type === "fusion" && Array.isArray(output.stages)) {
    const stage = (output.stages as StageData[]).find((item) => {
      const src = item.local_url || item.url || item.remote_url
      return /视频|video|clip/i.test(item.name ?? "") && typeof src === "string" && src.length > 0
    })
    if (stage) {
      return {
        type: "video",
        url: stage.url,
        local_url: stage.local_url,
        remote_url: stage.remote_url,
        poster: (stage as StageData & { poster?: string }).poster,
        thumbnail_url: (stage as StageData & { thumbnail_url?: string }).thumbnail_url,
        width: numericDimension(stage.width),
        height: numericDimension(stage.height),
      }
    }
  }

  const nested = asObj(output.video)
  if (nested) {
    const nestedUrl = pickUrl(nested)
    if (isVideoSource(nestedUrl)) {
      return {
        type: "video",
        url: typeof nested.url === "string" ? nested.url : nestedUrl,
        local_url: typeof nested.local_url === "string" ? nested.local_url : undefined,
        remote_url: typeof nested.remote_url === "string" ? nested.remote_url : undefined,
        poster: typeof nested.poster === "string" ? nested.poster : undefined,
        thumbnail_url: typeof nested.thumbnail_url === "string" ? nested.thumbnail_url : undefined,
        width: numericDimension(nested.width),
        height: numericDimension(nested.height),
      }
    }
  }

  const url = pickUrl(output)
  if ((output.type === "video" || isVideoSource(url)) && (url || output.local_url || output.remote_url)) {
    return {
      type: "video",
      url: typeof output.url === "string" ? output.url : url || undefined,
      local_url: typeof output.local_url === "string" ? output.local_url : undefined,
      remote_url: typeof output.remote_url === "string" ? output.remote_url : undefined,
      poster: typeof output.poster === "string" ? output.poster : undefined,
      thumbnail_url: typeof output.thumbnail_url === "string" ? output.thumbnail_url : undefined,
      width: numericDimension(output.width),
      height: numericDimension(output.height),
    }
  }

  return undefined
}

function canvasPatchFromNode(node: NodeFull): Record<string, unknown> {
  const patch: Record<string, unknown> = {
    title: node.title,
    status: node.status,
    prompt: node.prompt ?? undefined,
    renderState: renderStateFromNode(node),
    error_message: nodeDisplayError(node) || undefined,
  }
  const preview = previewPatchFromNode(node)
  if (preview) patch.preview = preview
  return patch
}

const inputClass =
  "w-full rounded-md border border-white/[0.08] bg-black/35 px-2.5 py-2 text-sm text-zinc-100 outline-none transition placeholder:text-zinc-600 focus:border-cyan-300/45 focus:bg-black/45"

function DraftField({
  label,
  children,
  className = "",
}: {
  label: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1.5 block text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</span>
      {children}
    </label>
  )
}

function ChipControl({
  label,
  value,
  options,
  placeholder,
  onChange,
}: {
  label: string
  value: string
  options: string[]
  placeholder?: string
  onChange: (value: string) => void
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</div>
      <div className="flex flex-wrap items-center gap-1.5">
        {options.map((option) => {
          const active = value === option
          return (
            <button
              key={option}
              type="button"
              onClick={() => onChange(option)}
              className={`rounded-md px-2.5 py-1.5 text-xs transition ${
                active
                  ? "bg-zinc-100 text-zinc-950"
                  : "bg-white/[0.06] text-zinc-300 hover:bg-white/[0.1] hover:text-zinc-50"
              }`}
            >
              {option}
            </button>
          )
        })}
        <input
          value={options.includes(value) ? "" : value}
          onChange={(event) => onChange(event.target.value)}
          className="h-8 min-w-0 flex-1 rounded-md border border-white/[0.08] bg-black/30 px-2 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-cyan-300/45"
          placeholder={placeholder || "自定义"}
        />
      </div>
    </div>
  )
}

function SelectControl({
  label,
  value,
  options,
  onChange,
  hint,
}: {
  label: string
  value: string
  options: Array<{ label: string; value: string; disabled?: boolean }>
  onChange: (value: string) => void
  hint?: string
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</div>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 w-full rounded-md border border-white/[0.08] bg-black/30 px-2 text-xs text-zinc-100 outline-none focus:border-cyan-300/45"
      >
        {options.map((option) => (
          <option key={`${option.value}:${option.label}`} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
      </select>
      {hint && <div className="text-[10px] text-zinc-600">{hint}</div>}
    </div>
  )
}

function ToggleControl({
  label,
  checked,
  onChange,
  hint,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
  hint?: string
}) {
  return (
    <label className="flex items-start justify-between gap-3 rounded-md border border-white/[0.08] bg-black/25 px-2.5 py-2">
      <span className="min-w-0">
        <span className="block text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</span>
        {hint && <span className="mt-1 block text-[10px] leading-4 text-zinc-600">{hint}</span>}
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 h-4 w-4 accent-cyan-400"
      />
    </label>
  )
}

function ReferenceEditor({
  refs,
  projectId,
  uploading,
  setLightbox,
  onChange,
  onUpload,
}: {
  refs: string[]
  projectId?: string | null
  uploading: boolean
  setLightbox: (v: { src: string; alt?: string } | null) => void
  onChange: (refs: string[]) => void
  onUpload: (files: FileList | null) => void | Promise<void>
}) {
  const [pending, setPending] = useState("")
  const normalized = refs
    .map((value) => normalizeReferenceValue(value, "引用图"))
    .filter((ref): ref is ReferenceItem => Boolean(ref))
  const displayRefs = uniqueReferenceItems(normalized, projectId)
  const addPending = () => {
    const value = pending.trim()
    if (!value) return
    onChange(Array.from(new Set([...refs, value])))
    setPending("")
  }
  const removeRef = (identity: string) => {
    onChange(refs.filter((value) => {
      const ref = normalizeReferenceValue(value, "引用图")
      return !ref || referenceIdentity(ref, projectId) !== identity
    }))
  }

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap gap-2">
        <label className="cursor-pointer rounded-md border border-white/[0.08] bg-white/[0.06] px-2.5 py-1.5 text-xs font-medium text-zinc-100 transition hover:bg-white/[0.1]">
          {uploading ? "上传中..." : "上传图片"}
          <input
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={(event) => {
              void onUpload(event.currentTarget.files)
              event.currentTarget.value = ""
            }}
          />
        </label>
        <div className="flex min-w-[190px] flex-1 overflow-hidden rounded-md border border-white/[0.08] bg-black/35">
          <input
            value={pending}
            onChange={(event) => setPending(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault()
                addPending()
              }
            }}
            placeholder="node:ID / asset:ID / uploads/... / URL"
            className="min-w-0 flex-1 bg-transparent px-2.5 py-1.5 text-xs text-zinc-100 outline-none placeholder:text-zinc-600"
          />
          <button
            type="button"
            onClick={addPending}
            className="border-l border-white/[0.08] px-3 text-xs text-zinc-300 transition hover:bg-white/[0.08] hover:text-white"
          >
            添加
          </button>
        </div>
      </div>

      {displayRefs.length > 0 ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {displayRefs.map((ref) => {
            const identity = referenceIdentity(ref, projectId)
            return (
            <div key={identity} className="group relative">
              <RefThumbnail ref={ref} projectId={projectId} setLightbox={setLightbox} />
              <button
                type="button"
                onClick={() => removeRef(identity)}
                className="absolute right-1 top-1 rounded bg-black/75 px-1.5 py-0.5 text-[10px] text-zinc-200 opacity-0 transition hover:bg-red-600 group-hover:opacity-100"
              >
                移除
              </button>
            </div>
            )
          })}
        </div>
      ) : (
        <div className="rounded-md border border-dashed border-white/[0.1] bg-black/20 px-3 py-3 text-center text-xs text-zinc-600">
          暂无引用图
        </div>
      )}
    </div>
  )
}

function NodeEditView({
  node,
  draft,
  audioProviders,
  audioConfigError,
  projectId,
  saving,
  uploading,
  setLightbox,
  onChange,
  onUploadRefs,
  onSave,
}: {
  node: NodeFull
  draft: EditableNodeDraft
  audioProviders: AudioProviderOption[]
  audioConfigError?: string | null
  projectId?: string | null
  saving: boolean
  uploading: boolean
  setLightbox: (v: { src: string; alt?: string } | null) => void
  onChange: (patch: Partial<EditableNodeDraft>) => void
  onUploadRefs: (files: FileList | null) => void | Promise<void>
  onSave: () => void | Promise<void>
}) {
  const isText = node.type === "text"
  const isImage = node.type === "image"
  const isVideo = node.type === "video"
  const isAudio = node.type === "audio"
  const hasSidePanel = isImage || isVideo || isAudio
  const mainLabel = isText ? "正文" : isImage ? "图片提示词" : isAudio ? "音频提示词" : "视频提示词"
  const enabledAudioProviders = audioProviders.filter((provider) => provider.enabled !== false)
  const selectedAudioProvider = isAudio ? resolveAudioProvider(draft.model, enabledAudioProviders) : undefined
  const selectedAudioMode = audioProviderModeFromFormat(selectedAudioProvider?.api_format)
  const audioProviderSelectValue = draft.model.trim()
    ? (selectedAudioProvider?.name || draft.model)
    : ""
  const hasConfiguredAudioProviders = enabledAudioProviders.length > 0
  const audioProviderOptions = [
    { label: "使用当前激活音频 Provider", value: "" },
    ...(draft.model && !selectedAudioProvider ? [{ label: `当前: ${draft.model}`, value: draft.model }] : []),
    ...enabledAudioProviders.map((provider) => {
      const mode = audioProviderModeFromFormat(provider.api_format)
      const suffix = provider.is_active ? " · 激活" : ""
      return {
        label: `${provider.name} · ${audioProviderTypeLabel(mode)} · ${provider.model_name}${suffix}`,
        value: provider.name,
      }
    }),
  ]
  const knownVideoModel = VIDEO_MODEL_OPTIONS.some((item) => item.modelName === draft.model)
  const videoModelOptions = [
    { label: "使用当前激活视频模型", value: "" },
    ...(draft.model && !knownVideoModel ? [{ label: `未适配: ${draft.model}`, value: draft.model }] : []),
    ...VIDEO_MODEL_OPTIONS.map((item) => ({
      label: item.label,
      value: item.modelName,
    })),
  ]
  const supportedVideoResolutions = videoSupportedResolutionsForModel(draft.model)
  const knownResolution = VIDEO_RESOLUTION_OPTIONS.some((item) => item.value === draft.resolution)
  const videoResolutionOptions = [
    ...(draft.resolution && !knownResolution ? [{ label: `当前: ${draft.resolution}`, value: draft.resolution, disabled: true }] : []),
    ...VIDEO_RESOLUTION_OPTIONS.map((item) => {
      const supported = supportedVideoResolutions.includes(item.value)
      return {
        label: `${item.label}${item.placeholder ? " (占位)" : ""}${supported ? "" : " (不支持)"}`,
        value: item.value,
        disabled: !supported,
      }
    }),
  ]
  const updateVideoModel = (model: string) => {
    const supported = videoSupportedResolutionsForModel(model)
    const resolution = supported.includes(draft.resolution) ? draft.resolution : defaultVideoResolutionForModel(model)
    onChange({ model, resolution })
  }
  const updateAudioProvider = (providerName: string) => {
    onChange({ model: providerName })
  }

  return (
    <div className={hasSidePanel ? "grid gap-3 lg:grid-cols-[minmax(0,1fr)_300px]" : "grid gap-3"}>
      <div className="min-w-0 rounded-lg border border-white/[0.08] bg-[#121722] p-3 shadow-[0_18px_45px_rgba(0,0,0,0.22)]">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-cyan-200/70">编辑节点</div>
            <div className="mt-0.5 text-xs text-zinc-500">{node.type}</div>
          </div>
          <button
            type="button"
            onClick={() => void onSave()}
            disabled={saving}
            className="rounded-md bg-cyan-500 px-3 py-1.5 text-xs font-semibold text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
        <div className="grid gap-3">
          <DraftField label="标题">
            <input
              value={draft.title}
              onChange={(event) => onChange({ title: event.target.value })}
              className={inputClass}
            />
          </DraftField>
          <DraftField label={mainLabel}>
            <textarea
              value={isText ? draft.content : draft.prompt}
              onChange={(event) => onChange(isText ? { content: event.target.value } : { prompt: event.target.value })}
              rows={isText ? 9 : 11}
              className={`${inputClass} min-h-[220px] resize-y font-mono text-[13px] leading-6`}
            />
          </DraftField>
        </div>
      </div>

      {hasSidePanel && (
        <div className="space-y-3">
          <div className="rounded-lg border border-white/[0.08] bg-[#121722] p-3">
            <div className="mb-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">规格</div>
            <div className="space-y-3">
              {isAudio ? (
                <>
                  <SelectControl
                    label="音频 Provider"
                    value={audioProviderSelectValue}
                    options={audioProviderOptions}
                    onChange={updateAudioProvider}
                    hint={
                      audioConfigError
                        ? audioConfigError
                        : selectedAudioProvider
                          ? `${audioProviderTypeLabel(selectedAudioMode)} · ${selectedAudioProvider.api_format}`
                          : "请先在设置里的音频 Provider 配置并启用。"
                    }
                  />
                  {!hasConfiguredAudioProviders && (
                    <div className="rounded-md border border-amber-500/20 bg-amber-950/15 px-3 py-2 text-xs leading-5 text-amber-100/80">
                      需要先在设置的「音频 Provider」里配置并启用 TTS 或音乐 Provider。
                    </div>
                  )}
                  {selectedAudioMode === "tts" && (
                    <>
                      <ChipControl
                        label="声音"
                        value={draft.voice}
                        options={["alloy", "nova", "shimmer", "onyx"]}
                        placeholder="TTS voice"
                        onChange={(voice) => onChange({ voice })}
                      />
                      <ChipControl
                        label="语速"
                        value={draft.speed}
                        options={["0.8", "1", "1.2"]}
                        placeholder="默认"
                        onChange={(speed) => onChange({ speed })}
                      />
                      <DraftField label="TTS 指令">
                        <textarea
                          value={draft.instructions}
                          onChange={(event) => onChange({ instructions: event.target.value })}
                          rows={3}
                          className={`${inputClass} resize-y text-[12px] leading-5`}
                          placeholder="例如：自然、清晰、轻松的旁白语气"
                        />
                      </DraftField>
                      <ChipControl
                        label="格式"
                        value={draft.format}
                        options={["mp3", "wav", "m4a"]}
                        placeholder="默认"
                        onChange={(format) => onChange({ format })}
                      />
                    </>
                  )}
                  {selectedAudioMode === "music" && (
                    <>
                      <DraftField label="风格">
                        <input
                          value={draft.style}
                          onChange={(event) => onChange({ style: event.target.value })}
                          className={inputClass}
                          placeholder="ambient piano, cinematic, lo-fi..."
                        />
                      </DraftField>
                      <ChipControl
                        label="时长"
                        value={draft.duration_seconds}
                        options={["30", "60", "120"]}
                        placeholder="秒"
                        onChange={(duration_seconds) => onChange({ duration_seconds })}
                      />
                      <ToggleControl
                        label="纯音乐"
                        checked={draft.instrumental}
                        onChange={(instrumental) => onChange({ instrumental })}
                        hint="关闭后 prompt 通常会作为歌词或含人声需求处理。"
                      />
                      <ToggleControl
                        label="高级模式"
                        checked={draft.custom_mode}
                        onChange={(custom_mode) => onChange({ custom_mode })}
                        hint="Suno-compatible 服务可用 style/title 等高级字段。"
                      />
                      <DraftField label="负面标签">
                        <input
                          value={draft.negative_tags}
                          onChange={(event) => onChange({ negative_tags: event.target.value })}
                          className={inputClass}
                          placeholder="不想要的风格、乐器或声音"
                        />
                      </DraftField>
                    </>
                  )}
                  {hasConfiguredAudioProviders && selectedAudioMode === "unknown" && (
                    <div className="rounded-md border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-xs leading-5 text-zinc-400">
                      当前音频 Provider 协议未适配详情字段；这里只保存标题、提示词和 Provider 选择。
                    </div>
                  )}
                </>
              ) : isVideo ? (
                <SelectControl
                  label="画幅"
                  value={normalizeVideoAspectRatio(draft.aspect_ratio)}
                  options={[
                    { label: "16:9", value: "16:9" },
                    { label: "9:16", value: "9:16" },
                  ]}
                  onChange={(aspect_ratio) => onChange({ aspect_ratio })}
                />
              ) : (
                <ChipControl
                  label="画幅"
                  value={draft.aspect_ratio}
                  options={["16:9", "9:16", "1:1"]}
                  onChange={(aspect_ratio) => onChange({ aspect_ratio })}
                />
              )}
              {isImage && (
                <>
                  <ChipControl
                    label="分辨率"
                    value={draft.resolution}
                    options={draft.aspect_ratio === "9:16" ? ["1440x2560", "2160x3840"] : ["2560x1440", "3840x2160"]}
                    placeholder="宽x高"
                    onChange={(resolution) => onChange({ resolution })}
                  />
                  <ChipControl
                    label="质量"
                    value={draft.quality}
                    options={["high", "medium", "low"]}
                    onChange={(quality) => onChange({ quality })}
                  />
                </>
              )}
              {isVideo && (
                <>
                  <SelectControl
                    label="适配模型"
                    value={draft.model}
                    options={videoModelOptions}
                    onChange={updateVideoModel}
                  />
                  <SelectControl
                    label="分辨率"
                    value={draft.resolution || defaultVideoResolutionForModel(draft.model)}
                    options={videoResolutionOptions}
                    onChange={(resolution) => onChange({ resolution })}
                  />
                  <ChipControl
                    label="时长"
                    value={draft.duration_seconds}
                    options={["5", "10", "15"]}
                    placeholder="秒"
                    onChange={(duration_seconds) => onChange({ duration_seconds })}
                  />
                </>
              )}
            </div>
          </div>

          {!isAudio && (
            <div className="rounded-lg border border-white/[0.08] bg-[#121722] p-3">
              <div className="mb-2.5 flex items-center justify-between">
                <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">引用图</div>
                <span className="text-[11px] text-zinc-600">{draft.reference_images.length}</span>
              </div>
              <ReferenceEditor
                refs={draft.reference_images}
                projectId={projectId}
                uploading={uploading}
                setLightbox={setLightbox}
                onChange={(reference_images) => onChange({ reference_images })}
                onUpload={onUploadRefs}
              />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function imagePreviewSize(grid: ImageGridOutput | null, image?: MediaItem): { width: number; height: number } | null {
  if (grid?.width && grid.height) return { width: grid.width, height: grid.height }
  if (image?.width && image.height) return { width: image.width, height: image.height }
  const cols = grid?.grid?.cols || 0
  const rows = grid?.grid?.rows || 0
  const cell = grid?.cells?.find((item) => item.width && item.height)
  if (cell?.width && cell.height && cols > 0 && rows > 0) {
    return { width: cell.width * cols, height: cell.height * rows }
  }
  return null
}

function ImagePreviewSection({
  node,
  image,
  refs,
  spec,
  busy,
  projectId,
  setLightbox,
  onEdited,
}: {
  node: NodeFull
  image?: MediaItem
  refs?: unknown[]
  spec: ReturnType<typeof pickMediaSpec>
  busy: boolean
  projectId?: string | null
  setLightbox: (v: { src: string; alt?: string } | null) => void
  onEdited?: () => void
}) {
  const overlayRef = useRef<HTMLDivElement | null>(null)
  const activeStrokeIdRef = useRef<string | null>(null)
  const [inpaintMode, setInpaintMode] = useState(false)
  const [inpaintPrompt, setInpaintPrompt] = useState("")
  const [brushSize, setBrushSize] = useState(34)
  const [strokes, setStrokes] = useState<InpaintStroke[]>([])
  const [inpaintBusy, setInpaintBusy] = useState(false)
  const [inpaintError, setInpaintError] = useState<string | null>(null)
  const grid = imageGridFromOutput(node.output)
  const imageSrc = grid
    ? resolveMediaUrl(grid.local_url || grid.composite_url || grid.url) || ""
    : image?.src || ""
  const size = imagePreviewSize(grid, image)
  const previewStyle: CSSProperties | undefined = size
    ? { aspectRatio: `${size.width} / ${size.height}` }
    : undefined
  const canInpaint = Boolean(projectId && imageSrc && node.type === "image" && !busy)
  const maskReady = strokes.some((stroke) => stroke.points.length > 0)

  useEffect(() => {
    if (!busy) return
    activeStrokeIdRef.current = null
    setInpaintMode(false)
  }, [busy])

  const pointerToImagePoint = (event: React.PointerEvent<HTMLDivElement>) => {
    const rect = overlayRef.current?.getBoundingClientRect()
    if (!rect || rect.width <= 0 || rect.height <= 0) return null
    return {
      x: clamp01((event.clientX - rect.left) / rect.width),
      y: clamp01((event.clientY - rect.top) / rect.height),
    }
  }

  const startBrushStroke = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!inpaintMode || inpaintBusy) return
    const point = pointerToImagePoint(event)
    if (!point) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    const strokeId = `stroke-${Date.now()}-${Math.random().toString(16).slice(2)}`
    activeStrokeIdRef.current = strokeId
    setStrokes((current) => [
      ...current,
      { id: strokeId, brushSize, points: [point] },
    ])
    setInpaintError(null)
  }

  const continueBrushStroke = (event: React.PointerEvent<HTMLDivElement>) => {
    const strokeId = activeStrokeIdRef.current
    if (!inpaintMode || !strokeId || inpaintBusy) return
    const point = pointerToImagePoint(event)
    if (!point) return
    event.preventDefault()
    event.stopPropagation()
    setStrokes((current) => current.map((stroke) => {
      if (stroke.id !== strokeId) return stroke
      const last = stroke.points[stroke.points.length - 1]
      if (last && pointDistance(last, point) < 0.0035) return stroke
      return { ...stroke, points: [...stroke.points, point] }
    }))
  }

  const finishBrushStroke = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!activeStrokeIdRef.current) return
    event.preventDefault()
    event.stopPropagation()
    activeStrokeIdRef.current = null
  }

  const runInpaint = async () => {
    if (!projectId || !maskReady || !inpaintPrompt.trim()) return
    setInpaintBusy(true)
    setInpaintError(null)
    try {
      const rect = overlayRef.current?.getBoundingClientRect()
      const result = await callTool<Record<string, unknown>>("image.inpaint_region", {
        project_id: projectId,
        node_id: node.id,
        prompt: inpaintPrompt.trim(),
        mask: {
          type: "brush",
          unit: "normalized",
          source_url: imageSrc,
          image_width: size?.width,
          image_height: size?.height,
          brush_size_unit: "relative_short_side",
          strokes: strokes
            .filter((stroke) => stroke.points.length > 0)
            .map((stroke) => ({
              brush_size: normalizedBrushSize(stroke.brushSize, rect),
              brush_px: stroke.brushSize,
              points: stroke.points.map((point) => ({
                x: Number(point.x.toFixed(4)),
                y: Number(point.y.toFixed(4)),
              })),
            })),
        },
      })
      if (result && result.ok === false) {
        throw new Error(String(result.error || "局部重绘失败"))
      }
      setInpaintMode(false)
      setStrokes([])
      setInpaintPrompt("")
      onEdited?.()
    } catch (error) {
      setInpaintError(error instanceof Error ? error.message : String(error))
    } finally {
      setInpaintBusy(false)
    }
  }

  return (
    <Section title={grid ? "图片预览" : image?.label || "图片预览"}>
      <div className="relative overflow-hidden rounded-lg bg-transparent">
        {imageSrc ? (
          <div
            className="group/image relative block w-full overflow-hidden rounded-lg bg-transparent ring-1 ring-white/[0.08]"
            style={previewStyle}
          >
            <div className="absolute right-2 top-2 z-20 flex gap-1.5 opacity-0 transition-opacity group-hover/image:opacity-100">
              {canInpaint && (
                <button
                  type="button"
	                  onClick={(event) => {
	                    event.stopPropagation()
	                    setInpaintMode((value) => {
	                      const next = !value
	                      if (next) setStrokes([])
	                      return next
	                    })
	                    setInpaintError(null)
	                  }}
                  className={inpaintMode
                    ? "rounded-md bg-cyan-300 px-2.5 py-1.5 text-[11px] font-semibold text-cyan-950 shadow-xl shadow-black/30 transition hover:bg-cyan-200"
                    : "rounded-md border border-white/10 bg-black/70 px-2.5 py-1.5 text-[11px] font-semibold text-zinc-100 shadow-xl shadow-black/30 backdrop-blur transition hover:bg-black/85"}
                >
                  {inpaintMode ? "取消" : "局部重绘"}
                </button>
              )}
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation()
                  setLightbox({ src: imageSrc, alt: image?.label })
                }}
                className="rounded-md border border-white/10 bg-black/70 px-2.5 py-1.5 text-[11px] font-semibold text-zinc-100 shadow-xl shadow-black/30 backdrop-blur transition hover:bg-black/85"
              >
                打开
              </button>
            </div>
            <img
              src={imageSrc}
              alt={image?.label || ""}
              className={size ? "block h-full w-full object-cover" : "block h-auto w-full"}
	              onError={(event) => {
	                ;(event.target as HTMLImageElement).style.opacity = "0.2"
	              }}
	            />
	            {busy && (
	              <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-black/42 backdrop-blur-[1px]">
	                <div className="flex items-center gap-2 rounded-md border border-blue-200/20 bg-black/72 px-3 py-2 text-xs font-medium text-blue-100 shadow-xl shadow-black/30">
	                  <span className="h-3.5 w-3.5 rounded-full border-2 border-blue-200 border-t-transparent animate-spin" />
	                  图片生成中...
	                </div>
	              </div>
	            )}
	            {inpaintMode && (
	              <div
	                ref={overlayRef}
	                className="absolute inset-0 z-10 cursor-crosshair bg-cyan-950/10"
	                onPointerDown={startBrushStroke}
	                onPointerMove={continueBrushStroke}
	                onPointerUp={finishBrushStroke}
	                onPointerCancel={() => {
	                  activeStrokeIdRef.current = null
	                }}
	              >
	                <div className="pointer-events-none absolute inset-0 bg-black/18" />
	                <svg
	                  className="pointer-events-none absolute inset-0 h-full w-full"
	                  viewBox="0 0 1000 1000"
	                  preserveAspectRatio="none"
	                >
	                  {strokes.map((stroke) => (
	                    <path
	                      key={stroke.id}
	                      d={strokePath(stroke)}
	                      fill="none"
	                      stroke="rgba(103,232,249,0.72)"
	                      strokeLinecap="round"
	                      strokeLinejoin="round"
	                      strokeWidth={stroke.brushSize}
	                      vectorEffect="non-scaling-stroke"
	                    />
	                  ))}
	                </svg>
	              </div>
	            )}
          </div>
        ) : (
          <div className="flex h-56 items-center justify-center">
            <ImagePlaceholder label={busy ? "图片生成中..." : "待生成图片"} busy={busy} />
          </div>
        )}

        <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
      </div>
      {inpaintMode && imageSrc && (
        <div className="mt-2 space-y-2 rounded-lg border border-cyan-300/18 bg-cyan-950/16 p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex min-w-0 flex-1 items-center gap-2 text-[11px] text-zinc-400">
              <span className="shrink-0">笔刷</span>
              <input
                type="range"
                min={12}
                max={72}
                step={2}
                value={brushSize}
                onChange={(event) => setBrushSize(Number(event.target.value))}
                className="min-w-[120px] flex-1 accent-cyan-300"
              />
              <span className="w-8 text-right text-zinc-500">{brushSize}</span>
            </label>
            <button
              type="button"
              onClick={() => setStrokes((current) => current.slice(0, -1))}
              disabled={!maskReady || inpaintBusy}
              className="rounded-md border border-white/10 bg-black/25 px-2.5 py-1.5 text-[11px] text-zinc-200 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              撤销一笔
            </button>
            <button
              type="button"
              onClick={() => setStrokes([])}
              disabled={!maskReady || inpaintBusy}
              className="rounded-md border border-white/10 bg-black/25 px-2.5 py-1.5 text-[11px] text-zinc-200 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
            >
              清空
            </button>
          </div>
          <textarea
            value={inpaintPrompt}
            onChange={(event) => setInpaintPrompt(event.target.value)}
            rows={2}
            placeholder="描述涂抹区域要改成什么"
            className="w-full resize-none rounded-md border border-white/10 bg-black/35 px-2.5 py-2 text-[12px] leading-5 text-zinc-100 placeholder-zinc-500 outline-none focus:border-cyan-200/55"
          />
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0 truncate text-[11px] text-zinc-500">
              {maskReady ? `已涂抹 ${strokes.length} 笔` : "在图片上涂抹要重绘的区域"}
            </div>
            <button
              type="button"
              onClick={() => void runInpaint()}
              disabled={inpaintBusy || !maskReady || !inpaintPrompt.trim()}
              className="rounded-md bg-cyan-300 px-3 py-1.5 text-[12px] font-semibold text-cyan-950 transition hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {inpaintBusy ? "重绘中..." : "重绘选区"}
            </button>
          </div>
          {inpaintError && (
            <div className="rounded border border-red-400/20 bg-red-950/35 px-2 py-1.5 text-[11px] leading-4 text-red-200">
              {inpaintError}
            </div>
          )}
        </div>
      )}
      <MediaSpecBadges spec={spec} className="mt-2" />
    </Section>
  )
}

function MediaHistorySection({
  kind,
  output,
  busy,
  switchingId,
  onSwitch,
}: {
  kind: "image" | "video" | "audio"
  output: unknown
  busy: boolean
  switchingId?: string | null
  onSwitch: (entry: MediaHistoryEntry) => void | Promise<void>
}) {
  const entries = mediaHistoryEntriesFromOutput(output, kind)
  if (entries.length === 0) return null
  const title = kind === "video" ? `视频历史状态 (${entries.length})` : kind === "audio" ? `音频历史状态 (${entries.length})` : `图片历史状态 (${entries.length})`
  return (
    <Section title={title}>
      <div className="space-y-3">
        {entries.map((entry) => {
          const primary = entry.media[0]
          const switching = switchingId === entry.id
          const label = formatHistoryTime(entry.created_at) || `历史 ${entry.index + 1}`
          return (
            <div
              key={`${entry.id}-${entry.index}`}
              role="button"
              tabIndex={busy || switching ? -1 : 0}
              aria-disabled={busy || switching}
              onClick={() => {
                if (!busy && !switching) void onSwitch(entry)
              }}
              onKeyDown={(event) => {
                if (busy || switching) return
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault()
                  void onSwitch(entry)
                }
              }}
              className={`rounded-lg border bg-black/24 p-2 transition ${
                busy || switching
                  ? "cursor-not-allowed border-white/[0.07] opacity-60"
                  : "cursor-pointer border-white/[0.08] hover:border-cyan-200/40 hover:bg-cyan-950/10"
              }`}
            >
              <div className="flex min-h-[66px] gap-2.5">
                <div className="flex h-16 w-20 shrink-0 items-center justify-center overflow-hidden rounded-md bg-black">
                  {primary.kind === "image" ? (
                    <img
                      src={primary.src}
                      alt={label}
                      className="h-full w-full object-cover"
                      onError={(event) => {
                        ;(event.currentTarget as HTMLImageElement).style.opacity = "0.25"
                      }}
                    />
                  ) : primary.kind === "video" ? (
                    <video
                      poster={primary.poster}
                      muted
                      playsInline
                      preload="metadata"
                      controls={false}
                      className="h-full w-full object-cover"
                    >
                      <source src={primary.src} type={videoMimeType(primary.src)} />
                    </video>
                  ) : (
                    <div className="flex h-full w-full flex-col items-center justify-center gap-1 bg-zinc-950 text-[10px] text-amber-100">
                      <span className="font-semibold tracking-[0.16em]">AU</span>
                      <span className="text-zinc-500">音频</span>
                    </div>
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0 truncate text-[11px] text-zinc-400">{label}</div>
                    <div className="shrink-0 rounded bg-cyan-300/10 px-1.5 py-0.5 text-[10px] font-semibold text-cyan-100">
                      {switching ? "还原中" : "还原"}
                    </div>
                  </div>
                  <div
                    className="mt-1 overflow-hidden text-[12px] leading-5 text-zinc-300"
                    style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}
                  >
                    {entry.prompt || "无提示词记录"}
                  </div>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </Section>
  )
}

function GenericNodeDetails({
  node,
  mediaCount,
}: {
  node: NodeFull
  mediaCount: number
}) {
  const inputObj = asObj(parseJson(node.input)) || {}
  const outputObj = asObj(parseJson(node.output)) || {}
  const nodePrompt = typeof node.prompt === "string" ? node.prompt : ""
  const prompt = pickPromptText(nodePrompt, inputObj, outputObj)
  const inputRest = compactObject(inputObj, new Set(["prompt", "image_prompt", "video_prompt"]))
  const outputRest = compactObject(outputObj, new Set(["prompt", "image_prompt", "video_prompt", "url", "local_url", "remote_url"]))
  const outputText = typeof node.output === "string" && node.output.trim() ? node.output.trim() : ""
  const hasInput = Object.keys(inputRest).length > 0
  const hasOutput = Object.keys(outputRest).length > 0 || outputText
  const hasPromptOnly = Boolean(prompt) && !hasInput && !hasOutput && mediaCount === 0

  if (!prompt && !hasInput && !hasOutput && !hasPromptOnly) return null

  return (
    <div className="space-y-3">
      {prompt && (
        <Section title="提示词">
          <PromptBlock>{prompt}</PromptBlock>
        </Section>
      )}
      {hasInput && (
        <Section title="输入参数">
          <FieldRows value={inputRest} />
        </Section>
      )}
      {outputText ? (
        <Section title="输出内容">
          <PromptBlock>{outputText}</PromptBlock>
        </Section>
      ) : hasOutput ? (
        <Section title="输出内容">
          <FieldRows value={outputRest} />
        </Section>
      ) : null}
    </div>
  )
}

function TypedRenderer({
  node,
  type,
  input,
  output,
  nodePrompt,
  nodeStatus,
  projectId,
  setLightbox,
  setVideoLightbox,
  onEdited,
  onSwitchHistory,
  switchingHistoryId,
}: {
  node: NodeFull
  type: string
  input: unknown
  output: unknown
  nodePrompt?: string | null
  nodeStatus?: string
  projectId?: string | null
  setLightbox: (v: { src: string; alt?: string } | null) => void
  setVideoLightbox: (v: VideoLightboxState | null) => void
  onEdited?: () => void
  onSwitchHistory: (entry: MediaHistoryEntry) => void | Promise<void>
  switchingHistoryId?: string | null
}) {
  const inObj = asObj(parseJson(input)) || {}
  const outObj = asObj(parseJson(output)) || {}
  const topPrompt = typeof nodePrompt === "string" ? nodePrompt : ""
  const busy = nodeStatus === "running" || nodeStatus === "queued"

  if (type === "text") {
    const text = pickReadableText(inObj, outObj, topPrompt)
    const refs = pickReferences(inObj, outObj)
    return (
      <div className="space-y-3">
        {text ? (
          <Section title="内容">
            <PromptBlock>{text}</PromptBlock>
            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        ) : (
          <Section title="内容">
            <ImagePlaceholder label="等待文本内容" />
            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        )}
      </div>
    )
  }

  if (type === "image") {
    const media = collectMedia(outObj).filter((item) => item.kind === "image")
    const image = media[0]
    const prompt = pickPromptText(topPrompt, inObj, outObj)
    const spec = pickMediaSpec(outObj, inObj)
    const refs = pickReferences(inObj, outObj)
    return (
      <div className="space-y-3">
	        <ImagePreviewSection
	          node={node}
	          image={image}
	          refs={refs}
	          spec={spec}
	          busy={busy}
          projectId={projectId}
          setLightbox={setLightbox}
          onEdited={onEdited}
        />
        {prompt && (
          <Section title="图片提示词">
            <PromptBlock>{prompt}</PromptBlock>
          </Section>
        )}
        <MediaHistorySection
          kind="image"
          output={outObj}
          busy={busy}
          switchingId={switchingHistoryId}
          onSwitch={onSwitchHistory}
        />
      </div>
    )
  }

  if (type === "video") {
    const media = collectMedia(outObj)
    const video = media.find((item) => item.kind === "video")
    const poster = media.find((item) => item.kind === "image")
    const prompt = pickPromptText(topPrompt, inObj, outObj)
    const refs = pickReferences(inObj, outObj)
    return (
      <div className="space-y-3">
	        {video ? (
	          <Section title={video.label || "视频预览"}>
	            <InlineVideoPreview
	              src={video.src}
	              poster={video.poster || poster?.src}
	              title={video.label || node.title || "视频预览"}
	              className="max-h-[360px] w-full bg-black"
	              onOpen={() => setVideoLightbox({
	                src: video.src,
	                poster: video.poster || poster?.src,
	                title: video.label || node.title || "视频预览",
	              })}
	            />
	            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
	          </Section>
	        ) : (
	          <Section title="视频预览">
	            <ImagePlaceholder label={busy ? "视频生成中..." : "待生成视频"} busy={busy} />
	            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
	          </Section>
	        )}
        {prompt && (
          <Section title="视频提示词">
            <PromptBlock>{prompt}</PromptBlock>
          </Section>
        )}
        <MediaHistorySection
          kind="video"
          output={outObj}
          busy={busy}
          switchingId={switchingHistoryId}
          onSwitch={onSwitchHistory}
        />
      </div>
    )
  }

  if (type === "audio") {
    const media = collectMedia(outObj)
    const audio = media.find((item) => item.kind === "audio")
    const prompt = pickPromptText(topPrompt, inObj, outObj)
    const refs = pickReferences(inObj, outObj)
    return (
      <div className="space-y-3">
        {audio ? (
          <Section title={audio.label || "音频预览"}>
            <InlineAudioPreview
              src={audio.src}
              title={audio.label || node.title || "音频预览"}
            />
            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        ) : (
          <Section title="音频预览">
            <ImagePlaceholder label={busy ? "音频生成中..." : "待生成音频"} busy={busy} />
            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        )}
        {prompt && (
          <Section title="音频提示词">
            <PromptBlock>{prompt}</PromptBlock>
          </Section>
        )}
        <MediaHistorySection
          kind="audio"
          output={outObj}
          busy={busy}
          switchingId={switchingHistoryId}
          onSwitch={onSwitchHistory}
        />
      </div>
    )
  }

  // ── character ──
  if (type === "character") {
    // 兼容三种 output 形态:
    // 1) fusion: { type:"fusion", stages:[{name:'人物设定',...},{name:'提示词',prompt},{name:'参考图',url,size,quality,...}] }
    // 2) 平铺单人: { character:{name,identity,appearance,visual_prompt,...}, character_id }
    // 3) 平铺多人: { characters:[...] } — 取第一个
    const stages = Array.isArray(outObj.stages) ? (outObj.stages as StageData[]) : []
    const promptStage = stages.find((s) => /提示词/.test(s.name))
    const imageStage = stages.find((s) => /图|参考/.test(s.name) && !/提示词/.test(s.name))
    const settingStage = stages.find((s) => /设定|人物/.test(s.name) && !/提示词|图|参考/.test(s.name)) as Record<string, unknown> | undefined

    const flatChar = (asObj(outObj.character)
      || (Array.isArray(outObj.characters) && outObj.characters.length > 0
          ? asObj((outObj.characters as unknown[])[0])
          : null)
      || null) as Record<string, unknown> | null

    // settingStage 是从 character 阶段抽出的人物字段;flatChar 是工具直接返回的字段
    const charFields = (settingStage || flatChar || {}) as Record<string, unknown>

    const pickStr = (k: string): string => {
      const v = charFields[k]
      return typeof v === "string" ? v : ""
    }
    const pickList = (k: string): string[] => {
      const v = charFields[k]
      return Array.isArray(v) ? v.filter((x) => typeof x === "string") as string[] : []
    }

    const name = (pickStr("name") || (inObj.character_name as string) || (inObj.name as string) || "") as string
    const role_type = pickStr("role_type")
    const age = (charFields.age != null && charFields.age !== "") ? String(charFields.age) : ""
    const gender = pickStr("gender")
    const identity = pickStr("identity") || pickStr("background")
    const appearance = pickStr("appearance")
    const personality = pickStr("personality")
    const motivation = pickStr("motivation")
    const traits = pickList("traits")

    // 当前节点 prompt 优先；旧 output/fusion prompt 只作为历史产物兜底。
    const visualPrompt =
      topPrompt ||
      (inObj.prompt as string) ||
      pickStr("visual_prompt") ||
      (promptStage?.prompt as string) ||
      ""
    // 图片来源:优先 fusion 图片阶段,其次平铺顶层 url
    const imageUrl = imageStage
      ? resolveMediaUrl((imageStage.local_url as string) || (imageStage.url as string))
      : (typeof outObj.url === "string" ? resolveMediaUrl(outObj.url as string) : null)
    const spec = pickMediaSpec(outObj, inObj, imageStage as Record<string, unknown> | undefined)
    const imageStageStatus = (imageStage as Record<string, unknown> | undefined)?.status
    const imgErr = imageStageStatus === "failed"
      ? (((imageStage as Record<string, unknown> | undefined)?.error as string) || "")
      : ""
    const refsChar = (Array.isArray(outObj.reference_images) && outObj.reference_images.length > 0
      ? outObj.reference_images
      : Array.isArray(inObj.reference_images) ? inObj.reference_images : []
    ) as string[]

    return (
      <div className="space-y-3">
        {name && (
          <Section title="姓名">
            <div className="text-base font-semibold text-gray-100">
              {name}
              {role_type && <span className="ml-2 text-[12px] text-purple-300">[{role_type}]</span>}
              {gender && <span className="ml-1.5 text-[12px] text-gray-400">{gender}</span>}
              {age && <span className="ml-1.5 text-[12px] text-gray-400">{age}岁</span>}
            </div>
          </Section>
        )}
        {imageUrl ? (
          <Section title="参考图">
            <button
              onClick={() => setLightbox({ src: imageUrl, alt: name })}
              className="block w-full rounded overflow-hidden bg-black/40 border border-gray-800"
            >
              <img
                src={imageUrl}
                alt={name}
                className="w-full h-56 object-cover"
                onError={(e) => {
                  const el = e.target as HTMLImageElement
                  const fb = imageStage ? resolveMediaUrl(imageStage.remote_url) : ""
                  if (fb && el.src !== fb) el.src = fb
                }}
              />
            </button>
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsChar} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        ) : (
          <Section title="生图规格(尚未出图)">
            <ImagePlaceholder label="未出图 — 调用 node.run(action='render') 生成" />
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsChar} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        )}
        {imgErr && (
          <Section title="图片错误">
            <div className="rounded border border-red-900/60 bg-red-950/40 px-2.5 py-1.5 text-[12px] text-red-300 whitespace-pre-wrap">
              {imgErr}
            </div>
          </Section>
        )}
        {visualPrompt && (
          <Section title="形象提示词">
            <PromptBlock>{visualPrompt}</PromptBlock>
          </Section>
        )}
        {identity && (
          <Section title="身份背景">
            <div className="rounded bg-black/30 border border-gray-800 p-2 text-[13px] text-gray-200">{identity}</div>
          </Section>
        )}
        {appearance && (
          <Section title="外貌">
            <div className="rounded bg-black/30 border border-gray-800 p-2 text-[13px] text-gray-200">{appearance}</div>
          </Section>
        )}
        {personality && (
          <Section title="性格">
            <div className="rounded bg-black/30 border border-gray-800 p-2 text-[13px] text-gray-200">{personality}</div>
          </Section>
        )}
        {motivation && (
          <Section title="动机">
            <div className="rounded bg-black/30 border border-gray-800 p-2 text-[13px] text-gray-200">{motivation}</div>
          </Section>
        )}
        {traits.length > 0 && (
          <Section title="特征">
            <div className="flex flex-wrap gap-1.5">
              {traits.map((t, i) => (
                <span key={i} className="px-2 py-0.5 text-[12px] rounded bg-purple-900/40 text-purple-200">{t}</span>
              ))}
            </div>
          </Section>
        )}
      </div>
    )
  }

  // ── scene ──
  if (type === "scene") {
    const stages = Array.isArray(outObj.stages) ? (outObj.stages as StageData[]) : []
    const promptStage = stages.find((s) => /提示词/.test(s.name))
    const imageStage = stages.find((s) => /图|景|全景/.test(s.name) && !/提示词/.test(s.name))
    const imageUrl = imageStage
      ? resolveMediaUrl((imageStage.local_url as string) || (imageStage.url as string))
      : (typeof outObj.url === "string" ? resolveMediaUrl(outObj.url as string) : null)
    const spec = pickMediaSpec(outObj, inObj, imageStage as Record<string, unknown> | undefined)
    const promptText =
      topPrompt ||
      (inObj.prompt as string) ||
      (promptStage?.prompt as string) ||
      (outObj.prompt as string) ||
      ""
    const refsScene = (Array.isArray(outObj.reference_images) && outObj.reference_images.length > 0
      ? outObj.reference_images
      : Array.isArray(inObj.reference_images) ? inObj.reference_images : []
    ) as string[]
    return (
      <div className="space-y-3">
        {imageUrl ? (
          <Section title="场景图">
            <button
              onClick={() => setLightbox({ src: imageUrl })}
              className="block w-full rounded overflow-hidden"
            >
              <img
                src={imageUrl}
                alt=""
                className="w-full h-56 object-cover"
              />
            </button>
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsScene} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        ) : (
          <Section title="生图规格(尚未出图)">
            <ImagePlaceholder label="未出图 — 调用 node.run(action='render') 生成" />
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsScene} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        )}
        {promptText && (
          <Section title="场景提示词">
            <PromptBlock>{promptText}</PromptBlock>
          </Section>
        )}
      </div>
    )
  }

  // ── episode_script ──
  if (type === "episode_script") {
    // 兼容三种产物形态:
    //  A) 正确:outObj.script = {...}
    //  B) 历史 wrapper:outObj.result.script = {...} (orchestrator 旧 bug 把 node.run 的返回壳整段写入)
    //  C) 顶层平铺:整个 outObj 就是 script(episode_number/scenes 直接在 outObj 上)
    const wrapped = asObj((outObj as Record<string, unknown>).result)
    const script =
      asObj(outObj.script) ||
      (wrapped ? asObj(wrapped.script) : null) ||
      // 顶层就有 scenes/title 时,把整个 outObj 当作 script
      ((outObj.scenes || outObj.episode_number || outObj.title) ? outObj : {})
    const summary = (script.summary as string) || ""
    const epNum = (script.episode_number ?? outObj.episode_number ?? (wrapped?.episode_number)) as number | undefined
    const title = (script.title as string) || ""
    const wordCount = script.word_count as number | undefined
    const scenes = Array.isArray(script.scenes) ? (script.scenes as Record<string, unknown>[]) : []
    // 整本剧本全文(prompt 让 LLM 也输出 script 字符串字段)
    const fullScript = typeof (script as Record<string, unknown>).script === "string"
      ? ((script as Record<string, unknown>).script as string)
      : ""
    return (
      <div className="space-y-3">
        {(epNum != null || title) && (
          <Section title="集别">
            <div className="text-[14px] text-gray-200">
              {epNum != null && <span className="text-purple-300 mr-2">第 {epNum} 集</span>}
              {title && <span>{title}</span>}
              {wordCount && <span className="ml-2 text-[12px] text-gray-500">{wordCount} 字</span>}
            </div>
          </Section>
        )}
        {summary && (
          <Section title="剧情概要">
            <div className="rounded bg-black/40 border border-gray-800 p-2.5 text-[14px] text-gray-200">
              <MarkdownView compact>{summary}</MarkdownView>
            </div>
          </Section>
        )}
        {fullScript && (
          <Section title="完整剧本">
            <div className="rounded bg-black/40 border border-gray-800 p-2.5 text-[13px] text-gray-200 max-h-[420px] overflow-y-auto whitespace-pre-wrap">
              {fullScript}
            </div>
          </Section>
        )}
        {scenes.length > 0 && (
          <Section title={`场次（${scenes.length}）`}>
            <div className="space-y-2">
              {scenes.map((sc, i) => {
                // dialogues 数组 / dialogue 字符串 / dialogue 数组 都兼容
                const rawDialogues = sc.dialogues ?? sc.dialogue
                const dialogues = Array.isArray(rawDialogues)
                  ? (rawDialogues as Record<string, string>[])
                  : []
                const dialogueText = typeof rawDialogues === "string" ? rawDialogues : ""
                const action = (sc.action as string) || (sc.description as string) || (sc.summary as string) || ""
                const sceneNum = sc.scene_number ?? i + 1
                const loc = (sc.location as string) || (sc.name as string) || ""
                const tod = (sc.time_of_day as string) || ""
                const chars = Array.isArray(sc.characters) ? (sc.characters as string[]).join("、") : ""
                return (
                  <div key={i} className="rounded bg-black/30 border border-gray-800 p-2 text-[13px]">
                    <div className="flex items-baseline gap-2 mb-1">
                      <span className="text-rose-300 font-bold">场 {String(sceneNum)}</span>
                      {loc && <span className="text-gray-400">{loc}</span>}
                      {tod && <span className="text-gray-500 text-[12px]">{tod}</span>}
                    </div>
                    {chars && <div className="text-purple-300 text-[12px] mb-1">人物：{chars}</div>}
                    {action && (
                      <div className="text-gray-200 whitespace-pre-wrap mb-1">{action}</div>
                    )}
                    {dialogues.length > 0 && (
                      <div className="space-y-0.5 border-l-2 border-gray-700 pl-2">
                        {dialogues.map((d, j) => (
                          <div key={j} className="text-[13px]">
                            <span className="text-purple-300 font-semibold">{d.speaker}：</span>
                            <span className="text-gray-200">{d.line}</span>
                          </div>
                        ))}
                      </div>
                    )}
                    {dialogueText && (
                      <div className="border-l-2 border-gray-700 pl-2 text-gray-200 whitespace-pre-wrap text-[13px]">
                        {dialogueText}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </Section>
        )}
      </div>
    )
  }

  // ── script_collection（全剧目录） ──
  if (type === "script_collection") {
    // 兼容历史 wrapper:outObj.result.outline
    const wrapped = asObj((outObj as Record<string, unknown>).result)
    const outline =
      asObj(outObj.outline) ||
      (wrapped ? asObj(wrapped.outline) : null) ||
      outObj
    const episodes = Array.isArray(outline.episodes)
      ? (outline.episodes as Record<string, unknown>[])
      : []
    const projTitle = (outline.title as string) || (outObj.title as string) || ""
    const genre = (outline.genre as string) || ""
    return (
      <div className="space-y-3">
        {(projTitle || genre) && (
          <Section title="剧目">
            <div className="text-[15px] font-semibold text-gray-100">{projTitle}</div>
            {genre && <div className="text-[12px] text-gray-400 mt-0.5">{genre}</div>}
          </Section>
        )}
        {episodes.length > 0 && (
          <Section title={`分集（${episodes.length}）`}>
            <div className="space-y-1.5">
              {episodes.map((ep, i) => (
                <div key={i} className="rounded bg-black/30 border border-gray-800 p-2 text-[13px]">
                  <div className="flex items-baseline gap-2">
                    <span className="text-purple-300 font-bold">E{(ep.episode_number as number) ?? i + 1}</span>
                    <span className="text-gray-100 font-medium">{(ep.title as string) || ""}</span>
                  </div>
                  {ep.summary != null && Boolean(ep.summary) && (
                    <div className="text-gray-300 mt-1 line-clamp-3">{String(ep.summary)}</div>
                  )}
                </div>
              ))}
            </div>
          </Section>
        )}
      </div>
    )
  }

  // ── episode_segment_plan ──
  if (type === "episode_segment_plan") {
    const wrapped = asObj((outObj as Record<string, unknown>).result)
    const segs = (Array.isArray(outObj.segments)
      ? outObj.segments
      : Array.isArray(wrapped?.segments) ? wrapped!.segments : []
    ) as Record<string, unknown>[]
    return (
      <Section title={`段落（${segs.length}）`}>
        <div className="space-y-1.5">
          {segs.map((s, i) => (
            <div key={i} className="rounded bg-black/30 border border-gray-800 p-2 text-[14px]">
              <div className="flex items-center gap-2">
                <span className="text-[14px] px-1.5 py-0.5 rounded bg-lime-900/40 text-lime-300">
                  段 {(s.index as number) ?? i + 1}
                </span>
                {s.duration_seconds != null && <span className="text-[14px] text-gray-500">{String(s.duration_seconds)}s</span>}
                {s.workflow_mode != null && <span className="text-[14px] text-blue-300">[{String(s.workflow_mode)}]</span>}
              </div>
              {s.plot != null && <div className="text-gray-300 mt-1 line-clamp-3">{String(s.plot)}</div>}
            </div>
          ))}
        </div>
      </Section>
    )
  }

  // ── episode_cast_scene_plan ──
  if (type === "episode_cast_scene_plan") {
    // 兼容 4 种形态:
    //  A) 顶层平铺(新): outObj.cast / outObj.scenes / outObj.segment_assignments
    //  B) plan 嵌套:outObj.plan.{cast,scenes,segment_assignments}
    //  C) wrapper:outObj.result.plan.{...}
    //  D) wrapper 平铺:outObj.result.{cast,scenes,segment_assignments}
    const wrapped = asObj((outObj as Record<string, unknown>).result)
    const planObj =
      asObj(outObj.plan) ||
      (wrapped ? asObj(wrapped.plan) : null) ||
      wrapped ||
      outObj
    const cast = (Array.isArray(outObj.cast) ? outObj.cast : (planObj ? planObj.cast : null) || []) as Record<string, unknown>[]
    const sceneRows = (Array.isArray(outObj.scenes) ? outObj.scenes : (planObj ? planObj.scenes : null) || []) as Record<string, unknown>[]
    // segment_assignments 可能是数组也可能是 dict;dict 转数组
    let assignmentsRaw: unknown =
      outObj.segment_assignments ?? (planObj ? planObj.segment_assignments : null)
    if (assignmentsRaw && !Array.isArray(assignmentsRaw) && typeof assignmentsRaw === "object") {
      assignmentsRaw = Object.entries(assignmentsRaw as Record<string, unknown>).map(([k, v]) => ({
        segment_index: Number.isFinite(Number(k)) ? Number(k) : k,
        ...(typeof v === "object" && v ? (v as Record<string, unknown>) : {}),
      }))
    }
    const assignments = (Array.isArray(assignmentsRaw) ? assignmentsRaw : []) as Record<string, unknown>[]
    return (
      <div className="space-y-3">
        {cast.length > 0 && (
          <Section title={`本集出场人物（${cast.length}）`}>
            <div className="flex flex-wrap gap-1.5">
              {cast.map((c, i) => (
                <span key={i} className="px-2 py-0.5 text-[13px] rounded bg-purple-900/40 text-purple-200">
                  {String(c.name || c)}
                </span>
              ))}
            </div>
          </Section>
        )}
        {sceneRows.length > 0 && (
          <Section title={`场景（${sceneRows.length}）`}>
            <div className="flex flex-wrap gap-1.5">
              {sceneRows.map((s, i) => (
                <span key={i} className="px-2 py-0.5 text-[13px] rounded bg-sky-900/40 text-sky-200">
                  {String(s.name || s)}
                </span>
              ))}
            </div>
          </Section>
        )}
        {assignments.length > 0 && (
          <Section title="段落分配">
            <div className="space-y-1">
              {assignments.map((a, i) => (
                <div key={i} className="text-[13px] text-gray-300 rounded bg-black/30 border border-gray-800 px-2 py-1">
                  <span className="text-lime-300">段 {String(a.segment_index ?? i + 1)}</span>
                  {a.characters != null && Boolean(a.characters) && <span className="ml-2 text-purple-300">人物 {Array.isArray(a.characters) ? (a.characters as string[]).join("、") : String(a.characters)}</span>}
                  {a.scene != null && Boolean(a.scene) && <span className="ml-2 text-sky-300">场景 {String(a.scene)}</span>}
                </div>
              ))}
            </div>
          </Section>
        )}
      </div>
    )
  }

  // ── segment_storyboard ──
  if (type === "segment_storyboard") {
    const mode = String(outObj.mode || inObj.mode || "shot_list")
    const shots = Array.isArray(outObj.shots) ? (outObj.shots as Record<string, unknown>[]) : []
    const cells = Array.isArray(outObj.cells) ? (outObj.cells as Record<string, unknown>[]) : []
    const gridUrl = (outObj.local_url as string) || (outObj.url as string) || ""
    const promptText = topPrompt || (inObj.prompt as string) || (outObj.prompt as string) || ""
    const refs = Array.isArray(outObj.reference_images) ? (outObj.reference_images as string[]) : []
    const grid = (outObj.grid as string) || (inObj.layout != null ? String(inObj.layout) : "")
    const spec = pickMediaSpec(outObj, inObj)
    return (
      <div className="space-y-3">
        <Section title="模式">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[13px] px-2 py-0.5 rounded bg-rose-900/40 text-rose-200">
              {mode === "grid" ? "多宫格分镜" : "单镜头清单"}
            </span>
            {grid && <span className="text-[12px] px-1.5 py-0.5 rounded bg-black/30 text-gray-300">{grid}</span>}
          </div>
        </Section>
        {gridUrl ? (
          <Section title="分镜图">
            <button
              onClick={() => setLightbox({ src: resolveMediaUrl(gridUrl) || "" })}
              className="block w-full rounded overflow-hidden"
            >
              <img src={resolveMediaUrl(gridUrl) || ""} alt="" className="w-full object-contain bg-black" />
            </button>
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        ) : (
          <Section title="生图规格(尚未出图)">
            <ImagePlaceholder label="未出图 — 调用 node.run(action='render') 生成" />
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        )}
        {cells.length > 0 && (
          <Section title={`宫格说明（${cells.length}）`}>
            <div className="space-y-2">
              {cells.map((c, i) => (
                <div key={i} className="rounded-lg border border-white/10 bg-black/25 p-2.5 text-[13px]">
                  <div className="mb-1.5 flex flex-wrap items-center gap-2 text-[12px]">
                    <span className="font-bold text-rose-300">
                      {c.row != null || c.col != null ? `${String(c.row)}行 ${String(c.col)}列` : `格 ${i + 1}`}
                    </span>
                    {c.time != null && Boolean(c.time) && <span className="rounded bg-white/5 px-1.5 py-0.5 text-gray-300">{String(c.time)}</span>}
                    {c.shot_type != null && Boolean(c.shot_type) && <span className="rounded bg-white/5 px-1.5 py-0.5 text-gray-300">{String(c.shot_type)}</span>}
                  </div>
                  {c.composition != null && Boolean(c.composition) && <div className="text-gray-200">{String(c.composition)}</div>}
                  {c.character_blocking != null && Boolean(c.character_blocking) && <div className="mt-1 text-gray-300">站位：{String(c.character_blocking)}</div>}
                  {c.action != null && Boolean(c.action) && <div className="mt-1 text-gray-300">动作：{String(c.action)}</div>}
                  {c.camera != null && Boolean(c.camera) && <div className="mt-1 text-gray-400">镜头：{String(c.camera)}</div>}
                  {c.lighting != null && Boolean(c.lighting) && <div className="mt-1 text-gray-400">光线：{String(c.lighting)}</div>}
                  {c.continuity != null && Boolean(c.continuity) && <div className="mt-1 text-emerald-200/90">连续性：{String(c.continuity)}</div>}
                  {c.content != null && Boolean(c.content) && <div className="mt-1 text-gray-200">{String(c.content)}</div>}
                  {c.dialogue != null && Boolean(c.dialogue) && <div className="text-purple-300 mt-0.5 text-[12px]">「{String(c.dialogue)}」</div>}
                </div>
              ))}
            </div>
          </Section>
        )}
        {shots.length > 0 && (
          <Section title={`镜头清单（${shots.length}）`}>
            <div className="space-y-1.5">
              {shots.map((sh, i) => (
                <div key={i} className="rounded bg-black/30 border border-gray-800 p-2 text-[13px]">
                  <div className="flex gap-2 mb-0.5">
                    <span className="text-rose-300 font-bold">镜 {(sh.index as number) ?? i + 1}</span>
                    {sh.shot_type != null && Boolean(sh.shot_type) && <span className="text-gray-400">{String(sh.shot_type)}</span>}
                    {sh.duration != null && Boolean(sh.duration) && <span className="text-gray-500">{String(sh.duration)}s</span>}
                  </div>
                  {sh.action != null && Boolean(sh.action) && <div className="text-gray-300">{String(sh.action)}</div>}
                </div>
              ))}
            </div>
          </Section>
        )}
        {promptText && (
          <Section title="完整提示词">
            <PromptBlock>{promptText}</PromptBlock>
          </Section>
        )}
      </div>
    )
  }

  // ── shot_first_frame / shot_last_frame / segment_story_template ──
  if (type === "shot_first_frame" || type === "shot_last_frame" || type === "segment_story_template") {
    const stages = Array.isArray(outObj.stages) ? (outObj.stages as StageData[]) : []
    const promptStage = stages.find((s) => /提示词/.test(s.name))
    const imageStage = stages.find((s) => /图|首帧|尾帧|模板/.test(s.name) && !/提示词/.test(s.name))
    // 顶层 url(node.run(action='render') 平铺写在 output 顶层)
    const url = imageStage
      ? resolveMediaUrl(imageStage.local_url || imageStage.url)
      : (typeof outObj.url === "string" ? resolveMediaUrl(outObj.url as string) : null)
    const imageStageStatus = (imageStage as Record<string, unknown> | undefined)?.status
    const imgErr = imageStageStatus === "failed"
      ? (
          ((imageStage as Record<string, unknown> | undefined)?.error as string | undefined) ||
          (outObj.image_error as string | undefined)
        )
      : undefined
    const promptText =
      topPrompt ||
      (inObj.prompt as string) ||
      (promptStage?.prompt as string) ||
      (outObj.prompt as string) ||
      ""
    const spec = pickMediaSpec(outObj, inObj, imageStage as Record<string, unknown> | undefined)
    const refsShot = (Array.isArray(outObj.reference_images) && outObj.reference_images.length > 0
      ? outObj.reference_images
      : Array.isArray(inObj.reference_images) ? inObj.reference_images : []
    ) as unknown[]
    const continuityNotes = (Array.isArray(outObj.continuity_notes) ? outObj.continuity_notes : []) as unknown[]
    const layoutModules = (Array.isArray(outObj.layout_modules) ? outObj.layout_modules : []) as unknown[]
    const styleTags = (Array.isArray(outObj.style_tags) ? outObj.style_tags : []) as unknown[]
    return (
      <div className="space-y-3">
        {url ? (
          <Section title="图片">
            <button onClick={() => setLightbox({ src: url })} className="block w-full rounded overflow-hidden">
              <img src={url} alt="" className="w-full object-contain bg-black" />
            </button>
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsShot} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        ) : (
          <Section title="生图规格(尚未出图)">
            <ImagePlaceholder label="未出图 — 调用 node.run(action='render') 生成" />
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsShot} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        )}
        {imgErr && (
          <Section title="图片错误">
            <div className="rounded border border-red-900/60 bg-red-950/40 px-2.5 py-1.5 text-[12px] text-red-300 whitespace-pre-wrap">{imgErr}</div>
          </Section>
        )}
        {(continuityNotes.length > 0 || layoutModules.length > 0 || styleTags.length > 0) && (
          <Section title="制作要点">
            <div className="flex flex-wrap gap-1.5">
              {[...continuityNotes, ...layoutModules, ...styleTags].map((item, i) => (
                <span key={i} className="rounded-full bg-white/[0.07] px-2 py-1 text-[12px] text-gray-200">
                  {String(item)}
                </span>
              ))}
            </div>
          </Section>
        )}
        {promptText && (
          <Section title="提示词">
            <PromptBlock>{promptText}</PromptBlock>
          </Section>
        )}
      </div>
    )
  }

  // ── segment_video_prompt ──
  if (type === "segment_video_prompt") {
    const prompt = topPrompt || (inObj.prompt as string) || (outObj.prompt as string) || (outObj.video_prompt as string) || ""
    const refs = (Array.isArray(outObj.reference_images) && outObj.reference_images.length > 0
      ? outObj.reference_images
      : Array.isArray(inObj.reference_images) ? inObj.reference_images : []
    ) as unknown[]
    const anchors = (Array.isArray(outObj.visual_anchors) ? outObj.visual_anchors : []) as unknown[]
    const timeline = (Array.isArray(outObj.timeline)
      ? outObj.timeline
      : Array.isArray(outObj.shots) ? outObj.shots : []
    ) as Record<string, unknown>[]
    const constraints = (Array.isArray(outObj.continuity_constraints) ? outObj.continuity_constraints : []) as unknown[]
    const duration = outObj.duration_seconds || inObj.duration_seconds
    const motionIntensity = outObj.motion_intensity
    const cameraMotion = outObj.camera_motion
    const hasVideoSpec = duration != null || motionIntensity != null || cameraMotion != null
    return (
      <div className="space-y-3">
        {hasVideoSpec && (
          <Section title="视频规格">
            <div className="flex flex-wrap gap-1.5">
              {duration != null && <span className="rounded-full bg-white/[0.07] px-2 py-1 text-[12px] text-gray-200">{String(duration)} 秒</span>}
              {motionIntensity != null && <span className="rounded-full bg-white/[0.07] px-2 py-1 text-[12px] text-gray-200">运动强度 {String(motionIntensity)}</span>}
              {cameraMotion != null && <span className="rounded-full bg-white/[0.07] px-2 py-1 text-[12px] text-gray-200">镜头 {String(cameraMotion)}</span>}
            </div>
            <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
          </Section>
        )}
        {!hasVideoSpec && <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />}
        {anchors.length > 0 && (
          <Section title="视觉锚点">
            <div className="flex flex-wrap gap-1.5">
              {anchors.map((anchor, i) => (
                <span key={i} className="rounded-full bg-cyan-400/10 px-2 py-1 text-[12px] text-cyan-100">
                  {String(anchor)}
                </span>
              ))}
            </div>
          </Section>
        )}
        {timeline.length > 0 && (
          <Section title={`运动时间轴 (${timeline.length})`}>
            <div className="space-y-2">
              {timeline.map((item, i) => (
                <div key={i} className="rounded-lg border border-white/10 bg-black/25 p-2.5 text-[13px]">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-cyan-200">{String(item.time || `镜头 ${i + 1}`)}</span>
                    {item.camera != null && Boolean(item.camera) && <span className="rounded bg-white/5 px-1.5 py-0.5 text-[12px] text-gray-300">{String(item.camera)}</span>}
                  </div>
                  {item.subject_motion != null && Boolean(item.subject_motion) && <div className="text-gray-200">主体：{String(item.subject_motion)}</div>}
                  {item.environment_motion != null && Boolean(item.environment_motion) && <div className="mt-1 text-gray-300">环境：{String(item.environment_motion)}</div>}
                  {item.continuity != null && Boolean(item.continuity) && <div className="mt-1 text-emerald-200/90">连续性：{String(item.continuity)}</div>}
                  {item.action != null && Boolean(item.action) && <div className="mt-1 text-gray-300">动作：{String(item.action)}</div>}
                </div>
              ))}
            </div>
          </Section>
        )}
        {constraints.length > 0 && (
          <Section title="连续性约束">
            <div className="flex flex-wrap gap-1.5">
              {constraints.map((item, i) => (
                <span key={i} className="rounded-full bg-emerald-400/10 px-2 py-1 text-[12px] text-emerald-100">
                  {String(item)}
                </span>
              ))}
            </div>
          </Section>
        )}
        {prompt && (
          <Section title="完整视频提示词">
            <PromptBlock>{prompt}</PromptBlock>
          </Section>
        )}
      </div>
    )
  }

  // ── segment_video_clip ──
  if (type === "segment_video_clip") {
    const url = (outObj.url as string) || (outObj.local_url as string) || ""
    const status = (outObj.status as string) || ""
    const dur = outObj.duration_seconds
    return (
      <div className="space-y-3">
        {url ? (
          <Section title="段落视频">
            <InlineVideoPreview
              src={resolveMediaUrl(url) || url}
              title="段落视频"
              className="w-full bg-black"
              onOpen={() => setVideoLightbox({ src: resolveMediaUrl(url) || url, title: "段落视频" })}
            />
          </Section>
        ) : (
          <div className="rounded bg-cyan-950/30 border border-cyan-900/40 p-3 text-[14px] text-cyan-200">
            视频生成{status ? `（${status}）` : ""}…后端接入后会显示播放器。
          </div>
        )}
        {dur != null && <div className="text-[13px] text-gray-500">时长 {String(dur)}s</div>}
      </div>
    )
  }

  // ── 兜底 ──
  return null
}

export default function NodeDetailPanel({
  nodeId,
  projectId,
  onClose,
  onRerun,
  onDelete,
  onSaved,
  onRequestStoryRevision,
  actionDisabled = false,
  presentation = "modal",
}: Props) {
  const storeProjectId = useProjectStore((s) => s.currentProject?.id)
  const updateCanvasNode = useCanvasStore((s) => s.updateNode)
  const appendMessage = useChatStore((s) => s.appendMessage)
  const currentProjectId = projectId || storeProjectId
  const [data, setData] = useState<NodeFull | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lightbox, setLightbox] = useState<{ src: string; alt?: string } | null>(null)
  const [videoLightbox, setVideoLightbox] = useState<VideoLightboxState | null>(null)
  const [debugRawEnabled, setDebugRawEnabled] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<EditableNodeDraft>(EMPTY_DRAFT)
  const [saving, setSaving] = useState(false)
  const [rerunning, setRerunning] = useState(false)
  const [uploadingRefs, setUploadingRefs] = useState(false)
  const [switchingHistoryId, setSwitchingHistoryId] = useState<string | null>(null)
  const [detailReloadTick, setDetailReloadTick] = useState(0)
  const [audioProviders, setAudioProviders] = useState<AudioProviderOption[]>([])
  const [audioConfigError, setAudioConfigError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const detailProjectId = currentProjectId
    const loadNode = async (showLoading: boolean) => {
      if (showLoading) setLoading(true)
      setError(null)
      try {
        if (!detailProjectId) return
        const result = await getProjectNodeDetails<NodeFull>(detailProjectId, nodeId)
        if (cancelled) return
        if ((result as unknown as { error?: string })?.error) {
          setError((result as unknown as { error: string }).error)
        } else {
          setData(result)
          updateCanvasNode(result.id, canvasPatchFromNode(result))
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled && showLoading) setLoading(false)
      }
    }

    setLoading(true)
    setError(null)
    setData(null)
    if (!currentProjectId) {
      setError("缺少项目 ID，无法读取节点详情。")
      setLoading(false)
      return () => {
        cancelled = true
      }
    }
    void loadNode(true)
    timer = window.setInterval(() => void loadNode(false), 2500)
    return () => {
      cancelled = true
      if (timer) window.clearInterval(timer)
    }
  }, [currentProjectId, nodeId, detailReloadTick, updateCanvasNode])

  useEffect(() => {
    setEditing(false)
    setDraft(EMPTY_DRAFT)
    setSaving(false)
    setRerunning(false)
    setUploadingRefs(false)
    setSwitchingHistoryId(null)
    setLightbox(null)
    setVideoLightbox(null)
  }, [nodeId])

  useEffect(() => {
    if (data && !editing) setDraft(draftFromNode(data))
  }, [data, editing])

  useEffect(() => {
    let cancelled = false
    const loadAudioProviders = async () => {
      try {
        const result = await getRuntimeConfigFile<{
          parsed?: { media_providers?: AudioProviderOption[] }
        }>(true)
        if (cancelled) return
        const providers = result.parsed?.media_providers || []
        setAudioProviders(providers.filter((provider) => provider.kind === "audio"))
        setAudioConfigError(null)
      } catch (err) {
        if (cancelled) return
        setAudioConfigError(err instanceof Error ? err.message : String(err))
      }
    }
    void loadAudioProviders()
    const handleConfigUpdate = () => void loadAudioProviders()
    window.addEventListener("drama:runtime-config-updated", handleConfigUpdate)
    return () => {
      cancelled = true
      window.removeEventListener("drama:runtime-config-updated", handleConfigUpdate)
    }
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !lightbox && !videoLightbox) onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose, lightbox, videoLightbox])

  useEffect(() => {
    try {
      setDebugRawEnabled(
        process.env.NODE_ENV !== "production" ||
        window.localStorage.getItem("drama.nodeDebugRaw") === "1",
      )
    } catch {
      setDebugRawEnabled(process.env.NODE_ENV !== "production")
    }
  }, [])

  const style = getNodeStyle(data?.type)
  const status = data?.status ?? "idle"
  const statusBadge = STATUS_LABELS[status] ?? STATUS_LABELS.idle
  const renderState = renderStateFromNode(data)
  const displayError = data ? nodeDisplayError(data) : ""

  const media = data ? collectMedia(data.output) : []

  const isModal = presentation === "modal"
  const panelClass = isModal
    ? "fixed left-1/2 top-1/2 z-[70] flex max-h-[calc(100dvh-28px)] w-[calc(100vw-20px)] flex-col overflow-hidden rounded-lg border border-white/[0.09] bg-[#0f131b]/96 shadow-[0_28px_90px_rgba(0,0,0,0.66)] backdrop-blur-xl sm:max-h-[84vh] sm:w-[min(900px,calc(100vw-72px))]"
    : "absolute bottom-3 left-3 right-3 top-3 z-30 flex flex-col overflow-hidden rounded-lg border border-white/[0.09] bg-[#0f131b]/96 shadow-2xl backdrop-blur sm:left-auto sm:w-[380px]"
  const canRequestStoryRevision = Boolean(data && onRequestStoryRevision && STORY_REVISION_NODE_TYPES.has(data.type))
  const canRerunMediaNode = Boolean(data && onRerun && MEDIA_RERUN_NODE_TYPES.has(data.type))
  const mediaRunTarget = data?.type === "video" ? "视频" : data?.type === "audio" ? "音频" : "图片"
  const mediaRunLabel = data && (data.status === "idle" || data.status === "queued")
    ? `生成${mediaRunTarget}`
    : `重新生成${mediaRunTarget}`
  const footerActionCount = [canRequestStoryRevision, canRerunMediaNode].filter(Boolean).length
  const footerClass = footerActionCount >= 3
    ? "grid grid-cols-3 gap-2 border-t border-white/[0.08] bg-[#111722]/92 px-3 py-3 shrink-0 sm:px-4"
    : footerActionCount === 2
    ? "grid grid-cols-2 gap-2 border-t border-white/[0.08] bg-[#111722]/92 px-3 py-3 shrink-0 sm:px-4"
    : "border-t border-white/[0.08] bg-[#111722]/92 px-3 py-3 shrink-0 sm:px-4"

  const canEdit = Boolean(data && EDITABLE_NODE_TYPES.has(data.type))
  const actionBusy = actionDisabled || rerunning
  const selectedAudioProvider = data?.type === "audio" ? resolveAudioProvider(draft.model, audioProviders) : undefined
  const selectedAudioMode = data?.type === "audio"
    ? audioProviderModeFromFormat(selectedAudioProvider?.api_format)
    : "unknown"

  const startEdit = () => {
    if (!data) return
    setDraft(draftFromNode(data))
    setEditing(true)
  }

  const cancelEdit = () => {
    if (data) setDraft(draftFromNode(data))
    setEditing(false)
  }

  const updateDraft = (patch: Partial<EditableNodeDraft>) => {
    setDraft((current) => ({ ...current, ...patch }))
  }

  const uploadReferenceFiles = async (files: FileList | null) => {
    if (!files?.length || !currentProjectId) return
    setUploadingRefs(true)
    setError(null)
    try {
      const uploaded = await Promise.all(Array.from(files).map((file) => uploadFile(currentProjectId, file)))
      const refs = uploaded
        .map((item) => item.rel_path || item.url || item.mention || "")
        .filter(Boolean)
      setDraft((current) => ({
        ...current,
        reference_images: Array.from(new Set([...current.reference_images, ...refs])),
      }))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUploadingRefs(false)
    }
  }

  const saveDraft = async () => {
    if (!data || !currentProjectId) return
    setSaving(true)
    setError(null)
    try {
      const result = await updateProjectNodeDetails<NodeFull>(
        currentProjectId,
        data.id,
        payloadFromDraft(data, draft, selectedAudioMode),
      )
      setData(result)
      setDraft(draftFromNode(result))
      setEditing(false)
      updateCanvasNode(result.id, canvasPatchFromNode(result))
      await onSaved?.(result)
      if (Array.isArray(result.changes) && result.changes.length > 0) {
        appendMessage({
          id: crypto.randomUUID?.() ?? `node-edit-change-${Date.now()}`,
          role: "assistant",
          content: "",
          createdAt: new Date().toISOString(),
          changeCard: { tool: "node.update", changes: result.changes },
        })
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const switchHistoryVersion = async (entry: MediaHistoryEntry) => {
    if (!data || !currentProjectId || switchingHistoryId) return
    setSwitchingHistoryId(entry.id)
    setError(null)
    try {
      const result = await switchProjectNodeHistory<NodeFull>(
        currentProjectId,
        data.id,
        entry.id.startsWith("history-")
          ? { index: entry.index }
          : { history_id: entry.id },
      )
      setData(result)
      setDraft(draftFromNode(result))
      updateCanvasNode(result.id, canvasPatchFromNode(result))
      setDetailReloadTick((tick) => tick + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSwitchingHistoryId(null)
    }
  }

  return (
    <>
      {isModal && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[60] bg-black/55 backdrop-blur-sm"
          onClick={onClose}
        />
      )}
      <motion.div
        initial={isModal ? { opacity: 0, scale: 0.96, x: "-50%", y: "-50%" } : { x: 32, opacity: 0 }}
        animate={isModal ? { opacity: 1, scale: 1, x: "-50%", y: "-50%" } : { x: 0, opacity: 1 }}
        exit={isModal ? { opacity: 0, scale: 0.96, x: "-50%", y: "-50%" } : { x: 32, opacity: 0 }}
        transition={{ duration: 0.18, ease: "easeOut" }}
        className={panelClass}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex shrink-0 items-start gap-3 border-b border-white/[0.08] bg-[#111722]/92 px-3.5 py-3 sm:px-4">
          <div
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-[12px] font-semibold tracking-tight"
            style={{ background: `${style.color}22`, border: `1px solid ${style.color}55` }}
          >
            {style.icon}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[10px] font-semibold uppercase tracking-[0.16em]" style={{ color: style.color }}>
              {style.label}
            </div>
            <div className="mt-1 break-words text-[15px] font-semibold leading-5 text-zinc-50">
              {data?.title || (loading ? "加载中…" : "未命名")}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className={`rounded px-1.5 py-0.5 text-[10px] ${statusBadge.cls}`}>
                {statusBadge.label}
              </span>
              {renderState && (
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] ${
                    renderState === "stale"
                      ? "bg-amber-900/45 text-amber-200"
                      : "bg-emerald-900/35 text-emerald-200"
                  }`}
                >
                  {renderState === "stale" ? "图片未更新" : "图片最新"}
                </span>
              )}
              {data?.version && data.version > 1 && (
                <span
                  className="rounded px-1.5 py-0.5 text-[10px] font-bold"
                  style={{ background: `${style.color}33`, color: style.color }}
                >
                  v{data.version}
                </span>
              )}
              {data?.creator && (
                <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-zinc-400">
                  创建人：{data.creator === "user" ? "用户" : "Agent"}
                </span>
              )}
            </div>
          </div>
          {canEdit && !editing && (
            <button
              type="button"
              onClick={startEdit}
              className="rounded-lg border border-white/[0.1] bg-white/[0.06] px-3 py-2 text-xs font-medium text-zinc-100 transition hover:bg-white/[0.1]"
            >
              编辑
            </button>
          )}
          {canEdit && editing && (
            <div className="flex shrink-0 items-center gap-2">
              <button
                type="button"
                onClick={cancelEdit}
                disabled={saving}
                className="rounded-lg border border-white/[0.1] bg-transparent px-3 py-2 text-xs font-medium text-zinc-300 transition hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-45"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void saveDraft()}
                disabled={saving}
                className="rounded-lg bg-cyan-500 px-3 py-2 text-xs font-semibold text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-45"
              >
                {saving ? "保存中..." : "保存"}
              </button>
            </div>
          )}
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm leading-none text-zinc-500 transition-colors hover:bg-white/10 hover:text-white"
            aria-label="关闭"
          >
            ×
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 space-y-3 overflow-y-auto bg-[#090c12] px-3.5 py-3.5 sm:px-4">
          {loading && (
            <div className="text-xs text-gray-500 py-8 text-center">加载节点详情…</div>
          )}

          {error && (
            <div className="rounded border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
              错误：{error}
            </div>
          )}

          {data && !loading && (
            editing ? (
              <NodeEditView
                node={data}
                draft={draft}
                audioProviders={audioProviders}
                audioConfigError={audioConfigError}
                projectId={currentProjectId}
                saving={saving}
                uploading={uploadingRefs}
                setLightbox={setLightbox}
                onChange={updateDraft}
                onUploadRefs={uploadReferenceFiles}
                onSave={saveDraft}
              />
            ) : (
            <>
              {/* Typed renderer (12 类节点的人性化视图) */}
              <TypedRenderer
                node={data}
                type={data.type}
                input={data.input}
                output={data.output}
                nodePrompt={data.prompt}
                nodeStatus={data.status}
                projectId={currentProjectId}
                setLightbox={setLightbox}
                setVideoLightbox={setVideoLightbox}
                onEdited={() => setDetailReloadTick((tick) => tick + 1)}
                onSwitchHistory={switchHistoryVersion}
                switchingHistoryId={switchingHistoryId}
              />
              {!TYPED_RENDERED_NODE_TYPES.has(data.type) && (
                <GenericNodeDetails node={data} mediaCount={media.length} />
              )}

              {/* Media gallery (兜底,如果 typed renderer 没覆盖到的媒体也展示) */}
              {media.length > 0 && !TYPED_RENDERED_NODE_TYPES.has(data.type) && (
                <Section title={`媒体 (${media.length})`}>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    {media.map((m, i) => (
                      <div
                        key={i}
                        className="overflow-hidden rounded-lg bg-black/40 ring-1 ring-white/[0.08]"
                      >
                        {m.kind === "image" ? (
                          <button
                            onClick={() => setLightbox({ src: m.src, alt: m.label })}
                            className="block w-full group"
                            title="点击放大"
                          >
                            <img
                              src={m.src}
                              alt={m.label || ""}
                              className="h-40 w-full object-cover transition-opacity group-hover:opacity-90"
                              onError={(e) => {
                                ;(e.target as HTMLImageElement).style.opacity = "0.2"
                              }}
                            />
                          </button>
                        ) : m.kind === "video" ? (
	                          <InlineVideoPreview
	                            src={m.src}
	                            poster={m.poster}
	                            title={m.label || "视频预览"}
	                            className="h-40 w-full bg-black object-cover"
	                            onOpen={() => setVideoLightbox({ src: m.src, poster: m.poster, title: m.label || "视频预览" })}
	                          />
                        ) : (
                          <div className="p-3">
                            <InlineAudioPreview src={m.src} title={m.label || "音频预览"} />
                          </div>
                        )}
                        {m.label && (
                          <div className="border-t border-white/[0.06] px-2.5 py-1.5 text-[10px] text-zinc-400">
                            {m.label}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </Section>
              )}

              {/* Error */}
              {displayError && (
                <Section title="错误">
                  <div className="rounded border border-red-900/60 bg-red-950/40 px-3 py-2 text-[13px] text-red-300 whitespace-pre-wrap break-words">
                    {displayError}
                  </div>
                </Section>
              )}

              {data.output == null && data.input == null && !data.prompt && !displayError && media.length === 0 && (
                <div className="text-xs text-gray-500 italic py-6 text-center">
                  {status === "running" || status === "queued" || status === "idle"
                    ? "节点尚未产出内容…"
                    : "无可展示内容"}
                </div>
              )}

              {debugRawEnabled && <NodeDebugSection node={data} />}
            </>
            )
          )}
        </div>

        {/* Footer */}
        {data && !editing && (status !== "running" || rerunning) && (canRequestStoryRevision || canRerunMediaNode) && (
          <div className={footerClass}>
            {canRequestStoryRevision && (
	              <button
	                onClick={() => onRequestStoryRevision?.(data.id)}
	                disabled={actionBusy}
	                className="rounded-md border border-amber-500/40 bg-amber-950/35 py-2 text-sm font-medium text-amber-100 transition-colors hover:bg-amber-900/45 disabled:cursor-not-allowed disabled:opacity-45"
	              >
                修改剧情
              </button>
            )}
            {canRerunMediaNode && (
	              <button
	                onClick={() => {
                    setError(null)
	                  setRerunning(true)
	                  setData((current) => current ? { ...current, status: "running", error_message: null } : current)
	                  updateCanvasNode(data.id, { status: "running", error: undefined, error_message: undefined })
	                  Promise.resolve(onRerun?.(data.id))
	                    .then(() => setDetailReloadTick((tick) => tick + 1))
	                    .catch((error) => {
                        const message = error instanceof Error ? error.message : String(error)
                        setError(message)
                        setData((current) => current ? { ...current, status: "failed", error_message: message } : current)
                        updateCanvasNode(data.id, { status: "failed", error: message, error_message: message })
                      })
	                    .finally(() => setRerunning(false))
	                }}
	                disabled={actionBusy}
	                className="flex items-center justify-center gap-2 rounded-md border border-cyan-200/20 bg-cyan-300 px-3 py-2.5 text-sm font-semibold text-cyan-950 shadow-[0_10px_26px_rgba(34,211,238,0.18)] transition hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-55"
	              >
	                {rerunning && <span className="h-3.5 w-3.5 rounded-full border-2 border-cyan-950/40 border-t-transparent animate-spin" />}
	                {rerunning ? "生成中..." : mediaRunLabel}
	              </button>
            )}
          </div>
        )}
      </motion.div>

      {lightbox && <Lightbox src={lightbox.src} alt={lightbox.alt} onClose={() => setLightbox(null)} />}
      {videoLightbox && (
        <VideoLightbox
          src={videoLightbox.src}
          poster={videoLightbox.poster}
          title={videoLightbox.title}
          onClose={() => setVideoLightbox(null)}
        />
      )}
    </>
  )
}
