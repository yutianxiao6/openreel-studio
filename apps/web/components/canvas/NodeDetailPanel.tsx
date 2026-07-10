"use client"

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type FocusEvent, type MouseEvent } from "react"
import { motion } from "framer-motion"
import { createPortal } from "react-dom"
import {
  callTool,
  getVideoProviderProtocols,
  getRuntimeConfigFile,
  getProjectNodeDetails,
  getProjectNodes,
  listProjectAssets,
  resolveMediaUrl,
  switchProjectNodeHistory,
  updateProjectNodeDetails,
  uploadFile,
  uploadProjectNodeMedia,
} from "@/lib/api"
import { videoReferenceImageLimit } from "@/lib/videoProtocolLimits"
import {
  inputFieldsFromNodeInput,
  nodePromptText,
  nodeReadableText,
} from "@/lib/nodeDisplay"
import { MarkdownView } from "@/components/common/MarkdownView"
import { useCanvasStore } from "@/stores/canvasStore"
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
  clarity: string
  duration_seconds: string
  video_mode: string
  instrumental: boolean
  custom_mode: boolean
  reference_images: string[]
  reference_videos: string[]
  reference_audios: string[]
}

interface MediaProviderOption {
  kind: string
  name: string
  model_name: string
  api_format: string
  params?: Record<string, unknown>
  is_active?: boolean
  enabled?: boolean
}

type AudioProviderOption = MediaProviderOption

interface LlmProviderOption {
  name: string
  provider: string
  model_name: string
  tier?: string
  enabled?: boolean
  supports_vision?: boolean | null
  params?: Record<string, unknown>
}

interface RuntimeModelDefaults {
  model_tier_defaults?: Record<string, string | null | undefined>
  model_assignments?: Record<string, string | null | undefined>
}

type AudioProviderMode = "tts" | "music" | "unknown"

interface VideoDurationSummary {
  min?: number | string | null
  max?: number | string | null
  allowed_values?: Array<number | string> | null
  step?: number | string | null
}

// Product fallback only. A declared provider, model profile, or protocol duration
// always takes precedence over this editable 5–15 second range.
const DEFAULT_VIDEO_DURATION_RULE: VideoDurationSummary = { min: 5, max: 15, step: 1 }

interface VideoProtocolModeSummary {
  label?: string
  prompt_required?: boolean | null
  min_images?: number | null
  max_images?: number | null
  min_videos?: number | null
  max_videos?: number | null
  min_audios?: number | null
  max_audios?: number | null
  min_total_media?: number | null
  max_total_media?: number | null
  required_roles?: string[]
  allowed_roles?: string[]
  supported_ratios?: string[]
  supported_resolutions?: string[]
  default_ratio?: string
  default_resolution?: string
  duration?: VideoDurationSummary
}

interface VideoProtocolProfileSummary {
  match?: string
  label?: string
  supported_ratios?: string[]
  supported_resolutions?: string[]
  default_ratio?: string
  default_resolution?: string
  duration?: VideoDurationSummary
  modes?: Record<string, VideoProtocolModeSummary> | string[]
  supported_modes?: string[]
}

interface VideoProtocolSummary {
  id: string
  display_name?: string
  model_names?: string[]
  model_profiles?: VideoProtocolProfileSummary[]
  modes?: Record<string, VideoProtocolModeSummary>
  supported_ratios?: string[]
  supported_resolutions?: string[]
  default_ratio?: string
  default_resolution?: string
  duration?: VideoDurationSummary
}

const EDITABLE_NODE_TYPES = new Set(["text", "image", "video", "audio"])

type ImageResolutionPreset = {
  label: string
  value: string
  tier: ImageResolutionTier
}

type ImageResolutionTier = "1k" | "2k" | "4k"

const IMAGE_RESOLUTION_TIER_OPTIONS: Array<{ label: string; value: ImageResolutionTier }> = [
  { label: "1K", value: "1k" },
  { label: "2K", value: "2k" },
  { label: "4K", value: "4k" },
]

const IMAGE_RESOLUTION_TIER_SHORT_EDGE: Record<ImageResolutionTier, number> = {
  "1k": 1080,
  "2k": 1440,
  "4k": 2160,
}

const IMAGE_ASPECT_RATIO_GRID_OPTIONS = [
  { label: "自适应", value: "auto" },
  { label: "1:1", value: "1:1" },
  { label: "1:2", value: "1:2" },
  { label: "2:1", value: "2:1" },
  { label: "9:16", value: "9:16" },
  { label: "16:9", value: "16:9" },
  { label: "3:4", value: "3:4" },
  { label: "4:3", value: "4:3" },
  { label: "3:2", value: "3:2" },
  { label: "2:3", value: "2:3" },
  { label: "5:4", value: "5:4" },
  { label: "4:5", value: "4:5" },
  { label: "21:9", value: "21:9" },
  { label: "9:21", value: "9:21" },
]

const MAX_IMAGE_PIXEL_AREA = 3840 * 2160

function parseAspectRatio(value: string): { width: number; height: number; value: string } | null {
  const match = value.trim().match(/^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$/)
  if (!match) return null
  const width = Number.parseFloat(match[1])
  const height = Number.parseFloat(match[2])
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null
  return { width, height, value: `${match[1]}:${match[2]}` }
}

function normalizeImageAspectRatio(value: string): string {
  if (value.trim().toLowerCase() === "auto") return "auto"
  return parseAspectRatio(value)?.value || "9:16"
}

function roundToMultipleOfEight(value: number): number {
  return Math.max(8, Math.round(value / 8) * 8)
}

function imageResolutionForAspectTier(aspectRatio: string, tier: ImageResolutionTier): string {
  const aspect = parseAspectRatio(aspectRatio) || { width: 9, height: 16, value: "9:16" }
  if (aspect.value === "1:1") {
    const size = tier === "1k" ? 1080 : tier === "2k" ? 2048 : 2880
    return `${size}x${size}`
  }
  const shortEdge = IMAGE_RESOLUTION_TIER_SHORT_EDGE[tier]
  let width = aspect.width >= aspect.height ? shortEdge * (aspect.width / aspect.height) : shortEdge
  let height = aspect.width >= aspect.height ? shortEdge : shortEdge * (aspect.height / aspect.width)
  if (width * height > MAX_IMAGE_PIXEL_AREA) {
    const scale = Math.sqrt(MAX_IMAGE_PIXEL_AREA / (width * height))
    width *= scale
    height *= scale
  }
  return `${roundToMultipleOfEight(width)}x${roundToMultipleOfEight(height)}`
}

function imageResolutionPresetsForAspect(aspectRatio: string): ImageResolutionPreset[] {
  const aspect = normalizeImageAspectRatio(aspectRatio)
  return IMAGE_RESOLUTION_TIER_OPTIONS.map((item) => ({
    label: `${item.label} · ${imageResolutionForAspectTier(aspect, item.value)}`,
    value: imageResolutionForAspectTier(aspect, item.value),
    tier: item.value,
  }))
}

function imageResolutionTier(value: string): ImageResolutionTier {
  const parsed = parseImageResolution(value)
  if (!parsed) return "1k"
  const width = Number.parseInt(parsed.width, 10)
  const height = Number.parseInt(parsed.height, 10)
  if (!Number.isFinite(width) || !Number.isFinite(height)) return "1k"
  const shortEdge = Math.min(width, height)
  const area = width * height
  if (area >= MAX_IMAGE_PIXEL_AREA * 0.75 || shortEdge >= 1900) return "4k"
  if (shortEdge >= 1300 || area >= 2_800_000) return "2k"
  return "1k"
}

function defaultImageResolutionForAspect(aspectRatio: string, preferredTier: ImageResolutionTier = "1k"): string {
  return imageResolutionForAspectTier(normalizeImageAspectRatio(aspectRatio), preferredTier)
}

function parseImageResolution(value: string): { width: string; height: string } | null {
  const match = value.trim().match(/^(\d+)\s*[xX×]\s*(\d+)$/)
  if (!match) return null
  return { width: match[1], height: match[2] }
}

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
  clarity: "",
  duration_seconds: "",
  video_mode: "",
  instrumental: true,
  custom_mode: false,
  reference_images: [],
  reference_videos: [],
  reference_audios: [],
}

const IMAGE_QUALITY_OPTIONS = [
  { label: "低画质", value: "low" },
  { label: "标准画质", value: "medium" },
  { label: "高画质", value: "high" },
]

function imageQualityLabel(value: string): string {
  const normalized = value.trim().toLowerCase()
  return IMAGE_QUALITY_OPTIONS.find((item) => item.value === normalized)?.label || value || "标准画质"
}

