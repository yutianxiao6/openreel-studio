"use client"

import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react"
import { AnimatePresence, motion } from "framer-motion"
import type { Node } from "reactflow"
import { useViewModeStore } from "@/stores/viewModeStore"
import { useProjectStore } from "@/stores/projectStore"
import { useCanvasStore } from "@/stores/canvasStore"
import { useBlueprintStore } from "@/stores/blueprintStore"
import { summarizeAgentRoundToolResult, useChatStore, type ChecklistItem } from "@/stores/chatStore"
import {
  chatStreamAsync,
  callTool,
  getPanelLayout,
  resolveMediaUrl,
  setPanelLayout,
  type BlueprintStreamEvent,
  type ChatStreamEvent,
} from "@/lib/api"
import { getNodeStyle } from "../canvas/nodeStyles"
import NodeDetailPanel from "../canvas/NodeDetailPanel"

interface NodeSummary {
  id: string
  title: string
  type: string
  status: string
  version?: number
  supersedes_id?: string | null
  preview?: Record<string, unknown> | null
  prompt?: string | null
  blueprint_node_id?: string | null
  source_ids?: Record<string, unknown> | null
}

interface BlueprintTreePanelNode {
  id: string
  type: string
  title: string
  status?: string
  materialize?: boolean
  content?: string | null
  description?: string | null
  prompt?: string | null
  url?: string | null
  fields?: Record<string, unknown>
  references?: string[]
  depends_on?: string[]
  canvas_node?: NodeSummary | null
  children?: BlueprintTreePanelNode[]
}

interface BlueprintTreePanel {
  title?: string | null
  summary?: string | null
  status?: string | null
  tree_version?: number | null
  root?: BlueprintTreePanelNode | null
  by_id?: Record<string, { id: string; title: string; type?: string }>
}

interface SegmentBucket {
  segment_id: string
  info?: NodeSummary[]
  guests?: NodeSummary[]
  scenes?: NodeSummary[]
  shot_list?: NodeSummary[]
  storyboard_grid?: NodeSummary[]
  story_template?: NodeSummary[]
  video_prompt?: NodeSummary[]
  video_clip?: NodeSummary[]
  first_frames?: NodeSummary[]
  last_frames?: NodeSummary[]
  loose?: NodeSummary[]
}

interface EpisodeBucket {
  episode_number: number
  scripts?: NodeSummary[]
  reviews?: NodeSummary[]
  segment_plans?: NodeSummary[]
  cast_scene_plans?: NodeSummary[]
  scenes?: NodeSummary[]
  guests?: NodeSummary[]
  segments?: Record<string, SegmentBucket>
  exports?: NodeSummary[]
  loose?: NodeSummary[]
}

interface PanelGrid {
  global: {
    settings?: NodeSummary[]
    outlines?: NodeSummary[]
    characters_main?: NodeSummary[]
    characters_recurring?: NodeSummary[]
    scene_assets?: NodeSummary[]
    relationships?: NodeSummary[]
  }
  episodes: Record<string, EpisodeBucket>
  exports?: NodeSummary[]
  unbucketed?: NodeSummary[]
}

interface PanelData {
  ok: boolean
  mode: string
  grid: PanelGrid | Record<string, NodeSummary[]>
  blueprint_tree?: BlueprintTreePanel | null
  episode_order?: number[]
  node_count: number
}

type PanelLayoutMode = "tier" | "type" | "phase" | "status"

interface DeleteNodeResult {
  ok?: boolean
  id?: string
  error?: string
  cleared_all?: boolean
  deleted_node_ids?: unknown[]
}

type CanvasNodeData = {
  type?: string
  status?: string
  title?: string
  prompt?: string | null
  preview?: Record<string, unknown>
}

function isWorkflowSpecOnlyRound(event: ChatStreamEvent): boolean {
  if (event.type !== "agent_round") return false
  const agents = Array.isArray(event.tool_agents) ? event.tool_agents.map((item) => String(item).trim()).filter(Boolean) : []
  return agents.length > 0 && agents.every((agent) => agent === "workflow_spec")
}

function isWorkflowSpecToolEvent(event: ChatStreamEvent): boolean {
  if (event.type !== "tool_start" && event.type !== "tool_done") return false
  if (typeof event.agent === "string" && event.agent.trim() === "workflow_spec") return true
  return event.type === "tool_start" && String(event.tool || "") === "tool.execute" && /workflow_spec|工作流.*模板|流程图/.test(String(event.content || ""))
}

function isWorkflowSpecPreviewResult(result: unknown): boolean {
  if (!result || typeof result !== "object" || Array.isArray(result)) return false
  const outer = result as Record<string, unknown>
  const nested = outer.result && typeof outer.result === "object" && !Array.isArray(outer.result)
    ? outer.result as Record<string, unknown>
    : null
  const candidate = typeof outer.artifact_ref === "string" ? outer : nested
  return typeof candidate?.artifact_ref === "string" && candidate.artifact_ref.startsWith("workflow_spec:")
}

const LAYOUT_MODES: { mode: PanelLayoutMode; label: string }[] = [
  { mode: "tier", label: "剧集" },
  { mode: "type", label: "类型" },
  { mode: "phase", label: "阶段" },
  { mode: "status", label: "状态" },
]

const BLUEPRINT_STATUS: Record<string, string> = {
  missing: "无蓝图",
  draft: "草稿",
  pending_review: "待确认",
  active: "已确认",
  revision_pending: "修订中",
}

function arr<T>(v: T[] | undefined): T[] {
  return Array.isArray(v) ? v : []
}

function isPanelLayoutMode(v: string): v is PanelLayoutMode {
  return v === "tier" || v === "type" || v === "phase" || v === "status"
}

function canvasData(node?: Node): CanvasNodeData {
  return ((node?.data ?? {}) as CanvasNodeData)
}

function asObj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function pickUrl(v: unknown): string {
  const obj = asObj(v)
  if (!obj) return ""
  const direct = obj.local_url || obj.url || obj.remote_url
  if (typeof direct === "string" && direct) return resolveMediaUrl(direct)
  const stages = Array.isArray(obj.stages) ? obj.stages : []
  for (const stage of stages) {
    const item = asObj(stage)
    if (!item) continue
    const stageUrl = item.local_url || item.url || item.remote_url
    if (typeof stageUrl === "string" && stageUrl && /图|首帧|尾帧|模板|参考|image/i.test(String(item.name ?? ""))) {
      return resolveMediaUrl(stageUrl)
    }
  }
  return ""
}

