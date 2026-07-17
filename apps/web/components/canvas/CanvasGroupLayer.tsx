"use client"

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react"
import type { Edge, Node, NodeChange } from "reactflow"
import { updateNodePosition } from "@/lib/api"

type GroupLayoutStrategy = "grid" | "horizontal" | "vertical" | "tree"

export interface CanvasViewport {
  x: number
  y: number
  zoom: number
}

interface CanvasNodeGroup {
  id: string
  nodeIds: string[]
  label: string
  layout: GroupLayoutStrategy
  createdAt: number
}

interface CanvasGroupLayerProps {
  projectId?: string
  nodes: Node[]
  edges: Edge[]
  selectedNodeIds: string[]
  viewport: CanvasViewport
  containerRef: RefObject<HTMLDivElement>
  applyNodeChanges: (changes: NodeChange[]) => void
  registerUndo: (record: { label: string; undo: () => Promise<void> }) => void
  onClearSelection?: () => void
  onGroupedNodeIdsChange?: (nodeIds: string[]) => void
}

interface FlowBounds {
  x: number
  y: number
  width: number
  height: number
}

interface ScreenRect {
  left: number
  top: number
  width: number
  height: number
}

interface GroupViewModel {
  group: CanvasNodeGroup
  screenRect: ScreenRect
}

interface GroupDragState {
  groupId: string
  startClientX: number
  startClientY: number
  initialPositions: Record<string, { x: number; y: number }>
  latestPositions: Record<string, { x: number; y: number }>
}

const GROUP_STORAGE_KEY = "openreel.canvas.nodeGroups.v1"
const NODE_CARD_WIDTH = 260
const NODE_CARD_HEIGHT = 176
const NODE_MIN_WIDTH = 160
const NODE_MIN_HEIGHT = 110
const NODE_MAX_WIDTH = 900
const NODE_MAX_HEIGHT = 720
const GROUP_PADDING_X = 26
const GROUP_PADDING_Y = 24
const GROUP_GAP_X = 44
const GROUP_GAP_Y = 34
const TOOLBAR_HEIGHT = 34

const NODE_TIER: Record<string, number> = {
  text: 0,
  image: 1,
  video: 2,
}

const GROUP_LAYOUTS: { id: GroupLayoutStrategy; label: string }[] = [
  { id: "grid", label: "网格" },
  { id: "horizontal", label: "水平" },
  { id: "vertical", label: "垂直" },
  { id: "tree", label: "依赖" },
]

function numericDimension(value: unknown): number | undefined {
  const number = typeof value === "number" ? value : typeof value === "string" ? Number.parseFloat(value) : NaN
  return Number.isFinite(number) && number > 0 ? number : undefined
}

function nodeData(node: Node): Record<string, unknown> {
  return node.data && typeof node.data === "object" ? node.data as Record<string, unknown> : {}
}

function getNodeTier(node: Node): number {
  const type = String(nodeData(node).type || node.type || "")
  return NODE_TIER[type] ?? 99
}

function nodeTitle(node: Node): string {
  return String(nodeData(node).title || node.id)
}

function nodePublicSortValue(node: Node): number | null {
  const raw = nodeData(node).publicId
  const value = typeof raw === "number" ? raw : typeof raw === "string" ? Number(raw) : NaN
  return Number.isFinite(value) ? value : null
}

function nodeSort(a: Node, b: Node): number {
  const publicA = nodePublicSortValue(a)
  const publicB = nodePublicSortValue(b)
  if (publicA !== null && publicB !== null && publicA !== publicB) return publicA - publicB
  if (publicA !== null && publicB === null) return -1
  if (publicA === null && publicB !== null) return 1
  const ay = Number.isFinite(a.position?.y) ? a.position.y : 0
  const by = Number.isFinite(b.position?.y) ? b.position.y : 0
  if (Math.abs(ay - by) > 1) return ay - by
  const ax = Number.isFinite(a.position?.x) ? a.position.x : 0
  const bx = Number.isFinite(b.position?.x) ? b.position.x : 0
  if (Math.abs(ax - bx) > 1) return ax - bx
  const tierDiff = getNodeTier(a) - getNodeTier(b)
  if (tierDiff !== 0) return tierDiff
  return nodeTitle(a).localeCompare(nodeTitle(b), "zh-CN")
}

