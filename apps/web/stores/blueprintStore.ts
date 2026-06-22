import { create } from "zustand"
import { type BlueprintStreamEvent } from "@/lib/api"

export type BlueprintViewBlock = Record<string, unknown>

export interface BlueprintViewSection {
  section_id?: string
  title?: string
  display_type?: string
  blocks?: BlueprintViewBlock[]
  items?: Record<string, unknown>[]
}

export interface BlueprintViewModel {
  header?: {
    title?: string
    status_label?: string
    badges?: string[]
  }
  sections?: BlueprintViewSection[]
  actions?: Record<string, unknown>[]
}

export interface BlueprintIndex {
  id?: string
  version?: number
  status?: string
  theme_title?: string
  short_summary?: string
  duration_seconds?: number
  episode_count?: number
  segment_seconds?: number
}

export interface BlueprintSectionProgress {
  section_id: string
  section_index?: number | null
  window_index?: number | null
  window_count?: number | null
  status?: string | null
  title?: string
  summary_text?: string | null
  failure_reason?: string | null
  display_blocks?: BlueprintViewBlock[] | null
  validator_result?: Record<string, unknown> | null
  updated_at: number
}

// ── tree node types ──────────────────────────────────────────────────────────

export interface BlueprintTreeNode {
  id: string
  type: string
  title: string
  status: string
  children: BlueprintTreeNode[]
  // text
  content?: string
  // image / video
  description?: string
  resolution?: string
  quality?: string
  prompt?: string | null
  negative_prompt?: string | null
  references?: string[]
  url?: string | null
  duration?: number  // video only
  updated_at?: string | number | null
}

export interface BlueprintTreeEvent {
  type: "blueprint_tree_changed"
  project_id?: string
  tree_version?: number
  draft_mode?: string
  replacement?: boolean
  action: "add_child" | "update_node" | "delete_node" | "replace_tree"
  parent_id?: string
  node_id?: string
  node?: BlueprintTreeNode
  patch?: Record<string, unknown>
}

// ── store interface ──────────────────────────────────────────────────────────

interface BlueprintState {
  projectId: string | null
  status: string
  blueprint: BlueprintIndex | null
  viewModel: BlueprintViewModel | null
  outlineMarkdown: string | null
  sectionProgress: Record<string, BlueprintSectionProgress>
  // new tree-based blueprint
  tree: BlueprintTreeNode | null
  treeVersion: number | null
  validation: Record<string, unknown> | null
  error: string | null
  loading: boolean
  updatedAt: number | null
  resetForProject: (projectId: string | null) => void
  load: (projectId: string) => Promise<void>
  applyStreamEvent: (event: BlueprintStreamEvent, fallbackProjectId?: string | null) => void
  applyTreeEvent: (event: BlueprintTreeEvent, fallbackProjectId?: string | null) => void
}

// ── tree helpers ─────────────────────────────────────────────────────────────

function applyTreeAddChild(
  tree: BlueprintTreeNode,
  parentId: string,
  node: BlueprintTreeNode,
): BlueprintTreeNode {
  if (tree.id === parentId) {
    return { ...tree, children: [...(tree.children ?? []), node] }
  }
  return {
    ...tree,
    children: (tree.children ?? []).map((c) => applyTreeAddChild(c, parentId, node)),
  }
}

function applyTreeUpdateNode(
  tree: BlueprintTreeNode,
  nodeId: string,
  patch: Record<string, unknown>,
): BlueprintTreeNode {
  if (tree.id === nodeId) {
    return { ...tree, ...patch, updated_at: new Date().toISOString() }
  }
  return {
    ...tree,
    children: (tree.children ?? []).map((c) => applyTreeUpdateNode(c, nodeId, patch)),
  }
}

function applyTreeDeleteNode(
  tree: BlueprintTreeNode,
  nodeId: string,
): BlueprintTreeNode {
  if (tree.id === nodeId) {
    return {
      ...tree,
      children: [],
    }
  }
  return {
    ...tree,
    children: (tree.children ?? []).filter((c) => c.id !== nodeId).map((c) =>
      applyTreeDeleteNode(c, nodeId),
    ),
  }
}

function mergeBlueprint(current: BlueprintIndex | null, patch: unknown): BlueprintIndex | null {
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) return current
  return { ...(current ?? {}), ...(patch as Record<string, unknown>) } as BlueprintIndex
}

function eventStatus(event: BlueprintStreamEvent): string {
  if (event.type === "blueprint_cleared") return "missing"
  if (event.type === "blueprint_approved" || event.type === "blueprint_revision_applied") return "active"
  if (event.type === "blueprint_proposed") return "pending_review"
  if (event.type === "blueprint_revision_proposed") return "revision_pending"
  if (event.type === "blueprint_draft_started" || event.type === "blueprint_draft_saved") return "draft"
  return event.status || "draft"
}

