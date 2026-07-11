import { create } from "zustand"
import {
  addEdge as addReactFlowEdge,
  applyEdgeChanges as applyReactFlowEdgeChanges,
  applyNodeChanges as applyReactFlowNodeChanges,
  MarkerType,
  Position,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from "reactflow"
import {
  nodeReadableText,
  textPreviewFromNode,
} from "@/lib/nodeDisplay"

export type LayoutStrategy = "vertical" | "horizontal" | "grid" | "timeline" | "iteration" | "tree"
export type NodeSurface = "project_panel" | "draft_canvas" | "workflow_runtime"

export interface LayoutHint {
  strategy?: LayoutStrategy
  group_id?: string
  group_label?: string
}

interface CanvasState {
  nodes: Node[]
  edges: Edge[]
  selectedNodeId: string | null
  manualLayout: boolean
  setNodes: (nodes: Node[]) => void
  setEdges: (edges: Edge[]) => void
  addNode: (node: Node, options?: { manual?: boolean }) => void
  updateNode: (id: string, data: Partial<Node["data"]>) => void
  resizeNode: (id: string, width: number, height: number, options?: { persist?: boolean; mode?: "manual" | "auto" }) => void
  applyNodeChanges: (changes: NodeChange[]) => void
  applyEdgeChanges: (changes: EdgeChange[]) => void
  connectNodes: (connection: Connection) => Edge | null
  replaceEdgeId: (oldId: string, newId: string) => void
  removeNodes: (nodeIds: string[]) => void
  removeEdges: (edgeIds: string[]) => void
  selectNode: (id: string | null) => void
  applyCanvasAction: (action: string, payload: Record<string, unknown>) => void
  loadNodes: (
    rawNodes: {
      id: string
      display_id?: number | null
      type: string
      title: string
      status: string
      position_x?: number | null
      position_y?: number | null
      version?: number
      supersedes_id?: string | null
      input?: unknown
      output?: unknown
      position?: { x?: number | null; y?: number | null } | null
      output_json?: string | null
      input_json?: unknown
      model_config_json?: string | null
      prompt?: string | null
      render_state?: string | null
      error_message?: string | null
      surface?: string | null
    }[],
    rawEdges: { id: string; source_node_id: string; target_node_id: string; label?: string | null }[],
    options?: { forceLayout?: boolean; preserveOnEmpty?: boolean; preserveLayout?: boolean },
  ) => void
}

// Professional canvas layout: fixed cards + horizontal dependency lanes.
const CANVAS_ORIGIN_X = 120
const CANVAS_ORIGIN_Y = 90
const NODE_CARD_WIDTH = 260
const NODE_CARD_HEIGHT = 176
const MEDIA_TARGET_AREA = NODE_CARD_WIDTH * NODE_CARD_HEIGHT
const MEDIA_MIN_WIDTH = 128
const MEDIA_MAX_WIDTH = 340
const MEDIA_MIN_HEIGHT = 96
const MEDIA_MAX_HEIGHT = 300
const NODE_MIN_WIDTH = 160
const NODE_MIN_HEIGHT = 110
const NODE_MAX_WIDTH = 900
const NODE_MAX_HEIGHT = 720
const COLUMN_GAP = 170
const ROW_GAP = 46
const NODE_DIMENSIONS_STORAGE_KEY = "openreel.canvas.nodeDimensions.v1"
const NODE_DRAG_HANDLE = ".openreel-smart-node-drag"

interface StoredNodeDimensions {
  width: number
  height: number
  mode?: "manual"
}

const NODE_TIER: Record<string, number> = {
  text: 0,
  image: 1,
  video: 2,
}

function getNodeTier(type: string | undefined): number {
  if (!type) return 99
  if (type in NODE_TIER) return NODE_TIER[type]
  return 99
}

function edgeVisual(edge: Edge): Edge {
  return {
    ...edge,
    type: edge.type || "bezier",
    interactionWidth: edge.interactionWidth ?? 28,
    animated: false,
    markerEnd: edge.markerEnd ?? {
      type: MarkerType.ArrowClosed,
      width: 16,
      height: 16,
      color: "#64748b",
    },
    style: {
      stroke: "#64748b",
      strokeWidth: 1.7,
      ...(edge.style || {}),
    },
  }
}

function dedupeEdges(edges: Edge[], nodeIds?: Set<string>): Edge[] {
  const seen = new Set<string>()
  const next: Edge[] = []
  for (const edge of edges) {
    if (!edge.source || !edge.target || edge.source === edge.target) continue
    if (nodeIds && (!nodeIds.has(edge.source) || !nodeIds.has(edge.target))) continue
    const key = `${edge.source}->${edge.target}`
    if (seen.has(key)) continue
    seen.add(key)
    next.push(edgeVisual({
      ...edge,
      id: edge.id || `e-${edge.source}-${edge.target}`,
      label: undefined,
      labelStyle: undefined,
      labelBgStyle: undefined,
    }))
  }
  return next
}

function rankNodes(nodes: Node[], edges: Edge[]): Map<string, number> {
  const ids = new Set(nodes.map((node) => node.id))
  const incoming = new Map<string, number>()
  const outgoing = new Map<string, string[]>()
  nodes.forEach((node) => {
    incoming.set(node.id, 0)
    outgoing.set(node.id, [])
  })
  for (const edge of edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target)) continue
    outgoing.get(edge.source)?.push(edge.target)
    incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1)
  }

  const rank = new Map<string, number>()
  const queue = nodes
    .filter((node) => (incoming.get(node.id) ?? 0) === 0)
    .sort(nodeSort)
    .map((node) => node.id)
  queue.forEach((id) => rank.set(id, 0))

  while (queue.length) {
    const id = queue.shift()!
    const sourceRank = rank.get(id) ?? 0
    for (const target of outgoing.get(id) ?? []) {
      rank.set(target, Math.max(rank.get(target) ?? 0, sourceRank + 1))
      incoming.set(target, Math.max(0, (incoming.get(target) ?? 0) - 1))
      if ((incoming.get(target) ?? 0) === 0) queue.push(target)
    }
  }

  nodes.forEach((node) => {
    if (!rank.has(node.id)) {
      rank.set(node.id, getNodeTier((node.data as { type?: string })?.type))
    }
  })
  return rank
}

function nodeSort(a: Node, b: Node): number {
  const publicA = nodePublicSortValue(a)
  const publicB = nodePublicSortValue(b)
  if (publicA !== null && publicB !== null && publicA !== publicB) return publicA - publicB
  if (publicA !== null && publicB === null) return -1
  if (publicA === null && publicB !== null) return 1
  const typeDiff = getNodeTier((a.data as { type?: string })?.type) - getNodeTier((b.data as { type?: string })?.type)
  if (typeDiff !== 0) return typeDiff
  const ay = Number.isFinite(a.position?.y) ? a.position.y : 0
  const by = Number.isFinite(b.position?.y) ? b.position.y : 0
  if (ay !== by) return ay - by
  return String((a.data as { title?: string })?.title || a.id).localeCompare(
    String((b.data as { title?: string })?.title || b.id),
    "zh-CN",
  )
}

function nodePublicSortValue(node: Node): number | null {
  const raw = (node.data as { publicId?: unknown })?.publicId
  const value = typeof raw === "number" ? raw : typeof raw === "string" ? Number(raw) : NaN
  return Number.isFinite(value) ? value : null
}

function isAutoSizedMediaType(type: unknown): boolean {
  return type === "image" || type === "video"
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
  if (Number.isFinite(numeric) && numeric > 0) {
    return ratioFromSize(numeric, 1)
  }
  const pair = text.match(/(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)/)
  if (pair) {
    return ratioFromSize(Number(pair[1]), Number(pair[2]))
  }
  const size = text.match(/(\d{2,5})\s*[x×*]\s*(\d{2,5})/)
  if (size) {
    return ratioFromSize(Number(size[1]), Number(size[2]))
  }
  if (/square|正方|方图/.test(text)) return 1
  if (/portrait|vertical|竖/.test(text)) return ratioFromSize(9, 16)
  if (/landscape|horizontal|横/.test(text)) return ratioFromSize(16, 9)
  return null
}

function hasMediaUrl(...values: unknown[]): boolean {
  return values.some((value) => typeof value === "string" && value.trim().length > 0)
}

function ratioFromPreviewObject(preview?: Record<string, unknown>): number | null {
  if (!preview) return null
  return (
    ratioFromSize(numericDimension(preview.width), numericDimension(preview.height)) ||
    ratioFromAspectValue(preview.aspect_ratio) ||
    ratioFromAspectValue(preview.ratio) ||
    ratioFromAspectValue(preview.size) ||
    ratioFromAspectValue(preview.size_requested) ||
    ratioFromAspectValue(preview.size_final) ||
    ratioFromAspectValue(preview.resolution) ||
    ratioFromAspectValue(preview.output_size)
  )
}

