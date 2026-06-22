"use client"

import { useState } from "react"
import type { ConfigContext, LlmProviderEntry } from "../SettingsModal"
import { callTool } from "@/lib/api"

export function LlmTab({ ctx }: { ctx: ConfigContext }) {
  const { config, applyPatch } = ctx
  const [editing, setEditing] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [errors, setErrors] = useState<string[]>([])

  const upsertProvider = async (entry: LlmProviderEntry, originalName?: string) => {
    let next = [...config.llm_providers]
    if (originalName) {
      next = next.map((p) => (p.name === originalName ? entry : p))
    } else {
      next.push(entry)
    }
    if (entry.is_default) {
      next = next.map((p) => (p.name === entry.name ? p : { ...p, is_default: false }))
    }
    const r = await applyPatch({ llm_providers: next })
    if (!r.ok) setErrors(r.errors)
    else {
      setErrors([])
      setEditing(null)
      setAdding(false)
    }
    return r
  }

  const removeProvider = async (name: string) => {
    if (!confirm(`确定删除 provider "${name}"？已引用此 provider 的模型映射会回退到默认。`)) return
    const next = config.llm_providers.filter((p) => p.name !== name)
    const cleanedAssign: Record<string, string | null> = {}
    for (const [t, v] of Object.entries(config.model_assignments)) {
      cleanedAssign[t] = v === name ? null : v
    }
    const r = await applyPatch({ llm_providers: next, model_assignments: cleanedAssign })
    if (!r.ok) setErrors(r.errors)
  }

  const setDefault = async (name: string) => {
    const next = config.llm_providers.map((p) => ({ ...p, is_default: p.name === name }))
    const r = await applyPatch({ llm_providers: next })
    if (!r.ok) setErrors(r.errors)
  }

  return (
    <div className="space-y-4">
      {errors.length > 0 && (
        <div className="rounded border border-red-800 bg-red-950/40 text-red-200 text-xs p-3">
          <div className="font-semibold mb-1">应用失败：</div>
          <ul className="list-disc pl-4 space-y-0.5">
            {errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}

      <section>
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-xs font-semibold text-gray-300">LLM Providers</h3>
          <button
            onClick={() => { setAdding(true); setEditing(null) }}
            className="text-xs px-2 py-1 rounded bg-indigo-700/40 hover:bg-indigo-700/60 text-indigo-200 border border-indigo-700"
          >
            + 添加 Provider
          </button>
        </div>
        <div className="space-y-2">
          {config.llm_providers.map((p) => (
            <ProviderRow
              key={p.name}
              entry={p}
              editing={editing === p.name}
              onEdit={() => { setEditing(p.name); setAdding(false) }}
              onCancel={() => setEditing(null)}
              onSave={(updated) => upsertProvider(updated, p.name)}
              onRemove={() => removeProvider(p.name)}
              onSetDefault={() => setDefault(p.name)}
            />
          ))}
          {adding && (
            <ProviderRow
              entry={blankProvider()}
              editing
              onEdit={() => {}}
              onCancel={() => setAdding(false)}
              onSave={(updated) => upsertProvider(updated)}
              onRemove={() => setAdding(false)}
              onSetDefault={() => {}}
            />
          )}
          {config.llm_providers.length === 0 && !adding && (
            <div className="text-center text-gray-500 text-xs py-6 border border-dashed border-gray-800 rounded">
              还没有 LLM Provider。点击「添加」开始。
            </div>
          )}
        </div>
      </section>
    </div>
  )
}

function blankProvider(): LlmProviderEntry {
  return { name: "", provider: "openai", model_name: "", base_url: "",
    api_key: "", context_window_tokens: null, max_input_tokens: null, max_output_tokens: null,
    supports_prompt_cache: null, supports_vision: null, tokenizer: "",
    is_default: false, enabled: true, notes: "", params: {} }
}

function numberToInput(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : ""
}

function parseOptionalInt(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = Number.parseInt(trimmed, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
}

function parseOptionalBool(value: string): boolean | null {
  if (value === "true") return true
  if (value === "false") return false
  return null
}

function boolToInput(value: boolean | null | undefined): string {
  if (value === true) return "true"
  if (value === false) return "false"
  return ""
}

function ProviderRow({
  entry, editing, onEdit, onCancel, onSave, onRemove, onSetDefault,
}: {
  entry: LlmProviderEntry
  editing: boolean
  onEdit: () => void
  onCancel: () => void
  onSave: (e: LlmProviderEntry) => Promise<{ ok: boolean; errors: string[] }>
  onRemove: () => void
  onSetDefault: () => void
}) {
  const [draft, setDraft] = useState<LlmProviderEntry>(entry)
  const [testing, setTesting] = useState<null | { ok: boolean; msg: string }>(null)
  const [paramsText, setParamsText] = useState(() =>
    JSON.stringify(entry.params ?? {}, null, 2),
  )
  const parsedParams = parseParams(paramsText)

  const handleTest = async () => {
    try {
      const r = await callTool<{ ok: boolean; message?: string }>(
        "media.test_provider", { name: draft.name, kind: "llm" })
      setTesting({ ok: !!r.ok, msg: r.message || (r.ok ? "OK" : "失败") })
    } catch {
      setTesting({ ok: false, msg: "test 工具不支持 LLM kind，可在保存后通过对话验证" })
    }
  }

  if (!editing) {
    return (
      <div className="flex items-center gap-3 rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-100 font-medium">{entry.name}</span>
            {entry.is_default && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-300 border border-emerald-800">
                默认
              </span>
            )}
            <span className="text-[10px] text-gray-500 font-mono">{entry.provider}</span>
          </div>
          <div className="text-[11px] text-indigo-300 font-mono truncate">{entry.model_name}</div>
          <div className="text-[10px] text-gray-500 font-mono truncate">{entry.base_url || "(未填写 Base URL)"}</div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-gray-500">
            <span>上下文 {entry.context_window_tokens ?? "未填"}</span>
            <span>输入 {entry.max_input_tokens ?? "未填"}</span>
            <span>输出 {entry.max_output_tokens ?? "未填"}</span>
            <span>缓存 {entry.supports_prompt_cache === true ? "支持" : entry.supports_prompt_cache === false ? "不支持" : "未填"}</span>
            <span>视觉 {entry.supports_vision === true ? "支持" : entry.supports_vision === false ? "不支持" : "未填"}</span>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {!entry.is_default && (
            <button onClick={onSetDefault}
              className="text-[10px] px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">设为默认</button>
          )}
          <button onClick={onEdit}
            className="text-[10px] px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">编辑</button>
          <button onClick={onRemove}
            className="text-[10px] px-2 py-1 rounded bg-red-900/40 hover:bg-red-900/60 text-red-300">删除</button>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-indigo-700/60 bg-indigo-950/20 px-3 py-3 space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <Field label="名称" required value={draft.name} onChange={(v) => setDraft({ ...draft, name: v })} />
        <Field label="Provider 前缀" required value={draft.provider} onChange={(v) => setDraft({ ...draft, provider: v })}
          hint="deepseek / openai / anthropic / dashscope / gemini" />
        <Field label="模型名" required value={draft.model_name} onChange={(v) => setDraft({ ...draft, model_name: v })} />
        <Field label="Base URL" required value={draft.base_url ?? ""} onChange={(v) => setDraft({ ...draft, base_url: v || null })}
          hint="填写服务商或中转站 Base URL。" />
        <Field
          label="上下文窗口 tokens"
          value={numberToInput(draft.context_window_tokens)}
          onChange={(v) => setDraft({ ...draft, context_window_tokens: parseOptionalInt(v) })}
          inputMode="numeric"
          defaultText="默认未知"
          hint="用于上下文使用率、剩余 tokens 和压缩监控。"
        />
        <Field
          label="最大输入 tokens"
          value={numberToInput(draft.max_input_tokens)}
          onChange={(v) => setDraft({ ...draft, max_input_tokens: parseOptionalInt(v) })}
          inputMode="numeric"
          defaultText="默认按上下文窗口计算"
          hint="服务商可用输入上限；通常小于或等于上下文窗口。"
        />
        <Field
          label="最大输出 tokens"
          value={numberToInput(draft.max_output_tokens)}
          onChange={(v) => setDraft({ ...draft, max_output_tokens: parseOptionalInt(v) })}
          inputMode="numeric"
          defaultText="默认 4000"
          hint="未单独设置任务 max_tokens 时使用。"
        />
        <Field
          label="Tokenizer"
          value={draft.tokenizer ?? ""}
          onChange={(v) => setDraft({ ...draft, tokenizer: v || null })}
          defaultText="默认 provider"
          hint="例如 o200k_base / cl100k_base / provider。"
        />
        <SelectField
          label="Prompt Cache"
          value={boolToInput(draft.supports_prompt_cache)}
          onChange={(v) => setDraft({ ...draft, supports_prompt_cache: parseOptionalBool(v) })}
          options={[
            { label: "未填写", value: "" },
            { label: "支持", value: "true" },
            { label: "不支持", value: "false" },
          ]}
          defaultText="默认未填写"
          hint="用于缓存命中率监控，不伪造 provider 未返回的 tokens。"
        />
        <SelectField
          label="视觉输入"
          value={boolToInput(draft.supports_vision)}
          onChange={(v) => setDraft({ ...draft, supports_vision: parseOptionalBool(v) })}
          options={[
            { label: "未填写", value: "" },
            { label: "支持", value: "true" },
            { label: "不支持", value: "false" },
          ]}
          defaultText="默认未填写"
          hint="不支持时会把 image_url 从聊天消息中移除。"
        />
        <Field label="API Key" required value={draft.api_key ?? ""} onChange={(v) => setDraft({ ...draft, api_key: v || null })}
          type="password" />
        <Field label="备注" value={draft.notes ?? ""} onChange={(v) => setDraft({ ...draft, notes: v || null })}
          defaultText="默认空" />
      </div>
      <div>
        <FieldLabel label="模型私有参数 params" defaultText="默认 {}" />
        <textarea
          value={paramsText}
          onChange={(e) => setParamsText(e.target.value)}
          rows={4}
          className="w-full resize-y rounded border border-gray-700 bg-gray-900 px-2 py-1 font-mono text-xs text-gray-100"
        />
        <div className={`mt-0.5 text-[10px] ${parsedParams.ok ? "text-gray-600" : "text-red-300"}`}>
          {parsedParams.ok
            ? "JSON object；可填写 cache_min_input_tokens、billing_multiplier 等模型私有元数据。"
            : parsedParams.error}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-1.5 text-xs text-gray-300">
          <input type="checkbox" checked={draft.is_default}
            onChange={(e) => setDraft({ ...draft, is_default: e.target.checked })} />
          设为默认 <span className="text-[10px] text-gray-500">默认否</span>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-gray-300">
          <input type="checkbox" checked={draft.enabled}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
          启用 <span className="text-[10px] text-gray-500">默认是</span>
        </label>
        <div className="flex-1" />
        {testing && (
          <span className={`text-[11px] ${testing.ok ? "text-emerald-300" : "text-red-300"}`}>
            {testing.ok ? "OK" : "FAIL"} {testing.msg}
          </span>
        )}
        <button onClick={handleTest}
          className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">测试</button>
        <button onClick={onCancel}
          className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">取消</button>
        <button
          onClick={() => onSave({ ...draft, params: parsedParams.value })}
          disabled={
            !draft.name.trim()
            || !draft.provider.trim()
            || !draft.model_name.trim()
            || !(draft.base_url ?? "").trim()
            || !(draft.api_key ?? "").trim()
            || !parsedParams.ok
          }
          className="text-xs px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50">保存</button>
      </div>
    </div>
  )
}

function parseParams(value: string): { ok: true; value: Record<string, unknown> } | { ok: false; error: string; value: Record<string, unknown> } {
  const trimmed = value.trim()
  if (!trimmed) return { ok: true, value: {} }
  try {
    const parsed = JSON.parse(trimmed)
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return { ok: true, value: parsed as Record<string, unknown> }
    }
    return { ok: false, error: "params 必须是 JSON object。", value: {} }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err), value: {} }
  }
}

