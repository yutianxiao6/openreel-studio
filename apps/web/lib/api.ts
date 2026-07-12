let _cachedApiBase: string | null = null
export const CANVAS_REFRESH_EVENT = "openreel:canvas-refresh"
export const WORKFLOW_REFRESH_EVENT = "openreel:workflow-refresh"

export interface CanvasRefreshOptions {
  projectId?: string
  preserveOnEmpty?: boolean
  preserveLayout?: boolean
  fitView?: boolean
}

export interface WorkflowRefreshOptions {
  projectId?: string
}

declare global {
  interface Window {
    openReelDesktop?: {
      apiBase?: string
      webBase?: string
      platform?: string
    }
  }
}

function getDesktopApiBase(): string {
  if (typeof window === "undefined") return ""
  const base = window.openReelDesktop?.apiBase?.trim()
  return base ?? ""
}

async function discoverApiBase(): Promise<string> {
  const desktopBase = getDesktopApiBase()
  if (desktopBase) {
    return desktopBase
  }
  if (process.env.NEXT_PUBLIC_API_BASE_URL) {
    return process.env.NEXT_PUBLIC_API_BASE_URL
  }
  if (process.env.NODE_ENV === 'production') {
    return ''
  }
  const startPort = 8000
  const endPort = 8020
  for (let port = startPort; port < endPort; port++) {
    const base = `http://localhost:${port}`
    try {
      const res = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(800) })
      if (res.ok) {
        const data = await res.json()
        if (data?.app === 'openreel-studio') return base
      }
    } catch {
      // port not available or wrong service, try next
    }
  }
  return `http://localhost:${startPort}`
}

async function getApiBase(): Promise<string> {
  if (_cachedApiBase) return _cachedApiBase
  _cachedApiBase = await discoverApiBase()
  return _cachedApiBase
}

export function resetApiBaseCache() {
  _cachedApiBase = null
}

export function requestCanvasRefresh(options: CanvasRefreshOptions = {}) {
  if (typeof window === "undefined") return
  window.dispatchEvent(new CustomEvent<CanvasRefreshOptions>(CANVAS_REFRESH_EVENT, {
    detail: {
      preserveOnEmpty: true,
      preserveLayout: true,
      ...options,
    },
  }))
}

export function requestWorkflowRefresh(options: WorkflowRefreshOptions = {}) {
  if (typeof window === "undefined") return
  window.dispatchEvent(new CustomEvent<WorkflowRefreshOptions>(WORKFLOW_REFRESH_EVENT, {
    detail: options,
  }))
}

/** Cached API base — returns "" if not yet discovered. Use for non-blocking URL prefixing. */
export function getApiBaseSync(): string {
  return _cachedApiBase || getDesktopApiBase() || process.env.NEXT_PUBLIC_API_BASE_URL || ""
}

/** Resolve a possibly-relative URL (starting with "/api/...") to an absolute URL using the discovered base. */
export function resolveMediaUrl(url: string | null | undefined): string {
  if (!url) return ""
  if (url.startsWith("http://") || url.startsWith("https://") || url.startsWith("data:")) return url
  if (url.startsWith("/")) {
    const base = getApiBaseSync()
    if (base && (url === base || url.startsWith(`${base}/`))) return url
    return base + url
  }
  return url
}

export function resolveAssetLibraryPreviewUrl(projectId: string, path: string): string {
  if (!projectId || !path) return ""
  return resolveMediaUrl(`/api/assets/${projectId}/preview?path=${encodeURIComponent(path)}`)
}

export interface CreateProjectInput {
  title: string
  description?: string
  genre?: string
  format?: string
  episode_count?: number
  duration_per_episode?: number
  budget_level?: 'low' | 'medium' | 'high'
}

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
  }
  return res.json() as Promise<T>
}

export async function listProjects() {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects`)
  return asJson<unknown[]>(res)
}

export async function createProject(data: CreateProjectInput) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return asJson<{ id: string } & Record<string, unknown>>(res)
}

export async function getProject(projectId: string) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}`)
  return asJson<Record<string, unknown>>(res)
}

export async function updateProject(projectId: string, data: Partial<CreateProjectInput>) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  return asJson<Record<string, unknown>>(res)
}

export async function getProjectState(projectId: string) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/state`)
  return asJson<Record<string, unknown>>(res)
}

export async function clearProjectSession(projectId: string) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/session/clear`, {
    method: 'POST',
  })
  return asJson<{
    ok: boolean
    project_id: string
    cleared: string[]
    archived_messages?: number
    cleared_tasks?: number
    removed_memory_facts?: number
    context_cleared_at?: string
  }>(res)
}

