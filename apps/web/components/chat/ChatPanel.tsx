"use client"

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react"
import { motion } from "framer-motion"
import {
  useChatStore,
  type ChatMessage,
  type AgentRound,
  type NodeBubble,
  type ToolBubble,
  type StepProgress,
  type PendingAttachment,
  type PlanDoc,
  type InteractionInputPayload,
  type PendingActionPayload,
  type ChecklistItem,
  type TokenUsageSnapshot,
  summarizeAgentRoundToolResult,
} from "@/stores/chatStore"
import { useBlueprintStore, type BlueprintTreeEvent } from "@/stores/blueprintStore"
import { useProjectStore, type ProjectRecord } from "@/stores/projectStore"
import { useCanvasStore } from "@/stores/canvasStore"
import { useViewModeStore } from "@/stores/viewModeStore"
import {
  chatStream,
  cancelChat,
  dequeueChat,
  enqueueChat,
  getChatQueueStatus,
  getAgentTokenUsage,
  uploadFile,
  listProjectAssets,
  callTool,
  api,
  requestWorkflowRefresh,
  getApiBaseSync,
  resolveMediaUrl,
  resolveAssetLibraryPreviewUrl,
  type ChatStreamEvent,
  type BlueprintStreamEvent,
  type UploadedAttachment,
  type ProjectAsset,
} from "@/lib/api"
import { ProposedPlanCard } from "./ProposedPlanCard"
import { PendingActionCard } from "./PendingActionCard"
import { InteractionInputCard } from "@/components/interaction/InteractionInputCard"
import { ChecklistPanel } from "./ChecklistPanel"
import { SlashMenu, filterSlashCommands, type SlashCommandDef } from "./SlashMenu"
import { MarkdownView } from "@/components/common/MarkdownView"
import { buildDecisionInputs } from "@/lib/decisionInputs"

const NODE_TYPE_LABEL: Record<string, string> = {
  text: "文本",
  image: "图片",
  video: "视频",
  audio: "音频",
}

const NODE_TYPE_ICON: Record<string, string> = {
  text: "TX",
  image: "IM",
  video: "VD",
  audio: "AU",
}

const APP_BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? ""
const APP_ICON_SRC = `${APP_BASE_PATH}/icon.png`

function attachmentMention(attachment: UploadedAttachment, index: number, imageIndex: number): string {
  const raw = attachment.mention || attachment.ref_label || attachment.display_label || ""
  if (raw.trim()) return raw.trim().startsWith("@") ? raw.trim() : `@${raw.trim()}`
  return attachment.kind === "image" ? `@图${imageIndex}` : `@附件${index}`
}

function decorateAttachmentsForSend(attachments: UploadedAttachment[]): UploadedAttachment[] {
  let imageIndex = 0
  return attachments.map((attachment, index) => {
    if (attachment.kind === "image") imageIndex += 1
    const mention = attachmentMention(attachment, index + 1, Math.max(1, imageIndex))
    return {
      ...attachment,
      mention,
      ref_label: mention.slice(1),
      display_label: mention,
    }
  })
}

function attachmentDisplayLines(attachments: UploadedAttachment[]): string {
  return attachments
    .map((attachment) => `${attachment.mention || attachment.display_label || ""} 附件：${attachment.filename}`.trim())
    .join("\n")
}

function selectedCanvasNodeIdsForPrompt(): string[] {
  const { nodes, selectedNodeId } = useCanvasStore.getState()
  const ids = new Set<string>()
  nodes.forEach((node) => {
    if (node.selected) ids.add(node.id)
  })
  if (selectedNodeId) ids.add(selectedNodeId)
  return [...ids]
}

function attachmentPreviewUrl(projectId: string | undefined, attachment: UploadedAttachment): string {
  if (attachment.url) return resolveMediaUrl(attachment.url)
  if (!projectId || !attachment.rel_path) return ""
  return resolveMediaUrl(`/api/uploads/${projectId}/file/${attachment.rel_path}`)
}

type SlashRunStatus = {
  command: string
  action?: string
  status: "running" | "completed" | "failed"
  message?: string
}

type AgentCollaborationMode = "default" | "plan" | "workflow_build"

const TOKEN_MONITOR_SETTING_KEY = "ui.show_token_monitor"

const LOCAL_SLASH_COMMANDS = new Set([
  "/help",
  "/status",
  "/config",
  "/model",
  "/mcp",
  "/clear",
])

function parseSlashMeta(message: string): { command: string; action?: string } | null {
  const parts = message.trim().split(/\s+/).filter(Boolean)
  if (!parts[0]?.startsWith("/")) return null
  return {
    command: parts[0].toLowerCase(),
    action: parts[1]?.toLowerCase(),
  }
}

function isLocalSlashCommand(message: string): boolean {
  const command = parseSlashMeta(message)?.command
  return command ? LOCAL_SLASH_COMMANDS.has(command) : false
}

function slashCompletionText(cmd: SlashCommandDef): string {
  const insertText = cmd.insertText ?? cmd.name
  if (!cmd.usage && !cmd.insertOnly) return insertText
  return insertText.endsWith(" ") ? insertText : `${insertText} `
}

function wantsProjectSelectionCompletion(input: string): boolean {
  return /^\/project\s+(switch|delete)(?:\s|$)/i.test(input.trimStart())
}

function projectIdToken(project: ProjectRecord, projects: ProjectRecord[]): string {
  const id = String(project.id || "")
  if (!id) return ""
  for (const size of [8, 12, 16, 24, 36]) {
    const token = id.slice(0, size)
    if (projects.filter((item) => String(item.id || "").startsWith(token)).length === 1) {
      return token
    }
  }
  return id
}

function buildProjectSlashCompletions(
  projects: ProjectRecord[],
  currentProjectId?: string | null,
): SlashCommandDef[] {
  return projects.flatMap((project, index) => {
    const token = projectIdToken(project, projects)
    if (!token) return []
    const title = (project.title || "未命名项目").trim()
    const current = project.id === currentProjectId ? " · 当前" : ""
    const label = `#${index + 1} ${title}${current} · ${token}`
    const searchText = `${index + 1} ${title} ${project.id} ${token} ${current ? "current 当前" : ""}`
    return [
      {
        name: `/project switch ${token}`,
        description: label,
        insertOnly: true,
        searchText,
      },
      {
        name: `/project delete ${token}`,
        description: label,
        insertOnly: true,
        searchText,
      },
    ]
  })
}

function slashLabel(status: SlashRunStatus): string {
  const action = status.action ? ` ${status.action}` : ""
  return `${status.command}${action}`
}

function parseProjectStateJson(project: ProjectRecord | null): Record<string, unknown> {
  const raw = project?.state_json
  if (!raw) return {}
  if (typeof raw === "object" && !Array.isArray(raw)) return raw
  if (typeof raw !== "string") return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {}
  } catch {
    return {}
  }
}

function normalizeCollaborationMode(value: unknown): AgentCollaborationMode {
  const raw = String(value || "").trim().toLowerCase()
  if (raw === "plan" || raw === "workflow_build") return raw
  return "default"
}

function collaborationModeFromProject(project: ProjectRecord | null): AgentCollaborationMode {
  return normalizeCollaborationMode(parseProjectStateJson(project).agent_collaboration_mode)
}

function collaborationModeLabel(mode: AgentCollaborationMode): string {
  if (mode === "plan") return "Plan Mode"
  if (mode === "workflow_build") return "工作流搭建模式"
  return "默认制作模式"
}

function collaborationModeClass(mode: AgentCollaborationMode): string {
  if (mode === "plan") return "border-sky-400/25 bg-sky-500/10 text-sky-200"
  if (mode === "workflow_build") return "border-amber-300/25 bg-amber-500/10 text-amber-100"
  return "border-white/10 bg-white/[0.04] text-zinc-400"
}

const BLUEPRINT_EVENT_LABEL: Record<string, string> = {
  blueprint_draft_started: "开始生成项目蓝图",
  blueprint_section_started: "开始生成蓝图片段",
  blueprint_section_delta: "蓝图片段更新",
  blueprint_section_completed: "蓝图片段完成",
  blueprint_section_needs_revision: "蓝图片段需要修改",
  blueprint_draft_saved: "蓝图草稿已保存",
  blueprint_validation_completed: "蓝图校验完成",
  blueprint_proposed: "项目蓝图已生成",
  blueprint_approved: "项目蓝图已确认",
  blueprint_revision_proposed: "蓝图修改方案已生成",
  blueprint_revision_applied: "蓝图修改已应用",
  blueprint_cleared: "项目蓝图已清空",
}

const SILENT_BLUEPRINT_PROGRESS_EVENTS = new Set([
  "blueprint_section_started",
  "blueprint_section_delta",
  "blueprint_section_completed",
  "blueprint_draft_started",
])

function isBlueprintEvent(event: ChatStreamEvent): boolean {
  return String(event.type).startsWith("blueprint_")
}

function isInteractionInputEvent(event: ChatStreamEvent): boolean {
  return event.type === "interaction_input_requested"
}

function isTokenUsageEvent(event: ChatStreamEvent): event is {
  type: "token_usage"
  project_id: string
  run_id: string
  round?: number | null
	  phase?: string
	  usage: Record<string, unknown>
	  run_totals: Record<string, unknown>
	  session_totals: Record<string, unknown>
	  latest_call_tokens?: Record<string, unknown> | null
	  latest_call_context?: Record<string, unknown> | null
	  run_cumulative_tokens?: Record<string, unknown> | null
	  session_cumulative_tokens?: Record<string, unknown> | null
	  run_context_peak?: Record<string, unknown> | null
	  session_context_peak?: Record<string, unknown> | null
	} {
  const data = event as Record<string, unknown>
  return (
    event.type === "token_usage" &&
    typeof data.project_id === "string" &&
    typeof data.run_id === "string" &&
    data.usage !== null &&
    typeof data.usage === "object" &&
    !Array.isArray(data.usage) &&
    data.run_totals !== null &&
    typeof data.run_totals === "object" &&
    !Array.isArray(data.run_totals) &&
    data.session_totals !== null &&
    typeof data.session_totals === "object" &&
    !Array.isArray(data.session_totals)
  )
}

const RUN_EVENT_TYPES = new Set<string>([
  "agent_round",
  "agent_round_done",
  "subagent_round",
  "tool_start",
  "tool_done",
  "text_delta",
  "interaction_input_requested",
  "proposed_plan",
  "confirm_required",
  "queued",
  "merged_messages",
  "queued_turn_started",
  "cancel_requested",
])

function isRunProgressEvent(event: ChatStreamEvent): boolean {
  return RUN_EVENT_TYPES.has(String(event.type))
}

function isRunTerminalEvent(event: ChatStreamEvent): boolean {
  return event.type === "done" || event.type === "error" || event.type === "cancelled"
}

function numericUsageValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value)
  return null
}

function recordUsageValue(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
  return null
}

function formatTokenCount(value: number | null): string {
  if (value === null) return "unknown"
  return Math.round(value).toLocaleString("en-US")
}

function formatTokenPercent(value: number | null): string {
  if (value === null) return "unknown"
  return `${Math.round(value * 1000) / 10}%`
}

function tokenMonitorPhaseLabel(phase?: string): string {
  if (phase === "blueprint_generation") return "蓝图"
  if (phase === "agent_loop") return "Agent"
  if (phase === "trace_summary") return "历史"
  return phase || "LLM"
}

function TokenMonitorBar({
  snapshot,
  visible,
}: {
  snapshot: TokenUsageSnapshot | null
  visible: boolean
}) {
  if (!visible || !snapshot) return null

  const latestCallContext =
    snapshot.latestCallContext ??
    recordUsageValue(snapshot.usage.latest_call_context)
  const runContextPeak =
    snapshot.runContextPeak ??
    recordUsageValue(snapshot.runTotals.context_peak)
  const sessionContextPeak =
    snapshot.sessionContextPeak ??
    recordUsageValue(snapshot.sessionTotals.context_peak)

  const contextAvailableRate =
    numericUsageValue(latestCallContext?.context_available_rate) ??
    numericUsageValue(snapshot.usage.context_available_rate) ??
    numericUsageValue(sessionContextPeak?.context_available_rate) ??
    numericUsageValue(runContextPeak?.context_available_rate) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_available_rate) ??
    numericUsageValue(snapshot.runTotals.context_peak_available_rate) ??
    (() => {
      const usedRate =
        numericUsageValue(latestCallContext?.context_used_rate) ??
        numericUsageValue(snapshot.usage.context_used_rate) ??
        numericUsageValue(sessionContextPeak?.context_used_rate) ??
        numericUsageValue(runContextPeak?.context_used_rate) ??
        numericUsageValue(snapshot.sessionTotals.context_peak_used_rate) ??
        numericUsageValue(snapshot.runTotals.context_peak_used_rate)
      return usedRate === null ? null : Math.max(0, 1 - usedRate)
    })()
  const contextRemainingTokens =
    numericUsageValue(latestCallContext?.context_remaining_tokens) ??
    numericUsageValue(snapshot.usage.context_remaining_tokens) ??
    numericUsageValue(sessionContextPeak?.context_remaining_tokens) ??
    numericUsageValue(runContextPeak?.context_remaining_tokens) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_remaining_tokens) ??
    numericUsageValue(snapshot.runTotals.context_peak_remaining_tokens)
  const contextLimitTokens =
    numericUsageValue(latestCallContext?.context_limit_tokens) ??
    numericUsageValue(snapshot.usage.context_limit_tokens) ??
    numericUsageValue(sessionContextPeak?.context_limit_tokens) ??
    numericUsageValue(runContextPeak?.context_limit_tokens) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_limit_tokens) ??
    numericUsageValue(snapshot.runTotals.context_peak_limit_tokens)
  const contextTone =
    contextAvailableRate !== null && contextAvailableRate < 0.15
      ? "text-red-300"
      : contextAvailableRate !== null && contextAvailableRate < 0.3
        ? "text-amber-300"
        : "text-zinc-200"

  return (
    <div className="mb-2 flex items-center rounded-lg border border-white/10 bg-black/25 px-3 py-2 text-[11px] text-zinc-400">
      <span
        className={`font-medium ${contextTone}`}
        title="上下文窗口剩余容量；token 花费、输入输出和缓存命中率等明细请用 /status 查看。"
      >
        上下文剩余 {formatTokenPercent(contextAvailableRate)}
        {contextRemainingTokens !== null ? ` · ${formatTokenCount(contextRemainingTokens)}` : ""}
        {contextLimitTokens !== null ? ` / ${formatTokenCount(contextLimitTokens)}` : ""}
      </span>
    </div>
  )
}

function formatTokenStatus(snapshot: TokenUsageSnapshot | null): string {
  if (!snapshot) return "**Token / 上下文**\n- 暂无数据"

  const latestCallTokensView =
    snapshot.latestCallTokens ??
    recordUsageValue(snapshot.usage.latest_call_tokens)
  const latestCallContext =
    snapshot.latestCallContext ??
    recordUsageValue(snapshot.usage.latest_call_context)
  const runCumulative =
    snapshot.runCumulativeTokens ??
    recordUsageValue(snapshot.runTotals.cumulative_tokens) ??
    snapshot.runTotals
  const sessionCumulative =
    snapshot.sessionCumulativeTokens ??
    recordUsageValue(snapshot.sessionTotals.cumulative_tokens) ??
    snapshot.sessionTotals
  const runContextPeak =
    snapshot.runContextPeak ??
    recordUsageValue(snapshot.runTotals.context_peak)
  const sessionContextPeak =
    snapshot.sessionContextPeak ??
    recordUsageValue(snapshot.sessionTotals.context_peak)

  const runTokens = numericUsageValue(runCumulative.total_tokens)
  const runPromptTokens = numericUsageValue(runCumulative.prompt_tokens)
  const runCompletionTokens = numericUsageValue(runCumulative.completion_tokens)
  const runCachedTokens = numericUsageValue(runCumulative.cached_prompt_tokens)
  const sessionTokens = numericUsageValue(sessionCumulative.total_tokens)
  const latestCallTokens =
    numericUsageValue(latestCallTokensView?.total_tokens) ??
    numericUsageValue(snapshot.usage.total_tokens)
  const latestContextAvailableRate =
    numericUsageValue(latestCallContext?.context_available_rate) ??
    numericUsageValue(snapshot.usage.context_available_rate)
  const latestContextUsedRate =
    numericUsageValue(latestCallContext?.context_used_rate) ??
    numericUsageValue(snapshot.usage.context_used_rate)
  const totalContextAvailableRate =
    numericUsageValue(sessionContextPeak?.context_available_rate) ??
    numericUsageValue(runContextPeak?.context_available_rate) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_available_rate) ??
    numericUsageValue(snapshot.runTotals.context_peak_available_rate)
  const totalContextUsedRate =
    numericUsageValue(sessionContextPeak?.context_used_rate) ??
    numericUsageValue(runContextPeak?.context_used_rate) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_used_rate) ??
    numericUsageValue(snapshot.runTotals.context_peak_used_rate) ??
    (totalContextAvailableRate === null ? null : Math.max(0, 1 - totalContextAvailableRate))
  const totalContextRemainingTokens =
    numericUsageValue(sessionContextPeak?.context_remaining_tokens) ??
    numericUsageValue(runContextPeak?.context_remaining_tokens) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_remaining_tokens) ??
    numericUsageValue(snapshot.runTotals.context_peak_remaining_tokens)
  const totalContextLimitTokens =
    numericUsageValue(sessionContextPeak?.context_limit_tokens) ??
    numericUsageValue(runContextPeak?.context_limit_tokens) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_limit_tokens) ??
    numericUsageValue(snapshot.runTotals.context_peak_limit_tokens)
  const totalActiveInputTokens =
    numericUsageValue(sessionContextPeak?.active_input_tokens) ??
    numericUsageValue(runContextPeak?.active_input_tokens) ??
    numericUsageValue(snapshot.sessionTotals.context_peak_active_input_tokens) ??
    numericUsageValue(snapshot.runTotals.context_peak_active_input_tokens)
  const cacheHitRate =
    numericUsageValue(sessionCumulative.cache_hit_rate) ??
    numericUsageValue(runCumulative.cache_hit_rate) ??
    numericUsageValue(latestCallTokensView?.cache_hit_rate) ??
    numericUsageValue(snapshot.usage.cache_hit_rate)

  return [
    "**Token / 上下文**",
    `- 阶段:${tokenMonitorPhaseLabel(snapshot.phase)}${snapshot.round ? ` / round ${snapshot.round}` : ""}`,
    `- 本轮累计:${formatTokenCount(runTokens)} tokens`,
    `- 本轮输入/输出/缓存复用:${formatTokenCount(runPromptTokens)} / ${formatTokenCount(runCompletionTokens)} / ${formatTokenCount(runCachedTokens)}`,
    `- clear 后累计:${formatTokenCount(sessionTokens)} tokens`,
    `- 上下文剩余:${formatTokenPercent(totalContextAvailableRate)}${totalContextRemainingTokens !== null ? ` · ${formatTokenCount(totalContextRemainingTokens)}` : ""}${totalContextLimitTokens !== null ? ` / ${formatTokenCount(totalContextLimitTokens)}` : ""}`,
    `- 上下文已用峰值:${formatTokenPercent(totalContextUsedRate)}${totalActiveInputTokens !== null ? ` · ${formatTokenCount(totalActiveInputTokens)}` : ""}`,
    `- 累计缓存命中:${formatTokenPercent(cacheHitRate)}`,
    `- 最近调用窗口:${formatTokenPercent(latestContextAvailableRate)} 剩余${latestContextUsedRate !== null ? ` / ${formatTokenPercent(latestContextUsedRate)} 已用` : ""}`,
    `- 最近调用花费:${formatTokenCount(latestCallTokens)} tokens`,
  ].join("\n")
}

