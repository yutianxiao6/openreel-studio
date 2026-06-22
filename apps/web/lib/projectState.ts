import type { ProjectRecord } from "@/stores/projectStore"

export function projectStateJson(project: ProjectRecord | null | undefined): Record<string, unknown> {
  const raw = project?.state_json
  if (!raw) return {}
  if (typeof raw === "object" && !Array.isArray(raw)) return raw as Record<string, unknown>
  if (typeof raw !== "string") return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {}
  } catch {
    return {}
  }
}
