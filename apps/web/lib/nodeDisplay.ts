export interface NodeDisplayInput {
  type?: string | null
  input?: unknown
  output?: unknown
  prompt?: string | null
  preview?: Record<string, unknown> | null
  previewText?: string | null
}

export function parseJsonValue(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return undefined
  }
}

export function parsePlainObject(value: unknown): Record<string, unknown> | undefined {
  if (!value) return undefined
  if (typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
  if (typeof value !== "string") return undefined
  const parsed = parseJsonValue(value)
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : undefined
}

export function inputFieldsFromNodeInput(input: unknown): Record<string, unknown> {
  const inputObj = parsePlainObject(input) || {}
  const fields = parsePlainObject(inputObj.fields)
  return fields ? { ...inputObj, ...fields } : inputObj
}

function scalarText(value: unknown): string {
  if (typeof value === "string") return value.trim()
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

function isEmptyDisplayValue(value: unknown): boolean {
  return (
    value == null ||
    value === "" ||
    (Array.isArray(value) && value.length === 0) ||
    (typeof value === "object" && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length === 0)
  )
}

const BODY_KEY_ORDER = [
  "title",
  "name",
  "index",
  "content",
  "full_text",
  "story_text",
  "text",
  "script",
  "outline",
  "summary",
  "description",
  "characters",
  "segments",
  "shots",
  "video_prompt",
  "image_prompt",
  "prompt",
]

const UNLABELED_SINGLE_BODY_KEYS = new Set([
  "content",
  "full_text",
  "story_text",
  "text",
  "script",
  "outline",
  "summary",
  "description",
])

const TECHNICAL_HIDDEN_KEYS = new Set([
  "chat_history",
  "depends_on",
  "hidden_keys",
  "labels",
  "llm_task_type",
  "model",
  "ok",
  "prompt_dump_run_id",
  "references",
  "run_id",
  "status",
  "state",
  "surface",
  "fields",
  "type",
  "kind",
  "usage",
  "workflow",
  "workflow_canvas_output",
  "workflow_runtime_runner",
  "workflow_text_runner",
  "prompt_template",
  "prompt_spec",
  "input_facts",
  "input_values",
  "run_history",
  "step_run_history",
  "text_chat_history",
])

const DEFAULT_LABELS: Record<string, string> = {
  title: "标题",
  name: "名称",
  content: "正文",
  full_text: "正文",
  story_text: "剧情正文",
  text: "文本",
  script: "剧本",
  outline: "大纲",
  summary: "摘要",
  description: "说明",
  characters: "人物",
  character: "人物",
  segments: "分段",
  segment: "分段",
  shots: "镜头",
  shot: "镜头",
  video_prompt: "视频提示词",
  image_prompt: "图片提示词",
  prompt: "提示词",
  index: "序号",
  duration_seconds: "时长",
  visual_style: "视觉风格",
  aspect_ratio: "画幅",
  resolution: "分辨率",
  quality: "质量",
  clarity: "清晰度",
}

function keyLabel(key: string, labels?: Record<string, unknown>): string {
  const custom = scalarText(labels?.[key])
  return custom || DEFAULT_LABELS[key] || key.replace(/[_-]+/g, " ")
}

function indent(text: string): string {
  return text.split("\n").map((line) => line ? `  ${line}` : line).join("\n")
}

function orderedEntries(obj: Record<string, unknown>): Array<[string, unknown]> {
  const entries = Object.entries(obj)
  const indexOf = (key: string) => {
    const index = BODY_KEY_ORDER.indexOf(key)
    return index < 0 ? Number.MAX_SAFE_INTEGER : index
  }
  return entries.sort(([a], [b]) => {
    const diff = indexOf(a) - indexOf(b)
    return diff || 0
  })
}

export function readableTextValue(value: unknown, depth = 0): string {
  if (isEmptyDisplayValue(value) || depth > 6) return ""
  if (typeof value === "string") {
    const text = value.trim()
    if (!text) return ""
    if (/^[\[{]/.test(text)) {
      const parsed = parseJsonValue(text)
      const rendered = readableTextValue(parsed, depth + 1)
      if (rendered) return rendered
    }
    return text
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  if (Array.isArray(value)) {
    return value
      .filter((item) => !isEmptyDisplayValue(item))
      .map((item, index) => {
        const rendered = readableTextValue(item, depth + 1)
        if (!rendered) return ""
        return `第 ${index + 1} 项:\n${indent(rendered)}`
      })
      .filter(Boolean)
      .join("\n\n")
  }
  if (typeof value !== "object") return ""

  const obj = value as Record<string, unknown>
  const labels = parsePlainObject(obj.labels)
  const hidden = new Set(
    Array.isArray(obj.hidden_keys)
      ? obj.hidden_keys.map((item) => scalarText(item)).filter(Boolean)
      : [],
  )
  const entries = orderedEntries(obj).filter(([key, item]) => (
    !TECHNICAL_HIDDEN_KEYS.has(key) &&
    !hidden.has(key) &&
    !isEmptyDisplayValue(item)
  ))
  if (entries.length === 1 && UNLABELED_SINGLE_BODY_KEYS.has(entries[0][0])) {
    return readableTextValue(entries[0][1], depth + 1)
  }
  return entries
    .map(([key, item]) => {
      const rendered = readableTextValue(item, depth + 1)
      if (!rendered) return ""
      const label = keyLabel(key, labels)
      return rendered.includes("\n")
        ? `${label}:\n${indent(rendered)}`
        : `${label}: ${rendered}`
    })
    .filter(Boolean)
    .join("\n\n")
}

export function nodeReadableText(node: NodeDisplayInput): string {
  if (node.type === "text") {
    return textNodeReadableText(node)
  }
  const outputText = readableTextValue(node.output)
  if (outputText) return outputText
  const inputText = readableTextValue(inputFieldsFromNodeInput(node.input))
  if (inputText) return inputText
  return scalarText(node.prompt)
}

function firstBodyTextFromObject(obj: Record<string, unknown> | undefined): string {
  if (!obj) return ""
  for (const key of ["content", "full_text", "story_text", "text", "script", "output", "reply", "response", "description"]) {
    const text = scalarText(obj[key])
    if (text) return text
  }
  const result = parsePlainObject(obj.result)
  if (result) {
    const text = firstBodyTextFromObject(result)
    if (text) return text
  }
  return ""
}

function textNodeReadableText(node: NodeDisplayInput): string {
  const outputScalar = scalarText(node.output)
  if (outputScalar) return outputScalar
  const output = parsePlainObject(node.output)
  const outputText = firstBodyTextFromObject(output)
  if (outputText) return outputText
  const input = inputFieldsFromNodeInput(node.input)
  const inputText = firstBodyTextFromObject(input)
  if (inputText) return inputText
  return scalarText(node.prompt)
}

export function nodePromptText(node: NodeDisplayInput): string {
  const input = inputFieldsFromNodeInput(node.input)
  const output = parsePlainObject(node.output) || {}
  for (const value of [
    node.prompt,
    input.prompt,
    input.video_prompt,
    input.image_prompt,
    output.prompt,
    output.video_prompt,
    output.image_prompt,
  ]) {
    const text = scalarText(value)
    if (text) return text
  }
  return ""
}

export function previewTextFromPreview(preview?: Record<string, unknown> | null, prompt?: string | null): string {
  if (!preview) return scalarText(prompt)
  if (preview.type === "text" && typeof preview.text === "string") return preview.text.trim() || scalarText(prompt)
  for (const key of ["summary", "prompt", "identity", "description", "content"]) {
    const text = scalarText(preview[key])
    if (text) return text
  }
  if (Array.isArray(preview.episodes) && preview.episodes.length) {
    return preview.episodes
      .map((item) => parsePlainObject(item)?.title)
      .map(scalarText)
      .filter(Boolean)
      .join("\n")
  }
  if (Array.isArray(preview.shots) && preview.shots.length) {
    return preview.shots
      .map((item, index) => {
        const obj = parsePlainObject(item)
        if (!obj) return ""
        return `${scalarText(obj.index) || index + 1}. ${scalarText(obj.action) || scalarText(obj.shot_type)}`
      })
      .filter(Boolean)
      .join("\n")
  }
  if (Array.isArray(preview.stages)) {
    const promptStage = preview.stages.find((stage) => {
      const obj = parsePlainObject(stage)
      return obj && /提示词|prompt/i.test(scalarText(obj.name)) && scalarText(obj.prompt)
    })
    const obj = parsePlainObject(promptStage)
    const text = scalarText(obj?.prompt)
    if (text) return text
  }
  return scalarText(prompt)
}

export function canvasNodeDisplayText(node: NodeDisplayInput): string {
  const direct = nodeReadableText(node)
  if (direct) return direct
  const previewText = scalarText(node.previewText)
  if (previewText) return previewText
  return previewTextFromPreview(node.preview, node.prompt)
}

export function textPreviewFromNode(node: NodeDisplayInput): Record<string, unknown> | undefined {
  const text = nodeReadableText(node)
  return text ? { type: "text", text } : undefined
}
