"use client"

import { useEffect, useMemo, useState } from "react"
import type {
  InteractionInputOption,
  InteractionInputPayload,
  InteractionInputQuestion,
} from "@/stores/chatStore"
import { buildDecisionInputs } from "@/lib/decisionInputs"
import { normalizeInteractionInputPayload } from "@/lib/interactionInput"

interface InteractionInputCardProps {
  inputRequest: InteractionInputPayload
  disabled?: boolean
  onSubmit: (message: string, decisionInputs?: Record<string, unknown> | null) => void
}

const CUSTOM_VALUE = "__custom__"
const EMPTY_QUESTIONS: InteractionInputQuestion[] = []

function questionTitle(question: InteractionInputQuestion): string {
  return question.header || question.question || question.id
}

function initialValue(question: InteractionInputQuestion): string {
  return Array.isArray(question.options) ? question.options[0]?.label || "" : ""
}

function optionLabel(option: InteractionInputOption): string {
  return option.label || ""
}

function answerLine(question: InteractionInputQuestion, value: string): string {
  const clean = value.trim()
  if (!clean || clean === CUSTOM_VALUE) return ""
  return `${questionTitle(question)}：${clean}`
}

function submittedSummary(inputRequest: InteractionInputPayload, values: Record<string, string>): string {
  const lines = inputRequest.questions
    .map((question) => answerLine(question, String(values[question.id] ?? "")))
    .filter(Boolean)
  return [`已提交：${inputRequest.title || "问题"}`, ...lines.map((line) => `- ${line}`)].join("\n")
}

function QuestionInput({
  question,
  value,
  disabled,
  onChange,
}: {
  question: InteractionInputQuestion
  value: string
  disabled?: boolean
  onChange: (value: string) => void
}) {
  const options = Array.isArray(question.options) ? question.options : []
  if (!options.length) {
    return (
      <input
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="输入回答"
        disabled={disabled}
        className="h-9 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-3 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-200/70 disabled:opacity-50"
      />
    )
  }
  const optionLabels = new Set(options.map((option) => option.label).filter(Boolean))
  const isCustom = Boolean(value) && !optionLabels.has(value)
  return (
    <div className="space-y-2">
      <div className="grid gap-1.5 sm:grid-cols-3">
        {options.map((option, index) => {
          const label = optionLabel(option)
          if (!label) return null
          const selected = value === label
          return (
            <button
              key={`${label}-${index}`}
              type="button"
              onClick={() => onChange(label)}
              disabled={disabled}
              className={
                "rounded-md border px-2.5 py-2 text-left text-xs transition-colors disabled:opacity-50 " +
                (selected
                  ? "border-zinc-200/70 bg-white/[0.09] text-zinc-50"
                  : "border-white/10 bg-white/[0.03] text-zinc-300 hover:bg-white/[0.07]")
              }
            >
              <span className="block font-medium">{label}</span>
              {option.description ? <span className="mt-0.5 block text-[10px] leading-snug text-zinc-500">{option.description}</span> : null}
            </button>
          )
        })}
        <button
          type="button"
          onClick={() => onChange(CUSTOM_VALUE)}
          disabled={disabled}
          className={
            "rounded-md border px-2.5 py-2 text-left text-xs transition-colors disabled:opacity-50 " +
            (isCustom || value === CUSTOM_VALUE
              ? "border-zinc-200/70 bg-white/[0.09] text-zinc-50"
              : "border-white/10 bg-white/[0.03] text-zinc-300 hover:bg-white/[0.07]")
          }
        >
          <span className="block font-medium">自定义</span>
          <span className="mt-0.5 block text-[10px] leading-snug text-zinc-500">自己输入，不受选项限制</span>
        </button>
      </div>
      {isCustom || value === CUSTOM_VALUE ? (
        <input
          type="text"
          value={value === CUSTOM_VALUE ? "" : value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="输入自定义内容"
          disabled={disabled}
          className="h-9 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-3 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-200/70 disabled:opacity-50"
        />
      ) : null}
    </div>
  )
}

export function InteractionInputCard({ inputRequest, disabled, onSubmit }: InteractionInputCardProps) {
  const safeInputRequest = useMemo(() => normalizeInteractionInputPayload(inputRequest), [inputRequest])
  const questions = safeInputRequest?.questions ?? EMPTY_QUESTIONS
  const initialValues = useMemo(() => {
    const entries = questions.map((question) => [question.id, initialValue(question)])
    return Object.fromEntries(entries) as Record<string, string>
  }, [questions])
  const [values, setValues] = useState<Record<string, string>>(initialValues)
  const [touched, setTouched] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    setValues(initialValues)
    setTouched(false)
    setSubmitted(false)
  }, [initialValues])

  if (!safeInputRequest) {
    return (
      <div className="mb-2 overflow-hidden rounded-lg border border-red-300/20 bg-red-500/[0.06] px-3 py-2.5 text-xs text-red-100">
        这张问题卡数据不完整，请重新发送需求。
      </div>
    )
  }

  const missingRequired = questions.some((question) => {
    const value = String(values[question.id] ?? "").trim()
    return !value || value === CUSTOM_VALUE
  })
  const submitLabel = safeInputRequest.submit_label || "提交"
  const isLocked = Boolean(disabled || submitted)

  const handleSubmit = () => {
    setTouched(true)
    if (missingRequired || isLocked) return
    const summary = submittedSummary(safeInputRequest, values)
    setSubmitted(true)
    onSubmit(
      summary,
      buildDecisionInputs({
        kind: "interaction_input",
        target: safeInputRequest.purpose || "interaction_input",
        action: "submit",
        values,
        extra: {
          purpose: safeInputRequest.purpose,
          stage: safeInputRequest.stage,
          title: safeInputRequest.title,
          description: safeInputRequest.description,
          submit_label: safeInputRequest.submit_label,
          questions,
        },
      }),
    )
  }

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-white/10 bg-white/[0.035] shadow-sm shadow-black/20">
      <div className="px-3 py-2.5">
        <div className="mb-1 flex items-center gap-2">
          <span className="rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-zinc-400">
            问题
          </span>
        </div>
        <div className="text-[13px] font-medium text-zinc-100">
          {safeInputRequest.title || "补充信息"}
        </div>
        {safeInputRequest.description ? <div className="mt-1 text-xs leading-relaxed text-zinc-500">{safeInputRequest.description}</div> : null}
      </div>
      <div className="space-y-3 px-3 pb-3">
        {questions.map((question) => {
          const value = String(values[question.id] ?? "")
          const invalid = touched && (!value.trim() || value === CUSTOM_VALUE)
          return (
            <div key={question.id}>
              <div className="mb-1.5 flex items-center gap-1.5 text-xs">
                <span className="font-medium text-zinc-300">{questionTitle(question)}</span>
                <span className="text-[10px] text-zinc-600">{question.question}</span>
              </div>
              <QuestionInput
                question={question}
                value={value}
                disabled={isLocked}
                onChange={(next) => setValues((prev) => ({ ...prev, [question.id]: next }))}
              />
              {invalid ? <div className="mt-1 text-[10px] text-red-300">请回答{questionTitle(question)}</div> : null}
            </div>
          )
        })}
      </div>
      <div className="flex items-center gap-2 px-3 pb-3">
        <button
          type="button"
          onClick={handleSubmit}
          disabled={isLocked}
          className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-950 transition-colors hover:bg-white disabled:opacity-40"
        >
          {submitted ? "已提交" : disabled ? "处理中" : submitLabel}
        </button>
        {missingRequired && touched ? <span className="text-[10px] text-red-300">还有问题未回答</span> : null}
      </div>
    </div>
  )
}