function clampDimension(value: number | undefined, fallback: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value || fallback))
}

function nodeVisualSize(node: Node): { width: number; height: number } {
  const data = nodeData(node)
  const style = node.style && typeof node.style === "object" ? node.style as Record<string, unknown> : {}
  return {
    width: clampDimension(
      numericDimension(node.width) ?? numericDimension(style.width) ?? numericDimension(data.canvasWidth),
      NODE_CARD_WIDTH,
      NODE_MIN_WIDTH,
      NODE_MAX_WIDTH,
    ),
    height: clampDimension(
      numericDimension(node.height) ?? numericDimension(style.height) ?? numericDimension(data.canvasHeight),
      NODE_CARD_HEIGHT,
      NODE_MIN_HEIGHT,
      NODE_MAX_HEIGHT,
    ),
  }
}

function boundsForNodes(nodes: Node[]): FlowBounds | null {
  if (nodes.length === 0) return null
  let minX = Number.POSITIVE_INFINITY
  let minY = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  let maxY = Number.NEGATIVE_INFINITY
  for (const node of nodes) {
    const size = nodeVisualSize(node)
    const x = Number.isFinite(node.position?.x) ? node.position.x : 0
    const y = Number.isFinite(node.position?.y) ? node.position.y : 0
    minX = Math.min(minX, x)
    minY = Math.min(minY, y)
    maxX = Math.max(maxX, x + size.width)
    maxY = Math.max(maxY, y + size.height)
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY) || !Number.isFinite(maxX) || !Number.isFinite(maxY)) {
    return null
  }
  return { x: minX, y: minY, width: Math.max(1, maxX - minX), height: Math.max(1, maxY - minY) }
}

function paddedBounds(bounds: FlowBounds): FlowBounds {
  return {
    x: bounds.x - GROUP_PADDING_X,
    y: bounds.y - GROUP_PADDING_Y,
    width: bounds.width + GROUP_PADDING_X * 2,
    height: bounds.height + GROUP_PADDING_Y * 2,
  }
}

function flowBoundsToScreen(bounds: FlowBounds, viewport: CanvasViewport): ScreenRect {
  const zoom = viewport.zoom || 1
  return {
    left: bounds.x * zoom + viewport.x,
    top: bounds.y * zoom + viewport.y,
    width: bounds.width * zoom,
    height: bounds.height * zoom,
  }
}

function rectContains(rect: ScreenRect, x: number, y: number): boolean {
  return x >= rect.left && x <= rect.left + rect.width && y >= rect.top && y <= rect.top + rect.height
}

