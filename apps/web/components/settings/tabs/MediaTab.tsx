"use client"

import { useState } from "react"
import type { ConfigContext, MediaProviderEntry } from "../SettingsModal"
import {
  VIDEO_API_FORMAT_OPTIONS,
  VIDEO_IMAGE_TRANSPORT_OPTIONS,
  VIDEO_MODEL_OPTIONS,
  isKnownVideoModel,
  videoApiFormatForModel,
} from "@/lib/videoModelOptions"

type MediaKind = "image" | "video" | "audio"

const AUDIO_API_FORMAT_OPTIONS = [
  { label: "TTS 语音 (OpenAI-compatible)", value: "openai_tts" },
  { label: "音乐生成 (Suno-compatible)", value: "suno_compatible" },
]

function normalizeAudioApiFormat(value?: string): string {
  return AUDIO_API_FORMAT_OPTIONS.some((item) => item.value === value) ? value as string : "openai_tts"
}

function normalizeVideoImageTransport(value?: unknown): string {
  return VIDEO_IMAGE_TRANSPORT_OPTIONS.some((item) => item.value === value) ? value as string : "data_url"
}

function normalizeMediaProvider(entry: MediaProviderEntry): MediaProviderEntry {
  if (entry.kind === "audio") {
    return {
      ...entry,
      api_format: normalizeAudioApiFormat(entry.api_format),
    }
  }
  if (entry.kind === "image") {
    return {
      ...entry,
      api_format: entry.api_format || "openai",
    }
  }
  return {
    ...entry,
    api_format: entry.api_format?.trim() || videoApiFormatForModel(entry.model_name, "lingke_media_generate"),
  }
}

function kindLabel(kind: MediaKind): string {
  if (kind === "image") return "图片"
  if (kind === "video") return "视频"
  return "音频"
}

export function MediaTab({ ctx, kind }: { ctx: ConfigContext; kind: MediaKind }) {
  const { config, applyPatch } = ctx
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [errors, setErrors] = useState<string[]>([])

  const items = config.media_providers.filter((p) => p.kind === kind)

  const upsert = async (entry: MediaProviderEntry, originalName?: string) => {
    const normalizedEntry = normalizeMediaProvider(entry)
    let next = [...config.media_providers]
    if (originalName) {
      next = next.map((p) =>
        p.kind === kind && p.name === originalName ? normalizedEntry : p,
      )
    } else {
      next.push(normalizedEntry)
    }
    if (normalizedEntry.is_active) {
      next = next.map((p) =>
        p.kind === kind && p.name !== normalizedEntry.name ? { ...p, is_active: false } : p,
      )
    }
    const r = await applyPatch({ media_providers: next })
    if (!r.ok) setErrors(r.errors)
    else { setErrors([]); setEditingKey(null); setAdding(false) }
    return r
  }

  const remove = async (name: string) => {
    if (!confirm(`确定删除 ${kind} provider "${name}"？`)) return
    const next = config.media_providers.filter(
      (p) => !(p.kind === kind && p.name === name),
    )
    const r = await applyPatch({ media_providers: next })
    if (!r.ok) setErrors(r.errors)
  }

  const setActive = async (name: string) => {
    const next = config.media_providers.map((p) =>
      p.kind === kind ? { ...p, is_active: p.name === name } : p,
    )
    const r = await applyPatch({ media_providers: next })
    if (!r.ok) setErrors(r.errors)
  }

  return (
    <div className="space-y-3">
      {errors.length > 0 && (
        <div className="rounded border border-red-800 bg-red-950/40 text-red-200 text-xs p-3">
          {errors.map((e, i) => <div key={i}>{e}</div>)}
        </div>
      )}

      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-500">
          {kindLabel(kind)}生成 Provider
        </p>
        <button
          onClick={() => { setAdding(true); setEditingKey(null) }}
          className="text-xs px-2 py-1 rounded bg-indigo-700/40 hover:bg-indigo-700/60 text-indigo-200 border border-indigo-700"
        >
          + 添加 Provider
        </button>
      </div>

      <div className="space-y-2">
        {items.map((p) => (
          <Row
            key={p.name}
            entry={p}
            editing={editingKey === p.name}
            onEdit={() => { setEditingKey(p.name); setAdding(false) }}
            onCancel={() => setEditingKey(null)}
            onSave={(updated) => upsert(updated, p.name)}
            onRemove={() => remove(p.name)}
            onSetActive={() => setActive(p.name)}
          />
        ))}
        {adding && (
          <Row
            entry={blank(kind)}
            editing
            onEdit={() => {}}
            onCancel={() => setAdding(false)}
            onSave={(updated) => upsert(updated)}
            onRemove={() => setAdding(false)}
            onSetActive={() => {}}
          />
        )}
        {items.length === 0 && !adding && (
          <div className="text-center text-gray-500 text-xs py-6 border border-dashed border-gray-800 rounded">
            还没有 {kindLabel(kind)} Provider。点击「添加」开始。
          </div>
        )}
      </div>
    </div>
  )
}