function isImageStageName(name: unknown): boolean {
  return /图|首帧|尾帧|模板|参考|image|storyboard/i.test(String(name ?? "")) && !/提示词|prompt/i.test(String(name ?? ""))
}

function mediaStageForNode(preview: Record<string, unknown> | undefined, nodeType: unknown): Record<string, unknown> | undefined {
  if (preview?.type !== "fusion" || !Array.isArray(preview.stages)) return undefined
  for (const stage of preview.stages) {
    if (!stage || typeof stage !== "object" || Array.isArray(stage)) continue
    const item = stage as Record<string, unknown>
    if (!hasMediaUrl(item.local_url, item.url, item.remote_url, item.composite_url)) continue
    if (nodeType === "image" && isImageStageName(item.name)) return item
    if (nodeType === "video" && /视频|video|clip/i.test(String(item.name ?? ""))) return item
  }
  return undefined
}

function hasOutputPreview(preview: Record<string, unknown> | undefined, nodeType: unknown): boolean {
  if (!preview) return false
  if (mediaStageForNode(preview, nodeType)) return true
  if (nodeType === "image") {
    return (
      (preview.type === "image" || preview.type === "image_grid" || preview.type === "storyboard") &&
      hasMediaUrl(preview.local_url, preview.url, preview.remote_url, preview.composite_url)
    )
  }
  if (nodeType === "video") {
    return preview.type === "video" && hasMediaUrl(preview.local_url, preview.url, preview.remote_url)
  }
  return false
}

function hasImageCandidate(preview: unknown): boolean {
  const item = parseObjectJson(preview)
  if (!item) return false
  const stage = mediaStageForNode(item, "image")
  return hasMediaUrl(
    item.local_url,
    item.url,
    item.remote_url,
    item.composite_url,
    item.poster,
    item.thumbnail_url,
    item.last_frame_url,
  ) || (
    stage ? hasMediaUrl(
      stage.local_url,
      stage.url,
      stage.remote_url,
      stage.composite_url,
      stage.poster,
      stage.thumbnail_url,
      stage.last_frame_url,
    ) : false
  )
}

function outputPreviewRatio(preview: Record<string, unknown> | undefined, nodeType: unknown): number | null {
  if (!hasOutputPreview(preview, nodeType)) return null
  const stage = mediaStageForNode(preview, nodeType)
  if (stage) return ratioFromPreviewObject(stage)

  const grid = preview?.grid && typeof preview.grid === "object" && !Array.isArray(preview.grid)
    ? preview.grid as Record<string, unknown>
    : undefined
  const cells = Array.isArray(preview?.cells) ? preview.cells as Record<string, unknown>[] : []
  const cell = cells.find((item) => numericDimension(item.width) && numericDimension(item.height))
  const gridCols = numericDimension(grid?.cols) || 1
  const gridRows = numericDimension(grid?.rows) || 1
  return (
    ratioFromPreviewObject(preview) ||
    (cell ? ratioFromSize((numericDimension(cell.width) || 1) * gridCols, (numericDimension(cell.height) || 1) * gridRows) : null) ||
    (preview?.type === "image_grid" ? ratioFromSize(gridCols, gridRows) : null)
  )
}

function mediaNodeDimensionsFromPreview(
  preview: Record<string, unknown> | undefined,
  nodeType: unknown,
): { width: number; height: number } {
  const ratio = outputPreviewRatio(preview, nodeType)
  if (!ratio) return { width: NODE_CARD_WIDTH, height: NODE_CARD_HEIGHT }

  let width = Math.sqrt(MEDIA_TARGET_AREA * ratio)
  const minWidthForRatio = Math.max(MEDIA_MIN_WIDTH, MEDIA_MIN_HEIGHT * ratio)
  const maxWidthForRatio = Math.min(MEDIA_MAX_WIDTH, MEDIA_MAX_HEIGHT * ratio)
  width = minWidthForRatio <= maxWidthForRatio
    ? Math.min(maxWidthForRatio, Math.max(minWidthForRatio, width))
    : maxWidthForRatio
  return { width: Math.round(width), height: Math.round(width / ratio) }
}

function nodeVisualSize(node: Node): { width: number; height: number } {
  const data = node.data as {
    type?: string
    preview?: Record<string, unknown>
    canvasWidth?: number
    canvasHeight?: number
    canvasSizeMode?: string
  } | undefined
  if (data?.canvasWidth && data.canvasHeight && (!isAutoSizedMediaType(data.type) || data.canvasSizeMode === "manual")) {
    return {
      width: Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, Number(data.canvasWidth) || NODE_CARD_WIDTH)),
      height: Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, Number(data.canvasHeight) || NODE_CARD_HEIGHT)),
    }
  }
  if (isAutoSizedMediaType(data?.type)) {
    return mediaNodeDimensionsFromPreview(data?.preview, data?.type)
  }
  return {
    width: Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, Number(node.width ?? NODE_CARD_WIDTH) || NODE_CARD_WIDTH)),
    height: Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, Number(node.height ?? NODE_CARD_HEIGHT) || NODE_CARD_HEIGHT)),
  }
}

function layoutMindMap(nodes: Node[], edges: Edge[]): Node[] {
  if (nodes.length === 0) return nodes
  const cleanEdges = dedupeEdges(edges, new Set(nodes.map((node) => node.id)))
  const ranks = cleanEdges.length > 0
    ? rankNodes(nodes, cleanEdges)
    : new Map(nodes.map((node) => [node.id, getNodeTier((node.data as { type?: string })?.type)]))
  const columns = new Map<number, Node[]>()
  nodes.forEach((node) => {
    const rank = ranks.get(node.id) ?? 0
    const bucket = columns.get(rank) ?? []
    bucket.push(node)
    columns.set(rank, bucket)
  })

  const orderedColumns = [...columns.keys()]
    .sort((a, b) => a - b)
    .map((rank) => {
      const items = [...(columns.get(rank) ?? [])].sort(nodeSort)
      const sizes = items.map(nodeVisualSize)
      const width = Math.max(...sizes.map((size) => size.width), NODE_CARD_WIDTH)
      const height = sizes.reduce((sum, size) => sum + size.height, 0) + Math.max(0, sizes.length - 1) * ROW_GAP
      return { rank, items, sizes, width, height }
    })
  const maxColumnHeight = Math.max(...orderedColumns.map((column) => column.height), NODE_CARD_HEIGHT)
  const canvasMidY = CANVAS_ORIGIN_Y + maxColumnHeight / 2
  let currentX = CANVAS_ORIGIN_X

  return orderedColumns.flatMap((column) => {
    const x = currentX
    currentX += column.width + COLUMN_GAP
    let currentY = canvasMidY - column.height / 2
    return column.items.map((node, rowIndex) => {
      const size = column.sizes[rowIndex] || { width: NODE_CARD_WIDTH, height: NODE_CARD_HEIGHT }
      const y = currentY
      currentY += size.height + ROW_GAP
      return {
      ...node,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      position: {
        x,
        y,
      },
      }
    })
  })
}

function layoutCanvas(nodes: Node[], edges: Edge[]): { nodes: Node[]; edges: Edge[] } {
  const nodeIds = new Set(nodes.map((node) => node.id))
  const cleanEdges = dedupeEdges(edges, nodeIds)
  return {
    nodes: layoutMindMap(nodes, cleanEdges),
    edges: cleanEdges,
  }
}

function keepManualCanvas(nodes: Node[], edges: Edge[]): { nodes: Node[]; edges: Edge[] } {
  const nodeIds = new Set(nodes.map((node) => node.id))
  return {
    nodes: nodes.map((node) => ({
      ...node,
      sourcePosition: node.sourcePosition ?? Position.Right,
      targetPosition: node.targetPosition ?? Position.Left,
    })),
    edges: dedupeEdges(edges, nodeIds),
  }
}

function expandLocalDependencyScope(seedIds: Set<string>, edges: Edge[], maxHops = 1): Set<string> {
  const scope = new Set(seedIds)
  let frontier = new Set(seedIds)
  for (let depth = 0; depth < maxHops && frontier.size > 0; depth += 1) {
    const next = new Set<string>()
    for (const edge of edges) {
      const touchesSource = frontier.has(edge.source)
      const touchesTarget = frontier.has(edge.target)
      if (!touchesSource && !touchesTarget) continue
      if (!scope.has(edge.source)) {
        scope.add(edge.source)
        next.add(edge.source)
      }
      if (!scope.has(edge.target)) {
        scope.add(edge.target)
        next.add(edge.target)
      }
    }
    frontier = next
  }
  return scope
}

function nodeCenter(node: Node): { x: number; y: number } {
  const size = nodeVisualSize(node)
  return {
    x: node.position.x + size.width / 2,
    y: node.position.y + size.height / 2,
  }
}

