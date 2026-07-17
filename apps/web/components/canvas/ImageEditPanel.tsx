"use client"

import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react"
import { cleanupProjectNodeImageEdit, editProjectNodeImage, previewProjectNodeImageCurve, resolveMediaUrl, type ImageEditOperation } from "@/lib/api"

type EditTool = "crop" | "brush" | "fill" | "curve" | "text" | "arrow"
type FillShape = "rect" | "ellipse" | "lasso"
type CropHandle = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw"
type TextStyle = "plain" | "outline" | "subtitle" | "title"

interface Point {
  x: number
  y: number
}

interface Rect {
  x: number
  y: number
  width: number
  height: number
}

interface Size {
  width: number
  height: number
}

interface CropDrag {
  handle: CropHandle
  start: Point
  initial: Rect
}

interface DraftShape {
  kind: "path" | "rect" | "arrow"
  points: Point[]
  start?: Point
  current?: Point
}

interface TextDraft {
  position: Point
  value: string
}

interface ImageEditPanelProps {
  projectId: string
  nodeId: string
  title: string
  imageUrl: string
  onClose: () => void
  onCommitted: () => void | Promise<void>
}

interface ImageEditResult {
  ok?: boolean
  action?: string
  candidate_ref?: string
  image?: { local_url?: string; url?: string; width?: number; height?: number }
  node?: Record<string, unknown>
  error?: string
}

interface PreviewSnapshot {
  displayUrl: string
  sourceRef: string
  candidateRef: string | null
  operations: ImageEditOperation[]
  cropRect: Rect
  naturalImageSize: Size | null
}

const TOOL_LABELS: Record<EditTool, string> = {
  crop: "裁剪",
  brush: "画笔",
  fill: "覆盖",
  curve: "曲线",
  text: "文字",
  arrow: "箭头",
}

const TOOL_MARKS: Record<EditTool, string> = {
  crop: "裁",
  brush: "画",
  fill: "填",
  curve: "线",
  text: "文",
  arrow: "箭",
}

const COLOR_SWATCHES = [
  "#ffffff",
  "#111827",
  "#ef4444",
  "#f97316",
  "#facc15",
  "#22c55e",
  "#22d3ee",
  "#3b82f6",
  "#a855f7",
  "#ec4899",
]

const BRUSH_PRESETS = [4, 8, 16, 32, 56]
const TEXT_STYLE_OPTIONS: Array<{ id: TextStyle; label: string; strokeWidth: number; strokeColor: string; fontWeight: number }> = [
  { id: "outline", label: "描边", strokeWidth: 2, strokeColor: "#000000", fontWeight: 600 },
  { id: "plain", label: "纯文字", strokeWidth: 0, strokeColor: "#000000", fontWeight: 500 },
  { id: "subtitle", label: "字幕", strokeWidth: 4, strokeColor: "#000000", fontWeight: 650 },
  { id: "title", label: "标题", strokeWidth: 3, strokeColor: "#111827", fontWeight: 750 },
]
const DEFAULT_CROP_RECT: Rect = { x: 0, y: 0, width: 1, height: 1 }
const MIN_CROP_SIZE = 0.04
const CROP_HANDLES: Array<{ id: CropHandle; className: string; cursor: string }> = [
  { id: "nw", className: "left-0 top-0 -translate-x-1/2 -translate-y-1/2", cursor: "cursor-nwse-resize" },
  { id: "n", className: "left-1/2 top-0 -translate-x-1/2 -translate-y-1/2", cursor: "cursor-n-resize" },
  { id: "ne", className: "right-0 top-0 translate-x-1/2 -translate-y-1/2", cursor: "cursor-nesw-resize" },
  { id: "e", className: "right-0 top-1/2 translate-x-1/2 -translate-y-1/2", cursor: "cursor-e-resize" },
  { id: "se", className: "bottom-0 right-0 translate-x-1/2 translate-y-1/2", cursor: "cursor-nwse-resize" },
  { id: "s", className: "bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2", cursor: "cursor-s-resize" },
  { id: "sw", className: "bottom-0 left-0 -translate-x-1/2 translate-y-1/2", cursor: "cursor-nesw-resize" },
  { id: "w", className: "left-0 top-1/2 -translate-x-1/2 -translate-y-1/2", cursor: "cursor-w-resize" },
]

function clamp01(value: number) {
  return Math.max(0, Math.min(1, value))
}

function pointFromEvent(event: ReactPointerEvent<HTMLElement>, element: HTMLElement | null): Point | null {
  if (!element) return null
  const rect = element.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return null
  return {
    x: clamp01((event.clientX - rect.left) / rect.width),
    y: clamp01((event.clientY - rect.top) / rect.height),
  }
}

function normalizedRect(a: Point, b: Point) {
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  return {
    x,
    y,
    width: Math.max(0.001, Math.abs(a.x - b.x)),
    height: Math.max(0.001, Math.abs(a.y - b.y)),
  }
}

function clampCropRect(rect: Rect): Rect {
  const width = Math.max(MIN_CROP_SIZE, Math.min(1, rect.width))
  const height = Math.max(MIN_CROP_SIZE, Math.min(1, rect.height))
  return {
    x: Math.max(0, Math.min(1 - width, rect.x)),
    y: Math.max(0, Math.min(1 - height, rect.y)),
    width,
    height,
  }
}

