"use client"

import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type PointerEvent as ReactPointerEvent, type TouchEvent as ReactTouchEvent } from "react"
import ReactFlow, {
  ConnectionLineType,
  ConnectionMode,
  Controls,
  MiniMap,
  MarkerType,
  Position,
  SelectionMode,
  getBezierPath,
  useReactFlow,
  type Connection,
  type ConnectionLineComponentProps,
  type EdgeChange,
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
  callTool,
  createProjectEdge,
  createProjectNode,
  deleteProjectEdge,
  deleteProjectNode,
  getApiBaseSync,
  getProjectNodeDetails,
  getProjectNodes,
  resolveMediaUrl,
  restoreProjectCanvasSnapshot,
  updateNodePosition,
  type CanvasRefreshOptions,
  type CanvasEdgeSnapshot,
  type CanvasNodeSnapshot,
  type CanvasNodeType,
} from "@/lib/api"
import { nodeTypes } from "./nodes"
import NodeDetailPanel from "./NodeDetailPanel"
import CanvasGroupLayer, { type CanvasViewport } from "./CanvasGroupLayer"

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
const PROJECT_ASSET_KINDS = ["script", "character", "scene", "first_frame", "last_frame", "storyboard", "story_template"]
const SHARED_ASSET_KINDS = ["character", "scene"]
const GENERIC_IMAGE_TITLES = new Set(["", "未命名", "未命名图片", "图片节点"])

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
    "button,a,input,textarea,select,[contenteditable='true'],.nodrag,.openreel-canvas-action-menu,.react-flow__handle,.react-flow__controls,.react-flow__minimap",
  ))
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
      const src = item.local_url || item.url || item.remote_url
      return isImageStageName(item.name) && typeof src === "string" && !isVideoUrl(src)
    })
    const src = stage ? stage.local_url || stage.url || stage.remote_url : null
    return typeof src === "string" ? resolveMediaUrl(src) : null
  }
  const src = preview.local_url || preview.url || preview.composite_url || preview.remote_url
  return typeof src === "string" && !isVideoUrl(src) ? resolveMediaUrl(src) : null
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