function averageNodeCenter(nodes: Node[]): { x: number; y: number } {
  if (nodes.length === 0) return { x: CANVAS_ORIGIN_X, y: CANVAS_ORIGIN_Y }
  const sum = nodes.reduce(
    (acc, node) => {
      const center = nodeCenter(node)
      return { x: acc.x + center.x, y: acc.y + center.y }
    },
    { x: 0, y: 0 },
  )
  return { x: sum.x / nodes.length, y: sum.y / nodes.length }
}

function layoutLocalMindMap(nodes: Node[], edges: Edge[], seedIds: Set<string>): Node[] {
  if (seedIds.size === 0 || nodes.length <= 1) return nodes
  const expandedIds = expandLocalDependencyScope(seedIds, edges, 1)
  const affectedNodes = nodes.filter((node) => expandedIds.has(node.id))
  if (affectedNodes.length <= 1) return nodes

  const affectedIdSet = new Set(affectedNodes.map((node) => node.id))
  const affectedEdges = edges.filter((edge) => affectedIdSet.has(edge.source) && affectedIdSet.has(edge.target))
  if (affectedEdges.length === 0) return nodes

  const previousCenter = averageNodeCenter(affectedNodes)
  const arranged = layoutMindMap(affectedNodes, affectedEdges)
  const arrangedCenter = averageNodeCenter(arranged)
  const dx = previousCenter.x - arrangedCenter.x
  const dy = previousCenter.y - arrangedCenter.y
  const arrangedById = new Map(
    arranged.map((node) => [
      node.id,
      {
        ...node,
        position: {
          x: Math.round(node.position.x + dx),
          y: Math.round(node.position.y + dy),
        },
      },
    ]),
  )

  return nodes.map((node) => arrangedById.get(node.id) ?? node)
}

function arrangeCanvas(nodes: Node[], edges: Edge[], manualLayout: boolean): { nodes: Node[]; edges: Edge[] } {
  return manualLayout ? keepManualCanvas(nodes, edges) : layoutCanvas(nodes, edges)
}

function hasStoredPosition(raw: {
  position?: { x?: number | null; y?: number | null } | null
  position_x?: number | null
  position_y?: number | null
}): boolean {
  const position = rawNodePosition(raw)
  return position !== null
}

function hasNonOriginStoredPosition(raw: {
  position?: { x?: number | null; y?: number | null } | null
  position_x?: number | null
  position_y?: number | null
}): boolean {
  const position = rawNodePosition(raw)
  if (!position) return false
  return Math.abs(position.x) > 0.01 || Math.abs(position.y) > 0.01
}

function storedPositionsAreUsable(rawNodes: {
  position?: { x?: number | null; y?: number | null } | null
  position_x?: number | null
  position_y?: number | null
}[]): boolean {
  if (rawNodes.length <= 1) return rawNodes.some(hasStoredPosition)
  const positioned = rawNodes.filter(hasStoredPosition)
  if (positioned.length !== rawNodes.length) return false
  if (positioned.some(hasNonOriginStoredPosition)) return true
  const buckets = new Set(positioned.map((node) => {
    const position = rawNodePosition(node) ?? { x: 0, y: 0 }
    const x = Math.round(position.x / 40)
    const y = Math.round(position.y / 40)
    return `${x}:${y}`
  }))
  return buckets.size >= Math.ceil(rawNodes.length * 0.7)
}

function payloadPosition(payload: Record<string, unknown>): { x: number; y: number } | null {
  const nested = payload.position && typeof payload.position === "object" && !Array.isArray(payload.position)
    ? payload.position as Record<string, unknown>
    : null
  const x = Number(payload.position_x ?? nested?.x)
  const y = Number(payload.position_y ?? nested?.y)
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  return { x, y }
}

function withCanvasPorts(node: Node): Node {
  return {
    ...node,
    dragHandle: node.dragHandle ?? NODE_DRAG_HANDLE,
    sourcePosition: node.sourcePosition ?? Position.Right,
    targetPosition: node.targetPosition ?? Position.Left,
  }
}

function readStoredNodeDimensions(): Record<string, StoredNodeDimensions> {
  if (typeof window === "undefined") return {}
  try {
    const raw = window.localStorage.getItem(NODE_DIMENSIONS_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, StoredNodeDimensions>
      : {}
  } catch {
    return {}
  }
}

function writeStoredNodeDimensions(updates: Record<string, StoredNodeDimensions>) {
  if (typeof window === "undefined" || Object.keys(updates).length === 0) return
  try {
    const current = readStoredNodeDimensions()
    const normalizedUpdates = Object.fromEntries(
      Object.entries(updates).map(([id, dimensions]) => [
        id,
        { ...dimensions, mode: dimensions.mode ?? "manual" },
      ]),
    )
    window.localStorage.setItem(
      NODE_DIMENSIONS_STORAGE_KEY,
      JSON.stringify({ ...current, ...normalizedUpdates }),
    )
  } catch {
    // localStorage can be unavailable in private mode; canvas editing still works for this session.
  }
}

function hasManualNodeDimensions(node: Node | undefined, storedDimensions: Record<string, StoredNodeDimensions>): boolean {
  if (!node) return false
  const data = node.data as { type?: unknown; canvasSizeMode?: unknown } | undefined
  const stored = storedDimensions[node.id]
  if (isAutoSizedMediaType(data?.type)) {
    return data?.canvasSizeMode === "manual" || stored?.mode === "manual"
  }
  return data?.canvasSizeMode === "manual"
    || Boolean(stored)
}

function clampNodeDimension(width: number, height: number): StoredNodeDimensions {
  return {
    width: Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, Math.round(width))),
    height: Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, Math.round(height))),
  }
}

function applyAutoMediaNodeDimensions(
  node: Node,
  storedDimensions: Record<string, StoredNodeDimensions>,
): Node {
  const data = node.data as {
    type?: string
    preview?: Record<string, unknown>
    canvasWidth?: number
    canvasHeight?: number
    canvasSizeMode?: string
  } | undefined
  if (!isAutoSizedMediaType(data?.type) || hasManualNodeDimensions(node, storedDimensions)) {
    return node
  }
  const size = mediaNodeDimensionsFromPreview(data?.preview, data?.type)
  const nextData = { ...(node.data as Record<string, unknown>) }
  delete nextData.canvasWidth
  delete nextData.canvasHeight
  delete nextData.canvasSizeMode
  return {
    ...node,
    style: { ...node.style, width: size.width, height: size.height },
    data: nextData,
  }
}

function applyStoredNodeDimensions(
  node: Node,
  storedDimensions: Record<string, StoredNodeDimensions>,
): Node {
  const dimensions = storedDimensions[node.id]
  if (!dimensions) return node
  const data = node.data as { type?: unknown } | undefined
  if (isAutoSizedMediaType(data?.type) && dimensions.mode !== "manual") {
    return node
  }
  const width = Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, dimensions.width))
  const height = Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, dimensions.height))
  return {
    ...node,
    style: { ...node.style, width, height },
    data: {
      ...node.data,
      canvasWidth: width,
      canvasHeight: height,
      canvasSizeMode: "manual",
    },
  }
}

function nextManualNodePosition(nodes: Node[]): { x: number; y: number } {
  if (nodes.length === 0) return { x: CANVAS_ORIGIN_X, y: CANVAS_ORIGIN_Y }
  const maxX = Math.max(
    ...nodes.map((node) => (Number.isFinite(node.position?.x) ? node.position.x : CANVAS_ORIGIN_X) + nodeVisualSize(node).width),
  )
  const lane = nodes.length % 4
  return {
    x: maxX + COLUMN_GAP,
    y: CANVAS_ORIGIN_Y + lane * (NODE_CARD_HEIGHT + ROW_GAP),
  }
}

function edgeFromConnection(connection: Connection): Edge | null {
  if (!connection.source || !connection.target || connection.source === connection.target) return null
  return {
    id: `manual-${connection.source}-${connection.target}`,
    source: connection.source,
    target: connection.target,
    sourceHandle: connection.sourceHandle,
    targetHandle: connection.targetHandle,
  }
}

function parseObjectJson(raw: unknown): Record<string, unknown> | undefined {
  if (!raw) return undefined
  if (typeof raw === "object") return raw as Record<string, unknown>
  if (typeof raw !== "string") return undefined
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === "object" ? parsed as Record<string, unknown> : undefined
  } catch {
    return undefined
  }
}

function rawNodeInput(raw: { input?: unknown; input_json?: unknown }): Record<string, unknown> | undefined {
  return parseObjectJson(raw.input ?? raw.input_json)
}

function rawNodeOutput(raw: { output?: unknown; output_json?: string | null }): unknown {
  if (raw.output !== undefined) return raw.output
  if (!raw.output_json) return undefined
  return parseJsonValue(raw.output_json) ?? raw.output_json
}

function rawNodePosition(raw: {
  position?: { x?: number | null; y?: number | null } | null
  position_x?: number | null
  position_y?: number | null
}): { x: number; y: number } | null {
  const x = Number(raw.position?.x ?? raw.position_x)
  const y = Number(raw.position?.y ?? raw.position_y)
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  return { x, y }
}