export async function getProjectNodes(projectId: string) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes`)
  return asJson<{ nodes: unknown[]; edges: unknown[] }>(res)
}

export interface WorkflowTemplateStepSummary {
  id: string
  title: string
  node_type: CanvasNodeType
  purpose?: string
  depends_on?: string[]
  primary_skill?: string
  skill_category?: string
  acceptance?: string
  phase?: string
  group?: string
  kind?: string
  description?: string
  execution?: "auto" | "manual" | string
  on_error?: "stop" | "continue" | string
  when?: Record<string, unknown>
  uses?: Array<Record<string, unknown>>
  ui?: Record<string, unknown>
  output?: Record<string, unknown>
  authoring?: Record<string, unknown>
  source_node_id?: string
  source_label?: string
  source_category?: string
  source_ui?: string
  source_behavior?: string
  mode?: string
  foreach?: Record<string, unknown> | Record<string, unknown>[]
  role?: string
  start_action?: string
  execution_state?: string
  status?: string
  stale?: boolean
  inputs_schema?: Record<string, unknown> | Record<string, unknown>[]
  expansion?: Record<string, unknown>
  collection?: Record<string, unknown>
  instance_scope?: Record<string, unknown>
  item_source?: string
  item_name?: string
  branch?: string
  template_step_id?: string
  expand_when?: string
  expands_to?: string[]
  repeat_group_id?: string
  repeat_group_label?: string
  repeat_group_index?: number
  prompt_ref?: string
  prompt?: string | Record<string, unknown>
  prompt_spec?: Record<string, unknown>
  prompt_template?: string
  reads_from?: string[]
  layout_after?: string[]
  shape?: string
  child_scope_id?: string
  has_children?: boolean
  surface?: "draft_canvas" | "workflow_runtime" | string
  visibility?: "canvas" | "flow_only" | "workflow_runtime" | string
  canvas_output?: boolean
  runtime_only?: boolean
  runner?: string
  fields?: Record<string, unknown>
  references?: unknown
  reference_selectors?: Array<Record<string, unknown>>
  optional?: boolean
  manual_only?: boolean
  extension?: string
  extension_config?: Record<string, unknown>
  capability?: string
  completion?: Record<string, unknown>
  settings?: Record<string, unknown>
  io?: Record<string, unknown>
  x?: unknown
  "x-openreel"?: unknown
  plugin?: string | Record<string, unknown>
  operation?: string
  runtime_hidden?: boolean
  virtual?: boolean
}

export interface WorkflowNodeTypeDefinition {
  id: string
  type: string
  kind?: string
  title: string
  name?: string
  description?: string
  category?: string
  plugin_id?: string
  plugin_name?: string
  plugin_version?: string
  inputs?: Array<Record<string, unknown>>
  outputs?: Array<Record<string, unknown>>
  settings?: Array<Record<string, unknown>>
  ui?: Record<string, unknown>
  runtime?: Record<string, unknown>
}

export interface WorkflowNodeTypesResponse {
  ok: boolean
  node_types: WorkflowNodeTypeDefinition[]
  plugins?: Array<Record<string, unknown>>
  errors?: Array<Record<string, unknown>>
  total: number
}

export interface WorkflowTemplateGraphScope {
  id: string
  title?: string
  nodes?: WorkflowTemplateStepSummary[]
  edges?: Array<Record<string, unknown>>
}

export interface WorkflowTemplateGraph {
  root_scope_id?: string
  scopes?: Record<string, WorkflowTemplateGraphScope>
}

export interface WorkflowTemplateSummary {
  id: string
  name: string
  description?: string
  tags?: string[]
  ui?: Record<string, unknown>
  extensions?: Record<string, unknown>
  category?: string
  applies_to?: string
  version?: string
  scope?: 'builtin' | 'user' | string
  overrides_builtin?: boolean
  source?: string
  downloadable?: boolean
  active_version_id?: string
  versions?: Array<Record<string, unknown>>
  inputs?: string[]
  inputs_schema?: Record<string, unknown>
  required_inputs?: string[]
  steps: WorkflowTemplateStepSummary[]
  template_graph?: WorkflowTemplateGraph
}

export interface ProjectActiveWorkflow {
  kind: 'template' | 'artifact' | 'imported'
  template_id?: string
  artifact_ref?: string
  workflow?: Record<string, unknown>
  preview?: Record<string, unknown>
  name?: string
  description?: string
  updated_at?: string
  error?: string
}

export interface ProjectWorkflowRuntimeStep {
  id: string
  title?: string
  type?: string
  status?: string
  execution_state?: string
  ready?: boolean
  waiting_on?: string[]
  node_id?: string
  error?: string
  updated_at?: string
  surface?: string
  visibility?: string
  canvas_output?: boolean
  runtime_only?: boolean
  template_step_id?: string
  repeat_group_id?: string
  repeat_group_label?: string
  repeat_group_index?: number
  phase?: string
  group?: string
  kind?: string
  role?: string
  purpose?: string
  acceptance?: string
  primary_skill?: string
  prompt_ref?: string
  depends_on?: string[]
  ui?: Record<string, unknown>
  output?: unknown
  outputs?: Array<Record<string, unknown>>
  artifacts?: Array<Record<string, unknown>>
  resolved_inputs?: Array<Record<string, unknown>>
  authoring?: Record<string, unknown>
  instance_scope?: Record<string, unknown>
  collection?: Record<string, unknown>
  expansion?: Record<string, unknown>
  stale?: boolean
  run_count?: number
  resolved_input_count?: number
  output_count?: number
  output_preview?: string
  artifact_count?: number
  artifact_node_ids?: string[]
  virtual?: boolean
}

export interface ProjectWorkflowRuntime {
  instance_id?: string
  template_id?: string
  template_name?: string
  input_values?: Record<string, unknown>
  status?: string
  pause_requested?: boolean
  pause_requested_at?: string
  pause_reason?: string
  paused_at?: string
  current_step_id?: string
  progress?: {
    total?: number
    completed?: number
    running?: number
    failed?: number
    pending?: number
    ready?: number
    waiting?: number
  }
  updated_at?: string
  steps?: ProjectWorkflowRuntimeStep[]
  local_draft?: boolean
}

export interface WorkflowTemplateListResponse {
  ok: boolean
  project_id: string
  templates: WorkflowTemplateSummary[]
  total: number
  active_workflow?: ProjectActiveWorkflow | null
  active_workflow_runtime?: ProjectWorkflowRuntime | null
  active_workflow_runtimes?: ProjectWorkflowRuntime[]
  workflow_input_values?: Record<string, unknown>
}

export interface MaterializeProjectWorkflowInput {
  template_id?: string
  artifact_ref?: string
  workflow?: Record<string, unknown>
  title?: string
  inputs?: Record<string, unknown>
  context?: Record<string, unknown>
  ui_overrides?: Record<string, unknown>
  origin_x?: number
  origin_y?: number
  spacing_x?: number
  spacing_y?: number
}

export interface RunProjectWorkflowStepInput extends MaterializeProjectWorkflowInput {
  step_id: string
  instance_id?: string
}

export interface RunProjectWorkflowNextInput extends MaterializeProjectWorkflowInput {
  instance_id?: string
}

export interface RunProjectWorkflowAllInput extends RunProjectWorkflowNextInput {
  max_steps?: number
}

export interface PauseProjectWorkflowRunInput {
  instance_id: string
  template_id?: string
  reason?: string
}

export interface PreviewProjectWorkflowInput {
  template_id?: string
  artifact_ref?: string
  workflow?: Record<string, unknown>
  instance_id?: string
  inputs?: Record<string, unknown>
  context?: Record<string, unknown>
}

export interface PreviewProjectWorkflowResponse {
  ok: boolean
  project_id: string
  template_id?: string
  name?: string
  description?: string
  inputs?: string[]
  required_inputs?: string[]
  steps: WorkflowTemplateStepSummary[]
  step_count?: number
  deferred_groups?: Array<Record<string, unknown>>
}

export async function listWorkflowTemplates(projectId: string): Promise<WorkflowTemplateListResponse> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/templates`)
  return asJson<WorkflowTemplateListResponse>(res)
}

