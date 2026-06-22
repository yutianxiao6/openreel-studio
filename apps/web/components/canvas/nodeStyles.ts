/**
 * Per-node-type visual style configuration.
 * Each node type has its own color scheme, icon, shape, and special elements.
 * The actual node component reads from this map.
 */

export interface NodeStyleConfig {
  icon: string
  label: string
  /** Tailwind color class for the accent (border highlight, badges) */
  accent: string
  /** Hex color for connections / minimap */
  color: string
  /** Tailwind class for the running-state border glow */
  runningGlow: string
  /** Shape variant */
  shape: "standard" | "wide" | "square" | "tall" | "pill"
  /** Width in px (used by layout engine) */
  width: number
  /** Optional special decoration element */
  decoration?: "top-bar" | "avatar" | "badge" | "thumb-grid" | "play-overlay" | "timeline-tick" | "folded-corner"
  /** Layer in the panel hierarchy (L0 global, L1 episode, L1.5 segment, L2 scene, L3/4 shot, L5 export) */
  tier?: "global" | "episode" | "segment" | "scene" | "shot" | "shot_artifact" | "export"
}

export const NODE_STYLES: Record<string, NodeStyleConfig> = {
  text: {
    icon: "TX", label: "文本", accent: "emerald", color: "#10b981",
    runningGlow: "shadow-[0_0_20px_rgba(16,185,129,0.5)]",
    shape: "wide", width: 260, decoration: "top-bar", tier: "global",
  },
  image: {
    icon: "IM", label: "图片", accent: "rose", color: "#f43f5e",
    runningGlow: "shadow-[0_0_20px_rgba(244,63,94,0.5)]",
    shape: "wide", width: 260, decoration: "thumb-grid", tier: "shot_artifact",
  },
  video: {
    icon: "VD", label: "视频", accent: "cyan", color: "#06b6d4",
    runningGlow: "shadow-[0_0_20px_rgba(6,182,212,0.5)]",
    shape: "wide", width: 280, decoration: "play-overlay", tier: "export",
  },
  audio: {
    icon: "AU", label: "音频", accent: "amber", color: "#f59e0b",
    runningGlow: "shadow-[0_0_20px_rgba(245,158,11,0.45)]",
    shape: "wide", width: 260, decoration: "timeline-tick", tier: "shot_artifact",
  },
}

export const DEFAULT_NODE_STYLE: NodeStyleConfig = {
  icon: "TL", label: "工具", accent: "gray", color: "#6b7280",
  runningGlow: "shadow-[0_0_20px_rgba(107,114,128,0.5)]",
  shape: "standard", width: 200,
}

export function getNodeStyle(type: string | undefined): NodeStyleConfig {
  if (!type) return DEFAULT_NODE_STYLE
  if (NODE_STYLES[type]) return NODE_STYLES[type]
  return DEFAULT_NODE_STYLE
}