function firstText(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  return undefined
}

function parseJsonValue(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return undefined
  }
}

function stringList(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const items = value
    .map((item) => String(item ?? "").trim())
    .filter(Boolean)
  return items.length ? Array.from(new Set(items)) : undefined
}

function workflowReferenceList(value: unknown): Array<Record<string, string>> | undefined {
  if (!Array.isArray(value)) return undefined
  const refs: Array<Record<string, string>> = []
  for (const item of value) {
    if ((typeof item === "string" && item.trim()) || typeof item === "number" || typeof item === "boolean") {
      refs.push({ ref: String(item).trim(), role: "context" })
      continue
    }
    const obj = parseObjectJson(item)
    const rawRef = obj?.ref ?? obj?.reference ?? obj?.value ?? obj?.node_id ?? obj?.nodeId
    const ref = typeof rawRef === "string"
      ? rawRef.trim()
      : typeof rawRef === "number" || typeof rawRef === "boolean"
      ? String(rawRef)
      : ""
    if (!ref) continue
    const role = typeof obj?.role === "string" && obj.role.trim() ? obj.role.trim() : "context"
    refs.push({ ref, role })
  }
  return refs.length ? refs : undefined
}

function fieldsFromInput(input: Record<string, unknown> | undefined): Record<string, unknown> | undefined {
  if (!input) return undefined
  return parseObjectJson(input.fields) ?? input
}

function workflowMetadataFromInput(input: Record<string, unknown> | undefined): Record<string, unknown> {
  if (!input) return {}
  const fields = fieldsFromInput(input)
  const workflow = parseObjectJson(fields?.workflow ?? input.workflow)
  const refs = workflowReferenceList(fields?.references ?? input.references)
  const dependsOn = stringList(fields?.depends_on ?? input.depends_on ?? workflow?.depends_on)
  const prompt = firstText(
    fields?.prompt,
    fields?.content,
    fields?.description,
    input.prompt,
    input.content,
    input.description,
    workflow?.acceptance,
  )
  const result: Record<string, unknown> = { input }
  if (workflow) result.workflow = workflow
  if (prompt) result.workflowStepPrompt = prompt
  if (refs) result.workflowReferences = refs
  if (dependsOn) result.workflowDependsOn = dependsOn
  return result
}

function workflowMetadataFromPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const explicitInput = parseObjectJson(payload.input_json ?? payload.input)
  if (explicitInput) return workflowMetadataFromInput(explicitInput)
  if (payload.workflow || payload.references || payload.depends_on || payload.fields) {
    return workflowMetadataFromInput(payload)
  }
  return {}
}

function normalizeSurface(raw: unknown): NodeSurface | undefined {
  return raw === "draft_canvas" || raw === "project_panel" || raw === "workflow_runtime" ? raw : undefined
}

function surfaceFromRawNode(raw: {
  surface?: string | null
  input?: unknown
  input_json?: unknown
  model_config_json?: string | null
}): NodeSurface {
  const direct = normalizeSurface(raw.surface)
  if (direct) return direct

  const modelConfig = parseObjectJson(raw.model_config_json)
  const fromModel = normalizeSurface(modelConfig?.surface ?? modelConfig?._surface)
  if (fromModel) return fromModel

  const input = rawNodeInput(raw)
  const fromInput = normalizeSurface(input?.surface ?? input?._surface)
  if (fromInput) return fromInput

  return "project_panel"
}

export function getCanvasNodeSurface(node: Node): NodeSurface {
  const data = node.data as { surface?: unknown }
  return normalizeSurface(data?.surface) ?? "project_panel"
}

function normalizedPrompt(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  const text = value.trim()
  return text ? text : undefined
}

function errorMessageFromUnknown(value: unknown): string | undefined {
  const obj = parseObjectJson(value)
  if (!obj) return undefined
  const parts: string[] = []
  for (const key of ["error_message", "error", "provider_msg", "message", "detail"]) {
    const text = normalizedPrompt(obj[key])
    if (text) parts.push(text)
  }
  const detail = parseObjectJson(obj.error_detail)
  if (detail) {
    for (const key of ["error", "provider_msg", "error_kind", "endpoint"]) {
      const text = normalizedPrompt(detail[key])
      if (text) parts.push(text)
    }
  }
  const feedback = parseObjectJson(obj.model_feedback)
  if (feedback) {
    for (const key of ["what_went_wrong", "how_to_fix", "retry_policy"]) {
      const text = normalizedPrompt(feedback[key])
      if (text) parts.push(text)
    }
  }
  const result = parseObjectJson(obj.result)
  const resultError = result ? errorMessageFromUnknown(result) : undefined
  if (resultError) parts.push(resultError)
  if (Array.isArray(obj.stages)) {
    for (const stage of obj.stages) {
      const item = parseObjectJson(stage)
      const text = item?.status === "failed" ? errorMessageFromUnknown(item) : undefined
      if (text) parts.push(text)
    }
  }
  return Array.from(new Set(parts)).join("\n") || undefined
}

function numericDimension(value: unknown): number | undefined {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? Math.round(n) : undefined
}

function mediaPreviewHints(data: Record<string, unknown>): Record<string, unknown> {
  const hints: Record<string, unknown> = {}
  const width = numericDimension(data.width)
  const height = numericDimension(data.height)
  if (width) hints.width = width
  if (height) hints.height = height
  for (const key of ["aspect_ratio", "ratio", "size", "size_requested", "size_final", "resolution", "output_size"]) {
    if (data[key] != null) hints[key] = data[key]
  }
  for (const key of ["panorama", "is_panorama", "projection", "panorama_capture", "capture_mode"]) {
    if (data[key] != null) hints[key] = data[key]
  }
  return hints
}

function collectMediaPreviewHints(...sources: unknown[]): Record<string, unknown> {
  const hints: Record<string, unknown> = {}
  for (const source of sources) {
    const item = parseObjectJson(source)
    if (!item) continue
    const fields = parseObjectJson(item.fields)
    if (fields) Object.assign(hints, mediaPreviewHints(fields))
    const input = parseObjectJson(item.input)
    if (input) Object.assign(hints, mediaPreviewHints(input))
    const inputJson = parseObjectJson(item.input_json)
    if (inputJson) Object.assign(hints, mediaPreviewHints(inputJson))
    Object.assign(hints, mediaPreviewHints(item))
  }
  return hints
}

function mergeMediaPreviewHints(
  nodeType: unknown,
  preview: Record<string, unknown> | undefined,
  ...sources: unknown[]
): Record<string, unknown> | undefined {
  if (!isAutoSizedMediaType(nodeType)) return preview
  const hints = collectMediaPreviewHints(...sources)
  if (Object.keys(hints).length === 0) return preview
  const base: Record<string, unknown> = {
    type: nodeType === "video" ? "video_prompt" : "image_prompt",
    ...(preview ?? {}),
  }
  for (const [key, value] of Object.entries(hints)) {
    if (base[key] == null) base[key] = value
  }
  return base
}

function existingNodeDimension(node: Node, key: "width" | "height"): number | undefined {
  const data = node.data as { canvasWidth?: unknown; canvasHeight?: unknown } | undefined
  const dataValue = key === "width" ? data?.canvasWidth : data?.canvasHeight
  const styleValue = node.style ? (node.style as Record<string, unknown>)[key] : undefined
  return numericDimension(dataValue) ?? numericDimension(styleValue) ?? numericDimension(node[key])
}

function preserveExistingNodeLayout(
  next: Node,
  existing: Node | undefined,
  storedDimensions: Record<string, StoredNodeDimensions> = {},
): Node {
  if (!existing) return next
  const nextData = next.data as { type?: string } | undefined
  const preserveDimensions =
    !isAutoSizedMediaType(nextData?.type) ||
    hasManualNodeDimensions(next, storedDimensions) ||
    hasManualNodeDimensions(existing, storedDimensions)
  const width = preserveDimensions ? existingNodeDimension(existing, "width") : undefined
  const height = preserveDimensions ? existingNodeDimension(existing, "height") : undefined
  return {
    ...next,
    position: existing.position,
    selected: existing.selected ?? next.selected,
    width: existing.width ?? next.width,
    height: existing.height ?? next.height,
    style: {
      ...next.style,
      ...(width ? { width } : {}),
      ...(height ? { height } : {}),
    },
    data: {
      ...next.data,
      ...(width ? { canvasWidth: width } : {}),
      ...(height ? { canvasHeight: height } : {}),
      ...(width && height ? { canvasSizeMode: "manual" } : {}),
    },
  }
}

function promptFromRawNode(raw: { prompt?: string | null; input?: unknown; input_json?: unknown }): string | undefined {
  const direct = normalizedPrompt(raw.prompt)
  if (direct) return direct
  const input = rawNodeInput(raw)
  return normalizedPrompt(input?.prompt)
}