function resizeCropRect(initial: Rect, start: Point, current: Point, handle: CropHandle): Rect {
  const dx = current.x - start.x
  const dy = current.y - start.y
  if (handle === "move") {
    return clampCropRect({
      ...initial,
      x: initial.x + dx,
      y: initial.y + dy,
    })
  }

  let left = initial.x
  let right = initial.x + initial.width
  let top = initial.y
  let bottom = initial.y + initial.height

  if (handle.includes("w")) left += dx
  if (handle.includes("e")) right += dx
  if (handle.includes("n")) top += dy
  if (handle.includes("s")) bottom += dy

  left = clamp01(left)
  right = clamp01(right)
  top = clamp01(top)
  bottom = clamp01(bottom)

  if (right - left < MIN_CROP_SIZE) {
    if (handle.includes("w")) left = Math.max(0, right - MIN_CROP_SIZE)
    else right = Math.min(1, left + MIN_CROP_SIZE)
  }
  if (bottom - top < MIN_CROP_SIZE) {
    if (handle.includes("n")) top = Math.max(0, bottom - MIN_CROP_SIZE)
    else bottom = Math.min(1, top + MIN_CROP_SIZE)
  }

  return clampCropRect({
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
  })
}

function cropForAspect(aspect: number | null): Rect {
  if (!aspect) return DEFAULT_CROP_RECT
  if (aspect >= 1) {
    const height = Math.min(1, 1 / aspect)
    const width = Math.min(1, height * aspect)
    return { x: (1 - width) / 2, y: (1 - height) / 2, width, height }
  }
  const width = Math.min(1, aspect)
  const height = Math.min(1, width / aspect)
  return { x: (1 - width) / 2, y: (1 - height) / 2, width, height }
}

function pathData(points: Point[]) {
  if (points.length === 0) return ""
  return points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ")
}

function rectPoints(rect: { x?: unknown; y?: unknown; width?: unknown; height?: unknown }) {
  const x = Number(rect.x || 0)
  const y = Number(rect.y || 0)
  const width = Number(rect.width || 0)
  const height = Number(rect.height || 0)
  return { x, y, width, height }
}

