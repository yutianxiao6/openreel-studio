import { create } from "zustand"
import type { ChatStreamEvent, UploadedAttachment } from "@/lib/api"
import { normalizeInteractionInputPayload } from "@/lib/interactionInput"

export type MessageRole = "user" | "assistant" | "system" | "tool"

export type PendingAttachment =
  | { id: string; status: "uploading"; filename: string; size: number }
  | { id: string; status: "ready"; uploaded: UploadedAttachment }
  | { id: string; status: "error"; filename: string; error: string }

export interface NodeBubble {
  nodeId: string
  type: string
  title: string
  status: "running" | "completed" | "failed"
}

export interface ToolBubble {
  tool: string
  status: "running" | "completed" | "failed"
  startedAt: number
}

export interface AgentRound {
  round: number
  content: string
  source: "model" | "action_summary"
  tools: string[]
  results: AgentRoundToolResult[]
  status: "running" | "completed"
  startedAt: number
}

export interface AgentRoundToolResult {
  tool: string
  status: "running" | "completed" | "failed"
  summary: string
}

export interface StepProgress {
  total: number
  steps: { title: string; tool: string; status: "pending" | "running" | "completed" | "failed" }[]
}

export interface PlanStep {
  step: number
  tool: string
  title: string
  input: Record<string, unknown>
  enabled: boolean
  status?: "pending" | "completed" | "failed"
  actual_node_id?: string | null
}

export interface PlanSection {
  type:
    | "markdown"
    | "characters_preview"
    | "scenes_preview"
    | "outline_preview"
    | "shots_preview"
    | "tool_steps"
    | "risks"
    | "alternatives"
    | "references"
    | "tree_preview"
  content?: string
  items?: unknown[]
  episodes?: unknown[]
  steps?: PlanStep[]
}

export interface PlanReviewIssue {
  code: string
  severity: "info" | "warning" | "error"
  message: string
  step_id?: string
  phase?: number
  tool?: string
  node_type?: string
}

export interface PlanReviewCheck {
  code: string
  status: "passed" | "warning" | "failed" | "skipped"
  message: string
}

export interface PlanReview {
  role: string
  readonly: boolean
  source?: string
  status: "passed" | "warning" | "failed"
  summary: string
  issue_count?: number
  warning_count?: number
  error_count?: number
  checks?: PlanReviewCheck[]
  issues?: PlanReviewIssue[]
}

export interface PlanPhaseSummary {
  phase: number
  title: string
  goal?: string
  depends_on: number[]
  step_count: number
}

export interface PlanPhase extends PlanPhaseSummary {
  steps: PlanStep[]
}

export interface PlanDoc {
  id: string
  kind?: string
  title: string
  summary: string
  sections: PlanSection[]
  phases?: PlanPhaseSummary[]
  review?: PlanReview
  iteration: number
  trigger_reason?: string
  created_at?: string
  source_request?: string
  blueprint?: Record<string, unknown> | null
  approval_role?: string
  ui_surface?: string
  execution_state_source?: string | null
  tree_version?: number
  tree_summary?: Record<string, unknown> | null
  tree_nodes?: Record<string, unknown>[]
}

export interface InteractionInputOption {
  label: string
  description?: string
}

export interface InteractionInputQuestion {
  id: string
  header: string
  question: string
  options: InteractionInputOption[]
}

export interface InteractionInputPayload {
  purpose?: string
  stage: "basic" | "structure" | string
  title: string
  description?: string
  submit_label?: string
  questions: InteractionInputQuestion[]
}

export interface TokenUsageSnapshot {
  projectId?: string
  runId: string
  round?: number | null
  phase?: string
  usage: Record<string, unknown>
  runTotals: Record<string, unknown>
  sessionTotals: Record<string, unknown>
  latestCallTokens?: Record<string, unknown> | null
  latestCallContext?: Record<string, unknown> | null
  runCumulativeTokens?: Record<string, unknown> | null
  sessionCumulativeTokens?: Record<string, unknown> | null
  runContextPeak?: Record<string, unknown> | null
  sessionContextPeak?: Record<string, unknown> | null
  updatedAt: number
}

export interface PendingActionPayload {
  id?: string
  kind: string
  target: string
  action: string
  title: string
  description?: string
  reason?: string
  risk?: "low" | "medium" | "high" | "destructive" | string
  confirmLabel?: string
  cancelLabel?: string
  confirmMessage?: string
  cancelMessage?: string
  confirmDisplay?: string
  cancelDisplay?: string
  values?: Record<string, unknown>
  status?: "pending" | "confirmed" | "cancelled"
}