function mediaDurationLabel(value: string): string {
  const parsed = Number.parseFloat(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return ""
  return `${Number.isInteger(parsed) ? parsed : parsed.toFixed(1).replace(/\.0$/, "")}s`
}
const VIDEO_MODE_LABELS: Record<string, string> = {
  text_to_video: "文生视频",
  first_frame: "图生视频",
  first_last_frame: "首尾帧",
  multimodal_reference: "多参考",
}
const VIDEO_MODE_ORDER = ["text_to_video", "first_frame", "first_last_frame", "multimodal_reference"]

interface Props {
  nodeId: string
  projectId?: string | null
  onClose: () => void
  onRerun?: (nodeId: string) => void | Promise<void>
  onDelete?: (nodeId: string) => void | Promise<void>
  onRequestStoryRevision?: (nodeId: string) => void | Promise<void>
  actionDisabled?: boolean
  presentation?: "drawer" | "modal" | "anchored"
  anchorStyle?: CSSProperties
  editRequestKey?: string | number | null
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
  current?: boolean
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

interface ReferenceMentionCandidate {
  mention: string
  label: string
  ref: string
  source: "node" | "reference"
  previewUrl?: string
}

interface ReferenceImageMention {
  mention: string
  label: string
  ref: string
  source: "node" | "reference"
  index?: number
}

type CanvasGraphNode = {
  id: string
  data?: Record<string, unknown>
}

type CanvasGraphEdge = {
  source?: string | null
  target?: string | null
}

interface ProjectNodeIndexItem {
  id?: string
  display_id?: string | number | null
  title?: string | null
}

function asObj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function pickUrl(o: Record<string, unknown> | null): string | null {
  if (!o) return null
  const u = o.local_url || o.url || o.remote_url || o.composite_url
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

function mediaProgressFromOutput(output: unknown): MediaProgressInfo | null {
  const obj = asObj(output)
  const percent = progressPercent(obj?.progress)
  if (percent == null) return null
  return {
    percent,
    label: `${percent}%`,
  }
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
  const normalized = value.startsWith("upload:") ? value.slice(7).trim() : value
  if (/^(https?:|\/api\/|data:)/.test(normalized)) return resolveMediaUrl(normalized)
  if (normalized.startsWith("uploads/")) return resolveMediaUrl(`/api/uploads/${projectId}/file/${normalized}`)
  if (normalized.startsWith("generated_images/")) {
    return resolveMediaUrl(`/api/media/${projectId}/${normalized.replace(/^generated_images\//, "")}`)
  }
  if (normalized.startsWith("generated_audio/")) {
    return resolveMediaUrl(`/api/media/${projectId}/${normalized}`)
  }
  return ""
}

function stripNodeReferenceMarker(value: string): string {
  let text = value.trim()
  let changed = true
  while (changed) {
    changed = false
    for (const prefix of ["@", "node:", "#"]) {
      if (text.startsWith(prefix)) {
        text = text.slice(prefix.length).trim()
        changed = true
      }
    }
  }
  return text
}

function isUuidLike(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
}

function addNodeLookupKey(lookup: Map<string, string>, key: unknown, nodeId: string) {
  const text = String(key ?? "").trim()
  if (!text) return
  lookup.set(text, nodeId)
  lookup.set(stripNodeReferenceMarker(text), nodeId)
}

async function resolveProjectNodeReference(projectId: string, value: string): Promise<string> {
  const raw = stripNodeReferenceMarker(value)
  if (!raw) return ""
  if (isUuidLike(raw)) return raw
  const canvas = await getProjectNodes(projectId)
  const lookup = new Map<string, string>()
  for (const item of (canvas.nodes || []) as ProjectNodeIndexItem[]) {
    const nodeId = String(item.id || "").trim()
    if (!nodeId) continue
    addNodeLookupKey(lookup, nodeId, nodeId)
    if (item.display_id !== undefined && item.display_id !== null) {
      addNodeLookupKey(lookup, item.display_id, nodeId)
      addNodeLookupKey(lookup, `#${item.display_id}`, nodeId)
      addNodeLookupKey(lookup, `node:${item.display_id}`, nodeId)
      addNodeLookupKey(lookup, `node:#${item.display_id}`, nodeId)
    }
  }
  return lookup.get(raw) || lookup.get(value.trim()) || ""
}

function normalizeReferenceValue(text: string, label: string, refId?: string): ReferenceItem | null {
  const value = text.trim()
  if (!value) return null
  if (/^(https?:|data:)/.test(value)) return { kind: "url", value, label }
  if (value.startsWith("/api/")) return { kind: "url", value, label }
  if (value.startsWith("/") && refId) return { kind: "reference", value: refId, label }
  if (value.startsWith("/")) return { kind: "url", value, label }
  if (value.startsWith("node:")) return { kind: "node", value: value.slice(5), label: "节点引用" }
  if (/^#?\d+$/.test(value)) return { kind: "node", value: stripNodeReferenceMarker(value), label: "节点引用" }
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    return { kind: "node", value, label: "节点引用" }
  }
  if (value.startsWith("asset:")) return { kind: "asset", value: value.slice(6), label: "资产引用" }
  if (value.startsWith("upload:")) return { kind: "file", value: value.slice(7).trim(), label }
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

function canvasNodeData(node: CanvasGraphNode | null | undefined): Record<string, unknown> {
  return asObj(node?.data) || {}
}

function canvasNodeType(node: CanvasGraphNode | null | undefined): string {
  return String(canvasNodeData(node).type || "").trim()
}

function canvasNodeTitle(node: CanvasGraphNode | null | undefined): string {
  const data = canvasNodeData(node)
  return String(data.title || data.label || node?.id || "").trim()
}

function canvasNodePublicId(node: CanvasGraphNode | null | undefined): string {
  const value = canvasNodeData(node).publicId
  if (value === undefined || value === null) return ""
  return String(value).trim()
}

function canvasNodeReferenceValue(node: CanvasGraphNode): string {
  const publicId = canvasNodePublicId(node)
  return publicId ? `node:${publicId}` : `node:${node.id}`
}

function canvasNodeInput(node: CanvasGraphNode | null | undefined): Record<string, unknown> {
  return asObj(parseJson(canvasNodeData(node).input)) || {}
}

function canvasNodeWorkflow(node: CanvasGraphNode | null | undefined): Record<string, unknown> {
  const data = canvasNodeData(node)
  const input = canvasNodeInput(node)
  return asObj(parseJson(data.workflow)) || asObj(parseJson(input.workflow)) || {}
}

function canvasNodeAliasValues(node: CanvasGraphNode): unknown[] {
  const data = canvasNodeData(node)
  const input = canvasNodeInput(node)
  const workflow = canvasNodeWorkflow(node)
  const stepId = String(workflow.step_id || input.stage || "").trim()
  return [
    node.id,
    `node:${node.id}`,
    data.nodeId,
    data.publicId,
    input.stage,
    input.source_node_id,
    input.sourceNodeId,
    workflow.step_id,
    workflow.template_step_id,
    workflow.source_node_id,
    stepId.endsWith("_canvas") ? stepId.slice(0, -7) : "",
  ]
}

function canvasImagePreviewUrl(node: CanvasGraphNode | null | undefined, projectId?: string | null): string {
  if (!node || canvasNodeType(node) !== "image") return ""
  const data = canvasNodeData(node)
  return (
    firstReferenceImageUrl(data.preview, projectId)
    || firstReferenceImageUrl(data.output, projectId)
    || firstReferenceImageUrl(data.workflowRuntimeOutput, projectId)
    || firstReferenceImageUrl(data.input, projectId)
    || firstReferenceImageUrl(data, projectId)
  )
}

function addCanvasNodeLookup(lookup: Map<string, CanvasGraphNode>, key: unknown, node: CanvasGraphNode) {
  const text = String(key ?? "").trim()
  if (!text) return
  if (!lookup.has(text)) lookup.set(text, node)
  const stripped = stripNodeReferenceMarker(text)
  if (stripped && !lookup.has(stripped)) lookup.set(stripped, node)
}

function buildCanvasNodeLookup(nodes: CanvasGraphNode[]): Map<string, CanvasGraphNode> {
  const lookup = new Map<string, CanvasGraphNode>()
  for (const node of nodes) {
    for (const alias of canvasNodeAliasValues(node)) {
      addCanvasNodeLookup(lookup, alias, node)
    }
    const publicId = canvasNodePublicId(node)
    if (publicId) {
      addCanvasNodeLookup(lookup, publicId, node)
      addCanvasNodeLookup(lookup, `#${publicId}`, node)
      addCanvasNodeLookup(lookup, `node:${publicId}`, node)
      addCanvasNodeLookup(lookup, `node:#${publicId}`, node)
    }
  }
  return lookup
}

function resolveCanvasReferenceNode(value: unknown, lookup: Map<string, CanvasGraphNode>): CanvasGraphNode | undefined {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    const text = String(value).trim()
    if (!text) return undefined
    return lookup.get(text) || lookup.get(`node:${text}`) || lookup.get(stripNodeReferenceMarker(text))
  }
  const obj = asObj(value)
  if (!obj) return undefined
  for (const key of [
    "ref",
    "reference",
    "reference_input",
    "value",
    "node_id",
    "nodeId",
    "source_node_id",
    "sourceNodeId",
    "source_step",
    "from_step",
    "source",
    "candidate",
    "id",
  ]) {
    const node = resolveCanvasReferenceNode(obj[key], lookup)
    if (node) return node
  }
  return undefined
}

function safeReferenceMentionLabel(value: string, fallback: string): string {
  const raw = (value || fallback || "参考图")
    .replace(/^[@#]+/, "")
    .replace(/\.(png|jpe?g|webp|gif|bmp|svg)$/i, "")
    .replace(/[^\p{L}\p{N}_\-\u4e00-\u9fa5]+/gu, "")
    .trim()
  const base = raw || fallback || "参考图"
  const label = /(图|图片|照片|参考)$/u.test(base) ? base : `${base}图片`
  return label.slice(0, 18)
}

function basenameFromReferenceValue(value: string): string {
  const raw = value.replace(/^upload:/, "").trim()
  const withoutQuery = raw.split(/[?#]/)[0] || raw
  const parts = withoutQuery.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || ""
}

function uniqueReferenceMention(base: string, used: Set<string>): string {
  const first = `@${base}`
  if (!used.has(first)) {
    used.add(first)
    return first
  }
  for (let index = 2; index < 100; index += 1) {
    const candidate = `@${base}${index}`
    if (!used.has(candidate)) {
      used.add(candidate)
      return candidate
    }
  }
  const fallback = `@${base}${used.size + 1}`
  used.add(fallback)
  return fallback
}

function referenceMentionCandidateKey(candidate: Pick<ReferenceMentionCandidate, "ref">): string {
  return stripNodeReferenceMarker(candidate.ref).toLowerCase()
}

function buildReferenceMentionCandidates(
  node: NodeFull,
  draft: EditableNodeDraft,
  canvasNodes: CanvasGraphNode[],
  canvasEdges: CanvasGraphEdge[],
  projectId?: string | null,
): ReferenceMentionCandidate[] {
  const nodeLookup = buildCanvasNodeLookup(canvasNodes)
  const candidates: ReferenceMentionCandidate[] = []
  const seenRefs = new Set<string>()
  const usedMentions = new Set<string>()

  const addCandidate = (
    labelSource: string,
    ref: string,
    source: ReferenceMentionCandidate["source"],
    previewUrl?: string,
  ) => {
    const normalizedRef = ref.trim()
    if (!normalizedRef) return
    const key = stripNodeReferenceMarker(normalizedRef).toLowerCase()
    if (seenRefs.has(key)) return
    seenRefs.add(key)
    const label = safeReferenceMentionLabel(labelSource, `参考图${candidates.length + 1}`)
    candidates.push({
      mention: uniqueReferenceMention(label, usedMentions),
      label,
      ref: normalizedRef,
      source,
      previewUrl,
    })
  }

  const addNodeCandidate = (sourceNode: CanvasGraphNode, ref: string = canvasNodeReferenceValue(sourceNode)) => {
    if (sourceNode.id === node.id) return
    if (canvasNodeType(sourceNode) !== "image") return
    addCandidate(canvasNodeTitle(sourceNode), ref, "node", canvasImagePreviewUrl(sourceNode, projectId))
  }

  for (const edge of canvasEdges) {
    if (String(edge.target || "") !== node.id) continue
    const sourceNode = canvasNodes.find((item) => item.id === String(edge.source || ""))
    if (!sourceNode) continue
    addNodeCandidate(sourceNode)
  }

  referenceValuesForMentions(node, draft).forEach((value, index) => {
    const sourceNode = resolveCanvasReferenceNode(value, nodeLookup)
    if (sourceNode) {
      addNodeCandidate(sourceNode, canvasNodeReferenceValue(sourceNode))
      return
    }
    const normalized = normalizeReferenceValue(value, `参考图${index + 1}`)
    if (!normalized) return
    if (normalized.kind === "node") {
      const sourceNode = nodeLookup.get(normalized.value)
        || nodeLookup.get(`node:${normalized.value}`)
        || nodeLookup.get(stripNodeReferenceMarker(normalized.value))
      if (sourceNode) {
        addNodeCandidate(sourceNode, value)
        return
      }
    }
    const fallback = basenameFromReferenceValue(value) || normalized.label || `参考图${index + 1}`
    const previewUrl = resolveReferenceImageUrl(projectId, value, true)
    addCandidate(fallback, value, "reference", previewUrl)
  })

  return candidates
}

function referenceImageMentionsFromPrompt(
  prompt: string,
  candidates: ReferenceMentionCandidate[],
  referenceImages: string[],
): ReferenceImageMention[] {
  if (!prompt || candidates.length === 0) return []
  const result: ReferenceImageMention[] = []
  const seen = new Set<string>()
  const refOrder = new Map<string, number>()
  referenceImages.forEach((ref, index) => {
    refOrder.set(stripNodeReferenceMarker(ref).toLowerCase(), index + 1)
  })
  for (const candidate of candidates) {
    if (!candidate.mention || !prompt.includes(candidate.mention)) continue
    const key = `${candidate.mention}:${referenceMentionCandidateKey(candidate)}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push({
      mention: candidate.mention,
      label: candidate.label,
      ref: candidate.ref,
      source: candidate.source,
      index: refOrder.get(referenceMentionCandidateKey(candidate)),
    })
  }
  return result
}

function referenceMentionCandidateRefs(candidates: ReferenceMentionCandidate[]): string[] {
  return Array.from(new Set(candidates.map((item) => item.ref.trim()).filter(Boolean)))
}

function referenceValueStrings(value: unknown, depth = 0): string[] {
  if (value == null || value === "" || depth > 4) return []
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    const text = String(value).trim()
    return text ? [text] : []
  }
  if (Array.isArray(value)) {
    return value.flatMap((item) => referenceValueStrings(item, depth + 1))
  }
  const obj = asObj(value)
  if (!obj) return []
  const keys = [
    "ref",
    "reference",
    "reference_input",
    "value",
    "node_id",
    "nodeId",
    "source_node_id",
    "sourceNodeId",
    "source_step",
    "from_step",
    "source",
    "candidate",
    "candidates",
    "id",
    "rel_path",
    "path",
    "source_path",
    "url",
    "local_url",
    "remote_url",
  ]
  return keys.flatMap((key) => referenceValueStrings(obj[key], depth + 1))
}

function referenceValuesForMentions(node: NodeFull, draft: EditableNodeDraft): string[] {
  const input = rawNodeInput(node.input)
  const fields = asObj(input.fields) || {}
  const output = asObj(parseJson(node.output)) || {}
  const values = [
    draft.reference_images,
    input.reference_images,
    input.references,
    input.depends_on,
    fields.reference_images,
    fields.references,
    fields.depends_on,
    output.reference_images,
    output.references,
  ].flatMap((item) => referenceValueStrings(item))
  return Array.from(new Set(values.map((item) => item.trim()).filter(Boolean)))
}

function referenceDisplayCount(
  refs: string[],
  implicitRefs: string[],
  projectId?: string | null,
): number {
  const items = [...refs, ...implicitRefs]
    .map((value) => normalizeReferenceValue(value, "引用图"))
    .filter((ref): ref is ReferenceItem => Boolean(ref))
  return uniqueReferenceItems(items, projectId).length
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
  if (typeof refValue === "number" || typeof refValue === "boolean") {
    const normalized = normalizeReferenceValue(String(refValue), label, refId)
    if (normalized) return normalized
  }
  const nodeId = obj.node_id || obj.nodeId || obj.source_node_id || obj.sourceNodeId
  if (typeof nodeId === "string" && nodeId) return { kind: "node", value: nodeId, label }
  if (typeof nodeId === "number" || typeof nodeId === "boolean") return { kind: "node", value: String(nodeId), label }
  const assetId = obj.asset_id || obj.assetId
  if (typeof assetId === "string" && assetId) return { kind: "asset", value: assetId, label }
  if (typeof assetId === "number" || typeof assetId === "boolean") return { kind: "asset", value: String(assetId), label }
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

  const pushImageList = (value: unknown, fallbackLabel: string) => {
    if (!Array.isArray(value)) return
    value.forEach((item, index) => {
      const image = asObj(item)
      if (image) {
        pushImage(
          pickUrl(image),
          firstText(image.label, image.title, image.name) || `${fallbackLabel} ${index + 1}`,
          typeof image.prompt === "string" ? image.prompt : undefined,
          image.width,
          image.height,
        )
      } else if (typeof item === "string") {
        pushImage(item, `${fallbackLabel} ${index + 1}`)
      }
    })
  }

  const pushVideoList = (value: unknown, fallbackLabel: string) => {
    if (!Array.isArray(value)) return
    value.forEach((item, index) => {
      const video = asObj(item)
      if (video) {
        pushVideo(
          pickUrl(video),
          typeof video.poster === "string" ? video.poster : undefined,
          firstText(video.label, video.title, video.name) || `${fallbackLabel} ${index + 1}`,
          typeof video.prompt === "string" ? video.prompt : undefined,
          video.width,
          video.height,
        )
      } else if (typeof item === "string") {
        pushVideo(item, undefined, `${fallbackLabel} ${index + 1}`)
      }
    })
  }

  const pushAudioList = (value: unknown, fallbackLabel: string) => {
    if (!Array.isArray(value)) return
    value.forEach((item, index) => {
      const audio = asObj(item)
      if (audio) {
        pushAudio(
          pickUrl(audio),
          firstText(audio.label, audio.title, audio.name) || `${fallbackLabel} ${index + 1}`,
          typeof audio.prompt === "string" ? audio.prompt : undefined,
        )
      } else if (typeof item === "string") {
        pushAudio(item, `${fallbackLabel} ${index + 1}`)
      }
    })
  }

  pushImageList(obj.images, "图片")
  pushImageList(obj.image_outputs, "图片")
  pushImageList(obj.output_images, "图片")
  pushVideoList(obj.videos, "视频")
  pushVideoList(obj.video_outputs, "视频")
  pushAudioList(obj.audios, "音频")
  pushAudioList(obj.audio_outputs, "音频")

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
  const entries: MediaHistoryEntry[] = []
  if (isSuccessfulMediaHistoryOutput(output)) {
    const currentMedia = collectMedia(output).filter((mediaItem) => mediaItem.kind === kind)
    if (currentMedia.length > 0) {
      entries.push({
        id: "current",
        index: -1,
        current: true,
        created_at: firstText(obj?.created_at, obj?.updated_at, obj?.completed_at) || undefined,
        type: firstText(obj?.type) || undefined,
        prompt: pickPromptText("", {}, obj || {}),
        output,
        media: currentMedia,
      })
    }
  }
  const raw = obj?.history ?? obj?.media_history
  if (!Array.isArray(raw)) return entries
  const historyEntries = raw
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
  return [...entries, ...historyEntries]
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
      const stageDone = ["completed", "success", "succeeded", "done"].includes(stageStatus)
      if (stageStatus && !stageDone) return false
      if (!stageDone && typeof stageObj.error === "string" && stageObj.error.trim()) return false
      if (!stageDone && typeof stageObj.error_message === "string" && stageObj.error_message.trim()) return false
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

const MEDIA_RERUN_NODE_TYPES = new Set(["text", "image", "video", "audio"])

function CopyIcon({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="8" y="8" width="11" height="11" rx="2.2" />
      <path d="M5 15.5V6.8C5 5.8 5.8 5 6.8 5h8.7" />
    </svg>
  )
}

function CheckIcon({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M5 12.5l4.2 4.2L19 7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function PlusIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
      <path d="M12 5v14M5 12h14" strokeLinecap="round" />
    </svg>
  )
}

function XIcon({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M7 7l10 10M17 7 7 17" strokeLinecap="round" />
    </svg>
  )
}

function ImageIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="4" y="5" width="16" height="14" rx="2.4" />
      <path d="m7 16 3.6-3.8 2.7 2.8 1.7-1.8L19 17" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="15.6" cy="9.2" r="1.2" />
    </svg>
  )
}

function UploadIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
      <path d="M12 15V5" strokeLinecap="round" />
      <path d="m8 9 4-4 4 4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 16.5v1.2A2.3 2.3 0 0 0 7.3 20h9.4a2.3 2.3 0 0 0 2.3-2.3v-1.2" strokeLinecap="round" />
    </svg>
  )
}

function ArrowUpIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M12 19V6" strokeLinecap="round" />
      <path d="m7 11 5-5 5 5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function SparkIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M13 3 11.2 8.2 6 10l5.2 1.8L13 17l1.8-5.2L20 10l-5.2-1.8L13 3Z" strokeLinejoin="round" />
      <path d="M5.5 15.5 4.6 18l-2.5.9 2.5.9.9 2.5.9-2.5 2.5-.9-2.5-.9-.9-2.5Z" strokeLinejoin="round" />
    </svg>
  )
}

function ChatBubbleIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M5 6.8A3.8 3.8 0 0 1 8.8 3h6.4A3.8 3.8 0 0 1 19 6.8v4.7a3.8 3.8 0 0 1-3.8 3.8H11l-4.5 4.2v-4.4A3.8 3.8 0 0 1 5 11.5V6.8Z" strokeLinejoin="round" />
      <path d="M8.5 8h7M8.5 11h4.8" strokeLinecap="round" />
    </svg>
  )
}

async function copyTextToClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const textarea = document.createElement("textarea")
  textarea.value = text
  textarea.setAttribute("readonly", "")
  textarea.style.position = "fixed"
  textarea.style.left = "-9999px"
  textarea.style.top = "0"
  document.body.appendChild(textarea)
  textarea.select()
  document.execCommand("copy")
  document.body.removeChild(textarea)
}

function CopyTextButton({
  text,
  label = "内容",
  className = "",
}: {
  text: string
  label?: string
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<number | null>(null)
  const disabled = !text.trim()
  const title = copied ? `已复制${label}` : `复制${label}`

  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current)
    }
  }, [])

  return (
    <button
      type="button"
      aria-label={title}
      title={title}
      disabled={disabled}
      onClick={(event) => {
        event.preventDefault()
        event.stopPropagation()
        if (disabled) return
        void copyTextToClipboard(text)
          .then(() => {
            setCopied(true)
            if (timerRef.current) window.clearTimeout(timerRef.current)
            timerRef.current = window.setTimeout(() => setCopied(false), 1200)
          })
          .catch((error) => console.warn("Failed to copy node detail text", error))
      }}
      className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border transition disabled:cursor-not-allowed disabled:opacity-35 ${
        copied
          ? "border-emerald-300/35 bg-emerald-400 text-emerald-950"
          : "border-white/[0.1] bg-black/55 text-zinc-200 hover:bg-white/[0.08] hover:text-white"
      } ${className}`}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </button>
  )
}

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
    <div className="relative">
      {children.trim() && <CopyTextButton text={children} label="内容" className="absolute right-2 top-2 z-10" />}
      <div className="max-h-[320px] overflow-y-auto rounded-lg bg-black/30 px-3.5 py-3 pr-12 text-[13px] leading-6 text-zinc-200 shadow-inner shadow-black/25">
        <MarkdownView compact>{children}</MarkdownView>
      </div>
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

function nodeAdvancedHistoryKinds(node: NodeFull): Array<"image" | "video" | "audio"> {
  return (["image", "video", "audio"] as const)
    .filter((kind) => mediaHistoryEntriesFromOutput(node.output, kind).length > 0)
}

function nodeAdvancedHistoryCount(node: NodeFull): number {
  if (node.type === "text") {
    return textChatHistoryFromPayload(nodeInputFields(node.input), node.output, node.prompt || "").length
  }
  return nodeAdvancedHistoryKinds(node)
    .reduce((count, kind) => count + mediaHistoryEntriesFromOutput(node.output, kind).length, 0)
}

function NodeRawDataBlock({ node }: { node: NodeFull }) {
  return (
    <div className="space-y-3">
      <div className="grid gap-2 text-[11px] text-zinc-400 sm:grid-cols-2">
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
          <div className="mb-1 text-[11px] uppercase tracking-wide text-zinc-500">{String(label)}</div>
          <pre className="max-h-72 overflow-auto rounded-md border border-white/[0.08] bg-black/45 p-2 text-[11px] leading-5 text-zinc-200">
            {prettyJson(value)}
          </pre>
        </div>
      ))}
    </div>
  )
}

function NodeAdvancedSurface({
  node,
  displayError,
  mediaProgress,
  switchingHistoryId,
  onSwitchHistory,
}: {
  node: NodeFull
  displayError: string
  mediaProgress: MediaProgressInfo | null
  switchingHistoryId?: string | null
  onSwitchHistory: (entry: MediaHistoryEntry) => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [rawOpen, setRawOpen] = useState(false)
  const historyKinds = nodeAdvancedHistoryKinds(node)
  const historyCount = nodeAdvancedHistoryCount(node)
  const busy = node.status === "running" || node.status === "queued"
  const chips = [
    displayError ? "错误" : "",
    mediaProgress ? "进度" : "",
    historyCount > 0 ? `历史 ${historyCount}` : "",
    "原始数据",
  ].filter(Boolean)

  return (
    <section className="overflow-hidden rounded-lg border border-white/[0.08] bg-[#10151d]/86">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-3 px-3.5 py-3 text-left transition hover:bg-white/[0.04]"
      >
        <span className="min-w-0">
          <span className="block text-[12px] font-semibold text-zinc-200">高级与诊断</span>
          <span className="mt-1 flex flex-wrap gap-1.5">
            {chips.map((chip) => (
              <span key={chip} className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-zinc-500">
                {chip}
              </span>
            ))}
          </span>
        </span>
        <span className="shrink-0 text-[11px] text-zinc-500">{open ? "收起" : "展开"}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-white/[0.08] p-3.5">
          {displayError && (
            <Section title="运行错误">
              <div className="rounded border border-red-900/60 bg-red-950/40 px-3 py-2 text-[13px] text-red-300 whitespace-pre-wrap break-words">
                {displayError}
              </div>
            </Section>
          )}
          {mediaProgress && (
            <Section title="运行进度">
              <div className="h-2 overflow-hidden rounded-full bg-white/[0.06]">
                <div
                  className="h-full rounded-full bg-cyan-300"
                  style={{ width: `${mediaProgress.percent}%` }}
                />
              </div>
              <div className="mt-2 text-[12px] text-zinc-400">{mediaProgress.label}</div>
            </Section>
          )}
          {historyKinds.map((kind) => (
            <MediaHistorySection
              key={kind}
              kind={kind}
              output={node.output}
              busy={busy}
              switchingId={switchingHistoryId}
              onSwitch={onSwitchHistory}
            />
          ))}
          {node.type === "text" && (
            <TextHistorySection
              input={nodeInputFields(node.input)}
              rawOutput={node.output}
              nodePrompt={node.prompt || ""}
            />
          )}
          <Section title="原始数据">
            <button
              type="button"
              onClick={() => setRawOpen((value) => !value)}
              className="rounded-md border border-white/[0.1] bg-white/[0.04] px-3 py-2 text-xs font-medium text-zinc-200 transition hover:bg-white/[0.08]"
            >
              {rawOpen ? "隐藏 input / output / prompt" : "查看 input / output / prompt"}
            </button>
            {rawOpen && (
              <div className="mt-3">
                <NodeRawDataBlock node={node} />
              </div>
            )}
          </Section>
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

function isImageSource(value: unknown): boolean {
  return typeof value === "string" && /(\.(png|jpe?g|webp|gif|bmp|svg)(\?|#|$)|^data:image\/)/i.test(value)
}

function resolveReferenceImageUrl(projectId: string | null | undefined, value: string, trustImage = false): string {
  const raw = value.trim()
  if (!raw) return ""
  const resolved = referenceFileUrl(projectId, raw) || resolveMediaUrl(raw) || raw
  const looksVideo = /\.(mp4|webm|mov)(\?|#|$)/i.test(raw) || /\.(mp4|webm|mov)(\?|#|$)/i.test(resolved)
  const looksAudio = /\.(mp3|wav|m4a|aac|ogg|flac)(\?|#|$)/i.test(raw) || /\.(mp3|wav|m4a|aac|ogg|flac)(\?|#|$)/i.test(resolved)
  if (!resolved || looksVideo || looksAudio) {
    return ""
  }
  if (
    trustImage ||
    isImageSource(raw) ||
    isImageSource(resolved) ||
    raw.startsWith("generated_images/") ||
    raw.startsWith("upload:") ||
    raw.startsWith("uploads/")
  ) {
    return resolved
  }
  return ""
}

function firstReferenceImageUrl(value: unknown, projectId?: string | null, depth = 0): string {
  if (value == null || depth > 5) return ""
  const parsed = parseJson(value)
  if (typeof parsed === "string") {
    return resolveReferenceImageUrl(projectId, parsed)
  }
  if (Array.isArray(parsed)) {
    for (const item of parsed) {
      const url = firstReferenceImageUrl(item, projectId, depth + 1)
      if (url) return url
    }
    return ""
  }
  const grid = imageGridFromOutput(parsed)
  if (grid) {
    const url = grid.local_url || grid.composite_url || grid.url || ""
    const resolved = resolveReferenceImageUrl(projectId, url, true)
    if (resolved) return resolved
  }
  const media = collectMedia(parsed).find((item) => item.kind === "image")
  if (media?.src) return media.src
  const obj = asObj(parsed)
  if (!obj) return ""
  const directKeys = [
    "local_url",
    "url",
    "remote_url",
    "composite_url",
    "image_url",
    "thumbnail_url",
    "preview_url",
    "poster",
  ]
  for (const key of directKeys) {
    const candidate = obj[key]
    if (typeof candidate !== "string" || !candidate) continue
    const url = resolveReferenceImageUrl(projectId, candidate, key !== "url")
    if (url) return url
  }
  const nestedKeys = [
    "image",
    "result",
    "output",
    "media",
    "asset",
    "file",
    "preview",
    "thumbnail",
    "source_image",
    "reference_image",
    "selected",
  ]
  for (const key of nestedKeys) {
    if (!Object.prototype.hasOwnProperty.call(obj, key)) continue
    const url = firstReferenceImageUrl(obj[key], projectId, depth + 1)
    if (url) return url
  }
  const listKeys = [
    "stages",
    "images",
    "assets",
    "media_items",
    "files",
    "items",
    "history",
    "outputs",
    "results",
    "reference_images",
  ]
  for (const key of listKeys) {
    const items = obj[key]
    if (!Array.isArray(items)) continue
    for (const item of items) {
      const url = firstReferenceImageUrl(item, projectId, depth + 1)
      if (url) return url
    }
  }
  return ""
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
  canvasLookup,
  compact = false,
}: {
  ref: ReferenceItem
  projectId?: string | null
  setLightbox: (v: { src: string; alt?: string } | null) => void
  canvasLookup?: Map<string, CanvasGraphNode>
  compact?: boolean
}) {
  const refTitle = ref.kind === "node" ? ref.label : `${ref.kind}:${ref.value}`
  const localNode = ref.kind === "node" && canvasLookup ? resolveCanvasReferenceNode(ref.value, canvasLookup) : undefined
  const localResolved = localNode ? canvasImagePreviewUrl(localNode, projectId) : ""
  // 异步把节点/资产引用解析到真实图片 url；本地画布已含上游输出时直接渲染，只把接口请求作为兜底。
  const [resolved, setResolved] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let cancelled = false
    setResolved(null)
    setFailed(false)
    if (localResolved) {
      setResolved(localResolved)
      return () => { cancelled = true }
    }
    ;(async () => {
      try {
        if (ref.kind === "url") {
          setResolved(resolveReferenceImageUrl(projectId, ref.value, true) || resolveMediaUrl(ref.value) || ref.value)
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
          if (!projectId) {
            setFailed(true)
            return
          }
          const nodeId = await resolveProjectNodeReference(projectId, ref.value)
          if (cancelled) return
          if (!nodeId) {
            setFailed(true)
            return
          }
          const r = await getProjectNodeDetails<Record<string, unknown>>(projectId, nodeId)
          if (cancelled) return
          const url = firstReferenceImageUrl(r?.output, projectId) || firstReferenceImageUrl(r, projectId)
          if (url) setResolved(url)
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
          if (u) setResolved(resolveReferenceImageUrl(projectId, u, true) || resolveMediaUrl(u) || u)
          else setFailed(true)
        }
      } catch {
        if (!cancelled) setFailed(true)
      }
    })()
    return () => { cancelled = true }
  }, [ref.kind, ref.value, projectId, localResolved])

  const displaySrc = localResolved || resolved
  if (displaySrc) {
    return (
      <button
        onClick={() => setLightbox({ src: displaySrc, alt: ref.label })}
        className={compact
          ? "group relative block h-8 w-8 overflow-hidden rounded-md bg-black/45 ring-1 ring-white/[0.13] shadow-[0_6px_16px_rgba(0,0,0,0.24)] transition hover:-translate-y-0.5 hover:ring-zinc-100/55"
          : "group relative block h-14 w-14 overflow-hidden rounded-lg bg-black/40 ring-1 ring-white/[0.08]"}
        title={ref.label}
      >
        <img
          src={displaySrc}
          alt={ref.label}
          className="h-full w-full object-cover"
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
        className="flex min-h-14 items-center rounded-lg bg-white/[0.03] px-2 py-1 text-[11px] text-gray-400 ring-1 ring-white/[0.08]"
        title={`${refTitle} 暂无可预览图片`}
      >
        {ref.kind === "text" ? ref.value : "引用图等待产出"}
      </span>
    )
  }
  // loading
  return (
    <div className="flex h-14 w-14 items-center justify-center rounded-lg bg-black/40 ring-1 ring-white/[0.08]" title={refTitle}>
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
  const canvasNodes = useCanvasStore((s) => s.nodes) as CanvasGraphNode[]
  const canvasLookup = useMemo(() => buildCanvasNodeLookup(canvasNodes), [canvasNodes])
  const normalized = uniqueReferenceItems((refs || [])
    .filter(isVisualReferenceRole)
    .map((ref) => normalizeReferenceForCanvas(ref, "引用图", canvasLookup))
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
          canvasLookup={canvasLookup}
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

function pickPromptText(nodePrompt: string, input: Record<string, unknown>, output: Record<string, unknown>): string {
  return nodePromptText({ input, output, prompt: nodePrompt })
}

function pickEditablePromptText(nodePrompt: string, input: Record<string, unknown>, output: Record<string, unknown>): string {
  return nodePromptText({ input, output, prompt: nodePrompt })
}

function pickReferences(input: Record<string, unknown>, output: Record<string, unknown>): unknown[] | undefined {
  const inputFields = asObj(input.fields) || {}
  const hasInputReferenceFields = ["depends_on", "reference_images", "references", "reference_assets"].some((key) =>
    hasOwnKey(input, key) || hasOwnKey(inputFields, key),
  )
  const inputValues = [
    input.depends_on,
    input.reference_images,
    input.references,
    input.reference_assets,
    inputFields.depends_on,
    inputFields.reference_images,
    inputFields.references,
    inputFields.reference_assets,
  ]
  const outputValues = [
    output.reference_images,
    output.references,
    output.reference_assets,
  ]
  const values = hasInputReferenceFields ? inputValues : [...inputValues, ...outputValues]
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

function textNodeBodyText(input: Record<string, unknown>, rawOutput: unknown, nodePrompt = ""): string {
  return nodeReadableText({ type: "text", input, output: rawOutput, prompt: nodePrompt })
}

interface TextChatHistoryEntry {
  id: string
  prompt: string
  content: string
  current?: boolean
  model?: string
  created_at?: string
}

function textChatHistoryFromPayload(input: Record<string, unknown>, rawOutput: unknown, nodePrompt = ""): TextChatHistoryEntry[] {
  const output = asObj(parseJson(rawOutput)) || {}
  const rawHistory = input.text_chat_history ?? input.chat_history ?? output.text_chat_history ?? output.chat_history
  const entries = Array.isArray(rawHistory) ? rawHistory
    .map((item, index): TextChatHistoryEntry | null => {
      const entry = asObj(item)
      if (!entry) return null
      const prompt = firstText(entry.prompt, entry.input, entry.user)
      const content = firstText(entry.content, entry.reply, entry.response, entry.output)
      if (!prompt && !content) return null
      return {
        id: firstText(entry.id) || `text-chat-${index}`,
        prompt,
        content,
        model: firstText(entry.model),
        created_at: firstText(entry.created_at, entry.completed_at),
      }
    })
    .filter((item): item is TextChatHistoryEntry => Boolean(item)) : []
  const currentContent = textNodeBodyText(input, rawOutput, nodePrompt)
  if (!currentContent) return entries
  const currentPrompt = firstText(output.prompt, input.prompt, nodePrompt)
  const duplicate = entries.some((entry) => entry.content === currentContent && entry.prompt === currentPrompt)
  if (duplicate) return entries
  return [
    ...entries,
    {
      id: "text-current",
      prompt: currentPrompt,
      content: currentContent,
      current: true,
      model: firstText(output.model, input.model),
      created_at: firstText(output.created_at, output.completed_at, input.updated_at),
    },
  ]
}

function TextNodeStructuredDetails({
  input,
  rawOutput,
  nodePrompt,
  refs,
  projectId,
  setLightbox,
}: {
  input: Record<string, unknown>
  rawOutput: unknown
  nodePrompt: string
  refs: unknown[] | undefined
  projectId?: string | null
  setLightbox: (v: { src: string; alt?: string } | null) => void
}) {
  const outputText = textNodeBodyText(input, rawOutput, nodePrompt)
  const hasContent = outputText.trim().length > 0
  return (
    <div className="space-y-3">
      {hasContent ? (
        <Section title="文本预览">
          <PromptBlock>{outputText}</PromptBlock>
          <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
        </Section>
      ) : (
        <Section title="文本预览">
          <ImagePlaceholder label="等待文本内容" />
          <ReferenceThumbStrip refs={refs} projectId={projectId} setLightbox={setLightbox} />
        </Section>
      )}
      {nodePrompt && (
        <Section title="当前提示词">
          <PromptBlock>{nodePrompt}</PromptBlock>
        </Section>
      )}
    </div>
  )
}

function TextHistorySection({
  input,
  rawOutput,
  nodePrompt,
}: {
  input: Record<string, unknown>
  rawOutput: unknown
  nodePrompt: string
}) {
  const entries = textChatHistoryFromPayload(input, rawOutput, nodePrompt)
  if (entries.length === 0) return null
  return (
    <Section title={`文本生成历史 (${entries.length})`}>
      <div className="space-y-2">
        {entries.slice().reverse().map((entry, index) => (
          <div key={`${entry.id}-${index}`} className="rounded-lg border border-white/[0.08] bg-black/25 p-3 text-[12px] leading-5 text-zinc-300">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[10px] text-zinc-500">
              <span>{entry.current ? "当前结果" : entry.created_at ? formatHistoryTime(entry.created_at) : `历史 ${entries.length - index}`}</span>
              {entry.model && <span>{entry.model}</span>}
            </div>
            {entry.prompt && (
              <div className="mb-2 rounded-md border border-white/[0.06] bg-white/[0.035] px-2.5 py-2 text-zinc-400">
                {entry.prompt}
              </div>
            )}
            <div className="whitespace-pre-wrap break-words text-zinc-100">
              {entry.content || "无正文记录"}
            </div>
          </div>
        ))}
      </div>
    </Section>
  )
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
  if (format === "audio_http_v1") return "unknown"
  if (["openai_tts", "tts", "openai_speech", "openai_audio_speech"].includes(format)) return "tts"
  if (["suno_compatible", "suno", "suno_api"].includes(format)) return "music"
  return "unknown"
}

function audioProviderModeFromProvider(provider?: AudioProviderOption): AudioProviderMode {
  const protocolId = String(provider?.params?.audio_protocol_id || "").trim().toLowerCase()
  if (protocolId.includes("suno") || protocolId.includes("music")) return "music"
  if (protocolId.includes("speech") || protocolId.includes("tts")) return "tts"
  return audioProviderModeFromFormat(provider?.api_format)
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
  return resolveMediaProvider(value, providers)
}

function mediaProvidersForKind(
  providers: MediaProviderOption[],
  kind: "image" | "video" | "audio",
): MediaProviderOption[] {
  return providers.filter((provider) => provider.kind === kind && provider.enabled !== false)
}

function resolveMediaProvider(
  value: string,
  providers: MediaProviderOption[],
): MediaProviderOption | undefined {
  const enabled = providers.filter((provider) => provider.enabled !== false)
  const selected = value.trim()
  if (selected) {
    return enabled.find((provider) => provider.name === selected || provider.model_name === selected)
  }
  return enabled.find((provider) => provider.is_active) || enabled[0]
}

function mediaProviderSelectValue(value: string, provider?: MediaProviderOption): string {
  return provider?.name || value.trim()
}

function mediaProviderDisplayLabel(provider: MediaProviderOption): string {
  const title = provider.name.trim()
  const model = provider.model_name.trim()
  if (title && model && title !== model) return `${title} · ${model}`
  return title || model || "未命名模型"
}

function mediaProviderSelectOptions(
  providers: MediaProviderOption[],
  currentValue: string,
  selectedProvider?: MediaProviderOption,
): Array<{ label: string; value: string; disabled?: boolean }> {
  const current = currentValue.trim()
  if (providers.length === 0) {
    return current
      ? [{ label: `当前: ${current}`, value: current }]
      : [{ label: "未配置可用模型", value: "", disabled: true }]
  }
  return [
    ...(current && !selectedProvider ? [{ label: `当前: ${current}`, value: current }] : []),
    ...providers.map((provider) => ({
      label: mediaProviderDisplayLabel(provider),
      value: provider.name,
    })),
  ]
}

function mediaProviderHint(provider?: MediaProviderOption, error?: string | null): string {
  if (error) return error
  if (provider) return mediaProviderDisplayLabel(provider)
  return "设置里还没有启用的生成模型。"
}

function mediaProviderParamStringArray(provider: MediaProviderOption | undefined, ...keys: string[]): string[] {
  const params = provider?.params || {}
  for (const key of keys) {
    const value = params[key]
    if (Array.isArray(value)) {
      const items = value.map((item) => String(item || "").trim()).filter(Boolean)
      if (items.length > 0) return items
    }
  }
  return []
}

function mediaProviderParamText(provider: MediaProviderOption | undefined, ...keys: string[]): string {
  const params = provider?.params || {}
  for (const key of keys) {
    const text = String(params[key] || "").trim()
    if (text) return text
  }
  return ""
}

function enabledLlmProviders(providers: LlmProviderOption[]): LlmProviderOption[] {
  return providers.filter((provider) => provider.enabled !== false)
}

function resolveLlmProvider(
  value: string,
  providers: LlmProviderOption[],
): LlmProviderOption | undefined {
  const enabled = enabledLlmProviders(providers)
  const selected = value.trim()
  if (!selected) return enabled[0]
  return enabled.find((provider) => {
    const model = provider.model_name.trim()
    const prefixed = model.includes("/") ? model : `${provider.provider}/${model}`
    return provider.name === selected || model === selected || prefixed === selected
  })
}

function llmProviderDisplayLabel(provider: LlmProviderOption): string {
  const title = provider.name.trim()
  const model = provider.model_name.trim()
  if (title && model && title !== model) return `${title} · ${model}`
  return title || model || "未命名模型"
}

function llmProviderSelectOptions(
  providers: LlmProviderOption[],
  currentValue: string,
  selectedProvider?: LlmProviderOption,
): Array<{ label: string; value: string; disabled?: boolean }> {
  const enabled = enabledLlmProviders(providers)
  const current = currentValue.trim()
  if (enabled.length === 0) {
    return current
      ? [{ label: `当前: ${current}`, value: current }]
      : [{ label: "未配置可用文本模型", value: "", disabled: true }]
  }
  return [
    ...(current && !selectedProvider ? [{ label: `当前: ${current}`, value: current }] : []),
    ...enabled.map((provider) => ({
      label: llmProviderDisplayLabel(provider),
      value: provider.name,
    })),
  ]
}

function defaultLlmProviderName(
  providers: LlmProviderOption[],
  defaults?: RuntimeModelDefaults,
): string {
  const enabled = enabledLlmProviders(providers)
  if (enabled.length === 0) return ""
  const assignments = defaults?.model_assignments || {}
  const tierDefaults = defaults?.model_tier_defaults || {}
  const preferred = [
    assignments.text_generation,
    assignments.outline_generation,
    assignments.script_generation,
    tierDefaults.balanced,
    tierDefaults.strong,
    tierDefaults.small,
  ].map((value) => String(value || "").trim()).filter(Boolean)
  for (const name of preferred) {
    if (enabled.some((provider) => provider.name === name)) return name
  }
  return enabled[0].name
}

function llmProviderHint(provider?: LlmProviderOption, error?: string | null): string {
  if (error) return error
  if (provider) return llmProviderDisplayLabel(provider)
  return "设置里还没有启用的文本模型。"
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item || "").trim()).filter(Boolean)
    : []
}

function finiteNumber(value: unknown): number | undefined {
  if (value == null || value === "") return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

type SelectOption = {
  label: string
  value: string
  disabled?: boolean
  hint?: string
}

function videoProtocolIdFromProvider(provider?: MediaProviderOption): string {
  return String(
    provider?.params?.video_protocol_id
    || provider?.params?.protocol_id
    || provider?.params?.protocol
    || "",
  ).trim()
}

function videoProtocolForProvider(
  provider: MediaProviderOption | undefined,
  protocols: VideoProtocolSummary[],
): VideoProtocolSummary | undefined {
  const protocolId = videoProtocolIdFromProvider(provider)
  if (protocolId) {
    const exact = protocols.find((protocol) => protocol.id === protocolId)
    if (exact) return exact
  }
  const modelName = String(provider?.model_name || "").trim()
  if (!modelName) return undefined
  return protocols.find((protocol) => {
    if (protocol.model_names?.includes(modelName)) return true
    return (protocol.model_profiles || []).some((profile) => profile.match === modelName)
  })
}

function videoProfileForModel(
  protocol: VideoProtocolSummary | undefined,
  modelName: string,
): VideoProtocolProfileSummary | undefined {
  const name = modelName.trim()
  if (!protocol || !name) return undefined
  return (protocol.model_profiles || []).find((profile) => profile.match === name)
}

function videoModeEntriesForProvider(
  protocol?: VideoProtocolSummary,
  profile?: VideoProtocolProfileSummary,
): Map<string, VideoProtocolModeSummary> {
  const modes = protocol?.modes || {}
  const byCanonical = new Map<string, VideoProtocolModeSummary>()
  Object.entries(modes).forEach(([mode, config]) => {
    byCanonical.set(canonicalVideoMode(mode), config)
  })
  if (Array.isArray(profile?.modes)) {
    const allowed = new Set(profile.modes.map((mode) => canonicalVideoMode(String(mode))))
    Array.from(byCanonical.keys()).forEach((mode) => {
      if (!allowed.has(mode)) byCanonical.delete(mode)
    })
  } else if (profile?.modes && typeof profile.modes === "object") {
    const overrides = new Map<string, VideoProtocolModeSummary>()
    Object.entries(profile.modes).forEach(([mode, config]) => {
      const canonical = canonicalVideoMode(mode)
      overrides.set(canonical, { ...(byCanonical.get(canonical) || {}), ...config })
    })
    byCanonical.clear()
    overrides.forEach((config, mode) => byCanonical.set(mode, config))
  }
  if (profile?.supported_modes?.length) {
    const allowed = new Set(profile.supported_modes.map((mode) => canonicalVideoMode(mode)))
    Array.from(byCanonical.keys()).forEach((mode) => {
      if (!allowed.has(mode)) byCanonical.delete(mode)
    })
  }
  return byCanonical
}

function mergeVideoDurationRule(...items: Array<VideoDurationSummary | undefined>): VideoDurationSummary {
  return items.reduce<VideoDurationSummary>((acc, item) => {
    if (!item || typeof item !== "object") return acc
    return { ...acc, ...item }
  }, {})
}

function hasDeclaredVideoDurationRule(rule: VideoDurationSummary): boolean {
  return finiteNumber(rule.min) !== undefined
    || finiteNumber(rule.max) !== undefined
    || (Array.isArray(rule.allowed_values) && rule.allowed_values.some((value) => {
      const parsed = finiteNumber(value)
      return parsed !== undefined && parsed > 0
    }))
}

function videoModeLimitHint(mode: string, config?: VideoProtocolModeSummary, durationRule?: VideoDurationSummary): string | undefined {
  const lines: string[] = []
  const fullLabel = String(config?.label || "").trim()
  if (fullLabel && fullLabel !== VIDEO_MODE_LABELS[mode]) lines.push(fullLabel)
  const duration = videoDurationHint(durationRule || {})
  if (duration) lines.push(duration.replace(/^当前模型支持/, "时长"))
  const minImages = finiteNumber(config?.min_images)
  const maxImages = finiteNumber(config?.max_images)
  if ((minImages === undefined || minImages === 0) && maxImages === 0) {
    lines.push("不使用参考图")
  } else if (minImages !== undefined && maxImages !== undefined && minImages === maxImages) {
    lines.push(`需要 ${minImages} 张参考图`)
  } else {
    if (minImages !== undefined && minImages > 0) lines.push(`至少 ${minImages} 张参考图`)
    if (maxImages !== undefined) lines.push(`最多 ${maxImages} 张参考图`)
  }
  const maxVideos = finiteNumber(config?.max_videos)
  const maxAudios = finiteNumber(config?.max_audios)
  if (maxVideos && maxVideos > 0) lines.push(`最多 ${maxVideos} 个视频参考`)
  if (maxAudios && maxAudios > 0) lines.push(`最多 ${maxAudios} 个音频参考`)
  return lines.length ? lines.join("\n") : undefined
}

function videoProtocolModeOptions(
  protocol?: VideoProtocolSummary,
  profile?: VideoProtocolProfileSummary,
): SelectOption[] {
  const byCanonical = videoModeEntriesForProvider(protocol, profile)
  const sortedModes = [
    ...VIDEO_MODE_ORDER.filter((mode) => byCanonical.has(mode)),
    ...Array.from(byCanonical.keys()).filter((mode) => !VIDEO_MODE_ORDER.includes(mode)),
  ]
  const options = sortedModes.map((mode) => {
    const config = byCanonical.get(mode)
    const duration = mergeVideoDurationRule(protocol?.duration, profile?.duration, config?.duration)
    return {
      label: VIDEO_MODE_LABELS[mode] || config?.label || mode,
      value: mode,
      hint: videoModeLimitHint(mode, config, duration),
    }
  })
  if (options.length > 0) return options
  if (Object.keys(protocol?.modes || {}).length > 0) return []
  return VIDEO_MODE_ORDER.map((mode) => ({ label: VIDEO_MODE_LABELS[mode], value: mode }))
}

function canonicalVideoMode(value: string): string {
  const mode = value.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_")
  if (["t2v", "txt2video", "text2video"].includes(mode)) return "text_to_video"
  if (["i2v", "image_to_video", "source_image", "single_image"].includes(mode)) return "first_frame"
  if (["first_last", "first_and_last_frame", "first_last_frames"].includes(mode)) return "first_last_frame"
  if (["reference_to_video", "reference_video", "omni_reference", "omni_reference_video"].includes(mode)) return "multimodal_reference"
  return mode
}

function effectiveVideoMode(value: string, options: Array<{ value: string }>): string {
  const canonical = canonicalVideoMode(value)
  if (options.some((item) => item.value === canonical)) return canonical
  return options[0]?.value || ""
}

function videoModeConfig(
  protocol: VideoProtocolSummary | undefined,
  mode: string,
  profile?: VideoProtocolProfileSummary,
): VideoProtocolModeSummary | undefined {
  return videoModeEntriesForProvider(protocol, profile).get(canonicalVideoMode(mode))
}

function videoDurationRuleForProvider(
  provider: MediaProviderOption | undefined,
  protocol: VideoProtocolSummary | undefined,
  profile: VideoProtocolProfileSummary | undefined,
  mode: string,
): VideoDurationSummary {
  const modeConfig = videoModeConfig(protocol, mode, profile)
  const params = provider?.params || {}
  const firstFiniteParam = (...keys: string[]) => {
    for (const key of keys) {
      const value = finiteNumber(params[key])
      if (value !== undefined) return value
    }
    return undefined
  }
  const allowedValues = ["supported_durations", "duration_values", "allowed_durations"]
    .map((key) => params[key])
    .find((value) => Array.isArray(value))
  const rule = mergeVideoDurationRule(
    protocol?.duration,
    profile?.duration,
    modeConfig?.duration,
    {
      min: firstFiniteParam("duration_min", "min_duration", "minDuration"),
      max: firstFiniteParam("duration_max", "max_duration", "maxDuration"),
      step: firstFiniteParam("duration_step", "step_duration", "durationStep"),
      allowed_values: Array.isArray(allowedValues) ? allowedValues : undefined,
    },
  )
  return hasDeclaredVideoDurationRule(rule) ? rule : DEFAULT_VIDEO_DURATION_RULE
}

function videoDurationBounds(rule: VideoDurationSummary): { min?: number; max?: number; step?: number; allowed: number[] } {
  const allowed = Array.isArray(rule.allowed_values)
    ? rule.allowed_values.map(finiteNumber).filter((item): item is number => item !== undefined && item > 0)
    : []
  const allowedMin = allowed.length ? Math.min(...allowed) : undefined
  const allowedMax = allowed.length ? Math.max(...allowed) : undefined
  const min = finiteNumber(rule.min) ?? allowedMin
  const max = finiteNumber(rule.max) ?? allowedMax
  const step = finiteNumber(rule.step) || 1
  return { min, max, step, allowed, }
}

function normalizeVideoDurationForRule(value: string, rule: VideoDurationSummary): string {
  const text = value.trim()
  if (!text) return text
  const parsed = finiteNumber(text)
  if (parsed === undefined) return text
  const bounds = videoDurationBounds(rule)
  let next = Math.round(parsed)
  if (bounds.min !== undefined && next < bounds.min) next = bounds.min
  if (bounds.max !== undefined && next > bounds.max) next = bounds.max
  if (bounds.allowed.length > 0 && !bounds.allowed.includes(next)) {
    next = bounds.allowed.reduce((best, candidate) => (
      Math.abs(candidate - next) < Math.abs(best - next) ? candidate : best
    ), bounds.allowed[0])
  }
  return String(next)
}

function videoDurationHint(rule: VideoDurationSummary): string | undefined {
  const bounds = videoDurationBounds(rule)
  const parts: string[] = []
  if (bounds.min !== undefined && bounds.max !== undefined) parts.push(`${bounds.min}-${bounds.max} 秒`)
  else if (bounds.min !== undefined) parts.push(`不少于 ${bounds.min} 秒`)
  else if (bounds.max !== undefined) parts.push(`不超过 ${bounds.max} 秒`)
  if (bounds.allowed.length > 0) parts.push(`可选 ${bounds.allowed.join(" / ")} 秒`)
  return parts.length ? `当前模型支持 ${parts.join("，")}` : undefined
}

function videoReferenceRuleHint(
  modeConfig: VideoProtocolModeSummary | undefined,
  referenceCount: number,
): { quota?: string; title?: string; invalid: boolean; limit?: number; remaining?: number; overLimit?: boolean; uploadBlocked?: boolean } {
  if (!modeConfig) return { invalid: false }
  const minImages = finiteNumber(modeConfig.min_images)
  const minTotal = finiteNumber(modeConfig.min_total_media)
  const limit = videoReferenceImageLimit(modeConfig)
  const parts: string[] = []
  if ((minImages === undefined || minImages === 0) && limit === 0) {
    parts.push("不使用参考图")
  } else if (minImages !== undefined && limit !== undefined && minImages === limit) {
    parts.push(`需要 ${minImages} 张图`)
  } else {
    if (minImages !== undefined && minImages > 0) parts.push(`至少 ${minImages} 张图`)
    if (limit !== undefined) parts.push(`最多 ${limit} 张图`)
  }
  if (minTotal !== undefined && minTotal > 0) parts.push(`至少 ${minTotal} 个参考`)
  const overLimit = limit !== undefined && referenceCount > limit
  const effectiveCount = limit !== undefined ? Math.min(referenceCount, limit) : referenceCount
  const remaining = limit !== undefined ? Math.max(0, limit - effectiveCount) : undefined
  const invalid = (minImages !== undefined && referenceCount < minImages)
    || (minTotal !== undefined && referenceCount < minTotal)
  return {
    quota: limit !== undefined ? `${effectiveCount}/${limit}` : undefined,
    title: parts.length
      ? `${parts.join("，")}；当前 ${referenceCount} 张图${overLimit ? `，实际使用前 ${effectiveCount} 张` : ""}`
      : undefined,
    invalid,
    limit,
    remaining,
    overLimit,
    uploadBlocked: limit !== undefined && remaining === 0,
  }
}

function videoSupportedRatiosForProvider(
  provider: MediaProviderOption | undefined,
  protocol: VideoProtocolSummary | undefined,
  profile: VideoProtocolProfileSummary | undefined,
  mode: string,
): string[] {
  const modeConfig = videoModeConfig(protocol, mode, profile)
  const providerValues = mediaProviderParamStringArray(provider, "supported_ratios", "ratios", "supported_aspect_ratios")
  const values = (
    providerValues.length ? providerValues
      : stringArray(modeConfig?.supported_ratios).length ? stringArray(modeConfig?.supported_ratios)
      : stringArray(profile?.supported_ratios).length ? stringArray(profile?.supported_ratios)
      : stringArray(protocol?.supported_ratios).length ? stringArray(protocol?.supported_ratios)
      : []
  )
  return Array.from(new Set(values)).filter((item) => item !== "adaptive")
}

function videoSupportedResolutionsForProvider(
  provider: MediaProviderOption | undefined,
  protocol: VideoProtocolSummary | undefined,
  profile: VideoProtocolProfileSummary | undefined,
  mode: string,
): string[] {
  const modeConfig = videoModeConfig(protocol, mode, profile)
  const providerValues = mediaProviderParamStringArray(provider, "supported_resolutions", "resolutions")
  const values = providerValues.length ? providerValues
    : stringArray(modeConfig?.supported_resolutions).length ? stringArray(modeConfig?.supported_resolutions)
    : stringArray(profile?.supported_resolutions).length ? stringArray(profile?.supported_resolutions)
    : stringArray(protocol?.supported_resolutions).length ? stringArray(protocol?.supported_resolutions)
    : []
  return Array.from(new Set(values.map((item) => item.toLowerCase())))
}

function defaultVideoResolutionForProvider(
  provider: MediaProviderOption | undefined,
  protocol: VideoProtocolSummary | undefined,
  profile: VideoProtocolProfileSummary | undefined,
  mode: string,
): string {
  const modeConfig = videoModeConfig(protocol, mode, profile)
  const direct = mediaProviderParamText(provider, "default_resolution", "resolution").toLowerCase()
    || String(modeConfig?.default_resolution || profile?.default_resolution || protocol?.default_resolution || "").trim().toLowerCase()
  const supported = videoSupportedResolutionsForProvider(provider, protocol, profile, mode)
  if (direct && supported.includes(direct)) return direct
  return supported[0] || ""
}

function defaultVideoAspectRatioForProvider(
  provider: MediaProviderOption | undefined,
  protocol: VideoProtocolSummary | undefined,
  profile: VideoProtocolProfileSummary | undefined,
  mode: string,
): string {
  const modeConfig = videoModeConfig(protocol, mode, profile)
  return mediaProviderParamText(provider, "default_ratio", "aspect_ratio", "aspectRatio")
    || String(modeConfig?.default_ratio || profile?.default_ratio || protocol?.default_ratio || "").trim()
}

function videoAspectSelectOptions(
  selectedRatio: string,
  supportedRatios: string[],
): SelectOption[] {
  const current = normalizeVideoAspectRatio(selectedRatio)
  const supported = Array.from(new Set(supportedRatios.filter(Boolean)))
  if (supported.length === 0) {
    return current
      ? [{ label: `当前: ${current}`, value: current, disabled: true }]
      : [{ label: "未配置比例", value: "", disabled: true }]
  }
  return [
    ...(current && !supported.includes(current)
      ? [{ label: `当前: ${current}`, value: current, disabled: true }]
      : []),
    ...supported.map((value) => ({ label: value, value })),
  ]
}

function videoResolutionSelectOptions(
  selectedResolution: string,
  supportedResolutions: string[],
): Array<{ label: string; value: string; disabled?: boolean }> {
  const current = selectedResolution.trim().toLowerCase()
  const supported = Array.from(new Set(supportedResolutions.map((item) => item.toLowerCase()).filter(Boolean)))
  if (supported.length === 0) {
    return current
      ? [{ label: `当前: ${current}`, value: current, disabled: true }]
      : [{ label: "未配置清晰度", value: "", disabled: true }]
  }
  return [
    ...(current && !supported.includes(current)
      ? [{ label: `当前: ${current}`, value: current, disabled: true }]
      : []),
    ...supported.map((value) => ({ label: value, value, disabled: false })),
  ]
}

function mediaResolutionButtonLabel(value: string, label?: string): string {
  const text = (label || value).replace(/\s*\(.+\)\s*$/, "").trim()
  if (/^\d+$/.test(text)) return `${text}P`
  if (/^\d+p$/i.test(value)) return value.toUpperCase()
  return text.toUpperCase()
}

function normalizeVideoDraftForMode(
  draft: EditableNodeDraft,
  provider: MediaProviderOption | undefined,
  protocol: VideoProtocolSummary | undefined,
  profile: VideoProtocolProfileSummary | undefined,
  mode: string,
): Pick<EditableNodeDraft, "video_mode" | "resolution" | "aspect_ratio" | "duration_seconds"> {
  const supported = videoSupportedResolutionsForProvider(provider, protocol, profile, mode)
  const draftResolution = draft.resolution.trim().toLowerCase()
  const resolution = supported.length === 0 || supported.includes(draftResolution)
    ? draft.resolution
    : defaultVideoResolutionForProvider(provider, protocol, profile, mode)
  const ratios = videoSupportedRatiosForProvider(provider, protocol, profile, mode)
  const currentRatio = normalizeVideoAspectRatio(draft.aspect_ratio)
  const defaultRatio = defaultVideoAspectRatioForProvider(provider, protocol, profile, mode)
  const aspect_ratio = ratios.length === 0 || ratios.includes(currentRatio)
    ? currentRatio
    : ratios.includes(defaultRatio) ? defaultRatio : ratios[0] || currentRatio
  const duration_seconds = normalizeVideoDurationForRule(
    draft.duration_seconds,
    videoDurationRuleForProvider(provider, protocol, profile, mode),
  )
  return { video_mode: mode, resolution, aspect_ratio, duration_seconds }
}

function videoReferenceLimitForDraft(
  draft: EditableNodeDraft,
  providers: MediaProviderOption[],
  protocols: VideoProtocolSummary[],
): number | undefined {
  const selectedProvider = resolveMediaProvider(draft.model, mediaProvidersForKind(providers, "video"))
  const modelName = selectedProvider?.model_name || draft.model
  const protocol = videoProtocolForProvider(selectedProvider, protocols)
  const profile = videoProfileForModel(protocol, modelName)
  const modeOptions = videoProtocolModeOptions(protocol, profile)
  const mode = effectiveVideoMode(draft.video_mode, modeOptions)
  return videoReferenceImageLimit(videoModeConfig(protocol, mode, profile))
}

function rawNodeInput(input: unknown): Record<string, unknown> {
  return asObj(parseJson(input)) || {}
}

function nodeInputFields(input: unknown): Record<string, unknown> {
  return inputFieldsFromNodeInput(input)
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
      if (typeof direct === "number" || typeof direct === "boolean") return String(direct)
      const ref = obj.ref || obj.reference || obj.value
      if (typeof ref === "string" && ref.trim()) return ref.trim()
      if (typeof ref === "number" || typeof ref === "boolean") return String(ref)
      const nodeId = obj.node_id || obj.nodeId || obj.source_node_id || obj.sourceNodeId
      if (typeof nodeId === "string" && nodeId.trim()) return `node:${nodeId.trim()}`
      if (typeof nodeId === "number" || typeof nodeId === "boolean") return `node:${String(nodeId)}`
      const workflowRef = obj.source_step || obj.from_step || obj.source || obj.candidate
      if (typeof workflowRef === "string" && workflowRef.trim()) return workflowRef.trim()
      if (typeof workflowRef === "number" || typeof workflowRef === "boolean") return String(workflowRef)
      const assetId = obj.asset_id || obj.assetId
      if (typeof assetId === "string" && assetId.trim()) return `asset:${assetId.trim()}`
      if (typeof assetId === "number" || typeof assetId === "boolean") return `asset:${String(assetId)}`
      const id = obj.ref_id || obj.id
      if (typeof id === "string" && id.trim()) return id.trim()
      if (typeof id === "number" || typeof id === "boolean") return String(id)
      return ""
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
    text = stripNodeReferenceMarker(text)
    if (/^\d+$/.test(text)) return text
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(text) ? text : ""
  }
  const obj = asObj(value)
  if (!obj) return ""
  for (const key of ["ref", "reference", "reference_input", "value"]) {
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

function isPersistableReferenceImageValue(value: string): boolean {
  const text = value.trim()
  if (!text) return false
  if (
    text.startsWith("node:")
    || text.startsWith("asset:")
    || text.startsWith("upload:")
    || text.startsWith("/api/")
    || text.startsWith("/")
    || text.startsWith("uploads/")
    || text.startsWith("generated_images/")
  ) {
    return true
  }
  if (/^#?\d+$/.test(text)) return true
  if (isUuidLike(stripNodeReferenceMarker(text))) return true
  return isImageSource(text)
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
  for (const key of ["depends_on", "references", "reference_images"] as const) {
    if (!hasOwnKey(container, key)) continue
    const filtered = filterRemovedNodeReferences(container[key], removedNodeIds)
    if (JSON.stringify(filtered) === JSON.stringify(referenceListFromUnknown(container[key]))) continue
    next[key] = filtered
    changed = true
  }
  return { next, changed }
}

function normalizeVideoAspectRatio(value: string): string {
  return value.trim()
}

function draftFromNode(node: NodeFull): EditableNodeDraft {
  const input = nodeInputFields(node.input)
  const rawOutput = parseJson(node.output)
  const output = asObj(rawOutput) || {}
  const nodePrompt = typeof node.prompt === "string" ? node.prompt : ""
  const editablePromptDraft = pickEditablePromptText(nodePrompt, input, output)
  const inputReferenceImages = stringArrayFromUnknown(input.reference_images)
  const inputReferences = stringArrayFromUnknown(input.references)
  const inputDependsOn = stringArrayFromUnknown(input.depends_on)
  const hasInputReferenceImages = hasOwnKey(input, "reference_images")
  const hasInputReferences = hasOwnKey(input, "references")
  const hasInputDependsOn = hasOwnKey(input, "depends_on")
  const referenceImages = (
    hasInputReferenceImages && inputReferenceImages.length > 0
      ? inputReferenceImages
      : hasInputReferences && inputReferences.length > 0
        ? inputReferences
        : hasInputDependsOn && inputDependsOn.length > 0
          ? inputDependsOn
          : hasInputReferenceImages || hasInputReferences || hasInputDependsOn
            ? []
            : stringArrayFromUnknown(output.reference_images)
  )

  return {
    ...EMPTY_DRAFT,
    title: firstText(node.title, input.title, output.title),
    content: node.type === "text" ? textNodeBodyText(input, rawOutput, nodePrompt) : "",
    prompt: editablePromptDraft,
    model: firstText(input.model, output.model),
    style: firstText(input.style, output.style),
    voice: firstText(input.voice, output.voice),
    speed: firstText(input.speed, output.speed),
    instructions: firstText(input.instructions, output.instructions),
    format: firstText(input.format, output.format),
    negative_tags: firstText(input.negative_tags, input.negativeTags, output.negative_tags, output.negativeTags),
    aspect_ratio: node.type === "video"
      ? firstText(input.aspect_ratio, output.aspect_ratio)
      : firstText(input.aspect_ratio, output.aspect_ratio) || (node.type === "image" ? "9:16" : ""),
    resolution: firstText(input.resolution, input.size, output.resolution, output.size)
      || (node.type === "image" ? defaultImageResolutionForAspect(firstText(input.aspect_ratio, output.aspect_ratio) || "9:16") : ""),
    quality: firstText(input.quality, output.quality) || (node.type === "image" ? "high" : ""),
    clarity: firstText(input.clarity, output.clarity) || (node.type === "image" ? "detailed" : ""),
    duration_seconds: firstText(input.duration_seconds, input.duration, output.duration_seconds, output.duration),
    video_mode: firstText(input.video_mode, input.mode, output.video_mode, output.mode),
    instrumental: firstBool(true, input.instrumental, output.instrumental),
    custom_mode: firstBool(false, input.custom_mode, input.customMode, output.custom_mode, output.customMode),
    reference_images: Array.from(new Set(referenceImages.filter(isPersistableReferenceImageValue))),
    reference_videos: Array.from(new Set(stringArrayFromUnknown(input.reference_videos))),
    reference_audios: Array.from(new Set(stringArrayFromUnknown(input.reference_audios))),
  }
}

function editableDraftEquals(a: EditableNodeDraft, b: EditableNodeDraft): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

function draftWithConcreteMediaProvider(
  node: NodeFull,
  draft: EditableNodeDraft,
  providers: MediaProviderOption[],
  llmProviders: LlmProviderOption[] = [],
  modelDefaults?: RuntimeModelDefaults,
): EditableNodeDraft {
  if (draft.model.trim()) return draft
  if (node.type === "text") {
    const model = defaultLlmProviderName(llmProviders, modelDefaults)
    return model ? { ...draft, model } : draft
  }
  const kind = node.type === "image" || node.type === "video" || node.type === "audio" ? node.type : null
  if (!kind) return draft
  const provider = resolveMediaProvider("", mediaProvidersForKind(providers, kind))
  return provider ? { ...draft, model: provider.name } : draft
}

function concreteDraftStateFromNode(
  node: NodeFull,
  providers: MediaProviderOption[],
  llmProviders: LlmProviderOption[] = [],
  modelDefaults?: RuntimeModelDefaults,
): { draft: EditableNodeDraft; dirty: boolean } {
  const raw = draftFromNode(node)
  const draft = draftWithConcreteMediaProvider(node, raw, providers, llmProviders, modelDefaults)
  return {
    draft,
    dirty: !editableDraftEquals(raw, draft),
  }
}

function payloadFromDraft(
  node: NodeFull,
  draft: EditableNodeDraft,
  audioMode: AudioProviderMode = "unknown",
  referenceMentionCandidates: ReferenceMentionCandidate[] = [],
  referenceImageLimit?: number,
): {
  title: string
  prompt: string | null
  input: Record<string, unknown>
  output?: unknown
} {
  const currentRaw = rawNodeInput(node.input)
  const output = asObj(parseJson(node.output)) || {}
  const nextInput: Record<string, unknown> = { ...currentRaw }
  const title = draft.title.trim() || node.title || "未命名节点"
  const prompt = draft.prompt.trim()

  nextInput.title = title
  const currentFields = asObj(currentRaw.fields)
  const currentFieldsHasReferenceImages = currentFields ? hasOwnKey(currentFields, "reference_images") : false
  const currentHasReferenceFields = ["depends_on", "reference_images", "references"].some((key) =>
    hasOwnKey(currentRaw, key) || Boolean(currentFields && hasOwnKey(currentFields, key)),
  )
  const outputHasReferenceImages = hasOwnKey(output, "reference_images") || stringArrayFromUnknown(output.reference_images).length > 0
  const rawReferenceImages = node.type === "video"
    ? Array.from(new Set([
      ...draft.reference_images,
      ...referenceMentionCandidateRefs(referenceMentionCandidates),
    ].map((item) => item.trim()).filter(Boolean)))
    : Array.from(new Set(draft.reference_images.map((item) => item.trim()).filter(Boolean)))
  const referenceImages = rawReferenceImages
  const effectiveReferenceImages = referenceImageLimit !== undefined
    ? rawReferenceImages.slice(0, Math.max(0, referenceImageLimit))
    : rawReferenceImages
  const hadPersistableReferenceImages = [
    ...stringArrayFromUnknown(currentRaw.reference_images),
    ...(currentFields ? stringArrayFromUnknown(currentFields.reference_images) : []),
  ].some(isPersistableReferenceImageValue)
  const referenceMentions = referenceImageMentionsFromPrompt(prompt, referenceMentionCandidates, effectiveReferenceImages)
  if (hadPersistableReferenceImages || outputHasReferenceImages || referenceImages.length > 0) {
    nextInput.reference_images = referenceImages
  } else {
    delete nextInput.reference_images
  }
  if (referenceMentions.length > 0) nextInput.reference_image_mentions = referenceMentions
  else delete nextInput.reference_image_mentions
  const previousEditableRefs = Array.from(new Set([
    ...stringArrayFromUnknown(currentRaw.reference_images),
    ...stringArrayFromUnknown(currentRaw.references),
    ...stringArrayFromUnknown(currentRaw.depends_on),
    ...(currentFields ? stringArrayFromUnknown(currentFields.reference_images) : []),
    ...(currentFields ? stringArrayFromUnknown(currentFields.references) : []),
    ...(currentFields ? stringArrayFromUnknown(currentFields.depends_on) : []),
  ]))
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
    if (hasOwnKey(currentFields, "reference_image_mentions") || referenceMentions.length > 0) {
      if (referenceMentions.length > 0) nextFields.reference_image_mentions = referenceMentions
      else delete nextFields.reference_image_mentions
    }
    const cleanedFields = removedReferenceNodeIds.size > 0
      ? removeNodeReferencesFromContainer(nextFields, removedReferenceNodeIds)
      : { next: nextFields, changed: false }
    if (currentFieldsHasReferenceImages || hasOwnKey(currentFields, "reference_image_mentions") || referenceMentions.length > 0 || cleanedFields.changed) {
      nextInput.fields = cleanedFields.next
    }
  }

  const textContent = draft.content.trim()
  if (node.type === "text") {
    const model = draft.model.trim()
    if (model) nextInput.model = model
    else delete nextInput.model
    nextInput.content = textContent
    if (prompt) nextInput.prompt = prompt
    else delete nextInput.prompt
  } else {
    nextInput.prompt = prompt
  }

  if (node.type === "image") {
    const model = draft.model.trim()
    if (model) nextInput.model = model
    else delete nextInput.model
    nextInput.aspect_ratio = draft.aspect_ratio.trim()
    nextInput.resolution = draft.resolution.trim()
    nextInput.quality = draft.quality.trim()
    const clarity = draft.clarity.trim()
    if (clarity) nextInput.clarity = clarity
    else delete nextInput.clarity
  }

  if (node.type === "video") {
    nextInput.aspect_ratio = draft.aspect_ratio.trim()
    const videoMode = draft.video_mode.trim()
    if (videoMode) nextInput.video_mode = videoMode
    else {
      delete nextInput.video_mode
      delete nextInput.mode
    }
    const model = draft.model.trim()
    if (model) nextInput.model = model
    else delete nextInput.model
    const resolution = draft.resolution.trim()
    if (resolution) nextInput.resolution = resolution
    else delete nextInput.resolution
    const duration = draft.duration_seconds.trim()
    if (duration) nextInput.duration_seconds = Number.isFinite(Number(duration)) ? Number(duration) : duration
    else {
      delete nextInput.duration_seconds
      delete nextInput.duration
    }
    const referenceVideos = Array.from(new Set(draft.reference_videos.map((item) => item.trim()).filter(Boolean)))
    if (referenceVideos.length > 0) nextInput.reference_videos = referenceVideos
    else delete nextInput.reference_videos
    const referenceAudios = Array.from(new Set(draft.reference_audios.map((item) => item.trim()).filter(Boolean)))
    if (referenceAudios.length > 0) nextInput.reference_audios = referenceAudios
    else delete nextInput.reference_audios
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

  return {
    title,
    prompt: prompt || null,
    input: nextInput,
    ...(node.type === "text" ? { output: textContent || null } : {}),
  }
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
  const text = node.type === "text" ? textNodeBodyText(input, parseJson(node.output), node.prompt || "") : ""
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
  const input = nodeInputFields(node.input)
  const previewText = nodeReadableText({
    type: node.type,
    input,
    output: node.output,
    prompt: node.prompt || "",
  })
  const patch: Record<string, unknown> = {
    title: node.title,
    type: node.type,
    status: node.status,
    prompt: node.prompt ?? undefined,
    input: node.input ?? undefined,
    output: node.output ?? undefined,
    workflowRuntimeOutput: node.output ?? undefined,
    previewText: previewText || undefined,
    renderState: renderStateFromNode(node),
    error_message: nodeDisplayError(node) || undefined,
  }
  const preview = previewPatchFromNode(node)
  if (preview) patch.preview = preview
  return patch
}

const inputClass =
  "w-full rounded-md border border-white/[0.1] bg-[#080c13] px-2.5 py-2 text-sm text-zinc-100 outline-none transition [color-scheme:dark] placeholder:text-zinc-500 focus:border-cyan-300/45 focus:bg-[#0b111a]"

function DraftField({
  label,
  children,
  className = "",
  action,
}: {
  label: string
  children: React.ReactNode
  className?: string
  action?: React.ReactNode
}) {
  return (
    <div className={`block ${className}`}>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="block text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</span>
        {action}
      </div>
      {children}
    </div>
  )
}

function AutoSaveBadge({ saving, dirty }: { saving: boolean; dirty: boolean }) {
  return (
    <span className={`rounded-md border px-2 py-1 text-[10px] font-medium ${
      saving
        ? "border-cyan-300/20 bg-cyan-300/10 text-cyan-100"
        : dirty
          ? "border-amber-300/20 bg-amber-300/10 text-amber-100"
          : "border-emerald-300/15 bg-emerald-300/8 text-emerald-100"
    }`}>
      {saving ? "保存中" : dirty ? "待自动保存" : "已保存"}
    </span>
  )
}

function ChipControl({
  label,
  value,
  options,
  placeholder,
  onChange,
  allowCustom = true,
}: {
  label: string
  value: string
  options: Array<string | SelectOption>
  placeholder?: string
  onChange: (value: string) => void
  allowCustom?: boolean
}) {
  const normalizedOptions = options.map((option) => (
    typeof option === "string" ? { label: option, value: option } : option
  ))
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</div>
      <div className="flex flex-wrap items-center gap-1.5">
        {normalizedOptions.map((option) => {
          const active = value === option.value
          return (
            <button
              key={`${option.value}:${option.label}`}
              type="button"
              onClick={() => onChange(option.value)}
              disabled={option.disabled}
              className={`rounded-md border px-2.5 py-1.5 text-xs transition disabled:cursor-not-allowed disabled:opacity-45 ${
                active
                  ? "border-cyan-300/35 bg-cyan-300/12 text-cyan-100"
                  : "border-transparent bg-white/[0.06] text-zinc-300 hover:bg-white/[0.1] hover:text-zinc-50"
              }`}
            >
              {option.label}
            </button>
          )
        })}
        {allowCustom && (
          <input
            value={normalizedOptions.some((option) => option.value === value) ? "" : value}
            onChange={(event) => onChange(event.target.value)}
            className="h-8 min-w-0 flex-1 rounded-md border border-white/[0.1] bg-[#080c13] px-2 text-xs text-zinc-100 outline-none [color-scheme:dark] placeholder:text-zinc-500 focus:border-cyan-300/45"
            placeholder={placeholder || "自定义"}
          />
        )}
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
  options: SelectOption[]
  onChange: (value: string) => void
  hint?: string
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</div>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 w-full rounded-md border border-white/[0.1] bg-[#080c13] px-2 text-xs text-zinc-100 shadow-inner shadow-black/20 outline-none [color-scheme:dark] focus:border-cyan-300/45 [&>option]:bg-[#080c13] [&>option]:text-zinc-100"
      >
        {options.map((option) => (
          <option
            key={`${option.value}:${option.label}`}
            value={option.value}
            disabled={option.disabled}
            className="bg-[#080c13] text-zinc-100"
          >
            {option.label}
          </option>
        ))}
      </select>
      {hint && <div className="text-[10px] text-zinc-600">{hint}</div>}
    </div>
  )
}

function SegmentedControl({
  label,
  value,
  options,
  onChange,
  hint,
}: {
  label: string
  value: string
  options: SelectOption[]
  onChange: (value: string) => void
  hint?: string
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-500">{label}</div>
      <div className="grid grid-cols-2 gap-1.5 rounded-lg border border-white/[0.08] bg-black/20 p-1 sm:grid-cols-3">
        {options.map((option) => {
          const active = value === option.value
          return (
            <button
              key={`${option.value}:${option.label}`}
              type="button"
              onClick={() => onChange(option.value)}
              disabled={option.disabled}
              title={option.hint || option.label}
              className={`min-h-8 rounded-md px-2 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-45 ${
                active
                  ? "bg-cyan-300 text-slate-950 shadow-[0_8px_18px_rgba(34,211,238,0.16)]"
                  : "bg-transparent text-zinc-300 hover:bg-white/[0.08] hover:text-zinc-50"
              }`}
            >
              {option.label}
            </button>
          )
        })}
      </div>
      {hint && <div className="text-[10px] leading-4 text-zinc-600">{hint}</div>}
    </div>
  )
}

function MediaOptionGrid({
  label,
  value,
  options,
  onChange,
  columns = "grid-cols-3",
  hint,
  compact = false,
  aspectGlyph = false,
}: {
  label: string
  value: string
  options: SelectOption[]
  onChange: (value: string) => void
  columns?: string
  hint?: string
  compact?: boolean
  aspectGlyph?: boolean
}) {
  const visibleOptions = options.length > 0 ? options : [{ label: "未配置", value: "", disabled: true }]
  return (
    <div className={compact ? "space-y-1.5" : "rounded-lg border border-white/[0.08] bg-black/20 p-2"}>
      <div className={compact ? "text-[11px] font-medium text-zinc-400" : "mb-1.5 text-[10px] font-medium text-zinc-500"}>{label}</div>
      <div className={`grid ${columns} ${compact ? "gap-2" : "gap-1.5"}`}>
        {visibleOptions.map((option) => {
          const active = value === option.value
          return (
            <button
              key={`${option.value}:${option.label}`}
              type="button"
              onClick={() => onChange(option.value)}
              disabled={option.disabled}
              title={option.hint || option.label}
              className={`min-h-8 ${compact ? "rounded-lg" : "rounded-md"} border px-2 py-1 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${
                compact
                  ? aspectGlyph
                    ? "flex h-[62px] flex-col items-center justify-center gap-1 px-1.5 py-1.5 text-[10px]"
                    : "h-8 px-1.5"
                  : ""
              } ${
                active
                  ? compact
                    ? "border-zinc-100 bg-white/[0.08] text-zinc-50"
                    : "border-zinc-100 bg-zinc-100 text-zinc-950 shadow-[0_8px_18px_rgba(255,255,255,0.10)]"
                  : compact
                    ? "border-white/[0.16] bg-transparent text-zinc-400 hover:border-white/[0.28] hover:bg-white/[0.06] hover:text-zinc-100"
                    : "border-white/[0.08] bg-white/[0.035] text-zinc-300 hover:border-white/[0.16] hover:bg-white/[0.07] hover:text-zinc-50"
              }`}
            >
              {aspectGlyph && <AspectRatioGlyph value={option.value} />}
              {option.label}
            </button>
          )
        })}
      </div>
      {hint && <div className="mt-1.5 text-[10px] leading-4 text-zinc-600">{hint}</div>}
    </div>
  )
}

function AspectRatioGlyph({ value }: { value: string }) {
  const aspect = value === "auto" ? null : parseAspectRatio(value)
  const ratio = aspect ? aspect.width / aspect.height : 1
  const width = ratio >= 1 ? 14 : Math.max(6, Math.round(14 * ratio))
  const height = ratio >= 1 ? Math.max(6, Math.round(14 / ratio)) : 14
  return (
    <span
      aria-hidden="true"
      className="flex h-3.5 w-4 items-center justify-center"
    >
      <span
        className="block rounded-[2px] border border-current"
        style={{ width, height }}
      />
    </span>
  )
}

function ImageResolutionControl({
  aspectRatio,
  resolution,
  onChange,
  compact = false,
}: {
  aspectRatio: string
  resolution: string
  onChange: (value: string) => void
  compact?: boolean
}) {
  const normalizedAspect = normalizeImageAspectRatio(aspectRatio)
  const presets = imageResolutionPresetsForAspect(normalizedAspect)
  const defaultResolution = defaultImageResolutionForAspect(normalizedAspect, imageResolutionTier(resolution))
  const parsed = parseImageResolution(resolution) || parseImageResolution(defaultResolution) || { width: "", height: "" }
  const exactResolution = parsed.width && parsed.height ? `${parsed.width}x${parsed.height}` : defaultResolution

  useEffect(() => {
    if (!resolution) onChange(defaultResolution)
  }, [defaultResolution, onChange, resolution])

  return (
    <div className="space-y-2">
      <MediaOptionGrid
        label="清晰度"
        value={presets.find((item) => item.value === resolution)?.value || defaultResolution}
        options={presets.map((item) => ({ label: item.label.split(" · ")[0], value: item.value, hint: item.label }))}
        onChange={onChange}
        compact={compact}
      />
      {!compact && <div className="text-[10px] text-zinc-600">保存值：{exactResolution}</div>}
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

function normalizeReferenceForCanvas(
  value: unknown,
  label: string,
  canvasLookup?: Map<string, CanvasGraphNode>,
): ReferenceItem | null {
  const sourceNode = canvasLookup ? resolveCanvasReferenceNode(value, canvasLookup) : undefined
  if (sourceNode) {
    return {
      kind: "node",
      value: stripNodeReferenceMarker(canvasNodeReferenceValue(sourceNode)),
      label: canvasNodeTitle(sourceNode) || label,
    }
  }
  if (typeof value === "string") return normalizeReferenceValue(value, label)
  return normalizeReference(value)
}

function ReferenceEditor({
  refs,
  implicitRefs = [],
  quota,
  maxRefs,
  projectId,
  canvasNodes = [],
  uploading,
  compact = false,
  setLightbox,
  onChange,
  onUpload,
}: {
  refs: string[]
  implicitRefs?: string[]
  quota?: { label?: string; title?: string; invalid?: boolean; overLimit?: boolean; uploadBlocked?: boolean; remaining?: number }
  maxRefs?: number
  projectId?: string | null
  canvasNodes?: CanvasGraphNode[]
  uploading: boolean
  compact?: boolean
  setLightbox: (v: { src: string; alt?: string } | null) => void
  onChange: (refs: string[]) => void
  onUpload: (files: FileList | File[] | null) => void | Promise<void>
}) {
  const [blockedPulse, setBlockedPulse] = useState(false)
  const canvasLookup = useMemo(() => buildCanvasNodeLookup(canvasNodes), [canvasNodes])
  const normalized = refs
    .map((value) => normalizeReferenceForCanvas(value, "引用图", canvasLookup))
    .filter((ref): ref is ReferenceItem => Boolean(ref))
  const implicitNormalized = implicitRefs
    .map((value) => normalizeReferenceForCanvas(value, "引用图", canvasLookup))
    .filter((ref): ref is ReferenceItem => Boolean(ref))
  const displayRefs = uniqueReferenceItems([...normalized, ...implicitNormalized], projectId)
  const visibleRefs = maxRefs !== undefined ? displayRefs.slice(0, Math.max(0, maxRefs)) : displayRefs
  const explicitIdentities = new Set(normalized.map((ref) => referenceIdentity(ref, projectId)).filter(Boolean))
  const uploadBlocked = Boolean(quota?.uploadBlocked)
  const uploadRemaining = quota?.remaining
  const flashBlocked = () => {
    setBlockedPulse(true)
    window.setTimeout(() => setBlockedPulse(false), 420)
  }
  const removeRef = (identity: string) => {
    onChange(refs.filter((value) => {
      const ref = normalizeReferenceForCanvas(value, "引用图", canvasLookup)
      return !ref || referenceIdentity(ref, projectId) !== identity
    }))
  }
  const tileClass = compact
    ? "flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-white/[0.1] bg-white/[0.045] text-zinc-300 shadow-[0_6px_16px_rgba(0,0,0,0.16)] transition hover:-translate-y-0.5 hover:border-white/[0.22] hover:bg-white/[0.08] hover:text-zinc-50"
    : "flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-white/[0.1] bg-white/[0.045] text-zinc-300 shadow-[0_8px_18px_rgba(0,0,0,0.16)] transition hover:-translate-y-0.5 hover:border-white/[0.22] hover:bg-white/[0.08] hover:text-zinc-50"
  const railClass = compact
    ? "flex min-h-[36px] items-center gap-1 overflow-x-auto px-0.5 py-0.5"
    : "flex min-h-[56px] items-center gap-1 overflow-x-auto rounded-lg border border-white/[0.08] bg-black/20 p-1"

  return (
    <div className={compact ? "" : "space-y-2.5"}>
      <div
        className={railClass}
        aria-label="参考图"
      >
        {visibleRefs.map((ref) => {
          const identity = referenceIdentity(ref, projectId)
          const canRemove = explicitIdentities.has(identity)
          return (
            <div key={identity} className="group relative shrink-0">
              <RefThumbnail
                ref={ref}
                projectId={projectId}
                setLightbox={setLightbox}
                canvasLookup={canvasLookup}
                compact={compact}
              />
              {canRemove && (
                <button
                  type="button"
                  aria-label="移除参考图"
                  title="移除参考图"
                  onClick={() => removeRef(identity)}
                  className="absolute -right-1 -top-1 flex h-[18px] w-[18px] items-center justify-center rounded-full border border-white/[0.16] bg-black/82 text-zinc-200 opacity-0 shadow-lg transition hover:bg-red-500 hover:text-white group-hover:opacity-100"
                >
                  <XIcon className="h-3 w-3" />
                </button>
              )}
            </div>
          )
        })}
        <label
          className={`${tileClass} ${uploadBlocked ? "cursor-not-allowed opacity-70 hover:translate-y-0" : "cursor-pointer"} ${
            blockedPulse ? "!border-red-400/70 !bg-red-500/12 !text-red-100" : ""
          }`}
          title={uploadBlocked ? "参考图已到上限" : uploading ? "上传中" : "上传参考图"}
          aria-label={uploadBlocked ? "参考图已到上限" : uploading ? "上传中" : "上传参考图"}
          onClick={(event) => {
            if (!uploadBlocked) return
            event.preventDefault()
            flashBlocked()
          }}
        >
          {uploading ? (
            <span className="h-4 w-4 rounded-full border-2 border-zinc-500 border-t-zinc-100 animate-spin" />
          ) : (
            <ImageIcon />
          )}
          <input
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            disabled={uploadBlocked}
            onChange={(event) => {
              const files = event.currentTarget.files
              const cappedFiles = uploadRemaining !== undefined
                ? Array.from(files || []).slice(0, Math.max(0, uploadRemaining))
                : files
              const cappedLength = cappedFiles ? cappedFiles.length : 0
              if (uploadRemaining !== undefined && files && files.length > cappedLength) flashBlocked()
              void onUpload(cappedFiles)
              event.currentTarget.value = ""
            }}
          />
        </label>
        {quota?.label && (
          <span
            title={quota.title}
            className={`flex h-8 shrink-0 items-center px-1 text-[10px] font-semibold ${
              quota.invalid || quota.overLimit ? "text-red-300" : "text-zinc-500"
            }`}
          >
            {quota.label}
          </span>
        )}
      </div>
    </div>
  )
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
}

function escapeAttribute(value: string): string {
  return escapeHtml(value).replace(/`/g, "&#96;")
}

function textSegmentHtml(value: string): string {
  return escapeHtml(value).replace(/\n/g, "<br>")
}

function mentionEditorHtml(value: string, candidates: ReferenceMentionCandidate[]): string {
  if (!value) return ""
  const mentions = Array.from(new Set(candidates.map((item) => item.mention).filter(Boolean)))
    .sort((a, b) => b.length - a.length)
  if (mentions.length === 0) return textSegmentHtml(value)
  let html = ""
  let index = 0
  while (index < value.length) {
    let matched = ""
    for (const mention of mentions) {
      if (value.startsWith(mention, index)) {
        matched = mention
        break
      }
    }
    if (matched) {
      html += `<span contenteditable="false" data-reference-mention="${escapeAttribute(matched)}" class="openreel-reference-mention-chip">${escapeHtml(matched)}</span>`
      index += matched.length
      continue
    }
    const nextMentionIndex = mentions.reduce((next, mention) => {
      const found = value.indexOf(mention, index + 1)
      return found >= 0 ? Math.min(next, found) : next
    }, value.length)
    html += textSegmentHtml(value.slice(index, nextMentionIndex))
    index = nextMentionIndex
  }
  return html
}

function mentionEditorPlainText(element: HTMLElement): string {
  let text = ""
  const walk = (node: ChildNode) => {
    if (node instanceof HTMLElement && node.dataset.referenceMention) {
      text += node.dataset.referenceMention
      return
    }
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.textContent || ""
      return
    }
    if (node.nodeName === "BR") {
      text += "\n"
      return
    }
    node.childNodes.forEach(walk)
    if (node instanceof HTMLDivElement || node instanceof HTMLParagraphElement) {
      text += "\n"
    }
  }
  element.childNodes.forEach(walk)
  return text.replace(/\n+$/g, "")
}

function caretTextOffset(root: HTMLElement): number {
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return mentionEditorPlainText(root).length
  const range = selection.getRangeAt(0)
  if (!root.contains(range.startContainer)) return mentionEditorPlainText(root).length
  const before = range.cloneRange()
  before.selectNodeContents(root)
  before.setEnd(range.startContainer, range.startOffset)
  const holder = document.createElement("div")
  holder.appendChild(before.cloneContents())
  return mentionEditorPlainText(holder)
    .replace(/\u00a0/g, " ")
    .length
}

function setCaretTextOffset(root: HTMLElement, offset: number) {
  const selection = window.getSelection()
  if (!selection) return
  const range = document.createRange()
  let remaining = Math.max(0, offset)
  let placed = false

  const walk = (node: ChildNode): boolean => {
    if (node instanceof HTMLElement && node.dataset.referenceMention) {
      const length = (node.dataset.referenceMention || "").length
      if (remaining <= length) {
        range.setStartAfter(node)
        placed = true
        return true
      }
      remaining -= length
      return false
    }
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent || ""
      if (remaining <= text.length) {
        range.setStart(node, remaining)
        placed = true
        return true
      }
      remaining -= text.length
      return false
    }
    if (node.nodeName === "BR") {
      if (remaining <= 1) {
        range.setStartAfter(node)
        placed = true
        return true
      }
      remaining -= 1
      return false
    }
    for (const child of Array.from(node.childNodes)) {
      if (walk(child)) return true
    }
    return false
  }

  for (const child of Array.from(root.childNodes)) {
    if (walk(child)) break
  }
  if (!placed) {
    range.selectNodeContents(root)
    range.collapse(false)
  }
  selection.removeAllRanges()
  selection.addRange(range)
}

function mentionQueryAtCaret(root: HTMLElement): { start: number; end: number; query: string } | null {
  const offset = caretTextOffset(root)
  const text = mentionEditorPlainText(root).slice(0, offset)
  const at = text.lastIndexOf("@")
  if (at < 0) return null
  const query = text.slice(at + 1)
  if (/[\s\n\r@]/.test(query)) return null
  return { start: at, end: offset, query }
}

function filteredMentionCandidates(candidates: ReferenceMentionCandidate[], query: string): ReferenceMentionCandidate[] {
  const normalized = query.trim().toLowerCase()
  if (!normalized) return candidates.slice(0, 8)
  return candidates
    .filter((item) =>
      item.mention.toLowerCase().includes(normalized)
      || item.label.toLowerCase().includes(normalized)
      || item.ref.toLowerCase().includes(normalized)
    )
    .slice(0, 8)
}

function MentionCandidateThumbnail({ src }: { src?: string }) {
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    setFailed(false)
  }, [src])

  if (src && !failed) {
    return (
      <img
        src={src}
        alt=""
        className="h-full w-full object-cover"
        onError={() => setFailed(true)}
      />
    )
  }
  return <ImageIcon className="h-3.5 w-3.5" />
}

function PromptMentionEditor({
  value,
  candidates,
  rows = 4,
  maxRows,
  placeholder,
  className = "",
  onChange,
}: {
  value: string
  candidates: ReferenceMentionCandidate[]
  rows?: number
  maxRows?: number
  placeholder?: string
  className?: string
  onChange: (value: string, selected?: ReferenceMentionCandidate) => void
}) {
  const editorRef = useRef<HTMLDivElement | null>(null)
  const focusedRef = useRef(false)
  const [query, setQuery] = useState<{ start: number; end: number; query: string } | null>(null)
  const candidateKey = candidates.map((item) => `${item.mention}:${item.ref}`).join("|")
  const visibleCandidates = useMemo(
    () => query ? filteredMentionCandidates(candidates, query.query) : [],
    [candidates, query],
  )

  const syncHtml = useCallback((nextValue: string, caretOffset?: number) => {
    const editor = editorRef.current
    if (!editor) return
    editor.innerHTML = mentionEditorHtml(nextValue, candidates)
    if (caretOffset !== undefined) {
      editor.focus()
      setCaretTextOffset(editor, caretOffset)
    }
  }, [candidates])

  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    const current = mentionEditorPlainText(editor)
    if (!focusedRef.current || current !== value) {
      editor.innerHTML = mentionEditorHtml(value, candidates)
    }
  }, [value, candidates, candidateKey])

  const emitInput = () => {
    const editor = editorRef.current
    if (!editor) return
    const text = mentionEditorPlainText(editor)
    onChange(text)
    setQuery(mentionQueryAtCaret(editor))
  }

  const insertMention = (candidate: ReferenceMentionCandidate) => {
    const editor = editorRef.current
    if (!editor) return
    const current = mentionEditorPlainText(editor)
    const currentQuery = query || mentionQueryAtCaret(editor)
    const start = currentQuery?.start ?? current.length
    const end = currentQuery?.end ?? current.length
    const suffix = current.slice(end)
    const needsSpace = suffix.length > 0 && !/^\s/.test(suffix)
    const nextValue = `${current.slice(0, start)}${candidate.mention}${needsSpace ? " " : ""}${suffix}`
    const nextCaret = start + candidate.mention.length + (needsSpace ? 1 : 0)
    onChange(nextValue, candidate)
    syncHtml(nextValue, nextCaret)
    setQuery(null)
  }

  return (
    <div className="relative">
      <div
        ref={editorRef}
        contentEditable
        role="textbox"
        aria-multiline="true"
        data-placeholder={placeholder || ""}
        suppressContentEditableWarning
        spellCheck={false}
        onFocus={() => {
          focusedRef.current = true
          const editor = editorRef.current
          if (editor) setQuery(mentionQueryAtCaret(editor))
        }}
        onBlur={() => {
          focusedRef.current = false
          window.setTimeout(() => {
            if (!focusedRef.current) setQuery(null)
          }, 120)
        }}
        onInput={emitInput}
        onKeyUp={() => {
          const editor = editorRef.current
          if (editor) setQuery(mentionQueryAtCaret(editor))
        }}
        onMouseUp={() => {
          const editor = editorRef.current
          if (editor) setQuery(mentionQueryAtCaret(editor))
        }}
        onKeyDown={(event) => {
          if (!query || visibleCandidates.length === 0) return
          if (event.key === "Enter" || event.key === "Tab") {
            event.preventDefault()
            insertMention(visibleCandidates[0])
          } else if (event.key === "Escape") {
            event.preventDefault()
            setQuery(null)
          }
        }}
        className={`openreel-mention-editor w-full overflow-y-auto whitespace-pre-wrap break-words border-0 bg-transparent px-3 py-2 text-[13px] leading-5 text-zinc-100 outline-none [color-scheme:dark] ${className}`}
        style={{
          minHeight: `${Math.max(2, rows) * 26}px`,
          maxHeight: maxRows ? `${Math.max(rows, maxRows) * 26}px` : undefined,
        }}
      />
      {query && visibleCandidates.length > 0 && (
        <div className="absolute bottom-full left-2 z-[120] mb-1 w-[min(320px,calc(100vw-48px))] overflow-hidden rounded-lg border border-white/[0.12] bg-[#111111]/98 p-1 shadow-[0_18px_44px_rgba(0,0,0,0.48)] backdrop-blur-xl">
          {visibleCandidates.map((candidate) => (
            <button
              key={`${candidate.mention}:${candidate.ref}`}
              type="button"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => insertMention(candidate)}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition hover:bg-white/[0.08]"
            >
              <span className="flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden rounded-md border border-white/[0.1] bg-white/[0.05] text-zinc-400">
                <MentionCandidateThumbnail src={candidate.previewUrl} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[12px] font-medium text-zinc-100">{candidate.mention}</span>
                <span className="block truncate text-[10px] text-zinc-500">{candidate.source === "node" ? "画布图片节点" : "参考图"}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function NodeEditView({
  node,
  draft,
  mediaProviders,
  llmProviders,
  modelDefaults,
  videoProtocols,
  mediaConfigError,
  projectId,
  canvasNodes,
  referenceMentionCandidates,
  saving,
  dirty,
  uploading,
  setLightbox,
  onChange,
  onUploadRefs,
  onSave,
}: {
  node: NodeFull
  draft: EditableNodeDraft
  mediaProviders: MediaProviderOption[]
  llmProviders: LlmProviderOption[]
  modelDefaults?: RuntimeModelDefaults
  videoProtocols: VideoProtocolSummary[]
  mediaConfigError?: string | null
  projectId?: string | null
  canvasNodes: CanvasGraphNode[]
  referenceMentionCandidates: ReferenceMentionCandidate[]
  saving: boolean
  dirty: boolean
  uploading: boolean
  setLightbox: (v: { src: string; alt?: string } | null) => void
  onChange: (patch: Partial<EditableNodeDraft>) => void
  onUploadRefs: (files: FileList | File[] | null) => void | Promise<void>
  onSave: () => void | Promise<void>
}) {
  const isText = node.type === "text"
  const isImage = node.type === "image"
  const isVideo = node.type === "video"
  const isAudio = node.type === "audio"
  const hasMediaControls = isImage || isVideo || isAudio
  const mainLabel = isText ? "回复正文" : isImage ? "图片提示词" : isAudio ? "音频提示词" : "视频提示词"
  const mainText = isText ? draft.content : draft.prompt
  const mainCopyLabel = isText ? "回复正文" : "提示词"
  const enabledImageProviders = mediaProvidersForKind(mediaProviders, "image")
  const enabledVideoProviders = mediaProvidersForKind(mediaProviders, "video")
  const enabledAudioProviders = mediaProvidersForKind(mediaProviders, "audio")
  const enabledTextProviders = enabledLlmProviders(llmProviders)
  const selectedTextProvider = isText ? resolveLlmProvider(draft.model, enabledTextProviders) : undefined
  const selectedImageProvider = isImage ? resolveMediaProvider(draft.model, enabledImageProviders) : undefined
  const selectedVideoProvider = isVideo ? resolveMediaProvider(draft.model, enabledVideoProviders) : undefined
  const selectedAudioProvider = isAudio ? resolveAudioProvider(draft.model, enabledAudioProviders) : undefined
  const selectedAudioMode = audioProviderModeFromProvider(selectedAudioProvider)
  const imageProviderSelectValue = mediaProviderSelectValue(draft.model, selectedImageProvider)
  const videoProviderSelectValue = mediaProviderSelectValue(draft.model, selectedVideoProvider)
  const audioProviderSelectValue = mediaProviderSelectValue(draft.model, selectedAudioProvider)
  const imageProviderOptions = mediaProviderSelectOptions(enabledImageProviders, draft.model, selectedImageProvider)
  const videoProviderOptions = mediaProviderSelectOptions(enabledVideoProviders, draft.model, selectedVideoProvider)
  const audioProviderOptions = mediaProviderSelectOptions(enabledAudioProviders, draft.model, selectedAudioProvider)
  const textProviderOptions = llmProviderSelectOptions(enabledTextProviders, draft.model, selectedTextProvider)
  const hasConfiguredImageProviders = enabledImageProviders.length > 0
  const hasConfiguredVideoProviders = enabledVideoProviders.length > 0
  const hasConfiguredAudioProviders = enabledAudioProviders.length > 0
  const selectedVideoModelName = selectedVideoProvider?.model_name || draft.model
  const selectedVideoProtocol = videoProtocolForProvider(selectedVideoProvider, videoProtocols)
  const selectedVideoProfile = videoProfileForModel(selectedVideoProtocol, selectedVideoModelName)
  const videoModeOptions = videoProtocolModeOptions(selectedVideoProtocol, selectedVideoProfile)
  const activeVideoMode = effectiveVideoMode(draft.video_mode, videoModeOptions)
  const providerVideoResolutions = mediaProviderParamStringArray(selectedVideoProvider, "supported_resolutions", "resolutions")
    .map((item) => item.toLowerCase())
  const providerVideoRatios = mediaProviderParamStringArray(selectedVideoProvider, "supported_ratios", "ratios", "supported_aspect_ratios")
  const supportedVideoResolutions = providerVideoResolutions.length
    ? providerVideoResolutions
    : videoSupportedResolutionsForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const supportedVideoRatios = providerVideoRatios.length
    ? providerVideoRatios.filter((item) => item !== "adaptive")
    : videoSupportedRatiosForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const defaultVideoAspectRatio = defaultVideoAspectRatioForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const activeVideoAspectRatio = draft.aspect_ratio.trim() || defaultVideoAspectRatio || supportedVideoRatios[0] || ""
  const videoAspectOptions = videoAspectSelectOptions(activeVideoAspectRatio, supportedVideoRatios)
  const videoResolutionOptions = videoResolutionSelectOptions(draft.resolution, supportedVideoResolutions)
  const providerDefaultVideoResolution = mediaProviderParamText(selectedVideoProvider, "default_resolution", "resolution").toLowerCase()
  const activeVideoResolution = draft.resolution || (providerDefaultVideoResolution && supportedVideoResolutions.includes(providerDefaultVideoResolution)
    ? providerDefaultVideoResolution
    : defaultVideoResolutionForProvider(
    selectedVideoProvider,
    selectedVideoProtocol,
    selectedVideoProfile,
    activeVideoMode,
  ))
  const activeVideoModeConfig = videoModeConfig(selectedVideoProtocol, activeVideoMode, selectedVideoProfile)
  const activeVideoDurationRule = videoDurationRuleForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const activeVideoDurationBounds = videoDurationBounds(activeVideoDurationRule)
  const videoDurationConfigured = activeVideoDurationBounds.min !== undefined
    || activeVideoDurationBounds.max !== undefined
    || activeVideoDurationBounds.allowed.length > 0
  const videoDurationMin = activeVideoDurationBounds.min ?? activeVideoDurationBounds.allowed[0] ?? 0
  const videoDurationMax = activeVideoDurationBounds.max
    ?? activeVideoDurationBounds.allowed[activeVideoDurationBounds.allowed.length - 1]
    ?? activeVideoDurationBounds.min
    ?? 0
  const imageAspectRatio = normalizeImageAspectRatio(draft.aspect_ratio)
  const updateImageAspectRatio = (aspect_ratio: string) => {
    const nextAspectRatio = normalizeImageAspectRatio(aspect_ratio)
    const resolution = defaultImageResolutionForAspect(nextAspectRatio, imageResolutionTier(draft.resolution))
    onChange({ aspect_ratio: nextAspectRatio, resolution })
  }
  const updateVideoModel = (model: string) => {
    const selectedProvider = resolveMediaProvider(model, enabledVideoProviders)
    const modelForResolution = selectedProvider?.model_name || model
    const protocol = videoProtocolForProvider(selectedProvider, videoProtocols)
    const profile = videoProfileForModel(protocol, modelForResolution)
    const modeOptions = videoProtocolModeOptions(protocol, profile)
    const video_mode = effectiveVideoMode(draft.video_mode, modeOptions)
    onChange({
      model,
      ...normalizeVideoDraftForMode(draft, selectedProvider, protocol, profile, video_mode),
    })
  }
  const updateVideoMode = (videoMode: string) => {
    const mode = effectiveVideoMode(videoMode, videoModeOptions)
    onChange(normalizeVideoDraftForMode(draft, selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, mode))
  }
  const updateAudioProvider = (providerName: string) => {
    onChange({ model: providerName })
  }
  const implicitReferenceImages = referenceMentionCandidateRefs(referenceMentionCandidates)
  const visibleReferenceImageCount = referenceDisplayCount(draft.reference_images, implicitReferenceImages, projectId)
  const videoReferenceRule = videoReferenceRuleHint(activeVideoModeConfig, visibleReferenceImageCount)
  const updatePrompt = (prompt: string, selected?: ReferenceMentionCandidate) => {
    if (selected && !draft.reference_images.includes(selected.ref)) {
      onChange({ prompt, reference_images: [...draft.reference_images, selected.ref] })
      return
    }
    onChange({ prompt })
  }

  if (isText) {
    return (
      <div className="grid gap-3">
        <div className="min-w-0 rounded-lg border border-white/[0.08] bg-[#121722] p-3 shadow-[0_18px_45px_rgba(0,0,0,0.22)]">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-cyan-200/70">文本对话</div>
              <div className="mt-0.5 text-xs text-zinc-500">写提示词、绑定参考图，运行后在预览中查看正文</div>
            </div>
            <AutoSaveBadge saving={saving} dirty={dirty} />
          </div>
          <div className="grid gap-3">
            <DraftField label="标题">
              <input
                value={draft.title}
                onChange={(event) => onChange({ title: event.target.value })}
                className={inputClass}
              />
            </DraftField>
            <SelectControl
              label="对话模型"
              value={selectedTextProvider?.name || draft.model || defaultLlmProviderName(enabledTextProviders, modelDefaults)}
              options={textProviderOptions}
              onChange={(model) => onChange({ model })}
              hint={llmProviderHint(selectedTextProvider, mediaConfigError)}
            />
            <DraftField
              label="提示词"
              action={<CopyTextButton text={draft.prompt} label="提示词" />}
            >
              <div className="overflow-visible rounded-md border border-white/[0.1] bg-[#080c13] transition focus-within:border-cyan-300/45 focus-within:bg-[#0b111a]">
                <div className="border-b border-white/[0.07] p-1.5">
                  <ReferenceEditor
                    refs={draft.reference_images}
                    implicitRefs={implicitReferenceImages}
                    projectId={projectId}
                    canvasNodes={canvasNodes}
                    uploading={uploading}
                    compact
                    setLightbox={setLightbox}
                    onChange={(reference_images) => onChange({ reference_images })}
                    onUpload={onUploadRefs}
                  />
                </div>
                <PromptMentionEditor
                  value={draft.prompt}
                  onChange={updatePrompt}
                  candidates={referenceMentionCandidates}
                  rows={6}
                  className="min-h-[150px] px-2.5 py-2 leading-6"
                  placeholder="输入这次要让模型回答、续写、改写或整理的内容；参考图会一起发送给模型"
                />
              </div>
            </DraftField>
          </div>
        </div>
      </div>
    )
  }

  if (isImage) {
    return (
      <div className="grid gap-3">
        <div className="min-w-0 rounded-lg border border-white/[0.08] bg-[#121722] p-3 shadow-[0_18px_45px_rgba(0,0,0,0.22)]">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-cyan-200/70">图片生成</div>
              <div className="mt-0.5 text-xs text-zinc-500">写提示词、选择参考图、设定输出规格</div>
            </div>
            <AutoSaveBadge saving={saving} dirty={dirty} />
          </div>

          <div className="grid gap-3">
            <DraftField label="标题">
              <input
                value={draft.title}
                onChange={(event) => onChange({ title: event.target.value })}
                className={inputClass}
              />
            </DraftField>
            <DraftField
              label="图片提示词"
              action={<CopyTextButton text={draft.prompt} label="提示词" />}
            >
              <div className="overflow-visible rounded-md border border-white/[0.1] bg-[#080c13] transition focus-within:border-cyan-300/45 focus-within:bg-[#0b111a]">
                <div className="border-b border-white/[0.07] p-1.5">
                  <ReferenceEditor
                    refs={draft.reference_images}
                    implicitRefs={implicitReferenceImages}
                    projectId={projectId}
                    canvasNodes={canvasNodes}
                    uploading={uploading}
                    compact
                    setLightbox={setLightbox}
                    onChange={(reference_images) => onChange({ reference_images })}
                    onUpload={onUploadRefs}
                  />
                </div>
                <PromptMentionEditor
                  value={draft.prompt}
                  onChange={updatePrompt}
                  candidates={referenceMentionCandidates}
                  rows={6}
                  className="min-h-[150px] px-2.5 py-2 font-mono leading-6"
                  placeholder="描述主体、动作、构图、光线、风格、材质和需要避免的问题"
                />
              </div>
            </DraftField>
          </div>
        </div>

        <div className="rounded-lg border border-white/[0.08] bg-[#121722] p-3">
          <div className="mb-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">生成参数</div>
          <div className="grid gap-3 md:grid-cols-2">
            <SelectControl
              label="生成模型"
              value={imageProviderSelectValue}
              options={imageProviderOptions}
              onChange={(model) => onChange({ model })}
              hint={mediaProviderHint(selectedImageProvider, mediaConfigError)}
            />
            <MediaOptionGrid
              label="比例"
              value={imageAspectRatio}
              options={IMAGE_ASPECT_RATIO_GRID_OPTIONS}
              onChange={updateImageAspectRatio}
              columns="grid-cols-3 sm:grid-cols-4"
            />
            <ImageResolutionControl
              aspectRatio={imageAspectRatio}
              resolution={draft.resolution}
              onChange={(resolution) => onChange({ resolution })}
            />
            <div className="grid gap-3">
              <MediaOptionGrid
                label="画质"
                value={draft.quality}
                options={IMAGE_QUALITY_OPTIONS}
                onChange={(quality) => onChange({ quality })}
                columns="grid-cols-3"
              />
            </div>
          </div>
          {!hasConfiguredImageProviders && (
            <div className="mt-3 rounded-md border border-amber-500/20 bg-amber-950/15 px-3 py-2 text-xs leading-5 text-amber-100/80">
              设置里还没有启用的图片模型；需要先在设置中配置并启用模型后才能运行。
            </div>
          )}
        </div>
      </div>
    )
  }

  if (isVideo) {
    return (
      <div className="grid gap-3">
        <div className="min-w-0 rounded-lg border border-white/[0.08] bg-[#121722] p-3 shadow-[0_18px_45px_rgba(0,0,0,0.22)]">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-cyan-200/70">视频生成</div>
              <div className="mt-0.5 text-xs text-zinc-500">选择模式、写提示词、绑定参考材料</div>
            </div>
            <AutoSaveBadge saving={saving} dirty={dirty} />
          </div>

          <div className="grid gap-3">
            <SegmentedControl
              label="生成方式"
              value={activeVideoMode}
              options={videoModeOptions}
              onChange={updateVideoMode}
            />

            <div className="grid gap-3">
              <DraftField label="标题">
                <input
                  value={draft.title}
                  onChange={(event) => onChange({ title: event.target.value })}
                  className={inputClass}
                />
              </DraftField>
              <DraftField
                label="视频提示词"
                action={<CopyTextButton text={draft.prompt} label="提示词" />}
              >
                <div className="overflow-visible rounded-md border border-white/[0.1] bg-[#080c13] transition focus-within:border-cyan-300/45 focus-within:bg-[#0b111a]">
                  <div className="border-b border-white/[0.07] p-1.5">
                    <ReferenceEditor
                      refs={draft.reference_images}
                      implicitRefs={implicitReferenceImages}
                      quota={isVideo ? {
                        label: videoReferenceRule.quota,
                        title: videoReferenceRule.title,
                        invalid: videoReferenceRule.invalid,
                        overLimit: videoReferenceRule.overLimit,
                        uploadBlocked: videoReferenceRule.uploadBlocked,
                        remaining: videoReferenceRule.remaining,
                      } : undefined}
                      maxRefs={isVideo ? videoReferenceRule.limit : undefined}
                      projectId={projectId}
                      canvasNodes={canvasNodes}
                      uploading={uploading}
                      compact
                      setLightbox={setLightbox}
                      onChange={(reference_images) => onChange({ reference_images })}
                      onUpload={onUploadRefs}
                    />
                  </div>
                  <PromptMentionEditor
                    value={draft.prompt}
                    onChange={updatePrompt}
                    candidates={referenceMentionCandidates}
                    rows={6}
                    className="min-h-[150px] px-2.5 py-2 font-mono leading-6"
                    placeholder="描述画面主体、动作、镜头运动、光线、风格、节奏和结尾状态"
                  />
                </div>
              </DraftField>
            </div>
          </div>
        </div>

        <div className="rounded-lg border border-white/[0.08] bg-[#121722] p-3">
          <div className="mb-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">生成参数</div>
          <div className="grid gap-3 md:grid-cols-2">
            <SelectControl
              label="生成模型"
              value={videoProviderSelectValue}
              options={videoProviderOptions}
              onChange={updateVideoModel}
              hint={mediaProviderHint(selectedVideoProvider, mediaConfigError)}
            />
            <MediaOptionGrid
              label="比例"
              value={activeVideoAspectRatio}
              options={videoAspectOptions}
              onChange={(aspect_ratio) => onChange({ aspect_ratio })}
              columns="grid-cols-3"
            />
            <MediaOptionGrid
              label="清晰度"
              value={activeVideoResolution}
              options={videoResolutionOptions.map((item) => ({
                ...item,
                label: mediaResolutionButtonLabel(item.value, item.label),
              }))}
              onChange={(resolution) => onChange({ resolution })}
              columns="grid-cols-4"
            />
            {videoDurationConfigured ? (
              <DraftField label="时长">
                <input
                  type="number"
                  min={videoDurationMin}
                  max={videoDurationMax}
                  step={activeVideoDurationBounds.step ?? 1}
                  inputMode="numeric"
                  value={draft.duration_seconds}
                  onChange={(event) => onChange({ duration_seconds: event.target.value })}
                  onBlur={(event) => onChange({ duration_seconds: normalizeVideoDurationForRule(event.target.value, activeVideoDurationRule) })}
                  className={inputClass}
                  placeholder="秒"
                />
              </DraftField>
            ) : (
              <div className="rounded-md border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-xs leading-5 text-zinc-500">
                当前模型未声明可编辑的视频时长范围。
              </div>
            )}
          </div>
          {!hasConfiguredVideoProviders && (
            <div className="mt-3 rounded-md border border-amber-500/20 bg-amber-950/15 px-3 py-2 text-xs leading-5 text-amber-100/80">
              设置里还没有启用的视频模型；需要先在设置中配置并启用模型后才能运行。
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="grid gap-3">
      <div className="min-w-0 rounded-lg border border-white/[0.08] bg-[#121722] p-3 shadow-[0_18px_45px_rgba(0,0,0,0.22)]">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-cyan-200/70">节点属性</div>
            <div className="mt-0.5 text-xs text-zinc-500">{node.type}</div>
          </div>
          <AutoSaveBadge saving={saving} dirty={dirty} />
        </div>
        <div className="grid gap-3">
          <DraftField
            label={mainLabel}
            action={<CopyTextButton text={mainText} label={mainCopyLabel} />}
          >
            <textarea
              value={mainText}
              onChange={(event) => onChange(isText ? { content: event.target.value } : { prompt: event.target.value })}
              rows={isText ? 9 : 11}
              className={`${inputClass} min-h-[220px] resize-y font-mono text-[13px] leading-6`}
            />
          </DraftField>
          <DraftField label="标题">
            <input
              value={draft.title}
              onChange={(event) => onChange({ title: event.target.value })}
              className={inputClass}
            />
          </DraftField>
        </div>
      </div>

      {hasMediaControls && (
        <div className="space-y-3">
          <div className="rounded-lg border border-white/[0.08] bg-[#121722] p-3">
            <div className="mb-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">规格</div>
            <div className="space-y-3">
              {isAudio ? (
                <>
                  <SelectControl
                    label="生成模型"
                    value={audioProviderSelectValue}
                    options={audioProviderOptions}
                    onChange={updateAudioProvider}
                    hint={
                      mediaConfigError
                        ? mediaConfigError
                        : selectedAudioProvider
                          ? `${audioProviderTypeLabel(selectedAudioMode)} · ${mediaProviderHint(selectedAudioProvider)}`
                          : "设置里还没有启用的音频模型。"
                    }
                  />
                  {!hasConfiguredAudioProviders && (
                    <div className="rounded-md border border-amber-500/20 bg-amber-950/15 px-3 py-2 text-xs leading-5 text-amber-100/80">
                      设置里还没有启用的音频模型；需要先在设置中配置并启用模型后才能运行。
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
                <>
                  <SelectControl
                    label="生成模型"
                    value={videoProviderSelectValue}
                    options={videoProviderOptions}
                    onChange={updateVideoModel}
                    hint={mediaProviderHint(selectedVideoProvider, mediaConfigError)}
                  />
                  {!hasConfiguredVideoProviders && (
                    <div className="rounded-md border border-amber-500/20 bg-amber-950/15 px-3 py-2 text-xs leading-5 text-amber-100/80">
                      设置里还没有启用的视频模型；需要先在设置中配置并启用模型后才能运行。
                    </div>
                  )}
                  <ChipControl
                    label="画幅"
                    value={activeVideoAspectRatio}
                    options={videoAspectOptions}
                    placeholder="比例"
                    onChange={(aspect_ratio) => onChange({ aspect_ratio })}
                    allowCustom={false}
                  />
                </>
              ) : (
                <>
                  <SelectControl
                    label="生成模型"
                    value={imageProviderSelectValue}
                    options={imageProviderOptions}
                    onChange={(model) => onChange({ model })}
                    hint={mediaProviderHint(selectedImageProvider, mediaConfigError)}
                  />
                  {!hasConfiguredImageProviders && (
                    <div className="rounded-md border border-amber-500/20 bg-amber-950/15 px-3 py-2 text-xs leading-5 text-amber-100/80">
                      设置里还没有启用的图片模型；需要先在设置中配置并启用模型后才能运行。
                    </div>
                  )}
                  <SelectControl
                    label="画幅"
                    value={imageAspectRatio}
                    options={[
                      { label: "16:9", value: "16:9" },
                      { label: "9:16", value: "9:16" },
                      { label: "1:1", value: "1:1" },
                    ]}
                    onChange={updateImageAspectRatio}
                  />
                </>
              )}
              {isImage && (
                <>
                  <ImageResolutionControl
                    aspectRatio={imageAspectRatio}
                    resolution={draft.resolution}
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
                    label="生成模式"
                    value={activeVideoMode}
                    options={videoModeOptions}
                    onChange={updateVideoMode}
                  />
                  <SelectControl
                    label="分辨率"
                    value={activeVideoResolution}
                    options={videoResolutionOptions}
                    onChange={(resolution) => onChange({ resolution })}
                  />
                  <DraftField label="时长">
                    <input
                      type="number"
                      min="1"
                      step="1"
                      inputMode="numeric"
                      value={draft.duration_seconds}
                      onChange={(event) => onChange({ duration_seconds: event.target.value })}
                      className={inputClass}
                      placeholder="秒"
                    />
                  </DraftField>
                </>
              )}
            </div>
          </div>

          {!isAudio && (
            <div className="rounded-lg border border-white/[0.08] bg-[#121722] p-3">
              <div className="mb-2.5 flex items-center justify-between">
                <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-500">引用图</div>
                <span className="text-[11px] text-zinc-600">{visibleReferenceImageCount}</span>
              </div>
              <ReferenceEditor
                refs={draft.reference_images}
                implicitRefs={implicitReferenceImages}
                projectId={projectId}
                canvasNodes={canvasNodes}
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

        <MediaSpecBadges spec={spec} className="mt-2" />
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
          const label = entry.current ? "当前结果" : formatHistoryTime(entry.created_at) || `历史 ${entry.index + 1}`
          const canSwitch = !entry.current && !busy && !switching
          return (
            <div
              key={`${entry.id}-${entry.index}`}
              role="button"
              tabIndex={canSwitch ? 0 : -1}
              aria-disabled={!canSwitch}
              onClick={() => {
                if (canSwitch) void onSwitch(entry)
              }}
              onKeyDown={(event) => {
                if (!canSwitch) return
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault()
                  void onSwitch(entry)
                }
              }}
              className={`rounded-lg border bg-black/24 p-2 transition ${
                canSwitch
                  ? "cursor-pointer border-white/[0.08] hover:border-cyan-200/40 hover:bg-cyan-950/10"
                  : entry.current
                    ? "border-emerald-300/18 bg-emerald-300/[0.035]"
                    : "cursor-not-allowed border-white/[0.07] opacity-60"
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
                    <div className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                      entry.current
                        ? "bg-emerald-300/10 text-emerald-100"
                        : "bg-cyan-300/10 text-cyan-100"
                    }`}>
                      {entry.current ? "当前" : switching ? "还原中" : "还原"}
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

function NodePanelMediaHistoryStrip({
  node,
}: {
  node: NodeFull
}) {
  const kind = node.type === "image" || node.type === "video" || node.type === "audio" ? node.type : null
  if (!kind) return null
  const entries = mediaHistoryEntriesFromOutput(node.output, kind)
  if (entries.length === 0) return null

  const openEntry = (entry: MediaHistoryEntry) => {
    const preview = asObj(parseJson(entry.output)) || {}
    window.dispatchEvent(new CustomEvent("openreel:preview-node", {
      detail: {
        nodeId: node.id,
        type: node.type,
        title: node.title,
        input: nodeInputFields(node.input),
        output: entry.output,
        preview,
        prompt: entry.prompt || node.prompt || "",
        readOnly: true,
      },
    }))
  }

  return (
    <div className="border-t border-white/[0.07] pt-2">
      <div className="flex min-h-[36px] items-center gap-1 overflow-x-auto px-0.5 py-0.5" aria-label="生成历史">
        {entries.map((entry) => {
          const primary = entry.media[0]
          if (!primary) return null
          const title = entry.current
            ? "当前结果"
            : formatHistoryTime(entry.created_at) || `历史 ${entry.index + 1}`
          return (
            <button
              key={`${entry.id}-${entry.index}`}
              type="button"
              title={title}
              aria-label={title}
              onClick={(event) => {
                event.preventDefault()
                event.stopPropagation()
                openEntry(entry)
              }}
              onPointerDown={(event) => event.stopPropagation()}
              className={`group relative flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-md border bg-white/[0.045] text-zinc-300 shadow-[0_6px_16px_rgba(0,0,0,0.16)] transition hover:-translate-y-0.5 hover:border-white/[0.22] hover:bg-white/[0.08] hover:text-zinc-50 ${
                entry.current ? "border-emerald-300/25" : "border-white/[0.1]"
              }`}
            >
              {primary.kind === "image" ? (
                <img src={primary.src} alt="" className="h-full w-full object-cover" draggable={false} />
              ) : primary.kind === "video" ? (
                <>
                  <video poster={primary.poster} muted playsInline preload="metadata" className="h-full w-full object-cover" draggable={false}>
                    <source src={primary.src} type={videoMimeType(primary.src)} />
                  </video>
                  <span className="absolute inset-0 flex items-center justify-center bg-black/18">
                    <span className="ml-0.5 h-0 w-0 border-y-[5px] border-l-[8px] border-y-transparent border-l-white" />
                  </span>
                </>
              ) : (
                <SparkIcon className="h-4 w-4" />
              )}
              {entry.current && (
                <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-emerald-300 shadow-[0_0_8px_rgba(110,231,183,0.8)]" />
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function NodePanelTextHistoryButton({
  node,
  input,
  rawOutput,
  nodePrompt,
}: {
  node: NodeFull
  input: Record<string, unknown>
  rawOutput: unknown
  nodePrompt: string
}) {
  const [open, setOpen] = useState(false)
  const entries = textChatHistoryFromPayload(input, rawOutput, nodePrompt)
  if (entries.length === 0) return null
  const openEntry = (entry: TextChatHistoryEntry) => {
    const output = {
      type: "text",
      content: entry.content,
      prompt: entry.prompt,
      model: entry.model,
      created_at: entry.created_at,
    }
    window.dispatchEvent(new CustomEvent("openreel:preview-node", {
      detail: {
        nodeId: node.id,
        type: "text",
        title: node.title,
        input: { ...input, prompt: entry.prompt, content: entry.content },
        output,
        previewText: entry.content,
        prompt: entry.prompt || nodePrompt,
        readOnly: true,
      },
    }))
  }
  return (
    <div className="border-t border-white/[0.07] pt-2">
      <button
        type="button"
        title="历史对话"
        aria-label="历史对话"
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          setOpen((value) => !value)
        }}
        onPointerDown={(event) => event.stopPropagation()}
        className={`relative flex h-8 w-8 items-center justify-center rounded-md border bg-white/[0.045] text-zinc-300 shadow-[0_6px_16px_rgba(0,0,0,0.16)] transition hover:-translate-y-0.5 hover:border-white/[0.22] hover:bg-white/[0.08] hover:text-zinc-50 ${
          open ? "border-sky-200/40 text-sky-100" : "border-white/[0.1]"
        }`}
      >
        <ChatBubbleIcon />
        <span className="absolute -right-1 -top-1 min-w-[16px] rounded-full border border-black/50 bg-sky-300 px-1 text-center text-[9px] font-semibold leading-4 text-sky-950">
          {entries.length}
        </span>
      </button>
      {open && (
        <div className="mt-2 max-h-72 overflow-y-auto rounded-lg border border-white/[0.08] bg-black/28 p-2.5 shadow-inner shadow-black/25">
          <div className="space-y-2">
            {entries.slice().reverse().map((entry, index) => (
              <button
                key={`${entry.id}-${index}`}
                type="button"
                onClick={(event) => {
                  event.preventDefault()
                  event.stopPropagation()
                  openEntry(entry)
                }}
                onPointerDown={(event) => event.stopPropagation()}
                className="block w-full rounded-lg border border-white/[0.07] bg-white/[0.035] p-2.5 text-left text-[12px] leading-5 text-zinc-300 transition hover:border-sky-200/30 hover:bg-sky-300/[0.035]"
              >
                <div className="mb-2 flex items-center justify-between gap-2 text-[10px] text-zinc-500">
                  <span>{entry.current ? "当前结果" : entry.created_at ? formatHistoryTime(entry.created_at) : `历史 ${entries.length - index}`}</span>
                  {entry.model && <span className="truncate">{entry.model}</span>}
                </div>
                {entry.prompt && (
                  <div className="mb-2 rounded-md bg-black/24 px-2 py-1.5 text-zinc-400">
                    {entry.prompt}
                  </div>
                )}
                <div className="whitespace-pre-wrap break-words text-zinc-100">{entry.content || "无正文记录"}</div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function MediaParameterDialog({
  open,
  anchorRef,
  nodeType,
  draft,
  selectedAudioMode,
  imageAspectRatio,
  imageResolution,
  videoAspectOptions,
  activeVideoAspectRatio,
  activeVideoResolution,
  videoResolutionOptions,
  videoDurationValue,
  videoDurationConfigured,
  videoDurationMin,
  videoDurationMax,
  videoDurationStep,
  videoDurationRule,
  onClose,
  onChange,
  onImageAspectRatio,
  onImageResolution,
}: {
  open: boolean
  anchorRef: { current: HTMLButtonElement | null }
  nodeType: "image" | "video" | "audio"
  draft: EditableNodeDraft
  selectedAudioMode: AudioProviderMode
  imageAspectRatio: string
  imageResolution: string
  videoAspectOptions: SelectOption[]
  activeVideoAspectRatio: string
  activeVideoResolution: string
  videoResolutionOptions: SelectOption[]
  videoDurationValue: number
  videoDurationConfigured: boolean
  videoDurationMin: number
  videoDurationMax: number
  videoDurationStep: number
  videoDurationRule: VideoDurationSummary
  onClose: () => void
  onChange: (patch: Partial<EditableNodeDraft>) => void
  onImageAspectRatio: (value: string) => void
  onImageResolution: (value: string) => void
}) {
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const [position, setPosition] = useState<{ left: number; top: number; maxHeight: number } | null>(null)

  useLayoutEffect(() => {
    if (!open || typeof window === "undefined") {
      setPosition(null)
      return
    }

    const updatePosition = () => {
      const anchor = anchorRef.current
      if (!anchor) return
      const anchorRect = anchor.getBoundingClientRect()
      const popover = popoverRef.current
      const viewportWidth = window.visualViewport?.width || window.innerWidth
      const viewportHeight = window.visualViewport?.height || window.innerHeight
      const margin = 12
      const gap = 8
      const measuredWidth = popover?.getBoundingClientRect().width || Math.min(352, viewportWidth - margin * 2)
      const contentHeight = popover?.scrollHeight || 460
      const availableAbove = Math.max(0, anchorRect.top - gap - margin)
      const availableBelow = Math.max(0, viewportHeight - anchorRect.bottom - gap - margin)
      const preferredHeight = Math.min(contentHeight, 520)
      const placeAbove = availableAbove >= preferredHeight || availableAbove >= availableBelow
      const availableHeight = placeAbove ? availableAbove : availableBelow
      const maxHeight = Math.max(160, Math.min(520, availableHeight, viewportHeight - margin * 2))
      const renderedHeight = Math.min(contentHeight, maxHeight)
      const top = Math.max(
        margin,
        Math.min(
          placeAbove ? anchorRect.top - gap - renderedHeight : anchorRect.bottom + gap,
          viewportHeight - margin - renderedHeight,
        ),
      )
      const left = Math.max(margin, Math.min(anchorRect.left, viewportWidth - measuredWidth - margin))
      const next = { left, top, maxHeight }
      setPosition((current) => (
        current && current.left === next.left && current.top === next.top && current.maxHeight === next.maxHeight
          ? current
          : next
      ))
    }

    const frame = window.requestAnimationFrame(updatePosition)
    window.addEventListener("resize", updatePosition)
    window.addEventListener("scroll", updatePosition, true)
    const resizeObserver = typeof ResizeObserver !== "undefined" && popoverRef.current
      ? new ResizeObserver(updatePosition)
      : null
    if (resizeObserver && popoverRef.current) resizeObserver.observe(popoverRef.current)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener("resize", updatePosition)
      window.removeEventListener("scroll", updatePosition, true)
      resizeObserver?.disconnect()
    }
  }, [anchorRef, open])

  useEffect(() => {
    if (!open) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      event.preventDefault()
      onClose()
    }
    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [onClose, open])

  useEffect(() => {
    if (!open) return
    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (!(target instanceof Node)) return
      if (popoverRef.current?.contains(target) || anchorRef.current?.contains(target)) return
      onClose()
    }
    document.addEventListener("pointerdown", handleOutsidePointerDown)
    return () => document.removeEventListener("pointerdown", handleOutsidePointerDown)
  }, [anchorRef, onClose, open])

  if (!open || typeof document === "undefined") return null

  const title = nodeType === "image" ? "图片生成参数" : nodeType === "video" ? "视频生成参数" : "音频生成参数"

  return createPortal(
    <div
      ref={popoverRef}
      role="dialog"
      aria-label={title}
      aria-modal="false"
      className="fixed z-[120] w-[352px] max-w-[calc(100vw-24px)] overflow-y-auto rounded-[15px] border border-white/[0.12] bg-[#292929] p-3 text-zinc-100 shadow-[0_12px_32px_rgba(0,0,0,0.48)] ring-1 ring-black/20 transition-[opacity,transform] duration-150"
      style={{
        left: position?.left ?? 0,
        top: position?.top ?? 0,
        maxHeight: position?.maxHeight ?? "calc(100dvh - 24px)",
        opacity: position ? 1 : 0,
        pointerEvents: position ? "auto" : "none",
        visibility: position ? "visible" : "hidden",
      }}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
    >
          {nodeType === "image" && (
            <div className="grid gap-3">
              <MediaOptionGrid
                label="画质"
                value={draft.quality}
                options={IMAGE_QUALITY_OPTIONS}
                onChange={(quality) => onChange({ quality })}
                columns="grid-cols-3"
                compact
              />
              <ImageResolutionControl
                aspectRatio={imageAspectRatio}
                resolution={imageResolution}
                onChange={onImageResolution}
                compact
              />
              <MediaOptionGrid
                label="比例"
                value={imageAspectRatio}
                options={IMAGE_ASPECT_RATIO_GRID_OPTIONS}
                onChange={onImageAspectRatio}
                columns="grid-cols-5"
                compact
                aspectGlyph
              />
            </div>
          )}

          {nodeType === "video" && (
            <div className="grid gap-3">
              <MediaOptionGrid
                label="比例"
                value={activeVideoAspectRatio}
                options={videoAspectOptions}
                onChange={(aspect_ratio) => onChange({ aspect_ratio })}
                columns="grid-cols-5"
                compact
                aspectGlyph
              />
              <MediaOptionGrid
                label="清晰度"
                value={activeVideoResolution}
                options={videoResolutionOptions.map((item) => ({
                  ...item,
                  label: mediaResolutionButtonLabel(item.value, item.label),
                }))}
                onChange={(resolution) => onChange({ resolution })}
                columns="grid-cols-4"
                compact
              />
              {videoDurationConfigured ? (
                <DraftField label="视频时长">
                  <div className="flex items-center gap-2">
                    <input
                      type="range"
                      min={videoDurationMin}
                      max={videoDurationMax}
                      step={videoDurationStep}
                      value={Number.isFinite(videoDurationValue) ? videoDurationValue : videoDurationMin}
                      onChange={(event) => onChange({ duration_seconds: event.target.value })}
                      className="h-1.5 min-w-0 flex-1 accent-cyan-300"
                    />
                    <input
                      type="number"
                      min={videoDurationMin}
                      max={videoDurationMax}
                      step={videoDurationStep}
                      inputMode="numeric"
                      value={draft.duration_seconds}
                      onChange={(event) => onChange({ duration_seconds: event.target.value })}
                      onBlur={(event) => onChange({ duration_seconds: normalizeVideoDurationForRule(event.target.value, videoDurationRule) })}
                      className="h-8 w-14 rounded-md border border-white/[0.12] bg-white/[0.05] px-2 text-xs text-zinc-100 outline-none [color-scheme:dark] focus:border-cyan-300/45"
                      placeholder="秒"
                    />
                    <span className="text-[11px] text-zinc-500">秒</span>
                  </div>
                </DraftField>
              ) : (
                <div className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-2.5 py-2 text-[11px] leading-4 text-zinc-500">
                  当前模型未声明可编辑的视频时长范围。
                </div>
              )}
            </div>
          )}

          {nodeType === "audio" && (
            <div className="grid gap-2.5">
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
                      className="h-8 w-full rounded-md border border-white/[0.1] bg-[#080c13] px-2 text-xs text-zinc-100 outline-none [color-scheme:dark] placeholder:text-zinc-500 focus:border-cyan-300/45"
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
                  />
                </>
              )}
              {selectedAudioMode === "unknown" && (
                <div className="rounded-md border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-xs leading-5 text-zinc-400">
                  当前音频模型未声明可编辑的生成参数。
                </div>
              )}
            </div>
          )}
    </div>,
    document.body,
  )
}

function NodeCanvasContextPanel({
  node,
  draft,
  mediaProviders,
  llmProviders,
  modelDefaults,
  videoProtocols,
  mediaConfigError,
  projectId,
  canvasNodes,
  referenceMentionCandidates,
  dirty,
  saving,
  uploading,
  uploadingOutput,
  rerunning,
  actionDisabled,
  displayError,
  setLightbox,
  onChange,
  onUploadRefs,
  onUploadOutput,
  onRerun,
}: {
  node: NodeFull
  draft: EditableNodeDraft
  mediaProviders: MediaProviderOption[]
  llmProviders: LlmProviderOption[]
  modelDefaults?: RuntimeModelDefaults
  videoProtocols: VideoProtocolSummary[]
  mediaConfigError?: string | null
  projectId?: string | null
  canvasNodes: CanvasGraphNode[]
  referenceMentionCandidates: ReferenceMentionCandidate[]
  dirty: boolean
  saving: boolean
  uploading: boolean
  uploadingOutput: boolean
  rerunning: boolean
  actionDisabled: boolean
  displayError?: string
  setLightbox: (v: { src: string; alt?: string } | null) => void
  onChange: (patch: Partial<EditableNodeDraft>) => void
  onUploadRefs: (files: FileList | File[] | null) => void | Promise<void>
  onUploadOutput: (files: FileList | null) => void | Promise<void>
  onRerun?: (nodeId: string) => void | Promise<void>
  onRequestStoryRevision?: (nodeId: string) => void | Promise<void>
}) {
  const isText = node.type === "text"
  const isImage = node.type === "image"
  const isVideo = node.type === "video"
  const isAudio = node.type === "audio"
  const enabledImageProviders = mediaProvidersForKind(mediaProviders, "image")
  const enabledVideoProviders = mediaProvidersForKind(mediaProviders, "video")
  const enabledAudioProviders = mediaProvidersForKind(mediaProviders, "audio")
  const enabledTextProviders = enabledLlmProviders(llmProviders)
  const selectedTextProvider = isText ? resolveLlmProvider(draft.model, enabledTextProviders) : undefined
  const selectedImageProvider = isImage ? resolveMediaProvider(draft.model, enabledImageProviders) : undefined
  const selectedVideoProvider = isVideo ? resolveMediaProvider(draft.model, enabledVideoProviders) : undefined
  const selectedAudioProvider = isAudio ? resolveAudioProvider(draft.model, enabledAudioProviders) : undefined
  const textProviderOptions = llmProviderSelectOptions(enabledTextProviders, draft.model, selectedTextProvider)
  const selectedAudioMode = audioProviderModeFromProvider(selectedAudioProvider)
  const imageAspectRatio = normalizeImageAspectRatio(draft.aspect_ratio)
  const selectedVideoModelName = selectedVideoProvider?.model_name || draft.model
  const selectedVideoProtocol = videoProtocolForProvider(selectedVideoProvider, videoProtocols)
  const selectedVideoProfile = videoProfileForModel(selectedVideoProtocol, selectedVideoModelName)
  const videoModeOptions = videoProtocolModeOptions(selectedVideoProtocol, selectedVideoProfile)
  const activeVideoMode = effectiveVideoMode(draft.video_mode, videoModeOptions)
  const providerVideoResolutions = mediaProviderParamStringArray(selectedVideoProvider, "supported_resolutions", "resolutions")
    .map((item) => item.toLowerCase())
  const providerVideoRatios = mediaProviderParamStringArray(selectedVideoProvider, "supported_ratios", "ratios", "supported_aspect_ratios")
  const supportedVideoResolutions = providerVideoResolutions.length
    ? providerVideoResolutions
    : videoSupportedResolutionsForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const supportedVideoRatios = providerVideoRatios.length
    ? providerVideoRatios.filter((item) => item !== "adaptive")
    : videoSupportedRatiosForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const defaultVideoAspectRatio = defaultVideoAspectRatioForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const activeVideoAspectRatio = draft.aspect_ratio.trim() || defaultVideoAspectRatio || supportedVideoRatios[0] || ""
  const videoAspectOptions = videoAspectSelectOptions(activeVideoAspectRatio, supportedVideoRatios)
  const videoResolutionOptions = videoResolutionSelectOptions(draft.resolution, supportedVideoResolutions)
  const providerDefaultVideoResolution = mediaProviderParamText(selectedVideoProvider, "default_resolution", "resolution").toLowerCase()
  const activeVideoResolution = draft.resolution || (providerDefaultVideoResolution && supportedVideoResolutions.includes(providerDefaultVideoResolution)
    ? providerDefaultVideoResolution
    : defaultVideoResolutionForProvider(
    selectedVideoProvider,
    selectedVideoProtocol,
    selectedVideoProfile,
    activeVideoMode,
  ))
  const activeVideoModeConfig = videoModeConfig(selectedVideoProtocol, activeVideoMode, selectedVideoProfile)
  const activeVideoDurationRule = videoDurationRuleForProvider(selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, activeVideoMode)
  const activeVideoDurationBounds = videoDurationBounds(activeVideoDurationRule)
  const videoReferenceRule = videoReferenceRuleHint(
    activeVideoModeConfig,
    referenceDisplayCount(draft.reference_images, referenceMentionCandidateRefs(referenceMentionCandidates), projectId),
  )
  const titleValue = draft.title || node.title || ""
  const mainText = isText ? draft.content : draft.prompt
  const mediaRunTarget = isText ? "文本" : isVideo ? "视频" : isAudio ? "音频" : "图片"
  const canRerun = Boolean((isText || isImage || isVideo || isAudio) && onRerun)
  const actionBusy = actionDisabled || rerunning || uploadingOutput || node.status === "running" || node.status === "queued"
  const saveStateLabel = saving ? "保存中" : dirty ? "待自动保存" : "已保存"
  const saveStateClass = saving
    ? "bg-cyan-300 shadow-[0_0_14px_rgba(103,232,249,0.55)]"
    : dirty
      ? "bg-amber-300 shadow-[0_0_14px_rgba(252,211,77,0.45)]"
      : "bg-emerald-300 shadow-[0_0_12px_rgba(110,231,183,0.3)]"
  const [mediaParameterDialogOpen, setMediaParameterDialogOpen] = useState(false)
  const mediaParameterToggleRef = useRef<HTMLButtonElement | null>(null)
  const compactSelectClass = "h-7 min-w-0 appearance-none rounded-md border border-white/[0.08] bg-[#1f1f1f] px-2 text-[11px] font-medium text-zinc-100 shadow-inner shadow-black/20 outline-none [color-scheme:dark] transition focus:border-white/[0.18] focus:bg-[#262626] focus:text-zinc-50 hover:border-white/[0.14] hover:bg-[#282828] [&>option]:bg-[#1f1f1f] [&>option]:text-zinc-100"
  const textareaHeightClass = isVideo ? "min-h-[78px]" : isAudio ? "min-h-[88px]" : "min-h-[96px]"
  const promptMaxRows = isText ? 7 : isVideo ? 6 : 7
  const imageResolutionValue = draft.resolution || defaultImageResolutionForAspect(imageAspectRatio)
  const imageResolutionTierLabel = IMAGE_RESOLUTION_TIER_OPTIONS.find((item) => item.value === imageResolutionTier(imageResolutionValue))?.label || "1K"
  const videoResolutionLabel = activeVideoResolution ? mediaResolutionButtonLabel(activeVideoResolution) : ""
  const videoDurationValue = Number.parseFloat(draft.duration_seconds)
  const videoDurationConfigured = activeVideoDurationBounds.min !== undefined
    || activeVideoDurationBounds.max !== undefined
    || activeVideoDurationBounds.allowed.length > 0
  const videoDurationMin = activeVideoDurationBounds.min ?? activeVideoDurationBounds.allowed[0] ?? 0
  const videoDurationMax = activeVideoDurationBounds.max
    ?? activeVideoDurationBounds.allowed[activeVideoDurationBounds.allowed.length - 1]
    ?? activeVideoDurationBounds.min
    ?? 0
  const videoDurationStep = activeVideoDurationBounds.step ?? 1
  const mediaParameterSummary = isImage
    ? [imageAspectRatio, imageQualityLabel(draft.quality), imageResolutionTierLabel].filter(Boolean).join(" · ")
    : isVideo
      ? [activeVideoAspectRatio, videoResolutionLabel, mediaDurationLabel(draft.duration_seconds)].filter(Boolean).join(" · ")
      : isAudio
        ? [
          selectedAudioMode === "music" ? "音乐" : selectedAudioMode === "tts" ? "语音" : "音频",
          mediaDurationLabel(draft.duration_seconds),
          draft.format,
        ].filter(Boolean).join(" · ")
        : ""
  const hasMediaParameterToggle = isImage || isVideo || isAudio

  useEffect(() => {
    setMediaParameterDialogOpen(false)
  }, [node.id, node.type])

  const updateImageAspectRatio = (aspectRatio: string) => {
    const nextAspectRatio = normalizeImageAspectRatio(aspectRatio)
    const resolution = defaultImageResolutionForAspect(nextAspectRatio, imageResolutionTier(draft.resolution))
    onChange({ aspect_ratio: nextAspectRatio, resolution })
  }
  const updateVideoModel = (model: string) => {
    const selectedProvider = resolveMediaProvider(model, enabledVideoProviders)
    const modelForResolution = selectedProvider?.model_name || model
    const protocol = videoProtocolForProvider(selectedProvider, videoProtocols)
    const profile = videoProfileForModel(protocol, modelForResolution)
    const modeOptions = videoProtocolModeOptions(protocol, profile)
    const video_mode = effectiveVideoMode(draft.video_mode, modeOptions)
    onChange({
      model,
      ...normalizeVideoDraftForMode(draft, selectedProvider, protocol, profile, video_mode),
    })
  }
  const updateVideoMode = (videoMode: string) => {
    const mode = effectiveVideoMode(videoMode, videoModeOptions)
    onChange(normalizeVideoDraftForMode(draft, selectedVideoProvider, selectedVideoProtocol, selectedVideoProfile, mode))
  }
  const implicitReferenceImages = referenceMentionCandidateRefs(referenceMentionCandidates)
  const updatePrompt = (prompt: string, selected?: ReferenceMentionCandidate) => {
    if (selected && !draft.reference_images.includes(selected.ref)) {
      onChange({ prompt, reference_images: [...draft.reference_images, selected.ref] })
      return
    }
    onChange({ prompt })
  }

  return (
    <>
      <div className="flex max-h-full min-h-0 w-full flex-col gap-2 rounded-xl border border-white/[0.1] bg-[#252525]/96 p-2.5 text-zinc-100 shadow-[0_24px_70px_rgba(0,0,0,0.46)] backdrop-blur-xl">
      <div className="flex shrink-0 items-center gap-1.5 px-0.5">
        <span
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-white/[0.09] bg-white/[0.06] text-zinc-300 shadow-inner shadow-white/[0.03]"
          title={node.type}
          aria-hidden="true"
        >
          {isImage ? <ImageIcon className="h-3.5 w-3.5" /> : isVideo ? <ArrowUpIcon className="h-3.5 w-3.5 rotate-90" /> : isAudio ? <SparkIcon className="h-3.5 w-3.5" /> : <span className="text-[11px] font-semibold">T</span>}
        </span>
        <input
          value={titleValue}
          onChange={(event) => onChange({ title: event.target.value })}
          className="min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-1.5 text-[13px] font-semibold text-zinc-100 outline-none transition placeholder:text-zinc-600 hover:border-white/[0.06] focus:border-white/[0.12] focus:bg-black/18"
          placeholder="节点标题"
        />
        <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${saveStateClass}`} title={saveStateLabel} aria-label={saveStateLabel} />
      </div>

      {isVideo && (
        <div className="flex shrink-0 gap-1.5 overflow-x-auto px-0.5 pb-0.5">
          {videoModeOptions.map((item) => {
            const active = activeVideoMode === item.value
            return (
              <button
                key={item.value || "auto"}
                type="button"
                onClick={() => updateVideoMode(item.value)}
                title={item.hint || item.label}
                className={`h-7 shrink-0 rounded-md border px-2 text-[11px] font-medium transition ${
                  active
                    ? "border-cyan-300/35 bg-cyan-300/12 text-cyan-100 shadow-[0_8px_18px_rgba(34,211,238,0.10)]"
                    : "border-white/[0.08] bg-white/[0.045] text-zinc-300 hover:bg-white/[0.09] hover:text-zinc-50"
                }`}
              >
                {item.label}
              </button>
            )
          })}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-visible rounded-lg border border-white/[0.08] bg-[#202020] shadow-inner shadow-black/20 transition focus-within:border-zinc-300/45 focus-within:bg-[#292929]">
        {(isText || isImage || isVideo) && (
          <div className="shrink-0 px-1.5 pt-1.5">
            <ReferenceEditor
              refs={draft.reference_images}
              implicitRefs={implicitReferenceImages}
              quota={isVideo ? {
                label: videoReferenceRule.quota,
                title: videoReferenceRule.title,
                invalid: videoReferenceRule.invalid,
                overLimit: videoReferenceRule.overLimit,
                uploadBlocked: videoReferenceRule.uploadBlocked,
                remaining: videoReferenceRule.remaining,
              } : undefined}
              maxRefs={isVideo ? videoReferenceRule.limit : undefined}
              projectId={projectId}
              canvasNodes={canvasNodes}
              uploading={uploading}
              compact
              setLightbox={setLightbox}
              onChange={(reference_images) => onChange({ reference_images })}
              onUpload={onUploadRefs}
            />
          </div>
        )}
        {isText ? (
          <div>
            <PromptMentionEditor
              value={draft.prompt}
              onChange={updatePrompt}
              candidates={referenceMentionCandidates}
              rows={4}
              maxRows={promptMaxRows}
              className="min-h-[104px]"
              placeholder="输入要让模型回答的内容；上方参考图会一起发送给模型"
            />
          </div>
        ) : (
          isAudio ? (
            <textarea
              value={mainText}
              onChange={(event) => onChange({ prompt: event.target.value })}
              rows={4}
              className={`${textareaHeightClass} w-full resize-y border-0 bg-transparent px-3 py-2 text-[13px] leading-5 text-zinc-100 outline-none [color-scheme:dark] placeholder:text-zinc-500`}
              placeholder="描述音乐、旁白或声音素材"
            />
          ) : (
            <PromptMentionEditor
              value={mainText}
              onChange={updatePrompt}
              candidates={referenceMentionCandidates}
              rows={isVideo ? 3 : 4}
              maxRows={promptMaxRows}
              className={textareaHeightClass}
              placeholder={isImage ? "描述你想生成或编辑的图片内容；输入 @ 可选择参考图" : "描述你想生成的画面内容；输入 @ 可选择参考图"}
            />
          )
        )}
      </div>

      {(isImage || isVideo || isAudio || isText) && (
        <div className="shrink-0 border-t border-white/[0.06] px-0.5 pt-2">
          <div className="flex flex-wrap items-center gap-1.5">
          {(isText || isImage || isVideo || isAudio) && (
            <select
              value={
                isText
                  ? (selectedTextProvider?.name || draft.model || defaultLlmProviderName(enabledTextProviders, modelDefaults))
                  : isImage
                  ? mediaProviderSelectValue(draft.model, selectedImageProvider)
                  : isVideo
                    ? mediaProviderSelectValue(draft.model, selectedVideoProvider)
                    : mediaProviderSelectValue(draft.model, selectedAudioProvider)
              }
              onChange={(event) => {
                if (isVideo) updateVideoModel(event.target.value)
                else onChange({ model: event.target.value })
              }}
              className={`${compactSelectClass} max-w-[240px] flex-1`}
              title="模型"
            >
              {(isText
                ? textProviderOptions
                : isImage
                ? mediaProviderSelectOptions(enabledImageProviders, draft.model, selectedImageProvider)
                : isVideo
                  ? mediaProviderSelectOptions(enabledVideoProviders, draft.model, selectedVideoProvider)
                  : mediaProviderSelectOptions(enabledAudioProviders, draft.model, selectedAudioProvider)
	              ).map((option) => (
	                <option
	                  key={`${option.value}:${option.label}`}
	                  value={option.value}
	                  disabled={option.disabled}
                  className="bg-[#1f1f1f] text-zinc-100"
                >
                  {option.label}
	                </option>
	              ))}
	            </select>
	          )}

	          {hasMediaParameterToggle && (
            <button
              type="button"
              ref={mediaParameterToggleRef}
              data-openreel-node-parameter-toggle="true"
              aria-expanded={mediaParameterDialogOpen}
              aria-haspopup="dialog"
              onClick={() => setMediaParameterDialogOpen((value) => !value)}
              onPointerDown={(event) => event.stopPropagation()}
              className={`flex h-7 min-w-[160px] flex-1 items-center gap-1.5 rounded-md border px-2 text-left text-[11px] font-medium shadow-inner shadow-black/20 transition ${
                mediaParameterDialogOpen
                  ? "border-white/[0.28] bg-[#303030] text-zinc-100 shadow-[0_0_0_1px_rgba(255,255,255,0.04)]"
                  : "border-white/[0.08] bg-[#1f1f1f] text-zinc-100 hover:border-white/[0.14] hover:bg-[#282828]"
              }`}
              title={mediaParameterSummary || "生成参数"}
            >
              <span className={`h-3 w-3 shrink-0 rounded-[3px] border ${mediaParameterDialogOpen ? "border-zinc-200/80" : "border-zinc-300/70"}`} />
	              <span className="min-w-0 flex-1 truncate">{mediaParameterSummary || "生成参数"}</span>
              <span className={`shrink-0 ${mediaParameterDialogOpen ? "text-zinc-300" : "text-zinc-500"}`}>⌄</span>
	            </button>
	          )}
	          {(isImage || isVideo) && (
	            <label
	              className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-md border border-transparent bg-transparent text-zinc-300 transition hover:border-white/[0.12] hover:bg-white/[0.06] hover:text-zinc-50"
              aria-label={uploadingOutput ? "上传中" : "上传成品"}
              title={uploadingOutput ? "上传中" : "上传成品"}
            >
              {uploadingOutput ? (
                <span className="block h-4 w-4 rounded-full border-2 border-zinc-500 border-t-zinc-100 animate-spin" />
              ) : (
                <UploadIcon />
              )}
              <input
                type="file"
                accept={isVideo ? "video/*,image/*" : "image/*"}
                className="hidden"
                onChange={(event) => {
                  void onUploadOutput(event.currentTarget.files)
                event.currentTarget.value = ""
              }}
            />
          </label>
          )}
          {canRerun && (
            <button
              type="button"
              onClick={() => onRerun?.(node.id)}
              disabled={actionBusy}
              aria-label={node.status === "idle" || node.status === "queued" ? `生成${mediaRunTarget}` : `重新生成${mediaRunTarget}`}
              title={node.status === "idle" || node.status === "queued" ? `生成${mediaRunTarget}` : `重新生成${mediaRunTarget}`}
              className="ml-auto flex h-7 w-7 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 shadow-[0_10px_24px_rgba(255,255,255,0.12)] transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-45"
            >
              {rerunning ? <span className="h-3.5 w-3.5 rounded-full border-2 border-zinc-950/35 border-t-transparent animate-spin" /> : <ArrowUpIcon className="h-4 w-4" />}
            </button>
          )}
          </div>
        </div>
      )}

      {(mediaConfigError || displayError) && (
        <div className="rounded-md border border-red-400/20 bg-red-950/35 px-2.5 py-2 text-[11px] leading-4 text-red-200">
          {displayError || mediaConfigError}
        </div>
      )}
      {isText ? (
        <NodePanelTextHistoryButton
          node={node}
          input={nodeInputFields(node.input)}
          rawOutput={node.output}
          nodePrompt={node.prompt || ""}
        />
      ) : (
        <NodePanelMediaHistoryStrip node={node} />
      )}
      </div>
      {hasMediaParameterToggle && (
        <MediaParameterDialog
          open={mediaParameterDialogOpen}
          anchorRef={mediaParameterToggleRef}
          nodeType={isImage ? "image" : isVideo ? "video" : "audio"}
          draft={draft}
          selectedAudioMode={selectedAudioMode}
          imageAspectRatio={imageAspectRatio}
          imageResolution={imageResolutionValue}
          videoAspectOptions={videoAspectOptions}
          activeVideoAspectRatio={activeVideoAspectRatio}
          activeVideoResolution={activeVideoResolution}
          videoResolutionOptions={videoResolutionOptions}
          videoDurationValue={videoDurationValue}
          videoDurationConfigured={videoDurationConfigured}
          videoDurationMin={videoDurationMin}
          videoDurationMax={videoDurationMax}
          videoDurationStep={videoDurationStep}
          videoDurationRule={activeVideoDurationRule}
          onClose={() => setMediaParameterDialogOpen(false)}
          onChange={onChange}
          onImageAspectRatio={updateImageAspectRatio}
          onImageResolution={(resolution) => onChange({ resolution })}
        />
      )}
    </>
  )
}

function NodeMediaUploadSection({
  nodeType,
  uploading,
  disabled,
  onUpload,
}: {
  nodeType: string
  uploading: boolean
  disabled: boolean
  onUpload: (files: FileList | null) => void | Promise<void>
}) {
  if (nodeType !== "image" && nodeType !== "video") return null
  const isVideo = nodeType === "video"
  const label = uploading ? "上传中..." : isVideo ? "上传视频替换" : "上传图片替换"
  const accept = isVideo
    ? "video/mp4,video/webm,video/quicktime,.mp4,.webm,.mov,.m4v"
    : "image/png,image/jpeg,image/webp,image/gif,image/bmp,.png,.jpg,.jpeg,.webp,.gif,.bmp"

  return (
    <Section title="节点产物">
      <div className="flex flex-wrap items-center gap-2">
        <label
          className={`rounded-md border px-3 py-2 text-xs font-semibold transition ${
            disabled
              ? "cursor-not-allowed border-white/[0.07] bg-white/[0.03] text-zinc-500"
              : "cursor-pointer border-cyan-200/25 bg-cyan-300/12 text-cyan-100 hover:border-cyan-200/45 hover:bg-cyan-300/18"
          }`}
        >
          {label}
          <input
            type="file"
            accept={accept}
            disabled={disabled}
            className="hidden"
            onChange={(event) => {
              void onUpload(event.currentTarget.files)
              event.currentTarget.value = ""
            }}
          />
        </label>
        {uploading && (
          <span className="h-3.5 w-3.5 rounded-full border-2 border-cyan-100/55 border-t-transparent animate-spin" />
        )}
      </div>
    </Section>
  )
}

function GenericNodeDetails({
  node,
}: {
  node: NodeFull
}) {
  const inputObj = nodeInputFields(node.input)
  const outputObj = asObj(parseJson(node.output)) || {}
  const nodePrompt = typeof node.prompt === "string" ? node.prompt : ""
  const prompt = pickPromptText(nodePrompt, inputObj, outputObj)
  const outputText = nodeReadableText({
    type: node.type,
    input: inputObj,
    output: node.output,
    prompt: nodePrompt,
  })

  if (!prompt && !outputText) return null

  return (
    <div className="space-y-3">
      {prompt && (
        <Section title="提示词">
          <PromptBlock>{prompt}</PromptBlock>
        </Section>
      )}
      {outputText && (
        <Section title="输出内容">
          <PromptBlock>{outputText}</PromptBlock>
        </Section>
      )}
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
  const inObj = nodeInputFields(input)
  const outObj = asObj(parseJson(output)) || {}
  const topPrompt = typeof nodePrompt === "string" ? nodePrompt : ""
  const busy = nodeStatus === "running" || nodeStatus === "queued"

  if (type === "text") {
    const refs = pickReferences(inObj, outObj)
    return (
      <TextNodeStructuredDetails
        input={inObj}
        rawOutput={parseJson(output)}
        nodePrompt={topPrompt}
        refs={refs}
        projectId={projectId}
        setLightbox={setLightbox}
      />
    )
  }

  if (type === "image") {
    const media = collectMedia(outObj).filter((item) => item.kind === "image")
    const image = media[0]
    const prompt = pickPromptText(topPrompt, inObj, outObj)
    const refs = pickReferences(inObj, outObj)
    const spec = pickMediaSpec(outObj, inObj)
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
      </div>
    )
  }

  // ── character ──
  if (type === "character") {
    // 识别三种结构化 output: fusion 阶段、单人 character、多人 characters。
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

    // 当前节点 prompt 优先；output/fusion prompt 作为补充来源。
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
    const refsChar = pickReferences(inObj, outObj)

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
            <ImagePlaceholder label="未出图 - 点击重新生成" />
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsChar} projectId={projectId} setLightbox={setLightbox} />
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
    const refsScene = pickReferences(inObj, outObj)
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
            <ImagePlaceholder label="未出图 - 点击重新生成" />
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
    // 识别三种剧本结构: script、result.script、顶层 scenes/title。
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
                // dialogues/dialogue 字段统一展示。
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
    // 支持 outline 或 result.outline 结构。
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
    // 识别 plan 的顶层结构和 result 嵌套结构。
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
    const refs = pickReferences(inObj, outObj)
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
            <ImagePlaceholder label="未出图 - 点击重新生成" />
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
    const promptText =
      topPrompt ||
      (inObj.prompt as string) ||
      (promptStage?.prompt as string) ||
      (outObj.prompt as string) ||
      ""
    const spec = pickMediaSpec(outObj, inObj, imageStage as Record<string, unknown> | undefined)
    const refsShot = pickReferences(inObj, outObj)
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
            <ImagePlaceholder label="未出图 - 点击重新生成" />
            <MediaSpecBadges spec={spec} className="mt-1.5" />
            <ReferenceThumbStrip refs={refsShot} projectId={projectId} setLightbox={setLightbox} />
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
    const refs = pickReferences(inObj, outObj)
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

  // ── 未匹配的类型由通用详情区处理 ──
  return null
}

export default function NodeDetailPanel({
  nodeId,
  projectId,
  onClose,
  onRerun,
  onDelete,
  onRequestStoryRevision,
  actionDisabled = false,
  presentation = "anchored",
  anchorStyle,
  editRequestKey = null,
}: Props) {
  const storeProjectId = useProjectStore((s) => s.currentProject?.id)
  const updateCanvasNode = useCanvasStore((s) => s.updateNode)
  const canvasNodes = useCanvasStore((s) => s.nodes)
  const canvasEdges = useCanvasStore((s) => s.edges)
  const currentProjectId = projectId || storeProjectId
  const [data, setData] = useState<NodeFull | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lightbox, setLightbox] = useState<{ src: string; alt?: string } | null>(null)
  const [videoLightbox, setVideoLightbox] = useState<VideoLightboxState | null>(null)
  const [draft, setDraft] = useState<EditableNodeDraft>(EMPTY_DRAFT)
  const [draftDirty, setDraftDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [rerunning, setRerunning] = useState(false)
  const [uploadingRefs, setUploadingRefs] = useState(false)
  const [uploadingOutput, setUploadingOutput] = useState(false)
  const [switchingHistoryId, setSwitchingHistoryId] = useState<string | null>(null)
  const [detailReloadTick, setDetailReloadTick] = useState(0)
  const [mediaProviders, setMediaProviders] = useState<MediaProviderOption[]>([])
  const [llmProviders, setLlmProviders] = useState<LlmProviderOption[]>([])
  const [modelDefaults, setModelDefaults] = useState<RuntimeModelDefaults>({})
  const [videoProtocols, setVideoProtocols] = useState<VideoProtocolSummary[]>([])
  const [mediaConfigError, setMediaConfigError] = useState<string | null>(null)
  const mountedRef = useRef(false)
  const dataRef = useRef<NodeFull | null>(null)
  const draftRef = useRef<EditableNodeDraft>(EMPTY_DRAFT)
  const draftDirtyRef = useRef(false)
  const currentProjectIdRef = useRef<string | null | undefined>(currentProjectId)
  const selectedAudioModeRef = useRef<AudioProviderMode>("unknown")
  const referenceMentionCandidatesRef = useRef<ReferenceMentionCandidate[]>([])
  const mediaProvidersRef = useRef<MediaProviderOption[]>([])
  const llmProvidersRef = useRef<LlmProviderOption[]>([])
  const modelDefaultsRef = useRef<RuntimeModelDefaults>({})
  const videoProtocolsRef = useRef<VideoProtocolSummary[]>([])
  const savingRef = useRef(false)
  const persistDraftRef = useRef<(updateState?: boolean) => Promise<void>>(async () => undefined)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    dataRef.current = data
  }, [data])

  useEffect(() => {
    draftRef.current = draft
  }, [draft])

  useEffect(() => {
    draftDirtyRef.current = draftDirty
  }, [draftDirty])

  useEffect(() => {
    currentProjectIdRef.current = currentProjectId
  }, [currentProjectId])

  useEffect(() => {
    mediaProvidersRef.current = mediaProviders
  }, [mediaProviders])

  useEffect(() => {
    llmProvidersRef.current = llmProviders
  }, [llmProviders])

  useEffect(() => {
    modelDefaultsRef.current = modelDefaults
  }, [modelDefaults])

  useEffect(() => {
    videoProtocolsRef.current = videoProtocols
  }, [videoProtocols])

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
    setDraft(EMPTY_DRAFT)
    draftRef.current = EMPTY_DRAFT
    setDraftDirty(false)
    draftDirtyRef.current = false
    setSaving(false)
    setRerunning(false)
    setUploadingRefs(false)
    setUploadingOutput(false)
    setSwitchingHistoryId(null)
    setLightbox(null)
    setVideoLightbox(null)
  }, [nodeId])

  useEffect(() => {
    if (data && !draftDirty) {
      const next = concreteDraftStateFromNode(data, mediaProviders, llmProviders, modelDefaults)
      setDraft(next.draft)
      draftRef.current = next.draft
      setDraftDirty(next.dirty)
      draftDirtyRef.current = next.dirty
    }
  }, [data, draftDirty, mediaProviders, llmProviders, modelDefaults])

  useEffect(() => {
    if (!editRequestKey || !data || !EDITABLE_NODE_TYPES.has(data.type)) return
    const next = concreteDraftStateFromNode(data, mediaProviders, llmProviders, modelDefaults)
    setDraft(next.draft)
    draftRef.current = next.draft
    setDraftDirty(next.dirty)
    draftDirtyRef.current = next.dirty
  }, [data, editRequestKey, mediaProviders, llmProviders, modelDefaults])

  useEffect(() => {
    let cancelled = false
    const loadMediaProviders = async () => {
      try {
        const [result, videoProtocolResult] = await Promise.all([
          getRuntimeConfigFile<{
            parsed?: {
              media_providers?: MediaProviderOption[]
              llm_providers?: LlmProviderOption[]
              model_tier_defaults?: Record<string, string | null | undefined>
              model_assignments?: Record<string, string | null | undefined>
            }
          }>(true),
          getVideoProviderProtocols<{
            ok?: boolean
            protocols?: VideoProtocolSummary[]
          }>().catch(() => null),
        ])
        if (cancelled) return
        const providers = result.parsed?.media_providers || []
        const llm = result.parsed?.llm_providers || []
        setMediaProviders(providers)
        setLlmProviders(llm)
        setModelDefaults({
          model_tier_defaults: result.parsed?.model_tier_defaults || {},
          model_assignments: result.parsed?.model_assignments || {},
        })
        setVideoProtocols(videoProtocolResult?.protocols || [])
        setMediaConfigError(null)
      } catch (err) {
        if (cancelled) return
        setMediaConfigError(err instanceof Error ? err.message : String(err))
        setLlmProviders([])
        setModelDefaults({})
        setVideoProtocols([])
      }
    }
    void loadMediaProviders()
    const handleConfigUpdate = () => void loadMediaProviders()
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

  const style = getNodeStyle(data?.type)
  const status = data?.status ?? "idle"
  const statusBadge = STATUS_LABELS[status] ?? STATUS_LABELS.idle
  const renderState = renderStateFromNode(data)
  const displayError = data ? nodeDisplayError(data) : ""
  const mediaProgress = data ? mediaProgressFromOutput(data.output) : null

  const media = data ? collectMedia(data.output) : []
  const referenceMentionCandidates = useMemo(
    () => data
      ? buildReferenceMentionCandidates(
        data,
        draft,
        canvasNodes as CanvasGraphNode[],
        canvasEdges as CanvasGraphEdge[],
        currentProjectId,
      )
      : [],
    [data, draft, canvasNodes, canvasEdges, currentProjectId],
  )

  const isModal = presentation === "modal"
  const isAnchored = presentation === "anchored"
  const panelClass = isModal
    ? "openreel-node-detail-panel nodrag nowheel fixed left-1/2 top-1/2 z-[90] flex max-h-[calc(100dvh-28px)] w-[calc(100vw-20px)] flex-col overflow-hidden rounded-lg border border-white/[0.09] bg-[#0f131b]/96 shadow-[0_28px_90px_rgba(0,0,0,0.66)] backdrop-blur-xl sm:max-h-[84vh] sm:w-[min(900px,calc(100vw-72px))]"
    : isAnchored
      ? "openreel-node-detail-panel nodrag nowheel absolute z-[90] flex flex-col overflow-hidden rounded-xl border border-white/[0.12] bg-[#111821]/98 shadow-[0_24px_80px_rgba(0,0,0,0.62)] ring-1 ring-cyan-300/10 backdrop-blur-xl"
      : "openreel-node-detail-panel nodrag nowheel absolute bottom-3 left-3 right-3 top-3 z-30 flex flex-col overflow-hidden rounded-lg border border-white/[0.09] bg-[#0f131b]/96 shadow-2xl backdrop-blur sm:left-auto sm:w-[380px]"
  const canRerunNode = Boolean(data && onRerun && MEDIA_RERUN_NODE_TYPES.has(data.type))
  const mediaRunTarget = data?.type === "text" ? "文本" : data?.type === "video" ? "视频" : data?.type === "audio" ? "音频" : "图片"
  const mediaRunLabel = data && (data.status === "idle" || data.status === "queued")
    ? `生成${mediaRunTarget}`
    : `重新生成${mediaRunTarget}`
  const footerClass = "border-t border-white/[0.08] bg-[#111722] px-3 py-3 shadow-[0_-14px_26px_rgba(0,0,0,0.32)] shrink-0 sm:px-4"

  const canEdit = Boolean(data && EDITABLE_NODE_TYPES.has(data.type))
  const actionBusy = actionDisabled || rerunning || uploadingOutput
  const selectedAudioProvider = data?.type === "audio"
    ? resolveAudioProvider(draft.model, mediaProvidersForKind(mediaProviders, "audio"))
    : undefined
  const selectedAudioMode = data?.type === "audio"
    ? audioProviderModeFromProvider(selectedAudioProvider)
    : "unknown"

  useEffect(() => {
    selectedAudioModeRef.current = selectedAudioMode
  }, [selectedAudioMode])

  useEffect(() => {
    referenceMentionCandidatesRef.current = referenceMentionCandidates
  }, [referenceMentionCandidates])

  const updateDraft = (patch: Partial<EditableNodeDraft>) => {
    setDraft((current) => {
      const next = { ...current, ...patch }
      draftRef.current = next
      if (!editableDraftEquals(current, next)) {
        draftDirtyRef.current = true
        setDraftDirty(true)
      }
      return next
    })
  }

  const uploadReferenceFiles = async (files: FileList | File[] | null) => {
    if (!files?.length || !currentProjectId) return
    let selectedFiles = Array.from(files)
    if (dataRef.current?.type === "video") {
      const limit = videoReferenceLimitForDraft(draftRef.current, mediaProvidersRef.current, videoProtocolsRef.current)
      if (limit !== undefined) {
        const currentCount = referenceDisplayCount(
          draftRef.current.reference_images,
          referenceMentionCandidateRefs(referenceMentionCandidatesRef.current),
          currentProjectId,
        )
        const remaining = Math.max(0, limit - currentCount)
        selectedFiles = selectedFiles.slice(0, remaining)
      }
    }
    if (selectedFiles.length === 0) return
    setUploadingRefs(true)
    setError(null)
    try {
      const uploaded = await Promise.all(selectedFiles.map((file) => uploadFile(currentProjectId, file)))
      const refs = uploaded
        .map((item) => item.rel_path || item.url || item.mention || "")
        .filter(Boolean)
      setDraft((current) => ({
        ...current,
        reference_images: Array.from(new Set([...current.reference_images, ...refs])),
      }))
      if (refs.length > 0) setDraftDirty(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUploadingRefs(false)
    }
  }

  const uploadNodeMediaOutput = async (files: FileList | null) => {
    if (!files?.length || !currentProjectId || !data) return
    if (data.type !== "image" && data.type !== "video") return
    setUploadingOutput(true)
    setError(null)
    try {
      const result = await uploadProjectNodeMedia<NodeFull>(currentProjectId, data.id, files[0])
      setData(result)
      const next = concreteDraftStateFromNode(result, mediaProviders, llmProviders, modelDefaults)
      setDraft(next.draft)
      setDraftDirty(next.dirty)
      updateCanvasNode(result.id, canvasPatchFromNode(result))
      setDetailReloadTick((tick) => tick + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUploadingOutput(false)
    }
  }

  const persistDraft = useCallback(async (updateState = true) => {
    const node = dataRef.current
    const detailProjectId = currentProjectIdRef.current
    if (!node || !detailProjectId || !draftDirtyRef.current || savingRef.current) return
    savingRef.current = true
    if (updateState && mountedRef.current) {
      setSaving(true)
      setError(null)
    }
    try {
      const result = await updateProjectNodeDetails<NodeFull>(
        detailProjectId,
        node.id,
        payloadFromDraft(
          node,
          draftRef.current,
          selectedAudioModeRef.current,
          referenceMentionCandidatesRef.current,
          node.type === "video"
            ? videoReferenceLimitForDraft(draftRef.current, mediaProvidersRef.current, videoProtocolsRef.current)
            : undefined,
        ),
      )
      updateCanvasNode(result.id, canvasPatchFromNode(result))
      const next = concreteDraftStateFromNode(
        result,
        mediaProvidersRef.current,
        llmProvidersRef.current,
        modelDefaultsRef.current,
      )
      dataRef.current = result
      draftRef.current = next.draft
      draftDirtyRef.current = next.dirty
      if (updateState && mountedRef.current) {
        setData(result)
        setDraft(next.draft)
        setDraftDirty(next.dirty)
      }
    } catch (err) {
      if (updateState && mountedRef.current) {
        setError(err instanceof Error ? err.message : String(err))
      } else {
        console.warn("Failed to auto-save node draft", err)
      }
    } finally {
      savingRef.current = false
      if (updateState && mountedRef.current) setSaving(false)
    }
  }, [updateCanvasNode])

  useEffect(() => {
    persistDraftRef.current = persistDraft
  }, [persistDraft])

  useEffect(() => {
    return () => {
      void persistDraftRef.current(false)
    }
  }, [nodeId])

  const saveDraft = async () => {
    await persistDraft(true)
  }

  const closeWithAutoSave = () => {
    void persistDraft(false)
    onClose()
  }

  const handlePanelBlur = (event: FocusEvent<HTMLDivElement>) => {
    const nextTarget = event.relatedTarget
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) return
    void persistDraft(true)
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
      const next = concreteDraftStateFromNode(result, mediaProviders, llmProviders, modelDefaults)
      setDraft(next.draft)
      setDraftDirty(next.dirty)
      updateCanvasNode(result.id, canvasPatchFromNode(result))
      setDetailReloadTick((tick) => tick + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSwitchingHistoryId(null)
    }
  }

  const runNodeFromContext = async (targetNodeId: string) => {
    if (!data) return
    await persistDraft(true)
    setError(null)
    setRerunning(true)
    setData((current) => current ? { ...current, status: "running", error_message: null } : current)
    updateCanvasNode(targetNodeId, { status: "running", error: undefined, error_message: undefined })
    try {
      await Promise.resolve(onRerun?.(targetNodeId))
      setDetailReloadTick((tick) => tick + 1)
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setError(message)
      setData((current) => current ? { ...current, status: "failed", error_message: message } : current)
      updateCanvasNode(targetNodeId, { status: "failed", error: message, error_message: message })
    } finally {
      setRerunning(false)
    }
  }

  if (isAnchored) {
    return (
      <>
        <motion.div
          initial={{ opacity: 0, y: -6, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -6, scale: 0.98 }}
          transition={{ duration: 0.16, ease: "easeOut" }}
          className="openreel-node-detail-panel nodrag nowheel absolute z-[90] flex flex-col overflow-hidden rounded-xl text-zinc-100"
          style={anchorStyle}
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          onBlurCapture={handlePanelBlur}
        >
          {error && (
            <div className="mb-2 rounded-lg border border-red-400/20 bg-red-950/55 px-3 py-2 text-xs text-red-200 shadow-xl shadow-black/30">
              {error}
            </div>
          )}
          <div className="min-h-0 flex-1">
            {data && !loading ? (
              <NodeCanvasContextPanel
                node={data}
                draft={draft}
                mediaProviders={mediaProviders}
                llmProviders={llmProviders}
                modelDefaults={modelDefaults}
                videoProtocols={videoProtocols}
                mediaConfigError={mediaConfigError}
                projectId={currentProjectId}
                canvasNodes={canvasNodes as CanvasGraphNode[]}
                referenceMentionCandidates={referenceMentionCandidates}
                dirty={draftDirty}
                saving={saving}
                uploading={uploadingRefs}
                uploadingOutput={uploadingOutput}
                rerunning={rerunning}
                actionDisabled={actionDisabled}
                displayError={displayError}
                setLightbox={setLightbox}
                onChange={updateDraft}
                onUploadRefs={uploadReferenceFiles}
                onUploadOutput={uploadNodeMediaOutput}
                onRerun={runNodeFromContext}
                onRequestStoryRevision={(targetNodeId) => {
                  void persistDraft(false)
                  onRequestStoryRevision?.(targetNodeId)
                }}
              />
            ) : (
              <div className="rounded-xl border border-white/[0.1] bg-[#252525]/96 px-3 py-6 text-center text-xs text-zinc-500 shadow-[0_24px_70px_rgba(0,0,0,0.46)] backdrop-blur-xl">加载节点...</div>
            )}
          </div>
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
        initial={isModal ? { opacity: 0, scale: 0.96, x: "-50%", y: "-50%" } : isAnchored ? { opacity: 0, y: -8, scale: 0.98 } : { x: 32, opacity: 0 }}
        animate={isModal ? { opacity: 1, scale: 1, x: "-50%", y: "-50%" } : isAnchored ? { opacity: 1, y: 0, scale: 1 } : { x: 0, opacity: 1 }}
        exit={isModal ? { opacity: 0, scale: 0.96, x: "-50%", y: "-50%" } : isAnchored ? { opacity: 0, y: -8, scale: 0.98 } : { x: 32, opacity: 0 }}
        transition={{ duration: 0.18, ease: "easeOut" }}
        className={panelClass}
        style={isAnchored ? anchorStyle : undefined}
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
        onPointerDown={(e) => e.stopPropagation()}
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
          {canEdit && draftDirty && (
            <span className="shrink-0 rounded-md border border-cyan-300/25 bg-cyan-300/10 px-2 py-1 text-[10px] font-medium text-cyan-100">
              未保存
            </span>
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
        <div className="flex-1 space-y-3 overflow-y-auto bg-[#090c12] px-3.5 pb-24 pt-3.5 sm:px-4">
          {loading && (
            <div className="text-xs text-gray-500 py-8 text-center">加载节点详情…</div>
          )}

          {error && (
            <div className="rounded border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
              错误：{error}
            </div>
          )}

          {data && !loading && (
            <>
              {canEdit && (
                <NodeEditView
                node={data}
                draft={draft}
                mediaProviders={mediaProviders}
                llmProviders={llmProviders}
                modelDefaults={modelDefaults}
                videoProtocols={videoProtocols}
                mediaConfigError={mediaConfigError}
                projectId={currentProjectId}
                canvasNodes={canvasNodes as CanvasGraphNode[]}
                referenceMentionCandidates={referenceMentionCandidates}
                saving={saving}
                dirty={draftDirty}
                uploading={uploadingRefs}
                setLightbox={setLightbox}
                onChange={updateDraft}
                onUploadRefs={uploadReferenceFiles}
                onSave={saveDraft}
              />
              )}
              <NodeMediaUploadSection
                nodeType={data.type}
                uploading={uploadingOutput}
                disabled={actionDisabled || uploadingOutput || data.status === "running" || data.status === "queued"}
                onUpload={uploadNodeMediaOutput}
              />
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
                <GenericNodeDetails node={data} />
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

              {data.output == null && data.input == null && !data.prompt && !displayError && media.length === 0 && (
                <div className="text-xs text-gray-500 italic py-6 text-center">
                  {status === "running" || status === "queued" || status === "idle"
                    ? "节点尚未产出内容…"
                    : "无可展示内容"}
                </div>
              )}

              <NodeAdvancedSurface
                node={data}
                displayError={displayError}
                mediaProgress={mediaProgress}
                switchingHistoryId={switchingHistoryId}
                onSwitchHistory={switchHistoryVersion}
              />
            </>
          )}
        </div>

        {/* Footer */}
        {data && (status !== "running" || rerunning) && canRerunNode && (
          <div className={footerClass}>
            {canRerunNode && (
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
	                {rerunning ? "运行中..." : mediaRunLabel}
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
