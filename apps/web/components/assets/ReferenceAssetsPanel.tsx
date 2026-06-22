"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  callTool,
  listProjectAssets,
  resolveAssetLibraryPreviewUrl,
  resolveMediaUrl,
  type ProjectAsset,
} from "@/lib/api"

interface ReferenceAsset {
  ref_id: string
  mention?: string
  label?: string
  filename?: string
  source?: string
  rel_path?: string
  reference_input?: string
  source_path?: string
  url?: string
  asset_id?: string
  node_id?: string
  status?: string
  roles?: string[]
  analysis?: Record<string, unknown>
  analysis_summary?: {
    summary?: string
    style_name?: string
    style_tags?: string[]
    prompt_fragment?: string
  }
}

interface ReferenceManageResult {
  ok?: boolean
  error?: string
  assets?: ReferenceAsset[]
  asset?: ReferenceAsset
  bindings?: Array<Record<string, unknown>>
}

interface AssetLibraryListResult {
  items?: Array<{
    path?: string
    kind?: string
    episode?: string
    category?: string
    size?: number
  }>
  error?: string
}

type SourceKind = "generated" | "project_library" | "shared_library"

interface SourceItem {
  key: string
  source: SourceKind
  title: string
  subtitle: string
  previewUrl: string
  assetId?: string
  sourcePath?: string
}

interface Props {
  open: boolean
  projectId?: string | null
  onClose: () => void
  onInsertReference?: (text: string) => void
}

const IMAGE_SUFFIX_RE = /\.(png|jpe?g|webp|gif|bmp)$/i

function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path
}

function stripExt(name: string): string {
  return name.replace(/\.[^.]+$/, "")
}

function compactText(value: unknown, fallback = ""): string {
  return String(value ?? fallback).trim()
}

function analysisOf(ref: ReferenceAsset): Record<string, unknown> {
  if (ref.analysis && typeof ref.analysis === "object") return ref.analysis
  if (ref.analysis_summary && typeof ref.analysis_summary === "object") return ref.analysis_summary as Record<string, unknown>
  return {}
}

function previewForReference(projectId: string, ref: ReferenceAsset): string {
  if (ref.url) return resolveMediaUrl(ref.url)
  if (ref.ref_id) return resolveMediaUrl(`/api/uploads/${projectId}/reference/${ref.ref_id}`)
  const rel = ref.reference_input || ref.rel_path || ""
  if (rel.startsWith("uploads/")) return resolveMediaUrl(`/api/uploads/${projectId}/file/${rel}`)
  if (rel.startsWith("generated_images/")) return resolveMediaUrl(`/api/media/${projectId}/${rel.replace(/^generated_images\//, "")}`)
  return ""
}

function generatedPreview(asset: ProjectAsset): string {
  if (asset.url) return resolveMediaUrl(asset.url)
  const path = asset.path || ""
  const marker = "/generated_images/"
  const index = path.indexOf(marker)
  if (index >= 0 && asset.project_id) return resolveMediaUrl(`/api/media/${asset.project_id}/${path.slice(index + marker.length)}`)
  return ""
}

function isImageAsset(asset: ProjectAsset): boolean {
  const type = String(asset.type || "").toLowerCase()
  const mime = String(asset.mime_type || "").toLowerCase()
  const path = `${asset.path || ""} ${asset.url || ""}`.toLowerCase()
  return type.includes("image") || mime.startsWith("image/") || IMAGE_SUFFIX_RE.test(path)
}

function sourceLabel(source: SourceKind): string {
  if (source === "generated") return "生成资产"
  if (source === "project_library") return "项目资产库"
  return "共享资产库"
}

function statusLabel(status?: string): string {
  if (status === "analyzed") return "已分析"
  if (status === "analysis_failed") return "分析失败"
  if (status === "pending_analysis") return "待分析"
  return status || "参考图"
}

