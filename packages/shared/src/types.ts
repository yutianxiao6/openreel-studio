export type ID = string;

export type ProjectStatus = "draft" | "active" | "archived";

export interface ProjectMetadata {
  title: string;
  genre?: string;
  format?: string;
  episode_count?: number;
  duration_per_episode?: number;
  budget_level?: "low" | "medium" | "high";
}

export interface Project {
  id: ID;
  title: string;
  description?: string;
  status: ProjectStatus;
  metadata: ProjectMetadata;
  created_at: string;
  updated_at: string;
}

export type WorkflowNodeType =
  | "text"
  | "image"
  | "video"
  | "audio";

export type WorkflowNodeStatus =
  | "idle"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "waiting_confirm";

export interface WorkflowNode {
  id: ID;
  project_id: ID;
  type: WorkflowNodeType;
  title: string;
  status: WorkflowNodeStatus;
  position: { x: number; y: number };
  input?: unknown;
  output?: unknown;
  model?: string;
  prompt?: string;
  error_message?: string;
  version?: number;
}

export interface WorkflowEdge {
  id: ID;
  project_id: ID;
  source_node_id: ID;
  target_node_id: ID;
  label?: string;
}

export type ChatRole = "user" | "assistant" | "system" | "tool";

export interface ChatMessage {
  id: ID;
  project_id: ID;
  role: ChatRole;
  content: string;
  created_at: string;
}

export type SseEventType =
  | "text_delta"
  | "tool_start"
  | "tool_done"
  | "canvas_action"
  | "project_update"
  | "asset_created"
  | "error"
  | "done";

export interface SseEvent<T = unknown> {
  type: SseEventType;
  payload?: T;
}
