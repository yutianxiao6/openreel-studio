"use client"

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent, type PointerEvent as ReactPointerEvent, type TouchEvent as ReactTouchEvent } from "react"
import ReactFlow, {
  Background,
  ConnectionLineType,
  ConnectionMode,
  Controls,
  MiniMap,
  MarkerType,
  Position,
  SelectionMode,
  applyEdgeChanges as applyFlowEdgeChanges,
  applyNodeChanges as applyFlowNodeChanges,
  getBezierPath,
  useReactFlow,
  type Connection,
  type ConnectionLineComponentProps,
  type Edge as FlowEdge,
  type EdgeChange,
  type NodeProps,
  type OnConnectStartParams,
  type Node as FlowNode,
  type NodeChange,
  type ReactFlowInstance,
} from "reactflow"
import "reactflow/dist/style.css"
import { AnimatePresence } from "framer-motion"
import { useCanvasStore } from "@/stores/canvasStore"
import { useProjectStore } from "@/stores/projectStore"
import { useChatStore } from "@/stores/chatStore"
import {
  CANVAS_REFRESH_EVENT,
  WORKFLOW_REFRESH_EVENT,
  callTool,
  createPanoramaCapture,
  createProjectEdge,
  createProjectNode,
  deleteProjectMediaHistoryItem,
  deleteProjectEdge,
  deleteProjectNode,
  deleteProjectNodes,
  deleteProjectWorkflowRuntime,
  getApiBaseSync,
  listProjectMediaHistory,
  listWorkflowNodeTypes,
  listWorkflowTemplates,
  materializeProjectWorkflow,
  pauseProjectWorkflowRun,
  previewProjectWorkflow,
  requestWorkflowRefresh,
  runProjectWorkflowAllSteps,
  runProjectWorkflowNextStep,
  runProjectWorkflowStep,
  saveWorkflowTemplate,
  setProjectActiveWorkflow,
  downloadWorkflowTemplatePackage,
  getProjectNodeDetails,
  getProjectNodes,
  getRuntimeConfigFile,
  getVideoProviderProtocols,
  resolveMediaUrl,
  restoreProjectMediaHistoryItem,
  restoreProjectCanvasSnapshot,
  restoreBuiltinWorkflowTemplate,
  runProjectMediaOperation,
  updateProjectNodeDetails,
  uploadProjectNodeMedia,
  updateNodePosition,
  type CanvasRefreshOptions,
  type WorkflowRefreshOptions,
  type CanvasEdgeSnapshot,
  type CanvasNodeSnapshot,
  type CanvasNodeType,
  type ProjectMediaHistoryItem,
  type ProjectActiveWorkflow,
  type ProjectWorkflowRuntime,
  type ProjectWorkflowRuntimeStep,
  type WorkflowTemplateStepSummary,
  type WorkflowTemplateSummary,
  type WorkflowNodeTypeDefinition,
} from "@/lib/api"
import { nodeTypes } from "./nodes"
import NodeDetailPanel from "./NodeDetailPanel"
import ImageEditPanel from "./ImageEditPanel"
import VideoEditPanel, { type VideoEditPanelMediaNode } from "./VideoEditPanel"
import CanvasGroupLayer, { type CanvasViewport } from "./CanvasGroupLayer"
import PanoramaViewer, { type PanoramaCaptureMode } from "./PanoramaViewer"
import MediaHistoryDrawer, { MEDIA_HISTORY_LABEL, type MediaHistoryFilter } from "./MediaHistoryDrawer"
import WorkflowRunOutputView, {
  workflowRuntimeDetailOutputText,
  type WorkflowRunDetailOutputItem,
} from "./WorkflowRunOutputView"
import { cn } from "@/lib/utils"
import { canvasNodeDisplayText } from "@/lib/nodeDisplay"
import {
  defaultVideoResolutionForProvider,
  resolveVideoProvider,
  videoReferenceImageLimitForProvider,
  videoSupportedRatiosForProvider,
  videoSupportedResolutionsForProvider,
  type MediaProviderSummary,
  type VideoProtocolSummary,
} from "@/lib/videoProtocolLimits"
import type { WorkspaceView } from "@/components/workspace/WorkspaceViewTabs"

interface GridDropTarget {
  gridNodeId: string
  cellId: string
  element: HTMLElement
}

interface CanvasUndoRecord {
  id: string
  label: string
  at: number
  undo: () => Promise<void>
}

interface PendingConnectionDraft {
  nodeId: string
  handleId: string | null
  handleType: "source" | "target" | null
}

interface PendingConnectionPreviewLine {
  fromX: number
  fromY: number
  toX: number
  toY: number
}

interface CanvasCreateMenuState {
  x: number
  y: number
  flowX: number
  flowY: number
  connectFrom?: PendingConnectionDraft
  previewLine?: PendingConnectionPreviewLine
}

interface CanvasAlignmentGuide {
  orientation: "vertical" | "horizontal"
  position: number
  start: number
  end: number
}

interface NodeBounds {
  id: string
  left: number
  right: number
  top: number
  bottom: number
  centerX: number
  centerY: number
  width: number
  height: number
}

const NODE_CONTEXT_PANEL_MARGIN = 14
const NODE_CONTEXT_PANEL_GAP = 10
const NODE_CONTEXT_PANEL_MIN_HEIGHT = 160
const NODE_CONTEXT_PANEL_PREFERRED_HEIGHT = 380
const NODE_CONTEXT_PANEL_MAX_HEIGHT = 420
const NODE_CONTEXT_PANEL_MEDIA_PREFERRED_HEIGHT = 500
const NODE_CONTEXT_PANEL_MEDIA_MAX_HEIGHT = 540
const NODE_CONTEXT_PANEL_MIN_WIDTH = 420
const NODE_CONTEXT_PANEL_IDEAL_WIDTH = 560
const NODE_CONTEXT_PANEL_MAX_WIDTH = 640

interface WorkflowCanvasProps {
  workspaceView?: WorkspaceView
  onWorkspaceViewChange?: (view: WorkspaceView) => void
}

interface NodeActionMenuState {
  x: number
  y: number
  nodeId: string
  title: string
  imageUrl?: string
}

interface AssetCategoryResult {
  project?: Array<{ episode?: string; kind?: string; count?: number }>
  shared?: Array<{ kind?: string; category?: string; count?: number }>
  error?: string
}

interface AssetSaveForm {
  library: "shared" | "project"
  kind: string
  category: string
  episode: string
  name: string
}

interface NodeAssetSaveRequest {
  nodeId: string
  title: string
  publicId?: string | number | null
}

interface NodeImageEditRequest {
  nodeId: string
  title: string
  imageUrl?: string
}

interface NodeVideoEditRequest {
  nodeId: string
  title: string
  videoUrl?: string
}

interface NodePreviewRequest {
  nodeId: string
  type?: string
  title?: string
  input?: unknown
  output?: unknown
  prompt?: string
  preview?: Record<string, unknown>
  previewText?: string
  readOnly?: boolean
}

interface NodePanoramaCreateRequest {
  nodeId: string
  title: string
  publicId?: string | number | null
  imageUrl?: string
}

interface PanoramaViewerRequest {
  nodeId: string
  title: string
  imageUrl: string
}

interface LongPressState {
  pointerId: number
  x: number
  y: number
  kind: "pane" | "node"
  nodeId?: string
  timer: number
  fired: boolean
}

const LONG_PRESS_MS = 560
const LONG_PRESS_MOVE_TOLERANCE = 28
const CANVAS_BLANK_CLICK_TOLERANCE = 6
const ALIGNMENT_SNAP_SCREEN_PX = 8
const ALIGNMENT_SNAP_SCREEN_PX_COARSE = 14
const ALIGNMENT_GUIDE_MARGIN = 44
const ALIGNMENT_DEFAULT_NODE_WIDTH = 260
const ALIGNMENT_DEFAULT_NODE_HEIGHT = 176
const ASSET_LIBRARY_KINDS = ["character", "scene", "storyboard"]
const ASSET_LIBRARY_KIND_LABEL: Record<string, string> = {
  character: "人物",
  scene: "场景",
  storyboard: "分镜",
}
const GENERIC_IMAGE_TITLES = new Set(["", "未命名", "未命名图片", "图片节点"])
const PANORAMA_PROMPT = [
  "参考当前图片生成一张 360 度 equirectangular 全景图。",
  "输出必须是 2:1 横向全景图，左右边界无缝衔接，适合在全景查看器中观看。",
  "保留参考图的场景风格、主体空间、材质、光线和时间氛围，并合理补全画面左右、背后、天顶和地面环境。",
  "避免文字、水印、边框、重复物体、断裂透视和明显拼接痕迹。",
].join("\n")

const CANVAS_NODE_CREATE_ITEMS: Array<{
  type: CanvasNodeType
  label: string
  description: string
  badge: string
  accentClass: string
}> = [
  {
    type: "text",
    label: "文本",
    description: "剧情、分段、设定或提示词草稿",
    badge: "T",
    accentClass: "bg-sky-400/14 text-sky-200 ring-sky-300/20",
  },
  {
    type: "image",
    label: "图片",
    description: "人物、场景、分镜或参考图",
    badge: "I",
    accentClass: "bg-emerald-400/14 text-emerald-200 ring-emerald-300/20",
  },
  {
    type: "video",
    label: "视频",
    description: "片段、成片或上传视频",
    badge: "V",
    accentClass: "bg-fuchsia-400/14 text-fuchsia-200 ring-fuchsia-300/20",
  },
  {
    type: "audio",
    label: "音频",
    description: "旁白、音乐或声音素材",
    badge: "A",
    accentClass: "bg-amber-400/14 text-amber-200 ring-amber-300/20",
  },
]

const CANVAS_CREATE_MENU_WIDTH = 286
const CANVAS_CREATE_MENU_HEIGHT = 318
const CANVAS_CONNECT_CREATE_MENU_HEIGHT = 318

function findAvailableNodePosition(
  initial: { x: number; y: number },
  nodes: FlowNode[],
  ignoreNodeId?: string,
): { x: number; y: number } {
  const padding = 18
  const width = ALIGNMENT_DEFAULT_NODE_WIDTH
  const height = ALIGNMENT_DEFAULT_NODE_HEIGHT
  const occupied = nodes
    .filter((node) => node.id !== ignoreNodeId)
    .map(nodeBounds)
  for (let index = 0; index < 12; index += 1) {
    const candidate = {
      x: Math.round(initial.x + (index % 3) * 28),
      y: Math.round(initial.y + Math.floor(index / 3) * 54),
    }
    const left = candidate.x - padding
    const top = candidate.y - padding
    const right = candidate.x + width + padding
    const bottom = candidate.y + height + padding
    const overlaps = occupied.some((bounds) => (
      left < bounds.right + padding
      && right > bounds.left - padding
      && top < bounds.bottom + padding
      && bottom > bounds.top - padding
    ))
    if (!overlaps) return candidate
  }
  return { x: Math.round(initial.x), y: Math.round(initial.y) }
}

const WORKFLOW_NODE_TYPE_LABEL: Record<string, string> = {
  text: "文本",
  image: "图片",
  video: "视频",
  audio: "音频",
}
const WORKFLOW_PHASE_LABELS: Record<string, string> = {
  input: "输入",
  inputs: "输入",
  intake: "输入",
  brief: "输入",
  plan: "规划",
  planning: "规划",
  structure: "结构",
  script: "剧本",
  story: "剧本",
  episode_script: "剧本",
  segment_script: "分段剧本",
  character: "人物",
  characters: "人物",
  character_reference: "人物参考",
  scene: "场景",
  scenes: "场景",
  scene_reference: "场景参考",
  storyboard: "分镜",
  frames: "分镜",
  frame: "分镜",
  video_prompt: "视频提示词",
  final_video_prompt: "视频提示词",
  image: "图片",
  video: "成片",
  audio: "音频",
  review: "检查",
}
const EMPTY_WORKFLOW_STEPS: WorkflowTemplateStepSummary[] = []

interface WorkflowStepNodeState {
  nodeId: string
  nodeIds: string[]
  title: string
  status: string
  count: number
  runningCount: number
  failedCount: number
  completedCount: number
  runCount?: number
  resolvedInputCount?: number
  outputCount?: number
  artifactCount?: number
  updatedAt?: string
  lastRunSummary?: string
  lastRunDetail?: string
  outputPreview?: string
  virtual?: boolean
}

interface WorkflowArtifactPreview {
  artifactRef: string
  source: "artifact" | "imported"
  workflow?: Record<string, unknown>
  id: string
  name: string
  description: string
  inputs: string[]
  requiredInputs: string[]
  stepCount: number
  dimensionCount: number
  deferredGroupCount: number
  dimensions: string[]
  deferredGroups: Array<{ id?: string; title?: string; status?: string }>
  steps: WorkflowTemplateStepSummary[]
}

interface WorkflowResolvedPreview {
  key: string
  steps: WorkflowTemplateStepSummary[]
}

interface WorkflowInputDraftSpec {
  type?: string
  label?: string
  description?: string
  default?: string
  options?: Array<{ value: string; label: string }>
}

interface WorkflowInputPreset {
  id: string
  label: string
  type: string
  description: string
  default?: string
  required?: boolean
}

type CanvasEdgeDisplayMode = "clean" | "selected" | "all"

interface WorkflowPhaseGroup {
  key: string
  title: string
  steps: WorkflowTemplateStepSummary[]
  completedCount: number
  runningCount: number
  failedCount: number
  canvasOutputCount: number
  runtimeOnlyCount: number
}

interface WorkflowAddStepOptions {
  afterStepId?: string
  position?: { x: number; y: number }
  detached?: boolean
}

interface WorkflowEditorCreateMenu {
  x: number
  y: number
  position: { x: number; y: number }
  sourceStepId?: string
}

type WorkflowAuthoringKind = "text" | "object" | "collection" | "loop" | "image" | "video" | "audio" | "plugin"
type WorkflowInspectorTab = "properties" | "io" | "prompt" | "run"

const WORKFLOW_SPEC_VERSION = "openreel.workflow.v2"

const WORKFLOW_AUTHORING_KIND_OPTIONS: Array<{ value: WorkflowAuthoringKind; label: string }> = [
  { value: "text", label: "文本" },
  { value: "object", label: "结构化对象" },
  { value: "collection", label: "结构化列表" },
  { value: "loop", label: "循环" },
  { value: "image", label: "图片" },
  { value: "video", label: "视频" },
  { value: "audio", label: "音频" },
  { value: "plugin", label: "插件" },
]

const WORKFLOW_INSPECTOR_TABS: Array<{ value: WorkflowInspectorTab; label: string }> = [
  { value: "properties", label: "设置" },
  { value: "io", label: "上下游" },
  { value: "prompt", label: "提示词" },
  { value: "run", label: "结果" },
]

const WORKFLOW_INPUT_TYPE_OPTIONS = [
  { value: "text", label: "单行文本" },
  { value: "long_text", label: "大段文字" },
  { value: "number", label: "数字" },
  { value: "integer", label: "整数" },
  { value: "boolean", label: "是/否" },
  { value: "enum", label: "单选" },
  { value: "image", label: "图片" },
  { value: "video", label: "视频节点" },
  { value: "audio", label: "音频" },
  { value: "json", label: "列表" },
]

const WORKFLOW_FORM_INPUT_PRESETS: WorkflowInputPreset[] = [
  { id: "plot", label: "剧情内容", type: "long_text", description: "输入完整剧情、梗概或要改编的文本。", required: true },
  { id: "total_duration_seconds", label: "视频总时长", type: "number", description: "输入整个视频的总秒数，例如 60。", required: true },
  { id: "segment_seconds", label: "分段秒数", type: "number", description: "每一段多少秒，默认 15。", default: "15" },
  { id: "style", label: "视觉风格", type: "text", description: "例如：写实、电影感、冷色调、国风。", required: false },
  { id: "aspect_ratio", label: "画面比例", type: "text", description: "例如：9:16、16:9。", default: "9:16" },
]

const WORKFLOW_FIELD_TYPE_OPTIONS = [
  { value: "string", label: "文本" },
  { value: "number", label: "数字" },
  { value: "boolean", label: "布尔" },
  { value: "object", label: "对象" },
  { value: "array", label: "数组" },
]

const WORKFLOW_REFERENCE_ROLE_OPTIONS = [
  { value: "vision", label: "让提示词模型看图" },
  { value: "reference", label: "作为媒体生成参考" },
  { value: "vision_reference", label: "看图并作为生成参考" },
  { value: "source", label: "直接采用现有媒体" },
]

const WORKFLOW_CONDITION_OPERATOR_OPTIONS = [
  { value: "", label: "不设置" },
  { value: "empty", label: "为空" },
  { value: "not_empty", label: "不为空" },
  { value: "eq", label: "等于" },
  { value: "ne", label: "不等于" },
  { value: "lt", label: "小于" },
  { value: "lte", label: "小于等于" },
  { value: "gt", label: "大于" },
  { value: "gte", label: "大于等于" },
]

type WorkflowConditionInputKind = "number" | "boolean" | "collection" | "text"

function workflowInputTypeCategory(inputType?: string): WorkflowConditionInputKind {
  const normalizedType = String(inputType || "text").trim().toLowerCase()
  if (["number", "integer", "int", "float", "decimal"].includes(normalizedType)) return "number"
  if (["boolean", "bool", "checkbox"].includes(normalizedType)) return "boolean"
  if (["object", "array", "json", "list"].includes(normalizedType)) return "collection"
  return "text"
}

function workflowCleanIdList(values: unknown): string[] {
  if (!Array.isArray(values)) return []
  const result: string[] = []
  for (const value of values) {
    const id = workflowStringValue(value)
    if (id && !result.includes(id)) result.push(id)
  }
  return result
}

function workflowInputOptionsFromRaw(raw: Record<string, unknown>): Array<{ value: string; label: string }> | undefined {
  const source = raw.options || raw.choices || raw.enum
  const items = Array.isArray(source) ? source : []
  const result = items
    .map((item) => {
      if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") {
        const value = String(item)
        return { value, label: value }
      }
      const option = asWorkflowObject(item)
      const value = workflowStringValue(option?.value ?? option?.id ?? option?.key ?? option?.name)
      if (!value) return null
      return {
        value,
        label: workflowStringValue(option?.label ?? option?.title ?? option?.name) || value,
      }
    })
    .filter((item): item is { value: string; label: string } => Boolean(item))
  return result.length > 0 ? result : undefined
}

function workflowInputTypeUsesOptions(inputType: unknown): boolean {
  return String(inputType || "").trim().toLowerCase() === "enum"
}

function workflowConditionOperatorOptionsForInputType(inputType?: string): typeof WORKFLOW_CONDITION_OPERATOR_OPTIONS {
  const kind = workflowInputTypeCategory(inputType)
  const allowed = new Set(
    kind === "number"
      ? ["", "empty", "not_empty", "eq", "ne", "lt", "lte", "gt", "gte"]
      : kind === "boolean"
        ? ["", "eq", "ne"]
        : kind === "collection"
          ? ["", "empty", "not_empty"]
          : ["", "empty", "not_empty", "eq", "ne"],
  )
  return WORKFLOW_CONDITION_OPERATOR_OPTIONS.filter((item) => allowed.has(item.value))
}

function workflowDefaultConditionOperatorForInputType(inputType?: string): string {
  return workflowInputTypeCategory(inputType) === "boolean" ? "eq" : "not_empty"
}

function workflowDefaultConditionCompareValueForInputType(inputType?: string, currentValue = ""): string {
  const value = currentValue.trim()
  const kind = workflowInputTypeCategory(inputType)
  if (kind === "boolean") return /^(true|false)$/i.test(value) ? value.toLowerCase() : "true"
  if (kind === "number") return /^-?\d+(?:\.\d+)?$/.test(value) ? value : "0"
  return currentValue
}

function workflowConditionOperatorIsAllowed(operator: string, inputType?: string): boolean {
  return workflowConditionOperatorOptionsForInputType(inputType).some((item) => item.value === operator)
}

const WORKFLOW_ADVANCED_WORKFLOW_KEYS = ["ui", "extensions"]

function asWorkflowObject(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function workflowCloneValue<T>(value: T): T {
  if (value === undefined || value === null) return value
  return JSON.parse(JSON.stringify(value)) as T
}

function workflowHasValue(value: unknown): boolean {
  if (value === undefined || value === null || value === "") return false
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === "object") return Object.keys(value as Record<string, unknown>).length > 0
  return true
}

function workflowStableStringify(value: unknown): string {
  return JSON.stringify(value, (_key, item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return item
    const sorted: Record<string, unknown> = {}
    for (const key of Object.keys(item as Record<string, unknown>).sort()) {
      sorted[key] = (item as Record<string, unknown>)[key]
    }
    return sorted
  }) ?? ""
}

function workflowJsonEditorText(value: unknown): string {
  if (!workflowHasValue(value)) return ""
  if (typeof value === "string") return JSON.stringify(value, null, 2)
  return JSON.stringify(value, null, 2)
}

function workflowListText(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean).join(", ")
  return typeof value === "string" ? value : ""
}

function workflowTextToList(value: string): string[] | undefined {
  const items = value
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  return items.length > 0 ? Array.from(new Set(items)) : undefined
}

function workflowAdvancedDraftFromWorkflow(sourceWorkflow?: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  if (!sourceWorkflow) return result
  for (const key of WORKFLOW_ADVANCED_WORKFLOW_KEYS) {
    if (workflowHasValue(sourceWorkflow[key])) result[key] = workflowCloneValue(sourceWorkflow[key])
  }
  return result
}

function workflowSetAdvancedDraftField(
  current: Record<string, unknown>,
  key: string,
  value: unknown,
): Record<string, unknown> {
  const next = { ...current }
  if (workflowHasValue(value)) next[key] = workflowCloneValue(value)
  else delete next[key]
  return next
}

function WorkflowJsonEditorField({
  label,
  value,
  onChange,
  readOnly,
  rows = 4,
}: {
  label: string
  value: unknown
  onChange: (value: unknown) => void
  readOnly?: boolean
  rows?: number
}) {
  const serialized = workflowJsonEditorText(value)
  const [text, setText] = useState(serialized)
  const [error, setError] = useState("")

  useEffect(() => {
    setText(serialized)
    setError("")
  }, [serialized])

  const commit = (nextText: string) => {
    setText(nextText)
    const trimmed = nextText.trim()
    if (!trimmed) {
      setError("")
      onChange(undefined)
      return
    }
    try {
      onChange(JSON.parse(trimmed))
      setError("")
    } catch {
      setError("JSON 格式错误")
    }
  }

  return (
    <label className="block text-[10px] font-medium text-zinc-500">
      {label}
      <textarea
        value={text}
        readOnly={readOnly}
        rows={rows}
        onChange={(event) => commit(event.target.value)}
        className={cn(
          "mt-1 w-full resize-none rounded-md border bg-[#090e15] px-2 py-1.5 font-mono text-[11px] leading-4 text-zinc-100 outline-none placeholder:text-zinc-600 read-only:cursor-default read-only:opacity-70",
          error ? "border-red-300/35 focus:border-red-300/55" : "border-white/10 focus:border-cyan-200/45",
        )}
      />
      {error && <div className="mt-1 text-[10px] text-red-200/80">{error}</div>}
    </label>
  )
}

function workflowNodeType(value: unknown): CanvasNodeType {
  const text = typeof value === "string" ? value.trim() : ""
  return text === "image" || text === "video" || text === "audio" || text === "text" ? text : "text"
}

function workflowSanitizeStepId(value: string, fallback = "step"): string {
  const normalized = value
    .trim()
    .replace(/(?<=[a-z0-9])(?=[A-Z])/g, "_")
    .replace(/[^A-Za-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_")
    .toLowerCase()
  const base = normalized && /^[a-z]/i.test(normalized) ? normalized : fallback
  return (base || fallback).slice(0, 80)
}

function workflowUniqueStepId(base: string, steps: WorkflowTemplateStepSummary[], ignoreId = ""): string {
  const used = new Set(steps.map((step) => step.id).filter((id) => id !== ignoreId))
  const root = workflowSanitizeStepId(base || "step")
  if (!used.has(root)) return root
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${root}_${index}`
    if (!used.has(candidate)) return candidate
  }
  return `${root}_${Date.now()}`
}

function workflowUniqueInputId(base: string, inputs: string[], ignoreId = ""): string {
  const used = new Set(inputs.filter((id) => id !== ignoreId))
  const root = workflowSanitizeStepId(base || "input", "input")
  if (!used.has(root)) return root
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${root}_${index}`
    if (!used.has(candidate)) return candidate
  }
  return `${root}_${Date.now()}`
}

function workflowInputDraftSpecsFromWorkflow(
  inputs: string[],
  sourceWorkflow?: Record<string, unknown>,
): Record<string, WorkflowInputDraftSpec> {
  const result: Record<string, WorkflowInputDraftSpec> = {}
  const addSpec = (id: string, raw: Record<string, unknown>) => {
    const type = String(raw.type || raw.kind || raw.input_type || "text").trim() || "text"
    const current = result[id] || { type: "text" }
    const options = workflowInputOptionsFromRaw(raw)
    const nextSpec: WorkflowInputDraftSpec = {
      ...current,
      type,
      label: String(raw.label || raw.title || raw.name || current.label || "").trim(),
      description: String(raw.description || raw.help || current.description || "").trim(),
      default: raw.default == null ? current.default || "" : String(raw.default),
    }
    if (workflowInputTypeUsesOptions(type) && (options || current.options)) {
      nextSpec.options = options || current.options
    }
    result[id] = nextSpec
  }
  const inputMap = asWorkflowObject(sourceWorkflow?.inputs)
  if (inputMap) {
    for (const [id, value] of Object.entries(inputMap)) {
      const spec = asWorkflowObject(value)
      if (spec) addSpec(id, spec)
    }
  }
  for (const input of inputs) {
    result[input] = { type: "text", ...(result[input] || {}) }
  }
  return result
}

function workflowCloneEditorStep(step: WorkflowTemplateStepSummary): WorkflowTemplateStepSummary {
  const layoutAfter = workflowCleanIdList(step.layout_after)
  const readsFrom = workflowCleanIdList(step.reads_from)
  return {
    ...step,
    depends_on: workflowCleanIdList(step.depends_on),
    layout_after: layoutAfter.length > 0 ? layoutAfter : undefined,
    reads_from: readsFrom.length > 0 ? readsFrom : undefined,
    ui: step.ui ? { ...step.ui } : undefined,
    output: step.output ? { ...step.output } : undefined,
    fields: step.fields ? { ...step.fields } : undefined,
    expansion: step.expansion ? workflowCloneValue(step.expansion) : undefined,
    collection: step.collection ? workflowCloneValue(step.collection) : undefined,
    instance_scope: step.instance_scope ? workflowCloneValue(step.instance_scope) : undefined,
    prompt_spec: step.prompt_spec ? workflowCloneValue(step.prompt_spec) : undefined,
    extension_config: step.extension_config ? workflowCloneValue(step.extension_config) : undefined,
    completion: step.completion ? workflowCloneValue(step.completion) : undefined,
    settings: step.settings ? workflowCloneValue(step.settings) : undefined,
    io: step.io ? workflowCloneValue(step.io) : undefined,
    x: workflowHasValue(step.x) ? workflowCloneValue(step.x) : undefined,
    "x-openreel": workflowHasValue(step["x-openreel"]) ? workflowCloneValue(step["x-openreel"]) : undefined,
    prompt: workflowHasValue(step.prompt) ? workflowCloneValue(step.prompt) : undefined,
    references: workflowHasValue(step.references) ? workflowCloneValue(step.references) : undefined,
    foreach: Array.isArray(step.foreach)
      ? step.foreach.map((item) => ({ ...item }))
      : step.foreach ? { ...step.foreach } : undefined,
  }
}

function workflowEditorPosition(step: WorkflowTemplateStepSummary, index: number): { x: number; y: number } {
  const explicit = workflowExplicitEditorPosition(step)
  if (explicit) return explicit
  return { x: (index % 3) * 260, y: Math.floor(index / 3) * 150 }
}

function workflowExplicitEditorPosition(step: WorkflowTemplateStepSummary): { x: number; y: number } | null {
  const ui = asWorkflowObject(step.ui)
  const position = asWorkflowObject(ui?.position)
  if (typeof position?.x === "number" && typeof position?.y === "number") {
    return { x: position.x, y: position.y }
  }
  return null
}

function workflowNormalizeEditorPosition(position: { x: number; y: number }): { x: number; y: number } {
  const normalize = (value: number) => Math.round((Number.isFinite(value) ? value : 0) * 10) / 10
  return { x: normalize(position.x), y: normalize(position.y) }
}

function workflowEditorPositionsEqual(
  left: { x: number; y: number } | null | undefined,
  right: { x: number; y: number } | null | undefined,
): boolean {
  if (!left && !right) return true
  if (!left || !right) return false
  const a = workflowNormalizeEditorPosition(left)
  const b = workflowNormalizeEditorPosition(right)
  return a.x === b.x && a.y === b.y
}

function workflowStepDirtySignatureValue(step: WorkflowTemplateStepSummary): Record<string, unknown> {
  const record = { ...(step as unknown as Record<string, unknown>) }
  delete record.position
  const ui = asWorkflowObject(record.ui)
  if (ui) {
    const nextUi = { ...ui }
    delete nextUi.position
    if (Object.keys(nextUi).length > 0) record.ui = nextUi
    else delete record.ui
  }
  return record
}

function workflowEditorDraftSignature(
  name: string,
  description: string,
  steps: WorkflowTemplateStepSummary[],
  inputs: string[],
  requiredInputs: string[],
  inputSpecs: Record<string, WorkflowInputDraftSpec>,
  workflowAdvanced: Record<string, unknown> = {},
): string {
  return workflowStableStringify({
    name,
    description,
    inputs,
    requiredInputs,
    inputSpecs,
    workflowAdvanced,
    steps: steps.map(workflowStepDirtySignatureValue),
  })
}

function workflowEditorStepIdMap(steps: WorkflowTemplateStepSummary[]): Map<string, string> {
  const used = new Set<string>()
  const result = new Map<string, string>()
  for (const step of steps) {
    const root = workflowSanitizeStepId(step.id, "step")
    let candidate = root
    for (let index = 2; used.has(candidate); index += 1) {
      const suffix = `_${index}`
      candidate = `${root.slice(0, Math.max(1, 80 - suffix.length))}${suffix}`
    }
    used.add(candidate)
    result.set(step.id, candidate)
  }
  return result
}

function workflowEditorTopologicalSteps(steps: WorkflowTemplateStepSummary[]): WorkflowTemplateStepSummary[] {
  const stepById = new Map(steps.map((step) => [step.id, step]))
  const result: WorkflowTemplateStepSummary[] = []
  const visited = new Set<string>()
  const visiting = new Set<string>()
  const visit = (step: WorkflowTemplateStepSummary) => {
    if (visited.has(step.id)) return
    if (visiting.has(step.id)) return
    visiting.add(step.id)
    for (const dep of workflowCleanIdList(step.depends_on)) {
      const depStep = stepById.get(dep)
      if (depStep) visit(depStep)
    }
    visiting.delete(step.id)
    visited.add(step.id)
    result.push(step)
  }
  for (const step of steps) visit(step)
  return result
}

function workflowStepAuthoringKind(step: WorkflowTemplateStepSummary): WorkflowAuthoringKind {
  const raw = String(step.kind || "").trim().toLowerCase()
  if (raw === "loop" || step.shape === "loop" || step.role === "repeat_group") return "loop"
  if (raw === "collection") return "collection"
  if (raw === "object") return "object"
  if (raw === "image") return "image"
  if (raw === "video") return "video"
  if (raw === "audio") return "audio"
  if (raw === "plugin") return "plugin"
  return "text"
}

function workflowStepIsCanvasProduct(step: WorkflowTemplateStepSummary): boolean {
  const kind = workflowStepAuthoringKind(step)
  const output = asWorkflowObject(step.output)
  return kind === "image" || kind === "video" || kind === "audio" || (kind === "text" && output?.canvas === true)
}

function workflowCanvasOutputFromStep(step: WorkflowTemplateStepSummary): boolean {
  return workflowStepIsCanvasProduct(step)
}

function workflowStepPromptObject(step: WorkflowTemplateStepSummary): Record<string, unknown> | undefined {
  if (workflowHasValue(step.prompt)) {
    const prompt = asWorkflowObject(step.prompt)
    if (prompt) return workflowCloneValue(prompt)
  }
  return undefined
}

function workflowAuthoringInputSpecs(
  inputs: string[],
  requiredInputs: string[],
  sourceWorkflow?: Record<string, unknown>,
  inputSpecs?: Record<string, WorkflowInputDraftSpec>,
): Record<string, Record<string, unknown>> {
  const required = new Set(requiredInputs)
  const sourceById = new Map<string, Record<string, unknown>>()
  const inputMap = asWorkflowObject(sourceWorkflow?.inputs)
  if (inputMap) {
    for (const [id, value] of Object.entries(inputMap)) {
      const spec = asWorkflowObject(value)
      if (spec) sourceById.set(id, workflowCloneValue({ id, ...spec }))
    }
  }
  return Object.fromEntries(inputs.map((id) => {
    const spec = workflowCloneValue(sourceById.get(id) || { id })
    const draft = inputSpecs?.[id]
    delete spec.id
    delete spec.name
    delete spec.key
    if (draft) {
      spec.type = draft.type || "text"
      if (draft.label) spec.label = draft.label
      else delete spec.label
      if (draft.description) spec.description = draft.description
      else delete spec.description
      if (draft.default != null && draft.default !== "") spec.default = draft.default
      else delete spec.default
      if (workflowInputTypeUsesOptions(draft.type) && draft.options && draft.options.length > 0) {
        spec.options = draft.options.map((option) => ({ ...option }))
      } else {
        delete spec.options
        delete spec.choices
        delete spec.enum
      }
    }
    spec.label = workflowStringValue(spec.label) || workflowInputLabel(id)
    spec.type = workflowStringValue(spec.type) || "text"
    spec.required = required.has(id)
    return [id, spec]
  }))
}

function workflowCopyAuthoringField(source: Record<string, unknown>, target: Record<string, unknown>, key: string) {
  const value = source[key]
  if (workflowHasValue(value)) target[key] = workflowCloneValue(value)
}

function workflowAuthoringFieldsWithoutUiModel(step: WorkflowTemplateStepSummary): Record<string, unknown> | undefined {
  const fields = asWorkflowObject(step.fields)
  if (!fields) return undefined
  const next = { ...fields }
  if (workflowStepIsCanvasProduct(step)) delete next.model
  return Object.keys(next).length > 0 ? next : undefined
}

function workflowSpecFieldsWithoutTemplateModel(step: Record<string, unknown>): Record<string, unknown> | undefined {
  const fields = asWorkflowObject(step.fields)
  if (!fields) return undefined
  const next = { ...fields }
  const kind = workflowStringValue(step.kind).toLowerCase()
  if (["image", "video", "audio"].includes(kind)) {
    delete next.model
  }
  return Object.keys(next).length > 0 ? next : undefined
}

function workflowMappedDependencyIds(
  values: unknown,
  stepIdMap: Map<string, string>,
  normalizedStepIds: Set<string>,
): string[] {
  if (!Array.isArray(values)) return []
  const result: string[] = []
  for (const value of values) {
    const raw = String(value || "").trim()
    if (!raw) continue
    const mapped = stepIdMap.get(raw) || workflowSanitizeStepId(raw, "step")
    if (!normalizedStepIds.has(mapped) || result.includes(mapped)) continue
    result.push(mapped)
  }
  return result
}

function workflowAuthoringStepFromSummary({
  step,
  index,
  stepIdMap,
  normalizedStepIds,
  nested,
  parentScopeId,
}: {
  step: WorkflowTemplateStepSummary
  index: number
  stepIdMap: Map<string, string>
  normalizedStepIds: Set<string>
  nested?: Record<string, unknown>[]
  parentScopeId?: string
}): Record<string, unknown> {
  const kind = workflowStepAuthoringKind(step)
  const stepRecord = step as unknown as Record<string, unknown>
  const id = stepIdMap.get(step.id) || workflowSanitizeStepId(step.id || `step_${index + 1}`, `step_${index + 1}`)
  const result: Record<string, unknown> = {
    id,
    title: step.title || id,
    kind,
  }
  if (step.description) result.description = step.description
  const needs = workflowMappedDependencyIds(step.depends_on, stepIdMap, normalizedStepIds)
  if (needs.length > 0) result.needs = needs
  result.execution = step.execution === "manual" ? "manual" : "auto"
  result.on_error = step.on_error === "continue" ? "continue" : "stop"
  workflowCopyAuthoringField(stepRecord, result, "when")
  workflowCopyAuthoringField(stepRecord, result, "ui")
  const authoringFields = workflowAuthoringFieldsWithoutUiModel(step)
  if (authoringFields) result.fields = authoringFields

  if (kind === "loop") {
    workflowCopyAuthoringField(stepRecord, result, "foreach")
    if (nested && nested.length > 0) result.steps = nested
    return result
  }

  const prompt = workflowStepPromptObject(step)
  if (prompt && kind !== "plugin") result.prompt = prompt
  if (Array.isArray(step.uses) && step.uses.length > 0) result.uses = workflowCloneValue(step.uses)

  if (kind === "plugin") {
    const plugin = asWorkflowObject(stepRecord.plugin)
    if (plugin) result.plugin = workflowCloneValue(plugin)
    return result
  }

  const output = asWorkflowObject(step.output)
  if (kind === "text" && output?.canvas === true) result.output = { canvas: true }
  if (kind === "object" || kind === "collection") {
    const schema = asWorkflowObject(output?.schema)
    if (schema) result.output = { schema: workflowCloneValue(schema) }
  }
  void parentScopeId
  return result
}

function workflowAuthoringSpecFromSteps({
  id,
  name,
  description,
  inputs,
  requiredInputs,
  steps,
  sourceWorkflow,
  inputSpecs,
  workflowAdvanced,
}: {
  id: string
  name: string
  description: string
  inputs: string[]
  requiredInputs: string[]
  steps: WorkflowTemplateStepSummary[]
  sourceWorkflow?: Record<string, unknown>
  inputSpecs?: Record<string, WorkflowInputDraftSpec>
  workflowAdvanced?: Record<string, unknown>
}): Record<string, unknown> {
  const stepIdMap = workflowEditorStepIdMap(steps)
  const normalizedStepIds = new Set(stepIdMap.values())
  const stepIds = new Set(steps.map((step) => step.id))
  const byScope = new Map<string, WorkflowTemplateStepSummary[]>()
  for (const step of steps) {
    const parent = workflowStringValue(step.repeat_group_id)
    const scopeId = parent && stepIds.has(parent) ? parent : WORKFLOW_TEMPLATE_ROOT_SCOPE_ID
    byScope.set(scopeId, [...(byScope.get(scopeId) || []), step])
  }
  const buildScope = (scopeId: string): Record<string, unknown>[] => {
    const scopeSteps = workflowEditorTopologicalSteps(byScope.get(scopeId) || [])
    return scopeSteps.map((step, index) => {
      const childScopeId = workflowStepChildScopeId(step) || (workflowStepAuthoringKind(step) === "loop" ? step.id : "")
      const nested = childScopeId ? buildScope(childScopeId) : []
      return workflowAuthoringStepFromSummary({
        step,
        index,
        stepIdMap,
        normalizedStepIds,
        nested,
        parentScopeId: scopeId === WORKFLOW_TEMPLATE_ROOT_SCOPE_ID ? "" : scopeId,
      })
    })
  }

  const workflow: Record<string, unknown> = {
    schema: WORKFLOW_SPEC_VERSION,
    id: workflowSanitizeStepId(id || name || "edited_workflow", "edited_workflow"),
    title: name || "编辑的工作流",
    description,
    inputs: workflowAuthoringInputSpecs(inputs, requiredInputs, sourceWorkflow, inputSpecs),
    steps: buildScope(WORKFLOW_TEMPLATE_ROOT_SCOPE_ID),
  }
  if (sourceWorkflow) {
    for (const key of ["tags", "ui", "extensions"]) {
      if (!(key in workflow)) workflowCopyAuthoringField(sourceWorkflow, workflow, key)
    }
  }
  for (const key of WORKFLOW_ADVANCED_WORKFLOW_KEYS) {
    if (workflowAdvanced && workflowHasValue(workflowAdvanced[key])) {
      workflow[key] = workflowCloneValue(workflowAdvanced[key])
    } else if (key in workflow && workflowAdvanced && !workflowHasValue(workflowAdvanced[key])) {
      delete workflow[key]
    }
  }
  return workflow
}


function workflowArtifactPreviewFromEvent(detail: unknown): WorkflowArtifactPreview | null {
  const payload = asWorkflowObject(detail)
  const artifactRef = typeof payload?.artifact_ref === "string" ? payload.artifact_ref.trim() : ""
  const preview = asWorkflowObject(payload?.preview)
  if (!artifactRef || !preview) return null
  const firstSteps = Array.isArray(preview.first_steps) ? preview.first_steps : []
  const steps = firstSteps
    .map((item, index): WorkflowTemplateStepSummary | null => {
      const step = asWorkflowObject(item)
      if (!step) return null
      const id = String(step.id || `step_${index + 1}`).trim()
      if (!id) return null
      return {
        id,
        title: String(step.title || id),
        node_type: workflowNodeType(["image", "video", "audio"].includes(workflowStringValue(step.kind)) ? step.kind : "text"),
        depends_on: workflowCleanIdList(step.needs).length > 0
          ? workflowCleanIdList(step.needs)
          : workflowCleanIdList(step.depends_on),
        kind: workflowStringValue(step.kind) || "text",
        description: typeof step.description === "string" ? step.description : undefined,
        ui: asWorkflowObject(step.ui),
        output: asWorkflowObject(step.output),
        prompt: asWorkflowObject(step.prompt),
        foreach: asWorkflowObject(step.foreach),
        when: asWorkflowObject(step.when),
        plugin: asWorkflowObject(step.plugin),
        execution: typeof step.execution === "string" ? step.execution : undefined,
        on_error: typeof step.on_error === "string" ? step.on_error : undefined,
        uses: Array.isArray(step.uses)
          ? step.uses.filter((item) => Boolean(asWorkflowObject(item))) as Array<Record<string, unknown>>
          : undefined,
      }
    })
    .filter((item): item is WorkflowTemplateStepSummary => Boolean(item))
  const deferredGroups: Array<{ id?: string; title?: string; status?: string }> = []
  if (Array.isArray(preview.deferred_groups)) {
    for (const item of preview.deferred_groups) {
      const group = asWorkflowObject(item)
      if (!group) continue
      deferredGroups.push({
        id: typeof group.id === "string" ? group.id : undefined,
        title: typeof group.title === "string" ? group.title : undefined,
        status: typeof group.status === "string" ? group.status : undefined,
      })
    }
  }
  return {
    artifactRef,
    source: "artifact",
    id: String(preview.id || artifactRef),
    name: String(preview.name || "生成的工作流"),
    description: String(preview.description || ""),
    inputs: Array.isArray(preview.input_ids)
      ? preview.input_ids.map((item) => String(item)).filter(Boolean)
      : [],
    requiredInputs: Array.isArray(preview.required_inputs)
      ? preview.required_inputs.map((item) => String(item)).filter(Boolean)
      : [],
    stepCount: Number(preview.step_count || steps.length) || steps.length,
    dimensionCount: Number(preview.dimension_count || 0) || 0,
    deferredGroupCount: Number(preview.deferred_group_count || deferredGroups.length) || deferredGroups.length,
    dimensions: Array.isArray(preview.dimensions)
      ? preview.dimensions.map((item) => String(item)).filter(Boolean).slice(0, 12)
      : [],
    deferredGroups,
    steps,
  }
}

function workflowInputIdsFromSpec(workflow: Record<string, unknown>): string[] {
  return Object.keys(asWorkflowObject(workflow.inputs) || {}).map((id) => id.trim()).filter(Boolean)
}

function workflowRequiredInputIdsFromSpec(workflow: Record<string, unknown>): string[] {
  const inputs = asWorkflowObject(workflow.inputs)
  if (!inputs) return []
  return Object.entries(inputs)
    .filter(([, value]) => asWorkflowObject(value)?.required === true)
    .map(([id]) => id)
}

function workflowStepMetadataFromSpec(step: Record<string, unknown>): Partial<WorkflowTemplateStepSummary> {
  const result: Partial<WorkflowTemplateStepSummary> = {}
  const objectKeys = ["foreach", "prompt", "ui", "output", "when", "plugin"] as const
  for (const key of objectKeys) {
    const value = asWorkflowObject(step[key])
    if (value) (result as Record<string, unknown>)[key] = value
  }
  const cleanFields = workflowSpecFieldsWithoutTemplateModel(step)
  if (cleanFields) result.fields = cleanFields
  if (typeof step.prompt === "string" && step.prompt.trim()) result.prompt = step.prompt
  if (Array.isArray(step.uses)) {
    result.uses = step.uses
      .map((item) => asWorkflowObject(item))
      .filter((item): item is Record<string, unknown> => Boolean(item))
  }
  const stringKeys = ["kind", "execution", "on_error"] as const
  for (const key of stringKeys) {
    if (typeof step[key] === "string") (result as Record<string, unknown>)[key] = step[key]
  }
  if (typeof step.description === "string") result.description = step.description
  return result
}

function workflowStepSummariesFromSpecSteps(rawSteps: unknown): WorkflowTemplateStepSummary[] {
  if (!Array.isArray(rawSteps)) return []
  const steps: WorkflowTemplateStepSummary[] = []
  const seen = new Set<string>()
  const visit = (
    items: unknown[],
    options: { parentScopeId?: string; parentScopeLabel?: string; prefix?: string } = {},
  ) => {
    for (const [rawIndex, item] of items.entries()) {
      const step = asWorkflowObject(item)
      if (!step) continue
      const nested = Array.isArray(step.steps) ? step.steps : []
      const kind = typeof step.kind === "string" ? step.kind : ""
      const nodeType = kind === "image" || kind === "video" || kind === "audio"
        ? kind
        : "text"
      const id = String(step.id || `${options.prefix || ""}step_${rawIndex + 1}`).trim()
      if (!id || seen.has(id)) continue
      seen.add(id)
      const childIds = nested
        .map((child, childIndex) => {
          const childStep = asWorkflowObject(child)
          return String(childStep?.id || `${id}_step_${childIndex + 1}`).trim()
        })
        .filter(Boolean)
      const summary: WorkflowTemplateStepSummary = {
        id,
        title: String(step.title || id),
        node_type: workflowNodeType(nodeType),
        depends_on: workflowCleanIdList(step.depends_on).length > 0
          ? workflowCleanIdList(step.depends_on)
          : workflowCleanIdList(step.needs),
        description: typeof step.description === "string" ? step.description : undefined,
        ...workflowStepMetadataFromSpec(step),
      }
      if (options.parentScopeId) {
        summary.repeat_group_id = options.parentScopeId
        summary.repeat_group_label = options.parentScopeLabel || options.parentScopeId
      }
      if (nested.length > 0 || kind === "loop") {
        summary.role = "repeat_group"
        summary.shape = "loop"
        summary.child_scope_id = id
        summary.has_children = true
        summary.expands_to = childIds
        summary.node_type = "text"
        summary.kind = kind || "loop"
      }
      steps.push(summary)
      if (nested.length > 0) {
        visit(nested, {
          parentScopeId: id,
          parentScopeLabel: summary.title || id,
          prefix: `${id}_`,
        })
      }
    }
  }
  visit(rawSteps)
  return steps
}

function workflowStepLooksRuntimeInstance(step: WorkflowTemplateStepSummary): boolean {
  const id = workflowStringValue(step.id)
  const templateStepId = workflowStringValue(step.template_step_id)
  if (templateStepId && templateStepId !== id) return true
  if (asWorkflowObject(step.instance_scope)) return true
  if (/_i\d+_/.test(id)) return true
  const repeatGroupId = workflowStringValue(step.repeat_group_id)
  if (repeatGroupId && id.startsWith(`${repeatGroupId}_i`)) return true
  return false
}

function workflowStepsContainRuntimeInstances(steps: WorkflowTemplateStepSummary[]): boolean {
  return steps.some(workflowStepLooksRuntimeInstance)
}

function workflowTemplateStepsWithoutRuntimeInstances(steps: WorkflowTemplateStepSummary[]): WorkflowTemplateStepSummary[] {
  return steps.filter((step) => !workflowStepLooksRuntimeInstance(step))
}

function workflowTemplateRootSteps(steps: WorkflowTemplateStepSummary[]): WorkflowTemplateStepSummary[] {
  const rootSteps = steps.filter((step) => !workflowStringValue(step.repeat_group_id))
  return rootSteps.length > 0 ? rootSteps : steps
}

const WORKFLOW_TEMPLATE_ROOT_SCOPE_ID = "root"

function workflowStepChildScopeId(step: WorkflowTemplateStepSummary | undefined): string {
  if (!step) return ""
  const explicit = workflowStringValue(step.child_scope_id)
  if (explicit) return explicit
  return step.has_children ? step.id : ""
}

function workflowTemplateScopeSteps(steps: WorkflowTemplateStepSummary[], scopeId: string): WorkflowTemplateStepSummary[] {
  const normalizedScopeId = workflowStringValue(scopeId) || WORKFLOW_TEMPLATE_ROOT_SCOPE_ID
  if (normalizedScopeId === WORKFLOW_TEMPLATE_ROOT_SCOPE_ID) return workflowTemplateRootSteps(steps)
  return steps.filter((step) => workflowStringValue(step.repeat_group_id) === normalizedScopeId)
}

function workflowTemplateVisibleSteps(
  steps: WorkflowTemplateStepSummary[],
  collapsedScopeIds: Set<string>,
): WorkflowTemplateStepSummary[] {
  if (collapsedScopeIds.size === 0) return steps
  const byId = new Map(steps.map((step) => [step.id, step]))
  const hiddenByCollapsedParent = (step: WorkflowTemplateStepSummary): boolean => {
    let parentId = workflowStringValue(step.repeat_group_id)
    const visited = new Set<string>()
    while (parentId && !visited.has(parentId)) {
      if (collapsedScopeIds.has(parentId)) return true
      visited.add(parentId)
      const parent = byId.get(parentId)
      parentId = parent ? workflowStringValue(parent.repeat_group_id) : ""
    }
    return false
  }
  return steps.filter((step) => !hiddenByCollapsedParent(step))
}

function workflowTemplateChildScopeCounts(steps: WorkflowTemplateStepSummary[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const step of steps) {
    const parentId = workflowStringValue(step.repeat_group_id)
    if (!parentId) continue
    counts[parentId] = (counts[parentId] || 0) + 1
  }
  return counts
}

function workflowDescendantStepIds(steps: WorkflowTemplateStepSummary[], rootId: string): Set<string> {
  const result = new Set<string>()
  const visit = (parentId: string) => {
    for (const step of steps) {
      if (workflowStringValue(step.repeat_group_id) !== parentId || result.has(step.id)) continue
      result.add(step.id)
      visit(step.id)
    }
  }
  visit(rootId)
  return result
}

function workflowTemplateScopeTitle(
  scopeId: string,
  steps: WorkflowTemplateStepSummary[],
  selected: WorkflowTemplateSummary | undefined,
): string {
  const normalizedScopeId = workflowStringValue(scopeId) || WORKFLOW_TEMPLATE_ROOT_SCOPE_ID
  if (normalizedScopeId === WORKFLOW_TEMPLATE_ROOT_SCOPE_ID) return "主流程"
  const graphTitle = workflowStringValue(selected?.template_graph?.scopes?.[normalizedScopeId]?.title)
  if (graphTitle) return graphTitle
  const parentStep = steps.find((step) => workflowStepChildScopeId(step) === normalizedScopeId || step.id === normalizedScopeId)
  return parentStep?.title || normalizedScopeId
}

function workflowPreviewShouldUseCanonicalTemplate(artifactPreview: WorkflowArtifactPreview | null): boolean {
  return Boolean(
    artifactPreview?.source === "imported" &&
    workflowStepsContainRuntimeInstances(artifactPreview.steps),
  )
}

function workflowCanonicalTemplateForPreview(
  artifactPreview: WorkflowArtifactPreview | null,
  selected: WorkflowTemplateSummary | undefined,
  templates: WorkflowTemplateSummary[],
): WorkflowTemplateSummary | undefined {
  if (artifactPreview?.id && workflowPreviewShouldUseCanonicalTemplate(artifactPreview)) {
    const canonical = templates.find((template) => template.id === artifactPreview.id)
    if (canonical) return canonical
  }
  return selected
}

function workflowSourceFromTemplateSummary(template: WorkflowTemplateSummary | undefined): Record<string, unknown> | undefined {
  if (!template) return undefined
  const inputSchema = asWorkflowObject(template.inputs_schema) || {}
  const inputs = Object.fromEntries((template.inputs || []).map((id) => [
    id,
    asWorkflowObject(inputSchema[id]) || {
      type: "text",
      label: workflowInputLabel(id),
      required: (template.required_inputs || []).includes(id),
    },
  ]))
  return {
    schema: WORKFLOW_SPEC_VERSION,
    id: template.id,
    title: template.name,
    description: template.description,
    inputs,
    steps: template.steps,
    ...(Array.isArray(template.tags) ? { tags: template.tags } : {}),
    ...(asWorkflowObject(template.ui) ? { ui: template.ui } : {}),
    ...(asWorkflowObject(template.extensions) ? { extensions: template.extensions } : {}),
  }
}

function workflowInputIdsForTemplateSummary(template: WorkflowTemplateSummary | undefined): string[] {
  const source = workflowSourceFromTemplateSummary(template)
  return source ? workflowInputIdsFromSpec(source) : []
}

function workflowRequiredInputIdsForTemplateSummary(template: WorkflowTemplateSummary | undefined): string[] {
  const source = workflowSourceFromTemplateSummary(template)
  return source ? workflowRequiredInputIdsFromSpec(source) : []
}

function workflowInputSpecsForTemplateSummary(template: WorkflowTemplateSummary | undefined): Record<string, WorkflowInputDraftSpec> {
  const source = workflowSourceFromTemplateSummary(template)
  const inputs = workflowInputIdsForTemplateSummary(template)
  return workflowInputDraftSpecsFromWorkflow(inputs, source)
}

function workflowInputsForTemplateSource(
  artifactPreview: WorkflowArtifactPreview | null,
  selected: WorkflowTemplateSummary | undefined,
  templates: WorkflowTemplateSummary[],
): string[] {
  const canonical = workflowCanonicalTemplateForPreview(artifactPreview, selected, templates)
  if (workflowPreviewShouldUseCanonicalTemplate(artifactPreview) && canonical && canonical.id === artifactPreview?.id) {
    const canonicalInputs = workflowInputIdsForTemplateSummary(canonical)
    return canonicalInputs.length > 0 ? canonicalInputs : workflowCleanIdList(artifactPreview?.inputs)
  }
  if (artifactPreview?.inputs) return workflowCleanIdList(artifactPreview.inputs)
  return workflowInputIdsForTemplateSummary(selected)
}

function workflowRequiredInputsForTemplateSource(
  artifactPreview: WorkflowArtifactPreview | null,
  selected: WorkflowTemplateSummary | undefined,
  templates: WorkflowTemplateSummary[],
): string[] {
  const canonical = workflowCanonicalTemplateForPreview(artifactPreview, selected, templates)
  if (workflowPreviewShouldUseCanonicalTemplate(artifactPreview) && canonical && canonical.id === artifactPreview?.id) {
    const canonicalInputs = workflowRequiredInputIdsForTemplateSummary(canonical)
    return canonicalInputs.length > 0 ? canonicalInputs : workflowCleanIdList(artifactPreview?.requiredInputs)
  }
  if (artifactPreview?.requiredInputs) return workflowCleanIdList(artifactPreview.requiredInputs)
  return workflowRequiredInputIdsForTemplateSummary(selected)
}

function workflowStepsForTemplateSource(
  artifactPreview: WorkflowArtifactPreview | null,
  selected: WorkflowTemplateSummary | undefined,
  templates: WorkflowTemplateSummary[],
): WorkflowTemplateStepSummary[] {
  const canonical = workflowCanonicalTemplateForPreview(artifactPreview, selected, templates)
  if (workflowPreviewShouldUseCanonicalTemplate(artifactPreview) && canonical && canonical.id === artifactPreview?.id && canonical.steps.length > 0) {
    return workflowStepSummariesFromSpecSteps(canonical.steps)
  }
  const artifactWorkflowSteps = artifactPreview?.workflow && Array.isArray(artifactPreview.workflow.steps)
    ? workflowStepSummariesFromSpecSteps(artifactPreview.workflow.steps)
    : []
  if (artifactWorkflowSteps.length > 0) return artifactWorkflowSteps
  const rawSteps = artifactPreview?.steps ?? selected?.steps ?? EMPTY_WORKFLOW_STEPS
  const normalized = workflowStepSummariesFromSpecSteps(rawSteps)
  const steps = normalized.length > 0 ? normalized : rawSteps
  return artifactPreview ? workflowTemplateStepsWithoutRuntimeInstances(steps) : steps
}

function workflowGraphStepsForTemplateSource(
  artifactPreview: WorkflowArtifactPreview | null,
  selected: WorkflowTemplateSummary | undefined,
  templates: WorkflowTemplateSummary[],
): WorkflowTemplateStepSummary[] {
  if (!artifactPreview) {
    const rootScopeId = workflowStringValue(selected?.template_graph?.root_scope_id) || "root"
    const rootScope = selected?.template_graph?.scopes?.[rootScopeId]
    const graphNodes = Array.isArray(rootScope?.nodes) ? rootScope.nodes : []
    if (graphNodes.length > 0) return graphNodes
  }
  return workflowTemplateRootSteps(workflowStepsForTemplateSource(artifactPreview, selected, templates))
}

function workflowPreviewFromImportedSpec(payload: unknown, filename: string): WorkflowArtifactPreview | null {
  const root = asWorkflowObject(payload)
  if (!root) return null
  const workflow = root
  if (workflow.schema !== WORKFLOW_SPEC_VERSION || !Array.isArray(workflow.steps)) return null
  const steps = workflowStepSummariesFromSpecSteps(workflow.steps)
  return {
    artifactRef: "",
    source: "imported",
    workflow,
    id: String(workflow.id || filename || "imported_workflow_spec"),
    name: String(workflow.title || filename || "导入的 workflow spec"),
    description: String(workflow.description || "本地导入的 workflow spec"),
    inputs: workflowInputIdsFromSpec(workflow),
    requiredInputs: workflowRequiredInputIdsFromSpec(workflow),
    stepCount: steps.length,
    dimensionCount: 0,
    deferredGroupCount: 0,
    dimensions: [],
    deferredGroups: [],
    steps,
  }
}

function workflowPreviewFromActiveWorkflow(active: unknown): WorkflowArtifactPreview | null {
  const payload = asWorkflowObject(active)
  if (!payload) return null
  const kind = String(payload.kind || "").trim()
  const workflow = asWorkflowObject(payload.workflow)
  const name = String(payload.name || payload.artifact_ref || "工作流模板")
  const description = typeof payload.description === "string" ? payload.description : ""
  if (kind === "artifact") {
    const artifactRef = typeof payload.artifact_ref === "string" ? payload.artifact_ref.trim() : ""
    if (!artifactRef) return null
    if (workflow) {
      const fullPreview = workflowPreviewFromImportedSpec({ workflow }, name)
      if (fullPreview) {
        return {
          ...fullPreview,
          artifactRef,
          source: "artifact",
          name: String(payload.name || fullPreview.name || "生成的工作流"),
          description: String(description || fullPreview.description || ""),
        }
      }
    }
    const preview = asWorkflowObject(payload.preview)
    if (preview) {
      const previewResult = workflowArtifactPreviewFromEvent({
        artifact_ref: artifactRef,
        preview,
      })
      if (previewResult) {
        return {
          ...previewResult,
          source: "artifact",
          name: String(payload.name || previewResult.name || "生成的工作流"),
          description: String(description || previewResult.description || ""),
        }
      }
    }
    return workflowArtifactPreviewFromEvent({
      artifact_ref: artifactRef,
      preview: asWorkflowObject(payload.preview) || {},
    })
  }
  if (kind === "imported" && workflow) {
    const imported = workflowPreviewFromImportedSpec({ workflow }, name)
    if (imported) {
      return {
        ...imported,
        name: String(payload.name || imported.name || "导入的 workflow spec"),
        description: String(description || imported.description || ""),
      }
    }
    const preview = asWorkflowObject(payload.preview)
    if (preview) {
      const previewResult = workflowArtifactPreviewFromEvent({
        artifact_ref: "__imported__",
        preview,
      })
      if (previewResult) {
        return {
          ...previewResult,
          artifactRef: "",
          source: "imported",
          workflow,
          name: String(payload.name || previewResult.name || "导入的 workflow spec"),
          description: String(description || previewResult.description || ""),
        }
      }
    }
    return null
  }
  return null
}

function workflowTemplateIdFromActiveWorkflow(active: unknown): string {
  const payload = asWorkflowObject(active)
  if (!payload || payload.kind !== "template") return ""
  return typeof payload.template_id === "string" ? payload.template_id.trim() : ""
}

function workflowInputLabel(name: string): string {
  const normalized = name.trim()
  const labels: Record<string, string> = {
    plot: "故事/剧情",
    type: "视频类型",
    topic: "主题",
    theme: "主题",
    style: "视觉风格",
    aspectRatio: "画面比例",
    aspect_ratio: "画面比例",
    durationSeconds: "单段时长",
    duration_seconds: "单段时长",
    total_duration_seconds: "视频总时长",
    segment_seconds: "分段秒数",
    duration: "时长",
    episodeCount: "集数",
    episode_count: "集数",
    episodes: "集数",
    segmentCount: "每集段数",
    segment_count: "段数",
    storyboardGrid: "分镜格数",
    segments_per_episode: "每集段数",
    resolution: "分辨率",
  }
  return labels[normalized] || normalized.replace(/_/g, " ")
}

function workflowNormalizeInputDisplayLabel(label: string): string {
  const normalized = label.trim()
  if (normalized === "画幅") return "画面比例"
  return normalized
}

function workflowInputDisplayName(inputId: string, inputSpecs: Record<string, WorkflowInputDraftSpec>): string {
  const label = inputSpecs[inputId]?.label
  return label ? workflowNormalizeInputDisplayLabel(label) : inputId.replace(/_/g, " ")
}

function workflowInputPlaceholder(name: string): string {
  const normalized = name.trim()
  const placeholders: Record<string, string> = {
    plot: "例如：雨夜误送古玉引来追兵",
    type: "例如：15秒短剧",
    topic: "例如：都市复仇短剧",
    theme: "例如：都市复仇短剧",
    style: "例如：写实、冷色、电影感",
    aspectRatio: "例如：16:9",
    aspect_ratio: "例如：9:16",
    durationSeconds: "例如：15",
    duration_seconds: "例如：15",
    total_duration_seconds: "例如：60",
    segment_seconds: "例如：15",
    duration: "例如：15秒",
    episodeCount: "例如：1",
    episode_count: "例如：3",
    episodes: "例如：3",
    segmentCount: "例如：2",
    segment_count: "例如：5",
    storyboardGrid: "例如：四宫格",
    segments_per_episode: "例如：5",
    resolution: "例如：1080x1920",
  }
  return placeholders[normalized] || "输入本次工作流参数"
}

const WORKFLOW_INPUT_STORAGE_PREFIX = "openreel.workflow.inputs.v1"

function workflowInputStorageKey(projectId?: string | null, workflowId?: string): string {
  return `${WORKFLOW_INPUT_STORAGE_PREFIX}:${projectId || "none"}:${workflowId || "none"}`
}

function stringifyWorkflowInputValue(value: unknown): string {
  if (value == null) return ""
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function workflowInputValuesFromObject(value: unknown): Record<string, string> {
  const obj = asWorkflowObject(value)
  if (!obj) return {}
  return Object.fromEntries(
    Object.entries(obj)
      .map(([key, item]) => [key, stringifyWorkflowInputValue(item)] as const)
      .filter(([, item]) => item.trim()),
  )
}

type WorkflowInputValuesByInstance = Record<string, Record<string, string>>

function workflowRuntimeInputValues(runtime: ProjectWorkflowRuntime | null | undefined): Record<string, string> {
  return workflowInputValuesFromObject(runtime?.input_values)
}

function mergeWorkflowInputValuesByInstance(
  current: WorkflowInputValuesByInstance,
  runtimes: ProjectWorkflowRuntime | ProjectWorkflowRuntime[] | null | undefined,
): WorkflowInputValuesByInstance {
  const items = Array.isArray(runtimes) ? runtimes : runtimes ? [runtimes] : []
  let next = current
  for (const runtime of items) {
    const runtimeId = workflowRuntimeId(runtime)
    if (!runtimeId) continue
    const values = workflowRuntimeInputValues(runtime)
    if (Object.keys(values).length === 0) continue
    if (next === current) next = { ...current }
    next[runtimeId] = { ...(next[runtimeId] || {}), ...values }
  }
  return next
}

function readStoredWorkflowInputs(projectId?: string | null, workflowId?: string): Record<string, string> {
  if (typeof window === "undefined" || !projectId || !workflowId) return {}
  try {
    const raw = window.localStorage.getItem(workflowInputStorageKey(projectId, workflowId))
    if (!raw) return {}
    return workflowInputValuesFromObject(JSON.parse(raw))
  } catch {
    return {}
  }
}

function writeStoredWorkflowInputs(projectId: string | undefined | null, workflowId: string, values: Record<string, string>) {
  if (typeof window === "undefined" || !projectId || !workflowId) return
  const cleaned = Object.fromEntries(Object.entries(values).filter(([, value]) => value.trim()))
  try {
    const key = workflowInputStorageKey(projectId, workflowId)
    if (Object.keys(cleaned).length === 0) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, JSON.stringify(cleaned))
  } catch {
    // localStorage may be unavailable; workflow execution still uses the current form state.
  }
}

function workflowErrorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error)
  const match = raw.match(/^HTTP\s+\d+:\s+([\s\S]+)$/)
  if (!match) return raw
  try {
    const parsed = JSON.parse(match[1]) as { detail?: unknown }
    const detail = parsed.detail
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const payload = detail as Record<string, unknown>
      const message = String(payload.error || payload.hint || raw)
      const missing = Array.isArray(payload.missing_step_ids)
        ? payload.missing_step_ids.map((item) => String(item)).filter(Boolean)
        : []
      return missing.length > 0 ? `${message}：${missing.join("、")}` : message
    }
    if (typeof detail === "string" && detail.trim()) return detail
  } catch {
    return raw
  }
  return raw
}

function parseWorkflowInputValue(key: string, value: string, spec?: WorkflowInputDraftSpec): unknown {
  const text = value.trim()
  if (!text) return undefined
  const type = String(spec?.type || "").trim().toLowerCase()
  if (type === "number" || type === "integer") {
    const numeric = Number(text)
    return Number.isFinite(numeric) ? numeric : text
  }
  if (type === "boolean" || type === "checkbox") {
    if (/^(true|yes|1|是)$/i.test(text)) return true
    if (/^(false|no|0|否)$/i.test(text)) return false
    return text
  }
  if (type === "json" || type === "object") {
    try {
      return JSON.parse(text)
    } catch {
      return text
    }
  }
  if (type === "array" || type === "list") {
    if (/^\s*\[/.test(text)) {
      try {
        return JSON.parse(text)
      } catch {
        return text
      }
    }
    return text.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean)
  }
  if (/(count|seconds?|duration|episode|segment)s?$/i.test(key) || /^(episodes|segments)$/i.test(key)) {
    const numeric = Number(text)
    if (Number.isFinite(numeric)) return numeric
  }
  return text
}

function workflowParsedJsonObject(value: unknown): Record<string, unknown> | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
  if (typeof value !== "string") return undefined
  const text = value.trim()
  if (!/^[{[]/.test(text)) return undefined
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : undefined
  } catch {
    return undefined
  }
}

function workflowStructuredOutput(value: unknown): unknown {
  if (typeof value === "string") {
    const text = value.trim()
    if (!/^[{[]/.test(text)) return text
    try {
      return workflowStructuredOutput(JSON.parse(text) as unknown)
    } catch {
      return text
    }
  }
  if (Array.isArray(value)) return value.map((item) => workflowStructuredOutput(item))
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, workflowStructuredOutput(item)]),
    )
  }
  return value
}

function workflowConditionValue(values: Record<string, string>, key: string): unknown {
  const normalized = key.trim().toLowerCase()
  for (const [candidate, value] of Object.entries(values)) {
    if (candidate.trim().toLowerCase() !== normalized) continue
    const parsed = parseWorkflowInputValue(candidate, value)
    return parsed === undefined ? value : parsed
  }
  return undefined
}

function workflowConditionNumber(value: unknown): number | null {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

function workflowConditionMet(condition: unknown, values: Record<string, string>): boolean {
  const parsed = workflowParseCondition(condition)
  if (!parsed.inputId || !parsed.operator) return true
  const left = workflowConditionValue(values, parsed.inputId)
  const rightObject = asWorkflowObject(condition)
  const right = rightObject?.value
  const isEmpty = left == null || left === "" || (Array.isArray(left) && left.length === 0)
  if (parsed.operator === "empty") return isEmpty
  if (parsed.operator === "not_empty") return !isEmpty
  const leftNumber = workflowConditionNumber(left)
  const rightNumber = workflowConditionNumber(right)
  const a = leftNumber !== null && rightNumber !== null ? leftNumber : left
  const b = leftNumber !== null && rightNumber !== null ? rightNumber : right
  switch (parsed.operator) {
    case "lte": return (a as number) <= (b as number)
    case "gte": return (a as number) >= (b as number)
    case "lt": return (a as number) < (b as number)
    case "gt": return (a as number) > (b as number)
    case "eq": return a === b
    case "ne": return a !== b
    default: return true
  }
}

function workflowStepIsVirtual(step: WorkflowTemplateStepSummary, values: Record<string, string>, inputIds: string[]): boolean {
  const runner = workflowStringValue(step.runner)
  const stepId = workflowStringValue(step.id).toLowerCase()
  const role = workflowStringValue(step.role)
  if (runner === "workflow_input" || runner === "input_form" || runner === "manual_input") return true
  if (inputIds.length > 0 && (stepId === "input" || stepId === "inputs" || stepId === "workflow_input")) return true
  if (role === "repeat_group") return true
  if (step.runtime_hidden) return true
  if (step.when && !workflowConditionMet(step.when, values)) return true
  return false
}

function workflowStepIsInputStep(step: WorkflowTemplateStepSummary | undefined, inputIds: string[] = []): boolean {
  if (!step) return false
  const kind = workflowStepAuthoringKind(step)
  const runner = workflowStringValue(step.runner)
  const stepId = workflowStringValue(step.id).toLowerCase()
  return (
    runner === "workflow_input" ||
    runner === "input_form" ||
    runner === "manual_input" ||
    step.role === "entry" ||
    (inputIds.length > 0 && (stepId === "input" || stepId === "inputs" || stepId === "workflow_input"))
  )
}

function workflowStepIsFlowOnly(step: WorkflowTemplateStepSummary): boolean {
  if (workflowStepIsCanvasProduct(step)) return false
  if (step.runtime_only === true) return true
  const surface = workflowStringValue(step.surface).toLowerCase()
  const visibility = workflowStringValue(step.visibility).toLowerCase()
  return surface === "workflow_runtime" || visibility === "flow_only" || visibility === "workflow_runtime"
}

function isWorkflowRuntimeCanvasNode(node: FlowNode): boolean {
  const data = node.data as { surface?: unknown; workflow?: unknown } | undefined
  const surface = workflowStringValue(data?.surface).toLowerCase()
  const workflow = asWorkflowObject(data?.workflow)
  const workflowSurface = workflowStringValue(workflow?.surface).toLowerCase()
  const workflowVisibility = workflowStringValue(workflow?.visibility).toLowerCase()
  return (
    surface === "workflow_runtime" ||
    workflowSurface === "workflow_runtime" ||
    workflowVisibility === "flow_only" ||
    workflowVisibility === "workflow_runtime"
  )
}

function workflowDynamicVisibleSteps(
  steps: WorkflowTemplateStepSummary[],
  nodeStates: Record<string, WorkflowStepNodeState>,
): WorkflowTemplateStepSummary[] {
  if (steps.length === 0) return []
  const indexes = new Set<number>()
  for (const [index, step] of steps.entries()) {
    const state = nodeStates[step.id]
    if (state) indexes.add(index)
  }
  const nextIndex = steps.findIndex((step) => {
    const state = nodeStates[step.id]
    return !state || state.status === "failed" || state.completedCount < state.count
  })
  if (nextIndex >= 0) indexes.add(nextIndex)
  if (indexes.size === 0) indexes.add(0)
  const maxIndex = Math.max(...Array.from(indexes))
  for (let index = 0; index <= maxIndex; index += 1) {
    indexes.add(index)
  }
  return steps.filter((_, index) => indexes.has(index))
}

function workflowRuntimeContextFromNodes(nodes: FlowNode[], workflowId: string, instanceId = ""): Record<string, unknown> {
  if (!workflowId || !instanceId) return {}
  const context: Record<string, unknown> = {}
  for (const node of nodes) {
    const data = node.data as Record<string, unknown> | undefined
    const workflow = asWorkflowObject(data?.workflow)
    if (!workflow) continue
    const templateId = workflowStringValue(workflow.template_id)
    if (templateId && templateId !== workflowId) continue
    const nodeInstanceId = workflowStringValue(workflow.instance_id)
    if (instanceId && nodeInstanceId !== instanceId) continue
    const stepId = workflowStringValue(workflow.step_id)
    const templateStepId = workflowStringValue(workflow.template_step_id)
    const sourceNodeId = workflowStringValue(workflow.source_node_id)
    const output = workflowStructuredOutput(data?.output ?? data?.workflowRuntimeOutput)
    const payload = {
      node_id: node.id,
      title: data?.title,
      type: data?.type,
      status: data?.status,
      output,
      input: data?.input,
    }
    for (const key of [stepId, templateStepId, sourceNodeId]) {
      if (key && !context[key]) context[key] = payload
    }
  }
  return context
}

function workflowStepStateLabel(status: string): string {
  if (status === "running") return "运行中"
  if (status === "completed") return "完成"
  if (status === "failed") return "失败"
  if (status === "stale") return "需重跑"
  if (status === "queued") return "排队"
  if (status === "idle") return "已加载"
  return status || "已加载"
}

function workflowStepAggregateLabel(state: WorkflowStepNodeState): string {
  if (state.count <= 1) return workflowStepStateLabel(state.status)
  if (state.runningCount > 0) return `${state.runningCount}/${state.count} 运行`
  if (state.failedCount > 0) return `${state.failedCount}/${state.count} 失败`
  if (state.completedCount === state.count) return `${state.count} 个完成`
  return `${state.count} 个已加载`
}

function workflowStepStateClass(status: string): string {
  if (status === "running") return "border-blue-200/25 bg-blue-300/14 text-blue-100"
  if (status === "completed") return "border-emerald-200/25 bg-emerald-300/12 text-emerald-100"
  if (status === "failed") return "border-red-300/25 bg-red-500/12 text-red-100"
  if (status === "stale") return "border-amber-200/25 bg-amber-300/12 text-amber-100"
  if (status === "queued") return "border-amber-200/25 bg-amber-300/12 text-amber-100"
  return "border-white/10 bg-white/[0.05] text-zinc-300"
}

function workflowStepKindLabel(step: WorkflowTemplateStepSummary): string {
  const kind = workflowStepAuthoringKind(step)
  if (kind === "text") return "文本"
  if (kind === "object") return "结构化对象"
  if (step.shape === "loop" || step.role === "repeat_group" || kind === "loop") return "循环"
  if (kind === "collection") return "结构化列表"
  if (kind === "image") return "图片"
  if (kind === "video") return "视频"
  if (kind === "audio") return "音频"
  if (step.runner === "node.run") return workflowRunnerDisplay(step.runner, step.node_type)
  if (step.runner === "workflow_plugin") return "插件动作"
  if (step.runner === "workflow_canvas_output") return "画布产物"
  return WORKFLOW_NODE_TYPE_LABEL[step.node_type] || step.node_type
}

function workflowStepGraphIcon(step: WorkflowTemplateStepSummary): string {
  const kind = workflowStepAuthoringKind(step)
  if (kind === "text") return "文"
  if (kind === "object") return "构"
  if (kind === "collection") return "列"
  if (kind === "loop") return "循"
  if (kind === "image") return "图"
  if (kind === "video") return "视"
  if (kind === "audio") return "音"
  if (kind === "plugin") return "插"
  return "文"
}

function workflowStepToneClass(step: WorkflowTemplateStepSummary): string {
  const kind = workflowStepAuthoringKind(step)
  if (kind === "text") return "border-sky-200/20 bg-sky-300/[0.07] text-sky-100"
  if (kind === "object") return "border-blue-200/20 bg-blue-300/[0.07] text-blue-100"
  if (kind === "loop" || step.role === "repeat_group") return "border-violet-200/28 bg-violet-300/[0.11] text-violet-100"
  if (kind === "collection" || step.collection || step.foreach) return "border-emerald-200/24 bg-emerald-300/[0.09] text-emerald-100"
  if (kind === "image") return "border-cyan-200/36 bg-cyan-300/[0.14] text-cyan-50"
  if (kind === "video") return "border-rose-200/36 bg-rose-300/[0.13] text-rose-50"
  if (kind === "audio") return "border-orange-200/34 bg-orange-300/[0.13] text-orange-50"
  if (kind === "plugin") return "border-fuchsia-200/24 bg-fuchsia-300/[0.09] text-fuchsia-100"
  return "border-white/10 bg-white/[0.04] text-zinc-300"
}

function workflowReadableLabel(value: unknown): string {
  const text = workflowStringValue(value)
  if (!text) return ""
  const normalized = text.trim().toLowerCase()
  if (WORKFLOW_PHASE_LABELS[normalized]) return WORKFLOW_PHASE_LABELS[normalized]
  if (/character|人物/.test(normalized)) return "人物"
  if (/scene|场景/.test(normalized)) return "场景"
  if (/storyboard|frame|分镜|宫格/.test(normalized)) return "分镜"
  if (/script|story|剧本|剧情/.test(normalized)) return "剧本"
  if (/video.*prompt|视频提示词/.test(normalized)) return "视频提示词"
  if (/video|成片/.test(normalized)) return "成片"
  if (/image|图片|参考图/.test(normalized)) return "图片"
  return text.replace(/^skill\./, "").replace(/[_-]+/g, " ")
}

function workflowStepRepeatLabel(step: WorkflowTemplateStepSummary): string {
  const explicit = workflowReadableLabel(step.repeat_group_label)
  if (explicit) return explicit
  const index = typeof step.repeat_group_index === "number" ? step.repeat_group_index : undefined
  if (index != null && index >= 0) return `第 ${index + 1} 组`
  return ""
}

function workflowStepRepeatSource(step: WorkflowTemplateStepSummary): string {
  const foreach = asWorkflowObject(step.foreach)
  if (typeof foreach?.items === "string") return foreach.items
  if (typeof foreach?.count === "string") return foreach.count
  if (typeof foreach?.count === "number") return String(foreach.count)
  return ""
}

function workflowLoopSourceParts(value: string): { type: "input" | "step" | "fixed" | ""; source: string; path: string } {
  const text = value.trim()
  if (!text) return { type: "", source: "", path: "" }
  const input = text.match(/^inputs\.([A-Za-z][A-Za-z0-9_-]*)(?:\.(.*))?$/)
  if (input) return { type: "input", source: input[1], path: input[2] || "" }
  const step = text.match(/^steps\.([A-Za-z][A-Za-z0-9_-]*)\.output(?:\.(.*?))?(?:\[\])?$/)
  if (step) return { type: "step", source: step[1], path: (step[2] || "").replace(/\[\]$/, "") }
  if (/^\d+$/.test(text)) return { type: "fixed", source: text, path: "" }
  return { type: "", source: "", path: "" }
}

function workflowDependencyCandidateSteps(
  step: WorkflowTemplateStepSummary,
  steps: WorkflowTemplateStepSummary[],
): WorkflowTemplateStepSummary[] {
  const index = steps.findIndex((item) => item.id === step.id)
  if (index <= 0) return []
  const descendants = workflowDescendantStepIds(steps, step.id)
  return steps
    .slice(0, index)
    .filter((candidate) => candidate.id !== step.id && !descendants.has(candidate.id))
}

function workflowLoopSourceStepCandidates(
  step: WorkflowTemplateStepSummary,
  steps: WorkflowTemplateStepSummary[],
): WorkflowTemplateStepSummary[] {
  return workflowDependencyCandidateSteps(step, steps)
    .filter((candidate) => ["object", "collection"].includes(workflowStepAuthoringKind(candidate)))
}

function workflowScopeOptionsForStep(
  step: WorkflowTemplateStepSummary,
  steps: WorkflowTemplateStepSummary[],
): Array<{ id: string; title: string }> {
  const descendants = workflowDescendantStepIds(steps, step.id)
  const result = [{ id: WORKFLOW_TEMPLATE_ROOT_SCOPE_ID, title: "不重复，放在主流程" }]
  for (const candidate of steps) {
    if (candidate.id === step.id || descendants.has(candidate.id)) continue
    if (workflowStepAuthoringKind(candidate) !== "loop" && !workflowStepChildScopeId(candidate)) continue
    result.push({ id: workflowStepChildScopeId(candidate) || candidate.id, title: `在“${candidate.title || candidate.id}”里重复` })
  }
  return result
}

function workflowNodePaletteItemIsDuplicate(item: WorkflowNodeTypeDefinition): boolean {
  const title = String(item.title || item.name || item.type || "").trim()
  const kind = String(item.kind || item.type || "").trim().toLowerCase()
  const plugin = Boolean(item.plugin_id || item.plugin_name)
  if (plugin) return false
  if (kind === "input" || /流程输入|输入|填写表单|运行输入/.test(title)) return true
  return /^(文本生成|生成文本|文本处理|提取集合|提取列表|集合提取|结构化规划|结构规划|分段拆分|拆分文本|循环块|遍历执行|循环处理|文本节点|图片节点|图片任务|图片生成|生成图片|视频节点|视频任务|视频生成|生成视频|视频|音频节点|检查|复核|质量检查)$/.test(title)
}

function workflowOutputSchemaFields(step: WorkflowTemplateStepSummary): Array<Record<string, unknown>> {
  const schema = asWorkflowObject(asWorkflowObject(step.output)?.schema)
  const fields = Array.isArray(schema?.fields) ? schema.fields : []
  return fields.map((item) => asWorkflowObject(item)).filter((item): item is Record<string, unknown> => Boolean(item))
}

function workflowUniqueFieldId(base: string, fields: Array<Record<string, unknown>>, ignoreIndex = -1): string {
  const used = new Set(fields.map((field, index) => index === ignoreIndex ? "" : workflowStringValue(field.id || field.key || field.name)).filter(Boolean))
  const root = workflowSanitizeStepId(base || "field", "field")
  if (!used.has(root)) return root
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${root}_${index}`
    if (!used.has(candidate)) return candidate
  }
  return `${root}_${Date.now()}`
}

function workflowPatchOutputSchemaField(
  step: WorkflowTemplateStepSummary,
  index: number,
  patch: Record<string, unknown>,
): Record<string, unknown> {
  const schema = asWorkflowObject(asWorkflowObject(step.output)?.schema) || {}
  const fields = workflowOutputSchemaFields(step)
  const nextFields = fields.map((field, fieldIndex) => fieldIndex === index ? { ...field, ...patch } : field)
  return { ...schema, fields: nextFields }
}

function workflowAddOutputSchemaField(step: WorkflowTemplateStepSummary): Record<string, unknown> {
  const schema = asWorkflowObject(asWorkflowObject(step.output)?.schema) || {}
  const fields = workflowOutputSchemaFields(step)
  const id = workflowUniqueFieldId("field", fields)
  return { ...schema, fields: [...fields, { id, label: id, type: "string" }] }
}

function workflowRemoveOutputSchemaField(step: WorkflowTemplateStepSummary, index: number): Record<string, unknown> {
  const schema = asWorkflowObject(asWorkflowObject(step.output)?.schema) || {}
  return { ...schema, fields: workflowOutputSchemaFields(step).filter((_, fieldIndex) => fieldIndex !== index) }
}

function workflowReferenceRows(step: WorkflowTemplateStepSummary): Array<Record<string, unknown>> {
  if (!Array.isArray(step.uses)) return []
  return step.uses.map((item) => {
    const roles = Array.isArray(item.as) ? item.as.map(workflowStringValue).filter(Boolean) : []
    return {
      ...item,
      source_step: workflowStringValue(item.from),
      role: roles.includes("vision") && roles.includes("reference")
        ? "vision_reference"
        : roles[0] || "reference",
    }
  })
}

function workflowAddReferenceRow(step: WorkflowTemplateStepSummary, candidates: WorkflowTemplateStepSummary[]): Array<Record<string, unknown>> {
  const rows = workflowReferenceRows(step)
  const source = candidates[0]?.id || ""
  return [
    ...rows,
    {
      from: source,
      as: ["reference"],
      source_step: source,
      role: "reference",
    },
  ]
}

function workflowPatchReferenceRow(step: WorkflowTemplateStepSummary, index: number, patch: Record<string, unknown>): Array<Record<string, unknown>> {
  return workflowReferenceRows(step).map((row, rowIndex) => {
    const next = rowIndex === index ? { ...row, ...patch } : row
    const from = workflowStringValue(next.source_step || next.from)
    const role = workflowStringValue(next.role) || "reference"
    const result: Record<string, unknown> = {
      from,
      as: role === "vision_reference" ? ["vision", "reference"] : [role],
    }
    if (workflowHasValue(next.select)) result.select = workflowCloneValue(next.select)
    return result
  })
}

function workflowRemoveReferenceRow(step: WorkflowTemplateStepSummary, index: number): Array<Record<string, unknown>> {
  return workflowReferenceRows(step)
    .filter((_, rowIndex) => rowIndex !== index)
    .map((row) => ({ from: workflowStringValue(row.from), as: Array.isArray(row.as) ? row.as : [workflowStringValue(row.role) || "reference"], ...(workflowHasValue(row.select) ? { select: row.select } : {}) }))
}

function workflowStepFields(step: WorkflowTemplateStepSummary): Record<string, unknown> {
  return asWorkflowObject(step.fields) || {}
}

function workflowPatchStepFields(step: WorkflowTemplateStepSummary, patch: Record<string, unknown>): Record<string, unknown> | undefined {
  const next: Record<string, unknown> = { ...workflowStepFields(step) }
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined || value === null || value === "") delete next[key]
    else next[key] = value
  }
  return Object.keys(next).length > 0 ? next : undefined
}

function workflowMediaProvidersForKind(providers: MediaProviderSummary[], kind: WorkflowAuthoringKind): MediaProviderSummary[] {
  if (kind !== "image" && kind !== "video" && kind !== "audio") return []
  return providers.filter((provider) => provider.kind === kind && provider.enabled !== false)
}

function workflowResolveMediaProvider(value: string, providers: MediaProviderSummary[]): MediaProviderSummary | undefined {
  const enabled = providers.filter((provider) => provider.enabled !== false)
  const selected = value.trim()
  if (selected) return enabled.find((provider) => provider.name === selected || provider.model_name === selected)
  return enabled.find((provider) => provider.is_active) || enabled[0]
}

function workflowMediaProviderLabel(provider: MediaProviderSummary): string {
  const name = String(provider.name || "").trim()
  const model = String(provider.model_name || "").trim()
  if (name && model && name !== model) return `${name} · ${model}`
  return name || model || "未命名模型"
}

function workflowMediaProviderOptions(
  providers: MediaProviderSummary[],
  currentValue: string,
  selectedProvider?: MediaProviderSummary,
): Array<{ label: string; value: string; disabled?: boolean }> {
  const current = currentValue.trim()
  if (providers.length === 0) {
    return current
      ? [{ label: `当前：${current}`, value: current }]
      : [{ label: "未配置可用模型", value: "", disabled: true }]
  }
  return [
    ...(current && !selectedProvider ? [{ label: `当前：${current}`, value: current }] : []),
    ...providers.map((provider) => ({ label: workflowMediaProviderLabel(provider), value: provider.name })),
  ]
}

function workflowCleanMediaModelOverrides(overrides: Record<string, string>): Record<string, string> {
  const cleaned: Record<string, string> = {}
  for (const [stepId, model] of Object.entries(overrides)) {
    const key = stepId.trim()
    const value = model.trim()
    if (key && value) cleaned[key] = value
  }
  return cleaned
}

function workflowUiOverridesFromMediaModels(overrides: Record<string, string>): Record<string, unknown> | undefined {
  const mediaModelOverrides = workflowCleanMediaModelOverrides(overrides)
  return Object.keys(mediaModelOverrides).length > 0
    ? { media_model_overrides: mediaModelOverrides }
    : undefined
}

function workflowProductSourceStep(step: WorkflowTemplateStepSummary): string {
  const firstUse = Array.isArray(step.uses) ? asWorkflowObject(step.uses[0]) : undefined
  return workflowStringValue(firstUse?.from) || workflowCleanIdList(step.depends_on)[0] || ""
}

interface WorkflowMediaDimensions {
  width: number
  height: number
}

const WORKFLOW_DEFAULT_MEDIA_ASPECT: WorkflowMediaDimensions = { width: 9, height: 16 }
const WORKFLOW_DEFAULT_MEDIA_RESOLUTION: WorkflowMediaDimensions = { width: 1080, height: 1920 }
type WorkflowImageResolutionTier = "1k" | "2k" | "4k"
const WORKFLOW_IMAGE_RESOLUTION_TIERS: Array<{ label: string; value: WorkflowImageResolutionTier }> = [
  { label: "1K", value: "1k" },
  { label: "2K", value: "2k" },
  { label: "4K", value: "4k" },
]
const WORKFLOW_IMAGE_RESOLUTION_SHORT_EDGE: Record<WorkflowImageResolutionTier, number> = {
  "1k": 1080,
  "2k": 1440,
  "4k": 2160,
}
const WORKFLOW_IMAGE_ASPECT_OPTIONS = ["9:16", "16:9", "1:1", "1:2", "2:1", "3:4", "4:3", "2:3", "3:2", "4:5", "5:4", "21:9", "9:21"]
const WORKFLOW_MAX_IMAGE_PIXEL_AREA = 3840 * 2160

function workflowPreventInvalidPositiveIntegerKey(event: ReactKeyboardEvent<HTMLInputElement>) {
  if (["e", "E", "+", "-", "."].includes(event.key)) event.preventDefault()
}

function workflowPositiveIntegerValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) return Math.round(value)
  const text = workflowStringValue(value).trim()
  if (!text) return undefined
  const match = text.match(/\d+/)
  if (!match) return undefined
  const parsed = Number.parseInt(match[0], 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}

function workflowDimensionPairFromText(value: unknown): WorkflowMediaDimensions | undefined {
  const text = workflowStringValue(value).trim()
  if (!text) return undefined
  const pair = text.match(/(\d+)\s*(?::|x|X|×|\*)\s*(\d+)/)
  if (!pair) return undefined
  const width = workflowPositiveIntegerValue(pair[1])
  const height = workflowPositiveIntegerValue(pair[2])
  return width && height ? { width, height } : undefined
}

function workflowParseAspectRatio(value: unknown): { width: number; height: number; value: string } | undefined {
  const text = workflowStringValue(value).trim()
  const match = text.match(/^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)$/)
  if (!match) return undefined
  const width = Number.parseFloat(match[1])
  const height = Number.parseFloat(match[2])
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return undefined
  return { width, height, value: `${match[1]}:${match[2]}` }
}

function workflowRoundToMultipleOfEight(value: number): number {
  return Math.max(8, Math.round(value / 8) * 8)
}

function workflowImageResolutionForAspectTier(aspectRatio: string, tier: WorkflowImageResolutionTier): WorkflowMediaDimensions {
  const aspect = workflowParseAspectRatio(aspectRatio) || { width: 9, height: 16, value: "9:16" }
  if (aspect.value === "1:1") {
    const size = tier === "1k" ? 1080 : tier === "2k" ? 2048 : 2880
    return { width: size, height: size }
  }
  const shortEdge = WORKFLOW_IMAGE_RESOLUTION_SHORT_EDGE[tier]
  let width = aspect.width >= aspect.height ? shortEdge * (aspect.width / aspect.height) : shortEdge
  let height = aspect.width >= aspect.height ? shortEdge : shortEdge * (aspect.height / aspect.width)
  if (width * height > WORKFLOW_MAX_IMAGE_PIXEL_AREA) {
    const scale = Math.sqrt(WORKFLOW_MAX_IMAGE_PIXEL_AREA / (width * height))
    width *= scale
    height *= scale
  }
  return { width: workflowRoundToMultipleOfEight(width), height: workflowRoundToMultipleOfEight(height) }
}

function workflowImageResolutionTierFromDimensions(dimensions: WorkflowMediaDimensions): WorkflowImageResolutionTier {
  const shortEdge = Math.min(dimensions.width, dimensions.height)
  const area = dimensions.width * dimensions.height
  if (area >= WORKFLOW_MAX_IMAGE_PIXEL_AREA * 0.75 || shortEdge >= 1900) return "4k"
  if (shortEdge >= 1300 || area >= 2_800_000) return "2k"
  return "1k"
}

function workflowMediaResolutionLabel(value: string): string {
  const text = value.trim()
  if (/^\d+p$/i.test(text)) return text.toUpperCase()
  return text.toUpperCase()
}

function workflowVideoResolutionValue(value: unknown, fallback = ""): string {
  const text = workflowStringValue(value).trim().toLowerCase()
  if (/^\d+p$/.test(text) || text === "4k") return text
  const pair = workflowDimensionPairFromText(text)
  if (pair) {
    const shortEdge = Math.min(pair.width, pair.height)
    if (shortEdge >= 2000) return "4k"
    if (shortEdge >= 1000) return "1080p"
    if (shortEdge >= 700) return "720p"
    return "480p"
  }
  if (text === "480" || text === "720" || text === "1080") return `${text}p`
  if (text === "2160" || text === "uhd") return "4k"
  return fallback
}

function workflowProductAspectDimensions(step: WorkflowTemplateStepSummary): WorkflowMediaDimensions {
  const fields = workflowStepFields(step)
  const pair = workflowDimensionPairFromText(fields.aspect_ratio || fields.ratio)
  const width = workflowPositiveIntegerValue(fields.aspect_width || fields.ratio_width || fields.width_ratio) || pair?.width || WORKFLOW_DEFAULT_MEDIA_ASPECT.width
  const height = workflowPositiveIntegerValue(fields.aspect_height || fields.ratio_height || fields.height_ratio) || pair?.height || WORKFLOW_DEFAULT_MEDIA_ASPECT.height
  return { width, height }
}

function workflowProductAspectRatio(step: WorkflowTemplateStepSummary): string {
  const fields = workflowStepFields(step)
  const parsed = workflowParseAspectRatio(fields.aspect_ratio || fields.ratio)
  if (parsed) return parsed.value
  const dimensions = workflowProductAspectDimensions(step)
  return `${dimensions.width}:${dimensions.height}`
}

function workflowProductResolutionDimensions(step: WorkflowTemplateStepSummary): WorkflowMediaDimensions {
  const fields = workflowStepFields(step)
  const pair = workflowDimensionPairFromText(fields.resolution || fields.size || fields.dimensions)
  const width = workflowPositiveIntegerValue(fields.width || fields.resolution_width || fields.pixel_width || fields.image_width || fields.video_width) || pair?.width || WORKFLOW_DEFAULT_MEDIA_RESOLUTION.width
  const height = workflowPositiveIntegerValue(fields.height || fields.resolution_height || fields.pixel_height || fields.image_height || fields.video_height) || pair?.height || WORKFLOW_DEFAULT_MEDIA_RESOLUTION.height
  return { width, height }
}

function workflowPatchProductAspectRatioFields(step: WorkflowTemplateStepSummary, aspectRatio: string): Record<string, unknown> | undefined {
  const parsed = workflowParseAspectRatio(aspectRatio)
  return workflowPatchStepFields(step, {
    aspect_width: parsed?.width,
    aspect_height: parsed?.height,
    aspect_ratio: parsed?.value || aspectRatio,
  })
}

function workflowPatchProductResolutionFields(
  step: WorkflowTemplateStepSummary,
  dimensions: WorkflowMediaDimensions,
): Record<string, unknown> | undefined {
  return workflowPatchStepFields(step, {
    width: dimensions.width,
    height: dimensions.height,
    resolution: `${dimensions.width}x${dimensions.height}`,
  })
}

function workflowPatchProductVideoResolutionFields(step: WorkflowTemplateStepSummary, resolution: string): Record<string, unknown> | undefined {
  return workflowPatchStepFields(step, {
    width: undefined,
    height: undefined,
    resolution,
  })
}

function workflowPatchProductImageAspectAndResolutionFields(
  step: WorkflowTemplateStepSummary,
  aspectRatio: string,
  dimensions: WorkflowMediaDimensions,
): Record<string, unknown> | undefined {
  const parsed = workflowParseAspectRatio(aspectRatio)
  return workflowPatchStepFields(step, {
    aspect_width: parsed?.width,
    aspect_height: parsed?.height,
    aspect_ratio: parsed?.value || aspectRatio,
    width: dimensions.width,
    height: dimensions.height,
    resolution: `${dimensions.width}x${dimensions.height}`,
  })
}

function workflowDefaultCanvasProductFields(
  kind: WorkflowAuthoringKind,
  currentStep?: WorkflowTemplateStepSummary,
): Record<string, unknown> {
  const fields: Record<string, unknown> = {}
  if (kind === "image" || kind === "video") {
    const aspect = currentStep ? workflowProductAspectDimensions(currentStep) : WORKFLOW_DEFAULT_MEDIA_ASPECT
    fields.aspect_width = aspect.width
    fields.aspect_height = aspect.height
    fields.aspect_ratio = `${aspect.width}:${aspect.height}`
  }
  if (kind === "image") {
    const resolution = currentStep ? workflowProductResolutionDimensions(currentStep) : WORKFLOW_DEFAULT_MEDIA_RESOLUTION
    fields.width = resolution.width
    fields.height = resolution.height
    fields.resolution = `${resolution.width}x${resolution.height}`
    fields.quality = currentStep ? workflowStringValue(workflowStepFields(currentStep).quality) || "high" : "high"
  }
  if (kind === "video") {
    const currentResolution = currentStep ? workflowStringValue(workflowStepFields(currentStep).resolution) : ""
    if (currentResolution) fields.resolution = workflowVideoResolutionValue(currentResolution, "")
  }
  const durationSeconds = currentStep ? workflowPositiveIntegerValue(workflowStepFields(currentStep).duration_seconds) : undefined
  if (kind === "video" || kind === "audio") fields.duration_seconds = durationSeconds || 5
  return fields
}

function workflowJsonScalar(value: unknown): string {
  if (value === undefined || value === null) return ""
  if (typeof value === "string") return value
  return JSON.stringify(value)
}

function workflowValueFromFieldInput(rawValue: string, fieldType: string): unknown {
  const value = rawValue.trim()
  const normalizedType = fieldType.toLowerCase()
  if (normalizedType === "number" || normalizedType === "integer") {
    if (!value) return ""
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : rawValue
  }
  if (normalizedType === "boolean" || normalizedType === "checkbox") {
    if (rawValue === "true") return true
    if (rawValue === "false") return false
    return ""
  }
  if (normalizedType === "object" || normalizedType === "array" || normalizedType === "json") {
    if (!value) return ""
    try {
      return JSON.parse(value)
    } catch {
      return rawValue
    }
  }
  return rawValue
}

function workflowConditionValueFromInput(rawValue: string, inputType: string): unknown {
  const value = rawValue.trim()
  const inputKind = workflowInputTypeCategory(inputType)
  if (inputKind === "boolean") {
    return value.toLowerCase() === "true"
  }
  if (inputKind === "number" && /^-?\d+(?:\.\d+)?$/.test(value)) {
    return Number(value)
  }
  return value
}

function workflowParseCondition(condition: unknown): { inputId: string; operator: string; value: string } {
  const object = asWorkflowObject(condition)
  const path = workflowStringValue(object?.path)
  const match = path.match(/^inputs\.([A-Za-z][A-Za-z0-9_-]*)$/)
  if (!match) return { inputId: "", operator: "", value: "" }
  return {
    inputId: match[1],
    operator: workflowStringValue(object?.op),
    value: object?.value == null ? "" : workflowJsonScalar(object.value),
  }
}

function workflowFormatCondition(
  inputId: string,
  operator: string,
  value: string,
  inputType = "text",
): Record<string, unknown> | undefined {
  const normalizedInput = workflowSanitizeStepId(inputId, "")
  if (!normalizedInput || !operator) return undefined
  if (operator === "empty" || operator === "not_empty") {
    return { path: `inputs.${normalizedInput}`, op: operator }
  }
  return {
    path: `inputs.${normalizedInput}`,
    op: operator,
    value: workflowConditionValueFromInput(value, inputType),
  }
}

function workflowPromptInputReference(inputId: string, label: string): string {
  const normalizedInput = workflowSanitizeStepId(inputId, "")
  if (!normalizedInput) return ""
  return `用户输入“${label || normalizedInput}”：{{ inputs.${normalizedInput} }}`
}

function workflowPromptStepReference(step: WorkflowTemplateStepSummary): string {
  const normalizedStep = workflowSanitizeStepId(step.id, "")
  if (!normalizedStep) return ""
  return `参考“${step.title || normalizedStep}”：{{ steps.${normalizedStep}.output }}`
}

function workflowConditionLabel(condition: unknown, inputSpecs: Record<string, WorkflowInputDraftSpec>): string {
  const parsed = workflowParseCondition(condition)
  if (!parsed.inputId || !parsed.operator) return ""
  const label = workflowInputDisplayName(parsed.inputId, inputSpecs)
  const inputSpec = inputSpecs[parsed.inputId]
  const operatorLabel = workflowConditionOperatorOptionsForInputType(inputSpec?.type).find((item) => item.value === parsed.operator)?.label
  if (!operatorLabel) return ""
  const noValue = parsed.operator === "empty" || parsed.operator === "not_empty"
  return `当 ${label} ${operatorLabel}${noValue ? "" : ` ${parsed.value}`} 时运行`
}

function workflowRunStatusLabel(status: string): string {
  const normalized = status.trim()
  if (normalized === "pause_requested") return "暂停中"
  if (normalized === "paused") return "已暂停"
  if (normalized === "running") return "运行中"
  if (normalized === "completed") return "完成"
  if (normalized === "failed") return "失败"
  if (normalized === "partial") return "进行中"
  return "待运行"
}

function workflowStepPillTone(status: string): string {
  if (status === "completed") return "border-emerald-300/35 bg-emerald-300/[0.11] text-emerald-100 shadow-[0_0_18px_rgba(52,211,153,0.10)]"
  if (status === "running") return "border-cyan-200/50 bg-cyan-300/[0.16] text-cyan-50 shadow-[0_0_24px_rgba(34,211,238,0.22)]"
  if (status === "pause_requested" || status === "paused") return "border-amber-200/35 bg-amber-300/[0.11] text-amber-100"
  if (status === "failed") return "border-red-300/40 bg-red-400/[0.13] text-red-100"
  if (status === "blocked") return "border-amber-200/28 bg-amber-300/[0.08] text-amber-100/80"
  if (status === "ready") return "border-violet-200/32 bg-violet-300/[0.11] text-violet-100"
  return "border-white/[0.09] bg-white/[0.035] text-zinc-400"
}

function workflowStepPillMark(status: string): string {
  if (status === "completed") return "✓"
  if (status === "running") return "●"
  if (status === "failed") return "!"
  if (status === "blocked") return "…"
  if (status === "ready") return "+"
  return "·"
}

function workflowStepPillKindLabel(step: WorkflowTemplateStepSummary): string {
  const kind = workflowStepAuthoringKind(step)
  if (kind === "text") return "文"
  if (kind === "object") return "构"
  if (kind === "collection") return "列"
  if (kind === "loop") return "循"
  if (kind === "image") return "图"
  if (kind === "video") return "视"
  if (kind === "audio") return "音"
  if (kind === "plugin") return "插"
  return "步"
}

function workflowStepPillKindClass(step: WorkflowTemplateStepSummary): string {
  const kind = workflowStepAuthoringKind(step)
  if (workflowStepIsCanvasProduct(step)) return "border-cyan-200/28 bg-cyan-300/[0.14] text-cyan-50"
  if (kind === "loop") return "border-violet-200/22 bg-violet-300/[0.10] text-violet-100"
  if (kind === "collection") return "border-emerald-200/20 bg-emerald-300/[0.09] text-emerald-100"
  if (kind === "object") return "border-blue-200/18 bg-blue-300/[0.08] text-blue-100"
  return "border-white/[0.08] bg-black/18 text-zinc-300"
}

function workflowRuntimeRawStepMap(runtime: ProjectWorkflowRuntime): Map<string, ProjectWorkflowRuntimeStep> {
  const map = new Map<string, ProjectWorkflowRuntimeStep>()
  for (const step of runtime.steps || []) {
    if (step.id) map.set(step.id, step)
  }
  return map
}

type WorkflowRunDockDetailSelection = {
  runtimeId: string
  stepId: string
}

function workflowRuntimeStepNodeIds(
  rawStep?: ProjectWorkflowRuntimeStep,
  state?: WorkflowStepNodeState,
): string[] {
  const ids: string[] = []
  const push = (value: unknown) => {
    const id = workflowStringValue(value)
    if (id && !ids.includes(id)) ids.push(id)
  }
  push(rawStep?.node_id)
  push(state?.nodeId)
  for (const nodeId of rawStep?.artifact_node_ids || []) push(nodeId)
  for (const nodeId of state?.nodeIds || []) push(nodeId)
  return ids
}

function workflowRuntimeOutputTitle(output: Record<string, unknown>, index: number): string {
  return workflowStringValue(output.label)
    || workflowStringValue(output.title)
    || workflowStringValue(output.name)
    || workflowStringValue(output.key)
    || `输出 ${index + 1}`
}

function workflowRuntimeOutputValue(output: Record<string, unknown>): unknown {
  if (Object.prototype.hasOwnProperty.call(output, "value")) return output.value
  return output
}

function workflowRuntimeStepOutputItems(rawStep?: ProjectWorkflowRuntimeStep): WorkflowRunDetailOutputItem[] {
  const items: WorkflowRunDetailOutputItem[] = []
  const outputs = Array.isArray(rawStep?.outputs) ? rawStep.outputs : []
  for (const [index, rawOutput] of outputs.entries()) {
    const output = asWorkflowObject(rawOutput)
    if (!output) continue
    const value = workflowRuntimeOutputValue(output)
    if (!workflowHasValue(value)) continue
    items.push({
      title: workflowRuntimeOutputTitle(output, index),
      value,
    })
  }
  if (items.length === 0 && workflowHasValue(rawStep?.output)) {
    items.push({ title: "输出", value: rawStep?.output })
  }
  if (items.length === 0 && rawStep?.output_preview) {
    items.push({ title: "摘要", value: rawStep.output_preview })
  }
  if (rawStep?.error) {
    items.push({ title: "错误", value: rawStep.error })
  }
  return items
}

function workflowRuntimeArtifactLines(
  artifacts: Array<Record<string, unknown>>,
  nodeIds: string[],
): string[] {
  const lines: string[] = []
  const seen = new Set<string>()
  const pushLine = (value: string) => {
    const line = value.trim()
    if (!line || seen.has(line)) return
    seen.add(line)
    lines.push(line)
  }

  for (const artifact of artifacts) {
    const title = workflowStringValue(artifact.title)
    const nodeId = workflowStringValue(artifact.node_id)
    const type = workflowStringValue(artifact.type)
    const assetId = workflowStringValue(artifact.asset_id)
    const outputPath = workflowStringValue(artifact.output_path)
    const url = workflowStringValue(artifact.url)
    const parts: string[] = []
    if (title) parts.push(title)
    else if (nodeId) parts.push(`画布节点 ${nodeId}`)
    else if (assetId) parts.push(`资产 ${assetId}`)
    else if (outputPath) parts.push(`文件 ${outputPath}`)
    else if (url) parts.push(`链接 ${url}`)
    if (type) parts.push(type)
    if (nodeId && title) parts.push(`节点 ${nodeId}`)
    if (assetId && !parts.includes(`资产 ${assetId}`)) parts.push(`资产 ${assetId}`)
    if (outputPath && !parts.includes(`文件 ${outputPath}`)) parts.push(`文件 ${outputPath}`)
    if (url && !parts.includes(`链接 ${url}`)) parts.push(`链接 ${url}`)
    pushLine(parts.join(" · "))
  }
  for (const nodeId of nodeIds) pushLine(`画布节点 ${nodeId}`)
  return lines
}

interface WorkflowVideoNodeOption {
  value: string
  label: string
  detail: string
  hasMedia: boolean
}

function workflowInputSpecIsVideo(spec?: WorkflowInputDraftSpec): boolean {
  const type = String(spec?.type || "").trim().toLowerCase()
  return ["video", "video_node", "canvas_video", "media_video", "file_video"].includes(type)
}

function workflowNodeHasMediaValue(value: unknown): boolean {
  if (value == null || value === "" || value === false) return false
  if (typeof value === "string") {
    const text = value.trim()
    return Boolean(text && (
      /^https?:\/\//i.test(text)
      || text.startsWith("/api/media/")
      || text.startsWith("/api/uploads/")
      || text.startsWith("generated_videos/")
      || text.startsWith("upload:")
      || /\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(text)
    ))
  }
  if (Array.isArray(value)) return value.some(workflowNodeHasMediaValue)
  const obj = asWorkflowObject(value)
  if (!obj) return false
  for (const key of ["url", "local_url", "remote_url", "path", "local_path", "rel_path", "output_path", "video", "source_video"]) {
    if (workflowNodeHasMediaValue(obj[key])) return true
  }
  return Object.values(obj).some(workflowNodeHasMediaValue)
}

function workflowVideoNodeOptions(nodes: FlowNode[]): WorkflowVideoNodeOption[] {
  return nodes
    .filter((node) => {
      const data = node.data as Record<string, unknown> | undefined
      return String(data?.type || node.type || "").toLowerCase() === "video"
    })
    .map((node) => {
      const data = node.data as Record<string, unknown> | undefined
      const publicId = workflowStringValue(data?.publicId || data?.display_id)
      const refId = publicId || node.id
      const title = workflowStringValue(data?.title) || "视频节点"
      const status = workflowStringValue(data?.status)
      const hasMedia = workflowNodeHasMediaValue(data?.output) || workflowNodeHasMediaValue(data?.preview) || workflowNodeHasMediaValue(data?.input)
      return {
        value: `node:${refId}`,
        label: publicId ? `#${publicId} ${title}` : title,
        detail: [status ? workflowRunStatusLabel(status) : "", hasMedia ? "已有视频" : "未生成/未上传"].filter(Boolean).join(" · "),
        hasMedia,
      }
    })
}

function workflowInputValueMatchesOption(value: string, optionValue: string): boolean {
  const left = stripCanvasNodeReferenceMarker(value)
  const right = stripCanvasNodeReferenceMarker(optionValue)
  return Boolean(left && right && left === right)
}

function WorkflowVideoInputField({
  input,
  label,
  required,
  missing,
  value,
  spec,
  nodes,
  onInputValueChange,
  onUploadVideoInput,
}: {
  input: string
  label: string
  required: boolean
  missing: boolean
  value: string
  spec: WorkflowInputDraftSpec
  nodes: FlowNode[]
  onInputValueChange: (id: string, value: string) => void
  onUploadVideoInput?: (file: File) => Promise<string>
}) {
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState("")
  const options = useMemo(() => workflowVideoNodeOptions(nodes), [nodes])
  const selectedOption = options.find((option) => workflowInputValueMatchesOption(value, option.value))
  const fieldClassName = cn(
    "w-full rounded-md border bg-[#090e15] px-2 text-xs text-zinc-100 outline-none transition placeholder:text-zinc-600 focus:border-amber-200/55",
    missing ? "border-amber-200/42" : "border-white/10",
  )
  return (
    <div className="block text-[10px] font-medium text-zinc-400 sm:col-span-2">
      <div className="mb-1 flex items-center gap-1">
        {label}
        {required && <span className="text-amber-200/85">必填</span>}
      </div>
      <div className="grid gap-2 rounded-md border border-white/[0.06] bg-black/18 p-2">
        <label className="block">
          <span className="mb-1 block text-[10px] text-zinc-500">选择画布上的视频节点</span>
          <select
            value={selectedOption?.value || value}
            onChange={(event) => onInputValueChange(input, event.target.value)}
            className={cn(fieldClassName, "h-8")}
          >
            <option value="">请选择视频节点</option>
            {value && !selectedOption && <option value={value}>当前引用：{value}</option>}
            {options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}{option.detail ? ` · ${option.detail}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-[10px] text-zinc-500">或上传一个新视频</span>
          <input
            type="file"
            accept="video/*,.mp4,.webm,.mov,.m4v"
            disabled={uploading || !onUploadVideoInput}
            onChange={async (event) => {
              const file = event.target.files?.[0]
              event.target.value = ""
              if (!file || !onUploadVideoInput) return
              setUploading(true)
              setUploadError("")
              try {
                const ref = await onUploadVideoInput(file)
                onInputValueChange(input, ref)
              } catch (error) {
                setUploadError(error instanceof Error ? error.message : String(error))
              } finally {
                setUploading(false)
              }
            }}
            className={cn(fieldClassName, "h-8 file:mr-2 file:h-7 file:border-0 file:bg-cyan-300 file:px-2 file:text-[10px] file:font-semibold file:text-cyan-950 disabled:cursor-not-allowed disabled:opacity-55")}
          />
        </label>
        {value ? (
          <div className="rounded border border-white/[0.06] bg-white/[0.025] px-2 py-1.5 text-[10px] leading-4 text-zinc-400">
            当前视频来源：<span className="text-zinc-100">{value}</span>
          </div>
        ) : null}
        {uploading && <div className="text-[10px] text-cyan-100/75">正在上传并创建画布视频节点...</div>}
        {uploadError && <div className="text-[10px] text-red-200/85">{uploadError}</div>}
        {spec.description && (
          <div className="text-[10px] leading-4 text-zinc-500">{spec.description}</div>
        )}
      </div>
    </div>
  )
}

function WorkflowRunInputFields({
  inputIds,
  inputSpecs,
  inputValues,
  requiredInputIds,
  missingInputIds,
  nodes,
  onInputValueChange,
  onUploadVideoInput,
}: {
  inputIds: string[]
  inputSpecs: Record<string, WorkflowInputDraftSpec>
  inputValues: Record<string, string>
  requiredInputIds: string[]
  missingInputIds: string[]
  nodes: FlowNode[]
  onInputValueChange: (id: string, value: string) => void
  onUploadVideoInput?: (file: File) => Promise<string>
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {inputIds.map((input) => {
        const spec = inputSpecs[input] || { type: "text" }
        const options = spec.options || []
        const value = workflowInputValueForId(input, inputValues, inputSpecs)
        const required = requiredInputIds.includes(input)
        const missing = missingInputIds.includes(input)
        const label = spec.label || workflowInputLabel(input)
        const fieldClassName = cn(
          "w-full rounded-md border bg-[#090e15] px-2 text-xs text-zinc-100 outline-none transition placeholder:text-zinc-600 focus:border-amber-200/55",
          missing ? "border-amber-200/42" : "border-white/10",
        )
        const longInput = workflowIsLongInput(input, spec)
        const type = String(spec.type || "text").toLowerCase()
        if (workflowInputSpecIsVideo(spec)) {
          return (
            <WorkflowVideoInputField
              key={input}
              input={input}
              label={label}
              required={required}
              missing={missing}
              value={value}
              spec={spec}
              nodes={nodes}
              onInputValueChange={onInputValueChange}
              onUploadVideoInput={onUploadVideoInput}
            />
          )
        }
        return (
          <label key={input} className={cn("block text-[10px] font-medium text-zinc-400", longInput && "sm:col-span-2")}>
            <span className="mb-1 flex items-center gap-1">
              {label}
              {required && <span className="text-amber-200/85">必填</span>}
            </span>
            {workflowInputTypeUsesOptions(type) && options.length > 0 ? (
              <select
                value={value}
                onChange={(event) => onInputValueChange(input, event.target.value)}
                className={cn(fieldClassName, "h-8")}
              >
                <option value="">请选择</option>
                {options.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            ) : type === "boolean" || type === "checkbox" ? (
              <select
                value={value.toLowerCase() === "true" || value === "是" ? "true" : value.toLowerCase() === "false" || value === "否" ? "false" : ""}
                onChange={(event) => onInputValueChange(input, event.target.value)}
                className={cn(fieldClassName, "h-8")}
              >
                <option value="">未设置</option>
                <option value="true">是</option>
                <option value="false">否</option>
              </select>
            ) : longInput ? (
              <textarea
                value={value}
                onChange={(event) => onInputValueChange(input, event.target.value)}
                placeholder={spec.description || workflowInputPlaceholder(input)}
                rows={4}
                className={cn(fieldClassName, "min-h-28 resize-none py-1.5 leading-4")}
              />
            ) : (
              <input
                type={type === "number" || type === "integer" ? "number" : "text"}
                value={value}
                onChange={(event) => onInputValueChange(input, event.target.value)}
                placeholder={spec.description || workflowInputPlaceholder(input)}
                className={cn(fieldClassName, "h-8")}
              />
            )}
            {spec.description && (
              <span className="mt-1 block text-[10px] leading-4 text-zinc-500">{spec.description}</span>
            )}
          </label>
        )
      })}
    </div>
  )
}

function workflowRuntimeTreeColumns(steps: WorkflowTemplateStepSummary[]): WorkflowTemplateStepSummary[][] {
  if (steps.length === 0) return []
  const byId = new Map(steps.map((step) => [step.id, step]))
  const indexById = new Map(steps.map((step, index) => [step.id, index]))
  const visiting = new Set<string>()
  const cache = new Map<string, number>()
  const levelFor = (step: WorkflowTemplateStepSummary): number => {
    if (cache.has(step.id)) return cache.get(step.id) || 0
    if (visiting.has(step.id)) return 0
    visiting.add(step.id)
    const depLevels = (step.depends_on || [])
      .map((dep) => byId.get(dep))
      .filter((dep): dep is WorkflowTemplateStepSummary => Boolean(dep))
      .map((dep) => levelFor(dep) + 1)
    visiting.delete(step.id)
    const level = depLevels.length > 0 ? Math.max(...depLevels) : 0
    cache.set(step.id, level)
    return level
  }
  const columns: WorkflowTemplateStepSummary[][] = []
  for (const step of steps) {
    const level = levelFor(step)
    if (!columns[level]) columns[level] = []
    columns[level].push(step)
  }
  return columns
    .filter(Boolean)
    .map((column) => column.sort((left, right) => (indexById.get(left.id) || 0) - (indexById.get(right.id) || 0)))
}

function WorkflowRunDock({
  open,
  runtimes,
  templates,
  canvasNodes,
  selectedTemplateId,
  runningIds,
  runningAllIds,
  pausingIds,
  deletingIds,
  expandedIds,
  detail,
  errors,
  inputValuesByInstance,
  onOpenChange,
  onTemplateChange,
  onAddRun,
  onRunNext,
  onRunAll,
  onPauseRun,
  onRunStep,
  onDeleteRun,
  onInspectStep,
  onCloseDetail,
  onInputValueChange,
  onUploadVideoInput,
  onToggleExpanded,
}: {
  open: boolean
  runtimes: ProjectWorkflowRuntime[]
  templates: WorkflowTemplateSummary[]
  canvasNodes: FlowNode[]
  selectedTemplateId: string
  runningIds: string[]
  runningAllIds: string[]
  pausingIds: string[]
  deletingIds: string[]
  expandedIds: string[]
  detail: WorkflowRunDockDetailSelection | null
  errors: Record<string, string>
  inputValuesByInstance: WorkflowInputValuesByInstance
  onOpenChange: (open: boolean) => void
  onTemplateChange: (templateId: string) => void
  onAddRun: () => void
  onRunNext: (runtime: ProjectWorkflowRuntime) => void
  onRunAll: (runtime: ProjectWorkflowRuntime) => void
  onPauseRun: (runtime: ProjectWorkflowRuntime) => void
  onRunStep: (runtime: ProjectWorkflowRuntime, stepId: string) => void
  onDeleteRun: (runtime: ProjectWorkflowRuntime) => void
  onInspectStep: (
    runtime: ProjectWorkflowRuntime,
    step: WorkflowTemplateStepSummary,
    rawStep?: ProjectWorkflowRuntimeStep,
    state?: WorkflowStepNodeState,
  ) => void
  onCloseDetail: () => void
  onInputValueChange: (runtimeId: string, templateId: string, id: string, value: string) => void
  onUploadVideoInput?: (file: File) => Promise<string>
  onToggleExpanded: (runtimeId: string) => void
}) {
  const [inputPanelOverrides, setInputPanelOverrides] = useState<Record<string, boolean>>({})
  const runningCount = runtimes.filter((runtime) => {
    const template = templates.find((item) => item.id === workflowRuntimeTemplateId(runtime))
    const progress = workflowRuntimeProgress(runtime, workflowRuntimeStepSummariesFromPayload(runtime, template?.steps || []))
    return runningIds.includes(workflowRuntimeId(runtime)) || runtime.status === "running" || progress.running > 0
  }).length
  const activeDetailRuntime = detail ? runtimes.find((runtime) => workflowRuntimeId(runtime) === detail.runtimeId) : undefined
  const activeDetailRuntimeId = workflowRuntimeId(activeDetailRuntime)
  const activeDetailTemplateId = workflowRuntimeTemplateId(activeDetailRuntime)
  const activeDetailTemplate = templates.find((item) => item.id === activeDetailTemplateId)
  const activeDetailInputIds = workflowInputIdsForTemplateSummary(activeDetailTemplate)
  const activeDetailRequiredInputIds = workflowRequiredInputIdsForTemplateSummary(activeDetailTemplate)
  const activeDetailInputSpecs = workflowInputSpecsForTemplateSummary(activeDetailTemplate)
  const activeDetailInputValues = activeDetailRuntimeId
    ? inputValuesByInstance[activeDetailRuntimeId] || workflowRuntimeInputValues(activeDetailRuntime) || {}
    : {}
  const activeDetailMissingInputIds = workflowMissingInputIds(activeDetailInputIds, activeDetailInputValues, activeDetailRequiredInputIds, activeDetailInputSpecs)
  const activeDetailSteps = activeDetailRuntime
    ? workflowRuntimeStepSummariesFromPayload(activeDetailRuntime, activeDetailTemplate?.steps || [])
    : []
  const activeDetailStep = detail ? activeDetailSteps.find((step) => step.id === detail.stepId) : undefined
  const activeDetailRawStepMap = activeDetailRuntime ? workflowRuntimeRawStepMap(activeDetailRuntime) : new Map<string, ProjectWorkflowRuntimeStep>()
  const activeDetailStepStates = activeDetailRuntime ? workflowRuntimeStepStatesFromPayload(activeDetailRuntime) : {}
  const activeDetailRawStep = activeDetailStep ? activeDetailRawStepMap.get(activeDetailStep.id) : undefined
  const activeDetailState = activeDetailStep ? activeDetailStepStates[activeDetailStep.id] : undefined
  const activeDetailStatus = workflowStringValue(activeDetailRawStep?.execution_state)
    || workflowStringValue(activeDetailState?.status)
    || workflowStringValue(activeDetailRawStep?.status)
    || "idle"
  const activeDetailWaitingOn = Array.isArray(activeDetailRawStep?.waiting_on) ? activeDetailRawStep.waiting_on : []
  const activeDetailRunning = activeDetailStatus === "running" || Boolean(activeDetailRuntimeId && runningIds.includes(activeDetailRuntimeId))
  const activeDetailCompleted = activeDetailStatus === "completed" && !Boolean(activeDetailRawStep?.stale)
  const activeDetailVirtual = Boolean(activeDetailRawStep?.virtual || activeDetailState?.virtual)
    || workflowStepIsInputStep(activeDetailStep, activeDetailTemplate ? workflowInputIdsForTemplateSummary(activeDetailTemplate) : [])
  const activeDetailBusy = Boolean(activeDetailRuntimeId && (
    runningIds.includes(activeDetailRuntimeId)
    || pausingIds.includes(activeDetailRuntimeId)
    || deletingIds.includes(activeDetailRuntimeId)
  ))
  const activeDetailRunnable = Boolean(
    activeDetailRuntime &&
    activeDetailStep &&
    workflowStringValue(activeDetailStep.role) !== "repeat_group" &&
    activeDetailMissingInputIds.length === 0 &&
    !activeDetailBusy &&
    !activeDetailRunning &&
    !activeDetailVirtual &&
    activeDetailWaitingOn.length === 0,
  )
  const activeDetailNodeIds = workflowRuntimeStepNodeIds(activeDetailRawStep, activeDetailState)
  const activeDetailOutputs = workflowRuntimeStepOutputItems(activeDetailRawStep)
  const activeDetailOutputText = workflowRuntimeDetailOutputText(activeDetailOutputs)
  const activeDetailArtifacts = Array.isArray(activeDetailRawStep?.artifacts) ? activeDetailRawStep.artifacts : []
  const activeDetailArtifactLines = workflowRuntimeArtifactLines(activeDetailArtifacts, activeDetailNodeIds)
  const showDetailDrawer = Boolean(activeDetailRuntime && activeDetailStep)
  const showSideDrawer = showDetailDrawer
  if (!open) {
    return (
      <div data-openreel-workflow-ui="true" className="absolute bottom-5 left-1/2 z-40 -translate-x-1/2">
        <button
          type="button"
          onClick={() => onOpenChange(true)}
          className="group flex h-10 items-center gap-2 rounded-full border border-cyan-200/22 bg-[#10151d]/90 px-4 text-xs font-semibold text-zinc-100 shadow-2xl shadow-black/40 backdrop-blur transition hover:border-cyan-200/45 hover:bg-[#121a25]/96"
        >
          <span className={cn("h-2 w-2 rounded-full", runningCount > 0 ? "bg-cyan-300 shadow-[0_0_14px_rgba(34,211,238,0.75)]" : "bg-zinc-500")} />
          <span>流程</span>
          <span className="rounded border border-white/[0.08] bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-zinc-300">{runtimes.length}</span>
          {runningCount > 0 && <span className="text-[10px] text-cyan-100">{runningCount} 运行中</span>}
        </button>
      </div>
    )
  }

  return (
    <>
      {showDetailDrawer && activeDetailRuntime && activeDetailStep && (
        <aside
          data-openreel-workflow-ui="true"
          className="absolute bottom-0 right-0 top-0 z-[70] flex w-[440px] max-w-[42vw] flex-col overflow-hidden border-l border-cyan-200/14 bg-[#0b1118] shadow-[-18px_0_40px_rgba(0,0,0,0.38)] max-md:inset-x-0 max-md:bottom-0 max-md:top-auto max-md:max-h-[72vh] max-md:w-auto max-md:max-w-none max-md:border-l-0 max-md:border-t"
          onClick={(event) => event.stopPropagation()}
          onDoubleClick={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
          onPointerMove={(event) => event.stopPropagation()}
          onPointerUp={(event) => event.stopPropagation()}
          onWheel={(event) => event.stopPropagation()}
        >
          <div className="flex items-start gap-3 border-b border-cyan-200/10 bg-[#0f1722] px-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-cyan-50">流程步骤详情</div>
              <div className="mt-1 truncate text-[11px] text-cyan-100/55">{workflowRuntimeTemplateName(activeDetailRuntime, templates)}</div>
            </div>
            <button
              type="button"
              onClick={onCloseDetail}
              className="h-8 shrink-0 rounded-md border border-white/10 px-2.5 text-[11px] text-zinc-300 transition hover:bg-white/[0.06]"
            >
              关闭
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
            <div className="min-w-0 truncate text-base font-semibold text-zinc-100">{activeDetailStep.title || activeDetailStep.id}</div>
            <div className="mt-1 text-[10px] text-zinc-500">{activeDetailStep.id}</div>
            <div className="mt-3 grid grid-cols-2 gap-1.5 text-[10px] text-zinc-400">
              <div className="rounded border border-white/[0.06] bg-black/16 px-2 py-1">
                <span className="text-zinc-600">运行</span>
                <span className="ml-1 text-zinc-200">{activeDetailRawStep?.run_count || activeDetailState?.runCount || 0} 次</span>
              </div>
              <div className="rounded border border-white/[0.06] bg-black/16 px-2 py-1">
                <span className="text-zinc-600">输入</span>
                <span className="ml-1 text-zinc-200">{activeDetailState?.resolvedInputCount || 0} 项</span>
              </div>
              <div className="rounded border border-white/[0.06] bg-black/16 px-2 py-1">
                <span className="text-zinc-600">输出</span>
                <span className="ml-1 text-zinc-200">{activeDetailOutputs.length || activeDetailState?.outputCount || 0} 项</span>
              </div>
              <div className="rounded border border-white/[0.06] bg-black/16 px-2 py-1">
                <span className="text-zinc-600">产物</span>
                <span className="ml-1 text-zinc-200">{activeDetailNodeIds.length || activeDetailState?.artifactCount || 0} 个</span>
              </div>
            </div>
            {activeDetailWaitingOn.length > 0 && (
              <div className="mt-3 rounded border border-amber-200/14 bg-amber-300/[0.045] px-2 py-1.5 text-[10px] leading-4 text-amber-100/80">
                等待：{activeDetailWaitingOn.join("、")}
              </div>
            )}
            {activeDetailVirtual && activeDetailInputIds.length > 0 && (
              <section className="mt-3 rounded-md border border-amber-200/18 bg-amber-300/[0.055] p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div>
                    <div className="text-xs font-semibold text-amber-100">运行输入</div>
                    <div className="mt-0.5 text-[10px] leading-4 text-amber-100/60">这里填写本次流程运行需要的内容。</div>
                  </div>
                  <div className="shrink-0 text-[10px] text-amber-100/65">
                    {workflowInputSummary(activeDetailInputIds, activeDetailInputValues, activeDetailRequiredInputIds, activeDetailInputSpecs)}
                  </div>
                </div>
                <WorkflowRunInputFields
                  inputIds={activeDetailInputIds}
                  inputSpecs={activeDetailInputSpecs}
                  inputValues={activeDetailInputValues}
                  requiredInputIds={activeDetailRequiredInputIds}
                  missingInputIds={activeDetailMissingInputIds}
                  nodes={canvasNodes}
                  onInputValueChange={(id, value) => onInputValueChange(activeDetailRuntimeId, activeDetailTemplateId, id, value)}
                  onUploadVideoInput={onUploadVideoInput}
                />
              </section>
            )}
            {!activeDetailVirtual && (
              <button
                type="button"
                onClick={() => activeDetailRunnable && onRunStep(activeDetailRuntime, activeDetailStep.id)}
                disabled={!activeDetailRunnable}
                className="mt-3 h-8 w-full rounded-md border border-cyan-200/25 bg-cyan-300/10 px-2 text-xs font-semibold text-cyan-100 transition hover:bg-cyan-300/16 disabled:cursor-not-allowed disabled:opacity-40"
                title={activeDetailMissingInputIds.length > 0 ? `先输入：${activeDetailMissingInputIds.map(workflowInputLabel).join("、")}` : activeDetailWaitingOn.length > 0 ? `等待：${activeDetailWaitingOn.join("、")}` : activeDetailCompleted ? "重新运行此步" : "运行此步"}
              >
                {activeDetailRunning ? "运行中" : activeDetailMissingInputIds.length > 0 ? "先输入" : activeDetailCompleted ? "重新运行此步" : "运行此步"}
              </button>
            )}
            {!activeDetailVirtual && (
              <div className="mt-3">
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-zinc-500">输出</div>
                {activeDetailOutputs.length === 0 ? (
                  <div className="rounded-md border border-white/[0.06] bg-black/18 px-2 py-2 text-xs text-zinc-500">
                    这个步骤还没有运行结果。
                  </div>
                ) : (
                  <div className="overflow-hidden rounded-md border border-white/[0.07] bg-black/18">
                    <div className="border-b border-white/[0.06] px-3 py-2 text-xs font-semibold text-zinc-300">正文</div>
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words px-3 py-3 font-sans text-sm leading-6 text-zinc-100">
                      {activeDetailOutputText}
                    </pre>
                  </div>
                )}
              </div>
            )}
            {!activeDetailVirtual && activeDetailArtifactLines.length > 0 && (
              <div className="mt-3">
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-zinc-500">产物引用</div>
                <div className="grid gap-1.5 rounded-md border border-white/[0.06] bg-black/18 px-2 py-2 text-xs leading-5 text-zinc-300">
                  {activeDetailArtifactLines.map((line) => (
                    <div key={line} className="break-words">{line}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </aside>
      )}
      <div
        data-openreel-workflow-ui="true"
        className={cn(
          "absolute bottom-5 z-40 rounded-2xl border border-white/[0.10] bg-[#0e141d]/96 shadow-2xl shadow-black/50 backdrop-blur-xl",
          showSideDrawer
            ? "left-4 right-[456px] max-lg:right-[432px] max-md:left-3 max-md:right-3"
            : "left-[43%] w-[min(760px,calc(100%-440px))] -translate-x-1/2 max-md:left-3 max-md:right-3 max-md:w-auto max-md:translate-x-0",
        )}
        onClick={(event) => event.stopPropagation()}
        onPointerDown={(event) => event.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-white/[0.08] px-3 py-2">
        <div className="min-w-0">
          <div className="text-xs font-semibold text-zinc-100">流程运行</div>
          <div className="text-[10px] text-zinc-500">可添加多个流程，并行运行；编辑仍在流程面板</div>
        </div>
        <div className="ml-auto flex min-w-0 items-center gap-1.5">
          <select
            value={selectedTemplateId}
            onChange={(event) => onTemplateChange(event.target.value)}
            className="h-8 w-[190px] min-w-[150px] rounded-md border border-white/10 bg-black/30 px-2 text-xs text-zinc-100 outline-none transition focus:border-cyan-300/45 max-sm:w-[140px]"
          >
            {templates.length === 0 ? (
              <option value="">暂无流程</option>
            ) : templates.map((template) => (
              <option key={template.id} value={template.id}>{template.name || template.id}</option>
            ))}
          </select>
          <button
            type="button"
            onClick={onAddRun}
            disabled={!selectedTemplateId}
            className="h-8 shrink-0 whitespace-nowrap rounded-full bg-cyan-300 px-3 text-xs font-semibold text-cyan-950 transition hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-45"
          >
            添加流程
          </button>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="h-8 shrink-0 whitespace-nowrap rounded-full border border-white/10 px-3 text-xs text-zinc-300 transition hover:bg-white/[0.06]"
          >
            隐藏
          </button>
        </div>
      </div>
        <div className="max-h-[48vh] overflow-y-auto p-2">
        {runtimes.length === 0 ? (
          <div className="flex min-h-16 items-center justify-center rounded-xl border border-dashed border-white/[0.10] bg-white/[0.025] text-xs text-zinc-500">
            添加一个流程后，可以在这里运行并查看每一步状态。
          </div>
        ) : (
          <div className="grid gap-2">
            {runtimes.map((runtime) => {
              const runtimeId = workflowRuntimeId(runtime)
              const templateId = workflowRuntimeTemplateId(runtime)
              const template = templates.find((item) => item.id === templateId)
              const inputIds = workflowInputIdsForTemplateSummary(template)
              const requiredInputIds = workflowRequiredInputIdsForTemplateSummary(template)
              const inputSpecs = workflowInputSpecsForTemplateSummary(template)
              const runtimeInputValues = inputValuesByInstance[runtimeId] || workflowRuntimeInputValues(runtime) || {}
              const missingInputIds = workflowMissingInputIds(inputIds, runtimeInputValues, requiredInputIds, inputSpecs)
              const inputBlocked = missingInputIds.length > 0
              const inputPanelOpen = inputIds.length > 0 && (inputPanelOverrides[runtimeId] ?? false)
              const rawStepMap = workflowRuntimeRawStepMap(runtime)
              const mergedSteps = workflowRuntimeStepSummariesFromPayload(runtime, template?.steps || [])
              const nodeStates = workflowRuntimeStepStatesFromPayload(runtime)
              const visibleSteps = expandedIds.includes(runtimeId)
                ? mergedSteps
                : workflowDynamicVisibleSteps(mergedSteps, nodeStates).slice(0, 9)
              const treeColumns = workflowRuntimeTreeColumns(visibleSteps)
              const progress = workflowRuntimeProgress(runtime, mergedSteps)
              const pauseRequested = Boolean(runtime.pause_requested) || workflowStringValue(runtime.status) === "pause_requested"
              const paused = workflowStringValue(runtime.status) === "paused"
              const pausing = pausingIds.includes(runtimeId) || pauseRequested
              const hasRunningSteps = progress.running > 0 || workflowStringValue(runtime.status) === "running"
              const busy = runningIds.includes(runtimeId) || hasRunningSteps
              const runningAll = runningAllIds.includes(runtimeId)
              const deleting = deletingIds.includes(runtimeId)
              const done = progress.total > 0 && progress.completed >= progress.total && progress.failed === 0
              const status = pausing ? "pause_requested" : busy ? "running" : workflowStringValue(runtime.status) || (done ? "completed" : "idle")
              const runningAllActive = runningAll || (hasRunningSteps && !pausing && !paused && !deleting && !done)
              const error = errors[runtimeId]
              return (
                <div key={runtimeId} className="rounded-xl border border-white/[0.08] bg-white/[0.035] p-2 shadow-xl shadow-black/20">
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => onToggleExpanded(runtimeId)}
                      className={cn(
                        "h-7 min-w-0 flex-1 rounded-full border px-2.5 text-left text-xs font-semibold transition",
                        status === "running"
                          ? "border-cyan-200/32 bg-cyan-300/[0.08] text-cyan-100"
                          : status === "pause_requested" || status === "paused"
                          ? "border-amber-200/28 bg-amber-300/[0.07] text-amber-100"
                          : "border-white/[0.08] bg-black/18 text-zinc-100 hover:bg-white/[0.05]",
                      )}
                    >
                      <span className="flex min-w-0 items-center gap-2">
                        <span className={cn(
                          "h-1.5 w-1.5 shrink-0 rounded-full",
                          status === "running"
                            ? "bg-cyan-300"
                            : status === "pause_requested" || status === "paused"
                            ? "bg-amber-300"
                            : status === "failed"
                            ? "bg-red-300"
                            : status === "completed"
                            ? "bg-emerald-300"
                            : "bg-zinc-500",
                        )} />
                        <span className="truncate">{workflowRuntimeTemplateName(runtime, templates)}</span>
                        <span className="shrink-0 text-[10px] text-zinc-500">{progress.completed}/{progress.total || mergedSteps.length}</span>
                        <span className="shrink-0 text-[10px] text-zinc-500">{workflowRunStatusLabel(status)}</span>
                      </span>
                    </button>
                    {inputIds.length > 0 && (
                      <button
                        type="button"
                        onClick={() => setInputPanelOverrides((current) => ({
                          ...current,
                          [runtimeId]: !inputPanelOpen,
                        }))}
                        className={cn(
                          "h-7 shrink-0 rounded-full border px-2.5 text-[11px] font-semibold transition",
                          inputBlocked
                            ? "border-amber-200/35 bg-amber-300/12 text-amber-100 hover:bg-amber-300/18"
                            : inputPanelOpen
                            ? "border-cyan-200/30 bg-cyan-300/12 text-cyan-100"
                            : "border-white/[0.09] bg-white/[0.035] text-zinc-300 hover:bg-white/[0.07]",
                        )}
                        title={inputPanelOpen ? "收起本次运行输入" : "填写本次运行输入"}
                      >
                        输入 {workflowInputSummary(inputIds, runtimeInputValues, requiredInputIds, inputSpecs).split(" ")[0]}
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onRunNext(runtime)}
                      disabled={busy || pausing || deleting || done || inputBlocked}
                      title={inputBlocked ? `先输入：${missingInputIds.map(workflowInputLabel).join("、")}` : "运行下一步"}
                      className={cn(
                        "h-7 shrink-0 rounded-full px-2.5 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-45",
                        "bg-cyan-300 text-cyan-950 hover:bg-cyan-200",
                      )}
                    >
                      {busy ? "运行中" : pausing ? "暂停中" : "运行一步"}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (runningAllActive) {
                          onPauseRun(runtime)
                          return
                        }
                        onRunAll(runtime)
                      }}
                      disabled={(busy && !runningAllActive) || pausing || deleting || done || (inputBlocked && !runningAllActive)}
                      title={inputBlocked && !runningAllActive ? `先输入：${missingInputIds.map(workflowInputLabel).join("、")}` : runningAllActive ? "暂停当前流程" : "一键执行当前流程"}
                      className={cn(
                        "h-7 shrink-0 rounded-full border px-2.5 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-45",
                        runningAllActive
                          ? "border-amber-200/35 bg-amber-300/12 text-amber-100 hover:bg-amber-300/18"
                          : "border-cyan-200/25 bg-cyan-300/10 text-cyan-100 hover:bg-cyan-300/16",
                      )}
                    >
                      {pausing ? "暂停中" : runningAllActive ? "暂停" : busy ? "运行中" : paused ? "继续执行" : "一键执行"}
                    </button>
                    <button
                      type="button"
                      onClick={() => onDeleteRun(runtime)}
                      disabled={busy || pausing || deleting}
                      className="h-7 shrink-0 rounded-full border border-red-300/20 px-2.5 text-[11px] text-red-100 transition hover:bg-red-500/12 disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      {deleting ? "删除中" : "删除"}
                    </button>
                  </div>
                  {inputPanelOpen && (
                    <section className="mt-2 rounded-xl border border-amber-200/16 bg-amber-300/[0.045] p-3">
                      <div className="mb-2 flex items-start justify-between gap-3">
                        <div>
                          <div className="text-xs font-semibold text-amber-100">本次运行输入</div>
                          <div className="mt-0.5 text-[10px] leading-4 text-amber-100/55">这些值只属于当前流程实例，填写完成后即可运行。</div>
                        </div>
                        <span className="shrink-0 rounded-full border border-amber-200/15 bg-black/16 px-2 py-0.5 text-[10px] text-amber-100/70">
                          {workflowInputSummary(inputIds, runtimeInputValues, requiredInputIds, inputSpecs)}
                        </span>
                      </div>
                      <WorkflowRunInputFields
                        inputIds={inputIds}
                        inputSpecs={inputSpecs}
                        inputValues={runtimeInputValues}
                        requiredInputIds={requiredInputIds}
                        missingInputIds={missingInputIds}
                        nodes={canvasNodes}
                        onInputValueChange={(id, value) => {
                          setInputPanelOverrides((current) => ({ ...current, [runtimeId]: true }))
                          onInputValueChange(runtimeId, templateId, id, value)
                        }}
                        onUploadVideoInput={onUploadVideoInput}
                      />
                    </section>
                  )}
                  <div className="mt-2 overflow-x-auto pb-1">
                    <div className="flex min-w-max items-start gap-3">
                      {treeColumns.map((column, columnIndex) => (
                        <div key={`${runtimeId}:column:${columnIndex}`} className="relative flex min-w-[136px] flex-col gap-1.5">
                          {columnIndex > 0 && (
                            <span className="pointer-events-none absolute -left-3 top-1/2 h-px w-3 bg-cyan-200/18" />
                          )}
                          {column.length > 1 && (
                            <span className="pointer-events-none absolute left-0 top-3 bottom-3 w-px bg-cyan-200/10" />
                          )}
                          {column.map((step) => {
                            const rawStep = rawStepMap.get(step.id)
                            const state = nodeStates[step.id]
                            const executionState = workflowStringValue(rawStep?.execution_state)
                            const statusValue = executionState || workflowStringValue(state?.status) || "idle"
                            const waitingOn = Array.isArray(rawStep?.waiting_on) ? rawStep.waiting_on : []
                            const stepIndex = Math.max(0, mergedSteps.findIndex((item) => item.id === step.id)) + 1
                            const productStep = workflowStepIsCanvasProduct(step)
                            const inputStepPill = workflowStepIsInputStep(step, inputIds)
                            return (
                              <button
                                key={`${runtimeId}:${step.id}`}
                                type="button"
                                onClick={() => onInspectStep(runtime, step, rawStep, state)}
                                className={cn(
                                  "relative flex h-7 min-w-0 items-center gap-1 rounded-full border px-1.5 text-left text-[11px] font-medium transition",
                                  workflowStepPillTone(waitingOn.length > 0 && statusValue === "idle" ? "blocked" : statusValue),
                                  productStep && "ring-1 ring-cyan-200/12",
                                  inputStepPill && missingInputIds.length > 0 && "ring-1 ring-amber-200/35",
                                )}
                                title={waitingOn.length > 0 ? `等待：${waitingOn.join("、")}` : step.title || step.id}
                              >
                                <span className="w-3 shrink-0 text-[9px] text-current/55">{stepIndex}</span>
                                <span className="shrink-0 text-[9px]">{workflowStepPillMark(waitingOn.length > 0 && statusValue === "idle" ? "blocked" : statusValue)}</span>
                                <span className={cn("shrink-0 rounded border px-1 py-0.5 text-[8px] leading-none", workflowStepPillKindClass(step))}>
                                  {workflowStepPillKindLabel(step)}
                                </span>
                                <span className="min-w-0 truncate">{step.title || step.id}</span>
                              </button>
                            )
                          })}
                        </div>
                      ))}
                    </div>
                    {visibleSteps.length < mergedSteps.length && (
                      <button
                        type="button"
                        onClick={() => onToggleExpanded(runtimeId)}
                        className="mt-1 h-7 rounded-md border border-white/[0.08] bg-black/18 px-2 text-[11px] text-zinc-400 hover:bg-white/[0.06]"
                      >
                        展开 {mergedSteps.length - visibleSteps.length} 步
                      </button>
                    )}
                  </div>
                  {error ? (
                    <div className="mt-2 rounded border border-red-300/20 bg-red-500/10 px-2 py-1.5 text-[11px] text-red-100">
                      {error}
                    </div>
                  ) : null}
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

function workflowSetObjectEntry(record: Record<string, unknown> | undefined, key: string, rawValue: string): Record<string, unknown> {
  const next = { ...(record || {}) }
  const normalizedKey = workflowSanitizeStepId(key || "key", "key")
  next[normalizedKey] = rawValue
  return next
}

function workflowRenameObjectEntry(record: Record<string, unknown> | undefined, oldKey: string, rawKey: string): Record<string, unknown> {
  const nextKey = workflowSanitizeStepId(rawKey || "key", "key")
  const next: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(record || {})) {
    next[key === oldKey ? nextKey : key] = value
  }
  if (!Object.prototype.hasOwnProperty.call(next, nextKey)) next[nextKey] = ""
  return next
}

function workflowRemoveObjectEntry(record: Record<string, unknown> | undefined, key: string): Record<string, unknown> {
  const next = { ...(record || {}) }
  delete next[key]
  return next
}

function workflowAddObjectEntry(record: Record<string, unknown> | undefined, base = "param"): Record<string, unknown> {
  const entries = Object.keys(record || {})
  const key = workflowUniqueInputId(base, entries)
  return { ...(record || {}), [key]: "" }
}

function workflowPluginDefinitionForStep(
  step: WorkflowTemplateStepSummary,
  nodeTypes: WorkflowNodeTypeDefinition[],
): WorkflowNodeTypeDefinition | undefined {
  const pluginConfig = asWorkflowObject(step.plugin)
  const plugin = workflowStringValue(pluginConfig?.id)
  const action = workflowStringValue(pluginConfig?.action)
  return nodeTypes.find((item) => (
    (plugin ? item.plugin_id === plugin || item.plugin_name === plugin : true) &&
    (action ? item.type === action || item.id === action : item.id === step.id)
  ))
}

function workflowDefinitionFieldKey(field: Record<string, unknown>, fallback: string): string {
  return workflowStringValue(field.id || field.key || field.name || field.field || fallback) || fallback
}

function workflowDefinitionFieldLabel(field: Record<string, unknown>, fallback: string): string {
  return workflowStringValue(field.label || field.title || field.name || field.id || fallback) || fallback
}

function workflowDefinitionFieldType(field: Record<string, unknown>): string {
  return workflowStringValue(field.type || field.kind || field.input_type).toLowerCase() || "text"
}

function workflowDefinitionFieldOptions(field: Record<string, unknown>): Array<{ value: string; label: string }> {
  const raw = Array.isArray(field.options)
    ? field.options
    : Array.isArray(field.enum)
    ? field.enum
    : []
  return raw
    .map((item): { value: string; label: string } | null => {
      if (item && typeof item === "object" && !Array.isArray(item)) {
        const option = item as Record<string, unknown>
        const value = workflowStringValue(option.value || option.id || option.key || option.name)
        if (!value) return null
        return { value, label: workflowStringValue(option.label || option.title || option.name || value) || value }
      }
      const value = workflowStringValue(item)
      return value ? { value, label: value } : null
    })
    .filter((item): item is { value: string; label: string } => Boolean(item))
}

function workflowStepPhaseLabel(step: WorkflowTemplateStepSummary): string {
  const ui = asWorkflowObject(step.ui)
  const explicit = workflowReadableLabel(ui?.phase_label || ui?.group_label || ui?.label)
  const phase = workflowReadableLabel(step.phase || step.group || step.kind)
  const repeat = workflowStepRepeatLabel(step)
  if (repeat && phase && repeat !== phase) return `${repeat} · ${phase}`
  if (repeat) return repeat
  if (phase) return phase
  if (step.collection || step.foreach) return "集合"
  return WORKFLOW_NODE_TYPE_LABEL[step.node_type] || "流程"
}

function workflowNodeTypeCategoryLabel(value: unknown): string {
  const text = String(value || "").trim().toLowerCase()
  const labels: Record<string, string> = {
    core: "内置节点",
    workflow: "工作流",
    image: "图片",
    video: "视频",
    audio: "音频",
    text: "文本",
    plugin: "插件",
  }
  return labels[text] || String(value || "其他")
}

function workflowStepPhaseKey(step: WorkflowTemplateStepSummary, fallbackIndex: number): string {
  const repeat = workflowStringValue(step.repeat_group_id || step.repeat_group_label || step.repeat_group_index)
  const phase = workflowStringValue(step.phase || step.group || step.kind || step.node_type)
  return `${repeat || "root"}:${phase || `phase-${fallbackIndex}`}`.toLowerCase()
}

function workflowStepOutputLabel(step: WorkflowTemplateStepSummary): string {
  if (workflowStepAuthoringKind(step) === "text") return "输出正文"
  if (workflowStepAuthoringKind(step) === "collection") return "输出集合"
  if (workflowStepAuthoringKind(step) === "object") return "输出结构化对象"
  if (workflowStepAuthoringKind(step) === "loop") return "逐项执行内部步骤"
  if (workflowStepAuthoringKind(step) === "image") return "画布图片"
  if (workflowStepAuthoringKind(step) === "video") return "画布视频"
  if (workflowStepAuthoringKind(step) === "audio") return "画布音频"
  if (workflowStepIsFlowOnly(step)) return "只传给后续步骤"
  return "流程内部输出"
}

function workflowBuildPhaseGroups(
  steps: WorkflowTemplateStepSummary[],
  nodeStates: Record<string, WorkflowStepNodeState>,
): WorkflowPhaseGroup[] {
  const groups: WorkflowPhaseGroup[] = []
  for (const [index, step] of steps.entries()) {
    const key = workflowStepPhaseKey(step, index)
    let group = groups[groups.length - 1]
    if (!group || group.key !== key) {
      group = {
        key,
        title: workflowStepPhaseLabel(step),
        steps: [],
        completedCount: 0,
        runningCount: 0,
        failedCount: 0,
        canvasOutputCount: 0,
        runtimeOnlyCount: 0,
      }
      groups.push(group)
    }
    group.steps.push(step)
    if (workflowStepIsFlowOnly(step)) group.runtimeOnlyCount += 1
    else group.canvasOutputCount += 1
    const state = nodeStates[step.id]
    if (state?.status === "running") group.runningCount += 1
    if (state?.status === "failed") group.failedCount += 1
    if (state?.status === "completed") group.completedCount += 1
  }
  return groups
}

function workflowPhaseGroupStateLabel(group: WorkflowPhaseGroup): string {
  if (group.runningCount > 0) return "运行中"
  if (group.failedCount > 0) return "有失败"
  if (group.steps.length > 0 && group.completedCount === group.steps.length) return "完成"
  return `${group.completedCount}/${group.steps.length}`
}

function workflowPhaseGroupMetaLabel(group: WorkflowPhaseGroup): string {
  const parts = [`${group.steps.length} 步`]
  if (group.canvasOutputCount > 0) parts.push(`${group.canvasOutputCount} 个产物`)
  if (group.runtimeOnlyCount > 0) parts.push(`${group.runtimeOnlyCount} 个中间步骤`)
  return parts.join(" · ")
}

function workflowStringValue(value: unknown): string {
  if (typeof value === "string") return value.trim()
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

function workflowInstanceScopeLabel(scope: unknown): string {
  const obj = asWorkflowObject(scope)
  if (!obj) return ""
  const episode = workflowStringValue(obj.episode || obj.episode_index || obj.episodeIndex)
  const segment = workflowStringValue(obj.segment || obj.segment_index || obj.segmentIndex)
  if (episode && segment) return `第${episode}集第${segment}段`
  if (episode) return `第${episode}集`
  const name = workflowStringValue(obj.name || obj.title || obj.label)
  if (name) return name
  return ""
}

function workflowTitleLooksMachine(value: string): boolean {
  const text = value.trim()
  if (!text) return false
  const tail = text.split("·").pop()?.trim() || text
  return /^[A-Za-z][A-Za-z0-9 _/-]*$/.test(tail)
}

function workflowDisplayStepTitle(
  step: Pick<WorkflowTemplateStepSummary, "title" | "template_step_id" | "instance_scope" | "repeat_group_id">,
  base?: WorkflowTemplateStepSummary,
): string {
  const rawTitle = workflowStringValue(step.title)
  const baseTitle = workflowStringValue(base?.title)
  const selectedTitle = baseTitle && (!rawTitle || workflowTitleLooksMachine(rawTitle) || step.template_step_id)
    ? baseTitle
    : rawTitle || baseTitle
  const scope = workflowInstanceScopeLabel(step.instance_scope)
  if (scope && selectedTitle && !selectedTitle.includes(scope)) return `${scope} · ${selectedTitle}`
  return selectedTitle || rawTitle || workflowStringValue(step.template_step_id) || "流程步骤"
}

function workflowDisplayLabel(rawValue: unknown, baseValue: unknown): string {
  const raw = workflowStringValue(rawValue)
  const base = workflowStringValue(baseValue)
  if (base && (!raw || workflowTitleLooksMachine(raw))) return base
  return raw || base
}

function workflowStepTitleById(steps: WorkflowTemplateStepSummary[], id: string): string {
  const step = steps.find((item) => (
    item.id === id ||
    item.source_node_id === id ||
    item.template_step_id === id ||
    item.repeat_group_id === id
  ))
  return step?.title || id
}

function workflowRuntimeStepStatesFromPayload(runtime: ProjectWorkflowRuntime | null | undefined): Record<string, WorkflowStepNodeState> {
  const result: Record<string, WorkflowStepNodeState> = {}
  const steps = Array.isArray(runtime?.steps) ? runtime.steps : []
  for (const step of steps) {
    const id = workflowStringValue(step.id)
    if (!id) continue
    const status = workflowStringValue(step.status) || "idle"
    const stale = Boolean(step.stale)
    const artifactCount = Number(step.artifact_count || 0)
    const outputCount = Number(step.output_count || 0)
    const runCount = Number(step.run_count || 0)
    const outputPreview = workflowStringValue(step.output_preview)
    const detailParts = [
      runCount > 0 ? `运行 ${runCount} 次` : "",
      outputCount > 0 ? `输出 ${outputCount}` : "",
      artifactCount > 0 ? `产物 ${artifactCount}` : "",
      step.updated_at ? `更新时间 ${workflowStringValue(step.updated_at)}` : "",
    ].filter(Boolean)
    result[id] = {
      nodeId: workflowStringValue(step.node_id) || id,
      nodeIds: step.artifact_node_ids?.length ? step.artifact_node_ids.map(String) : step.node_id ? [workflowStringValue(step.node_id)] : [],
      title: workflowStringValue(step.title) || id,
      status: stale ? "stale" : status,
      count: 1,
      runningCount: status === "running" ? 1 : 0,
      failedCount: status === "failed" ? 1 : 0,
      completedCount: status === "completed" ? 1 : 0,
      runCount,
      resolvedInputCount: Number(step.resolved_input_count || 0),
      outputCount,
      artifactCount,
      updatedAt: workflowStringValue(step.updated_at),
      lastRunSummary: stale ? "上游已更新，建议重跑" : step.error ? workflowStringValue(step.error) : undefined,
      lastRunDetail: detailParts.length ? detailParts.join(" · ") : undefined,
      outputPreview,
      virtual: Boolean(step.virtual),
    }
  }
  return result
}

function workflowRuntimeId(runtime: ProjectWorkflowRuntime | null | undefined): string {
  return workflowStringValue(runtime?.instance_id)
}

function workflowRuntimeTemplateId(runtime: ProjectWorkflowRuntime | null | undefined): string {
  return workflowStringValue(runtime?.template_id)
}

function workflowRuntimeTemplateName(runtime: ProjectWorkflowRuntime, templates: WorkflowTemplateSummary[]): string {
  const templateId = workflowRuntimeTemplateId(runtime)
  const template = templates.find((item) => item.id === templateId)
  return workflowStringValue(runtime.template_name) || template?.name || templateId || "未命名流程"
}

function workflowRuntimeProgress(runtime: ProjectWorkflowRuntime, steps: WorkflowTemplateStepSummary[]): {
  total: number
  completed: number
  running: number
  failed: number
  waiting: number
  ready: number
} {
  const progress = runtime.progress || {}
  const runtimeSteps = Array.isArray(runtime.steps) ? runtime.steps : []
  const sourceSteps = runtimeSteps.length > 0 && runtimeSteps.length >= steps.length ? runtimeSteps : steps
  const fromSteps = sourceSteps.reduce(
    (acc, step) => {
      const runtimeStep = step as ProjectWorkflowRuntimeStep
      const status = workflowStringValue(runtimeStep.status)
      const stale = Boolean(runtimeStep.stale)
      if (status === "completed" && !stale) acc.completed += 1
      else if (status === "running") acc.running += 1
      else if (status === "failed") acc.failed += 1
      if (Boolean(runtimeStep.ready)) acc.ready += 1
      const waitingOn = runtimeStep.waiting_on
      if (Array.isArray(waitingOn) && waitingOn.length > 0) acc.waiting += 1
      return acc
    },
    { completed: 0, running: 0, failed: 0, waiting: 0, ready: 0 },
  )
  return {
    total: Math.max(Number(progress.total || 0), sourceSteps.length || 0),
    completed: Number(progress.total || 0) >= (sourceSteps.length || 0) ? Number(progress.completed ?? fromSteps.completed) : fromSteps.completed,
    running: Number(progress.total || 0) >= (sourceSteps.length || 0) ? Number(progress.running ?? fromSteps.running) : fromSteps.running,
    failed: Number(progress.total || 0) >= (sourceSteps.length || 0) ? Number(progress.failed ?? fromSteps.failed) : fromSteps.failed,
    waiting: Number(progress.total || 0) >= (sourceSteps.length || 0) ? Number(progress.waiting ?? fromSteps.waiting) : fromSteps.waiting,
    ready: Number(progress.total || 0) >= (sourceSteps.length || 0) ? Number(progress.ready ?? fromSteps.ready) : fromSteps.ready,
  }
}

function mergeWorkflowRuntimePayloads(
  current: ProjectWorkflowRuntime[],
  incoming: ProjectWorkflowRuntime | ProjectWorkflowRuntime[] | null | undefined,
): ProjectWorkflowRuntime[] {
  const items = Array.isArray(incoming) ? incoming : incoming ? [incoming] : []
  if (items.length === 0) return current
  const byId = new Map<string, ProjectWorkflowRuntime>()
  for (const item of current) {
    const id = workflowRuntimeId(item)
    if (id) byId.set(id, item)
  }
  for (const item of items) {
    const id = workflowRuntimeId(item)
    if (id) byId.set(id, { ...(byId.get(id) || {}), ...item, local_draft: Boolean(item.local_draft) })
  }
  return Array.from(byId.values()).sort((a, b) => (
    workflowStringValue(b.updated_at).localeCompare(workflowStringValue(a.updated_at))
  ))
}

function createWorkflowRuntimeDraft(template: WorkflowTemplateSummary): ProjectWorkflowRuntime {
  const id = createWorkflowRuntimeInstanceId()
  return {
    instance_id: id,
    template_id: template.id,
    template_name: template.name,
    status: "idle",
    local_draft: true,
    progress: {
      total: Array.isArray(template.steps) ? template.steps.length : 0,
      completed: 0,
      running: 0,
      failed: 0,
      pending: Array.isArray(template.steps) ? template.steps.length : 0,
      ready: 0,
      waiting: 0,
    },
    steps: [],
  }
}

function createWorkflowRuntimeInstanceId(): string {
  return `wf_ui_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`
}

function workflowRuntimeScopeNumber(value: unknown): string {
  if (value == null) return ""
  const text = workflowStringValue(value)
  if (!text) return ""
  const numeric = Number(text)
  return Number.isFinite(numeric) ? String(numeric) : text
}

function workflowRuntimeScopesMatch(
  source: WorkflowTemplateStepSummary,
  target: WorkflowTemplateStepSummary,
): boolean {
  const sourceScope = asWorkflowObject(source.instance_scope)
  const targetScope = asWorkflowObject(target.instance_scope)
  if (!sourceScope || !targetScope) return false
  const sourceEpisode = workflowRuntimeScopeNumber(sourceScope.episode || sourceScope.episode_index || sourceScope.episodeIndex)
  const targetEpisode = workflowRuntimeScopeNumber(targetScope.episode || targetScope.episode_index || targetScope.episodeIndex)
  const sourceSegment = workflowRuntimeScopeNumber(sourceScope.segment || sourceScope.segment_index || sourceScope.segmentIndex)
  const targetSegment = workflowRuntimeScopeNumber(targetScope.segment || targetScope.segment_index || targetScope.segmentIndex)
  if (sourceEpisode || targetEpisode || sourceSegment || targetSegment) {
    return sourceEpisode === targetEpisode && sourceSegment === targetSegment
  }
  const sourceReuseKey = workflowStringValue(sourceScope.reuse_key || sourceScope.reuseKey)
  const targetReuseKey = workflowStringValue(targetScope.reuse_key || targetScope.reuseKey)
  if (sourceReuseKey || targetReuseKey) return sourceReuseKey === targetReuseKey
  const sourceName = workflowStringValue(sourceScope.name || sourceScope.title || sourceScope.label)
  const targetName = workflowStringValue(targetScope.name || targetScope.title || targetScope.label)
  if (sourceName || targetName) return sourceName === targetName
  const sourceIndex = workflowRuntimeScopeNumber(sourceScope.index)
  const targetIndex = workflowRuntimeScopeNumber(targetScope.index)
  return Boolean(sourceIndex && targetIndex && sourceIndex === targetIndex)
}

function workflowRuntimeRemapDependency(
  dep: string,
  step: WorkflowTemplateStepSummary,
  runtimeByTemplateId: Map<string, WorkflowTemplateStepSummary[]>,
  visibleById: Map<string, WorkflowTemplateStepSummary>,
): string {
  const candidates = runtimeByTemplateId.get(dep) || []
  if (candidates.length === 0) return dep
  const sameRepeat = candidates.filter((candidate) => (
    workflowStringValue(candidate.repeat_group_id) === workflowStringValue(step.repeat_group_id)
  ))
  const scoped = sameRepeat.find((candidate) => workflowRuntimeScopesMatch(candidate, step))
  if (scoped) return scoped.id
  if (visibleById.has(dep)) return dep
  if (sameRepeat.length === 1) return sameRepeat[0].id
  const scopedAnyRepeat = candidates.find((candidate) => workflowRuntimeScopesMatch(candidate, step))
  return scopedAnyRepeat?.id || candidates[0].id
}

function workflowRuntimeRemappedDependencies(
  step: WorkflowTemplateStepSummary,
  runtimeByTemplateId: Map<string, WorkflowTemplateStepSummary[]>,
  visibleById: Map<string, WorkflowTemplateStepSummary>,
): string[] {
  const result: string[] = []
  for (const rawDep of workflowCleanIdList(step.depends_on)) {
    const dep = workflowRuntimeRemapDependency(workflowStringValue(rawDep), step, runtimeByTemplateId, visibleById)
    if (dep && dep !== step.id && !result.includes(dep)) result.push(dep)
  }
  const repeatGroupId = workflowStringValue(step.repeat_group_id)
  if (result.length === 0 && repeatGroupId && repeatGroupId !== step.id && visibleById.has(repeatGroupId)) {
    result.push(repeatGroupId)
  }
  return result
}

function workflowRuntimeStepSummariesFromPayload(
  runtime: ProjectWorkflowRuntime | null | undefined,
  fallbackSteps: WorkflowTemplateStepSummary[],
): WorkflowTemplateStepSummary[] {
  const runtimeSteps = Array.isArray(runtime?.steps) ? runtime.steps : []
  if (runtimeSteps.length === 0) return fallbackSteps
  const fallbackById = new Map<string, WorkflowTemplateStepSummary>()
  for (const step of fallbackSteps) {
    fallbackById.set(step.id, step)
    if (step.template_step_id) fallbackById.set(step.template_step_id, step)
  }
  const runtimeSummaries = runtimeSteps
    .map((step): WorkflowTemplateStepSummary | null => {
      const id = workflowStringValue(step.id)
      if (!id) return null
      const templateStepId = workflowStringValue(step.template_step_id)
      const base = fallbackById.get(id) || (templateStepId ? fallbackById.get(templateStepId) : undefined)
      const instanceScope = step.instance_scope || base?.instance_scope
      return {
        ...(base || {}),
        id,
        title: workflowDisplayStepTitle({
          title: workflowStringValue(step.title) || base?.title || id,
          template_step_id: templateStepId || base?.template_step_id,
          repeat_group_id: workflowStringValue(step.repeat_group_id) || base?.repeat_group_id,
          instance_scope: instanceScope,
        }, base),
        node_type: workflowNodeType(step.type || base?.node_type || "text"),
        status: workflowStringValue(step.status) || base?.status,
        execution_state: workflowStringValue(step.execution_state) || base?.execution_state,
        stale: typeof step.stale === "boolean" ? step.stale : base?.stale,
        depends_on: workflowCleanIdList(step.depends_on).length > 0
          ? workflowCleanIdList(step.depends_on)
          : workflowCleanIdList(base?.depends_on),
        phase: workflowStringValue(step.phase) || base?.phase,
        group: workflowStringValue(step.group) || base?.group,
        kind: workflowStringValue(step.kind) || base?.kind,
        purpose: workflowStringValue(step.purpose) || base?.purpose,
        acceptance: workflowStringValue(step.acceptance) || base?.acceptance,
        primary_skill: workflowStringValue(step.primary_skill) || base?.primary_skill,
        prompt_ref: workflowStringValue(step.prompt_ref) || base?.prompt_ref,
        role: workflowStringValue(step.role) || base?.role,
        surface: workflowStringValue(step.surface) || base?.surface,
        visibility: workflowStringValue(step.visibility) || base?.visibility,
        canvas_output: typeof step.canvas_output === "boolean" ? step.canvas_output : base?.canvas_output,
        runtime_only: typeof step.runtime_only === "boolean" ? step.runtime_only : base?.runtime_only,
        template_step_id: templateStepId || base?.template_step_id,
        repeat_group_id: workflowStringValue(step.repeat_group_id) || base?.repeat_group_id,
        repeat_group_label: workflowDisplayLabel(step.repeat_group_label, base?.repeat_group_label),
        repeat_group_index: typeof step.repeat_group_index === "number" ? step.repeat_group_index : base?.repeat_group_index,
        ui: step.ui || base?.ui,
        output: asWorkflowObject(step.output) || base?.output,
        authoring: step.authoring || base?.authoring,
        instance_scope: instanceScope,
        collection: step.collection || base?.collection,
        expansion: step.expansion || base?.expansion,
      }
    })
    .filter((step): step is WorkflowTemplateStepSummary => Boolean(step))
  if (runtimeSummaries.length === 0) return fallbackSteps
  const runtimeByTemplateId = new Map<string, WorkflowTemplateStepSummary[]>()
  const runtimeById = new Map<string, WorkflowTemplateStepSummary>()
  for (const step of runtimeSummaries) {
    runtimeById.set(step.id, step)
    const templateStepId = step.template_step_id || step.id
    runtimeByTemplateId.set(templateStepId, [...(runtimeByTemplateId.get(templateStepId) || []), step])
  }
  const visibleById = new Map<string, WorkflowTemplateStepSummary>([...fallbackById, ...runtimeById])
  for (const step of runtimeSummaries) {
    step.depends_on = workflowRuntimeRemappedDependencies(step, runtimeByTemplateId, visibleById)
  }
  const used = new Set<string>()
  const merged: WorkflowTemplateStepSummary[] = []
  for (const fallback of fallbackSteps) {
    if (
      workflowStringValue(fallback.role) === "repeat_group" &&
      runtimeSummaries.some((step) => workflowStringValue(step.repeat_group_id) === fallback.id)
    ) {
      continue
    }
    const byTemplate = runtimeByTemplateId.get(fallback.id) || []
    const byId = runtimeById.get(fallback.id)
    const replacements = byTemplate.length > 0
      ? byTemplate
      : byId
      ? [byId]
      : []
    if (replacements.length > 0) {
      for (const replacement of replacements) {
        if (!used.has(replacement.id)) {
          merged.push(replacement)
          used.add(replacement.id)
        }
      }
    } else {
      merged.push(fallback)
    }
  }
  for (const step of runtimeSummaries) {
    if (!used.has(step.id)) merged.push(step)
  }
  return merged
}

function workflowDependencyLabels(
  step: WorkflowTemplateStepSummary,
  steps: WorkflowTemplateStepSummary[],
): string[] {
  return (step.depends_on || []).map((id) => workflowStepTitleById(steps, id)).filter(Boolean)
}

function workflowStepSummaryLines(
  step: WorkflowTemplateStepSummary,
  steps: WorkflowTemplateStepSummary[],
): Array<{ label: string; value: string }> {
  const dynamicReference = workflowReferenceSelectorSummary(step, steps)
  return [
    { label: "用途", value: workflowStringValue(step.purpose) },
    { label: "验收", value: workflowStringValue(step.acceptance) },
    { label: "动态参考", value: dynamicReference },
  ].filter((item) => item.value && !workflowLooksTechnical(item.value))
}

function workflowReferenceSelectorSummary(step: WorkflowTemplateStepSummary, steps: WorkflowTemplateStepSummary[]): string {
  const selectors = Array.isArray(step.reference_selectors) ? step.reference_selectors : []
  const lines = selectors
    .map((selector) => {
      const source = workflowStringValue(selector.source_step || selector.source || selector.from_source_step)
      const sourcePath = workflowStringValue(selector.source_path || selector.path) || "输出"
      const group = workflowStringValue(selector.from_group || selector.candidate_group || selector.from_step)
      const sourceLabel = source ? workflowStepTitleById(steps, source) : ""
      const groupLabel = group ? workflowStepTitleById(steps, group) : ""
      if (sourceLabel && groupLabel) return `按 ${sourceLabel} 的 ${sourcePath} 选择 ${groupLabel}`
      if (sourceLabel) return `按 ${sourceLabel} 的 ${sourcePath} 选择参考`
      if (groupLabel) return `按上游输出选择 ${groupLabel}`
      return ""
    })
    .filter(Boolean)
  return Array.from(new Set(lines)).join("\n")
}

function workflowSkillDisplay(value: unknown): string {
  const text = workflowStringValue(value)
  if (!text) return ""
  const labels: Record<string, string> = {
    script_writing: "剧本写法",
    character_prompt: "人物提示词",
    scene_prompt: "场景提示词",
    storyboard: "分镜写法",
    video_prompt: "视频提示词",
    workflow: "工作流流程",
    review: "检查规则",
  }
  return labels[text] || text.replace(/^skill\./, "").replace(/_/g, " ")
}

function workflowRunnerDisplay(value: unknown, nodeType?: string): string {
  const text = workflowStringValue(value)
  if (!text) return ""
  if (text === "node.run") {
    if (nodeType === "text") return "生成文本"
    if (nodeType === "image") return "图片节点"
    if (nodeType === "video") return "视频节点"
    if (nodeType === "audio") return "音频节点"
    return "运行节点"
  }
  const labels: Record<string, string> = {
    workflow_input: "用户输入",
    input_form: "用户输入",
    manual_input: "用户输入",
    llm: "生成文本",
    text_generation: "生成文本",
    image_generation: "图片节点",
    video_generation: "视频节点",
    audio_generation: "音频节点",
    workflow_canvas_output: "画布产物",
    workflow_plugin: "插件动作",
  }
  return labels[text] || text.replace(/_/g, " ")
}

function workflowCollectionSummary(value: unknown, steps: WorkflowTemplateStepSummary[]): string {
  const obj = asWorkflowObject(value)
  if (!obj) return ""
  const label = workflowStringValue(obj.label || obj.name || obj.title)
  const source = workflowStringValue(obj.from_step || obj.source_step || obj.source)
  const sourceLabel = source ? workflowStepTitleById(steps, source) : ""
  if (label && sourceLabel) return `${sourceLabel} 的${label}`
  return label || sourceLabel
}

function workflowRepeatSummary(step: WorkflowTemplateStepSummary): string {
  const foreach = asWorkflowObject(step.foreach)
  if (workflowHasValue(foreach?.items)) return `逐项处理 ${workflowStringValue(foreach?.items)}`
  if (workflowHasValue(foreach?.count)) return `重复 ${workflowStringValue(foreach?.count)} 次`
  if (workflowStringValue(step.repeat_group_label)) return workflowReadableLabel(step.repeat_group_label)
  return ""
}

function workflowInstanceSummary(step: WorkflowTemplateStepSummary): string {
  const scope = workflowInstanceScopeLabel(step.instance_scope)
  if (scope) return scope
  const group = workflowReadableLabel(step.repeat_group_label || step.repeat_group_id)
  const index = typeof step.repeat_group_index === "number" ? step.repeat_group_index : undefined
  if (group && index != null) return `${group} 第${index}项`
  return group
}

function workflowPromptTemplateSections(value: unknown): Array<{ key: string; label: string; text: string }> {
  const text = workflowStringValue(value)
  if (!text) return []
  const labels: Record<string, string> = {
    SYSTEM: "步骤角色",
    USER: "输入组织",
    OUTPUT: "输出格式",
    CHECK: "检查标准",
  }
  const sections: Array<{ key: string; label: string; text: string }> = []
  let currentKey = "SYSTEM"
  let lines: string[] = []
  const flush = () => {
    const sectionText = lines.join("\n").trim()
    if (sectionText) sections.push({ key: currentKey, label: labels[currentKey] || currentKey, text: sectionText })
  }
  for (const rawLine of text.split(/\r?\n/)) {
    const match = rawLine.match(/^\s*(SYSTEM|USER|OUTPUT|CHECK)\s*:\s*(.*)$/i)
    if (match) {
      flush()
      currentKey = match[1].toUpperCase()
      lines = [match[2] || ""]
      continue
    }
    lines.push(rawLine)
  }
  flush()
  return sections.length > 0 ? sections : [{ key: "PROMPT", label: "提示词", text }]
}

function workflowExecutionDetailRows(
  step: WorkflowTemplateStepSummary,
  steps: WorkflowTemplateStepSummary[],
  inputSpecs: Record<string, WorkflowInputDraftSpec> = {},
): Array<{ label: string; value: string }> {
  const referenceSummary = workflowReferenceSelectorSummary(step, steps)
  const conditionLabel = workflowConditionLabel(step.when, inputSpecs)
  const rows: Array<{ label: string; value: string }> = [
    { label: "输出位置", value: workflowStepOutputLabel(step) },
    { label: "生成方式", value: workflowRunnerDisplay(step.runner, step.node_type) || workflowStepKindLabel(step) },
    { label: "参考写法", value: workflowSkillDisplay(step.prompt_ref || step.primary_skill || step.skill_category) },
    { label: "执行范围", value: workflowInstanceSummary(step) },
    { label: "集合来源", value: workflowCollectionSummary(step.collection || step.foreach, steps) },
    { label: "展开方式", value: workflowRepeatSummary(step) },
    { label: "动态参考", value: referenceSummary },
    { label: "运行条件", value: conditionLabel },
  ]
  if (step.on_error === "continue") rows.push({ label: "失败处理", value: "继续后续步骤" })
  if (step.execution === "manual") rows.push({ label: "生成方式", value: "手动运行" })
  return rows.filter((item) => item.value.trim())
}

function workflowLooksTechnical(value: string): boolean {
  return /\b(node\.|runner|prompt_|template_|fields\.|workflow|JSON)\b|[{}\[\]]/.test(value)
}

function workflowInputValueForId(
  inputId: string,
  values: Record<string, string>,
  inputSpecs: Record<string, WorkflowInputDraftSpec> = {},
): string {
  const value = values[inputId]
  if (String(value || "").trim()) return value
  return inputSpecs[inputId]?.default || ""
}

function workflowInputHasValue(
  inputId: string,
  values: Record<string, string>,
  inputSpecs: Record<string, WorkflowInputDraftSpec> = {},
): boolean {
  return Boolean(workflowInputValueForId(inputId, values, inputSpecs).trim())
}

function workflowMissingInputIds(
  inputs: string[],
  values: Record<string, string>,
  requiredIds: string[],
  inputSpecs: Record<string, WorkflowInputDraftSpec> = {},
): string[] {
  const inputSet = new Set(inputs)
  return requiredIds.filter((input) => inputSet.has(input) && !workflowInputHasValue(input, values, inputSpecs))
}

function workflowInputSummary(
  inputs: string[],
  values: Record<string, string>,
  requiredIds: string[],
  inputSpecs: Record<string, WorkflowInputDraftSpec> = {},
): string {
  if (inputs.length === 0) return "无运行前输入"
  const requiredSet = new Set(requiredIds)
  const filled = inputs.filter((input) => workflowInputHasValue(input, values, inputSpecs)).length
  const requiredCount = inputs.filter((input) => requiredSet.has(input)).length
  return `${filled}/${inputs.length} 已输入${requiredCount > 0 ? ` · ${requiredCount} 必填` : ""}`
}

function workflowIsLongInput(name: string, spec?: WorkflowInputDraftSpec): boolean {
  const type = String(spec?.type || "").trim().toLowerCase()
  if (["textarea", "long_text", "multiline", "markdown", "json", "object", "array", "list"].includes(type)) return true
  if (["number", "integer", "boolean", "checkbox", "select", "enum"].includes(type)) return false
  return /plot|story|script|brief/i.test(name)
}

function workflowInputStepId(steps: WorkflowTemplateStepSummary[], inputs: string[]): string {
  if (steps.length === 0 || inputs.length === 0) return ""
  const explicit = steps.find((step) => {
    const role = workflowStringValue(step.role)
    const startAction = workflowStringValue(step.start_action)
    const id = workflowStringValue(step.id)
    return (
      role === "entry" ||
      /collect.*input|input|intake/i.test(startAction) ||
      /input|intake|brief|需求/i.test(id)
    )
  })
  return explicit?.id || steps[0].id
}

function workflowRunSummaryFromWorkflow(workflow: Record<string, unknown>, status: string): { summary?: string; detail?: string } {
  const lastRun = asWorkflowObject(workflow.last_run)
  const lastStepRun = asWorkflowObject(workflow.last_step_run)
  const model = workflowStringValue(lastRun?.model)
  const taskType = workflowStringValue(lastRun?.task_type)
  const tokens = workflowStringValue(lastRun?.usage_total_tokens)
  const promptDump = workflowStringValue(lastRun?.prompt_dump_run_id)
  const error = workflowStringValue(lastRun?.error) || workflowStringValue(workflow.last_error)
  if (lastRun) {
    const summary = [
      lastRun.status === "failed" || status === "failed" ? "LLM 失败" : "LLM 已调用",
      model,
      tokens ? `${tokens} tokens` : "",
    ].filter(Boolean).join(" · ")
    const detail = [
      taskType ? `任务: ${taskType}` : "",
      promptDump ? `日志: ${promptDump}` : "",
      error ? `错误: ${error}` : "",
    ].filter(Boolean).join("\n")
    return { summary, detail }
  }
  if (lastStepRun) {
    const stepStatus = workflowStringValue(lastStepRun.status) || status
    const at = workflowStringValue(lastStepRun.at)
    return {
      summary: `${workflowStepStateLabel(stepStatus)}${at ? ` · ${at}` : ""}`,
      detail: error ? `错误: ${error}` : undefined,
    }
  }
  if (status === "running") return { summary: "运行中" }
  if (status === "failed" && error) return { summary: "运行失败", detail: `错误: ${error}` }
  return {}
}

function workflowRuntimeSnapshotFromNodes(
  nodes: FlowNode[],
  workflowId: string,
  inputIds: string[],
): { instanceId: string; values: Record<string, string> } {
  if (!workflowId) return { instanceId: "", values: {} }
  let instanceId = ""
  const values: Record<string, string> = {}
  for (const node of nodes) {
    const data = node.data as Record<string, unknown> | undefined
    const workflow = data?.workflow && typeof data.workflow === "object"
      ? data.workflow as Record<string, unknown>
      : undefined
    if (!workflow) continue
    const templateId = workflowStringValue(workflow.template_id)
    if (templateId && templateId !== workflowId) continue
    const nextInstanceId = workflowStringValue(workflow.instance_id)
    if (nextInstanceId) instanceId = nextInstanceId
    const inputObj = data?.input && typeof data.input === "object"
      ? data.input as Record<string, unknown>
      : {}
    Object.assign(values, workflowInputValuesFromObject(inputObj.input_values))
    Object.assign(values, workflowInputValuesFromObject(workflow.input_facts))
  }
  if (inputIds.length > 0) {
    for (const key of Object.keys(values)) {
      if (!inputIds.includes(key)) delete values[key]
    }
  }
  return { instanceId, values }
}

function WorkflowChevron({ open }: { open: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "h-2 w-2 border-b border-r border-current transition-transform",
        open ? "-rotate-135" : "rotate-45",
      )}
    />
  )
}

function WorkflowStepDetailDialog({
  step,
  steps,
  nodeState,
  onClose,
  running,
  onRunStep,
  isInputStep,
  inputIds,
  inputSpecs = {},
  inputValues,
  requiredInputIds,
  missingRequiredInputIds,
  onInputValueChange,
}: {
  step: WorkflowTemplateStepSummary
  steps: WorkflowTemplateStepSummary[]
  nodeState?: WorkflowStepNodeState
  onClose: () => void
  running: boolean
  onRunStep: (stepId: string) => void
  isInputStep: boolean
  inputIds: string[]
  inputSpecs?: Record<string, WorkflowInputDraftSpec>
  inputValues: Record<string, string>
  requiredInputIds: string[]
  missingRequiredInputIds: string[]
  onInputValueChange: (id: string, value: string) => void
}) {
  const [technicalOpen, setTechnicalOpen] = useState(false)
  const status = nodeState?.status || ""
  const isRunning = running || status === "running"
  const inputBlocked = inputIds.length > 0 && missingRequiredInputIds.length > 0
  const summaryRows = workflowStepSummaryLines(step, steps)
  const dependencyLabels = workflowDependencyLabels(step, steps)
  const executionRows = workflowExecutionDetailRows(step, steps)
  const promptSections = workflowPromptTemplateSections(step.prompt_template)
  const methodRows = [
    { label: "阶段", value: workflowStepPhaseLabel(step) },
    { label: "产物", value: workflowStepOutputLabel(step) },
    { label: "生成方式", value: isInputStep ? "用户输入" : workflowRunnerDisplay(step.runner, step.node_type) || workflowStepKindLabel(step) },
    { label: "参考写法", value: workflowSkillDisplay(step.prompt_ref || step.primary_skill || step.skill_category) },
  ].filter((item) => item.value && !workflowLooksTechnical(item.value))
  const requiredSet = useMemo(() => new Set(requiredInputIds), [requiredInputIds])
  const hasReadableDetails = !isInputStep && (methodRows.length > 0 || summaryRows.length > 0 || dependencyLabels.length > 0)
  const hasPromptSections = !isInputStep && promptSections.length > 0
  const hasTechnicalDetails = !isInputStep && executionRows.length > 0

  return (
    <div className="fixed inset-0 z-[75] bg-black/44 backdrop-blur-sm" onClick={onClose}>
      <div
        className="absolute bottom-4 right-4 top-4 flex w-[min(460px,calc(100vw-24px))] flex-col overflow-hidden rounded-md border border-white/10 bg-[#10151d] text-zinc-100 shadow-2xl shadow-black/50"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex shrink-0 items-start gap-3 border-b border-white/10 px-4 py-3">
          <div className={cn("mt-0.5 flex h-7 w-7 items-center justify-center rounded-md border text-[11px] font-semibold", workflowStepToneClass(step))}>
            {WORKFLOW_NODE_TYPE_LABEL[step.node_type]?.slice(0, 1) || "步"}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-semibold text-zinc-50">{step.title || step.id}</div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              <span className={cn("rounded border px-1.5 py-0.5 text-[10px]", workflowStepToneClass(step))}>
                {isInputStep ? "输入" : workflowStepKindLabel(step)}
              </span>
              <span className={cn(
                "rounded border px-1.5 py-0.5 text-[10px]",
                nodeState ? workflowStepStateClass(status) : "border-white/10 bg-white/[0.03] text-zinc-500",
              )}>
                {nodeState ? workflowStepAggregateLabel(nodeState) : "模板步骤"}
              </span>
            </div>
          </div>
          <button
            type="button"
            onClick={() => onRunStep(step.id)}
            disabled={isRunning || inputBlocked}
            className="h-8 rounded-md border border-white/10 px-3 text-xs text-zinc-200 transition hover:bg-white/[0.07] disabled:cursor-wait disabled:opacity-55"
          >
            {isRunning ? "运行中" : inputBlocked ? "先输入" : "运行步骤"}
          </button>
          <button
            type="button"
            aria-label="关闭详情"
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-md border border-white/10 text-zinc-400 transition hover:bg-white/[0.07] hover:text-zinc-100"
          >
            <span className="text-base leading-none">x</span>
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <div className="grid gap-3">
            {isInputStep && inputIds.length > 0 && (
              <section className="rounded-md border border-amber-200/18 bg-amber-300/[0.055] p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-xs font-semibold text-amber-100">输入参数</div>
                  <div className="text-[10px] text-amber-100/65">
                    {workflowInputSummary(inputIds, inputValues, requiredInputIds, inputSpecs)}
                  </div>
                </div>
                <div className="grid gap-2">
                  {inputIds.map((input) => {
                    const spec = inputSpecs[input] || { type: "text" }
                    const longInput = workflowIsLongInput(input, spec)
                    const value = workflowInputValueForId(input, inputValues, inputSpecs)
                    const inputClassName = cn(
                      "w-full rounded-md border px-2 text-xs outline-none transition placeholder:text-zinc-500 focus:border-amber-200/55",
                      missingRequiredInputIds.includes(input)
                        ? "border-amber-200/40"
                        : "border-white/10",
                      longInput ? "min-h-20 py-1.5 leading-4" : "h-8",
                    )
                    const inputStyle = { backgroundColor: "#0b0f16", color: "#f4f4f5", caretColor: "#fbbf24" }
                    return (
                      <label key={input} className="block text-[10px] font-medium text-zinc-400">
                        <span className="mb-1 flex items-center gap-1">
                          {spec.label || workflowInputLabel(input)}
                          {requiredSet.has(input) && <span className="text-amber-200/85">必填</span>}
                        </span>
                        {longInput ? (
                          <textarea
                            value={value}
                            onChange={(event) => onInputValueChange(input, event.target.value)}
                            placeholder={spec.description || workflowInputPlaceholder(input)}
                            rows={3}
                            className={cn(inputClassName, "resize-none")}
                            style={inputStyle}
                          />
                        ) : (
                          <input
                            type={String(spec.type || "").toLowerCase() === "number" || String(spec.type || "").toLowerCase() === "integer" ? "number" : "text"}
                            value={value}
                            onChange={(event) => onInputValueChange(input, event.target.value)}
                            placeholder={spec.description || workflowInputPlaceholder(input)}
                            className={inputClassName}
                            style={inputStyle}
                          />
                        )}
                      </label>
                    )
                  })}
                </div>
              </section>
            )}
            {(nodeState?.lastRunSummary || nodeState?.lastRunDetail) && (
              <section className="rounded-md border border-cyan-200/14 bg-cyan-300/[0.045] px-3 py-2.5">
                <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-cyan-200/75">最近运行</div>
                {nodeState.lastRunSummary && (
                  <div className="mt-1 text-[12px] leading-5 text-cyan-50/90">{nodeState.lastRunSummary}</div>
                )}
                {nodeState.lastRunDetail && (
                  <div className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-5 text-cyan-100/70">{nodeState.lastRunDetail}</div>
                )}
              </section>
            )}
            {nodeState?.outputPreview && (
              <section className="overflow-hidden rounded-md border border-emerald-200/14 bg-emerald-300/[0.045]">
                <div className="border-b border-emerald-200/10 px-3 py-2 text-[11px] font-semibold text-emerald-100/80">
                  运行输出
                </div>
                <WorkflowRunOutputView value={nodeState.outputPreview} />
              </section>
            )}
            {hasReadableDetails && (
              <section className="overflow-hidden rounded-md border border-white/[0.08] bg-black/18">
                <div className="border-b border-white/[0.06] px-3 py-2 text-[11px] font-semibold text-zinc-300">
                  步骤信息
                </div>
                <div className="grid gap-3 p-3">
                  {methodRows.length > 0 && (
                    <div className="grid grid-cols-2 gap-2">
                      {methodRows.map((item) => (
                        <div key={item.label} className="rounded-md border border-white/[0.08] bg-white/[0.035] px-3 py-2">
                          <div className="text-[10px] font-semibold text-zinc-500">{item.label}</div>
                          <div className="mt-1 truncate text-[12px] text-zinc-200" title={item.value}>{item.value}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  {summaryRows.length > 0 && (
                    <div className="rounded-md border border-white/[0.08] bg-white/[0.035]">
                      {summaryRows.map((item) => (
                        <div key={item.label} className="grid gap-1 border-b border-white/[0.06] px-3 py-2.5 last:border-b-0">
                          <div className="text-[10px] font-semibold text-zinc-500">{item.label}</div>
                          <div className="whitespace-pre-wrap break-words text-[12px] leading-5 text-zinc-200">{item.value}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  {dependencyLabels.length > 0 && (
                    <div>
                      <div className="mb-1.5 text-[10px] font-semibold text-zinc-500">输入来源</div>
                      <div className="flex flex-wrap gap-1.5">
                        {dependencyLabels.map((label) => (
                          <span
                            key={label}
                            className="rounded-md border border-white/[0.08] bg-black/24 px-2 py-1 text-[11px] text-zinc-300"
                          >
                            {label}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </section>
            )}
            {hasPromptSections && (
              <section className="overflow-hidden rounded-md border border-cyan-200/12 bg-cyan-300/[0.04]">
                <div className="border-b border-cyan-200/10 px-3 py-2 text-[11px] font-semibold text-cyan-100/80">
                  提示词
                </div>
                <div className="grid gap-2 p-3">
                  {promptSections.map((section) => (
                    <div key={section.key} className="overflow-hidden rounded-md border border-cyan-200/12 bg-black/18">
                      <div className="border-b border-cyan-200/10 px-3 py-2 text-[10px] font-semibold text-cyan-100/75">
                        {section.label}
                      </div>
                      <div className="whitespace-pre-wrap break-words px-3 py-2.5 text-[12px] leading-5 text-cyan-50/90">
                        {section.text}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
            {hasTechnicalDetails && (
              <section className="overflow-hidden rounded-md border border-white/[0.08] bg-black/18">
                <button
                  type="button"
                  onClick={() => setTechnicalOpen((open) => !open)}
                  className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-[11px] font-semibold text-zinc-300 transition hover:bg-white/[0.04]"
                >
                  <span>执行信息</span>
                  <span className="flex h-5 w-5 items-center justify-center rounded border border-white/10 text-zinc-400">
                    <WorkflowChevron open={technicalOpen} />
                  </span>
                </button>
                {technicalOpen && (
                  <div className="grid gap-3 border-t border-white/[0.06] p-3">
                    {executionRows.length > 0 && (
                      <div className="rounded-md border border-white/[0.08] bg-white/[0.035]">
                        {executionRows.map((item) => (
                          <div key={item.label} className="grid gap-1 border-b border-white/[0.06] px-3 py-2.5 last:border-b-0">
                            <div className="text-[10px] font-semibold text-zinc-500">{item.label}</div>
                            <div className="whitespace-pre-wrap break-words text-[12px] leading-5 text-zinc-200">{item.value}</div>
                          </div>
                        ))}
                      </div>
                    )}
                    {executionRows.length === 0 && (
                      <div className="rounded-md border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-[12px] text-zinc-500">
                        暂无执行信息
                      </div>
                    )}
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function WorkflowSpecGraph({
  steps,
  nodeStates,
  selectedStepId,
  onSelectStep,
  onRunStep,
  runningStepIds,
  disabledRun,
}: {
  steps: WorkflowTemplateStepSummary[]
  nodeStates: Record<string, WorkflowStepNodeState>
  selectedStepId: string
  onSelectStep: (stepId: string) => void
  onRunStep: (stepId: string) => void
  runningStepIds: string[]
  disabledRun: boolean
}) {
  const runningSet = useMemo(() => new Set(runningStepIds), [runningStepIds])
  const stepIdSet = useMemo(() => new Set(steps.map((step) => step.id)), [steps])
  const graphNodes = useMemo<FlowNode[]>(() => steps.map((step, index) => {
    const status = nodeStates[step.id]?.status || ""
    const running = runningSet.has(step.id) || status === "running"
    const selected = selectedStepId === step.id
    const x = (index % 2) * 230
    const y = Math.floor(index / 2) * 112
    const border = selected
      ? "#67e8f9"
      : running
      ? "#22d3ee"
      : status === "completed"
      ? "#6ee7b7"
      : status === "failed"
      ? "#fca5a5"
      : "rgba(255,255,255,0.12)"
    return {
      id: step.id,
      position: { x, y },
      data: {
        label: (
          <div className="nodrag min-w-0">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate text-[11px] font-semibold text-zinc-50">{step.title || step.id}</div>
                <div className="mt-0.5 truncate text-[9px] text-zinc-500">{workflowRunnerDisplay(step.runner, step.node_type) || workflowStepKindLabel(step)}</div>
              </div>
              <button
                type="button"
                disabled={disabledRun}
                onClick={(event) => {
                  event.stopPropagation()
                  onRunStep(step.id)
                }}
                className="flex h-5 w-5 shrink-0 items-center justify-center rounded border border-white/10 text-[9px] text-zinc-300 transition hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-35"
              >
                {running ? "..." : "▶"}
              </button>
            </div>
            <div className="mt-2 h-1 rounded bg-white/[0.06]">
              <div
                className={cn(
                  "h-1 rounded",
                  running ? "bg-cyan-300" : status === "completed" ? "bg-emerald-300" : status === "failed" ? "bg-red-300" : "bg-white/12",
                )}
                style={{ width: status === "completed" ? "100%" : running ? "62%" : status === "failed" ? "100%" : "28%" }}
              />
            </div>
          </div>
        ),
      },
      type: "default",
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      draggable: true,
      style: {
        width: 198,
        minHeight: 68,
        border,
        borderRadius: 8,
        background: selected ? "rgba(8,145,178,0.16)" : "rgba(2,6,23,0.82)",
        color: "#f4f4f5",
        boxShadow: selected ? "0 0 0 1px rgba(103,232,249,0.25)" : "none",
        padding: 8,
      },
    }
  }), [disabledRun, nodeStates, onRunStep, runningSet, selectedStepId, steps])
  const graphEdges = useMemo<FlowEdge[]>(() => {
    const result: FlowEdge[] = []
    for (const step of steps) {
      for (const dep of workflowCleanIdList(step.depends_on)) {
        const source = String(dep || "").trim()
        if (!source || !stepIdSet.has(source)) continue
        result.push({
          id: `workflow-${source}-${step.id}`,
          source,
          target: step.id,
          type: "smoothstep",
          style: { stroke: "rgba(148,163,184,0.58)", strokeWidth: 1.4 },
          markerEnd: { type: MarkerType.ArrowClosed, color: "rgba(148,163,184,0.72)" },
        })
      }
    }
    return result
  }, [stepIdSet, steps])

  return (
    <div className="h-[300px] overflow-hidden rounded-md border border-white/[0.08] bg-[#080d14]">
      <div className="flex h-8 items-center justify-between border-b border-white/[0.06] px-3">
        <div className="text-[11px] font-semibold text-zinc-300">流程图</div>
        <div className="text-[10px] text-zinc-600">可拖动节点查看结构</div>
      </div>
      <div className="h-[268px]">
        <ReactFlow
          nodes={graphNodes}
          edges={graphEdges}
          fitView
          fitViewOptions={{ padding: 0.18 }}
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable
          panOnScroll={false}
          zoomOnScroll
          onNodeClick={(_, node) => onSelectStep(node.id)}
          proOptions={{ hideAttribution: true }}
        >
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>
      </div>
    </div>
  )
}

const WORKFLOW_GRAPH_NODE_WIDTH = 272
const WORKFLOW_GRAPH_NODE_HEIGHT = 132
const WORKFLOW_GRAPH_COLUMN_GAP = 360
const WORKFLOW_GRAPH_LEVEL_GAP = 190
const WORKFLOW_EDITOR_INPUT_STEP_ID = "__workflow_root_inputs__"
const WORKFLOW_GRAPH_SNAP_DISTANCE = 14
const WORKFLOW_GRAPH_DRAG_COMMIT_DISTANCE = 10

type WorkflowAlignmentGuide = {
  id: string
  orientation: "vertical" | "horizontal"
  position: number
  start: number
  end: number
}

function WorkflowAlignmentGuideNode({ data }: NodeProps<{ orientation: "vertical" | "horizontal"; length: number }>) {
  const vertical = data.orientation === "vertical"
  return (
    <div
      className="pointer-events-none shadow-[0_0_14px_rgba(103,232,249,0.45)]"
      style={{
        width: vertical ? 1 : data.length,
        height: vertical ? data.length : 1,
        borderLeft: vertical ? "1px dashed rgba(103,232,249,0.82)" : undefined,
        borderTop: vertical ? undefined : "1px dashed rgba(103,232,249,0.82)",
      }}
    />
  )
}

const WORKFLOW_ALIGNMENT_NODE_TYPES = {
  workflowAlignmentGuide: WorkflowAlignmentGuideNode,
}

function WorkflowScopeToggleIcon({ expanded }: { expanded: boolean }) {
  return (
    <span className="relative flex h-4 w-4 items-center justify-center" aria-hidden="true">
      <span className="absolute left-0.5 top-0.5 h-2.5 w-2.5 rounded-[2px] border border-current opacity-55" />
      <span className="absolute bottom-0.5 right-0.5 h-2.5 w-2.5 rounded-[2px] border border-current bg-white/[0.06]" />
      <span
        className={cn(
          "absolute h-1.5 w-1.5 border-b border-r border-current transition-transform",
          expanded ? "translate-y-0.5 rotate-45" : "-translate-x-0.5 -rotate-45",
        )}
      />
    </span>
  )
}

function workflowGraphDependencyIds(
  step: WorkflowTemplateStepSummary,
  stepIds: Set<string>,
): string[] {
  const result: string[] = []
  const repeatParent = workflowStringValue(step.repeat_group_id)
  if (repeatParent && repeatParent !== step.id && stepIds.has(repeatParent)) result.push(repeatParent)
  for (const rawDep of workflowCleanIdList(step.depends_on)) {
    const dep = workflowStringValue(rawDep)
    if (dep && dep !== step.id && stepIds.has(dep) && !result.includes(dep)) result.push(dep)
  }
  for (const rawDep of workflowCleanIdList(step.layout_after)) {
    const dep = workflowStringValue(rawDep)
    if (dep && dep !== step.id && stepIds.has(dep) && !result.includes(dep)) result.push(dep)
  }
  return result
}

function workflowNearestFreeLane(used: Set<number>, desired: number): number {
  const rounded = Math.max(0, Math.round(Number.isFinite(desired) ? desired : 0))
  if (!used.has(rounded)) return rounded
  for (let offset = 1; offset < 100; offset += 1) {
    const down = rounded + offset
    if (!used.has(down)) return down
    const up = rounded - offset
    if (up >= 0 && !used.has(up)) return up
  }
  return used.size
}

function workflowGraphAutoLayout(steps: WorkflowTemplateStepSummary[]): Map<string, { x: number; y: number }> {
  const result = new Map<string, { x: number; y: number }>()
  const stepById = new Map(steps.map((step) => [step.id, step]))
  const stepIds = new Set(stepById.keys())
  const dependencyById = new Map<string, string[]>()
  for (const step of steps) {
    dependencyById.set(step.id, workflowGraphDependencyIds(step, stepIds))
  }
  const levelCache = new Map<string, number>()
  const visiting = new Set<string>()
  const levelOf = (stepId: string): number => {
    if (levelCache.has(stepId)) return levelCache.get(stepId) || 0
    if (visiting.has(stepId)) return 0
    visiting.add(stepId)
    const deps = dependencyById.get(stepId) || []
    const level = deps.length > 0 ? Math.max(...deps.map((dep) => levelOf(dep))) + 1 : 0
    visiting.delete(stepId)
    levelCache.set(stepId, level)
    return level
  }
  for (const step of steps) levelOf(step.id)
  const ordered = [...steps].sort((a, b) => {
    const levelDelta = (levelCache.get(a.id) || 0) - (levelCache.get(b.id) || 0)
    if (levelDelta !== 0) return levelDelta
    return steps.indexOf(a) - steps.indexOf(b)
  })
  const usedLanesByLevel = new Map<number, Set<number>>()
  const laneById = new Map<string, number>()
  let rootLane = 0
  for (const step of ordered) {
    const level = levelCache.get(step.id) || 0
    const used = usedLanesByLevel.get(level) || new Set<number>()
    usedLanesByLevel.set(level, used)
    const deps = dependencyById.get(step.id) || []
    const parentLanes = deps.map((dep) => laneById.get(dep)).filter((lane): lane is number => typeof lane === "number")
    const repeatParent = workflowStringValue(step.repeat_group_id)
    const repeatParentLane = repeatParent ? laneById.get(repeatParent) : undefined
    const desired = parentLanes.length > 0
      ? parentLanes.reduce((sum, lane) => sum + lane, 0) / parentLanes.length
      : typeof repeatParentLane === "number"
      ? repeatParentLane
      : rootLane
    const lane = workflowNearestFreeLane(used, desired)
    if (deps.length === 0 && typeof repeatParentLane !== "number") rootLane = Math.max(rootLane, lane + 1)
    used.add(lane)
    laneById.set(step.id, lane)
    result.set(step.id, {
      x: level * WORKFLOW_GRAPH_COLUMN_GAP,
      y: lane * WORKFLOW_GRAPH_LEVEL_GAP,
    })
  }
  return result
}

function workflowGraphSnapDragPosition(
  node: FlowNode,
  nodes: FlowNode[],
): { position: { x: number; y: number }; guides: WorkflowAlignmentGuide[] } {
  const dragged = workflowNormalizeEditorPosition(node.position)
  let bestX: { delta: number; guide: WorkflowAlignmentGuide } | null = null
  let bestY: { delta: number; guide: WorkflowAlignmentGuide } | null = null

  const draggedXAnchors = [
    { value: dragged.x, offset: 0 },
    { value: dragged.x + WORKFLOW_GRAPH_NODE_WIDTH / 2, offset: WORKFLOW_GRAPH_NODE_WIDTH / 2 },
    { value: dragged.x + WORKFLOW_GRAPH_NODE_WIDTH, offset: WORKFLOW_GRAPH_NODE_WIDTH },
  ]
  const draggedYAnchors = [
    { value: dragged.y, offset: 0 },
    { value: dragged.y + WORKFLOW_GRAPH_NODE_HEIGHT / 2, offset: WORKFLOW_GRAPH_NODE_HEIGHT / 2 },
    { value: dragged.y + WORKFLOW_GRAPH_NODE_HEIGHT, offset: WORKFLOW_GRAPH_NODE_HEIGHT },
  ]

  for (const other of nodes) {
    if (other.id === node.id || String(other.id).startsWith("__workflow_alignment_")) continue
    const otherPosition = workflowNormalizeEditorPosition(other.position)
    const otherXAnchors = [
      otherPosition.x,
      otherPosition.x + WORKFLOW_GRAPH_NODE_WIDTH / 2,
      otherPosition.x + WORKFLOW_GRAPH_NODE_WIDTH,
    ]
    const otherYAnchors = [
      otherPosition.y,
      otherPosition.y + WORKFLOW_GRAPH_NODE_HEIGHT / 2,
      otherPosition.y + WORKFLOW_GRAPH_NODE_HEIGHT,
    ]
    for (const draggedAnchor of draggedXAnchors) {
      for (const otherAnchor of otherXAnchors) {
        const delta = otherAnchor - draggedAnchor.value
        if (Math.abs(delta) > WORKFLOW_GRAPH_SNAP_DISTANCE) continue
        if (bestX && Math.abs(bestX.delta) <= Math.abs(delta)) continue
        const top = Math.min(dragged.y, otherPosition.y) - 36
        const bottom = Math.max(dragged.y + WORKFLOW_GRAPH_NODE_HEIGHT, otherPosition.y + WORKFLOW_GRAPH_NODE_HEIGHT) + 36
        bestX = {
          delta,
          guide: {
            id: `__workflow_alignment_v_${Math.round(otherAnchor * 10)}`,
            orientation: "vertical",
            position: otherAnchor,
            start: top,
            end: bottom,
          },
        }
      }
    }
    for (const draggedAnchor of draggedYAnchors) {
      for (const otherAnchor of otherYAnchors) {
        const delta = otherAnchor - draggedAnchor.value
        if (Math.abs(delta) > WORKFLOW_GRAPH_SNAP_DISTANCE) continue
        if (bestY && Math.abs(bestY.delta) <= Math.abs(delta)) continue
        const left = Math.min(dragged.x, otherPosition.x) - 36
        const right = Math.max(dragged.x + WORKFLOW_GRAPH_NODE_WIDTH, otherPosition.x + WORKFLOW_GRAPH_NODE_WIDTH) + 36
        bestY = {
          delta,
          guide: {
            id: `__workflow_alignment_h_${Math.round(otherAnchor * 10)}`,
            orientation: "horizontal",
            position: otherAnchor,
            start: left,
            end: right,
          },
        }
      }
    }
  }

  return {
    position: workflowNormalizeEditorPosition({
      x: dragged.x + (bestX?.delta || 0),
      y: dragged.y + (bestY?.delta || 0),
    }),
    guides: [bestX?.guide, bestY?.guide].filter((guide): guide is WorkflowAlignmentGuide => Boolean(guide)),
  }
}

function WorkflowEditorGraph({
  steps,
  nodeStates,
  selectedStepId,
  onSelectStep,
  onRunStep,
  onMoveStep,
  onConnectSteps,
  onDisconnectSteps,
  onToggleStepScope,
  onCreateStep,
  onDeleteSteps,
  insertNodeTypes = [],
  collapsedScopeIds,
  scopeChildCounts,
  runningStepIds,
  disabledRun,
  editable = true,
  showRunButton = true,
}: {
  steps: WorkflowTemplateStepSummary[]
  nodeStates: Record<string, WorkflowStepNodeState>
  selectedStepId: string
  onSelectStep: (stepId: string | null) => void
  onRunStep: (stepId: string) => void
  onMoveStep: (stepId: string, position: { x: number; y: number }) => void
  onConnectSteps: (source: string, target: string) => void
  onDisconnectSteps: (source: string, target: string) => void
  onToggleStepScope?: (stepId: string) => void
  onCreateStep?: (item: WorkflowNodeTypeDefinition, options?: WorkflowAddStepOptions) => void
  onDeleteSteps?: (stepIds: string[]) => void
  insertNodeTypes?: WorkflowNodeTypeDefinition[]
  collapsedScopeIds?: Set<string>
  scopeChildCounts?: Record<string, number>
  runningStepIds: string[]
  disabledRun: boolean
  editable?: boolean
  showRunButton?: boolean
}) {
  const runningSet = useMemo(() => new Set(runningStepIds), [runningStepIds])
  const stepById = useMemo(() => new Map(steps.map((step) => [step.id, step])), [steps])
  const [insertMenuStepId, setInsertMenuStepId] = useState<string | null>(null)
  const [createMenu, setCreateMenu] = useState<WorkflowEditorCreateMenu | null>(null)
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance | null>(null)
  const layoutPositions = useMemo(() => workflowGraphAutoLayout(steps), [steps])
  const explicitPositionById = useMemo(() => {
    const result = new Map<string, { x: number; y: number }>()
    for (const step of steps) {
      const position = workflowExplicitEditorPosition(step)
      if (position) result.set(step.id, position)
    }
    return result
  }, [steps])
  const sourceNodes = useMemo<FlowNode[]>(() => steps.map((step, index) => {
    const graphNodeState = editable ? undefined : nodeStates[step.id]
    const status = graphNodeState?.status || ""
    const running = !editable && (runningSet.has(step.id) || status === "running")
    const selected = selectedStepId === step.id
    const kind = workflowStepAuthoringKind(step)
    const isInputStep = step.id === WORKFLOW_EDITOR_INPUT_STEP_ID || workflowStepIsInputStep(step)
    const kindLabel = isInputStep ? "输入字段" : WORKFLOW_AUTHORING_KIND_OPTIONS.find((item) => item.value === kind)?.label || kind
    const childScopeId = workflowStepChildScopeId(step)
    const canToggleScope = editable && Boolean(childScopeId) && Boolean(onToggleStepScope)
    const scopeExpanded = Boolean(childScopeId) && !collapsedScopeIds?.has(childScopeId)
    const scopeChildCount = childScopeId ? scopeChildCounts?.[childScopeId] || 0 : 0
    const addMenuOpen = editable && insertMenuStepId === step.id && insertNodeTypes.length > 0 && Boolean(onCreateStep)
    const position = editable
      ? workflowExplicitEditorPosition(step) || layoutPositions.get(step.id) || workflowEditorPosition(step, index)
      : layoutPositions.get(step.id) || workflowEditorPosition(step, index)
    const tone = selected
      ? "rgba(103,232,249,0.95)"
      : running
      ? "rgba(34,211,238,0.78)"
      : status === "completed"
      ? "rgba(110,231,183,0.72)"
      : status === "failed"
      ? "rgba(252,165,165,0.78)"
      : childScopeId
      ? "rgba(167,139,250,0.52)"
      : "rgba(255,255,255,0.14)"
    const isProductStep = workflowStepIsCanvasProduct(step)
    const isLoopKind = kind === "loop" || Boolean(childScopeId)
    const categoryLabel = isInputStep
      ? "流程输入"
      : isProductStep
      ? "画布产物"
      : isLoopKind
      ? "流程控制"
      : "处理动作"
    const categoryClass = isInputStep
      ? "border-amber-200/30 bg-amber-300/[0.12] text-amber-100"
      : isProductStep
      ? "border-cyan-200/28 bg-cyan-300/[0.12] text-cyan-50"
      : isLoopKind
      ? "border-violet-200/24 bg-violet-300/[0.09] text-violet-100"
      : "border-white/[0.08] bg-white/[0.035] text-zinc-300"
    const productSourceId = isProductStep ? workflowProductSourceStep(step) : ""
    const productSourceTitle = productSourceId ? workflowStepTitleById(steps, productSourceId) : ""
    const dependencyCount = workflowCleanIdList(step.depends_on).length
    const footerText = isProductStep
      ? productSourceTitle
        ? `来自 ${productSourceTitle}`
        : "选择上游输出"
      : step.description || workflowStepKindLabel(step)
    const cardBackground = selected
      ? "linear-gradient(145deg, rgba(8,145,178,0.24), rgba(9,16,25,0.98) 58%, rgba(5,9,15,0.99))"
      : isInputStep
      ? "linear-gradient(145deg, rgba(120,53,15,0.34), rgba(24,18,12,0.98) 62%)"
      : isProductStep
      ? "linear-gradient(145deg, rgba(8,47,73,0.82), rgba(6,55,60,0.42) 48%, rgba(7,11,18,0.98))"
      : childScopeId
      ? "linear-gradient(145deg, rgba(88,28,135,0.38), rgba(12,13,22,0.98) 64%)"
      : "linear-gradient(145deg, rgba(25,31,40,0.88), rgba(7,11,18,0.98) 68%)"
    const cardShadow = selected
      ? "0 0 0 1px rgba(103,232,249,0.34), 0 20px 42px rgba(0,0,0,0.38), 0 0 30px rgba(34,211,238,0.08)"
      : isProductStep
      ? "inset 3px 0 0 rgba(34,211,238,0.7), 0 18px 34px rgba(0,0,0,0.3)"
      : "0 16px 30px rgba(0,0,0,0.24)"
    return {
      id: step.id,
      position,
      data: {
        label: (
          <div className="relative min-w-0">
            <div className="mb-2.5 flex items-center justify-between gap-2">
              <div className={cn("inline-flex max-w-full items-center rounded-md border px-1.5 py-0.5 text-[9px] font-semibold", categoryClass)}>
                <span className="truncate">{categoryLabel}</span>
              </div>
              <span className="max-w-[112px] truncate font-mono text-[8px] tracking-wide text-zinc-600" title={step.id}>{step.id}</span>
            </div>
            <div className="mb-2.5 flex items-start gap-2.5">
              <div className={cn(
                "flex h-9 w-9 shrink-0 items-center justify-center border text-[12px] font-semibold",
                isProductStep ? "rounded-xl shadow-[0_0_20px_rgba(34,211,238,0.14)]" : "rounded-lg",
                workflowStepToneClass(step),
              )}>
                {isInputStep ? "入" : workflowStepGraphIcon(step)}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-semibold tracking-[0.01em] text-zinc-50">{step.title || step.id}</div>
                <div className="mt-1 flex items-center gap-1.5 text-[9px] text-zinc-500">
                  <span className="truncate">{kindLabel}</span>
                  <span className="text-zinc-700">/</span>
                  <span className="truncate">{isInputStep ? "字段集合" : workflowStepOutputLabel(step)}</span>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {showRunButton && (
                  <button
                    type="button"
                    disabled={disabledRun}
                    onClick={(event) => {
                      event.stopPropagation()
                      onRunStep(step.id)
                    }}
                    className="nodrag flex h-7 min-w-7 shrink-0 items-center justify-center rounded border border-white/10 bg-black/28 px-1.5 text-[10px] font-semibold text-zinc-300 transition hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-35"
                    title="运行这一步"
                  >
                    {running ? "..." : "运行"}
                  </button>
                )}
                {canToggleScope && (
                  <button
                    type="button"
                    aria-label={scopeExpanded ? "收起循环内部步骤" : "查看循环内部步骤"}
                    onClick={(event) => {
                      event.stopPropagation()
                      setInsertMenuStepId(null)
                      onToggleStepScope?.(step.id)
                    }}
                    className={cn(
                      "nodrag flex h-7 w-7 shrink-0 items-center justify-center rounded border text-cyan-100 transition",
                      scopeExpanded
                        ? "border-cyan-200/25 bg-cyan-300/[0.14] hover:bg-cyan-300/20"
                        : "border-white/10 bg-white/[0.04] hover:border-cyan-200/22 hover:bg-cyan-300/10",
                    )}
                    title={scopeExpanded ? "收起循环内部步骤" : "查看循环内部步骤"}
                  >
                    <WorkflowScopeToggleIcon expanded={scopeExpanded} />
                  </button>
                )}
                {editable && !isInputStep && onCreateStep && insertNodeTypes.length > 0 && (
                  <button
                    type="button"
                    aria-label={childScopeId ? "添加到循环内部" : "添加下一步"}
                    onClick={(event) => {
                      event.stopPropagation()
                      setInsertMenuStepId((current) => current === step.id ? null : step.id)
                    }}
                    className="nodrag flex h-7 w-7 shrink-0 items-center justify-center rounded border border-cyan-200/22 bg-cyan-300/[0.08] text-sm font-semibold text-cyan-100 transition hover:bg-cyan-300/[0.16]"
                    title={childScopeId ? "添加到循环内部" : "添加下一步"}
                  >
                    +
                  </button>
                )}
              </div>
            </div>
            <div className="flex min-h-6 items-center justify-between gap-2 border-t border-white/[0.055] pt-2 text-[9px] text-zinc-500">
              <span className={cn("min-w-0 truncate", isProductStep ? "text-cyan-100/68" : "text-zinc-500")}>{footerText}</span>
              <div className="flex shrink-0 items-center gap-1">
                {dependencyCount > 0 && (
                  <span className="rounded-md border border-white/[0.07] bg-black/18 px-1.5 py-0.5 text-zinc-500">
                    上游 {dependencyCount}
                  </span>
                )}
                {scopeChildCount > 0 && (
                  <span className="rounded border border-violet-200/18 bg-violet-300/10 px-1.5 py-0.5 text-violet-100">
                    {scopeExpanded ? `显示 ${scopeChildCount}` : `收起 ${scopeChildCount}`}
                  </span>
                )}
                {graphNodeState && (
                  <span className={cn("rounded border px-1.5 py-0.5", workflowStepStateClass(status))}>
                    {workflowStepAggregateLabel(graphNodeState)}
                  </span>
                )}
              </div>
            </div>
            {addMenuOpen && (
              <div
                className="nodrag absolute left-[calc(100%+10px)] top-0 z-50 w-44 overflow-hidden rounded-md border border-cyan-200/20 bg-[#111823] shadow-2xl shadow-black/45"
                onClick={(event) => event.stopPropagation()}
              >
                <div className="border-b border-white/[0.08] px-2.5 py-2 text-[10px] font-semibold text-cyan-100">
                  {childScopeId ? "添加到循环里" : "添加下一步"}
                </div>
                <div className="grid gap-1 p-1.5">
                  {insertNodeTypes.slice(0, 8).map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation()
                        onCreateStep?.(item, { afterStepId: step.id })
                        setInsertMenuStepId(null)
                      }}
                      className="rounded px-2 py-1.5 text-left transition hover:bg-cyan-300/[0.08]"
                    >
                      <span className="block truncate text-[11px] font-semibold text-zinc-100">{item.title || item.name || item.type}</span>
                      <span className="mt-0.5 block truncate text-[9px] text-zinc-500">{item.description || "添加处理步骤"}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        ),
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      type: "default",
      draggable: editable && !isInputStep,
      connectable: editable && !isInputStep,
      style: {
        width: WORKFLOW_GRAPH_NODE_WIDTH,
        minHeight: WORKFLOW_GRAPH_NODE_HEIGHT,
        border: `1px solid ${tone}`,
        borderRadius: 12,
        background: cardBackground,
        boxShadow: cardShadow,
        color: "#f4f4f5",
        padding: 12,
        overflow: "visible",
      },
    }
  }), [collapsedScopeIds, disabledRun, editable, insertMenuStepId, insertNodeTypes, layoutPositions, nodeStates, onCreateStep, onRunStep, onToggleStepScope, runningSet, scopeChildCounts, selectedStepId, showRunButton, steps])

  const sourceEdges = useMemo<FlowEdge[]>(() => {
    const result: FlowEdge[] = []
    for (const step of steps) {
      for (const dep of workflowCleanIdList(step.depends_on)) {
        const source = String(dep || "").trim()
        if (!source || !stepById.has(source)) continue
        const status = editable ? "" : nodeStates[step.id]?.status || ""
        result.push({
          id: `workflow-editor-${source}-${step.id}`,
          source,
          target: step.id,
          type: "smoothstep",
          interactionWidth: 20,
          style: {
            stroke: status === "failed" ? "rgba(248,113,113,0.78)" : "rgba(148,163,184,0.72)",
            strokeWidth: 2.2,
          },
          markerEnd: { type: MarkerType.ArrowClosed, color: "rgba(148,163,184,0.78)" },
        })
      }
      for (const dep of workflowCleanIdList(step.layout_after)) {
        const source = String(dep || "").trim()
        if (!source || !stepById.has(source)) continue
        result.push({
          id: `workflow-editor-layout-${source}-${step.id}`,
          source,
          target: step.id,
          type: "smoothstep",
          interactionWidth: 18,
          style: {
            stroke: "rgba(103,232,249,0.48)",
            strokeWidth: 1.8,
            strokeDasharray: "5 5",
          },
          markerEnd: { type: MarkerType.ArrowClosed, color: "rgba(103,232,249,0.58)" },
        })
      }
      const repeatParent = workflowStringValue(step.repeat_group_id)
      const groupDependencies = workflowCleanIdList(step.depends_on)
        .map((dep) => stepById.get(workflowStringValue(dep)))
        .filter((depStep): depStep is WorkflowTemplateStepSummary => Boolean(depStep))
        .filter((depStep) => workflowStringValue(depStep.repeat_group_id) === repeatParent)
      if (repeatParent && stepById.has(repeatParent) && groupDependencies.length === 0) {
        result.push({
          id: `workflow-editor-scope-${repeatParent}-${step.id}`,
          source: repeatParent,
          target: step.id,
          type: "smoothstep",
          interactionWidth: 14,
          style: {
            stroke: "rgba(167,139,250,0.48)",
            strokeWidth: 1.8,
            strokeDasharray: "4 5",
          },
          markerEnd: { type: MarkerType.ArrowClosed, color: "rgba(167,139,250,0.62)" },
        })
      }
    }
    return result
  }, [editable, nodeStates, stepById, steps])
  const [flowNodes, setFlowNodes] = useState<FlowNode[]>(sourceNodes)
  const [flowEdges, setFlowEdges] = useState<FlowEdge[]>(sourceEdges)
  const [alignmentGuides, setAlignmentGuides] = useState<WorkflowAlignmentGuide[]>([])
  const flowNodesRef = useRef<FlowNode[]>(sourceNodes)
  const dragStartPositionRef = useRef<Map<string, { x: number; y: number }>>(new Map())

  const guideNodes = useMemo<FlowNode[]>(() => alignmentGuides.map((guide) => {
    const length = Math.max(1, guide.end - guide.start)
    return {
      id: guide.id,
      type: "workflowAlignmentGuide",
      position: guide.orientation === "vertical"
        ? { x: guide.position, y: guide.start }
        : { x: guide.start, y: guide.position },
      data: { orientation: guide.orientation, length },
      draggable: false,
      selectable: false,
      connectable: false,
      focusable: false,
      style: {
        pointerEvents: "none",
        zIndex: 1000,
      },
    }
  }), [alignmentGuides])

  const renderedNodes = useMemo(() => [...flowNodes, ...guideNodes], [flowNodes, guideNodes])

  useEffect(() => {
    setFlowNodes((current) => {
      if (!editable) return sourceNodes
      const currentPositionById = new Map(current.map((node) => [node.id, node.position]))
      return sourceNodes.map((node) => ({
        ...node,
        position: explicitPositionById.has(node.id) ? node.position : currentPositionById.get(node.id) || node.position,
      }))
    })
  }, [editable, explicitPositionById, sourceNodes])

  useEffect(() => {
    flowNodesRef.current = flowNodes
  }, [flowNodes])

  useEffect(() => {
    setFlowEdges(sourceEdges)
  }, [sourceEdges])

  useEffect(() => {
    if (!insertMenuStepId || steps.some((step) => step.id === insertMenuStepId)) return
    setInsertMenuStepId(null)
  }, [insertMenuStepId, steps])

  useEffect(() => {
    if (!createMenu?.sourceStepId || steps.some((step) => step.id === createMenu.sourceStepId)) return
    setCreateMenu(null)
  }, [createMenu?.sourceStepId, steps])

  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    if (!editable) return
    setFlowNodes((current) => applyFlowNodeChanges(changes, current))
  }, [editable])

  const handleEdgesChange = useCallback((changes: EdgeChange[]) => {
    if (!editable) return
    setFlowEdges((current) => applyFlowEdgeChanges(changes, current))
    for (const change of changes) {
      if (change.type !== "remove") continue
      const edge = flowEdges.find((item) => item.id === change.id)
      if (edge) onDisconnectSteps(edge.source, edge.target)
    }
  }, [editable, flowEdges, onDisconnectSteps])

  const handleConnect = useCallback((connection: Connection) => {
    if (!editable) return
    if (!connection.source || !connection.target || connection.source === connection.target) return
    onConnectSteps(connection.source, connection.target)
  }, [editable, onConnectSteps])

  const handleNodeDragStart = useCallback((_: MouseEvent, node: FlowNode) => {
    if (!editable) return
    dragStartPositionRef.current.set(node.id, workflowNormalizeEditorPosition(node.position))
    setAlignmentGuides([])
  }, [editable])

  const handleNodeDrag = useCallback((_: MouseEvent, node: FlowNode) => {
    if (!editable) return
    const snap = workflowGraphSnapDragPosition(node, flowNodesRef.current)
    setAlignmentGuides(snap.guides)
    const nextNodes = flowNodesRef.current.map((item) => item.id === node.id ? { ...item, position: snap.position } : item)
    flowNodesRef.current = nextNodes
    setFlowNodes(nextNodes)
  }, [editable])

  const handleNodeDragStop = useCallback((_: MouseEvent, node: FlowNode) => {
    if (!editable) return
    setAlignmentGuides([])
    const finalPosition = workflowGraphSnapDragPosition(node, flowNodesRef.current).position
    const nextNodes = flowNodesRef.current.map((item) => item.id === node.id ? { ...item, position: finalPosition } : item)
    flowNodesRef.current = nextNodes
    setFlowNodes(nextNodes)
    const startPosition = dragStartPositionRef.current.get(node.id)
    dragStartPositionRef.current.delete(node.id)
    if (startPosition) {
      const dx = finalPosition.x - startPosition.x
      const dy = finalPosition.y - startPosition.y
      if (Math.hypot(dx, dy) < WORKFLOW_GRAPH_DRAG_COMMIT_DISTANCE) {
        const revertedNodes = flowNodesRef.current.map((item) => item.id === node.id ? { ...item, position: startPosition } : item)
        flowNodesRef.current = revertedNodes
        setFlowNodes(revertedNodes)
        return
      }
    }
    onMoveStep(node.id, finalPosition)
  }, [editable, onMoveStep])

  const handlePaneContextMenu = useCallback((event: MouseEvent) => {
    if (!editable || !flowInstance) return
    event.preventDefault()
    const position = flowInstance.screenToFlowPosition({ x: event.clientX, y: event.clientY })
    setInsertMenuStepId(null)
    setCreateMenu({
      x: event.clientX,
      y: event.clientY,
      position: workflowNormalizeEditorPosition({
        x: position.x - WORKFLOW_GRAPH_NODE_WIDTH / 2,
        y: position.y - WORKFLOW_GRAPH_NODE_HEIGHT / 2,
      }),
    })
  }, [editable, flowInstance])

  const handleNodeContextMenu = useCallback((event: MouseEvent, node: FlowNode) => {
    if (!editable || !flowInstance || node.id === WORKFLOW_EDITOR_INPUT_STEP_ID || String(node.id).startsWith("__workflow_alignment_")) return
    event.preventDefault()
    event.stopPropagation()
    const position = flowInstance.screenToFlowPosition({ x: event.clientX, y: event.clientY })
    setInsertMenuStepId(null)
    setCreateMenu({
      x: event.clientX,
      y: event.clientY,
      sourceStepId: node.id,
      position: workflowNormalizeEditorPosition({
        x: position.x - WORKFLOW_GRAPH_NODE_WIDTH / 2,
        y: position.y - WORKFLOW_GRAPH_NODE_HEIGHT / 2,
      }),
    })
    onSelectStep(node.id)
  }, [editable, flowInstance, onSelectStep])

  const handleNodesDelete = useCallback((deletedNodes: FlowNode[]) => {
    if (!editable || !onDeleteSteps) return
    const stepIds = deletedNodes
      .map((node) => String(node.id))
      .filter((id) => id && !id.startsWith("__workflow_alignment_") && stepById.has(id))
    if (stepIds.length === 0) return
    setInsertMenuStepId(null)
    setCreateMenu(null)
    onDeleteSteps(stepIds)
  }, [editable, onDeleteSteps, stepById])

  const createStepFromMenu = useCallback((item: WorkflowNodeTypeDefinition) => {
    if (!createMenu || !onCreateStep) return
    onCreateStep(item, createMenu.sourceStepId
      ? { afterStepId: createMenu.sourceStepId, position: createMenu.position }
      : { position: createMenu.position, detached: true })
    setCreateMenu(null)
  }, [createMenu, onCreateStep])

  return (
    <ReactFlow
      nodes={renderedNodes}
      edges={flowEdges}
      nodeTypes={WORKFLOW_ALIGNMENT_NODE_TYPES}
      defaultEdgeOptions={{
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed, color: "rgba(148,163,184,0.78)" },
      }}
      nodesDraggable={editable}
      nodesConnectable={editable}
      elementsSelectable
      selectNodesOnDrag={false}
      selectionOnDrag={editable}
      selectionMode={SelectionMode.Partial}
      panOnDrag={editable ? [1] : true}
      panOnScroll
      zoomOnScroll
      zoomOnPinch
      deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
      minZoom={0.28}
      maxZoom={1.8}
      defaultViewport={{ x: steps.some((step) => step.id === WORKFLOW_EDITOR_INPUT_STEP_ID) ? 356 : 48, y: 96, zoom: 0.86 }}
      onInit={setFlowInstance}
      onNodesChange={handleNodesChange}
      onEdgesChange={handleEdgesChange}
      onNodesDelete={handleNodesDelete}
      onConnect={handleConnect}
      onNodeDragStart={handleNodeDragStart}
      onNodeDrag={handleNodeDrag}
      onNodeDragStop={handleNodeDragStop}
      onPaneContextMenu={handlePaneContextMenu}
      onNodeContextMenu={handleNodeContextMenu}
      onNodeClick={(_, node) => {
        setInsertMenuStepId((current) => current === node.id ? current : null)
        setCreateMenu(null)
        onSelectStep(node.id)
      }}
      onNodeDoubleClick={(_, node) => {
        setInsertMenuStepId(null)
        setCreateMenu(null)
        onToggleStepScope?.(node.id)
      }}
      onPaneClick={() => {
        setInsertMenuStepId(null)
        setCreateMenu(null)
        onSelectStep(null)
      }}
      className="bg-[#070b11]"
      proOptions={{ hideAttribution: true }}
    >
      <Background color="rgba(148,163,184,0.13)" gap={22} size={1} />
      <MiniMap
        pannable
        zoomable
        className="!rounded-md !border !border-white/10 !bg-[#11151d]/90"
        nodeColor={() => "#64748b"}
        maskColor="rgba(3,7,18,0.62)"
      />
      <Controls className="!rounded-md !border !border-white/10 !bg-[#11151d]/90 [&_button]:!border-white/10 [&_button]:!bg-transparent [&_button]:!text-zinc-300 hover:[&_button]:!bg-white/10" />
      {createMenu && insertNodeTypes.length > 0 && (
        <div
          className="fixed z-[80] w-48 overflow-hidden rounded-md border border-cyan-200/20 bg-[#111823]/96 py-1 text-sm text-zinc-200 shadow-2xl shadow-black/50 backdrop-blur"
          style={menuPositionStyle(createMenu.x, createMenu.y, 192, 274)}
          onClick={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <div className="border-b border-white/[0.08] px-3 py-2 text-[10px] font-semibold text-cyan-100/80">
            {createMenu.sourceStepId ? "接在这个节点后" : "在这里创建节点"}
          </div>
          {insertNodeTypes.slice(0, 8).map((item) => (
            <button
              key={item.id}
              type="button"
              className="block w-full px-3 py-2 text-left transition-colors hover:bg-cyan-300/[0.08]"
              onClick={() => createStepFromMenu(item)}
            >
              <span className="block truncate text-[11px] font-semibold text-zinc-100">{item.title || item.name || item.type}</span>
              <span className="mt-0.5 block truncate text-[9px] text-zinc-500">{item.description || "添加处理步骤"}</span>
            </button>
          ))}
        </div>
      )}
    </ReactFlow>
  )
}

function WorkflowMediaOptionGrid({
  label,
  value,
  options,
  onChange,
  disabled = false,
  columns = "grid-cols-3",
  hint,
}: {
  label: string
  value: string
  options: Array<{ label: string; value: string; disabled?: boolean; hint?: string }>
  onChange: (value: string) => void
  disabled?: boolean
  columns?: string
  hint?: string
}) {
  const visibleOptions = options.length > 0 ? options : [{ label: "未配置", value: "", disabled: true }]
  return (
    <div className="rounded-md border border-cyan-200/10 bg-black/16 p-2">
      <div className="mb-1.5 text-[10px] font-semibold text-cyan-100/65">{label}</div>
      <div className={`grid ${columns} gap-1.5`}>
        {visibleOptions.map((option) => {
          const active = value === option.value
          return (
            <button
              key={`${option.value}:${option.label}`}
              type="button"
              disabled={disabled || option.disabled}
              title={option.hint || option.label}
              onClick={() => onChange(option.value)}
              className={cn(
                "min-h-8 rounded-md border px-2 py-1 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-45",
                active
                  ? "border-zinc-100 bg-zinc-100 text-zinc-950 shadow-[0_8px_18px_rgba(255,255,255,0.10)]"
                  : "border-white/[0.08] bg-white/[0.035] text-zinc-300 hover:border-white/[0.16] hover:bg-white/[0.07] hover:text-zinc-50",
              )}
            >
              {option.label}
            </button>
          )
        })}
      </div>
      {hint && <div className="mt-1.5 text-[10px] leading-4 text-zinc-600">{hint}</div>}
    </div>
  )
}

function WorkflowStepInspector({
  step,
  steps,
  nodeState,
  running,
  workflowName,
  workflowDescription,
  workflowAdvanced,
  nodeTypes,
  mediaProviders = [],
  videoProtocols = [],
  mediaConfigError = null,
  mediaModelOverrides = {},
  readOnly,
  showRunButton,
  inputIds,
  inputSpecs,
  inputValues,
  requiredInputIds,
  missingRequiredInputIds,
  onWorkflowNameChange,
  onWorkflowDescriptionChange,
  onWorkflowAdvancedChange,
  onAddWorkflowInput,
  onAddWorkflowInputPreset,
  onRenameWorkflowInput,
  onDeleteWorkflowInput,
  onToggleWorkflowInputRequired,
  onUpdateWorkflowInputSpec,
  onInputValueChange,
  onMediaModelOverrideChange,
  onRunStep,
  onUpdateStep,
  onRenameStep,
  onMoveStepScope,
  onDeleteStep,
}: {
  step?: WorkflowTemplateStepSummary
  steps: WorkflowTemplateStepSummary[]
  nodeState?: WorkflowStepNodeState
  running: boolean
  workflowName: string
  workflowDescription: string
  workflowAdvanced: Record<string, unknown>
  nodeTypes: WorkflowNodeTypeDefinition[]
  mediaProviders?: MediaProviderSummary[]
  videoProtocols?: VideoProtocolSummary[]
  mediaConfigError?: string | null
  mediaModelOverrides?: Record<string, string>
  readOnly: boolean
  showRunButton: boolean
  inputIds: string[]
  inputSpecs: Record<string, WorkflowInputDraftSpec>
  inputValues: Record<string, string>
  requiredInputIds: string[]
  missingRequiredInputIds: string[]
  onWorkflowNameChange: (value: string) => void
  onWorkflowDescriptionChange: (value: string) => void
  onWorkflowAdvancedChange: (key: string, value: unknown) => void
  onAddWorkflowInput: () => void
  onAddWorkflowInputPreset: (preset: WorkflowInputPreset) => void
  onRenameWorkflowInput: (currentId: string, nextId: string) => void
  onDeleteWorkflowInput: (inputId: string) => void
  onToggleWorkflowInputRequired: (inputId: string, required: boolean) => void
  onUpdateWorkflowInputSpec: (inputId: string, patch: Partial<WorkflowInputDraftSpec>) => void
  onInputValueChange: (id: string, value: string) => void
  onMediaModelOverrideChange?: (stepId: string, value: string) => void
  onRunStep: (stepId: string) => void
  onUpdateStep: (stepId: string, patch: Partial<WorkflowTemplateStepSummary>) => void
  onRenameStep: (stepId: string, nextId: string) => void
  onMoveStepScope: (stepId: string, scopeId: string) => void
  onDeleteStep: (stepId: string) => void
}) {
  const requiredSet = useMemo(() => new Set(requiredInputIds), [requiredInputIds])
  const [activeTab, setActiveTab] = useState<WorkflowInspectorTab>("properties")
  const textFieldClass = "w-full rounded-md border border-white/10 bg-[#090e15] px-2 text-xs text-zinc-100 outline-none transition placeholder:text-zinc-600 focus:border-cyan-200/45 disabled:cursor-not-allowed disabled:opacity-60"
  const renderFormFieldConfig = (description = "运行前让用户输入的内容，比如剧情、时长、风格。") => (
    <section className="rounded-md border border-amber-200/16 bg-amber-300/[0.045] p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div>
          <div className="text-[11px] font-semibold text-amber-100">输入字段定义</div>
          <div className="mt-0.5 text-[10px] text-amber-100/60">{description}</div>
        </div>
        {!readOnly && (
          <button
            type="button"
            onClick={onAddWorkflowInput}
            className="h-6 rounded border border-cyan-200/20 bg-cyan-300/10 px-2 text-[10px] font-semibold text-cyan-100 transition hover:bg-cyan-300/16"
          >
            添加一项
          </button>
        )}
      </div>
      {!readOnly && (
        <div className="mb-2 rounded border border-amber-200/10 bg-black/16 p-2">
          <div className="mb-1.5 text-[10px] font-semibold text-amber-100/75">直接添加常用内容</div>
          <div className="flex flex-wrap gap-1.5">
            {WORKFLOW_FORM_INPUT_PRESETS.map((preset) => {
              const exists = inputIds.includes(preset.id)
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => onAddWorkflowInputPreset(preset)}
                  disabled={exists}
                  className="h-7 rounded border border-white/10 bg-white/[0.035] px-2 text-[10px] font-medium text-zinc-200 transition hover:border-cyan-200/30 hover:bg-cyan-300/[0.06] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {exists ? `${preset.label} 已有` : preset.label}
                </button>
              )
            })}
          </div>
        </div>
      )}
      <div className="grid gap-2">
        {inputIds.map((input) => {
          const spec = inputSpecs[input] || { type: "text" }
          return (
            <div key={input} className="grid gap-2 rounded border border-white/[0.06] bg-white/[0.025] p-2">
              <label className="block text-[10px] font-medium text-zinc-500">
                要输入什么
                <input
                  value={spec.label || ""}
                  onChange={(event) => onUpdateWorkflowInputSpec(input, { label: event.target.value })}
                  placeholder="例如：剧情"
                  disabled={readOnly}
                  className={cn(textFieldClass, "mt-1 h-7")}
                />
              </label>
              <div className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-2">
                <label className="block text-[10px] font-medium text-zinc-500">
                  输入方式
                  <select
                    value={spec.type || "text"}
                    onChange={(event) => onUpdateWorkflowInputSpec(input, { type: event.target.value })}
                    disabled={readOnly}
                    className={cn(textFieldClass, "mt-1 h-7")}
                  >
                    {WORKFLOW_INPUT_TYPE_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    ))}
                  </select>
                </label>
                <label className="flex h-7 items-center gap-1 text-[10px] text-zinc-400">
                  <input
                    type="checkbox"
                    checked={requiredSet.has(input)}
                    disabled={readOnly}
                    onChange={(event) => onToggleWorkflowInputRequired(input, event.target.checked)}
                  />
                  必填
                </label>
              </div>
              {workflowInputTypeUsesOptions(spec.type) && (
                <div className="rounded-md border border-white/[0.06] bg-black/16 p-2">
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="text-[10px] font-medium text-zinc-500">单选项</span>
                    {!readOnly && (
                      <button
                        type="button"
                        onClick={() => {
                          const currentOptions = spec.options || []
                          const optionId = `option_${currentOptions.length + 1}`
                          onUpdateWorkflowInputSpec(input, {
                            options: [...currentOptions, { value: optionId, label: `选项 ${currentOptions.length + 1}` }],
                          })
                        }}
                        className="h-6 rounded border border-cyan-200/18 bg-cyan-300/[0.06] px-2 text-[10px] text-cyan-100"
                      >
                        添加选项
                      </button>
                    )}
                  </div>
                  <div className="grid gap-1.5">
                    {(spec.options || []).map((option, optionIndex) => (
                      <div key={`${optionIndex}:${option.value}`} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-1.5">
                        <input
                          value={option.label}
                          disabled={readOnly}
                          aria-label={`选项 ${optionIndex + 1} 显示名称`}
                          onChange={(event) => onUpdateWorkflowInputSpec(input, {
                            options: (spec.options || []).map((item, index) => index === optionIndex ? { ...item, label: event.target.value } : item),
                          })}
                          placeholder="显示名称"
                          className={cn(textFieldClass, "h-7")}
                        />
                        <input
                          value={option.value}
                          disabled={readOnly}
                          aria-label={`选项 ${optionIndex + 1} 提交值`}
                          onChange={(event) => onUpdateWorkflowInputSpec(input, {
                            options: (spec.options || []).map((item, index) => index === optionIndex ? { ...item, value: event.target.value } : item),
                          })}
                          placeholder="提交值"
                          className={cn(textFieldClass, "h-7")}
                        />
                        {!readOnly && (
                          <button
                            type="button"
                            onClick={() => onUpdateWorkflowInputSpec(input, {
                              options: (spec.options || []).filter((_, index) => index !== optionIndex),
                            })}
                            className="h-7 rounded border border-red-300/16 px-2 text-[10px] text-red-100"
                          >
                            删除
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <label className="block text-[10px] font-medium text-zinc-500">
                提示用户怎么输入
                <input
                  value={spec.description || ""}
                  onChange={(event) => onUpdateWorkflowInputSpec(input, { description: event.target.value })}
                  placeholder="例如：输入完整剧情梗概"
                  disabled={readOnly}
                  className={cn(textFieldClass, "mt-1 h-7")}
                />
              </label>
              <label className="block text-[10px] font-medium text-zinc-500">
                默认内容
                <input
                  value={spec.default || ""}
                  onChange={(event) => onUpdateWorkflowInputSpec(input, { default: event.target.value })}
                  placeholder="不需要默认值可以留空"
                  disabled={readOnly}
                  className={cn(textFieldClass, "mt-1 h-7")}
                />
              </label>
              {!readOnly && (
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={() => onDeleteWorkflowInput(input)}
                    className="h-7 rounded border border-red-300/20 px-2 text-[10px] text-red-100 transition hover:bg-red-500/12"
                  >
                    删除这一项
                  </button>
                </div>
              )}
            </div>
          )
        })}
        {inputIds.length === 0 && (
          <div className="rounded border border-white/[0.06] bg-white/[0.025] px-2 py-2 text-[11px] text-zinc-500">
            还没有要输入的内容
          </div>
        )}
      </div>
    </section>
  )
  const renderRuntimeInputNotice = (
    title = "运行输入",
    description = "本次运行值从底部流程胶囊的输入按钮填写。",
  ) => {
    const missingLabels = missingRequiredInputIds.map((input) => inputSpecs[input]?.label || workflowInputLabel(input))
    return (
      <section className="rounded-md border border-amber-200/18 bg-amber-300/[0.055] p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div>
            <div className="text-[11px] font-semibold text-amber-100">{title}</div>
            <div className="mt-0.5 text-[10px] leading-4 text-amber-100/60">{description}</div>
          </div>
          <div className="shrink-0 text-[10px] text-amber-100/65">
            {workflowInputSummary(inputIds, inputValues, requiredInputIds, inputSpecs)}
          </div>
        </div>
        {missingLabels.length > 0 ? (
          <div className="rounded border border-amber-200/16 bg-black/16 px-2 py-1.5 text-[10px] leading-4 text-amber-100/75">
            待填写：{missingLabels.join("、")}
          </div>
        ) : (
          <div className="rounded border border-white/[0.06] bg-black/16 px-2 py-1.5 text-[10px] leading-4 text-zinc-400">
            输入已准备好，可以运行这个流程。
          </div>
        )}
      </section>
    )
  }
  useEffect(() => {
    const tabAllowed = WORKFLOW_INSPECTOR_TABS.some((tab) => {
      if (tab.value !== activeTab) return false
      if (tab.value === "run" && !readOnly) return false
      return true
    })
    if (!tabAllowed) setActiveTab("properties")
  }, [activeTab, readOnly])
  if (!step) {
    return (
      <aside className="flex h-full w-[400px] shrink-0 flex-col border-l border-white/[0.08] bg-[#0d1219] shadow-[-16px_0_36px_rgba(0,0,0,0.12)]">
        <div className="border-b border-white/10 px-4 py-3">
          <div className="text-sm font-semibold text-zinc-100">流程设置</div>
          <div className="mt-1 text-[11px] text-zinc-500">
            {readOnly ? "选择步骤查看运行结果。" : "定义流程信息和运行时需要填写的字段。实际内容在画布流程控制栏填写。"}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <div className="grid gap-3">
            {!readOnly && (
              <details className="rounded-xl border border-amber-200/14 bg-amber-300/[0.035]">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-[11px] font-semibold text-amber-100/85 hover:bg-amber-300/[0.04]">
                  <span>输入字段定义</span>
                  <span className="rounded-full border border-amber-200/14 bg-black/18 px-2 py-0.5 text-[10px] font-normal text-amber-100/60">
                    {inputIds.length} 项
                  </span>
                </summary>
                <div className="border-t border-amber-200/10 p-2">
                  {renderFormFieldConfig("这里只定义运行时需要哪些字段；具体内容在画布流程控制栏填写。")}
                </div>
              </details>
            )}
            <section className="rounded-md border border-white/[0.08] bg-black/18 p-3">
              <div className="mb-2 text-[11px] font-semibold text-zinc-300">基础信息</div>
              <div className="grid gap-2">
                <label className="block text-[10px] font-medium text-zinc-500">
                  名称
                  <input
                    value={workflowName}
                    onChange={(event) => onWorkflowNameChange(event.target.value)}
                    placeholder="工作流名称"
                    disabled={readOnly}
                    className={cn(textFieldClass, "mt-1 h-8")}
                  />
                </label>
                <label className="block text-[10px] font-medium text-zinc-500">
                  说明
                  <textarea
                    value={workflowDescription}
                    onChange={(event) => onWorkflowDescriptionChange(event.target.value)}
                    placeholder="这个工作流适合什么场景，运行时需要注意什么。"
                    rows={5}
                    disabled={readOnly}
                    className={cn(textFieldClass, "mt-1 min-h-28 resize-none py-1.5 leading-4")}
                  />
                </label>
              </div>
            </section>
            <section className="rounded-md border border-white/[0.08] bg-black/18 p-3">
              <div className="mb-1 text-[11px] font-semibold text-zinc-300">流程步骤</div>
              <div className="text-[12px] leading-5 text-zinc-500">
                左侧只添加真正会处理内容的步骤，例如分段、循环、出图、出视频。
              </div>
            </section>
            <details className="rounded-md border border-white/[0.08] bg-black/18">
              <summary className="cursor-pointer px-3 py-2 text-[11px] font-semibold text-zinc-400 hover:text-zinc-200">插件扩展</summary>
              <div className="grid gap-2 border-t border-white/[0.08] p-3">
                <div className="text-[10px] leading-4 text-zinc-500">仅在工作流使用已安装插件时填写命名空间扩展配置。</div>
                <WorkflowJsonEditorField
                  label="extensions"
                  value={workflowAdvanced.extensions}
                  onChange={(value) => onWorkflowAdvancedChange("extensions", value)}
                  readOnly={readOnly}
                  rows={5}
                />
              </div>
            </details>
          </div>
        </div>
      </aside>
    )
  }

  const kind = workflowStepAuthoringKind(step)
  const isInputStep = workflowStepIsInputStep(step, inputIds)

  if (readOnly) {
    const status = nodeState?.status || "idle"
    const inputBlocked = isInputStep && missingRequiredInputIds.length > 0
    const isRunning = running || status === "running"
    const dependencyLabels = workflowDependencyLabels(step, steps)
    const runtimeRows = [
      { label: "状态", value: nodeState ? workflowStepAggregateLabel(nodeState) : "未运行" },
      { label: "运行次数", value: nodeState?.runCount != null && nodeState.runCount > 0 ? String(nodeState.runCount) : "" },
      { label: "输入", value: nodeState?.resolvedInputCount != null && nodeState.resolvedInputCount > 0 ? `${nodeState.resolvedInputCount} 项` : "" },
      { label: "输出", value: nodeState?.outputCount != null && nodeState.outputCount > 0 ? `${nodeState.outputCount} 项` : "" },
      { label: "产物", value: nodeState?.artifactCount != null && nodeState.artifactCount > 0 ? `${nodeState.artifactCount} 个` : "" },
      { label: "更新时间", value: nodeState?.updatedAt || "" },
    ].filter((item) => item.value)

    return (
      <aside className="flex h-full w-[400px] shrink-0 flex-col border-l border-white/[0.08] bg-[#0d1219] shadow-[-16px_0_36px_rgba(0,0,0,0.12)]">
        <div className="flex shrink-0 items-start gap-3 border-b border-white/10 px-4 py-3">
          <div className={cn("mt-0.5 flex h-8 w-8 items-center justify-center rounded-md border text-[11px] font-semibold", workflowStepToneClass(step))}>
            {WORKFLOW_NODE_TYPE_LABEL[step.node_type]?.slice(0, 1) || "节"}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold text-zinc-50">{step.title || step.id}</div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              <span className={cn("rounded border px-1.5 py-0.5 text-[10px]", workflowStepToneClass(step))}>
                {isInputStep ? "输入" : workflowStepKindLabel(step)}
              </span>
              <span className={cn("rounded border px-1.5 py-0.5 text-[10px]", nodeState ? workflowStepStateClass(status) : "border-white/10 text-zinc-500")}>
                {nodeState ? workflowStepAggregateLabel(nodeState) : "未运行"}
              </span>
            </div>
          </div>
          {showRunButton && (
            <button
              type="button"
              onClick={() => onRunStep(step.id)}
              disabled={isRunning || inputBlocked}
              className="h-8 rounded-md border border-cyan-200/25 bg-cyan-300/10 px-3 text-xs font-semibold text-cyan-100 transition hover:bg-cyan-300/16 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {isRunning ? "运行中" : inputBlocked ? "先输入" : "运行"}
            </button>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <div className="grid gap-3">
            {isInputStep && renderRuntimeInputNotice("输入参数")}

            <section className="rounded-md border border-white/[0.08] bg-black/18 p-3">
              <div className="mb-2 text-[11px] font-semibold text-zinc-300">运行状态</div>
              <div className="grid grid-cols-2 gap-2">
                {runtimeRows.map((item) => (
                  <div key={item.label} className="rounded-md border border-white/[0.06] bg-white/[0.03] px-2.5 py-2">
                    <div className="text-[10px] font-semibold text-zinc-500">{item.label}</div>
                    <div className="mt-1 truncate text-[12px] text-zinc-200" title={item.value}>{item.value}</div>
                  </div>
                ))}
              </div>
              {(nodeState?.lastRunSummary || nodeState?.lastRunDetail) && (
                <div className="mt-3 rounded-md border border-cyan-200/12 bg-cyan-300/[0.04] px-3 py-2">
                  {nodeState.lastRunSummary && <div className="text-[12px] leading-5 text-cyan-50/90">{nodeState.lastRunSummary}</div>}
                  {nodeState.lastRunDetail && <div className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-5 text-cyan-100/70">{nodeState.lastRunDetail}</div>}
                </div>
              )}
            </section>

            {!isInputStep && nodeState?.outputPreview && (
              <section className="overflow-hidden rounded-md border border-emerald-200/14 bg-emerald-300/[0.045]">
                <div className="border-b border-emerald-200/10 px-3 py-2 text-[11px] font-semibold text-emerald-100/80">
                  运行输出
                </div>
                <WorkflowRunOutputView value={nodeState.outputPreview} />
              </section>
            )}

            {!isInputStep && !nodeState?.outputPreview && (
              <section className="rounded-md border border-white/[0.08] bg-black/18 px-3 py-2 text-[12px] text-zinc-500">
                这个节点还没有运行输出。
              </section>
            )}

            {(dependencyLabels.length > 0 || (nodeState?.nodeIds.length || 0) > 0) && (
              <section className="rounded-md border border-white/[0.08] bg-black/18 p-3">
                <div className="mb-2 text-[11px] font-semibold text-zinc-300">产物与上游</div>
                {dependencyLabels.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-1.5">
                    {dependencyLabels.map((label) => (
                      <span key={label} className="rounded border border-white/[0.08] bg-white/[0.03] px-2 py-1 text-[10px] text-zinc-300">
                        {label}
                      </span>
                    ))}
                  </div>
                )}
                {(nodeState?.nodeIds.length || 0) > 0 && (
                  <div className="text-[11px] leading-5 text-zinc-500">
                    已生成 {nodeState?.nodeIds.length || 0} 个画布产物。
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      </aside>
    )
  }

  if (isInputStep) {
    return (
      <aside className="flex h-full w-[400px] shrink-0 flex-col border-l border-white/[0.08] bg-[#0d1219] shadow-[-16px_0_36px_rgba(0,0,0,0.12)]">
        <div className="flex shrink-0 items-start gap-3 border-b border-white/10 px-4 py-3">
          <div className={cn("mt-0.5 flex h-8 w-8 items-center justify-center rounded-md border text-[11px] font-semibold", workflowStepToneClass(step))}>
            入
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold text-zinc-50">{step.title || "输入"}</div>
            <div className="mt-1 text-[11px] leading-4 text-zinc-500">
              这个节点决定运行前要输入哪些内容。
            </div>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <div className="grid gap-3">
            <section className="rounded-md border border-white/[0.08] bg-black/18 p-3">
              <label className="block text-[10px] font-medium text-zinc-500">
                节点名称
                <input
                  value={step.title || ""}
                  onChange={(event) => onUpdateStep(step.id, { title: event.target.value })}
                  placeholder="输入"
                  className={cn(textFieldClass, "mt-1 h-8")}
                />
              </label>
            </section>
            {renderFormFieldConfig("这里配置输入项的名称、类型、提示和默认值。")}
            <button
              type="button"
              onClick={() => onDeleteStep(step.id)}
              className="h-8 rounded-md border border-red-300/20 bg-red-500/10 text-xs font-semibold text-red-100 transition hover:bg-red-500/16"
            >
              删除这个节点
            </button>
          </div>
        </div>
      </aside>
    )
  }

  const cleanStepDependencies = workflowCleanIdList(step.depends_on)
  const selectedDependencies = new Set(cleanStepDependencies)
  const dependencyCandidates = workflowDependencyCandidateSteps(step, steps)
  const dependencyCandidateIds = new Set(dependencyCandidates.map((candidate) => candidate.id))
  const outOfRangeDependencies = cleanStepDependencies.filter((dep) => !dependencyCandidateIds.has(dep))
  const scopeOptions = workflowScopeOptionsForStep(step, steps)
  const currentScopeId = workflowStringValue(step.repeat_group_id) || WORKFLOW_TEMPLATE_ROOT_SCOPE_ID
  const loopForeach = asWorkflowObject(step.foreach) || {}
  const loopSource = workflowStepRepeatSource(step)
  const loopSourceParts = workflowLoopSourceParts(loopSource)
  const runCondition = workflowParseCondition(step.when)
  const runConditionInputSpec = runCondition.inputId ? inputSpecs[runCondition.inputId] : undefined
  const runConditionInputKind = workflowInputTypeCategory(runConditionInputSpec?.type)
  const runConditionOperatorOptions = workflowConditionOperatorOptionsForInputType(runConditionInputSpec?.type)
  const runConditionSelectedOperator = workflowConditionOperatorIsAllowed(runCondition.operator, runConditionInputSpec?.type)
    ? runCondition.operator
    : ""
  const runConditionNeedsCompareValue = Boolean(
    runCondition.inputId && runConditionSelectedOperator && !["empty", "not_empty"].includes(runConditionSelectedOperator),
  )
  const runConditionLabel = workflowConditionLabel(step.when, inputSpecs)
  const loopSourceStepCandidates = workflowLoopSourceStepCandidates(step, steps)
  const loopChildScopeId = kind === "loop" ? workflowStepChildScopeId(step) || step.id : ""
  const loopDescendantIds = loopChildScopeId ? workflowDescendantStepIds(steps, step.id) : new Set<string>()
  const loopChildSteps = loopChildScopeId
    ? steps.filter((item) => workflowStringValue(item.repeat_group_id) === loopChildScopeId)
    : []
  const loopMoveCandidateSteps = loopChildScopeId
    ? steps.filter((candidate) => (
      candidate.id !== step.id &&
      !loopDescendantIds.has(candidate.id) &&
      !workflowDescendantStepIds(steps, candidate.id).has(step.id) &&
      !workflowStepIsInputStep(candidate, inputIds)
    ))
    : []
  const loopSourceSelectValue = loopSourceParts.type === "input" && inputIds.includes(loopSourceParts.source)
    ? `${"count" in loopForeach ? "count" : "input"}:${loopSourceParts.source}`
    : loopSourceParts.type === "step" && loopSourceStepCandidates.some((candidate) => candidate.id === loopSourceParts.source)
    ? `step:${loopSourceParts.source}`
    : loopSourceParts.type === "fixed"
    ? "fixed"
    : ""
  const inputBlocked = isInputStep && missingRequiredInputIds.length > 0
  const isCanvasProduct = workflowStepIsCanvasProduct(step)
  const outputSchema = asWorkflowObject(asWorkflowObject(step.output)?.schema) || {}
  const isCollectionOutput = kind === "collection"
  const outputSchemaFields = workflowOutputSchemaFields(step)
  const referenceRows = workflowReferenceRows(step)
  const referenceCandidates = dependencyCandidates.filter((candidate) => {
    const candidateKind = workflowStepAuthoringKind(candidate)
    return candidateKind !== "loop"
  })
  const productFields = workflowStepFields(step)
  const productModel = workflowStringValue(mediaModelOverrides[step.id])
  const productModelProviders = isCanvasProduct ? workflowMediaProvidersForKind(mediaProviders, kind) : []
  const selectedProductProvider = productModel ? workflowResolveMediaProvider(productModel, productModelProviders) : undefined
  const defaultProductProvider = workflowResolveMediaProvider("", productModelProviders)
  const productModelOptions = workflowMediaProviderOptions(productModelProviders, productModel, selectedProductProvider)
  const productModelSelectDisabled = readOnly || !onMediaModelOverrideChange || productModelOptions.length === 0 || productModelOptions.every((item) => item.disabled)
  const productModelHint = mediaConfigError
    ? "模型配置读取失败"
    : selectedProductProvider
    ? workflowMediaProviderLabel(selectedProductProvider)
    : productModel
    ? productModel
    : defaultProductProvider
    ? `未指定时使用当前模型：${workflowMediaProviderLabel(defaultProductProvider)}`
    : "未配置可用模型"
  const productProviderForSpecs = selectedProductProvider || defaultProductProvider
  const productAspectRatio = isCanvasProduct ? workflowProductAspectRatio(step) : "9:16"
  const productResolutionDimensions = isCanvasProduct ? workflowProductResolutionDimensions(step) : WORKFLOW_DEFAULT_MEDIA_RESOLUTION
  const productImageResolutionTier = workflowImageResolutionTierFromDimensions(productResolutionDimensions)
  const productImageResolutionValue = `${productResolutionDimensions.width}x${productResolutionDimensions.height}`
  const productImageResolutionByTier = WORKFLOW_IMAGE_RESOLUTION_TIERS.map((item) => {
    const dimensions = workflowImageResolutionForAspectTier(productAspectRatio, item.value)
    return {
      ...item,
      dimensions,
      value: `${dimensions.width}x${dimensions.height}`,
      hint: `${item.label} · ${dimensions.width}x${dimensions.height}`,
    }
  })
  const productImageSelectedResolution = productImageResolutionByTier.find((item) => item.value === productImageResolutionValue)?.value
    || `${workflowImageResolutionForAspectTier(productAspectRatio, productImageResolutionTier).width}x${workflowImageResolutionForAspectTier(productAspectRatio, productImageResolutionTier).height}`
  const productVideoMode = workflowStringValue(productFields.video_mode || productFields.mode)
  const productVideoSupportedRatios = kind === "video"
    ? videoSupportedRatiosForProvider(productProviderForSpecs, videoProtocols, productVideoMode)
    : []
  const productVideoSupportedResolutions = kind === "video"
    ? videoSupportedResolutionsForProvider(productProviderForSpecs, videoProtocols, productVideoMode)
    : []
  const productVideoResolution = kind === "video"
    ? workflowVideoResolutionValue(
      productFields.resolution,
      defaultVideoResolutionForProvider(productProviderForSpecs, videoProtocols, productVideoMode),
    )
    : ""
  const productVideoAspectOptions = kind === "video"
    ? [
      ...(productAspectRatio && !productVideoSupportedRatios.includes(productAspectRatio)
        ? [{ label: `当前 ${productAspectRatio}`, value: productAspectRatio, disabled: true }]
        : []),
      ...productVideoSupportedRatios.map((value) => ({ label: value, value })),
    ]
    : []
  const productVideoResolutionOptions = kind === "video"
    ? [
      ...(productVideoResolution && !productVideoSupportedResolutions.includes(productVideoResolution)
        ? [{ label: `当前 ${workflowMediaResolutionLabel(productVideoResolution)}`, value: productVideoResolution, disabled: true }]
        : []),
      ...productVideoSupportedResolutions.map((value) => ({ label: workflowMediaResolutionLabel(value), value })),
    ]
    : []
  const productVideoResolutionHint = productProviderForSpecs
    ? productVideoSupportedResolutions.length > 0
      ? `来自当前视频模型：${workflowMediaProviderLabel(productProviderForSpecs)}`
      : "当前视频模型未声明支持清晰度"
    : mediaConfigError
    ? "视频模型配置读取失败"
    : "未配置可用视频模型"
  const productDurationSeconds = workflowPositiveIntegerValue(productFields.duration_seconds)
  const pluginDefinition = workflowPluginDefinitionForStep(step, nodeTypes)
  const pluginConfig = asWorkflowObject(step.plugin) || {}
  const pluginInputs = asWorkflowObject(pluginConfig.inputs) || {}
  const pluginSettings = asWorkflowObject(pluginConfig.settings) || {}
  const pluginInputSpecs = Array.isArray(pluginDefinition?.inputs)
    ? pluginDefinition.inputs.map((item) => asWorkflowObject(item)).filter((item): item is Record<string, unknown> => Boolean(item))
    : []
  const pluginSettingSpecs = Array.isArray(pluginDefinition?.settings)
    ? pluginDefinition.settings.map((item) => asWorkflowObject(item)).filter((item): item is Record<string, unknown> => Boolean(item))
    : []
  const promptObject = asWorkflowObject(step.prompt) || {}
  const promptValue = workflowStringValue(promptObject.task)
  const templateTabs = WORKFLOW_INSPECTOR_TABS.filter((tab) => tab.value !== "run")
  const appendPromptText = (text: string) => {
    const value = text.trim()
    if (!value || readOnly) return
    const separator = promptValue.trim() ? "\n\n" : ""
    onUpdateStep(step.id, { prompt: { ...promptObject, task: `${promptValue}${separator}${value}` } })
  }
  const renderPluginFieldInput = (
    field: Record<string, unknown>,
    value: unknown,
    onChange: (value: unknown) => void,
  ) => {
    const fieldType = workflowDefinitionFieldType(field)
    const options = workflowDefinitionFieldOptions(field)
    if (options.length > 0) {
      return (
        <select
          value={workflowJsonScalar(value)}
          disabled={readOnly}
          onChange={(event) => onChange(workflowValueFromFieldInput(event.target.value, fieldType))}
          className={cn(textFieldClass, "h-8")}
        >
          <option value="">未设置</option>
          {options.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      )
    }
    if (fieldType === "boolean" || fieldType === "checkbox") {
      return (
        <select
          value={typeof value === "boolean" ? String(value) : workflowStringValue(value).toLowerCase()}
          disabled={readOnly}
          onChange={(event) => onChange(workflowValueFromFieldInput(event.target.value, "boolean"))}
          className={cn(textFieldClass, "h-8")}
        >
          <option value="">未设置</option>
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      )
    }
    if (fieldType === "object" || fieldType === "array" || fieldType === "json") {
      return (
        <textarea
          value={workflowJsonScalar(value)}
          disabled={readOnly}
          rows={3}
          onChange={(event) => onChange(workflowValueFromFieldInput(event.target.value, fieldType))}
          className={cn(textFieldClass, "min-h-20 resize-none py-1.5 leading-4")}
        />
      )
    }
    return (
      <input
        type={fieldType === "number" || fieldType === "integer" ? "number" : "text"}
        value={workflowJsonScalar(value)}
        disabled={readOnly}
        onChange={(event) => onChange(workflowValueFromFieldInput(event.target.value, fieldType))}
        className={cn(textFieldClass, "h-8")}
      />
    )
  }

  return (
    <aside className="flex h-full w-[400px] shrink-0 flex-col border-l border-white/[0.08] bg-[#0d1219] shadow-[-16px_0_36px_rgba(0,0,0,0.12)]">
      <div className="flex shrink-0 items-start gap-3 border-b border-white/[0.08] bg-gradient-to-b from-white/[0.025] to-transparent px-4 py-3.5">
        <div className={cn("mt-0.5 flex h-10 w-10 items-center justify-center rounded-xl border text-[12px] font-semibold shadow-lg shadow-black/20", workflowStepToneClass(step))}>
          {WORKFLOW_NODE_TYPE_LABEL[step.node_type]?.slice(0, 1) || "节"}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold tracking-[0.01em] text-zinc-50">{step.title || step.id}</div>
          <div className="mt-0.5 truncate font-mono text-[9px] tracking-wide text-zinc-600" title={step.id}>{step.id}</div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <span className={cn("rounded border px-1.5 py-0.5 text-[10px]", workflowStepToneClass(step))}>
              {WORKFLOW_AUTHORING_KIND_OPTIONS.find((item) => item.value === kind)?.label || kind}
            </span>
            {readOnly && (
              <span className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-500">
                只读实例
              </span>
            )}
          </div>
        </div>
        {showRunButton && (
          <button
            type="button"
            onClick={() => onRunStep(step.id)}
            disabled={running || inputBlocked}
            className="h-8 rounded-md border border-cyan-200/25 bg-cyan-300/10 px-3 text-xs font-semibold text-cyan-100 transition hover:bg-cyan-300/16 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {running ? "运行中" : inputBlocked ? "先输入" : "运行"}
          </button>
        )}
      </div>
      <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-white/[0.08] bg-black/16 px-3 py-1.5">
        {templateTabs.map((tab) => (
          <button
            key={tab.value}
            type="button"
            onClick={() => setActiveTab(tab.value)}
            className={cn(
              "h-8 shrink-0 border-b-2 px-2.5 text-[11px] font-medium transition-colors",
              activeTab === tab.value
                ? "border-cyan-300 text-cyan-50"
                : "border-transparent text-zinc-500 hover:border-white/15 hover:text-zinc-200",
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <div className="grid gap-3">
          {activeTab === "properties" && (
            <section className="grid grid-cols-3 gap-2">
              {[
                {
                  label: "步骤角色",
                  value: workflowStepIsCanvasProduct(step) ? "画布产物" : kind === "loop" ? "流程控制" : "处理步骤",
                },
                {
                  label: "所在范围",
                  value: currentScopeId === WORKFLOW_TEMPLATE_ROOT_SCOPE_ID ? "主流程" : workflowTemplateScopeTitle(currentScopeId, steps, undefined),
                },
                {
                  label: "上游依赖",
                  value: cleanStepDependencies.length > 0 ? `${cleanStepDependencies.length} 个步骤` : "流程起点",
                },
              ].map((item) => (
                <div key={item.label} className="rounded-lg border border-white/[0.07] bg-black/18 px-2.5 py-2">
                  <div className="text-[9px] font-semibold tracking-wide text-zinc-600">{item.label}</div>
                  <div className="mt-1 truncate text-[11px] font-medium text-zinc-200" title={item.value}>{item.value}</div>
                </div>
              ))}
            </section>
          )}
          {activeTab === "properties" && (
          <section className="rounded-xl border border-white/[0.08] bg-black/18 p-3.5">
            <div className="mb-2 text-[11px] font-semibold text-zinc-300">这一步做什么</div>
            <div className="grid gap-2">
              <label className="block text-[10px] font-medium text-zinc-500">
                步骤名称
                <input
                  value={step.title || ""}
                  onChange={(event) => onUpdateStep(step.id, { title: event.target.value })}
                  disabled={readOnly}
                  className={cn(textFieldClass, "mt-1 h-8")}
                />
              </label>
              <label className="block text-[10px] font-medium text-zinc-500">
                步骤类型
                <select
                    value={kind}
                    disabled={readOnly}
                    onChange={(event) => {
                      const nextKind = event.target.value as WorkflowAuthoringKind
                      const nextIsProduct = nextKind === "image" || nextKind === "video" || nextKind === "audio"
                      const nextNodeType = nextKind === "image" || nextKind === "video" || nextKind === "audio" ? nextKind : "text"
                      const currentOutput = asWorkflowObject(step.output) || {}
                      const currentSchema = asWorkflowObject(currentOutput.schema) || {}
                      const structured = nextKind === "collection" || nextKind === "object"
                      const nextSchema = structured
                        ? { ...currentSchema, fields: Array.isArray(currentSchema.fields) ? currentSchema.fields : [] }
                        : undefined
                      const nextOutput = nextKind === "text" && currentOutput.canvas === true
                        ? { canvas: true }
                        : nextSchema
                        ? { schema: nextSchema }
                        : undefined
                      const nextFields = nextIsProduct
                        ? workflowDefaultCanvasProductFields(nextKind, step)
                        : step.fields
                      onUpdateStep(step.id, {
                        kind: nextKind,
                        node_type: nextNodeType,
                        output: nextOutput,
                        runner: nextIsProduct ? "workflow_canvas_output" : nextKind === "plugin" ? "workflow_plugin" : "node.run",
                        fields: nextFields,
                        collection: undefined,
                      })
                    }}
                  className={cn(textFieldClass, "mt-1 h-8")}
                >
                  {WORKFLOW_AUTHORING_KIND_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </select>
              </label>
              {kind === "text" && (
                <label className="flex items-center gap-2 rounded border border-cyan-200/12 bg-cyan-300/[0.04] px-2 py-2 text-[11px] text-cyan-50/80">
                  <input
                    type="checkbox"
                    checked={asWorkflowObject(step.output)?.canvas === true}
                    disabled={readOnly}
                    onChange={(event) => onUpdateStep(step.id, {
                      output: event.target.checked ? { canvas: true } : undefined,
                    })}
                  />
                  在画布上显示文本产物
                </label>
              )}
              <label className="block text-[10px] font-medium text-zinc-500">
                是否放进循环
                <select
                  value={currentScopeId}
                  disabled={readOnly}
                  onChange={(event) => onMoveStepScope(step.id, event.target.value)}
                  className={cn(textFieldClass, "mt-1 h-8")}
                >
                  {scopeOptions.map((option) => (
                    <option key={option.id} value={option.id}>{option.title}</option>
                  ))}
                </select>
              </label>
              <label className="block text-[10px] font-medium text-zinc-500">
                说明这一步
                <textarea
                  value={step.description || ""}
                  onChange={(event) => onUpdateStep(step.id, { description: event.target.value })}
                  rows={2}
                  disabled={readOnly}
                  className={cn(textFieldClass, "mt-1 min-h-16 resize-none py-1.5 leading-4")}
                />
              </label>
              {kind === "loop" && (
                <div className="grid gap-2 rounded-md border border-violet-200/12 bg-violet-300/[0.035] p-2">
                  <label className="block text-[10px] font-medium text-zinc-500">
                    要重复处理哪一组内容
                    <select
                      value={loopSourceSelectValue}
                      onChange={(event) => {
                        const selected = event.target.value
                        const base = {
                          as: workflowStringValue(loopForeach.as) || "item",
                          ...(workflowStringValue(loopForeach.key) ? { key: workflowStringValue(loopForeach.key) } : {}),
                        }
                        const nextForeach = selected.startsWith("input:")
                          ? { ...base, items: `inputs.${selected.slice("input:".length)}` }
                          : selected.startsWith("step:")
                          ? { ...base, items: `steps.${selected.slice("step:".length)}.output[]` }
                          : selected.startsWith("count:")
                          ? { ...base, count: `inputs.${selected.slice("count:".length)}` }
                          : selected === "fixed"
                          ? { ...base, count: 1 }
                          : base
                        onUpdateStep(step.id, {
                          foreach: nextForeach,
                        })
                      }}
                      disabled={readOnly}
                      className={cn(textFieldClass, "mt-1 h-8")}
                    >
                      <option value="">选择分段列表或上一步列表</option>
                      {inputIds.filter((input) => workflowStringValue(inputSpecs[input]?.type).toLowerCase() === "json").map((input) => (
                        <option key={`input:${input}`} value={`input:${input}`}>列表输入 · {workflowInputDisplayName(input, inputSpecs)}</option>
                      ))}
                      {inputIds.filter((input) => workflowInputTypeCategory(inputSpecs[input]?.type) === "number").map((input) => (
                        <option key={`count:${input}`} value={`count:${input}`}>循环次数 · {workflowInputDisplayName(input, inputSpecs)}</option>
                      ))}
                      {loopSourceStepCandidates.map((candidate) => (
                        <option key={`step:${candidate.id}`} value={`step:${candidate.id}`}>上一步 · {candidate.title || candidate.id}</option>
                      ))}
                      <option value="fixed">固定次数</option>
                    </select>
                  </label>
                  {loopSourceSelectValue.startsWith("step:") && (
                    <label className="block text-[10px] font-medium text-zinc-500">
                      列表所在字段
                      <input
                        value={loopSourceParts.path}
                        onChange={(event) => {
                          const source = loopSourceParts.source
                          const path = event.target.value.trim()
                          onUpdateStep(step.id, {
                            foreach: {
                              ...loopForeach,
                              items: `steps.${source}.output${path ? `.${path}` : ""}[]`,
                            },
                          })
                        }}
                        placeholder="例如：segments"
                        disabled={readOnly}
                        className={cn(textFieldClass, "mt-1 h-8")}
                      />
                    </label>
                  )}
                  {loopSourceSelectValue === "fixed" && (
                    <label className="block text-[10px] font-medium text-zinc-500">
                      循环次数
                      <input
                        type="number"
                        min={1}
                        step={1}
                        value={typeof loopForeach.count === "number" ? loopForeach.count : 1}
                        onChange={(event) => onUpdateStep(step.id, {
                          foreach: {
                            ...loopForeach,
                            count: Math.max(1, Number.parseInt(event.target.value || "1", 10)),
                          },
                        })}
                        disabled={readOnly}
                        className={cn(textFieldClass, "mt-1 h-8")}
                      />
                    </label>
                  )}
                  <div className="grid grid-cols-2 gap-2">
                    <label className="block text-[10px] font-medium text-zinc-500">
                      循环项变量
                      <input
                        value={workflowStringValue(loopForeach.as) || "item"}
                        onChange={(event) => onUpdateStep(step.id, {
                          foreach: { ...loopForeach, as: workflowSanitizeStepId(event.target.value, "item") },
                        })}
                        placeholder="item"
                        disabled={readOnly}
                        className={cn(textFieldClass, "mt-1 h-8 font-mono")}
                      />
                    </label>
                    <label className="block text-[10px] font-medium text-zinc-500">
                      唯一键字段（可选）
                      <input
                        value={workflowStringValue(loopForeach.key)}
                        onChange={(event) => {
                          const next = { ...loopForeach }
                          const value = event.target.value.trim()
                          if (value) next.key = value
                          else delete next.key
                          onUpdateStep(step.id, { foreach: next })
                        }}
                        placeholder="例如 id"
                        disabled={readOnly}
                        className={cn(textFieldClass, "mt-1 h-8 font-mono")}
                      />
                    </label>
                  </div>
                </div>
              )}
            </div>
          </section>
          )}

          {activeTab === "properties" && isInputStep && renderFormFieldConfig("这个输入节点运行时会显示这些字段。")}

          {activeTab === "properties" && kind === "loop" && (
            <section className="rounded-md border border-violet-200/12 bg-violet-300/[0.035] p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-[11px] font-semibold text-violet-100/85">循环内容</div>
                <div className="text-[10px] text-violet-100/55">{loopChildSteps.length} 个步骤</div>
              </div>
              <div className="grid gap-2">
                {loopChildSteps.map((child) => (
                  <div key={child.id} className="flex items-center justify-between gap-2 rounded border border-white/[0.07] bg-black/18 px-2 py-1.5">
                    <div className="min-w-0">
                      <div className="truncate text-[11px] font-semibold text-zinc-200">{child.title || child.id}</div>
                      <div className="text-[9px] text-zinc-500">{workflowStepKindLabel(child)}</div>
                    </div>
                    <button
                      type="button"
                      disabled={readOnly}
                      onClick={() => onMoveStepScope(child.id, WORKFLOW_TEMPLATE_ROOT_SCOPE_ID)}
                      className="h-6 shrink-0 rounded border border-white/10 px-2 text-[10px] text-zinc-300 transition hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      移出
                    </button>
                  </div>
                ))}
                {loopChildSteps.length === 0 && (
                  <div className="rounded border border-white/[0.06] bg-black/14 px-2 py-2 text-[11px] text-zinc-500">
                    暂无重复执行的步骤
                  </div>
                )}
                {loopMoveCandidateSteps.length > 0 && (
                  <div className="grid gap-1.5 border-t border-white/[0.06] pt-2">
                    <div className="text-[10px] font-semibold text-violet-100/65">加入循环</div>
                    {loopMoveCandidateSteps.slice(0, 6).map((candidate) => (
                      <button
                        key={candidate.id}
                        type="button"
                        disabled={readOnly}
                        onClick={() => onMoveStepScope(candidate.id, loopChildScopeId)}
                        className="flex min-h-7 items-center justify-between gap-2 rounded border border-white/[0.07] bg-black/14 px-2 text-left text-[11px] text-zinc-300 transition hover:border-violet-200/28 hover:bg-violet-300/[0.06] disabled:cursor-not-allowed disabled:opacity-45"
                      >
                        <span className="min-w-0 truncate">{candidate.title || candidate.id}</span>
                        <span className="shrink-0 text-[9px] text-violet-100/60">加入</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </section>
          )}

          {activeTab === "properties" && (kind === "image" || kind === "video" || kind === "audio") && (
            <section className="rounded-md border border-cyan-200/12 bg-cyan-300/[0.04] p-3">
              <div className="mb-2">
                <div className="text-[11px] font-semibold text-cyan-100/80">节点属性</div>
                <div className="mt-1 text-[10px] leading-4 text-cyan-100/45">
                  生成参数会保存到模板；模型选择仅用于本次运行，不写入可复用模板。
                </div>
              </div>
              <div className="grid gap-3">
                {(kind === "image" || kind === "video" || kind === "audio") && (
                  <label className="block text-[10px] font-medium text-zinc-500">
                    本次运行模型
                    <select
                      value={productModel ? selectedProductProvider?.name || productModel : ""}
                      disabled={productModelSelectDisabled}
                      onChange={(event) => onMediaModelOverrideChange?.(step.id, event.target.value)}
                      className={cn(textFieldClass, "mt-1 h-8")}
                    >
                      <option value="">未指定</option>
                      {productModelOptions.map((item) => (
                        <option key={item.value || item.label} value={item.value} disabled={item.disabled}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                    <span className="mt-1 block truncate text-[10px] text-zinc-600" title={productModelHint}>
                      {productModelHint}
                    </span>
                  </label>
                )}
                {kind === "image" && (
                  <WorkflowMediaOptionGrid
                    label="画质"
                    value={workflowStringValue(productFields.quality) || "high"}
                    disabled={readOnly}
                    columns="grid-cols-3"
                    options={[
                      { label: "低画质", value: "low" },
                      { label: "标准画质", value: "medium" },
                      { label: "高画质", value: "high" },
                    ]}
                    onChange={(quality) => onUpdateStep(step.id, {
                      fields: workflowPatchStepFields(step, { quality }),
                    })}
                  />
                )}
                {kind === "image" && (
                  <WorkflowMediaOptionGrid
                    label="清晰度"
                    value={productImageSelectedResolution}
                    disabled={readOnly}
                    columns="grid-cols-3"
                    options={productImageResolutionByTier.map((item) => ({
                      label: item.label,
                      value: item.value,
                      hint: item.hint,
                    }))}
                    hint={`保存值：${productResolutionDimensions.width}x${productResolutionDimensions.height}`}
                    onChange={(resolution) => {
                      const dimensions = workflowDimensionPairFromText(resolution) || WORKFLOW_DEFAULT_MEDIA_RESOLUTION
                      onUpdateStep(step.id, {
                        fields: workflowPatchProductResolutionFields(step, dimensions),
                      })
                    }}
                  />
                )}
                {kind === "image" && (
                  <WorkflowMediaOptionGrid
                    label="比例"
                    value={productAspectRatio}
                    disabled={readOnly}
                    columns="grid-cols-3"
                    options={WORKFLOW_IMAGE_ASPECT_OPTIONS.map((value) => ({ label: value, value }))}
                    onChange={(aspectRatio) => {
                      const tier = workflowImageResolutionTierFromDimensions(productResolutionDimensions)
                      const dimensions = workflowImageResolutionForAspectTier(aspectRatio, tier)
                      onUpdateStep(step.id, {
                        fields: workflowPatchProductImageAspectAndResolutionFields(step, aspectRatio, dimensions),
                      })
                    }}
                  />
                )}
                {kind === "video" && (
                  <WorkflowMediaOptionGrid
                    label="比例"
                    value={productAspectRatio}
                    disabled={readOnly}
                    columns="grid-cols-3"
                    options={productVideoAspectOptions}
                    onChange={(aspectRatio) => onUpdateStep(step.id, {
                      fields: workflowPatchProductAspectRatioFields(step, aspectRatio),
                    })}
                  />
                )}
                {kind === "video" && (
                  <WorkflowMediaOptionGrid
                    label="清晰度"
                    value={productVideoResolution}
                    disabled={readOnly}
                    columns="grid-cols-4"
                    options={productVideoResolutionOptions}
                    hint={productVideoResolutionHint}
                    onChange={(resolution) => onUpdateStep(step.id, {
                      fields: workflowPatchProductVideoResolutionFields(step, resolution),
                    })}
                  />
                )}
                {kind === "image" && (
                  <div className="rounded-md border border-cyan-200/10 bg-black/16 px-2 py-1.5 text-[10px] text-cyan-100/45">
                    当前规格：{productAspectRatio} · {productResolutionDimensions.width}x{productResolutionDimensions.height}
                  </div>
                )}
                {(kind === "video" || kind === "audio") && (
                  <label className="block text-[10px] font-medium text-zinc-500">
                    时长（秒）
                    <input
                      type="number"
                      inputMode="numeric"
                      min={1}
                      step={1}
                      value={productDurationSeconds || ""}
                      disabled={readOnly}
                      onKeyDown={workflowPreventInvalidPositiveIntegerKey}
                      onChange={(event) => {
                        const seconds = workflowPositiveIntegerValue(event.target.value)
                        onUpdateStep(step.id, {
                          fields: workflowPatchStepFields(step, { duration_seconds: seconds }),
                        })
                      }}
                      placeholder="例如：15"
                      className={cn(textFieldClass, "mt-1 h-8")}
                    />
                  </label>
                )}
              </div>
            </section>
          )}

          {activeTab === "properties" && (
            <section className="rounded-md border border-white/[0.08] bg-black/18 p-3">
              <div className="mb-2 text-[11px] font-semibold text-zinc-300">运行方式</div>
              <div className="grid gap-3">
                <div className="grid grid-cols-2 gap-2">
                  <label className="flex items-center gap-2 rounded border border-white/[0.06] bg-white/[0.025] px-2 py-2 text-[11px] text-zinc-300">
                    <input
                      type="checkbox"
                      checked={step.on_error === "continue"}
                      disabled={readOnly}
                      onChange={(event) => onUpdateStep(step.id, { on_error: event.target.checked ? "continue" : "stop" })}
                    />
                    失败后继续
                  </label>
                  <label className="flex items-center gap-2 rounded border border-white/[0.06] bg-white/[0.025] px-2 py-2 text-[11px] text-zinc-300">
                    <input
                      type="checkbox"
                      checked={step.execution === "manual"}
                      disabled={readOnly}
                      onChange={(event) => onUpdateStep(step.id, { execution: event.target.checked ? "manual" : "auto" })}
                    />
                    手动运行
                  </label>
                </div>
                <div className="grid gap-2 rounded-md border border-amber-200/12 bg-amber-300/[0.035] p-2">
                  <div>
                    <div className="text-[10px] font-semibold text-amber-100/75">运行条件</div>
                    <div className="mt-0.5 text-[10px] leading-4 text-amber-100/50">
                      设置后，只有流程输入满足条件时才运行；不设置则始终运行。
                    </div>
                  </div>
                  <div className="grid grid-cols-[minmax(0,1fr)_112px] gap-2">
                    <label className="block text-[10px] font-medium text-zinc-500">
                      看哪个输入
                      <select
                        value={runCondition.inputId}
                        disabled={readOnly}
                        onChange={(event) => {
                          const nextInput = event.target.value
                          const nextSpec = inputSpecs[nextInput]
                          const currentOperator = runConditionSelectedOperator || workflowDefaultConditionOperatorForInputType(nextSpec?.type)
                          const nextOperator = workflowConditionOperatorIsAllowed(currentOperator, nextSpec?.type)
                            ? currentOperator
                            : workflowDefaultConditionOperatorForInputType(nextSpec?.type)
                          const nextValue = nextOperator && !["empty", "not_empty"].includes(nextOperator)
                            ? workflowDefaultConditionCompareValueForInputType(nextSpec?.type, runCondition.value)
                            : runCondition.value
                          const nextCondition = workflowFormatCondition(
                            nextInput,
                            nextOperator,
                            nextValue,
                            nextSpec?.type,
                          )
                          onUpdateStep(step.id, { when: nextCondition })
                        }}
                        className={cn(textFieldClass, "mt-1 h-8")}
                      >
                        <option value="">不设置</option>
                        {inputIds.map((input) => (
                          <option key={input} value={input}>{workflowInputDisplayName(input, inputSpecs)}</option>
                        ))}
                      </select>
                    </label>
                    <label className="block text-[10px] font-medium text-zinc-500">
                      怎么判断
                      <select
                        value={runConditionSelectedOperator}
                        disabled={readOnly || !runCondition.inputId}
                        onChange={(event) => {
                          const operator = event.target.value
                          const nextValue = operator && !["empty", "not_empty"].includes(operator)
                            ? workflowDefaultConditionCompareValueForInputType(runConditionInputSpec?.type, runCondition.value)
                            : runCondition.value
                          const nextCondition = workflowFormatCondition(
                            runCondition.inputId,
                            operator,
                            nextValue,
                            runConditionInputSpec?.type,
                          )
                          onUpdateStep(step.id, { when: nextCondition })
                        }}
                        className={cn(textFieldClass, "mt-1 h-8")}
                      >
                        {runConditionOperatorOptions.map((item) => (
                          <option key={item.value || "none"} value={item.value}>{item.label}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  {runConditionNeedsCompareValue && (
                    <label className="block text-[10px] font-medium text-zinc-500">
                      和什么值比较
                      {runConditionInputKind === "boolean" ? (
                        <select
                          value={runCondition.value.toLowerCase() === "true" ? "true" : "false"}
                          disabled={readOnly}
                          onChange={(event) => {
                            const nextCondition = workflowFormatCondition(
                              runCondition.inputId,
                              runConditionSelectedOperator,
                              event.target.value,
                              runConditionInputSpec?.type,
                            )
                            onUpdateStep(step.id, { when: nextCondition })
                          }}
                          className={cn(textFieldClass, "mt-1 h-8")}
                        >
                          <option value="true">是</option>
                          <option value="false">否</option>
                        </select>
                      ) : (
                        <input
                          type={runConditionInputKind === "number" ? "number" : "text"}
                          value={runCondition.value}
                          disabled={readOnly}
                          onChange={(event) => {
                            const nextCondition = workflowFormatCondition(
                              runCondition.inputId,
                              runConditionSelectedOperator,
                              event.target.value,
                              runConditionInputSpec?.type,
                            )
                            onUpdateStep(step.id, { when: nextCondition })
                          }}
                          className={cn(textFieldClass, "mt-1 h-8")}
                        />
                      )}
                    </label>
                  )}
                  {runConditionLabel && (
                    <div className="rounded border border-amber-200/10 bg-black/16 px-2 py-1.5 text-[11px] leading-4 text-amber-100/75">
                      {runConditionLabel}
                    </div>
                  )}
                </div>
              </div>
            </section>
          )}

          {activeTab === "io" && isInputStep && !readOnly && renderFormFieldConfig("配置这个输入节点要让用户输入哪些内容。")}

          {activeTab === "io" && isInputStep && readOnly && inputIds.length > 0 && (
            <section className="rounded-md border border-amber-200/18 bg-amber-300/[0.055] p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-[11px] font-semibold text-amber-100">输入参数</div>
                <div className="text-[10px] text-amber-100/65">{workflowInputSummary(inputIds, inputValues, requiredInputIds, inputSpecs)}</div>
              </div>
              <div className="grid gap-2">
                {inputIds.map((input) => {
                  const spec = inputSpecs[input] || { type: "text" }
                  const longInput = workflowIsLongInput(input, spec)
                  const value = workflowInputValueForId(input, inputValues, inputSpecs)
                  return (
                    <label key={input} className="block text-[10px] font-medium text-zinc-400">
                      <span className="mb-1 flex items-center gap-1">
                        {spec.label || workflowInputLabel(input)}
                        {requiredSet.has(input) && <span className="text-amber-200/85">必填</span>}
                      </span>
                      {longInput ? (
                        <textarea
                          value={value}
                          onChange={(event) => onInputValueChange(input, event.target.value)}
                          placeholder={spec.description || workflowInputPlaceholder(input)}
                          rows={3}
                          className={cn(textFieldClass, "min-h-20 resize-none py-1.5 leading-4 focus:border-amber-200/55")}
                        />
                      ) : (
                        <input
                          type={String(spec.type || "").toLowerCase() === "number" || String(spec.type || "").toLowerCase() === "integer" ? "number" : "text"}
                          value={value}
                          onChange={(event) => onInputValueChange(input, event.target.value)}
                          placeholder={spec.description || workflowInputPlaceholder(input)}
                          className={cn(textFieldClass, "h-8 focus:border-amber-200/55")}
                        />
                      )}
                    </label>
                  )
                })}
              </div>
            </section>
          )}

          {activeTab === "io" && (
          <section className="rounded-md border border-white/[0.08] bg-black/18 p-3">
              <div className="mb-2 text-[11px] font-semibold text-zinc-300">先读取哪一步</div>
            <div className="grid max-h-36 gap-1.5 overflow-y-auto pr-1">
              {dependencyCandidates.map((candidate) => (
                <label key={candidate.id} className="flex items-center gap-2 rounded border border-white/[0.06] bg-white/[0.025] px-2 py-1.5 text-[11px] text-zinc-300">
                  <input
                    type="checkbox"
                    checked={selectedDependencies.has(candidate.id)}
                    disabled={readOnly}
                    onChange={(event) => {
                      const next = new Set(selectedDependencies)
                      if (event.target.checked) next.add(candidate.id)
                      else next.delete(candidate.id)
                      onUpdateStep(step.id, { depends_on: workflowCleanIdList(Array.from(next)) })
                    }}
                  />
                  <span className="min-w-0 truncate">{candidate.title || candidate.id}</span>
                </label>
              ))}
              {outOfRangeDependencies.map((dep) => (
                <div key={dep} className="flex items-center justify-between gap-2 rounded border border-amber-200/14 bg-amber-300/[0.045] px-2 py-1.5 text-[11px] text-amber-100/80">
                  <span className="min-w-0 truncate">{workflowStepTitleById(steps, dep)}</span>
                  <button
                    type="button"
                    onClick={() => onUpdateStep(step.id, { depends_on: cleanStepDependencies.filter((item) => item !== dep) })}
                    className="shrink-0 rounded border border-amber-200/20 px-1.5 py-0.5 text-[10px] transition hover:bg-amber-300/10"
                  >
                    移除
                  </button>
                </div>
              ))}
              {dependencyCandidates.length === 0 && outOfRangeDependencies.length === 0 && <div className="text-[11px] text-zinc-500">前面还没有可读取的步骤</div>}
            </div>
          </section>
          )}

          {activeTab === "io" && (kind === "object" || kind === "collection") && (
            <section className="rounded-md border border-emerald-200/12 bg-emerald-300/[0.035] p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-[11px] font-semibold text-emerald-100/80">{isCollectionOutput ? "列表字段" : "输出格式"}</div>
                {!readOnly && (
                  <button
                    type="button"
                    onClick={() => onUpdateStep(step.id, {
                      output: { schema: workflowAddOutputSchemaField(step) },
                    })}
                    className="h-6 rounded border border-emerald-200/20 bg-emerald-300/10 px-2 text-[10px] font-semibold text-emerald-100 transition hover:bg-emerald-300/16"
                  >
                    {isCollectionOutput ? "新增列" : "新增字段"}
                  </button>
                )}
              </div>
              <div className="grid gap-2">
                <div className="rounded border border-emerald-200/10 bg-black/16 px-2 py-2 text-[11px] leading-4 text-emerald-50/75">
                  {isCollectionOutput
                    ? "每一项包含下面这些字段。结构化输出要求由运行器自动加入，不需要在提示词里写 JSON。"
                    : "对象包含下面这些字段。结构化输出要求由运行器自动加入，不需要在提示词里写 JSON。"}
                </div>
                <div className="grid gap-2">
                  {outputSchemaFields.map((field, fieldIndex) => (
                    <div key={`${workflowStringValue(field.id || field.key || field.name) || "field"}-${fieldIndex}`} className="grid gap-2 rounded border border-white/[0.06] bg-white/[0.025] p-2">
                      <div className="grid grid-cols-[minmax(0,1fr)_92px_auto] items-center gap-2">
                        <input
                          value={workflowStringValue(field.id || field.key || field.name)}
                          disabled={readOnly}
                          onChange={(event) => {
                            const id = workflowUniqueFieldId(event.target.value, outputSchemaFields, fieldIndex)
                            onUpdateStep(step.id, { output: { schema: workflowPatchOutputSchemaField(step, fieldIndex, { id }) } })
                          }}
                          placeholder={isCollectionOutput ? "字段标识" : "字段名"}
                          className={cn(textFieldClass, "h-7 font-mono")}
                        />
                        <select
                          value={workflowStringValue(field.type) || "string"}
                          disabled={readOnly}
                          onChange={(event) => onUpdateStep(step.id, {
                            output: { schema: workflowPatchOutputSchemaField(step, fieldIndex, { type: event.target.value }) },
                          })}
                          className={cn(textFieldClass, "h-7")}
                        >
                          {WORKFLOW_FIELD_TYPE_OPTIONS.map((item) => (
                            <option key={item.value} value={item.value}>{item.label}</option>
                          ))}
                        </select>
                        <label className="flex items-center gap-1 text-[10px] text-zinc-400">
                          <input
                            type="checkbox"
                            checked={field.required === true}
                            disabled={readOnly}
                            onChange={(event) => onUpdateStep(step.id, {
                              output: { schema: workflowPatchOutputSchemaField(step, fieldIndex, { required: event.target.checked }) },
                            })}
                          />
                          必填
                        </label>
                      </div>
                      <input
                        value={workflowStringValue(field.label || field.title)}
                        disabled={readOnly}
                        onChange={(event) => onUpdateStep(step.id, {
                          output: { schema: workflowPatchOutputSchemaField(step, fieldIndex, { label: event.target.value }) },
                        })}
                        placeholder={isCollectionOutput ? "给用户看的列名" : "显示名"}
                        className={cn(textFieldClass, "h-7")}
                      />
                      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
                        <input
                          value={workflowStringValue(field.description)}
                          disabled={readOnly}
                          onChange={(event) => onUpdateStep(step.id, {
                            output: { schema: workflowPatchOutputSchemaField(step, fieldIndex, { description: event.target.value }) },
                          })}
                          placeholder="说明"
                          className={cn(textFieldClass, "h-7")}
                        />
                        {!readOnly && (
                          <button
                            type="button"
                            onClick={() => onUpdateStep(step.id, { output: { schema: workflowRemoveOutputSchemaField(step, fieldIndex) } })}
                            className="h-7 rounded border border-red-300/20 px-2 text-[10px] text-red-100 transition hover:bg-red-500/12"
                          >
                            删除
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                  {outputSchemaFields.length === 0 && (
                  <div className="rounded border border-white/[0.06] bg-white/[0.025] px-2 py-2 text-[11px] text-zinc-500">
                      {isCollectionOutput ? "先添加至少一列，例如：名称、说明、数量、时间等。" : "不需要固定字段时可以留空"}
                    </div>
                  )}
                </div>
              </div>
            </section>
          )}

          {activeTab === "io" && !isInputStep && kind !== "loop" && (
            <section className="rounded-md border border-cyan-200/12 bg-cyan-300/[0.035] p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-[11px] font-semibold text-cyan-100/80">把上游产物作为参考</div>
                {!readOnly && (
                  <button
                    type="button"
                    onClick={() => onUpdateStep(step.id, { uses: workflowAddReferenceRow(step, referenceCandidates) })}
                    disabled={referenceCandidates.length === 0}
                    className="h-6 rounded border border-cyan-200/20 bg-cyan-300/10 px-2 text-[10px] font-semibold text-cyan-100 transition hover:bg-cyan-300/16 disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    新增引用
                  </button>
                )}
              </div>
              <div className="grid gap-2">
                {referenceRows.map((row, rowIndex) => {
                  const rowSource = workflowStringValue(row.source_step || row.source || row.from_step)
                  const sourceValue = referenceCandidates.some((candidate) => candidate.id === rowSource) ? rowSource : ""
                  return (
                    <div key={`${workflowStringValue(row.name) || "reference"}-${rowIndex}`} className="grid gap-2 rounded border border-white/[0.06] bg-white/[0.025] p-2">
                      <div className="grid grid-cols-[minmax(0,1fr)_104px] gap-2">
                        <label className="block text-[10px] font-medium text-zinc-500">
                          选择上游步骤
                          <select
                            value={sourceValue}
                            disabled={readOnly}
                            onChange={(event) => onUpdateStep(step.id, {
                              uses: workflowPatchReferenceRow(step, rowIndex, { source_step: event.target.value }),
                            })}
                            className={cn(textFieldClass, "mt-1 h-7")}
                          >
                            <option value="">选择上游步骤</option>
                            {rowSource && !sourceValue && (
                              <option value={rowSource}>{workflowStepTitleById(steps, rowSource)}</option>
                            )}
                            {referenceCandidates.map((candidate) => (
                              <option key={candidate.id} value={candidate.id}>{candidate.title || candidate.id}</option>
                            ))}
                          </select>
                        </label>
                        <label className="block text-[10px] font-medium text-zinc-500">
                          参考用途
                          <select
                            value={workflowStringValue(row.role) || "reference"}
                            disabled={readOnly}
                            onChange={(event) => onUpdateStep(step.id, {
                              uses: workflowPatchReferenceRow(step, rowIndex, { role: event.target.value }),
                            })}
                            className={cn(textFieldClass, "mt-1 h-7")}
                          >
                            {WORKFLOW_REFERENCE_ROLE_OPTIONS.map((item) => (
                              <option key={item.value} value={item.value}>{item.label}</option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
                        <input
                          value={workflowStringValue(asWorkflowObject(row.select)?.values)}
                          disabled={readOnly}
                          onChange={(event) => onUpdateStep(step.id, {
                            uses: workflowPatchReferenceRow(step, rowIndex, {
                              select: event.target.value
                                ? { values: event.target.value, by: asWorkflowObject(row.select)?.by || ["id"] }
                                : undefined,
                            }),
                          })}
                          placeholder="动态选择路径（可留空）"
                          className={cn(textFieldClass, "h-7")}
                        />
                        {!readOnly && (
                          <button
                            type="button"
                            onClick={() => onUpdateStep(step.id, { uses: workflowRemoveReferenceRow(step, rowIndex) })}
                            className="h-7 rounded border border-red-300/20 px-2 text-[10px] text-red-100 transition hover:bg-red-500/12"
                          >
                            删除
                          </button>
                        )}
                      </div>
                    </div>
                  )
                })}
                {referenceRows.length === 0 && (
                  <div className="rounded border border-white/[0.06] bg-white/[0.025] px-2 py-2 text-[11px] text-zinc-500">
                    暂无引用
                  </div>
                )}
              </div>
            </section>
          )}

          {activeTab === "io" && kind === "plugin" && (
            <section className="rounded-md border border-violet-200/12 bg-violet-300/[0.035] p-3">
              <div className="mb-2 text-[11px] font-semibold text-violet-100/80">插件配置</div>
              {pluginDefinition && (
                <div className="mb-2 rounded border border-white/[0.06] bg-white/[0.025] px-2 py-1.5 text-[11px] text-zinc-300">
                  {pluginDefinition.title || pluginDefinition.name || pluginDefinition.id}
                </div>
              )}
              <div className="grid gap-3">
                <div className="grid gap-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-zinc-500">输入参数</div>
                    {!readOnly && pluginInputSpecs.length === 0 && (
                      <button
                        type="button"
                        onClick={() => onUpdateStep(step.id, { plugin: { ...pluginConfig, inputs: workflowAddObjectEntry(pluginInputs, "input") } })}
                        className="h-6 rounded border border-violet-200/20 px-2 text-[10px] text-violet-100 transition hover:bg-violet-300/10"
                      >
                        新增
                      </button>
                    )}
                  </div>
                  {pluginInputSpecs.length > 0 ? pluginInputSpecs.map((field, fieldIndex) => {
                    const key = workflowDefinitionFieldKey(field, `input_${fieldIndex + 1}`)
                    return (
                      <label key={key} className="block text-[10px] font-medium text-zinc-500">
                        {workflowDefinitionFieldLabel(field, key)}
                        <div className="mt-1">
                          {renderPluginFieldInput(field, pluginInputs[key], (value) => onUpdateStep(step.id, {
                            plugin: { ...pluginConfig, inputs: { ...pluginInputs, [key]: value } },
                          }))}
                        </div>
                      </label>
                    )
                  }) : Object.entries(pluginInputs).map(([key, value]) => (
                    <div key={key} className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)_auto] gap-2">
                      <input
                        value={key}
                        disabled={readOnly}
                        onChange={(event) => onUpdateStep(step.id, {
                          plugin: { ...pluginConfig, inputs: workflowRenameObjectEntry(pluginInputs, key, event.target.value) },
                        })}
                        className={cn(textFieldClass, "h-7 font-mono")}
                      />
                      <input
                        value={workflowJsonScalar(value)}
                        disabled={readOnly}
                        onChange={(event) => onUpdateStep(step.id, {
                          plugin: { ...pluginConfig, inputs: workflowSetObjectEntry(pluginInputs, key, event.target.value) },
                        })}
                        className={cn(textFieldClass, "h-7")}
                      />
                      {!readOnly && (
                        <button
                          type="button"
                          onClick={() => onUpdateStep(step.id, { plugin: { ...pluginConfig, inputs: workflowRemoveObjectEntry(pluginInputs, key) } })}
                          className="h-7 rounded border border-red-300/20 px-2 text-[10px] text-red-100 transition hover:bg-red-500/12"
                        >
                          删除
                        </button>
                      )}
                    </div>
                  ))}
                  {pluginInputSpecs.length === 0 && Object.keys(pluginInputs).length === 0 && (
                    <div className="rounded border border-white/[0.06] bg-white/[0.025] px-2 py-2 text-[11px] text-zinc-500">
                      暂无输入参数
                    </div>
                  )}
                </div>

                <div className="grid gap-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-zinc-500">运行设置</div>
                    {!readOnly && pluginSettingSpecs.length === 0 && (
                      <button
                        type="button"
                        onClick={() => onUpdateStep(step.id, { plugin: { ...pluginConfig, settings: workflowAddObjectEntry(pluginSettings, "setting") } })}
                        className="h-6 rounded border border-violet-200/20 px-2 text-[10px] text-violet-100 transition hover:bg-violet-300/10"
                      >
                        新增
                      </button>
                    )}
                  </div>
                  {pluginSettingSpecs.length > 0 ? pluginSettingSpecs.map((field, fieldIndex) => {
                    const key = workflowDefinitionFieldKey(field, `setting_${fieldIndex + 1}`)
                    return (
                      <label key={key} className="block text-[10px] font-medium text-zinc-500">
                        {workflowDefinitionFieldLabel(field, key)}
                        <div className="mt-1">
                          {renderPluginFieldInput(field, pluginSettings[key], (value) => onUpdateStep(step.id, {
                            plugin: { ...pluginConfig, settings: { ...pluginSettings, [key]: value } },
                          }))}
                        </div>
                      </label>
                    )
                  }) : Object.entries(pluginSettings).map(([key, value]) => (
                    <div key={key} className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)_auto] gap-2">
                      <input
                        value={key}
                        disabled={readOnly}
                        onChange={(event) => onUpdateStep(step.id, {
                          plugin: { ...pluginConfig, settings: workflowRenameObjectEntry(pluginSettings, key, event.target.value) },
                        })}
                        className={cn(textFieldClass, "h-7 font-mono")}
                      />
                      <input
                        value={workflowJsonScalar(value)}
                        disabled={readOnly}
                        onChange={(event) => onUpdateStep(step.id, {
                          plugin: { ...pluginConfig, settings: workflowSetObjectEntry(pluginSettings, key, event.target.value) },
                        })}
                        className={cn(textFieldClass, "h-7")}
                      />
                      {!readOnly && (
                        <button
                          type="button"
                          onClick={() => onUpdateStep(step.id, { plugin: { ...pluginConfig, settings: workflowRemoveObjectEntry(pluginSettings, key) } })}
                          className="h-7 rounded border border-red-300/20 px-2 text-[10px] text-red-100 transition hover:bg-red-500/12"
                        >
                          删除
                        </button>
                      )}
                    </div>
                  ))}
                  {pluginSettingSpecs.length === 0 && Object.keys(pluginSettings).length === 0 && (
                    <div className="rounded border border-white/[0.06] bg-white/[0.025] px-2 py-2 text-[11px] text-zinc-500">
                      暂无运行设置
                    </div>
                  )}
                </div>
              </div>
            </section>
          )}

          {activeTab === "prompt" && !isInputStep && kind !== "loop" && kind !== "plugin" && (
            <section className="rounded-md border border-cyan-200/12 bg-cyan-300/[0.04] p-3">
              <div className="mb-2">
                <div className="text-[11px] font-semibold text-cyan-100/80">提示词</div>
                <div className="mt-1 text-[10px] leading-4 text-cyan-100/45">直接写自然语言，不需要 JSON。需要带入上一步结果时，从下方插入可用内容。</div>
              </div>
              {!readOnly && (inputIds.length > 0 || referenceCandidates.length > 0) && (
                <div className="mb-2 rounded-md border border-cyan-200/10 bg-black/18 p-2">
                  <div className="mb-1.5 text-[10px] font-semibold text-cyan-100/65">插入可用内容</div>
                  <div className="flex flex-wrap gap-1.5">
                    {inputIds.map((input) => {
                      const label = workflowInputDisplayName(input, inputSpecs)
                      const text = workflowPromptInputReference(input, label)
                      return (
                        <button
                          key={`prompt-input-${input}`}
                          type="button"
                          onClick={() => appendPromptText(text)}
                          className="h-7 max-w-[160px] truncate rounded border border-white/10 bg-white/[0.035] px-2 text-[10px] font-medium text-zinc-200 transition hover:border-cyan-200/30 hover:bg-cyan-300/[0.06]"
                          title={text}
                        >
                          输入 · {label}
                        </button>
                      )
                    })}
                    {referenceCandidates.map((candidate) => {
                      const text = workflowPromptStepReference(candidate)
                      return (
                        <button
                          key={`prompt-step-${candidate.id}`}
                          type="button"
                          onClick={() => appendPromptText(text)}
                          className="h-7 max-w-[160px] truncate rounded border border-white/10 bg-white/[0.035] px-2 text-[10px] font-medium text-zinc-200 transition hover:border-cyan-200/30 hover:bg-cyan-300/[0.06]"
                          title={text}
                        >
                          上游 · {candidate.title || candidate.id}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )}
              <textarea
                value={promptValue}
                onChange={(event) => onUpdateStep(step.id, { prompt: { ...promptObject, task: event.target.value } })}
                placeholder="例如：根据上一步的剧情内容，拆成多个 15 秒片段。每段写清楚剧情、画面重点、出场人物。"
                rows={10}
                readOnly={readOnly}
                className="min-h-52 w-full resize-none rounded-md border border-cyan-200/14 bg-[#071019] px-3 py-2 text-xs leading-5 text-cyan-50 outline-none placeholder:text-cyan-100/25 focus:border-cyan-200/45 read-only:cursor-default read-only:opacity-70"
              />
            </section>
          )}

          {activeTab === "prompt" && isInputStep && (
            <section className="rounded-md border border-white/[0.08] bg-black/18 px-3 py-2 text-[12px] text-zinc-500">
              输入节点不需要提示词。
            </section>
          )}

          {activeTab === "run" && (nodeState?.lastRunSummary || nodeState?.lastRunDetail || nodeState?.outputPreview) && (
            <section className="overflow-hidden rounded-md border border-emerald-200/14 bg-emerald-300/[0.045]">
              <div className="border-b border-emerald-200/10 px-3 py-2 text-[11px] font-semibold text-emerald-100/80">
                运行结果
              </div>
              {(nodeState?.lastRunSummary || nodeState?.lastRunDetail) && (
                <div className="border-b border-emerald-200/10 px-3 py-2">
                  {nodeState.lastRunSummary && <div className="text-[12px] leading-5 text-emerald-50/90">{nodeState.lastRunSummary}</div>}
                  {nodeState.lastRunDetail && <div className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-5 text-emerald-100/70">{nodeState.lastRunDetail}</div>}
                </div>
              )}
              {nodeState?.outputPreview && <WorkflowRunOutputView value={nodeState.outputPreview} />}
            </section>
          )}

          {activeTab === "run" && !(nodeState?.lastRunSummary || nodeState?.lastRunDetail || nodeState?.outputPreview) && (
            <section className="rounded-md border border-white/[0.08] bg-black/18 px-3 py-2 text-[12px] text-zinc-500">
              这个节点还没有运行结果。
            </section>
          )}

          {activeTab === "properties" && !readOnly && (
          <button
            type="button"
            onClick={() => onDeleteStep(step.id)}
            className="h-8 rounded-md border border-red-300/20 bg-red-500/10 text-xs font-semibold text-red-100 transition hover:bg-red-500/16"
          >
            删除这个流程节点
          </button>
          )}
        </div>
      </div>
    </aside>
  )
}

function WorkflowTemplatePanel({
  templates,
  selectedId,
  artifactPreview,
  nodeTypes,
  nodeTypesError,
  runtimeSteps,
  loading,
  error,
  materializing,
  runningStepIds,
  runningAll,
  inputValues,
  requiredInputIds,
  nodeStates,
  mediaModelOverrides,
  onSelectedIdChange,
  onInputValueChange,
  onMediaModelOverrideChange,
  onClearArtifactPreview,
  onRefresh,
  onImportSpecFile,
  onRunStep,
  onRunNext,
  onRunAll,
  onSaveWorkflowSpec,
  onSaveWorkflowTemplate,
  onDownloadWorkflowTemplate,
  onRestoreBuiltinTemplate,
}: {
  templates: WorkflowTemplateSummary[]
  selectedId: string
  artifactPreview: WorkflowArtifactPreview | null
  nodeTypes: WorkflowNodeTypeDefinition[]
  nodeTypesError: string | null
  runtimeSteps: WorkflowTemplateStepSummary[]
  loading: boolean
  error: string | null
  materializing: boolean
  runningStepIds: string[]
  runningAll: boolean
  inputValues: Record<string, string>
  requiredInputIds: string[]
  nodeStates: Record<string, WorkflowStepNodeState>
  mediaModelOverrides: Record<string, string>
  onSelectedIdChange: (id: string) => void
  onInputValueChange: (id: string, value: string) => void
  onMediaModelOverrideChange: (stepId: string, value: string) => void
  onClearArtifactPreview: () => void
  onRefresh: () => void
  onImportSpecFile: (file: File) => void
  onMaterialize: () => void
  onRunStep: (stepId: string) => void
  onRunNext: () => void
  onRunAll: () => void
  onSaveWorkflowSpec: (workflow: Record<string, unknown>) => Promise<void>
  onSaveWorkflowTemplate: (
    workflow: Record<string, unknown>,
    options: { templateId?: string; replaceExisting?: boolean },
  ) => Promise<string | void>
  onDownloadWorkflowTemplate: (template: WorkflowTemplateSummary) => Promise<void>
  onRestoreBuiltinTemplate: (template: WorkflowTemplateSummary) => Promise<void>
}) {
  const [detailStepId, setDetailStepId] = useState<string | null>(null)
  const [draftSteps, setDraftSteps] = useState<WorkflowTemplateStepSummary[]>([])
  const [draftBaselineSignature, setDraftBaselineSignature] = useState<string | null>(null)
  const [savingDraft, setSavingDraft] = useState(false)
  const [savingTemplate, setSavingTemplate] = useState(false)
  const [downloadingTemplate, setDownloadingTemplate] = useState(false)
  const [restoringBuiltinTemplate, setRestoringBuiltinTemplate] = useState(false)
  const [restoreBuiltinConfirmOpen, setRestoreBuiltinConfirmOpen] = useState(false)
  const [draftError, setDraftError] = useState<string | null>(null)
  const [workflowName, setWorkflowName] = useState("")
  const [workflowDescription, setWorkflowDescription] = useState("")
  const [workflowAdvanced, setWorkflowAdvanced] = useState<Record<string, unknown>>({})
  const [draftWorkflowId, setDraftWorkflowId] = useState("")
  const [draftInputIds, setDraftInputIds] = useState<string[]>([])
  const [draftRequiredInputIds, setDraftRequiredInputIds] = useState<string[]>([])
  const [draftInputSpecs, setDraftInputSpecs] = useState<Record<string, WorkflowInputDraftSpec>>({})
  const [paletteSearch, setPaletteSearch] = useState("")
  const [toolboxOpen, setToolboxOpen] = useState(true)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [collapsedTemplateScopeIds, setCollapsedTemplateScopeIds] = useState<Set<string>>(() => new Set())
  const [workflowMediaProviders, setWorkflowMediaProviders] = useState<MediaProviderSummary[]>([])
  const [workflowVideoProtocols, setWorkflowVideoProtocols] = useState<VideoProtocolSummary[]>([])
  const [workflowMediaConfigError, setWorkflowMediaConfigError] = useState<string | null>(null)
  const selected = templates.find((item) => item.id === selectedId) || templates[0]
  const artifactMode = Boolean(artifactPreview)
  const inputs = useMemo(
    () => workflowInputsForTemplateSource(artifactPreview, selected, templates),
    [artifactPreview, selected, templates],
  )
  const templateSteps = useMemo(
    () => workflowStepsForTemplateSource(artifactPreview, selected, templates),
    [artifactPreview, selected, templates],
  )
  const selectedName = artifactPreview?.name || selected?.name || "暂无流程"
  const selectedDescription = artifactPreview?.description || selected?.description || selected?.applies_to || "结构化流程"
  const sourceKey = useMemo(() => [
    artifactPreview?.source || "template",
    artifactPreview?.id || selected?.id || "",
    templateSteps.map((step) => step.id).join("|"),
  ].join("::"), [artifactPreview?.id, artifactPreview?.source, selected?.id, templateSteps])
  const sourceSteps = useMemo(() => templateSteps.map(workflowCloneEditorStep), [templateSteps])
  const draftSignature = useMemo(
    () => workflowEditorDraftSignature(workflowName, workflowDescription, draftSteps, draftInputIds, draftRequiredInputIds, draftInputSpecs, workflowAdvanced),
    [draftInputIds, draftInputSpecs, draftRequiredInputIds, draftSteps, workflowAdvanced, workflowDescription, workflowName],
  )
  const draftDirty = draftBaselineSignature !== null && draftSignature !== draftBaselineSignature
  const hasMediaModelOverrides = Object.keys(workflowCleanMediaModelOverrides(mediaModelOverrides)).length > 0

  useEffect(() => {
    let cancelled = false
    const loadWorkflowMediaConfig = async () => {
      try {
        const [result, protocols] = await Promise.all([
          getRuntimeConfigFile<{ parsed?: { media_providers?: MediaProviderSummary[] } }>(true),
          getVideoProviderProtocols<{ protocols?: VideoProtocolSummary[] }>().catch(() => null),
        ])
        if (cancelled) return
        setWorkflowMediaProviders(result.parsed?.media_providers || [])
        setWorkflowVideoProtocols(protocols?.protocols || [])
        setWorkflowMediaConfigError(null)
      } catch (err) {
        if (cancelled) return
        setWorkflowMediaProviders([])
        setWorkflowVideoProtocols([])
        setWorkflowMediaConfigError(err instanceof Error ? err.message : String(err))
      }
    }
    void loadWorkflowMediaConfig()
    window.addEventListener("drama:runtime-config-updated", loadWorkflowMediaConfig)
    return () => {
      cancelled = true
      window.removeEventListener("drama:runtime-config-updated", loadWorkflowMediaConfig)
    }
  }, [])

  useEffect(() => {
    const nextSteps = sourceSteps
    const nextName = selectedName
    const nextDescription = selectedDescription
    const sourceWorkflowForAdvanced = artifactPreview?.workflow || workflowSourceFromTemplateSummary(selected)
    setCollapsedTemplateScopeIds(new Set())
    setDraftSteps(nextSteps)
    setDraftInputIds(inputs)
    setDraftRequiredInputIds(requiredInputIds.filter((input) => inputs.includes(input)))
    setDraftInputSpecs(workflowInputDraftSpecsFromWorkflow(inputs, sourceWorkflowForAdvanced))
    setWorkflowAdvanced(workflowAdvancedDraftFromWorkflow(sourceWorkflowForAdvanced))
    setDetailStepId((current) => current && nextSteps.some((step) => step.id === current) ? current : null)
    setWorkflowName(nextName)
    setWorkflowDescription(nextDescription)
    setDraftWorkflowId("")
    setDraftBaselineSignature(workflowEditorDraftSignature(
      nextName,
      nextDescription,
      nextSteps,
      inputs,
      requiredInputIds.filter((input) => inputs.includes(input)),
      workflowInputDraftSpecsFromWorkflow(inputs, sourceWorkflowForAdvanced),
      workflowAdvancedDraftFromWorkflow(sourceWorkflowForAdvanced),
    ))
    setDraftError(null)
  }, [artifactPreview?.workflow, inputs, requiredInputIds, selected, selectedDescription, selectedName, sourceKey, sourceSteps])

  const resetDraftWorkflow = useCallback(() => {
    const nextSteps = sourceSteps
    const nextName = selectedName
    const nextDescription = selectedDescription
    const sourceWorkflowForAdvanced = artifactPreview?.workflow || workflowSourceFromTemplateSummary(selected)
    const nextRequiredInputIds = requiredInputIds.filter((input) => inputs.includes(input))
    const nextInputSpecs = workflowInputDraftSpecsFromWorkflow(inputs, sourceWorkflowForAdvanced)
    const nextAdvanced = workflowAdvancedDraftFromWorkflow(sourceWorkflowForAdvanced)
    setCollapsedTemplateScopeIds(new Set())
    setDraftSteps(nextSteps)
    setDraftInputIds(inputs)
    setDraftRequiredInputIds(nextRequiredInputIds)
    setDraftInputSpecs(nextInputSpecs)
    setWorkflowAdvanced(nextAdvanced)
    setDetailStepId((current) => current && nextSteps.some((step) => step.id === current) ? current : null)
    setWorkflowName(nextName)
    setWorkflowDescription(nextDescription)
    setDraftWorkflowId("")
    setDraftBaselineSignature(workflowEditorDraftSignature(
      nextName,
      nextDescription,
      nextSteps,
      inputs,
      nextRequiredInputIds,
      nextInputSpecs,
      nextAdvanced,
    ))
    for (const step of draftSteps) {
      onMediaModelOverrideChange(step.id, "")
    }
    setDraftError(null)
  }, [
    artifactPreview?.workflow,
    draftSteps,
    inputs,
    onMediaModelOverrideChange,
    requiredInputIds,
    selected,
    selectedDescription,
    selectedName,
    sourceSteps,
  ])

  const missingRequiredInputs = useMemo(
    () => workflowMissingInputIds(draftInputIds, inputValues, draftRequiredInputIds, draftInputSpecs),
    [draftInputIds, draftInputSpecs, draftRequiredInputIds, inputValues],
  )
  const selectedDraftStep = detailStepId ? draftSteps.find((step) => step.id === detailStepId) : undefined
  const activeTemplateScopeId = workflowStringValue(selectedDraftStep?.repeat_group_id) || WORKFLOW_TEMPLATE_ROOT_SCOPE_ID
  const templateScopeChildCounts = useMemo(
    () => workflowTemplateChildScopeCounts(draftSteps),
    [draftSteps],
  )
  const templateDisplaySteps = useMemo(
    () => workflowTemplateVisibleSteps(draftSteps, collapsedTemplateScopeIds),
    [collapsedTemplateScopeIds, draftSteps],
  )
  const activeTemplateScopeTitle = workflowTemplateScopeTitle(activeTemplateScopeId, draftSteps, selected)
  const displayedSteps = templateDisplaySteps
  const editorDisplayedSteps = useMemo(() => {
    const layoutPositions = workflowGraphAutoLayout(displayedSteps)
    const positioned = displayedSteps.map((step, index) => ({
      step,
      position: workflowExplicitEditorPosition(step) || layoutPositions.get(step.id) || workflowEditorPosition(step, index),
    }))
    const entrySteps = positioned.filter(({ step }) => (
      !workflowStringValue(step.repeat_group_id) && workflowCleanIdList(step.depends_on).length === 0
    ))
    const anchors = entrySteps.length > 0 ? entrySteps : positioned.slice(0, 1)
    const minX = positioned.length > 0 ? Math.min(...positioned.map((item) => item.position.x)) : 0
    const inputY = anchors.length > 0
      ? anchors.reduce((sum, item) => sum + item.position.y, 0) / anchors.length
      : 0
    const inputStep: WorkflowTemplateStepSummary = {
      id: WORKFLOW_EDITOR_INPUT_STEP_ID,
      title: "流程输入",
      node_type: "text",
      kind: "text",
      role: "entry",
      runner: "workflow_input",
      depends_on: [],
      description: `定义 ${draftInputIds.length} 个运行输入字段`,
      ui: {
        position: {
          x: minX - WORKFLOW_GRAPH_COLUMN_GAP,
          y: inputY,
        },
      },
    }
    const entryIds = new Set(entrySteps.map((item) => item.step.id))
    const projectedSteps = displayedSteps.map((step) => (
      entryIds.has(step.id)
        ? workflowCloneEditorStep({
            ...step,
            layout_after: workflowCleanIdList([...(step.layout_after || []), WORKFLOW_EDITOR_INPUT_STEP_ID]),
          })
        : step
    ))
    return [inputStep, ...projectedSteps]
  }, [displayedSteps, draftInputIds.length])
  const detailStep = detailStepId ? displayedSteps.find((step) => step.id === detailStepId) : undefined
  const runningSet = useMemo(() => new Set(runningStepIds), [runningStepIds])
  const nodeTypeGroups = useMemo(() => {
    const groups = new Map<string, WorkflowNodeTypeDefinition[]>()
    for (const item of nodeTypes) {
      const key = String(item.category || "workflow")
      const list = groups.get(key) || []
      list.push(item)
      groups.set(key, list)
    }
    return Array.from(groups.entries()).slice(0, 5)
  }, [nodeTypes])
  const processNodeTypes = useMemo<WorkflowNodeTypeDefinition[]>(() => [
    { id: "core-text", type: "text", kind: "text", title: "文本", category: "core", description: "生成、改写或整理文本" },
    { id: "core-object", type: "text", kind: "object", title: "结构化对象", category: "core", description: "输出一组有明确字段的数据" },
    { id: "core-collection", type: "text", kind: "collection", title: "结构化列表", category: "core", description: "输出人物、场景、段落等多项数据" },
    { id: "core-loop", type: "text", kind: "loop", title: "循环", category: "core", description: "对列表里的每一项执行内部步骤" },
  ], [])
  const productNodeTypes = useMemo<WorkflowNodeTypeDefinition[]>(() => [
    { id: "core-canvas-text", type: "text", kind: "text", title: "文本", category: "core", description: "生成画布上可见的文本节点" },
    { id: "core-image", type: "image", kind: "image", title: "图片", category: "core", description: "生成图片或采用已有图片" },
    { id: "core-video", type: "video", kind: "video", title: "视频", category: "core", description: "生成视频或采用已有视频" },
    { id: "core-audio", type: "audio", kind: "audio", title: "音频", category: "core", description: "生成音频或采用已有音频" },
  ], [])
  const coreNodeTypes = useMemo(() => [...processNodeTypes, ...productNodeTypes], [processNodeTypes, productNodeTypes])
  const paletteQuery = paletteSearch.trim().toLowerCase()
  const visibleProcessNodeTypes = useMemo(() => (
    paletteQuery
      ? processNodeTypes.filter((item) => `${item.title} ${item.description} ${item.kind} ${item.type}`.toLowerCase().includes(paletteQuery))
      : processNodeTypes
  ), [processNodeTypes, paletteQuery])
  const visibleProductNodeTypes = useMemo(() => (
    paletteQuery
      ? productNodeTypes.filter((item) => `${item.title} ${item.description} ${item.kind} ${item.type}`.toLowerCase().includes(paletteQuery))
      : productNodeTypes
  ), [productNodeTypes, paletteQuery])
  const visibleNodeTypeGroups = useMemo(() => (
    nodeTypeGroups
      .map(([category, items]) => [
        category,
        paletteQuery
          ? items.filter((item) => !workflowNodePaletteItemIsDuplicate(item) && `${item.title} ${item.name || ""} ${item.description || ""} ${item.plugin_name || ""} ${item.type}`.toLowerCase().includes(paletteQuery))
          : items.filter((item) => !workflowNodePaletteItemIsDuplicate(item)),
      ] as [string, WorkflowNodeTypeDefinition[]])
      .filter(([, items]) => items.length > 0)
  ), [nodeTypeGroups, paletteQuery])

  const updateWorkflowName = useCallback((value: string) => {
    setWorkflowName((current) => current === value ? current : value)
    setDraftError(null)
  }, [])

  const updateWorkflowDescription = useCallback((value: string) => {
    setWorkflowDescription((current) => current === value ? current : value)
    setDraftError(null)
  }, [])

  const updateWorkflowAdvanced = useCallback((key: string, value: unknown) => {
    setWorkflowAdvanced((current) => workflowSetAdvancedDraftField(current, key, value))
    setDraftError(null)
  }, [])

  const createBlankWorkflow = useCallback(() => {
    const nextName = "未命名流程"
    const nextDescription = ""
    const nextSteps: WorkflowTemplateStepSummary[] = []
    const nextInputs: string[] = []
    const nextRequiredInputs: string[] = []
    const nextInputSpecs: Record<string, WorkflowInputDraftSpec> = {}
    const nextAdvanced: Record<string, unknown> = {}
    setCollapsedTemplateScopeIds(new Set())
    setDraftWorkflowId(`workflow_${Date.now()}`)
    setDraftSteps(nextSteps)
    setDraftInputIds(nextInputs)
    setDraftRequiredInputIds(nextRequiredInputs)
    setDraftInputSpecs(nextInputSpecs)
    setWorkflowAdvanced(nextAdvanced)
    setDetailStepId(null)
    setWorkflowName(nextName)
    setWorkflowDescription(nextDescription)
    setDraftBaselineSignature(workflowEditorDraftSignature(
      nextName,
      nextDescription,
      nextSteps,
      nextInputs,
      nextRequiredInputs,
      nextInputSpecs,
      nextAdvanced,
    ))
    setDraftError(null)
  }, [])

  const addDraftInput = useCallback(() => {
    const id = workflowUniqueInputId("input", draftInputIds)
    setDraftInputIds((current) => [...current, id])
    setDraftInputSpecs((specs) => ({ ...specs, [id]: { type: "text", label: "新输入内容" } }))
    setDraftError(null)
  }, [draftInputIds])

  const addDraftInputPreset = useCallback((preset: WorkflowInputPreset) => {
    const id = workflowUniqueInputId(preset.id, draftInputIds)
    setDraftInputIds((current) => [...current, id])
    setDraftInputSpecs((specs) => ({
      ...specs,
      [id]: {
        type: preset.type,
        label: preset.label,
        description: preset.description,
        default: preset.default || "",
      },
    }))
    if (preset.required) {
      setDraftRequiredInputIds((current) => current.includes(id) ? current : [...current, id])
    }
    setDraftError(null)
  }, [draftInputIds])

  const renameDraftInput = useCallback((currentId: string, rawNextId: string) => {
    const nextId = workflowUniqueInputId(rawNextId, draftInputIds, currentId)
    if (!nextId || nextId === currentId) return
    setDraftInputIds((current) => current.map((input) => input === currentId ? nextId : input))
    setDraftRequiredInputIds((current) => current.map((input) => input === currentId ? nextId : input))
    setDraftInputSpecs((current) => {
      const next = { ...current }
      next[nextId] = next[currentId] || { type: "text" }
      delete next[currentId]
      return next
    })
    setDraftError(null)
  }, [draftInputIds])

  const deleteDraftInput = useCallback((inputId: string) => {
    setDraftInputIds((current) => current.filter((input) => input !== inputId))
    setDraftRequiredInputIds((current) => current.filter((input) => input !== inputId))
    setDraftInputSpecs((current) => {
      const next = { ...current }
      delete next[inputId]
      return next
    })
    setDraftError(null)
  }, [])

  const toggleDraftInputRequired = useCallback((inputId: string, required: boolean) => {
    setDraftRequiredInputIds((current) => {
      const next = new Set(current)
      if (required) next.add(inputId)
      else next.delete(inputId)
      return draftInputIds.filter((input) => next.has(input))
    })
    setDraftError(null)
  }, [draftInputIds])

  const updateDraftInputSpec = useCallback((inputId: string, patch: Partial<WorkflowInputDraftSpec>) => {
    setDraftInputSpecs((current) => {
      const nextSpec: WorkflowInputDraftSpec = {
        type: "text",
        ...(current[inputId] || {}),
        ...patch,
      }
      if (!workflowInputTypeUsesOptions(nextSpec.type)) {
        delete nextSpec.options
      } else if (!nextSpec.options || nextSpec.options.length === 0) {
        nextSpec.options = [{ value: "option_1", label: "选项 1" }]
      }
      return {
        ...current,
        [inputId]: nextSpec,
      }
    })
    setDraftError(null)
  }, [])

  const updateDraftStep = useCallback((stepId: string, patch: Partial<WorkflowTemplateStepSummary>) => {
    setDraftSteps((current) => {
      let changed = false
      const next = current.map((step) => {
        if (step.id !== stepId) return step
        const candidate = workflowCloneEditorStep({ ...step, ...patch })
        if (workflowStableStringify(candidate) === workflowStableStringify(step)) return step
        changed = true
        return candidate
      })
      return changed ? next : current
    })
    setDraftError(null)
  }, [])

  const renameDraftStep = useCallback((stepId: string, nextRawId: string) => {
    const nextId = workflowUniqueStepId(nextRawId, draftSteps, stepId)
    if (nextId === stepId) return
    setDraftSteps((current) => {
      return current.map((step) => {
        if (step.id === stepId) {
          return workflowCloneEditorStep({
            ...step,
            id: nextId,
            child_scope_id: workflowStringValue(step.child_scope_id) === stepId ? nextId : step.child_scope_id,
          })
        }
        return workflowCloneEditorStep({
          ...step,
          depends_on: workflowCleanIdList(step.depends_on).map((dep) => dep === stepId ? nextId : dep),
          layout_after: workflowCleanIdList(step.layout_after).map((dep) => dep === stepId ? nextId : dep),
          repeat_group_id: workflowStringValue(step.repeat_group_id) === stepId ? nextId : step.repeat_group_id,
        })
      })
    })
    setCollapsedTemplateScopeIds((current) => {
      if (!current.has(stepId)) return current
      const next = new Set(current)
      next.delete(stepId)
      next.add(nextId)
      return next
    })
    setDetailStepId(nextId)
    setDraftError(null)
  }, [draftSteps])

  const addDraftStep = useCallback((item: WorkflowNodeTypeDefinition, options?: WorkflowAddStepOptions) => {
    setDraftSteps((current) => {
      const kindText = String(item.kind || item.type || "text").toLowerCase()
      const plugin = Boolean(item.plugin_id || item.plugin_name || kindText === "plugin")
      const kind = (
        plugin
          ? "plugin"
          : kindText === "object"
          ? "object"
          : kindText === "collection"
          ? "collection"
          : kindText === "loop"
          ? "loop"
          : item.type === "image" || item.type === "video" || item.type === "audio"
          ? item.type
          : "text"
      ) as WorkflowAuthoringKind
      const nodeType = workflowNodeType(plugin ? "text" : item.type)
      const id = workflowUniqueStepId(item.name || item.title || item.type || "step", current)
      const explicitAfterStep = options?.afterStepId
        ? current.find((candidate) => candidate.id === options.afterStepId)
        : undefined
      const explicitChildScopeId = explicitAfterStep ? workflowStepChildScopeId(explicitAfterStep) : ""
      const targetScopeId = explicitChildScopeId
        || workflowStringValue(explicitAfterStep?.repeat_group_id)
        || (activeTemplateScopeId !== WORKFLOW_TEMPLATE_ROOT_SCOPE_ID ? activeTemplateScopeId : "")
      const targetScopeStep = targetScopeId
        ? current.find((candidate) => workflowStepChildScopeId(candidate) === targetScopeId || candidate.id === targetScopeId)
        : undefined
      const targetScopeTitle = targetScopeStep?.title || activeTemplateScopeTitle
      const sameScopeSteps = current.filter((candidate) => workflowStringValue(candidate.repeat_group_id) === targetScopeId)
      const selectedInSameScope = detailStepId
        ? current.find((candidate) => candidate.id === detailStepId && workflowStringValue(candidate.repeat_group_id) === targetScopeId)
        : undefined
      const previousStep = options?.detached
        ? undefined
        : explicitChildScopeId
        ? sameScopeSteps[sameScopeSteps.length - 1]
        : explicitAfterStep || selectedInSameScope || sameScopeSteps[sameScopeSteps.length - 1]
      const canvasTextProduct = item.id === "core-canvas-text"
      const productKind = canvasTextProduct || kind === "image" || kind === "video" || kind === "audio"
      const step: WorkflowTemplateStepSummary = {
        id,
        title: item.title || item.name || id,
        node_type: kind === "loop" ? "text" : nodeType,
        kind,
        depends_on: previousStep ? [previousStep.id] : [],
        description: item.description || "",
        runner: plugin ? "workflow_plugin" : kind === "image" || kind === "video" || kind === "audio" ? "workflow_canvas_output" : "node.run",
        execution: "auto",
        on_error: "stop",
      }
      if (plugin) step.plugin = { id: item.plugin_id || "", action: item.type }
      if (canvasTextProduct) {
        step.output = { canvas: true }
      }
      if (productKind) {
        if (kind === "image" || kind === "video" || kind === "audio") {
          step.fields = workflowDefaultCanvasProductFields(kind)
        }
      }
      if (kind === "loop") {
        step.role = "repeat_group"
        step.shape = "loop"
        step.child_scope_id = id
        step.has_children = true
        step.foreach = { items: "", as: "item" }
      }
      if (kind === "collection" || kind === "object") {
        const schema = {
          fields: [
            { id: "value", label: "内容", type: "string", required: true },
          ],
        }
        step.output = { schema }
      }
      if (targetScopeId) {
        step.repeat_group_id = targetScopeId
        step.repeat_group_label = targetScopeTitle
      }
      const positionAnchorStep = previousStep || explicitAfterStep
      const positionAnchorIndex = positionAnchorStep ? current.findIndex((candidate) => candidate.id === positionAnchorStep.id) : -1
      const autoLayoutPositions = positionAnchorStep ? workflowGraphAutoLayout(current) : undefined
      const positionAnchor = positionAnchorStep
        ? workflowExplicitEditorPosition(positionAnchorStep)
          || autoLayoutPositions?.get(positionAnchorStep.id)
          || workflowEditorPosition(positionAnchorStep, positionAnchorIndex >= 0 ? positionAnchorIndex : current.length)
        : undefined
      const nextPosition = options?.position
        ? workflowNormalizeEditorPosition(options.position)
        : positionAnchor
        ? workflowNormalizeEditorPosition({
          x: positionAnchor.x + WORKFLOW_GRAPH_COLUMN_GAP,
          y: positionAnchor.y,
        })
        : undefined
      if (nextPosition) {
        step.ui = {
          ...(asWorkflowObject(step.ui) || {}),
          position: nextPosition,
        }
      }
      const shouldShiftSameRow = Boolean(nextPosition && positionAnchor && !options?.position && !options?.detached)
      const currentForInsert = shouldShiftSameRow
        ? current.map((candidate, candidateIndex) => {
          const candidatePosition = workflowExplicitEditorPosition(candidate)
            || autoLayoutPositions?.get(candidate.id)
            || workflowEditorPosition(candidate, candidateIndex)
          if (
            Math.abs(candidatePosition.y - nextPosition!.y) > WORKFLOW_GRAPH_NODE_HEIGHT / 2 ||
            candidatePosition.x < nextPosition!.x - WORKFLOW_GRAPH_NODE_WIDTH / 2
          ) {
            return candidate
          }
          return workflowCloneEditorStep({
            ...candidate,
            ui: {
              ...(asWorkflowObject(candidate.ui) || {}),
              position: workflowNormalizeEditorPosition({
                x: candidatePosition.x + WORKFLOW_GRAPH_COLUMN_GAP,
                y: candidatePosition.y,
              }),
            },
          })
        })
        : current
      const insertAfterStep = previousStep || explicitAfterStep
      const insertIndex = insertAfterStep
        ? Math.max(0, currentForInsert.findIndex((candidate) => candidate.id === insertAfterStep.id)) + 1
        : currentForInsert.length
      setDetailStepId(id)
      return [
        ...currentForInsert.slice(0, insertIndex),
        step,
        ...currentForInsert.slice(insertIndex),
      ]
    })
    const scopeToOpen = options?.afterStepId
      ? workflowStepChildScopeId(draftSteps.find((step) => step.id === options.afterStepId))
        || workflowStringValue(draftSteps.find((step) => step.id === options.afterStepId)?.repeat_group_id)
      : activeTemplateScopeId !== WORKFLOW_TEMPLATE_ROOT_SCOPE_ID
      ? activeTemplateScopeId
      : ""
    if (scopeToOpen) {
      setCollapsedTemplateScopeIds((current) => {
        if (!current.has(scopeToOpen)) return current
        const next = new Set(current)
        next.delete(scopeToOpen)
        return next
      })
    }
    setDraftError(null)
  }, [activeTemplateScopeId, activeTemplateScopeTitle, detailStepId, draftSteps])

  const deleteDraftSteps = useCallback((stepIds: string[]) => {
    const requestedIds = Array.from(new Set(stepIds.filter(Boolean)))
    if (requestedIds.length === 0) return
    setDraftSteps((current) => {
      if (!requestedIds.some((stepId) => current.some((step) => step.id === stepId))) return current
      const deleteIds = new Set<string>()
      for (const stepId of requestedIds) {
        if (!current.some((step) => step.id === stepId)) continue
        workflowDescendantStepIds(current, stepId).forEach((id) => deleteIds.add(id))
        deleteIds.add(stepId)
      }
      if (deleteIds.size === 0) return current
      const next = current
        .filter((step) => !deleteIds.has(step.id))
        .map((step) => workflowCloneEditorStep({
          ...step,
          depends_on: workflowCleanIdList(step.depends_on).filter((dep) => !deleteIds.has(dep)),
          layout_after: workflowCleanIdList(step.layout_after).filter((dep) => !deleteIds.has(dep)),
        }))
      setDetailStepId((currentSelected) => currentSelected && deleteIds.has(currentSelected) ? null : currentSelected)
      return next
    })
    setCollapsedTemplateScopeIds((current) => {
      if (!requestedIds.some((stepId) => current.has(stepId))) return current
      const next = new Set(current)
      requestedIds.forEach((stepId) => next.delete(stepId))
      return next
    })
    setDraftError(null)
  }, [])

  const deleteDraftStep = useCallback((stepId: string) => {
    deleteDraftSteps([stepId])
  }, [deleteDraftSteps])

  const moveDraftStepScope = useCallback((stepId: string, scopeId: string) => {
    setDraftSteps((current) => {
      const step = current.find((item) => item.id === stepId)
      if (!step) return current
      const nextScopeId = scopeId === WORKFLOW_TEMPLATE_ROOT_SCOPE_ID ? "" : scopeId
      const descendants = workflowDescendantStepIds(current, stepId)
      if (nextScopeId && (nextScopeId === stepId || descendants.has(nextScopeId))) return current
      const scopeStep = nextScopeId ? current.find((item) => workflowStepChildScopeId(item) === nextScopeId || item.id === nextScopeId) : undefined
      const scopeLabel = scopeStep?.title || nextScopeId
      let changed = false
      const next = current.map((item) => {
        if (item.id !== stepId) return item
        if (workflowStringValue(item.repeat_group_id) === nextScopeId) return item
        changed = true
        return workflowCloneEditorStep({
          ...item,
          repeat_group_id: nextScopeId || undefined,
          repeat_group_label: nextScopeId ? scopeLabel : undefined,
        })
      })
      return changed ? next : current
    })
    if (scopeId !== WORKFLOW_TEMPLATE_ROOT_SCOPE_ID) {
      setCollapsedTemplateScopeIds((current) => {
        if (!current.has(scopeId)) return current
        const next = new Set(current)
        next.delete(scopeId)
        return next
      })
    }
    setDraftError(null)
  }, [])

  const moveDraftStep = useCallback((stepId: string, position: { x: number; y: number }) => {
    const nextPosition = workflowNormalizeEditorPosition(position)
    setDraftSteps((current) => {
      const visibleSteps = workflowTemplateVisibleSteps(current, collapsedTemplateScopeIds)
      const visibleIndexById = new Map(visibleSteps.map((step, index) => [step.id, index]))
      const layoutPositions = workflowGraphAutoLayout(visibleSteps)
      let changed = false
      const next = current.map((step, index) => {
        if (step.id !== stepId) return step
        const explicitPosition = workflowExplicitEditorPosition(step)
        const layoutPosition = layoutPositions.get(step.id) || workflowEditorPosition(step, visibleIndexById.get(step.id) ?? index)
        if (!explicitPosition && workflowEditorPositionsEqual(nextPosition, layoutPosition)) return step
        if (workflowEditorPositionsEqual(explicitPosition, nextPosition)) return step
        changed = true
        return workflowCloneEditorStep({
          ...step,
          ui: {
            ...(asWorkflowObject(step.ui) || {}),
            position: nextPosition,
          },
        })
      })
      return changed ? next : current
    })
    setDraftError(null)
  }, [collapsedTemplateScopeIds])

  const connectDraftSteps = useCallback((source: string, target: string) => {
    if (!source || !target || source === target) return
    const sourceIndex = draftSteps.findIndex((step) => step.id === source)
    const targetIndex = draftSteps.findIndex((step) => step.id === target)
    if (sourceIndex < 0 || targetIndex < 0 || sourceIndex >= targetIndex) {
      setDraftError("依赖只能连接到当前节点之前的节点。")
      return
    }
    setDraftSteps((current) => {
      let changed = false
      const next = current.map((step) => {
        if (step.id !== target) return step
        const deps = new Set(workflowCleanIdList(step.depends_on))
        if (deps.has(source)) return step
        deps.add(source)
        changed = true
        return workflowCloneEditorStep({ ...step, depends_on: Array.from(deps) })
      })
      return changed ? next : current
    })
    setDraftError(null)
  }, [draftSteps])

  const disconnectDraftSteps = useCallback((source: string, target: string) => {
    setDraftSteps((current) => {
      let changed = false
      const next = current.map((step) => {
        if (step.id !== target) return step
        const deps = workflowCleanIdList(step.depends_on)
        if (!deps.includes(source)) return step
        changed = true
        return workflowCloneEditorStep({ ...step, depends_on: deps.filter((dep) => dep !== source) })
      })
      return changed ? next : current
    })
    setDraftError(null)
  }, [])

  const autoLayoutDraftSteps = useCallback(() => {
    setDraftSteps((current) => {
      if (current.length === 0) return current
      const visibleSteps = workflowTemplateVisibleSteps(current, collapsedTemplateScopeIds)
      const visibleIds = new Set(visibleSteps.map((step) => step.id))
      const visibleIndexById = new Map(visibleSteps.map((step, index) => [step.id, index]))
      const layoutPositions = workflowGraphAutoLayout(visibleSteps)
      let changed = false
      const next = current.map((step, index) => {
        if (!visibleIds.has(step.id)) return step
        const layoutPosition = workflowNormalizeEditorPosition(
          layoutPositions.get(step.id) || { x: (visibleIndexById.get(step.id) ?? index) * WORKFLOW_GRAPH_COLUMN_GAP, y: 0 },
        )
        const explicitPosition = workflowExplicitEditorPosition(step)
        if (!explicitPosition || workflowEditorPositionsEqual(explicitPosition, layoutPosition)) return step
        changed = true
        return workflowCloneEditorStep({
          ...step,
          ui: {
            ...(asWorkflowObject(step.ui) || {}),
            position: layoutPosition,
          },
        })
      })
      return changed ? next : current
    })
    setDraftError(null)
  }, [collapsedTemplateScopeIds])

  const toggleTemplateStepScope = useCallback((stepId: string) => {
    const step = draftSteps.find((item) => item.id === stepId)
    const childScopeId = workflowStepChildScopeId(step)
    if (!childScopeId) return
    const childSteps = workflowTemplateScopeSteps(draftSteps, childScopeId)
    if (childSteps.length === 0) return
    setCollapsedTemplateScopeIds((current) => {
      const next = new Set(current)
      if (next.has(childScopeId)) next.delete(childScopeId)
      else next.add(childScopeId)
      return next
    })
    setDetailStepId(stepId)
  }, [draftSteps])

  const saveDraftWorkflow = useCallback(async () => {
    if (draftSteps.length === 0 || savingDraft) return
    setSavingDraft(true)
    setDraftError(null)
    try {
      const workflow = workflowAuthoringSpecFromSteps({
        id: draftWorkflowId || artifactPreview?.id || selected?.id || workflowName || "edited_workflow",
        name: workflowName || (draftWorkflowId ? "未命名流程" : selectedName),
        description: workflowDescription || (draftWorkflowId ? "" : selectedDescription),
        inputs: draftInputIds,
        requiredInputs: draftRequiredInputIds,
        steps: draftSteps,
        sourceWorkflow: draftWorkflowId ? undefined : artifactPreview?.workflow,
        inputSpecs: draftInputSpecs,
        workflowAdvanced,
      })
      const currentTemplateId = !artifactMode && !draftWorkflowId ? workflowStringValue(selected?.id) : ""
      if (currentTemplateId) {
        await onSaveWorkflowTemplate(workflow, {
          templateId: currentTemplateId,
          replaceExisting: true,
        })
      } else {
        await onSaveWorkflowSpec(workflow)
      }
      setDraftBaselineSignature(draftSignature)
    } catch (saveError) {
      setDraftError(saveError instanceof Error ? saveError.message : String(saveError))
    } finally {
      setSavingDraft(false)
    }
  }, [
    artifactPreview?.id,
    artifactMode,
    draftWorkflowId,
    draftInputIds,
    draftInputSpecs,
    draftRequiredInputIds,
    draftSteps,
    workflowAdvanced,
    onSaveWorkflowSpec,
    onSaveWorkflowTemplate,
    savingDraft,
    artifactPreview?.workflow,
    selected?.id,
    selectedDescription,
    selectedName,
    draftSignature,
    workflowDescription,
    workflowName,
  ])

  const buildDraftWorkflowForSave = useCallback(() => workflowAuthoringSpecFromSteps({
    id: draftWorkflowId || artifactPreview?.id || selected?.id || workflowName || "edited_workflow",
    name: workflowName || (draftWorkflowId ? "未命名流程" : selectedName),
    description: workflowDescription || (draftWorkflowId ? "" : selectedDescription),
    inputs: draftInputIds,
    requiredInputs: draftRequiredInputIds,
    steps: draftSteps,
    sourceWorkflow: draftWorkflowId ? undefined : artifactPreview?.workflow,
    inputSpecs: draftInputSpecs,
    workflowAdvanced,
  }), [
    artifactPreview?.id,
    artifactPreview?.workflow,
    draftInputIds,
    draftInputSpecs,
    draftRequiredInputIds,
    draftSteps,
    draftWorkflowId,
    selected?.id,
    selectedDescription,
    selectedName,
    workflowAdvanced,
    workflowDescription,
    workflowName,
  ])

  const saveDraftWorkflowAsTemplate = useCallback(async () => {
    if (draftSteps.length === 0 || savingTemplate) return
    setSavingTemplate(true)
    setDraftError(null)
    try {
      const workflow = buildDraftWorkflowForSave()
      await onSaveWorkflowTemplate(workflow, {
        templateId: String(workflow.id || ""),
        replaceExisting: false,
      })
      setDraftBaselineSignature(draftSignature)
    } catch (saveError) {
      setDraftError(saveError instanceof Error ? saveError.message : String(saveError))
    } finally {
      setSavingTemplate(false)
    }
  }, [
    buildDraftWorkflowForSave,
    draftSignature,
    draftSteps.length,
    onSaveWorkflowTemplate,
    savingTemplate,
  ])

  const downloadSelectedTemplate = useCallback(async () => {
    if (!selected || !selected.downloadable || downloadingTemplate) return
    setDownloadingTemplate(true)
    setDraftError(null)
    try {
      await onDownloadWorkflowTemplate(selected)
    } catch (error) {
      setDraftError(error instanceof Error ? error.message : String(error))
    } finally {
      setDownloadingTemplate(false)
    }
  }, [downloadingTemplate, onDownloadWorkflowTemplate, selected])

  const restoreSelectedBuiltinTemplate = useCallback(async () => {
    if (!selected?.overrides_builtin || restoringBuiltinTemplate) return
    setRestoreBuiltinConfirmOpen(false)
    setRestoringBuiltinTemplate(true)
    setDraftError(null)
    try {
      await onRestoreBuiltinTemplate(selected)
    } catch (error) {
      setDraftError(error instanceof Error ? error.message : String(error))
    } finally {
      setRestoringBuiltinTemplate(false)
    }
  }, [onRestoreBuiltinTemplate, restoringBuiltinTemplate, selected])

  useEffect(() => {
    setRestoreBuiltinConfirmOpen(false)
  }, [selected?.id])

  const pickImportSpecFile = () => {
    if (typeof document === "undefined" || typeof window === "undefined") return
    const input = document.createElement("input")
    input.type = "file"
    input.accept = "application/json,.json"
    input.style.position = "fixed"
    input.style.left = "-10000px"
    input.style.top = "-10000px"
    input.style.opacity = "0"
    input.style.pointerEvents = "none"
    const cleanup = () => {
      window.setTimeout(() => input.remove(), 0)
    }
    input.addEventListener("change", () => {
      const file = input.files?.[0]
      cleanup()
      if (file) onImportSpecFile(file)
    }, { once: true })
    input.addEventListener("cancel", cleanup, { once: true })
    document.body.appendChild(input)
    input.click()
  }

  useEffect(() => {
    if (detailStepId && !displayedSteps.some((step) => step.id === detailStepId)) {
      setDetailStepId(null)
    }
  }, [detailStepId, displayedSteps])

  return (
    <section className="flex h-full w-full flex-col overflow-hidden bg-[#10151d] text-zinc-100">
      <div className="flex min-h-12 items-center gap-2 border-b border-white/10 px-3 py-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <div className="flex h-8 shrink-0 items-center rounded-md border border-cyan-200/18 bg-cyan-300/[0.06] px-3 text-xs font-semibold text-cyan-100">
            搭建流程
          </div>
          {draftWorkflowId ? (
            <span className="flex h-8 w-[120px] items-center rounded-md border border-cyan-200/18 bg-cyan-300/[0.06] px-2 text-xs font-semibold text-cyan-100">
              新建流程
            </span>
          ) : (
            <select
              value={selected?.id || ""}
              onChange={(event) => onSelectedIdChange(event.target.value)}
              disabled={artifactMode || loading || templates.length === 0}
              className="h-8 w-[220px] rounded-md border border-white/10 bg-black/30 px-2 text-xs text-zinc-100 outline-none transition focus:border-cyan-300/45 disabled:opacity-55"
            >
              {templates.length === 0 ? (
                <option value="">暂无流程</option>
              ) : (
                templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.scope === "user" ? "我的 · " : "内置 · "}{template.name || template.id}
                  </option>
                ))
              )}
            </select>
          )}
          <input
            value={workflowName}
            onChange={(event) => updateWorkflowName(event.target.value)}
            placeholder="流程名称"
            className="h-8 min-w-[180px] flex-1 rounded-md border border-white/10 bg-black/30 px-2 text-xs font-semibold text-zinc-100 outline-none transition placeholder:text-zinc-600 focus:border-cyan-300/45"
          />
          <span className="hidden shrink-0 text-[10px] text-zinc-600 xl:block">
            {activeTemplateScopeTitle} · {templateDisplaySteps.length} 个步骤
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={createBlankWorkflow}
            className="hidden h-8 rounded-md border border-cyan-200/20 bg-cyan-300/10 px-2 text-[11px] font-semibold text-cyan-100 transition hover:bg-cyan-300/16 sm:inline-flex sm:items-center"
          >
            新建流程
          </button>
          <button
            type="button"
            onClick={pickImportSpecFile}
            className="hidden h-8 rounded-md border border-white/10 px-2 text-[11px] text-zinc-300 transition hover:bg-white/[0.06] sm:inline-flex sm:items-center"
          >
            导入流程
          </button>
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="hidden h-8 rounded-md border border-white/10 px-2 text-[11px] text-zinc-300 transition hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-50 sm:inline-flex sm:items-center"
          >
            {loading ? "读取中" : "刷新"}
          </button>
          <button
            type="button"
            onClick={resetDraftWorkflow}
            disabled={!draftDirty && !hasMediaModelOverrides}
            className="hidden h-8 rounded-md border border-amber-200/20 bg-amber-300/10 px-2 text-[11px] font-semibold text-amber-100 transition hover:bg-amber-300/16 disabled:cursor-not-allowed disabled:opacity-45 sm:inline-flex sm:items-center"
            title="恢复到当前模板打开时的内容"
          >
            重置
          </button>
          <button
            type="button"
            onClick={() => void saveDraftWorkflow()}
            disabled={!draftDirty || savingDraft || draftSteps.length === 0}
            className="h-8 rounded-md border border-emerald-200/25 bg-emerald-300/10 px-3 text-xs font-semibold text-emerald-100 transition hover:bg-emerald-300/16 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {savingDraft ? "保存中" : draftDirty ? "保存流程" : "已保存"}
          </button>
          <button
            type="button"
            onClick={() => void saveDraftWorkflowAsTemplate()}
            disabled={savingTemplate || draftSteps.length === 0}
            className="h-8 rounded-md border border-sky-200/25 bg-sky-300/10 px-3 text-xs font-semibold text-sky-100 transition hover:bg-sky-300/16 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {savingTemplate ? "保存中" : !artifactMode && !draftWorkflowId ? "另存副本" : "保存为模板"}
          </button>
          <button
            type="button"
            onClick={() => void downloadSelectedTemplate()}
            disabled={!selected?.downloadable || downloadingTemplate}
            className="hidden h-8 rounded-md border border-white/10 px-2 text-[11px] text-zinc-300 transition hover:bg-white/[0.06] disabled:cursor-not-allowed disabled:opacity-45 sm:inline-flex sm:items-center"
            title={selected?.downloadable ? "下载当前用户模板 JSON" : "内置流程需先保存为模板后下载"}
          >
            {downloadingTemplate ? "下载中" : "下载模板"}
          </button>
          {selected?.overrides_builtin && (
            <div className="relative hidden sm:block">
              <button
                type="button"
                onClick={() => setRestoreBuiltinConfirmOpen((current) => !current)}
                disabled={restoringBuiltinTemplate || savingDraft || savingTemplate}
                className="inline-flex h-8 items-center rounded-md border border-amber-200/25 bg-amber-300/10 px-2 text-[11px] font-semibold text-amber-100 transition hover:bg-amber-300/16 disabled:cursor-not-allowed disabled:opacity-45"
                title="删除当前用户覆盖版本并恢复只读的内置原版"
              >
                {restoringBuiltinTemplate ? "恢复中" : "恢复内置"}
              </button>
              {restoreBuiltinConfirmOpen && (
                <div className="absolute right-0 top-10 z-[90] w-72 rounded-lg border border-amber-200/20 bg-[#151a22] p-3 shadow-2xl shadow-black/55">
                  <div className="text-xs font-semibold text-zinc-100">恢复内置原版？</div>
                  <div className="mt-1.5 text-[11px] leading-5 text-zinc-400">
                    当前用户修改版会被删除，“{selected.name || selected.id}”将恢复为系统内置内容。
                  </div>
                  <div className="mt-3 flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setRestoreBuiltinConfirmOpen(false)}
                      className="h-7 rounded-md border border-white/10 px-2.5 text-[11px] text-zinc-300 transition hover:bg-white/[0.06]"
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      onClick={() => void restoreSelectedBuiltinTemplate()}
                      className="h-7 rounded-md bg-amber-300 px-2.5 text-[11px] font-semibold text-amber-950 transition hover:bg-amber-200"
                    >
                      确认恢复
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {artifactMode && (
        <div className="flex items-center justify-between gap-2 border-t border-white/[0.08] bg-cyan-300/[0.05] px-3 py-2">
          <div className="min-w-0 truncate text-[11px] text-cyan-100/80">
            {artifactPreview?.source === "imported" ? "已导入流程，可在这里编辑；运行请回到画布流程托盘。" : "已生成流程，可在这里编辑；运行请回到画布流程托盘。"}
          </div>
          <button
            type="button"
            onClick={onClearArtifactPreview}
            className="shrink-0 rounded-md border border-white/10 px-2 py-1 text-[10px] text-zinc-300 transition hover:bg-white/[0.06]"
          >
            返回流程
          </button>
        </div>
      )}

      {(error || draftError || draftDirty) && (
        <div className="border-t border-red-400/15 bg-red-500/10 px-3 py-2 text-[11px] leading-4 text-red-200">
          {draftError || error || "流程有未保存修改，保存后再运行。"}
        </div>
      )}

      <div className="flex min-h-0 flex-1 border-t border-white/[0.08]">
        {toolboxOpen && (
        <aside className="flex w-[260px] shrink-0 flex-col border-r border-white/[0.08] bg-[#0b1017]">
          <div className="border-b border-white/[0.08] px-3 py-3">
            <div className="flex items-end justify-between gap-2">
              <div>
                <div className="text-xs font-semibold tracking-wide text-zinc-100">步骤库</div>
                <div className="mt-0.5 text-[10px] text-zinc-600">点选添加，或用节点右侧 + 接下一步</div>
              </div>
              <span className="rounded-full border border-white/[0.07] bg-white/[0.035] px-2 py-0.5 text-[9px] text-zinc-500">
                {visibleProcessNodeTypes.length + visibleProductNodeTypes.length}
              </span>
            </div>
            <input
              value={paletteSearch}
              onChange={(event) => setPaletteSearch(event.target.value)}
              placeholder="搜索步骤"
              className="mt-2.5 h-8 w-full rounded-lg border border-white/[0.08] bg-black/30 px-2.5 text-xs text-zinc-100 outline-none transition placeholder:text-zinc-600 focus:border-cyan-200/45 focus:bg-black/45"
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2.5">
            <div className="grid gap-2.5">
              <div className="rounded-xl border border-white/[0.06] bg-white/[0.018] p-2.5">
                <div className="mb-2 flex items-center justify-between gap-2 px-0.5">
                  <span className="text-[10px] font-semibold tracking-wide text-zinc-400">处理步骤</span>
                  <span className="text-[9px] text-zinc-600">中间计算</span>
                </div>
                <div className="grid grid-cols-2 gap-1.5">
                  {visibleProcessNodeTypes.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => addDraftStep(item)}
                      className="group min-h-[66px] rounded-lg border border-white/[0.07] bg-[#10161f] px-2 py-2 text-left transition hover:-translate-y-px hover:border-sky-200/25 hover:bg-sky-300/[0.05] hover:shadow-[0_8px_20px_rgba(0,0,0,0.22)]"
                    >
                      <span className="mb-1 flex h-5 w-5 items-center justify-center rounded-md border border-sky-200/10 bg-sky-300/[0.06] text-[9px] font-bold text-sky-100/75">
                        {String(item.title || "步").slice(0, 1)}
                      </span>
                      <span className="block truncate text-[11px] font-semibold text-zinc-100 group-hover:text-sky-50">{item.title}</span>
                      <span className="mt-0.5 block truncate text-[9px] text-zinc-600">{item.description}</span>
                    </button>
                  ))}
                </div>
              </div>
              <div className="rounded-xl border border-cyan-200/[0.12] bg-gradient-to-b from-cyan-300/[0.045] to-cyan-300/[0.015] p-2.5 shadow-[inset_0_1px_0_rgba(103,232,249,0.08)]">
                <div className="mb-2 flex items-center justify-between gap-2 px-0.5">
                  <span className="text-[10px] font-semibold tracking-wide text-cyan-100/80">画布产物</span>
                  <span className="text-[9px] text-cyan-100/45">用户可见节点</span>
                </div>
                <div className="grid grid-cols-2 gap-1.5">
                  {visibleProductNodeTypes.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => addDraftStep(item)}
                      className="group min-h-[66px] rounded-lg border border-cyan-200/[0.13] bg-[#07151d]/88 px-2 py-2 text-left transition hover:-translate-y-px hover:border-cyan-200/40 hover:bg-cyan-300/[0.08] hover:shadow-[0_9px_22px_rgba(6,182,212,0.08)]"
                    >
                      <span className="mb-1 flex h-5 w-5 items-center justify-center rounded-md border border-cyan-200/18 bg-cyan-300/[0.11] text-[9px] font-bold text-cyan-50">
                        {String(item.title || "产").slice(0, 1)}
                      </span>
                      <span className="block truncate text-[11px] font-semibold text-cyan-50">{item.title}</span>
                      <span className="mt-0.5 block truncate text-[9px] text-cyan-100/42">{item.description}</span>
                    </button>
                  ))}
                </div>
              </div>
              {nodeTypesError ? (
                <div className="rounded-md border border-red-300/20 bg-red-500/10 px-3 py-2 text-[11px] text-red-200">{nodeTypesError}</div>
              ) : null}
              {visibleNodeTypeGroups.length > 0 && (
                <details className="rounded-md border border-white/[0.06] bg-white/[0.025]">
                  <summary className="cursor-pointer px-2 py-2 text-[10px] font-semibold text-zinc-500 hover:text-zinc-300">
                    更多步骤
                  </summary>
                  <div className="grid gap-2 border-t border-white/[0.06] p-2">
                    {visibleNodeTypeGroups.map(([category, items]) => (
                      <div key={category}>
                        <div className="mb-1.5 text-[10px] font-semibold text-zinc-500">{workflowNodeTypeCategoryLabel(category)}</div>
                        <div className="grid gap-1.5">
                          {items.slice(0, 8).map((item) => (
                            <button
                              key={item.id}
                              type="button"
                              onClick={() => addDraftStep(item)}
                              className="min-h-12 rounded-md border border-white/[0.08] bg-black/24 px-2 py-1.5 text-left transition hover:border-cyan-200/30 hover:bg-cyan-300/[0.045]"
                              title={item.description || item.title}
                            >
                              <span className="block truncate text-[11px] font-semibold text-zinc-100">{item.title || item.name || item.type}</span>
                              <span className="mt-0.5 block truncate text-[9px] text-zinc-500">
                                {item.plugin_name ? `插件 · ${item.plugin_name}` : "扩展步骤"}
                              </span>
                            </button>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              )}
              {visibleProcessNodeTypes.length === 0 && visibleProductNodeTypes.length === 0 && visibleNodeTypeGroups.length === 0 && !nodeTypesError && (
                <div className="rounded-md border border-white/[0.08] bg-black/18 px-3 py-2 text-[11px] text-zinc-500">没有匹配的步骤</div>
              )}
            </div>
          </div>
        </aside>
        )}
        <main className="relative min-w-0 flex-1 bg-[#080d14]">
          <div className="absolute left-3 top-3 z-20 flex max-w-[calc(100%-24px)] items-center gap-2 rounded-md border border-white/10 bg-[#10151d]/88 px-2 py-1.5 text-[10px] text-zinc-500 shadow-xl shadow-black/25 backdrop-blur">
            <span className="flex min-w-0 shrink-0 items-center gap-1 text-zinc-300">
              <span className="max-w-[150px] truncate rounded bg-white/[0.06] px-1.5 py-0.5 text-zinc-100" title={activeTemplateScopeTitle}>
                {activeTemplateScopeTitle}
              </span>
            </span>
            <span>步骤 {displayedSteps.length}</span>
            <button
              type="button"
              onClick={() => {
                setDetailStepId(null)
                setInspectorOpen(true)
              }}
              className={cn(
                "flex h-6 items-center gap-1.5 rounded border px-2 text-[10px] font-semibold transition",
                detailStepId === null
                  ? "border-amber-200/35 bg-amber-300/12 text-amber-100"
                  : "border-amber-200/16 bg-amber-300/[0.055] text-amber-100/75 hover:border-amber-200/30 hover:bg-amber-300/10",
              )}
              title="打开流程名称、说明和输入字段定义"
            >
              <span>流程设置</span>
              <span className="font-normal text-amber-100/55">输入字段 {draftInputIds.length}</span>
            </button>
            {displayedSteps.length > 1 && (
              <button
                type="button"
                onClick={autoLayoutDraftSteps}
                className="h-6 rounded border border-cyan-200/20 bg-cyan-300/10 px-2 text-[10px] font-semibold text-cyan-100 transition hover:bg-cyan-300/16"
                title="按依赖关系重新排布流程节点"
              >
                自动对齐
              </button>
            )}
            <span className="h-4 w-px bg-white/[0.08]" />
            <button
              type="button"
              onClick={() => setToolboxOpen((current) => !current)}
              className={cn(
                "h-6 rounded border px-2 text-[10px] font-semibold transition",
                toolboxOpen
                  ? "border-white/[0.09] bg-white/[0.055] text-zinc-300"
                  : "border-cyan-200/22 bg-cyan-300/[0.08] text-cyan-100",
              )}
              title={toolboxOpen ? "收起步骤库，扩大流程画布" : "打开步骤库"}
            >
              步骤库
            </button>
            <button
              type="button"
              onClick={() => setInspectorOpen((current) => !current)}
              className={cn(
                "h-6 rounded border px-2 text-[10px] font-semibold transition",
                inspectorOpen
                  ? "border-white/[0.09] bg-white/[0.055] text-zinc-300"
                  : "border-cyan-200/22 bg-cyan-300/[0.08] text-cyan-100",
              )}
              title={inspectorOpen ? "收起详情栏，扩大流程画布" : "打开详情栏"}
            >
              详情
            </button>
          </div>
          {editorDisplayedSteps.length > 0 ? (
            <WorkflowEditorGraph
              steps={editorDisplayedSteps}
              nodeStates={nodeStates}
              selectedStepId={detailStepId || (inspectorOpen ? WORKFLOW_EDITOR_INPUT_STEP_ID : "")}
              onSelectStep={(stepId) => {
                if (stepId === WORKFLOW_EDITOR_INPUT_STEP_ID) {
                  setDetailStepId(null)
                  setInspectorOpen(true)
                  return
                }
                setDetailStepId(stepId)
                setInspectorOpen(Boolean(stepId))
              }}
              onRunStep={onRunStep}
              onMoveStep={moveDraftStep}
              onConnectSteps={(source, target) => {
                if (source === WORKFLOW_EDITOR_INPUT_STEP_ID || target === WORKFLOW_EDITOR_INPUT_STEP_ID) return
                connectDraftSteps(source, target)
              }}
              onDisconnectSteps={(source, target) => {
                if (source === WORKFLOW_EDITOR_INPUT_STEP_ID || target === WORKFLOW_EDITOR_INPUT_STEP_ID) return
                disconnectDraftSteps(source, target)
              }}
              onToggleStepScope={toggleTemplateStepScope}
              onCreateStep={addDraftStep}
              onDeleteSteps={deleteDraftSteps}
              insertNodeTypes={coreNodeTypes}
              collapsedScopeIds={collapsedTemplateScopeIds}
              scopeChildCounts={templateScopeChildCounts}
              runningStepIds={runningStepIds}
              disabledRun={false}
              editable={true}
              showRunButton={false}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-zinc-500">
              从左侧工具箱添加步骤
            </div>
          )}
        </main>
        {inspectorOpen && (
          <WorkflowStepInspector
            step={detailStep}
            steps={draftSteps}
            nodeState={undefined}
            running={Boolean(detailStep && (runningSet.has(detailStep.id) || nodeStates[detailStep.id]?.status === "running"))}
            workflowName={workflowName}
            workflowDescription={workflowDescription}
            workflowAdvanced={workflowAdvanced}
            nodeTypes={nodeTypes}
            mediaProviders={workflowMediaProviders}
            videoProtocols={workflowVideoProtocols}
            mediaConfigError={workflowMediaConfigError}
            mediaModelOverrides={mediaModelOverrides}
            readOnly={false}
            showRunButton={false}
            inputIds={draftInputIds}
            inputSpecs={draftInputSpecs}
            inputValues={inputValues}
            requiredInputIds={draftRequiredInputIds}
            missingRequiredInputIds={missingRequiredInputs}
            onWorkflowNameChange={updateWorkflowName}
            onWorkflowDescriptionChange={updateWorkflowDescription}
            onWorkflowAdvancedChange={updateWorkflowAdvanced}
            onAddWorkflowInput={addDraftInput}
            onAddWorkflowInputPreset={addDraftInputPreset}
            onRenameWorkflowInput={renameDraftInput}
            onDeleteWorkflowInput={deleteDraftInput}
            onToggleWorkflowInputRequired={toggleDraftInputRequired}
            onUpdateWorkflowInputSpec={updateDraftInputSpec}
            onInputValueChange={onInputValueChange}
            onMediaModelOverrideChange={onMediaModelOverrideChange}
            onRunStep={onRunStep}
            onUpdateStep={updateDraftStep}
            onRenameStep={renameDraftStep}
            onMoveStepScope={moveDraftStepScope}
            onDeleteStep={deleteDraftStep}
          />
        )}
      </div>
    </section>
  )
}

function menuPositionStyle(x: number, y: number, width: number, height: number) {
  if (typeof window === "undefined") return { left: x, top: y }
  const margin = 10
  return {
    left: Math.max(margin, Math.min(x, window.innerWidth - width - margin)),
    top: Math.max(margin, Math.min(y, window.innerHeight - height - margin)),
  }
}

function isTouchPointer(event: ReactPointerEvent) {
  return event.pointerType === "touch" || event.pointerType === "pen"
}

function touchPoint(event: ReactTouchEvent<HTMLDivElement>) {
  const touch = event.changedTouches[0] || event.touches[0]
  return touch ? { id: touch.identifier, x: touch.clientX, y: touch.clientY } : null
}

function isInteractiveTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false
  return Boolean(target.closest(
    "button,a,input,textarea,select,[contenteditable='true'],.nodrag,.nowheel,.openreel-node-detail-panel,.openreel-node-preview-card,.openreel-video-edit-panel,.openreel-canvas-action-menu,.react-flow__handle,.react-flow__controls,.react-flow__minimap",
  ))
}

function isCanvasBlankTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false
  if (isInteractiveTarget(target)) return false
  if (target.closest("[data-openreel-workflow-ui='true'],[data-openreel-group-toolbar='true']")) return false
  if (target.closest(".react-flow__node,.react-flow__edge,.react-flow__handle")) return false
  return Boolean(target.closest(".react-flow__pane,.react-flow__viewport,.react-flow__renderer"))
}

function isVideoUrl(value: unknown): value is string {
  return typeof value === "string" && /\.(mp4|webm|mov)(?:\?|#|$)/i.test(value)
}

function isPersistedEdgeId(edgeId: string): boolean {
  return Boolean(edgeId) && !edgeId.startsWith("manual-") && !edgeId.startsWith("dep-")
}

function isImageStageName(name: unknown): boolean {
  return /图|首帧|尾帧|模板|参考|image|storyboard/i.test(String(name ?? "")) && !/提示词|prompt|视频|video|clip/i.test(String(name ?? ""))
}

function imageDownloadUrlFromNode(node: FlowNode | undefined): string | null {
  const data = node?.data as { type?: string; preview?: Record<string, unknown> } | undefined
  if (data?.type !== "image") return null
  const preview = data.preview
  if (!preview) return null
  if (preview.type === "fusion" && Array.isArray(preview.stages)) {
    const stage = (preview.stages as Record<string, unknown>[]).find((item) => {
      const src = item.local_url || item.url || item.remote_url || item.composite_url
      return isImageStageName(item.name) && typeof src === "string" && !isVideoUrl(src)
    })
    const src = stage ? stage.local_url || stage.url || stage.remote_url || stage.composite_url : null
    return typeof src === "string" ? resolveMediaUrl(src) : null
  }
  const src = preview.local_url || preview.url || preview.composite_url || preview.remote_url
  return typeof src === "string" && !isVideoUrl(src) ? resolveMediaUrl(src) : null
}

function previewObject(value: unknown): Record<string, unknown> | null {
  if (!value) return null
  if (typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
  if (typeof value !== "string") return null
  try {
    const parsed = JSON.parse(value)
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function previewString(value: unknown): string {
  return typeof value === "string" && value.trim()
    ? value.trim()
    : typeof value === "number" || typeof value === "boolean"
      ? String(value)
      : ""
}

function previewUrlFromObject(obj: Record<string, unknown> | null | undefined, keys = ["local_url", "url", "remote_url", "composite_url"]): string {
  if (!obj) return ""
  for (const key of keys) {
    const value = previewString(obj[key])
    if (value) return resolveMediaUrl(value)
  }
  return ""
}

function previewImageUrlFromNode(node: FlowNode | undefined): string {
  const fromPreview = imageDownloadUrlFromNode(node)
  if (fromPreview) return fromPreview
  const data = node?.data as { type?: string; output?: unknown } | undefined
  if (data?.type !== "image") return ""
  const output = previewObject(data.output)
  return previewUrlFromObject(output)
}

function previewVideoFromNode(node: FlowNode | undefined): { src: string; poster?: string } | null {
  const data = node?.data as { type?: string; preview?: Record<string, unknown>; output?: unknown } | undefined
  if (data?.type !== "video") return null
  const candidates = [data.preview, previewObject(data.output)].filter(Boolean) as Record<string, unknown>[]
  for (const item of candidates) {
    if (item.type === "fusion" && Array.isArray(item.stages)) {
      const stage = (item.stages as Record<string, unknown>[]).find((stageItem) => (
        /视频|video|clip/i.test(String(stageItem.name ?? "")) &&
        previewUrlFromObject(stageItem, ["local_url", "url", "remote_url"])
      ))
      const src = previewUrlFromObject(stage, ["local_url", "url", "remote_url"])
      if (src) return { src, poster: previewUrlFromObject(stage, ["poster", "thumbnail_url"]) || undefined }
    }
    const src = previewUrlFromObject(item, ["local_url", "url", "remote_url"])
    if (src && (item.type === "video" || isVideoUrl(src))) {
      return { src, poster: previewUrlFromObject(item, ["poster", "thumbnail_url"]) || undefined }
    }
  }
  return null
}

function isAudioUrl(value: unknown): value is string {
  return typeof value === "string" && /\.(mp3|wav|m4a|aac|ogg|flac)(?:\?|#|$)/i.test(value)
}

function previewAudioFromNode(node: FlowNode | undefined): { src: string } | null {
  const data = node?.data as { type?: string; preview?: Record<string, unknown>; output?: unknown } | undefined
  if (data?.type !== "audio") return null
  const candidates = [data.preview, previewObject(data.output)].filter(Boolean) as Record<string, unknown>[]
  for (const item of candidates) {
    if (item.type === "fusion" && Array.isArray(item.stages)) {
      const stage = (item.stages as Record<string, unknown>[]).find((stageItem) => (
        /音频|audio|sound|music/i.test(String(stageItem.name ?? "")) &&
        previewUrlFromObject(stageItem, ["local_url", "url", "remote_url"])
      ))
      const src = previewUrlFromObject(stage, ["local_url", "url", "remote_url"])
      if (src) return { src }
    }
    const src = previewUrlFromObject(item, ["local_url", "url", "remote_url"])
    if (src && (item.type === "audio" || isAudioUrl(src))) return { src }
  }
  return null
}

function mediaOperationSourceNodeIdFromNode(node: FlowNode | undefined): string | undefined {
  const data = node?.data as { input?: unknown } | undefined
  const input = asWorkflowObject(data?.input)
  const fields = asWorkflowObject(input?.fields)
  const candidates = [
    asWorkflowObject(fields?.media_operation),
    asWorkflowObject(input?.source),
    input,
  ].filter(Boolean) as Record<string, unknown>[]

  for (const candidate of candidates) {
    const direct = previewString(candidate.source_node_id) || previewString(candidate.sourceNodeId)
    if (direct) return direct
    const ids = candidate.source_node_ids || candidate.sourceNodeIds
    if (Array.isArray(ids)) {
      const first = ids.map(previewString).find(Boolean)
      if (first) return first
    }
  }
  return undefined
}

function previewInputFields(value: unknown): Record<string, unknown> {
  const input = previewObject(value) || {}
  const fields = previewObject(input.fields)
  return fields ? { ...input, ...fields } : input
}

function previewFirstString(...values: unknown[]): string {
  for (const value of values) {
    const text = previewString(value)
    if (text) return text
  }
  return ""
}

function previewImageStageFromPreview(preview: Record<string, unknown> | null | undefined): Record<string, unknown> {
  if (!preview) return {}
  if (preview.type === "fusion" && Array.isArray(preview.stages)) {
    const stage = (preview.stages as Record<string, unknown>[]).find((item) => {
      const src = item.local_url || item.url || item.remote_url
      return isImageStageName(item.name) && typeof src === "string" && !isVideoUrl(src)
    })
    return stage || {}
  }
  return preview
}

function previewImageInfoFromNode(node: FlowNode): {
  prompt: string
  resolution: string
  aspect: string
  quality: string
  clarity: string
  model: string
  provider: string
  size: string
} {
  const data = node.data as { input?: unknown; output?: unknown; prompt?: string; preview?: Record<string, unknown> } | undefined
  const input = previewInputFields(data?.input)
  const output = previewObject(data?.output) || {}
  const preview = data?.preview || {}
  const stage = previewImageStageFromPreview(preview)
  const width = previewFirstString(stage.width, output.width, preview.width)
  const height = previewFirstString(stage.height, output.height, preview.height)
  const size = width && height ? `${width} x ${height}` : ""
  return {
    prompt: previewFirstString(data?.prompt, input.prompt, input.image_prompt, output.prompt, output.image_prompt, stage.prompt, preview.prompt),
    resolution: previewFirstString(stage.size_final, stage.size, stage.resolution, output.size_final, output.resolution, output.size, input.resolution, input.size, preview.resolution, preview.size),
    aspect: previewFirstString(stage.aspect_ratio, output.aspect_ratio, input.aspect_ratio, preview.aspect_ratio),
    quality: previewFirstString(stage.quality, output.quality, input.quality, preview.quality),
    clarity: previewFirstString(output.clarity, input.clarity, preview.clarity),
    model: previewFirstString(stage.model, output.model, input.model, preview.model),
    provider: previewFirstString(stage.provider, output.provider, preview.provider),
    size,
  }
}

function previewStageFromPreview(preview: Record<string, unknown> | null | undefined, type: string): Record<string, unknown> {
  if (!preview || !Array.isArray(preview.stages)) return preview || {}
  const matcher = type === "video"
    ? /视频|video|clip/i
    : type === "audio"
      ? /音频|audio|sound|music/i
      : /图|首帧|尾帧|模板|参考|image|storyboard/i
  const stage = (preview.stages as Record<string, unknown>[]).find((item) => {
    const src = previewUrlFromObject(item, ["local_url", "url", "remote_url", "composite_url"])
    return matcher.test(String(item.name ?? "")) && Boolean(src)
  })
  return stage || preview
}

function previewInfoFromNode(node: FlowNode, type: string): {
  prompt: string
  resolution: string
  aspect: string
  quality: string
  clarity: string
  model: string
  provider: string
  size: string
  duration: string
  format: string
} {
  if (type === "image") {
    return { ...previewImageInfoFromNode(node), duration: "", format: "" }
  }
  const data = node.data as { input?: unknown; output?: unknown; prompt?: string; preview?: Record<string, unknown> } | undefined
  const input = previewInputFields(data?.input)
  const output = previewObject(data?.output) || {}
  const preview = data?.preview || {}
  const stage = previewStageFromPreview(preview, type)
  const width = previewFirstString(stage.width, output.width, preview.width)
  const height = previewFirstString(stage.height, output.height, preview.height)
  const size = width && height ? `${width} x ${height}` : ""
  return {
    prompt: previewFirstString(
      data?.prompt,
      input.prompt,
      input.video_prompt,
      input.audio_prompt,
      input.text_prompt,
      output.prompt,
      output.video_prompt,
      output.audio_prompt,
      output.input,
      stage.prompt,
      preview.prompt,
    ),
    resolution: previewFirstString(stage.size_final, stage.size, stage.resolution, output.size_final, output.resolution, output.size, input.resolution, input.size, preview.resolution, preview.size),
    aspect: previewFirstString(stage.aspect_ratio, output.aspect_ratio, input.aspect_ratio, preview.aspect_ratio),
    quality: previewFirstString(stage.quality, output.quality, input.quality, preview.quality),
    clarity: previewFirstString(output.clarity, input.clarity, preview.clarity),
    model: previewFirstString(stage.model, output.model, input.model, preview.model),
    provider: previewFirstString(stage.provider, output.provider, input.provider, preview.provider),
    size,
    duration: previewFirstString(stage.duration_seconds, output.duration_seconds, input.duration_seconds, preview.duration_seconds),
    format: previewFirstString(stage.format, output.format, input.format, preview.format),
  }
}

function previewSpecEntries(info: ReturnType<typeof previewInfoFromNode>, type: string): Array<[string, string]> {
  return [
    ["分辨率", info.resolution],
    [type === "audio" ? "时长" : "尺寸", type === "audio" ? info.duration : info.size],
    ["比例", info.aspect],
    ["格式", info.format],
    ["画质", info.quality],
    ["清晰度", info.clarity],
    ["模型", info.model],
    ["服务", info.provider],
  ].filter((item): item is [string, string] => Boolean(item[1]))
}

function PreviewSpecRail({ entries }: { entries: Array<[string, string]> }) {
  if (entries.length === 0) return null
  return (
    <div className="flex min-h-8 items-center gap-2 overflow-x-auto rounded-xl bg-white/[0.035] px-2.5 py-1.5 shadow-inner shadow-black/20 [scrollbar-width:none]">
      {entries.map(([label, value]) => (
        <span key={`${label}:${value}`} className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-black/30 px-2.5 py-1 text-[10px] leading-none text-zinc-400 ring-1 ring-white/[0.055]">
          <span className="font-medium text-zinc-500">{label}</span>
          <span className="font-semibold text-zinc-100">{value}</span>
        </span>
      ))}
    </div>
  )
}

function PreviewPromptPanel({
  title = "生成提示词",
  prompt,
  note,
}: {
  title?: string
  prompt?: string
  note?: string
}) {
  return (
    <aside className="min-h-0 overflow-hidden rounded-xl bg-[#10141b]/92 shadow-[0_18px_50px_rgba(0,0,0,0.28)] ring-1 ring-white/[0.08]">
      <div className="flex h-11 items-center justify-between border-b border-white/[0.06] px-4">
        <div className="text-[11px] font-semibold tracking-wide text-zinc-300">{title}</div>
        <span className="h-1.5 w-1.5 rounded-full bg-cyan-200/75" />
      </div>
      <div className="max-h-[calc(100dvh-212px)] overflow-auto px-4 py-3.5">
        <div className="whitespace-pre-wrap break-words text-[13px] font-medium leading-6 text-zinc-100/95">
          {prompt || note || "暂无提示词记录"}
        </div>
      </div>
    </aside>
  )
}

function nodePreviewTypeLabel(type: string): string {
  if (type === "text") return "文本"
  if (type === "image") return "图片"
  if (type === "video") return "视频"
  if (type === "audio") return "音频"
  return type || "节点"
}

const NODE_PREVIEW_WIDE_TYPES = new Set(["text", "image", "video", "audio"])
const NODE_PREVIEW_LAYOUT_CLASS = "grid min-h-[520px] gap-3 lg:min-h-[min(720px,calc(100dvh-112px))] lg:grid-cols-[minmax(0,1fr)_minmax(380px,440px)]"
const NODE_PREVIEW_MEDIA_FRAME_CLASS = "flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded-xl bg-black shadow-inner shadow-black/30 ring-1 ring-white/[0.08]"
const NODE_PREVIEW_EMPTY_CLASS = "flex min-h-[520px] items-center justify-center rounded-xl bg-[#070a0f] text-sm font-medium text-zinc-500 ring-1 ring-white/[0.08] lg:min-h-[min(720px,calc(100dvh-112px))]"

function videoPreviewMimeType(src: string): string {
  const path = src.split(/[?#]/, 1)[0]?.toLowerCase() || ""
  if (path.endsWith(".webm")) return "video/webm"
  if (path.endsWith(".mov")) return "video/quicktime"
  return "video/mp4"
}

function NodeOutputPreviewCard({
  node,
  projectId,
  readOnly,
  onClose,
  onTextSaved,
}: {
  node: FlowNode
  projectId?: string
  readOnly?: boolean
  onClose: () => void
  onTextSaved: () => void | Promise<void>
}) {
  const data = node.data as {
    type?: string
    title?: string
    input?: unknown
    output?: unknown
    prompt?: string
    preview?: Record<string, unknown>
    previewText?: string
  }
  const type = String(data.type || "")
  const title = data.title || "节点预览"
  const isImagePreview = type === "image"
  const isWidePreview = NODE_PREVIEW_WIDE_TYPES.has(type)
  const info = previewInfoFromNode(node, type)
  const specEntries = previewSpecEntries(info, type)
  const textValue = type === "text"
    ? canvasNodeDisplayText({
      type,
      input: data.input,
      output: data.output,
      prompt: data.prompt,
      preview: data.preview,
      previewText: data.previewText,
    })
    : ""
  const imageUrl = type === "image" ? previewImageUrlFromNode(node) : ""
  const video = type === "video" ? previewVideoFromNode(node) : null
  const audio = type === "audio" ? previewAudioFromNode(node) : null
  const [textDraft, setTextDraft] = useState(textValue)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [imageExpanded, setImageExpanded] = useState(false)

  useEffect(() => {
    setTextDraft(textValue)
    setError(null)
  }, [node.id, textValue])

  useEffect(() => {
    setImageExpanded(false)
  }, [node.id, imageUrl])

  const saveText = useCallback(async () => {
    if (readOnly || type !== "text" || !projectId || saving || textDraft === textValue) return
    setSaving(true)
    setError(null)
    try {
      const input = previewObject(data.input) || {}
      await updateProjectNodeDetails(projectId, node.id, {
        title,
        prompt: data.prompt || previewString(input.prompt) || null,
        input: { ...input, content: textDraft },
        output: textDraft || null,
      })
      await onTextSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }, [data.input, data.prompt, node.id, onTextSaved, projectId, readOnly, saving, textDraft, textValue, title, type])

  const closeWithSave = async () => {
    await saveText()
    onClose()
  }

  return (
    <div
      className="openreel-node-preview-card nodrag nowheel fixed inset-0 z-[92] flex items-center justify-center bg-black/45 p-4 backdrop-blur-sm"
      onClick={() => void closeWithSave()}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
    >
      <div
        className={cn(
          "flex max-h-[calc(100dvh-16px)] flex-col overflow-hidden rounded-xl border border-white/[0.12] bg-[#0d1118]/98 text-zinc-100 shadow-[0_30px_110px_rgba(0,0,0,0.72)]",
          isImagePreview && imageExpanded
            ? "w-[calc(100vw-32px)] max-h-[calc(100dvh-32px)]"
            : isWidePreview
            ? "w-[min(1680px,calc(100vw-16px))]"
            : "w-[min(760px,calc(100vw-24px))]",
        )}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex min-h-12 items-center gap-3 border-b border-white/[0.08] bg-[#141922]/96 px-4">
          <span className="rounded-full bg-white/[0.07] px-2.5 py-1 text-[11px] font-semibold text-zinc-300 ring-1 ring-white/[0.075]">
            {nodePreviewTypeLabel(type)}
          </span>
          <div className="min-w-0 flex-1 truncate text-sm font-semibold text-zinc-50">{title}</div>
          {type === "text" && (
            <span className={`text-[11px] ${saving ? "text-cyan-200" : textDraft !== textValue ? "text-amber-200" : "text-zinc-500"}`}>
              {readOnly ? "历史预览" : saving ? "保存中" : textDraft !== textValue ? "待保存" : "已保存"}
            </span>
          )}
          <button
            type="button"
            onClick={() => void closeWithSave()}
            className="flex h-8 w-8 items-center justify-center rounded-full text-lg leading-none text-zinc-500 transition hover:bg-white/10 hover:text-zinc-100"
            aria-label="关闭预览"
          >
            ×
          </button>
        </div>
        <div className={cn(
          "min-h-0 flex-1 bg-[#070a0f]",
          isWidePreview ? "p-3" : "p-3",
          isImagePreview ? "overflow-hidden" : "overflow-auto",
        )}>
          {type === "text" ? (
            <div className={NODE_PREVIEW_LAYOUT_CLASS}>
              <div className="flex min-h-0 flex-col gap-2.5">
                <PreviewSpecRail entries={specEntries} />
                <div className="flex min-h-0 flex-1 overflow-hidden rounded-xl bg-[#11151c] shadow-inner shadow-black/30 ring-1 ring-white/[0.08]">
                  <textarea
                    value={textDraft}
                    onChange={(event) => setTextDraft(event.target.value)}
                    onBlur={() => void saveText()}
                    readOnly={readOnly}
                    className="min-h-[420px] flex-1 resize-none bg-transparent px-5 py-4 text-[15px] font-medium leading-8 text-zinc-50 outline-none [color-scheme:dark] placeholder:text-zinc-600 read-only:cursor-default read-only:text-zinc-100 lg:min-h-0"
                    placeholder="正文会显示在这里，也可以直接编辑"
                  />
                </div>
              </div>
              <PreviewPromptPanel title="输入提示词" prompt={info.prompt || data.prompt || ""} note="暂无输入提示词记录" />
            </div>
          ) : type === "image" ? (
            imageUrl ? (
              <div
                className={cn(
                  "h-full min-h-[420px] gap-2.5",
                  imageExpanded
                    ? "flex min-h-[calc(100dvh-84px)] flex-col"
                    : "grid lg:grid-cols-[minmax(0,1fr)_minmax(380px,440px)]",
                )}
              >
                <div className="flex min-h-0 flex-col gap-2.5">
                  <PreviewSpecRail entries={specEntries} />
                  <button
                    type="button"
                    onClick={() => setImageExpanded((value) => !value)}
                    className={cn(
                      `${NODE_PREVIEW_MEDIA_FRAME_CLASS} w-full outline-none transition focus-visible:ring-cyan-200/60`,
                      imageExpanded
                        ? "min-h-0 cursor-zoom-out"
                        : "min-h-[520px] cursor-zoom-in hover:ring-white/[0.16] lg:min-h-0",
                    )}
                    title={imageExpanded ? "缩回图片" : "放大图片"}
                  >
                    <img
                      src={imageUrl}
                      alt={title}
                      className={cn(
                        "max-w-full object-contain",
                        imageExpanded ? "max-h-[calc(100dvh-120px)]" : "max-h-[calc(100dvh-166px)]",
                      )}
                    />
                  </button>
                </div>
                {!imageExpanded && (
                  <PreviewPromptPanel prompt={info.prompt} />
                )}
              </div>
            ) : (
              <div className={NODE_PREVIEW_EMPTY_CLASS}>
                暂无图片产物
              </div>
            )
          ) : type === "video" ? (
            video?.src ? (
              <div className={NODE_PREVIEW_LAYOUT_CLASS}>
                <div className="flex min-h-0 flex-col gap-2.5">
                  <PreviewSpecRail entries={specEntries} />
                  <div className={NODE_PREVIEW_MEDIA_FRAME_CLASS}>
                    <video controls playsInline poster={video.poster} className="h-full max-h-[calc(100dvh-176px)] w-full object-contain">
                      <source src={video.src} type={videoPreviewMimeType(video.src)} />
                    </video>
                  </div>
                </div>
                <PreviewPromptPanel prompt={info.prompt} />
              </div>
            ) : (
              <div className={NODE_PREVIEW_EMPTY_CLASS}>
                暂无视频产物
              </div>
            )
          ) : type === "audio" ? (
            audio?.src ? (
              <div className={NODE_PREVIEW_LAYOUT_CLASS}>
                <div className="flex min-h-0 flex-col gap-2.5">
                  <PreviewSpecRail entries={specEntries} />
                  <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-8 overflow-hidden rounded-xl bg-[#080b10] px-8 py-10 shadow-inner shadow-black/30 ring-1 ring-white/[0.08]">
                    <div className="flex h-44 w-full max-w-2xl items-center justify-center gap-1.5">
                      {[26, 54, 78, 42, 92, 118, 72, 136, 98, 62, 150, 112, 84, 132, 70, 102, 146, 88, 58, 120, 94, 48, 76, 108].map((height, index) => (
                        <span
                          key={`${height}-${index}`}
                          className="w-2 rounded-full bg-gradient-to-t from-cyan-300/35 via-cyan-100/85 to-white/95 shadow-[0_0_18px_rgba(103,232,249,0.18)]"
                          style={{ height }}
                        />
                      ))}
                    </div>
                    <audio controls className="w-full max-w-2xl [color-scheme:dark]" src={audio.src} />
                  </div>
                </div>
                <PreviewPromptPanel prompt={info.prompt} />
              </div>
            ) : (
              <div className={NODE_PREVIEW_EMPTY_CLASS}>
                暂无音频产物
              </div>
            )
          ) : (
            <div className="flex min-h-[240px] items-center justify-center rounded-xl bg-black/35 text-sm font-medium text-zinc-500 ring-1 ring-white/[0.08]">
              暂无可预览内容
            </div>
          )}
          {error && (
            <div className="mt-3 rounded-md border border-red-400/20 bg-red-950/35 px-3 py-2 text-xs text-red-200">
              {error}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function stripCanvasNodeReferenceMarker(value: string): string {
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

function addCanvasNodeReferenceLookupKey(lookup: Map<string, string>, key: unknown, nodeId: string) {
  const text = String(key ?? "").trim()
  if (!text) return
  lookup.set(text, nodeId)
  lookup.set(stripCanvasNodeReferenceMarker(text), nodeId)
}

function buildCanvasNodeReferenceLookup(nodes: FlowNode[]): Map<string, string> {
  const lookup = new Map<string, string>()
  for (const node of nodes) {
    const data = node.data as { nodeId?: unknown; publicId?: unknown } | undefined
    addCanvasNodeReferenceLookupKey(lookup, node.id, node.id)
    addCanvasNodeReferenceLookupKey(lookup, `node:${node.id}`, node.id)
    addCanvasNodeReferenceLookupKey(lookup, data?.nodeId, node.id)
    const publicId = data?.publicId
    if (publicId !== undefined && publicId !== null && String(publicId).trim()) {
      addCanvasNodeReferenceLookupKey(lookup, publicId, node.id)
      addCanvasNodeReferenceLookupKey(lookup, `#${publicId}`, node.id)
      addCanvasNodeReferenceLookupKey(lookup, `node:${publicId}`, node.id)
      addCanvasNodeReferenceLookupKey(lookup, `node:#${publicId}`, node.id)
    }
  }
  return lookup
}

function nodeIdFromCanvasReference(value: unknown, nodeLookup: Map<string, string>): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    const text = String(value).trim()
    if (!text) return ""
    return nodeLookup.get(text) || nodeLookup.get(stripCanvasNodeReferenceMarker(text)) || ""
  }
  const obj = asWorkflowObject(value)
  if (!obj) return ""
  for (const key of ["ref", "reference", "reference_input", "value"]) {
    const nodeId = nodeIdFromCanvasReference(obj[key], nodeLookup)
    if (nodeId) return nodeId
  }
  for (const key of ["node_id", "nodeId", "source_node_id", "sourceNodeId"]) {
    const raw = obj[key]
    const nodeId = nodeIdFromCanvasReference(raw, nodeLookup)
    if (nodeId) return nodeId
  }
  return ""
}

function nodeDirectReferenceIds(
  node: FlowNode,
  nodeLookup: Map<string, string>,
  incomingEdges: Map<string, string[]>,
): string[] {
  const data = node.data as {
    workflowReferences?: unknown
    workflowDependsOn?: unknown
    input?: Record<string, unknown>
  } | undefined
  const result: string[] = []
  const add = (value: unknown) => {
    const id = nodeIdFromCanvasReference(value, nodeLookup)
    if (id && id !== node.id) result.push(id)
  }
  for (const id of incomingEdges.get(node.id) || []) add(id)
  if (Array.isArray(data?.workflowReferences)) {
    for (const item of data.workflowReferences) add(item)
  }
  if (Array.isArray(data?.workflowDependsOn)) {
    for (const item of data.workflowDependsOn) add(item)
  }
  const input = data?.input
  if (input) {
    for (const key of ["references", "depends_on", "reference_images"] as const) {
      const value = input[key]
      if (Array.isArray(value)) {
        for (const item of value) add(item)
      }
    }
    const fields = asWorkflowObject(input.fields)
    if (fields) {
      for (const key of ["references", "depends_on", "reference_images"] as const) {
        const value = fields[key]
        if (Array.isArray(value)) {
          for (const item of value) add(item)
        }
      }
    }
  }
  return Array.from(new Set(result))
}

function annotateCanvasNodesWithReferences(nodes: FlowNode[], edges: FlowEdge[]): FlowNode[] {
  if (nodes.length === 0) return nodes
  const visibleNodeIds = new Set(nodes.map((node) => node.id))
  const nodeLookup = buildCanvasNodeReferenceLookup(nodes)
  const incoming = new Map<string, string[]>()
  for (const edge of edges) {
    if (!visibleNodeIds.has(edge.source) || !visibleNodeIds.has(edge.target)) continue
    incoming.set(edge.target, [...(incoming.get(edge.target) || []), edge.source])
  }
  const nodeById = new Map(nodes.map((node) => [node.id, node]))
  return nodes.map((node) => {
    const referenceIds = nodeDirectReferenceIds(node, nodeLookup, incoming)
    const thumbs = referenceIds
      .map((id) => {
        const source = nodeById.get(id)
        const src = imageDownloadUrlFromNode(source)
        if (!src) return null
        const data = source?.data as { title?: string; publicId?: number | string | null } | undefined
        return {
          id,
          src,
          title: String(data?.title || id),
          publicId: data?.publicId ?? null,
        }
      })
      .filter((item): item is { id: string; src: string; title: string; publicId: number | string | null } => Boolean(item))
      .slice(0, 4)
    const data = node.data as Record<string, unknown>
    if (referenceIds.length === 0 && !data.referenceCount && !data.referenceThumbs) return node
    return {
      ...node,
      data: {
        ...data,
        referenceCount: referenceIds.length,
        referenceThumbs: thumbs,
      },
    }
  })
}

function edgeKey(edge: Pick<FlowEdge, "source" | "target">): string {
  return `${edge.source}->${edge.target}`
}

function canvasNodeType(node: FlowNode | undefined): string {
  const data = node?.data as { type?: unknown } | undefined
  return String(data?.type || node?.type || "")
}

function canvasNodeInput(node: FlowNode | undefined): Record<string, unknown> {
  const data = node?.data as { input?: unknown; workflowRuntimeOutput?: unknown; output?: unknown } | undefined
  return asWorkflowObject(data?.input) || {}
}

function videoReferenceLimitForCanvasNode(
  node: FlowNode,
  providers: MediaProviderSummary[],
  protocols: VideoProtocolSummary[],
): number | undefined {
  if (canvasNodeType(node) !== "video") return undefined
  const input = canvasNodeInput(node)
  const model = workflowStringValue(input.model)
  const mode = workflowStringValue(input.video_mode || input.mode)
  const provider = resolveVideoProvider(model, providers)
  return videoReferenceImageLimitForProvider(provider, protocols, mode)
}

function invalidVideoReferenceEdgeKeys(
  nodes: FlowNode[],
  edges: FlowEdge[],
  providers: MediaProviderSummary[],
  protocols: VideoProtocolSummary[],
): Set<string> {
  const invalid = new Set<string>()
  if (providers.length === 0 || protocols.length === 0) return invalid
  const nodeById = new Map(nodes.map((node) => [node.id, node]))
  for (const target of nodes) {
    if (canvasNodeType(target) !== "video") continue
    const limit = videoReferenceLimitForCanvasNode(target, providers, protocols)
    if (limit === undefined) continue
    const incomingImageEdges = edges.filter((edge) => {
      if (edge.target !== target.id) return false
      return canvasNodeType(nodeById.get(edge.source)) === "image"
    })
    const max = Math.max(0, limit)
    incomingImageEdges.slice(max).forEach((edge) => invalid.add(edgeKey(edge)))
  }
  return invalid
}

function hasAlternateDirectedPath(
  source: string,
  target: string,
  outgoing: Map<string, string[]>,
  ignoredKey: string,
): boolean {
  const visited = new Set<string>([source])
  const queue = (outgoing.get(source) || []).filter((next) => `${source}->${next}` !== ignoredKey)
  while (queue.length) {
    const current = queue.shift()!
    if (current === target) return true
    if (visited.has(current)) continue
    visited.add(current)
    for (const next of outgoing.get(current) || []) {
      if (`${current}->${next}` === ignoredKey) continue
      queue.push(next)
    }
  }
  return false
}

function transitiveReducedEdges(edges: FlowEdge[]): FlowEdge[] {
  if (edges.length <= 1) return edges
  const outgoing = new Map<string, string[]>()
  for (const edge of edges) {
    outgoing.set(edge.source, [...(outgoing.get(edge.source) || []), edge.target])
  }
  return edges.filter((edge) => !hasAlternateDirectedPath(edge.source, edge.target, outgoing, edgeKey(edge)))
}

function edgeWithDisplayStyle(edge: FlowEdge, emphasized = false): FlowEdge {
  const invalidReference = Boolean((edge.data as { invalidReference?: boolean } | undefined)?.invalidReference)
  return {
    ...edge,
    animated: emphasized || edge.animated,
    markerEnd: {
      type: MarkerType.ArrowClosed,
      color: invalidReference ? "#ef4444" : emphasized ? "#22d3ee" : "#64748b",
    },
    style: {
      ...(edge.style || {}),
      stroke: invalidReference ? "#ef4444" : emphasized ? "#22d3ee" : (edge.style as { stroke?: string } | undefined)?.stroke || "#64748b",
      strokeWidth: invalidReference || emphasized ? 2.2 : (edge.style as { strokeWidth?: number } | undefined)?.strokeWidth || 1.7,
      strokeDasharray: invalidReference ? "5 4" : (edge.style as { strokeDasharray?: string } | undefined)?.strokeDasharray,
      opacity: invalidReference || emphasized ? 0.95 : (edge.style as { opacity?: number } | undefined)?.opacity || 0.72,
    },
  }
}

function deriveDisplayedEdges(
  edges: FlowEdge[],
  mode: CanvasEdgeDisplayMode,
  selectedNodeIds: string[],
  invalidReferenceEdgeKeys: Set<string> = new Set(),
): FlowEdge[] {
  const markInvalid = (edge: FlowEdge): FlowEdge => invalidReferenceEdgeKeys.has(edgeKey(edge))
    ? { ...edge, data: { ...(edge.data as Record<string, unknown> | undefined), invalidReference: true } }
    : edge
  if (mode === "all") return edges.map((edge) => edgeWithDisplayStyle(markInvalid(edge)))
  const cleanEdges = transitiveReducedEdges(edges)
  if (mode === "clean" || selectedNodeIds.length === 0) return cleanEdges.map((edge) => edgeWithDisplayStyle(markInvalid(edge)))

  const selected = new Set(selectedNodeIds)
  const cleanKeys = new Set(cleanEdges.map(edgeKey))
  const directSelectedEdges = edges.filter((edge) => selected.has(edge.source) || selected.has(edge.target))
  const merged = [...cleanEdges]
  for (const edge of directSelectedEdges) {
    if (!cleanKeys.has(edgeKey(edge))) merged.push(edge)
  }
  const selectedKeys = new Set(directSelectedEdges.map(edgeKey))
  return merged.map((edge) => edgeWithDisplayStyle(markInvalid(edge), selectedKeys.has(edgeKey(edge))))
}

function deriveCanvasVisibleEdges(
  edges: FlowEdge[],
  nodes: FlowNode[],
  visibleNodeIds: Set<string>,
): FlowEdge[] {
  if (edges.length === 0) return []
  const knownNodeIds = new Set(nodes.map((node) => node.id))
  const hiddenNodeIds = new Set(nodes.filter((node) => !visibleNodeIds.has(node.id)).map((node) => node.id))
  const outgoing = new Map<string, FlowEdge[]>()
  const directEdges: FlowEdge[] = []
  const byKey = new Map<string, FlowEdge>()

  for (const edge of edges) {
    if (!knownNodeIds.has(edge.source) || !knownNodeIds.has(edge.target)) continue
    outgoing.set(edge.source, [...(outgoing.get(edge.source) || []), edge])
    if (visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)) {
      const key = edgeKey(edge)
      if (!byKey.has(key)) {
        byKey.set(key, edge)
        directEdges.push(edge)
      }
    }
  }

  const bridgedEdges: FlowEdge[] = []
  const addBridge = (source: string, target: string) => {
    if (source === target) return
    const key = `${source}->${target}`
    if (byKey.has(key)) return
    byKey.set(key, {
      id: `dep-${source}-${target}`,
      source,
      target,
      sourceHandle: "out",
      targetHandle: "in",
      type: "bezier",
      data: { synthetic: true, throughWorkflowRuntime: true },
    })
    bridgedEdges.push(byKey.get(key)!)
  }

  for (const source of visibleNodeIds) {
    const visited = new Set<string>([source])
    const queue = [...(outgoing.get(source) || [])]
    while (queue.length > 0) {
      const edge = queue.shift()!
      const target = edge.target
      if (visited.has(target)) continue
      visited.add(target)
      if (visibleNodeIds.has(target)) {
        addBridge(source, target)
        continue
      }
      if (!hiddenNodeIds.has(target)) continue
      for (const next of outgoing.get(target) || []) queue.push(next)
    }
  }

  return [...directEdges, ...bridgedEdges]
}

function safeDownloadName(title: string, url: string) {
  const cleanTitle = (title || "openreel-image")
    .replace(/[\\/:*?"<>|]+/g, "-")
    .replace(/\s+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64) || "openreel-image"
  const path = url.split(/[?#]/, 1)[0] || ""
  const ext = path.match(/\.(png|jpe?g|webp|gif|bmp|svg)$/i)?.[0]?.toLowerCase() || ".png"
  return cleanTitle.toLowerCase().endsWith(ext) ? cleanTitle : `${cleanTitle}${ext}`
}

async function downloadUrl(url: string, filename: string) {
  try {
    const response = await fetch(url, { credentials: "include" })
    if (response.ok) {
      const blob = await response.blob()
      const objectUrl = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = objectUrl
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
      return
    }
  } catch {
    // Fall back to a normal download link; cross-origin URLs may block fetch.
  }
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  anchor.rel = "noopener"
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

function downloadJsonPayload(payload: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" })
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = objectUrl
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
}

function getPointerClientPoint(event: globalThis.MouseEvent | globalThis.TouchEvent) {
  if ("changedTouches" in event && event.changedTouches.length) {
    return { x: event.changedTouches[0].clientX, y: event.changedTouches[0].clientY }
  }
  return { x: (event as globalThis.MouseEvent).clientX, y: (event as globalThis.MouseEvent).clientY }
}

function isWorkflowUiTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest("[data-openreel-workflow-ui='true']"))
}

function findPortHandleElement(nodeId: string, position: Position): HTMLElement | null {
  const nodeElement = Array.from(document.querySelectorAll<HTMLElement>(".react-flow__node"))
    .find((element) => element.dataset.id === nodeId)
  if (!nodeElement) return null
  const port = position === Position.Right
    ? "output"
    : position === Position.Left
      ? "input"
      : ""
  if (port) {
    const handle = nodeElement.querySelector<HTMLElement>(`[data-openreel-port='${port}']`)
    if (handle) return handle
  }
  return nodeElement.querySelector<HTMLElement>(".react-flow__handle")
}

function getPortHandleScreenPoint(nodeId: string, position: Position): { x: number; y: number } | null {
  if (typeof document === "undefined") return null
  const handleElement = findPortHandleElement(nodeId, position)
  if (!handleElement) return null
  const rect = handleElement.getBoundingClientRect()
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  }
}

function OpenReelConnectionLine({
  connectionLineStyle,
  fromNode,
  fromX,
  fromY,
  toX,
  toY,
  fromPosition,
  toPosition,
}: ConnectionLineComponentProps) {
  const { screenToFlowPosition } = useReactFlow()
  let sourceX = fromX
  let sourceY = fromY

  if (typeof document !== "undefined" && fromNode?.id) {
    const handleElement = findPortHandleElement(String(fromNode.id), fromPosition)
    if (handleElement) {
      const rect = handleElement.getBoundingClientRect()
      const sourcePoint = screenToFlowPosition({
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
      })
      sourceX = sourcePoint.x
      sourceY = sourcePoint.y
    }
  }

  const [path] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition: fromPosition,
    targetX: toX,
    targetY: toY,
    targetPosition: toPosition,
  })

  return (
    <path
      d={path}
      fill="none"
      className="react-flow__connection-path openreel-connection-path"
      style={connectionLineStyle}
    />
  )
}

function PendingConnectionPreview({ line }: { line: PendingConnectionPreviewLine }) {
  const control = Math.max(80, Math.abs(line.toX - line.fromX) * 0.48)
  const path = `M${line.fromX},${line.fromY} C${line.fromX + control},${line.fromY} ${line.toX - control},${line.toY} ${line.toX},${line.toY}`
  return (
    <svg className="openreel-pending-connection-preview pointer-events-none fixed inset-0 z-[70] h-screen w-screen">
      <path
        d={path}
        fill="none"
        stroke="#22d3ee"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity={0.95}
      />
      <circle cx={line.fromX} cy={line.fromY} r={4} fill="#67e8f9" stroke="#0f131b" strokeWidth={2} />
      <circle cx={line.toX} cy={line.toY} r={3.5} fill="#22d3ee" opacity={0.8} />
    </svg>
  )
}

function numericDimension(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) return value
  if (typeof value === "string") {
    const parsed = Number.parseFloat(value)
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null
  }
  return null
}

function nodeDimension(node: FlowNode, key: "width" | "height", fallback: number): number {
  const style = node.style as Record<string, unknown> | undefined
  const data = node.data as Record<string, unknown> | undefined
  return (
    numericDimension(node[key]) ??
    numericDimension(style?.[key]) ??
    numericDimension(data?.[key === "width" ? "canvasWidth" : "canvasHeight"]) ??
    fallback
  )
}

function nodeBounds(node: FlowNode): NodeBounds {
  const width = nodeDimension(node, "width", ALIGNMENT_DEFAULT_NODE_WIDTH)
  const height = nodeDimension(node, "height", ALIGNMENT_DEFAULT_NODE_HEIGHT)
  const left = Number.isFinite(node.position?.x) ? node.position.x : 0
  const top = Number.isFinite(node.position?.y) ? node.position.y : 0
  return {
    id: node.id,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    centerX: left + width / 2,
    centerY: top + height / 2,
  }
}

function nodeContextPanelStyle(
  node: FlowNode | null,
  viewport: CanvasViewport,
  container: HTMLDivElement | null,
): CSSProperties | null {
  if (!node || !container) return null
  const bounds = nodeBounds(node)
  const data = node.data as { type?: unknown } | undefined
  const nodeType = String(data?.type || "")
  const mediaPanel = nodeType === "image" || nodeType === "video" || nodeType === "audio"
  const maxPanelHeight = mediaPanel ? NODE_CONTEXT_PANEL_MEDIA_MAX_HEIGHT : NODE_CONTEXT_PANEL_MAX_HEIGHT
  const zoom = viewport.zoom || 1
  const margin = NODE_CONTEXT_PANEL_MARGIN
  const gap = NODE_CONTEXT_PANEL_GAP
  const containerWidth = container.clientWidth || window.innerWidth
  const containerHeight = container.clientHeight || window.innerHeight
  if (containerWidth <= margin * 2 || containerHeight <= margin * 2) return null

  const nodeScreenLeft = bounds.left * zoom + viewport.x
  const nodeScreenTop = bounds.top * zoom + viewport.y
  const nodeScreenWidth = bounds.width * zoom
  const nodeScreenHeight = bounds.height * zoom
  const nodeScreenCenterX = nodeScreenLeft + nodeScreenWidth / 2
  const nodeScreenBottom = nodeScreenTop + nodeScreenHeight
  const maxPanelWidth = Math.min(NODE_CONTEXT_PANEL_MAX_WIDTH, containerWidth - margin * 2)
  const panelWidth = Math.min(
    maxPanelWidth,
    Math.max(
      NODE_CONTEXT_PANEL_MIN_WIDTH,
      Math.min(NODE_CONTEXT_PANEL_MAX_WIDTH, Math.max(nodeScreenWidth, NODE_CONTEXT_PANEL_IDEAL_WIDTH)),
    ),
  )
  const left = Math.max(margin, Math.min(nodeScreenCenterX - panelWidth / 2, containerWidth - panelWidth - margin))

  let top = nodeScreenBottom + gap
  let maxHeight = Math.min(maxPanelHeight, containerHeight - top - margin)
  let transformOrigin = "top center"
  if (maxHeight < NODE_CONTEXT_PANEL_MIN_HEIGHT) {
    const aboveHeight = nodeScreenTop - margin - gap
    if (aboveHeight >= NODE_CONTEXT_PANEL_MIN_HEIGHT) {
      maxHeight = Math.min(maxPanelHeight, aboveHeight)
      top = Math.max(margin, nodeScreenTop - gap - maxHeight)
      transformOrigin = "bottom center"
    } else {
      maxHeight = Math.max(
        NODE_CONTEXT_PANEL_MIN_HEIGHT,
        Math.min(maxPanelHeight, containerHeight - margin * 2),
      )
      top = Math.max(margin, Math.min(nodeScreenBottom + gap, containerHeight - margin - maxHeight))
    }
  }

  return {
    left,
    top,
    width: panelWidth,
    maxHeight,
    transformOrigin,
  }
}

function nodeContextViewportNudge(
  node: FlowNode | null,
  viewport: CanvasViewport,
  container: HTMLDivElement | null,
): CanvasViewport | null {
  if (!node || !container) return null
  const bounds = nodeBounds(node)
  const data = node.data as { type?: unknown } | undefined
  const nodeType = String(data?.type || "")
  const preferredHeight = nodeType === "image" || nodeType === "video" || nodeType === "audio"
    ? NODE_CONTEXT_PANEL_MEDIA_PREFERRED_HEIGHT
    : NODE_CONTEXT_PANEL_PREFERRED_HEIGHT
  const zoom = viewport.zoom || 1
  const margin = NODE_CONTEXT_PANEL_MARGIN
  const gap = NODE_CONTEXT_PANEL_GAP
  const containerHeight = container.clientHeight || window.innerHeight
  const nodeScreenTop = bounds.top * zoom + viewport.y
  const nodeScreenBottom = nodeScreenTop + bounds.height * zoom
  const belowSpace = containerHeight - nodeScreenBottom - gap - margin
  if (belowSpace >= preferredHeight) return null

  const desiredBottom = containerHeight - margin - gap - preferredHeight
  const desiredShiftY = desiredBottom - nodeScreenBottom
  const maxUpwardShift = margin - nodeScreenTop
  const shiftY = Math.min(0, Math.max(desiredShiftY, maxUpwardShift))
  if (Math.abs(shiftY) < 1) return null

  return {
    ...viewport,
    y: viewport.y + shiftY,
  }
}

function nearestAlignment(
  active: NodeBounds,
  others: NodeBounds[],
  axis: "x" | "y",
  threshold: number,
): { delta: number; guide: CanvasAlignmentGuide } | null {
  const activePoints = axis === "x"
    ? [active.left, active.centerX, active.right]
    : [active.top, active.centerY, active.bottom]
  let best: { delta: number; distance: number; target: number; other: NodeBounds } | null = null

  for (const other of others) {
    const targetPoints = axis === "x"
      ? [other.left, other.centerX, other.right]
      : [other.top, other.centerY, other.bottom]
    for (const activePoint of activePoints) {
      for (const target of targetPoints) {
        const delta = target - activePoint
        const distance = Math.abs(delta)
        if (distance > threshold) continue
        if (!best || distance < best.distance) {
          best = { delta, distance, target, other }
        }
      }
    }
  }

  if (!best) return null
  if (axis === "x") {
    return {
      delta: best.delta,
      guide: {
        orientation: "vertical",
        position: best.target,
        start: Math.min(active.top, best.other.top) - ALIGNMENT_GUIDE_MARGIN,
        end: Math.max(active.bottom, best.other.bottom) + ALIGNMENT_GUIDE_MARGIN,
      },
    }
  }
  return {
    delta: best.delta,
    guide: {
      orientation: "horizontal",
      position: best.target,
      start: Math.min(active.left, best.other.left) - ALIGNMENT_GUIDE_MARGIN,
      end: Math.max(active.right, best.other.right) + ALIGNMENT_GUIDE_MARGIN,
    },
  }
}

function computeAlignmentSnap({
  activeNode,
  nodes,
  draggedNodeIds,
  zoom,
  coarsePointer,
}: {
  activeNode: FlowNode
  nodes: FlowNode[]
  draggedNodeIds: Set<string>
  zoom: number
  coarsePointer: boolean
}): { deltaX: number; deltaY: number; guides: CanvasAlignmentGuide[] } {
  const threshold = (coarsePointer ? ALIGNMENT_SNAP_SCREEN_PX_COARSE : ALIGNMENT_SNAP_SCREEN_PX) / Math.max(zoom || 1, 0.15)
  const active = nodeBounds(activeNode)
  const others = nodes
    .filter((node) => !draggedNodeIds.has(node.id) && !node.hidden)
    .map(nodeBounds)
  const x = nearestAlignment(active, others, "x", threshold)
  const y = nearestAlignment(active, others, "y", threshold)
  return {
    deltaX: x?.delta ?? 0,
    deltaY: y?.delta ?? 0,
    guides: [x?.guide, y?.guide].filter((guide): guide is CanvasAlignmentGuide => Boolean(guide)),
  }
}

function alignmentGuideSignature(guides: CanvasAlignmentGuide[]): string {
  return guides
    .map((guide) => `${guide.orientation}:${Math.round(guide.position * 10) / 10}:${Math.round(guide.start * 10) / 10}:${Math.round(guide.end * 10) / 10}`)
    .join("|")
}

function AlignmentGuides({ guides, viewport }: { guides: CanvasAlignmentGuide[]; viewport: CanvasViewport }) {
  if (guides.length === 0) return null
  const zoom = viewport.zoom || 1
  return (
    <div className="pointer-events-none absolute inset-0 z-[55] overflow-hidden">
      {guides.map((guide, index) => {
        if (guide.orientation === "vertical") {
          const left = guide.position * zoom + viewport.x
          const top = guide.start * zoom + viewport.y
          const height = Math.max(1, (guide.end - guide.start) * zoom)
          return (
            <div
              key={`${guide.orientation}-${index}`}
              className="absolute border-l border-dashed border-cyan-200/90 shadow-[0_0_12px_rgba(103,232,249,0.45)]"
              style={{ left, top, height }}
            />
          )
        }
        const top = guide.position * zoom + viewport.y
        const left = guide.start * zoom + viewport.x
        const width = Math.max(1, (guide.end - guide.start) * zoom)
        return (
          <div
            key={`${guide.orientation}-${index}`}
            className="absolute border-t border-dashed border-cyan-200/90 shadow-[0_0_12px_rgba(103,232,249,0.45)]"
            style={{ left, top, width }}
          />
        )
      })}
    </div>
  )
}

export default function WorkflowCanvas({
  workspaceView = "canvas",
  onWorkspaceViewChange,
}: WorkflowCanvasProps) {
  const allNodes = useCanvasStore((s) => s.nodes)
  const allEdges = useCanvasStore((s) => s.edges)
  const selectedNodeId = useCanvasStore((s) => s.selectedNodeId)
  const selectNode = useCanvasStore((s) => s.selectNode)
  const applyCanvasAction = useCanvasStore((s) => s.applyCanvasAction)
  const applyNodeChanges = useCanvasStore((s) => s.applyNodeChanges)
  const applyEdgeChanges = useCanvasStore((s) => s.applyEdgeChanges)
  const updateCanvasNode = useCanvasStore((s) => s.updateNode)
  const addNode = useCanvasStore((s) => s.addNode)
  const loadCanvasNodes = useCanvasStore((s) => s.loadNodes)
  const connectNodes = useCanvasStore((s) => s.connectNodes)
  const replaceEdgeId = useCanvasStore((s) => s.replaceEdgeId)
  const removeNodes = useCanvasStore((s) => s.removeNodes)
  const removeEdges = useCanvasStore((s) => s.removeEdges)
  const currentProject = useProjectStore((s) => s.currentProject)
  const streaming = useChatStore((s) => s.streaming)
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance | null>(null)
  const [viewport, setViewport] = useState<CanvasViewport>({ x: 0, y: 0, zoom: 1 })
  const [groupedNodeIds, setGroupedNodeIds] = useState<string[]>([])
  const [contextMenu, setContextMenu] = useState<CanvasCreateMenuState | null>(null)
  const [nodeActionMenu, setNodeActionMenu] = useState<NodeActionMenuState | null>(null)
  const [assetSaveRequest, setAssetSaveRequest] = useState<NodeAssetSaveRequest | null>(null)
  const [imageEditRequest, setImageEditRequest] = useState<NodeImageEditRequest | null>(null)
  const [videoEditRequest, setVideoEditRequest] = useState<NodeVideoEditRequest | null>(null)
  const [nodePreviewRequest, setNodePreviewRequest] = useState<NodePreviewRequest | null>(null)
  const [nodeDetailEditRequestKey, setNodeDetailEditRequestKey] = useState<string | null>(null)
  const [panoramaViewer, setPanoramaViewer] = useState<PanoramaViewerRequest | null>(null)
  const [mediaHistoryOpen, setMediaHistoryOpen] = useState(false)
  const [mediaHistoryItems, setMediaHistoryItems] = useState<ProjectMediaHistoryItem[]>([])
  const [mediaHistoryFilter, setMediaHistoryFilter] = useState<MediaHistoryFilter>("all")
  const [mediaHistoryLoading, setMediaHistoryLoading] = useState(false)
  const [mediaHistoryError, setMediaHistoryError] = useState<string | null>(null)
  const [restoringHistoryId, setRestoringHistoryId] = useState<string | null>(null)
  const [deletingHistoryId, setDeletingHistoryId] = useState<string | null>(null)
  const [workflowTemplates, setWorkflowTemplates] = useState<WorkflowTemplateSummary[]>([])
  const [selectedWorkflowTemplateId, setSelectedWorkflowTemplateId] = useState("")
  const [workflowTemplatesLoading, setWorkflowTemplatesLoading] = useState(false)
  const [workflowTemplatesError, setWorkflowTemplatesError] = useState<string | null>(null)
  const [workflowMaterializing, setWorkflowMaterializing] = useState(false)
  const [workflowNodeTypes, setWorkflowNodeTypes] = useState<WorkflowNodeTypeDefinition[]>([])
  const [workflowNodeTypesError, setWorkflowNodeTypesError] = useState<string | null>(null)
  const [workflowInputValues, setWorkflowInputValues] = useState<Record<string, string>>({})
  const [workflowMediaModelOverrides, setWorkflowMediaModelOverrides] = useState<Record<string, string>>({})
  const [workflowArtifactPreview, setWorkflowArtifactPreview] = useState<WorkflowArtifactPreview | null>(null)
  const [workflowImportedSpec, setWorkflowImportedSpec] = useState<Record<string, unknown> | null>(null)
  const [workflowResolvedPreview, setWorkflowResolvedPreview] = useState<WorkflowResolvedPreview | null>(null)
  const [workflowRuntimePayload, setWorkflowRuntimePayload] = useState<ProjectWorkflowRuntime | null>(null)
  const [workflowRuntimePayloads, setWorkflowRuntimePayloads] = useState<ProjectWorkflowRuntime[]>([])
  const [workflowRuntimeInstanceId, setWorkflowRuntimeInstanceId] = useState("")
  const workflowRuntimeAutoSelectSuppressedRef = useRef(false)
  const workflowRuntimePayloadRef = useRef<ProjectWorkflowRuntime | null>(null)
  const [workflowRuntimeOrigin, setWorkflowRuntimeOrigin] = useState<{ x: number; y: number } | null>(null)
  const [workflowInstanceInputValues, setWorkflowInstanceInputValues] = useState<WorkflowInputValuesByInstance>({})
  const [workflowRunningStepIds, setWorkflowRunningStepIds] = useState<string[]>([])
  const [workflowRunningAll, setWorkflowRunningAll] = useState(false)
  const [workflowDockOpen, setWorkflowDockOpen] = useState(false)
  const [workflowDockTemplateId, setWorkflowDockTemplateId] = useState("")
  const [workflowDockExpandedRunIds, setWorkflowDockExpandedRunIds] = useState<string[]>([])
  const [workflowInstanceRunningIds, setWorkflowInstanceRunningIds] = useState<string[]>([])
  const [workflowInstanceRunningAllIds, setWorkflowInstanceRunningAllIds] = useState<string[]>([])
  const [workflowInstancePausingIds, setWorkflowInstancePausingIds] = useState<string[]>([])
  const [workflowInstanceDeletingIds, setWorkflowInstanceDeletingIds] = useState<string[]>([])
  const [workflowInstanceErrors, setWorkflowInstanceErrors] = useState<Record<string, string>>({})
  const [workflowDockDetail, setWorkflowDockDetail] = useState<WorkflowRunDockDetailSelection | null>(null)
  const [edgeDisplayMode, setEdgeDisplayMode] = useState<CanvasEdgeDisplayMode>("clean")
  const [videoReferenceProviders, setVideoReferenceProviders] = useState<MediaProviderSummary[]>([])
  const [videoReferenceProtocols, setVideoReferenceProtocols] = useState<VideoProtocolSummary[]>([])
  const [alignmentGuides, setAlignmentGuides] = useState<CanvasAlignmentGuide[]>([])
  const [assetSaveForm, setAssetSaveForm] = useState<AssetSaveForm>({
    library: "shared",
    kind: "scene",
    category: "",
    episode: "1",
    name: "",
  })
  const [assetCategories, setAssetCategories] = useState<AssetCategoryResult>({})
  const [assetSaveLoading, setAssetSaveLoading] = useState(false)
  const [assetSaveError, setAssetSaveError] = useState<string | null>(null)
  const [coarsePointer, setCoarsePointer] = useState(false)
  const undoStackRef = useRef<CanvasUndoRecord[]>([])
  const canvasContainerRef = useRef<HTMLDivElement>(null)
  const dragStartPositionsRef = useRef<Record<string, { x: number; y: number }>>({})
  const activeDragNodeIdsRef = useRef<string[]>([])
  const alignmentGuideSignatureRef = useRef("")
  const connectionStartRef = useRef<PendingConnectionDraft | null>(null)
  const connectionCompletedRef = useRef(false)
  const suppressPaneClickRef = useRef(false)
  const blankPointerRef = useRef<{ pointerId: number; x: number; y: number } | null>(null)
  const longPressRef = useRef<LongPressState | null>(null)
  const refreshTimerRef = useRef<number | null>(null)
  useEffect(() => {
    let cancelled = false
    const loadVideoReferenceConfig = async () => {
      try {
        const [config, protocols] = await Promise.all([
          getRuntimeConfigFile<{ parsed?: { media_providers?: MediaProviderSummary[] } }>(true),
          getVideoProviderProtocols<{ protocols?: VideoProtocolSummary[] }>().catch(() => null),
        ])
        if (cancelled) return
        setVideoReferenceProviders(config.parsed?.media_providers || [])
        setVideoReferenceProtocols(protocols?.protocols || [])
      } catch {
        if (cancelled) return
        setVideoReferenceProviders([])
        setVideoReferenceProtocols([])
      }
    }
    void loadVideoReferenceConfig()
    window.addEventListener("drama:runtime-config-updated", loadVideoReferenceConfig)
    return () => {
      cancelled = true
      window.removeEventListener("drama:runtime-config-updated", loadVideoReferenceConfig)
    }
  }, [])
  const groupedNodeIdSet = useMemo(() => new Set(groupedNodeIds), [groupedNodeIds])
  const visibleNodeIds = useMemo(() => new Set(allNodes.map((node) => node.id)), [allNodes])
  const canvasEdges = useMemo(
    () => allEdges.filter((edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)),
    [allEdges, visibleNodeIds],
  )
  const canvasVisibleNodeIds = useMemo(
    () => new Set(allNodes.filter((node) => !isWorkflowRuntimeCanvasNode(node)).map((node) => node.id)),
    [allNodes],
  )
  const canvasVisibleEdges = useMemo(
    () => deriveCanvasVisibleEdges(canvasEdges, allNodes, canvasVisibleNodeIds),
    [allNodes, canvasEdges, canvasVisibleNodeIds],
  )
  const nodes = useMemo(
    () => annotateCanvasNodesWithReferences(
      allNodes.filter((node) => canvasVisibleNodeIds.has(node.id)),
      canvasVisibleEdges,
    ),
    [allNodes, canvasVisibleEdges, canvasVisibleNodeIds],
  )
  const videoEditMediaNodes = useMemo<VideoEditPanelMediaNode[]>(() => {
    return nodes
      .map((node): VideoEditPanelMediaNode | null => {
        const data = node.data as { type?: string; title?: string } | undefined
        const sourceNodeId = mediaOperationSourceNodeIdFromNode(node)
        if (data?.type === "video") {
          const video = previewVideoFromNode(node)
          if (!video?.src) return null
          return {
            id: node.id,
            type: "video" as const,
            title: data.title || "视频节点",
            src: video.src,
            ...(sourceNodeId ? { sourceNodeId } : {}),
          }
        }
        if (data?.type === "audio") {
          const audio = previewAudioFromNode(node)
          if (!audio?.src) return null
          return {
            id: node.id,
            type: "audio" as const,
            title: data.title || "音频节点",
            src: audio.src,
            ...(sourceNodeId ? { sourceNodeId } : {}),
          }
        }
        if (data?.type === "image") {
          const src = previewImageUrlFromNode(node)
          if (!src) return null
          return {
            id: node.id,
            type: "image" as const,
            title: data.title || "图片节点",
            src,
            ...(sourceNodeId ? { sourceNodeId } : {}),
          }
        }
        return null
      })
      .filter((item): item is VideoEditPanelMediaNode => Boolean(item))
  }, [nodes])
  const selectedCanvasNodeId = selectedNodeId && canvasVisibleNodeIds.has(selectedNodeId) ? selectedNodeId : null
  const basePreviewCanvasNode = nodePreviewRequest?.nodeId
    ? nodes.find((node) => node.id === nodePreviewRequest.nodeId)
    : undefined
  const previewCanvasNode = useMemo(() => {
    if (!basePreviewCanvasNode) return undefined
    if (!nodePreviewRequest) return basePreviewCanvasNode
    const hasOverride = (
      nodePreviewRequest.type ||
      nodePreviewRequest.title ||
      nodePreviewRequest.input !== undefined ||
      nodePreviewRequest.output !== undefined ||
      nodePreviewRequest.prompt !== undefined ||
      nodePreviewRequest.preview !== undefined ||
      nodePreviewRequest.previewText !== undefined
    )
    if (!hasOverride) return basePreviewCanvasNode
    const data = basePreviewCanvasNode.data as Record<string, unknown>
    return {
      ...basePreviewCanvasNode,
      data: {
        ...data,
        type: nodePreviewRequest.type || data.type,
        title: nodePreviewRequest.title || data.title,
        input: nodePreviewRequest.input !== undefined ? nodePreviewRequest.input : data.input,
        output: nodePreviewRequest.output !== undefined ? nodePreviewRequest.output : data.output,
        prompt: nodePreviewRequest.prompt !== undefined ? nodePreviewRequest.prompt : data.prompt,
        preview: nodePreviewRequest.preview !== undefined ? nodePreviewRequest.preview : data.preview,
        previewText: nodePreviewRequest.previewText !== undefined ? nodePreviewRequest.previewText : data.previewText,
      },
    } as FlowNode
  }, [basePreviewCanvasNode, nodePreviewRequest])
  const selectedNodeIds = useMemo(
    () => {
      const ids = new Set(nodes.filter((node) => node.selected && canvasVisibleNodeIds.has(node.id)).map((node) => node.id))
      if (selectedCanvasNodeId) ids.add(selectedCanvasNodeId)
      return [...ids]
    },
    [canvasVisibleNodeIds, nodes, selectedCanvasNodeId],
  )
  const invalidReferenceEdgeKeys = useMemo(
    () => invalidVideoReferenceEdgeKeys(nodes, canvasVisibleEdges, videoReferenceProviders, videoReferenceProtocols),
    [canvasVisibleEdges, nodes, videoReferenceProviders, videoReferenceProtocols],
  )
  const edges = useMemo(
    () => deriveDisplayedEdges(canvasVisibleEdges, edgeDisplayMode, selectedNodeIds, invalidReferenceEdgeKeys),
    [canvasVisibleEdges, edgeDisplayMode, invalidReferenceEdgeKeys, selectedNodeIds],
  )
  const selectedEdgeIds = useMemo(
    () => edges.filter((edge) => edge.selected).map((edge) => edge.id),
    [edges],
  )
  const selectedWorkflowTemplate = useMemo(
    () => workflowTemplates.find((item) => item.id === selectedWorkflowTemplateId) || workflowTemplates[0],
    [selectedWorkflowTemplateId, workflowTemplates],
  )
  const workflowTemplateById = useMemo(
    () => new Map(workflowTemplates.map((template) => [template.id, template])),
    [workflowTemplates],
  )
  useEffect(() => {
    workflowRuntimePayloadRef.current = workflowRuntimePayload
  }, [workflowRuntimePayload])
  useEffect(() => {
    setWorkflowDockTemplateId((current) => (
      current && workflowTemplateById.has(current)
        ? current
        : selectedWorkflowTemplate?.id || workflowTemplates[0]?.id || ""
    ))
  }, [selectedWorkflowTemplate?.id, workflowTemplateById, workflowTemplates])
  const replaceWorkflowRuntimePayloads = useCallback((runtimes: ProjectWorkflowRuntime[] | null | undefined, selected?: ProjectWorkflowRuntime | null, preserveCurrent = true) => {
    const incoming = mergeWorkflowRuntimePayloads([], selected ? mergeWorkflowRuntimePayloads(runtimes || [], selected) : runtimes || [])
    setWorkflowInstanceInputValues((current) => mergeWorkflowInputValuesByInstance(current, incoming))
    const selectedId = workflowRuntimeAutoSelectSuppressedRef.current ? "" : workflowRuntimeId(selected)
    setWorkflowRuntimePayloads((current) => {
      const incomingIds = new Set(incoming.map(workflowRuntimeId).filter(Boolean))
      const localDrafts = current.filter((runtime) => {
        const id = workflowRuntimeId(runtime)
        return Boolean(runtime.local_draft && id && !incomingIds.has(id))
      })
      const next = mergeWorkflowRuntimePayloads(localDrafts, incoming)
      const selectedRuntimeBeforeRefresh = workflowRuntimePayloadRef.current
      const selectedRuntime = selectedId
        ? next.find((runtime) => workflowRuntimeId(runtime) === selectedId) || selected || null
        : preserveCurrent && selectedRuntimeBeforeRefresh && next.some((runtime) => workflowRuntimeId(runtime) === workflowRuntimeId(selectedRuntimeBeforeRefresh))
        ? next.find((runtime) => workflowRuntimeId(runtime) === workflowRuntimeId(selectedRuntimeBeforeRefresh)) || null
        : null
      workflowRuntimePayloadRef.current = selectedRuntime
      setWorkflowRuntimePayload(selectedRuntime)
      setWorkflowRuntimeInstanceId(workflowRuntimeId(selectedRuntime))
      return next
    })
  }, [])
  const upsertWorkflowRuntimePayload = useCallback((runtime: ProjectWorkflowRuntime | null | undefined) => {
    if (!runtime || !workflowRuntimeId(runtime)) return
    workflowRuntimeAutoSelectSuppressedRef.current = false
    setWorkflowInstanceInputValues((current) => mergeWorkflowInputValuesByInstance(current, runtime))
    workflowRuntimePayloadRef.current = runtime
    setWorkflowRuntimePayload(runtime)
    setWorkflowRuntimeInstanceId(workflowRuntimeId(runtime))
    setWorkflowRuntimePayloads((current) => mergeWorkflowRuntimePayloads(current, runtime))
  }, [])
  const activeWorkflowTemplateId = workflowArtifactPreview?.id || selectedWorkflowTemplate?.id || ""
  const activeWorkflowInputIds = useMemo(
    () => workflowInputsForTemplateSource(workflowArtifactPreview, selectedWorkflowTemplate, workflowTemplates),
    [selectedWorkflowTemplate, workflowArtifactPreview, workflowTemplates],
  )
  const activeWorkflowRequiredInputIds = useMemo(
    () => workflowRequiredInputsForTemplateSource(workflowArtifactPreview, selectedWorkflowTemplate, workflowTemplates),
    [selectedWorkflowTemplate, workflowArtifactPreview, workflowTemplates],
  )
  const activeWorkflowInputSpecs = useMemo(() => {
    const source = workflowArtifactPreview?.workflow
      || workflowSourceFromTemplateSummary(selectedWorkflowTemplate)
    return workflowInputDraftSpecsFromWorkflow(activeWorkflowInputIds, source)
  }, [activeWorkflowInputIds, selectedWorkflowTemplate, workflowArtifactPreview?.workflow])
  const activeWorkflowMissingInputIds = useMemo(() => (
    workflowMissingInputIds(activeWorkflowInputIds, workflowInputValues, activeWorkflowRequiredInputIds, activeWorkflowInputSpecs)
  ), [activeWorkflowInputIds, activeWorkflowInputSpecs, activeWorkflowRequiredInputIds, workflowInputValues])
  const workflowTemplateBaseSteps = useMemo(
    () => workflowStepsForTemplateSource(workflowArtifactPreview, selectedWorkflowTemplate, workflowTemplates),
    [selectedWorkflowTemplate, workflowArtifactPreview, workflowTemplates],
  )
  const workflowMaterializeInputs = useMemo(() => {
    const result: Record<string, unknown> = {}
    for (const input of activeWorkflowInputIds) {
      const parsed = parseWorkflowInputValue(input, workflowInputValueForId(input, workflowInputValues, activeWorkflowInputSpecs), activeWorkflowInputSpecs[input])
      if (parsed !== undefined) result[input] = parsed
    }
    return result
  }, [activeWorkflowInputIds, activeWorkflowInputSpecs, workflowInputValues])
  const workflowPreviewTarget = useMemo(() => {
    if (workflowImportedSpec) return { workflow: workflowImportedSpec }
    if (workflowArtifactPreview?.artifactRef) return { artifact_ref: workflowArtifactPreview.artifactRef }
    if (selectedWorkflowTemplate?.id) return { template_id: selectedWorkflowTemplate.id }
    return null
  }, [selectedWorkflowTemplate?.id, workflowArtifactPreview?.artifactRef, workflowImportedSpec])
  const workflowPreviewRequestKey = useMemo(
    () => workflowPreviewTarget
      ? workflowStableStringify({ target: workflowPreviewTarget, inputs: workflowMaterializeInputs, instance_id: workflowRuntimeInstanceId })
      : "",
    [workflowMaterializeInputs, workflowPreviewTarget, workflowRuntimeInstanceId],
  )
  const workflowTemplateSteps = useMemo(
    () => workflowResolvedPreview?.key === workflowPreviewRequestKey && workflowResolvedPreview.steps.length > 0
      ? workflowResolvedPreview.steps
      : workflowTemplateBaseSteps,
    [workflowPreviewRequestKey, workflowResolvedPreview, workflowTemplateBaseSteps],
  )
  const workflowRuntimeMergedSteps = useMemo(
    () => workflowRuntimeStepSummariesFromPayload(workflowRuntimePayload, workflowTemplateSteps),
    [workflowRuntimePayload, workflowTemplateSteps],
  )
  const activeWorkflowSteps = useMemo(
    () => workflowRuntimeMergedSteps.filter((step) => !workflowStepIsVirtual(step, workflowInputValues, activeWorkflowInputIds)),
    [activeWorkflowInputIds, workflowInputValues, workflowRuntimeMergedSteps],
  )

  useEffect(() => {
    setWorkflowRuntimeInstanceId("")
    setWorkflowRuntimeOrigin(null)
    setWorkflowRunningStepIds([])
    setWorkflowRunningAll(false)
  }, [activeWorkflowTemplateId, workflowArtifactPreview?.artifactRef, workflowImportedSpec])

  useEffect(() => {
    if (workflowRuntimePayload?.instance_id) {
      setWorkflowRuntimeInstanceId(String(workflowRuntimePayload.instance_id))
    }
  }, [workflowRuntimePayload?.instance_id])

  const workflowRuntimeStepStates = useMemo(
    () => workflowRuntimeStepStatesFromPayload(workflowRuntimePayload),
    [workflowRuntimePayload],
  )

  const workflowVirtualStepStates = useMemo(() => {
    const states: Record<string, WorkflowStepNodeState> = {}
    for (const step of workflowRuntimeMergedSteps) {
      if (!workflowStepIsVirtual(step, workflowInputValues, activeWorkflowInputIds)) continue
      states[step.id] = {
        nodeId: "",
        nodeIds: [],
        title: step.title || step.id,
        status: "completed",
        count: 1,
        runningCount: 0,
        failedCount: 0,
        completedCount: 1,
        lastRunSummary: "输入已完成",
      }
    }
    return states
  }, [activeWorkflowInputIds, workflowInputValues, workflowRuntimeMergedSteps])

  const workflowStepNodeStates = useMemo(() => {
    return {
      ...workflowVirtualStepStates,
      ...workflowRuntimeStepStates,
    }
  }, [workflowRuntimeStepStates, workflowVirtualStepStates])

  const workflowRuntimeContext = useMemo(
    () => workflowRuntimeContextFromNodes(nodes, activeWorkflowTemplateId, workflowRuntimeInstanceId),
    [activeWorkflowTemplateId, nodes, workflowRuntimeInstanceId],
  )
  const workflowRunUiOverrides = useMemo(
    () => workflowUiOverridesFromMediaModels(workflowMediaModelOverrides),
    [workflowMediaModelOverrides],
  )

  useEffect(() => {
    setWorkflowMediaModelOverrides({})
  }, [currentProject?.id])

  useEffect(() => {
    if (!currentProject?.id || !workflowPreviewTarget || !workflowPreviewRequestKey) {
      setWorkflowResolvedPreview(null)
      return
    }
    if (Object.keys(workflowMaterializeInputs).length === 0) {
      setWorkflowResolvedPreview(null)
      return
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      void previewProjectWorkflow(currentProject.id, {
        ...workflowPreviewTarget,
        instance_id: workflowRuntimeInstanceId || undefined,
        inputs: workflowMaterializeInputs,
        context: workflowRuntimeContext,
      }).then((result) => {
        if (cancelled) return
        const steps = Array.isArray(result.steps) ? result.steps : []
        setWorkflowResolvedPreview({ key: workflowPreviewRequestKey, steps })
      }).catch((error) => {
        if (cancelled) return
        console.warn("Failed to preview workflow", error)
        setWorkflowResolvedPreview((current) => (
          current?.key === workflowPreviewRequestKey ? null : current
        ))
      })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [
    currentProject?.id,
    workflowMaterializeInputs,
    workflowPreviewRequestKey,
    workflowPreviewTarget,
    workflowRuntimeInstanceId,
    workflowRuntimeContext,
  ])

  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    applyNodeChanges(changes)
  }, [applyNodeChanges])

  const handleEdgesChange = useCallback((changes: EdgeChange[]) => {
    applyEdgeChanges(changes)
  }, [applyEdgeChanges])

  const handleGroupedNodeIdsChange = useCallback((nodeIds: string[]) => {
    setGroupedNodeIds((current) => current.join("\0") === nodeIds.join("\0") ? current : nodeIds)
  }, [])

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return
    const query = window.matchMedia("(pointer: coarse)")
    const update = () => setCoarsePointer(query.matches)
    update()
    query.addEventListener("change", update)
    return () => query.removeEventListener("change", update)
  }, [])

  const refreshCanvas = useCallback(async (options?: { preserveOnEmpty?: boolean; preserveLayout?: boolean; fitView?: boolean }) => {
    if (!currentProject?.id) return
    const canvas = await getProjectNodes(currentProject.id)
    const rawNodes = (canvas.nodes || []) as Parameters<typeof loadCanvasNodes>[0]
    const rawEdges = (canvas.edges || []) as Parameters<typeof loadCanvasNodes>[1]
    console.debug("[openreel:workflow refreshCanvas]", {
      projectId: currentProject.id,
      rawNodes: rawNodes.length,
      rawEdges: rawEdges.length,
      options,
    })
    loadCanvasNodes(rawNodes, rawEdges, options)
    if (options?.fitView && rawNodes.length > 0) {
      window.requestAnimationFrame(() => {
        flowInstance?.fitView({ padding: 0.3, duration: 180 })
      })
    }
  }, [currentProject?.id, flowInstance, loadCanvasNodes])

  const refreshMediaHistory = useCallback(async () => {
    if (!currentProject?.id) {
      setMediaHistoryItems([])
      return
    }
    setMediaHistoryLoading(true)
    setMediaHistoryError(null)
    try {
      const result = await listProjectMediaHistory(currentProject.id)
      setMediaHistoryItems(Array.isArray(result.items) ? result.items : [])
    } catch (error) {
      setMediaHistoryError(error instanceof Error ? error.message : String(error))
    } finally {
      setMediaHistoryLoading(false)
    }
  }, [currentProject?.id])

  const saveActiveWorkflowSelection = useCallback(async (input: ProjectActiveWorkflow) => {
    if (!currentProject?.id) return null
    const result = await setProjectActiveWorkflow(currentProject.id, input)
    const selectedTemplateId = workflowPreviewFromActiveWorkflow(result.active_workflow)?.id
      || workflowTemplateIdFromActiveWorkflow(result.active_workflow)
    const matchingRuntime = selectedTemplateId
      ? [result.active_workflow_runtime, ...(result.active_workflow_runtimes || [])]
        .find((runtime): runtime is ProjectWorkflowRuntime => Boolean(runtime && workflowStringValue(runtime.template_id) === selectedTemplateId)) || null
      : result.active_workflow_runtime ?? null
    replaceWorkflowRuntimePayloads(result.active_workflow_runtimes, matchingRuntime, false)
    return result.active_workflow ?? null
  }, [currentProject?.id, replaceWorkflowRuntimePayloads])

  const refreshWorkflowTemplates = useCallback(async () => {
    if (!currentProject?.id) {
      setWorkflowTemplates([])
      setSelectedWorkflowTemplateId("")
      setWorkflowArtifactPreview(null)
      setWorkflowImportedSpec(null)
      replaceWorkflowRuntimePayloads([])
      return
    }
    setWorkflowTemplatesLoading(true)
    setWorkflowTemplatesError(null)
    try {
      const result = await listWorkflowTemplates(currentProject.id)
      const templates = Array.isArray(result.templates) ? result.templates : []
      const activeWorkflow = result.active_workflow
      const serverInputValues = workflowInputValuesFromObject(result.workflow_input_values)
      if (Object.keys(serverInputValues).length > 0) {
        const serverRuntimeId = workflowStringValue(result.active_workflow_runtime?.instance_id)
        if (serverRuntimeId) {
          setWorkflowInstanceInputValues((current) => ({
            ...current,
            [serverRuntimeId]: { ...(current[serverRuntimeId] || {}), ...serverInputValues },
          }))
        } else {
          setWorkflowInputValues((current) => ({ ...current, ...serverInputValues }))
        }
      }
      const activePreview = workflowPreviewFromActiveWorkflow(activeWorkflow)
      const activeTemplateId = workflowTemplateIdFromActiveWorkflow(activeWorkflow)
      const runtimeTemplateId = workflowStringValue(result.active_workflow_runtime?.template_id)
      const selectedTemplateId = activePreview?.id || activeTemplateId || runtimeTemplateId
      const matchingRuntime = selectedTemplateId
        ? [result.active_workflow_runtime, ...(result.active_workflow_runtimes || [])]
          .find((runtime): runtime is ProjectWorkflowRuntime => Boolean(runtime && workflowStringValue(runtime.template_id) === selectedTemplateId)) || null
        : result.active_workflow_runtime ?? null
      replaceWorkflowRuntimePayloads(result.active_workflow_runtimes, matchingRuntime, false)
      setWorkflowTemplates(templates)
      if (activePreview) {
        const canonicalTemplate = templates.find((template) => template.id === activePreview.id)
        const activeLooksRuntimePolluted = activePreview.source === "imported" && workflowStepsContainRuntimeInstances(activePreview.steps)
        if (canonicalTemplate && activeLooksRuntimePolluted) {
          setWorkflowArtifactPreview(null)
          setWorkflowImportedSpec(null)
          setSelectedWorkflowTemplateId(canonicalTemplate.id)
          return
        }
        setWorkflowArtifactPreview(activePreview)
        setWorkflowImportedSpec(activePreview.source === "imported" ? activePreview.workflow || null : null)
        setSelectedWorkflowTemplateId((current) => {
          if (activePreview.id && templates.some((template) => template.id === activePreview.id)) return activePreview.id
          if (current && templates.some((template) => template.id === current)) return current
          return templates[0]?.id || ""
        })
      } else {
        setWorkflowArtifactPreview(null)
        setWorkflowImportedSpec(null)
        setSelectedWorkflowTemplateId(() => {
          if (activeTemplateId && templates.some((template) => template.id === activeTemplateId)) {
            return activeTemplateId
          }
          if (runtimeTemplateId && templates.some((template) => template.id === runtimeTemplateId)) {
            return runtimeTemplateId
          }
          return templates[0]?.id || ""
        })
      }
    } catch (error) {
      setWorkflowTemplatesError(error instanceof Error ? error.message : String(error))
      setWorkflowTemplates([])
      setSelectedWorkflowTemplateId("")
      setWorkflowArtifactPreview(null)
      setWorkflowImportedSpec(null)
      replaceWorkflowRuntimePayloads([])
    } finally {
      setWorkflowTemplatesLoading(false)
    }
  }, [currentProject?.id, replaceWorkflowRuntimePayloads])

  const refreshWorkflowNodeTypes = useCallback(async () => {
    setWorkflowNodeTypesError(null)
    try {
      const result = await listWorkflowNodeTypes()
      setWorkflowNodeTypes(Array.isArray(result.node_types) ? result.node_types : [])
    } catch (error) {
      setWorkflowNodeTypes([])
      setWorkflowNodeTypesError(error instanceof Error ? error.message : String(error))
    }
  }, [])

  useEffect(() => {
    void refreshWorkflowTemplates()
  }, [refreshWorkflowTemplates])

  useEffect(() => {
    const handleWorkflowRefresh = (event: Event) => {
      const detail = (event as CustomEvent<WorkflowRefreshOptions>).detail || {}
      if (detail.projectId && detail.projectId !== currentProject?.id) return
      void refreshWorkflowTemplates()
    }
    window.addEventListener(WORKFLOW_REFRESH_EVENT, handleWorkflowRefresh)
    return () => window.removeEventListener(WORKFLOW_REFRESH_EVENT, handleWorkflowRefresh)
  }, [currentProject?.id, refreshWorkflowTemplates])

  useEffect(() => {
    void refreshWorkflowNodeTypes()
  }, [refreshWorkflowNodeTypes])

  const handleWorkflowTemplateSelection = useCallback((id: string) => {
    workflowRuntimeAutoSelectSuppressedRef.current = false
    setWorkflowArtifactPreview(null)
    setWorkflowImportedSpec(null)
    setSelectedWorkflowTemplateId(id)
    setWorkflowTemplatesError(null)
    if (!id) return
    void saveActiveWorkflowSelection({ kind: "template", template_id: id }).catch((error) => {
      setWorkflowTemplatesError(workflowErrorMessage(error))
    })
  }, [saveActiveWorkflowSelection])

  useEffect(() => {
    const handleWorkflowSpecPreview = (event: Event) => {
      const preview = workflowArtifactPreviewFromEvent((event as CustomEvent).detail)
      if (!preview) return
      setWorkflowImportedSpec(null)
      setWorkflowArtifactPreview(preview)
      onWorkspaceViewChange?.("workflow")
      setWorkflowTemplatesError(null)
      void saveActiveWorkflowSelection({
        kind: "artifact",
        artifact_ref: preview.artifactRef,
        name: preview.name,
        description: preview.description,
      }).then((active) => {
        const restored = workflowPreviewFromActiveWorkflow(active)
        if (restored) {
          setWorkflowArtifactPreview(restored)
          setWorkflowImportedSpec(null)
        }
      }).catch((error) => {
        setWorkflowTemplatesError(workflowErrorMessage(error))
      })
    }
    window.addEventListener("openreel:workflow-spec-preview", handleWorkflowSpecPreview)
    return () => window.removeEventListener("openreel:workflow-spec-preview", handleWorkflowSpecPreview)
  }, [onWorkspaceViewChange, saveActiveWorkflowSelection])

  useEffect(() => {
    const inputs = activeWorkflowInputIds
    const stored = readStoredWorkflowInputs(currentProject?.id, activeWorkflowTemplateId)
    const snapshot = workflowRuntimeSnapshotFromNodes(nodes, activeWorkflowTemplateId, inputs)
    setWorkflowInputValues((current) => {
      const next: Record<string, string> = {}
      for (const input of inputs) {
        const currentValue = current[input]
        next[input] = String(currentValue || "").trim()
          ? currentValue
          : snapshot.values[input] || stored[input] || activeWorkflowInputSpecs[input]?.default || ""
      }
      return next
    })
  }, [activeWorkflowInputIds, activeWorkflowInputSpecs, activeWorkflowTemplateId, currentProject?.id, nodes])

  useEffect(() => {
    writeStoredWorkflowInputs(currentProject?.id, activeWorkflowTemplateId, workflowInputValues)
  }, [activeWorkflowTemplateId, currentProject?.id, workflowInputValues])

  const updateWorkflowInputValue = useCallback((id: string, value: string) => {
    setWorkflowInputValues((current) => ({ ...current, [id]: value }))
  }, [])

  const updateWorkflowMediaModelOverride = useCallback((stepId: string, value: string) => {
    setWorkflowMediaModelOverrides((current) => {
      const key = stepId.trim()
      if (!key) return current
      const nextValue = value.trim()
      const currentValue = current[key] || ""
      if (currentValue === nextValue) return current
      const next = { ...current }
      if (nextValue) next[key] = nextValue
      else delete next[key]
      return next
    })
  }, [])

  const importWorkflowSpecFile = useCallback(async (file: File) => {
    setWorkflowTemplatesError(null)
    if (!currentProject?.id) {
      setWorkflowTemplatesError("项目加载后才能导入流程。")
      return
    }
    try {
      const text = await file.text()
      const parsed = JSON.parse(text) as unknown
      const preview = workflowPreviewFromImportedSpec(parsed, file.name)
      if (!preview?.workflow) throw new Error("JSON 中没有可导入的 workflow.steps")
      const workflow = preview.workflow
      const result = await saveWorkflowTemplate(currentProject.id, {
        workflow,
        template_id: String(workflow.id || preview.id || ""),
        name: preview.name,
        description: preview.description,
        category: workflowStringValue(workflow.category) || "user",
        applies_to: workflowStringValue(workflow.applies_to),
        version: workflowStringValue(workflow.version),
        replace_existing: false,
      })
      const templateId = String(result.template_id || result.summary?.id || "").trim()
      if (!templateId) throw new Error("导入流程已保存，但没有返回模板 ID")
      setWorkflowImportedSpec(null)
      setWorkflowArtifactPreview(null)
      setSelectedWorkflowTemplateId(templateId)
      await saveActiveWorkflowSelection({ kind: "template", template_id: templateId })
      await refreshWorkflowTemplates()
      onWorkspaceViewChange?.("workflow")
    } catch (error) {
      setWorkflowTemplatesError(error instanceof Error ? error.message : String(error))
    }
  }, [currentProject?.id, onWorkspaceViewChange, refreshWorkflowTemplates, saveActiveWorkflowSelection])

  const saveWorkflowEditorSpec = useCallback(async (workflow: Record<string, unknown>) => {
    setWorkflowTemplatesError(null)
    if (!currentProject?.id) throw new Error("项目加载后才能保存工作流。")
    try {
      await previewProjectWorkflow(currentProject.id, {
        workflow,
        inputs: workflowInputValues,
        context: workflowRuntimeContext,
      })
    } catch (error) {
      const message = `工作流校验失败：${workflowErrorMessage(error)}`
      setWorkflowTemplatesError(message)
      throw new Error(message)
    }
    const active = await saveActiveWorkflowSelection({
      kind: "imported",
      workflow,
      name: String(workflow.name || "编辑的工作流"),
      description: String(workflow.description || ""),
    })
    const restored = workflowPreviewFromActiveWorkflow(active) || workflowPreviewFromImportedSpec({ workflow }, String(workflow.name || "编辑的工作流"))
    if (restored) {
      setWorkflowImportedSpec(restored.workflow || workflow)
      setWorkflowArtifactPreview(restored)
      setSelectedWorkflowTemplateId((current) => current || workflowTemplates[0]?.id || "")
    } else {
      setWorkflowImportedSpec(workflow)
    }
    replaceWorkflowRuntimePayloads([])
    onWorkspaceViewChange?.("workflow")
  }, [currentProject?.id, onWorkspaceViewChange, replaceWorkflowRuntimePayloads, saveActiveWorkflowSelection, workflowInputValues, workflowRuntimeContext, workflowTemplates])

  const saveWorkflowEditorTemplate = useCallback(async (
    workflow: Record<string, unknown>,
    options: { templateId?: string; replaceExisting?: boolean },
  ) => {
    setWorkflowTemplatesError(null)
    if (!currentProject?.id) throw new Error("项目加载后才能保存模板。")
    try {
      await previewProjectWorkflow(currentProject.id, {
        workflow,
        inputs: workflowInputValues,
        context: workflowRuntimeContext,
      })
    } catch (error) {
      const message = `工作流校验失败：${workflowErrorMessage(error)}`
      setWorkflowTemplatesError(message)
      throw new Error(message)
    }
    const result = await saveWorkflowTemplate(currentProject.id, {
      workflow,
      template_id: options.templateId || String(workflow.id || ""),
      name: String(workflow.name || "未命名流程"),
      description: String(workflow.description || ""),
      category: "user",
      replace_existing: Boolean(options.replaceExisting),
      inputs: workflowMaterializeInputs,
    })
    const templateId = String(result.template_id || result.summary?.id || options.templateId || "")
    if (templateId) {
      setWorkflowArtifactPreview(null)
      setWorkflowImportedSpec(null)
      setSelectedWorkflowTemplateId(templateId)
      await saveActiveWorkflowSelection({ kind: "template", template_id: templateId })
    }
    await refreshWorkflowTemplates()
    return templateId
  }, [
    currentProject?.id,
    refreshWorkflowTemplates,
    saveActiveWorkflowSelection,
    workflowInputValues,
    workflowMaterializeInputs,
    workflowRuntimeContext,
  ])

  const downloadWorkflowTemplate = useCallback(async (template: WorkflowTemplateSummary) => {
    if (!currentProject?.id) throw new Error("项目加载后才能下载模板。")
    if (!template.downloadable) throw new Error("内置流程需要先保存为模板再下载。")
    const result = await downloadWorkflowTemplatePackage(
      currentProject.id,
      template.id,
      String(template.active_version_id || ""),
    )
    downloadJsonPayload(
      result.package,
      result.filename || `${template.id || "workflow_template"}.openreel-workflow-template.json`,
    )
  }, [currentProject?.id])

  const restoreWorkflowEditorBuiltinTemplate = useCallback(async (template: WorkflowTemplateSummary) => {
    setWorkflowTemplatesError(null)
    if (!currentProject?.id) throw new Error("项目加载后才能恢复内置模板。")
    if (!template.overrides_builtin) throw new Error("当前模板没有可恢复的内置版本。")
    const result = await restoreBuiltinWorkflowTemplate(currentProject.id, template.id)
    const templateId = String(result.template_id || result.summary?.id || template.id).trim()
    setWorkflowArtifactPreview(null)
    setWorkflowImportedSpec(null)
    setSelectedWorkflowTemplateId(templateId)
    await saveActiveWorkflowSelection({ kind: "template", template_id: templateId })
    await refreshWorkflowTemplates()
  }, [currentProject?.id, refreshWorkflowTemplates, saveActiveWorkflowSelection])

  const materializeSelectedWorkflow = useCallback(async () => {
    if (!currentProject?.id || workflowMaterializing) return
    const template = selectedWorkflowTemplate
    const importedWorkflow = workflowImportedSpec
    const artifact = importedWorkflow ? null : workflowArtifactPreview
    if (!template && !artifact && !importedWorkflow) return
    const missingInputs = activeWorkflowMissingInputIds
    if (missingInputs.length > 0) {
      setWorkflowTemplatesError(`先输入 ${missingInputs.map(workflowInputLabel).join("、")}`)
      return
    }
    setWorkflowMaterializing(true)
    setWorkflowTemplatesError(null)
    try {
      const rect = workspaceView === "canvas" ? canvasContainerRef.current?.getBoundingClientRect() : null
      const point = rect
        ? { x: rect.left + rect.width * 0.46, y: rect.top + rect.height * 0.38 }
        : null
      const position = point && flowInstance ? flowInstance.screenToFlowPosition(point) : { x: 120, y: 120 }
      const result = await materializeProjectWorkflow<{
        ok?: boolean
        instance_id?: string
        nodes?: Array<{ _canvas_id?: string; id?: string }>
        runtime?: ProjectWorkflowRuntime
        error?: string
      }>(currentProject.id, {
        ...(importedWorkflow
          ? { workflow: importedWorkflow }
          : artifact
          ? { artifact_ref: artifact.artifactRef }
          : { template_id: template?.id }),
        inputs: workflowMaterializeInputs,
        context: workflowRuntimeContext,
        origin_x: position.x,
        origin_y: position.y,
      })
      if (result?.ok === false) throw new Error(String(result.error || "工作流加载失败"))
      if (result.instance_id) setWorkflowRuntimeInstanceId(String(result.instance_id))
      if (result.runtime) upsertWorkflowRuntimePayload(result.runtime)
      setWorkflowRuntimeOrigin(position)
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
      const firstNodeId = result.nodes?.[0]?._canvas_id || result.nodes?.[0]?.id
      if (firstNodeId) selectNode(String(firstNodeId))
    } catch (error) {
      setWorkflowTemplatesError(workflowErrorMessage(error))
    } finally {
      setWorkflowMaterializing(false)
    }
  }, [
    currentProject?.id,
    activeWorkflowMissingInputIds,
    flowInstance,
    refreshCanvas,
    selectNode,
    selectedWorkflowTemplate,
    workflowArtifactPreview,
    workflowImportedSpec,
    workflowMaterializeInputs,
    workflowMaterializing,
    workflowRuntimeContext,
    upsertWorkflowRuntimePayload,
    workspaceView,
  ])

  const workflowCanvasOrigin = useCallback(() => {
    const rect = workspaceView === "canvas" ? canvasContainerRef.current?.getBoundingClientRect() : null
    const point = rect
      ? { x: rect.left + rect.width * 0.46, y: rect.top + rect.height * 0.38 }
      : null
    return point && flowInstance ? flowInstance.screenToFlowPosition(point) : { x: 120, y: 120 }
  }, [flowInstance, workspaceView])

  const uploadWorkflowVideoInput = useCallback(async (file: File): Promise<string> => {
    if (!currentProject?.id) throw new Error("项目加载后才能上传视频。")
    const origin = workflowCanvasOrigin()
    const created = await createProjectNode(currentProject.id, {
      type: "video",
      title: file.name ? `输入视频：${file.name}` : "输入视频",
      x: origin.x,
      y: origin.y,
    })
    const nodeId = workflowStringValue(created.id)
    if (!nodeId) throw new Error("视频节点创建失败。")
    const uploaded = await uploadProjectNodeMedia<Record<string, unknown>>(currentProject.id, nodeId, file)
    await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
    const publicId = workflowStringValue(uploaded.display_id || created.display_id)
    return `node:${publicId || nodeId}`
  }, [currentProject?.id, refreshCanvas, workflowCanvasOrigin])

  const workflowRunTarget = useCallback(() => {
    if (workflowImportedSpec) return { workflow: workflowImportedSpec }
    if (workflowArtifactPreview?.artifactRef) return { artifact_ref: workflowArtifactPreview.artifactRef }
    if (selectedWorkflowTemplate?.id) return { template_id: selectedWorkflowTemplate.id }
    return null
  }, [selectedWorkflowTemplate?.id, workflowArtifactPreview?.artifactRef, workflowImportedSpec])

  const workflowInputsForTemplateId = useCallback((templateId: string): string[] => {
    const template = workflowTemplateById.get(templateId)
    return workflowInputsForTemplateSource(null, template, workflowTemplates)
  }, [workflowTemplateById, workflowTemplates])

  const workflowRequiredInputsForTemplateId = useCallback((templateId: string): string[] => {
    const template = workflowTemplateById.get(templateId)
    return workflowRequiredInputsForTemplateSource(null, template, workflowTemplates)
  }, [workflowTemplateById, workflowTemplates])

  const workflowInputSpecsForTemplateId = useCallback((templateId: string): Record<string, WorkflowInputDraftSpec> => {
    const template = workflowTemplateById.get(templateId)
    const source = workflowSourceFromTemplateSummary(template)
    return workflowInputDraftSpecsFromWorkflow(workflowInputsForTemplateId(templateId), source)
  }, [workflowInputsForTemplateId, workflowTemplateById])

  const workflowInputValuesForInstance = useCallback((instanceId: string): Record<string, string> => (
    workflowInstanceInputValues[instanceId] || {}
  ), [workflowInstanceInputValues])

  const workflowMaterializeInputsForTemplateId = useCallback((templateId: string, instanceId = ""): Record<string, unknown> => {
    const result: Record<string, unknown> = {}
    const inputSpecs = workflowInputSpecsForTemplateId(templateId)
    const sourceValues = instanceId ? workflowInputValuesForInstance(instanceId) : workflowInputValues
    for (const input of workflowInputsForTemplateId(templateId)) {
      const parsed = parseWorkflowInputValue(input, workflowInputValueForId(input, sourceValues, inputSpecs), inputSpecs[input])
      if (parsed !== undefined) result[input] = parsed
    }
    return result
  }, [workflowInputSpecsForTemplateId, workflowInputValues, workflowInputValuesForInstance, workflowInputsForTemplateId])

  const workflowMissingInputsForTemplateId = useCallback((templateId: string, instanceId = ""): string[] => (
    workflowMissingInputIds(
      workflowInputsForTemplateId(templateId),
      instanceId ? workflowInputValuesForInstance(instanceId) : workflowInputValues,
      workflowRequiredInputsForTemplateId(templateId),
      workflowInputSpecsForTemplateId(templateId),
    )
  ), [workflowInputSpecsForTemplateId, workflowInputValues, workflowInputValuesForInstance, workflowInputsForTemplateId, workflowRequiredInputsForTemplateId])

  const updateWorkflowInstanceInputValue = useCallback((runtimeId: string, _templateId: string, id: string, value: string) => {
    if (!runtimeId || !id) return
    setWorkflowInstanceInputValues((current) => ({
      ...current,
      [runtimeId]: {
        ...(current[runtimeId] || {}),
        [id]: value,
      },
    }))
  }, [])

  const addWorkflowRunDraft = useCallback(() => {
    const template = workflowTemplateById.get(workflowDockTemplateId) || selectedWorkflowTemplate || workflowTemplates[0]
    if (!template) return
    workflowRuntimeAutoSelectSuppressedRef.current = false
    const draft = createWorkflowRuntimeDraft(template)
    const draftId = workflowRuntimeId(draft)
    setWorkflowInstanceInputValues((current) => ({ ...current, [draftId]: {} }))
    setWorkflowRuntimePayloads((current) => mergeWorkflowRuntimePayloads(current, draft))
    setWorkflowRuntimePayload(draft)
    setWorkflowRuntimeInstanceId(draftId)
    setWorkflowDockExpandedRunIds((current) => current.includes(draftId) ? current : [draftId, ...current])
    setWorkflowDockOpen(true)
  }, [selectedWorkflowTemplate, workflowDockTemplateId, workflowTemplateById, workflowTemplates])

  const setWorkflowInstanceRunning = useCallback((instanceId: string, running: boolean) => {
    setWorkflowInstanceRunningIds((current) => (
      running
        ? current.includes(instanceId) ? current : [...current, instanceId]
        : current.filter((id) => id !== instanceId)
    ))
  }, [])

  const setWorkflowInstanceRunningAll = useCallback((instanceId: string, running: boolean) => {
    setWorkflowInstanceRunningAllIds((current) => (
      running
        ? current.includes(instanceId) ? current : [...current, instanceId]
        : current.filter((id) => id !== instanceId)
    ))
  }, [])

  const setWorkflowInstancePausing = useCallback((instanceId: string, pausing: boolean) => {
    setWorkflowInstancePausingIds((current) => (
      pausing
        ? current.includes(instanceId) ? current : [...current, instanceId]
        : current.filter((id) => id !== instanceId)
    ))
  }, [])

  const setWorkflowInstanceDeleting = useCallback((instanceId: string, deleting: boolean) => {
    setWorkflowInstanceDeletingIds((current) => (
      deleting
        ? current.includes(instanceId) ? current : [...current, instanceId]
        : current.filter((id) => id !== instanceId)
    ))
  }, [])

  const removeWorkflowRuntimeLocally = useCallback((
    instanceId: string,
    syncedRuntimes?: ProjectWorkflowRuntime[] | null,
    options?: { selectFallback?: boolean },
  ) => {
    setWorkflowRuntimePayloads((current) => {
      const withoutTarget = current.filter((runtime) => workflowRuntimeId(runtime) !== instanceId)
      const next = syncedRuntimes ? mergeWorkflowRuntimePayloads(withoutTarget, syncedRuntimes) : withoutTarget
      const selectedStillExists = workflowRuntimeInstanceId && next.some((runtime) => workflowRuntimeId(runtime) === workflowRuntimeInstanceId)
      if (!selectedStillExists) {
        const nextSelected = options?.selectFallback === false ? null : next[0] || null
        setWorkflowRuntimePayload(nextSelected)
        setWorkflowRuntimeInstanceId(workflowRuntimeId(nextSelected))
      }
      return next
    })
    setWorkflowDockExpandedRunIds((current) => current.filter((id) => id !== instanceId))
    setWorkflowInstanceRunningIds((current) => current.filter((id) => id !== instanceId))
    setWorkflowInstanceRunningAllIds((current) => current.filter((id) => id !== instanceId))
    setWorkflowInstancePausingIds((current) => current.filter((id) => id !== instanceId))
    setWorkflowInstanceDeletingIds((current) => current.filter((id) => id !== instanceId))
    setWorkflowInstanceInputValues((current) => {
      if (!(instanceId in current)) return current
      const next = { ...current }
      delete next[instanceId]
      return next
    })
    setWorkflowDockDetail((current) => current?.runtimeId === instanceId ? null : current)
    setWorkflowInstanceErrors((current) => {
      if (!(instanceId in current)) return current
      const next = { ...current }
      delete next[instanceId]
      return next
    })
  }, [workflowRuntimeInstanceId])

  const deleteWorkflowRun = useCallback(async (runtime: ProjectWorkflowRuntime) => {
    const instanceId = workflowRuntimeId(runtime)
    if (!instanceId || workflowInstanceDeletingIds.includes(instanceId)) return
    workflowRuntimeAutoSelectSuppressedRef.current = true
    removeWorkflowRuntimeLocally(instanceId, null, { selectFallback: false })
    if (!currentProject?.id || runtime.local_draft) return
    setWorkflowInstanceDeleting(instanceId, true)
    try {
      await deleteProjectWorkflowRuntime(currentProject.id, instanceId)
      removeWorkflowRuntimeLocally(instanceId, null, { selectFallback: false })
    } catch (error) {
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: workflowErrorMessage(error) }))
      void refreshWorkflowTemplates()
    } finally {
      setWorkflowInstanceDeleting(instanceId, false)
    }
  }, [
    currentProject?.id,
    refreshWorkflowTemplates,
    removeWorkflowRuntimeLocally,
    setWorkflowInstanceDeleting,
    workflowInstanceDeletingIds,
  ])

  const inspectWorkflowRunStep = useCallback((
    runtime: ProjectWorkflowRuntime,
    step: WorkflowTemplateStepSummary,
    rawStep?: ProjectWorkflowRuntimeStep,
    state?: WorkflowStepNodeState,
  ) => {
    const runtimeId = workflowRuntimeId(runtime)
    if (!runtimeId || !step.id) return
    setWorkflowDockDetail((current) => (
      current?.runtimeId === runtimeId && current.stepId === step.id
        ? null
        : { runtimeId, stepId: step.id }
    ))
  }, [])

  const runWorkflowInstanceStep = useCallback(async (runtime: ProjectWorkflowRuntime, stepId: string) => {
    if (!currentProject?.id || !stepId) return
    const instanceId = workflowRuntimeId(runtime)
    const templateId = workflowRuntimeTemplateId(runtime)
    if (!instanceId || !templateId) return
    const missingInputs = workflowMissingInputsForTemplateId(templateId, instanceId)
    if (missingInputs.length > 0) {
      const message = `先输入 ${missingInputs.map(workflowInputLabel).join("、")}`
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: message }))
      return
    }
    const origin = workflowCanvasOrigin()
    setWorkflowInstanceRunning(instanceId, true)
    setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: "" }))
    try {
      const result = await runProjectWorkflowStep<{
        ok?: boolean
        instance_id?: string
        runtime?: ProjectWorkflowRuntime
        error?: string
      }>(currentProject.id, {
        template_id: templateId,
        instance_id: instanceId,
        step_id: stepId,
        inputs: workflowMaterializeInputsForTemplateId(templateId, instanceId),
        context: workflowRuntimeContextFromNodes(nodes, templateId, instanceId),
        ui_overrides: workflowRunUiOverrides,
        origin_x: origin.x,
        origin_y: origin.y,
      })
      if (result?.ok === false) throw new Error(String(result.error || "步骤运行失败"))
      if (result?.runtime) upsertWorkflowRuntimePayload(result.runtime)
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
      await refreshWorkflowTemplates()
    } catch (error) {
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: workflowErrorMessage(error) }))
      try {
        await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      } catch {
        // Keep the per-flow error if refresh fails.
      }
    } finally {
      setWorkflowInstanceRunning(instanceId, false)
    }
  }, [
    currentProject?.id,
    nodes,
    refreshCanvas,
    refreshWorkflowTemplates,
    setWorkflowInstanceRunning,
    upsertWorkflowRuntimePayload,
    workflowCanvasOrigin,
    workflowRunUiOverrides,
    workflowMaterializeInputsForTemplateId,
    workflowMissingInputsForTemplateId,
  ])

  const runWorkflowInstanceNext = useCallback(async (runtime: ProjectWorkflowRuntime) => {
    if (!currentProject?.id) return
    const instanceId = workflowRuntimeId(runtime)
    const templateId = workflowRuntimeTemplateId(runtime)
    if (!instanceId || !templateId) return
    const missingInputs = workflowMissingInputsForTemplateId(templateId, instanceId)
    if (missingInputs.length > 0) {
      const message = `先输入 ${missingInputs.map(workflowInputLabel).join("、")}`
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: message }))
      return
    }
    const origin = workflowCanvasOrigin()
    setWorkflowInstanceRunning(instanceId, true)
    setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: "" }))
    try {
      const result = await runProjectWorkflowNextStep<{
        ok?: boolean
        done?: boolean
        instance_id?: string
        runtime?: ProjectWorkflowRuntime
        error?: string
      }>(currentProject.id, {
        template_id: templateId,
        instance_id: instanceId,
        inputs: workflowMaterializeInputsForTemplateId(templateId, instanceId),
        context: workflowRuntimeContextFromNodes(nodes, templateId, instanceId),
        ui_overrides: workflowRunUiOverrides,
        origin_x: origin.x,
        origin_y: origin.y,
      })
      if (result?.ok === false) throw new Error(String(result.error || "步骤运行失败"))
      if (result?.runtime) upsertWorkflowRuntimePayload(result.runtime)
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
      await refreshWorkflowTemplates()
    } catch (error) {
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: workflowErrorMessage(error) }))
      try {
        await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      } catch {
        // Keep the per-flow error if refresh fails.
      }
    } finally {
      setWorkflowInstanceRunning(instanceId, false)
    }
  }, [
    currentProject?.id,
    nodes,
    refreshCanvas,
    refreshWorkflowTemplates,
    setWorkflowInstanceRunning,
    upsertWorkflowRuntimePayload,
    workflowCanvasOrigin,
    workflowRunUiOverrides,
    workflowMaterializeInputsForTemplateId,
    workflowMissingInputsForTemplateId,
  ])

  const runWorkflowInstanceAll = useCallback(async (runtime: ProjectWorkflowRuntime) => {
    if (!currentProject?.id) return
    const instanceId = workflowRuntimeId(runtime)
    const templateId = workflowRuntimeTemplateId(runtime)
    if (!instanceId || !templateId) return
    const missingInputs = workflowMissingInputsForTemplateId(templateId, instanceId)
    if (missingInputs.length > 0) {
      const message = `先输入 ${missingInputs.map(workflowInputLabel).join("、")}`
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: message }))
      return
    }
    const template = workflowTemplateById.get(templateId)
    const origin = workflowCanvasOrigin()
    setWorkflowInstanceRunning(instanceId, true)
    setWorkflowInstanceRunningAll(instanceId, true)
    setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: "" }))
    try {
      const result = await runProjectWorkflowAllSteps<{
        ok?: boolean
        done?: boolean
        instance_id?: string
        runtime?: ProjectWorkflowRuntime
        error?: string
      }>(currentProject.id, {
        template_id: templateId,
        instance_id: instanceId,
        inputs: workflowMaterializeInputsForTemplateId(templateId, instanceId),
        context: workflowRuntimeContextFromNodes(nodes, templateId, instanceId),
        ui_overrides: workflowRunUiOverrides,
        origin_x: origin.x,
        origin_y: origin.y,
        max_steps: Math.max((template?.steps?.length || 0) + 20, 120),
      })
      if (result?.ok === false) throw new Error(String(result.error || "工作流执行失败"))
      if (result?.runtime) upsertWorkflowRuntimePayload(result.runtime)
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
      await refreshWorkflowTemplates()
    } catch (error) {
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: workflowErrorMessage(error) }))
      try {
        await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      } catch {
        // Keep the per-flow error if refresh fails.
      }
    } finally {
      setWorkflowInstanceRunning(instanceId, false)
      setWorkflowInstanceRunningAll(instanceId, false)
    }
  }, [
    currentProject?.id,
    nodes,
    refreshCanvas,
    refreshWorkflowTemplates,
    setWorkflowInstanceRunningAll,
    setWorkflowInstanceRunning,
    upsertWorkflowRuntimePayload,
    workflowCanvasOrigin,
    workflowRunUiOverrides,
    workflowMaterializeInputsForTemplateId,
    workflowMissingInputsForTemplateId,
    workflowTemplateById,
  ])

  const pauseWorkflowInstanceRun = useCallback(async (runtime: ProjectWorkflowRuntime) => {
    if (!currentProject?.id) return
    const instanceId = workflowRuntimeId(runtime)
    if (!instanceId || workflowInstancePausingIds.includes(instanceId)) return
    const templateId = workflowRuntimeTemplateId(runtime)
    setWorkflowInstancePausing(instanceId, true)
    setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: "" }))
    try {
      const result = await pauseProjectWorkflowRun(currentProject.id, {
        instance_id: instanceId,
        template_id: templateId,
        reason: "user_requested",
      })
      if (result?.runtime) upsertWorkflowRuntimePayload(result.runtime)
      if (result?.active_workflow_runtimes) {
        replaceWorkflowRuntimePayloads(result.active_workflow_runtimes, result.runtime || undefined)
      }
      await refreshWorkflowTemplates()
    } catch (error) {
      setWorkflowInstanceErrors((current) => ({ ...current, [instanceId]: workflowErrorMessage(error) }))
    } finally {
      setWorkflowInstancePausing(instanceId, false)
    }
  }, [
    currentProject?.id,
    refreshWorkflowTemplates,
    replaceWorkflowRuntimePayloads,
    setWorkflowInstancePausing,
    upsertWorkflowRuntimePayload,
    workflowInstancePausingIds,
  ])

  const runWorkflowStepInternal = useCallback(async (
    stepId: string,
    options?: { origin?: { x: number; y: number }; instanceId?: string },
  ) => {
    if (!currentProject?.id) return null
    const target = workflowRunTarget()
    if (!target) return null
    const missingInputs = activeWorkflowMissingInputIds
    if (missingInputs.length > 0) {
      const message = `先输入 ${missingInputs.map(workflowInputLabel).join("、")}`
      setWorkflowTemplatesError(message)
      throw new Error(message)
    }
    const origin = options?.origin ?? workflowRuntimeOrigin ?? workflowCanvasOrigin()
    if (!workflowRuntimeOrigin) setWorkflowRuntimeOrigin(origin)
    const targetInstanceId = options?.instanceId || workflowRuntimeInstanceId || createWorkflowRuntimeInstanceId()
    if (!options?.instanceId && !workflowRuntimeInstanceId) setWorkflowRuntimeInstanceId(targetInstanceId)
    setWorkflowRunningStepIds((current) => current.includes(stepId) ? current : [...current, stepId])
    setWorkflowTemplatesError(null)
    try {
      const result = await runProjectWorkflowStep<{
        ok?: boolean
        instance_id?: string
        node_id?: string
        node?: { id?: string }
        runtime?: ProjectWorkflowRuntime
        error?: string
      }>(currentProject.id, {
        ...target,
        step_id: stepId,
        instance_id: targetInstanceId,
        inputs: workflowMaterializeInputs,
        ui_overrides: workflowRunUiOverrides,
        origin_x: origin.x,
        origin_y: origin.y,
      })
      if (result?.ok === false) throw new Error(String(result.error || "步骤运行失败"))
      if (result?.instance_id) setWorkflowRuntimeInstanceId(String(result.instance_id))
      if (result?.runtime) upsertWorkflowRuntimePayload(result.runtime)
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
      await refreshWorkflowTemplates()
      return result
    } catch (error) {
      setWorkflowTemplatesError(workflowErrorMessage(error))
      try {
        await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      } catch {
        // Keep the visible workflow error if the follow-up refresh also fails.
      }
      throw error
    } finally {
      setWorkflowRunningStepIds((current) => current.filter((id) => id !== stepId))
    }
  }, [
    activeWorkflowMissingInputIds,
    currentProject?.id,
    refreshCanvas,
    refreshWorkflowTemplates,
    workflowCanvasOrigin,
    workflowMaterializeInputs,
    workflowRunUiOverrides,
    workflowRunTarget,
    workflowRuntimeInstanceId,
    workflowRuntimeOrigin,
    upsertWorkflowRuntimePayload,
  ])

  const runWorkflowStep = useCallback(async (stepId: string) => {
    if (!stepId || workflowRunningStepIds.length > 0 || workflowRunningAll) return
    await runWorkflowStepInternal(stepId)
  }, [runWorkflowStepInternal, workflowRunningAll, workflowRunningStepIds.length])

  const runNextWorkflowStep = useCallback(async () => {
    if (workflowRunningStepIds.length > 0 || workflowRunningAll) return
    if (!currentProject?.id) return
    const target = workflowRunTarget()
    if (!target) return
    const missingInputs = activeWorkflowMissingInputIds
    if (missingInputs.length > 0) {
      const message = `先输入 ${missingInputs.map(workflowInputLabel).join("、")}`
      setWorkflowTemplatesError(message)
      throw new Error(message)
    }
    const origin = workflowRuntimeOrigin ?? workflowCanvasOrigin()
    if (!workflowRuntimeOrigin) setWorkflowRuntimeOrigin(origin)
    const targetInstanceId = workflowRuntimeInstanceId || createWorkflowRuntimeInstanceId()
    if (!workflowRuntimeInstanceId) setWorkflowRuntimeInstanceId(targetInstanceId)
    setWorkflowRunningStepIds(["__next__"])
    setWorkflowTemplatesError(null)
    try {
      const result = await runProjectWorkflowNextStep<{
        ok?: boolean
        done?: boolean
        instance_id?: string
        selected_step_id?: string
        node_id?: string
        node?: { id?: string; surface?: unknown }
        runtime?: ProjectWorkflowRuntime
        error?: string
      }>(currentProject.id, {
        ...target,
        instance_id: targetInstanceId,
        inputs: workflowMaterializeInputs,
        ui_overrides: workflowRunUiOverrides,
        origin_x: origin.x,
        origin_y: origin.y,
      })
      if (result?.ok === false) throw new Error(String(result.error || "步骤运行失败"))
      if (result?.instance_id) setWorkflowRuntimeInstanceId(String(result.instance_id))
      if (result?.runtime) upsertWorkflowRuntimePayload(result.runtime)
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
      await refreshWorkflowTemplates()
    } catch (error) {
      setWorkflowTemplatesError(workflowErrorMessage(error))
      try {
        await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      } catch {
        // Keep the visible workflow error if the follow-up refresh also fails.
      }
      throw error
    } finally {
      setWorkflowRunningStepIds([])
    }
  }, [
    activeWorkflowMissingInputIds,
    currentProject?.id,
    refreshCanvas,
    refreshWorkflowTemplates,
    workflowCanvasOrigin,
    workflowMaterializeInputs,
    workflowRunUiOverrides,
    workflowRunTarget,
    workflowRuntimeInstanceId,
    workflowRuntimeOrigin,
    workflowRunningAll,
    workflowRunningStepIds.length,
    upsertWorkflowRuntimePayload,
  ])

  const runAllWorkflowSteps = useCallback(async () => {
    if (workflowRunningStepIds.length > 0 || workflowRunningAll || activeWorkflowSteps.length === 0) return
    if (!currentProject?.id) return
    const target = workflowRunTarget()
    if (!target) return
    const missingInputs = activeWorkflowMissingInputIds
    if (missingInputs.length > 0) {
      const message = `先输入 ${missingInputs.map(workflowInputLabel).join("、")}`
      setWorkflowTemplatesError(message)
      throw new Error(message)
    }
    const origin = workflowRuntimeOrigin ?? workflowCanvasOrigin()
    if (!workflowRuntimeOrigin) setWorkflowRuntimeOrigin(origin)
    const targetInstanceId = workflowRuntimeInstanceId || createWorkflowRuntimeInstanceId()
    if (!workflowRuntimeInstanceId) setWorkflowRuntimeInstanceId(targetInstanceId)
    setWorkflowRunningAll(true)
    setWorkflowTemplatesError(null)
    try {
      const result = await runProjectWorkflowAllSteps<{
        ok?: boolean
        done?: boolean
        instance_id?: string
        runtime?: ProjectWorkflowRuntime
        error?: string
      }>(currentProject.id, {
        ...target,
        instance_id: targetInstanceId,
        inputs: workflowMaterializeInputs,
        ui_overrides: workflowRunUiOverrides,
        origin_x: origin.x,
        origin_y: origin.y,
        max_steps: Math.max(activeWorkflowSteps.length + 20, 120),
      })
      if (result?.ok === false) throw new Error(String(result.error || "工作流执行失败"))
      if (result?.runtime) upsertWorkflowRuntimePayload(result.runtime)
      if (result?.instance_id) setWorkflowRuntimeInstanceId(String(result.instance_id))
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
    } catch (error) {
      setWorkflowTemplatesError(workflowErrorMessage(error))
      try {
        await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      } catch {
        // Keep the visible workflow error if the follow-up refresh also fails.
      }
    } finally {
      setWorkflowRunningAll(false)
    }
  }, [
    activeWorkflowMissingInputIds,
    activeWorkflowSteps,
    currentProject?.id,
    refreshCanvas,
    workflowCanvasOrigin,
    workflowMaterializeInputs,
    workflowRunUiOverrides,
    workflowRunTarget,
    workflowRunningAll,
    workflowRunningStepIds.length,
    workflowRuntimeInstanceId,
    workflowRuntimeOrigin,
    upsertWorkflowRuntimePayload,
  ])

  useEffect(() => {
    if (!mediaHistoryOpen || !currentProject?.id) return
    void refreshMediaHistory()
    const timer = window.setInterval(() => {
      void refreshMediaHistory()
    }, 5000)
    return () => window.clearInterval(timer)
  }, [currentProject?.id, mediaHistoryOpen, refreshMediaHistory])

  const sharedAssetCategoryOptions = useMemo(() => (
    (assetCategories.shared ?? [])
      .filter((item) => !assetSaveForm.kind || item.kind === assetSaveForm.kind)
      .map((item) => item.category)
      .filter((item): item is string => Boolean(item))
  ), [assetCategories.shared, assetSaveForm.kind])

  const openNodeAssetSaveDialog = useCallback(async (request: NodeAssetSaveRequest) => {
    const title = String(request.title || "").trim()
    const usableTitle = GENERIC_IMAGE_TITLES.has(title) ? "" : title
    setAssetSaveRequest(request)
    setAssetSaveForm({
      library: "shared",
      kind: "scene",
      category: "",
      episode: "1",
      name: usableTitle,
    })
    setAssetSaveError(null)
    if (!currentProject?.id) return
    try {
      const categories = await callTool<AssetCategoryResult>("assets.list_categories", {
        project_id: currentProject.id,
      })
      if (!categories?.error) setAssetCategories(categories)
    } catch (error) {
      console.warn("Failed to load asset categories", error)
    }
  }, [currentProject?.id])

  useEffect(() => {
    const handleAddNodeToAssetLibrary = (event: Event) => {
      const detail = (event as CustomEvent<NodeAssetSaveRequest>).detail
      if (!detail?.nodeId) return
      void openNodeAssetSaveDialog({
        nodeId: String(detail.nodeId),
        title: String(detail.title || ""),
        publicId: detail.publicId ?? null,
      })
    }
    window.addEventListener("openreel:add-node-to-asset-library", handleAddNodeToAssetLibrary)
    return () => window.removeEventListener("openreel:add-node-to-asset-library", handleAddNodeToAssetLibrary)
  }, [openNodeAssetSaveDialog])

  useEffect(() => {
    const handleEditImageNode = (event: Event) => {
      const detail = (event as CustomEvent<NodeImageEditRequest>).detail
      if (!detail?.nodeId) return
      const nodeId = String(detail.nodeId)
      const sourceNode = nodes.find((item) => item.id === nodeId)
      const imageUrl = String(detail.imageUrl || imageDownloadUrlFromNode(sourceNode) || "")
      setNodeActionMenu(null)
      if (!imageUrl) {
        setNodeDetailEditRequestKey(`${nodeId}:${Date.now()}`)
        selectNode(nodeId)
        return
      }
      setImageEditRequest({
        nodeId,
        title: String(detail.title || (sourceNode?.data as { title?: string } | undefined)?.title || "图片编辑"),
        imageUrl,
      })
    }
    window.addEventListener("openreel:edit-image-node", handleEditImageNode)
    return () => window.removeEventListener("openreel:edit-image-node", handleEditImageNode)
  }, [nodes, selectNode])

  const mediaOperationPositionForNode = useCallback((node: FlowNode | undefined) => {
    if (!node) return undefined
    const width = nodeDimension(node, "width", 320)
    return {
      x: node.position.x + Math.max(360, width + 140),
      y: node.position.y,
    }
  }, [])

  const runQuickVideoOperation = useCallback(async (
    nodeId: string,
    operation: "video.export_frame" | "video.split_tracks",
  ) => {
    if (!currentProject?.id || !nodeId) return
    const sourceNode = nodes.find((item) => item.id === nodeId)
    const sourceTitle = String((sourceNode?.data as { title?: string } | undefined)?.title || "视频")
    try {
      setNodeActionMenu(null)
      const result = await runProjectMediaOperation<{
        ok?: boolean
        nodes?: Array<{ id?: string }>
      }>(currentProject.id, {
        operation,
        source_node_id: nodeId,
        frame_mode: operation === "video.export_frame" ? "tail" : undefined,
        title: operation === "video.export_frame" ? `${sourceTitle} 尾帧` : undefined,
        position: mediaOperationPositionForNode(sourceNode),
      })
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      const firstNodeId = String(result.nodes?.[0]?.id || "")
      if (firstNodeId) selectNode(firstNodeId)
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "媒体处理失败")
    }
  }, [currentProject?.id, mediaOperationPositionForNode, nodes, refreshCanvas, selectNode])

  useEffect(() => {
    const handleEditVideoNode = (event: Event) => {
      const detail = (event as CustomEvent<NodeVideoEditRequest>).detail
      if (!detail?.nodeId) return
      const nodeId = String(detail.nodeId)
      const sourceNode = nodes.find((item) => item.id === nodeId)
      const video = previewVideoFromNode(sourceNode)
      const videoUrl = String(detail.videoUrl || video?.src || "")
      setContextMenu(null)
      setNodeActionMenu(null)
      if (!videoUrl) {
        selectNode(nodeId)
        return
      }
      setVideoEditRequest({
        nodeId,
        title: String(detail.title || (sourceNode?.data as { title?: string } | undefined)?.title || "视频剪辑"),
        videoUrl,
      })
    }
    window.addEventListener("openreel:edit-video-node", handleEditVideoNode)
    return () => window.removeEventListener("openreel:edit-video-node", handleEditVideoNode)
  }, [nodes, selectNode])

  useEffect(() => {
    const handleExportTailFrame = (event: Event) => {
      const nodeId = String((event as CustomEvent<{ nodeId?: string }>).detail?.nodeId || "")
      if (!nodeId) return
      void runQuickVideoOperation(nodeId, "video.export_frame")
    }
    const handleSplitAudio = (event: Event) => {
      const nodeId = String((event as CustomEvent<{ nodeId?: string }>).detail?.nodeId || "")
      if (!nodeId) return
      void runQuickVideoOperation(nodeId, "video.split_tracks")
    }
    window.addEventListener("openreel:video-export-tail-frame", handleExportTailFrame)
    window.addEventListener("openreel:video-split-audio", handleSplitAudio)
    return () => {
      window.removeEventListener("openreel:video-export-tail-frame", handleExportTailFrame)
      window.removeEventListener("openreel:video-split-audio", handleSplitAudio)
    }
  }, [runQuickVideoOperation])

  useEffect(() => {
    const handlePreviewNode = (event: Event) => {
      const detail = (event as CustomEvent<NodePreviewRequest>).detail
      const nodeId = String(detail?.nodeId || "").trim()
      if (!nodeId) return
      setContextMenu(null)
      setNodeActionMenu(null)
      setNodePreviewRequest({
        nodeId,
        type: detail?.type,
        title: detail?.title,
        input: detail?.input,
        output: detail?.output,
        prompt: detail?.prompt,
        preview: detail?.preview,
        previewText: detail?.previewText,
        readOnly: Boolean(detail?.readOnly),
      })
    }
    window.addEventListener("openreel:preview-node", handlePreviewNode)
    return () => window.removeEventListener("openreel:preview-node", handlePreviewNode)
  }, [])

  const createPanoramaFromNode = useCallback(async (request: NodePanoramaCreateRequest) => {
    if (!currentProject?.id || !request.nodeId) return
    const sourceNode = nodes.find((item) => item.id === request.nodeId)
    const sourceData = sourceNode?.data as { title?: string; publicId?: string | number | null } | undefined
    const sourceTitle = String(sourceData?.title || request.title || "图片节点").trim() || "图片节点"
    const sourcePublicId = request.publicId ?? sourceData?.publicId ?? null
    const sourceRef = sourcePublicId !== null && sourcePublicId !== undefined && String(sourcePublicId).trim()
      ? `node:${String(sourcePublicId).trim()}`
      : `node:${request.nodeId}`
    const sourceWidth = Number(
      (sourceNode?.style as Record<string, unknown> | undefined)?.width ??
      sourceNode?.width ??
      280,
    )
    const sourcePosition = sourceNode?.position ?? { x: 120, y: 120 }
    const title = `${sourceTitle} 全景图`
    const x = sourcePosition.x + Math.max(360, sourceWidth + 140)
    const y = sourcePosition.y
    let newNodeId = ""

    try {
      const raw = await createProjectNode(currentProject.id, {
        type: "image",
        title,
        x,
        y,
      })
      newNodeId = String(raw.id ?? "")
      if (!newNodeId) throw new Error("全景节点创建失败")

      const references = [{ ref: sourceRef, role: "visual_reference" }]
      const input = {
        surface: "draft_canvas",
        title,
        prompt: PANORAMA_PROMPT,
        aspect_ratio: "2:1",
        resolution: "2048x1024",
        references,
        fields: {
          panorama: true,
          is_panorama: true,
          projection: "equirectangular",
          aspect_ratio: "2:1",
          resolution: "2048x1024",
          source_node_ref: sourceRef,
        },
        render_state: "stale",
      }
      await updateProjectNodeDetails(currentProject.id, newNodeId, {
        title,
        prompt: PANORAMA_PROMPT,
        input,
      })
      await createProjectEdge(currentProject.id, request.nodeId, newNodeId, "全景参考")
      updateCanvasNode(newNodeId, {
        title,
        status: "running",
        prompt: PANORAMA_PROMPT,
        renderState: "stale",
        preview: {
          type: "image_prompt",
          prompt: PANORAMA_PROMPT,
          aspect_ratio: "2:1",
          resolution: "2048x1024",
          panorama: true,
          is_panorama: true,
          projection: "equirectangular",
        },
      })
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      const result = await callTool<Record<string, unknown>>("node.run", {
        project_id: currentProject.id,
        node_id: newNodeId,
        action: "render",
      })
      if (result?.ok === false) throw new Error(String(result.error || "全景图生成失败"))
      await refreshCanvas({ preserveOnEmpty: true, fitView: true })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      console.error("Failed to create panorama image node", error)
      if (newNodeId) {
        updateCanvasNode(newNodeId, { status: "failed", error: message, error_message: message })
      }
    }
  }, [currentProject?.id, nodes, refreshCanvas, updateCanvasNode])

  useEffect(() => {
    const handleCreatePanorama = (event: Event) => {
      const detail = (event as CustomEvent<NodePanoramaCreateRequest>).detail
      if (!detail?.nodeId) return
      void createPanoramaFromNode({
        nodeId: String(detail.nodeId),
        title: String(detail.title || ""),
        publicId: detail.publicId ?? null,
        imageUrl: detail.imageUrl ? String(detail.imageUrl) : undefined,
      })
    }
    window.addEventListener("openreel:create-panorama-from-node", handleCreatePanorama)
    return () => window.removeEventListener("openreel:create-panorama-from-node", handleCreatePanorama)
  }, [createPanoramaFromNode])

  const openPanoramaViewer = useCallback((request: PanoramaViewerRequest) => {
    const node = nodes.find((item) => item.id === request.nodeId)
    const imageUrl = request.imageUrl || imageDownloadUrlFromNode(node) || ""
    if (!imageUrl) return
    setPanoramaViewer({
      nodeId: request.nodeId,
      title: request.title || String((node?.data as { title?: string } | undefined)?.title || "全景图"),
      imageUrl,
    })
  }, [nodes])

  useEffect(() => {
    const handleOpenPanoramaViewer = (event: Event) => {
      const detail = (event as CustomEvent<PanoramaViewerRequest>).detail
      if (!detail?.nodeId) return
      openPanoramaViewer({
        nodeId: String(detail.nodeId),
        title: String(detail.title || ""),
        imageUrl: detail.imageUrl ? String(detail.imageUrl) : "",
      })
    }
    window.addEventListener("openreel:open-panorama-viewer", handleOpenPanoramaViewer)
    return () => window.removeEventListener("openreel:open-panorama-viewer", handleOpenPanoramaViewer)
  }, [openPanoramaViewer])

  const savePanoramaCapture = useCallback(async (dataUrl: string, mode: PanoramaCaptureMode) => {
    if (!currentProject?.id || !panoramaViewer) return
    const sourceNode = nodes.find((item) => item.id === panoramaViewer.nodeId)
    const sourcePosition = sourceNode?.position ?? { x: 120, y: 120 }
    const sourceWidth = Number(
      (sourceNode?.style as Record<string, unknown> | undefined)?.width ??
      sourceNode?.width ??
      340,
    )
    const sourceHeight = Number(
      (sourceNode?.style as Record<string, unknown> | undefined)?.height ??
      sourceNode?.height ??
      180,
    )
    const modeTitle = mode === "single" ? "单视角截图" : mode === "four" ? "四视角截图" : "八视角截图"
    const siblingOffset = Math.max(0, nodes.length % 5) * 34
    await createPanoramaCapture(currentProject.id, {
      title: `${panoramaViewer.title || "全景"} ${modeTitle}`,
      data_url: dataUrl,
      mode,
      source_node_id: panoramaViewer.nodeId,
      x: sourcePosition.x + Math.max(360, sourceWidth + 140),
      y: sourcePosition.y + Math.min(220, sourceHeight * 0.35) + siblingOffset,
    })
    await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true, fitView: true })
  }, [currentProject?.id, nodes, panoramaViewer, refreshCanvas])

  const saveNodeToAssetLibrary = useCallback(async () => {
    if (!currentProject?.id || !assetSaveRequest) return
    const name = assetSaveForm.name.trim()
    if (!name) {
      setAssetSaveError("请先填写资产标题")
      return
    }
    if (!assetSaveForm.category.trim()) {
      setAssetSaveError("请先填写分类文件夹")
      return
    }
    setAssetSaveLoading(true)
    setAssetSaveError(null)
    try {
      const source = `node:${assetSaveRequest.publicId ?? assetSaveRequest.nodeId}`
      const result = await callTool<Record<string, unknown>>("assets.save_to_shared", {
        project_id: currentProject.id,
        source,
        kind: assetSaveForm.kind,
        category: assetSaveForm.category,
        name,
      })
      if (result?.error || result?.ok === false) {
        throw new Error(String(result.error || "加入资产库失败"))
      }
      setAssetSaveRequest(null)
    } catch (error) {
      setAssetSaveError(error instanceof Error ? error.message : String(error))
    } finally {
      setAssetSaveLoading(false)
    }
  }, [assetSaveForm, assetSaveRequest, currentProject?.id])

  useEffect(() => {
    const handleCanvasRefresh = (event: Event) => {
      const detail = (event as CustomEvent<CanvasRefreshOptions>).detail || {}
      if (detail.projectId && detail.projectId !== currentProject?.id) return
      if (refreshTimerRef.current) window.clearTimeout(refreshTimerRef.current)
      refreshTimerRef.current = window.setTimeout(() => {
        refreshTimerRef.current = null
        void refreshCanvas({
          preserveOnEmpty: detail.preserveOnEmpty ?? true,
          preserveLayout: detail.preserveLayout ?? true,
          fitView: detail.fitView,
        })
      }, 60)
    }
    window.addEventListener(CANVAS_REFRESH_EVENT, handleCanvasRefresh)
    return () => {
      window.removeEventListener(CANVAS_REFRESH_EVENT, handleCanvasRefresh)
      if (refreshTimerRef.current) window.clearTimeout(refreshTimerRef.current)
      refreshTimerRef.current = null
    }
  }, [currentProject?.id, refreshCanvas])

  const pushUndo = useCallback((record: Omit<CanvasUndoRecord, "id" | "at">) => {
    undoStackRef.current = [
      ...undoStackRef.current.slice(-49),
      { ...record, id: `${Date.now()}-${Math.random().toString(16).slice(2)}`, at: Date.now() },
    ]
  }, [])

  const runUndo = useCallback(async () => {
    const record = undoStackRef.current.pop()
    if (!record) return
    try {
      await record.undo()
      await refreshCanvas()
    } catch (error) {
      console.warn("Failed to undo canvas operation", record.label, error)
    }
  }, [refreshCanvas])

  const clearGridDropPreview = useCallback(() => {
    document.querySelectorAll<HTMLElement>(".openreel-grid-drop-target").forEach((element) => {
      element.classList.remove("openreel-grid-drop-target")
    })
    document.querySelectorAll<HTMLElement>(".openreel-grid-drop-source").forEach((element) => {
      element.classList.remove("openreel-grid-drop-source")
    })
    document.querySelectorAll<HTMLElement>(".openreel-smart-node-card").forEach((element) => {
      element.style.removeProperty("--openreel-grid-drop-scale")
    })
  }, [])

  const findFlowNodeElement = useCallback((nodeId: string) => {
    return Array.from(document.querySelectorAll<HTMLElement>(".react-flow__node"))
      .find((element) => element.dataset.id === nodeId) ?? null
  }, [])

  const findGridCellAtPoint = useCallback((x: number, y: number, sourceNodeId: string): GridDropTarget | null => {
    const cells = Array.from(document.querySelectorAll<HTMLElement>("[data-openreel-grid-cell='true']"))
    for (const cell of cells) {
      const gridNodeId = cell.dataset.gridNodeId || ""
      const cellId = cell.dataset.gridCellId || ""
      if (!gridNodeId || !cellId || gridNodeId === sourceNodeId) continue
      const rect = cell.getBoundingClientRect()
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        return { gridNodeId, cellId, element: cell }
      }
    }
    return null
  }, [])

  const applyGridDropPreview = useCallback((sourceNodeId: string, target: GridDropTarget | null) => {
    clearGridDropPreview()
    if (!target) return
    target.element.classList.add("openreel-grid-drop-target")
    const sourceElement = findFlowNodeElement(sourceNodeId)
    if (!sourceElement) return
    const card = sourceElement.querySelector<HTMLElement>(".openreel-smart-node-card")
    if (!card) return
    const sourceRect = card.getBoundingClientRect()
    const targetRect = target.element.getBoundingClientRect()
    const scale = sourceRect.width > 0 && sourceRect.height > 0
      ? Math.max(0.28, Math.min(1, targetRect.width / sourceRect.width, targetRect.height / sourceRect.height))
      : 0.72
    card.style.setProperty("--openreel-grid-drop-scale", scale.toFixed(3))
    sourceElement.classList.add("openreel-grid-drop-source")
  }, [clearGridDropPreview, findFlowNodeElement])

  const isPlaceableImageNode = useCallback((node: FlowNode) => {
    const nodeData = node.data as { type?: string; preview?: { type?: string } } | undefined
    const nodeType = String(nodeData?.type || "")
    const nodePreviewType = String(nodeData?.preview?.type || "")
    return nodeType === "image" && nodePreviewType !== "image_grid"
  }, [])

  const handleNodeDrag = useCallback((event: MouseEvent, node: FlowNode) => {
    if (isPlaceableImageNode(node)) {
      const targetCell = findGridCellAtPoint(event.clientX, event.clientY, node.id)
      applyGridDropPreview(node.id, targetCell)
    } else {
      clearGridDropPreview()
    }

    const latestNodes = flowInstance?.getNodes() ?? nodes
    const draggedIds = new Set(activeDragNodeIdsRef.current.length ? activeDragNodeIdsRef.current : [node.id])
    const latestById = new Map(latestNodes.map((item) => [item.id, item]))
    const latestActiveNode = latestById.get(node.id)
    const activeNode = latestActiveNode ? { ...latestActiveNode, position: node.position } : node
    const snap = computeAlignmentSnap({
      activeNode,
      nodes: latestNodes,
      draggedNodeIds: draggedIds,
      zoom: viewport.zoom,
      coarsePointer,
    })
    const signature = alignmentGuideSignature(snap.guides)
    if (signature !== alignmentGuideSignatureRef.current) {
      alignmentGuideSignatureRef.current = signature
      setAlignmentGuides(snap.guides)
    }
    if (Math.abs(snap.deltaX) <= 0.01 && Math.abs(snap.deltaY) <= 0.01) return

    const changes: NodeChange[] = []
    for (const id of draggedIds) {
      const current = latestById.get(id)
      const position = id === node.id ? node.position : current?.position
      if (!position) continue
      changes.push({
        id,
        type: "position",
        position: { x: position.x + snap.deltaX, y: position.y + snap.deltaY },
        dragging: true,
      })
    }
    if (changes.length > 0) applyNodeChanges(changes)
  }, [
    applyGridDropPreview,
    applyNodeChanges,
    clearGridDropPreview,
    coarsePointer,
    findGridCellAtPoint,
    flowInstance,
    isPlaceableImageNode,
    nodes,
    viewport.zoom,
  ])

  const handleNodeDragStart = useCallback((_event: MouseEvent, node: FlowNode) => {
    const latestNodes = flowInstance?.getNodes() ?? nodes
    const selectedDragNodes = node.selected
      ? latestNodes.filter((item) => item.selected || item.id === node.id)
      : [node]
    const draggedIds = selectedDragNodes.length > 0 ? selectedDragNodes.map((item) => item.id) : [node.id]
    const latestById = new Map(latestNodes.map((item) => [item.id, item]))
    activeDragNodeIdsRef.current = draggedIds
    dragStartPositionsRef.current = {}
    for (const id of draggedIds) {
      const current = id === node.id ? node : latestById.get(id)
      if (current?.position) {
        dragStartPositionsRef.current[id] = { x: current.position.x, y: current.position.y }
      }
    }
    alignmentGuideSignatureRef.current = ""
    setAlignmentGuides([])
  }, [flowInstance, nodes])

  const handleNodeDragStop = useCallback((event: MouseEvent, node: FlowNode) => {
    setAlignmentGuides([])
    alignmentGuideSignatureRef.current = ""
    if (!currentProject?.id) {
      clearGridDropPreview()
      activeDragNodeIdsRef.current = []
      dragStartPositionsRef.current = {}
      return
    }
    const targetCell = isPlaceableImageNode(node)
      ? findGridCellAtPoint(event.clientX, event.clientY, node.id)
      : null
    clearGridDropPreview()
    if (targetCell) {
      void callTool<Record<string, unknown>>("image.place_grid_cell", {
        project_id: currentProject.id,
        grid_node_id: targetCell.gridNodeId,
        cell_id: targetCell.cellId,
        source_ref: `node:${node.id}`,
        fit: "cover",
        remove_source_node: true,
      })
        .then((result) => {
          if (result && result.ok === false) {
            console.warn("Failed to place image in grid cell", result.error || result)
          }
        })
        .catch((error) => {
          console.warn("Failed to place image in grid cell", error)
        })
      activeDragNodeIdsRef.current = []
      dragStartPositionsRef.current = {}
      return
    }
    const draggedIds = activeDragNodeIdsRef.current.length ? activeDragNodeIdsRef.current : [node.id]
    const previousPositions = dragStartPositionsRef.current
    activeDragNodeIdsRef.current = []
    dragStartPositionsRef.current = {}
    const latestNodes = flowInstance?.getNodes() ?? nodes
    const latestById = new Map(latestNodes.map((item) => [item.id, item]))
    const changedPositions = draggedIds
      .map((id) => {
        const current = latestById.get(id) ?? (id === node.id ? node : undefined)
        const previous = previousPositions[id]
        const position = current?.position
        if (!previous || !position) return null
        if (Math.abs(previous.x - position.x) <= 0.5 && Math.abs(previous.y - position.y) <= 0.5) return null
        return { id, previous, position: { x: position.x, y: position.y } }
      })
      .filter((item): item is { id: string; previous: { x: number; y: number }; position: { x: number; y: number } } => Boolean(item))
    if (changedPositions.length === 0) return

    void Promise.all(changedPositions.map(({ id, position }) => updateNodePosition(currentProject.id, id, position))).then(() => {
      if (changedPositions.length > 0) {
        pushUndo({
          label: changedPositions.length > 1 ? "移动节点组" : "移动节点",
          undo: async () => {
            if (!currentProject?.id) return
            await Promise.all(changedPositions.map(({ id, previous }) => updateNodePosition(currentProject.id, id, previous)))
          },
        })
      }
    }).catch((error) => {
      console.warn("Failed to persist node position", error)
    })
  }, [clearGridDropPreview, currentProject?.id, findGridCellAtPoint, flowInstance, isPlaceableImageNode, nodes, pushUndo])

  const isOutputToInputConnection = useCallback((connection: Connection) => {
    if (!connection.source || !connection.target || connection.source === connection.target) return false
    if (connection.sourceHandle && connection.sourceHandle !== "out") return false
    if (connection.targetHandle && connection.targetHandle !== "in") return false
    return true
  }, [])

  const handleConnect = useCallback((connection: Connection) => {
    if (!isOutputToInputConnection(connection)) return
    connectionCompletedRef.current = true
    const edge = connectNodes(connection)
    if (!edge || !currentProject?.id) return
    void createProjectEdge(currentProject.id, edge.source, edge.target)
      .then((result) => {
        const persistedId = String(result.id ?? "")
        if (persistedId && persistedId !== edge.id) replaceEdgeId(edge.id, persistedId)
        const undoEdgeId = persistedId || edge.id
        pushUndo({
          label: "创建连接",
          undo: async () => {
            if (!currentProject?.id || undoEdgeId.startsWith("manual-")) return
            await deleteProjectEdge(currentProject.id, undoEdgeId)
          },
        })
      })
      .catch((error) => {
        console.warn("Failed to persist manual edge", error)
        removeEdges([edge.id])
      })
  }, [connectNodes, currentProject?.id, isOutputToInputConnection, pushUndo, removeEdges, replaceEdgeId])

  const openCreateMenuFromConnection = useCallback((
    event: globalThis.MouseEvent | globalThis.TouchEvent,
    start: PendingConnectionDraft | null = connectionStartRef.current,
  ) => {
    connectionStartRef.current = null
    if (connectionCompletedRef.current) {
      connectionCompletedRef.current = false
      return
    }
    if (!start?.nodeId || start.handleType !== "source" || !flowInstance) return
    const point = getPointerClientPoint(event)
    const target = document.elementFromPoint(point.x, point.y)
    if (target?.closest(".react-flow__node") || target?.closest(".react-flow__handle")) return
    const flowPosition = flowInstance.screenToFlowPosition(point)
    const sourcePoint = getPortHandleScreenPoint(start.nodeId, Position.Right)
    suppressPaneClickRef.current = true
    window.setTimeout(() => {
      suppressPaneClickRef.current = false
    }, 220)
    setContextMenu({
      x: point.x,
      y: point.y,
      flowX: flowPosition.x,
      flowY: flowPosition.y,
      connectFrom: start,
      previewLine: sourcePoint
        ? { fromX: sourcePoint.x, fromY: sourcePoint.y, toX: point.x, toY: point.y }
        : undefined,
    })
  }, [flowInstance])

  const handleWindowConnectEnd = useCallback((event: globalThis.MouseEvent | globalThis.TouchEvent) => {
    window.setTimeout(() => {
      if (!connectionStartRef.current && !connectionCompletedRef.current) return
      openCreateMenuFromConnection(event)
    }, 0)
  }, [openCreateMenuFromConnection])

  const handleConnectStart = useCallback((_event: MouseEvent | ReactTouchEvent, params: OnConnectStartParams) => {
    if (params.handleType !== "source") {
      connectionCompletedRef.current = false
      connectionStartRef.current = null
      return
    }
    connectionCompletedRef.current = false
    connectionStartRef.current = {
      nodeId: params.nodeId || "",
      handleId: params.handleId,
      handleType: params.handleType,
    }
    window.addEventListener("mouseup", handleWindowConnectEnd, { once: true })
    window.addEventListener("touchend", handleWindowConnectEnd, { once: true })
  }, [handleWindowConnectEnd])

  const handleConnectEnd = useCallback((event: globalThis.MouseEvent | globalThis.TouchEvent) => {
    openCreateMenuFromConnection(event)
  }, [openCreateMenuFromConnection])

  const handleGridCellExtract = useCallback(async (event: Event) => {
    if (!currentProject?.id || !flowInstance) return
    const detail = (event as CustomEvent<{
      gridNodeId?: string
      cellId?: string
      clientX?: number
      clientY?: number
    }>).detail
    const gridNodeId = String(detail?.gridNodeId || "")
    const cellId = String(detail?.cellId || "")
    const clientX = Number(detail?.clientX)
    const clientY = Number(detail?.clientY)
    if (!gridNodeId || !cellId || !Number.isFinite(clientX) || !Number.isFinite(clientY)) return
    const flowPosition = flowInstance.screenToFlowPosition({ x: clientX, y: clientY })
    try {
      const result = await callTool<Record<string, unknown>>("image.extract_grid_cell", {
        project_id: currentProject.id,
        grid_node_id: gridNodeId,
        cell_id: cellId,
        x: Math.round(flowPosition.x - 130),
        y: Math.round(flowPosition.y - 88),
        remove_from_grid: true,
      })
      if (result && result.ok === false) {
        console.warn("Failed to extract grid cell", result.error || result)
        return
      }
    } catch (error) {
      console.warn("Failed to extract grid cell", error)
    }
  }, [currentProject?.id, flowInstance])

  const handlePaneContextMenu = useCallback((event: MouseEvent) => {
    event.preventDefault()
    if (!flowInstance) return
    const position = flowInstance.screenToFlowPosition({ x: event.clientX, y: event.clientY })
    setNodeActionMenu(null)
    setContextMenu({
      x: event.clientX,
      y: event.clientY,
      flowX: position.x,
      flowY: position.y,
    })
  }, [flowInstance])

  const openPaneCreateMenuAt = useCallback((x: number, y: number) => {
    if (!flowInstance) return
    const position = flowInstance.screenToFlowPosition({ x, y })
    setNodeActionMenu(null)
    setContextMenu({
      x,
      y,
      flowX: position.x,
      flowY: position.y,
    })
  }, [flowInstance])

  const openNodeActionMenuAt = useCallback((nodeId: string, x: number, y: number) => {
    const node = nodes.find((item) => item.id === nodeId)
    if (!node) return
    const title = String((node.data as { title?: string } | undefined)?.title || "未命名")
    setContextMenu(null)
    setNodeActionMenu({
      x,
      y,
      nodeId,
      title,
      imageUrl: imageDownloadUrlFromNode(node) || undefined,
    })
  }, [nodes])

  const handleNodeContextMenu = useCallback((event: MouseEvent, node: FlowNode) => {
    event.preventDefault()
    event.stopPropagation()
    openNodeActionMenuAt(node.id, event.clientX, event.clientY)
  }, [openNodeActionMenuAt])

  const clearLongPress = useCallback(() => {
    const state = longPressRef.current
    if (state?.timer) window.clearTimeout(state.timer)
    longPressRef.current = null
  }, [])

  const markTouchMenuOpened = useCallback(() => {
    suppressPaneClickRef.current = true
    window.setTimeout(() => {
      suppressPaneClickRef.current = false
    }, 360)
    if ("vibrate" in navigator) navigator.vibrate?.(10)
  }, [])

  const clearCanvasSelection = useCallback(() => {
    setContextMenu(null)
    setNodeActionMenu(null)
    selectNode(null)
  }, [selectNode])

  const startLongPress = useCallback((
    pointerId: number,
    x: number,
    y: number,
    rawTarget: EventTarget | null,
  ) => {
    if (!flowInstance || isInteractiveTarget(rawTarget)) return
    const target = rawTarget instanceof Element ? rawTarget : null
    const nodeElement = target?.closest<HTMLElement>(".react-flow__node")
    const paneElement = target?.closest<HTMLElement>(".react-flow__pane")
    const kind = nodeElement?.dataset.id ? "node" : paneElement ? "pane" : null
    if (!kind) return
    clearLongPress()
    const nodeId = kind === "node" ? nodeElement?.dataset.id : undefined
    const timer = window.setTimeout(() => {
      const state = longPressRef.current
      if (!state || state.pointerId !== pointerId || state.fired) return
      state.fired = true
      markTouchMenuOpened()
      if (state.kind === "node" && state.nodeId) {
        openNodeActionMenuAt(state.nodeId, state.x, state.y)
      } else {
        openPaneCreateMenuAt(state.x, state.y)
      }
    }, LONG_PRESS_MS)
    longPressRef.current = {
      pointerId,
      x,
      y,
      kind,
      nodeId,
      timer,
      fired: false,
    }
  }, [clearLongPress, flowInstance, markTouchMenuOpened, openNodeActionMenuAt, openPaneCreateMenuAt])

  const handlePointerDownCapture = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (isWorkflowUiTarget(event.target)) return
    blankPointerRef.current = event.button === 0 && isCanvasBlankTarget(event.target)
      ? { pointerId: event.pointerId, x: event.clientX, y: event.clientY }
      : null
    if (!isTouchPointer(event)) return
    startLongPress(event.pointerId, event.clientX, event.clientY, event.target)
  }, [startLongPress])

  const handleTouchStartCapture = useCallback((event: ReactTouchEvent<HTMLDivElement>) => {
    if (isWorkflowUiTarget(event.target)) return
    const point = touchPoint(event)
    if (!point) return
    startLongPress(-point.id - 1, point.x, point.y, event.target)
  }, [startLongPress])

  const handlePointerMoveCapture = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (isWorkflowUiTarget(event.target)) return
    const state = longPressRef.current
    if (!state || state.pointerId !== event.pointerId || state.fired) return
    if (Math.hypot(event.clientX - state.x, event.clientY - state.y) > LONG_PRESS_MOVE_TOLERANCE) {
      clearLongPress()
    }
  }, [clearLongPress])

  const handleTouchMoveCapture = useCallback((event: ReactTouchEvent<HTMLDivElement>) => {
    if (isWorkflowUiTarget(event.target)) return
    const point = touchPoint(event)
    const state = longPressRef.current
    if (!point || !state || state.pointerId !== -point.id - 1 || state.fired) return
    if (Math.hypot(point.x - state.x, point.y - state.y) > LONG_PRESS_MOVE_TOLERANCE) {
      clearLongPress()
    }
  }, [clearLongPress])

  const handlePointerEndCapture = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (isWorkflowUiTarget(event.target)) {
      blankPointerRef.current = null
      return
    }
    const state = longPressRef.current
    if (state && state.pointerId === event.pointerId) {
      const fired = state.fired
      clearLongPress()
      if (fired) {
        blankPointerRef.current = null
        event.preventDefault()
        event.stopPropagation()
        return
      }
    }
    const blankPointer = blankPointerRef.current
    blankPointerRef.current = null
    if (!blankPointer || blankPointer.pointerId !== event.pointerId || suppressPaneClickRef.current) return
    if (!isCanvasBlankTarget(event.target)) return
    const moved = Math.hypot(event.clientX - blankPointer.x, event.clientY - blankPointer.y)
    if (moved <= CANVAS_BLANK_CLICK_TOLERANCE) {
      clearCanvasSelection()
    }
  }, [clearCanvasSelection, clearLongPress])

  const handleTouchEndCapture = useCallback((event: ReactTouchEvent<HTMLDivElement>) => {
    if (isWorkflowUiTarget(event.target)) return
    const point = touchPoint(event)
    const state = longPressRef.current
    if (!point || !state || state.pointerId !== -point.id - 1) return
    const fired = state.fired
    clearLongPress()
    if (fired) {
      event.preventDefault()
      event.stopPropagation()
    }
  }, [clearLongPress])

  const resolveCreateMenuPosition = useCallback((menu: CanvasCreateMenuState) => {
    let initial = { x: menu.flowX, y: menu.flowY }
    if (menu.connectFrom?.nodeId) {
      const sourceNode = nodes.find((node) => node.id === menu.connectFrom?.nodeId)
      if (sourceNode) {
        const bounds = nodeBounds(sourceNode)
        const gap = 104
        initial = menu.connectFrom.handleType === "target"
          ? { x: bounds.left - ALIGNMENT_DEFAULT_NODE_WIDTH - gap, y: bounds.top }
          : { x: bounds.right + gap, y: bounds.top }
      }
    }
    return findAvailableNodePosition(initial, nodes, menu.connectFrom?.nodeId)
  }, [nodes])

  const createNodeFromMenu = useCallback(async (
    type: CanvasNodeType,
    title: string,
    menu: CanvasCreateMenuState,
    undoLabel?: string,
  ) => {
    if (!currentProject?.id) return null
    const positionInput = resolveCreateMenuPosition(menu)
    try {
      const raw = await createProjectNode(currentProject.id, {
        type,
        title,
        x: positionInput.x,
        y: positionInput.y,
      })
      const id = String(raw.id ?? "")
      if (!id) return null
      const position = { x: Number(raw.position_x ?? positionInput.x), y: Number(raw.position_y ?? positionInput.y) }
      addNode({
        id,
        type,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        position,
        data: {
          nodeId: id,
          type,
          title: String(raw.title ?? title),
          status: String(raw.status ?? "idle"),
          surface: "draft_canvas",
          createdBy: "user",
        },
      }, { manual: true })
      let connectedEdgeId = ""
      if (menu.connectFrom?.nodeId) {
        const sourceNodeId = menu.connectFrom.handleType === "target" ? id : menu.connectFrom.nodeId
        const targetNodeId = menu.connectFrom.handleType === "target" ? menu.connectFrom.nodeId : id
        const edge = connectNodes({
          source: sourceNodeId,
          target: targetNodeId,
          sourceHandle: "out",
          targetHandle: "in",
        })
        if (edge) {
          try {
            const edgeResult = await createProjectEdge(currentProject.id, sourceNodeId, targetNodeId)
            connectedEdgeId = String(edgeResult.id ?? "")
            if (connectedEdgeId && connectedEdgeId !== edge.id) replaceEdgeId(edge.id, connectedEdgeId)
          } catch (error) {
            console.warn("Failed to connect newly created node", error)
            removeEdges([edge.id])
          }
        }
      }
      pushUndo({
        label: undoLabel || (menu.connectFrom ? "创建并连接节点" : "创建节点"),
        undo: async () => {
          if (!currentProject?.id) return
          if (connectedEdgeId) {
            await deleteProjectEdge(currentProject.id, connectedEdgeId).catch(() => undefined)
          }
          await deleteProjectNode(currentProject.id, id)
        },
      })
      selectNode(null)
      return { id, connectedEdgeId }
    } catch (error) {
      console.warn("Failed to create canvas node", error)
      return null
    }
  }, [addNode, connectNodes, currentProject?.id, pushUndo, removeEdges, replaceEdgeId, resolveCreateMenuPosition, selectNode])

  const handleCreateNode = useCallback(async (type: CanvasNodeType) => {
    if (!contextMenu) return
    const menu = contextMenu
    const item = CANVAS_NODE_CREATE_ITEMS.find((entry) => entry.type === type)
    const title = item ? `${item.label}节点` : "新建节点"
    setContextMenu(null)
    await createNodeFromMenu(type, title, menu)
  }, [contextMenu, createNodeFromMenu])

  const openWorkflowTemplatesFromCreateMenu = useCallback(() => {
    setContextMenu(null)
    onWorkspaceViewChange?.("workflow")
    void refreshWorkflowTemplates()
  }, [onWorkspaceViewChange, refreshWorkflowTemplates])

  const openCanvasCreateMenuAtCenter = useCallback(() => {
    const rect = canvasContainerRef.current?.getBoundingClientRect()
    const x = rect ? rect.left + rect.width * 0.5 : window.innerWidth * 0.5
    const y = rect ? rect.top + rect.height * 0.46 : window.innerHeight * 0.46
    openPaneCreateMenuAt(x, y)
  }, [openPaneCreateMenuAt])

  const deleteCanvasItems = useCallback(async (nodeIdsInput: string[], edgeIdsInput: string[]) => {
    if (!currentProject?.id) return
    const nodeIds = [...new Set(nodeIdsInput)]
    const nodeIdSet = new Set(nodeIds)
    const edgeIds = [...new Set(edgeIdsInput)]
      .filter((edgeId) => !edgeId.startsWith("manual-"))
      .filter((edgeId) => {
        const edge = canvasEdges.find((item) => item.id === edgeId)
        return !edge || (!nodeIdSet.has(edge.source) && !nodeIdSet.has(edge.target))
      })
    if (nodeIds.length === 0 && edgeIds.length === 0) return
    const deletedEdgeSnapshots: CanvasEdgeSnapshot[] = canvasEdges
      .filter((edge) => edgeIds.includes(edge.id) || nodeIdSet.has(edge.source) || nodeIdSet.has(edge.target))
      .map((edge) => ({
        id: isPersistedEdgeId(edge.id) ? edge.id : undefined,
        source_node_id: edge.source,
        target_node_id: edge.target,
        label: typeof edge.label === "string" ? edge.label : undefined,
      }))
    const deletedNodeSnapshots = await Promise.all(
      nodeIds.map((nodeId) =>
        getProjectNodeDetails<CanvasNodeSnapshot>(currentProject.id, nodeId)
          .catch(() => null),
      ),
    )
    const restoreNodes = deletedNodeSnapshots.filter(Boolean) as CanvasNodeSnapshot[]
    try {
      const deleteRequests: Promise<unknown>[] = []
      if (nodeIds.length > 0) {
        deleteRequests.push(deleteProjectNodes(currentProject.id, nodeIds))
      }
      deleteRequests.push(
        ...edgeIds.map((edgeId) => {
          const edge = canvasEdges.find((item) => item.id === edgeId)
          return deleteProjectEdge(currentProject.id, edgeId, edge ? {
            sourceNodeId: edge.source,
            targetNodeId: edge.target,
          } : undefined)
        }),
      )
      await Promise.all(deleteRequests)
      if (nodeIds.length) removeNodes(nodeIds)
      if (edgeIds.length) removeEdges(edgeIds)
      pushUndo({
        label: nodeIds.length ? "删除节点" : "删除连接",
        undo: async () => {
          if (!currentProject?.id) return
          await restoreProjectCanvasSnapshot(currentProject.id, {
            nodes: restoreNodes,
            edges: deletedEdgeSnapshots,
          })
        },
      })
    } catch (error) {
      console.warn("Failed to delete canvas selection", error)
    }
  }, [canvasEdges, currentProject?.id, pushUndo, removeEdges, removeNodes])

  const deleteSelection = useCallback(async () => {
    await deleteCanvasItems(selectedNodeIds, selectedEdgeIds)
  }, [deleteCanvasItems, selectedEdgeIds, selectedNodeIds])

  const handleDeleteNodeFromMenu = useCallback(async (nodeId: string) => {
    setNodeActionMenu(null)
    setContextMenu(null)
    await deleteCanvasItems([nodeId], [])
    selectNode(null)
  }, [deleteCanvasItems, selectNode])

  const handleDownloadImageFromMenu = useCallback(async (url: string, title: string) => {
    setNodeActionMenu(null)
    await downloadUrl(url, safeDownloadName(title, url))
  }, [])

  // 项目级长连 SSE — 接收后台任务完成的画布事件和工作流运行态刷新
  useEffect(() => {
    if (!currentProject?.id) return
    const url = `${getApiBaseSync()}/api/chat/events/${currentProject.id}`
    const es = new EventSource(url)
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.type === "canvas_action" && ev.payload) {
          if (ev.action === "workflow_runtime_update") {
            const runtime = (ev.payload as { runtime?: unknown }).runtime
            if (runtime && typeof runtime === "object" && !Array.isArray(runtime)) {
              upsertWorkflowRuntimePayload(runtime as ProjectWorkflowRuntime)
            }
            requestWorkflowRefresh({ projectId: currentProject.id })
            return
          }
          if (streaming) return
          if (ev.action === "clear_all") {
            console.warn("[openreel:workflow ignored background clear_all]", { payload: ev.payload })
            return
          }
          console.debug("[openreel:workflow canvas_action]", {
            action: ev.action,
            payload: ev.payload,
          })
          applyCanvasAction(String(ev.action ?? ""), ev.payload as Record<string, unknown>)
        }
      } catch {
        // ignore parse error
      }
    }
    es.onerror = () => {
      // EventSource 自带自动重连,这里不主动 close
    }
    return () => es.close()
  }, [currentProject?.id, streaming, applyCanvasAction, upsertWorkflowRuntimePayload])

  useEffect(() => {
    window.addEventListener("openreel:grid-cell-extract", handleGridCellExtract)
    return () => window.removeEventListener("openreel:grid-cell-extract", handleGridCellExtract)
  }, [handleGridCellExtract])

  useEffect(() => clearGridDropPreview, [clearGridDropPreview])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const tagName = target?.tagName?.toLowerCase()
      const isTyping = tagName === "input" || tagName === "textarea" || target?.isContentEditable
      if (isTyping) return
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !event.shiftKey) {
        event.preventDefault()
        void runUndo()
        return
      }
      if (event.key === "Escape") {
        setContextMenu(null)
        setNodeActionMenu(null)
        return
      }
      if (event.key !== "Delete" && event.key !== "Backspace") return
      if (selectedNodeIds.length === 0 && selectedEdgeIds.length === 0) return
      event.preventDefault()
      void deleteSelection()
    }
    const closeContextMenu = () => {
      if (suppressPaneClickRef.current) return
      setContextMenu(null)
      setNodeActionMenu(null)
    }
    window.addEventListener("keydown", onKeyDown)
    window.addEventListener("click", closeContextMenu)
    window.addEventListener("blur", closeContextMenu)
    return () => {
      window.removeEventListener("keydown", onKeyDown)
      window.removeEventListener("click", closeContextMenu)
      window.removeEventListener("blur", closeContextMenu)
    }
  }, [deleteSelection, runUndo, selectedEdgeIds.length, selectedNodeIds.length])

  const handleRerun = useCallback(async (nodeId: string) => {
    if (!currentProject || streaming) return
    const targetNode = nodes.find((node) => node.id === nodeId)
    const targetData = targetNode?.data as Record<string, unknown> | undefined
    const targetType = String(targetData?.type ?? "")
    const action = targetType === "image" ? "render" : "force"
    updateCanvasNode(nodeId, { status: "running", error: undefined, error_message: undefined })
    try {
      const result = await callTool<Record<string, unknown>>("node.run", {
        project_id: currentProject.id,
        node_id: nodeId,
        action,
      })
      if (result && result.ok === false) throw new Error(String(result.error || "重新生成失败"))
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      updateCanvasNode(nodeId, { status: "failed", error: message, error_message: message })
      try {
        await refreshCanvas({ preserveOnEmpty: true, fitView: true })
      } catch {
        // Keep the local failed state if the follow-up refresh also fails.
      }
      throw error
    }
  }, [currentProject, nodes, refreshCanvas, streaming, updateCanvasNode])

  useEffect(() => {
    const handleRunNode = (event: Event) => {
      const detail = (event as CustomEvent<{ nodeId?: string }>).detail
      const nodeId = String(detail?.nodeId || "")
      if (!nodeId) return
      void handleRerun(nodeId).catch((error) => console.warn("Failed to run workflow node", error))
    }
    window.addEventListener("openreel:run-node", handleRunNode)
    return () => window.removeEventListener("openreel:run-node", handleRunNode)
  }, [handleRerun])

  const restoreMediaHistoryItem = useCallback(async (item: ProjectMediaHistoryItem) => {
    if (!currentProject?.id || restoringHistoryId) return
    if (item.kind === "text") return
    setRestoringHistoryId(item.id)
    setMediaHistoryError(null)
    try {
      const rect = canvasContainerRef.current?.getBoundingClientRect()
      const point = rect
        ? { x: rect.left + rect.width * 0.56, y: rect.top + rect.height * 0.44 }
        : { x: window.innerWidth * 0.5, y: window.innerHeight * 0.5 }
      const position = flowInstance?.screenToFlowPosition(point) ?? { x: 0, y: 0 }
      const result = await restoreProjectMediaHistoryItem<{
        ok?: boolean
        node?: { id?: string; title?: string }
      }>(currentProject.id, item.id, {
        x: position.x,
        y: position.y,
        title: item.title || item.filename || "历史素材",
      })
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
      if (result.node?.id) selectNode(String(result.node.id))
      void refreshMediaHistory()
    } catch (error) {
      setMediaHistoryError(error instanceof Error ? error.message : String(error))
    } finally {
      setRestoringHistoryId(null)
    }
  }, [currentProject?.id, flowInstance, refreshCanvas, refreshMediaHistory, restoringHistoryId, selectNode])

  const deleteMediaHistoryItem = useCallback(async (item: ProjectMediaHistoryItem) => {
    if (!currentProject?.id || deletingHistoryId) return
    if (item.kind === "text") return
    const ok = window.confirm(`确定彻底删除这个${MEDIA_HISTORY_LABEL[item.kind]}文件吗？`)
    if (!ok) return
    setDeletingHistoryId(item.id)
    setMediaHistoryError(null)
    try {
      await deleteProjectMediaHistoryItem(currentProject.id, item.id)
      await refreshMediaHistory()
      await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
    } catch (error) {
      setMediaHistoryError(error instanceof Error ? error.message : String(error))
    } finally {
      setDeletingHistoryId(null)
    }
  }, [currentProject?.id, deletingHistoryId, refreshCanvas, refreshMediaHistory])

  const flowNodes = useMemo(() => {
    const visibleNodes = nodes.filter((node) => canvasVisibleNodeIds.has(node.id))
    return groupedNodeIdSet.size === 0
      ? visibleNodes
      : visibleNodes.map((node) => groupedNodeIdSet.has(node.id) ? { ...node, draggable: false } : node)
  }, [canvasVisibleNodeIds, groupedNodeIdSet, nodes])
  const flowEdges = edges
  const selectedCanvasNode = useMemo(
    () => selectedCanvasNodeId ? flowNodes.find((node) => node.id === selectedCanvasNodeId) || null : null,
    [flowNodes, selectedCanvasNodeId],
  )
  const selectedNodeContextPanelStyle = useMemo(
    () => nodeContextPanelStyle(selectedCanvasNode, viewport, canvasContainerRef.current),
    [selectedCanvasNode, viewport],
  )
  useEffect(() => {
    if (!selectedCanvasNode || !flowInstance || !canvasContainerRef.current) return
    const nextViewport = nodeContextViewportNudge(selectedCanvasNode, viewport, canvasContainerRef.current)
    if (!nextViewport) return
    const frame = window.requestAnimationFrame(() => {
      setViewport(nextViewport)
      void flowInstance.setViewport(nextViewport, { duration: 160 })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [flowInstance, selectedCanvasNode, viewport])
  const hiddenEdgeCount = Math.max(0, canvasVisibleEdges.length - flowEdges.length)
  const workflowPanel = currentProject?.id ? (
    <WorkflowTemplatePanel
      templates={workflowTemplates}
      selectedId={selectedWorkflowTemplateId}
      artifactPreview={workflowArtifactPreview}
      nodeTypes={workflowNodeTypes}
      nodeTypesError={workflowNodeTypesError}
      runtimeSteps={workflowRuntimeMergedSteps}
      loading={workflowTemplatesLoading}
      error={workflowTemplatesError}
      materializing={workflowMaterializing}
      runningStepIds={workflowRunningStepIds}
      runningAll={workflowRunningAll}
      inputValues={workflowInputValues}
      requiredInputIds={activeWorkflowRequiredInputIds}
      nodeStates={workflowStepNodeStates}
      mediaModelOverrides={workflowMediaModelOverrides}
      onSelectedIdChange={handleWorkflowTemplateSelection}
      onInputValueChange={updateWorkflowInputValue}
      onMediaModelOverrideChange={updateWorkflowMediaModelOverride}
      onClearArtifactPreview={() => {
        const nextTemplateId = selectedWorkflowTemplateId || workflowTemplates[0]?.id || ""
        setWorkflowArtifactPreview(null)
        setWorkflowImportedSpec(null)
        if (nextTemplateId) {
          setSelectedWorkflowTemplateId(nextTemplateId)
          void saveActiveWorkflowSelection({ kind: "template", template_id: nextTemplateId }).catch((error) => {
            setWorkflowTemplatesError(workflowErrorMessage(error))
          })
        }
      }}
      onRefresh={() => {
        void refreshWorkflowTemplates()
        void refreshWorkflowNodeTypes()
      }}
      onImportSpecFile={(file) => void importWorkflowSpecFile(file)}
      onMaterialize={() => void materializeSelectedWorkflow()}
      onRunStep={(stepId) => void runWorkflowStep(stepId).catch((error) => {
        console.warn("Failed to run workflow step", error)
      })}
      onRunNext={() => void runNextWorkflowStep().catch((error) => {
        console.warn("Failed to run next workflow step", error)
      })}
      onRunAll={() => void runAllWorkflowSteps().catch((error) => {
        console.warn("Failed to run workflow", error)
      })}
      onSaveWorkflowSpec={saveWorkflowEditorSpec}
      onSaveWorkflowTemplate={saveWorkflowEditorTemplate}
      onDownloadWorkflowTemplate={downloadWorkflowTemplate}
      onRestoreBuiltinTemplate={restoreWorkflowEditorBuiltinTemplate}
    />
  ) : (
    <div className="flex h-full items-center justify-center bg-[#10151d] text-sm text-zinc-500">
      项目加载后可查看流程面板
    </div>
  )

  return (
    <div className="h-full w-full overflow-hidden bg-black">
      {workspaceView === "workflow" ? workflowPanel : (
        <div
        ref={canvasContainerRef}
        className="relative h-full w-full select-none bg-black"
        onPointerDownCapture={handlePointerDownCapture}
        onPointerMoveCapture={handlePointerMoveCapture}
        onPointerUpCapture={handlePointerEndCapture}
        onPointerCancelCapture={handlePointerEndCapture}
        onTouchStartCapture={handleTouchStartCapture}
        onTouchMoveCapture={handleTouchMoveCapture}
        onTouchEndCapture={handleTouchEndCapture}
        onTouchCancelCapture={handleTouchEndCapture}
      >
      <MediaHistoryDrawer
        open={mediaHistoryOpen}
        items={mediaHistoryItems}
        filter={mediaHistoryFilter}
        loading={mediaHistoryLoading}
        error={mediaHistoryError}
        restoringId={restoringHistoryId}
        deletingId={deletingHistoryId}
        onToggle={() => setMediaHistoryOpen((value) => !value)}
        onFilterChange={setMediaHistoryFilter}
        onRefresh={() => void refreshMediaHistory()}
        onRestore={(item) => void restoreMediaHistoryItem(item)}
        onDelete={(item) => void deleteMediaHistoryItem(item)}
      />
      {flowNodes.length === 0 && !contextMenu && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center text-zinc-500">
          <div className="pointer-events-auto rounded-md border border-white/10 bg-[#10151d]/82 px-4 py-3 text-center shadow-xl shadow-black/30 backdrop-blur">
            <div className="mx-auto mb-2 flex h-9 w-9 items-center justify-center rounded-md bg-white/[0.07] text-[12px] font-semibold tracking-tight text-zinc-300">+</div>
            <div className="text-sm text-zinc-200">创作画布</div>
            <div className="mt-1 text-xs text-zinc-500">从节点开始，或直接打开流程模板</div>
            <div className="mt-3 flex items-center justify-center gap-2">
              <button
                type="button"
                onClick={openCanvasCreateMenuAtCenter}
                className="h-8 rounded-md bg-cyan-300 px-3 text-xs font-medium text-cyan-950 transition hover:bg-cyan-200"
              >
                新建节点
              </button>
              <button
                type="button"
                onClick={openWorkflowTemplatesFromCreateMenu}
                className="h-8 rounded-md border border-white/10 bg-white/[0.04] px-3 text-xs font-medium text-zinc-200 transition hover:bg-white/[0.08]"
              >
                流程模板
              </button>
            </div>
          </div>
        </div>
      )}
      {canvasVisibleEdges.length > 0 && (
        <div className="absolute bottom-4 left-14 z-30 hidden items-center gap-1.5 rounded-md border border-white/10 bg-[#10151d]/92 px-2 py-1.5 text-[11px] text-zinc-300 shadow-xl shadow-black/30 backdrop-blur md:flex">
          <span className="px-1 text-zinc-500">依赖线</span>
          {([
            ["clean", "干净"],
            ["selected", "当前"],
            ["all", "全部"],
          ] as Array<[CanvasEdgeDisplayMode, string]>).map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              onClick={() => setEdgeDisplayMode(mode)}
              className={cn(
                "h-6 rounded px-2 transition",
                edgeDisplayMode === mode
                  ? "bg-cyan-300 text-cyan-950"
                  : "text-zinc-400 hover:bg-white/[0.07] hover:text-zinc-100",
              )}
            >
              {label}
            </button>
          ))}
          {hiddenEdgeCount > 0 && edgeDisplayMode !== "all" && (
            <span className="rounded border border-white/[0.08] bg-black/24 px-1.5 py-0.5 text-[10px] text-zinc-500">
              隐藏 {hiddenEdgeCount}
            </span>
          )}
        </div>
      )}
      <WorkflowRunDock
        open={workflowDockOpen}
        runtimes={workflowRuntimePayloads}
        templates={workflowTemplates}
        canvasNodes={nodes}
        selectedTemplateId={workflowDockTemplateId}
        runningIds={workflowInstanceRunningIds}
        runningAllIds={workflowInstanceRunningAllIds}
        pausingIds={workflowInstancePausingIds}
        deletingIds={workflowInstanceDeletingIds}
        expandedIds={workflowDockExpandedRunIds}
        detail={workflowDockDetail}
        errors={workflowInstanceErrors}
        inputValuesByInstance={workflowInstanceInputValues}
        onOpenChange={setWorkflowDockOpen}
        onTemplateChange={setWorkflowDockTemplateId}
        onAddRun={addWorkflowRunDraft}
        onRunNext={(runtime) => void runWorkflowInstanceNext(runtime)}
        onRunAll={(runtime) => void runWorkflowInstanceAll(runtime)}
        onPauseRun={(runtime) => void pauseWorkflowInstanceRun(runtime)}
        onRunStep={(runtime, stepId) => void runWorkflowInstanceStep(runtime, stepId)}
        onDeleteRun={(runtime) => void deleteWorkflowRun(runtime)}
        onInspectStep={inspectWorkflowRunStep}
        onCloseDetail={() => setWorkflowDockDetail(null)}
        onInputValueChange={updateWorkflowInstanceInputValue}
        onUploadVideoInput={uploadWorkflowVideoInput}
        onToggleExpanded={(runtimeId) => {
          setWorkflowDockExpandedRunIds((current) => (
            current.includes(runtimeId)
              ? current.filter((id) => id !== runtimeId)
              : [runtimeId, ...current]
          ))
        }}
      />
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        defaultEdgeOptions={{
          type: "bezier",
          interactionWidth: 28,
          markerEnd: { type: MarkerType.ArrowClosed, color: "#64748b" },
          style: { stroke: "#64748b", strokeWidth: 1.7 },
        }}
        connectionLineType={ConnectionLineType.Bezier}
        connectionLineComponent={OpenReelConnectionLine}
        connectionLineStyle={{ stroke: "#22d3ee", strokeWidth: 1.8 }}
        connectionMode={ConnectionMode.Strict}
        connectionRadius={56}
        nodesDraggable={true}
        nodesConnectable={true}
        deleteKeyCode={null}
        elementsSelectable={true}
        selectNodesOnDrag={false}
        selectionOnDrag={true}
        selectionMode={SelectionMode.Partial}
        panOnDrag={coarsePointer ? true : [1]}
        panOnScroll={false}
        zoomOnScroll={true}
        zoomOnPinch={true}
        minZoom={0.15}
        maxZoom={2.5}
        onlyRenderVisibleElements={true}
        nodeDragThreshold={coarsePointer ? 8 : 2}
        elevateEdgesOnSelect={true}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onNodeDragStart={handleNodeDragStart}
        onNodeDrag={handleNodeDrag}
        onNodeDragStop={handleNodeDragStop}
        onConnect={handleConnect}
        onConnectStart={handleConnectStart}
        onConnectEnd={handleConnectEnd}
        isValidConnection={isOutputToInputConnection}
        onNodeContextMenu={handleNodeContextMenu}
        onInit={(instance) => {
          setFlowInstance(instance)
          setViewport(instance.getViewport())
        }}
        onMove={(_, nextViewport) => setViewport(nextViewport)}
        onPaneContextMenu={handlePaneContextMenu}
        onPaneClick={() => {
          if (suppressPaneClickRef.current) return
          clearCanvasSelection()
        }}
        className="openreel-canvas-flow bg-black"
      >
        <MiniMap
          pannable
          zoomable
          className="!bottom-4 !right-4 !hidden !rounded-md !border !border-white/10 !bg-[#11151d]/90 !shadow-xl !shadow-black/30 md:!block"
          nodeColor={() => "#71717a"}
          maskColor="rgba(3,7,18,0.58)"
        />
        <Controls className="!rounded-md !border !border-white/10 !bg-[#11151d]/90 !shadow-xl !shadow-black/30 [&_button]:!h-9 [&_button]:!w-9 [&_button]:!border-white/10 [&_button]:!bg-transparent [&_button]:!text-zinc-300 hover:[&_button]:!bg-white/10 sm:[&_button]:!h-7 sm:[&_button]:!w-7" />
      </ReactFlow>

      <button
        type="button"
        title="新建节点"
        aria-label="新建节点"
        data-openreel-workflow-ui="true"
        onClick={(event) => {
          event.stopPropagation()
          openCanvasCreateMenuAtCenter()
        }}
        className="absolute left-4 top-4 z-40 flex h-9 w-9 items-center justify-center rounded-md border border-white/10 bg-[#11151d]/92 text-zinc-100 shadow-xl shadow-black/30 backdrop-blur transition hover:border-cyan-300/35 hover:bg-cyan-300/12 hover:text-cyan-100"
      >
        <span className="text-base font-light leading-none">+</span>
      </button>

      <CanvasGroupLayer
        projectId={currentProject?.id}
        nodes={flowNodes}
        edges={flowEdges}
        selectedNodeIds={selectedNodeIds}
        viewport={viewport}
        containerRef={canvasContainerRef}
        applyNodeChanges={applyNodeChanges}
        registerUndo={pushUndo}
        onClearSelection={() => selectNode(null)}
        onGroupedNodeIdsChange={handleGroupedNodeIdsChange}
      />

      <AlignmentGuides guides={alignmentGuides} viewport={viewport} />

      {contextMenu?.previewLine && (
        <PendingConnectionPreview line={contextMenu.previewLine} />
      )}

      {contextMenu && (
        <div
          className="fixed z-[80] overflow-hidden rounded-lg border border-white/10 bg-[#10151d]/98 p-2 text-sm text-zinc-200 shadow-2xl shadow-black/55 backdrop-blur"
          style={menuPositionStyle(
            contextMenu.x,
            contextMenu.y,
            CANVAS_CREATE_MENU_WIDTH,
            contextMenu.connectFrom ? CANVAS_CONNECT_CREATE_MENU_HEIGHT : CANVAS_CREATE_MENU_HEIGHT,
          )}
          onClick={(event) => event.stopPropagation()}
          onContextMenu={(event) => event.preventDefault()}
        >
          <div className="border-b border-white/10 px-2 pb-2">
            <div className="text-xs font-semibold text-zinc-100">
              {contextMenu.connectFrom ? "接在当前节点后" : "添加到画布"}
            </div>
            <div className="mt-0.5 text-[11px] text-zinc-500">
              {contextMenu.connectFrom ? "新节点会自动建立依赖连线" : "选择要添加的节点"}
            </div>
          </div>
          <div className="py-2">
            <div className="px-2 pb-1 text-[10px] font-medium uppercase tracking-[0.16em] text-zinc-600">节点</div>
            {CANVAS_NODE_CREATE_ITEMS.map((item) => (
              <button
                key={item.type}
                type="button"
                className="group flex w-full items-center gap-3 rounded-md px-2 py-2 text-left transition-colors hover:bg-white/[0.07]"
                onClick={() => void handleCreateNode(item.type)}
              >
                <span className={cn("flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold ring-1", item.accentClass)}>
                  {item.badge}
                </span>
                <span className="min-w-0">
                  <span className="block text-xs font-medium text-zinc-100">{item.label}</span>
                  <span className="block truncate text-[11px] text-zinc-500 group-hover:text-zinc-400">{item.description}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {assetSaveRequest && (
        <div
          className="fixed inset-0 z-[95] flex items-center justify-center bg-black/55 p-4 backdrop-blur-sm"
          onClick={() => setAssetSaveRequest(null)}
        >
          <div
            className="w-full max-w-md rounded-lg border border-white/10 bg-[#11151d] p-4 text-zinc-100 shadow-2xl shadow-black/60"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-sm font-semibold">加入资产库</div>
                <div className="mt-1 text-[11px] text-zinc-500">保存到本地资产库文件夹</div>
              </div>
              <button
                type="button"
                onClick={() => setAssetSaveRequest(null)}
                className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]"
              >
                关闭
              </button>
            </div>
            <div className="mt-4 space-y-2">
              <label className="block text-[11px] text-zinc-500">
                资产标题
                <input
                  value={assetSaveForm.name}
                  onChange={(event) => setAssetSaveForm((current) => ({ ...current, name: event.target.value }))}
                  placeholder="用于文件命名"
                  className="mt-1 h-8 w-full rounded-md border border-white/10 bg-black/28 px-2 text-xs text-zinc-100 placeholder-zinc-600"
                />
              </label>
              <label className="block text-[11px] text-zinc-500">
                类型
                <select
                  value={assetSaveForm.kind}
                  onChange={(event) => setAssetSaveForm((current) => ({ ...current, kind: event.target.value }))}
                  className="mt-1 h-8 w-full rounded-md border border-white/10 bg-black/28 px-2 text-xs text-zinc-100"
                >
                  {ASSET_LIBRARY_KINDS.map((kind) => (
                    <option key={kind} value={kind}>{ASSET_LIBRARY_KIND_LABEL[kind] ?? kind}</option>
                  ))}
                </select>
              </label>
              <label className="block text-[11px] text-zinc-500">
                分类文件夹
                <input
                  value={assetSaveForm.category}
                  list="workflow-asset-category-options"
                  onChange={(event) => setAssetSaveForm((current) => ({ ...current, category: event.target.value }))}
                  placeholder="选择或输入新分类"
                  className="mt-1 h-8 w-full rounded-md border border-white/10 bg-black/28 px-2 text-xs text-zinc-100 placeholder-zinc-600"
                />
                <datalist id="workflow-asset-category-options">
                  {sharedAssetCategoryOptions.map((category) => <option key={category} value={category} />)}
                </datalist>
              </label>
            </div>
            {assetSaveError ? (
              <div className="mt-3 rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                {assetSaveError}
              </div>
            ) : null}
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setAssetSaveRequest(null)}
                className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.06]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void saveNodeToAssetLibrary()}
                disabled={assetSaveLoading || !assetSaveForm.name.trim() || !assetSaveForm.category.trim()}
                className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-950 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {assetSaveLoading ? "保存中" : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}

      {previewCanvasNode && currentProject?.id && (
        <NodeOutputPreviewCard
          node={previewCanvasNode}
          projectId={currentProject.id}
          readOnly={Boolean(nodePreviewRequest?.readOnly)}
          onClose={() => setNodePreviewRequest(null)}
          onTextSaved={async () => {
            await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
          }}
        />
      )}

      {nodeActionMenu && (
        <div
          className="openreel-canvas-action-menu fixed z-[80] w-44 overflow-hidden rounded-md border border-white/10 bg-[#11151d]/96 py-1 text-sm text-zinc-200 shadow-2xl shadow-black/50 backdrop-blur"
          style={menuPositionStyle(nodeActionMenu.x, nodeActionMenu.y, 176, nodeActionMenu.imageUrl ? 92 : 50)}
          onClick={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
        >
          {nodeActionMenu.imageUrl && (
            <button
              type="button"
              className="block w-full appearance-none bg-transparent px-3 py-2.5 text-left text-xs text-zinc-100 transition-colors hover:bg-white/10"
              onClick={() => void handleDownloadImageFromMenu(nodeActionMenu.imageUrl!, nodeActionMenu.title)}
            >
              保存图片
            </button>
          )}
          <button
            type="button"
            className="block w-full appearance-none bg-transparent px-3 py-2.5 text-left text-xs text-red-200 transition-colors hover:bg-red-500/12 hover:text-red-100"
            onClick={() => void handleDeleteNodeFromMenu(nodeActionMenu.nodeId)}
          >
            删除节点
          </button>
        </div>
      )}

      <AnimatePresence>
        {selectedCanvasNodeId && selectedNodeContextPanelStyle && (
          <NodeDetailPanel
            key={selectedCanvasNodeId}
            nodeId={selectedCanvasNodeId}
            projectId={currentProject?.id}
            onClose={() => selectNode(null)}
            onRerun={handleRerun}
            presentation="anchored"
            anchorStyle={selectedNodeContextPanelStyle}
            editRequestKey={nodeDetailEditRequestKey}
          />
        )}
      </AnimatePresence>

      {imageEditRequest && currentProject?.id && (
        <ImageEditPanel
          projectId={currentProject.id}
          nodeId={imageEditRequest.nodeId}
          title={imageEditRequest.title || "图片编辑"}
          imageUrl={imageEditRequest.imageUrl || ""}
          onClose={() => setImageEditRequest(null)}
          onCommitted={async () => {
            setImageEditRequest(null)
            await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
          }}
        />
      )}

      {videoEditRequest && currentProject?.id && (
        <VideoEditPanel
          projectId={currentProject.id}
          nodeId={videoEditRequest.nodeId}
          title={videoEditRequest.title || "视频剪辑"}
          videoUrl={videoEditRequest.videoUrl || ""}
          mediaNodes={videoEditMediaNodes}
          onClose={() => setVideoEditRequest(null)}
          onCommitted={async () => {
            await refreshCanvas({ preserveOnEmpty: true, preserveLayout: true })
          }}
        />
      )}

      {panoramaViewer && (
        <PanoramaViewer
          src={panoramaViewer.imageUrl}
          title={panoramaViewer.title}
          onClose={() => setPanoramaViewer(null)}
          onCapture={savePanoramaCapture}
        />
      )}
        </div>
      )}
    </div>
  )
}