function numberValue(value: unknown, fallback: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function pointValue(value: unknown): Point {
  const point = (value || {}) as { x?: unknown; y?: unknown }
  return {
    x: clamp01(numberValue(point.x, 0)),
    y: clamp01(numberValue(point.y, 0)),
  }
}

function textStyleOption(value: unknown) {
  const key = String(value || "outline")
  return TEXT_STYLE_OPTIONS.find((item) => item.id === key) || TEXT_STYLE_OPTIONS[0]
}

function textShadow(strokeColor: string, strokeWidth: number) {
  const width = Math.max(0, strokeWidth)
  if (width <= 0) return "none"
  const offset = Math.max(1, Math.round(width))
  return [
    `${offset}px 0 0 ${strokeColor}`,
    `-${offset}px 0 0 ${strokeColor}`,
    `0 ${offset}px 0 ${strokeColor}`,
    `0 -${offset}px 0 ${strokeColor}`,
    `0 0 ${Math.max(2, offset * 1.5)}px ${strokeColor}`,
  ].join(", ")
}

function resultImageSize(result: ImageEditResult): Size | null {
  const width = Number(result.image?.width)
  const height = Number(result.image?.height)
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null
  return { width, height }
}

function versionedDisplayUrl(url: string, version: number) {
  const resolved = resolveMediaUrl(url)
  if (!resolved || version <= 0 || resolved.startsWith("data:")) return resolved
  if (!resolved.includes("/api/media/") && !resolved.includes("/api/uploads/") && !resolved.includes("/api/assets/")) return resolved
  const separator = resolved.includes("?") ? "&" : "?"
  return `${resolved}${separator}image_edit_v=${version}`
}

function ellipseFromRect(rect: Rect) {
  return {
    cx: rect.x + rect.width / 2,
    cy: rect.y + rect.height / 2,
    rx: rect.width / 2,
    ry: rect.height / 2,
  }
}

function operationOverlay(operation: ImageEditOperation, index: number) {
  const type = operation.type
  if (type === "brush") {
    const points = Array.isArray(operation.points) ? operation.points as Point[] : []
    const strokeWidth = numberValue(operation.display_width, numberValue(operation.width, 8))
    return (
      <path
        key={index}
        d={pathData(points)}
        fill="none"
        stroke={String(operation.color || "#67e8f9")}
        strokeWidth={Math.max(1, strokeWidth)}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
        opacity={Number(operation.opacity || 1)}
      />
    )
  }
  if (type === "crop") {
    const rect = rectPoints((operation.rect || {}) as Record<string, unknown>)
    return (
      <rect
        key={index}
        x={rect.x}
        y={rect.y}
        width={rect.width}
        height={rect.height}
        fill="rgba(34,211,238,0.08)"
        stroke="#67e8f9"
        strokeWidth={2}
        vectorEffect="non-scaling-stroke"
      />
    )
  }
  if (type === "fill") {
    const style = (operation.style || {}) as { type?: string; color?: string; opacity?: number }
    const fill = String(style.color || operation.color || "#22d3ee")
    const opacity = Number(style.opacity ?? 0.55)
    if (operation.shape === "polygon") {
      const points = Array.isArray(operation.points) ? operation.points as Point[] : []
      return <path key={index} d={`${pathData(points)} Z`} fill={fill} opacity={opacity} stroke="#67e8f9" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    }
    const rect = rectPoints((operation.rect || {}) as Record<string, unknown>)
    if (operation.shape === "ellipse") {
      const ellipse = ellipseFromRect(rect)
      return <ellipse key={index} cx={ellipse.cx} cy={ellipse.cy} rx={ellipse.rx} ry={ellipse.ry} fill={fill} opacity={opacity} stroke="#67e8f9" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    }
    return <rect key={index} x={rect.x} y={rect.y} width={rect.width} height={rect.height} fill={fill} opacity={opacity} stroke="#67e8f9" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
  }
  if (type === "mask" || type === "selection" || type === "segment") {
    if (operation.shape === "polygon") {
      const points = Array.isArray(operation.points) ? operation.points as Point[] : []
      return <path key={index} d={`${pathData(points)} Z`} fill="rgba(248,250,252,0.06)" stroke="#f8fafc" strokeDasharray="0.012 0.01" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    }
    const rect = rectPoints((operation.rect || operation.box || operation.bounds || {}) as Record<string, unknown>)
    if (operation.shape === "ellipse") {
      const ellipse = ellipseFromRect(rect)
      return <ellipse key={index} cx={ellipse.cx} cy={ellipse.cy} rx={ellipse.rx} ry={ellipse.ry} fill="rgba(248,250,252,0.06)" stroke="#f8fafc" strokeDasharray="0.012 0.01" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    }
    return <rect key={index} x={rect.x} y={rect.y} width={rect.width} height={rect.height} fill="rgba(248,250,252,0.06)" stroke="#f8fafc" strokeDasharray="0.012 0.01" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
  }
  if (type === "arrow") {
    const start = (operation.start || {}) as Point
    const end = (operation.end || {}) as Point
    const strokeWidth = numberValue(operation.display_width, numberValue(operation.width, 6))
    return (
      <line
        key={index}
        x1={Number(start.x || 0)}
        y1={Number(start.y || 0)}
        x2={Number(end.x || 0)}
        y2={Number(end.y || 0)}
        stroke={String(operation.color || "#ffffff")}
        strokeWidth={Math.max(1, strokeWidth)}
        markerEnd="url(#image-edit-arrow)"
        vectorEffect="non-scaling-stroke"
        strokeLinecap="round"
      />
    )
  }
  return null
}

function textOperationOverlay(operation: ImageEditOperation, index: number) {
  if (operation.type !== "text") return null
  const value = String(operation.text || "").trim()
  if (!value) return null
  const position = pointValue(operation.position)
  const style = textStyleOption(operation.text_style)
  const strokeWidth = numberValue(operation.display_stroke_width, style.strokeWidth)
  const strokeColor = String(operation.stroke_color || style.strokeColor)
  const fontSize = Math.max(10, numberValue(operation.display_font_size, numberValue(operation.font_size, 36)))
  return (
    <div
      key={`text-${index}`}
      className="absolute max-w-[70%] whitespace-pre-wrap break-words leading-tight"
      style={{
        left: `${position.x * 100}%`,
        top: `${position.y * 100}%`,
        color: String(operation.color || "#ffffff"),
        fontSize,
        fontWeight: style.fontWeight,
        textShadow: textShadow(strokeColor, strokeWidth),
      }}
    >
      {value}
    </div>
  )
}

function ColorControl({
  value,
  onChange,
}: {
  value: string
  onChange: (value: string) => void
}) {
  return (
    <div className="space-y-2.5">
      <div className="grid grid-cols-5 gap-1.5">
        {COLOR_SWATCHES.map((item) => (
          <button
            key={item}
            type="button"
            title={item}
            onClick={() => onChange(item)}
            className={`h-7 rounded-[5px] border transition ${value.toLowerCase() === item.toLowerCase() ? "border-white shadow-[0_0_0_2px_rgba(255,255,255,0.2)]" : "border-white/12 hover:border-white/45"}`}
            style={{ background: item }}
          />
        ))}
      </div>
      <div className="flex items-center gap-2">
        <input
          type="color"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-9 w-11 rounded-md border border-white/10 bg-black/40 p-1"
        />
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-9 min-w-0 flex-1 rounded-md border border-white/10 bg-black/35 px-2 text-xs font-medium uppercase text-zinc-100 outline-none focus:border-cyan-200/60"
        />
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3 rounded-md border border-white/10 bg-white/[0.03] p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.035)]">
      <div className="text-[11px] font-semibold text-zinc-400">{title}</div>
      {children}
    </section>
  )
}