function pickText(v: unknown): string {
  const obj = asObj(v)
  if (!obj) return ""
  for (const key of ["prompt", "summary", "identity", "description", "content"]) {
    const value = obj[key]
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  const timeline = Array.isArray(obj.timeline) ? obj.timeline : []
  if (timeline.length) {
    return timeline
      .slice(0, 3)
      .map((beat) => {
        const item = asObj(beat)
        return [item?.time, item?.camera, item?.subject_motion].filter(Boolean).join(" · ")
      })
      .filter(Boolean)
      .join("\n")
  }
  const cells = Array.isArray(obj.cells) ? obj.cells : []
  if (cells.length) {
    return cells
      .slice(0, 4)
      .map((cell) => {
        const item = asObj(cell)
        return [item?.time, item?.shot_type, item?.action].filter(Boolean).join(" · ")
      })
      .filter(Boolean)
      .join("\n")
  }
  const stages = Array.isArray(obj.stages) ? obj.stages : []
  for (const stage of stages) {
    const item = asObj(stage)
    if (typeof item?.prompt === "string" && item.prompt.trim()) return item.prompt.trim()
  }
  return ""
}

function statusText(status: string) {
  const map: Record<string, string> = {
    completed: "完成",
    running: "生成中",
    failed: "失败",
    created: "待运行",
    pending: "待执行",
    queued: "排队",
  }
  return map[status] || status || "未知"
}

function statusTone(status: string) {
  if (status === "completed") return "border-emerald-400/25 bg-emerald-400/10 text-emerald-200"
  if (status === "running") return "border-blue-400/25 bg-blue-400/10 text-blue-200"
  if (status === "failed") return "border-red-400/25 bg-red-400/10 text-red-200"
  return "border-white/10 bg-white/[0.04] text-zinc-400"
}

function collectPanelNodes(data: PanelData | null): NodeSummary[] {
  if (!data) return []
  const out: NodeSummary[] = []
  const seen = new Set<string>()
  const push = (nodes?: NodeSummary[]) => {
    for (const node of arr(nodes)) {
      if (!node?.id || seen.has(node.id)) continue
      seen.add(node.id)
      out.push(node)
    }
  }

  if (data.mode !== "tier") {
    Object.values(data.grid as Record<string, NodeSummary[]>).forEach(push)
    return out
  }

  const grid = data.grid as PanelGrid
  push(grid.global.settings)
  push(grid.global.outlines)
  push(grid.global.characters_main)
  push(grid.global.characters_recurring)
  push(grid.global.scene_assets)
  push(grid.global.relationships)
  push(grid.exports)
  push(grid.unbucketed)
  for (const episode of Object.values(grid.episodes ?? {})) {
    push(episode.scripts)
    push(episode.reviews)
    push(episode.segment_plans)
    push(episode.cast_scene_plans)
    push(episode.scenes)
    push(episode.guests)
    push(episode.exports)
    push(episode.loose)
    for (const segment of Object.values(episode.segments ?? {})) {
      push(segment.info)
      push(segment.guests)
      push(segment.scenes)
      push(segment.shot_list)
      push(segment.storyboard_grid)
      push(segment.story_template)
      push(segment.video_prompt)
      push(segment.video_clip)
      push(segment.first_frames)
      push(segment.last_frames)
      push(segment.loose)
    }
  }
  return out
}

function segmentNodes(segment: SegmentBucket): NodeSummary[] {
  return [
    ...arr(segment.info),
    ...arr(segment.guests),
    ...arr(segment.scenes),
    ...arr(segment.shot_list),
    ...arr(segment.storyboard_grid),
    ...arr(segment.story_template),
    ...arr(segment.first_frames),
    ...arr(segment.last_frames),
    ...arr(segment.video_prompt),
    ...arr(segment.video_clip),
    ...arr(segment.loose),
  ]
}

function episodeNodes(episode: EpisodeBucket): NodeSummary[] {
  return [
    ...arr(episode.scripts),
    ...arr(episode.reviews),
    ...arr(episode.segment_plans),
    ...arr(episode.cast_scene_plans),
    ...arr(episode.scenes),
    ...arr(episode.guests),
    ...arr(episode.exports),
    ...arr(episode.loose),
    ...Object.values(episode.segments ?? {}).flatMap(segmentNodes),
  ]
}

function nodeProgress(nodes: NodeSummary[]) {
  const total = nodes.length
  const completed = nodes.filter((node) => node.status === "completed").length
  const failed = nodes.filter((node) => node.status === "failed").length
  const running = nodes.filter((node) => node.status === "running").length
  return { total, completed, failed, running, ratio: total ? completed / total : 0 }
}

function deletedIdsFromResult(result: DeleteNodeResult, fallbackId: string): string[] {
  const ids = (result.deleted_node_ids ?? [])
    .map((id) => (typeof id === "string" ? id : ""))
    .filter(Boolean)
  if (ids.length) return ids
  if (typeof result.id === "string" && result.id) return [result.id]
  return [fallbackId]
}

function segmentSortKey(segment: SegmentBucket): number {
  const explicit = segment.info?.[0]?.source_ids?.segment_index
  if (typeof explicit === "number") return explicit
  const match = String(segment.segment_id || "").match(/(\d+)(?!.*\d)/)
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER
}

function nextChecklistLabel(items: ChecklistItem[]): string {
  const item = items.find((entry) => entry.status === "in_progress" || entry.status === "pending")
  return item?.title || "暂无待执行步骤"
}

function ProgressLine({ nodes }: { nodes: NodeSummary[] }) {
  const progress = nodeProgress(nodes)
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-28 overflow-hidden rounded-full bg-white/10">
        <div className="h-full rounded-full bg-emerald-300 transition-all" style={{ width: `${Math.round(progress.ratio * 100)}%` }} />
      </div>
      <span className="text-[11px] text-zinc-500">{progress.completed}/{progress.total || 0}</span>
      {progress.running > 0 ? <span className="text-[11px] text-blue-300">{progress.running} 生成中</span> : null}
      {progress.failed > 0 ? <span className="text-[11px] text-red-300">{progress.failed} 失败</span> : null}
    </div>
  )
}