export async function saveWorkflowTemplate(
  projectId: string,
  input: {
    workflow: Record<string, unknown>
    template_id?: string
    name?: string
    description?: string
    category?: string
    applies_to?: string
    version?: string
    replace_existing?: boolean
    inputs?: Record<string, unknown>
  },
): Promise<{
  ok: boolean
  project_id: string
  template_id: string
  version_id?: string
  summary?: WorkflowTemplateSummary
  preview?: Record<string, unknown>
  storage_path?: string
}> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/templates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  return asJson<{
    ok: boolean
    project_id: string
    template_id: string
    version_id?: string
    summary?: WorkflowTemplateSummary
    preview?: Record<string, unknown>
    storage_path?: string
  }>(res)
}

export async function downloadWorkflowTemplatePackage(
  projectId: string,
  templateId: string,
  versionId = '',
): Promise<{
  ok: boolean
  project_id: string
  template_id: string
  version_id?: string
  filename?: string
  package: Record<string, unknown>
}> {
  const base = await getApiBase()
  const params = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/templates/${encodeURIComponent(templateId)}/download${params}`)
  return asJson<{
    ok: boolean
    project_id: string
    template_id: string
    version_id?: string
    filename?: string
    package: Record<string, unknown>
  }>(res)
}

export async function restoreBuiltinWorkflowTemplate(
  projectId: string,
  templateId: string,
): Promise<{
  ok: boolean
  project_id: string
  template_id: string
  restored_scope: 'builtin'
  summary: WorkflowTemplateSummary
}> {
  const base = await getApiBase()
  const res = await fetch(
    `${base}/api/projects/${projectId}/workflow/templates/${encodeURIComponent(templateId)}/restore-builtin`,
    { method: 'POST' },
  )
  return asJson<{
    ok: boolean
    project_id: string
    template_id: string
    restored_scope: 'builtin'
    summary: WorkflowTemplateSummary
  }>(res)
}

export async function listWorkflowNodeTypes(): Promise<WorkflowNodeTypesResponse> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/workflow/node-types`)
  return asJson<WorkflowNodeTypesResponse>(res)
}

export async function setProjectActiveWorkflow(
  projectId: string,
  input: ProjectActiveWorkflow,
): Promise<{
  ok: boolean
  project_id: string
  active_workflow?: ProjectActiveWorkflow | null
  active_workflow_runtime?: ProjectWorkflowRuntime | null
  active_workflow_runtimes?: ProjectWorkflowRuntime[]
}> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/active`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  return asJson<{
    ok: boolean
    project_id: string
    active_workflow?: ProjectActiveWorkflow | null
    active_workflow_runtime?: ProjectWorkflowRuntime | null
    active_workflow_runtimes?: ProjectWorkflowRuntime[]
  }>(res)
}

export async function deleteProjectWorkflowRuntime(
  projectId: string,
  instanceId: string,
): Promise<{
  ok: boolean
  project_id: string
  instance_id: string
  deleted: boolean
  active_workflow_runtime?: ProjectWorkflowRuntime | null
  active_workflow_runtimes?: ProjectWorkflowRuntime[]
}> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/runtime/${encodeURIComponent(instanceId)}`, {
    method: 'DELETE',
  })
  return asJson<{
    ok: boolean
    project_id: string
    instance_id: string
    deleted: boolean
    active_workflow_runtime?: ProjectWorkflowRuntime | null
    active_workflow_runtimes?: ProjectWorkflowRuntime[]
  }>(res)
}

export async function pauseProjectWorkflowRun(
  projectId: string,
  input: PauseProjectWorkflowRunInput,
): Promise<{
  ok: boolean
  project_id: string
  instance_id: string
  template_id?: string
  pause_requested?: boolean
  runtime?: ProjectWorkflowRuntime | null
  active_workflow_runtime?: ProjectWorkflowRuntime | null
  active_workflow_runtimes?: ProjectWorkflowRuntime[]
}> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/runtime/${encodeURIComponent(input.instance_id)}/pause`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      template_id: input.template_id || '',
      reason: input.reason || '',
    }),
  })
  const result = await asJson<{
    ok: boolean
    project_id: string
    instance_id: string
    template_id?: string
    pause_requested?: boolean
    runtime?: ProjectWorkflowRuntime | null
    active_workflow_runtime?: ProjectWorkflowRuntime | null
    active_workflow_runtimes?: ProjectWorkflowRuntime[]
  }>(res)
  requestWorkflowRefresh({ projectId })
  return result
}

export async function previewProjectWorkflow(
  projectId: string,
  input: PreviewProjectWorkflowInput,
): Promise<PreviewProjectWorkflowResponse> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  return asJson<PreviewProjectWorkflowResponse>(res)
}

export async function materializeProjectWorkflow<T = Record<string, unknown>>(
  projectId: string,
  input: MaterializeProjectWorkflowInput,
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/materialize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId, preserveOnEmpty: true, preserveLayout: true, fitView: true })
  requestWorkflowRefresh({ projectId })
  return result
}

export async function runProjectWorkflowStep<T = Record<string, unknown>>(
  projectId: string,
  input: RunProjectWorkflowStepInput,
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/run-step`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId, preserveOnEmpty: true, preserveLayout: true, fitView: true })
  requestWorkflowRefresh({ projectId })
  return result
}

export async function runProjectWorkflowNextStep<T = Record<string, unknown>>(
  projectId: string,
  input: RunProjectWorkflowNextInput,
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/run-next`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId, preserveOnEmpty: true, preserveLayout: true, fitView: true })
  requestWorkflowRefresh({ projectId })
  return result
}

export async function runProjectWorkflowAllSteps<T = Record<string, unknown>>(
  projectId: string,
  input: RunProjectWorkflowAllInput,
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/workflow/run-all`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId, preserveOnEmpty: true, preserveLayout: true, fitView: true })
  requestWorkflowRefresh({ projectId })
  return result
}

export type CanvasNodeType = 'text' | 'image' | 'video' | 'audio'

export type ProjectMediaOperation =
  | 'video.export_frame'
  | 'video.split_tracks'
  | 'video.trim'
  | 'video.concat'
  | 'audio.concat'

export interface ProjectMediaOperationInput {
  operation: ProjectMediaOperation
  source_node_id?: string
  source_node_ids?: string[]
  frame_mode?: 'tail' | 'time'
  time_seconds?: number
  range?: { start_seconds: number; end_seconds: number }
  position?: { x: number; y: number }
  title?: string
}

export async function createProjectNode(
  projectId: string,
  input: { type: CanvasNodeType; title?: string; x: number; y: number },
) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<Record<string, unknown>>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function runProjectMediaOperation<T = Record<string, unknown>>(
  projectId: string,
  input: ProjectMediaOperationInput,
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/media-operations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId, preserveOnEmpty: true, preserveLayout: true })
  return result
}

