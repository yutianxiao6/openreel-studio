"use client"

import { useState } from "react"
import type { ConfigContext, MediaProviderEntry, MediaProtocolSummary } from "../SettingsModal"

type MediaKind = "image" | "video" | "audio"

function normalizeMediaProvider(
  entry: MediaProviderEntry,
): MediaProviderEntry {
  return { ...entry, params: { ...(entry.params || {}) } }
}

function videoModelTemplateOptions(protocols: MediaProtocolSummary[]): Array<{
  label: string
  value: string
  modelName: string
  protocolId: string
  targetProfileId: string
}> {
  const options: Array<{ label: string; value: string; modelName: string; protocolId: string; targetProfileId: string }> = []
  const seen = new Set<string>()
  for (const protocol of protocols) {
    const protocolLabel = protocol.display_name || protocol.id
    const add = (modelName: string, label?: string, targetProfileId = "") => {
      const clean = modelName.trim()
      if (!clean || clean === "*") return
      const key = `${protocol.id}:${clean}`
      if (seen.has(key)) return
      seen.add(key)
      options.push({
        label: `${label?.trim() || clean} · ${protocolLabel}`,
        value: key,
        modelName: clean,
        protocolId: protocol.id,
        targetProfileId,
      })
    }
    protocol.model_profiles?.forEach((profile) => add(
      profile.match || "",
      profile.label,
      profile.target_profile_id,
    ))
    protocol.model_names?.forEach((modelName) => add(modelName))
  }
  return options
}