function readStoredGroups(): Record<string, CanvasNodeGroup[]> {
  if (typeof window === "undefined") return {}
  try {
    const raw = window.localStorage.getItem(GROUP_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, CanvasNodeGroup[]>
      : {}
  } catch {
    return {}
  }
}

function writeStoredGroups(groupsByProject: Record<string, CanvasNodeGroup[]>) {
  if (typeof window === "undefined") return
  try {
    window.localStorage.setItem(GROUP_STORAGE_KEY, JSON.stringify(groupsByProject))
  } catch {
    // Local storage is optional canvas state; ignore quota/private-mode failures.
  }
}

function normalizeStoredGroup(group: Partial<CanvasNodeGroup>, existingIds?: Set<string>): CanvasNodeGroup | null {
  const rawNodeIds = Array.isArray(group.nodeIds) ? group.nodeIds.map((id) => String(id)) : []
  const nodeIds = Array.from(new Set(existingIds ? rawNodeIds.filter((id) => existingIds.has(id)) : rawNodeIds))
  if (nodeIds.length < 2) return null
  const layout: GroupLayoutStrategy = group.layout && GROUP_LAYOUTS.some((item) => item.id === group.layout)
    ? group.layout
    : "grid"
  const createdAt = typeof group.createdAt === "number" && Number.isFinite(group.createdAt)
    ? group.createdAt
    : Date.now()
  return {
    id: String(group.id || `group-${Date.now()}`),
    nodeIds,
    label: String(group.label || "分组"),
    layout,
    createdAt,
  }
}

function positionsFromNodes(nodes: Node[]): Record<string, { x: number; y: number }> {
  return Object.fromEntries(
    nodes.map((node) => [
      node.id,
      {
        x: Math.round(Number.isFinite(node.position?.x) ? node.position.x : 0),
        y: Math.round(Number.isFinite(node.position?.y) ? node.position.y : 0),
      },
    ]),
  )
}

function positionChanges(positions: Record<string, { x: number; y: number }>): NodeChange[] {
  return Object.entries(positions).map(([id, position]) => ({
    id,
    type: "position",
    position,
    dragging: false,
  }))
}

function hasMoved(
  previous: Record<string, { x: number; y: number }>,
  next: Record<string, { x: number; y: number }>,
): boolean {
  return Object.entries(next).some(([id, position]) => {
    const before = previous[id]
    return !before || Math.abs(before.x - position.x) > 0.5 || Math.abs(before.y - position.y) > 0.5
  })
}

function rankNodesByDependencies(nodes: Node[], edges: Edge[]): Map<string, number> {
  const ids = new Set(nodes.map((node) => node.id))
  const incoming = new Map<string, number>()
  const outgoing = new Map<string, string[]>()
  nodes.forEach((node) => {
    incoming.set(node.id, 0)
    outgoing.set(node.id, [])
  })
  for (const edge of edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target) || edge.source === edge.target) continue
    outgoing.get(edge.source)?.push(edge.target)
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1)
  }

  const rank = new Map<string, number>()
  const queue = nodes.filter((node) => (incoming.get(node.id) || 0) === 0).sort(nodeSort).map((node) => node.id)
  queue.forEach((id) => rank.set(id, 0))

  while (queue.length) {
    const id = queue.shift()!
    const sourceRank = rank.get(id) || 0
    for (const target of outgoing.get(id) || []) {
      rank.set(target, Math.max(rank.get(target) || 0, sourceRank + 1))
      incoming.set(target, Math.max(0, (incoming.get(target) || 0) - 1))
      if ((incoming.get(target) || 0) === 0) queue.push(target)
    }
  }

  nodes.forEach((node) => {
    if (!rank.has(node.id)) rank.set(node.id, getNodeTier(node))
  })
  return rank
}

