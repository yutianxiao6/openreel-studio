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

export type LayoutStrategy = "vertical" | "horizontal" | "grid" | "timeline" | "iteration" | "tree"
export type NodeSurface = "project_panel" | "draft_canvas"

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
  resizeNode: (id: string, width: number, height: number, options?: { persist?: boolean }) => void
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
      type: string
      title: string
      status: string
      position_x: number
      position_y: number
      version?: number
      supersedes_id?: string | null
      output_json?: string | null
      input_json?: string | null
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

function ratioFromSize(width?: number, height?: number): number | null {
  if (!width || !height || !Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return null
  }
  return Math.min(3.2, Math.max(0.42, width / height))
}

function mediaNodeDimensionsFromPreview(preview?: Record<string, unknown>): { width: number; height: number } {
  const grid = preview?.grid && typeof preview.grid === "object" && !Array.isArray(preview.grid)
    ? preview.grid as Record<string, unknown>
    : undefined
  const cells = Array.isArray(preview?.cells) ? preview.cells as Record<string, unknown>[] : []
  const cell = cells.find((item) => numericDimension(item.width) && numericDimension(item.height))
  const gridCols = numericDimension(grid?.cols) || 1
  const gridRows = numericDimension(grid?.rows) || 1
  const ratio =
    ratioFromSize(numericDimension(preview?.width), numericDimension(preview?.height)) ||
    (cell ? ratioFromSize((numericDimension(cell.width) || 1) * gridCols, (numericDimension(cell.height) || 1) * gridRows) : null) ||
    (preview?.type === "image_grid" ? gridCols / gridRows : null) ||
    NODE_CARD_WIDTH / NODE_CARD_HEIGHT

  let width = Math.sqrt(MEDIA_TARGET_AREA * ratio)
  const minWidthForRatio = Math.max(MEDIA_MIN_WIDTH, MEDIA_MIN_HEIGHT * ratio)
  const maxWidthForRatio = Math.min(MEDIA_MAX_WIDTH, MEDIA_MAX_HEIGHT * ratio)
  width = minWidthForRatio <= maxWidthForRatio
    ? Math.min(maxWidthForRatio, Math.max(minWidthForRatio, width))
    : maxWidthForRatio
  return { width: Math.round(width), height: Math.round(width / ratio) }
}

function nodeVisualSize(node: Node): { width: number; height: number } {
  const data = node.data as { type?: string; preview?: Record<string, unknown>; canvasWidth?: number; canvasHeight?: number } | undefined
  if (data?.canvasWidth && data.canvasHeight) {
    return {
      width: Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, Number(data.canvasWidth) || NODE_CARD_WIDTH)),
      height: Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, Number(data.canvasHeight) || NODE_CARD_HEIGHT)),
    }
  }
  if (data?.type === "image") {
    const size = mediaNodeDimensionsFromPreview(data.preview)
    return { width: size.width, height: size.height }
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

function edgeKey(edge: Pick<Edge, "source" | "target">): string {
  return `${edge.source}->${edge.target}`
}

function changedEdgeEndpointIds(previousEdges: Edge[], nextEdges: Edge[]): Set<string> {
  const previousKeys = new Set(previousEdges.map(edgeKey))
  const nextKeys = new Set(nextEdges.map(edgeKey))
  const affected = new Set<string>()
  for (const edge of nextEdges) {
    if (previousKeys.has(edgeKey(edge))) continue
    affected.add(edge.source)
    affected.add(edge.target)
  }
  for (const edge of previousEdges) {
    if (nextKeys.has(edgeKey(edge))) continue
    affected.add(edge.source)
    affected.add(edge.target)
  }
  return affected
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

function hasStoredPosition(raw: { position_x?: number | null; position_y?: number | null }): boolean {
  const x = Number(raw.position_x ?? 0)
  const y = Number(raw.position_y ?? 0)
  return Number.isFinite(x) && Number.isFinite(y) && (Math.abs(x) > 0.01 || Math.abs(y) > 0.01)
}

function storedPositionsAreUsable(rawNodes: { position_x?: number | null; position_y?: number | null }[]): boolean {
  if (rawNodes.length <= 1) return rawNodes.some(hasStoredPosition)
  const positioned = rawNodes.filter(hasStoredPosition)
  if (positioned.length !== rawNodes.length) return false
  const buckets = new Set(positioned.map((node) => {
    const x = Math.round(Number(node.position_x ?? 0) / 40)
    const y = Math.round(Number(node.position_y ?? 0) / 40)
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
    window.localStorage.setItem(
      NODE_DIMENSIONS_STORAGE_KEY,
      JSON.stringify({ ...current, ...updates }),
    )
  } catch {
    // localStorage can be unavailable in private mode; canvas editing still works for this session.
  }
}

function clampNodeDimension(width: number, height: number): StoredNodeDimensions {
  return {
    width: Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, Math.round(width))),
    height: Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, Math.round(height))),
  }
}