export default function ImageEditPanel({
  projectId,
  nodeId,
  title,
  imageUrl,
  onClose,
  onCommitted,
}: ImageEditPanelProps) {
  const stageRef = useRef<HTMLDivElement | null>(null)
  const canvasViewportRef = useRef<HTMLDivElement | null>(null)
  const textInputRef = useRef<HTMLTextAreaElement | null>(null)
  const [tool, setTool] = useState<EditTool>("crop")
  const [displayUrl, setDisplayUrl] = useState(imageUrl)
  const [displayVersion, setDisplayVersion] = useState(0)
  const [sourceRef, setSourceRef] = useState(imageUrl)
  const [candidateRef, setCandidateRef] = useState<string | null>(null)
  const [operations, setOperations] = useState<ImageEditOperation[]>([])
  const [previewHistory, setPreviewHistory] = useState<PreviewSnapshot[]>([])
  const [draft, setDraft] = useState<DraftShape | null>(null)
  const [cropRect, setCropRect] = useState<Rect>(DEFAULT_CROP_RECT)
  const [cropDrag, setCropDrag] = useState<CropDrag | null>(null)
  const [busy, setBusy] = useState<"curve" | "commit" | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [color, setColor] = useState("#22d3ee")
  const [opacity, setOpacity] = useState(0.55)
  const [brushWidth, setBrushWidth] = useState(18)
  const [fontSize, setFontSize] = useState(44)
  const [textStyle, setTextStyle] = useState<TextStyle>("outline")
  const [textDraft, setTextDraft] = useState<TextDraft | null>(null)
  const [fillShape, setFillShape] = useState<FillShape>("rect")
  const [curveDetail, setCurveDetail] = useState(0.78)
  const [curveStrength, setCurveStrength] = useState(0.92)
  const [curveBaseVisibility, setCurveBaseVisibility] = useState(0.12)
  const [naturalImageSize, setNaturalImageSize] = useState<Size | null>(null)
  const [canvasViewportSize, setCanvasViewportSize] = useState<Size>({ width: 0, height: 0 })

  useEffect(() => {
    setDisplayUrl(imageUrl)
    setDisplayVersion(0)
    setSourceRef(imageUrl)
    setCandidateRef(null)
    setOperations([])
    setPreviewHistory([])
    setDraft(null)
    setTextDraft(null)
    setCropRect(DEFAULT_CROP_RECT)
    setCropDrag(null)
    setNaturalImageSize(null)
    setError(null)
  }, [imageUrl, nodeId])

  useEffect(() => {
    if (!textDraft) return
    const timer = window.setTimeout(() => textInputRef.current?.focus(), 0)
    return () => window.clearTimeout(timer)
  }, [textDraft])

  useEffect(() => {
    if (tool !== "text") setTextDraft(null)
  }, [tool])

  useEffect(() => {
    return () => {
      void cleanupProjectNodeImageEdit(projectId, nodeId).catch(() => undefined)
    }
  }, [projectId, nodeId])

  const previewDisplayUrl = useMemo(() => versionedDisplayUrl(displayUrl, displayVersion), [displayUrl, displayVersion])
  useEffect(() => {
    const element = canvasViewportRef.current
    if (!element) return
    const update = () => {
      const rect = element.getBoundingClientRect()
      setCanvasViewportSize({ width: rect.width, height: rect.height })
    }
    update()
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", update)
      return () => window.removeEventListener("resize", update)
    }
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  const stageSize = useMemo<Size | null>(() => {
    if (!naturalImageSize || canvasViewportSize.width <= 0 || canvasViewportSize.height <= 0) return null
    const maxWidth = Math.max(120, canvasViewportSize.width - 32)
    const maxHeight = Math.max(120, canvasViewportSize.height - 32)
    const scale = Math.min(maxWidth / naturalImageSize.width, maxHeight / naturalImageSize.height, 1)
    return {
      width: Math.max(1, Math.floor(naturalImageSize.width * scale)),
      height: Math.max(1, Math.floor(naturalImageSize.height * scale)),
    }
  }, [canvasViewportSize.height, canvasViewportSize.width, naturalImageSize])

  const imageDisplayScale = useMemo(() => {
    if (!stageSize || !naturalImageSize) return 1
    const scale = Math.min(stageSize.width / naturalImageSize.width, stageSize.height / naturalImageSize.height)
    return Number.isFinite(scale) && scale > 0 ? scale : 1
  }, [naturalImageSize, stageSize])

  const displayPixelsToSourcePixels = useCallback((value: number) => {
    if (value <= 0) return 0
    return Math.max(1, Math.round(value / Math.max(imageDisplayScale, 0.001)))
  }, [imageDisplayScale])

  const hasPendingText = Boolean(textDraft?.value.trim())
  const canCommit = busy === null && (Boolean(candidateRef) || operations.length > 0 || hasPendingText)
  const canUndo = busy === null && (Boolean(textDraft) || operations.length > 0 || previewHistory.length > 0)
  const pendingOperationCount = operations.length + (hasPendingText ? 1 : 0)

  const appendOperation = (operation: ImageEditOperation) => {
    setOperations((current) => [...current, operation])
    setError(null)
  }

  const pushPreviewSnapshot = () => {
    setPreviewHistory((current) => [
      ...current,
      {
        displayUrl,
        sourceRef,
        candidateRef,
        operations,
        cropRect,
        naturalImageSize,
      },
    ])
  }

  const applyPreviewResult = (nextRef: string, result: ImageEditResult) => {
    setCandidateRef(nextRef)
    setSourceRef(nextRef)
    setNaturalImageSize(resultImageSize(result))
    setDisplayUrl(nextRef)
    setDisplayVersion(Date.now())
    setOperations([])
    setDraft(null)
    setTextDraft(null)
    setCropRect(DEFAULT_CROP_RECT)
    setCropDrag(null)
  }

  const undoLastEdit = () => {
    if (busy !== null) return
    if (textDraft) {
      setTextDraft(null)
      setError(null)
      return
    }
    if (operations.length > 0) {
      setOperations((current) => current.slice(0, -1))
      setError(null)
      return
    }
    const snapshot = previewHistory[previewHistory.length - 1]
    if (!snapshot) return
    setPreviewHistory((current) => current.slice(0, -1))
    setDisplayUrl(snapshot.displayUrl)
    setSourceRef(snapshot.sourceRef)
    setCandidateRef(snapshot.candidateRef)
    setOperations(snapshot.operations)
    setCropRect(snapshot.cropRect)
    setNaturalImageSize(snapshot.naturalImageSize)
    setDisplayVersion(Date.now())
    setDraft(null)
    setTextDraft(null)
    setCropDrag(null)
    setError(null)
  }

  const buildTextOperation = useCallback((position: Point, value: string): ImageEditOperation => {
    const style = textStyleOption(textStyle)
    return {
      type: "text",
      unit: "normalized",
      position,
      text: value.trim(),
      color,
      opacity: 1,
      text_style: style.id,
      font_size: displayPixelsToSourcePixels(fontSize),
      display_font_size: fontSize,
      stroke_width: displayPixelsToSourcePixels(style.strokeWidth),
      display_stroke_width: style.strokeWidth,
      stroke_color: style.strokeColor,
    }
  }, [color, displayPixelsToSourcePixels, fontSize, textStyle])

  const commitTextDraft = () => {
    if (!textDraft) return
    const value = textDraft.value.trim()
    if (!value) {
      setTextDraft(null)
      return
    }
    appendOperation(buildTextOperation(textDraft.position, value))
    setTextDraft(null)
  }

  const settleTextDraft = () => {
    if (!textDraft) return
    const value = textDraft.value.trim()
    if (value) appendOperation(buildTextOperation(textDraft.position, value))
    setTextDraft(null)
  }

  const buildFillOperation = useCallback((selection: { shape: "rect" | "ellipse"; rect: Rect } | { shape: "polygon"; points: Point[] }): ImageEditOperation => {
    return {
      type: "fill",
      unit: "normalized",
      shape: selection.shape,
      ...(selection.shape === "polygon" ? { points: selection.points } : { rect: selection.rect }),
      style: {
        type: "solid",
        color,
        opacity,
        spacing: 28,
        line_width: 2,
      },
    }
  }, [color, opacity])

  const applyCropOperation = () => {
    appendOperation({ type: "crop", unit: "normalized", rect: cropRect })
  }

  const startCropDrag = (handle: CropHandle, event: ReactPointerEvent<HTMLElement>) => {
    if (busy || tool !== "crop") return
    const point = pointFromEvent(event, stageRef.current)
    if (!point) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    setCropDrag({ handle, start: point, initial: cropRect })
  }

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (busy) return
    const point = pointFromEvent(event, stageRef.current)
    if (!point) return
    event.currentTarget.setPointerCapture(event.pointerId)
    if (tool === "crop" || tool === "curve") return
    if (tool === "text") {
      if (textDraft) return
      setTextDraft({ position: point, value: "" })
      setError(null)
      return
    }
    if (tool === "brush" || (tool === "fill" && fillShape === "lasso")) {
      setDraft({ kind: "path", points: [point] })
      return
    }
    setDraft({ kind: tool === "arrow" ? "arrow" : "rect", points: [], start: point, current: point })
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (cropDrag && tool === "crop") {
      const point = pointFromEvent(event, stageRef.current)
      if (!point) return
      setCropRect(resizeCropRect(cropDrag.initial, cropDrag.start, point, cropDrag.handle))
      return
    }
    if (!draft || busy) return
    const point = pointFromEvent(event, stageRef.current)
    if (!point) return
    if (draft.kind === "path") {
      setDraft({ ...draft, points: [...draft.points, point] })
      return
    }
    setDraft({ ...draft, current: point })
  }

  const finishDraft = () => {
    if (cropDrag) {
      setCropDrag(null)
      return
    }
    if (!draft || busy) return
    if (tool === "brush" && draft.kind === "path" && draft.points.length >= 2) {
      const sourceWidth = displayPixelsToSourcePixels(brushWidth)
      appendOperation({
        type: "brush",
        unit: "normalized",
        points: draft.points,
        color,
        opacity: 1,
        width: sourceWidth,
        display_width: brushWidth,
      })
    } else if (tool === "fill" && draft.kind === "path" && draft.points.length >= 3) {
      appendOperation(buildFillOperation({ shape: "polygon", points: draft.points }))
    } else if (draft.start && draft.current) {
      if (tool === "fill") {
        appendOperation(buildFillOperation({ shape: fillShape === "ellipse" ? "ellipse" : "rect", rect: normalizedRect(draft.start, draft.current) }))
      } else if (tool === "arrow") {
        const displayWidth = Math.max(2, Math.round(brushWidth / 2))
        const displayHeadSize = Math.max(10, brushWidth * 2)
        appendOperation({
          type: "arrow",
          unit: "normalized",
          start: draft.start,
          end: draft.current,
          color,
          opacity: 1,
          width: displayPixelsToSourcePixels(displayWidth),
          display_width: displayWidth,
          head_size: displayPixelsToSourcePixels(displayHeadSize),
          display_head_size: displayHeadSize,
        })
      }
    }
    setDraft(null)
  }

  const previewCurve = async () => {
    setBusy("curve")
    setError(null)
    try {
      const result = await previewProjectNodeImageCurve<ImageEditResult>(projectId, nodeId, {
        source_ref: sourceRef,
        color,
        detail: curveDetail,
        line_strength: curveStrength,
        base_visibility: curveBaseVisibility,
      })
      const nextRef = result.candidate_ref || result.image?.local_url || result.image?.url
      if (!nextRef) throw new Error("曲线图没有返回图片")
      pushPreviewSnapshot()
      applyPreviewResult(nextRef, result)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const commitEdit = async () => {
    const pendingTextOperation = textDraft?.value.trim() ? buildTextOperation(textDraft.position, textDraft.value) : null
    const nextOperations = pendingTextOperation ? [...operations, pendingTextOperation] : operations
    if (!candidateRef && nextOperations.length === 0) {
      setError("请先添加编辑操作或生成曲线图")
      return
    }
    setBusy("commit")
    setError(null)
    try {
      await editProjectNodeImage<ImageEditResult>(projectId, nodeId, {
        action: "commit",
        candidate_ref: candidateRef,
        source_ref: candidateRef ? undefined : sourceRef,
        operations: nextOperations,
      })
      await onCommitted()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const closePanel = () => {
    void cleanupProjectNodeImageEdit(projectId, nodeId).catch(() => undefined)
    onClose()
  }

  const draftOverlay = draft ? (() => {
    const fillPreview = color
    const stroke = "#facc15"
    if (draft.kind === "path") return <path d={pathData(draft.points)} fill="none" stroke="#facc15" strokeWidth={2} vectorEffect="non-scaling-stroke" strokeLinecap="round" strokeLinejoin="round" />
    if (draft.start && draft.current && draft.kind === "arrow") {
      return <line x1={draft.start.x} y1={draft.start.y} x2={draft.current.x} y2={draft.current.y} stroke="#facc15" strokeWidth={2} markerEnd="url(#image-edit-arrow)" vectorEffect="non-scaling-stroke" />
    }
    if (draft.start && draft.current) {
      const rect = normalizedRect(draft.start, draft.current)
      if (tool === "fill" && fillShape === "ellipse") {
        const ellipse = ellipseFromRect(rect)
        return <ellipse cx={ellipse.cx} cy={ellipse.cy} rx={ellipse.rx} ry={ellipse.ry} fill={fillPreview} opacity={Math.max(0.25, opacity)} stroke={stroke} strokeWidth={2} vectorEffect="non-scaling-stroke" />
      }
      return <rect x={rect.x} y={rect.y} width={rect.width} height={rect.height} fill={tool === "fill" ? fillPreview : "rgba(250,204,21,0.12)"} opacity={tool === "fill" ? Math.max(0.25, opacity) : 1} stroke={stroke} strokeWidth={2} vectorEffect="non-scaling-stroke" />
    }
    return null
  })() : null

  const cropStyle = {
    left: `${cropRect.x * 100}%`,
    top: `${cropRect.y * 100}%`,
    width: `${cropRect.width * 100}%`,
    height: `${cropRect.height * 100}%`,
  }
  const cropShadeStyle = {
    top: `${cropRect.y * 100}%`,
    left: `${cropRect.x * 100}%`,
    right: `${(1 - cropRect.x - cropRect.width) * 100}%`,
    bottom: `${(1 - cropRect.y - cropRect.height) * 100}%`,
  }
  const activeTextStyle = textStyleOption(textStyle)
  const textDraftTransform = textDraft
    ? [
        textDraft.position.x > 0.66 ? "translateX(-100%)" : "",
        textDraft.position.y > 0.72 ? "translateY(-100%)" : "",
      ].filter(Boolean).join(" ")
    : undefined

  return (
    <div
      className="openreel-image-edit-panel absolute inset-4 z-[92] overflow-hidden rounded-2xl border border-white/10 bg-[#080a0f]/96 text-zinc-100 shadow-[0_28px_90px_rgba(0,0,0,0.62)] backdrop-blur-md"
      onPointerDown={(event) => {
        settleTextDraft()
        event.stopPropagation()
      }}
    >
      <div className="flex h-full min-h-0 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-3 border-b border-white/10 bg-[#10141d] px-3">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <div className="truncate text-sm font-semibold">{title || "未命名图片"}</div>
              {pendingOperationCount > 0 ? (
                <span className="shrink-0 rounded border border-cyan-200/18 bg-cyan-300/10 px-1.5 py-0.5 text-[11px] font-medium text-cyan-100">{pendingOperationCount} 个未保存操作</span>
              ) : candidateRef ? (
                <span className="shrink-0 rounded border border-emerald-200/18 bg-emerald-300/10 px-1.5 py-0.5 text-[11px] font-medium text-emerald-100">候选图待保存</span>
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={undoLastEdit} disabled={!canUndo} className="h-7 rounded-md border border-white/10 px-2.5 text-xs font-medium text-zinc-300 hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-35">撤销</button>
            <button type="button" onClick={() => { setOperations([]); setTextDraft(null) }} disabled={busy !== null || (operations.length === 0 && !textDraft)} className="h-7 rounded-md border border-white/10 px-2.5 text-xs font-medium text-zinc-300 hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-35">清空</button>
            <button type="button" onClick={() => void commitEdit()} disabled={!canCommit} className="h-7 rounded-md bg-cyan-300 px-3 text-xs font-semibold text-cyan-950 hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-40">{busy === "commit" ? "保存中..." : "保存"}</button>
            <button type="button" onClick={closePanel} disabled={busy !== null} className="h-7 rounded-md border border-white/10 px-2.5 text-xs font-medium text-zinc-300 hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40">关闭</button>
          </div>
        </header>

        <div className="grid min-h-0 flex-1 grid-cols-[48px_252px_minmax(0,1fr)]">
          <nav className="flex min-h-0 flex-col gap-1 border-r border-white/10 bg-[#0d1119] p-1.5">
            {(Object.keys(TOOL_LABELS) as EditTool[]).map((item) => (
              <button
                key={item}
                type="button"
                title={TOOL_LABELS[item]}
                onClick={() => setTool(item)}
                className={`flex h-10 flex-col items-center justify-center gap-0.5 rounded-md border text-[9px] font-semibold leading-none transition ${tool === item ? "border-cyan-200/45 bg-cyan-300/14 text-cyan-100 shadow-[inset_2px_0_0_rgba(103,232,249,0.9)]" : "border-transparent text-zinc-500 hover:border-white/10 hover:bg-white/[0.045] hover:text-zinc-200"}`}
              >
                <span className={`flex h-5 w-5 items-center justify-center rounded border text-[11px] ${tool === item ? "border-cyan-200/35 bg-cyan-200/15" : "border-white/10 bg-white/[0.035]"}`}>
                  {TOOL_MARKS[item]}
                </span>
                {TOOL_LABELS[item]}
              </button>
            ))}
          </nav>

          <aside className="flex min-h-0 flex-col border-r border-white/10 bg-[#0f131b]">
            <div className="min-h-0 flex-1 space-y-2.5 overflow-auto p-3">
              {tool === "crop" && (
                <Section title="裁剪">
                  <div className="grid grid-cols-4 gap-1.5">
                    {[
                      ["全图", null],
                      ["1:1", 1],
                      ["16:9", 16 / 9],
                      ["9:16", 9 / 16],
                    ].map(([label, aspect]) => (
                      <button
                        key={String(label)}
                        type="button"
                        onClick={() => setCropRect(cropForAspect(aspect as number | null))}
                        className="h-8 rounded-md border border-white/10 bg-black/28 text-xs font-medium text-zinc-200 hover:bg-white/[0.08]"
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs text-zinc-500">
                    <div className="rounded border border-white/10 bg-black/25 px-2 py-1.5">W {Math.round(cropRect.width * 100)}%</div>
                    <div className="rounded border border-white/10 bg-black/25 px-2 py-1.5">H {Math.round(cropRect.height * 100)}%</div>
                  </div>
                  <button type="button" onClick={applyCropOperation} disabled={busy !== null} className="h-9 w-full rounded-md bg-cyan-300 text-sm font-semibold text-cyan-950 hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-45">应用裁剪</button>
                </Section>
              )}

              {(tool === "brush" || tool === "arrow") && (
                <Section title={tool === "brush" ? "画笔" : "箭头"}>
                  <ColorControl value={color} onChange={setColor} />
                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-xs text-zinc-400">
                      <span>线宽</span>
                      <span>{brushWidth}px</span>
                    </div>
                    <div className="grid grid-cols-5 gap-1.5">
                      {BRUSH_PRESETS.map((size) => (
                        <button
                          key={size}
                          type="button"
                          onClick={() => setBrushWidth(size)}
                          className={`flex h-8 items-center justify-center rounded-md border text-[11px] font-medium transition ${brushWidth === size ? "border-cyan-200 bg-cyan-300/15 text-cyan-100" : "border-white/10 bg-black/24 text-zinc-300 hover:bg-white/[0.08]"}`}
                        >
                          {size}
                        </button>
                      ))}
                    </div>
                    <input type="range" min={2} max={80} value={brushWidth} onChange={(event) => setBrushWidth(Number(event.target.value))} className="w-full accent-cyan-300" />
                    <div className="rounded-md border border-white/10 bg-black/28 px-3 py-2">
                      <div className="h-7 rounded bg-white/[0.04] p-3">
                        <div className="rounded-full" style={{ height: Math.max(2, Math.min(24, brushWidth / 2)), background: color }} />
                      </div>
                    </div>
                  </div>
                </Section>
              )}

              {tool === "fill" && (
                <Section title="颜色填充">
                  <div className="grid grid-cols-3 gap-1.5">
                    {(["rect", "ellipse", "lasso"] as FillShape[]).map((item) => (
                      <button
                        key={item}
                        type="button"
                        onClick={() => setFillShape(item)}
                        className={`h-9 rounded-md border text-xs font-semibold transition ${fillShape === item ? "border-cyan-200 bg-cyan-300/15 text-cyan-100" : "border-white/10 bg-black/24 text-zinc-300 hover:bg-white/[0.08]"}`}
                      >
                        {item === "rect" ? "矩形" : item === "ellipse" ? "椭圆" : "自由"}
                      </button>
                    ))}
                  </div>
                  <ColorControl value={color} onChange={setColor} />
                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-xs text-zinc-400">
                      <span>不透明度</span>
                      <span>{Math.round(opacity * 100)}%</span>
                    </div>
                    <input type="range" min={0.05} max={1} step={0.05} value={opacity} onChange={(event) => setOpacity(Number(event.target.value))} className="w-full accent-cyan-300" />
                  </div>
                </Section>
              )}

              {tool === "curve" && (
                <Section title="整图曲线">
                  <ColorControl value={color} onChange={setColor} />
                  <div className="space-y-3 rounded-md border border-white/10 bg-black/22 p-3">
                    <label className="space-y-1 text-xs text-zinc-400">
                      <span>曲线密度 {Math.round(curveDetail * 100)}%</span>
                      <input type="range" min={0.15} max={1} step={0.05} value={curveDetail} onChange={(event) => setCurveDetail(Number(event.target.value))} className="w-full accent-cyan-300" />
                    </label>
                    <label className="space-y-1 text-xs text-zinc-400">
                      <span>线条强度 {Math.round(curveStrength * 100)}%</span>
                      <input type="range" min={0.2} max={1} step={0.05} value={curveStrength} onChange={(event) => setCurveStrength(Number(event.target.value))} className="w-full accent-cyan-300" />
                    </label>
                    <label className="space-y-1 text-xs text-zinc-400">
                      <span>原图保留 {Math.round(curveBaseVisibility * 100)}%</span>
                      <input type="range" min={0} max={0.35} step={0.01} value={curveBaseVisibility} onChange={(event) => setCurveBaseVisibility(Number(event.target.value))} className="w-full accent-cyan-300" />
                    </label>
                  </div>
                  <button type="button" onClick={() => void previewCurve()} disabled={busy !== null} className="h-9 w-full rounded-md bg-cyan-300 text-sm font-semibold text-cyan-950 hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-45">
                    {busy === "curve" ? "生成中..." : "生成整图曲线图"}
                  </button>
                </Section>
              )}

              {tool === "text" && (
                <Section title="文字">
                  <label className="space-y-2 text-xs text-zinc-400">
                    <span>样式</span>
                    <select
                      value={textStyle}
                      onChange={(event) => setTextStyle(event.target.value as TextStyle)}
                      className="h-9 w-full rounded-md border border-white/10 bg-black/35 px-2 text-sm font-medium text-zinc-100 outline-none focus:border-cyan-300/45"
                    >
                      {TEXT_STYLE_OPTIONS.map((item) => (
                        <option key={item.id} value={item.id}>{item.label}</option>
                      ))}
                    </select>
                  </label>
                  <ColorControl value={color} onChange={setColor} />
                  <label className="space-y-2 text-xs text-zinc-400">
                    <span>字号 {fontSize}</span>
                    <input type="range" min={12} max={180} value={fontSize} onChange={(event) => setFontSize(Number(event.target.value))} className="w-full accent-cyan-300" />
                  </label>
                </Section>
              )}

              {error ? <div className="rounded-md border border-red-400/20 bg-red-950/35 px-3 py-2 text-xs leading-5 text-red-200">{error}</div> : null}
            </div>
          </aside>

          <main ref={canvasViewportRef} className="min-w-0 overflow-auto bg-[linear-gradient(45deg,rgba(255,255,255,0.035)_25%,transparent_25%),linear-gradient(-45deg,rgba(255,255,255,0.035)_25%,transparent_25%),linear-gradient(45deg,transparent_75%,rgba(255,255,255,0.035)_75%),linear-gradient(-45deg,transparent_75%,rgba(255,255,255,0.035)_75%)] bg-[length:28px_28px] bg-[position:0_0,0_14px,14px_-14px,-14px_0]">
            <div className="flex min-h-full items-center justify-center p-4">
              <div
                ref={stageRef}
                className="relative overflow-visible rounded-sm bg-black shadow-[0_32px_90px_rgba(0,0,0,0.55)]"
                style={stageSize ? { width: stageSize.width, height: stageSize.height } : undefined}
                onPointerDown={handlePointerDown}
                onPointerMove={handlePointerMove}
                onPointerUp={finishDraft}
                onPointerCancel={() => {
                  setDraft(null)
                  setCropDrag(null)
                }}
              >
                <img
                  src={previewDisplayUrl}
                  alt={title || ""}
                  className={stageSize ? "block h-full w-full select-none object-contain" : "block max-h-[calc(100dvh-92px)] max-w-[calc(100vw-330px)] select-none object-contain"}
                  draggable={false}
                  onLoad={(event) => {
                    const image = event.currentTarget
                    if (image.naturalWidth > 0 && image.naturalHeight > 0) {
                      setNaturalImageSize({ width: image.naturalWidth, height: image.naturalHeight })
                    }
                  }}
                />
                <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 1 1" preserveAspectRatio="none">
                  <defs>
                    <marker id="image-edit-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
                      <path d="M0,0 L8,4 L0,8 Z" fill="currentColor" />
                    </marker>
                  </defs>
                  {operations.map(operationOverlay)}
                  {draftOverlay}
                </svg>
                <div className="pointer-events-none absolute inset-0">
                  {operations.map(textOperationOverlay)}
                </div>
                {textDraft ? (
                  <div
                    className="absolute z-20 min-w-32 max-w-[70%] border border-cyan-100/85 bg-white/[0.035]"
                    style={{
                      left: `${textDraft.position.x * 100}%`,
                      top: `${textDraft.position.y * 100}%`,
                      transform: textDraftTransform || undefined,
                    }}
                    onPointerDown={(event) => event.stopPropagation()}
                  >
                    <textarea
                      ref={textInputRef}
                      value={textDraft.value}
                      onChange={(event) => setTextDraft((current) => current ? { ...current, value: event.target.value } : current)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !event.shiftKey) {
                          event.preventDefault()
                          commitTextDraft()
                        } else if (event.key === "Escape") {
                          event.preventDefault()
                          setTextDraft(null)
                        }
                      }}
                      rows={2}
                      className="block min-h-12 w-[min(320px,70vw)] resize overflow-auto border-0 bg-transparent px-1.5 py-1 leading-tight text-zinc-100 outline-none"
                      style={{
                        color,
                        fontSize,
                        fontWeight: activeTextStyle.fontWeight,
                        textShadow: textShadow(activeTextStyle.strokeColor, activeTextStyle.strokeWidth),
                      }}
                    />
                  </div>
                ) : null}
                {tool === "crop" && (
                  <>
                    <div className="pointer-events-none absolute inset-x-0 top-0 bg-black/58" style={{ height: cropShadeStyle.top }} />
                    <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-black/58" style={{ height: cropShadeStyle.bottom }} />
                    <div className="pointer-events-none absolute bg-black/58" style={{ left: 0, top: cropShadeStyle.top, width: cropShadeStyle.left, bottom: cropShadeStyle.bottom }} />
                    <div className="pointer-events-none absolute bg-black/58" style={{ right: 0, top: cropShadeStyle.top, width: cropShadeStyle.right, bottom: cropShadeStyle.bottom }} />
                    <div
                      className="absolute border border-white shadow-[0_0_0_1px_rgba(34,211,238,0.85)]"
                      style={cropStyle}
                      onPointerDown={(event) => startCropDrag("move", event)}
                    >
                      <div className="pointer-events-none absolute inset-0 grid grid-cols-3 grid-rows-3">
                        {Array.from({ length: 9 }).map((_, index) => (
                          <span key={index} className="border border-white/30" />
                        ))}
                      </div>
                      {CROP_HANDLES.map((handle) => (
                        <button
                          key={handle.id}
                          type="button"
                          aria-label={`调整裁剪 ${handle.id}`}
                          className={`absolute h-4 w-4 rounded-[3px] border border-cyan-950 bg-white shadow-md shadow-black/45 ${handle.className} ${handle.cursor}`}
                          onPointerDown={(event) => startCropDrag(handle.id, event)}
                        />
                      ))}
                    </div>
                  </>
                )}
              </div>
            </div>
          </main>
        </div>
      </div>
    </div>
  )
}