function normalizeRenderState(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  const text = value.trim()
  if (!text) return undefined
  if (["stale", "dirty", "outdated", "needs_render", "未更新"].includes(text)) return "stale"
  if (["fresh", "current", "latest", "最新"].includes(text)) return "fresh"
  return text
}

function renderStateFromRawNode(raw: {
  type?: string | null
  status?: string | null
  input?: unknown
  output?: unknown
  output_json?: string | null
  input_json?: unknown
  render_state?: string | null
}): string | undefined {
  if (raw.type !== "image") return undefined
  const direct = normalizeRenderState(raw.render_state)
  if (direct) return direct
  const input = rawNodeInput(raw)
  const fromInput = normalizeRenderState(input?.render_state)
  if (fromInput) return fromInput
  return raw.status === "completed" && rawNodeOutput(raw) ? "fresh" : undefined
}

function renderStateFromPayload(
  payload: Record<string, unknown>,
  nodeType: unknown,
  status: unknown,
  preview: unknown,
): string | undefined {
  if (String(nodeType ?? "") !== "image") return undefined
  const direct = normalizeRenderState(payload.render_state ?? payload.renderState)
  if (direct) return direct
  const input = parseObjectJson(payload.input_json ?? payload.input)
  const fromInput = normalizeRenderState(input?.render_state)
  if (fromInput) return fromInput
  return status === "completed" && preview ? "fresh" : undefined
}

function promptPreviewFromNodeType(nodeType: unknown, prompt: unknown): Record<string, unknown> | undefined {
  const currentPrompt = normalizedPrompt(prompt)
  if (!currentPrompt) return undefined
  const type = String(nodeType ?? "")
  if (type !== "image" && type !== "video" && type !== "audio") return undefined
  return {
    type: type === "video" ? "video_prompt" : type === "audio" ? "audio_prompt" : "image_prompt",
    prompt: currentPrompt,
  }
}

function applyCurrentPromptToPreview(
  preview: unknown,
  prompt: unknown,
): Record<string, unknown> | undefined {
  if (!preview || typeof preview !== "object" || Array.isArray(preview)) return undefined
  const currentPrompt = normalizedPrompt(prompt)
  const data = preview as Record<string, unknown>
  if (!currentPrompt) return data

  if (data.type === "fusion" && Array.isArray(data.stages)) {
    let changed = false
    const stages = data.stages.map((stage) => {
      if (!stage || typeof stage !== "object" || Array.isArray(stage)) return stage
      const item = stage as Record<string, unknown>
      const name = String(item.name ?? "")
      if (/提示词|prompt/i.test(name)) {
        changed = true
        return { ...item, prompt: currentPrompt }
      }
      return item
    })
    return changed ? { ...data, stages } : data
  }

  if (data.type === "image_prompt" || data.type === "video_prompt" || data.type === "audio_prompt") {
    return { ...data, prompt: currentPrompt }
  }

  return data
}

function normalizeFusionPreviewErrors(preview: Record<string, unknown>): Record<string, unknown> {
  if (preview.type !== "fusion" || !Array.isArray(preview.stages)) return preview
  return {
    ...preview,
    stages: preview.stages.map((stage) => {
      if (!stage || typeof stage !== "object" || Array.isArray(stage)) return stage
      const item = { ...(stage as Record<string, unknown>) }
      if (item.status === "completed" || item.status === "running") {
        delete item.error
        delete item.diagnostics
      }
      return item
    }),
  }
}

function normalizePreviewForNode(
  nodeType: unknown,
  preview: unknown,
  prompt: unknown,
  input?: unknown,
  output?: unknown,
): Record<string, unknown> | undefined {
  const normalized = applyCurrentPromptToPreview(preview, prompt) ?? promptPreviewFromNodeType(nodeType, prompt)
  if (normalized) return normalizeFusionPreviewErrors(normalized)
  if (String(nodeType ?? "") === "text") return textPreviewFromNode({ type: "text", input, output, prompt: normalizedPrompt(prompt) })
  return undefined
}