function getPointerClientPoint(event: globalThis.MouseEvent | globalThis.TouchEvent) {
  if ("changedTouches" in event && event.changedTouches.length) {
    return { x: event.changedTouches[0].clientX, y: event.changedTouches[0].clientY }
  }
  return { x: (event as globalThis.MouseEvent).clientX, y: (event as globalThis.MouseEvent).clientY }
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

export default function WorkflowCanvas() {
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
  const [contextMenu, setContextMenu] = useState<{
    x: number
    y: number
    flowX: number
    flowY: number
    connectFrom?: PendingConnectionDraft
    previewLine?: PendingConnectionPreviewLine
  } | null>(null)
  const [nodeActionMenu, setNodeActionMenu] = useState<NodeActionMenuState | null>(null)
  const [assetSaveRequest, setAssetSaveRequest] = useState<NodeAssetSaveRequest | null>(null)
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
  const connectionStartRef = useRef<PendingConnectionDraft | null>(null)
  const connectionCompletedRef = useRef(false)
  const suppressPaneClickRef = useRef(false)
  const longPressRef = useRef<LongPressState | null>(null)
  const refreshTimerRef = useRef<number | null>(null)
  const nodes = allNodes
  const groupedNodeIdSet = useMemo(() => new Set(groupedNodeIds), [groupedNodeIds])
  const visibleNodeIds = useMemo(() => new Set(nodes.map((node) => node.id)), [nodes])
  const edges = useMemo(
    () => allEdges.filter((edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)),
    [allEdges, visibleNodeIds],
  )
  const selectedCanvasNodeId = selectedNodeId && visibleNodeIds.has(selectedNodeId) ? selectedNodeId : null
  const selectedNodeIds = useMemo(
    () => {
      const ids = new Set(nodes.filter((node) => node.selected).map((node) => node.id))
      if (selectedCanvasNodeId) ids.add(selectedCanvasNodeId)
      return [...ids]
    },
    [nodes, selectedCanvasNodeId],
  )
  const selectedEdgeIds = useMemo(
    () => edges.filter((edge) => edge.selected).map((edge) => edge.id),
    [edges],
  )

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

  const saveNodeToAssetLibrary = useCallback(async () => {
    if (!currentProject?.id || !assetSaveRequest) return
    const name = assetSaveForm.name.trim()
    if (!name) {
      setAssetSaveError("请先填写资产标题")
      return
    }
    setAssetSaveLoading(true)
    setAssetSaveError(null)
    try {
      const source = `node:${assetSaveRequest.publicId ?? assetSaveRequest.nodeId}`
      const result = assetSaveForm.library === "shared"
        ? await callTool<Record<string, unknown>>("assets.save_to_shared", {
            project_id: currentProject.id,
            source,
            kind: assetSaveForm.kind,
            category: assetSaveForm.category,
            name,
          })
        : await callTool<Record<string, unknown>>("assets.save_to_project", {
            project_id: currentProject.id,
            source,
            kind: assetSaveForm.kind,
            episode: Number(assetSaveForm.episode || 1),
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
    if (!isPlaceableImageNode(node)) {
      clearGridDropPreview()
      return
    }
    const targetCell = findGridCellAtPoint(event.clientX, event.clientY, node.id)
    applyGridDropPreview(node.id, targetCell)
  }, [applyGridDropPreview, clearGridDropPreview, findGridCellAtPoint, isPlaceableImageNode])

  const handleNodeDragStart = useCallback((_event: MouseEvent, node: FlowNode) => {
    dragStartPositionsRef.current[node.id] = { x: node.position.x, y: node.position.y }
  }, [])

  const handleNodeDragStop = useCallback((event: MouseEvent, node: FlowNode) => {
    if (!currentProject?.id) {
      clearGridDropPreview()
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
      return
    }
    const previous = dragStartPositionsRef.current[node.id]
    delete dragStartPositionsRef.current[node.id]
    void updateNodePosition(currentProject.id, node.id, node.position).then(() => {
      if (previous && (Math.abs(previous.x - node.position.x) > 0.5 || Math.abs(previous.y - node.position.y) > 0.5)) {
        pushUndo({
          label: "移动节点",
          undo: async () => {
            if (!currentProject?.id) return
            await updateNodePosition(currentProject.id, node.id, previous)
          },
        })
      }
    }).catch((error) => {
      console.warn("Failed to persist node position", error)
    })
  }, [clearGridDropPreview, currentProject?.id, findGridCellAtPoint, isPlaceableImageNode, pushUndo])

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
    if (!isTouchPointer(event)) return
    startLongPress(event.pointerId, event.clientX, event.clientY, event.target)
  }, [startLongPress])

  const handleTouchStartCapture = useCallback((event: ReactTouchEvent<HTMLDivElement>) => {
    const point = touchPoint(event)
    if (!point) return
    startLongPress(-point.id - 1, point.x, point.y, event.target)
  }, [startLongPress])

  const handlePointerMoveCapture = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const state = longPressRef.current
    if (!state || state.pointerId !== event.pointerId || state.fired) return
    if (Math.hypot(event.clientX - state.x, event.clientY - state.y) > LONG_PRESS_MOVE_TOLERANCE) {
      clearLongPress()
    }
  }, [clearLongPress])

  const handleTouchMoveCapture = useCallback((event: ReactTouchEvent<HTMLDivElement>) => {
    const point = touchPoint(event)
    const state = longPressRef.current
    if (!point || !state || state.pointerId !== -point.id - 1 || state.fired) return
    if (Math.hypot(point.x - state.x, point.y - state.y) > LONG_PRESS_MOVE_TOLERANCE) {
      clearLongPress()
    }
  }, [clearLongPress])

  const handlePointerEndCapture = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const state = longPressRef.current
    if (!state || state.pointerId !== event.pointerId) return
    const fired = state.fired
    clearLongPress()
    if (fired) {
      event.preventDefault()
      event.stopPropagation()
    }
  }, [clearLongPress])

  const handleTouchEndCapture = useCallback((event: ReactTouchEvent<HTMLDivElement>) => {
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

  const handleCreateNode = useCallback(async (type: CanvasNodeType) => {
    if (!currentProject?.id || !contextMenu) return
    const menu = contextMenu
    const title = {
      text: "文本节点",
      image: "图片节点",
      video: "视频节点",
      audio: "音频节点",
    }[type]
    setContextMenu(null)
    try {
      const raw = await createProjectNode(currentProject.id, {
        type,
        title,
        x: menu.flowX,
        y: menu.flowY,
      })
      const id = String(raw.id ?? "")
      if (!id) return
      const position = { x: Number(raw.position_x ?? menu.flowX), y: Number(raw.position_y ?? menu.flowY) }
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
        label: menu.connectFrom ? "创建并连接节点" : "创建节点",
        undo: async () => {
          if (!currentProject?.id) return
          if (connectedEdgeId) {
            await deleteProjectEdge(currentProject.id, connectedEdgeId).catch(() => undefined)
          }
          await deleteProjectNode(currentProject.id, id)
        },
      })
      selectNode(null)
    } catch (error) {
      console.warn("Failed to create canvas node", error)
    }
  }, [addNode, connectNodes, contextMenu, currentProject?.id, pushUndo, removeEdges, replaceEdgeId, selectNode])

  const deleteCanvasItems = useCallback(async (nodeIdsInput: string[], edgeIdsInput: string[]) => {
    if (!currentProject?.id) return
    const nodeIds = [...new Set(nodeIdsInput)]
    const nodeIdSet = new Set(nodeIds)
    const edgeIds = [...new Set(edgeIdsInput)]
      .filter((edgeId) => !edgeId.startsWith("manual-"))
      .filter((edgeId) => {
        const edge = edges.find((item) => item.id === edgeId)
        return !edge || (!nodeIdSet.has(edge.source) && !nodeIdSet.has(edge.target))
      })
    if (nodeIds.length === 0 && edgeIds.length === 0) return
    const deletedEdgeSnapshots: CanvasEdgeSnapshot[] = edges
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
      await Promise.all([
        ...nodeIds.map((nodeId) => deleteProjectNode(currentProject.id, nodeId)),
        ...edgeIds.map((edgeId) => {
          const edge = edges.find((item) => item.id === edgeId)
          return deleteProjectEdge(currentProject.id, edgeId, edge ? {
            sourceNodeId: edge.source,
            targetNodeId: edge.target,
          } : undefined)
        }),
      ])
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
  }, [currentProject?.id, edges, pushUndo, removeEdges, removeNodes])

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

  // 项目级长连 SSE — 接收后台任务完成的画布事件,即使 chat stream 已结束也能刷新
  useEffect(() => {
    if (!currentProject?.id || streaming) return
    const url = `${getApiBaseSync()}/api/chat/events/${currentProject.id}`
    const es = new EventSource(url)
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        if (ev.type === "canvas_action" && ev.payload) {
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
  }, [currentProject?.id, streaming, applyCanvasAction])

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
    const targetType = String(nodes.find((node) => node.id === nodeId)?.data?.type ?? "")
    const action = targetType === "image" ? "render" : "force"
    updateCanvasNode(nodeId, { status: "running", error: undefined, error_message: undefined })
    try {
      const result = await callTool<Record<string, unknown>>("node.run", {
        project_id: currentProject.id,
        node_id: nodeId,
        action,
      })
      if (result && result.ok === false) throw new Error(String(result.error || "重新生成失败"))
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

  const flowNodes = useMemo(
    () => groupedNodeIdSet.size === 0
      ? nodes
      : nodes.map((node) => groupedNodeIdSet.has(node.id) ? { ...node, draggable: false } : node),
    [groupedNodeIdSet, nodes],
  )

  return (
    <div
      ref={canvasContainerRef}
      className="relative h-full w-full bg-black select-none"
      onPointerDownCapture={handlePointerDownCapture}
      onPointerMoveCapture={handlePointerMoveCapture}
      onPointerUpCapture={handlePointerEndCapture}
      onPointerCancelCapture={handlePointerEndCapture}
      onTouchStartCapture={handleTouchStartCapture}
      onTouchMoveCapture={handleTouchMoveCapture}
      onTouchEndCapture={handleTouchEndCapture}
      onTouchCancelCapture={handleTouchEndCapture}
    >
      <div className="pointer-events-none absolute left-3 top-3 z-10 rounded-md border border-white/10 bg-[#11151d]/92 px-2.5 py-2 text-[11px] text-zinc-400 shadow-xl shadow-black/25 backdrop-blur sm:left-4 sm:top-4 sm:px-3 sm:text-xs">
        <span className="text-zinc-200">{nodes.length}</span> 个节点 · <span className="text-zinc-200">{edges.length}</span> 条连接
      </div>
      {nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center text-zinc-500">
          <div className="text-center">
            <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-md bg-white/[0.06] text-[12px] font-semibold tracking-tight text-zinc-300">WF</div>
            <div className="text-sm text-zinc-200">创作画布</div>
            <div className="text-xs mt-1 text-zinc-500">任务驱动的 text / image / video / audio 节点会显示在这里</div>
          </div>
        </div>
      )}
      <ReactFlow
        nodes={flowNodes}
        edges={edges}
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
          setContextMenu(null)
          setNodeActionMenu(null)
          selectNode(null)
        }}
        className="bg-black"
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

      <CanvasGroupLayer
        projectId={currentProject?.id}
        nodes={nodes}
        edges={edges}
        selectedNodeIds={selectedNodeIds}
        viewport={viewport}
        containerRef={canvasContainerRef}
        applyNodeChanges={applyNodeChanges}
        registerUndo={pushUndo}
        onClearSelection={() => selectNode(null)}
        onGroupedNodeIdsChange={handleGroupedNodeIdsChange}
      />

      {contextMenu?.previewLine && (
        <PendingConnectionPreview line={contextMenu.previewLine} />
      )}

      {contextMenu && (
        <div
          className="fixed z-[80] w-40 overflow-hidden rounded-md border border-white/10 bg-[#11151d]/96 py-1 text-sm text-zinc-200 shadow-2xl shadow-black/50 backdrop-blur"
          style={menuPositionStyle(contextMenu.x, contextMenu.y, 160, contextMenu.connectFrom ? 190 : 154)}
          onClick={(event) => event.stopPropagation()}
        >
          {contextMenu.connectFrom && (
            <div className="border-b border-white/10 px-3 py-2 text-[10px] font-medium uppercase tracking-[0.12em] text-cyan-200/80">
              创建并连接
            </div>
          )}
          {([
            ["text", "文本节点"],
            ["image", "图片节点"],
            ["video", "视频节点"],
            ["audio", "音频节点"],
          ] as const).map(([type, label]) => (
            <button
              key={type}
              type="button"
              className="block w-full px-3 py-2 text-left text-xs transition-colors hover:bg-white/10"
              onClick={() => void handleCreateNode(type)}
            >
              {label}
            </button>
          ))}
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
                目标库
                <select
                  value={assetSaveForm.library}
                  onChange={(event) => setAssetSaveForm((current) => ({
                    ...current,
                    library: event.target.value as AssetSaveForm["library"],
                    kind: event.target.value === "project" ? "scene" : "scene",
                  }))}
                  className="mt-1 h-8 w-full rounded-md border border-white/10 bg-black/28 px-2 text-xs text-zinc-100"
                >
                  <option value="shared">共享资产库</option>
                  <option value="project">项目资产库</option>
                </select>
              </label>
              <label className="block text-[11px] text-zinc-500">
                类型
                <select
                  value={assetSaveForm.kind}
                  onChange={(event) => setAssetSaveForm((current) => ({ ...current, kind: event.target.value }))}
                  className="mt-1 h-8 w-full rounded-md border border-white/10 bg-black/28 px-2 text-xs text-zinc-100"
                >
                  {(assetSaveForm.library === "shared" ? SHARED_ASSET_KINDS : PROJECT_ASSET_KINDS).map((kind) => (
                    <option key={kind} value={kind}>{kind}</option>
                  ))}
                </select>
              </label>
              {assetSaveForm.library === "shared" ? (
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
              ) : (
                <label className="block text-[11px] text-zinc-500">
                  集数
                  <input
                    value={assetSaveForm.episode}
                    type="number"
                    min="1"
                    onChange={(event) => setAssetSaveForm((current) => ({ ...current, episode: event.target.value }))}
                    className="mt-1 h-8 w-full rounded-md border border-white/10 bg-black/28 px-2 text-xs text-zinc-100"
                  />
                </label>
              )}
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
                disabled={assetSaveLoading || !assetSaveForm.name.trim() || (assetSaveForm.library === "shared" && !assetSaveForm.category.trim())}
                className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-950 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {assetSaveLoading ? "保存中" : "保存"}
              </button>
            </div>
          </div>
        </div>
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
              className="block w-full px-3 py-2.5 text-left text-xs transition-colors hover:bg-white/10"
              onClick={() => void handleDownloadImageFromMenu(nodeActionMenu.imageUrl!, nodeActionMenu.title)}
            >
              保存图片
            </button>
          )}
          <button
            type="button"
            className="block w-full px-3 py-2.5 text-left text-xs text-red-200 transition-colors hover:bg-red-500/12 hover:text-red-100"
            onClick={() => void handleDeleteNodeFromMenu(nodeActionMenu.nodeId)}
          >
            删除节点
          </button>
        </div>
      )}

      <AnimatePresence>
        {selectedCanvasNodeId && (
          <NodeDetailPanel
            key={selectedCanvasNodeId}
            nodeId={selectedCanvasNodeId}
            projectId={currentProject?.id}
            onClose={() => selectNode(null)}
            onRerun={handleRerun}
            presentation="modal"
          />
        )}
      </AnimatePresence>
    </div>
  )
}