export async function getProjectNodeDetails<T = Record<string, unknown>>(projectId: string, nodeId: string): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}`)
  return asJson<T>(res)
}

export async function updateProjectNodeDetails<T = Record<string, unknown>>(
  projectId: string,
  nodeId: string,
  input: { title?: string; prompt?: string | null; input?: Record<string, unknown>; output?: unknown },
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function uploadProjectNodeMedia<T = Record<string, unknown>>(
  projectId: string,
  nodeId: string,
  file: File,
): Promise<T> {
  const base = await getApiBase()
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}/media`, {
    method: 'POST',
    body: form,
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function switchProjectNodeHistory<T = Record<string, unknown>>(
  projectId: string,
  nodeId: string,
  input: { history_id?: string; index?: number },
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}/history/switch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export interface ProjectMediaHistoryItem {
  id: string
  project_id: string
  kind: 'text' | 'image' | 'video' | 'audio'
  rel_path?: string | null
  url?: string | null
  filename?: string | null
  title?: string | null
  created_at?: string | null
  updated_at?: string | null
  size?: number | null
  mime_type?: string | null
  source?: string | null
  source_node_id?: string | null
  source_node_title?: string | null
  prompt?: string | null
  content?: string | null
  model?: string | null
}

export async function listProjectMediaHistory(projectId: string): Promise<{ items: ProjectMediaHistoryItem[] }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/media-history`)
  return asJson<{ items: ProjectMediaHistoryItem[] }>(res)
}

export async function restoreProjectMediaHistoryItem<T = Record<string, unknown>>(
  projectId: string,
  itemId: string,
  input: { x?: number; y?: number; title?: string | null },
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/media-history/${encodeURIComponent(itemId)}/restore`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function deleteProjectMediaHistoryItem(
  projectId: string,
  itemId: string,
): Promise<{ ok: boolean; id: string; rel_path?: string; deleted?: boolean }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/media-history/${encodeURIComponent(itemId)}`, {
    method: 'DELETE',
  })
  return asJson<{ ok: boolean; id: string; rel_path?: string; deleted?: boolean }>(res)
}

export interface ImageEditOperation {
  type: 'crop' | 'brush' | 'fill' | 'mask' | 'selection' | 'segment' | 'text' | 'arrow'
  unit?: 'normalized' | 'pixel'
  [key: string]: unknown
}

export async function editProjectNodeImage<T = Record<string, unknown>>(
  projectId: string,
  nodeId: string,
  input: {
    action?: 'preview' | 'commit'
    source_ref?: string | null
    candidate_ref?: string | null
    operations?: ImageEditOperation[]
  },
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}/image/edit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  if (input.action === 'commit') requestCanvasRefresh({ projectId })
  return result
}

export async function cleanupProjectNodeImageEdit(
  projectId: string,
  nodeId: string,
): Promise<{ ok: boolean; node_id?: string; deleted_temp_files?: string[]; cleanup_errors?: Array<Record<string, string>> }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}/image/edit/cleanup`, {
    method: 'POST',
  })
  return asJson<{ ok: boolean; node_id?: string; deleted_temp_files?: string[]; cleanup_errors?: Array<Record<string, string>> }>(res)
}

export async function previewProjectNodeImageCurve<T = Record<string, unknown>>(
  projectId: string,
  nodeId: string,
  input: {
    source_ref?: string | null
    color?: string
    detail?: number
    line_strength?: number
    base_visibility?: number
  },
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}/image/curve-preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  return asJson<T>(res)
}

export async function createPanoramaCapture<T = Record<string, unknown>>(
  projectId: string,
  input: {
    title?: string
    data_url: string
    x: number
    y: number
    source_node_id?: string | null
    mode: 'single' | 'four' | 'eight'
  },
): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/panorama/captures`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<T>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function deleteProjectNode(projectId: string, nodeId: string) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}`, {
    method: 'DELETE',
  })
  const result = await asJson<{ ok: boolean; id: string; deleted_edges?: number }>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function deleteProjectNodes(projectId: string, nodeIds: string[]) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/delete`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ node_ids: nodeIds }),
  })
  const result = await asJson<{
    ok: boolean
    id?: string | null
    deleted_node_ids?: string[]
    deleted_nodes?: number
    deleted_edges?: number
    deleted_asset_records?: number
    cleaned_dependency_nodes?: number
  }>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function updateNodePosition(
  projectId: string,
  nodeId: string,
  position: { x: number; y: number },
) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/nodes/${nodeId}/position`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(position),
  })
  const result = await asJson<{ ok: boolean; id: string; position: { x: number; y: number } }>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function createProjectEdge(
  projectId: string,
  sourceNodeId: string,
  targetNodeId: string,
  label?: string | null,
) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/edges`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_node_id: sourceNodeId, target_node_id: targetNodeId, label }),
  })
  const result = await asJson<Record<string, unknown>>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export async function deleteProjectEdge(
  projectId: string,
  edgeId: string,
  endpoints?: { sourceNodeId?: string | null; targetNodeId?: string | null },
) {
  const base = await getApiBase()
  const params = new URLSearchParams()
  if (endpoints?.sourceNodeId) params.set('source_node_id', endpoints.sourceNodeId)
  if (endpoints?.targetNodeId) params.set('target_node_id', endpoints.targetNodeId)
  const query = params.toString()
  const res = await fetch(`${base}/api/projects/${projectId}/edges/${encodeURIComponent(edgeId)}${query ? `?${query}` : ''}`, {
    method: 'DELETE',
  })
  const result = await asJson<{
    ok: boolean
    id: string
    deleted_edge_id?: string | null
    source_node_id?: string | null
    target_node_id?: string | null
    dependency_removed?: boolean
  }>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export interface CanvasNodeSnapshot {
  id: string
  display_id?: number | null
  type: string
  title?: string | null
  status?: string | null
  position?: { x: number; y: number } | null
  input?: Record<string, unknown> | null
  output?: unknown
  prompt?: string | null
  error_message?: string | null
  version?: number | null
  supersedes_id?: string | null
  creator?: string | null
}

export interface CanvasEdgeSnapshot {
  id?: string | null
  source_node_id?: string | null
  target_node_id?: string | null
  source?: string | null
  target?: string | null
  label?: string | null
}

export async function restoreProjectCanvasSnapshot(
  projectId: string,
  input: { nodes?: CanvasNodeSnapshot[]; edges?: CanvasEdgeSnapshot[] },
) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/canvas/restore-snapshot`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  const result = await asJson<{ ok: boolean; nodes: string[]; edges: string[] }>(res)
  requestCanvasRefresh({ projectId })
  return result
}

