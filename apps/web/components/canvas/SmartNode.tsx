import { memo, useCallback, useEffect, useRef, useState } from "react"
import { Handle, NodeResizer, Position, useUpdateNodeInternals, type NodeProps } from "reactflow"
import { motion } from "framer-motion"
import { cn } from "@/lib/utils"
import { callTool, getProjectNodes, resolveMediaUrl } from "@/lib/api"
import { canvasNodeDisplayText } from "@/lib/nodeDisplay"
import { useCanvasStore } from "@/stores/canvasStore"
import { useProjectStore } from "@/stores/projectStore"
import { getNodeStyle } from "./nodeStyles"

export interface StageErrorDetail {
  error?: string
  error_kind?: string
  http_code?: number
  provider_msg?: string
  endpoint?: string
  provider?: string
  model?: string
}

export interface StageData {
  name: string
  status?: "running" | "completed" | "failed" | string
  progress?: unknown
  poll_status?: unknown
  poll_count?: unknown
  prompt?: string
  url?: string
  local_url?: string
  remote_url?: string
  composite_url?: string
  thumbnail_url?: string
  poster?: string
  last_frame_url?: string
  local_path?: string
  asset_id?: string
  duration_seconds?: number
  error?: string
  error_detail?: StageErrorDetail
  // 图片元数据（生成参数）
  size?: string
  size_requested?: string
  size_final?: string
  aspect_ratio?: string
  ratio?: string | number
  resolution?: string
  output_size?: string
  quality?: string
  downgraded?: boolean
  provider?: string
  model?: string
  width?: number
  height?: number
}

interface ImageGridPreviewCell {
  cell_id?: string
  index?: number
  row?: number
  col?: number
  title?: string
  url?: string
  local_url?: string
  width?: number
  height?: number
  empty?: boolean
}

interface PreviewData {
  type?: string
  text?: string
  status?: unknown
  progress?: unknown
  poll_status?: unknown
  poll_count?: unknown
  subject?: string
  stages?: StageData[]
  items?: { name: string; role_type?: string; identity?: string }[]
  name?: string
  role_type?: string
  identity?: string
  traits?: string[]
  episode_count?: number
  episodes?: { num: number; title: string }[]
  summary?: string
  scene_count?: number
  score?: number
  shot_count?: number
  mode?: string
  shots?: { index?: number; shot_type?: string; action?: string; duration?: number }[]
  url?: string
  local_url?: string
  remote_url?: string
  composite_url?: string
  poster?: string
  thumbnail_url?: string
  format?: unknown
  duration_seconds?: unknown
  width?: number
  height?: number
  aspect_ratio?: string
  ratio?: string | number
  size?: string
  size_requested?: string
  size_final?: string
  resolution?: string
  output_size?: string
  panorama?: boolean
  is_panorama?: boolean
  projection?: string
  panorama_capture?: boolean
  grid?: { rows?: number; cols?: number }
  cells?: ImageGridPreviewCell[]
  prompt?: string
}

interface NodeData {
  type?: string
  title?: string
  status?: string
  model?: string
  error?: string
  output?: unknown
  prompt?: string
  previewText?: string
  renderState?: string
  preview?: PreviewData
  group_id?: string
  group_label?: string
  layout_strategy?: string
  episodeCount?: number
  version?: number
  superseded?: boolean
  supersedes_id?: string
  publicId?: number | string | null
  // Identifiers and metadata for the detail modal:
  nodeId?: string
  createdAt?: string
  updatedAt?: string
  canvasWidth?: number
  canvasHeight?: number
  canvasSizeMode?: "manual"
  input?: Record<string, unknown>
  workflow?: Record<string, unknown>
  workflowStepPrompt?: string
  workflowReferences?: Array<{ ref?: string; role?: string }>
  workflowDependsOn?: string[]
  referenceCount?: number
  referenceThumbs?: Array<{ id: string; src: string; title?: string; publicId?: number | string | null }>
}

const CARD_WIDTH = 260
const CARD_HEIGHT = 176
const MEDIA_TARGET_AREA = CARD_WIDTH * CARD_HEIGHT
const MEDIA_MIN_WIDTH = 128
const MEDIA_MAX_WIDTH = 340
const MEDIA_MIN_HEIGHT = 96
const MEDIA_MAX_HEIGHT = 300
const NODE_PORT_GUTTER = 18
const NODE_PORT_INSET = 3
const NODE_MIN_WIDTH = 160
const NODE_MIN_HEIGHT = 110
const NODE_MAX_WIDTH = 900
const NODE_MAX_HEIGHT = 720
const GRID_PRESETS = [
  { label: "2x2", rows: 2, cols: 2 },
  { label: "2x3", rows: 2, cols: 3 },
  { label: "3x2", rows: 3, cols: 2 },
  { label: "3x3", rows: 3, cols: 3 },
] as const

type GridToolMode = "idle" | "choosing" | "editing"

function isImageStageName(name: string | undefined): boolean {
  return /图|首帧|尾帧|模板|参考|image|storyboard/i.test(name ?? "") && !/提示词|prompt/i.test(name ?? "")
}

function imageFromPreview(preview?: PreviewData): { primary: string; secondary?: string; status?: string; width?: number; height?: number } | null {
  if (!preview) return null
  if (preview.type === "fusion" && Array.isArray(preview.stages)) {
    const imageStage = preview.stages.find((stage) => isImageStageName(stage.name) && (stage.local_url || stage.url || stage.remote_url || stage.composite_url))
      ?? preview.stages.find((stage) => isImageStageName(stage.name) && stage.status === "running")
    if (!imageStage) return null
    const primary = resolveMediaUrl(imageStage.local_url || imageStage.url || imageStage.composite_url)
    const secondary = resolveMediaUrl(imageStage.remote_url)
    return primary
      ? { primary, secondary, status: imageStage.status, width: imageStage.width, height: imageStage.height }
      : { primary: "", secondary, status: imageStage.status, width: imageStage.width, height: imageStage.height }
  }
  if ((preview.type === "image" || preview.type === "image_grid" || preview.type === "storyboard") && (preview.local_url || preview.url || preview.remote_url || preview.composite_url)) {
    return {
      primary: resolveMediaUrl(preview.local_url || preview.url || preview.composite_url),
      secondary: resolveMediaUrl(preview.remote_url),
      status: "completed",
      width: preview.width,
      height: preview.height,
    }
  }
  return null
}

