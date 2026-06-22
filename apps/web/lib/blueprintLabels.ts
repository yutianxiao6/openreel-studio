const BLUEPRINT_DISPLAY_TYPE_LABELS: Record<string, string> = {
  cards: "卡片",
  facts: "信息",
  issues: "校验",
  list: "列表",
  progress: "进度",
  prose: "正文",
  script: "剧本",
  table: "表格",
  timeline: "时间线",
}

const BLUEPRINT_STATUS_LABELS: Record<string, string> = {
  active: "已确认",
  draft: "草稿生成中",
  missing: "无蓝图",
  pending_review: "待确认",
  revision_pending: "修订待确认",
}

export function blueprintDisplayTypeLabel(type: string | null | undefined): string {
  return BLUEPRINT_DISPLAY_TYPE_LABELS[String(type || "")] || ""
}

export function blueprintStatusLabel(status: string | null | undefined, fallback = "未知"): string {
  return BLUEPRINT_STATUS_LABELS[String(status || "")] || fallback
}