function previewTextForNodeData(
  nodeType: unknown,
  input: unknown,
  output: unknown,
  prompt: unknown,
  fallback?: unknown,
): string | undefined {
  const text = nodeReadableText({
    type: String(nodeType ?? ""),
    input,
    output,
    prompt: normalizedPrompt(prompt),
  })
  return text || normalizedPrompt(fallback)
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function parseOutputPreview(outputJson: unknown, nodeType?: unknown): Record<string, unknown> | undefined {
  if (!outputJson) return undefined
  try {
    const data = typeof outputJson === "string" ? JSON.parse(outputJson) : outputJson
    if (!data || typeof data !== "object") return undefined
    if (String(nodeType ?? "") === "text") {
      const preview = textPreviewFromNode({ type: "text", output: data })
      if (preview) return preview
    }
    // Fusion node — multi-stage payload (prompt + image + ...)
    if (data.type === "fusion" && Array.isArray(data.stages)) {
      return normalizeFusionPreviewErrors({
        type: "fusion",
        subject: data.subject,
        stages: data.stages,
      })
    }
    // Characters
    if (Array.isArray(data.characters)) {
      return {
        type: "characters",
        items: data.characters.slice(0, 6).map((c: Record<string, unknown>) => ({
          name: c.name || "", role_type: c.role_type || "", identity: c.identity || "",
        })),
      }
    }
    if (data.character && typeof data.character === "object") {
      const c = data.character as Record<string, unknown>
      return { type: "character", name: c.name, role_type: c.role_type, identity: c.identity, traits: (c.traits as string[] || []).slice(0, 3) }
    }
    // Outline
    if (data.outline && typeof data.outline === "object") {
      const o = data.outline as Record<string, unknown>
      const eps = Array.isArray(o.episodes) ? o.episodes : []
      return {
        type: "outline", episode_count: eps.length,
        episodes: eps.slice(0, 5).map((ep: Record<string, unknown>, i: number) => ({ num: ep.episode_number || i + 1, title: ep.title || "" })),
      }
    }
    // Script
    if (data.script && typeof data.script === "object") {
      const s = data.script as Record<string, unknown>
      return { type: "script", summary: ((s.summary as string) || "").slice(0, 120), scene_count: Array.isArray(s.scenes) ? s.scenes.length : undefined }
    }
    // Review
    if (data.review && typeof data.review === "object") {
      const r = data.review as Record<string, unknown>
      return { type: "review", score: r.score, summary: ((r.summary as string) || "").slice(0, 120) }
    }
    // Storyboard with grid image / shot list
    if (data.type === "storyboard" || (Array.isArray(data.shots) && data.mode)) {
      return {
        type: "storyboard",
        mode: data.mode,
        shot_count: data.shot_count,
        shots: data.shots,
        url: data.url,
        local_url: data.local_url,
        remote_url: data.remote_url,
        ...mediaPreviewHints(data),
      }
    }
    // Storyboard with grid image / shot list
    if (data.type === "storyboard" || (Array.isArray(data.shots) && data.mode)) {
      return {
        type: "storyboard",
        mode: data.mode,
        shot_count: data.shot_count,
        shots: data.shots,
        url: data.url,
        local_url: data.local_url,
        remote_url: data.remote_url,
        ...mediaPreviewHints(data),
      }
    }
    // Standalone image preview from node/media service output
    if (data.type === "image" && (data.url || data.local_url)) {
      return {
        type: "image",
        url: data.url as string | undefined,
        local_url: data.local_url as string | undefined,
        remote_url: data.remote_url as string | undefined,
        ...mediaPreviewHints(data),
      }
    }
    if (data.type === "video" && (data.url || data.local_url || data.remote_url)) {
      return {
        type: "video",
        url: data.url as string | undefined,
        local_url: data.local_url as string | undefined,
        remote_url: data.remote_url as string | undefined,
        poster: data.poster as string | undefined,
        thumbnail_url: data.thumbnail_url as string | undefined,
        status: data.status,
        progress: data.progress,
        poll_status: data.poll_status,
        poll_count: data.poll_count,
        ...mediaPreviewHints(data),
      }
    }
    if (
      data.type === "video" &&
      (data.status === "queued" || data.status === "running" || data.progress != null || data.poll_status != null)
    ) {
      return {
        type: "video_prompt",
        prompt: typeof data.prompt === "string" ? data.prompt : undefined,
        status: data.status,
        progress: data.progress,
        poll_status: data.poll_status,
        poll_count: data.poll_count,
        ...mediaPreviewHints(data),
      }
    }
    if (data.type === "audio" && (data.url || data.local_url || data.remote_url)) {
      return {
        type: "audio",
        url: data.url as string | undefined,
        local_url: data.local_url as string | undefined,
        remote_url: data.remote_url as string | undefined,
        format: data.format,
        duration_seconds: data.duration_seconds,
        status: data.status,
        progress: data.progress,
        poll_status: data.poll_status,
        poll_count: data.poll_count,
      }
    }
    if (
      data.type === "audio" &&
      (data.status === "queued" || data.status === "running" || data.progress != null || data.poll_status != null)
    ) {
      return {
        type: "audio_prompt",
        prompt: typeof data.prompt === "string" ? data.prompt : undefined,
        status: data.status,
        progress: data.progress,
        poll_status: data.poll_status,
        poll_count: data.poll_count,
      }
    }
    if (data.type === "image_grid" && (data.local_url || data.url || data.composite_url)) {
      return {
        type: "image_grid",
        grid: data.grid,
        cells: data.cells,
        url: (data.url || data.composite_url) as string | undefined,
        local_url: (data.local_url || data.composite_url) as string | undefined,
        composite_url: data.composite_url as string | undefined,
        ...mediaPreviewHints(data),
      }
    }
    // Nested image: { image: {url, local_url, ...} }
    const img = data.image as Record<string, unknown> | undefined
    if (img && typeof img === "object" && (img.url || img.local_url)) {
      return {
        type: "image",
        url: img.url as string | undefined,
        local_url: img.local_url as string | undefined,
        remote_url: img.remote_url as string | undefined,
        ...mediaPreviewHints(img),
      }
    }
    // Nested video: { video: {url, local_url, ...} }
    const video = data.video as Record<string, unknown> | undefined
    if (video && typeof video === "object" && (video.url || video.local_url || video.remote_url)) {
      return {
        type: "video",
        url: video.url as string | undefined,
        local_url: video.local_url as string | undefined,
        remote_url: video.remote_url as string | undefined,
        poster: video.poster as string | undefined,
        thumbnail_url: video.thumbnail_url as string | undefined,
        status: video.status,
        progress: video.progress,
        poll_status: video.poll_status,
        poll_count: video.poll_count,
        ...mediaPreviewHints(video),
      }
    }
    const audio = data.audio as Record<string, unknown> | undefined
    if (audio && typeof audio === "object" && (audio.url || audio.local_url || audio.remote_url)) {
      return {
        type: "audio",
        url: audio.url as string | undefined,
        local_url: audio.local_url as string | undefined,
        remote_url: audio.remote_url as string | undefined,
        format: audio.format,
        duration_seconds: audio.duration_seconds,
        status: audio.status,
        progress: audio.progress,
        poll_status: audio.poll_status,
        poll_count: audio.poll_count,
      }
    }
    // Bare {url|local_url} pointing at an image extension
    const bareUrl = (data.local_url || data.url) as string | undefined
    if (typeof bareUrl === "string" && /\.(png|jpe?g|webp|gif|bmp|svg)$/i.test(bareUrl)) {
      return {
        type: "image",
        url: data.url as string | undefined,
        local_url: data.local_url as string | undefined,
        remote_url: data.remote_url as string | undefined,
        ...mediaPreviewHints(data),
      }
    }
    if (typeof bareUrl === "string" && /\.(mp4|webm|mov)(\?|$)/i.test(bareUrl)) {
      return {
        type: "video",
        url: data.url as string | undefined,
        local_url: data.local_url as string | undefined,
        remote_url: data.remote_url as string | undefined,
        poster: data.poster as string | undefined,
        thumbnail_url: data.thumbnail_url as string | undefined,
        ...mediaPreviewHints(data),
      }
    }
    if (typeof bareUrl === "string" && /\.(mp3|wav|m4a|aac|ogg|flac)(\?|$)/i.test(bareUrl)) {
      return {
        type: "audio",
        url: data.url as string | undefined,
        local_url: data.local_url as string | undefined,
        remote_url: data.remote_url as string | undefined,
        format: data.format,
        duration_seconds: data.duration_seconds,
      }
    }
    return undefined
  } catch {
    return undefined
  }
}

export const useCanvasStore = create<CanvasState>((set, get) => ({
  nodes: [],
  edges: [],
  selectedNodeId: null,
  manualLayout: false,
  setNodes: (nodes) => set((s) => arrangeCanvas(nodes, s.edges, s.manualLayout)),
  setEdges: (edges) => set((s) => arrangeCanvas(s.nodes, edges, s.manualLayout)),
  addNode: (node, options) => set((s) => {
    const manualLayout = options?.manual ?? s.manualLayout
    const nextNode = withCanvasPorts(
      manualLayout
        ? { ...node, position: node.position ?? nextManualNodePosition(s.nodes) }
        : node,
    )
      return {
        ...arrangeCanvas([...s.nodes, nextNode], s.edges, manualLayout),
        manualLayout,
      }
  }),
	  updateNode: (id, data) => {
    const storedDimensions = readStoredNodeDimensions()
	    set((s) => {
	      const nodes = s.nodes.map((n) => {
        if (n.id !== id) return n
        const nextData = { ...n.data, ...data, ...workflowMetadataFromPayload(data as Record<string, unknown>) }
        const nextRenderState = renderStateFromPayload(
          data as Record<string, unknown>,
          nextData.type ?? n.type,
          nextData.status,
          nextData.preview,
        )
        if (nextRenderState) nextData.renderState = nextRenderState
        nextData.preview = mergeMediaPreviewHints(
          nextData.type ?? n.type,
          normalizePreviewForNode(nextData.type ?? n.type, nextData.preview, nextData.prompt, nextData.input, nextData.output),
          data as Record<string, unknown>,
          nextData,
        )
        nextData.previewText = previewTextForNodeData(
          nextData.type ?? n.type,
          nextData.input,
          nextData.output,
          nextData.prompt,
          nextData.previewText,
        )
        return applyAutoMediaNodeDimensions({ ...n, data: nextData }, storedDimensions)
      })
	      return {
	        ...keepManualCanvas(nodes, s.edges),
	        manualLayout: s.manualLayout,
	      }
	    })
  },
  resizeNode: (id, width, height, options) => {
    const dimensions = clampNodeDimension(width, height)
    const mode = options?.mode ?? "manual"
    if (mode === "manual" && options?.persist) {
      writeStoredNodeDimensions({ [id]: dimensions })
    }
    set((s) => ({
      ...keepManualCanvas(
        s.nodes.map((node) => {
          if (node.id !== id) return node
          return {
            ...node,
            style: { ...node.style, width: dimensions.width, height: dimensions.height },
            data: mode === "manual"
              ? {
                  ...node.data,
                  canvasWidth: dimensions.width,
                  canvasHeight: dimensions.height,
                  canvasSizeMode: "manual",
                }
              : (() => {
                  const nextData = { ...(node.data as Record<string, unknown>) }
                  delete nextData.canvasWidth
                  delete nextData.canvasHeight
                  delete nextData.canvasSizeMode
                  return nextData
                })(),
          }
        }),
        s.edges,
      ),
      manualLayout: s.manualLayout,
    }))
  },
  applyNodeChanges: (changes) => {
    set((s) => ({
      ...keepManualCanvas(
        applyReactFlowNodeChanges(changes, s.nodes),
        s.edges,
      ),
      manualLayout: s.manualLayout || changes.some((change) => change.type === "position"),
    }))
  },
  applyEdgeChanges: (changes) =>
    set((s) => ({
      ...keepManualCanvas(s.nodes, applyReactFlowEdgeChanges(changes, s.edges)),
      manualLayout: s.manualLayout || changes.some((change) => change.type !== "select"),
    })),
  connectNodes: (connection) => {
    const edge = edgeFromConnection(connection)
    if (!edge) return null
    set((s) => ({
      ...keepManualCanvas(s.nodes, addReactFlowEdge(edge, s.edges)),
      manualLayout: true,
    }))
    return edge
  },
  replaceEdgeId: (oldId, newId) =>
    set((s) => ({
      ...keepManualCanvas(
        s.nodes,
        s.edges.map((edge) => edge.id === oldId ? { ...edge, id: newId } : edge),
      ),
      manualLayout: true,
    })),
  removeNodes: (nodeIds) =>
    set((s) => {
      const ids = new Set(nodeIds)
      return {
        ...keepManualCanvas(
          s.nodes.filter((node) => !ids.has(node.id)),
          s.edges.filter((edge) => !ids.has(edge.source) && !ids.has(edge.target)),
        ),
        selectedNodeId: s.selectedNodeId && ids.has(s.selectedNodeId) ? null : s.selectedNodeId,
        manualLayout: true,
      }
    }),
  removeEdges: (edgeIds) =>
    set((s) => ({
      ...keepManualCanvas(
        s.nodes,
        s.edges.filter((edge) => !edgeIds.includes(edge.id)),
      ),
      manualLayout: true,
    })),
  selectNode: (id) =>
    set((s) => ({
      selectedNodeId: id,
      nodes: s.nodes.map((node) => (
        node.selected === (id === node.id) ? node : { ...node, selected: id === node.id }
      )),
    })),
  applyCanvasAction: (action, payload) => {
    if (action === "clear_all") {
      console.warn("[openreel:canvas clear_all]", { payload, previousNodes: get().nodes.length })
      set({ nodes: [], edges: [], manualLayout: false })
      return
    }
    if (action === "delete_node") {
      const id = String(payload.id ?? "")
      if (!id) return
      set((s) => arrangeCanvas(
        s.nodes.filter((n) => n.id !== id),
        s.edges.filter((e) => e.source !== id && e.target !== id),
        s.manualLayout,
      ))
      return
    }
    if (action === "add_edge") {
      const id = String(payload.id ?? "")
      const source = String(payload.source_node_id ?? payload.source ?? "")
      const target = String(payload.target_node_id ?? payload.target ?? "")
      if (!id || !source || !target) return
      const newEdge: Edge = {
        id,
        source,
        target,
      }
      set((s) => {
        const edges = s.edges.some((edge) => edge.id === id || (edge.source === source && edge.target === target))
          ? s.edges
          : [...s.edges, newEdge]
        return {
          ...keepManualCanvas(
            layoutLocalMindMap(s.nodes, edges, new Set([source, target])),
            edges,
          ),
          manualLayout: s.manualLayout,
        }
      })
      return
    }
    if (action === "delete_edge" || action === "remove_edge") {
      const id = String(payload.id ?? "")
      const source = String(payload.source_node_id ?? payload.source ?? "")
      const target = String(payload.target_node_id ?? payload.target ?? "")
      set((s) => ({
        ...keepManualCanvas(
          s.nodes,
          s.edges.filter((edge) => {
            if (id && edge.id === id) return false
            return !(source && target && edge.source === source && edge.target === target)
          }),
        ),
        manualLayout: s.manualLayout,
      }))
      return
    }
    if (action === "create_node") {
      const id = String(payload.id ?? "")
      if (!id) return

      const { nodes, edges } = get()
      const storedDimensions = readStoredNodeDimensions()
      if (nodes.some((n) => n.id === id)) {
        set((s) => arrangeCanvas(
          s.nodes.map((n) => {
            if (n.id !== id) return n
            const hasPayloadOutput = Object.prototype.hasOwnProperty.call(payload, "output")
            const nextOutput = hasPayloadOutput ? payload.output : n.data.output
            return applyAutoMediaNodeDimensions({
              ...n,
              data: {
                ...n.data,
                ...workflowMetadataFromPayload(payload as Record<string, unknown>),
                type: payload.type ?? n.data.type,
                publicId: payload.display_id ?? payload._canvas_display_id ?? n.data.publicId,
                title: payload.title ?? n.data.title,
                status: payload.status ?? n.data.status,
                surface: payload.surface ?? n.data.surface,
                prompt: payload.prompt ?? n.data.prompt,
                renderState: renderStateFromPayload(
                  payload as Record<string, unknown>,
                  payload.type ?? n.data.type,
                  payload.status ?? n.data.status,
                  payload.preview ?? n.data.preview,
                ) ?? n.data.renderState,
                preview: mergeMediaPreviewHints(
                  payload.type ?? n.data.type,
                  normalizePreviewForNode(
                    payload.type ?? n.data.type,
                    payload.preview ?? n.data.preview,
                    payload.prompt ?? n.data.prompt,
                    payload.input_json ?? payload.input ?? payload,
                    nextOutput,
                  ),
                  payload,
                  n.data,
                ),
                input: payload.input_json ?? payload.input ?? n.data.input,
                output: nextOutput,
                workflowRuntimeOutput: hasPayloadOutput ? nextOutput : n.data.workflowRuntimeOutput,
                previewText: previewTextForNodeData(
                  payload.type ?? n.data.type,
                  payload.input_json ?? payload.input ?? n.data.input,
                  nextOutput,
                  payload.prompt ?? n.data.prompt,
                  n.data.previewText,
                ),
              },
            }, storedDimensions)
          }),
          s.edges,
          s.manualLayout,
        ))
        return
      }
      const hint = (payload.layout as LayoutHint | undefined) ?? undefined
      const supersedesId = payload.supersedes_id
        ? String(payload.supersedes_id)
        : undefined
      const surface = normalizeSurface(payload.surface) ?? "project_panel"

      let groupId: string | undefined
      let extraEdges: Edge[] = []
      let mutatedNodes = nodes

      if (supersedesId) {
        const prev = nodes.find((n) => n.id === supersedesId)
        if (prev) {
          groupId = (prev.data as { group_id?: string })?.group_id
          mutatedNodes = nodes.map((n) =>
            n.id === supersedesId
              ? { ...n, data: { ...n.data, superseded: true } }
              : n,
          )
          extraEdges = [
            {
              id: `e-iter-${supersedesId}-${id}`,
              source: supersedesId,
              target: id,
              type: "bezier",
              style: {
                stroke: "#94a3b8",
                strokeWidth: 1.7,
                strokeDasharray: "6 4",
              },
            },
          ]
        }
      }
      if (!groupId) groupId = hint?.group_id

	      const explicitPosition = payloadPosition(payload)
	      const preserveExistingLayout = mutatedNodes.length > 0 && !explicitPosition
	      const newNode: Node = applyAutoMediaNodeDimensions({
        id,
        type: String(payload.type ?? "default"),
        dragHandle: NODE_DRAG_HANDLE,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
	        position: explicitPosition ?? (preserveExistingLayout || get().manualLayout ? nextManualNodePosition(mutatedNodes) : { x: CANVAS_ORIGIN_X, y: CANVAS_ORIGIN_Y }),
        data: {
          nodeId: id,
          publicId: payload.display_id ?? payload._canvas_display_id,
          type: payload.type,
          title: payload.title,
          status: payload.status ?? "running",
          prompt: normalizedPrompt(payload.prompt),
          input: payload.input_json ?? payload.input,
          output: payload.output,
          workflowRuntimeOutput: payload.output,
          ...workflowMetadataFromPayload(payload as Record<string, unknown>),
          renderState: renderStateFromPayload(payload as Record<string, unknown>, payload.type, payload.status, payload.preview),
          preview: mergeMediaPreviewHints(
            payload.type,
            normalizePreviewForNode(payload.type, payload.preview, payload.prompt, payload.input_json ?? payload.input ?? payload, payload.output),
            payload,
          ),
          previewText: previewTextForNodeData(
            payload.type,
            payload.input_json ?? payload.input,
            payload.output,
            payload.prompt,
          ),
          group_id: groupId,
          group_label: hint?.group_label,
          layout_strategy: hint?.strategy ?? "vertical",
          surface,
          version: payload.version ?? 1,
          supersedes_id: supersedesId,
          createdAt: new Date().toISOString(),
        },
      }, storedDimensions)

	      set((s) => {
	        const nextNodes = [...mutatedNodes, newNode]
	        const nextEdges = [...edges, ...extraEdges]
	        if (preserveExistingLayout || s.manualLayout || explicitPosition) {
	          return {
	            ...keepManualCanvas(nextNodes, nextEdges),
	            manualLayout: s.manualLayout || Boolean(explicitPosition),
	          }
	        }
	        return arrangeCanvas(nextNodes, nextEdges, false)
	      })
    } else if (action === "update_node") {
      const id = String(payload.id ?? "")
      if (!id) return
      const storedDimensions = readStoredNodeDimensions()
      set((s) => {
        const nodes = s.nodes.map((n) => {
          if (n.id !== id) return n
          // 保持 fusion 阶段结构稳定：单张图片更新只写入对应图阶段，不整段替换 stages。
          const prevData = n.data as {
            status?: string
            prompt?: string | null
            error_message?: string | null
            preview?: {
              type?: string
              stages?: Array<{
                name?: string
                status?: string
                prompt?: string
                url?: string
                local_url?: string
                remote_url?: string
                width?: unknown
                height?: unknown
                aspect_ratio?: unknown
                ratio?: unknown
                size?: unknown
                size_requested?: unknown
                size_final?: unknown
                resolution?: unknown
                output_size?: unknown
                error?: string
                diagnostics?: unknown
              }>
            }
          }
          const nextPayload = payload as {
            preview?: {
              type?: string
              status?: string
              url?: string
              local_url?: string
              remote_url?: string
              width?: unknown
              height?: unknown
              aspect_ratio?: unknown
              ratio?: unknown
              size?: unknown
              size_requested?: unknown
              size_final?: unknown
              resolution?: unknown
              output_size?: unknown
            }
          }
          const prevPreview = prevData.preview
          const nextPreview = nextPayload.preview
          const nextType = (payload as Record<string, unknown>).type ?? n.data.type
          const outputPreview = parseOutputPreview((payload as Record<string, unknown>).output, nextType)
          let mergedPreview: unknown = nextPreview ?? outputPreview
          if (
            nextType === "image" &&
            !hasImageCandidate(nextPreview) &&
            !hasImageCandidate(outputPreview) &&
            hasImageCandidate(prevPreview)
          ) {
            mergedPreview = prevPreview
          }
          if (
            prevPreview?.type === "fusion" &&
            nextPreview?.type === "image" &&
            (nextPreview.url || nextPreview.local_url || nextPreview.remote_url)
          ) {
            const stages = Array.isArray(prevPreview.stages)
              ? prevPreview.stages.map((x) => ({ ...x }))
              : []
            const isImageStage = (x: { name?: string }) =>
              /图|首帧|尾帧|模板|参考|image/i.test(x.name ?? "") && !/提示词|prompt/i.test(x.name ?? "")
            const pendingIdx = stages.findIndex(
              (x) =>
                (x.status === "running" || !x.url) &&
                isImageStage(x),
            )
            const idx = pendingIdx >= 0 ? pendingIdx : stages.findIndex(isImageStage)
            if (idx >= 0) {
              const hasIncomingMedia = Boolean(nextPreview.url || nextPreview.local_url || nextPreview.remote_url)
              stages[idx] = {
                ...stages[idx],
                status: nextPreview.status ?? "completed",
                url: nextPreview.url ?? (hasIncomingMedia ? undefined : stages[idx].url),
                local_url: nextPreview.local_url ?? (hasIncomingMedia ? undefined : stages[idx].local_url),
                remote_url: nextPreview.remote_url ?? (hasIncomingMedia ? undefined : stages[idx].remote_url),
                width: nextPreview.width ?? stages[idx].width,
                height: nextPreview.height ?? stages[idx].height,
                aspect_ratio: nextPreview.aspect_ratio ?? stages[idx].aspect_ratio,
                ratio: nextPreview.ratio ?? stages[idx].ratio,
                size: nextPreview.size ?? stages[idx].size,
                size_requested: nextPreview.size_requested ?? stages[idx].size_requested,
                size_final: nextPreview.size_final ?? stages[idx].size_final,
                resolution: nextPreview.resolution ?? stages[idx].resolution,
                output_size: nextPreview.output_size ?? stages[idx].output_size,
              }
              delete stages[idx].error
              delete stages[idx].diagnostics
              mergedPreview = { ...prevPreview, stages }
            } else {
              mergedPreview = prevPreview
            }
          }
          const dataPatch: Record<string, unknown> = { ...(payload as Record<string, unknown>) }
          const hadExplicitErrorMessage = Object.prototype.hasOwnProperty.call(dataPatch, "error_message")
          const outputError = errorMessageFromUnknown(dataPatch.output ?? mergedPreview)
          const explicitError = normalizedPrompt(dataPatch.error)
          const explicitErrorMessage = normalizedPrompt(dataPatch.error_message)
          if (
            prevData.status === "failed" &&
            dataPatch.status === "running" &&
            !explicitError &&
            !outputError &&
            !hadExplicitErrorMessage
          ) {
            delete dataPatch.status
          }
          if ((dataPatch.status === "completed" || dataPatch.status === "running") && !dataPatch.error) {
            dataPatch.error = undefined
            dataPatch.error_message = undefined
          }
          if ((dataPatch.status === "failed" || explicitError || outputError) && !explicitErrorMessage) {
            dataPatch.error_message = explicitError || outputError
          }
          const nextPrompt = Object.prototype.hasOwnProperty.call(dataPatch, "prompt")
            ? dataPatch.prompt
            : prevData.prompt
          dataPatch.input = dataPatch.input_json ?? dataPatch.input ?? n.data.input
          dataPatch.output = Object.prototype.hasOwnProperty.call(dataPatch, "output") ? dataPatch.output : n.data.output
          dataPatch.workflowRuntimeOutput = dataPatch.output ?? n.data.workflowRuntimeOutput
          dataPatch.previewText = previewTextForNodeData(
            nextType,
            dataPatch.input,
            dataPatch.output,
            nextPrompt,
            n.data.previewText,
          )
          Object.assign(dataPatch, workflowMetadataFromPayload(dataPatch))
          const nextRenderState = renderStateFromPayload(
            dataPatch,
            nextType,
            dataPatch.status ?? n.data.status,
            mergedPreview ?? prevPreview,
          )
          if (nextRenderState) {
            dataPatch.renderState = nextRenderState
          }
          delete dataPatch.render_state
          const hasMediaHintPatch =
            isAutoSizedMediaType(nextType) &&
            Object.keys(collectMediaPreviewHints(dataPatch)).length > 0
          if (mergedPreview !== undefined || Object.prototype.hasOwnProperty.call(dataPatch, "prompt") || hasMediaHintPatch) {
            dataPatch.preview = mergeMediaPreviewHints(
              nextType,
              normalizePreviewForNode(nextType, mergedPreview ?? prevPreview, nextPrompt, dataPatch.input, dataPatch.output),
              dataPatch,
            )
          }
          return applyAutoMediaNodeDimensions({ ...n, data: { ...n.data, ...dataPatch } }, storedDimensions)
        })
	        return {
	          ...keepManualCanvas(nodes, s.edges),
	          manualLayout: s.manualLayout,
	        }
	      })
	    }
	  },
  loadNodes: (rawNodes, rawEdges, options) => {
    if (options?.preserveOnEmpty && rawNodes.length === 0 && get().nodes.length > 0) {
      console.warn("[openreel:canvas ignored-empty-refresh]", {
        previousNodes: get().nodes.length,
        rawEdges: rawEdges.length,
        options,
      })
      return
    }
    console.debug("[openreel:canvas loadNodes]", {
      rawNodes: rawNodes.length,
      rawEdges: rawEdges.length,
      previousNodes: get().nodes.length,
      options,
    })
	    const currentState = get()
	    const currentNodeList = currentState.nodes
	    const currentNodes = new Map(currentNodeList.map((node) => [node.id, node]))
	    const forceLayout = Boolean(options?.forceLayout)
	    const preserveExistingLayout = Boolean(options?.preserveLayout && currentNodeList.length > 0 && !forceLayout)
	    const manualLayout = forceLayout ? false : currentState.manualLayout || storedPositionsAreUsable(rawNodes)
    const storedDimensions = readStoredNodeDimensions()
    const supersededIds = new Set(
      rawNodes.filter((n) => n.supersedes_id).map((n) => n.supersedes_id!),
    )

    const nodes: Node[] = []
    let visibleOrdinal = 0
    for (const n of rawNodes) {
      const input = rawNodeInput(n)
      const output = rawNodeOutput(n)
      const prompt = promptFromRawNode(n)
      const surface = surfaceFromRawNode(n)
      const rawPublicId = n.display_id ?? undefined
      const fallbackPublicId = surface === "workflow_runtime" ? undefined : ++visibleOrdinal
      const publicId = surface === "workflow_runtime" ? undefined : rawPublicId ?? fallbackPublicId
      const preview = mergeMediaPreviewHints(
        n.type,
        normalizePreviewForNode(n.type, parseOutputPreview(output, n.type), prompt, input ?? n.input_json, output),
        input ?? n.input_json,
        n.model_config_json,
      )
      const previewText = previewTextForNodeData(n.type, input, output, prompt)
      const existing = currentNodes.get(n.id)
	      const rawHasPosition = !forceLayout && hasStoredPosition(n)
      const storedPosition = rawNodePosition(n)
	      const position = rawHasPosition
	        ? storedPosition ?? { x: CANVAS_ORIGIN_X, y: CANVAS_ORIGIN_Y }
	        : preserveExistingLayout && existing
	        ? existing.position
	        : !forceLayout && manualLayout && existing
	        ? existing.position
	        : preserveExistingLayout
	        ? nextManualNodePosition([...currentNodeList, ...nodes])
	        : { x: CANVAS_ORIGIN_X, y: CANVAS_ORIGIN_Y }
	      const baseNode: Node = {
	        id: n.id,
	        type: n.type,
        dragHandle: NODE_DRAG_HANDLE,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        position,
	        data: {
	          nodeId: n.id,
	          publicId,
	          type: n.type,
	          title: n.title,
	          status: n.status,
	          version: n.version ?? 1,
	          superseded: supersededIds.has(n.id),
	          supersedes_id: n.supersedes_id ?? undefined,
	          surface,
	          prompt,
	          input,
	          output,
	          workflowRuntimeOutput: output,
	          ...workflowMetadataFromInput(input),
	          renderState: renderStateFromRawNode(n),
	          error_message: n.error_message ?? errorMessageFromUnknown(output ?? n.output_json),
            previewText,
		          preview,
	        },
	      }
      const nextNode = applyStoredNodeDimensions(
        applyAutoMediaNodeDimensions(baseNode, storedDimensions),
        storedDimensions,
      )
	      nodes.push(preserveExistingLayout ? preserveExistingNodeLayout(nextNode, existing, storedDimensions) : nextNode)
	    }

	    const edges: Edge[] = rawEdges.map((e) => ({
	      id: e.id,
	      source: e.source_node_id,
	      target: e.target_node_id,
	      ...(e.label ? { label: e.label } : {}),
	    }))
		    if (preserveExistingLayout) {
		      set({
		        ...keepManualCanvas(nodes, edges),
		        manualLayout,
		      })
		      return
		    }
	    set({
	      ...arrangeCanvas(nodes, edges, manualLayout),
	      manualLayout,
	    })
	  },
	}))