export interface ProjectAsset {
  id: string
  project_id: string
  node_id?: string | null
  type?: string | null
  name?: string | null
  path?: string | null
  url?: string | null
  mime_type?: string | null
  prompt?: string | null
}

export async function listProjectAssets(projectId: string) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/assets/${projectId}`)
  return asJson<{ project_id: string; assets: ProjectAsset[] }>(res)
}

export async function getPanelLayout<T = unknown>(projectId: string): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/panel/layout`)
  return asJson<T>(res)
}

export async function setPanelLayout<T = unknown>(projectId: string, mode: string): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/panel/layout`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
  return asJson<T>(res)
}

export async function getProjectMessages(projectId: string) {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/projects/${projectId}/messages`)
  return asJson<unknown[]>(res)
}

export async function getModelConfigs() {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/models/configs`)
  return asJson<{ defaults: Record<string, string>; configs: unknown[] }>(res)
}

export async function getProviders() {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/models/providers`)
  return asJson<Record<string, boolean>>(res)
}

export interface UploadedAttachment {
  attachment_id?: string
  rel_path: string
  filename: string
  size: number
  mime_type: string | null
  kind: 'image' | 'video' | 'script' | 'document' | 'other'
  url?: string
  base64_rel_path?: string
  base64_size?: number
  base64_chars?: number
  mention?: string
  ref_label?: string
  display_label?: string
}