function layoutGroupNodes(nodes: Node[], edges: Edge[], strategy: GroupLayoutStrategy): Record<string, { x: number; y: number }> {
  const bounds = boundsForNodes(nodes)
  if (!bounds) return {}
  const ordered = [...nodes].sort(nodeSort)

  if (strategy === "horizontal") {
    let x = bounds.x
    return Object.fromEntries(ordered.map((node) => {
      const position = { x: Math.round(x), y: Math.round(bounds.y) }
      x += nodeVisualSize(node).width + GROUP_GAP_X
      return [node.id, position]
    }))
  }

  if (strategy === "vertical") {
    let y = bounds.y
    return Object.fromEntries(ordered.map((node) => {
      const position = { x: Math.round(bounds.x), y: Math.round(y) }
      y += nodeVisualSize(node).height + GROUP_GAP_Y
      return [node.id, position]
    }))
  }

  if (strategy === "tree") {
    const nodeIdSet = new Set(nodes.map((node) => node.id))
    const dependencyEdges = edges.filter((edge) => nodeIdSet.has(edge.source) && nodeIdSet.has(edge.target))
    if (dependencyEdges.length === 0) return layoutGroupNodes(nodes, edges, "horizontal")
    const ranks = rankNodesByDependencies(nodes, dependencyEdges)
    const columns = new Map<number, Node[]>()
    for (const node of nodes) {
      const rank = ranks.get(node.id) || 0
      const column = columns.get(rank) || []
      column.push(node)
      columns.set(rank, column)
    }

    const orderedColumns = Array.from(columns.entries())
      .sort(([a], [b]) => a - b)
      .map(([rank, columnNodes]) => {
        const items = [...columnNodes].sort(nodeSort)
        const sizes = items.map(nodeVisualSize)
        const width = Math.max(...sizes.map((size) => size.width), NODE_CARD_WIDTH)
        const height = sizes.reduce((sum, size) => sum + size.height, 0) + Math.max(0, sizes.length - 1) * GROUP_GAP_Y
        return { rank, items, sizes, width, height }
      })

    const maxHeight = Math.max(...orderedColumns.map((column) => column.height), NODE_CARD_HEIGHT)
    let x = bounds.x
    const positions: Record<string, { x: number; y: number }> = {}
    for (const column of orderedColumns) {
      let y = bounds.y + Math.max(0, (maxHeight - column.height) / 2)
      for (let index = 0; index < column.items.length; index += 1) {
        const node = column.items[index]
        const size = column.sizes[index] || { width: NODE_CARD_WIDTH, height: NODE_CARD_HEIGHT }
        positions[node.id] = { x: Math.round(x), y: Math.round(y) }
        y += size.height + GROUP_GAP_Y
      }
      x += column.width + GROUP_GAP_X + 80
    }
    return positions
  }

  const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)))
  const rows = Math.max(1, Math.ceil(ordered.length / columns))
  const columnWidths = Array.from({ length: columns }, (_, column) => {
    const items = ordered.filter((_, index) => index % columns === column)
    return Math.max(...items.map((node) => nodeVisualSize(node).width), NODE_CARD_WIDTH)
  })
  const rowHeights = Array.from({ length: rows }, (_, row) => {
    const items = ordered.filter((_, index) => Math.floor(index / columns) === row)
    return Math.max(...items.map((node) => nodeVisualSize(node).height), NODE_CARD_HEIGHT)
  })
  const positions: Record<string, { x: number; y: number }> = {}
  ordered.forEach((node, index) => {
    const column = index % columns
    const row = Math.floor(index / columns)
    const x = bounds.x + columnWidths.slice(0, column).reduce((sum, width) => sum + width + GROUP_GAP_X, 0)
    const y = bounds.y + rowHeights.slice(0, row).reduce((sum, height) => sum + height + GROUP_GAP_Y, 0)
    positions[node.id] = { x: Math.round(x), y: Math.round(y) }
  })
  return positions
}

function buttonClass(active?: boolean): string {
  return [
    "h-7 rounded px-2 text-[11px] font-medium transition-colors whitespace-nowrap",
    active ? "bg-cyan-300 text-zinc-950" : "bg-white/[0.06] text-zinc-200 hover:bg-white/[0.12]",
  ].join(" ")
}