function blank(kind: MediaKind): MediaProviderEntry {
  return {
    kind,
    name: "",
    base_url: "",
    api_key: "",
    model_name: kind === "audio" ? "tts-1" : "",
    api_format: kind === "video" ? "lingke_media_generate" : kind === "audio" ? "openai_tts" : "openai",
    is_active: false, enabled: true, notes: "", params: {},
  }
}

function Row({
  entry, editing, onEdit, onCancel, onSave, onRemove, onSetActive,
}: {
  entry: MediaProviderEntry
  editing: boolean
  onEdit: () => void
  onCancel: () => void
  onSave: (e: MediaProviderEntry) => Promise<{ ok: boolean; errors: string[] }>
  onRemove: () => void
  onSetActive: () => void
}) {
  const [draft, setDraft] = useState(entry)
  const [advancedOpen, setAdvancedOpen] = useState(false)

  if (!editing) {
    return (
      <div className={`flex items-center gap-3 rounded-lg border px-3 py-2 ${
        entry.is_active
          ? "border-emerald-700/60 bg-emerald-950/20"
          : "border-gray-800 bg-gray-950/40"
      }`}>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-100 font-medium">{entry.name}</span>
            {entry.is_active && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-300 border border-emerald-800">激活</span>
            )}
            <span className="text-[10px] text-gray-500 font-mono">{entry.api_format}</span>
          </div>
          <div className="text-[11px] text-indigo-300 font-mono truncate">{entry.model_name}</div>
          <div className="text-[10px] text-gray-500 font-mono truncate">{entry.base_url}</div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {!entry.is_active && (
            <button onClick={onSetActive}
              className="text-[10px] px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">激活</button>
          )}
          <button onClick={onEdit}
            className="text-[10px] px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">编辑</button>
          <button onClick={onRemove}
            className="text-[10px] px-2 py-1 rounded bg-red-900/40 hover:bg-red-900/60 text-red-300">删除</button>
        </div>
      </div>
    )
  }

  const setField = (k: keyof MediaProviderEntry, v: string | boolean | object) =>
    setDraft({ ...draft, [k]: v } as MediaProviderEntry)
  const setParamField = (key: string, value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const clean = value.trim()
    if (clean) nextParams[key] = clean
    else delete nextParams[key]
    setDraft({ ...draft, params: nextParams })
  }

  const applyVideoTemplate = (modelName: string) => {
    if (!modelName) return
    setDraft({
      ...draft,
      model_name: modelName,
      api_format: videoApiFormatForModel(modelName, draft.api_format || "lingke_media_generate"),
    })
  }
  const setAudioApiFormat = (apiFormat: string) => {
    setDraft({
      ...draft,
      api_format: normalizeAudioApiFormat(apiFormat),
      model_name: draft.model_name || (apiFormat === "suno_compatible" ? "V5" : "tts-1"),
    })
  }

  const selectedVideoTemplate = entry.kind === "video" && isKnownVideoModel(draft.model_name)
    ? draft.model_name
    : ""
  const videoImageTransport = normalizeVideoImageTransport(draft.params?.image_transport)

  return (
    <div className="rounded-lg border border-indigo-700/60 bg-indigo-950/20 px-3 py-3 space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <F label="名称" required value={draft.name} onChange={(v) => setField("name", v)} />
        <F label="Base URL" required value={draft.base_url} onChange={(v) => setField("base_url", v)}
          hint={entry.kind === "video"
            ? "填写当前服务商的 Base URL；请求结构由协议/API Format 决定。"
            : entry.kind === "audio"
              ? draft.api_format === "suno_compatible"
                ? "填写 Suno-compatible 服务的 Base URL；系统不会绑定固定中转站。"
                : "填写 OpenAI-compatible 服务根地址或 /v1 地址；系统会调用 /audio/speech。"
            : undefined} />
        {entry.kind === "video" ? (
          <>
            <SelectField
              label="模型模板"
              value={selectedVideoTemplate}
              onChange={applyVideoTemplate}
              options={[
                { label: "自定义/不套用模板", value: "" },
                ...VIDEO_MODEL_OPTIONS.map((item) => ({
                  label: item.label,
                  value: item.modelName,
                })),
              ]}
              defaultText="可手填"
              hint="选择模板会同时填入模型名和推荐协议；自定义模型可直接改下面的模型名。"
            />
            <F
              label="模型名"
              required
              value={draft.model_name}
              onChange={(v) => setField("model_name", v)}
              hint="填写当前中转站或官方接口实际支持的模型 ID。"
            />
            <div className="col-span-2 rounded border border-gray-800 bg-gray-950/35 p-2">
              <button
                type="button"
                onClick={() => setAdvancedOpen((value) => !value)}
                className="flex w-full items-center justify-between text-left text-[11px] text-gray-300"
              >
                <span>高级设置</span>
                <span className="text-gray-500">{advancedOpen ? "收起" : "展开"}</span>
              </button>
              {advancedOpen && (
                <div className="mt-2 grid grid-cols-2 gap-2">
                  <SelectField
                    label="协议/API Format"
                    value={draft.api_format || "lingke_media_generate"}
                    onChange={(v) => setField("api_format", v)}
                    options={VIDEO_API_FORMAT_OPTIONS}
                    hint="同一个模型名在不同中转站可能使用不同请求结构；这里选择后端适配器。"
                  />
                  <SelectField
                    label="图片输入"
                    value={videoImageTransport}
                    onChange={(v) => setParamField("image_transport", v)}
                    options={VIDEO_IMAGE_TRANSPORT_OPTIONS}
                    hint="默认本地项目图转 Base64/data URL，已有公网 URL 原样传；公网 URL 模式需要服务商能直接访问图片地址。"
                  />
                  {videoImageTransport === "public_url" && (
                    <F
                      label="公网根地址"
                      value={String(draft.params?.public_base_url || "")}
                      onChange={(v) => setParamField("public_base_url", v)}
                      defaultText="默认空"
                      hint="用于把 /api/media/... 项目图片转成外网可访问 URL，例如 https://example.com。"
                    />
                  )}
                </div>
              )}
            </div>
          </>
        ) : entry.kind === "audio" ? (
          <>
            <F label="模型名" required value={draft.model_name} onChange={(v) => setField("model_name", v)}
              hint={draft.api_format === "suno_compatible"
                ? "填写服务商支持的音乐模型名，例如 V5、V5_5；具体以当前 Base URL 文档为准。"
                : "填写服务商支持的 TTS 模型名，例如 tts-1；具体以当前 Base URL 文档为准。"} />
            <SelectField
              label="协议/API Format"
              value={normalizeAudioApiFormat(draft.api_format)}
              onChange={setAudioApiFormat}
              options={AUDIO_API_FORMAT_OPTIONS}
              defaultText="默认 openai_tts"
              hint="TTS 语音走同步 /v1/audio/speech；音乐生成走 Suno-compatible 异步任务协议。"
            />
          </>
        ) : (
          <>
            <F label="模型名" required value={draft.model_name} onChange={(v) => setField("model_name", v)} />
            <F label="协议/API Format" value={draft.api_format} onChange={(v) => setField("api_format", v)}
              defaultText="默认 openai" />
          </>
        )}
        <F label="API Key" required value={draft.api_key ?? ""} type="password"
          onChange={(v) => setField("api_key", v || "")} />
        <F label="备注" value={draft.notes ?? ""} onChange={(v) => setField("notes", v || "")}
          defaultText="默认空" />
      </div>
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-1.5 text-xs text-gray-300">
          <input type="checkbox" checked={draft.is_active}
            onChange={(e) => setField("is_active", e.target.checked)} />
          激活 <span className="text-[10px] text-gray-500">默认否</span>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-gray-300">
          <input type="checkbox" checked={draft.enabled}
            onChange={(e) => setField("enabled", e.target.checked)} />
          启用 <span className="text-[10px] text-gray-500">默认是</span>
        </label>
        <div className="flex-1" />
        <button onClick={onCancel}
          className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">取消</button>
        <button
          onClick={() => onSave(normalizeMediaProvider(draft))}
          disabled={
            !draft.name.trim()
            || !draft.base_url.trim()
            || !draft.model_name.trim()
            || !(draft.api_key ?? "").trim()
          }
          className="text-xs px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50">保存</button>
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

function SelectField({ label, value, onChange, options, hint, disabled = false, required = false, defaultText }:
  {
    label: string
    value: string
    onChange: (v: string) => void
    options: ReadonlyArray<{ label: string; value: string }>
    hint?: string
    disabled?: boolean
    required?: boolean
    defaultText?: string
  }) {
  return (
    <div>
      <FieldLabel label={label} required={required} defaultText={defaultText} />
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="w-full text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100 disabled:text-gray-400 disabled:cursor-not-allowed"
      >
        {options.map((option) => (
          <option key={`${option.value}:${option.label}`} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint && <div className="text-[10px] text-gray-600 mt-0.5">{hint}</div>}
    </div>
  )
}

function F({ label, value, onChange, hint, type = "text", required = false, defaultText }:
  {
    label: string
    value: string
    onChange: (v: string) => void
    hint?: string
    type?: string
    required?: boolean
    defaultText?: string
  }) {
  return (
    <div>
      <FieldLabel label={label} required={required} defaultText={defaultText} />
      <input type={type} value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full text-xs bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100" />
      {hint && <div className="text-[10px] text-gray-600 mt-0.5">{hint}</div>}
    </div>
  )
}