export async function uploadFile(
  projectId: string,
  file: File,
): Promise<UploadedAttachment> {
  const base = await getApiBase()
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${base}/api/uploads/${projectId}`, {
    method: 'POST',
    body: form,
  })
  return asJson<UploadedAttachment>(res)
}

export type BlueprintStreamEvent = {
  type:
    | 'blueprint_draft_started'
    | 'blueprint_section_started'
    | 'blueprint_section_delta'
    | 'blueprint_section_completed'
    | 'blueprint_section_needs_revision'
    | 'blueprint_draft_saved'
    | 'blueprint_validation_completed'
    | 'blueprint_proposed'
    | 'blueprint_approved'
    | 'blueprint_revision_proposed'
    | 'blueprint_revision_applied'
    | 'blueprint_cleared'
  project_id?: string
  section_id?: string | null
  title?: string | null
  section_index?: number | null
  window_index?: number | null
  window_count?: number | null
  status?: string | null
  summary_text?: string | null
  failure_reason?: string | null
  display_blocks?: Record<string, unknown>[]
  view_model_patch?: Record<string, unknown>
  blueprint_ref?: Record<string, unknown>
  intake?: Record<string, unknown>
  debug_json_path?: string | null
  validation?: Record<string, unknown>
}

export type BlueprintTreeEvent = {
  type: 'blueprint_tree_changed'
  project_id?: string
  tree_version?: number
  draft_mode?: string
  replacement?: boolean
  action: 'add_child' | 'update_node' | 'delete_node' | 'replace_tree'
  parent_id?: string
  node_id?: string
  node?: Record<string, unknown>
  patch?: Record<string, unknown>
}

export type InteractionStreamEvent = {
  type: 'interaction_input_requested'
  project_id?: string
  status?: string | null
  summary_text?: string | null
  intake?: Record<string, unknown>
}

export type ChatStreamEvent =
  | { type: 'text_delta'; content: string }
  | { type: 'agent_round'; round: number; content: string; source: 'model' | 'action_summary'; tools: string[]; tool_agents?: string[] }
  | { type: 'agent_round_done'; round: number }
  | { type: 'subagent_round'; agent: string; step: number; content: string; tool?: string | null; status?: 'running' | 'completed' | 'failed'; source?: 'model' | null }
  | {
      type: 'token_usage'
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
	    }
  | { type: 'tool_start'; tool: string; round?: number; content?: string; agent?: string | null }
  | { type: 'tool_done'; tool: string; round?: number; result?: unknown; tool_output?: Record<string, unknown> | null; agent?: string | null }
  | { type: 'step_start'; step_index: number; total: number; tool: string; title: string }
  | { type: 'step_done'; step_index: number; tool: string; status: string }
  | { type: 'canvas_action'; action: string; payload: Record<string, unknown> }
  | { type: 'project_update'; project_id: string; updates: Record<string, unknown> }
  | { type: 'project_switch'; project_id: string; title?: string }
  | { type: 'project_reset'; project_id: string; scope: 'full'; title?: string; cleared_all?: boolean; message?: string | null }
  | { type: 'subscribed'; project_id: string }
  | { type: 'proposed_plan'; project_id?: string; plan: Record<string, unknown> }
  | { type: 'checklist_updated'; checklist: unknown }
  | { type: 'slash_command'; command: string; action?: string; ok: boolean; result?: unknown; error?: string; [k: string]: unknown }
  | { type: 'doctor_result'; ok: boolean; project_id: string; text?: string; feature_flags?: AgentFeatureFlagSummary; [k: string]: unknown }
  | { type: 'mode_updated'; ok?: boolean; mode?: string; sub_mode?: string | null; [k: string]: unknown }
  | { type: 'confirm_required'; action: string; scope?: string; reason?: string; [k: string]: unknown }
  | { type: 'queued'; ok?: boolean; queued_count?: number; error?: string }
  | { type: 'merged_messages'; count: number }
  | { type: 'queued_turn_started'; client_user_message_id?: string | null; message?: string | null; queued_remaining?: number | null }
  | { type: 'parallel_start'; total_steps: number; waves: number; project_id: string }
  | { type: 'step_failed'; error: string; step_index?: number | null; tool?: string | null }
  | { type: 'step_completed'; step_index: number; tool: string; title?: string; result?: unknown; progress?: string }
  | { type: 'parallel_done'; completed: number; total: number }
  | { type: 'info'; message: string }
  | { type: 'error'; message: string; recoverable?: boolean }
  | { type: 'cancel_requested'; project_id?: string; streaming?: boolean; queued_count?: number }
  | { type: 'cancelled'; message?: string }
  | BlueprintTreeEvent
  | BlueprintStreamEvent
  | InteractionStreamEvent
  | { type: 'done'; status?: string }
  | { type: string; [k: string]: unknown }

const STREAM_TEXT_DELTA_CHUNK_CHARS = 56
const STREAM_TEXT_DELTA_DISPATCH_DELAY_MS = 8

function splitStreamTextDelta(content: string, maxChars = STREAM_TEXT_DELTA_CHUNK_CHARS): string[] {
  if (maxChars <= 0 || content.length <= maxChars) return [content]
  const preferredBreaks = ' \t\n，。！？；：、,.!?;:'
  const chunks: string[] = []
  let start = 0
  while (start < content.length) {
    let end = Math.min(start + maxChars, content.length)
    if (end < content.length) {
      const windowText = content.slice(start, end)
      let breakAt = -1
      for (const marker of preferredBreaks) {
        breakAt = Math.max(breakAt, windowText.lastIndexOf(marker))
      }
      if (breakAt >= Math.floor(maxChars / 2)) end = start + breakAt + 1
    }
    const chunk = content.slice(start, end)
    if (chunk) chunks.push(chunk)
    start = end
  }
  return chunks
}

function waitForStreamDispatch(ms = STREAM_TEXT_DELTA_DISPATCH_DELAY_MS): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

async function dispatchStreamEvent(
  event: ChatStreamEvent,
  emit: (event: ChatStreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  if (event.type !== 'text_delta' || typeof event.content !== 'string') {
    emit(event)
    return
  }
  for (const chunk of splitStreamTextDelta(event.content)) {
    if (signal.aborted) return
    emit({ ...event, content: chunk })
    if (STREAM_TEXT_DELTA_DISPATCH_DELAY_MS > 0) {
      await waitForStreamDispatch()
    }
  }
}

type SseDispatch = (event: ChatStreamEvent, rawLine: string) => void | Promise<void>

async function dispatchSseLine(
  line: string,
  dispatch: SseDispatch,
  onMalformed?: (line: string) => void,
): Promise<void> {
  const normalized = line.endsWith('\r') ? line.slice(0, -1) : line
  if (!normalized.startsWith('data: ')) return
  try {
    await dispatch(JSON.parse(normalized.slice(6)) as ChatStreamEvent, normalized)
  } catch {
    onMalformed?.(normalized)
  }
}

async function readSseResponse(
  res: Response,
  dispatch: SseDispatch,
  signal: AbortSignal,
  onMalformed?: (line: string) => void,
): Promise<void> {
  const body = res.body
  if (!body || typeof body.getReader !== 'function') {
    const text = await res.text()
    for (const line of text.split('\n')) {
      if (signal.aborted) return
      await dispatchSseLine(line, dispatch, onMalformed)
    }
    return
  }

  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (signal.aborted) return
      await dispatchSseLine(line, dispatch, onMalformed)
    }
  }
  buffer += decoder.decode()
  if (buffer.trim()) {
    await dispatchSseLine(buffer, dispatch, onMalformed)
  }
}

function responseHeadersToRecord(headers: Headers): Record<string, string> {
  const out: Record<string, string> = {}
  headers.forEach((value, key) => {
    out[key] = value
  })
  return out
}

function shouldUseXhrSseFallback(): boolean {
  if (typeof window === 'undefined') return false
  try {
    if (window.localStorage.getItem('drama.forceXhrSse') === '1') return true
  } catch {
    // localStorage can be unavailable in private or embedded contexts.
  }
  return typeof ReadableStream === 'undefined' || typeof TextDecoder === 'undefined'
}

function startXhrSseRequest(
  url: string,
  body: Record<string, unknown>,
  dispatch: SseDispatch,
  signal: AbortSignal,
  onEnd: () => void,
  onError: (err: unknown) => void,
  onMalformed?: (line: string) => void,
  onDebug?: (message: string, data?: unknown) => void,
): () => void {
  const xhr = new XMLHttpRequest()
  let cursor = 0
  let buffer = ''
  let aborted = false
  let processing = Promise.resolve()

  const enqueue = (text: string) => {
    if (!text || aborted) return
    processing = processing
      .then(async () => {
        if (aborted) return
        buffer += text
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          if (aborted || signal.aborted) return
          await dispatchSseLine(line, dispatch, onMalformed)
        }
      })
      .catch(onError)
  }

  const finish = () => {
    processing = processing
      .then(async () => {
        if (aborted || signal.aborted) return
        if (buffer.trim()) {
          await dispatchSseLine(buffer, dispatch, onMalformed)
        }
        buffer = ''
        if (xhr.status < 200 || xhr.status >= 300) {
          onError(new Error(`Stream failed (${xhr.status})`))
          return
        }
        onEnd()
      })
      .catch(onError)
  }

  const pullChunk = () => {
    const text = xhr.responseText.slice(cursor)
    cursor = xhr.responseText.length
    enqueue(text)
  }

  const abort = () => {
    aborted = true
    try {
      xhr.abort()
    } catch {
      // already closed
    }
  }

  signal.addEventListener('abort', abort, { once: true })
  xhr.open('POST', url, true)
  xhr.setRequestHeader('Content-Type', 'application/json')
  xhr.setRequestHeader('Accept', 'text/event-stream')
  xhr.onprogress = () => {
    onDebug?.('progress', { loaded: xhr.responseText.length })
    pullChunk()
  }
  xhr.onload = () => {
    onDebug?.('load', { status: xhr.status, loaded: xhr.responseText.length })
    pullChunk()
    signal.removeEventListener('abort', abort)
    finish()
  }
  xhr.onerror = () => {
    signal.removeEventListener('abort', abort)
    if (!aborted && !signal.aborted) onError(new Error('Stream network error'))
  }
  xhr.onabort = () => {
    aborted = true
    signal.removeEventListener('abort', abort)
  }
  xhr.send(JSON.stringify(body))
  return abort
}

export async function chatStream(
  projectId: string,
  message: string,
  onEvent: (event: ChatStreamEvent) => void,
  attachments: UploadedAttachment[] = [],
  decisionInputs?: Record<string, unknown> | null,
  clientUserMessageId?: string | null,
  referencedNodeIds: string[] = [],
): Promise<() => void> {
  const base = await getApiBase()
  const controller = new AbortController()
  let terminalEventSeen = false
  const debug = () => {
    try {
      return window.localStorage.getItem('drama.debugSse') !== '0'
    } catch {
      return true
    }
  }

  const emit = (event: ChatStreamEvent) => {
    if (event.type === 'done' || event.type === 'error' || event.type === 'cancelled') terminalEventSeen = true
    if (debug()) {
      console.info('[chatStream:sse:event]', event.type, event)
    }
    onEvent(event)
  }

  if (debug()) {
    console.info('[chatStream:start]', { projectId, message, attachments, decisionInputs, clientUserMessageId, referencedNodeIds, url: `${base}/api/chat/stream` })
  }
  const url = `${base}/api/chat/stream`
  const body = {
    project_id: projectId,
    message,
    attachments,
    referenced_node_ids: referencedNodeIds,
    decision_inputs: decisionInputs ?? null,
    client_user_message_id: clientUserMessageId ?? null,
  }
  if (shouldUseXhrSseFallback()) {
    if (debug()) console.info('[chatStream:xhr:start]', { projectId, url })
    const cancelXhr = startXhrSseRequest(
      url,
      body,
      (event) => dispatchStreamEvent(event, emit, controller.signal),
      controller.signal,
      () => {
        if (!terminalEventSeen) {
          if (debug()) console.warn('[chatStream:xhr:end_without_terminal]')
          emit({ type: 'error', message: '连接意外结束，请重试。已创建的节点会保留，可在原节点继续执行。' })
        }
      },
      (err) => {
        if (!controller.signal.aborted) {
          console.error('Stream error:', err)
          emit({ type: 'error', message: `连接中断：${err instanceof Error ? err.message : String(err)}` })
        }
      },
      (line) => {
        if (debug()) console.warn('[chatStream:sse:malformed]', line)
      },
      debug() ? (message, data) => console.info(`[chatStream:xhr:${message}]`, data) : undefined,
    )
    return () => {
      controller.abort()
      cancelXhr()
    }
  }

  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`Stream failed (${res.status})`)
      if (debug()) {
        console.info('[chatStream:response]', { status: res.status, headers: responseHeadersToRecord(res.headers) })
      }
      await readSseResponse(
        res,
        (event) => dispatchStreamEvent(event, emit, controller.signal),
        controller.signal,
        (line) => {
          if (debug()) console.warn('[chatStream:sse:malformed]', line)
        },
      )
      if (!terminalEventSeen) {
        if (debug()) console.warn('[chatStream:end_without_terminal]')
        emit({ type: 'error', message: '连接意外结束，请重试。已创建的节点会保留，可在原节点继续执行。' })
      }
    })
    .catch((err) => {
      if (err.name !== 'AbortError') {
        console.error('Stream error:', err)
        emit({ type: 'error', message: `连接中断：${err instanceof Error ? err.message : String(err)}` })
      } else if (debug()) {
        console.warn('[chatStream:aborted]')
      }
    })

  return () => controller.abort()
}

export async function chatStreamAsync(
  projectId: string,
  message: string,
  onEvent: (event: ChatStreamEvent) => void,
  attachments: UploadedAttachment[] = [],
): Promise<void> {
  await new Promise<void>((resolve) => {
    void chatStream(projectId, message, (event) => {
      onEvent(event)
      if (event.type === 'done' || event.type === 'error') resolve()
    }, attachments)
  })
}

export async function enqueueChat(
  projectId: string,
  message: string,
  attachments: UploadedAttachment[] = [],
  clientUserMessageId?: string | null,
  referencedNodeIds: string[] = [],
): Promise<{ ok?: boolean; queued_count?: number; error?: string }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/chat/enqueue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      project_id: projectId,
      message,
      attachments,
      referenced_node_ids: referencedNodeIds,
      client_user_message_id: clientUserMessageId ?? null,
    }),
  })
  return asJson(res)
}

export async function dequeueChat(
  projectId: string,
  clientUserMessageId: string,
): Promise<{ ok?: boolean; removed?: boolean; queued_count?: number; error?: string }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/chat/dequeue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      project_id: projectId,
      client_user_message_id: clientUserMessageId,
    }),
  })
  return asJson(res)
}

export async function cancelChat(
  projectId: string,
  reason = '',
): Promise<{ ok?: boolean; streaming?: boolean; queued_count?: number; error?: string }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/chat/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, reason }),
  })
  return asJson(res)
}

export async function getChatQueueStatus(
  projectId: string,
): Promise<{ queued?: number; streaming?: boolean; queue_streaming?: boolean; running?: boolean; error?: string }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/chat/queue/${encodeURIComponent(projectId)}`)
  return asJson(res)
}