function NodeThumb({
  node,
  canvasNode,
  onOpen,
  compact = false,
  disabled = false,
}: {
  node: NodeSummary
  canvasNode?: Node
  onOpen: (id: string) => void
  compact?: boolean
  disabled?: boolean
}) {
  const data = canvasData(canvasNode)
  const preview = (node.preview ?? data.preview) as Record<string, unknown> | undefined
  const img = pickUrl(preview)
  const style = getNodeStyle(node.type)
  const nodeStatus = data.status || node.status
  const isRunning = nodeStatus === "running"
  const isImageNode = node.type === "image" || /图|帧|分镜|image|frame|storyboard/i.test(`${node.title} ${style.label}`)
  const isVideoNode = node.type === "video" || /视频|成片|video|clip/i.test(`${node.title} ${style.label}`)
  const hasMediaPreview = Boolean(img) || isImageNode || isVideoNode
  const previewHeight = compact ? "h-24" : "h-36"
  const cardMinHeight = hasMediaPreview
    ? (compact ? "min-h-[150px]" : "min-h-[202px]")
    : (compact ? "min-h-[104px]" : "min-h-[122px]")

  return (
    <button
      onClick={() => {
        if (!disabled) onOpen(node.id)
      }}
      disabled={disabled}
      className={`group flex ${cardMinHeight} min-w-0 flex-col overflow-hidden rounded-md border bg-[#15181d] text-left shadow-sm transition hover:-translate-y-0.5 hover:border-white/25 ${
        nodeStatus === "failed" ? "border-red-400/60" : nodeStatus === "completed" ? "border-emerald-400/35" : "border-white/10"
      } ${disabled ? "cursor-default opacity-80 hover:translate-y-0 hover:brightness-100" : ""}`}
      title={`${style.label} · ${node.title}`}
    >
      {hasMediaPreview ? (
        <div className={`relative ${previewHeight} shrink-0 overflow-hidden bg-black/25`}>
          {img ? (
            <img
              src={img}
              alt=""
              className="h-full w-full object-cover"
              onError={(event) => {
                ;(event.currentTarget as HTMLImageElement).style.opacity = "0.15"
              }}
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center bg-black/20 text-[13px] font-semibold" style={{ color: style.color }}>
              {style.icon}
            </div>
          )}
          {isRunning ? (
            <div className="absolute inset-0 flex items-center justify-center bg-black/35">
              <span className="h-5 w-5 animate-spin rounded-full border-2 border-white/70 border-t-transparent" />
            </div>
          ) : null}
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1 flex-col justify-between gap-2 border-t border-white/10 p-2.5">
        <div className="line-clamp-2 text-[12px] font-medium leading-4 text-zinc-100">{node.title || style.label}</div>
        <div className="flex items-center gap-1.5 text-[10px]">
          <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: style.color }} />
          <span className="min-w-0 flex-1 truncate text-zinc-500">{style.label}</span>
          <span className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] ${statusTone(nodeStatus)}`}>
            {statusText(nodeStatus)}
          </span>
        </div>
      </div>
    </button>
  )
}

function treeText(node: BlueprintTreePanelNode): string {
  return [node.content, node.description, node.prompt]
    .map((value) => (typeof value === "string" ? value.trim() : ""))
    .find(Boolean) || ""
}

function resolveTreeId(id: string, ids: Set<string>): string | null {
  if (ids.has(id)) return id
  const stripped = id.startsWith("@") ? id.slice(1) : id
  if (ids.has(stripped)) return stripped
  const tagged = `@${id}`
  if (ids.has(tagged)) return tagged
  return null
}

function relationTitles(ids: string[] | undefined, byId: BlueprintTreePanel["by_id"] | undefined): string[] {
  return (ids || [])
    .map((id) => byId?.[id]?.title || byId?.[id.startsWith("@") ? id.slice(1) : `@${id}`]?.title || id)
    .filter(Boolean)
}

function treeRelationIds(node: BlueprintTreePanelNode): string[] {
  return [...(node.references || []), ...(node.depends_on || [])]
}

function treeRole(node: BlueprintTreePanelNode): string {
  const text = `${node.id} ${node.title} ${node.type} ${treeText(node)}`.toLowerCase()
  if (node.id === "root") return "root"
  if (/episode|ep\d|第\s*\d+\s*集|剧集/.test(text)) return "episode"
  if (/segment|seg\d|第\s*\d+\s*段|分段|片段/.test(text)) return "segment"
  if (/storyboard|分镜|keyframe|首帧|尾帧|template|故事模板/.test(text)) return "storyboard"
  if (/character|角色|人物|scene|场景|asset|资产/.test(text)) return "asset"
  if (node.type === "video") return "video"
  if (node.type === "image") return "image"
  return "text"
}

function treeSortKey(node: BlueprintTreePanelNode): string {
  const roleRank: Record<string, number> = {
    root: 0,
    text: 1,
    asset: 2,
    episode: 3,
    segment: 4,
    storyboard: 5,
    image: 6,
    video: 7,
  }
  const role = treeRole(node)
  const indexMatch = `${node.id} ${node.title}`.match(/(?:episode|ep|segment|seg|第)?[_\s-]*(\d+)/i)
  const index = indexMatch ? Number(indexMatch[1]).toString().padStart(4, "0") : "9999"
  return `${String(roleRank[role] ?? 9).padStart(2, "0")}-${index}-${node.title || node.id}`
}

function dependencyLanes(nodes: BlueprintTreePanelNode[]): BlueprintTreePanelNode[][] {
  const ids = new Set(nodes.map((node) => node.id).filter(Boolean))
  const byId = new Map(nodes.map((node) => [node.id, node]))
  const level = new Map<string, number>()

  const visit = (node: BlueprintTreePanelNode, stack: Set<string>): number => {
    if (level.has(node.id)) return level.get(node.id) || 0
    if (stack.has(node.id)) return 0
    stack.add(node.id)
    const deps = treeRelationIds(node)
      .map((id) => resolveTreeId(id, ids))
      .filter((id): id is string => Boolean(id))
    const value = deps.length
      ? Math.max(...deps.map((id) => {
          const dep = byId.get(id)
          return dep ? visit(dep, stack) + 1 : 0
        }))
      : 0
    stack.delete(node.id)
    level.set(node.id, value)
    return value
  }

  for (const node of nodes) visit(node, new Set())
  const lanes: BlueprintTreePanelNode[][] = []
  for (const node of [...nodes].sort((a, b) => treeSortKey(a).localeCompare(treeSortKey(b), "zh-CN"))) {
    const idx = level.get(node.id) || 0
    if (!lanes[idx]) lanes[idx] = []
    lanes[idx].push(node)
  }
  return lanes.filter((lane) => lane.length > 0)
}

function TreeRelationLine({
  label,
  ids,
  byId,
}: {
  label: string
  ids?: string[]
  byId?: BlueprintTreePanel["by_id"]
}) {
  const titles = relationTitles(ids, byId)
  if (!titles.length) return null
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-zinc-500">
      <span>{label}</span>
      {titles.slice(0, 6).map((title) => (
        <span key={`${label}-${title}`} className="max-w-[180px] truncate rounded border border-white/10 bg-black/20 px-1.5 py-0.5 text-zinc-400">
          {title}
        </span>
      ))}
      {titles.length > 6 ? <span>+{titles.length - 6}</span> : null}
    </div>
  )
}

const MIND_CARD_WIDTH = 220
const MIND_CARD_HEIGHT = 96
const MIND_COL_GAP = 112
const MIND_ROW_GAP = 24
const MIND_PADDING = 32

interface MindMapNode {
  key: string
  node: BlueprintTreePanelNode
  x: number
  y: number
  depth: number
  parentId?: string
  isRoot?: boolean
}

interface MindMapEdge {
  from: string
  to: string
  kind: "hierarchy" | "dependency" | "reference"
}

interface MindMapLayout {
  nodes: MindMapNode[]
  edges: MindMapEdge[]
  width: number
  height: number
}

function sortedChildren(node: BlueprintTreePanelNode): BlueprintTreePanelNode[] {
  return [...(Array.isArray(node.children) ? node.children : [])].sort((a, b) => treeSortKey(a).localeCompare(treeSortKey(b), "zh-CN"))
}

function addRelationEdges(nodes: MindMapNode[], edges: MindMapEdge[]) {
  const ids = new Set(nodes.map((item) => item.node.id))
  const existing = new Set(edges.map((edge) => `${edge.kind}:${edge.from}->${edge.to}`))
  for (const item of nodes) {
    if (item.isRoot) continue
    for (const rawId of item.node.references || []) {
      const from = resolveTreeId(rawId, ids)
      if (!from || from === item.node.id || from === item.parentId) continue
      const key = `reference:${from}->${item.node.id}`
      if (!existing.has(key)) {
        existing.add(key)
        edges.push({ from, to: item.node.id, kind: "reference" })
      }
    }
    for (const rawId of item.node.depends_on || []) {
      const from = resolveTreeId(rawId, ids)
      if (!from || from === item.node.id || from === item.parentId) continue
      const key = `dependency:${from}->${item.node.id}`
      if (!existing.has(key)) {
        existing.add(key)
        edges.push({ from, to: item.node.id, kind: "dependency" })
      }
    }
  }
}

function layoutNestedMindMap(root: BlueprintTreePanelNode): MindMapLayout {
  const nodes: MindMapNode[] = []
  const edges: MindMapEdge[] = []
  let cursorY = MIND_PADDING
  let maxDepth = 0

  const place = (node: BlueprintTreePanelNode, depth: number, parentId?: string): number => {
    const children = sortedChildren(node)
    let y = cursorY
    if (children.length) {
      const childYs = children.map((child) => place(child, depth + 1, node.id))
      y = (childYs[0] + childYs[childYs.length - 1]) / 2
    } else {
      cursorY += MIND_CARD_HEIGHT + MIND_ROW_GAP
    }
    maxDepth = Math.max(maxDepth, depth)
    nodes.push({
      key: node.id,
      node,
      x: MIND_PADDING + depth * (MIND_CARD_WIDTH + MIND_COL_GAP),
      y,
      depth,
      parentId,
      isRoot: node.id === "root",
    })
    if (parentId) edges.push({ from: parentId, to: node.id, kind: "hierarchy" })
    return y
  }

  place(root, 0)
  addRelationEdges(nodes, edges)
  return {
    nodes,
    edges,
    width: MIND_PADDING * 2 + (maxDepth + 1) * MIND_CARD_WIDTH + maxDepth * MIND_COL_GAP,
    height: Math.max(cursorY + MIND_PADDING, MIND_CARD_HEIGHT + MIND_PADDING * 2),
  }
}

function layoutFlatMindMap(root: BlueprintTreePanelNode, children: BlueprintTreePanelNode[]): MindMapLayout {
  const lanes = dependencyLanes(children)
  const maxRows = Math.max(1, ...lanes.map((lane) => lane.length))
  const mapHeight = MIND_PADDING * 2 + maxRows * MIND_CARD_HEIGHT + (maxRows - 1) * MIND_ROW_GAP
  const nodes: MindMapNode[] = [{
    key: root.id,
    node: root,
    x: MIND_PADDING,
    y: MIND_PADDING + (mapHeight - MIND_PADDING * 2 - MIND_CARD_HEIGHT) / 2,
    depth: 0,
    isRoot: true,
  }]
  const edges: MindMapEdge[] = []
  const childIds = new Set(children.map((node) => node.id))

  lanes.forEach((lane, laneIndex) => {
    const laneHeight = lane.length * MIND_CARD_HEIGHT + Math.max(0, lane.length - 1) * MIND_ROW_GAP
    const yOffset = MIND_PADDING + (mapHeight - MIND_PADDING * 2 - laneHeight) / 2
    lane.forEach((node, rowIndex) => {
      nodes.push({
        key: node.id,
        node,
        x: MIND_PADDING + (laneIndex + 1) * (MIND_CARD_WIDTH + MIND_COL_GAP),
        y: yOffset + rowIndex * (MIND_CARD_HEIGHT + MIND_ROW_GAP),
        depth: laneIndex + 1,
        parentId: root.id,
      })
      const refs = (node.references || []).map((id) => resolveTreeId(id, childIds)).filter((id): id is string => Boolean(id))
      const deps = (node.depends_on || []).map((id) => resolveTreeId(id, childIds)).filter((id): id is string => Boolean(id))
      const linked = new Set([...refs, ...deps])
      if (linked.size === 0) {
        edges.push({ from: root.id, to: node.id, kind: "hierarchy" })
      } else {
        refs.forEach((from) => edges.push({ from, to: node.id, kind: "reference" }))
        deps.forEach((from) => edges.push({ from, to: node.id, kind: "dependency" }))
      }
    })
  })

  return {
    nodes,
    edges,
    width: MIND_PADDING * 2 + (lanes.length + 1) * MIND_CARD_WIDTH + lanes.length * MIND_COL_GAP,
    height: mapHeight,
  }
}

function buildMindMapLayout(root: BlueprintTreePanelNode): MindMapLayout {
  const children = sortedChildren(root)
  const hasNestedChildren = children.some((child) => sortedChildren(child).length > 0)
  if (hasNestedChildren) return layoutNestedMindMap(root)
  return layoutFlatMindMap(root, children)
}

function mindColumnLabel(depth: number, nodes: MindMapNode[]): string {
  if (depth === 0) return "剧情"
  const roles = new Set(nodes.filter((node) => node.depth === depth).map((node) => treeRole(node.node)))
  if (roles.has("episode")) return "剧集"
  if (roles.has("segment")) return "分段"
  if (roles.has("storyboard")) return "分镜 / 关键帧"
  if (roles.has("video")) return "视频"
  if (roles.has("asset") || roles.has("image")) return "资产"
  return `层级 ${depth}`
}

function MindMapCard({
  item,
  canvasById,
  onOpen,
}: {
  item: MindMapNode
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
}) {
  const { node, isRoot } = item
  const canvas = node.canvas_node || null
  const liveCanvas = canvas ? canvasById.get(canvas.id) : undefined
  const liveData = canvasData(liveCanvas)
  const nodeStatus = liveData.status || canvas?.status || node.status || (isRoot ? "active" : "pending")
  const style = getNodeStyle(isRoot ? "text" : node.type)
  const preview = canvas?.preview || liveData.preview || (node.url ? { url: node.url } : null)
  const img = pickUrl(preview)
  const canOpen = Boolean(canvas)
  const title = isRoot ? (node.title || "剧情") : (node.title || node.id)
  const roleLabel = isRoot ? "剧情" : getNodeStyle(node.type).label
  const hasVisual = Boolean(img) || node.type === "image" || node.type === "video"

  return (
    <div
      className={`absolute overflow-hidden rounded-md border bg-[#15181d] shadow-sm shadow-black/30 ${
        isRoot ? "border-emerald-300/35" : "border-white/10"
      }`}
      style={{
        left: item.x,
        top: item.y,
        width: MIND_CARD_WIDTH,
        height: MIND_CARD_HEIGHT,
        boxShadow: isRoot ? "0 0 0 1px rgba(16,185,129,0.12), 0 16px 40px rgba(0,0,0,0.26)" : undefined,
      }}
    >
      <button
        type="button"
        disabled={!canOpen}
        onClick={() => canvas && onOpen(canvas.id)}
        className={`flex h-full w-full gap-2.5 p-2.5 text-left ${canOpen ? "hover:bg-white/[0.035]" : "cursor-default"}`}
      >
        {hasVisual ? (
          <div className="relative h-[58px] w-[68px] shrink-0 overflow-hidden rounded border border-white/10 bg-black/25">
            {img ? (
              <img src={img} alt="" className="h-full w-full object-cover" />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-[13px] font-semibold" style={{ color: style.color }}>
                {style.icon}
              </div>
            )}
          </div>
        ) : null}
        <div className="flex min-w-0 flex-1 flex-col justify-between">
          <div className="flex items-center gap-1.5">
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: style.color }} />
            <span className="min-w-0 truncate text-[10px] text-zinc-500">{roleLabel}</span>
          </div>
          <div className="line-clamp-3 text-[12px] font-medium leading-4 text-zinc-100">{title}</div>
          <div className={`w-fit rounded-full border px-1.5 py-0.5 text-[9px] ${statusTone(nodeStatus)}`}>
            {statusText(nodeStatus)}
          </div>
        </div>
      </button>
    </div>
  )
}

function MindMapEdges({ layout }: { layout: MindMapLayout }) {
  const byId = new Map(layout.nodes.map((node) => [node.node.id, node]))
  const seen = new Set<string>()
  const paths = layout.edges
    .map((edge) => {
      const from = byId.get(edge.from)
      const to = byId.get(edge.to)
      if (!from || !to) return null
      const key = `${edge.kind}:${edge.from}->${edge.to}`
      if (seen.has(key)) return null
      seen.add(key)
      const x1 = from.x + MIND_CARD_WIDTH
      const y1 = from.y + MIND_CARD_HEIGHT / 2
      const x2 = to.x
      const y2 = to.y + MIND_CARD_HEIGHT / 2
      const dx = Math.max(64, Math.abs(x2 - x1) * 0.45)
      const path = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
      return { key, path, kind: edge.kind }
    })
    .filter((item): item is { key: string; path: string; kind: MindMapEdge["kind"] } => Boolean(item))

  return (
    <svg className="pointer-events-none absolute inset-0" width={layout.width} height={layout.height} aria-hidden>
      {paths.map((item) => (
        <path
          key={item.key}
          d={item.path}
          fill="none"
          stroke={item.kind === "hierarchy" ? "rgba(161,161,170,0.42)" : item.kind === "dependency" ? "rgba(34,211,238,0.48)" : "rgba(244,114,182,0.42)"}
          strokeWidth={item.kind === "hierarchy" ? 1.6 : 1.2}
          strokeDasharray={item.kind === "hierarchy" ? undefined : "5 5"}
        />
      ))}
    </svg>
  )
}

function BlueprintTreeLayout({
  tree,
  canvasById,
  onOpen,
}: {
  tree: BlueprintTreePanel
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
}) {
  const root = tree.root
  const children = Array.isArray(root?.children) ? root.children : []
  if (!root || children.length === 0) return null
  const layout = buildMindMapLayout({
    ...root,
    title: root.title || tree.title || "剧情",
    content: root.content || tree.summary || "",
  })
  const depths = Array.from(new Set(layout.nodes.map((node) => node.depth))).sort((a, b) => a - b)
  return (
    <section className="overflow-hidden rounded-lg border border-white/10 bg-[#0f1217] shadow-xl shadow-black/20">
      <div className="border-b border-white/10 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-[11px] uppercase tracking-[0.16em] text-zinc-500">Blueprint Mind Map</div>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <h2 className="min-w-0 text-lg font-semibold text-zinc-100">{tree.title || root.title || "蓝图树"}</h2>
              {tree.tree_version != null ? (
                <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-zinc-500">
                  v{tree.tree_version}
                </span>
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-3 text-[10px] text-zinc-500">
            <span className="inline-flex items-center gap-1"><span className="h-px w-6 bg-zinc-500/60" />层级</span>
            <span className="inline-flex items-center gap-1"><span className="h-px w-6 border-t border-dashed border-cyan-300/60" />依赖</span>
            <span className="inline-flex items-center gap-1"><span className="h-px w-6 border-t border-dashed border-pink-300/60" />引用</span>
          </div>
        </div>
        {(tree.summary || root.content) ? (
          <div className="mt-2 max-w-5xl text-[12px] leading-5 text-zinc-500">{tree.summary || root.content}</div>
        ) : null}
      </div>
      <div className="overflow-auto">
        <div className="relative" style={{ width: layout.width, height: layout.height + 34 }}>
          <div className="absolute left-0 top-0 h-8" style={{ width: layout.width }}>
            {depths.map((depth) => {
              const x = MIND_PADDING + depth * (MIND_CARD_WIDTH + MIND_COL_GAP)
              return (
                <div key={depth} className="absolute top-2 text-[10px] font-medium uppercase tracking-[0.14em] text-zinc-600" style={{ left: x, width: MIND_CARD_WIDTH }}>
                  {mindColumnLabel(depth, layout.nodes)}
                </div>
              )
            })}
          </div>
          <div className="absolute left-0 top-8" style={{ width: layout.width, height: layout.height }}>
            <MindMapEdges layout={layout} />
            {layout.nodes.map((item) => (
              <MindMapCard
                key={item.key}
                item={item}
                canvasById={canvasById}
                onOpen={onOpen}
              />
            ))}
          </div>
        </div>
      </div>
      <div className="border-t border-white/10 px-4 py-2">
        <div className="flex flex-wrap items-center gap-2 text-[10px] text-zinc-500">
          {children.slice(0, 8).map((child) => (
            <span key={child.id} className="rounded border border-white/10 bg-black/20 px-1.5 py-0.5">
              {child.title || child.id}
            </span>
          ))}
          {children.length > 8 ? <span>+{children.length - 8}</span> : null}
        </div>
      </div>
    </section>
  )
}

function LegacyTreeNodeCard({
  node,
  byId,
  canvasById,
  onOpen,
  depth = 0,
}: {
  node: BlueprintTreePanelNode
  byId?: BlueprintTreePanel["by_id"]
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
  depth?: number
}) {
  const canvas = node.canvas_node || null
  const summary: NodeSummary = canvas || {
    id: node.id,
    title: node.title || node.id,
    type: node.type || "text",
    status: node.status || "pending",
    prompt: node.prompt || node.description || node.content || null,
    blueprint_node_id: node.id,
  }
  const children = Array.isArray(node.children) ? node.children : []
  const fields = node.fields || {}
  const specs = [
    fields.purpose,
    fields.duration_seconds || fields.duration,
    fields.aspect_ratio,
    fields.resolution,
    fields.quality,
    fields.production_path,
  ]
    .map((item) => (item == null ? "" : String(item)))
    .filter(Boolean)
  const text = treeText(node)

  return (
    <div className={`rounded-lg border border-white/10 bg-white/[0.025] p-3 ${depth > 0 ? "ml-0 sm:ml-4" : ""}`}>
      <div className="grid gap-3 lg:grid-cols-[minmax(180px,220px)_minmax(0,1fr)]">
        <NodeThumb
          node={summary}
          canvasNode={canvas ? canvasById.get(canvas.id) : undefined}
          onOpen={onOpen}
          compact
          disabled={!canvas}
        />
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded border border-white/10 bg-black/20 px-1.5 py-0.5 text-[10px] text-zinc-500">
              {node.type}
            </span>
            <div className="min-w-0 flex-1 truncate text-sm font-medium text-zinc-100">{node.title || node.id}</div>
            {canvas ? (
              <span className={`rounded-full border px-1.5 py-0.5 text-[10px] ${statusTone(canvas.status)}`}>
                {statusText(canvas.status)}
              </span>
            ) : null}
          </div>
          {specs.length ? (
            <div className="flex flex-wrap gap-1.5">
              {specs.slice(0, 6).map((item) => (
                <span key={item} className="rounded bg-black/20 px-1.5 py-0.5 text-[10px] text-zinc-500">
                  {item}
                </span>
              ))}
            </div>
          ) : null}
          {text ? <div className="line-clamp-3 text-[12px] leading-5 text-zinc-400">{text}</div> : null}
          <TreeRelationLine label="引用" ids={node.references} byId={byId} />
          <TreeRelationLine label="依赖" ids={node.depends_on} byId={byId} />
        </div>
      </div>
      {children.length ? (
        <div className="mt-3 space-y-3 border-l border-white/10 pl-3">
          {children.map((child) => (
            <LegacyTreeNodeCard
              key={child.id}
              node={child}
              byId={byId}
              canvasById={canvasById}
              onOpen={onOpen}
              depth={depth + 1}
            />
          ))}
        </div>
      ) : null}
    </div>
  )
}

function LegacyBlueprintTreeLayout({
  tree,
  canvasById,
  onOpen,
}: {
  tree: BlueprintTreePanel
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
}) {
  const root = tree.root
  const children = Array.isArray(root?.children) ? root.children : []
  if (!root || children.length === 0) return null
  const hasNestedChildren = children.some((child) => Array.isArray(child.children) && child.children.length > 0)
  const lanes = hasNestedChildren ? [] : dependencyLanes(children)
  return (
    <section className="rounded-lg border border-white/10 bg-[#0f1217] p-4 shadow-xl shadow-black/20">
      <div className="mb-4">
        <div className="text-[11px] uppercase tracking-[0.16em] text-zinc-500">Blueprint Tree</div>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <h2 className="min-w-0 text-lg font-semibold text-zinc-100">{tree.title || root.title || "蓝图树"}</h2>
          {tree.tree_version != null ? (
            <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-zinc-500">
              v{tree.tree_version}
            </span>
          ) : null}
        </div>
        {(tree.summary || root.content) ? (
          <div className="mt-2 max-w-5xl text-[12px] leading-5 text-zinc-500">{tree.summary || root.content}</div>
        ) : null}
      </div>
      {hasNestedChildren ? (
        <div className="space-y-3">
          {children.map((child) => (
            <LegacyTreeNodeCard
              key={child.id}
              node={child}
              byId={tree.by_id}
              canvasById={canvasById}
              onOpen={onOpen}
            />
          ))}
        </div>
      ) : (
        <div className="space-y-4">
          {lanes.map((lane, index) => (
            <div key={index} className="rounded-lg border border-white/10 bg-white/[0.02] p-3">
              <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                  <div className="text-xs font-medium text-zinc-200">{mindColumnLabel(index, [])}</div>
                  <div className="mt-0.5 text-[11px] text-zinc-500">按 references / depends_on 自动排列</div>
                </div>
                <ProgressLine nodes={lane.map((node) => node.canvas_node).filter(Boolean) as NodeSummary[]} />
              </div>
              <div className="grid gap-3 xl:grid-cols-2">
                {lane.map((child) => (
                  <LegacyTreeNodeCard
                    key={child.id}
                    node={child}
                    byId={tree.by_id}
                    canvasById={canvasById}
                    onOpen={onOpen}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function NodeRail({
  label,
  nodes,
  canvasById,
  onOpen,
  emptyLabel,
  compact = false,
}: {
  label: string
  nodes: NodeSummary[]
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
  emptyLabel: string
  compact?: boolean
}) {
  return (
    <div className="min-h-[126px] rounded-lg border border-white/10 bg-white/[0.025] p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-xs font-medium text-zinc-200">{label}</div>
          <div className="mt-0.5 text-[11px] text-zinc-500">{nodes.length ? `${nodes.length} 个产物` : emptyLabel}</div>
        </div>
        {nodes.length ? <ProgressLine nodes={nodes} /> : null}
      </div>
      {nodes.length ? (
        <div
          className="grid gap-2"
          style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${compact ? 138 : 178}px, 1fr))` }}
        >
          {nodes.map((node) => (
            <NodeThumb key={node.id} node={node} canvasNode={canvasById.get(node.id)} onOpen={onOpen} compact={compact} />
          ))}
        </div>
      ) : (
        <div className="flex h-[74px] items-center justify-center rounded-md border border-dashed border-white/10 bg-black/10 text-[11px] text-zinc-600">
          {emptyLabel}
        </div>
      )}
    </div>
  )
}

