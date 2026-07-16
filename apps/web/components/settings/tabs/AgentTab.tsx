"use client"

import { useState } from "react"
import type { ConfigContext } from "../SettingsModal"

type KnownKeyMeta = {
  key: string
  label: string
  type: "bool" | "int"
  defaultValue?: unknown
  hint?: string
}

const KNOWN_KEYS: KnownKeyMeta[] = [
  { key: "agent.max_iterations", label: "Agent Loop 最大轮次", type: "int",
    defaultValue: 200,
    hint: "默认 200；一节点一调用和长剧集需要较高上限，调低会更省 token 但可能提前停止" },
  { key: "agent.auto_archive", label: "自动压缩长对话", type: "bool", defaultValue: true,
    hint: "每轮结束后检查上下文长度，达到阈值时压缩历史；完成任务始终由任务系统单独归档。" },
  { key: "ui.show_token_monitor", label: "聊天栏显示上下文剩余", type: "bool", defaultValue: true,
    hint: "聊天栏只显示上下文剩余；token 花费、输入输出和缓存命中率等明细用 /status 或 Agent 诊断页查看" },
  { key: "agent.vision_context_max_images", label: "单次视觉上下文图片上限", type: "int",
    defaultValue: 8,
    hint: "控制一轮模型调用最多携带的真实图片数量。" },
  { key: "agent.vision_context_max_dimension", label: "发送给 AI 的图片最大边长", type: "int",
    defaultValue: 2048,
    hint: "默认 2048px；整张图片等比例缩小，不会裁剪。" },
]

export function AgentTab({ ctx }: { ctx: ConfigContext }) {
  const { config, applyPatch } = ctx
  const [errors, setErrors] = useState<string[]>([])

  const setKey = async (key: string, value: unknown) => {
    const r = await applyPatch({ app_settings: { [key]: value } })
    if (!r.ok) setErrors(r.errors)
    else setErrors([])
  }

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

      <p className="text-[11px] leading-5 text-gray-500">
        Feature flags、kill switches 和其他开发配置请在「配置文件」中维护；这里仅显示运行时有明确消费者的用户偏好。
      </p>
    </div>
  )
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
        </div>
      </div>
    </div>
  )
}


function formatDefaultValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "开启" : "关闭"
  if (typeof value === "number") return String(value)
  return "空"
}