export interface AgentDoctorSnapshot {
  ok: boolean
  project_id: string
  error?: string
  text?: string
  mode?: {
    ok?: boolean
    mode?: string | null
    sub_mode?: string | null
    is_set?: boolean
    error?: string
  }
  node_summary?: {
    total: number
    by_type: Record<string, number>
    by_status: Record<string, number>
  }
  has_pending_reset?: boolean
  project_mode?: string | null
  feature_flags?: AgentFeatureFlagSummary
}

export interface AgentFeatureFlagState {
  name: string
  enabled: boolean
  default: boolean
  source: string
  killed: boolean
  kill_source?: string | null
  owner: string
  description: string
}

export interface AgentFeatureFlagOwnerSummary {
  total: number
  enabled: number
  disabled: number
  killed: number
}

export interface AgentFeatureFlagSummary {
  total: number
  enabled: number
  disabled: number
  killed: number
  owners: Record<string, AgentFeatureFlagOwnerSummary>
  disabled_names: string[]
  killed_names: string[]
  items: AgentFeatureFlagState[]
}

export interface AgentTraceSummary {
  project_id: string
  run_id: string
  path: string
  source?: 'db' | 'files'
  size_bytes: number | null
  mtime: string | null
  event_count: number
  started_at?: string | null
  last_event_at?: string | null
  last_event?: string | null
  last_tool_name?: string | null
  last_error_kind?: string | null
  error_count: number
}

export interface AgentTraceList {
  project_id: string
  traces: AgentTraceSummary[]
  total: number
  limit: number
  source?: 'db' | 'files'
}

export interface AgentTraceDetail {
  project_id: string
  run_id: string
  path: string
  source?: 'db' | 'files'
  events: Array<Record<string, unknown>>
  event_count: number
  returned: number
  truncated: boolean
  limit: number
}

export interface AgentTokenUsageSummary {
  project_id: string
  run_id?: string | null
  source: 'db' | 'unavailable'
  event_count: number
  limit: number
  since_ts?: string | null
  context_cleared_at?: string | null
  include_before_clear?: boolean
	  totals: Record<string, unknown>
	  by_run: Array<{ run_id: string; totals: Record<string, unknown> }>
	  last_usage?: Record<string, unknown> | null
	  latest_call_tokens?: Record<string, unknown> | null
	  latest_call_context?: Record<string, unknown> | null
	  session_cumulative_tokens?: Record<string, unknown> | null
	  session_context_peak?: Record<string, unknown> | null
	  last_event_at?: string | null
	}

export type AgentArtifactKind = 'traces' | 'prompt_dumps' | 'tool_results'

export interface AgentArtifactSummary {
  id: string
  name: string
  path: string
  relative_path: string
  size_bytes: number
  mtime: string
}

export interface AgentArtifactGroup {
  items: AgentArtifactSummary[]
  total: number
}