export interface ChecklistItem {
  step_id: string
  step: number
  title: string
  tool: string
  expected_node_type?: string
  status: "pending" | "in_progress" | "completed" | "failed"
  actual_node_id?: string | null
}

export interface UnfinishedNode {
  node_id: string
  type: string
  title: string
  status: string
  reason: string
  suggested_action: string
}

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  createdAt: string
  nodes?: NodeBubble[]
  tools?: ToolBubble[]
  rounds?: AgentRound[]
  stepProgress?: StepProgress
  proposedPlan?: PlanDoc
  interactionInput?: InteractionInputPayload
  pendingAction?: PendingActionPayload
  changeCard?: { tool: string; changes: Array<{ field: string; label: string; before: string; after: string }> }
  metadata?: Record<string, unknown>
}

interface ChatStore {
  messages: ChatMessage[]
  streaming: boolean
  pendingAttachments: PendingAttachment[]
  lastFailedMessage: string | null
  activeChecklist: ChecklistItem[]
  unfinishedNodes: UnfinishedNode[]
  tokenUsage: TokenUsageSnapshot | null
  appendMessage: (msg: ChatMessage) => void
  appendToLastAssistant: (delta: string) => void
  setLastAssistantNode: (node: NodeBubble) => void
  updateLastAssistantNode: (nodeId: string, patch: Partial<NodeBubble>) => void
  addToolBubble: (tool: string) => void
  updateToolBubble: (tool: string, patch: Partial<ToolBubble>) => void
  addAgentRound: (round: Omit<AgentRound, "status" | "startedAt" | "results">) => void
  addAgentRoundToolStart: (tool: string, summary: string) => void
  addAgentRoundToolResult: (result: AgentRoundToolResult) => void
  completeAgentRound: (round: number) => void
  initStepProgress: (steps: { title: string; tool: string }[]) => void
  advanceStep: (index: number, status: "running" | "completed" | "failed") => void
  setLastAssistantProposedPlan: (plan: PlanDoc) => void
  setLastAssistantInteractionInput: (inputRequest: InteractionInputPayload) => void
  setLastAssistantPendingAction: (action: PendingActionPayload) => void
  markLastAssistantPendingActionStatus: (status: "confirmed" | "cancelled") => void
  setStreaming: (v: boolean) => void
  addPendingAttachment: (att: PendingAttachment) => void
  updatePendingAttachment: (id: string, patch: Partial<PendingAttachment>) => void
  removePendingAttachment: (id: string) => void
  clearPendingAttachments: () => void
  clearMessages: () => void
  resetProjectRuntime: (options?: { clearMessages?: boolean }) => void
  loadHistory: (raw: { id: string; role: string; content: string; created_at: string }[]) => void
  setLastFailed: (msg: string | null) => void
  setActiveChecklist: (items: ChecklistItem[]) => void
  updateChecklistItem: (stepId: string, patch: Partial<ChecklistItem>) => void
  setUnfinishedNodes: (nodes: UnfinishedNode[]) => void
  setTokenUsage: (snapshot: TokenUsageSnapshot | null) => void
  applyTokenUsageEvent: (event: Extract<ChatStreamEvent, { type: "token_usage" }>) => void
}

function findLastAssistantIndex(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "assistant") return i
  }
  return -1
}

export function summarizeAgentRoundToolResult(tool: string, result: unknown): AgentRoundToolResult {
  if (result && typeof result === "object" && !Array.isArray(result)) {
    const data = result as Record<string, unknown>
    const awaitingConfirmation = Boolean(data.requires_user_confirm) && !data.error
    if (!awaitingConfirmation && (data.error || data.ok === false)) {
      return { tool, status: "failed", summary: String(data.error ?? "执行失败").slice(0, 240) }
    }
    const parts: string[] = []
    if (data.message) parts.push(String(data.message).slice(0, 180))
    if (data.title) parts.push(`标题: ${String(data.title)}`)
    if (data.type) parts.push(`类型: ${String(data.type)}`)
    if (data.status) parts.push(`状态: ${String(data.status)}`)
    const nodeId = data.node_id ?? data.id
    if (nodeId) parts.push(`节点: ${String(nodeId).slice(0, 8)}`)
    if (Array.isArray(data.nodes)) parts.push(`节点数: ${data.nodes.length}`)
    return { tool, status: "completed", summary: parts.slice(0, 4).join("；") || "执行完成" }
  }
  if (Array.isArray(result)) {
    return {
      tool,
      status: "completed",
      summary: `返回 ${result.length} 条记录`,
    }
  }
  return { tool, status: "completed", summary: result == null ? "执行完成" : String(result).slice(0, 240) }
}

