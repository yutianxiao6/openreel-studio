"use client"

import { useState } from "react"
import type { ConfigContext, MediaProviderEntry, MediaProtocolSummary } from "../SettingsModal"
import {
  VIDEO_IMAGE_TRANSPORT_OPTIONS,
} from "@/lib/videoModelOptions"

type MediaKind = "image" | "video" | "audio"

function normalizeVideoImageTransport(value?: unknown): string {
  return VIDEO_IMAGE_TRANSPORT_OPTIONS.some((item) => item.value === value) ? value as string : "data_url"
}

function normalizeMediaProvider(
  entry: MediaProviderEntry,
  imageProtocols: MediaProtocolSummary[] = [],
  videoProtocols: MediaProtocolSummary[] = [],
  audioProtocols: MediaProtocolSummary[] = [],
): MediaProviderEntry {
  if (entry.kind === "audio") {
    const rawApiFormat = entry.api_format?.trim() || "audio_http_v1"
    const nextParams = { ...(entry.params || {}) }
    delete nextParams.audio_protocol
    delete nextParams.protocol
    const legacyProtocolId =
      rawApiFormat === "suno_compatible" ? "newapi_suno_music"
      : ["openai_tts", "tts", "openai_speech", "openai_audio_speech"].includes(rawApiFormat) ? "openai_audio_speech"
      : ""
    const catalogProtocolId = String(nextParams.audio_protocol_id || legacyProtocolId || audioProtocols[0]?.id || "").trim()
    if (catalogProtocolId) nextParams.audio_protocol_id = catalogProtocolId
    return {
      ...entry,
      api_format: "audio_http_v1",
      params: nextParams,
    }
  }
  if (entry.kind === "image") {
    const rawApiFormat = entry.api_format?.trim() || "image_http_v1"
    const nextParams = { ...(entry.params || {}) }
    delete nextParams.image_protocol
    delete nextParams.protocol
    const catalogProtocolId = String(nextParams.image_protocol_id || imageProtocols[0]?.id || "").trim()
    const shouldUseCatalog = rawApiFormat === "openai" || rawApiFormat === "image_http_v1"
    if (shouldUseCatalog && catalogProtocolId) nextParams.image_protocol_id = catalogProtocolId
    return {
      ...entry,
      api_format: shouldUseCatalog ? "image_http_v1" : rawApiFormat,
      params: nextParams,
    }
  }
  const rawApiFormat = entry.api_format?.trim() || "video_http_v1"
  const nextParams = { ...(entry.params || {}) }
  delete nextParams.video_protocol
  delete nextParams.protocol
  const catalogProtocolId = String(
    nextParams.video_protocol_id
    || protocolIdForVideoModel(entry.model_name, videoProtocols)
    || "",
  ).trim()
  const apiFormat = rawApiFormat === "video_http_v1" || catalogProtocolId ? "video_http_v1" : rawApiFormat
  if (apiFormat === "video_http_v1" && catalogProtocolId) {
    nextParams.video_protocol_id = catalogProtocolId
  }
  return {
    ...entry,
    api_format: apiFormat,
    params: nextParams,
  }
}

function protocolIdForVideoModel(modelName: string, protocols: MediaProtocolSummary[]): string {
  const name = modelName.trim()
  if (!name) return ""
  const matched = protocols.find((protocol) =>
    protocol.model_names?.includes(name)
    || protocol.model_profiles?.some((profile) => profile.match === name),
  )
  return matched?.id || ""
}

function videoModelTemplateOptions(protocols: MediaProtocolSummary[]): Array<{
  label: string
  value: string
  modelName: string
  protocolId: string
}> {
  const options: Array<{ label: string; value: string; modelName: string; protocolId: string }> = []
  const seen = new Set<string>()
  for (const protocol of protocols) {
    const protocolLabel = protocol.display_name || protocol.id
    const add = (modelName: string, label?: string) => {
      const clean = modelName.trim()
      if (!clean) return
      const key = `${protocol.id}:${clean}`
      if (seen.has(key)) return
      seen.add(key)
      options.push({
        label: `${label?.trim() || clean} · ${protocolLabel}`,
        value: key,
        modelName: clean,
        protocolId: protocol.id,
      })
    }
    protocol.model_profiles?.forEach((profile) => add(profile.match || "", profile.label))
    protocol.model_names?.forEach((modelName) => add(modelName))
  }
  return options
}