export const useBlueprintStore = create<BlueprintState>((set, get) => ({
  projectId: null,
  status: "missing",
  blueprint: null,
  viewModel: null,
  outlineMarkdown: null,
  sectionProgress: {},
  tree: null,
  treeVersion: null,
  validation: null,
  error: null,
  loading: false,
  updatedAt: null,

  resetForProject: (projectId) =>
    set({
      projectId,
      status: "missing",
      blueprint: null,
      viewModel: null,
      outlineMarkdown: null,
      sectionProgress: {},
      tree: null,
      treeVersion: null,
      validation: null,
      error: null,
      loading: false,
      updatedAt: null,
    }),

  load: async (projectId) => {
    if (!projectId) return
    const currentProjectId = get().projectId
    if (currentProjectId !== projectId) get().resetForProject(projectId)
    set({
      projectId,
      status: "missing",
      blueprint: null,
      viewModel: null,
      outlineMarkdown: null,
      sectionProgress: {},
      tree: null,
      treeVersion: null,
      validation: null,
      error: null,
      loading: false,
      updatedAt: Date.now(),
    })
  },

  applyStreamEvent: (event, fallbackProjectId) =>
    set((state) => {
      const projectId = event.project_id || fallbackProjectId || state.projectId
      if (state.projectId && projectId && state.projectId !== projectId) return state
      if (event.type === "blueprint_cleared") {
        return {
          projectId: projectId ?? state.projectId,
          status: "missing",
          blueprint: null,
          tree: null,
          treeVersion: null,
          viewModel: null,
          outlineMarkdown: null,
          sectionProgress: {},
          validation: null,
          error: null,
          updatedAt: Date.now(),
        }
      }

      const nextProgress = { ...state.sectionProgress }
      if (event.section_id) {
        nextProgress[event.section_id] = {
          ...(nextProgress[event.section_id] ?? { section_id: event.section_id }),
          section_id: event.section_id,
          title: event.title ?? nextProgress[event.section_id]?.title,
          section_index: event.section_index,
          window_index: event.window_index,
          window_count: event.window_count,
          status: event.status ?? event.type.replace("blueprint_", ""),
          summary_text: event.summary_text ?? nextProgress[event.section_id]?.summary_text,
          failure_reason: event.failure_reason ?? nextProgress[event.section_id]?.failure_reason,
          display_blocks: event.display_blocks ?? nextProgress[event.section_id]?.display_blocks,
          updated_at: Date.now(),
        }
      }

      return {
        projectId: projectId ?? state.projectId,
        status: eventStatus(event),
        blueprint: mergeBlueprint(state.blueprint, event.blueprint_ref),
        viewModel: event.view_model_patch ? (event.view_model_patch as BlueprintViewModel) : state.viewModel,
        sectionProgress: nextProgress,
        validation: event.validation ?? state.validation,
        error: event.type === "blueprint_section_needs_revision" ? event.failure_reason || null : state.error,
        updatedAt: Date.now(),
      }
    }),

  // ── tree events ──────────────────────────────────────────────────────

  applyTreeEvent: (event, fallbackProjectId) =>
    set((state) => {
      const projectId = event.project_id || fallbackProjectId || state.projectId
      if (state.projectId && projectId && state.projectId !== projectId) {
        return state
      }
      const hasVersion = typeof event.tree_version === "number"
      const currentVersion = state.treeVersion
      const nextVersion = hasVersion && typeof event.tree_version === "number" ? event.tree_version : null
      const eventVersion = hasVersion ? nextVersion : null

      if (hasVersion) {
        if (currentVersion === null) {
          if (projectId) {
            void get().load(projectId)
          }
          return state
        }
        if (eventVersion !== null && eventVersion <= currentVersion) {
          return state
        }
        if (eventVersion !== null && eventVersion > currentVersion + 1) {
          if (projectId) {
            void get().load(projectId)
          }
          return state
        }
      }

      const root = state.tree
      if (event.replacement || event.draft_mode === "replacement") {
        return {
          projectId,
          status: "pending_review",
          treeVersion: nextVersion ?? state.treeVersion,
          updatedAt: Date.now(),
        }
      }
      if (event.action === "replace_tree") {
        if (projectId) {
          void get().load(projectId)
        }
        return {
          projectId,
          status: "pending_review",
          treeVersion: nextVersion ?? state.treeVersion,
          updatedAt: Date.now(),
        }
      }

      if (!root) {
        if (!hasVersion) {
          // Initialize tree from first event (add_child to root)
          if (
            event.action === "add_child"
            && (event.parent_id ?? "root") === "root"
            && event.node
          ) {
            const newRoot: BlueprintTreeNode = {
              id: "root",
              type: "text",
              title: "根节点",
              status: "drafting",
              children: [event.node],
            }
            return { projectId, tree: newRoot, treeVersion: nextVersion, updatedAt: Date.now() }
          }
        }
        if (projectId && hasVersion) {
          void get().load(projectId)
        }
        return state
      }

      const parentId = event.parent_id ?? "root"

      if (event.action === "add_child" && event.node) {
        return {
          projectId,
          tree: applyTreeAddChild(root, parentId, event.node),
          treeVersion: nextVersion,
          updatedAt: Date.now(),
        }
      }

      if (event.action === "update_node" && event.node_id && event.patch) {
        return {
          projectId,
          tree: applyTreeUpdateNode(root, event.node_id, event.patch),
          treeVersion: nextVersion,
          updatedAt: Date.now(),
        }
      }

      if (event.action === "delete_node" && event.node_id) {
        const nextTree = applyTreeDeleteNode(root, event.node_id)
        if (nextTree.id === "root" && event.node_id === "root" && nextTree.children.length === 0) {
          return {
            projectId,
            tree: null,
            treeVersion: nextVersion,
            updatedAt: Date.now(),
          }
        }
        return {
          projectId,
          tree: nextTree,
          treeVersion: nextVersion,
          updatedAt: Date.now(),
        }
      }

      return state
    }),
}))
