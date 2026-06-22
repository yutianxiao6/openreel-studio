import type {
  InteractionInputOption,
  InteractionInputPayload,
  InteractionInputQuestion,
} from "@/stores/chatStore"

function cleanText(value: unknown, fallback = ""): string {
  if (typeof value === "string") {
    const trimmed = value.trim()
    return trimmed || fallback
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return fallback
}

function cleanId(value: unknown, fallback: string): string {
  const raw = cleanText(value, fallback)
    .toLowerCase()
    .replace(/[\s-]+/g, "_")
    .replace(/[^\w\u4e00-\u9fa5]/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "")
  return raw || fallback
}

function normalizeOption(raw: unknown): InteractionInputOption | null {
  if (typeof raw === "string") return raw.trim() ? { label: raw.trim() } : null
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null

  const item = raw as Record<string, unknown>
  const label = cleanText(item.label, cleanText(item.value, cleanText(item.title, cleanText(item.id))))
  if (!label) return null

  const description = cleanText(item.description, cleanText(item.help, cleanText(item.caption)))
  return description ? { label, description } : { label }
}

function normalizeQuestion(raw: unknown, index: number): InteractionInputQuestion | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null

  const item = raw as Record<string, unknown>
  const fallbackId = `question_${index + 1}`
  const header = cleanText(item.header, cleanText(item.title, cleanText(item.label)))
  const question = cleanText(
    item.question,
    cleanText(item.prompt, cleanText(item.placeholder, header || `问题 ${index + 1}`)),
  )
  const options = (Array.isArray(item.options) ? item.options : [])
    .map(normalizeOption)
    .filter((option): option is InteractionInputOption => Boolean(option))

  return {
    id: cleanId(item.id ?? item.key, fallbackId),
    header: header || question || `问题 ${index + 1}`,
    question: question || header || `问题 ${index + 1}`,
    options,
  }
}

export function normalizeInteractionInputPayload(raw: unknown): InteractionInputPayload | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null

  const item = raw as Record<string, unknown>
  const seenIds = new Map<string, number>()
  const questions = (Array.isArray(item.questions) ? item.questions : [])
    .map(normalizeQuestion)
    .filter((question): question is InteractionInputQuestion => Boolean(question))
    .map((question) => {
      const count = seenIds.get(question.id) ?? 0
      seenIds.set(question.id, count + 1)
      return count === 0 ? question : { ...question, id: `${question.id}_${count + 1}` }
    })

  if (!questions.length) return null

  const title = cleanText(item.title, cleanText(item.summary_text, questions[0]?.header || "补充信息"))
  const description = cleanText(item.description)
  const submitLabel = cleanText(item.submit_label, cleanText(item.submitLabel, "提交"))
  const purpose = cleanText(item.purpose)

  return {
    ...(purpose ? { purpose } : {}),
    stage: cleanText(item.stage, "general"),
    title,
    ...(description ? { description } : {}),
    submit_label: submitLabel,
    questions,
  }
}