function mediaModelTemplateOptions(protocols: MediaProtocolSummary[]): Array<{
  label: string
  value: string
  modelName: string
  protocolId: string
  targetProfileId: string
  operation: string
}> {
  const options: Array<{
    label: string
    value: string
    modelName: string
    protocolId: string
    targetProfileId: string
    operation: string
  }> = []
  for (const protocol of protocols) {
    const protocolLabel = protocol.display_name || protocol.id
    for (const profile of protocol.model_profiles || []) {
      const modelName = String(profile.match || "").trim()
      const targetProfileId = String(profile.target_profile_id || "").trim()
      const operation = String(profile.operation || "").trim()
      if (!modelName || modelName === "*" || !targetProfileId || !operation) continue
      options.push({
        label: `${profile.label?.trim() || modelName} · ${protocolLabel}`,
        value: `${targetProfileId}:${modelName}`,
        modelName,
        protocolId: protocol.id,
        targetProfileId,
        operation,
      })
    }
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
              onCancel={() => setEditingKey(null)}
              onSave={(updated) => upsert(updated, p.name)}
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
              onCancel={() => setAdding(false)}
              onSave={(updated) => upsert(updated)}
              imageProtocols={ctx.imageProtocols}
              videoProtocols={ctx.videoProtocols}
              audioProtocols={ctx.audioProtocols}
            />
          ) : selectedItem && editingKey === selectedItem.name ? (
            <Row
              key={`edit-${selectedItem.name}`}
              entry={selectedItem}
              editing
              onCancel={() => setEditingKey(null)}
              onSave={(updated) => upsert(updated, selectedItem.name)}
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
  const uma = entry.params?.uma
  const umaProtocolId = uma && typeof uma === "object" && "protocol_id" in uma
    ? String(uma.protocol_id || "")
    : ""
  const umaBases = uma && typeof uma === "object" && "bases" in uma
    && uma.bases && typeof uma.bases === "object"
    ? uma.bases as Record<string, unknown>
    : null
  const protocolId = String(
    (
      entry.api_format === "universal_adapter" ? umaProtocolId : ""
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
        {entry.kind === "video" && umaBases
          && Object.entries(umaBases).map(([slot, value]) => (
            <SummaryField key={slot} label={`${slot} Base URL`} value={String(value)} mono />
          ))}
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
    api_format: "universal_adapter",
    is_active: false, enabled: true, notes: "", params: {},
  }
}

function Row({
  entry, editing, selected = false, onSelect, onCancel, onSave, imageProtocols, videoProtocols, audioProtocols,
}: {
  entry: MediaProviderEntry
  editing: boolean
  selected?: boolean
  onSelect?: () => void
  onCancel: () => void
  onSave: (e: MediaProviderEntry) => Promise<{ ok: boolean; errors: string[] }>
  imageProtocols: MediaProtocolSummary[]
  videoProtocols: MediaProtocolSummary[]
  audioProtocols: MediaProtocolSummary[]
}) {
  const [draft, setDraft] = useState(() => normalizeMediaProvider(entry))

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
  const setVideoBaseField = (slot: string, value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const uma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    const bases = uma.bases && typeof uma.bases === "object"
      ? { ...(uma.bases as Record<string, unknown>) }
      : {}
    const clean = value.trim()
    if (clean) bases[slot] = clean
    else delete bases[slot]
    uma.bases = bases
    nextParams.uma = uma
    setDraft({ ...draft, params: nextParams })
  }
  const setImageProtocolId = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    const clean = value.trim()
    if (clean) currentUma.protocol_id = clean
    else delete currentUma.protocol_id
    const selectedProtocol = imageProtocols.find((item) => item.id === clean)
    const matchedProfile = selectedProtocol?.model_profiles?.find(
      (item) => item.match === draft.model_name,
    ) || selectedProtocol?.model_profiles?.find((item) => item.match === "*")
    if (matchedProfile?.target_profile_id) {
      currentUma.target_profile_id = matchedProfile.target_profile_id
      currentUma.operation = matchedProfile.operation || "image.generate"
    } else {
      delete currentUma.target_profile_id
      delete currentUma.operation
    }
    nextParams.uma = currentUma
    setDraft({ ...draft, api_format: "universal_adapter", params: nextParams })
  }
  const setImageModelName = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    const protocol = imageProtocols.find(
      (item) => item.id === String(currentUma.protocol_id || ""),
    )
    const matchedProfile = protocol?.model_profiles?.find((item) => item.match === value)
      || protocol?.model_profiles?.find((item) => item.match === "*")
    if (matchedProfile?.target_profile_id) {
      currentUma.target_profile_id = matchedProfile.target_profile_id
      currentUma.operation = matchedProfile.operation || "image.generate"
    } else {
      delete currentUma.target_profile_id
      delete currentUma.operation
    }
    nextParams.uma = currentUma
    setDraft({ ...draft, model_name: value, params: nextParams })
  }
  const setVideoProtocolId = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    const clean = value.trim()
    if (clean) currentUma.protocol_id = clean
    else delete currentUma.protocol_id
    const selectedProtocol = videoProtocols.find((item) => item.id === clean)
    const matchedProfile = selectedProtocol?.model_profiles?.find(
      (item) => item.match === draft.model_name,
    ) || selectedProtocol?.model_profiles?.find((item) => item.match === "*")
    if (matchedProfile?.target_profile_id) {
      currentUma.target_profile_id = matchedProfile.target_profile_id
    } else {
      delete currentUma.target_profile_id
    }
    currentUma.operation = "video.generate"
    nextParams.uma = currentUma
    setDraft({ ...draft, api_format: "universal_adapter", params: nextParams })
  }
  const setVideoModelName = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    const protocol = videoProtocols.find(
      (item) => item.id === String(currentUma.protocol_id || ""),
    )
    const matchedProfile = protocol?.model_profiles?.find((item) => item.match === value)
      || protocol?.model_profiles?.find((item) => item.match === "*")
    if (matchedProfile?.target_profile_id) {
      currentUma.target_profile_id = matchedProfile.target_profile_id
    } else {
      delete currentUma.target_profile_id
    }
    nextParams.uma = currentUma
    setDraft({ ...draft, model_name: value, params: nextParams })
  }
  const setAudioProtocolId = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    const clean = value.trim()
    if (clean) currentUma.protocol_id = clean
    else delete currentUma.protocol_id
    const selectedProtocol = audioProtocols.find((item) => item.id === clean)
    const matchedProfile = selectedProtocol?.model_profiles?.find(
      (item) => item.match === draft.model_name,
    ) || selectedProtocol?.model_profiles?.find((item) => item.match === "*")
    if (matchedProfile?.target_profile_id) {
      currentUma.target_profile_id = matchedProfile.target_profile_id
      currentUma.operation = matchedProfile.operation || "audio.speech"
    } else {
      delete currentUma.target_profile_id
      delete currentUma.operation
    }
    nextParams.uma = currentUma
    setDraft({ ...draft, api_format: "universal_adapter", params: nextParams })
  }
  const setAudioModelName = (value: string) => {
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    const protocol = audioProtocols.find(
      (item) => item.id === String(currentUma.protocol_id || ""),
    )
    const matchedProfile = protocol?.model_profiles?.find((item) => item.match === value)
      || protocol?.model_profiles?.find((item) => item.match === "*")
    if (matchedProfile?.target_profile_id) {
      currentUma.target_profile_id = matchedProfile.target_profile_id
      currentUma.operation = matchedProfile.operation || "audio.speech"
    } else {
      delete currentUma.target_profile_id
      delete currentUma.operation
    }
    nextParams.uma = currentUma
    setDraft({ ...draft, model_name: value, params: nextParams })
  }

  const videoModelTemplates = videoModelTemplateOptions(videoProtocols)
  const applyVideoTemplate = (templateKey: string) => {
    const template = videoModelTemplates.find((item) => item.value === templateKey)
    if (!template) return
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    nextParams.uma = {
      ...currentUma,
      protocol_id: template.protocolId,
      operation: "video.generate",
      target_profile_id: template.targetProfileId,
    }
    setDraft({
      ...draft,
      model_name: template.modelName,
      api_format: "universal_adapter",
      params: nextParams,
    })
  }
  const draftUma = draft.params?.uma && typeof draft.params.uma === "object"
    ? draft.params.uma as Record<string, unknown>
    : {}
  const selectedVideoTemplate = entry.kind === "video"
    ? videoModelTemplates.find((item) =>
      item.modelName === draft.model_name
      && item.protocolId === String(draftUma.protocol_id || "")
      && (!item.targetProfileId || item.targetProfileId === String(draftUma.target_profile_id || "")),
    )?.value || ""
    : ""
  const imageModelTemplates = mediaModelTemplateOptions(imageProtocols)
  const selectedImageTemplate = entry.kind === "image"
    ? imageModelTemplates.find((item) =>
      item.modelName === draft.model_name
      && item.protocolId === String(draftUma.protocol_id || "")
      && item.targetProfileId === String(draftUma.target_profile_id || ""),
    )?.value || ""
    : ""
  const applyImageTemplate = (templateKey: string) => {
    const template = imageModelTemplates.find((item) => item.value === templateKey)
    if (!template) return
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    nextParams.uma = {
      ...currentUma,
      protocol_id: template.protocolId,
      operation: template.operation,
      target_profile_id: template.targetProfileId,
    }
    setDraft({
      ...draft,
      model_name: template.modelName,
      api_format: "universal_adapter",
      params: nextParams,
    })
  }
  const audioModelTemplates = mediaModelTemplateOptions(audioProtocols)
  const selectedAudioTemplate = entry.kind === "audio"
    ? audioModelTemplates.find((item) =>
      item.modelName === draft.model_name
      && item.protocolId === String(draftUma.protocol_id || "")
      && item.targetProfileId === String(draftUma.target_profile_id || ""),
    )?.value || ""
    : ""
  const applyAudioTemplate = (templateKey: string) => {
    const template = audioModelTemplates.find((item) => item.value === templateKey)
    if (!template) return
    const nextParams = { ...(draft.params || {}) }
    const currentUma = nextParams.uma && typeof nextParams.uma === "object"
      ? { ...(nextParams.uma as Record<string, unknown>) }
      : {}
    nextParams.uma = {
      ...currentUma,
      protocol_id: template.protocolId,
      operation: template.operation,
      target_profile_id: template.targetProfileId,
    }
    setDraft({
      ...draft,
      model_name: template.modelName,
      api_format: "universal_adapter",
      params: nextParams,
    })
  }
  const imageProtocolId = String(draftUma.protocol_id || "")
  const imageProtocolOptions = imageProtocols.map((item) => ({
    label: item.display_name && item.display_name !== item.id
      ? `${item.display_name} · ${item.id}`
      : item.id,
    value: item.id,
  }))
  const selectedCatalogImageProtocolId = imageProtocolOptions.some((item) => item.value === imageProtocolId)
    ? imageProtocolId
    : ""
  const canSaveImageProtocol = draft.kind !== "image"
    || (
      draft.api_format === "universal_adapter"
      && Boolean(selectedCatalogImageProtocolId)
      && Boolean(String(draftUma.target_profile_id || "").trim())
      && draftUma.operation === "image.generate"
    )
  const videoProtocolId = String(draftUma.protocol_id || "")
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
  const videoBases = draftUma.bases && typeof draftUma.bases === "object"
    ? draftUma.bases as Record<string, unknown>
    : {}
  const hasRequiredVideoBaseUrls = additionalVideoBaseUrls.every((item) =>
    !item.required || Boolean(String(videoBases[item.slot || item.param] || "").trim()),
  )
  const canSaveVideoProtocol = draft.kind !== "video"
    || (
      Boolean(selectedCatalogProtocolId)
      && Boolean(String(draftUma.target_profile_id || "").trim())
      && hasRequiredVideoBaseUrls
    )
  const audioProtocolId = String(draftUma.protocol_id || "")
  const audioProtocolOptions = audioProtocols.map((item) => ({
    label: item.display_name && item.display_name !== item.id
      ? `${item.display_name} · ${item.id}`
      : item.id,
    value: item.id,
  }))
  const selectedCatalogAudioProtocolId = audioProtocolOptions.some((item) => item.value === audioProtocolId)
    ? audioProtocolId
    : ""
  const canSaveAudioProtocol = draft.kind !== "audio"
    || (
      draft.api_format === "universal_adapter"
      && Boolean(selectedCatalogAudioProtocolId)
      && Boolean(String(draftUma.target_profile_id || "").trim())
      && String(draftUma.operation || "").startsWith("audio.")
    )

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
            <SelectField
              label="推荐模型"
              value={selectedImageTemplate}
              onChange={applyImageTemplate}
              options={[
                { label: imageModelTemplates.length ? "手动填写模型名" : "target 配置里没有具体模型建议", value: "" },
                ...imageModelTemplates.map((item) => ({
                  label: item.label,
                  value: item.value,
                })),
              ]}
              defaultText="可手填"
              hint="选项来自独立的图片 target 配置；选择后会同时绑定模型名、UMA 协议、operation 和 target profile。"
            />
            <F
              label="模型名"
              required
              value={draft.model_name}
              onChange={setImageModelName}
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
              hint="模型能力来自独立 target 配置；请求拼装、参考图编码和图片结果读取由 Universal Model Adapter V2 协议执行。"
            />
            {imageProtocolId && !selectedCatalogImageProtocolId && (
              <div className="col-span-2 rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-200">
                当前保存的协议 ID「{imageProtocolId}」不在 UMA 图片 target 配置中，请改选已有协议。
              </div>
            )}
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
              hint="这些选项来自独立的视频 target 配置；选择后会同时绑定模型名、协议 ID 和 target profile。"
            />
            <F
              label="模型名"
              required
              value={draft.model_name}
              onChange={setVideoModelName}
              hint="填写当前中转站或官方接口实际支持的模型 ID。"
            />
            <SelectField
                  label="视频协议"
                  value={selectedCatalogProtocolId}
                  onChange={setVideoProtocolId}
                  options={[
                    { label: videoProtocolOptions.length ? "请选择协议" : "未读取到协议配置", value: "" },
                    ...videoProtocolOptions,
                  ]}
                  required
                  hint="模型能力来自独立 target 配置；请求、轮询和结果读取由 Universal Model Adapter V2 协议执行。"
                />
            {videoProtocolId && !selectedCatalogProtocolId && (
              <div className="col-span-2 rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-200">
                当前保存的协议 ID「{videoProtocolId}」不在 UMA V2 协议目录中，请改选已有协议。
              </div>
            )}
            {additionalVideoBaseUrls.map((item) => (
              <F
                key={item.param}
                label={item.label || item.param}
                required={item.required}
                value={String(videoBases[item.slot || item.param] || "")}
                onChange={(value) => setVideoBaseField(item.slot || item.param, value)}
                hint={item.hint || "该协议的这个操作使用独立的版本化 API Base URL。"}
              />
            ))}
          </>
        ) : entry.kind === "audio" ? (
          <>
            <SelectField
              label="推荐模型"
              value={selectedAudioTemplate}
              onChange={applyAudioTemplate}
              options={[
                { label: audioModelTemplates.length ? "手动填写模型名" : "target 配置里没有具体模型建议", value: "" },
                ...audioModelTemplates.map((item) => ({
                  label: item.label,
                  value: item.value,
                })),
              ]}
              defaultText="可手填"
              hint="选项来自独立的音频 target 配置；选择后会同时绑定模型名、UMA 协议、operation 和 target profile。"
            />
            <F
              label="模型名"
              required
              value={draft.model_name}
              onChange={setAudioModelName}
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
              hint="模型能力来自独立 target 配置；请求、轮询和音频结果读取由 Universal Model Adapter V2 协议执行。"
            />
            {audioProtocolId && !selectedCatalogAudioProtocolId && (
              <div className="col-span-2 rounded border border-amber-800 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-200">
                当前保存的协议 ID「{audioProtocolId}」不在 UMA 音频 target 配置中，请改选已有协议。
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
          onClick={() => onSave(normalizeMediaProvider(draft))}
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
