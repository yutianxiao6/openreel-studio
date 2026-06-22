export const TASK_TYPES = [
  "agent_loop",
  "planning",
  "character_generation",
  "outline_generation",
  "script_generation",
  "script_review",
  "storyboard_generation",
  "image_prompt_generation",
  "video_prompt_generation",
  "image_generation",
  "video_generation",
] as const;

export type TaskType = (typeof TASK_TYPES)[number];

export const WORKFLOW_NODE_TYPES = [
  "project_setting",
  "character_generation",
  "outline_generation",
  "script_generation",
  "script_review",
  "storyboard_generation",
  "image_prompt_generation",
  "image_generation",
  "video_prompt_generation",
  "video_generation",
  "export",
] as const;

export const API_BASE_PATH = "/api";
export const SSE_CHAT_PATH = "/api/chat/stream";