function SegmentProductionCard({
  episodeNumber,
  segment,
  canvasById,
  onOpen,
}: {
  episodeNumber: number
  segment: SegmentBucket
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
}) {
  const visualNodes = [
    ...arr(segment.storyboard_grid),
    ...arr(segment.shot_list),
    ...arr(segment.story_template),
    ...arr(segment.first_frames),
    ...arr(segment.last_frames),
  ]
  const allNodes = segmentNodes(segment)
  const segIndex = segmentSortKey(segment)
  const segmentLabel = Number.isFinite(segIndex) && segIndex !== Number.MAX_SAFE_INTEGER ? `第 ${segIndex} 段` : "连续段落"

  return (
    <div className="rounded-lg border border-white/10 bg-[#12151b] p-3 shadow-sm shadow-black/20">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-zinc-100">EP{String(episodeNumber).padStart(2, "0")} · {segmentLabel}</div>
          <div className="mt-0.5 text-[11px] text-zinc-500">剧情段落驱动视觉、提示词和成片产物</div>
        </div>
        <ProgressLine nodes={allNodes} />
      </div>
      <div className="grid gap-3 lg:grid-cols-2 2xl:grid-cols-4">
        <NodeRail
          label="分段剧情"
          nodes={[...arr(segment.info), ...arr(segment.scenes), ...arr(segment.guests)]}
          canvasById={canvasById}
          onOpen={onOpen}
          emptyLabel="从蓝图读取"
          compact
        />
        <NodeRail label="视觉制作" nodes={visualNodes} canvasById={canvasById} onOpen={onOpen} emptyLabel="等待视觉产物" />
        <NodeRail label="视频提示词" nodes={arr(segment.video_prompt)} canvasById={canvasById} onOpen={onOpen} emptyLabel="等待视觉锚点" compact />
        <NodeRail label="视频片段" nodes={arr(segment.video_clip)} canvasById={canvasById} onOpen={onOpen} emptyLabel="等待生成" compact />
      </div>
    </div>
  )
}

