"use client"

import { useState } from "react"
import type { ConfigContext, LlmProviderEntry, ModelTier } from "../SettingsModal"

const MODEL_TIERS: Array<{
  key: ModelTier
  title: string
  summary: string
}> = [
  { key: "strong", title: "强模型", summary: "主创作、长上下文和复杂推理。" },
  { key: "balanced", title: "平衡模型", summary: "图片生成、图片编辑和常规生产 worker。" },
  { key: "small", title: "小模型", summary: "审查、摘要和轻量辅助调用。" },
]

const TIER_LABELS: Record<ModelTier, string> = {
  strong: "强",
  balanced: "平衡",
  small: "小",
}

export function LlmTab({ ctx }: { ctx: ConfigContext }) {
  const { config, applyPatch } = ctx
  const [editing, setEditing] = useState<string | null>(null)
  const [addingTier, setAddingTier] = useState<ModelTier | null>(null)
  const [errors, setErrors] = useState<string[]>([])

  const upsertProvider = async (entry: LlmProviderEntry, originalName?: string) => {
    const normalizedEntry = { ...entry, tier: providerTier(entry) }
    let next = [...config.llm_providers]
    const previous = originalName
      ? config.llm_providers.find((p) => p.name === originalName)
      : undefined
    if (originalName) {
      next = next.map((p) => (p.name === originalName ? normalizedEntry : p))
    } else {
      next.push(normalizedEntry)
    }
    const tierDefaults = normalizedTierDefaults(config.model_tier_defaults)
    const assignments = { ...config.model_assignments }
    if (originalName && originalName !== normalizedEntry.name) {
      for (const [task, providerName] of Object.entries(assignments)) {
        if (providerName === originalName) assignments[task] = normalizedEntry.name
      }
      for (const tier of MODEL_TIERS) {
        if (tierDefaults[tier.key] === originalName) {
          tierDefaults[tier.key] = normalizedEntry.name
        }
      }
    }

    const previousTier = previous ? providerTier(previous) : normalizedEntry.tier
    if (
      previous
      && previousTier !== normalizedEntry.tier
      && tierDefaults[previousTier] === (originalName || normalizedEntry.name)
    ) {
      tierDefaults[previousTier] = null
    }
    if (!tierDefaults[normalizedEntry.tier]) {
      tierDefaults[normalizedEntry.tier] = normalizedEntry.name
    }

    const r = await applyPatch({
      llm_providers: next,
      model_tier_defaults: tierDefaults,
      model_assignments: assignments,
    })
    if (!r.ok) setErrors(r.errors)
    else {
      setErrors([])
      setEditing(null)
      setAddingTier(null)
    }
    return r
  }

  const removeProvider = async (name: string) => {
    if (!confirm(`确定删除 provider "${name}"？已引用此 provider 的模型映射会回退到该档默认。`)) return
    const next = config.llm_providers.filter((p) => p.name !== name)
    const cleanedAssign: Record<string, string | null> = {}
    for (const [t, v] of Object.entries(config.model_assignments)) {
      cleanedAssign[t] = v === name ? null : v
    }
    const tierDefaults = normalizedTierDefaults(config.model_tier_defaults)
    for (const tier of MODEL_TIERS) {
      if (tierDefaults[tier.key] === name) tierDefaults[tier.key] = null
    }
    const r = await applyPatch({
      llm_providers: next,
      model_tier_defaults: tierDefaults,
      model_assignments: cleanedAssign,
    })
    if (!r.ok) setErrors(r.errors)
  }

  const setTierDefault = async (tier: ModelTier, name: string) => {
    const tierDefaults = normalizedTierDefaults(config.model_tier_defaults)
    tierDefaults[tier] = name
    const r = await applyPatch({ model_tier_defaults: tierDefaults })
    if (!r.ok) setErrors(r.errors)
  }

  const tierDefaults = normalizedTierDefaults(config.model_tier_defaults)

  return (
    <div className="space-y-4">
      {errors.length > 0 && (
        <div className="rounded border border-red-800 bg-red-950/40 p-3 text-xs text-red-200">
          <div className="mb-1 font-semibold">应用失败：</div>
          <ul className="list-disc space-y-0.5 pl-4">
            {errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}

      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-xs font-semibold text-gray-300">LLM Providers</h3>
          <span className="text-[10px] text-gray-500">三档策略由后端绑定到 Agent 角色</span>
        </div>
        <div className="grid gap-3 lg:grid-cols-3">
          {MODEL_TIERS.map((tier) => {
            const providers = config.llm_providers.filter((p) => providerTier(p) === tier.key)
            return (
              <div key={tier.key} className="rounded-lg border border-gray-800 bg-gray-950/30 p-3">
                <div className="mb-3 flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-semibold text-gray-100">{tier.title}</div>
                    <div className="mt-0.5 text-[11px] leading-4 text-gray-500">{tier.summary}</div>
                    <div className="mt-1 truncate text-[10px] text-gray-600">
                      默认：{tierDefaults[tier.key] || "未设置"}
                    </div>
                  </div>
                  <button
                    onClick={() => { setAddingTier(tier.key); setEditing(null) }}
                    className="shrink-0 rounded border border-indigo-700 bg-indigo-700/40 px-2 py-1 text-[10px] text-indigo-200 hover:bg-indigo-700/60"
                  >
                    + 添加
                  </button>
                </div>

                <div className="space-y-2">
                  {providers.map((p) => (
                    <ProviderRow
                      key={p.name}
                      entry={p}
                      editing={editing === p.name}
                      tierDefault={tierDefaults[tier.key] === p.name}
                      onEdit={() => { setEditing(p.name); setAddingTier(null) }}
                      onCancel={() => setEditing(null)}
                      onSave={(updated) => upsertProvider(updated, p.name)}
                      onRemove={() => removeProvider(p.name)}
                      onSetTierDefault={() => setTierDefault(tier.key, p.name)}
                    />
                  ))}
                  {addingTier === tier.key && (
                    <ProviderRow
                      entry={blankProvider(tier.key)}
                      editing
                      tierDefault={false}
                      onEdit={() => {}}
                      onCancel={() => setAddingTier(null)}
                      onSave={(updated) => upsertProvider(updated)}
                      onRemove={() => setAddingTier(null)}
                      onSetTierDefault={() => {}}
                    />
                  )}
                  {providers.length === 0 && addingTier !== tier.key && (
                    <div className="rounded border border-dashed border-gray-800 py-5 text-center text-xs text-gray-500">
                      还没有{tier.title} Provider。
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}

function blankProvider(tier: ModelTier): LlmProviderEntry {
  return {
    name: "",
    provider: "openai",
    model_name: "",
    base_url: "",
    api_key: "",
    context_window_tokens: null,
    max_input_tokens: null,
    max_output_tokens: null,
    supports_prompt_cache: null,
    supports_vision: null,
    tokenizer: "",
    tier,
    enabled: true,
    notes: "",
    params: {},
  }
}

function providerTier(entry: LlmProviderEntry): ModelTier {
  return entry.tier === "strong" || entry.tier === "small" ? entry.tier : "balanced"
}

function normalizedTierDefaults(value: Partial<Record<ModelTier, string | null>> | undefined): Record<ModelTier, string | null> {
  return {
    strong: value?.strong ?? null,
    balanced: value?.balanced ?? null,
    small: value?.small ?? null,
  }
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
  entry, editing, tierDefault, onEdit, onCancel, onSave, onRemove, onSetTierDefault,
}: {
  entry: LlmProviderEntry
  editing: boolean
  tierDefault: boolean
  onEdit: () => void
  onCancel: () => void
  onSave: (e: LlmProviderEntry) => Promise<{ ok: boolean; errors: string[] }>
  onRemove: () => void
  onSetTierDefault: () => void
}) {
  const [draft, setDraft] = useState<LlmProviderEntry>({ ...entry, tier: providerTier(entry) })

  if (!editing) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="truncate text-sm font-medium text-gray-100">{entry.name}</span>
              {tierDefault && (
                <span className="rounded border border-indigo-800 bg-indigo-900/40 px-1.5 py-0.5 text-[10px] text-indigo-200">
                  档位默认
                </span>
              )}
              <span className="text-[10px] font-mono text-gray-500">{entry.provider}</span>
            </div>
            <div className="truncate font-mono text-[11px] text-indigo-300">{entry.model_name}</div>
            <div className="truncate font-mono text-[10px] text-gray-500">{entry.base_url || "(未填写 Base URL)"}</div>
            <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-gray-500">
              <span>上下文 {entry.context_window_tokens ?? "未填"}</span>
              <span>输入 {entry.max_input_tokens ?? "未填"}</span>
              <span>输出 {entry.max_output_tokens ?? "未填"}</span>
              <span>缓存 {entry.supports_prompt_cache === true ? "支持" : entry.supports_prompt_cache === false ? "不支持" : "未填"}</span>
              <span>视觉 {entry.supports_vision === true ? "支持" : entry.supports_vision === false ? "不支持" : "未填"}</span>
            </div>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {!tierDefault && (
            <button onClick={onSetTierDefault}
              className="rounded bg-indigo-900/40 px-2 py-1 text-[10px] text-indigo-200 hover:bg-indigo-900/60">设为档位默认</button>
          )}
          <button onClick={onEdit}
            className="rounded bg-gray-800 px-2 py-1 text-[10px] text-gray-300 hover:bg-gray-700">编辑</button>
          <button onClick={onRemove}
            className="rounded bg-red-900/40 px-2 py-1 text-[10px] text-red-300 hover:bg-red-900/60">删除</button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2 rounded-lg border border-indigo-700/60 bg-indigo-950/20 px-3 py-3">
      <div className="grid grid-cols-2 gap-2">
        <Field label="名称" required value={draft.name} onChange={(v) => setDraft({ ...draft, name: v })} />
        <SelectField
          label="策略档位"
          value={providerTier(draft)}
          onChange={(v) => setDraft({ ...draft, tier: v as ModelTier })}
          options={MODEL_TIERS.map((tier) => ({ label: `${TIER_LABELS[tier.key]} · ${tier.title}`, value: tier.key }))}
          required
        />
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
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-1.5 text-xs text-gray-300">
          <input type="checkbox" checked={draft.enabled}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
          启用 <span className="text-[10px] text-gray-500">默认是</span>
        </label>
        <div className="flex-1" />
        <button onClick={onCancel}
          className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-700">取消</button>
        <button
          onClick={() => onSave({ ...draft, tier: providerTier(draft) })}
          disabled={
            !draft.name.trim()
            || !draft.provider.trim()
            || !draft.model_name.trim()
            || !(draft.base_url ?? "").trim()
            || !(draft.api_key ?? "").trim()
          }
          className="rounded bg-indigo-600 px-3 py-1 text-xs text-white hover:bg-indigo-500 disabled:opacity-50">保存</button>
      </div>
    </div>
  )
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
        className="w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-100">
        {options.map((option) => (
          <option key={option.value || "empty"} value={option.value}>{option.label}</option>
        ))}
      </select>
      {hint && <div className="mt-0.5 text-[10px] text-gray-600">{hint}</div>}
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
        className="w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-100" />
      {hint && <div className="mt-0.5 text-[10px] text-gray-600">{hint}</div>}
    </div>
  )
}