export function ReferenceAssetsPanel({ open, projectId, onClose, onInsertReference }: Props) {
  const [references, setReferences] = useState<ReferenceAsset[]>([])
  const [sources, setSources] = useState<SourceItem[]>([])
  const [selectedRefId, setSelectedRefId] = useState<string | null>(null)
  const [query, setQuery] = useState("")
  const [sourceQuery, setSourceQuery] = useState("")
  const [aliasDraft, setAliasDraft] = useState("")
  const [loading, setLoading] = useState(false)
  const [sourceLoading, setSourceLoading] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const selected = useMemo(
    () => references.find((item) => item.ref_id === selectedRefId) ?? references[0] ?? null,
    [references, selectedRefId],
  )

  const loadReferences = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    try {
      const result = await callTool<ReferenceManageResult>("reference.manage", {
        project_id: projectId,
        action: "list",
        include_analysis: true,
      })
      if (result.ok === false) throw new Error(result.error || "读取参考图失败")
      const next = result.assets ?? []
      setReferences(next)
      setSelectedRefId((current) => current && next.some((item) => item.ref_id === current) ? current : next[0]?.ref_id ?? null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  const loadSources = useCallback(async () => {
    if (!projectId) return
    setSourceLoading(true)
    try {
      const next: SourceItem[] = []
      const generated = await listProjectAssets(projectId)
      generated.assets.filter(isImageAsset).slice(0, 100).forEach((asset, index) => {
        const title = asset.name || asset.type || `生成图 ${index + 1}`
        next.push({
          key: `asset:${asset.id}`,
          source: "generated",
          title,
          subtitle: asset.type || "图片",
          previewUrl: generatedPreview(asset),
          assetId: asset.id,
        })
      })
      const [projectLibrary, sharedLibrary] = await Promise.all([
        callTool<AssetLibraryListResult>("assets.list_project", { project_id: projectId }),
        callTool<AssetLibraryListResult>("assets.list_shared", { project_id: projectId }),
      ])
      ;[
        { source: "project_library" as const, result: projectLibrary },
        { source: "shared_library" as const, result: sharedLibrary },
      ].forEach(({ source, result }) => {
        if (result?.error) return
        ;(result?.items ?? [])
          .filter((item) => item.path && IMAGE_SUFFIX_RE.test(item.path))
          .slice(0, 100)
          .forEach((item) => {
            const path = String(item.path || "")
            next.push({
              key: `${source}:${path}`,
              source,
              title: basename(path),
              subtitle: compactText(item.category || item.episode || item.kind, "图片"),
              previewUrl: resolveAssetLibraryPreviewUrl(projectId, path),
              sourcePath: path,
            })
          })
      })
      setSources(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSourceLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (!open || !projectId) return
    void loadReferences()
    void loadSources()
  }, [open, projectId, loadReferences, loadSources])

  useEffect(() => {
    setAliasDraft(selected?.mention || "")
  }, [selected?.ref_id, selected?.mention])

  const filteredReferences = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return references
    return references.filter((item) => {
      const analysis = analysisOf(item)
      return [
        item.mention,
        item.label,
        item.filename,
        item.source,
        item.status,
        analysis.summary,
        analysis.style_name,
        analysis.prompt_fragment,
      ].some((value) => String(value || "").toLowerCase().includes(q))
    })
  }, [references, query])

  const filteredSources = useMemo(() => {
    const q = sourceQuery.trim().toLowerCase()
    if (!q) return sources
    return sources.filter((item) => `${item.title} ${item.subtitle} ${item.sourcePath || ""}`.toLowerCase().includes(q))
  }, [sources, sourceQuery])

  const runReferenceAction = async (action: string, extra: Record<string, unknown> = {}) => {
    if (!projectId || !selected) return
    setBusy(action)
    setError(null)
    try {
      const result = await callTool<ReferenceManageResult>("reference.manage", {
        project_id: projectId,
        action,
        ref_id: selected.ref_id,
        include_analysis: true,
        ...extra,
      })
      if (result.ok === false) throw new Error(result.error || "操作失败")
      await loadReferences()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const registerSource = async (item: SourceItem) => {
    if (!projectId) return
    setBusy(item.key)
    setError(null)
    try {
      const defaultMention = `@${stripExt(item.title).slice(0, 18) || "参考图"}`
      const payload: Record<string, unknown> = {
        project_id: projectId,
        mention: defaultMention,
        include_analysis: true,
      }
      if (item.assetId) {
        payload.action = "register_asset"
        payload.asset_id = item.assetId
      } else {
        payload.action = "register_file"
        payload.source_path = item.sourcePath
      }
      const result = await callTool<ReferenceManageResult>("reference.manage", payload)
      if (result.ok === false) throw new Error(result.error || "登记参考图失败")
      await loadReferences()
      if (result.asset?.ref_id) setSelectedRefId(result.asset.ref_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const renameSelected = async () => {
    if (!aliasDraft.trim()) return
    await runReferenceAction("alias", { alias: aliasDraft.trim() })
  }

  const bindSelected = async () => {
    await runReferenceAction("bind_to_blueprint", {
      role: "style_reference",
      apply_to: ["character", "scene", "segment_storyboard", "segment_video_prompt"],
    })
  }

  const analyzeSelected = async () => {
    await runReferenceAction("analyze", { force: true })
  }

  const saveMemory = async () => {
    await runReferenceAction("save_to_user_memory", { save_user_memory: true })
  }

  const insertSelected = () => {
    if (!selected) return
    const mention = selected.mention || selected.label || "@图"
    onInsertReference?.(`请使用 ${mention} 作为参考图。`)
    onClose()
  }

  if (!open) return null

  const selectedAnalysis = selected ? analysisOf(selected) : {}
  const selectedPreview = selected && projectId ? previewForReference(projectId, selected) : ""

  return (
    <div className="fixed inset-0 z-[90] bg-black/62 backdrop-blur-sm" onClick={onClose}>
      <div
        className="absolute inset-2 flex overflow-hidden rounded-lg border border-white/10 bg-[var(--studio-panel)] shadow-2xl shadow-black/60 sm:left-1/2 sm:top-1/2 sm:h-[min(760px,calc(100dvh-48px))] sm:w-[min(1080px,calc(100vw-32px))] sm:-translate-x-1/2 sm:-translate-y-1/2"
        onClick={(event) => event.stopPropagation()}
      >
        <aside className="flex w-[132px] shrink-0 flex-col border-r border-white/10 bg-black/18 sm:w-[320px]">
          <div className="border-b border-white/10 px-2 py-3 sm:px-4">
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="text-sm font-semibold text-zinc-100">参考图</div>
                <div className="mt-0.5 text-[11px] text-zinc-500">{references.length} 张</div>
              </div>
              <button
                type="button"
                onClick={() => void loadReferences()}
                className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]"
              >
                刷新
              </button>
            </div>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索参考图"
              className="mt-3 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-200/60"
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-1.5 sm:p-2">
            {loading ? (
              <div className="flex h-32 items-center justify-center text-xs text-zinc-500">正在读取…</div>
            ) : filteredReferences.length === 0 ? (
              <div className="rounded-md border border-white/10 bg-white/[0.03] px-3 py-3 text-xs text-zinc-500">暂无参考图</div>
            ) : (
              <div className="space-y-1.5">
                {filteredReferences.map((ref) => {
                  const preview = projectId ? previewForReference(projectId, ref) : ""
                  const active = selected?.ref_id === ref.ref_id
                  const analysis = analysisOf(ref)
                  return (
                    <button
                      key={ref.ref_id}
                      type="button"
                      onClick={() => setSelectedRefId(ref.ref_id)}
                      className={
                        "flex w-full items-center gap-2 rounded-md border px-1.5 py-2 text-left transition-colors sm:px-2 " +
                        (active
                          ? "border-zinc-200/60 bg-white/[0.09]"
                          : "border-white/10 bg-white/[0.03] hover:bg-white/[0.06]")
                      }
                    >
                      <span className="h-10 w-10 shrink-0 overflow-hidden rounded-md border border-white/10 bg-black/25 sm:h-12 sm:w-12">
                        {preview ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={preview} alt={ref.filename || ref.mention || "参考图"} className="h-full w-full object-cover" />
                        ) : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-xs font-medium text-zinc-200">{ref.mention || ref.label || ref.filename || "参考图"}</span>
                        <span className="mt-0.5 block truncate text-[10px] text-zinc-500">
                          {compactText(analysis.style_name || ref.filename || ref.source, statusLabel(ref.status))}
                        </span>
                      </span>
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center justify-between gap-2 border-b border-white/10 px-3 py-3 sm:px-4">
            <div>
              <div className="text-sm font-semibold text-zinc-100">参考图管理</div>
              <div className="mt-0.5 text-[11px] text-zinc-500">OpenReel reference assets</div>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/[0.06]"
            >
              关闭
            </button>
          </div>

          <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px]">
            <section className="min-w-0 overflow-y-auto px-3 py-3 sm:px-4 sm:py-4">
              {!selected ? (
                <div className="flex h-full items-center justify-center text-sm text-zinc-500">选择一张参考图</div>
              ) : (
                <div className="space-y-4">
                  <div className="overflow-hidden rounded-lg border border-white/10 bg-black/20">
                    <div className="aspect-video bg-black/30">
                      {selectedPreview ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={selectedPreview} alt={selected.filename || selected.mention || "参考图"} className="h-full w-full object-contain" />
                      ) : (
                        <div className="flex h-full items-center justify-center text-xs text-zinc-600">无预览</div>
                      )}
                    </div>
                    <div className="border-t border-white/10 px-3 py-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="rounded border border-white/10 bg-white/[0.04] px-2 py-1 font-mono text-[11px] text-zinc-200">
                          {selected.mention || selected.label || "@图"}
                        </span>
                        <span className="text-xs text-zinc-500">{statusLabel(selected.status)}</span>
                      </div>
                    </div>
                  </div>

                  <div className="rounded-lg border border-white/10 bg-white/[0.03] px-3 py-3">
                    <div className="text-xs font-medium text-zinc-300">别名</div>
                    <div className="mt-2 flex gap-2">
                      <input
                        value={aliasDraft}
                        onChange={(event) => setAliasDraft(event.target.value)}
                        placeholder="@角色风格"
                        className="h-9 flex-1 rounded-md border border-white/10 bg-[var(--studio-control)] px-3 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-200/60"
                      />
                      <button
                        type="button"
                        onClick={() => void renameSelected()}
                        disabled={Boolean(busy) || !aliasDraft.trim()}
                        className="rounded-md bg-zinc-100 px-3 text-xs font-medium text-zinc-950 hover:bg-white disabled:opacity-40"
                      >
                        保存
                      </button>
                    </div>
                  </div>

                  <div className="grid gap-3 md:grid-cols-2">
                    {([
                      ["文件", selected.filename || selected.reference_input || selected.rel_path || selected.source_path],
                      ["来源", selected.source || selected.asset_id || selected.node_id],
                      ["风格", selectedAnalysis.style_name],
                      ["摘要", selectedAnalysis.summary],
                      ["提示词片段", selectedAnalysis.prompt_fragment],
                      ["避免偏差", Array.isArray(selectedAnalysis.negative_constraints) ? selectedAnalysis.negative_constraints.join("、") : selectedAnalysis.negative_constraints],
                    ] as Array<[string, unknown]>).map(([label, value]) => (
                      <div key={String(label)} className="rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2">
                        <div className="text-[10px] uppercase tracking-[0.14em] text-zinc-600">{label}</div>
                        <div className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-zinc-300">{compactText(value, "无")}</div>
                      </div>
                    ))}
                  </div>

                  {error ? (
                    <div className="rounded-md border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{error}</div>
                  ) : null}

                  <div className="flex flex-wrap gap-2">
                    <button type="button" onClick={insertSelected} className="rounded-md border border-white/10 bg-white/[0.05] px-3 py-2 text-xs text-zinc-200 hover:bg-white/[0.09]">
                      插入引用
                    </button>
                    <button type="button" onClick={() => void analyzeSelected()} disabled={Boolean(busy)} className="rounded-md border border-white/10 bg-white/[0.05] px-3 py-2 text-xs text-zinc-200 hover:bg-white/[0.09] disabled:opacity-40">
                      {busy === "analyze" ? "分析中" : "重新分析"}
                    </button>
                    <button type="button" onClick={() => void bindSelected()} disabled={Boolean(busy)} className="rounded-md border border-white/10 bg-white/[0.05] px-3 py-2 text-xs text-zinc-200 hover:bg-white/[0.09] disabled:opacity-40">
                      绑定蓝图
                    </button>
                    <button type="button" onClick={() => void saveMemory()} disabled={Boolean(busy)} className="rounded-md border border-white/10 bg-white/[0.05] px-3 py-2 text-xs text-zinc-200 hover:bg-white/[0.09] disabled:opacity-40">
                      记住风格
                    </button>
                  </div>
                </div>
              )}
            </section>

            <aside className="flex min-h-0 flex-col border-l border-white/10 bg-black/14">
              <div className="border-b border-white/10 px-3 py-3">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-xs font-semibold text-zinc-200">登记图片</div>
                    <div className="mt-0.5 text-[10px] text-zinc-600">{sources.length} 项</div>
                  </div>
                  <button
                    type="button"
                    onClick={() => void loadSources()}
                    className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-zinc-400 hover:bg-white/[0.06]"
                  >
                    刷新
                  </button>
                </div>
                <input
                  value={sourceQuery}
                  onChange={(event) => setSourceQuery(event.target.value)}
                  placeholder="搜索资产"
                  className="mt-2 h-8 w-full rounded-md border border-white/10 bg-[var(--studio-control)] px-2.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-zinc-200/60"
                />
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-2">
                {sourceLoading ? (
                  <div className="flex h-28 items-center justify-center text-xs text-zinc-500">正在读取…</div>
                ) : filteredSources.length === 0 ? (
                  <div className="rounded-md border border-white/10 bg-white/[0.03] px-3 py-3 text-xs text-zinc-500">没有图片资产</div>
                ) : (
                  <div className="space-y-2">
                    {filteredSources.map((item) => (
                      <div key={item.key} className="overflow-hidden rounded-md border border-white/10 bg-white/[0.035]">
                        <div className="aspect-[4/3] bg-black/25">
                          {item.previewUrl ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img src={item.previewUrl} alt={item.title} className="h-full w-full object-cover" />
                          ) : null}
                        </div>
                        <div className="px-2 py-2">
                          <div className="truncate text-xs font-medium text-zinc-200">{item.title}</div>
                          <div className="mt-0.5 truncate text-[10px] text-zinc-500">{sourceLabel(item.source)} · {item.subtitle}</div>
                          <button
                            type="button"
                            onClick={() => void registerSource(item)}
                            disabled={Boolean(busy)}
                            className="mt-2 w-full rounded-md border border-white/10 bg-white/[0.05] px-2 py-1.5 text-[11px] text-zinc-200 hover:bg-white/[0.09] disabled:opacity-40"
                          >
                            {busy === item.key ? "登记中" : "登记为参考图"}
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </aside>
          </div>
        </main>
      </div>
    </div>
  )
}