function EpisodeSection({
  episode,
  globalCharacters,
  canvasById,
  onOpen,
}: {
  episode: EpisodeBucket
  globalCharacters: NodeSummary[]
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
}) {
  const planningNodes = [
    ...arr(episode.scripts),
    ...arr(episode.reviews),
    ...arr(episode.segment_plans),
    ...arr(episode.cast_scene_plans),
    ...arr(episode.guests),
  ]
  const segments = Object.values(episode.segments ?? {}).sort((a, b) => segmentSortKey(a) - segmentSortKey(b))
  const allNodes = episodeNodes(episode)
  const fallbackSegment: SegmentBucket = {
    segment_id: `episode-${episode.episode_number}`,
    info: [...arr(episode.segment_plans), ...arr(episode.loose)],
    scenes: [],
    storyboard_grid: [],
    story_template: [],
    video_prompt: [],
    video_clip: arr(episode.exports),
  }

  return (
    <section className="rounded-lg border border-white/10 bg-[#0f1217] p-4 shadow-xl shadow-black/20">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-zinc-500">Episode</div>
          <h2 className="mt-1 text-lg font-semibold text-zinc-100">第 {episode.episode_number} 集制作流</h2>
        </div>
        <ProgressLine nodes={allNodes} />
      </div>
      <div className="grid gap-3 xl:grid-cols-[minmax(320px,0.95fr)_minmax(320px,1fr)_minmax(320px,1fr)]">
        <NodeRail label="剧本与规划" nodes={planningNodes} canvasById={canvasById} onOpen={onOpen} emptyLabel="等待剧本节点" />
        <NodeRail label="人物资产" nodes={globalCharacters} canvasById={canvasById} onOpen={onOpen} emptyLabel="等待人物图" />
        <NodeRail label="场景资产" nodes={arr(episode.scenes)} canvasById={canvasById} onOpen={onOpen} emptyLabel="等待场景图" />
      </div>
      <div className="mt-4 space-y-3">
        {(segments.length ? segments : [fallbackSegment]).map((segment) => (
          <SegmentProductionCard
            key={segment.segment_id}
            episodeNumber={episode.episode_number}
            segment={segment}
            canvasById={canvasById}
            onOpen={onOpen}
          />
        ))}
      </div>
    </section>
  )
}

