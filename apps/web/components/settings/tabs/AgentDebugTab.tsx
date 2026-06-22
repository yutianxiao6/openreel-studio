"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  getAgentDoctor,
  getAgentTrace,
  getAgentTokenUsage,
  listAgentArtifacts,
  listAgentTraces,
  readAgentArtifact,
  type AgentArtifactContent,
  type AgentArtifactKind,
  type AgentArtifactList,
  type AgentArtifactSummary,
  type AgentDoctorSnapshot,
  type AgentFeatureFlagSummary,
  type AgentFeatureFlagState,
  type AgentTraceDetail,
  type AgentTraceSummary,
  type AgentTokenUsageSummary,
} from "@/lib/api"
import { useProjectStore } from "@/stores/projectStore"

export function AgentDebugTab() {
  const currentProject = useProjectStore((s) => s.currentProject)
  const [doctor, setDoctor] = useState<AgentDoctorSnapshot | null>(null)
  const [traces, setTraces] = useState<AgentTraceSummary[]>([])
  const [traceDetail, setTraceDetail] = useState<AgentTraceDetail | null>(null)
  const [tokenUsage, setTokenUsage] = useState<AgentTokenUsageSummary | null>(null)
  const [artifacts, setArtifacts] = useState<AgentArtifactList | null>(null)
  const [selectedArtifact, setSelectedArtifact] = useState<SelectedArtifact | null>(null)
  const [artifactContent, setArtifactContent] = useState<AgentArtifactContent | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [loadingDoctor, setLoadingDoctor] = useState(false)
  const [loadingTraces, setLoadingTraces] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [loadingTokenUsage, setLoadingTokenUsage] = useState(false)
  const [loadingArtifacts, setLoadingArtifacts] = useState(false)
  const [loadingArtifactContent, setLoadingArtifactContent] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const projectId = currentProject?.id ?? ""

  const refreshDoctor = useCallback(async () => {
    if (!projectId) return
    setLoadingDoctor(true)
    setError(null)
    try {
      setDoctor(await getAgentDoctor(projectId))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingDoctor(false)
    }
  }, [projectId])

  const refreshTraces = useCallback(async () => {
    if (!projectId) return
    setLoadingTraces(true)
    setError(null)
    try {
      const result = await listAgentTraces(projectId, 20)
      setTraces(result.traces)
      setSelectedRunId((current) => {
        if (current && result.traces.some((trace) => trace.run_id === current)) return current
        return result.traces[0]?.run_id ?? null
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingTraces(false)
    }
  }, [projectId])

  const refreshSelectedTrace = useCallback(async () => {
    if (!projectId || !selectedRunId) {
      setTraceDetail(null)
      return
    }
    setLoadingDetail(true)
    setError(null)
    try {
      setTraceDetail(await getAgentTrace(projectId, selectedRunId, 200))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingDetail(false)
    }
  }, [projectId, selectedRunId])

  const refreshTokenUsage = useCallback(async () => {
    if (!projectId) return
    setLoadingTokenUsage(true)
    setError(null)
    try {
      setTokenUsage(await getAgentTokenUsage(projectId))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingTokenUsage(false)
    }
  }, [projectId])

  const refreshArtifacts = useCallback(async () => {
    if (!projectId) return
    setLoadingArtifacts(true)
    setError(null)
    try {
      const result = await listAgentArtifacts(projectId, 20)
      setArtifacts(result)
      setSelectedArtifact((current) => {
        const items = flattenArtifacts(result)
        if (current && items.some((item) => artifactKey(item) === artifactKey(current))) return current
        return items[0] ?? null
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingArtifacts(false)
    }
  }, [projectId])

  const refreshSelectedArtifact = useCallback(async () => {
    if (!projectId || !selectedArtifact) {
      setArtifactContent(null)
      return
    }
    setLoadingArtifactContent(true)
    setError(null)
    try {
      setArtifactContent(
        await readAgentArtifact(
          projectId,
          selectedArtifact.kind,
          selectedArtifact.relative_path,
          32768,
          selectedArtifact.kind === "tool_results" ? 0 : 200,
        ),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingArtifactContent(false)
    }
  }, [projectId, selectedArtifact])

  useEffect(() => {
    if (!projectId) return
    refreshDoctor()
    refreshTraces()
    refreshTokenUsage()
    refreshArtifacts()
  }, [projectId, refreshDoctor, refreshTraces, refreshTokenUsage, refreshArtifacts])

  useEffect(() => {
    refreshSelectedTrace()
  }, [refreshSelectedTrace])

  useEffect(() => {
    refreshSelectedArtifact()
  }, [refreshSelectedArtifact])

  const selectedTrace = useMemo(
    () => traces.find((trace) => trace.run_id === selectedRunId) ?? null,
    [traces, selectedRunId],
  )
  const selectedRunArtifacts = useMemo(() => {
    if (!selectedRunId) return []
    return flattenArtifacts(artifacts).filter((artifact) => artifactBelongsToRun(artifact, selectedRunId))
  }, [artifacts, selectedRunId])

  if (!currentProject) {
    return (
      <div className="rounded border border-gray-800 bg-gray-950/40 p-4 text-sm text-gray-400">
        当前没有项目，无法读取 agent 诊断。
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {error && (
        <div className="rounded border border-red-800 bg-red-950/40 p-3 text-xs text-red-200">
          {error}
        </div>
      )}

      <section className="rounded border border-gray-800 bg-gray-950/40">
        <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
          <div>
            <h3 className="text-xs font-semibold text-gray-300">Doctor 快照</h3>
            <p className="mt-0.5 text-[10px] text-gray-600">等价于 /doctor，但不会写入聊天历史</p>
          </div>
          <button
            onClick={refreshDoctor}
            disabled={loadingDoctor}
            className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50"
          >
            {loadingDoctor ? "刷新中..." : "刷新"}
          </button>
        </div>
        <DoctorSummary doctor={doctor} loading={loadingDoctor} />
      </section>

      <TokenUsageBlock
        usage={tokenUsage}
        selectedRunId={selectedRunId}
        loading={loadingTokenUsage}
        onRefresh={refreshTokenUsage}
      />

      <section className="grid min-h-[360px] grid-cols-1 overflow-hidden rounded border border-gray-800 bg-gray-950/40 lg:grid-cols-[320px_minmax(0,1fr)]">
        <div className="border-r border-gray-800">
          <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
            <div>
              <h3 className="text-xs font-semibold text-gray-300">最近 Trace</h3>
              <p className="mt-0.5 text-[10px] text-gray-600">{traces.length} / 最近 20 条 run</p>
            </div>
            <button
              onClick={refreshTraces}
              disabled={loadingTraces}
              className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50"
            >
              {loadingTraces ? "读取中..." : "刷新"}
            </button>
          </div>
          <div className="max-h-[520px] overflow-y-auto">
            {traces.length === 0 && (
              <div className="p-4 text-xs text-gray-500">
                还没有 trace。执行一次 agent 对话后会写入 data/agent_traces。
              </div>
            )}
            {traces.map((trace) => (
              <button
                key={trace.run_id}
                onClick={() => setSelectedRunId(trace.run_id)}
                className={`block w-full border-b border-gray-900 px-3 py-2 text-left transition-colors ${
                  selectedRunId === trace.run_id
                    ? "bg-indigo-950/40"
                    : "bg-transparent hover:bg-white/[0.04]"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-[11px] text-gray-200">{trace.run_id}</span>
                  {trace.error_count > 0 && (
                    <span className="shrink-0 rounded bg-red-950 px-1.5 py-0.5 text-[10px] text-red-300">
                      {trace.error_count} err
                    </span>
                  )}
                </div>
                <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-gray-500">
                  <span>{trace.last_event || "unknown"}</span>
                  <span>{formatTime(trace.last_event_at || trace.mtime)}</span>
                </div>
                <div className="mt-1 truncate text-[10px] text-gray-600">
                  {trace.event_count} events {trace.last_tool_name ? `· ${trace.last_tool_name}` : ""}
                </div>
              </button>
            ))}
          </div>
        </div>

        <TraceDetail
          trace={selectedTrace}
          detail={traceDetail}
          runArtifacts={selectedRunArtifacts}
          loading={loadingDetail}
          onRefresh={refreshSelectedTrace}
          onSelectArtifact={setSelectedArtifact}
        />
      </section>

      <ArtifactSection
        artifacts={artifacts}
        selected={selectedArtifact}
        content={artifactContent}
        loadingList={loadingArtifacts}
        loadingContent={loadingArtifactContent}
        onRefreshList={refreshArtifacts}
        onRefreshContent={refreshSelectedArtifact}
        onSelect={setSelectedArtifact}
      />
    </div>
  )
}

type SelectedArtifact = AgentArtifactSummary & { kind: AgentArtifactKind }

function ArtifactSection({
  artifacts,
  selected,
  content,
  loadingList,
  loadingContent,
  onRefreshList,
  onRefreshContent,
  onSelect,
}: {
  artifacts: AgentArtifactList | null
  selected: SelectedArtifact | null
  content: AgentArtifactContent | null
  loadingList: boolean
  loadingContent: boolean
  onRefreshList: () => void
  onRefreshContent: () => void
  onSelect: (artifact: SelectedArtifact) => void
}) {
  const items = useMemo(() => flattenArtifacts(artifacts), [artifacts])
  return (
    <section className="grid min-h-[320px] grid-cols-1 overflow-hidden rounded border border-gray-800 bg-gray-950/40 lg:grid-cols-[320px_minmax(0,1fr)]">
      <div className="border-r border-gray-800">
        <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
          <div>
            <h3 className="text-xs font-semibold text-gray-300">Debug Artifacts</h3>
            <p className="mt-0.5 text-[10px] text-gray-600">{items.length} / 最近 20 个文件</p>
          </div>
          <button
            onClick={onRefreshList}
            disabled={loadingList}
            className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50"
          >
            {loadingList ? "读取中..." : "刷新"}
          </button>
        </div>
        <div className="max-h-[460px] overflow-y-auto">
          {items.length === 0 && (
            <div className="p-4 text-xs text-gray-500">
              暂无 artifact。执行一次 agent 对话后会产生 trace 或 tool result。
            </div>
          )}
          {items.map((artifact) => (
            <button
              key={artifactKey(artifact)}
              onClick={() => onSelect(artifact)}
              className={`block w-full border-b border-gray-900 px-3 py-2 text-left transition-colors ${
                selected && artifactKey(selected) === artifactKey(artifact)
                  ? "bg-indigo-950/40"
                  : "bg-transparent hover:bg-white/[0.04]"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-[11px] text-gray-200">{artifact.name}</span>
                <span className="shrink-0 rounded bg-gray-900 px-1.5 py-0.5 text-[10px] text-gray-500">
                  {artifactKindLabel(artifact.kind)}
                </span>
              </div>
              <div className="mt-1 truncate text-[10px] text-gray-600">{artifact.relative_path}</div>
              <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-gray-500">
                <span>{formatBytes(artifact.size_bytes)}</span>
                <span>{formatTime(artifact.mtime)}</span>
              </div>
            </button>
          ))}
        </div>
      </div>

      <ArtifactContentPanel
        selected={selected}
        content={content}
        loading={loadingContent}
        onRefresh={onRefreshContent}
      />
    </section>
  )
}

function ArtifactContentPanel({
  selected,
  content,
  loading,
  onRefresh,
}: {
  selected: SelectedArtifact | null
  content: AgentArtifactContent | null
  loading: boolean
  onRefresh: () => void
}) {
  if (!selected) {
    return <div className="p-4 text-sm text-gray-500">选择一个 artifact 查看受限预览。</div>
  }

  return (
    <div className="min-w-0">
      <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-xs text-gray-200">{selected.name}</div>
          <div className="mt-0.5 truncate text-[10px] text-gray-600">{selected.path}</div>
        </div>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="ml-3 shrink-0 rounded bg-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50"
        >
          {loading ? "读取中..." : "重读"}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2 border-b border-gray-900 px-3 py-2 text-[10px] text-gray-500">
        <span>{content?.mode ?? "tail"}</span>
        <span>{formatBytes(content?.returned_bytes ?? selected.size_bytes)} / {formatBytes(selected.size_bytes)}</span>
        {content?.truncated && <span className="text-amber-300">已截断</span>}
        {content?.total_lines !== undefined && <span>{content.returned_lines}/{content.total_lines} lines</span>}
      </div>
      <div className="max-h-[420px] overflow-y-auto">
        {!content && loading && <div className="p-4 text-sm text-gray-500">读取中...</div>}
        {content && (
          <pre className="min-h-[260px] whitespace-pre-wrap break-words bg-black/30 px-3 py-2 font-mono text-[11px] leading-relaxed text-gray-300">
            {content.content || "(empty)"}
          </pre>
        )}
      </div>
    </div>
  )
}

function DoctorSummary({
  doctor,
  loading,
}: {
  doctor: AgentDoctorSnapshot | null
  loading: boolean
}) {
  if (loading && !doctor) {
    return <div className="p-4 text-sm text-gray-500">读取中...</div>
  }
  if (!doctor) {
    return <div className="p-4 text-sm text-gray-500">暂无诊断数据。</div>
  }
  if (!doctor.ok) {
    return <div className="p-4 text-sm text-red-300">{doctor.error || "诊断失败"}</div>
  }

  const nodeSummary = doctor.node_summary
  const featureFlags = doctor.feature_flags

  return (
    <div className="grid grid-cols-1 gap-3 p-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <div className="grid grid-cols-2 gap-2">
        <Metric label="节点总数" value={String(nodeSummary?.total ?? 0)} />
        <Metric label="待确认重置" value={doctor.has_pending_reset ? "有" : "无"} tone={doctor.has_pending_reset ? "red" : "gray"} />
        <Metric
          label="功能开关"
          value={featureFlags ? `${featureFlags.enabled}/${featureFlags.total} 开启` : "未知"}
          tone={featureFlags?.disabled ? "amber" : "gray"}
        />
        <Metric
          label="Kill switch"
          value={featureFlags ? `${featureFlags.killed} 生效` : "未知"}
          tone={featureFlags?.killed ? "red" : "gray"}
        />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <CountBlock title="节点状态" counts={nodeSummary?.by_status ?? {}} />
        <CountBlock title="节点类型" counts={nodeSummary?.by_type ?? {}} />
      </div>
      <FeatureFlagBlock summary={featureFlags} />
    </div>
  )
}

function TokenUsageBlock({
  usage,
  selectedRunId,
  loading,
  onRefresh,
}: {
  usage: AgentTokenUsageSummary | null
  selectedRunId: string | null
  loading: boolean
  onRefresh: () => void
}) {
  const selectedRun = usage?.by_run.find((item) => item.run_id === selectedRunId)
  const totals = usage?.totals ?? {}
  const selectedTotals = selectedRun?.totals ?? null
  const lastUsage = usage?.last_usage ?? {}
  const cumulative = recordValue(usage?.session_cumulative_tokens) ?? recordValue(totals.cumulative_tokens) ?? totals
  const selectedCumulative = recordValue(selectedTotals?.cumulative_tokens) ?? selectedTotals
  const latestCallContext =
    recordValue(usage?.latest_call_context) ??
    recordValue(lastUsage.latest_call_context) ??
    lastUsage
  const latestCallTokens =
    recordValue(usage?.latest_call_tokens) ??
    recordValue(lastUsage.latest_call_tokens)
  const sessionContextPeak =
    recordValue(usage?.session_context_peak) ??
    recordValue(totals.context_peak)
  const selectedContextPeak = recordValue(selectedTotals?.context_peak)
  const cacheRate = numberValue(cumulative.cache_hit_rate)
  const selectedCacheRate = numberValue(selectedCumulative?.cache_hit_rate)
  const latestRemaining =
    numberValue(latestCallContext.context_remaining_tokens) ??
    numberValue(lastUsage.context_remaining_tokens)
  const latestCallTotal = numberValue(latestCallTokens?.total_tokens) ?? numberValue(lastUsage.total_tokens)
  const latestContextUsedRate =
    numberValue(latestCallContext.context_used_rate) ??
    numberValue(lastUsage.context_used_rate)
  const latestContextAvailableRate =
    numberValue(latestCallContext.context_available_rate) ??
    numberValue(lastUsage.context_available_rate) ??
    (latestContextUsedRate === null ? null : Math.max(0, 1 - latestContextUsedRate))
  const latestContextSource =
    typeof latestCallContext.context_limit_source === "string" ? latestCallContext.context_limit_source :
    typeof lastUsage.context_limit_source === "string" ? lastUsage.context_limit_source :
    "unknown"
  const totalContextAvailableRate =
    numberValue(sessionContextPeak?.context_available_rate) ??
    numberValue(selectedContextPeak?.context_available_rate) ??
    numberValue(totals.context_peak_available_rate) ??
    numberValue(selectedTotals?.context_peak_available_rate)
  const totalContextUsedRate =
    numberValue(sessionContextPeak?.context_used_rate) ??
    numberValue(selectedContextPeak?.context_used_rate) ??
    numberValue(totals.context_peak_used_rate) ??
    numberValue(selectedTotals?.context_peak_used_rate) ??
    (totalContextAvailableRate === null ? null : Math.max(0, 1 - totalContextAvailableRate))
  const totalContextRemaining =
    numberValue(sessionContextPeak?.context_remaining_tokens) ??
    numberValue(totals.context_peak_remaining_tokens)
  const totalContextLimit =
    numberValue(sessionContextPeak?.context_limit_tokens) ??
    numberValue(totals.context_peak_limit_tokens)
  const totalContextSource =
    typeof sessionContextPeak?.context_limit_source === "string" ? sessionContextPeak.context_limit_source :
    typeof totals.context_peak_limit_source === "string" ? totals.context_peak_limit_source :
    "unknown"

  return (
    <section className="rounded border border-gray-800 bg-gray-950/40">
      <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
        <div>
          <h3 className="text-xs font-semibold text-gray-300">Token 监控</h3>
          <p className="mt-0.5 text-[10px] text-gray-600">来自 llm_usage trace；窗口剩余是单次调用容量压力，不是累计花费；累计花费看项目/选中 run tokens</p>
          {usage?.context_cleared_at && (
            <p className="mt-0.5 text-[10px] text-gray-600">当前累计从最近一次 clear 后开始：{usage.context_cleared_at}</p>
          )}
        </div>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="rounded bg-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50"
        >
          {loading ? "读取中..." : "刷新"}
        </button>
      </div>
      <div className="grid gap-2 p-3 md:grid-cols-4">
        <Metric label="clear 后累计 tokens" value={formatNumber(numberValue(cumulative.total_tokens))} />
        <Metric label="clear 后 LLM 调用" value={formatNumber(numberValue(cumulative.llm_calls))} />
        <Metric label="clear 后缓存命中率" value={formatPercent(cacheRate)} tone={cacheRate === null ? "amber" : "gray"} />
        <Metric
          label={`单次窗口峰值剩余 (${totalContextSource})`}
          value={formatPercent(totalContextAvailableRate)}
          tone={totalContextAvailableRate !== null && totalContextAvailableRate < 0.15 ? "red" : totalContextAvailableRate !== null && totalContextAvailableRate < 0.3 ? "amber" : "gray"}
        />
      </div>
      <div className="grid gap-2 border-t border-gray-900 p-3 md:grid-cols-4">
        <Metric label="选中 run 累计 tokens" value={formatNumber(numberValue(selectedCumulative?.total_tokens))} />
        <Metric label="选中 run 调用" value={formatNumber(numberValue(selectedCumulative?.llm_calls))} />
        <Metric
          label="单次窗口峰值已用"
          value={formatPercent(totalContextUsedRate)}
          tone={totalContextUsedRate !== null && totalContextUsedRate > 0.85 ? "red" : totalContextUsedRate !== null && totalContextUsedRate > 0.7 ? "amber" : "gray"}
        />
        <Metric
          label="峰值窗口剩余 tokens"
          value={totalContextRemaining === null ? "unknown" : `${formatNumber(totalContextRemaining)} / ${formatNumber(totalContextLimit)}`}
          tone={totalContextRemaining !== null && totalContextRemaining < 8000 ? "amber" : "gray"}
        />
      </div>
      <div className="grid gap-2 border-t border-gray-900 p-3 md:grid-cols-4">
        <Metric label="选中 run 缓存命中率" value={formatPercent(selectedCacheRate)} tone={selectedCacheRate === null ? "amber" : "gray"} />
        <Metric
          label={`最近调用窗口剩余 (${latestContextSource})`}
          value={formatPercent(latestContextAvailableRate)}
          tone={latestContextAvailableRate !== null && latestContextAvailableRate < 0.15 ? "red" : latestContextAvailableRate !== null && latestContextAvailableRate < 0.3 ? "amber" : "gray"}
        />
        <Metric
          label="最近调用已用"
          value={formatPercent(latestContextUsedRate)}
          tone={latestContextUsedRate !== null && latestContextUsedRate > 0.85 ? "red" : latestContextUsedRate !== null && latestContextUsedRate > 0.7 ? "amber" : "gray"}
        />
        <Metric
          label="最近调用剩余 tokens"
          value={latestRemaining === null ? "unknown" : formatNumber(latestRemaining)}
          tone={latestRemaining !== null && latestRemaining < 8000 ? "amber" : "gray"}
        />
        <Metric
          label="最近调用 total tokens"
          value={formatNumber(latestCallTotal)}
          tone={latestCallTotal === null ? "amber" : "gray"}
        />
      </div>
    </section>
  )
}

function TraceDetail({
  trace,
  detail,
  runArtifacts,
  loading,
  onRefresh,
  onSelectArtifact,
}: {
  trace: AgentTraceSummary | null
  detail: AgentTraceDetail | null
  runArtifacts: SelectedArtifact[]
  loading: boolean
  onRefresh: () => void
  onSelectArtifact: (artifact: SelectedArtifact) => void
}) {
  if (!trace) {
    return <div className="p-4 text-sm text-gray-500">选择一条 trace 查看事件。</div>
  }

  return (
    <div className="min-w-0">
      <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-xs text-gray-200">{trace.run_id}</div>
          <div className="mt-0.5 truncate text-[10px] text-gray-600">{trace.path}</div>
        </div>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="ml-3 shrink-0 rounded bg-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-700 disabled:opacity-50"
        >
          {loading ? "读取中..." : "重读"}
        </button>
      </div>
      <div className="flex items-center gap-2 border-b border-gray-900 px-3 py-2 text-[10px] text-gray-500">
        <span>{detail?.event_count ?? trace.event_count} events</span>
        {detail?.truncated && <span>仅显示尾部 {detail.returned} 条</span>}
        {trace.last_error_kind && <span className="text-red-300">{trace.last_error_kind}</span>}
      </div>
      {detail && (
        <TraceRunTimeline
          detail={detail}
          artifacts={runArtifacts}
          onSelectArtifact={onSelectArtifact}
        />
      )}
      <div className="max-h-[470px] overflow-y-auto">
        {!detail && loading && <div className="p-4 text-sm text-gray-500">读取中...</div>}
        {detail && detail.events.length === 0 && (
          <div className="p-4 text-sm text-gray-500">trace 为空。</div>
        )}
        {detail?.events.map((event, index) => (
          <TraceEventRow key={`${trace.run_id}-${index}`} event={event} index={index} />
        ))}
      </div>
    </div>
  )
}

function TraceRunTimeline({
  detail,
  artifacts,
  onSelectArtifact,
}: {
  detail: AgentTraceDetail
  artifacts: SelectedArtifact[]
  onSelectArtifact: (artifact: SelectedArtifact) => void
}) {
  const items = useMemo(() => buildTraceTimeline(detail.events), [detail.events])
  if (items.length === 0 && artifacts.length === 0) return null

  return (
    <div className="border-b border-gray-900 bg-black/15 px-3 py-2">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-[10px] font-medium uppercase text-gray-500">Run Timeline</div>
        <div className="truncate font-mono text-[10px] text-gray-600">{detail.run_id}</div>
      </div>
      <div className="grid gap-1.5 md:grid-cols-2 xl:grid-cols-3">
        {items.map((item, index) => (
          <div
            key={`${item.kind}-${index}`}
            className={`min-w-0 rounded border px-2 py-1.5 ${timelineToneClass(item.tone)}`}
          >
            <div className="flex items-center gap-2">
              <span className="shrink-0 text-[10px] font-medium">{item.label}</span>
              {item.badge && (
                <span className="shrink-0 rounded bg-black/25 px-1.5 py-0.5 font-mono text-[10px]">
                  {item.badge}
                </span>
              )}
            </div>
            <div className="mt-1 truncate text-[10px] opacity-80">{item.detail}</div>
          </div>
        ))}
        {artifacts.length > 0 && (
          <div className="min-w-0 rounded border border-gray-800 bg-gray-950/50 px-2 py-1.5 text-gray-300 md:col-span-2 xl:col-span-3">
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="text-[10px] font-medium">Artifacts</span>
              <span className="text-[10px] text-gray-600">{artifacts.length} linked</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {artifacts.slice(0, 8).map((artifact) => (
                <button
                  key={artifactKey(artifact)}
                  onClick={() => onSelectArtifact(artifact)}
                  className="max-w-full rounded border border-gray-700 bg-black/25 px-2 py-1 text-[10px] text-gray-300 hover:border-indigo-700 hover:text-indigo-200"
                  title={artifact.relative_path}
                >
                  <span className="font-medium">{artifactKindLabel(artifact.kind)}</span>
                  <span className="ml-1 font-mono text-gray-500">{artifact.name}</span>
                </button>
              ))}
              {artifacts.length > 8 && (
                <span className="rounded border border-gray-800 px-2 py-1 text-[10px] text-gray-500">
                  +{artifacts.length - 8}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function TraceEventRow({ event, index }: { event: Record<string, unknown>; index: number }) {
  const [open, setOpen] = useState(false)
  const name = String(event.event ?? "event")
  const tool = typeof event.tool_name === "string" ? event.tool_name : ""
  const errorKind = typeof event.error_kind === "string" ? event.error_kind : ""
  const transition = typeof event.transition_reason === "string" ? event.transition_reason : ""
  const duration = typeof event.duration_ms === "number" ? `${event.duration_ms}ms` : ""

  return (
    <div className="border-b border-gray-900">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-white/[0.035]"
      >
        <span className="w-8 shrink-0 text-[10px] text-gray-600">{index + 1}</span>
        <span className={`shrink-0 text-xs ${errorKind ? "text-red-300" : "text-indigo-300"}`}>{name}</span>
        {tool && <span className="min-w-0 truncate font-mono text-[11px] text-gray-300">{tool}</span>}
        {transition && <span className="hidden min-w-0 truncate text-[10px] text-gray-500 md:inline">{transition}</span>}
        <span className="ml-auto shrink-0 text-[10px] text-gray-600">{duration}</span>
        {errorKind && <span className="shrink-0 rounded bg-red-950 px-1.5 py-0.5 text-[10px] text-red-300">{errorKind}</span>}
      </button>
      {open && (
        <pre className="max-h-72 overflow-auto border-t border-gray-900 bg-black/30 px-3 py-2 text-[11px] leading-relaxed text-gray-300">
          {JSON.stringify(event, null, 2)}
        </pre>
      )}
    </div>
  )
}

type TimelineTone = "gray" | "blue" | "green" | "amber" | "red"

interface TraceTimelineItem {
  kind: string
  label: string
  detail: string
  badge?: string
  tone: TimelineTone
}

function buildTraceTimeline(events: Array<Record<string, unknown>>): TraceTimelineItem[] {
  const out: TraceTimelineItem[] = []
  const prompt = events.find((event) => event.event === "prompt_assembly")
  if (prompt) {
    out.push({
      kind: "prompt",
      label: "Prompt",
      badge: asText(prompt.tools_count) ? `${asText(prompt.tools_count)} tools` : undefined,
      detail: [
        asText(prompt.section_count) ? `${asText(prompt.section_count)} sections` : "",
        asText(prompt.cache_key) ? `cache ${truncateMiddle(asText(prompt.cache_key), 18)}` : "",
      ].filter(Boolean).join(" · ") || "assembled",
      tone: "blue",
    })
  }

  const requested = events.find((event) => event.event === "tool_calls_requested")
  if (requested) {
    const tools = Array.isArray(requested.tool_names) ? requested.tool_names.map(String) : []
    out.push({
      kind: "tool_calls",
      label: "Tool Calls",
      badge: tools.length ? String(tools.length) : undefined,
      detail: tools.slice(0, 4).join(", ") || "model requested tools",
      tone: "gray",
    })
  }

  const permissionEvents = events.filter((event) => event.event === "permission_decision")
  if (permissionEvents.length) {
    const denied = permissionEvents.filter((event) => event.allowed === false).length
    out.push({
      kind: "permission",
      label: "Permission",
      badge: denied ? `${denied} denied` : `${permissionEvents.length} ok`,
      detail: summarizeEventTools(permissionEvents),
      tone: denied ? "amber" : "green",
    })
  }

  const resultEvents = events.filter((event) => event.event === "tool_result")
  if (resultEvents.length) {
    const failed = resultEvents.filter((event) => typeof event.error_kind === "string" && event.error_kind).length
    out.push({
      kind: "tool_results",
      label: "Tool Results",
      badge: failed ? `${failed} err` : `${resultEvents.length} done`,
      detail: summarizeEventTools(resultEvents),
      tone: failed ? "red" : "green",
    })
  }

  const confirmation = events.find((event) => String(event.event || "").startsWith("confirmation_"))
  if (confirmation) {
    out.push({
      kind: "confirmation",
      label: "Confirm",
      badge: asText(confirmation.event).replace("confirmation_", ""),
      detail: [
        asText(confirmation.confirmation_kind),
        asText(confirmation.action),
        asText(confirmation.risk),
      ].filter(Boolean).join(" · ") || asText(confirmation.transition_reason) || "confirmation event",
      tone: confirmation.event === "confirmation_created" ? "amber" : "green",
    })
  }

  const compact = events.find((event) => event.event === "compact_boundary")
  if (compact) {
    out.push({
      kind: "compact",
      label: "Compact",
      badge: asText(compact.compact_kind) || undefined,
      detail: asText(compact.estimated_tokens_before)
        ? `${asText(compact.estimated_tokens_before)} tokens before`
        : "context compacted",
      tone: "amber",
    })
  }

  const complete = [...events].reverse().find((event) => event.event === "run_complete")
  if (complete) {
    out.push({
      kind: "complete",
      label: "Complete",
      badge: asText(complete.transition_reason) || undefined,
      detail: asText(complete.error_kind) || asText(complete.ts) || "run completed",
      tone: asText(complete.error_kind) ? "red" : "green",
    })
  }

  return out
}

function summarizeEventTools(events: Array<Record<string, unknown>>): string {
  const names = events
    .map((event) => asText(event.tool_name))
    .filter(Boolean)
  const unique = Array.from(new Set(names))
  return unique.slice(0, 5).join(", ") || `${events.length} events`
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return ""
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return ""
}

function truncateMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value
  const side = Math.max(4, Math.floor((maxLength - 1) / 2))
  return `${value.slice(0, side)}…${value.slice(-side)}`
}

function timelineToneClass(tone: TimelineTone): string {
  if (tone === "blue") return "border-indigo-900/70 bg-indigo-950/20 text-indigo-200"
  if (tone === "green") return "border-emerald-900/70 bg-emerald-950/20 text-emerald-200"
  if (tone === "amber") return "border-amber-900/70 bg-amber-950/20 text-amber-200"
  if (tone === "red") return "border-red-900/70 bg-red-950/20 text-red-200"
  return "border-gray-800 bg-gray-950/50 text-gray-300"
}

function Metric({
  label,
  value,
  tone = "gray",
}: {
  label: string
  value: string
  tone?: "gray" | "amber" | "red"
}) {
  const cls = {
    gray: "text-gray-100",
    amber: "text-amber-300",
    red: "text-red-300",
  }[tone]
  return (
    <div className="rounded border border-gray-800 bg-black/20 px-3 py-2">
      <div className="text-[10px] text-gray-600">{label}</div>
      <div className={`mt-1 truncate text-sm ${cls}`}>{value}</div>
    </div>
  )
}

function CountBlock({ title, counts }: { title: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1])
  return (
    <div className="min-h-[120px] rounded border border-gray-800 bg-black/20 px-3 py-2">
      <div className="mb-2 text-[10px] text-gray-600">{title}</div>
      {entries.length === 0 ? (
        <div className="text-xs text-gray-500">无</div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {entries.slice(0, 12).map(([key, value]) => (
            <span key={key} className="rounded bg-gray-900 px-2 py-1 text-[10px] text-gray-300">
              {key}:{value}
            </span>
          ))}
          {entries.length > 12 && (
            <span className="rounded bg-gray-900 px-2 py-1 text-[10px] text-gray-500">
              +{entries.length - 12}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function FeatureFlagBlock({ summary }: { summary?: AgentFeatureFlagSummary }) {
  const items = summary?.items ?? []
  return (
    <div className="rounded border border-gray-800 bg-black/20 px-3 py-2 xl:col-span-2">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-[10px] text-gray-600">功能开关</div>
        {summary ? (
          <div className="text-[10px] text-gray-500">
            {summary.enabled}/{summary.total} enabled
            {summary.killed ? ` · ${summary.killed} killed` : ""}
          </div>
        ) : null}
      </div>
      {items.length === 0 ? (
        <div className="text-xs text-gray-500">无</div>
      ) : (
        <div className="grid gap-1.5 md:grid-cols-2">
          {items.map((flag) => (
            <FeatureFlagRow key={flag.name} flag={flag} />
          ))}
        </div>
      )}
      {summary && summary.killed_names.length > 0 ? (
        <div className="mt-2 truncate text-[10px] text-red-300">
          killed: {summary.killed_names.join(", ")}
        </div>
      ) : null}
    </div>
  )
}

function FeatureFlagRow({ flag }: { flag: AgentFeatureFlagState }) {
  const statusClass = flag.killed
    ? "border-red-900/70 bg-red-950/30 text-red-300"
    : flag.enabled
      ? "border-emerald-900/70 bg-emerald-950/20 text-emerald-300"
      : "border-amber-900/70 bg-amber-950/20 text-amber-300"
  const status = flag.killed ? "killed" : flag.enabled ? "on" : "off"
  return (
    <div
      className="min-w-0 rounded border border-gray-900 bg-gray-950/40 px-2 py-1.5"
      title={flag.description}
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-gray-200">
          {flag.name}
        </span>
        <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${statusClass}`}>
          {status}
        </span>
      </div>
      <div className="mt-1 flex items-center gap-2 text-[10px] text-gray-600">
        <span>{flag.owner}</span>
        <span className="min-w-0 truncate">{flag.killed ? flag.kill_source || "kill switch" : flag.source}</span>
      </div>
    </div>
  )
}

function flattenArtifacts(list: AgentArtifactList | null): SelectedArtifact[] {
  if (!list) return []
  const kinds: AgentArtifactKind[] = ["traces", "prompt_dumps", "tool_results"]
  return kinds
    .flatMap((kind) => (list.artifacts[kind]?.items ?? []).map((item) => ({ ...item, kind })))
    .sort((a, b) => new Date(b.mtime).getTime() - new Date(a.mtime).getTime())
}

function artifactKey(artifact: Pick<SelectedArtifact, "kind" | "relative_path">): string {
  return `${artifact.kind}:${artifact.relative_path}`
}

function artifactBelongsToRun(artifact: SelectedArtifact, runId: string): boolean {
  if (!runId) return false
  if (artifact.kind === "tool_results") {
    return artifact.relative_path === runId || artifact.relative_path.startsWith(`${runId}/`)
  }
  return (
    artifact.id === runId ||
    artifact.name === `${runId}.jsonl` ||
    artifact.relative_path === `${runId}.jsonl`
  )
}

function artifactKindLabel(kind: AgentArtifactKind): string {
  if (kind === "prompt_dumps") return "prompt"
  if (kind === "tool_results") return "tool"
  return "trace"
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value)
  return null
}

function recordValue(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
  return null
}

function formatNumber(value: number | null): string {
  if (value === null) return "0"
  return Math.round(value).toLocaleString()
}

function formatPercent(value: number | null): string {
  if (value === null) return "unknown"
  return `${Math.round(value * 1000) / 10}%`
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B"
  if (value < 1024) return `${value} B`
  const kb = value / 1024
  if (kb < 1024) return `${kb.toFixed(kb >= 10 ? 0 : 1)} KB`
  const mb = kb / 1024
  return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`
}

function formatTime(value: string | null | undefined): string {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}