function kindLabel(kind: MediaKind): string {
  if (kind === "image") return "图片"
  if (kind === "video") return "视频"
  return "音频"
}

export function MediaTab({ ctx, kind }: { ctx: ConfigContext; kind: MediaKind }) {
  const { config, applyPatch } = ctx
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const items = config.media_providers.filter((p) => p.kind === kind)
  const selectedItem = items.find((p) => p.name === selectedKey)
    || items.find((p) => p.is_active)
    || items[0]

  const upsert = async (entry: MediaProviderEntry, originalName?: string) => {
    const normalizedEntry = normalizeMediaProvider(entry, ctx.imageProtocols, ctx.videoProtocols, ctx.audioProtocols)
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
    else {
      setErrors([])
      setEditingKey(null)
      setAdding(false)
      setSelectedKey(normalizedEntry.name)
    }
    return r
  }

  const remove = async (name: string) => {
    if (!confirm(`确定删除 ${kind} provider "${name}"？`)) return
    const next = config.media_providers.filter(
      (p) => !(p.kind === kind && p.name === name),
    )
    const r = await applyPatch({ media_providers: next })
    if (!r.ok) setErrors(r.errors)
    else {
      setSelectedKey((current) => current === name ? null : current)
      setEditingKey((current) => current === name ? null : current)
    }
  }

  const setActive = async (name: string) => {
    const next = config.media_providers.map((p) =>
      p.kind === kind ? { ...p, is_active: p.name === name } : p,
    )
    const r = await applyPatch({ media_providers: next })
    if (!r.ok) setErrors(r.errors)
    else setSelectedKey(name)
  }

  return (
    <div className="space-y-3">
      {errors.length > 0 && (
        <div className="rounded border border-red-800 bg-red-950/40 text-red-200 text-xs p-3">
          {errors.map((e, i) => <div key={i}>{e}</div>)}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-800 bg-gray-950/35 px-3 py-3">
        <div>
          <div className="text-sm font-semibold text-gray-100">{kindLabel(kind)}生成模型</div>
          <p className="mt-0.5 text-xs text-gray-500">
            配置服务商和模型协议；节点编辑里可以直接选择这里启用的模型。
          </p>
        </div>
        <button
          onClick={() => { setAdding(true); setEditingKey(null); setSelectedKey(null) }}
          className="text-xs px-2 py-1 rounded bg-indigo-700/40 hover:bg-indigo-700/60 text-indigo-200 border border-indigo-700"
        >
          + 添加 Provider
        </button>
      </div>

      <div className="grid gap-3 lg:grid-cols-[330px_minmax(0,1fr)]">
        <div className="space-y-2">
          {items.map((p) => (
            <Row
              key={p.name}
              entry={p}
              editing={false}
              selected={!adding && selectedItem?.name === p.name}
              onSelect={() => { setSelectedKey(p.name); setAdding(false) }}
              onEdit={() => { setSelectedKey(p.name); setEditingKey(p.name); setAdding(false) }}
              onCancel={() => setEditingKey(null)}
              onSave={(updated) => upsert(updated, p.name)}
              onRemove={() => remove(p.name)}
              onSetActive={() => setActive(p.name)}
              imageProtocols={ctx.imageProtocols}
              videoProtocols={ctx.videoProtocols}
              audioProtocols={ctx.audioProtocols}
            />
          ))}
          {items.length === 0 && !adding && (
            <div className="text-center text-gray-500 text-xs py-8 border border-dashed border-gray-800 rounded-lg bg-gray-950/25">
              还没有 {kindLabel(kind)} Provider。点击「添加」开始。
            </div>
          )}
        </div>

        <div className="min-w-0">
          {adding ? (
            <Row
              key={`new-${kind}`}
              entry={blank(kind)}
              editing
              onEdit={() => {}}
              onCancel={() => setAdding(false)}
              onSave={(updated) => upsert(updated)}
              onRemove={() => setAdding(false)}
              onSetActive={() => {}}
              imageProtocols={ctx.imageProtocols}
              videoProtocols={ctx.videoProtocols}
              audioProtocols={ctx.audioProtocols}
            />
          ) : selectedItem && editingKey === selectedItem.name ? (
            <Row
              key={`edit-${selectedItem.name}`}
              entry={selectedItem}
              editing
              onEdit={() => {}}
              onCancel={() => setEditingKey(null)}
              onSave={(updated) => upsert(updated, selectedItem.name)}
              onRemove={() => remove(selectedItem.name)}
              onSetActive={() => setActive(selectedItem.name)}
              imageProtocols={ctx.imageProtocols}
              videoProtocols={ctx.videoProtocols}
              audioProtocols={ctx.audioProtocols}
            />
          ) : selectedItem ? (
            <ProviderSummary
              entry={selectedItem}
              onEdit={() => setEditingKey(selectedItem.name)}
              onRemove={() => remove(selectedItem.name)}
              onSetActive={() => setActive(selectedItem.name)}
            />
          ) : (
            <div className="rounded-lg border border-dashed border-gray-800 bg-gray-950/25 px-4 py-10 text-center text-xs text-gray-500">
              选择左侧 Provider 查看详情，或添加一个新的 {kindLabel(kind)}模型。
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function ProviderSummary({
  entry,
  onEdit,
  onRemove,
  onSetActive,
}: {
  entry: MediaProviderEntry
  onEdit: () => void
  onRemove: () => void
  onSetActive: () => void
}) {
  const protocolId = String(
    (
      entry.kind === "image" ? entry.params?.image_protocol_id
      : entry.kind === "video" ? entry.params?.video_protocol_id
      : entry.params?.audio_protocol_id
    ) || "",
  )
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-950/35">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-800 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-gray-100">{entry.name}</span>
            {entry.is_active && (
              <span className="rounded border border-emerald-800 bg-emerald-950/50 px-1.5 py-0.5 text-[10px] text-emerald-300">默认</span>
            )}
            {!entry.enabled && (
              <span className="rounded border border-gray-700 bg-gray-900 px-1.5 py-0.5 text-[10px] text-gray-400">停用</span>
            )}
          </div>
          <div className="mt-1 truncate font-mono text-xs text-indigo-300">{entry.model_name}</div>
        </div>
        <div className="flex items-center gap-1.5">
          {!entry.is_active && (
            <button onClick={onSetActive} className="rounded bg-gray-800 px-2 py-1 text-[10px] text-gray-300 hover:bg-gray-700">设为默认</button>
          )}
          <button onClick={onEdit} className="rounded bg-indigo-700/40 px-2 py-1 text-[10px] text-indigo-200 hover:bg-indigo-700/60">编辑</button>
          <button onClick={onRemove} className="rounded bg-red-900/40 px-2 py-1 text-[10px] text-red-300 hover:bg-red-900/60">删除</button>
        </div>
      </div>
      <div className="grid gap-3 px-4 py-4 sm:grid-cols-2">
        <SummaryField label="API Base URL" value={entry.base_url} mono />
        {entry.kind === "video" && String(entry.params?.upload_base_url || "").trim() && (
          <SummaryField label="上传 API Base URL" value={String(entry.params?.upload_base_url)} mono />
        )}
        <SummaryField label="协议 ID" value={protocolId || "未设置"} mono />
        <SummaryField label="API Key" value={entry.api_key ? "已配置" : "未配置"} />
        {entry.notes && <SummaryField label="备注" value={entry.notes} />}
      </div>
    </div>
  )
}

function SummaryField({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-md border border-gray-800 bg-gray-900/45 px-3 py-2">
      <div className="mb-1 text-[10px] text-gray-500">{label}</div>
      <div className={`truncate text-xs text-gray-200 ${mono ? "font-mono" : ""}`}>{value}</div>
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
    api_format: kind === "video" ? "video_http_v1" : kind === "audio" ? "audio_http_v1" : "image_http_v1",
    is_active: false, enabled: true, notes: "", params: {},
  }
}

function Row({
  entry, editing, selected = false, onSelect, onEdit, onCancel, onSave, onRemove, onSetActive, imageProtocols, videoProtocols, audioProtocols,
}: {
  entry: MediaProviderEntry
  editing: boolean
  selected?: boolean
  onSelect?: () => void
  onEdit: () => void
  onCancel: () => void
  onSave: (e: MediaProviderEntry) => Promise<{ ok: boolean; errors: string[] }>
  onRemove: () => void
  onSetActive: () => void
  imageProtocols: MediaProtocolSummary[]
  videoProtocols: MediaProtocolSummary[]
  audioProtocols: MediaProtocolSummary[]
}) {
  const [draft, setDraft] = useState(() => normalizeMediaProvider(entry, imageProtocols, videoProtocols, audioProtocols))
  const [advancedOpen, setAdvancedOpen] = useState(false)

  if (!editing) {
    return (
      <button
        type="button"
        onClick={onSelect}
        className={`block w-full rounded-lg border px-3 py-2.5 text-left transition ${
          selected
            ? "border-indigo-500/70 bg-indigo-950/25 shadow-[0_0_0_1px_rgba(99,102,241,0.22)]"
            : entry.is_active
              ? "border-emerald-700/60 bg-emerald-950/15 hover:border-emerald-600/70"
              : "border-gray-800 bg-gray-950/35 hover:border-gray-700"
        }`}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-100">{entry.name}</span>
            {entry.is_active && (
              <span className="rounded border border-emerald-800 bg-emerald-900/50 px-1.5 py-0.5 text-[10px] text-emerald-300">默认</span>
            )}
            {!entry.enabled && (
              <span className="rounded border border-gray-700 bg-gray-900 px-1.5 py-0.5 text-[10px] text-gray-400">停用</span>
            )}
          </div>
          <div className="mt-1 truncate font-mono text-[11px] text-indigo-300">{entry.model_name}</div>
          <div className="mt-1 flex min-w-0 items-center gap-2 text-[10px] text-gray-500">
            <span className="shrink-0 font-mono">{entry.api_format}</span>
            <span className="min-w-0 truncate font-mono">{entry.base_url}</span>
          </div>
        </div>
      </button>
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
  const setImageProtocolId = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    delete nextParams.image_protocol
    delete nextParams.protocol
    const clean = value.trim()
    if (clean) nextParams.image_protocol_id = clean
    else delete nextParams.image_protocol_id
    setDraft({ ...draft, api_format: "image_http_v1", params: nextParams })
  }
  const setVideoProtocolId = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    delete nextParams.video_protocol
    delete nextParams.protocol
    const clean = value.trim()
    if (clean) nextParams.video_protocol_id = clean
    else delete nextParams.video_protocol_id
    setDraft({ ...draft, params: nextParams })
  }
  const setAudioProtocolId = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    delete nextParams.audio_protocol
    delete nextParams.protocol
    const clean = value.trim()
    if (clean) nextParams.audio_protocol_id = clean
    else delete nextParams.audio_protocol_id
    setDraft({ ...draft, api_format: "audio_http_v1", params: nextParams })
  }

  const videoModelTemplates = videoModelTemplateOptions(videoProtocols)
  const applyVideoTemplate = (templateKey: string) => {
    const template = videoModelTemplates.find((item) => item.value === templateKey)
    if (!template) return
    const nextParams = { ...(draft.params || {}) }
    delete nextParams.video_protocol
    delete nextParams.protocol
    nextParams.video_protocol_id = template.protocolId
    setDraft({
      ...draft,
      model_name: template.modelName,
      api_format: "video_http_v1",
      params: nextParams,
    })
  }
  const selectedVideoTemplate = entry.kind === "video"
    ? videoModelTemplates.find((item) =>
      item.modelName === draft.model_name && item.protocolId === String(draft.params?.video_protocol_id || ""),
    )?.value || ""
    : ""
  const imageInputTransport = normalizeVideoImageTransport(draft.params?.image_transport)
  const imageProtocolId = String(draft.params?.image_protocol_id || "")
  const imageProtocolOptions = imageProtocols.map((item) => ({
    label: item.display_name && item.display_name !== item.id
      ? `${item.display_name} · ${item.id}`
      : item.id,
    value: item.id,
  }))
  const selectedCatalogImageProtocolId = imageProtocolOptions.some((item) => item.value === imageProtocolId)
    ? imageProtocolId
    : ""
  const canSaveImageProtocol = draft.api_format !== "image_http_v1" || Boolean(selectedCatalogImageProtocolId)
  const videoProtocolId = String(draft.params?.video_protocol_id || "")
  const videoProtocolOptions = videoProtocols.map((item) => ({
    label: item.display_name && item.display_name !== item.id
      ? `${item.display_name} · ${item.id}`
      : item.id,
    value: item.id,
  }))
  const selectedCatalogProtocolId = videoProtocolOptions.some((item) => item.value === videoProtocolId)
    ? videoProtocolId
    : ""
  const selectedVideoProtocol = videoProtocols.find((item) => item.id === selectedCatalogProtocolId)
  const additionalVideoBaseUrls = selectedVideoProtocol?.additional_base_urls || []
  const hasRequiredVideoBaseUrls = additionalVideoBaseUrls.every((item) =>
    !item.required || Boolean(String(draft.params?.[item.param] || "").trim()),
  )
  const canSaveVideoProtocol = draft.api_format !== "video_http_v1"
    || (Boolean(selectedCatalogProtocolId) && hasRequiredVideoBaseUrls)
  const audioProtocolId = String(draft.params?.audio_protocol_id || "")
  const audioProtocolOptions = audioProtocols.map((item) => ({
    label: item.display_name && item.display_name !== item.id
      ? `${item.display_name} · ${item.id}`
      : item.id,
    value: item.id,
  }))
  const selectedCatalogAudioProtocolId = audioProtocolOptions.some((item) => item.value === audioProtocolId)
    ? audioProtocolId
    : ""
  const canSaveAudioProtocol = draft.api_format !== "audio_http_v1" || Boolean(selectedCatalogAudioProtocolId)

  return (
    <div className="overflow-hidden rounded-lg border border-indigo-700/60 bg-indigo-950/15">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-indigo-900/60 px-4 py-3">
        <div>
          <div className="text-sm font-semibold text-gray-100">{draft.name.trim() || `新建${kindLabel(entry.kind)}模型`}</div>
          <div className="mt-0.5 text-[11px] text-gray-500">常用字段在上方，高级协议参数默认收起。</div>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-gray-300">
            <input type="checkbox" checked={draft.is_active}
              onChange={(e) => setField("is_active", e.target.checked)} />
            默认
          </label>
          <label className="flex items-center gap-1.5 text-xs text-gray-300">
            <input type="checkbox" checked={draft.enabled}
              onChange={(e) => setField("enabled", e.target.checked)} />
            启用
          </label>
        </div>
      </div>
      <div className="grid gap-3 px-4 py-4 md:grid-cols-2">
        <F label="名称" required value={draft.name} onChange={(v) => setField("name", v)} />
        <F label="API Base URL" required value={draft.base_url} onChange={(v) => setField("base_url", v)}
          hint="填写带版本或 API 命名空间的接口基础地址，例如 /v1、/v2、/api/v3 或 /suno；不要只填裸域名，也不要填到 images、videos、files 等资源路径。后端原样使用，协议只追加资源路径。" />
        {entry.kind === "image" ? (
          <>
            <F
              label="模型名"
              required
              value={draft.model_name}
              onChange={(v) => setField("model_name", v)}
              hint="填写当前中转站或官方接口实际支持的图片模型 ID。"
            />
            <SelectField
              label="图片协议"
              value={selectedCatalogImageProtocolId}
              onChange={setImageProtocolId}
              options={[
                { label: imageProtocolOptions.length ? "请选择协议" : "未读取到协议配置", value: "" },
                ...imageProtocolOptions,
              ]}
              required
              hint="从 config/image_provider_protocols/catalog.json 动态读取；选择协议后系统按该协议构造请求和解析图片结果。"
            />
            {imageProtocolId && !selectedCatalogImageProtocolId && (
              <div className="col-span-2 rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-200">
                当前保存的协议 ID「{imageProtocolId}」不在配置文件中，请先在 catalog 中加入该协议，或改选已有协议。
              </div>
            )}
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
                    label="图片输入"
                    value={imageInputTransport}
                    onChange={(v) => setParamField("image_transport", v)}
                    options={VIDEO_IMAGE_TRANSPORT_OPTIONS}
                    hint="默认本地项目图转 Base64/data URL，已有公网 URL 原样传；公网 URL 模式需要服务商能直接访问图片地址。"
                  />
                  {imageInputTransport === "public_url" && (
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
        ) : entry.kind === "video" ? (
          <>
            <SelectField
              label="推荐模型"
              value={selectedVideoTemplate}
              onChange={applyVideoTemplate}
              options={[
                { label: videoModelTemplates.length ? "手动填写模型名" : "协议里没有模型建议", value: "" },
                ...videoModelTemplates.map((item) => ({
                  label: item.label,
                  value: item.value,
                })),
              ]}
              defaultText="可手填"
              hint="这些选项来自视频协议配置的 model_profiles；选择后只填模型名和协议 ID。"
            />
            <F
              label="模型名"
              required
              value={draft.model_name}
              onChange={(v) => setField("model_name", v)}
              hint="填写当前中转站或官方接口实际支持的模型 ID。"
            />
            {draft.api_format === "video_http_v1" && (
              <>
                <SelectField
                  label="视频协议"
                  value={selectedCatalogProtocolId}
                  onChange={setVideoProtocolId}
                  options={[
                    { label: videoProtocolOptions.length ? "请选择协议" : "未读取到协议配置", value: "" },
                    ...videoProtocolOptions,
                  ]}
                  required
                  hint="从 config/video_provider_protocols/catalog.json 动态读取；选择协议后系统按该协议构造请求、轮询和解析结果。"
                />
                {videoProtocolId && !selectedCatalogProtocolId && (
                  <div className="col-span-2 rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-200">
                    当前保存的协议 ID「{videoProtocolId}」不在配置文件中，请先在 catalog 中加入该协议，或改选已有协议。
                  </div>
                )}
                {additionalVideoBaseUrls.map((item) => (
                  <F
                    key={item.param}
                    label={item.label || item.param}
                    required={item.required}
                    value={String(draft.params?.[item.param] || "")}
                    onChange={(value) => setParamField(item.param, value)}
                    hint={item.hint || "该协议的这个操作使用独立的版本化 API Base URL。"}
                  />
                ))}
              </>
            )}
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
                    label="图片输入"
                    value={imageInputTransport}
                    onChange={(v) => setParamField("image_transport", v)}
                    options={VIDEO_IMAGE_TRANSPORT_OPTIONS}
                    hint="默认本地项目图转 Base64/data URL，已有公网 URL 原样传；公网 URL 模式需要服务商能直接访问图片地址。"
                  />
                  {imageInputTransport === "public_url" && (
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
            <F
              label="模型名"
              required
              value={draft.model_name}
              onChange={(v) => setField("model_name", v)}
              hint="填写当前中转站或官方接口实际支持的音频模型 ID，例如 tts-1、V5。"
            />
            <SelectField
              label="音频协议"
              value={selectedCatalogAudioProtocolId}
              onChange={setAudioProtocolId}
              options={[
                { label: audioProtocolOptions.length ? "请选择协议" : "未读取到协议配置", value: "" },
                ...audioProtocolOptions,
              ]}
              required
              hint="从 config/audio_provider_protocols/catalog.json 动态读取；选择协议后系统按该协议构造请求、轮询和解析音频结果。"
            />
            {audioProtocolId && !selectedCatalogAudioProtocolId && (
              <div className="col-span-2 rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-200">
                当前保存的协议 ID「{audioProtocolId}」不在配置文件中，请先在 catalog 中加入该协议，或改选已有协议。
              </div>
            )}
          </>
        ) : null}
        <F label="API Key" required value={draft.api_key ?? ""} type="password"
          onChange={(v) => setField("api_key", v || "")} />
        <F label="备注" value={draft.notes ?? ""} onChange={(v) => setField("notes", v || "")}
          defaultText="默认空" />
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-indigo-900/60 bg-gray-950/35 px-4 py-3">
        <button onClick={onCancel}
          className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">取消</button>
        <button
          onClick={() => onSave(normalizeMediaProvider(draft, imageProtocols, videoProtocols, audioProtocols))}
          disabled={
            !draft.name.trim()
            || !draft.base_url.trim()
            || !draft.model_name.trim()
            || !(draft.api_key ?? "").trim()
            || !canSaveImageProtocol
            || !canSaveVideoProtocol
            || !canSaveAudioProtocol
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