function FieldLabel({
  label, required = false, defaultText,
}: {
  label: string
  required?: boolean
  defaultText?: string
}) {
  return (
    <label className="mb-0.5 flex items-center gap-1.5 text-[10px] text-gray-500">
      <span>{label}</span>
      {required ? (
        <span className="rounded border border-red-800/70 bg-red-950/40 px-1 py-px text-[9px] text-red-200">必填</span>
      ) : (
        <span className="rounded border border-gray-800 bg-gray-900 px-1 py-px text-[9px] text-gray-400">
          选填{defaultText ? ` · ${defaultText}` : ""}
        </span>
      )}
    </label>
  )
}

function SelectField({ label, value, onChange, options, hint, required = false, defaultText }:
  {
    label: string
    value: string
    onChange: (v: string) => void
    options: Array<{ label: string; value: string }>
    hint?: string
    required?: boolean
    defaultText?: string
  }) {
  return (
    <div>
      <FieldLabel label={label} required={required} defaultText={defaultText} />
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100">
        {options.map((option) => (
          <option key={option.value || "empty"} value={option.value}>{option.label}</option>
        ))}
      </select>
      {hint && <div className="text-[10px] text-gray-600 mt-0.5">{hint}</div>}
    </div>
  )
}

function Field({ label, value, onChange, hint, type = "text", inputMode, required = false, defaultText }:
  {
    label: string
    value: string
    onChange: (v: string) => void
    hint?: string
    type?: string
    inputMode?: "none" | "text" | "tel" | "url" | "email" | "numeric" | "decimal" | "search"
    required?: boolean
    defaultText?: string
  }) {
  return (
    <div>
      <FieldLabel label={label} required={required} defaultText={defaultText} />
      <input type={type} inputMode={inputMode} value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100" />
      {hint && <div className="text-[10px] text-gray-600 mt-0.5">{hint}</div>}
    </div>
  )
}