function readableDisplayBlocks(blocks: unknown): string {
  if (!Array.isArray(blocks)) return ""
  const lines: string[] = []
  for (const raw of blocks) {
    if (!raw || typeof raw !== "object") continue
    const block = raw as Record<string, unknown>
    const title = typeof block.title === "string" ? block.title.trim() : ""
    const text = (
      typeof block.text === "string" ? block.text :
      typeof block.summary === "string" ? block.summary :
      typeof block.body === "string" ? block.body :
      ""
    ).trim()
    if (title && text) lines.push(`**${title}**: ${text}`)
    else if (title) lines.push(`**${title}**`)
    else if (text) lines.push(text)
  }
  return lines.join("\n")
}

function formatBlueprintEventSummary(event: ChatStreamEvent): string | null {
  const raw = event as Record<string, unknown>
  const type = String(event.type)
  if (SILENT_BLUEPRINT_PROGRESS_EVENTS.has(type)) return null
  const label = BLUEPRINT_EVENT_LABEL[type] ?? "蓝图更新"
  const sectionId = typeof raw.section_id === "string" && raw.section_id ? ` · ${raw.section_id}` : ""
  const summary = typeof raw.summary_text === "string" ? raw.summary_text.trim() : ""
  const failure = typeof raw.failure_reason === "string" ? raw.failure_reason.trim() : ""
  const blockText = readableDisplayBlocks(raw.display_blocks)
  const body = blockText || failure || summary
  if (!body && (type === "blueprint_section_delta" || type === "blueprint_section_started")) return null
  const text = body || label
  return `\n\n> **${label}${sectionId}**\n>\n> ${text.replace(/\n/g, "\n> ")}`
}

function blueprintTitleFromEvent(event: BlueprintStreamEvent): string | null {
  if (event.type === "blueprint_cleared") return "未命名项目"
  const viewModel = event.view_model_patch
  const header = viewModel && typeof viewModel === "object" ? viewModel.header : null
  if (header && typeof header === "object" && !Array.isArray(header)) {
    const title = (header as Record<string, unknown>).title
    if (typeof title === "string" && title.trim()) return title.trim()
  }
  const ref = event.blueprint_ref
  if (ref && typeof ref === "object") {
    for (const key of ["theme_title", "title", "blueprint_title"]) {
      const value = ref[key]
      if (typeof value === "string" && value.trim()) return value.trim()
    }
  }
  return null
}

function NodeBubbleCard({ node }: { node: NodeBubble }) {
  const label = NODE_TYPE_LABEL[node.type] ?? node.type
  const icon = NODE_TYPE_ICON[node.type] ?? "ND"
  const statusUI =
    node.status === "running"
      ? {
          dot: "bg-blue-400 animate-pulse",
          text: "text-blue-300",
          label: "运行中",
        }
      : node.status === "completed"
      ? { dot: "bg-green-400", text: "text-green-300", label: "完成" }
      : { dot: "bg-red-400", text: "text-red-300", label: "失败" }

  return (
    <div className="flex items-center gap-2 rounded-lg border border-gray-700 bg-gray-900/80 px-3 py-2 text-xs">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-gray-800 text-[10px] font-semibold leading-none tracking-tight text-gray-200">
        {icon}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-gray-200 truncate">{node.title}</div>
        <div className="text-[10px] text-gray-500 uppercase tracking-wide">{label}</div>
      </div>
      <div className="flex items-center gap-1.5">
        <span className={`w-1.5 h-1.5 rounded-full ${statusUI.dot}`} />
        <span className={`${statusUI.text} text-[10px]`}>{statusUI.label}</span>
      </div>
    </div>
  )
}

const TOOL_LABEL: Record<string, string> = {
  // project
  "project.list": "项目列表",
  "project.create": "创建项目",
  "project.get_state": "读取项目状态",
  // drama
  "drama.parse_uploaded_script": "解析上传剧本",
  // canvas
  "canvas.delete": "删除画布节点",
  // memory
  "memory.save_fact": "保存记忆",
  "memory.recall": "回忆记忆",
  "memory.save_user_fact": "保存用户偏好",
  "memory.recall_user": "读取用户偏好",
  // system
  "system.status": "系统状态",
  "system.models": "模型列表",
  // file
  "file.list_dir": "列出文件",
  "file.read_text": "读取文件",
  "file.extract_text_from_upload": "提取文本",
  // media
  "media.cancel_image_generation": "停止图片生成",
  "media.list_providers": "媒体源列表",
  "media.test_provider": "测试媒体源",
  // config
  "config.read": "读取配置",
  "config.read_file": "读取配置文件",
  "config.validate": "校验配置",
  // tool meta
  "tool.describe": "工具描述",
  // node (universal)
  "node.create": "创建节点",
  "node.get": "获取节点",
  "node.list": "节点列表",
  "node.update": "更新节点",
  "node.run": "运行节点",
  // project extras
  "project.reset": "重置项目",
  "media.get_presets": "生图预设",
  // asset / shot
  "scene.list": "场景列表",
  "scene.update": "更新场景",
  "scene.delete": "删除场景",
  "asset.create": "创建资产",
  "asset.list": "资产列表",
  "asset.update": "更新资产",
  "asset.delete": "删除资产",
  "shot.list": "镜头列表",
  "shot.delete": "删除镜头",
  // skill (dynamic)
  // assets
  "assets.get_library_path": "查询资产库",
  "assets.list_project": "资产列表",
  "assets.list_shared": "资产列表",
  "assets.read_asset": "读取资产",
  "assets.list_categories": "资产分类",
  "assets.create_category": "创建资产分类",
  "assets.move_asset": "移动资产",
  "assets.add_to_canvas": "资产加入画布",
  // task
  "task.create": "创建任务",
  "task.list": "任务列表",
  "task.update": "更新任务",
  "task.complete": "完成任务",
  // background
  "background.list": "后台任务",
  "background.status": "任务状态",
  "background.list_running": "运行中任务",
  // events
  "events.tail": "事件追踪",
  "events.query": "查询事件",
  // agent
  "agent.map_reduce": "并行归并",
  "agent.pipeline": "流水线协作",
  "agent.hierarchical": "层级协作",
  // plan
}

function ToolBubbleCard({ tool }: { tool: ToolBubble }) {
  const label = TOOL_LABEL[tool.tool] ?? tool.tool
  const isRunning = tool.status === "running"
  const elapsed = isRunning ? "" : `${((Date.now() - tool.startedAt) / 1000).toFixed(1)}s`

  return (
    <div className="flex items-center gap-2 text-[11px] text-gray-400 py-0.5">
      {isRunning ? (
        <span className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
      ) : tool.status === "completed" ? (
        <span className="text-[10px] font-semibold text-green-400">OK</span>
      ) : (
        <span className="text-[10px] font-semibold text-red-400">FAIL</span>
      )}
      <span className={isRunning ? "text-blue-300" : "text-gray-500"}>
        {isRunning ? `正在${label}` : tool.status === "completed" ? `${label}完成` : `${label}失败`}
      </span>
      {elapsed && <span className="text-gray-600">{elapsed}</span>}
    </div>
  )
}

function workflowSpecPreviewFromToolResult(result: unknown): Record<string, unknown> | null {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null
  const outer = result as Record<string, unknown>
  const nested = outer.result && typeof outer.result === "object" && !Array.isArray(outer.result)
    ? outer.result as Record<string, unknown>
    : null
  const candidate = typeof outer.artifact_ref === "string" ? outer : nested
  const artifactRef = typeof candidate?.artifact_ref === "string" ? candidate.artifact_ref.trim() : ""
  const preview = candidate?.preview
  if (!artifactRef.startsWith("workflow_spec:") || !preview || typeof preview !== "object" || Array.isArray(preview)) {
    return null
  }
  return {
    artifact_ref: artifactRef,
    preview,
    validation: candidate?.validation,
    self_check: candidate?.self_check,
  }
}

function workflowToolNameFromResult(tool: string, result: unknown): string {
  if (tool.startsWith("workflow.")) return tool
  if (!result || typeof result !== "object" || Array.isArray(result)) return ""
  const deferred = (result as Record<string, unknown>)._deferred_tool
  return typeof deferred === "string" ? deferred.trim() : ""
}

function shouldRefreshWorkflowForTool(tool: string, result: unknown): boolean {
  const workflowTool = workflowToolNameFromResult(tool, result)
  if (workflowTool.startsWith("workflow.")) return true
  if (!result || typeof result !== "object" || Array.isArray(result)) return false
  const resultObj = result as Record<string, unknown>
  return Boolean(
    resultObj.active_workflow_runtime
    || resultObj.active_workflow_runtimes
    || resultObj.workflow_input_values
    || resultObj.runtime,
  )
}

function workflowSpecToolAgent(event: ChatStreamEvent): string {
  if (!("agent" in event)) return ""
  return typeof event.agent === "string" ? event.agent.trim() : ""
}

function isWorkflowSpecToolEvent(event: ChatStreamEvent): boolean {
  if ((event.type === "tool_start" || event.type === "tool_done") && workflowSpecToolAgent(event) === "workflow_spec") {
    return true
  }
  if (event.type !== "tool_start") return false
  const content = String(event.content ?? "")
  return String(event.tool || "") === "tool.execute" && /workflow_spec|工作流.*模板|流程图/.test(content)
}

function isWorkflowSpecOnlyRound(event: ChatStreamEvent): boolean {
  if (event.type !== "agent_round") return false
  const agents = Array.isArray(event.tool_agents) ? event.tool_agents.map((item) => String(item).trim()).filter(Boolean) : []
  return agents.length > 0 && agents.every((agent) => agent === "workflow_spec")
}

function workflowMetadataFromCanvasPayload(payload: Record<string, unknown>): Record<string, unknown> | null {
  const candidates = [
    payload.workflow,
    payload.input && typeof payload.input === "object" && !Array.isArray(payload.input)
      ? (payload.input as Record<string, unknown>).workflow
      : null,
    payload.input_json && typeof payload.input_json === "object" && !Array.isArray(payload.input_json)
      ? (payload.input_json as Record<string, unknown>).workflow
      : null,
  ]
  for (const candidate of candidates) {
    if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
      return candidate as Record<string, unknown>
    }
  }
  return null
}

function isWorkflowManagedCanvasPayload(payload: Record<string, unknown>): boolean {
  const workflow = workflowMetadataFromCanvasPayload(payload)
  if (!workflow) return false
  return Boolean(workflow.template_id || workflow.instance_id || workflow.step_id || workflow.template_step_id)
}

function AgentRoundCard({ round }: { round: AgentRound }) {
  const text = round.content?.trim() || ""
  if (!text) return null

  return (
    <div className={`whitespace-pre-wrap text-sm leading-relaxed ${round.status === "running" ? "text-zinc-300" : "text-zinc-400"}`}>
      {text}
    </div>
  )
}

function AgentActivityTimeline({
  rounds,
  tools,
}: {
  rounds?: AgentRound[]
  tools?: ToolBubble[]
}) {
  const hasRounds = Boolean(rounds && rounds.length > 0)
  if (!hasRounds) return null

  return (
    <div className="mb-3 space-y-2">
      {rounds?.map((round) => (
        <AgentRoundCard key={`${round.round}-${round.startedAt}`} round={round} />
      ))}
    </div>
  )
}