function dimensionUpdatesFromChanges(
  changes: NodeChange[],
  options: { completedOnly?: boolean } = { completedOnly: true },
): Record<string, StoredNodeDimensions> {
  const updates: Record<string, StoredNodeDimensions> = {}
  for (const change of changes) {
    if (change.type !== "dimensions" || !change.dimensions) continue
    if (options.completedOnly && change.resizing !== false) continue
    const width = Math.round(change.dimensions.width)
    const height = Math.round(change.dimensions.height)
    if (width >= NODE_MIN_WIDTH && height >= NODE_MIN_HEIGHT) {
      updates[change.id] = clampNodeDimension(width, height)
    }
  }
  return updates
}

function applyDimensionChangesToData(nodes: Node[], changes: NodeChange[]): Node[] {
  const updates = dimensionUpdatesFromChanges(changes, { completedOnly: false })
  if (Object.keys(updates).length === 0) return nodes
  return nodes.map((node) => {
    const dimensions = updates[node.id]
    if (!dimensions) return node
    const width = Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, dimensions.width))
    const height = Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, dimensions.height))
    return {
      ...node,
      style: { ...node.style, width, height },
      data: {
        ...node.data,
        canvasWidth: width,
        canvasHeight: height,
      },
    }
  })
}

function applyStoredNodeDimensions(
  node: Node,
  storedDimensions: Record<string, StoredNodeDimensions>,
): Node {
  const dimensions = storedDimensions[node.id]
  if (!dimensions) return node
  const width = Math.max(NODE_MIN_WIDTH, Math.min(NODE_MAX_WIDTH, dimensions.width))
  const height = Math.max(NODE_MIN_HEIGHT, Math.min(NODE_MAX_HEIGHT, dimensions.height))
  return {
    ...node,
    style: { ...node.style, width, height },
    data: {
      ...node.data,
      canvasWidth: width,
      canvasHeight: height,
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

function normalizeSurface(raw: unknown): NodeSurface | undefined {
  return raw === "draft_canvas" || raw === "project_panel" ? raw : undefined
}

function surfaceFromRawNode(raw: {
  surface?: string | null
  input_json?: string | null
  model_config_json?: string | null
}): NodeSurface {
  const direct = normalizeSurface(raw.surface)
  if (direct) return direct

  const modelConfig = parseObjectJson(raw.model_config_json)
  const fromModel = normalizeSurface(modelConfig?.surface ?? modelConfig?._surface)
  if (fromModel) return fromModel

  const input = parseObjectJson(raw.input_json)
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

function existingNodeDimension(node: Node, key: "width" | "height"): number | undefined {
  const data = node.data as { canvasWidth?: unknown; canvasHeight?: unknown } | undefined
  const dataValue = key === "width" ? data?.canvasWidth : data?.canvasHeight
  const styleValue = node.style ? (node.style as Record<string, unknown>)[key] : undefined
  return numericDimension(dataValue) ?? numericDimension(styleValue) ?? numericDimension(node[key])
}

function preserveExistingNodeLayout(next: Node, existing: Node | undefined): Node {
  if (!existing) return next
  const width = existingNodeDimension(existing, "width")
  const height = existingNodeDimension(existing, "height")
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
    },
  }
}

function promptFromRawNode(raw: { prompt?: string | null; input_json?: string | null }): string | undefined {
  const direct = normalizedPrompt(raw.prompt)
  if (direct) return direct
  const input = parseObjectJson(raw.input_json)
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
  output_json?: string | null
  input_json?: string | null
  render_state?: string | null
}): string | undefined {
  if (raw.type !== "image") return undefined
  const direct = normalizeRenderState(raw.render_state)
  if (direct) return direct
  const input = parseObjectJson(raw.input_json)
  const fromInput = normalizeRenderState(input?.render_state)
  if (fromInput) return fromInput
  return raw.status === "completed" && raw.output_json ? "fresh" : undefined
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
  if (type !== "image" && type !== "video") return undefined
  return {
    type: type === "video" ? "video_prompt" : "image_prompt",
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

  if (data.type === "image_prompt" || data.type === "video_prompt") {
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
): Record<string, unknown> | undefined {
  const normalized = applyCurrentPromptToPreview(preview, prompt) ?? promptPreviewFromNodeType(nodeType, prompt)
  return normalized ? normalizeFusionPreviewErrors(normalized) : undefined
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function parseOutputPreview(outputJson: string | null | undefined): Record<string, unknown> | undefined {
  if (!outputJson) return undefined
  try {
    const data = typeof outputJson === "string" ? JSON.parse(outputJson) : outputJson
    if (!data || typeof data !== "object") return undefined
    for (const key of ["content", "text", "summary", "description"]) {
      const value = data[key]
      if (typeof value === "string" && value.trim()) {
        return { type: "text", text: value.trim() }
      }
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
        width: numericDimension(data.width),
        height: numericDimension(data.height),
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
        width: numericDimension(data.width),
        height: numericDimension(data.height),
      }
    }
    // Standalone image preview from node/media service output
    if (data.type === "image" && (data.url || data.local_url)) {
      return {
        type: "image",
        url: data.url as string | undefined,
        local_url: data.local_url as string | undefined,
        remote_url: data.remote_url as string | undefined,
        width: numericDimension(data.width),
        height: numericDimension(data.height),
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
        width: numericDimension(data.width),
        height: numericDimension(data.height),
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
        width: numericDimension(data.width),
        height: numericDimension(data.height),
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
        width: numericDimension(img.width),
        height: numericDimension(img.height),
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
        width: numericDimension(video.width),
        height: numericDimension(video.height),
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
        width: numericDimension(data.width),
        height: numericDimension(data.height),
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
        width: numericDimension(data.width),
        height: numericDimension(data.height),
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
	  updateNode: (id, data) =>
	    set((s) => {
	      const nodes = s.nodes.map((n) => {
        if (n.id !== id) return n
        const nextData = { ...n.data, ...data }
        const nextRenderState = renderStateFromPayload(
          data as Record<string, unknown>,
          nextData.type ?? n.type,
          nextData.status,
          nextData.preview,
        )
        if (nextRenderState) nextData.renderState = nextRenderState
        nextData.preview = normalizePreviewForNode(nextData.type ?? n.type, nextData.preview, nextData.prompt)
        return { ...n, data: nextData }
      })
	      return {
	        ...keepManualCanvas(nodes, s.edges),
	        manualLayout: s.manualLayout,
	      }
	    }),
  resizeNode: (id, width, height, options) => {
    const dimensions = clampNodeDimension(width, height)
    if (options?.persist) {
      writeStoredNodeDimensions({ [id]: dimensions })
    }
    set((s) => ({
      ...keepManualCanvas(
        s.nodes.map((node) => {
          if (node.id !== id) return node
          return {
            ...node,
            style: { ...node.style, width: dimensions.width, height: dimensions.height },
            data: {
              ...node.data,
              canvasWidth: dimensions.width,
              canvasHeight: dimensions.height,
            },
          }
        }),
        s.edges,
      ),
      manualLayout: s.manualLayout,
    }))
  },
  applyNodeChanges: (changes) => {
    writeStoredNodeDimensions(dimensionUpdatesFromChanges(changes))
    set((s) => ({
      ...keepManualCanvas(
        applyDimensionChangesToData(applyReactFlowNodeChanges(changes, s.nodes), changes),
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
  selectNode: (id) => set({ selectedNodeId: id }),
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
	      set((s) => ({
	        ...keepManualCanvas(
	          layoutLocalMindMap(s.nodes, [...s.edges, newEdge], new Set([source, target])),
	          [...s.edges, newEdge],
	        ),
	        manualLayout: s.manualLayout,
	      }))
	      return
	    }
    if (action === "create_node") {
      const id = String(payload.id ?? "")
      if (!id) return

      const { nodes, edges } = get()
      if (nodes.some((n) => n.id === id)) {
        set((s) => arrangeCanvas(
          s.nodes.map((n) =>
            n.id === id
              ? {
                  ...n,
                  data: {
                    ...n.data,
                    type: payload.type ?? n.data.type,
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
                    preview: normalizePreviewForNode(
                      payload.type ?? n.data.type,
                      payload.preview ?? n.data.preview,
                      payload.prompt ?? n.data.prompt,
                    ),
                  },
                }
              : n,
          ),
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
	      const newNode: Node = {
        id,
        type: String(payload.type ?? "default"),
        dragHandle: NODE_DRAG_HANDLE,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
	        position: explicitPosition ?? (preserveExistingLayout || get().manualLayout ? nextManualNodePosition(mutatedNodes) : { x: CANVAS_ORIGIN_X, y: CANVAS_ORIGIN_Y }),
        data: {
          nodeId: id,
          type: payload.type,
          title: payload.title,
          status: payload.status ?? "running",
          prompt: normalizedPrompt(payload.prompt),
          renderState: renderStateFromPayload(payload as Record<string, unknown>, payload.type, payload.status, payload.preview),
          preview: normalizePreviewForNode(payload.type, payload.preview, payload.prompt),
          group_id: groupId,
          group_label: hint?.group_label,
          layout_strategy: hint?.strategy ?? "vertical",
          surface,
          version: payload.version ?? 1,
          supersedes_id: supersedesId,
          createdAt: new Date().toISOString(),
        },
      }

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
      set((s) => {
        const nodes = s.nodes.map((n) => {
          if (n.id !== id) return n
          // 防御性 preview 合并:旧 fusion(含已生成的人物设定/提示词阶段)+
          // 新简易 image preview → 把 url 注入 fusion stages 找到的图阶段,而非整段替换。
          // 修复"图来了又消失"bug:之前 _emit_node_canvas_event 推扁平 image preview
          // 浅合并会覆盖正在显示的 fusion stages。
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
            }
          }
          const prevPreview = prevData.preview
          const nextPreview = nextPayload.preview
          let mergedPreview: unknown = nextPreview
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
              }
              delete stages[idx].error
              delete stages[idx].diagnostics
              mergedPreview = { ...prevPreview, stages }
            } else {
              // fusion 已经有 completed 图阶段,扁平 image preview 是后端 node.run 的兜底事件,
              // 不要让它整段替换已有的 fusion 结构(否则前一张图会"消失")。保留原 fusion。
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
          const nextType = dataPatch.type ?? n.data.type
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
          if (mergedPreview !== undefined || Object.prototype.hasOwnProperty.call(dataPatch, "prompt")) {
            dataPatch.preview = normalizePreviewForNode(nextType, mergedPreview ?? prevPreview, nextPrompt)
          }
          return { ...n, data: { ...n.data, ...dataPatch } }
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
    for (const n of rawNodes) {
      const prompt = promptFromRawNode(n)
      const preview = normalizePreviewForNode(n.type, parseOutputPreview(n.output_json), prompt)
      const existing = currentNodes.get(n.id)
	      const rawHasPosition = !forceLayout && hasStoredPosition(n)
	      const position = rawHasPosition
	        ? { x: n.position_x || CANVAS_ORIGIN_X, y: n.position_y || CANVAS_ORIGIN_Y }
	        : preserveExistingLayout && existing
	        ? existing.position
	        : !forceLayout && manualLayout && existing
	        ? existing.position
	        : preserveExistingLayout
	        ? nextManualNodePosition([...currentNodeList, ...nodes])
	        : { x: CANVAS_ORIGIN_X, y: CANVAS_ORIGIN_Y }
	      const nextNode = applyStoredNodeDimensions({
	        id: n.id,
	        type: n.type,
        dragHandle: NODE_DRAG_HANDLE,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        position,
        data: {
          nodeId: n.id,
          type: n.type,
          title: n.title,
          status: n.status,
          version: n.version ?? 1,
          superseded: supersededIds.has(n.id),
          supersedes_id: n.supersedes_id ?? undefined,
          surface: surfaceFromRawNode(n),
          prompt,
          renderState: renderStateFromRawNode(n),
          error_message: n.error_message ?? errorMessageFromUnknown(n.output_json),
	          preview,
	        },
	      }, storedDimensions)
	      nodes.push(preserveExistingLayout ? preserveExistingNodeLayout(nextNode, existing) : nextNode)
	    }

    const edges: Edge[] = rawEdges.map((e) => ({
      id: e.id,
      source: e.source_node_id,
      target: e.target_node_id,
      ...(e.label ? { label: e.label } : {}),
    }))
	    if (preserveExistingLayout) {
	      const affectedIds = changedEdgeEndpointIds(currentState.edges, edges)
	      set({
	        ...keepManualCanvas(
	          affectedIds.size > 0 ? layoutLocalMindMap(nodes, edges, affectedIds) : nodes,
	          edges,
	        ),
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
