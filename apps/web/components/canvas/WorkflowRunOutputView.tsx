"use client"

export type WorkflowRunDetailOutputItem = {
  title: string
  value: unknown
}

const WORKFLOW_OUTPUT_HIDDEN_KEYS = new Set([
  "id",
  "key",
  "type",
  "kind",
  "status",
  "state",
  "ref",
  "role",
  "node_id",
  "nodeId",
  "source_node_id",
  "sourceNodeId",
  "template_id",
  "template_step_id",
  "workflow_runtime_runner",
  "workflow_text_runner",
  "llm_task_type",
  "run_id",
  "prompt_dump_run_id",
  "usage",
  "model",
])

function hasValue(value: unknown): boolean {
  if (value === undefined || value === null || value === "") return false
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === "object") return Object.keys(value as Record<string, unknown>).length > 0
  return true
}

function asObject(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function stringValue(value: unknown): string {
  if (typeof value === "string") return value.trim()
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

function outputKeyLabel(key: string): string {
  const labels: Record<string, string> = {
    content: "正文",
    full_text: "正文",
    story_text: "剧情正文",
    text: "文本",
    script: "剧本",
    summary: "摘要",
    description: "说明",
    prompt: "提示词",
    image_prompt: "图片提示词",
    video_prompt: "视频提示词",
    audio_prompt: "音频提示词",
    segments: "分段",
    characters: "人物",
    scenes: "场景",
    shots: "镜头",
  }
  return labels[key] || key.replace(/[_-]+/g, " ")
}

function outputParseJson(value: string): unknown | undefined {
  const text = value.trim()
  if (!/^[{[]/.test(text)) return undefined
  try {
    return JSON.parse(text) as unknown
  } catch {
    return undefined
  }
}

function outputStructuredValue(value: unknown): unknown {
  const parsed = typeof value === "string" ? outputParseJson(value) : undefined
  return parsed === undefined ? value : parsed
}

function outputPlainIndent(text: string): string {
  return text.split("\n").map((line) => line ? `  ${line}` : line).join("\n")
}

function outputScalar(value: unknown): string {
  if (value == null || value === "") return ""
  if (typeof value === "boolean") return value ? "是" : "否"
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : ""
  if (typeof value === "string") return value.trim()
  return ""
}

function outputPlainText(value: unknown): string {
  const structured = outputStructuredValue(value)
  const scalar = outputScalar(structured)
  if (scalar) return scalar
  if (Array.isArray(structured)) {
    return structured
      .filter((item) => hasValue(item))
      .map((item, index) => {
        const rendered = outputPlainText(item)
        if (!rendered) return ""
        return `第 ${index + 1} 项:\n${outputPlainIndent(rendered)}`
      })
      .filter(Boolean)
      .join("\n\n")
  }
  const obj = asObject(structured)
  if (!obj) return ""
  const entries = Object.entries(obj).filter(([key, item]) => (
    !WORKFLOW_OUTPUT_HIDDEN_KEYS.has(key) && hasValue(item)
  ))
  if (
    entries.length === 1
    && ["content", "full_text", "story_text", "text", "script", "summary", "description", "prompt"].includes(entries[0][0])
  ) {
    return outputPlainText(entries[0][1])
  }
  return entries
    .map(([key, item]) => {
      const rendered = outputPlainText(item)
      if (!rendered) return ""
      const label = outputKeyLabel(key)
      return rendered.includes("\n")
        ? `${label}:\n${outputPlainIndent(rendered)}`
        : `${label}: ${rendered}`
    })
    .filter(Boolean)
    .join("\n\n")
}

export function workflowRuntimeDetailOutputText(items: WorkflowRunDetailOutputItem[]): string {
  return items
    .map((item, index) => {
      const rendered = outputPlainText(item.value)
      if (!rendered) return ""
      const title = stringValue(item.title) || `输出 ${index + 1}`
      if (items.length === 1 && title === "输出") return rendered
      return `${title}:\n${outputPlainIndent(rendered)}`
    })
    .filter(Boolean)
    .join("\n\n")
}

function outputCollectionRows(value: unknown): Array<Record<string, unknown>> {
  const structured = outputStructuredValue(value)
  const obj = asObject(structured)
  const rows = Array.isArray(obj?.items)
    ? obj.items
    : Array.isArray(structured)
      ? structured
      : []
  return rows.map((item) => asObject(item)).filter((item): item is Record<string, unknown> => Boolean(item))
}

function outputTableColumns(rows: Array<Record<string, unknown>>): string[] {
  const columns: string[] = []
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (WORKFLOW_OUTPUT_HIDDEN_KEYS.has(key) || columns.includes(key)) continue
      columns.push(key)
    }
  }
  return columns.slice(0, 12)
}

export default function WorkflowRunOutputView({ value }: { value: unknown }) {
  const rows = outputCollectionRows(value)
  const columns = outputTableColumns(rows)
  if (rows.length > 0 && columns.length > 0) {
    return (
      <div className="max-h-80 overflow-auto px-3 py-2.5">
        <table className="w-full border-collapse text-left text-[11px] text-emerald-50/90">
          <thead className="sticky top-0 bg-[#10151d] text-emerald-100/70">
            <tr>
              {columns.map((column) => (
                <th key={column} className="border-b border-emerald-200/12 px-2 py-1.5 font-semibold">
                  {outputKeyLabel(column)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index} className="border-b border-white/[0.04] last:border-b-0">
                {columns.map((column) => (
                  <td key={column} className="max-w-[220px] align-top px-2 py-1.5">
                    <div className="whitespace-pre-wrap break-words leading-4">
                      {outputPlainText(row[column])}
                    </div>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }
  return (
    <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words px-3 py-2.5 font-sans text-[12px] leading-5 text-emerald-50/90">
      {outputPlainText(value)}
    </pre>
  )
}