function GlobalStrip({
  grid,
  canvasById,
  onOpen,
}: {
  grid: PanelGrid["global"]
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
}) {
  const globalGroups = [
    { label: "项目资料", nodes: [...arr(grid.settings), ...arr(grid.outlines), ...arr(grid.relationships)] },
    { label: "主角", nodes: arr(grid.characters_main) },
    { label: "常驻人物", nodes: arr(grid.characters_recurring) },
    { label: "公共场景", nodes: arr(grid.scene_assets) },
  ]
  if (globalGroups.every((group) => group.nodes.length === 0)) return null
  return (
    <section className="rounded-lg border border-white/10 bg-[#0f1217] p-4 shadow-xl shadow-black/20">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-zinc-500">Blueprint Assets</div>
          <h2 className="mt-1 text-lg font-semibold text-zinc-100">全局资产</h2>
        </div>
        <ProgressLine nodes={globalGroups.flatMap((group) => group.nodes)} />
      </div>
      <div className="grid gap-3 xl:grid-cols-4">
        {globalGroups.map((group) => (
          <NodeRail key={group.label} label={group.label} nodes={group.nodes} canvasById={canvasById} onOpen={onOpen} emptyLabel="等待产物" compact />
        ))}
      </div>
    </section>
  )
}

function FlatLayout({
  grid,
  canvasById,
  onOpen,
}: {
  grid: Record<string, NodeSummary[]>
  canvasById: Map<string, Node>
  onOpen: (id: string) => void
}) {
  const entries = Object.entries(grid)
    .filter(([, nodes]) => Array.isArray(nodes))
    .sort(([a], [b]) => a.localeCompare(b, "zh-CN"))

  if (!entries.length) return <div className="p-6 text-sm text-zinc-500">当前布局没有可展示分组。</div>

  return (
    <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
      {entries.map(([label, nodes]) => (
        <div key={label} className="min-h-[168px] rounded-lg border border-white/10 bg-white/[0.025] p-3">
          <div className="mb-3 flex items-center justify-between text-xs">
            <span className="font-medium text-zinc-300">{label}</span>
            <span className="text-zinc-500">{nodes.length}</span>
          </div>
          <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(178px, 1fr))" }}>
            {nodes.map((node) => (
              <NodeThumb key={node.id} node={node} canvasNode={canvasById.get(node.id)} onOpen={onOpen} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

export function ProjectPanel() {
  const { currentProject } = useProjectStore()
  const { mode: viewMode } = useViewModeStore()
  const canvasNodes = useCanvasStore((s) => s.nodes)
  const applyCanvasAction = useCanvasStore((s) => s.applyCanvasAction)
  const updateCanvasNode = useCanvasStore((s) => s.updateNode)
  const appendMessage = useChatStore((s) => s.appendMessage)
  const setStreaming = useChatStore((s) => s.setStreaming)
  const streaming = useChatStore((s) => s.streaming)
  const addAgentRound = useChatStore((s) => s.addAgentRound)
  const addAgentRoundToolStart = useChatStore((s) => s.addAgentRoundToolStart)
  const addAgentRoundToolResult = useChatStore((s) => s.addAgentRoundToolResult)
  const completeAgentRound = useChatStore((s) => s.completeAgentRound)
  const addToolBubble = useChatStore((s) => s.addToolBubble)
  const updateToolBubble = useChatStore((s) => s.updateToolBubble)
  const activeChecklist = useChatStore((s) => s.activeChecklist)
  const setActiveChecklist = useChatStore((s) => s.setActiveChecklist)
  const blueprintStatus = useBlueprintStore((s) => s.status)
  const blueprint = useBlueprintStore((s) => s.blueprint)
  const applyBlueprintEvent = useBlueprintStore((s) => s.applyStreamEvent)
  const [data, setData] = useState<PanelData | null>(null)
  const [loading, setLoading] = useState(false)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [zoom, setZoom] = useState(0.92)
  const [layoutMode, setLayoutMode] = useState<PanelLayoutMode>("tier")

  const canvasById = useMemo(() => new Map(canvasNodes.map((node) => [node.id, node])), [canvasNodes])
  const allPanelNodes = useMemo(() => collectPanelNodes(data), [data])
  const panelNodeById = useMemo(() => new Map(allPanelNodes.map((node) => [node.id, node])), [allPanelNodes])
  const progress = nodeProgress(allPanelNodes)

  const fetchPanel = useCallback(async () => {
    if (!currentProject) return
    setLoading(true)
    try {
      const result = await getPanelLayout<PanelData>(currentProject.id)
      if (isPanelLayoutMode(result.mode)) setLayoutMode(result.mode)
      setData(result)
    } catch (error) {
      console.error("panel fetch error", error)
    } finally {
      setLoading(false)
    }
  }, [currentProject])

  const changeLayoutMode = useCallback(async (mode: PanelLayoutMode) => {
    if (!currentProject || mode === layoutMode) return
    setLayoutMode(mode)
    setLoading(true)
    try {
      const result = await setPanelLayout<PanelData>(currentProject.id, mode)
      if (isPanelLayoutMode(result.mode)) setLayoutMode(result.mode)
      setData(result)
    } catch (error) {
      console.error("panel layout error", error)
    } finally {
      setLoading(false)
    }
  }, [currentProject, layoutMode])

  const handleStreamEvent = useCallback((event: ChatStreamEvent) => {
    if (String(event.type).startsWith("blueprint_") && event.type !== "blueprint_tree_changed") {
      applyBlueprintEvent(event as BlueprintStreamEvent, currentProject?.id)
      void fetchPanel()
      return
    }
    if (event.type === "agent_round") {
      if (isWorkflowSpecOnlyRound(event)) return
      addAgentRound({
        round: Number(event.round),
        content: String(event.content ?? ""),
        source: event.source === "model" ? "model" : "action_summary",
        tools: Array.isArray(event.tools) ? event.tools.map(String) : [],
      })
      return
    }
    if (event.type === "agent_round_done") {
      completeAgentRound(Number(event.round))
      return
    }
    if (event.type === "tool_start" && event.tool) {
      if (isWorkflowSpecToolEvent(event)) return
      const tool = String(event.tool)
      addToolBubble(tool)
      addAgentRoundToolStart(tool, String(event.content || ""))
      return
    }
    if (event.type === "tool_done" && event.tool) {
      const result = event.result
      if (isWorkflowSpecPreviewResult(result) || isWorkflowSpecToolEvent(event)) return
      const resultObj = result as Record<string, unknown> | null
      const awaitingConfirmation = Boolean(resultObj?.requires_user_confirm) && !resultObj?.error
      const failed = Boolean(
        result && typeof result === "object" &&
        !awaitingConfirmation &&
        ("error" in result || ("ok" in result && result.ok === false))
      )
      updateToolBubble(String(event.tool), { status: failed ? "failed" : "completed" })
      addAgentRoundToolResult(summarizeAgentRoundToolResult(String(event.tool), result))
      return
    }
    if (event.type === "canvas_action") {
      applyCanvasAction(String(event.action ?? ""), (event.payload as Record<string, unknown>) ?? {})
      void fetchPanel()
      return
    }
    if (event.type === "checklist_updated") {
      if (Array.isArray(event.checklist)) {
        setActiveChecklist(event.checklist as ChecklistItem[])
      }
      void fetchPanel()
    }
  }, [
    addAgentRound,
    addAgentRoundToolResult,
    addAgentRoundToolStart,
    addToolBubble,
    applyBlueprintEvent,
    applyCanvasAction,
    completeAgentRound,
    currentProject?.id,
    fetchPanel,
    setActiveChecklist,
    updateToolBubble,
  ])

  const handleRerun = useCallback(async (nodeId: string) => {
    if (!currentProject || streaming) return
    const canvasType = String(canvasById.get(nodeId)?.data?.type ?? "")
    const panelType = String(panelNodeById.get(nodeId)?.type ?? "")
    const action = canvasType === "image" || panelType === "image" ? "render" : "force"
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
      throw error
    } finally {
      await fetchPanel()
    }
  }, [canvasById, currentProject, fetchPanel, panelNodeById, streaming, updateCanvasNode])

  const handleStoryRevision = useCallback(async (nodeId: string) => {
    if (!currentProject || streaming) return
    const target = panelNodeById.get(nodeId)
    const targetType = target?.type || ""
    const targetTitle = target?.title || "未命名节点"
    const revision = window.prompt(`修改剧情节点「${targetTitle}」\n\n请输入你要怎么改：`, "")?.trim()
    if (!revision) return
    const revisionMsg = [
      `请根据用户要求修改蓝图中的剧情源内容，目标节点 node_id=${nodeId}（类型：${targetType}，标题：${targetTitle}）。`,
      `修改要求：${revision}`,
      "必须先读取节点和项目蓝图，定位 blueprint_source_paths，创建 pending_blueprint_revision；不要直接修改剧情节点 output。修订草稿生成后请让用户确认。",
    ].join("\n")
    setSelectedNodeId(null)
    appendMessage({ role: "user", content: `修改剧情节点【${targetTitle}】：${revision}`, id: `${Date.now()}-u`, createdAt: new Date().toISOString() })
    appendMessage({ role: "assistant", content: "", id: `${Date.now()}-a`, createdAt: new Date().toISOString(), nodes: [] })
    setStreaming(true)
    try {
      await chatStreamAsync(currentProject.id, revisionMsg, handleStreamEvent)
    } finally {
      setStreaming(false)
      await fetchPanel()
    }
  }, [appendMessage, currentProject, fetchPanel, handleStreamEvent, panelNodeById, setStreaming, streaming])

  const handleDelete = useCallback(async (nodeId: string) => {
    const result = await callTool<DeleteNodeResult>("canvas.delete", { scope: "selected", node_ids: [nodeId] })
    if (result.error || result.ok === false) throw new Error(result.error || "节点删除失败")
    if (result.cleared_all) {
      applyCanvasAction("clear_all", {})
    } else {
      for (const id of deletedIdsFromResult(result, nodeId)) {
        applyCanvasAction("delete_node", { id })
      }
    }
    await fetchPanel()
  }, [applyCanvasAction, fetchPanel])

  useEffect(() => {
    if (viewMode === "panel") void fetchPanel()
  }, [viewMode, fetchPanel])

  useEffect(() => {
    if (viewMode !== "panel") return
    const timer = setTimeout(() => void fetchPanel(), 500)
    return () => clearTimeout(timer)
  }, [canvasNodes.length, viewMode, fetchPanel])

  if (!currentProject) {
    return <div className="flex h-full w-full items-center justify-center bg-[#0c0e11] text-zinc-500">准备中...</div>
  }

  if (loading && !data) {
    return <div className="flex h-full w-full items-center justify-center bg-[#0c0e11] text-zinc-500">加载工程面板...</div>
  }

  if (!data) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-[#0c0e11] text-zinc-500">
        <button onClick={fetchPanel} className="rounded-lg border border-white/10 bg-white/[0.04] px-4 py-2 text-sm text-zinc-200">
          加载工程面板
        </button>
      </div>
    )
  }

  const grid = data.mode === "tier" ? (data.grid as PanelGrid) : null
  const blueprintTree = data.mode === "tier" ? data.blueprint_tree : null
  const hasBlueprintTree = Boolean(blueprintTree?.root)
  const episodeOrder = grid
    ? data.episode_order ?? Object.keys(grid.episodes).map((key) => Number(key)).sort((a, b) => a - b)
    : []
  const globalCharacters = grid
    ? [...arr(grid.global.characters_main), ...arr(grid.global.characters_recurring)]
    : []
  const title = blueprint?.theme_title || currentProject.title || "未命名项目"
  const specs = [
    blueprint?.duration_seconds ? `${blueprint.duration_seconds}秒` : "",
    blueprint?.episode_count ? `${blueprint.episode_count}集` : "",
    blueprint?.segment_seconds ? `${blueprint.segment_seconds}秒/段` : "",
  ].filter(Boolean)

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-[#0c0e11]">
      <div className="border-b border-white/10 bg-[var(--studio-panel)] px-3 py-3 sm:px-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-[11px] uppercase tracking-[0.16em] text-zinc-500">Production Board</div>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <h1 className="min-w-0 truncate text-base font-semibold text-zinc-100">{title}</h1>
              <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-zinc-400">
                {BLUEPRINT_STATUS[blueprintStatus] || blueprintStatus || "未知"}
              </span>
              {specs.map((item) => (
                <span key={String(item)} className="rounded-full border border-white/10 bg-white/[0.035] px-2 py-0.5 text-[10px] text-zinc-400">
                  {String(item)}
                </span>
              ))}
            </div>
          </div>
          <div className="flex w-full flex-wrap items-center gap-2 text-xs text-zinc-500 sm:ml-auto sm:w-auto sm:gap-3">
            <div className="min-w-0 truncate">下一步：{nextChecklistLabel(activeChecklist)}</div>
            <ProgressLine nodes={allPanelNodes} />
            <span>{progress.total || data.node_count} 产物</span>
            <div className="flex rounded-md border border-white/10 bg-black/20 p-0.5">
              {LAYOUT_MODES.map((item) => (
                <button
                  key={item.mode}
                  onClick={() => changeLayoutMode(item.mode)}
                  disabled={loading || streaming}
                  className={`px-2 py-0.5 text-[11px] transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                    layoutMode === item.mode ? "rounded bg-zinc-100 text-zinc-950" : "text-zinc-500 hover:text-zinc-200"
                  }`}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <input
              type="range"
              min="0.7"
              max="1.15"
              step="0.05"
              value={zoom}
              onChange={(event) => setZoom(Number(event.target.value))}
              className="hidden w-24 accent-zinc-200 sm:block"
              aria-label="缩放工程面板"
            />
            <button
              onClick={fetchPanel}
              disabled={loading}
              className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-1 text-zinc-300 hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-45"
            >
              {loading ? "刷新中" : "刷新"}
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        {data.node_count === 0 && !hasBlueprintTree ? (
          <div className="flex h-full items-center justify-center text-zinc-500">
            <div className="rounded-lg border border-white/10 bg-white/[0.03] px-8 py-7 text-center">
              <div className="text-sm text-zinc-200">还没有视频工作流产物</div>
              <div className="mt-1 text-xs text-zinc-500">确认蓝图并开始制作后，人物、场景、分镜和视频提示词会进入这里。</div>
            </div>
          </div>
        ) : (
          <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }} className="p-3 sm:p-4">
            <div className="min-h-[620px] sm:min-h-[760px]" style={{ zoom } as CSSProperties}>
              {grid ? (
                <div className="space-y-4">
                  {blueprintTree?.root ? (
                    <BlueprintTreeLayout tree={blueprintTree} canvasById={canvasById} onOpen={setSelectedNodeId} />
                  ) : (
                    <>
                      <GlobalStrip grid={grid.global} canvasById={canvasById} onOpen={setSelectedNodeId} />
                      {episodeOrder.map((episodeNumber) => {
                        const episode = grid.episodes[String(episodeNumber)]
                        if (!episode) return null
                        return (
                          <EpisodeSection
                            key={episode.episode_number}
                            episode={episode}
                            globalCharacters={globalCharacters}
                            canvasById={canvasById}
                            onOpen={setSelectedNodeId}
                          />
                        )
                      })}
                      {grid.unbucketed && grid.unbucketed.length > 0 ? (
                        <div className="rounded-lg border border-white/10 bg-white/[0.025] p-3">
                          <div className="mb-2 text-xs text-zinc-500">草稿 / 未分类</div>
                          <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(178px, 1fr))" }}>
                            {grid.unbucketed.map((node) => (
                              <NodeThumb key={node.id} node={node} canvasNode={canvasById.get(node.id)} onOpen={setSelectedNodeId} />
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </>
                  )}
                </div>
              ) : (
                <FlatLayout grid={data.grid as Record<string, NodeSummary[]>} canvasById={canvasById} onOpen={setSelectedNodeId} />
              )}
            </div>
          </motion.div>
        )}
      </div>

      <AnimatePresence>
        {selectedNodeId ? (
          <NodeDetailPanel
            key={selectedNodeId}
            nodeId={selectedNodeId}
            projectId={currentProject.id}
            onClose={() => setSelectedNodeId(null)}
            onRerun={handleRerun}
            onRequestStoryRevision={handleStoryRevision}
            onDelete={handleDelete}
            actionDisabled={streaming || loading}
            presentation="modal"
          />
        ) : null}
      </AnimatePresence>
    </div>
  )
}

export default ProjectPanel