function StepProgressCard({ progress }: { progress: StepProgress }) {
  const completed = progress.steps.filter((s) => s.status === "completed").length
  const failed = progress.steps.filter((s) => s.status === "failed").length

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-900/60 px-3 py-2 mb-2 text-xs">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-gray-400">执行进度</span>
        <span className="text-gray-500">{completed + failed}/{progress.total}</span>
      </div>
      <ul className="space-y-1">
        {progress.steps.map((step, i) => (
          <li key={i} className="flex items-center gap-2">
            {step.status === "completed" ? (
              <span className="text-[10px] font-semibold text-green-400">OK</span>
            ) : step.status === "running" ? (
              <span className="w-2.5 h-2.5 border border-blue-400 border-t-transparent rounded-full animate-spin" />
            ) : step.status === "failed" ? (
              <span className="text-[10px] font-semibold text-red-400">FAIL</span>
            ) : (
              <span className="w-2 h-2 rounded-full bg-gray-600" />
            )}
            <span className={
              step.status === "completed" ? "text-gray-500" :
              step.status === "running" ? "text-blue-200" :
              step.status === "failed" ? "text-red-300" :
              "text-gray-400"
            }>
              {step.title}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

const STOP_MESSAGE_RE = /(停止|中止|取消|终止|不要继续|别生成|停下|stop|cancel|abort)/i

function isStopMessage(text: string) {
  return STOP_MESSAGE_RE.test(text.trim())
}

function WorkingIndicator({ label = "正在工作" }: { label?: string }) {
  return (
    <div className="mt-3 flex items-center gap-2 text-[12px] text-zinc-400">
      <span className="relative flex h-2.5 w-2.5">
        <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400/50 opacity-75 animate-ping" />
        <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-emerald-400" />
      </span>
      <span>{label}</span>
    </div>
  )
}

type AssetInfoSource = "generated" | "project_library" | "shared_library"

type AssetInfoItem = {
  key: string
  source: AssetInfoSource
  title: string
  subtitle: string
  path: string
  sourceRef: string
  mediaKind: "image" | "video" | "audio" | "text" | "file"
  kind?: string
  category?: string
  episode?: string
  size?: number | null
  mimeType?: string | null
  previewUrl?: string
  prompt?: string | null
}

type AssetLibraryListResult = {
  items?: Array<{
    path?: string
    name?: string
    title?: string
    kind?: string
    episode?: string
    category?: string
    size?: number
    mime_type?: string
    width?: number
    height?: number
    resolution?: string
    prompt?: string
    prompt_snippet?: string
    modified_at?: string
  }>
  error?: string
  project_dir?: string
  shared_root?: string
  count?: number
}

type AssetCategoryResult = {
  project?: Array<{ library?: string; episode?: string; kind?: string; path?: string; count?: number }>
  shared?: Array<{ library?: string; kind?: string; category?: string; path?: string; count?: number }>
  project_kinds?: string[]
  shared_kinds?: string[]
  error?: string
}

type AssetAction =
  | { type: "preview"; item: AssetInfoItem }
  | { type: "save"; item: AssetInfoItem }
  | { type: "move"; item: AssetInfoItem }
  | { type: "category" }
  | null

type AssetTargetForm = {
  library: "shared" | "project"
  kind: string
  category: string
  episode: string
  name: string
}

const ASSET_IMAGE_SUFFIX_RE = /\.(png|jpe?g|webp|gif|bmp)$/i
const ASSET_VIDEO_SUFFIX_RE = /\.(mp4|webm|mov|m4v)$/i
const ASSET_AUDIO_SUFFIX_RE = /\.(mp3|wav|m4a|aac|ogg|flac)$/i
const ASSET_TEXT_SUFFIX_RE = /\.(txt|md|markdown|json|csv|ya?ml)$/i
const ASSET_LIBRARY_KINDS = ["character", "scene", "storyboard"]
const ASSET_LIBRARY_KIND_LABEL: Record<string, string> = {
  character: "人物",
  scene: "场景",
  storyboard: "分镜",
}

function assetMediaKind(text: string, mimeType?: string | null, type?: string | null): AssetInfoItem["mediaKind"] {
  const raw = `${text || ""} ${type || ""}`.toLowerCase()
  const mime = String(mimeType || "").toLowerCase()
  if (mime.startsWith("image/") || raw.includes("image") || ASSET_IMAGE_SUFFIX_RE.test(raw)) return "image"
  if (mime.startsWith("video/") || raw.includes("video") || ASSET_VIDEO_SUFFIX_RE.test(raw)) return "video"
  if (mime.startsWith("audio/") || raw.includes("audio") || ASSET_AUDIO_SUFFIX_RE.test(raw)) return "audio"
  if (mime.startsWith("text/") || ASSET_TEXT_SUFFIX_RE.test(raw)) return "text"
  return "file"
}

function assetBasename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path
}

function assetSourceLabel(source: AssetInfoSource): string {
  if (source === "generated") return "生成资产"
  return "资产库"
}

function assetKindLabel(kind: AssetInfoItem["mediaKind"]): string {
  if (kind === "image") return "图片"
  if (kind === "video") return "视频"
  if (kind === "audio") return "音频"
  if (kind === "text") return "文本"
  return "文件"
}

function formatAssetSize(size?: number | null): string {
  if (!Number.isFinite(size || 0) || !size) return ""
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function generatedAssetUrl(asset: ProjectAsset): string {
  if (asset.url) return resolveMediaUrl(asset.url)
  const path = asset.path || ""
  const match = path.match(/(?:^|\/)(generated_images|generated_videos|generated_audio)\/(.+)$/)
  if (match && asset.project_id) {
    return resolveMediaUrl(`/api/media/${asset.project_id}/${match[1]}/${match[2]}`)
  }
  return ""
}

function AssetInfoPanel({
  projectId,
  disabled,
}: {
  projectId?: string | null
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [items, setItems] = useState<AssetInfoItem[]>([])
  const [categories, setCategories] = useState<AssetCategoryResult>({})
  const [query, setQuery] = useState("")
  const [action, setAction] = useState<AssetAction>(null)
  const [form, setForm] = useState<AssetTargetForm>({
    library: "shared",
    kind: "character",
    category: "",
    episode: "1",
    name: "",
  })
  const [operationLoading, setOperationLoading] = useState(false)
  const [operationError, setOperationError] = useState<string | null>(null)
  const [operationMessage, setOperationMessage] = useState<string | null>(null)
  const canvasNodeCount = useCanvasStore((state) => state.nodes.length)

  const itemUrl = useCallback((item: AssetInfoItem): string => {
    if (item.previewUrl) return item.previewUrl
    if (item.source !== "generated" && item.path) return resolveAssetLibraryPreviewUrl(projectId || "", item.path)
    return ""
  }, [projectId])

  const resetOperationState = useCallback(() => {
    setOperationError(null)
    setOperationMessage(null)
  }, [])

  const defaultForm = useCallback((item?: AssetInfoItem): AssetTargetForm => {
    return {
      library: "shared",
      kind: item?.kind || "character",
      category: item?.category || "",
      episode: "1",
      name: "",
    }
  }, [])

  const openAction = useCallback((nextAction: AssetAction) => {
    resetOperationState()
    if (nextAction && nextAction.type !== "preview" && "item" in nextAction) {
      setForm(defaultForm(nextAction.item))
    } else if (nextAction?.type === "category") {
      setForm({ library: "shared", kind: "character", category: "", episode: "1", name: "" })
    }
    setAction(nextAction)
  }, [defaultForm, resetOperationState])

  const loadAssets = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    try {
      const next: AssetInfoItem[] = []
      const generated = await listProjectAssets(projectId)
      generated.assets.slice(0, 120).forEach((asset, index) => {
        const title = asset.name || asset.type || assetBasename(asset.path || "") || `生成资产 ${index + 1}`
        const pathText = asset.path || asset.url || asset.id
        const mediaKind = assetMediaKind(`${asset.url || ""} ${asset.path || ""}`, asset.mime_type, asset.type)
        next.push({
          key: `generated:${asset.id}`,
          source: "generated",
          title,
          subtitle: asset.type || asset.mime_type || "生成资产",
          path: pathText,
          sourceRef: `asset:${asset.id}`,
          mediaKind,
          mimeType: asset.mime_type,
          previewUrl: mediaKind === "image" || mediaKind === "video" || mediaKind === "audio" ? generatedAssetUrl(asset) : "",
          prompt: asset.prompt,
        })
      })

      const [library, categoryResult] = await Promise.all([
        callTool<AssetLibraryListResult>("assets.list_shared", { project_id: projectId }),
        callTool<AssetCategoryResult>("assets.list_categories", { project_id: projectId }),
      ])
      if (!categoryResult?.error) setCategories(categoryResult)
      ;(library?.items ?? []).slice(0, 120).forEach((item, index) => {
        const path = String(item.path || "")
        if (!path) return
        const mediaKind = assetMediaKind(path)
        next.push({
          key: `asset:${path}:${index}`,
          source: "shared_library",
          title: assetBasename(path),
          subtitle: item.category || item.kind || "资产库",
          path,
          sourceRef: path,
          mediaKind,
          kind: item.kind,
          category: item.category,
          episode: item.episode,
          size: item.size,
          previewUrl: mediaKind === "image" || mediaKind === "video" || mediaKind === "audio"
            ? resolveAssetLibraryPreviewUrl(projectId, path)
            : "",
        })
      })
      setItems(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (open) void loadAssets()
  }, [open, loadAssets])

  const sharedCategoryOptions = useMemo(() => {
    const targetKind = form.kind.trim()
    return (categories.shared ?? [])
      .filter((item) => !targetKind || item.kind === targetKind)
      .map((item) => item.category)
      .filter((item): item is string => Boolean(item))
  }, [categories.shared, form.kind])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter((item) =>
      `${item.title} ${item.subtitle} ${item.path} ${item.mimeType || ""} ${assetSourceLabel(item.source)} ${item.category || ""} ${item.kind || ""}`
        .toLowerCase()
        .includes(q),
    )
  }, [items, query])

  const assertToolOk = (result: Record<string, unknown>) => {
    if (result?.error) throw new Error(String(result.error))
    if (result?.ok === false) throw new Error(String(result.error || "操作失败"))
  }

  const handleDownload = useCallback((item: AssetInfoItem) => {
    const url = itemUrl(item)
    if (!url) {
      setOperationError("这个资产没有可下载地址")
      return
    }
    const anchor = document.createElement("a")
    anchor.href = url
    anchor.download = assetBasename(item.path || item.title)
    anchor.rel = "noopener"
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }, [itemUrl])

  const handleAddToCanvas = useCallback(async (item: AssetInfoItem) => {
    if (!projectId) return
    resetOperationState()
    setOperationLoading(true)
    try {
      const result = await callTool<Record<string, unknown>>("assets.add_to_canvas", {
        project_id: projectId,
        source: item.sourceRef,
        title: item.title,
        node_type: item.mediaKind === "file" ? undefined : item.mediaKind,
        x: 120 + (canvasNodeCount % 4) * 300,
        y: 90 + Math.floor(canvasNodeCount / 4) * 220,
      })
      assertToolOk(result)
      setOperationMessage("已加入画布")
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err))
    } finally {
      setOperationLoading(false)
    }
  }, [canvasNodeCount, projectId, resetOperationState])

  const submitAssetAction = useCallback(async () => {
    if (!projectId || !action || action.type === "preview") return
    resetOperationState()
    setOperationLoading(true)
    try {
      if (action.type === "category") {
        const result = await callTool<Record<string, unknown>>("assets.create_category", {
          project_id: projectId,
          library: "asset",
          kind: form.kind,
          category: form.category,
        })
        assertToolOk(result)
        setOperationMessage("分类已创建")
      } else if (action.type === "save") {
        const item = action.item
        const result = await callTool<Record<string, unknown>>("assets.save_to_shared", {
          project_id: projectId,
          source: item.sourceRef,
          kind: form.kind,
          category: form.category,
          name: form.name || undefined,
        })
        assertToolOk(result)
        setOperationMessage("已加入资产库")
      } else if (action.type === "move") {
        const item = action.item
        const result = await callTool<Record<string, unknown>>("assets.move_asset", {
          project_id: projectId,
          path: item.path,
          library: "asset",
          kind: form.kind,
          category: form.category,
          name: form.name || undefined,
        })
        assertToolOk(result)
        setOperationMessage("资产已移动")
      }
      await loadAssets()
      setAction(null)
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err))
    } finally {
      setOperationLoading(false)
    }
  }, [action, form, loadAssets, projectId, resetOperationState])

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        disabled={!projectId || disabled}
        title="查看资产信息"
        className="h-8 rounded-md border border-white/10 bg-white/[0.04] px-2.5 text-xs font-medium text-zinc-300 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
      >
        资产
      </button>
      {open ? (
        <div className="fixed inset-x-3 bottom-20 z-30 max-h-[70dvh] overflow-hidden rounded-lg border border-white/10 bg-[var(--studio-panel)] shadow-2xl shadow-black/50 sm:absolute sm:bottom-10 sm:left-0 sm:right-auto sm:max-h-none sm:w-[420px]">
          <div className="border-b border-white/10 px-3 py-2.5">
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="text-sm font-medium text-zinc-100">资产信息</div>
                <div className="mt-0.5 text-[11px] text-zinc-500">预览、下载、入库、分类整理和加入画布</div>
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => openAction({ type: "category" })}
                  className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]"
                >
                  新分类
                </button>
                <button
                  type="button"
                  onClick={() => void loadAssets()}
                  disabled={loading}
                  className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06] disabled:opacity-50"
                >
                  刷新
                </button>
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]"
                >
                  关闭
                </button>
              </div>
            </div>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索名称、类型或路径"
              className="mt-2 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-200/60"
            />
          </div>
          <div className="max-h-[calc(70dvh-88px)] overflow-y-auto p-2 sm:max-h-[420px]">
            {operationError ? (
              <div className="mb-2 rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{operationError}</div>
            ) : null}
            {operationMessage ? (
              <div className="mb-2 rounded-md border border-emerald-400/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">{operationMessage}</div>
            ) : null}
            {loading ? (
              <div className="flex h-24 items-center justify-center text-xs text-zinc-500">正在读取资产…</div>
            ) : error ? (
              <div className="rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{error}</div>
            ) : filtered.length === 0 ? (
              <div className="flex h-24 items-center justify-center text-xs text-zinc-500">没有资产</div>
            ) : (
              <div className="space-y-2">
                {filtered.map((item) => {
                  const sizeText = formatAssetSize(item.size)
                  return (
                    <div key={item.key} className="flex gap-2 rounded-md border border-white/10 bg-white/[0.035] p-2">
                      <div className="h-16 w-20 shrink-0 overflow-hidden rounded bg-black/25">
                        {item.previewUrl ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={item.previewUrl} alt={item.title} className="h-full w-full object-cover" />
                        ) : (
                          <div className="flex h-full w-full items-center justify-center text-[10px] text-zinc-600">资产</div>
                        )}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="truncate text-xs font-medium text-zinc-200">{item.title}</span>
                          <span className="shrink-0 rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-500">
                            {assetSourceLabel(item.source)}
                          </span>
                        </div>
                        <div className="mt-1 truncate text-[11px] text-zinc-500">{item.subtitle}</div>
                        <div className="mt-1 truncate font-mono text-[10px] text-zinc-500">{item.path}</div>
                        {(sizeText || item.mimeType || item.prompt) ? (
                          <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-zinc-600">
                            <span>{assetKindLabel(item.mediaKind)}</span>
                            {sizeText ? <span>{sizeText}</span> : null}
                            {item.mimeType ? <span>{item.mimeType}</span> : null}
                            {item.prompt ? <span className="max-w-full truncate">prompt: {item.prompt}</span> : null}
                          </div>
                        ) : null}
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          <button
                            type="button"
                            onClick={() => openAction({ type: "preview", item })}
                            className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-white/[0.06]"
                          >
                            预览
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDownload(item)}
                            className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-white/[0.06]"
                          >
                            下载
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleAddToCanvas(item)}
                            disabled={operationLoading}
                            className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-white/[0.06] disabled:opacity-50"
                          >
                            加入画布
                          </button>
                          <button
                            type="button"
                            onClick={() => openAction({ type: "save", item })}
                            className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-white/[0.06]"
                          >
                            入库
                          </button>
                          {item.source !== "generated" ? (
                            <button
                              type="button"
                              onClick={() => openAction({ type: "move", item })}
                              className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-white/[0.06]"
                            >
                              移动
                            </button>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
          {action ? (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/55 p-3">
              <div className="w-full max-w-[360px] rounded-lg border border-white/10 bg-[var(--studio-panel)] p-3 shadow-xl">
                {action.type === "preview" ? (
                  <>
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-zinc-100">{action.item.title}</div>
                        <div className="mt-0.5 text-[11px] text-zinc-500">{assetSourceLabel(action.item.source)} · {assetKindLabel(action.item.mediaKind)}</div>
                      </div>
                      <button type="button" onClick={() => setAction(null)} className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]">关闭</button>
                    </div>
                    <div className="mt-3 overflow-hidden rounded-md border border-white/10 bg-black/30">
                      {action.item.mediaKind === "image" && itemUrl(action.item) ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={itemUrl(action.item)} alt={action.item.title} className="max-h-[48dvh] w-full object-contain" />
                      ) : action.item.mediaKind === "video" && itemUrl(action.item) ? (
                        <video controls preload="metadata" className="max-h-[48dvh] w-full">
                          <source src={itemUrl(action.item)} />
                        </video>
                      ) : action.item.mediaKind === "audio" && itemUrl(action.item) ? (
                        <div className="p-3">
                          <audio controls preload="metadata" className="w-full">
                            <source src={itemUrl(action.item)} />
                          </audio>
                        </div>
                      ) : (
                        <div className="p-3 text-xs text-zinc-400">
                          <div className="font-medium text-zinc-200">{assetKindLabel(action.item.mediaKind)}</div>
                          <div className="mt-1 break-all font-mono text-[11px] text-zinc-500">{action.item.path}</div>
                          {action.item.prompt ? <div className="mt-2 text-zinc-500">{action.item.prompt}</div> : null}
                        </div>
                      )}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-sm font-medium text-zinc-100">
                          {action.type === "category" ? "创建分类" : action.type === "move" ? "移动资产" : "加入资产库"}
                        </div>
                        <div className="mt-0.5 text-[11px] text-zinc-500">
                          {action.type === "category" ? "创建资产分类" : "选择类型和分类"}
                        </div>
                      </div>
                      <button type="button" onClick={() => setAction(null)} className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]">关闭</button>
                    </div>
                    <div className="mt-3 space-y-2">
                      <label className="block text-[11px] text-zinc-500">
                        类型
                        <select
                          value={form.kind}
                          onChange={(event) => setForm((current) => ({ ...current, kind: event.target.value }))}
                          className="mt-1 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2 text-xs text-zinc-100"
                        >
                          {ASSET_LIBRARY_KINDS.map((kind) => (
                            <option key={kind} value={kind}>{ASSET_LIBRARY_KIND_LABEL[kind] ?? kind}</option>
                          ))}
                        </select>
                      </label>
                      <label className="block text-[11px] text-zinc-500">
                        分类
                        <input
                          value={form.category}
                          list="asset-shared-category-options"
                          onChange={(event) => setForm((current) => ({ ...current, category: event.target.value }))}
                          placeholder="输入分类名，跟随当前用户语言"
                          className="mt-1 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2 text-xs text-zinc-100 placeholder-zinc-600"
                        />
                        <datalist id="asset-shared-category-options">
                          {sharedCategoryOptions.map((category) => <option key={category} value={category} />)}
                        </datalist>
                      </label>
                      {action.type !== "category" ? (
                        <label className="block text-[11px] text-zinc-500">
                          新名称
                          <input
                            value={form.name}
                            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                            placeholder="选填，默认沿用原文件名"
                            className="mt-1 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2 text-xs text-zinc-100 placeholder-zinc-600"
                          />
                        </label>
                      ) : null}
                    </div>
                    {operationError ? <div className="mt-3 rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{operationError}</div> : null}
                    <div className="mt-3 flex justify-end gap-2">
                      <button type="button" onClick={() => setAction(null)} className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.06]">取消</button>
                      <button
                        type="button"
                        onClick={() => void submitAssetAction()}
                        disabled={operationLoading || !form.category.trim()}
                        className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-950 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {operationLoading ? "处理中" : "确认"}
                      </button>
                    </div>
                  </>
                )}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

type AssetFolder = {
  key: string
  label: string
  subtitle: string
  library: "project" | "shared"
  source: "project_library" | "shared_library"
  kind: string
  category?: string
  episode?: string
  count?: number
}

function folderKeyForAsset(item: AssetInfoItem): string {
  return `asset:${item.kind || "asset"}:${item.category || "未分类"}`
}

function folderLabel(folder: Pick<AssetFolder, "library" | "kind" | "category" | "episode">): string {
  return folder.category || "未分类"
}

function AssetLibraryPanel({
  projectId,
  disabled,
}: {
  projectId?: string | null
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [items, setItems] = useState<AssetInfoItem[]>([])
  const [categories, setCategories] = useState<AssetCategoryResult>({})
  const [selectedFolderKey, setSelectedFolderKey] = useState<string | null>(null)
  const [query, setQuery] = useState("")
  const [action, setAction] = useState<AssetAction>(null)
  const [form, setForm] = useState<AssetTargetForm>({
    library: "shared",
    kind: "character",
    category: "",
    episode: "1",
    name: "",
  })
  const [operationLoading, setOperationLoading] = useState(false)
  const [operationError, setOperationError] = useState<string | null>(null)
  const [operationMessage, setOperationMessage] = useState<string | null>(null)
  const canvasNodeCount = useCanvasStore((state) => state.nodes.length)

  const itemUrl = useCallback((item: AssetInfoItem): string => {
    if (item.previewUrl) return item.previewUrl
    return item.path && projectId ? resolveAssetLibraryPreviewUrl(projectId, item.path) : ""
  }, [projectId])

  const folders = useMemo(() => {
    const map = new Map<string, AssetFolder>()
    ;(categories.shared ?? []).forEach((folder) => {
      const kind = folder.kind || "asset"
      const category = folder.category || "未分类"
      const key = `asset:${kind}:${category}`
      map.set(key, {
        key,
        label: category,
        subtitle: `资产库 · ${kind}`,
        library: "shared",
        source: "shared_library",
        kind,
        category,
        count: folder.count,
      })
    })
    items.forEach((item) => {
      const key = folderKeyForAsset(item)
      if (map.has(key)) return
      map.set(key, {
        key,
        label: folderLabel({ library: "shared", kind: item.kind || "asset", category: item.category, episode: item.episode }),
        subtitle: assetSourceLabel(item.source),
        library: "shared",
        source: "shared_library",
        kind: item.kind || "asset",
        category: item.category,
        episode: item.episode,
      })
    })
    return [...map.values()].sort((a, b) => `${a.kind}:${a.label}`.localeCompare(`${b.kind}:${b.label}`, "zh-CN"))
  }, [categories.shared, items])

  const selectedFolder = useMemo(
    () => folders.find((folder) => folder.key === selectedFolderKey) ?? null,
    [folders, selectedFolderKey],
  )

  useEffect(() => {
    if (!open) return
    if (selectedFolderKey && folders.some((folder) => folder.key === selectedFolderKey)) return
    setSelectedFolderKey(folders[0]?.key ?? null)
  }, [folders, open, selectedFolderKey])

  const sharedCategoryOptions = useMemo(() => {
    const targetKind = form.kind.trim()
    return (categories.shared ?? [])
      .filter((item) => !targetKind || item.kind === targetKind)
      .map((item) => item.category)
      .filter((item): item is string => Boolean(item))
  }, [categories.shared, form.kind])

  const loadAssets = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    try {
      const [library, categoryResult] = await Promise.all([
        callTool<AssetLibraryListResult>("assets.list_shared", { project_id: projectId }),
        callTool<AssetCategoryResult>("assets.list_categories", { project_id: projectId }),
      ])
      if (!categoryResult?.error) setCategories(categoryResult)
      const next: AssetInfoItem[] = []
      ;(library?.items ?? []).forEach((item, index) => {
        const path = String(item.path || "")
        if (!path) return
        const mediaKind = assetMediaKind(path, item.mime_type)
        next.push({
          key: `asset:${path}:${index}`,
          source: "shared_library",
          title: item.title || assetBasename(path),
          subtitle: item.category || item.kind || "资产库",
          path,
          sourceRef: path,
          mediaKind,
          kind: item.kind,
          category: item.category,
          episode: item.episode,
          size: item.size,
          mimeType: item.mime_type,
          previewUrl: mediaKind === "image" || mediaKind === "video" || mediaKind === "audio"
            ? resolveAssetLibraryPreviewUrl(projectId, path)
            : "",
          prompt: item.prompt_snippet || item.prompt,
        } as AssetInfoItem & {
          width?: number
          height?: number
          resolution?: string
          modifiedAt?: string
        })
        const pushed = next[next.length - 1] as AssetInfoItem & {
          width?: number
          height?: number
          resolution?: string
          modifiedAt?: string
        }
        pushed.width = item.width
        pushed.height = item.height
        pushed.resolution = item.resolution
        pushed.modifiedAt = item.modified_at
      })
      setItems(next)
      const errors = [library?.error].filter(Boolean)
      setError(errors.length && next.length === 0 ? errors.join("；") : null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (open) void loadAssets()
  }, [open, loadAssets])

  const visibleItems = useMemo(() => {
    const q = query.trim().toLowerCase()
    return items.filter((item) => {
      if (selectedFolder && folderKeyForAsset(item) !== selectedFolder.key) return false
      if (!q) return true
      return `${item.title} ${item.path} ${item.kind || ""} ${item.category || ""} ${item.episode || ""} ${item.prompt || ""}`
        .toLowerCase()
        .includes(q)
    })
  }, [items, query, selectedFolder])

  const resetOperationState = useCallback(() => {
    setOperationError(null)
    setOperationMessage(null)
  }, [])

  const openAction = useCallback((nextAction: AssetAction) => {
    resetOperationState()
    if (nextAction && nextAction.type === "move") {
      const item = nextAction.item
      setForm({
        library: "shared",
        kind: item.kind || "character",
        category: item.category || "",
        episode: "1",
        name: "",
      })
    } else if (nextAction?.type === "category") {
      setForm({ library: "shared", kind: "character", category: "", episode: "1", name: "" })
    }
    setAction(nextAction)
  }, [resetOperationState])

  const assertToolOk = (result: Record<string, unknown>) => {
    if (result?.error) throw new Error(String(result.error))
    if (result?.ok === false) throw new Error(String(result.error || "操作失败"))
  }

  const handleDownload = useCallback((item: AssetInfoItem) => {
    const url = itemUrl(item)
    if (!url) {
      setOperationError("这个资产没有可下载地址")
      return
    }
    const anchor = document.createElement("a")
    anchor.href = url
    anchor.download = assetBasename(item.path || item.title)
    anchor.rel = "noopener"
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }, [itemUrl])

  const handleAddToCanvas = useCallback(async (item: AssetInfoItem) => {
    if (!projectId) return
    resetOperationState()
    setOperationLoading(true)
    try {
      const result = await callTool<Record<string, unknown>>("assets.add_to_canvas", {
        project_id: projectId,
        source: item.sourceRef,
        title: item.title,
        node_type: item.mediaKind === "file" ? undefined : item.mediaKind,
        x: 120 + (canvasNodeCount % 4) * 300,
        y: 90 + Math.floor(canvasNodeCount / 4) * 220,
      })
      assertToolOk(result)
      setOperationMessage("已加入画布")
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err))
    } finally {
      setOperationLoading(false)
    }
  }, [canvasNodeCount, projectId, resetOperationState])

  const submitAssetAction = useCallback(async () => {
    if (!projectId || !action || action.type === "preview" || action.type === "save") return
    resetOperationState()
    setOperationLoading(true)
    try {
      if (action.type === "category") {
        const result = await callTool<Record<string, unknown>>("assets.create_category", {
          project_id: projectId,
          library: "asset",
          kind: form.kind,
          category: form.category,
        })
        assertToolOk(result)
        setOperationMessage("分类已创建")
      } else if (action.type === "move") {
        const result = await callTool<Record<string, unknown>>("assets.move_asset", {
          project_id: projectId,
          path: action.item.path,
          library: "asset",
          kind: form.kind,
          category: form.category,
          name: form.name || undefined,
        })
        assertToolOk(result)
        setOperationMessage("资产已移动")
      }
      await loadAssets()
      setAction(null)
    } catch (err) {
      setOperationError(err instanceof Error ? err.message : String(err))
    } finally {
      setOperationLoading(false)
    }
  }, [action, form, loadAssets, projectId, resetOperationState])

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(true)}
        disabled={!projectId || disabled}
        title="打开资产库"
        className="h-8 rounded-md border border-white/10 bg-white/[0.04] px-2.5 text-xs font-medium text-zinc-300 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
      >
        资产库
      </button>
      {open ? (
        <div className="fixed inset-3 z-[90] flex items-center justify-center bg-black/48 backdrop-blur-sm sm:inset-6">
          <div className="flex h-[82dvh] w-full max-w-6xl overflow-hidden rounded-lg border border-white/10 bg-[var(--studio-panel)] shadow-2xl shadow-black/60">
            <aside className="flex w-64 shrink-0 flex-col border-r border-white/10 bg-black/18">
              <div className="border-b border-white/10 px-4 py-3">
                <div className="text-sm font-semibold text-zinc-100">资产库</div>
                <div className="mt-1 text-[11px] text-zinc-500">本地文件夹分类</div>
              </div>
              <div className="flex-1 overflow-y-auto p-2">
                {folders.length === 0 ? (
                  <div className="px-2 py-6 text-center text-xs text-zinc-500">没有分类</div>
                ) : folders.map((folder) => (
                  <button
                    key={folder.key}
                    type="button"
                    onClick={() => setSelectedFolderKey(folder.key)}
                    className={`mb-1 flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left transition-colors ${
                      selectedFolderKey === folder.key ? "bg-white/12 text-zinc-50" : "text-zinc-400 hover:bg-white/[0.06]"
                    }`}
                  >
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-amber-300/12 text-[13px] text-amber-200">DIR</span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium">{folder.label}</span>
                      <span className="block truncate text-[10px] text-zinc-500">{folder.subtitle}</span>
                    </span>
                    {folder.count !== undefined ? <span className="text-[10px] text-zinc-500">{folder.count}</span> : null}
                  </button>
                ))}
              </div>
              <div className="border-t border-white/10 p-2">
                <button
                  type="button"
                  onClick={() => openAction({ type: "category" })}
                  className="w-full rounded-md border border-white/10 px-3 py-2 text-xs text-zinc-300 hover:bg-white/[0.06]"
                >
                  创建分类
                </button>
              </div>
            </aside>
            <main className="flex min-w-0 flex-1 flex-col">
              <header className="border-b border-white/10 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-zinc-100">
                      {selectedFolder ? selectedFolder.label : "资产库内容"}
                    </div>
                    <div className="mt-1 truncate text-[11px] text-zinc-500">
                      {selectedFolder ? selectedFolder.subtitle : "只显示本地资产库中的文件"}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="搜索资产"
                      className="h-8 w-52 rounded-md border border-white/10 bg-[var(--studio-control)] px-2.5 text-xs text-zinc-100 placeholder-zinc-600"
                    />
                    <button
                      type="button"
                      onClick={() => void loadAssets()}
                      disabled={loading}
                      className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.06] disabled:opacity-50"
                    >
                      刷新
                    </button>
                    <button
                      type="button"
                      onClick={() => setOpen(false)}
                      className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.06]"
                    >
                      关闭
                    </button>
                  </div>
                </div>
                {operationError ? <div className="mt-2 rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{operationError}</div> : null}
                {operationMessage ? <div className="mt-2 rounded-md border border-emerald-400/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">{operationMessage}</div> : null}
              </header>
              <div className="flex-1 overflow-y-auto p-4">
                {loading ? (
                  <div className="flex h-full items-center justify-center text-sm text-zinc-500">正在读取资产库...</div>
                ) : error ? (
                  <div className="rounded-md border border-amber-400/20 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">{error}</div>
                ) : visibleItems.length === 0 ? (
                  <div className="flex h-full items-center justify-center text-sm text-zinc-500">这个分类里没有资产</div>
                ) : (
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {visibleItems.map((item) => {
                      const rich = item as AssetInfoItem & { resolution?: string; width?: number; height?: number }
                      const resolution = rich.resolution || (rich.width && rich.height ? `${rich.width}x${rich.height}` : "")
                      return (
                        <div key={item.key} className="overflow-hidden rounded-md border border-white/10 bg-white/[0.035]">
                          <button
                            type="button"
                            onClick={() => openAction({ type: "preview", item })}
                            className="block aspect-video w-full bg-black/30"
                          >
                            {item.previewUrl && item.mediaKind === "image" ? (
                              // eslint-disable-next-line @next/next/no-img-element
                              <img src={item.previewUrl} alt={item.title} className="h-full w-full object-cover" />
                            ) : (
                              <div className="flex h-full w-full items-center justify-center text-xs text-zinc-600">{assetKindLabel(item.mediaKind)}</div>
                            )}
                          </button>
                          <div className="p-3">
                            <div className="truncate text-sm font-medium text-zinc-100" title={item.title}>{item.title}</div>
                            <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-[11px] text-zinc-500">
                              <span>{assetKindLabel(item.mediaKind)}</span>
                              {resolution ? <span>{resolution}</span> : null}
                              {item.size ? <span>{formatAssetSize(item.size)}</span> : null}
                            </div>
                            {item.prompt ? <div className="mt-2 line-clamp-2 text-[11px] leading-4 text-zinc-500">{item.prompt}</div> : null}
                            <div className="mt-3 flex flex-wrap gap-1.5">
                              <button type="button" onClick={() => openAction({ type: "preview", item })} className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]">预览</button>
                              <button type="button" onClick={() => handleDownload(item)} className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]">下载</button>
                              <button type="button" onClick={() => void handleAddToCanvas(item)} disabled={operationLoading} className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06] disabled:opacity-50">加入画布</button>
                              <button type="button" onClick={() => openAction({ type: "move", item })} className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]">移动</button>
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </main>
            {action ? (
              <AssetLibraryActionDialog
                action={action}
                form={form}
                setForm={setForm}
                sharedCategoryOptions={sharedCategoryOptions}
                itemUrl={itemUrl}
                operationLoading={operationLoading}
                operationError={operationError}
                onCancel={() => setAction(null)}
                onSubmit={submitAssetAction}
              />
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  )
}

function AssetLibraryActionDialog({
  action,
  form,
  setForm,
  sharedCategoryOptions,
  itemUrl,
  operationLoading,
  operationError,
  onCancel,
  onSubmit,
}: {
  action: Exclude<AssetAction, null>
  form: AssetTargetForm
  setForm: Dispatch<SetStateAction<AssetTargetForm>>
  sharedCategoryOptions: string[]
  itemUrl: (item: AssetInfoItem) => string
  operationLoading: boolean
  operationError: string | null
  onCancel: () => void
  onSubmit: () => void
}) {
  if (action.type === "preview" || action.type === "save") {
    const item = action.item
    const url = itemUrl(item)
    return (
      <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/62 p-5">
        <div className="max-h-full w-full max-w-4xl overflow-hidden rounded-lg border border-white/10 bg-[var(--studio-panel)] shadow-2xl">
          <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-zinc-100">{item.title}</div>
              <div className="mt-0.5 text-[11px] text-zinc-500">{assetKindLabel(item.mediaKind)}</div>
            </div>
            <button type="button" onClick={onCancel} className="rounded border border-white/10 px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.06]">关闭</button>
          </div>
          <div className="max-h-[68dvh] overflow-auto bg-black/25 p-4">
            {item.mediaKind === "image" && url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={url} alt={item.title} className="mx-auto max-h-[62dvh] max-w-full object-contain" />
            ) : item.mediaKind === "video" && url ? (
              <video controls preload="metadata" className="mx-auto max-h-[62dvh] max-w-full"><source src={url} /></video>
            ) : item.mediaKind === "audio" && url ? (
              <audio controls preload="metadata" className="w-full"><source src={url} /></audio>
            ) : (
              <div className="break-all font-mono text-xs text-zinc-400">{item.path}</div>
            )}
            {item.prompt ? <div className="mt-3 rounded-md border border-white/10 bg-black/30 p-3 text-xs leading-5 text-zinc-400">{item.prompt}</div> : null}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/62 p-5">
      <div className="w-full max-w-md rounded-lg border border-white/10 bg-[var(--studio-panel)] p-4 shadow-2xl">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-zinc-100">{action.type === "category" ? "创建分类" : "移动资产"}</div>
            <div className="mt-0.5 text-[11px] text-zinc-500">分类会同步为本地文件夹</div>
          </div>
          <button type="button" onClick={onCancel} className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]">关闭</button>
        </div>
        <div className="mt-3 space-y-2">
          <label className="block text-[11px] text-zinc-500">
            类型
            <select
              value={form.kind}
              onChange={(event) => setForm((current) => ({ ...current, kind: event.target.value }))}
              className="mt-1 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2 text-xs text-zinc-100"
            >
              {ASSET_LIBRARY_KINDS.map((kind) => (
                <option key={kind} value={kind}>{ASSET_LIBRARY_KIND_LABEL[kind] ?? kind}</option>
              ))}
            </select>
          </label>
          <label className="block text-[11px] text-zinc-500">
            分类文件夹
            <input
              value={form.category}
              list="asset-library-shared-categories"
              onChange={(event) => setForm((current) => ({ ...current, category: event.target.value }))}
              placeholder="输入分类名，跟随当前用户语言"
              className="mt-1 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2 text-xs text-zinc-100 placeholder-zinc-600"
            />
            <datalist id="asset-library-shared-categories">
              {sharedCategoryOptions.map((category) => <option key={category} value={category} />)}
            </datalist>
          </label>
          {action.type === "move" ? (
            <label className="block text-[11px] text-zinc-500">
              新文件名
              <input
                value={form.name}
                onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                placeholder="选填，默认沿用原文件名"
                className="mt-1 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2 text-xs text-zinc-100 placeholder-zinc-600"
              />
            </label>
          ) : null}
        </div>
        {operationError ? <div className="mt-3 rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{operationError}</div> : null}
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onCancel} className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.06]">取消</button>
          <button
            type="button"
            onClick={onSubmit}
            disabled={operationLoading || !form.category.trim()}
            className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-950 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {operationLoading ? "处理中" : "确认"}
          </button>
        </div>
      </div>
    </div>
  )
}

function SmoothMarkdownView({
  content,
  active,
}: {
  content: string
  active: boolean
}) {
  const [displayed, setDisplayed] = useState(content)

  useEffect(() => {
    if (!active) {
      setDisplayed(content)
      return
    }
    setDisplayed((current) => {
      if (!content.startsWith(current)) return content.slice(0, Math.min(content.length, current.length))
      return current
    })
  }, [active, content])

  useEffect(() => {
    if (!active || displayed.length >= content.length) return
    const id = window.setInterval(() => {
      setDisplayed((current) => {
        if (current.length >= content.length) return current
        const remaining = content.length - current.length
        const step = Math.max(1, Math.ceil(remaining / 10))
        return content.slice(0, current.length + step)
      })
    }, 18)
    return () => window.clearInterval(id)
  }, [active, content, displayed.length])

  return <MarkdownView>{displayed}</MarkdownView>
}

export function ChatPanel() {
  const [input, setInput] = useState("")
  const [dragOver, setDragOver] = useState(false)
  const [slashSelectedIdx, setSlashSelectedIdx] = useState(0)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const messagesScrollRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const streamCancelRef = useRef<(() => void) | null>(null)
  const activeStreamRef = useRef(false)
  const pendingProjectSwitchRef = useRef<string | null>(null)
  const projectListLoadingRef = useRef(false)
  const handleStreamEventRef = useRef<(event: ChatStreamEvent) => void>(() => {})
  const [autoScroll, setAutoScroll] = useState(true)
  const [queuedCount, setQueuedCount] = useState(0)
  const [stopping, setStopping] = useState(false)
  const [slashRunStatus, setSlashRunStatus] = useState<SlashRunStatus | null>(null)
  const [showTokenMonitor, setShowTokenMonitor] = useState(true)
  const [localStreamActive, setLocalStreamActive] = useState(false)
  const [collaborationMode, setCollaborationMode] = useState<AgentCollaborationMode>("default")

  // 细粒度订阅:setInput 重渲只触发 input 相关 UI,不会让 messages / 各 action 引起的 setState
  // 把整个 ChatPanel 重渲一次。整体 useChatStore() 解构会让任意 store 字段变都重渲所有订阅者,
  // 在长会话里输入框打字会明显卡顿。
  const messages = useChatStore((s) => s.messages)
  const streaming = useChatStore((s) => s.streaming)
  const pendingAttachments = useChatStore((s) => s.pendingAttachments)
  const lastFailedMessage = useChatStore((s) => s.lastFailedMessage)
  const tokenUsage = useChatStore((s) => s.tokenUsage)
  const appendMessage = useChatStore((s) => s.appendMessage)
  const markQueuedUserMessage = useChatStore((s) => s.markQueuedUserMessage)
  const removeQueuedUserMessage = useChatStore((s) => s.removeQueuedUserMessage)
  const refreshQueuedUserMessagePositions = useChatStore((s) => s.refreshQueuedUserMessagePositions)
  const ensureAssistantAfterQueuedUser = useChatStore((s) => s.ensureAssistantAfterQueuedUser)
  const loadHistory = useChatStore((s) => s.loadHistory)
  const setStreaming = useChatStore((s) => s.setStreaming)
  const appendToLastAssistant = useChatStore((s) => s.appendToLastAssistant)
  const setLastAssistantNode = useChatStore((s) => s.setLastAssistantNode)
  const updateLastAssistantNode = useChatStore((s) => s.updateLastAssistantNode)
  const addToolBubble = useChatStore((s) => s.addToolBubble)
  const updateToolBubble = useChatStore((s) => s.updateToolBubble)
  const addAgentRound = useChatStore((s) => s.addAgentRound)
  const addAgentRoundToolResult = useChatStore((s) => s.addAgentRoundToolResult)
  const addAgentRoundToolStart = useChatStore((s) => s.addAgentRoundToolStart)
  const completeAgentRound = useChatStore((s) => s.completeAgentRound)
  const initStepProgress = useChatStore((s) => s.initStepProgress)
  const advanceStep = useChatStore((s) => s.advanceStep)
  const setLastAssistantProposedPlan = useChatStore((s) => s.setLastAssistantProposedPlan)
  const setLastAssistantInteractionInput = useChatStore((s) => s.setLastAssistantInteractionInput)
  const setLastAssistantPendingAction = useChatStore((s) => s.setLastAssistantPendingAction)
  const markLastAssistantPendingActionStatus = useChatStore((s) => s.markLastAssistantPendingActionStatus)
  const addPendingAttachment = useChatStore((s) => s.addPendingAttachment)
  const updatePendingAttachment = useChatStore((s) => s.updatePendingAttachment)
  const removePendingAttachment = useChatStore((s) => s.removePendingAttachment)
  const clearPendingAttachments = useChatStore((s) => s.clearPendingAttachments)
  const resetProjectRuntime = useChatStore((s) => s.resetProjectRuntime)
  const setLastFailed = useChatStore((s) => s.setLastFailed)
  const setActiveChecklist = useChatStore((s) => s.setActiveChecklist)
  const setUnfinishedNodes = useChatStore((s) => s.setUnfinishedNodes)
  const setTokenUsage = useChatStore((s) => s.setTokenUsage)
  const applyTokenUsageEvent = useChatStore((s) => s.applyTokenUsageEvent)
  const applyBlueprintEvent = useBlueprintStore((s) => s.applyStreamEvent)
  const applyBlueprintTreeEvent = useBlueprintStore((s) => s.applyTreeEvent)
  const loadBlueprint = useBlueprintStore((s) => s.load)
  const resetBlueprintForProject = useBlueprintStore((s) => s.resetForProject)
  const setViewMode = useViewModeStore((s) => s.setMode)
  const currentProject = useProjectStore((s) => s.currentProject)
  const projects = useProjectStore((s) => s.projects)
  const setProjects = useProjectStore((s) => s.setProjects)
  const applyCanvasAction = useCanvasStore((s) => s.applyCanvasAction)
  const loadNodes = useCanvasStore((s) => s.loadNodes)
  const setCurrentProject = useProjectStore((s) => s.setCurrentProject)
  const updateCurrentProject = useProjectStore((s) => s.updateCurrentProject)

  useEffect(() => {
    setCollaborationMode(collaborationModeFromProject(currentProject))
  }, [currentProject])

  useEffect(() => {
    activeStreamRef.current = streaming
  }, [streaming])

  const isStreamActive = useCallback(() => {
    return activeStreamRef.current || useChatStore.getState().streaming
  }, [])
  const projectSlashCompletions = useMemo(
    () => (wantsProjectSelectionCompletion(input) ? buildProjectSlashCompletions(projects, currentProject?.id) : []),
    [currentProject?.id, input, projects],
  )
  const slashMatches = useMemo(
    () => (
      input.startsWith("/") && !input.includes("\n")
        ? filterSlashCommands(input, projectSlashCompletions)
        : []
    ),
    [input, projectSlashCompletions],
  )
  const slashMenuOpen = slashMatches.length > 0

  const refreshUiSettings = useCallback(async () => {
    try {
      const payload = await api.getRuntimeConfigFile<{
        parsed?: { app_settings?: Record<string, unknown> }
      }>(true)
      const settings = payload.parsed?.app_settings ?? {}
      setShowTokenMonitor(settings[TOKEN_MONITOR_SETTING_KEY] !== false)
    } catch {
      setShowTokenMonitor(true)
    }
  }, [])

  const refreshProjectList = useCallback(async () => {
    if (projectListLoadingRef.current) return
    projectListLoadingRef.current = true
    try {
      const items = await api.listProjects()
      setProjects(items as ProjectRecord[])
    } catch {
      // Project command completions are opportunistic; slash execution still works.
    } finally {
      projectListLoadingRef.current = false
    }
  }, [setProjects])

  useEffect(() => {
    if (!wantsProjectSelectionCompletion(input) || projects.length > 0) return
    void refreshProjectList()
  }, [input, projects.length, refreshProjectList])

  useEffect(() => {
    resetBlueprintForProject(currentProject?.id ?? null)
  }, [currentProject?.id, resetBlueprintForProject])

  const loadProjectSnapshot = useCallback(async (projectId: string) => {
    try {
      const [project, historyRes, nodesRes] = await Promise.all([
        api.getProject(projectId),
        api.getProjectMessages(projectId),
        api.getProjectNodes(projectId),
      ])
      const record = project as unknown as ProjectRecord
      try {
        localStorage.setItem("drama.currentProjectId", record.id)
      } catch {}
      setCurrentProject(record)
      resetBlueprintForProject(record.id)
      void loadBlueprint(record.id)
      if (Array.isArray(historyRes)) {
        loadHistory(historyRes as { id: string; role: string; content: string; created_at: string }[])
      }
      const nr = nodesRes as { nodes?: unknown[]; edges?: unknown[] }
      loadNodes(
        (Array.isArray(nr.nodes) ? nr.nodes : []) as {
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
          surface?: string | null
        }[],
        (Array.isArray(nr.edges) ? nr.edges : []) as { id: string; source_node_id: string; target_node_id: string; label?: string | null }[],
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      appendToLastAssistant(`\n\n错误：加载项目失败：${msg}`)
    }
  }, [
    appendToLastAssistant,
    loadBlueprint,
    loadHistory,
    loadNodes,
    resetBlueprintForProject,
    setActiveChecklist,
    setCurrentProject,
  ])

  useEffect(() => {
    refreshUiSettings()
    const handleConfigUpdate = () => {
      void refreshUiSettings()
    }
    window.addEventListener("drama:runtime-config-updated", handleConfigUpdate)
    return () => window.removeEventListener("drama:runtime-config-updated", handleConfigUpdate)
  }, [refreshUiSettings])

  useEffect(() => {
    if (!currentProject?.id) {
      setTokenUsage(null)
      return
    }
    const projectId = currentProject.id
    const loadStartedAt = Date.now()
    let active = true
    getAgentTokenUsage(projectId, null, 200)
      .then((summary) => {
        if (!active) return
        const current = useChatStore.getState().tokenUsage
        if (current?.projectId === projectId && current.updatedAt > loadStartedAt) return
        const latestRun = summary.by_run?.[0] ?? null
        const lastUsage = summary.last_usage ?? null
        if (!latestRun && !lastUsage) {
          setTokenUsage(null)
          return
        }
        setTokenUsage({
          projectId,
          runId: latestRun?.run_id ?? summary.run_id ?? "trace_summary",
          round: null,
          phase: "trace_summary",
          usage: lastUsage ?? {},
          runTotals: latestRun?.totals ?? {},
          sessionTotals: summary.totals ?? {},
          updatedAt: Date.now(),
        })
      })
      .catch(() => {
        if (active) setTokenUsage(null)
      })
    return () => {
      active = false
    }
  }, [currentProject?.id, setTokenUsage])

  useEffect(() => {
    if (autoScroll) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
    }
  }, [messages, autoScroll])

  useEffect(() => {
    if (!slashRunStatus || slashRunStatus.status === "running") return
    const timer = window.setTimeout(() => setSlashRunStatus(null), 3500)
    return () => window.clearTimeout(timer)
  }, [slashRunStatus])

  // Detect user-initiated scroll-up to pause auto-scroll; resume when back at bottom.
  const handleMessagesScroll = () => {
    const el = messagesScrollRef.current
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    const atBottom = distanceFromBottom < 40
    if (atBottom !== autoScroll) setAutoScroll(atBottom)
  }

  // textarea autosize: grows with content (max 8 rows)
  useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = "auto"
    const lineHeight = 22
    const maxH = lineHeight * 8 + 16
    const next = Math.min(maxH, el.scrollHeight)
    el.style.height = `${next}px`
  }, [input])

  const handleFiles = async (files: FileList | File[]) => {
    if (!currentProject) return
    const list = Array.from(files)
    for (const file of list) {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      addPendingAttachment({
        id,
        status: "uploading",
        filename: file.name,
        size: file.size,
      })
      try {
        const uploaded = await uploadFile(currentProject.id, file)
        updatePendingAttachment(id, { status: "ready", uploaded } as PendingAttachment)
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        updatePendingAttachment(id, {
          status: "error",
          filename: file.name,
          error: msg,
        } as PendingAttachment)
      }
    }
  }

  const handlePickFiles = () => fileInputRef.current?.click()

  const insertAttachmentMention = (mention: string) => {
    if (!mention || pendingInputRequestId || pendingActionRequestId) return
    setInput((prev) => {
      const needsSpace = prev.trim().length > 0 && !prev.endsWith(" ") && !prev.endsWith("\n")
      return `${prev}${needsSpace ? " " : ""}${mention} `
    })
    requestAnimationFrame(() => textareaRef.current?.focus())
  }

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      handleFiles(e.target.files)
      e.target.value = ""
    }
  }

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    if (!dragOver) setDragOver(true)
  }
  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
  }
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleFiles(e.dataTransfer.files)
    }
  }

  const handleStreamEvent = (event: ChatStreamEvent) => {
    console.info("[chat-ui:event]", event.type, event)
    if (event.type === "text_delta" && typeof event.content === "string") {
      console.info("[chat-ui:text_delta]", { length: event.content.length })
      appendToLastAssistant(event.content)
      return
    }
    if (isTokenUsageEvent(event)) {
      applyTokenUsageEvent(event)
      return
    }
    if (isInteractionInputEvent(event)) {
      const raw = event as Record<string, unknown>
      if (raw.intake && typeof raw.intake === "object") {
        setLastAssistantInteractionInput(raw.intake as InteractionInputPayload)
      }
      return
    }
    if (event.type === "subagent_round") {
      const rawText = String(event.content ?? "").trim()
      if (rawText) {
        const agentLabel = event.agent === "image_editor" ? "图片编辑" : String(event.agent || "子 Agent")
        const roundNo = -(Date.now() + Number(event.step || 0))
        addAgentRound({
          round: roundNo,
          content: rawText,
          source: "model",
          tools: event.tool ? [String(event.tool)] : [agentLabel],
        })
        completeAgentRound(roundNo)
      }
      return
    }
    if (event.type === "blueprint_tree_changed") {
      applyBlueprintTreeEvent(event as unknown as BlueprintTreeEvent, currentProject?.id)
      return
    }
    if (isBlueprintEvent(event)) {
      const blueprintEvent = event as BlueprintStreamEvent
      applyBlueprintEvent(blueprintEvent, currentProject?.id)
      const blueprintTitle = blueprintTitleFromEvent(blueprintEvent)
      if (blueprintTitle && currentProject?.id) {
        updateCurrentProject({ title: blueprintTitle })
      }
      if (event.type === "blueprint_revision_proposed") {
        setLastAssistantPendingAction(pendingActionFromBlueprintRevision(blueprintEvent))
      }
      const text = formatBlueprintEventSummary(event)
      if (text) appendToLastAssistant(text)
      if (event.type === "blueprint_approved") setViewMode("canvas")
      return
    }
    if (event.type === "mode_updated") {
      const raw = event as Record<string, unknown>
      const nextMode = normalizeCollaborationMode(raw.collaboration_mode ?? raw.mode)
      setCollaborationMode(nextMode)
      if (currentProject?.id) {
        const state = { ...parseProjectStateJson(currentProject), agent_collaboration_mode: nextMode }
        updateCurrentProject({ state_json: state })
      }
      return
    }
    if (event.type === "slash_command") {
      const rawCommand = String(event.command ?? "")
      const command = rawCommand.startsWith("/") ? rawCommand : `/${rawCommand}`
      const action = typeof event.action === "string" ? event.action : undefined
      const ok = event.ok !== false
      setSlashRunStatus({
        command,
        action,
        status: ok ? "completed" : "failed",
        message: ok ? "已完成" : String(event.error ?? "执行失败"),
      })
      return
    }
    if (event.type === "agent_round") {
      console.info("[chat-ui:agent_round]", {
        round: event.round,
        source: event.source,
        tools: event.tools,
        tool_agents: event.tool_agents,
        content: event.content,
      })
      if (isWorkflowSpecOnlyRound(event)) return
      completeAgentRound(0)
      const roundNo = Number(event.round)
      const tools = Array.isArray(event.tools) ? event.tools.map(String) : []
      const progressText = String(event.content ?? "").trim()
      addAgentRound({
        round: roundNo,
        content: progressText,
        source: event.source === "model" ? "model" : "action_summary",
        tools,
      })
      return
    }
    if (event.type === "agent_round_done") {
      console.info("[chat-ui:agent_round_done]", { round: event.round })
      completeAgentRound(Number(event.round))
      return
    }
    if (event.type === "tool_start" && event.tool) {
      if (isWorkflowSpecToolEvent(event)) return
      const tool = String(event.tool)
      console.info("[chat-ui:tool_start]", { round: event.round, tool, content: event.content })
      addToolBubble(tool)
      addAgentRoundToolStart(tool, String(event.content ?? ""))
      return
    }
    if (event.type === "tool_done" && event.tool) {
      const tool = String(event.tool)
      const result = event.result
      console.info("[chat-ui:tool_done]", { round: event.round, tool, result })
      const workflowSpecPreview = workflowSpecPreviewFromToolResult(result)
      if (workflowSpecPreview) {
        window.dispatchEvent(new CustomEvent("openreel:workflow-spec-preview", {
          detail: workflowSpecPreview,
        }))
        return
      }
      if (shouldRefreshWorkflowForTool(tool, result)) {
        requestWorkflowRefresh({ projectId: currentProject?.id })
      }
      if (isWorkflowSpecToolEvent(event)) return
      const summary = summarizeAgentRoundToolResult(tool, result)
      const resultObj = result as Record<string, unknown> | null
      const awaitingConfirmation = Boolean(resultObj?.requires_user_confirm) && !resultObj?.error
      const failed = Boolean(
        result && typeof result === "object" &&
        !awaitingConfirmation &&
        ("error" in result || ("ok" in result && result.ok === false))
      )
      updateToolBubble(tool, { status: failed ? "failed" : "completed" })
      addAgentRoundToolResult(summary)
      return
    }
    if (event.type === "step_start") {
      const idx = event.step_index as number
      const total = event.total as number
      const title = String(event.title ?? event.tool ?? "")
      const tool = String(event.tool ?? "")
      const currentProgress = useChatStore.getState().messages
      const lastMsg = [...currentProgress].reverse().find((m) => m.role === "assistant")
      if (!lastMsg?.stepProgress && total > 0) {
        const steps = Array.from({ length: total }, (_, i) => ({
          title: i === idx ? title : `步骤 ${i + 1}`,
          tool: i === idx ? tool : "",
        }))
        initStepProgress(steps)
      } else if (lastMsg?.stepProgress) {
        const steps = [...lastMsg.stepProgress.steps]
        if (idx < steps.length && steps[idx].title.startsWith("步骤")) {
          steps[idx] = { ...steps[idx], title, tool }
          initStepProgress(steps.map((s) => ({ title: s.title, tool: s.tool })))
        }
      }
      advanceStep(idx, "running")
      return
    }
    if (event.type === "step_done") {
      const idx = event.step_index as number
      const status = event.status === "failed" ? "failed" : "completed"
      advanceStep(idx, status)
      return
    }
    if (event.type === "canvas_action" && event.payload) {
      const payload = event.payload as Record<string, unknown>
      if (event.action === "clear_all" || event.action === "delete_node" || event.action === "add_edge" || event.action === "delete_edge" || event.action === "remove_edge") {
        applyCanvasAction(String(event.action), payload)
        return
      }
      const nodeId = String(payload.id ?? "")
      if (!nodeId) return
      const workflowManaged = isWorkflowManagedCanvasPayload(payload)
      if (event.action === "create_node") {
        setViewMode("canvas")
        if (!workflowManaged) {
          setLastAssistantNode({
            nodeId,
            type: String(payload.type ?? ""),
            title: String(payload.title ?? ""),
            status: "running",
          })
        }
        applyCanvasAction("create_node", payload)
      } else if (event.action === "update_node") {
        setViewMode("canvas")
        const status = String(payload.status ?? "")
        if (!workflowManaged && (status === "completed" || status === "failed")) {
          updateLastAssistantNode(nodeId, { status })
        }
        applyCanvasAction("update_node", payload)
        const projectId = currentProject?.id
        if (projectId) {
          void api.getProjectNodes(projectId).then((nodesRes) => {
            const nr = nodesRes as { nodes?: unknown[]; edges?: unknown[] }
            if (Array.isArray(nr.nodes)) {
              loadNodes(
                nr.nodes as {
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
                  surface?: string | null
                }[],
                (nr.edges ?? []) as { id: string; source_node_id: string; target_node_id: string; label?: string | null }[],
                { preserveOnEmpty: true, preserveLayout: true },
              )
            }
          }).catch(console.error)
        }
      }
      return
    }
    if (event.type === "proposed_plan") {
      const plan = event.plan as unknown as PlanDoc
      if (plan && plan.id) {
        setLastAssistantProposedPlan(plan)
      }
      return
    }
    if (event.type === "confirm_required") {
      setLastAssistantPendingAction(pendingActionFromConfirmRequired(event))
      return
    }
    if (event.type === "queued") {
      const cnt = event.queued_count as number | undefined
      if (typeof cnt === "number") setQueuedCount(cnt)
      if (event.error) respondSystem(`排队失败：${event.error}`)
      return
    }
    if (event.type === "merged_messages") {
      const cnt = event.count as number | undefined
      if (typeof cnt === "number") setQueuedCount(cnt)
      return
    }
    if (event.type === "queued_turn_started") {
      const clientId = typeof event.client_user_message_id === "string" ? event.client_user_message_id : ""
      const display = String(event.message ?? "").trim()
      const remaining = typeof event.queued_remaining === "number" ? event.queued_remaining : 0
      setQueuedCount(Math.max(0, remaining))
      if (clientId) {
        const exists = useChatStore
          .getState()
          .messages
          .some((message) => message.metadata?.clientUserMessageId === clientId)
        if (!exists && display) {
          appendMessage({
            role: "user",
            content: display,
            id: clientId,
            createdAt: new Date().toISOString(),
            metadata: {
              clientUserMessageId: clientId,
              queueStatus: "processing",
            },
          })
        }
        markQueuedUserMessage(clientId, { queueStatus: "processing", queuePosition: null })
        ensureAssistantAfterQueuedUser(clientId)
        refreshQueuedUserMessagePositions()
      }
      return
    }
    if (event.type === "cancel_requested") {
      respondSystem("已请求停止当前生成。")
      return
    }
    if (event.type === "cancelled") {
      respondSystem(String(event.message ?? "当前生成已停止。"))
      return
    }
    if (event.type === "checklist_updated") {
      const items = event.checklist
      if (Array.isArray(items)) {
        setActiveChecklist(items as ChecklistItem[])
      }
      return
    }
    if (event.type === "project_update" && event.updates) {
      updateCurrentProject(event.updates as Partial<Record<string, unknown>>)
      return
    }
    if (event.type === "project_reset") {
      const title = String(event.title ?? "未命名项目")
      resetProjectRuntime({ clearMessages: true })
      applyCanvasAction("clear_all", {})
      resetBlueprintForProject(currentProject?.id ?? null)
      updateCurrentProject({ title })
      setViewMode("canvas")
      appendMessage({
        id: `${Date.now()}-project-reset`,
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
      })
      return
    }
    if (event.type === "project_switch" && event.project_id) {
      const newId = String(event.project_id)
      const title = String(event.title ?? "新项目")
      pendingProjectSwitchRef.current = newId
      localStorage.setItem("drama.currentProjectId", newId)
      resetProjectRuntime({ clearMessages: true })
      applyCanvasAction("clear_all", {})
      resetBlueprintForProject(newId)
      setActiveChecklist([])
      setCurrentProject({ id: newId, title })
      void refreshProjectList()
      setViewMode("canvas")
      appendMessage({
        id: `${Date.now()}-project-switch`,
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
      })
      return
    }
    if (event.type === "error") {
      const msg = String(event.message ?? "")
      const recoverable = event.recoverable as boolean | undefined
      // 方案执行失败但已还原 → 提示用户可重试
      if (recoverable) {
        appendToLastAssistant(
          `\n\n错误：${msg}\n\n> 提示：方案已还原，你可以修改需求后重新批准执行。`
        )
        return
      }
      // 项目已被删除 → 清掉旧 id,自动建一个新项目,提示用户重发
      if (/Project\s.*not found/i.test(msg)) {
        try {
          localStorage.removeItem("drama.currentProjectId")
        } catch {}
        appendToLastAssistant(
          "\n\n错误：当前项目已被删除。正在为你创建新项目,创建完成后请重新发送消息。"
        )
        // 异步创建新项目并切换
        ;(async () => {
          try {
            const created = (await api.createProject({
              title: "未命名项目",
              genre: "",
              episode_count: 1,
              budget_level: "low",
            })) as { id: string; title: string }
            try {
              localStorage.setItem("drama.currentProjectId", created.id)
            } catch {}
            updateCurrentProject({ id: created.id, title: created.title })
          } catch (e) {
            appendToLastAssistant(`\n\n错误：创建新项目失败:${String(e)}`)
          }
        })()
        return
      }
      appendToLastAssistant(`\n\n错误：${msg}`)
    }
  }

  useEffect(() => {
    handleStreamEventRef.current = handleStreamEvent
  })

  const ensureLiveAssistantPlaceholder = useCallback(() => {
    const current = useChatStore.getState().messages
    const last = current[current.length - 1]
    if (last?.role === "assistant") return
    appendMessage({
      role: "assistant",
      content: "",
      id: `live-${Date.now()}-a`,
      createdAt: new Date().toISOString(),
      nodes: [],
    })
  }, [appendMessage])

  useEffect(() => {
    if (!currentProject?.id) {
      setQueuedCount(0)
      setLocalStreamActive(false)
      activeStreamRef.current = false
      setStreaming(false)
      return
    }
    let cancelled = false
    const projectId = currentProject.id
    void getChatQueueStatus(projectId)
      .then((status) => {
        if (cancelled) return
        if (typeof status.queued === "number") setQueuedCount(status.queued)
        const active = Boolean(status.streaming || status.running)
        if (active) {
          activeStreamRef.current = true
          setStreaming(true)
          ensureLiveAssistantPlaceholder()
        } else {
          activeStreamRef.current = false
          setStreaming(false)
          setStopping(false)
        }
      })
      .catch(console.error)
    return () => {
      cancelled = true
    }
  }, [currentProject?.id, ensureLiveAssistantPlaceholder, setStreaming])

  useEffect(() => {
    if (!currentProject?.id || localStreamActive) return
    const projectId = currentProject.id
    const es = new EventSource(`${getApiBaseSync()}/api/chat/events/${projectId}`)

    es.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data) as ChatStreamEvent
        if (event.type === "subscribed") return
        if (isRunProgressEvent(event)) {
          activeStreamRef.current = true
          setStreaming(true)
        }
        if (
          event.type !== "done" &&
          event.type !== "canvas_action" &&
          event.type !== "blueprint_tree_changed" &&
          event.type !== "token_usage"
        ) {
          ensureLiveAssistantPlaceholder()
        }
        handleStreamEventRef.current(event)
        if (isRunTerminalEvent(event)) {
          streamCancelRef.current = null
          activeStreamRef.current = false
          setStreaming(false)
          setStopping(false)
          void Promise.all([
            api.getProjectMessages(projectId),
            api.getProjectNodes(projectId),
          ]).then(([historyRes, nodesRes]) => {
            if (Array.isArray(historyRes)) {
              loadHistory(historyRes as { id: string; role: string; content: string; created_at: string }[])
            }
            const nr = nodesRes as { nodes?: unknown[]; edges?: unknown[] }
            if (Array.isArray(nr.nodes)) {
              loadNodes(
                nr.nodes as {
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
                  render_state?: string | null
                  surface?: string | null
                }[],
                (nr.edges ?? []) as { id: string; source_node_id: string; target_node_id: string; label?: string | null }[],
                { preserveOnEmpty: true, preserveLayout: true },
              )
            }
          }).catch(console.error)
        }
      } catch {
        // Ignore malformed keepalive or partial project events.
      }
    }
    es.onerror = () => {
      // EventSource reconnects automatically while the project page is open.
    }
    return () => es.close()
  }, [
    currentProject?.id,
    ensureLiveAssistantPlaceholder,
    loadHistory,
    loadNodes,
    localStreamActive,
    setStreaming,
  ])

  const respondAssistant = (text: string) => {
    appendMessage({
      role: "assistant",
      content: text,
      id: `${Date.now()}-sys`,
      createdAt: new Date().toISOString(),
    })
  }

  const respondSystem = (text: string) => {
    appendMessage({
      role: "system",
      content: text,
      id: `${Date.now()}-sys-${Math.random().toString(36).slice(2, 6)}`,
      createdAt: new Date().toISOString(),
    })
  }

  const formatStatus = (r: Record<string, unknown>): string => {
    const models = (r.models ?? {}) as Record<string, string>
    const provs = (r.providers_configured ?? {}) as Record<string, boolean>
    const byNs = (r.tools_by_namespace ?? {}) as Record<string, number>
    const mcp = (r.mcp_servers ?? []) as { name: string; status: string; tools: number }[]
    const lines: string[] = []
    lines.push("**系统状态**\n")
    lines.push(`- 工具总数:**${r.tools_total}** / 命名空间:${r.namespaces}`)
    lines.push(`- Agent Loop 最大轮数:${r.agent_loop_max_iterations}`)
    lines.push("\n**模型映射**")
    for (const [task, m] of Object.entries(models)) lines.push(`- ${task} → \`${m}\``)
    lines.push("\n**Provider 配置**")
    for (const [p, ok] of Object.entries(provs)) lines.push(`- ${p}: ${ok ? "OK" : "未配置"}`)
    lines.push("\n**命名空间分布**")
    const sorted = Object.entries(byNs).sort((a, b) => b[1] - a[1])
    for (const [ns, n] of sorted) lines.push(`- ${ns}: ${n}`)
    if (mcp.length > 0) {
      lines.push("\n**MCP**")
      for (const s of mcp) lines.push(`- ${s.name} [${s.status}] ${s.tools} 工具`)
    } else {
      lines.push("\n**MCP**:未连接外部 server")
    }
    return lines.join("\n")
  }

  const formatModels = (r: Record<string, unknown>): string => {
    const tm = (r.task_models ?? {}) as Record<string, string>
    const lines = ["**当前模型映射**\n"]
    for (const [task, m] of Object.entries(tm)) lines.push(`- ${task} → \`${m}\``)
    if (r.note) lines.push(`\n_${r.note}_`)
    return lines.join("\n")
  }

  const formatMcp = (r: Record<string, unknown>): string => {
    const servers = (r.servers ?? r ?? []) as unknown
    const list = Array.isArray(servers) ? servers : []
    if (list.length === 0) return "**MCP**:未连接外部 server。在 `apps/api/mcp_servers.json` 配置后重启。"
    const lines = ["**MCP 连接状态**\n"]
    for (const s of list as Record<string, unknown>[]) {
      lines.push(`- **${s.name}** [${s.status ?? "?"}] ${s.tool_count ?? s.tools ?? 0} 工具`)
    }
    return lines.join("\n")
  }

  const formatConfig = (r: Record<string, unknown>): string => {
    const llm = (r.llm ?? []) as Array<{ task: string; model: string; source: string; temperature?: number; max_tokens?: number }>
    const image = (r.image ?? []) as Array<{ name: string; model_name: string; is_active: boolean; api_format?: string }>
    const video = (r.video ?? []) as Array<{ name: string; model_name: string; is_active: boolean }>
    const keys = (r.api_keys ?? {}) as Record<string, boolean>
    const summary = (r.summary ?? {}) as { active_image?: string | null; active_video?: string | null }

    const lines: string[] = ["**统一配置总览**\n"]

    lines.push("**LLM 模型**")
    for (const e of llm) {
      const tag = e.source === "db" ? "已配置" : "默认"
      lines.push(`- ${e.task} → \`${e.model}\` _(${tag})_`)
    }

    lines.push("\n**图片 Provider**")
    if (image.length === 0) {
      lines.push("- _未配置 — 在聊天中说「添加 SiliconFlow flux-pro provider」即可_")
    } else {
      const active = summary.active_image ?? null
      lines.push(`- 激活: ${active ? `\`${active}\`` : "_无_"}`)
      for (const p of image) {
        const star = p.is_active ? "★ " : "  "
        lines.push(`- ${star}\`${p.name}\` → ${p.model_name}`)
      }
    }

    lines.push("\n**视频 Provider**")
    if (video.length === 0) {
      lines.push("- _未配置 (P3 — 后端目前是 stub)_")
    } else {
      const active = summary.active_video ?? null
      lines.push(`- 激活: ${active ? `\`${active}\`` : "_无_"}`)
      for (const p of video) {
        const star = p.is_active ? "★ " : "  "
        lines.push(`- ${star}\`${p.name}\` → ${p.model_name}`)
      }
    }

    lines.push("\n**API Keys**")
    for (const [k, ok] of Object.entries(keys)) {
      lines.push(`- ${k}: ${ok ? "OK 已配置" : "未配置"}`)
    }
    lines.push("\n_LLM 模型映射通过设置面板或配置 API 修改；API Key 在 .env.local 改后重启_")
    return lines.join("\n")
  }

  const handleSlashCommand = async (cmd: string): Promise<boolean> => {
    const parts = cmd.trim().split(/\s+/)
    const command = parts[0].toLowerCase()

    try {
      switch (command) {
        case "/help":
          respondAssistant(
            "**可用命令(本地直调,不消耗 LLM tokens)**\n\n" +
            "- `/help` — 显示此帮助\n" +
            "- `/plan [目标|execute|exit]` — 进入只读 Plan Mode、执行最近计划或退出(后端确定性执行)\n" +
            "- `/workflow [exit]` — 进入或退出工作流搭建模式(后端确定性执行)\n" +
            "- `/reset [failed|full|confirm|cancel]` — 清理失败节点或确认重置(后端确定性执行)\n" +
            "- `/doctor` — 项目诊断快照(后端确定性执行)\n" +
            "- `/status` — 系统状态(模型/工具/MCP)\n" +
            "- `/config` — LLM/图片/视频/API Keys 配置总览\n" +
            "- `/model` — 当前模型映射\n" +
            "- `/project [new|switch|delete]` — 查看/新建/切换/删除项目(后端确定性执行)\n" +
            "- `/mcp` — 外部 MCP server 连接状态\n" +
            "- `/clear` — 清空模型可见对话、任务和流程运行态，保留画布节点和资产\n\n" +
            "↑↓ 选择 / Enter 或 Tab 补全 / Esc 关闭",
          )
          return true

        case "/clear":
          if (currentProject?.id) {
            try {
              const { clearProjectSession } = await import("@/lib/api")
              const result = await clearProjectSession(currentProject.id)
              useChatStore.getState().clearMessages()
              respondAssistant(
                `对话上下文已清空，画布节点和资产已保留。已归档 ${result.archived_messages ?? 0} 条消息，清理 ${result.cleared_tasks ?? 0} 个任务。`,
              )
            } catch (error) {
              respondAssistant(`清空上下文失败：${error instanceof Error ? error.message : String(error)}`)
            }
          } else {
            useChatStore.getState().clearMessages()
            respondAssistant("本地对话已清空。")
          }
          return true

        case "/status": {
          const r = await callTool<Record<string, unknown>>("system.status")
          respondAssistant(`${formatStatus(r)}\n\n${formatTokenStatus(tokenUsage)}`)
          return true
        }

        case "/config": {
          const { getRuntimeConfigSummary } = await import("@/lib/api")
          const r = await getRuntimeConfigSummary<Record<string, unknown>>()
          respondAssistant(formatConfig(r))
          return true
        }

        case "/model": {
          const r = await callTool<Record<string, unknown>>("system.models")
          respondAssistant(formatModels(r))
          return true
        }

        case "/mcp": {
          const { listMcpServers } = await import("@/lib/api")
          const r = await listMcpServers()
          respondAssistant(formatMcp(r))
          return true
        }

        default:
          return false
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      respondAssistant(`执行 ${command} 失败:${msg}`)
      return true
    }
  }

  const stopCurrentGeneration = async (reason = "用户请求停止当前生成") => {
    if (!currentProject || stopping) return
    setStopping(true)
    try {
      await cancelChat(currentProject.id, reason)
      streamCancelRef.current?.()
      streamCancelRef.current = null
      activeStreamRef.current = false
      setLocalStreamActive(false)
      setStreaming(false)
      respondSystem("已停止当前生成。")
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err)
      respondSystem(`停止失败：${errMsg}`)
    } finally {
      setStopping(false)
    }
  }

  const sendChatToBackend = async (
    userMessage: string,
    readyAttachments: UploadedAttachment[],
    displayContent: string,
    slashMeta?: { command: string; action?: string } | null,
    decisionInputs?: Record<string, unknown> | null,
    referencedNodeIds: string[] = [],
  ) => {
    if (!currentProject) return
    const clientUserMessageId = `${Date.now()}-u`
    activeStreamRef.current = true
    setLocalStreamActive(true)
    appendMessage({
      role: "user",
      content: displayContent,
      id: clientUserMessageId,
      createdAt: new Date().toISOString(),
      metadata: {
        clientUserMessageId,
        ...(decisionInputs ? { decisionInputs } : {}),
        ...(readyAttachments.length > 0 ? { attachments: readyAttachments } : {}),
        ...(referencedNodeIds.length > 0 ? { referencedNodeIds } : {}),
      },
    })
    appendMessage({
      role: "assistant",
      content: "",
      id: `${Date.now()}-a`,
      createdAt: new Date().toISOString(),
      nodes: [],
    })
    if (slashMeta) {
      setSlashRunStatus({
        command: slashMeta.command,
        action: slashMeta.action,
        status: "running",
        message: "执行中",
      })
    }
    setStreaming(true)

    try {
      const cancel = await chatStream(currentProject.id, userMessage, (event) => {
        handleStreamEvent(event)
        if (event.type === "done" || event.type === "error" || event.type === "cancelled") {
          streamCancelRef.current = null
          activeStreamRef.current = false
          setLocalStreamActive(false)
          setStreaming(false)
          setStopping(false)
          if (slashMeta) {
            setSlashRunStatus((prev) => {
              if (!prev || prev.command !== slashMeta.command || prev.status !== "running") return prev
              return {
                ...prev,
                status: event.type === "error" ? "failed" : "completed",
                message: event.type === "error" ? String(event.message ?? "执行失败") : "已完成",
              }
            })
          }
          if (event.type !== "error") setLastFailed(null)
          const switchedProjectId = pendingProjectSwitchRef.current
          if (switchedProjectId && event.type === "done") {
            pendingProjectSwitchRef.current = null
            void loadProjectSnapshot(switchedProjectId)
          }
        }
      }, readyAttachments, decisionInputs, clientUserMessageId, referencedNodeIds)
      streamCancelRef.current = cancel
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err)
      appendToLastAssistant(`\n\n连接失败：${errMsg}`)
      if (slashMeta) {
        setSlashRunStatus({
          command: slashMeta.command,
          action: slashMeta.action,
          status: "failed",
          message: errMsg,
        })
      }
      setLastFailed(userMessage)
      activeStreamRef.current = false
      setLocalStreamActive(false)
      setStreaming(false)
    }
  }

  const handleInteractionInputSubmit = async (
    message: string,
    decisionInputs?: Record<string, unknown> | null,
  ) => {
    if (!currentProject || isStreamActive()) return
    if (pendingAttachments.some((a) => a.status === "uploading")) {
      respondSystem("参考图还在上传，上传完成后再提交。")
      return
    }
    const readyAttachments = decorateAttachmentsForSend(
      pendingAttachments
        .filter((a) => a.status === "ready")
        .map((a) => (a as { status: "ready"; uploaded: UploadedAttachment }).uploaded),
    )
    const attachmentLines = attachmentDisplayLines(readyAttachments)
    const displayContent = attachmentLines ? `${message}\n${attachmentLines}` : message
    clearPendingAttachments()
    await sendChatToBackend(message, readyAttachments, displayContent, null, decisionInputs, selectedCanvasNodeIdsForPrompt())
  }

  const handlePendingActionResolve = async (
    pendingAction: PendingActionPayload,
    decision: "confirm" | "cancel",
  ) => {
    if (!currentProject || isStreamActive()) return
    const confirmed = decision === "confirm"
    markLastAssistantPendingActionStatus(confirmed ? "confirmed" : "cancelled")
    const userMessage = confirmed
      ? pendingAction.confirmMessage ?? "确认执行"
      : pendingAction.cancelMessage ?? "取消"
    const displayContent = confirmed
      ? pendingAction.confirmDisplay ?? pendingAction.confirmLabel ?? "确认"
      : pendingAction.cancelDisplay ?? pendingAction.cancelLabel ?? "取消"
    const values = {
      ...(pendingAction.values ?? {}),
      target: pendingAction.target,
      requested_action: pendingAction.action,
      decision,
    }
    const decisionInputs = buildDecisionInputs({
      kind: pendingAction.kind || "confirmation",
      target: pendingAction.target,
      action: decision,
      values,
      extra: {
        title: pendingAction.title,
        risk: pendingAction.risk,
      },
    })
    await sendChatToBackend(userMessage, [], displayContent, parseSlashMeta(userMessage), decisionInputs)
  }

  const handleSend = async () => {
    if (!currentProject) return
    const streamActive = isStreamActive()
    const latest = messages[messages.length - 1]
    if (latest?.role === "assistant" && latest.interactionInput) return
    if (latest?.role === "assistant" && latest.pendingAction && (latest.pendingAction.status ?? "pending") === "pending") return
    const readyAttachments: UploadedAttachment[] = decorateAttachmentsForSend(
      pendingAttachments
        .filter((a) => a.status === "ready")
        .map((a) => (a as { status: "ready"; uploaded: UploadedAttachment }).uploaded),
    )

    if (!input.trim() && readyAttachments.length === 0) return
    if (pendingAttachments.some((a) => a.status === "uploading")) return

    const userMessage = input.trim() || "(看一下我刚发的附件)"
    setInput("")
    clearPendingAttachments()

    const attachmentLines = attachmentDisplayLines(readyAttachments)
    const displayContent = attachmentLines
      ? `${userMessage}\n${attachmentLines}`
      : userMessage

    const slashMeta = readyAttachments.length === 0 ? parseSlashMeta(userMessage) : null
    if (slashMeta) {
      if (streamActive) {
        respondSystem("当前项目已有任务在执行，slash command 不会排队。等它结束后再发送。")
        return
      }
      if (isLocalSlashCommand(userMessage)) {
        await handleSlashCommand(userMessage)
        return
      }
      await sendChatToBackend(userMessage, [], userMessage, slashMeta)
      return
    }

    if (streamActive) {
      if (isStopMessage(userMessage)) {
        await stopCurrentGeneration(userMessage)
        return
      }
      const clientUserMessageId = `${Date.now()}-u`
      const referencedNodeIds = selectedCanvasNodeIdsForPrompt()
      appendMessage({
        role: "user",
        content: displayContent,
        id: clientUserMessageId,
        createdAt: new Date().toISOString(),
        metadata: {
          clientUserMessageId,
          queueStatus: "sending",
          ...(readyAttachments.length > 0 ? { attachments: readyAttachments } : {}),
          ...(referencedNodeIds.length > 0 ? { referencedNodeIds } : {}),
        },
      })
      try {
        const queued = await enqueueChat(
          currentProject.id,
          userMessage,
          readyAttachments,
          clientUserMessageId,
          referencedNodeIds,
        )
        if (queued.error || queued.ok === false) {
          markQueuedUserMessage(clientUserMessageId, {
            queueStatus: "failed",
            queueError: queued.error ? String(queued.error) : "排队失败，请稍后再试。",
          })
          respondSystem(queued.error ? `排队失败：${queued.error}` : "排队失败，请稍后再试。")
        } else {
          const count = queued.queued_count ?? queuedCount + 1
          setQueuedCount(count)
          markQueuedUserMessage(clientUserMessageId, {
            queueStatus: "queued",
            queuePosition: count,
            queueError: null,
          })
        }
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : String(err)
        markQueuedUserMessage(clientUserMessageId, {
          queueStatus: "failed",
          queueError: errMsg,
        })
        respondSystem(`排队失败：${errMsg}`)
      }
      return
    }

    await sendChatToBackend(userMessage, readyAttachments, displayContent, null, null, selectedCanvasNodeIdsForPrompt())
  }

  const handleCancelQueuedMessage = useCallback(async (message: ChatMessage) => {
    if (!currentProject) return
    const clientId = typeof message.metadata?.clientUserMessageId === "string"
      ? message.metadata.clientUserMessageId
      : ""
    if (!clientId) return
    const status = typeof message.metadata?.queueStatus === "string" ? message.metadata.queueStatus : ""
    if (status === "failed") {
      removeQueuedUserMessage(clientId)
      return
    }
    if (status !== "queued") return
    markQueuedUserMessage(clientId, { queueStatus: "cancelling", queuePosition: null })
    try {
      const result = await dequeueChat(currentProject.id, clientId)
      if (typeof result.queued_count === "number") setQueuedCount(result.queued_count)
      if (result.removed) {
        removeQueuedUserMessage(clientId)
        refreshQueuedUserMessagePositions()
        return
      }
      markQueuedUserMessage(clientId, {
        queueStatus: "processing",
        queuePosition: null,
        queueError: result.error ? String(result.error) : null,
      })
      respondSystem("这条追加消息已经开始处理，不能删除。")
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err)
      markQueuedUserMessage(clientId, {
        queueStatus: "queued",
        queueError: errMsg,
      })
      respondSystem(`删除失败：${errMsg}`)
    }
  }, [currentProject, markQueuedUserMessage, refreshQueuedUserMessagePositions, removeQueuedUserMessage])

  const handleProposedPlanExecute = useCallback(async () => {
    if (!currentProject || isStreamActive()) return
    await sendChatToBackend("/plan execute", [], "执行上一条计划", { command: "/plan", action: "execute" })
  // sendChatToBackend captures current store actions from this render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject, isStreamActive])

  const handleSlashSelect = (cmd: SlashCommandDef) => {
    const insertText = slashCompletionText(cmd)
    if (cmd.usage || cmd.insertOnly) {
      setInput(insertText)
    } else {
      setInput("")
      if (isLocalSlashCommand(insertText)) {
        void handleSlashCommand(insertText)
      } else if (currentProject && !isStreamActive()) {
        void sendChatToBackend(insertText, [], insertText, parseSlashMeta(insertText))
      } else if (isStreamActive()) {
        respondSystem("当前项目已有任务在执行，slash command 不会排队。等它结束后再发送。")
      }
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (slashMenuOpen) {
      const filtered = filterSlashCommands(input, projectSlashCompletions)
      if (filtered.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault()
          setSlashSelectedIdx((i) => (i + 1) % filtered.length)
          return
        }
        if (e.key === "ArrowUp") {
          e.preventDefault()
          setSlashSelectedIdx((i) => (i - 1 + filtered.length) % filtered.length)
          return
        }
        if (e.key === "Tab") {
          e.preventDefault()
          const cmd = filtered[Math.min(slashSelectedIdx, filtered.length - 1)]
          setInput(slashCompletionText(cmd))
          return
        }
        if (e.key === "Escape") {
          e.preventDefault()
          setInput("")
          return
        }
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault()
          const cmd = filtered[Math.min(slashSelectedIdx, filtered.length - 1)]
          if (input.trim() === cmd.name && !cmd.usage) {
            handleSlashSelect(cmd)
          } else if (input.trim() !== cmd.name) {
            handleSlashSelect(cmd)
          } else {
            handleSend()
          }
          return
        }
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  useEffect(() => {
    setSlashSelectedIdx(0)
  }, [input])

  const latestMessage = messages[messages.length - 1]
  const pendingInputRequestId =
    latestMessage?.role === "assistant" && latestMessage.interactionInput
      ? latestMessage.id
      : null
  const pendingActionRequestId =
    latestMessage?.role === "assistant" &&
    latestMessage.pendingAction &&
    (latestMessage.pendingAction.status ?? "pending") === "pending"
      ? latestMessage.id
      : null
  const liveAssistantIndex = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === "assistant") return index
    }
    return -1
  }, [messages])

  const sendDisabled =
    !currentProject ||
    Boolean(pendingInputRequestId) ||
    Boolean(pendingActionRequestId) ||
    pendingAttachments.some((a) => a.status === "uploading") ||
    (!input.trim() && !pendingAttachments.some((a) => a.status === "ready"))

  const slashStatusText = slashRunStatus
    ? `${slashLabel(slashRunStatus)} ${slashRunStatus.message ?? (
        slashRunStatus.status === "running" ? "执行中" : slashRunStatus.status === "completed" ? "已完成" : "失败"
      )}`
    : ""
  const slashDotClass =
    slashRunStatus?.status === "running"
      ? "bg-indigo-400 animate-pulse"
      : slashRunStatus?.status === "completed"
        ? "bg-emerald-400"
        : "bg-red-400"

  return (
    <div
      className={`flex flex-col h-full bg-[var(--studio-bg)] relative ${
        dragOver ? "ring-2 ring-indigo-500/60 ring-inset" : ""
      }`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-indigo-950/60 backdrop-blur-sm text-indigo-200 text-sm">
          松开以上传文件
        </div>
      )}
      <ChecklistPanel />
      <div
        ref={messagesScrollRef}
        onScroll={handleMessagesScroll}
        className="flex-1 space-y-4 overflow-y-auto bg-[radial-gradient(circle_at_top_left,rgba(99,102,241,0.10),transparent_34%),linear-gradient(180deg,rgba(255,255,255,0.025),transparent_26%)] px-3 py-4 sm:space-y-5 sm:px-5 sm:py-5"
      >
        {messages.length === 0 && (
          <div className="text-center text-gray-500 text-sm mt-12">
            <img
              src={APP_ICON_SRC}
              alt="OpenReel Studio"
              className="mx-auto mb-3 h-16 w-16 object-contain drop-shadow-[0_14px_28px_rgba(6,182,212,0.18)]"
              draggable={false}
            />
            <p className="text-gray-300">你好，我是 OpenReel Agent。</p>
            <p className="mt-1">告诉我你想创作什么样的视频，我会帮你完成。</p>
            <div className="mt-6 space-y-1.5 text-xs text-gray-600">
              <p>试试：</p>
              <p className="text-gray-400">「做一支 30 秒国风动作短片」</p>
              <p className="text-gray-400">「基于画布人物生成一张双人参考图」</p>
              <p className="text-gray-400">「规划一个 3 集短剧项目」</p>
              <p className="text-gray-500 mt-2">或拖一个剧本 / 图片到这里</p>
            </div>
          </div>
        )}
        {messages.map((msg, idx) => (
          <MemoMessage
            key={msg.id}
            msg={msg}
            streaming={streaming && idx === liveAssistantIndex}
            planActionsDisabled={streaming}
            interactionInputDisabled={
              streaming ||
              msg.id !== pendingInputRequestId ||
              pendingAttachments.some((a) => a.status === "uploading")
            }
            pendingActionDisabled={streaming || msg.id !== pendingActionRequestId}
            onProposedPlanExecute={handleProposedPlanExecute}
            onInteractionInputSubmit={handleInteractionInputSubmit}
            onPendingActionResolve={handlePendingActionResolve}
            onCancelQueuedMessage={handleCancelQueuedMessage}
          />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {!autoScroll && (
        <button
          onClick={() => {
            setAutoScroll(true)
            messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
          }}
          className="absolute bottom-24 right-6 z-20 rounded-full bg-indigo-600/90 hover:bg-indigo-500 text-white text-xs px-3 py-1.5 shadow-lg backdrop-blur-sm flex items-center gap-1"
        >
          <span>↓</span> 回到最新
        </button>
      )}

      <div className="border-t border-white/10 bg-[var(--studio-panel)] px-3 py-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] shadow-[0_-18px_40px_rgba(0,0,0,0.28)] sm:px-4">
        <TokenMonitorBar snapshot={tokenUsage} visible={showTokenMonitor} />
        <div className="mb-2 flex items-center justify-between gap-2 text-xs">
          <span className={`inline-flex items-center rounded-md border px-2 py-1 ${collaborationModeClass(collaborationMode)}`}>
            模式 · {collaborationModeLabel(collaborationMode)}
          </span>
        </div>
        {(streaming || queuedCount > 0 || slashRunStatus) && (
          <div className="mb-2 flex items-center justify-between rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-xs text-zinc-300">
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full ${slashRunStatus ? slashDotClass : "bg-emerald-400 animate-pulse"}`} />
              <span>
                {slashRunStatus
                  ? slashStatusText
                  : streaming
                    ? "Agent 正在工作，可继续发送补充消息"
                    : "准备继续处理"}
              </span>
              {queuedCount > 0 && <span className="text-zinc-500">队列 {queuedCount}</span>}
            </div>
            {streaming && (
              <button
                onClick={() => stopCurrentGeneration("用户点击停止生成")}
                disabled={stopping}
                className="rounded-md border border-red-400/30 bg-red-500/10 px-2.5 py-1 text-[11px] text-red-200 hover:bg-red-500/20 disabled:opacity-50"
              >
                {stopping ? "停止中" : "停止生成"}
              </button>
            )}
          </div>
        )}
        {pendingAttachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {(() => {
              let imageIndex = 0
              return pendingAttachments.map((a, index) => {
                const filename =
                  a.status === "ready" ? a.uploaded.filename : a.filename
                const kind =
                  a.status === "ready" ? a.uploaded.kind : null
                if (kind === "image") imageIndex += 1
                const mention =
                  a.status === "ready"
                    ? attachmentMention(a.uploaded, index + 1, Math.max(1, imageIndex))
                    : kind === "image"
                      ? `@图${Math.max(1, imageIndex)}`
                      : `@附件${index + 1}`
                const dotClass =
                  a.status === "uploading"
                    ? "bg-yellow-400 animate-pulse"
                    : a.status === "ready"
                    ? "bg-green-400"
                    : "bg-red-400"
                const tooltip =
                  a.status === "error" ? a.error : kind ? `${mention} [${kind}]` : ""
                const previewUrl =
                  a.status === "ready" && a.uploaded.kind === "image"
                    ? attachmentPreviewUrl(currentProject?.id, a.uploaded)
                    : ""
                return (
                  <div
                    key={a.id}
                    title={tooltip}
                    className="flex min-h-10 items-center gap-2 rounded-md border border-white/10 bg-white/[0.04] px-2 py-1.5 text-xs text-zinc-200"
                  >
                    {previewUrl ? (
                      <button
                        type="button"
                        onClick={() => insertAttachmentMention(mention)}
                        disabled={a.status !== "ready" || Boolean(pendingInputRequestId) || Boolean(pendingActionRequestId)}
                        className="h-9 w-9 overflow-hidden rounded-md border border-white/10 bg-black/20 disabled:opacity-60"
                        title="点击插入引用标签"
                      >
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img src={previewUrl} alt={filename} className="h-full w-full object-cover" />
                      </button>
                    ) : (
                      <span className={`w-1.5 h-1.5 rounded-full ${dotClass}`} />
                    )}
                    <button
                      type="button"
                      onClick={() => insertAttachmentMention(mention)}
                      disabled={a.status !== "ready" || Boolean(pendingInputRequestId) || Boolean(pendingActionRequestId)}
                      className="rounded border border-white/10 bg-white/[0.04] px-1.5 py-0.5 font-mono text-[10px] text-zinc-200 disabled:opacity-60"
                      title="点击插入引用标签"
                    >
                      {mention}
                    </button>
                    <span className="max-w-[42vw] truncate text-zinc-400 sm:max-w-[180px]">{filename}</span>
                    <button
                      onClick={() => removePendingAttachment(a.id)}
                      className="ml-0.5 text-zinc-500 hover:text-zinc-200"
                      aria-label="移除附件"
                    >
                      X
                    </button>
                  </div>
                )
              })
            })()}
          </div>
        )}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileInputChange}
        />
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <button
              onClick={handlePickFiles}
              disabled={!currentProject || Boolean(pendingActionRequestId)}
              title="上传文件"
              className="flex h-8 items-center justify-center rounded-md border border-white/10 bg-white/[0.04] px-2.5 text-xs font-medium text-zinc-300 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
            >
              上传
            </button>
            <AssetLibraryPanel
              projectId={currentProject?.id}
              disabled={false}
            />
          </div>
          {lastFailedMessage && !streaming && (
            <button
              onClick={() => {
                setLastFailed(null)
                setInput(lastFailedMessage)
              }}
              className="h-8 rounded-md border border-amber-300/20 bg-amber-500/10 px-2.5 text-xs font-medium text-amber-200 transition-colors hover:bg-amber-500/20"
              title="重新发送上次失败的消息"
            >
              重试
            </button>
          )}
        </div>
        <div className="flex items-end gap-2">
          <div className="flex-1 relative">
            {slashMenuOpen && slashMatches.length > 0 && (
              <SlashMenu
                query={input}
                extraCompletions={projectSlashCompletions}
                selectedIndex={Math.min(slashSelectedIdx, Math.max(0, slashMatches.length - 1))}
                onSelect={handleSlashSelect}
                onHover={setSlashSelectedIdx}
              />
            )}
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                currentProject
                  ? pendingInputRequestId
                    ? "请先在上方信息卡片里提交回答…"
                    : pendingActionRequestId
                    ? "请先在上方确认卡中选择…"
                    : streaming
                    ? "继续补充，Agent 会在当前任务结束后接着处理…"
                    : "和 OpenReel Agent 对话… (输入 / 查看命令)"
                  : "正在连接…"
              }
              disabled={!currentProject || Boolean(pendingInputRequestId) || Boolean(pendingActionRequestId)}
              rows={1}
              className="w-full rounded-lg border border-white/10 bg-[var(--studio-control)] px-3.5 py-2.5 text-sm leading-[22px] text-zinc-100 placeholder-zinc-500 shadow-inner shadow-black/20 resize-none focus:outline-none focus:ring-1 focus:ring-zinc-200/70 disabled:opacity-50 overflow-y-auto"
              style={{ minHeight: 44 }}
            />
          </div>
          <button
            onClick={handleSend}
            disabled={sendDisabled}
            className="h-[44px] min-w-[58px] rounded-lg bg-zinc-100 px-3 text-sm font-semibold text-zinc-950 shadow-sm transition-colors hover:bg-white disabled:cursor-not-allowed disabled:opacity-40 sm:min-w-[64px] sm:px-4"
          >
            {streaming ? "追加" : "发送"}
          </button>
        </div>
      </div>
    </div>
  )
}

// 单条消息气泡 — memo'd 让父组件 setInput 等无关 state 变化不再重渲所有历史消息。
// 关键点:streaming 和结构化操作回调从 ChatPanel 传入,只要其引用稳定
// (用了 useCallback / 我们只把 streaming=true 给最后一条),memo 就能跳过其余气泡。
interface MemoMessageProps {
  msg: ChatMessage
  streaming: boolean
  planActionsDisabled: boolean
  interactionInputDisabled: boolean
  pendingActionDisabled: boolean
  onProposedPlanExecute: () => void
  onInteractionInputSubmit: (message: string, decisionInputs?: Record<string, unknown> | null) => void
  onPendingActionResolve: (action: PendingActionPayload, decision: "confirm" | "cancel") => void
  onCancelQueuedMessage: (message: ChatMessage) => void
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function submittedFieldValue(field: Record<string, unknown>, values: Record<string, unknown>): string {
  const id = String(field.id ?? "")
  if (!id) return ""
  const raw = values[id] ?? field.default
  const value = String(raw ?? "").trim()
  if (!value || value === "__custom__") return ""
  const options = Array.isArray(field.options) ? field.options : []
  for (const option of options) {
    const item = recordValue(option)
    if (!item) continue
    if (String(item.value ?? "") === value) return String(item.label ?? value)
  }
  if ((id === "duration_seconds" || id === "segment_seconds") && /^\d+$/.test(value)) return `${value}秒`
  if (id === "episode_count" && /^\d+$/.test(value)) return `${value}集`
  return value
}

function messageAttachments(metadata: Record<string, unknown> | undefined): UploadedAttachment[] {
  const raw = metadata?.attachments
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => (item && typeof item === "object" ? item as UploadedAttachment : null))
    .filter((item): item is UploadedAttachment => Boolean(item?.filename && item?.rel_path))
}

function queuedUserStatus(metadata: Record<string, unknown> | undefined): { label: string; className: string } | null {
  const status = typeof metadata?.queueStatus === "string" ? metadata.queueStatus : ""
  const position = typeof metadata?.queuePosition === "number" ? metadata.queuePosition : null
  const error = typeof metadata?.queueError === "string" ? metadata.queueError : ""
  if (status === "sending") {
    return { label: "正在加入队列", className: "border-indigo-200/70 bg-indigo-100/70 text-indigo-700" }
  }
  if (status === "queued") {
    return {
      label: position && position > 0 ? `已排队 · 第 ${position} 条` : "已排队",
      className: "border-zinc-300/80 bg-white/55 text-zinc-600",
    }
  }
  if (status === "cancelling") {
    return { label: "正在删除", className: "border-zinc-300/80 bg-white/55 text-zinc-600" }
  }
  if (status === "processing") {
    return { label: "正在处理这条追加消息", className: "border-emerald-200/80 bg-emerald-100/70 text-emerald-700" }
  }
  if (status === "failed") {
    return { label: error ? `排队失败：${error}` : "排队失败", className: "border-red-200/80 bg-red-100/80 text-red-700" }
  }
  return null
}

function MessageAttachmentStrip({ attachments }: { attachments: UploadedAttachment[] }) {
  if (attachments.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {attachments.map((attachment, index) => {
        const label = attachment.mention || attachment.display_label || (attachment.kind === "image" ? `@图${index + 1}` : `@附件${index + 1}`)
        const previewUrl = attachment.kind === "image" ? attachmentPreviewUrl(undefined, attachment) : ""
        return (
          <div
            key={`${attachment.rel_path}-${index}`}
            className="flex items-center gap-2 rounded-md border border-black/10 bg-white/55 px-2 py-1.5 text-[11px] text-zinc-700"
          >
            {previewUrl ? (
              <span className="h-9 w-9 overflow-hidden rounded-md border border-black/10 bg-white">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={previewUrl} alt={attachment.filename} className="h-full w-full object-cover" />
              </span>
            ) : null}
            <span className="font-mono text-[10px] text-zinc-900">{label}</span>
            <span className="max-w-[160px] truncate">{attachment.filename}</span>
          </div>
        )
      })}
    </div>
  )
}

function pendingActionFromConfirmRequired(event: ChatStreamEvent): PendingActionPayload {
  const record = event as Record<string, unknown>
  const action = String(record.action ?? "confirmation")
  const scope = typeof record.scope === "string" ? record.scope : undefined
  const reason = typeof record.reason === "string" ? record.reason : undefined
  if (action === "reset_project") {
    return {
      id: `confirm-${action}-${scope ?? "full"}`,
      kind: "confirmation",
      target: action,
      action,
      title: "确认全量重置项目",
      description: "该操作会清空蓝图、任务、面板和画布内容，并归档模型可见上下文。",
      reason,
      risk: "destructive",
      confirmLabel: "确认重置",
      cancelLabel: "取消重置",
      confirmMessage: "/reset confirm",
      cancelMessage: "/reset cancel",
      confirmDisplay: "确认全量重置",
      cancelDisplay: "取消全量重置",
      values: { scope: scope ?? "full", reason },
    }
  }
  if (action === "delete_project") {
    const targetTitle = typeof record.target_title === "string" ? record.target_title : "当前项目"
    const targetProjectId = typeof record.target_project_id === "string" ? record.target_project_id : undefined
    return {
      id: `confirm-${action}-${targetProjectId ?? "project"}`,
      kind: "confirmation",
      target: action,
      action,
      title: "确认删除项目",
      description: `该操作会删除项目「${targetTitle}」。`,
      reason,
      risk: "destructive",
      confirmLabel: "删除项目",
      cancelLabel: "取消删除",
      confirmMessage: "/project delete confirm",
      cancelMessage: "/project delete cancel",
      confirmDisplay: "确认删除项目",
      cancelDisplay: "取消删除项目",
      values: { scope: scope ?? "project", reason, target_project_id: targetProjectId },
    }
  }
  if (action === "canvas.delete") {
    const nodeId = typeof record.node_id === "string" ? record.node_id : undefined
    const isClearAll = scope === "canvas" || scope === "all"
    return {
      id: `confirm-${action}-${isClearAll ? "canvas" : nodeId ?? "node"}`,
      kind: "confirmation",
      target: action,
      action,
      title: isClearAll ? "确认清空画布" : "确认删除节点",
      description: isClearAll
        ? "该操作会删除当前画布上的节点、连线和节点本地生成产物，但不会清空项目任务或标题。"
        : "该操作会删除指定节点、关联连线和该节点的本地生成产物。",
      reason,
      risk: "destructive",
      confirmLabel: isClearAll ? "清空画布" : "删除节点",
      cancelLabel: "取消",
      confirmMessage: isClearAll ? "确认清空画布" : "确认删除节点",
      cancelMessage: isClearAll ? "取消清空画布" : "取消删除节点",
      confirmDisplay: isClearAll ? "清空画布" : "确认删除节点",
      cancelDisplay: isClearAll ? "取消清空画布" : "取消删除节点",
      values: { scope: isClearAll ? "all" : "selected", reason, node_ids: nodeId ? [nodeId] : undefined },
    }
  }
  return {
    id: `confirm-${action}`,
    kind: "confirmation",
    target: action,
    action,
    title: "确认操作",
    description: "确认前不会执行该操作。",
    reason,
    risk: "high",
    confirmLabel: "确认",
    cancelLabel: "取消",
    confirmMessage: "确认执行",
    cancelMessage: "取消",
    values: { scope, reason },
  }
}

function riskFromBlueprintRevisionEvent(event: BlueprintStreamEvent): string {
  const record = event as Record<string, unknown>
  const risk = recordValue(record.risk)
  const value = risk?.risk ?? risk?.level ?? record.risk
  return typeof value === "string" && value.trim() ? value.trim() : "medium"
}

function pendingActionFromBlueprintRevision(event: BlueprintStreamEvent): PendingActionPayload {
  const record = event as Record<string, unknown>
  const pending = recordValue(record.pending_revision) ?? {}
  const affected = Array.isArray(record.affected_source_paths)
    ? record.affected_source_paths.map(String).filter(Boolean)
    : Array.isArray(pending.applied_source_paths)
      ? pending.applied_source_paths.map(String).filter(Boolean)
      : []
  const version = pending.version ?? record.version
  const targetNodeId = typeof pending.target_node_id === "string" ? pending.target_node_id : undefined
  const risk = riskFromBlueprintRevisionEvent(event)
  const affectedText = affected.length > 0
    ? `影响 ${affected.length} 个蓝图路径。`
    : "会更新 active blueprint，并同步相关下游节点状态。"

  return {
    id: `confirm-blueprint-revision-${String(pending.id ?? version ?? "current")}`,
    kind: "blueprint_revision",
    target: "blueprint_revision",
    action: "apply_blueprint_revision",
    title: version ? `确认应用蓝图修订 v${String(version)}` : "确认应用蓝图修订",
    description: "应用后会更新项目蓝图，并重物化或标记受影响的剧情、视觉和媒体节点。",
    reason: targetNodeId ? `${affectedText} 目标节点：${targetNodeId}` : affectedText,
    risk,
    confirmLabel: "应用修订",
    cancelLabel: "取消修订",
    confirmMessage: "确认应用蓝图修订",
    cancelMessage: "取消蓝图修订",
    confirmDisplay: "应用蓝图修订",
    cancelDisplay: "取消蓝图修订",
    values: {
      pending_revision_id: pending.id,
      version,
      target_node_id: targetNodeId,
      affected_source_paths: affected,
    },
  }
}

function SubmittedDecisionCard({ metadata }: { metadata?: Record<string, unknown> }) {
  const decision = recordValue(metadata?.decisionInputs)
  if (!decision || decision.kind !== "interaction_input") return null
  const fields = Array.isArray(decision.fields) ? decision.fields.map(recordValue).filter(Boolean) as Record<string, unknown>[] : []
  const values = recordValue(decision.values) ?? {}
  const rows = fields
    .map((field) => ({
      id: String(field.id ?? ""),
      label: String(field.label ?? field.id ?? ""),
      value: submittedFieldValue(field, values),
    }))
    .filter((row) => row.id && row.value)

  return (
    <div className="w-full min-w-0 rounded-md border border-indigo-200/80 bg-white/70 p-2.5 text-xs text-[#141722] shadow-sm sm:min-w-[260px]">
      <div className="flex items-center justify-between gap-2 border-b border-indigo-100 pb-1.5">
        <span className="font-semibold">已提交</span>
        {decision.purpose ? <span className="text-[10px] text-indigo-500">{String(decision.purpose)}</span> : null}
      </div>
      <div className="mt-1.5 font-medium">{String(decision.title ?? "结构化信息")}</div>
      {rows.length > 0 ? (
        <div className="mt-2 grid gap-1.5">
          {rows.map((row) => (
            <div key={row.id} className="grid grid-cols-[82px_minmax(0,1fr)] gap-2">
              <span className="text-zinc-500">{row.label}</span>
              <span className="break-words text-zinc-900">{row.value}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function MessageBubbleImpl({
  msg,
  streaming,
  planActionsDisabled,
  interactionInputDisabled,
  pendingActionDisabled,
  onProposedPlanExecute,
  onInteractionInputSubmit,
  onPendingActionResolve,
  onCancelQueuedMessage,
}: MemoMessageProps) {
  if (msg.role === "system") {
    return (
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
        className="flex justify-center"
      >
        <div className="max-w-[80%] rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-center text-[12px] text-zinc-400">
          {msg.content}
        </div>
      </motion.div>
    )
  }

  const hasLiveProgress = Boolean(
    (msg.rounds && msg.rounds.length > 0) ||
    (msg.tools && msg.tools.length > 0) ||
    (msg.stepProgress && msg.stepProgress.steps.length > 0) ||
    (msg.nodes && msg.nodes.length > 0) ||
    msg.proposedPlan
  )
  const submittedDecision = msg.role === "user" ? recordValue(msg.metadata?.decisionInputs) : null
  const attachments = msg.role === "user" ? messageAttachments(msg.metadata) : []
  const queueStatus = msg.role === "user" ? queuedUserStatus(msg.metadata) : null
  const rawQueueStatus = msg.role === "user" && typeof msg.metadata?.queueStatus === "string" ? msg.metadata.queueStatus : ""
  const canCancelQueued = rawQueueStatus === "queued" || rawQueueStatus === "failed"

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className={`flex gap-2 sm:gap-3 ${msg.role === "user" ? "justify-end" : "justify-start"}`}
    >
      {msg.role === "assistant" && (
        <img
          src={APP_ICON_SRC}
          alt="OpenReel Studio"
          className="mt-0.5 h-8 w-8 flex-shrink-0 object-contain drop-shadow-[0_8px_18px_rgba(6,182,212,0.18)]"
          draggable={false}
        />
      )}
      <div
        className={`max-w-[92%] min-w-0 px-3 py-2.5 text-sm leading-relaxed shadow-lg sm:max-w-[88%] sm:px-4 sm:py-3 ${
          msg.role === "user"
            ? "rounded-lg rounded-br-sm bg-[#eef2ff] text-[#141722] shadow-black/10"
            : "rounded-lg rounded-bl-sm border border-white/10 bg-[var(--studio-control)] text-zinc-100 shadow-black/25"
        }`}
      >
        {submittedDecision?.kind === "interaction_input" ? (
          <SubmittedDecisionCard metadata={msg.metadata} />
        ) : null}
        {msg.role === "assistant" && (
          <AgentActivityTimeline rounds={msg.rounds} tools={msg.tools} />
        )}
        {msg.stepProgress && msg.stepProgress.steps.length > 0 && (
          <StepProgressCard progress={msg.stepProgress} />
        )}
        {msg.nodes && msg.nodes.length > 0 && (
          <div className="mb-2 space-y-1.5">
            {msg.nodes.map((n) => (
              <NodeBubbleCard key={n.nodeId} node={n} />
            ))}
          </div>
        )}
        {msg.interactionInput && (
          <InteractionInputCard
            inputRequest={msg.interactionInput}
            disabled={interactionInputDisabled}
            onSubmit={onInteractionInputSubmit}
          />
        )}
        {msg.pendingAction && (
          <PendingActionCard
            action={msg.pendingAction}
            disabled={pendingActionDisabled}
            onResolve={onPendingActionResolve}
          />
        )}
        {msg.proposedPlan ? (
          <ProposedPlanCard
            plan={msg.proposedPlan}
            disabled={planActionsDisabled}
            onExecute={onProposedPlanExecute}
          />
        ) : null}
        {msg.content && submittedDecision?.kind !== "interaction_input" && !(msg.role === "assistant" && msg.proposedPlan) ? (
          msg.role === "assistant" ? (
            <SmoothMarkdownView content={msg.content} active={streaming} />
          ) : (
            <div className="whitespace-pre-wrap">{msg.content}</div>
          )
        ) : msg.role === "assistant" && streaming && !hasLiveProgress ? (
          <WorkingIndicator label="正在理解你的请求" />
        ) : null}
        {queueStatus ? (
          <div className={`mt-2 inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${queueStatus.className}`}>
            <span>{queueStatus.label}</span>
            {canCancelQueued ? (
              <button
                type="button"
                onClick={() => onCancelQueuedMessage(msg)}
                className="rounded-full px-1.5 py-0.5 text-[11px] font-semibold text-current underline-offset-2 hover:underline"
              >
                删除
              </button>
            ) : null}
          </div>
        ) : null}
        {attachments.length > 0 ? <MessageAttachmentStrip attachments={attachments} /> : null}
        {msg.role === "assistant" && streaming && (msg.content || hasLiveProgress) && (
          <WorkingIndicator label="仍在工作中" />
        )}
      </div>
    </motion.div>
  )
}

const MemoMessage = memo(MessageBubbleImpl)
