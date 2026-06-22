"use client"

import { useState } from "react"
import type { ConfigContext } from "../SettingsModal"

type KnownKeyMeta = {
  key: string
  label: string
  type: "bool" | "int" | "string"
  defaultValue?: unknown
  hint?: string
  options?: Array<{ value: string; label: string }>
}

const KNOWN_KEYS: KnownKeyMeta[] = [
  { key: "agent.skip_confirmations", label: "跳过低风险蓝图修订确认", type: "bool",
    defaultValue: false,
    hint: "只允许字段级低风险小改自动应用；重置、删除、中高风险蓝图改动仍需要确认" },
  { key: "agent.max_iterations", label: "Agent Loop 最大轮次", type: "int",
    defaultValue: 200,
    hint: "默认 200；一节点一调用和长剧集需要较高上限，调低会更省 token 但可能提前停止" },
  { key: "agent.auto_archive", label: "自动归档完成的任务", type: "bool", defaultValue: true },
  { key: "ui.show_token_monitor", label: "聊天栏显示上下文剩余", type: "bool", defaultValue: true,
    hint: "聊天栏只显示上下文剩余；token 花费、输入输出和缓存命中率等明细用 /status 或 Agent 诊断页查看" },
  { key: "agent.blueprint_review_mode", label: "蓝图确认方式", type: "string",
    defaultValue: "continuous_final_review",
    hint: "默认整体确认。模型可以分段生成进度，但只在完整蓝图完成后让用户确认",
    options: [
      { value: "continuous_final_review", label: "整体确认" },
      { value: "section_review", label: "逐节确认" },
    ] },
  { key: "agent.video_plan_confirmation_mode", label: "旧视频确认兼容键", type: "string",
    defaultValue: "one_shot",
    hint: "兼容旧键；新逻辑优先读取蓝图确认方式",
    options: [
      { value: "one_shot", label: "整体确认" },
      { value: "stepwise", label: "逐节确认" },
    ] },
  { key: "ui.canvas_default_view", label: "画布默认视图", type: "string", defaultValue: "canvas", hint: "canvas / panel" },
]

export function AgentTab({ ctx }: { ctx: ConfigContext }) {
  const { config, applyPatch } = ctx
  const [errors, setErrors] = useState<string[]>([])
  const [customKey, setCustomKey] = useState("")
  const [customVal, setCustomVal] = useState("")

  const setKey = async (key: string, value: unknown) => {
    const r = await applyPatch({ app_settings: { [key]: value } })
    if (!r.ok) setErrors(r.errors)
    else setErrors([])
  }

  const removeKey = async (key: string) => {
    if (!confirm(`确定移除 "${key}"？`)) return
    const next = { ...config.app_settings }
    delete next[key]
    const r = await applyPatch({ app_settings: replaceAll(next) })
    if (!r.ok) setErrors(r.errors)
  }

  const addCustom = async () => {
    if (!customKey.trim()) return
    let parsed: unknown = customVal
    try { parsed = JSON.parse(customVal) } catch {}
    const r = await applyPatch({ app_settings: { [customKey.trim()]: parsed } })
    if (r.ok) { setCustomKey(""); setCustomVal(""); setErrors([]) }
    else setErrors(r.errors)
  }

  const knownKeys = new Set(KNOWN_KEYS.map((k) => k.key))
  const customEntries = Object.entries(config.app_settings).filter(([k]) => !knownKeys.has(k))

  return (
    <div className="space-y-4">
      {errors.length > 0 && (
        <div className="rounded border border-red-800 bg-red-950/40 text-red-200 text-xs p-3">
          {errors.map((e, i) => <div key={i}>{e}</div>)}
        </div>
      )}

      <section>
        <h3 className="text-xs font-semibold text-gray-300 mb-2">已知偏好</h3>
        <div className="space-y-2">
          {KNOWN_KEYS.map((meta) => {
            const value = Object.prototype.hasOwnProperty.call(config.app_settings, meta.key)
              ? config.app_settings[meta.key]
              : meta.defaultValue
            return (
              <KnownRow
                key={meta.key}
                meta={meta}
                value={value}
                onChange={(v) => setKey(meta.key, v)}
              />
            )
          })}
        </div>
      </section>

      {customEntries.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-gray-300 mb-2">自定义键</h3>
          <div className="space-y-2">
            {customEntries.map(([k, v]) => (
              <div key={k} className="flex items-center gap-2 rounded border border-gray-800 bg-gray-950/40 px-3 py-2">
                <span className="text-xs text-gray-200 font-mono shrink-0">{k}</span>
                <span className="flex-1 text-[11px] text-indigo-300 font-mono truncate">{JSON.stringify(v)}</span>
                <button onClick={() => removeKey(k)}
                  className="text-[10px] px-2 py-1 rounded bg-red-900/40 hover:bg-red-900/60 text-red-300">移除</button>
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h3 className="text-xs font-semibold text-gray-300 mb-2">添加自定义键</h3>
        <div className="flex items-center gap-2">
          <div className="flex-1">
            <FieldLabel label="Key" required />
            <input value={customKey} onChange={(e) => setCustomKey(e.target.value)}
              placeholder="my.feature.flag"
              className="w-full text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100" />
          </div>
          <div className="flex-1">
            <FieldLabel label="Value" defaultText="默认空字符串" />
            <input value={customVal} onChange={(e) => setCustomVal(e.target.value)}
              placeholder='value（true / 5 / "abc" / {...}）'
              className="w-full text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100" />
          </div>
          <button onClick={addCustom}
            className="text-xs px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-white">添加</button>
        </div>
        <p className="text-[10px] text-gray-600 mt-1">
          值会先 JSON.parse 试，失败则按字符串存
        </p>
      </section>
    </div>
  )
}

function replaceAll(obj: Record<string, unknown>): Record<string, unknown | null> {
  // 全量重写：让 patch 的 deep merge 拿到完整新 dict（None 删除单键 这里保留）
  return obj
}

function KnownRow({
  meta, value, onChange,
}: {
  meta: KnownKeyMeta
  value: unknown
  onChange: (v: unknown) => void
}) {
  return (
    <div className="rounded border border-gray-800 bg-gray-950/40 px-3 py-2">
      <div className="flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-sm text-gray-200">{meta.label}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-gray-600 font-mono">{meta.key}</span>
            <span className="rounded border border-gray-800 bg-gray-900 px-1 py-px text-[9px] text-gray-400">
              选填 · 默认 {formatDefaultValue(meta.defaultValue)}
            </span>
          </div>
          {meta.hint && <div className="text-[10px] text-gray-500 mt-0.5">{meta.hint}</div>}
        </div>
        <div className="shrink-0">
          {meta.type === "bool" && (
            <input type="checkbox" checked={!!value}
              onChange={(e) => onChange(e.target.checked)}
              className="w-4 h-4" />
          )}
          {meta.type === "int" && (
            <input type="number" value={typeof value === "number" ? value : 0}
              onChange={(e) => onChange(parseInt(e.target.value, 10) || 0)}
              className="w-20 text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100" />
          )}
          {meta.type === "string" && (
            meta.options ? (
              <select value={typeof value === "string" ? value : ""}
                onChange={(e) => onChange(e.target.value)}
                className="w-32 text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100">
                {meta.options.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            ) : (
              <input type="text" value={typeof value === "string" ? value : ""}
                onChange={(e) => onChange(e.target.value)}
                className="w-32 text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100" />
            )
          )}
        </div>
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

function formatDefaultValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "开启" : "关闭"
  if (typeof value === "number") return String(value)
  if (typeof value === "string") return value
  return "空"
}