export default function CanvasGroupLayer({
  projectId,
  nodes,
  edges,
  selectedNodeIds,
  viewport,
  containerRef,
  applyNodeChanges,
  registerUndo,
  onClearSelection,
  onGroupedNodeIdsChange,
}: CanvasGroupLayerProps) {
  const [groups, setGroups] = useState<CanvasNodeGroup[]>([])
  const [loadedProjectId, setLoadedProjectId] = useState<string | null>(null)
  const [frameGroupId, setFrameGroupId] = useState<string | null>(null)
  const [toolbarGroupId, setToolbarGroupId] = useState<string | null>(null)
  const [selectionPointerActive, setSelectionPointerActive] = useState(false)
  const groupModelsRef = useRef<GroupViewModel[]>([])
  const dragRef = useRef<GroupDragState | null>(null)

  const nodeById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes])
  const groupedNodeIds = useMemo(() => {
    const ids = new Set<string>()
    groups.forEach((group) => {
      group.nodeIds.forEach((nodeId) => {
        if (nodeById.has(nodeId)) ids.add(nodeId)
      })
    })
    return Array.from(ids).sort()
  }, [groups, nodeById])
  const selectedNodes = useMemo(
    () => selectedNodeIds.map((id) => nodeById.get(id)).filter(Boolean) as Node[],
    [nodeById, selectedNodeIds],
  )
  const selectionBounds = useMemo(() => {
    if (selectedNodes.length < 2) return null
    const bounds = boundsForNodes(selectedNodes)
    return bounds ? flowBoundsToScreen(paddedBounds(bounds), viewport) : null
  }, [selectedNodes, viewport])
  const selectionToolbarVisible = Boolean(selectionBounds && !selectionPointerActive)
  const groupModels = useMemo<GroupViewModel[]>(() => groups
    .map((group) => {
      const groupNodes = group.nodeIds.map((id) => nodeById.get(id)).filter(Boolean) as Node[]
      if (groupNodes.length < 2) return null
      const bounds = boundsForNodes(groupNodes)
      if (!bounds) return null
      return {
        group,
        screenRect: flowBoundsToScreen(paddedBounds(bounds), viewport),
      }
    })
    .filter(Boolean) as GroupViewModel[], [groups, nodeById, viewport])

  useEffect(() => {
    groupModelsRef.current = groupModels
  }, [groupModels])

  const revealGroup = useCallback((groupId: string) => {
    setFrameGroupId(groupId)
    setToolbarGroupId(groupId)
  }, [])

  const revealToolbar = useCallback((groupId: string) => {
    setToolbarGroupId(groupId)
  }, [])

  useEffect(() => {
    onGroupedNodeIdsChange?.(groupedNodeIds)
  }, [groupedNodeIds, onGroupedNodeIdsChange])

  useEffect(() => {
    if (!projectId) {
      setGroups([])
      setLoadedProjectId(null)
      return
    }
    const stored = readStoredGroups()
    const projectGroups = (stored[projectId] || [])
      .map((group) => normalizeStoredGroup(group))
      .filter(Boolean) as CanvasNodeGroup[]
    setGroups(projectGroups)
    setLoadedProjectId(projectId)
  }, [projectId])

  useEffect(() => {
    if (!projectId || loadedProjectId !== projectId) return
    if (nodes.length === 0) return
    const existingIds = new Set(nodes.map((node) => node.id))
    setGroups((current) => {
      const next = current
        .map((group) => normalizeStoredGroup(group, existingIds))
        .filter(Boolean) as CanvasNodeGroup[]
      return JSON.stringify(next) === JSON.stringify(current) ? current : next
    })
  }, [loadedProjectId, nodes, projectId])

  useEffect(() => {
    if (!projectId || loadedProjectId !== projectId) return
    const stored = readStoredGroups()
    stored[projectId] = groups
    writeStoredGroups(stored)
  }, [groups, loadedProjectId, projectId])

  const applyPositions = useCallback((positions: Record<string, { x: number; y: number }>) => {
    applyNodeChanges(positionChanges(positions))
  }, [applyNodeChanges])

  const persistPositions = useCallback(async (positions: Record<string, { x: number; y: number }>) => {
    if (!projectId) return
    await Promise.all(Object.entries(positions).map(([nodeId, position]) => updateNodePosition(projectId, nodeId, position)))
  }, [projectId])

  const createGroupFromSelection = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    if (!projectId || selectedNodes.length < 2) return
    const nextId = `group-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`
    const nextNodeIds = selectedNodes.map((node) => node.id)
    const nextGroup: CanvasNodeGroup = {
      id: nextId,
      nodeIds: nextNodeIds,
      label: `分组 ${groups.length + 1}`,
      layout: "grid",
      createdAt: Date.now(),
    }
    setGroups((current) => {
      const selectedIdSet = new Set(nextNodeIds)
      const retained = current
        .map((group) => ({ ...group, nodeIds: group.nodeIds.filter((id) => !selectedIdSet.has(id)) }))
        .filter((group) => group.nodeIds.length >= 2)
      return [...retained, nextGroup]
    })
    applyNodeChanges(selectedNodes.map((node) => ({ id: node.id, type: "select", selected: false })))
    onClearSelection?.()
    revealGroup(nextId)
  }, [applyNodeChanges, groups.length, onClearSelection, projectId, revealGroup, selectedNodes])

  const removeGroup = useCallback((groupId: string) => {
    setGroups((current) => current.filter((group) => group.id !== groupId))
    if (frameGroupId === groupId) setFrameGroupId(null)
    if (toolbarGroupId === groupId) setToolbarGroupId(null)
  }, [frameGroupId, toolbarGroupId])

  const applyGroupLayout = useCallback((group: CanvasNodeGroup, strategy: GroupLayoutStrategy) => {
    const groupNodes = group.nodeIds.map((id) => nodeById.get(id)).filter(Boolean) as Node[]
    if (groupNodes.length < 2) return
    const previous = positionsFromNodes(groupNodes)
    const next = layoutGroupNodes(groupNodes, edges, strategy)
    if (!hasMoved(previous, next)) {
      setGroups((current) => current.map((item) => item.id === group.id ? { ...item, layout: strategy } : item))
      return
    }
    applyPositions(next)
    setGroups((current) => current.map((item) => item.id === group.id ? { ...item, layout: strategy } : item))
    void persistPositions(next).catch((error) => {
      console.warn("Failed to persist group layout", error)
    })
    registerUndo({
      label: "分组布局",
      undo: async () => {
        applyPositions(previous)
        await persistPositions(previous)
      },
    })
  }, [applyPositions, edges, nodeById, persistPositions, registerUndo])

  const applySelectionLayout = useCallback((strategy: GroupLayoutStrategy, event: ReactMouseEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    if (selectedNodes.length < 2) return
    const previous = positionsFromNodes(selectedNodes)
    const next = layoutGroupNodes(selectedNodes, edges, strategy)
    if (!hasMoved(previous, next)) return
    applyPositions(next)
    void persistPositions(next).catch((error) => {
      console.warn("Failed to persist selection layout", error)
    })
    registerUndo({
      label: "选择布局",
      undo: async () => {
        applyPositions(previous)
        await persistPositions(previous)
      },
    })
  }, [applyPositions, edges, persistPositions, registerUndo, selectedNodes])

  const beginGroupDrag = useCallback((group: CanvasNodeGroup, clientX: number, clientY: number) => {
    const groupNodes = group.nodeIds.map((id) => nodeById.get(id)).filter(Boolean) as Node[]
    if (groupNodes.length < 2) return
    const initialPositions = positionsFromNodes(groupNodes)
    dragRef.current = {
      groupId: group.id,
      startClientX: clientX,
      startClientY: clientY,
      initialPositions,
      latestPositions: initialPositions,
    }
    revealGroup(group.id)
  }, [nodeById, revealGroup])

  const startGroupDrag = useCallback((group: CanvasNodeGroup, event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    event.stopPropagation()
    beginGroupDrag(group, event.clientX, event.clientY)
  }, [beginGroupDrag])

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      const drag = dragRef.current
      if (!drag) return
      event.preventDefault()
      const zoom = viewport.zoom || 1
      const dx = (event.clientX - drag.startClientX) / zoom
      const dy = (event.clientY - drag.startClientY) / zoom
      const nextPositions = Object.fromEntries(Object.entries(drag.initialPositions).map(([nodeId, position]) => [
        nodeId,
        { x: Math.round(position.x + dx), y: Math.round(position.y + dy) },
      ]))
      drag.latestPositions = nextPositions
      applyPositions(nextPositions)
    }
    const handlePointerUp = () => {
      const drag = dragRef.current
      if (!drag) return
      dragRef.current = null
      if (!hasMoved(drag.initialPositions, drag.latestPositions)) return
      void persistPositions(drag.latestPositions).catch((error) => {
        console.warn("Failed to persist group drag", error)
      })
      registerUndo({
        label: "移动分组",
        undo: async () => {
          applyPositions(drag.initialPositions)
          await persistPositions(drag.initialPositions)
        },
      })
    }
    window.addEventListener("pointermove", handlePointerMove)
    window.addEventListener("pointerup", handlePointerUp)
    window.addEventListener("pointercancel", handlePointerUp)
    return () => {
      window.removeEventListener("pointermove", handlePointerMove)
      window.removeEventListener("pointerup", handlePointerUp)
      window.removeEventListener("pointercancel", handlePointerUp)
    }
  }, [applyPositions, persistPositions, registerUndo, viewport.zoom])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const endSelectionPointer = () => {
      setSelectionPointerActive(false)
    }
    const handlePointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return
      const target = event.target as HTMLElement | null
      if (target?.closest("[data-openreel-group-toolbar='true']")) return
      setSelectionPointerActive(true)
      if (target?.closest(".react-flow__node")) return
      const rect = container.getBoundingClientRect()
      const x = event.clientX - rect.left
      const y = event.clientY - rect.top
      const model = [...groupModelsRef.current].reverse().find((item) => rectContains(item.screenRect, x, y))
      if (!model) return
      event.preventDefault()
      event.stopPropagation()
      beginGroupDrag(model.group, event.clientX, event.clientY)
    }
    const handlePointerMove = (event: PointerEvent) => {
      const target = event.target as HTMLElement | null
      const toolbar = target?.closest("[data-openreel-group-toolbar='true']") as HTMLElement | null
      const toolbarGroupId = toolbar?.dataset.openreelGroupId
      if (toolbarGroupId) {
        revealGroup(toolbarGroupId)
        return
      }
      if (dragRef.current) return
      const rect = container.getBoundingClientRect()
      const x = event.clientX - rect.left
      const y = event.clientY - rect.top
      const model = [...groupModelsRef.current].reverse().find((item) => rectContains(item.screenRect, x, y))
      setFrameGroupId(model?.group.id || null)
      if (model) {
        revealToolbar(model.group.id)
      } else {
        setToolbarGroupId(null)
      }
    }
    const handlePointerLeave = () => {
      if (!dragRef.current) {
        setFrameGroupId(null)
        setToolbarGroupId(null)
      }
    }
    container.addEventListener("pointerdown", handlePointerDown, true)
    container.addEventListener("pointermove", handlePointerMove)
    container.addEventListener("pointerleave", handlePointerLeave)
    window.addEventListener("pointerup", endSelectionPointer)
    window.addEventListener("pointercancel", endSelectionPointer)
    return () => {
      container.removeEventListener("pointerdown", handlePointerDown, true)
      container.removeEventListener("pointermove", handlePointerMove)
      container.removeEventListener("pointerleave", handlePointerLeave)
      window.removeEventListener("pointerup", endSelectionPointer)
      window.removeEventListener("pointercancel", endSelectionPointer)
    }
  }, [beginGroupDrag, containerRef, revealGroup, revealToolbar])

  return (
    <div className="openreel-canvas-group-layer pointer-events-none absolute inset-0 z-20 overflow-hidden">
      {groupModels.map(({ group, screenRect }) => {
        const toolbarTop = screenRect.top + 8
        const frameVisible = frameGroupId === group.id
        const toolbarVisible = toolbarGroupId === group.id
        return (
          <div key={group.id}>
            <div
              className={[
                "pointer-events-none absolute rounded-md border transition-[background-color,border-color,box-shadow]",
                frameVisible
                  ? "border-cyan-200/85 bg-cyan-300/[0.05] shadow-[0_0_0_1px_rgba(34,211,238,0.08),0_18px_50px_rgba(0,0,0,0.22)]"
                  : "border-transparent bg-transparent shadow-none",
              ].join(" ")}
              style={{
                left: screenRect.left,
                top: screenRect.top,
                width: screenRect.width,
                height: screenRect.height,
              }}
            />
            {frameVisible && (
              <div
                className="pointer-events-none absolute rounded bg-[#0f131b]/90 px-2 py-1 text-[10px] font-medium text-cyan-100 shadow-lg shadow-black/25"
                style={{ left: screenRect.left + 10, top: screenRect.top + 8 }}
              >
                {group.label}
              </div>
            )}
            {toolbarVisible && (
              <div
                data-openreel-group-toolbar="true"
                data-openreel-group-id={group.id}
                className="pointer-events-auto absolute z-40 flex h-[34px] items-center gap-1 rounded-md border border-white/10 bg-[#11151d]/95 px-1.5 shadow-2xl shadow-black/35 backdrop-blur"
                style={{
                  left: Math.max(8, screenRect.left + screenRect.width - 8),
                  top: Math.max(8, toolbarTop),
                  transform: "translateX(-100%)",
                }}
                onClick={(event) => event.stopPropagation()}
                onPointerEnter={() => revealGroup(group.id)}
                onPointerDown={(event) => event.stopPropagation()}
              >
                <div
                  className="flex h-7 cursor-move items-center rounded px-2 text-[11px] font-medium text-cyan-100 hover:bg-white/[0.08]"
                  title="拖动整个分组"
                  onPointerDown={(event) => startGroupDrag(group, event)}
                >
                  分组
                </div>
                <div className="mx-1 h-4 w-px bg-white/10" />
                {GROUP_LAYOUTS.map((layout) => (
                  <button
                    key={layout.id}
                    type="button"
                    className={buttonClass(group.layout === layout.id)}
                    onClick={(event) => {
                      event.preventDefault()
                      event.stopPropagation()
                      applyGroupLayout(group, layout.id)
                    }}
                  >
                    {layout.label}
                  </button>
                ))}
                <div className="mx-1 h-4 w-px bg-white/10" />
                <button
                  type="button"
                  className={buttonClass(false)}
                  onClick={(event) => {
                    event.preventDefault()
                    event.stopPropagation()
                    removeGroup(group.id)
                  }}
                >
                  解组
                </button>
              </div>
            )}
          </div>
        )
      })}

      {selectionToolbarVisible && selectionBounds && (
        <div
          data-openreel-group-toolbar="true"
          className="pointer-events-auto absolute z-50 flex h-[34px] items-center gap-1 rounded-md border border-white/10 bg-[#11151d]/95 px-1.5 shadow-2xl shadow-black/35 backdrop-blur"
          style={{
            left: selectionBounds.left + selectionBounds.width - 8,
            top: Math.max(8, selectionBounds.top - 42),
            transform: "translateX(-100%)",
          }}
          onClick={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <div className="flex h-7 items-center rounded px-2 text-[11px] font-medium text-cyan-100">
            选择
          </div>
          <div className="mx-1 h-4 w-px bg-white/10" />
          {GROUP_LAYOUTS.map((layout) => (
            <button
              key={layout.id}
              type="button"
              className={buttonClass(false)}
              onClick={(event) => applySelectionLayout(layout.id, event)}
            >
              {layout.label}
            </button>
          ))}
          <div className="mx-1 h-4 w-px bg-white/10" />
          <button
            type="button"
            className="h-7 rounded bg-cyan-300 px-2.5 text-[11px] font-semibold text-zinc-950 transition-colors hover:bg-cyan-200"
            onPointerDown={createGroupFromSelection}
          >
            打组
          </button>
        </div>
      )}
    </div>
  )
}