export const useChatStore = create<ChatStore>((set) => ({
  messages: [],
  streaming: false,
  pendingAttachments: [],
  lastFailedMessage: null,
  activeChecklist: [],
  unfinishedNodes: [],
  tokenUsage: null,

  appendMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),

  appendToLastAssistant: (delta) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      next[idx] = { ...next[idx], content: next[idx].content + delta }
      return { messages: next }
    }),

  setLastAssistantNode: (node) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const current = next[idx].nodes ?? []
      const existing = current.findIndex((n) => n.nodeId === node.nodeId)
      const newNodes =
        existing === -1
          ? [...current, node]
          : current.map((n) => (n.nodeId === node.nodeId ? { ...n, ...node } : n))
      next[idx] = { ...next[idx], nodes: newNodes }
      return { messages: next }
    }),

  updateLastAssistantNode: (nodeId, patch) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const current = next[idx].nodes ?? []
      next[idx] = {
        ...next[idx],
        nodes: current.map((n) => (n.nodeId === nodeId ? { ...n, ...patch } : n)),
      }
      return { messages: next }
    }),

  addToolBubble: (tool) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const current = next[idx].tools ?? []
      next[idx] = { ...next[idx], tools: [...current, { tool, status: "running", startedAt: Date.now() }] }
      return { messages: next }
    }),

  updateToolBubble: (tool, patch) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const current = next[idx].tools ?? []
      const toolIdx = [...current].reverse().findIndex((t) => t.tool === tool)
      if (toolIdx === -1) return s
      const realIdx = current.length - 1 - toolIdx
      const updated = [...current]
      updated[realIdx] = { ...updated[realIdx], ...patch }
      next[idx] = { ...next[idx], tools: updated }
      return { messages: next }
    }),

  addAgentRound: (round) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const current = next[idx].rounds ?? []
      next[idx] = {
        ...next[idx],
        rounds: [...current, { ...round, results: [], status: "running" as const, startedAt: Date.now() }],
      }
      return { messages: next }
    }),

  addAgentRoundToolStart: (tool, summary) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const rounds = [...(next[idx].rounds ?? [])]
      const roundIdx = rounds.findLastIndex((round) => round.status === "running")
      if (roundIdx === -1) return s
      rounds[roundIdx] = {
        ...rounds[roundIdx],
        results: [...rounds[roundIdx].results, { tool, status: "running", summary }],
      }
      next[idx] = { ...next[idx], rounds }
      return { messages: next }
    }),

  addAgentRoundToolResult: (result) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const rounds = [...(next[idx].rounds ?? [])]
      const roundIdx = rounds.findLastIndex((round) => round.status === "running")
      if (roundIdx === -1) return s
      const resultIdx = rounds[roundIdx].results.findLastIndex(
        (item) => item.tool === result.tool && item.status === "running"
      )
      rounds[roundIdx] = {
        ...rounds[roundIdx],
        results: resultIdx === -1
          ? [...rounds[roundIdx].results, result]
          : rounds[roundIdx].results.map((item, index) => index === resultIdx ? result : item),
      }
      next[idx] = { ...next[idx], rounds }
      return { messages: next }
    }),

  completeAgentRound: (round) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const current = next[idx].rounds ?? []
      next[idx] = {
        ...next[idx],
        rounds: current.map((item) =>
          item.round === round ? { ...item, status: "completed" as const } : item
        ),
      }
      return { messages: next }
    }),

  initStepProgress: (steps) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      next[idx] = {
        ...next[idx],
        stepProgress: {
          total: steps.length,
          steps: steps.map((st) => ({ ...st, status: "pending" as const })),
        },
      }
      return { messages: next }
    }),

  advanceStep: (index, status) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      const progress = next[idx].stepProgress
      if (!progress) return s
      const steps = [...progress.steps]
      if (index >= 0 && index < steps.length) {
        steps[index] = { ...steps[index], status }
      }
      next[idx] = { ...next[idx], stepProgress: { ...progress, steps } }
      return { messages: next }
    }),

  setLastAssistantProposedPlan: (plan) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      next[idx] = { ...next[idx], proposedPlan: plan }
      return { messages: next }
    }),

  setLastAssistantInteractionInput: (inputRequest) =>
    set((s) => {
      const interactionInput = normalizeInteractionInputPayload(inputRequest)
      if (!interactionInput) return s
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      next[idx] = { ...next[idx], interactionInput }
      return { messages: next }
    }),

  setLastAssistantPendingAction: (action) =>
    set((s) => {
      const idx = findLastAssistantIndex(s.messages)
      if (idx === -1) return s
      const next = [...s.messages]
      next[idx] = { ...next[idx], pendingAction: { ...action, status: action.status ?? "pending" } }
      return { messages: next }
    }),

  markLastAssistantPendingActionStatus: (status) =>
    set((s) => {
      const idx = s.messages
        .map((m, i) => ({ m, i }))
        .reverse()
        .find(({ m }) => m.pendingAction && (m.pendingAction.status ?? "pending") === "pending")?.i
      if (idx === undefined) return s
      const next = [...s.messages]
      const pendingAction = next[idx].pendingAction
      if (!pendingAction) return s
      next[idx] = { ...next[idx], pendingAction: { ...pendingAction, status } }
      return { messages: next }
    }),

  setStreaming: (v) => set({ streaming: v }),

  addPendingAttachment: (att) =>
    set((s) => ({ pendingAttachments: [...s.pendingAttachments, att] })),

  updatePendingAttachment: (id, patch) =>
    set((s) => ({
      pendingAttachments: s.pendingAttachments.map((a) =>
        a.id === id ? ({ ...a, ...patch } as PendingAttachment) : a,
      ),
    })),

  removePendingAttachment: (id) =>
    set((s) => ({
      pendingAttachments: s.pendingAttachments.filter((a) => a.id !== id),
    })),

  clearPendingAttachments: () => set({ pendingAttachments: [] }),

  clearMessages: () =>
    set({ messages: [], streaming: false, pendingAttachments: [], tokenUsage: null }),

  resetProjectRuntime: (options) =>
    set((state) => ({
      messages: options?.clearMessages === false ? state.messages : [],
      streaming: false,
      pendingAttachments: [],
      lastFailedMessage: null,
      activeChecklist: [],
      unfinishedNodes: [],
      tokenUsage: null,
    })),

  loadHistory: (raw: { id: string; role: string; content: string; created_at: string; metadata_json?: string | null }[]) => {
    const msgs: ChatMessage[] = raw
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => {
        let proposedPlan: PlanDoc | undefined
        let interactionInput: InteractionInputPayload | undefined
        let metadata: Record<string, unknown> | undefined
        let nodes: NodeBubble[] | undefined
        let rounds: AgentRound[] | undefined
        if (m.metadata_json) {
          try {
            const meta = JSON.parse(m.metadata_json)
            metadata = meta as Record<string, unknown>
            if (meta.proposedPlan) proposedPlan = meta.proposedPlan as PlanDoc
            if (meta.interactionInput) interactionInput = normalizeInteractionInputPayload(meta.interactionInput) ?? undefined
            if (meta.nodes) nodes = meta.nodes as NodeBubble[]
            if (Array.isArray(meta.rounds)) {
              rounds = meta.rounds.map((round: Partial<AgentRound>) => ({
                round: Number(round.round ?? 0),
                content: String(round.content ?? ""),
                source: round.source === "model" ? "model" : "action_summary",
                tools: Array.isArray(round.tools) ? round.tools.map(String) : [],
                results: Array.isArray(round.results) ? round.results : [],
                status: "completed",
                startedAt: Number(round.startedAt ?? 0),
              }))
            }
          } catch { /* ignore broken json */ }
        }
        return {
          id: m.id,
          role: m.role as MessageRole,
          content: m.content,
          createdAt: m.created_at,
          proposedPlan,
          interactionInput,
          metadata,
          nodes,
          rounds,
        }
      })
    set({ messages: msgs })
  },

  setLastFailed: (msg) => set({ lastFailedMessage: msg }),

  setActiveChecklist: (items) => set({ activeChecklist: items }),

  updateChecklistItem: (stepId, patch) =>
    set((s) => ({
      activeChecklist: s.activeChecklist.map((item) =>
        item.step_id === stepId ? { ...item, ...patch } : item,
      ),
    })),

  setUnfinishedNodes: (nodes) => set({ unfinishedNodes: nodes }),

  setTokenUsage: (snapshot) => set({ tokenUsage: snapshot }),

  applyTokenUsageEvent: (event) =>
    set({
      tokenUsage: {
        projectId: event.project_id,
        runId: event.run_id,
        round: event.round ?? null,
        phase: event.phase,
        usage: event.usage ?? {},
        runTotals: event.run_totals ?? {},
        sessionTotals: event.session_totals ?? {},
        latestCallTokens: event.latest_call_tokens ?? null,
        latestCallContext: event.latest_call_context ?? null,
        runCumulativeTokens: event.run_cumulative_tokens ?? null,
        sessionCumulativeTokens: event.session_cumulative_tokens ?? null,
        runContextPeak: event.run_context_peak ?? null,
        sessionContextPeak: event.session_context_peak ?? null,
        updatedAt: Date.now(),
      },
    }),
}))