export interface AgentArtifactList {
  project_id: string
  limit: number
  artifacts: Record<AgentArtifactKind, AgentArtifactGroup>
}

export interface AgentArtifactContent {
  project_id: string
  kind: AgentArtifactKind
  name: string
  path: string
  relative_path: string
  size_bytes: number
  mtime: string
  max_bytes: number
  tail_lines: number
  mode: 'tail_lines' | 'tail_bytes'
  content: string
  returned_bytes: number
  total_lines?: number
  returned_lines?: number
  offset?: number
  truncated: boolean
  truncated_by_lines: boolean
  truncated_by_bytes: boolean
}

export async function getAgentDoctor(projectId: string): Promise<AgentDoctorSnapshot> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/agent/debug/${projectId}/doctor`)
  return asJson(res)
}

export async function listAgentTraces(projectId: string, limit = 20, source: 'auto' | 'db' | 'files' = 'auto'): Promise<AgentTraceList> {
  const base = await getApiBase()
  const res = await fetch(
    `${base}/api/agent/debug/${projectId}/traces?limit=${encodeURIComponent(String(limit))}&source=${encodeURIComponent(source)}`,
  )
  return asJson(res)
}

export async function getAgentTrace(
  projectId: string,
  runId: string,
  limit = 200,
  source: 'auto' | 'db' | 'files' = 'auto',
): Promise<AgentTraceDetail> {
  const base = await getApiBase()
  const res = await fetch(
    `${base}/api/agent/debug/${projectId}/traces/${encodeURIComponent(runId)}?limit=${encodeURIComponent(String(limit))}&source=${encodeURIComponent(source)}`,
  )
  return asJson(res)
}

export async function getAgentTokenUsage(projectId: string, runId?: string | null, limit = 1000): Promise<AgentTokenUsageSummary> {
  const base = await getApiBase()
  const params = new URLSearchParams({ limit: String(limit) })
  if (runId) params.set('run_id', runId)
  const res = await fetch(`${base}/api/agent/debug/${projectId}/token-usage?${params.toString()}`)
  return asJson(res)
}

export async function listAgentArtifacts(projectId: string, limit = 20): Promise<AgentArtifactList> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/agent/debug/${projectId}/artifacts?limit=${encodeURIComponent(String(limit))}`)
  return asJson(res)
}

export async function readAgentArtifact(
  projectId: string,
  kind: AgentArtifactKind,
  path: string,
  maxBytes = 32768,
  tailLines = 200,
): Promise<AgentArtifactContent> {
  const base = await getApiBase()
  const params = new URLSearchParams({
    kind,
    path,
    max_bytes: String(maxBytes),
    tail_lines: String(tailLines),
  })
  const res = await fetch(`${base}/api/agent/debug/${projectId}/artifacts/read?${params.toString()}`)
  return asJson(res)
}

export interface ToolListItem {
  name: string
  namespace: string
  description: string
  tags: string[]
}

export async function callTool<T = unknown>(
  tool: string,
  args: Record<string, unknown> = {},
): Promise<T> {
  const base = await getApiBase()
  const url = `${base}/api/tools/call`
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tool, args }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    console.error("[openreel:callTool failed]", {
      tool,
      args,
      url,
      status: res.status,
      body: text.slice(0, 1200),
    })
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
  }
  const body = await asJson<{ tool: string; result: T }>(res)
  if (typeof args.project_id === "string" && args.project_id.trim()) {
    requestCanvasRefresh({ projectId: args.project_id.trim() })
  }
  return body.result
}

export async function listAllTools(): Promise<{
  tools: ToolListItem[]
  namespaces: string[]
  total: number
}> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/list`)
  return asJson(res)
}

export async function listMcpServers(): Promise<{
  servers: Array<Record<string, unknown>>
  total: number
}> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/mcp/servers`)
  return asJson(res)
}

export async function getRuntimeConfigFile<T = unknown>(maskSecrets = true): Promise<T> {
  const base = await getApiBase()
  const params = new URLSearchParams({ mask_secrets: String(maskSecrets) })
  const res = await fetch(`${base}/api/tools/config/file?${params.toString()}`)
  return asJson<T>(res)
}

export async function getRuntimeConfigSummary<T = unknown>(): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/config/summary`)
  return asJson<T>(res)
}

export async function getVideoProviderProtocols<T = unknown>(): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/config/video-protocols`)
  return asJson<T>(res)
}

export async function getImageProviderProtocols<T = unknown>(): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/config/image-protocols`)
  return asJson<T>(res)
}

export async function getAudioProviderProtocols<T = unknown>(): Promise<T> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/config/audio-protocols`)
  return asJson<T>(res)
}

export async function validateRuntimeConfig(content: string): Promise<{ ok: boolean; errors: string[] }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/config/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  return asJson(res)
}

export async function writeRuntimeConfigFile(content: string): Promise<{ ok: boolean; errors: string[] }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/config/file`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  return asJson(res)
}

export async function patchRuntimeConfig(patch: Record<string, unknown>): Promise<{ ok: boolean; errors: string[] }> {
  const base = await getApiBase()
  const res = await fetch(`${base}/api/tools/config`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ patch }),
  })
  return asJson(res)
}

export const api = {
  listProjects,
  createProject,
  getProject,
  updateProject,
  getProjectState,
  clearProjectSession,
  getProjectNodes,
  createProjectNode,
  getProjectNodeDetails,
  updateProjectNodeDetails,
  uploadProjectNodeMedia,
  runProjectMediaOperation,
  createPanoramaCapture,
  listProjectMediaHistory,
  restoreProjectMediaHistoryItem,
  deleteProjectMediaHistoryItem,
  cleanupProjectNodeImageEdit,
  deleteProjectNode,
  deleteProjectNodes,
  updateNodePosition,
  createProjectEdge,
  deleteProjectEdge,
  getPanelLayout,
  setPanelLayout,
  getProjectMessages,
  getModelConfigs,
  getProviders,
  uploadFile,
  chatStream,
  chatStreamAsync,
  enqueueChat,
  dequeueChat,
  cancelChat,
  getChatQueueStatus,
  getAgentDoctor,
  listAgentTraces,
  getAgentTrace,
  listAgentArtifacts,
  readAgentArtifact,
  listMcpServers,
  getRuntimeConfigFile,
  getRuntimeConfigSummary,
  getImageProviderProtocols,
  getVideoProviderProtocols,
  getAudioProviderProtocols,
  validateRuntimeConfig,
  writeRuntimeConfigFile,
  patchRuntimeConfig,
}

export const apiClient = api
