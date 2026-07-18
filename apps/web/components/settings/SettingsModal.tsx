"use client"

import { useEffect, useState } from "react"
import { getAudioProviderProtocols, getImageProviderProtocols, getRuntimeConfigFile, getVideoProviderProtocols, isOpenReelDesktop, patchRuntimeConfig } from "@/lib/api"
import { LlmTab } from "./tabs/LlmTab"
import { MediaTab } from "./tabs/MediaTab"
import { AgentTab } from "./tabs/AgentTab"
import { AgentDebugTab } from "./tabs/AgentDebugTab"
import { RawFileTab } from "./tabs/RawFileTab"
import { DownloadTab } from "./tabs/DownloadTab"

export interface RuntimeConfig {
  $schema_version: number
  llm_providers: LlmProviderEntry[]
  media_providers: MediaProviderEntry[]
  model_tier_defaults: Record<ModelTier, string | null>
  model_assignments: Record<string, string | null>
  app_settings: Record<string, unknown>
}

export type ModelTier = "strong" | "balanced" | "small"

export interface LlmProviderEntry {
  name: string
  provider: string
  model_name: string
  base_url: string | null
  api_key: string | null
  context_window_tokens?: number | null
  max_input_tokens?: number | null
  max_output_tokens?: number | null
  supports_prompt_cache?: boolean | null
  supports_vision?: boolean | null
  tokenizer?: string | null
  tier?: ModelTier
  enabled: boolean
  notes?: string | null
  params?: Record<string, unknown>
}

export interface MediaProviderEntry {
  kind: "image" | "video" | "audio"
  name: string
  base_url: string
  api_key: string | null
  model_name: string
  api_format: string
  is_active: boolean
  enabled: boolean
  notes?: string | null
  params: Record<string, unknown>
}

export interface MediaProtocolSummary {
  id: string
  display_name?: string
  additional_base_urls?: Array<{
    param: string
    label?: string
    hint?: string
    section?: string
    required?: boolean
  }>
  model_names?: string[]
  model_profiles?: Array<{
    match?: string
    label?: string
    supported_ratios?: string[]
    supported_resolutions?: string[]
    default_ratio?: string
    default_resolution?: string
    modes?: Record<string, unknown> | string[]
    supported_modes?: string[]
  }>
  supported_ratios?: string[]
  supported_resolutions?: string[]
  supported_sizes?: string[]
  result_type?: string
}

export type TabKey = "llm" | "image" | "video" | "audio" | "agent" | "download" | "debug" | "raw"

export interface ConfigContext {
  config: RuntimeConfig
  imageProtocols: MediaProtocolSummary[]
  videoProtocols: MediaProtocolSummary[]
  audioProtocols: MediaProtocolSummary[]
  reload: () => Promise<void>
  applyPatch: (patch: Record<string, unknown>) => Promise<{ ok: boolean; errors: string[] }>
}

interface Props {
  open: boolean
  onClose: () => void
}