function videoFromPreview(preview?: PreviewData): { src: string; poster?: string; width?: number; height?: number } | null {
  if (!preview) return null
  if (preview.type === "fusion" && Array.isArray(preview.stages)) {
    const videoStage = preview.stages.find((stage) => {
      const src = stage.local_url || stage.url || stage.remote_url
      return /视频|video|clip/i.test(stage.name ?? "") && typeof src === "string" && src.length > 0
    })
    const src = videoStage ? resolveMediaUrl(videoStage.local_url || videoStage.url || videoStage.remote_url) : ""
    return src ? { src, width: videoStage?.width, height: videoStage?.height } : null
  }
  if (preview.type === "video" || [preview.local_url, preview.url, preview.remote_url].some((item) => typeof item === "string" && /\.(mp4|webm|mov)(\?|#|$)/i.test(item))) {
    const src = resolveMediaUrl(preview.local_url || preview.url || preview.remote_url)
    const poster = resolveMediaUrl(preview.poster || preview.thumbnail_url)
    return src ? { src, poster, width: preview.width, height: preview.height } : null
  }
  return null
}

function audioFromPreview(preview?: PreviewData): { src: string; format?: string; duration?: string } | null {
  if (!preview) return null
  if (preview.type === "fusion" && Array.isArray(preview.stages)) {
    const audioStage = preview.stages.find((stage) => {
      const src = stage.local_url || stage.url || stage.remote_url
      return /音频|audio|sound/i.test(stage.name ?? "") && typeof src === "string" && src.length > 0
    })
    const src = audioStage ? resolveMediaUrl(audioStage.local_url || audioStage.url || audioStage.remote_url) : ""
    return src ? { src, duration: audioStage?.duration_seconds ? `${audioStage.duration_seconds}s` : undefined } : null
  }
  if (preview.type === "audio" || [preview.local_url, preview.url, preview.remote_url].some((item) => typeof item === "string" && /\.(mp3|wav|m4a|aac|ogg|flac)(\?|#|$)/i.test(item))) {
    const src = resolveMediaUrl(preview.local_url || preview.url || preview.remote_url)
    const format = typeof preview.format === "string" ? preview.format : undefined
    const duration = preview.duration_seconds != null ? `${preview.duration_seconds}s` : undefined
    return src ? { src, format, duration } : null
  }
  return null
}

function objectFromUnknown(value: unknown): Record<string, unknown> | null {
  if (!value) return null
  if (typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
  if (typeof value !== "string" || !value.trim().startsWith("{")) return null
  try {
    const parsed = JSON.parse(value)
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function scalarText(value: unknown): string {
  if (typeof value === "string") return value.trim()
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

function hasTextBodyValue(value: unknown): boolean {
  const direct = scalarText(value)
  if (direct) return true
  const obj = objectFromUnknown(value)
  if (!obj) return false
  for (const key of ["content", "full_text", "story_text", "text", "script", "output", "reply", "response", "summary", "description"]) {
    if (scalarText(obj[key])) return true
  }
  const result = objectFromUnknown(obj.result)
  return result ? hasTextBodyValue(result) : false
}

function hasTextProduct(data: NodeData): boolean {
  if (hasTextBodyValue(data.output)) return true
  const input = data.input || {}
  return hasTextBodyValue({
    content: input.content,
    full_text: input.full_text,
    story_text: input.story_text,
    text: input.text,
    script: input.script,
    output: input.output,
    reply: input.reply,
    response: input.response,
  })
}

function videoMimeType(src: string): string {
  const path = src.split(/[?#]/, 1)[0]?.toLowerCase() || ""
  if (path.endsWith(".webm")) return "video/webm"
  if (path.endsWith(".mov")) return "video/quicktime"
  return "video/mp4"
}

function isPanoramaImage(preview?: PreviewData, title?: string, prompt?: string): boolean {
  if (!preview) return false
  if (preview.panorama || preview.is_panorama) return true
  const projection = String(preview.projection ?? "").toLowerCase()
  if (projection === "equirectangular" || projection.includes("360")) return true
  const label = `${title ?? ""}\n${prompt ?? ""}`.toLowerCase()
  return Boolean(
    ratioFromAspectValue(preview.aspect_ratio) === 2 &&
    /全景|panorama|equirectangular|360/.test(label),
  )
}

function ratioFromSize(width?: number, height?: number): number | null {
  if (!width || !height || !Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return null
  }
  return Math.min(3.2, Math.max(0.42, width / height))
}

function ratioFromAspectValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return ratioFromSize(value, 1)
  }
  if (typeof value !== "string") return null
  const text = value.trim().toLowerCase()
  if (!text) return null
  const numeric = Number(text)
  if (Number.isFinite(numeric) && numeric > 0) return ratioFromSize(numeric, 1)
  const pair = text.match(/(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)/)
  if (pair) return ratioFromSize(Number(pair[1]), Number(pair[2]))
  const size = text.match(/(\d{2,5})\s*[x×*]\s*(\d{2,5})/)
  if (size) return ratioFromSize(Number(size[1]), Number(size[2]))
  if (/square|正方|方图/.test(text)) return 1
  if (/portrait|vertical|竖/.test(text)) return ratioFromSize(9, 16)
  if (/landscape|horizontal|横/.test(text)) return ratioFromSize(16, 9)
  return null
}

function hasMediaUrl(...values: unknown[]): boolean {
  return values.some((value) => typeof value === "string" && value.trim().length > 0)
}

function ratioFromPreviewLike(item?: {
  width?: number
  height?: number
  aspect_ratio?: string
  ratio?: string | number
  size?: string
  size_requested?: string
  size_final?: string
  resolution?: string
  output_size?: string
}): number | null {
  if (!item) return null
  return (
    ratioFromSize(item.width, item.height) ||
    ratioFromAspectValue(item.aspect_ratio) ||
    ratioFromAspectValue(item.ratio) ||
    ratioFromAspectValue(item.size) ||
    ratioFromAspectValue(item.size_requested) ||
    ratioFromAspectValue(item.size_final) ||
    ratioFromAspectValue(item.resolution) ||
    ratioFromAspectValue(item.output_size)
  )
}

function fusionMediaStage(preview: PreviewData | undefined, nodeType: "image" | "video"): StageData | undefined {
  if (preview?.type !== "fusion" || !Array.isArray(preview.stages)) return undefined
  if (nodeType === "image") {
    return preview.stages.find((stage) => isImageStageName(stage.name) && hasMediaUrl(stage.local_url, stage.url, stage.remote_url, stage.composite_url))
  }
  return preview.stages.find((stage) =>
    /视频|video|clip/i.test(stage.name ?? "") &&
    hasMediaUrl(stage.local_url, stage.url, stage.remote_url)
  )
}

function previewHasOutput(preview: PreviewData | undefined, nodeType: "image" | "video"): boolean {
  if (!preview) return false
  if (fusionMediaStage(preview, nodeType)) return true
  if (nodeType === "image") {
    return (
      (preview.type === "image" || preview.type === "image_grid" || preview.type === "storyboard") &&
      hasMediaUrl(preview.local_url, preview.url, preview.remote_url, preview.composite_url)
    )
  }
  return (
    preview.type === "video" &&
    hasMediaUrl(preview.local_url, preview.url, preview.remote_url)
  )
}

function ratioFromOutputPreview(preview: PreviewData | undefined, nodeType: "image" | "video"): number | null {
  if (!previewHasOutput(preview, nodeType)) return null
  const stage = fusionMediaStage(preview, nodeType)
  if (stage) return ratioFromPreviewLike(stage)

  const cell = Array.isArray(preview?.cells)
    ? preview.cells.find((item) => item.width && item.height)
    : undefined
  const gridCols = preview?.grid?.cols || 1
  const gridRows = preview?.grid?.rows || 1
  return (
    ratioFromPreviewLike(preview) ||
    (cell ? ratioFromSize((cell.width || 1) * gridCols, (cell.height || 1) * gridRows) : null) ||
    (preview?.type === "image_grid" ? ratioFromSize(gridCols, gridRows) : null)
  )
}

function mediaNodeDimensions(
  preview: PreviewData | undefined,
  nodeType: "image" | "video",
  media?: { width?: number; height?: number } | null,
): { width: number; height: number } {
  if (!previewHasOutput(preview, nodeType)) {
    return { width: CARD_WIDTH, height: CARD_HEIGHT }
  }
  const ratio =
    ratioFromSize(media?.width, media?.height) ||
    ratioFromOutputPreview(preview, nodeType) ||
    CARD_WIDTH / CARD_HEIGHT

  let width = Math.sqrt(MEDIA_TARGET_AREA * ratio)
  const minWidthForRatio = Math.max(MEDIA_MIN_WIDTH, MEDIA_MIN_HEIGHT * ratio)
  const maxWidthForRatio = Math.min(MEDIA_MAX_WIDTH, MEDIA_MAX_HEIGHT * ratio)
  if (minWidthForRatio <= maxWidthForRatio) {
    width = Math.min(maxWidthForRatio, Math.max(minWidthForRatio, width))
  } else {
    width = maxWidthForRatio
  }
  const height = width / ratio
  return { width: Math.round(width), height: Math.round(height) }
}

function statusBorderStyle(status: string, color: string): React.CSSProperties {
  switch (status) {
    case "running":
      return { borderColor: `${color}cc`, background: `linear-gradient(180deg, ${color}18, rgba(18,20,26,0.96))` }
    case "completed":
      return { borderColor: `${color}88`, background: "rgba(18,20,26,0.96)" }
    case "failed":
      return { borderColor: "#ef4444aa", background: "rgba(42,18,22,0.95)" }
    case "queued":
    case "idle":
      return { borderColor: "#3f4654", background: "rgba(18,20,26,0.86)", borderStyle: "dashed" }
    default:
      return { borderColor: "#343a46", background: "rgba(18,20,26,0.95)" }
  }
}

function QueuedIndicator() {
  return (
    <div className="flex items-center gap-1.5 mt-1 text-[11px] text-slate-400">
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-slate-500 animate-pulse" />
      <span>排队中</span>
    </div>
  )
}

function RunningDots() {
  return (
    <div className="flex items-center gap-1 mt-1">
      <div className="w-1.5 h-1.5 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
      <div className="w-1.5 h-1.5 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
      <div className="w-1.5 h-1.5 bg-blue-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
      <span className="text-xs text-blue-400 ml-1">生成中…</span>
    </div>
  )
}

interface MediaProgressInfo {
  percent: number
  label: string
}

function progressPercent(value: unknown): number | null {
  if (value == null || value === "") return null
  const raw = typeof value === "string" ? value.trim().replace(/%$/, "") : value
  const parsed = Number(raw)
  if (!Number.isFinite(parsed)) return null
  const percent = parsed > 0 && parsed < 1 ? parsed * 100 : parsed
  return Math.max(0, Math.min(100, Math.round(percent)))
}

function mediaProgressFromPreview(preview: PreviewData | undefined): MediaProgressInfo | null {
  const directPercent = progressPercent(preview?.progress)
  if (directPercent != null) {
    return {
      percent: directPercent,
      label: `${directPercent}%`,
    }
  }
  if (Array.isArray(preview?.stages)) {
    const stage = preview.stages.find((item) => item.status === "running" || item.progress != null || item.poll_status != null)
    if (stage) {
      const percent = progressPercent(stage.progress)
      if (percent != null) return { percent, label: `${percent}%` }
    }
  }
  return null
}

function MediaProgressText({ progress }: { progress: MediaProgressInfo | null }) {
  if (!progress) return null
  return (
    <span className="whitespace-nowrap text-xs font-semibold tabular-nums text-blue-100">
      {progress.label}
    </span>
  )
}

function NodeStatusMarker({
  status,
  progress,
  renderState,
}: {
  status: string
  progress?: MediaProgressInfo | null
  renderState?: string
}) {
  if (status === "running") {
    return (
      <span className="rounded-full border border-blue-200/25 bg-blue-300/12 px-1.5 py-0.5 text-[10px] font-medium text-blue-100 shadow-[0_6px_14px_rgba(59,130,246,0.14)]">
        {progress?.label || "生成中"}
      </span>
    )
  }
  if (status === "failed") {
    return (
      <span className="rounded-full border border-red-300/25 bg-red-400/12 px-1.5 py-0.5 text-[10px] font-medium text-red-100">
        失败
      </span>
    )
  }
  if (renderState === "stale") {
    return (
      <span className="rounded-full border border-amber-300/25 bg-amber-300/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-100">
        未更新
      </span>
    )
  }
  return null
}

function NodeCanvasLabel({
  label,
  color,
  publicIdText,
  status,
  progress,
  renderState,
}: {
  label: string
  color: string
  publicIdText?: string
  status: string
  progress?: MediaProgressInfo | null
  renderState?: string
}) {
  const idText = publicIdText ? publicIdText.replace(/^#/, "") : ""
  return (
    <div className="pointer-events-none absolute left-0 right-0 top-0 z-30 flex -translate-y-full items-center justify-between gap-2 pb-1.5">
      <div className="flex min-w-0 items-center gap-1.5 text-[11px] font-medium text-zinc-400/90">
        <span
          className="h-2 w-2 shrink-0 rounded-[3px]"
          style={{ background: color, boxShadow: `0 0 14px ${color}55` }}
        />
        <span className="truncate">
          {label}节点{idText ? ` ${idText}` : ""}
        </span>
      </div>
      <NodeStatusMarker status={status} progress={progress} renderState={renderState} />
    </div>
  )
}

function MediaPlaceholderIcon({ type, color }: { type?: string; color: string }) {
  if (type === "video") {
    return (
      <span className="flex h-12 w-12 items-center justify-center rounded-full border border-white/10 bg-white/[0.04]">
        <span className="ml-0.5 h-0 w-0 border-y-[9px] border-l-[14px] border-y-transparent border-l-zinc-500" />
      </span>
    )
  }
  if (type === "audio") {
    return (
      <span className="flex h-11 w-14 items-end justify-center gap-1.5 rounded-md border border-white/10 bg-white/[0.035] px-2 py-2">
        {[0.42, 0.74, 0.56, 0.9].map((height, index) => (
          <span key={index} className="w-1.5 rounded-full bg-zinc-500" style={{ height: `${height * 100}%` }} />
        ))}
      </span>
    )
  }
  return (
    <span className="relative block h-11 w-14 overflow-hidden rounded-md border border-white/10 bg-white/[0.035]">
      <span className="absolute right-2 top-2 h-2 w-2 rounded-full bg-zinc-500/80" />
      <span
        className="absolute bottom-2 left-2 h-0 w-0 border-b-[18px] border-l-[16px] border-r-[16px] border-l-transparent border-r-transparent"
        style={{ borderBottomColor: `${color}66` }}
      />
    </span>
  )
}

function StageDot({ status }: { status?: string }) {
  if (status === "completed") return <span className="text-green-400 text-[10px]">OK</span>
  if (status === "failed") return <span className="text-red-400 text-[10px]">✗</span>
  if (status === "running") return <span className="w-2.5 h-2.5 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
  return <span className="w-2 h-2 rounded-full bg-gray-600" />
}

function StageImage({ stage, compact }: { stage: StageData; compact?: boolean }) {
  // Prefer local URL (stable). Fall back to remote URL via onError if needed.
  const primary = resolveMediaUrl(stage.local_url || stage.url || stage.composite_url)
  const secondary = resolveMediaUrl(stage.remote_url)
  const hasMeta = stage.size || stage.aspect_ratio || stage.quality
  // 生成中且尚未有图 → 渲染 skeleton + spinner 占位,与最终图同区域,避免节点尺寸跳变
  if (!primary) {
    if (stage.status === "running") {
      return (
        <div className="rounded overflow-hidden mt-1 relative bg-gradient-to-br from-blue-950/40 via-gray-900 to-purple-950/40 border border-blue-900/40">
          <div
            className={cn(
              compact ? "w-full h-20" : "w-full h-40",
              "flex items-center justify-center",
            )}
          >
            {/* shimmer 效果 */}
            <div className="absolute inset-0 -translate-x-full animate-[shimmer_2s_infinite] bg-gradient-to-r from-transparent via-blue-400/10 to-transparent" />
            <div className="flex flex-col items-center gap-1.5 z-10">
              <div className="w-6 h-6 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
              <span className="text-[10px] text-blue-300">图片生成中…</span>
            </div>
          </div>
          {hasMeta && (
            <div className="flex flex-wrap items-center gap-1 mt-1 text-[10px] text-gray-500 px-1 pb-1">
              {stage.size && <span className="px-1 py-px rounded bg-black/40">{stage.size}</span>}
              {stage.aspect_ratio && <span className="px-1 py-px rounded bg-black/40">{stage.aspect_ratio}</span>}
              {stage.quality && <span className="px-1 py-px rounded bg-black/40">{stage.quality}</span>}
            </div>
          )}
        </div>
      )
    }
    return null
  }
  return (
    <div className="rounded overflow-hidden mt-1">
      <img
        src={primary}
        alt={stage.name}
        className={compact ? "w-full h-20 object-cover" : "w-full h-40 object-contain bg-black/40"}
        onError={(e) => {
          const el = e.target as HTMLImageElement
          if (secondary && el.src !== secondary) {
            el.src = secondary
          } else {
            el.style.display = "none"
          }
        }}
      />
      {hasMeta && (
        <div className="flex flex-wrap items-center gap-1 mt-1 text-[10px] text-gray-500">
          {stage.size && <span className="px-1 py-px rounded bg-black/40">{stage.size}</span>}
          {stage.aspect_ratio && <span className="px-1 py-px rounded bg-black/40">{stage.aspect_ratio}</span>}
          {stage.quality && <span className="px-1 py-px rounded bg-black/40">{stage.quality}</span>}
          {stage.downgraded && <span className="px-1 py-px rounded bg-yellow-900/40 text-yellow-300" title="分辨率/质量降级后成功">降级</span>}
        </div>
      )}
    </div>
  )
}

function StagesContent({ stages, compact }: { stages: StageData[]; compact?: boolean }) {
  if (!stages.length) return null
  return (
    <div className={cn("space-y-1.5", compact ? "mt-1.5" : "mt-2")}>
      {stages.map((s, i) => {
        const hasImage = !!(s.local_url || s.url || s.remote_url || s.composite_url)
        return (
          <div key={i} className="rounded border border-gray-800 bg-black/20 px-2 py-1.5">
            <div className="flex items-center gap-1.5 mb-1">
              <StageDot status={s.status} />
              <span className="text-[11px] text-gray-300">{s.name}</span>
            </div>
            {s.prompt && (
              <div className="text-[10px] text-gray-400 italic line-clamp-3" title={s.prompt}>
                {s.prompt}
              </div>
            )}
            {hasImage && <StageImage stage={s} compact />}
            {/* status=running 且本阶段有图槽位但还没图 → 由 StageImage 渲染 skeleton */}
            {s.status === "running" && !hasImage && /图|首帧|尾帧|模板|参考/.test(s.name) && (
              <StageImage stage={s} compact />
            )}
            {s.status === "running" && !s.prompt && !hasImage && !/图|首帧|尾帧|模板|参考/.test(s.name) && (
              <div className="text-[10px] text-blue-300">生成中…</div>
            )}
            {s.status === "failed" && (
              <div className="text-[10px] text-red-400 line-clamp-2" title={s.error}>
                失败：{s.error || "生成失败"}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function PreviewContent({ preview, color }: { preview: PreviewData; color: string }) {
  if (!preview?.type) return null

  if (preview.type === "fusion" && Array.isArray(preview.stages)) {
    return <StagesContent stages={preview.stages} compact />
  }

  if (preview.type === "characters" && preview.items) {
    return (
      <div className="mt-1.5 space-y-0.5">
        {preview.items.map((c, i) => (
          <div key={i} className="flex items-center gap-1.5 text-[11px]">
            <span style={{ color }} className="font-medium">{c.name}</span>
            {c.role_type && <span className="text-gray-500">({c.role_type})</span>}
          </div>
        ))}
      </div>
    )
  }

  if (preview.type === "character") {
    return (
      <div className="mt-1.5 text-[11px] space-y-0.5">
        <div className="text-gray-200">{preview.identity}</div>
        {preview.traits && preview.traits.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1">
            {preview.traits.map((t, i) => (
              <span key={i} className="px-1.5 py-0.5 rounded text-[9px]" style={{ background: `${color}22`, color }}>
                {t}
              </span>
            ))}
          </div>
        )}
      </div>
    )
  }

  if (preview.type === "outline" && preview.episodes) {
    return (
      <div className="mt-1.5 space-y-0.5 text-[11px]">
        <div className="text-gray-400">{preview.episode_count} 集</div>
        {preview.episodes.map((ep, i) => (
          <div key={i} className="text-gray-300 truncate">
            <span className="text-gray-500">E{ep.num}</span> {ep.title}
          </div>
        ))}
        {(preview.episode_count ?? 0) > 5 && (
          <div className="text-gray-500">…及其余 {(preview.episode_count ?? 0) - 5} 集</div>
        )}
      </div>
    )
  }

  if (preview.type === "script") {
    return (
      <div className="mt-1.5 text-[11px] space-y-0.5">
        {preview.summary && <div className="text-gray-300 line-clamp-2">{preview.summary}</div>}
        {preview.scene_count != null && <div className="text-gray-500">{preview.scene_count} 场</div>}
      </div>
    )
  }

  if (preview.type === "review") {
    return (
      <div className="mt-1.5 text-[11px] space-y-0.5">
        {preview.score != null && (
          <div className="flex items-center gap-1">
            <span className="text-gray-400">评分</span>
            <span style={{ color }} className="font-bold">{preview.score}</span>
          </div>
        )}
        {preview.summary && <div className="text-gray-300 line-clamp-2">{preview.summary}</div>}
      </div>
    )
  }

  if (preview.type === "image_grid" && Array.isArray(preview.cells) && preview.cells.length > 0) {
    const cols = preview.grid?.cols || 2
    return (
      <div
        className="mt-1.5 grid h-24 overflow-hidden rounded bg-black gap-px"
        style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
      >
        {preview.cells.map((cell, index) => {
          const src = resolveMediaUrl(cell.local_url || cell.url)
          return (
            <div key={cell.cell_id || index} className="relative min-h-0 overflow-hidden bg-black/70">
              {src ? (
                <img src={src} alt={cell.title || ""} className="h-full w-full object-cover" />
              ) : (
                <div className="flex h-full w-full items-center justify-center text-[9px] text-zinc-600">
                  {cell.index ?? index + 1}
                </div>
              )}
            </div>
          )
        })}
      </div>
    )
  }

  if ((preview.type === "image" || preview.type === "image_grid") && (preview.url || preview.local_url || preview.composite_url)) {
    const primary = resolveMediaUrl(preview.local_url || preview.url || preview.composite_url)
    const secondary = resolveMediaUrl(preview.remote_url)
    return (
      <div className="mt-1.5 rounded overflow-hidden">
        <img
          src={primary}
          alt=""
          className="w-full h-24 object-cover"
          onError={(e) => {
            const el = e.target as HTMLImageElement
            if (secondary && el.src !== secondary) el.src = secondary
          }}
        />
      </div>
    )
  }

  if (preview.type === "image_prompt" || preview.type === "video_prompt" || preview.type === "audio_prompt") {
    return (
      <div className="mt-1.5 text-[10px] text-gray-400 line-clamp-3 italic">
        {preview.prompt}
      </div>
    )
  }

  if (preview.type === "storyboard") {
    const url = resolveMediaUrl(preview.local_url || preview.url)
    const secondary = resolveMediaUrl(preview.remote_url)
    const shotCount = preview.shot_count ?? (Array.isArray(preview.shots) ? preview.shots.length : 0)
    const modeLabel = preview.mode === "grid" ? "多宫格" : "镜头清单"
    return (
      <div className="mt-1.5 space-y-1.5">
        <div className="flex items-center gap-2 text-[11px]">
          <span className="px-1.5 py-0.5 rounded text-[10px]" style={{ background: `${color}22`, color }}>
            {modeLabel}
          </span>
          {shotCount > 0 && <span className="text-gray-400">{shotCount} 个镜头</span>}
        </div>
        {url && (
          <div className="rounded overflow-hidden">
            <img
              src={url}
              alt="分镜图"
              className="w-full max-h-32 object-contain bg-black/40"
              onError={(e) => {
                const el = e.target as HTMLImageElement
                if (secondary && el.src !== secondary) el.src = secondary
                else el.style.display = "none"
              }}
            />
          </div>
        )}
        {Array.isArray(preview.shots) && preview.shots.length > 0 && !url && (
          <div className="space-y-0.5">
            {preview.shots.slice(0, 4).map((s, i) => (
              <div key={i} className="flex items-baseline gap-1.5 text-[10px]">
                <span style={{ color }} className="font-bold">镜{s.index ?? i + 1}</span>
                {s.shot_type && <span className="text-gray-500">{s.shot_type}</span>}
                <span className="text-gray-300 truncate flex-1">{s.action}</span>
              </div>
            ))}
            {preview.shots.length > 4 && <div className="text-[10px] text-gray-500">…及其余 {preview.shots.length - 4} 镜</div>}
          </div>
        )}
      </div>
    )
  }

  return null
}

export const SmartNode = memo(function SmartNode(props: NodeProps<NodeData>) {
  const { data, id, selected } = props
  const selectNode = useCanvasStore((s) => s.selectNode)
  const loadCanvasNodes = useCanvasStore((s) => s.loadNodes)
  const updateCanvasNode = useCanvasStore((s) => s.updateNode)
  const resizeCanvasNode = useCanvasStore((s) => s.resizeNode)
  const currentProjectId = useProjectStore((s) => s.currentProject?.id)
  const updateNodeInternals = useUpdateNodeInternals()
  const style = getNodeStyle(data.type)
  const status = data.status ?? "idle"
  const publicIdText = data.publicId !== undefined && data.publicId !== null && String(data.publicId).trim()
    ? `#${String(data.publicId).trim()}`
    : ""
  const isRunning = status === "running"
  const isSuperseded = !!data.superseded
  const isMediaNode = data.type === "image" || data.type === "video" || data.type === "audio"
  const image = imageFromPreview(data.preview)
  const video = videoFromPreview(data.preview)
  const audio = audioFromPreview(data.preview)
  const mediaProgress = mediaProgressFromPreview(data.preview)
  const [naturalImage, setNaturalImage] = useState<{ src: string; width: number; height: number } | null>(null)
  const [naturalVideo, setNaturalVideo] = useState<{ src: string; width: number; height: number } | null>(null)
  const imageForSize = image?.width && image?.height
    ? image
    : naturalImage?.src === image?.primary
    ? naturalImage
    : image
  const videoForSize = video?.width && video?.height
    ? video
    : naturalVideo?.src === video?.src
    ? naturalVideo
    : video
  const gridPreview = data.preview?.type === "image_grid" ? data.preview : undefined
  const gridCells = Array.isArray(gridPreview?.cells) ? gridPreview.cells : []
  const gridCols = gridPreview?.grid?.cols || 2
  const gridRows = gridPreview?.grid?.rows || Math.max(1, Math.ceil(gridCells.length / Math.max(gridCols, 1)))
  const [resizeHover, setResizeHover] = useState(false)
  const [gridMode, setGridMode] = useState<GridToolMode>("idle")
  const [gridBusy, setGridBusy] = useState<string | null>(null)
  const [gridDragStart, setGridDragStart] = useState<{ cellId: string; x: number; y: number } | null>(null)
  const [gridError, setGridError] = useState<string | null>(null)
  const [imageToolbarVisible, setImageToolbarVisible] = useState(false)
  const [panoramaConfirmOpen, setPanoramaConfirmOpen] = useState(false)
  const imageToolbarHideTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const lastAutoSizeRef = useRef<string | null>(null)
  const [cardVideoPlaying, setCardVideoPlaying] = useState(false)
  const previewText = canvasNodeDisplayText({
    type: data.type,
    input: data.input,
    output: data.output,
    prompt: data.prompt,
    preview: data.preview as Record<string, unknown> | undefined,
    previewText: data.previewText,
  })
  const autoNodeSize = data.type === "image"
    ? mediaNodeDimensions(data.preview, "image", imageForSize)
    : data.type === "video"
    ? mediaNodeDimensions(data.preview, "video", videoForSize)
    : { width: CARD_WIDTH, height: CARD_HEIGHT }
  const useManualCanvasSize = data.canvasSizeMode === "manual" || (data.type !== "image" && data.type !== "video")
  const storedWidth = useManualCanvasSize && typeof data.canvasWidth === "number" && Number.isFinite(data.canvasWidth)
    ? data.canvasWidth
    : undefined
  const storedHeight = useManualCanvasSize && typeof data.canvasHeight === "number" && Number.isFinite(data.canvasHeight)
    ? data.canvasHeight
    : undefined
  const nodeWidth = Math.max(
    NODE_MIN_WIDTH,
    Math.min(NODE_MAX_WIDTH, storedWidth ?? autoNodeSize.width),
  )
  const nodeHeight = Math.max(
    NODE_MIN_HEIGHT,
    Math.min(NODE_MAX_HEIGHT, storedHeight ?? autoNodeSize.height),
  )
  useEffect(() => {
    updateNodeInternals(id)
  }, [id, nodeWidth, nodeHeight, updateNodeInternals])
  useEffect(() => {
    const autoSizedMedia = (data.type === "image" || data.type === "video") && !useManualCanvasSize
    if (!autoSizedMedia) {
      lastAutoSizeRef.current = null
      return
    }
    const key = `${Math.round(autoNodeSize.width)}x${Math.round(autoNodeSize.height)}`
    if (lastAutoSizeRef.current === key) return
    lastAutoSizeRef.current = key
    resizeCanvasNode(id, autoNodeSize.width, autoNodeSize.height, { mode: "auto" })
  }, [
    autoNodeSize.height,
    autoNodeSize.width,
    data.type,
    id,
    resizeCanvasNode,
    useManualCanvasSize,
  ])
  const gridToolActive = gridMode !== "idle"
  const gridEditing = gridToolActive && gridCells.length > 0
  const hasPreviewProduct = (
    (data.type === "text" && hasTextProduct(data)) ||
    (data.type === "image" && Boolean(image?.primary || gridCells.length)) ||
    (data.type === "video" && Boolean(video?.src)) ||
    (data.type === "audio" && Boolean(audio?.src))
  )
  const hasImageProduct = data.type === "image" && Boolean(image?.primary || gridCells.length)
  const canGridCrop = hasImageProduct && !isRunning && !isSuperseded
  const panoramaNode = data.type === "image" && isPanoramaImage(data.preview, data.title, data.prompt)
  const renderState = data.type === "image" ? data.renderState : undefined
  const resizeActive = resizeHover && !selected
  const handleClass = cn(
    "openreel-port-handle !h-2.5 !w-2.5 !rounded-full !border-2 !border-[#0f131b] !opacity-45 !shadow-[0_0_0_1px_rgba(255,255,255,0.22)] transition-[opacity,background-color,box-shadow] group-hover:!opacity-95",
    resizeActive && "!opacity-100",
  )
  const portStyle = {
    top: "50%",
    transform: "translateY(-50%)",
  } as const
  const portOffset = -NODE_PORT_GUTTER + NODE_PORT_INSET
  const showNodeToolbar = hasPreviewProduct
  const showImageToolbarActions = data.type === "image" && Boolean(image?.primary)
  const showVideoToolbarActions = data.type === "video" && Boolean(video?.src) && !isRunning && !isSuperseded

  const clearImageToolbarHideTimer = useCallback(() => {
    if (imageToolbarHideTimer.current) {
      clearTimeout(imageToolbarHideTimer.current)
      imageToolbarHideTimer.current = null
    }
  }, [])

  const revealImageToolbar = useCallback(() => {
    if (!showNodeToolbar) return
    clearImageToolbarHideTimer()
    setImageToolbarVisible(true)
  }, [clearImageToolbarHideTimer, showNodeToolbar])

  const scheduleImageToolbarHide = useCallback(() => {
    if (!showNodeToolbar || gridToolActive) return
    clearImageToolbarHideTimer()
    imageToolbarHideTimer.current = setTimeout(() => {
      setImageToolbarVisible(false)
      setPanoramaConfirmOpen(false)
      imageToolbarHideTimer.current = null
    }, 1500)
  }, [clearImageToolbarHideTimer, gridToolActive, showNodeToolbar])

  useEffect(() => clearImageToolbarHideTimer, [clearImageToolbarHideTimer])

  useEffect(() => {
    if (!showNodeToolbar) {
      clearImageToolbarHideTimer()
      setImageToolbarVisible(false)
      setPanoramaConfirmOpen(false)
    }
  }, [clearImageToolbarHideTimer, showNodeToolbar])

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (gridToolActive) return
    if (data.type === "video" && videoRef.current && !videoRef.current.paused) {
      videoRef.current.pause()
      setCardVideoPlaying(false)
    }
    selectNode(id)
  }

  useEffect(() => {
    setCardVideoPlaying(false)
  }, [video?.src])
  const handleResize = useCallback((_event: unknown, params: { width: number; height: number }) => {
    resizeCanvasNode(id, params.width, params.height)
  }, [id, resizeCanvasNode])

  const handleResizeEnd = useCallback((_event: unknown, params: { width: number; height: number }) => {
    resizeCanvasNode(id, params.width, params.height, { persist: true })
  }, [id, resizeCanvasNode])

  const refreshCanvas = useCallback(async () => {
    if (!currentProjectId) return
    const canvas = await getProjectNodes(currentProjectId)
    const rawNodes = (canvas.nodes || []) as Parameters<typeof loadCanvasNodes>[0]
    const rawEdges = (canvas.edges || []) as Parameters<typeof loadCanvasNodes>[1]
    loadCanvasNodes(rawNodes, rawEdges, { preserveOnEmpty: true })
  }, [currentProjectId, loadCanvasNodes])

  const toggleGridTool = useCallback((event: React.MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    setGridError(null)
    if (gridToolActive) {
      setGridMode("idle")
      void refreshCanvas()
      return
    }
    setGridMode("choosing")
  }, [gridToolActive, refreshCanvas])

  const splitGrid = useCallback(async (
    preset: (typeof GRID_PRESETS)[number],
    event: React.MouseEvent,
  ) => {
    event.preventDefault()
    event.stopPropagation()
    if (!currentProjectId || !canGridCrop || gridBusy) return
    setGridBusy(preset.label)
    setGridError(null)
    setGridMode("editing")
    try {
      const result = await callTool<Record<string, unknown>>("image.grid_split", {
        project_id: currentProjectId,
        node_id: id,
        rows: preset.rows,
        cols: preset.cols,
        source_ref: image?.primary || undefined,
      })
      if (result && result.ok === false) {
        throw new Error(String(result.error || "宫格裁剪失败"))
      }
      if (result) {
        updateCanvasNode(id, {
          status: "completed",
          preview: {
            type: "image_grid",
            grid: result.grid,
            cells: result.cells,
            url: result.url || result.composite_url,
            local_url: result.local_url || result.composite_url,
            composite_url: result.composite_url,
            width: typeof result.width === "number" ? result.width : undefined,
            height: typeof result.height === "number" ? result.height : undefined,
          },
        })
      }
    } catch (error) {
      setGridError(error instanceof Error ? error.message : String(error))
      setGridMode("choosing")
    } finally {
      setGridBusy(null)
    }
  }, [canGridCrop, currentProjectId, gridBusy, id, image?.primary, updateCanvasNode])

  const finishGridTool = useCallback((event: React.MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    setGridError(null)
    setGridMode("idle")
    void refreshCanvas()
  }, [refreshCanvas])

  const toggleCardVideoPlayback = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    const player = videoRef.current
    if (!player) return
    if (player.paused || player.ended) {
      void player.play()
        .then(() => setCardVideoPlaying(true))
        .catch((error) => console.warn("Failed to play canvas video preview", error))
      return
    }
    player.pause()
    setCardVideoPlaying(false)
  }, [])

  const requestAddImageToAssetLibrary = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    window.dispatchEvent(new CustomEvent("openreel:add-node-to-asset-library", {
      detail: {
        nodeId: id,
        title: data.title || "",
        publicId: data.publicId ?? null,
      },
    }))
  }, [data.publicId, data.title, id])

  const requestEditImage = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    window.dispatchEvent(new CustomEvent("openreel:edit-image-node", {
      detail: {
        nodeId: id,
        title: data.title || "",
        imageUrl: image?.primary || "",
      },
    }))
  }, [data.title, id, image?.primary])

  const requestPreviewNode = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    window.dispatchEvent(new CustomEvent("openreel:preview-node", {
      detail: {
        nodeId: id,
      },
    }))
  }, [id])

  const requestEditVideo = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    window.dispatchEvent(new CustomEvent("openreel:edit-video-node", {
      detail: {
        nodeId: id,
        title: data.title || "",
        videoUrl: video?.src || "",
      },
    }))
  }, [data.title, id, video?.src])

  const requestExportTailFrame = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    window.dispatchEvent(new CustomEvent("openreel:video-export-tail-frame", {
      detail: { nodeId: id },
    }))
  }, [id])

  const requestSplitAudio = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    window.dispatchEvent(new CustomEvent("openreel:video-split-audio", {
      detail: { nodeId: id },
    }))
  }, [id])

  const requestOpenPanorama = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    if (!image?.primary) return
    window.dispatchEvent(new CustomEvent("openreel:open-panorama-viewer", {
      detail: {
        nodeId: id,
        title: data.title || "全景图",
        imageUrl: image.primary,
      },
    }))
  }, [data.title, id, image?.primary])

  const requestPanoramaConfirm = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    if (panoramaNode) {
      requestOpenPanorama(event)
      return
    }
    setPanoramaConfirmOpen((value) => !value)
  }, [panoramaNode, requestOpenPanorama])

  const requestCreatePanorama = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    setPanoramaConfirmOpen(false)
    window.dispatchEvent(new CustomEvent("openreel:create-panorama-from-node", {
      detail: {
        nodeId: id,
        title: data.title || "图片节点",
        publicId: data.publicId ?? null,
        imageUrl: image?.primary || "",
      },
    }))
  }, [data.publicId, data.title, id, image?.primary])

  const emitCellExtract = useCallback((cell: ImageGridPreviewCell, clientX: number, clientY: number) => {
    if (!cell.cell_id) return
    window.dispatchEvent(new CustomEvent("openreel:grid-cell-extract", {
      detail: {
        gridNodeId: id,
        cellId: cell.cell_id,
        clientX,
        clientY,
      },
    }))
  }, [id])

  return (
    <>
    <motion.div
      initial={{ opacity: 0, scale: 0.85 }}
      animate={{
        opacity: isSuperseded ? 0.5 : 1,
        scale: 1,
        filter: isSuperseded ? "grayscale(1)" : "grayscale(0)",
      }}
      transition={{ type: "spring", stiffness: 320, damping: 28, mass: 0.72 }}
      className={cn(
        "group relative rounded-md text-white transition-[filter] will-change-transform",
      )}
      style={{ width: nodeWidth, height: nodeHeight, pointerEvents: "auto" }}
      onMouseEnter={() => {
        setResizeHover(true)
        revealImageToolbar()
      }}
      onMouseLeave={() => {
        setResizeHover(false)
        scheduleImageToolbarHide()
      }}
    >
      <NodeResizer
        isVisible={resizeActive}
        minWidth={NODE_MIN_WIDTH}
        minHeight={NODE_MIN_HEIGHT}
        maxWidth={NODE_MAX_WIDTH}
        maxHeight={NODE_MAX_HEIGHT}
        keepAspectRatio
        lineClassName="!border-transparent !z-30"
        handleClassName="!h-2.5 !w-2.5 !rounded-sm !border !border-black !bg-cyan-100 !shadow-[0_0_0_3px_rgba(34,211,238,0.16)] !z-40"
        onResize={handleResize}
        onResizeEnd={handleResizeEnd}
      />
      <Handle
        type="target"
        id="in"
        position={Position.Left}
        className={handleClass}
        data-openreel-port="input"
        isConnectableStart={false}
        isConnectableEnd={true}
        onMouseDown={(event) => event.stopPropagation()}
        onTouchStart={(event) => event.stopPropagation()}
        style={{
          ...portStyle,
          left: portOffset,
          pointerEvents: "auto",
          background: resizeActive ? style.color : "rgba(212,212,216,0.72)",
          boxShadow: resizeActive ? `0 0 0 4px ${style.color}30` : undefined,
        }}
      />
      <Handle
        type="source"
        id="out"
        position={Position.Right}
        className={handleClass}
        data-openreel-port="output"
        isConnectableStart={true}
        isConnectableEnd={false}
        style={{
          ...portStyle,
          right: portOffset,
          background: resizeActive ? style.color : "rgba(212,212,216,0.72)",
          boxShadow: resizeActive ? `0 0 0 4px ${style.color}30` : undefined,
        }}
      />

      <NodeCanvasLabel
        label={style.label}
        color={style.color}
        publicIdText={publicIdText}
        status={status}
        progress={mediaProgress}
        renderState={renderState}
      />

      {showNodeToolbar && (
        <div
          className={cn(
            "nodrag absolute left-1/2 top-0 z-50 -mt-2 flex -translate-x-1/2 -translate-y-full items-center gap-1.5 rounded-md border border-white/10 bg-[#0b0f16]/88 px-1.5 py-1 shadow-[0_16px_40px_rgba(0,0,0,0.45)] backdrop-blur-md transition-opacity duration-150",
            imageToolbarVisible || gridToolActive
              ? "pointer-events-auto opacity-100"
              : "pointer-events-none opacity-0",
          )}
          onClick={(event) => event.stopPropagation()}
          onMouseEnter={revealImageToolbar}
          onMouseLeave={scheduleImageToolbarHide}
          onMouseDown={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
        >
          {hasPreviewProduct && (
            <button
              type="button"
              onClick={requestPreviewNode}
              className="h-7 whitespace-nowrap rounded border border-white/10 bg-white/[0.06] px-2.5 text-[11px] font-medium text-zinc-100 transition hover:bg-white/[0.12]"
            >
              预览
            </button>
          )}
          {showVideoToolbarActions && (
            <>
              <button
                type="button"
                onClick={requestEditVideo}
                className="h-7 whitespace-nowrap rounded border border-cyan-200/20 bg-cyan-300/12 px-2.5 text-[11px] font-medium text-cyan-100 transition hover:bg-cyan-300/18"
              >
                剪辑
              </button>
              <button
                type="button"
                onClick={requestExportTailFrame}
                className="h-7 whitespace-nowrap rounded border border-white/10 bg-white/[0.06] px-2.5 text-[11px] font-medium text-zinc-100 transition hover:bg-white/[0.12]"
              >
                尾帧
              </button>
              <button
                type="button"
                onClick={requestSplitAudio}
                className="h-7 whitespace-nowrap rounded border border-white/10 bg-white/[0.06] px-2.5 text-[11px] font-medium text-zinc-100 transition hover:bg-white/[0.12]"
              >
                分音
              </button>
            </>
          )}
          {canGridCrop && (
            <button
              type="button"
              onClick={gridToolActive ? finishGridTool : toggleGridTool}
              disabled={Boolean(gridBusy)}
              className={cn(
                "h-7 whitespace-nowrap rounded px-2.5 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-55",
                gridToolActive
                  ? "bg-emerald-300 text-emerald-950 hover:bg-emerald-200"
                  : "border border-white/10 bg-white/[0.06] text-zinc-100 hover:bg-white/[0.12]",
              )}
            >
              {gridToolActive ? "完成" : "宫格裁剪"}
            </button>
          )}
          {gridMode === "choosing" && (
            <div className="flex items-center gap-1 border-l border-white/10 pl-1.5">
              {GRID_PRESETS.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  onClick={(event) => void splitGrid(preset, event)}
                  disabled={Boolean(gridBusy)}
                  className="h-7 rounded px-2 text-[10px] font-medium text-zinc-100 transition hover:bg-white/[0.12] disabled:cursor-not-allowed disabled:opacity-55"
                >
                  {gridBusy === preset.label ? "..." : preset.label}
                </button>
              ))}
            </div>
          )}
          {!gridToolActive && showImageToolbarActions && (
            <>
              <div className="relative">
                <button
                  type="button"
                  onClick={requestPanoramaConfirm}
                  className={cn(
                    "h-7 whitespace-nowrap rounded px-2.5 text-[11px] font-medium transition",
                    panoramaNode
                      ? "border border-sky-200/25 bg-sky-300/14 text-sky-100 hover:bg-sky-300/22"
                      : "border border-white/10 bg-white/[0.06] text-zinc-100 hover:bg-white/[0.12]",
                  )}
                >
                  {panoramaNode ? "进入全景" : "全景"}
                </button>
                {panoramaConfirmOpen && !panoramaNode && (
                  <div
                    className="absolute bottom-full left-1/2 z-[70] mb-2 w-56 -translate-x-1/2 rounded-md border border-white/12 bg-[#080c13]/95 p-2.5 text-left shadow-[0_18px_48px_rgba(0,0,0,0.5)] backdrop-blur-md"
                    onClick={(event) => event.stopPropagation()}
                    onMouseDown={(event) => event.stopPropagation()}
                  >
                    <div className="text-[11px] font-medium leading-4 text-zinc-100">
                      参考此图片生成一张 360 全景图？
                    </div>
                    <div className="mt-1 text-[10px] leading-4 text-zinc-400">
                      会自动创建新的图片节点并开始生成。
                    </div>
                    <div className="mt-2 flex justify-end gap-1.5">
                      <button
                        type="button"
                        onClick={(event) => {
                          event.preventDefault()
                          event.stopPropagation()
                          setPanoramaConfirmOpen(false)
                        }}
                        className="h-6 rounded px-2 text-[10px] text-zinc-300 transition hover:bg-white/10"
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        onClick={requestCreatePanorama}
                        className="h-6 rounded bg-sky-300 px-2 text-[10px] font-semibold text-sky-950 transition hover:bg-sky-200"
                      >
                        生成
                      </button>
                    </div>
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={requestEditImage}
                className="h-7 whitespace-nowrap rounded border border-cyan-200/20 bg-cyan-300/12 px-2.5 text-[11px] font-medium text-cyan-100 transition hover:bg-cyan-300/18"
              >
                编辑
              </button>
              <button
                type="button"
                onClick={requestAddImageToAssetLibrary}
                className="h-7 whitespace-nowrap rounded border border-white/10 bg-white/[0.06] px-2.5 text-[11px] font-medium text-zinc-100 transition hover:bg-white/[0.12]"
              >
                加入资产库
              </button>
            </>
          )}
          {gridError && (
            <div className="max-w-[190px] truncate rounded border border-red-400/25 bg-red-950/80 px-2 py-1 text-[10px] leading-4 text-red-100">
              {gridError}
            </div>
          )}
        </div>
      )}

      <div
        className={cn(
          "relative h-full w-full overflow-hidden rounded-xl border bg-[#232323] shadow-[0_18px_34px_rgba(0,0,0,0.28)] transition-[transform,box-shadow,border-color] duration-200 hover:border-white/25 hover:shadow-[0_24px_46px_rgba(0,0,0,0.34)]",
          "openreel-smart-node-card openreel-smart-node-drag",
          "border-white/[0.08]",
          resizeActive && !selected && "border-cyan-200/80 shadow-[0_0_0_1px_rgba(34,211,238,0.36),0_24px_46px_rgba(0,0,0,0.34)]",
          isRunning && !isSuperseded && style.runningGlow,
          gridToolActive && "nodrag",
        )}
        data-node-status={status}
        onClick={handleClick}
      >
        {isMediaNode ? (
        <div className={cn("relative h-full w-full bg-transparent", gridToolActive && "nodrag")}>
          {gridEditing ? (
            <div
              className="nodrag grid h-full w-full gap-0 bg-transparent"
              style={{
                gridTemplateColumns: `repeat(${gridCols}, minmax(0, 1fr))`,
                gridTemplateRows: `repeat(${gridRows}, minmax(0, 1fr))`,
              }}
            >
              {gridCells.map((cell, index) => {
                const src = resolveMediaUrl(cell.local_url || cell.url)
                const hasImage = Boolean(src) && !cell.empty
                const cellId = cell.cell_id || `cell-${index + 1}`
                const label = cell.title || `第${cell.index ?? index + 1}图片`
                return (
                  <div
                    key={cellId}
                    data-openreel-grid-cell="true"
                    data-grid-node-id={id}
                    data-grid-cell-id={cell.cell_id || ""}
                    role="button"
                    tabIndex={0}
                    draggable={hasImage && !gridBusy}
                    title={label}
                    onClick={(event) => event.stopPropagation()}
                    onMouseDown={(event) => event.stopPropagation()}
                    onPointerDown={(event) => event.stopPropagation()}
                    onDragOver={(event) => {
                      if (!gridBusy && cell.cell_id) event.preventDefault()
                    }}
                    onDragStart={(event) => {
                      event.stopPropagation()
                      if (gridBusy || !cell.cell_id || !hasImage) {
                        event.preventDefault()
                        return
                      }
                      setGridDragStart({ cellId: cell.cell_id, x: event.clientX, y: event.clientY })
                      event.dataTransfer.effectAllowed = "copy"
                      event.dataTransfer.setData("text/plain", `${id}#${cell.cell_id}`)
                      event.dataTransfer.setData(
                        "application/x-openreel-grid-cell",
                        JSON.stringify({ gridNodeId: id, cellId: cell.cell_id, title: label }),
                      )
                    }}
                    onDragEnd={(event) => {
                      event.stopPropagation()
                      const start = gridDragStart
                      setGridDragStart(null)
                      if (!start || !cell.cell_id || start.cellId !== cell.cell_id || gridBusy) return
                      const distance = Math.hypot(event.clientX - start.x, event.clientY - start.y)
                      if (distance > 60) emitCellExtract(cell, event.clientX, event.clientY)
                    }}
                    onDoubleClick={(event) => {
                      event.stopPropagation()
                      emitCellExtract(cell, event.clientX, event.clientY)
                    }}
                    className={cn(
                      "nodrag group/cell relative min-h-0 overflow-hidden bg-transparent outline-none",
                      hasImage ? "cursor-grab active:cursor-grabbing" : "cursor-copy bg-zinc-950/60",
                    )}
                  >
                    {hasImage ? (
                      <img src={src} alt={label} className="nodrag h-full w-full object-cover transition duration-150 group-hover/cell:scale-[1.02]" />
                    ) : (
                      <div className="flex h-full w-full items-center justify-center border border-dashed border-white/12 bg-white/[0.025] text-[10px] text-zinc-500">
                        放入图片
                      </div>
                    )}
                    <div className="absolute inset-0 border border-white/0 transition group-hover/cell:border-cyan-200/80 group-focus/cell:border-cyan-200/80" />
                    <div className="absolute bottom-1 left-1 rounded bg-black/65 px-1.5 py-0.5 text-[10px] font-medium text-white opacity-0 transition group-hover/cell:opacity-100 group-focus/cell:opacity-100">
                      {cell.index ?? index + 1}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : data.type === "audio" && audio?.src ? (
            <div className="flex h-full w-full items-center justify-center bg-[radial-gradient(circle_at_18%_12%,rgba(245,158,11,0.24),transparent_32%),linear-gradient(135deg,#111827,#18181b)] px-5">
              <div className="pointer-events-none flex h-16 w-full max-w-[220px] items-end justify-center gap-2 rounded-md border border-white/10 bg-black/24 px-4 py-3 shadow-inner shadow-black/20">
                {[0.35, 0.72, 0.48, 0.86, 0.55, 0.68, 0.4, 0.78, 0.5, 0.62].map((height, index) => (
                  <span
                    key={index}
                    className="w-2 rounded-full bg-amber-200/80"
                    style={{ height: `${Math.round(height * 100)}%` }}
                  />
                ))}
              </div>
              {isRunning && (
                <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-black/38 backdrop-blur-[1px]">
                  <div className="flex min-w-[150px] items-center gap-2 rounded-md border border-blue-200/20 bg-black/70 px-3 py-2 text-xs font-medium text-blue-100 shadow-xl shadow-black/30">
                    <span className="h-3.5 w-3.5 rounded-full border-2 border-blue-200 border-t-transparent animate-spin" />
                    <MediaProgressText progress={mediaProgress} />
                  </div>
                </div>
              )}
            </div>
          ) : data.type === "video" && video?.src ? (
            <>
              <video
                ref={videoRef}
                poster={video.poster}
                className="pointer-events-none block h-full w-full select-none object-cover"
                controls={false}
                disablePictureInPicture
                controlsList="nodownload nofullscreen noremoteplayback"
                playsInline
                preload="metadata"
                draggable={false}
                onLoadedMetadata={(event) => {
                  const el = event.currentTarget
                  if ((!video.width || !video.height) && el.videoWidth > 0 && el.videoHeight > 0) {
                    setNaturalVideo({ src: video.src, width: el.videoWidth, height: el.videoHeight })
                  }
                }}
                onPlay={() => setCardVideoPlaying(true)}
                onPause={() => setCardVideoPlaying(false)}
                onEnded={() => setCardVideoPlaying(false)}
              >
                <source src={video.src} type={videoMimeType(video.src)} />
              </video>
              <button
                type="button"
                aria-label={cardVideoPlaying ? "暂停视频预览" : "播放视频预览"}
                onClick={toggleCardVideoPlayback}
                onMouseDown={(event) => event.stopPropagation()}
                onPointerDown={(event) => event.stopPropagation()}
                className={cn(
                  "nodrag absolute left-1/2 top-1/2 z-30 flex h-12 w-12 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-white/18 bg-black/70 text-white shadow-2xl shadow-black/35 backdrop-blur transition hover:scale-105 hover:bg-black/85",
                  cardVideoPlaying && "md:opacity-0 md:group-hover:opacity-100",
                )}
              >
                {cardVideoPlaying ? (
                  <span className="flex items-center gap-1">
                    <span className="h-4 w-1.5 rounded-sm bg-white" />
                    <span className="h-4 w-1.5 rounded-sm bg-white" />
                  </span>
                ) : (
                  <span className="ml-0.5 h-0 w-0 border-y-[9px] border-l-[14px] border-y-transparent border-l-white" />
                )}
              </button>
              {isRunning && (
                <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-black/38 backdrop-blur-[1px]">
                  <div className="flex min-w-[150px] items-center gap-2 rounded-md border border-blue-200/20 bg-black/70 px-3 py-2 text-xs font-medium text-blue-100 shadow-xl shadow-black/30">
                    <span className="h-3.5 w-3.5 rounded-full border-2 border-blue-200 border-t-transparent animate-spin" />
                    <MediaProgressText progress={mediaProgress} />
                  </div>
                </div>
              )}
            </>
          ) : image?.primary ? (
            <>
              <img
                src={image.primary}
                alt={data.title || ""}
                draggable={false}
                className="block h-full w-full select-none object-cover"
                onLoad={(e) => {
                  const el = e.currentTarget
                  if ((!image.width || !image.height) && el.naturalWidth > 0 && el.naturalHeight > 0) {
                    setNaturalImage({ src: image.primary, width: el.naturalWidth, height: el.naturalHeight })
                  }
                }}
                onError={(e) => {
                  const el = e.target as HTMLImageElement
                  if (image.secondary && el.src !== image.secondary) el.src = image.secondary
                }}
              />
              {isRunning && (
                <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-black/38 backdrop-blur-[1px]">
                  <div className="flex min-w-[150px] items-center gap-2 rounded-md border border-blue-200/20 bg-black/70 px-3 py-2 text-xs font-medium text-blue-100 shadow-xl shadow-black/30">
                    <span className="h-3.5 w-3.5 rounded-full border-2 border-blue-200 border-t-transparent animate-spin" />
                    <MediaProgressText progress={mediaProgress} />
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="flex h-full w-full items-center justify-center bg-[linear-gradient(135deg,#111827,#18181b)]">
              <div className="flex flex-col items-center gap-2 text-zinc-400">
                {isRunning ? (
                  <span className="h-5 w-5 rounded-full border-2 border-zinc-500 border-t-zinc-100 animate-spin" />
                ) : (
                  <MediaPlaceholderIcon type={data.type} color={style.color} />
                )}
	                {isRunning ? (
                  <MediaProgressText progress={mediaProgress} />
                ) : (
                  <span className="text-xs">{data.type === "video" ? "待生成视频" : data.type === "audio" ? "待生成音频" : "待生成图片"}</span>
                )}
              </div>
            </div>
          )}

          {gridBusy && (
            <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-black/20 backdrop-blur-[1px]">
              <span className="rounded-md bg-black/70 px-2.5 py-1.5 text-[11px] text-white">
                裁剪中...
              </span>
            </div>
          )}

          <div className="pointer-events-none absolute inset-x-0 bottom-0 z-40 bg-gradient-to-t from-black/88 via-black/54 to-transparent px-2.5 pb-2.5 pt-10">
            <div className="line-clamp-1 min-w-0 text-[12px] font-semibold leading-4 text-white drop-shadow" title={data.title}>
              {data.title || "未命名"}
            </div>
            {audio?.duration && data.type === "audio" && (
              <div className="mt-1 text-[10px] text-zinc-300/80">
                {audio.duration}
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="flex h-full flex-col p-3">
          <div className="mb-2 min-w-0 truncate text-[13px] font-semibold text-zinc-100" title={data.title}>
            {data.title || "未命名"}
          </div>
          <div className="min-h-0 flex-1 overflow-hidden rounded border border-white/[0.06] bg-white/[0.035] px-3 py-2.5">
            {isRunning ? (
              <div className="flex h-full items-center justify-center gap-2 text-xs text-zinc-400">
                <span className="h-4 w-4 rounded-full border-2 border-zinc-600 border-t-zinc-200 animate-spin" />
                生成中...
              </div>
            ) : (
              <div className="line-clamp-5 whitespace-pre-wrap text-[12px] leading-5 text-zinc-300">
                {previewText || "暂无文本预览"}
              </div>
            )}
          </div>
        </div>
      )}
      </div>
    </motion.div>
    </>
  )
})

export default SmartNode