export function SettingsModal({ open, onClose }: Props) {
  const [tab, setTab] = useState<TabKey>("llm")
  const [config, setConfig] = useState<RuntimeConfig | null>(null)
  const [imageProtocols, setImageProtocols] = useState<MediaProtocolSummary[]>([])
  const [videoProtocols, setVideoProtocols] = useState<MediaProtocolSummary[]>([])
  const [audioProtocols, setAudioProtocols] = useState<MediaProtocolSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const [r, imageProtocolResult, videoProtocolResult, audioProtocolResult] = await Promise.all([
        getRuntimeConfigFile<{
          raw_text: string
          parsed: RuntimeConfig
          valid: boolean
          errors: string[]
        }>(false),
        getImageProviderProtocols<{
          ok: boolean
          protocols: MediaProtocolSummary[]
        }>().catch(() => null),
        getVideoProviderProtocols<{
          ok: boolean
          protocols: MediaProtocolSummary[]
        }>().catch(() => null),
        getAudioProviderProtocols<{
          ok: boolean
          protocols: MediaProtocolSummary[]
        }>().catch(() => null),
      ])
      setConfig(r.parsed)
      setImageProtocols(imageProtocolResult?.protocols ?? [])
      setVideoProtocols(videoProtocolResult?.protocols ?? [])
      setAudioProtocols(audioProtocolResult?.protocols ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  const applyPatch = async (patch: Record<string, unknown>) => {
    try {
      const r = await patchRuntimeConfig(patch)
      if (r.ok) {
        setToast("已应用")
        setTimeout(() => setToast(null), 1500)
        await refresh()
        window.dispatchEvent(new CustomEvent("drama:runtime-config-updated", { detail: patch }))
      }
      return r
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      return { ok: false, errors: [msg] }
    }
  }

  useEffect(() => {
    if (!open) return
    refresh()
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onClose])

  if (!open) return null

  const ctx: ConfigContext | null = config
    ? { config, imageProtocols, videoProtocols, audioProtocols, reload: refresh, applyPatch }
    : null

  const imageCount = config?.media_providers.filter((p) => p.kind === "image").length ?? 0
  const videoCount = config?.media_providers.filter((p) => p.kind === "video").length ?? 0
  const audioCount = config?.media_providers.filter((p) => p.kind === "audio").length ?? 0
  const desktop = isOpenReelDesktop()
  const tabs: Array<{ key: TabKey; label: string; count?: number }> = [
    { key: "llm", label: "LLM 模型", count: config?.llm_providers.length },
    { key: "image", label: "图片 Provider", count: imageCount },
    { key: "video", label: "视频 Provider", count: videoCount },
    { key: "audio", label: "音频 Provider", count: audioCount },
    { key: "agent", label: "运行偏好" },
    ...(desktop ? [{ key: "download" as const, label: "下载位置" }] : []),
    { key: "debug", label: "Agent 诊断" },
    { key: "raw", label: "配置文件" },
  ]

  return (
    <div
      className="studio-settings-backdrop fixed inset-0 z-[70] flex items-center justify-center"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="studio-settings-title"
        className="studio-settings-dialog flex max-h-[calc(100dvh-16px)] w-[calc(100vw-16px)] flex-col border sm:max-h-[90vh] sm:w-[min(1100px,94vw)]"
      >
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-white/[0.08] bg-white/[0.015] px-3 py-3.5 sm:px-5">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-violet-300/20 bg-gradient-to-br from-violet-400/15 to-cyan-300/[0.06] text-[10px] font-bold tracking-tight text-violet-100 shadow-inner shadow-white/[0.04]">SET</span>
            <div>
              <h2 id="studio-settings-title" className="shrink-0 bg-gradient-to-r from-white to-violet-200 bg-clip-text text-sm font-semibold text-transparent">系统设置</h2>
              <span className="hidden truncate text-[10px] text-zinc-600 sm:block">runtime.jsonc · 修改后即时生效</span>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {toast && <span className="text-[11px] text-emerald-300">{toast}</span>}
            <button
              onClick={refresh}
              disabled={loading}
              className="h-8 rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 text-[10px] text-zinc-400 transition hover:border-violet-300/20 hover:bg-violet-400/[0.08] hover:text-violet-100 disabled:opacity-50"
            >
              {loading ? "刷新中…" : "刷新"}
            </button>
            <button
              onClick={onClose}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-transparent text-sm text-zinc-500 transition hover:rotate-90 hover:border-white/10 hover:bg-white/[0.06] hover:text-white"
              title="关闭 (Esc)"
              aria-label="关闭设置"
            >
              X
            </button>
          </div>
        </header>

        <div className="flex shrink-0 overflow-x-auto border-b border-white/[0.07] bg-black/10 px-2 sm:px-3">
          {tabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              aria-pressed={tab === t.key}
              className={`relative -mb-px shrink-0 border-b-2 px-3 py-3 text-[11px] font-medium transition-all sm:px-4 ${
                tab === t.key
                  ? "border-violet-400 text-violet-200 bg-gradient-to-t from-violet-400/[0.08] to-transparent"
                  : "border-transparent text-zinc-500 hover:bg-white/[0.025] hover:text-zinc-200"
              }`}
            >
              {t.label}
              {typeof t.count === "number" && (
                <span className="ml-1.5 text-[10px] text-gray-500">({t.count})</span>
              )}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto bg-[radial-gradient(circle_at_100%_0,rgba(85,215,255,.035),transparent_24%)] px-3 py-3 sm:px-5 sm:py-4">
          {error && (
            <div className="rounded border border-red-800 bg-red-950/40 text-red-200 text-xs p-3 mb-3">
              {error}
            </div>
          )}
          {loading && !ctx && (
            <div className="text-center text-gray-500 text-sm py-12">加载中…</div>
          )}
          {ctx && tab === "llm" && <LlmTab ctx={ctx} />}
          {ctx && tab === "image" && <MediaTab ctx={ctx} kind="image" />}
          {ctx && tab === "video" && <MediaTab ctx={ctx} kind="video" />}
          {ctx && tab === "audio" && <MediaTab ctx={ctx} kind="audio" />}
          {ctx && tab === "agent" && <AgentTab ctx={ctx} />}
          {desktop && tab === "download" && <DownloadTab />}
          {tab === "debug" && <AgentDebugTab />}
          {ctx && tab === "raw" && <RawFileTab onSaved={refresh} />}
        </div>
      </div>
    </div>
  )
}
