"use client"

import { useMemo } from "react"
import { MarkdownView } from "@/components/common/MarkdownView"
import {
  blueprintDisplayTypeLabel,
  blueprintStatusLabel,
} from "@/lib/blueprintLabels"
import {
  type BlueprintSectionProgress,
  type BlueprintTreeNode,
  type BlueprintViewSection,
  useBlueprintStore,
} from "@/stores/blueprintStore"

function textValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}

function numberValue(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : ""
}

function blockText(block: Record<string, unknown>): string {
  const title = textValue(block.title)
  const body =
    textValue(block.text) ||
    textValue(block.summary) ||
    textValue(block.body) ||
    textValue(block.description) ||
    textValue(block.content)
  const lines = [title ? `**${title}**` : "", body].filter(Boolean)
  return lines.join("\n")
}

function itemText(item: Record<string, unknown>): string {
  const title = textValue(item.title) || textValue(item.name)
  const summary =
    textValue(item.summary) ||
    textValue(item.description) ||
    textValue(item.plot) ||
    textValue(item.body) ||
    textValue(item.text)
  const duration = textValue(item.duration) || numberValue(item.duration_seconds)
  const scene = textValue(item.scene) || textValue(item.location)
  const parts = [
    summary,
    scene ? `场景: ${scene}` : "",
    duration ? `时长: ${duration}秒` : "",
  ].filter(Boolean)
  if (title && parts.length) return `- **${title}**: ${parts.join("；")}`
  if (title) return `- **${title}**`
  return parts.length ? `- ${parts.join("；")}` : ""
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : []
}

function treeNodePurpose(node: BlueprintTreeNode): string {
  if (node.type === "text") return "剧情文本"
  if (node.type === "image") return "视觉资产"
  if (node.type === "video") return "视频片段"
  return "蓝图节点"
}

function treeNodeSummary(node: BlueprintTreeNode): string {
  return (
    textValue(node.content) ||
    textValue(node.description) ||
    textValue(node.prompt || "") ||
    ""
  )
}

function flattenTree(node: BlueprintTreeNode | null | undefined, level = 0): Array<BlueprintTreeNode & { level: number }> {
  if (!node) return []
  const current = node.id === "root" ? [] : [{ ...node, level }]
  const children = (node.children ?? []).flatMap((child) => flattenTree(child, node.id === "root" ? 0 : level + 1))
  return [...current, ...children]
}

function treeToSections(tree: BlueprintTreeNode | null): BlueprintViewSection[] {
  const nodes = flattenTree(tree)
  if (nodes.length === 0) return []
  const textNodes = nodes.filter((node) => node.type === "text")
  const imageNodes = nodes.filter((node) => node.type === "image")
  const videoNodes = nodes.filter((node) => node.type === "video")
  const toItems = (items: Array<BlueprintTreeNode & { level: number }>) =>
    items.map((node) => ({
      title: `${node.level > 0 ? "  ".repeat(node.level) : ""}${node.title || node.id}`,
      summary: treeNodeSummary(node),
      description: treeNodePurpose(node),
      duration: node.duration,
      scene: node.resolution || node.quality || "",
    }))
  return [
    textNodes.length > 0 ? { section_id: "tree_text", title: "剧情与结构", display_type: "tree", items: toItems(textNodes) } : null,
    imageNodes.length > 0 ? { section_id: "tree_image", title: "人物、场景与分镜图", display_type: "tree", items: toItems(imageNodes) } : null,
    videoNodes.length > 0 ? { section_id: "tree_video", title: "视频制作节点", display_type: "tree", items: toItems(videoNodes) } : null,
  ].filter(Boolean) as BlueprintViewSection[]
}

function sectionMarkdown(section: BlueprintViewSection): string {
  const blockLines = (section.blocks ?? []).map(blockText).filter(Boolean)
  if (blockLines.length > 0) return blockLines.join("\n\n")
  const itemLines = (section.items ?? []).slice(0, 80).map(itemText).filter(Boolean)
  return itemLines.join("\n")
}

function ScriptSection({ section }: { section: BlueprintViewSection }) {
  const episodes = records(section.items)
  if (episodes.length === 0) {
    const fallback = sectionMarkdown(section)
    return fallback ? <MarkdownView compact>{fallback}</MarkdownView> : null
  }
  return (
    <div className="space-y-3">
      {episodes.map((episode, episodeIndex) => {
        const title = textValue(episode.title) || `第 ${episodeIndex + 1} 集`
        const summary = textValue(episode.summary)
        const script = textValue(episode.script)
        const segments = records(episode.segments)
        return (
          <div key={`${title}-${episodeIndex}`} className="rounded-md border border-white/10 bg-black/20">
            <div className="border-b border-white/10 px-3 py-2">
              <div className="text-xs font-medium text-zinc-200">{title}</div>
              {(summary || script) && (
                <div className="mt-1 text-xs leading-relaxed text-zinc-400">{summary || script}</div>
              )}
            </div>
            <div className="divide-y divide-white/10">
              {segments.length === 0 && (
                <div className="px-3 py-2 text-xs text-zinc-500">暂无分段剧本。</div>
              )}
              {segments.map((segment, segmentIndex) => {
                const segmentTitle = textValue(segment.title) || `第 ${segmentIndex + 1} 段`
                const duration = textValue(segment.duration)
                const scene = textValue(segment.scene)
                const action = textValue(segment.action) || textValue(segment.beat)
                const dialogue = textValue(segment.dialogue)
                return (
                  <div key={`${segmentTitle}-${segmentIndex}`} className="grid gap-2 px-3 py-2 md:grid-cols-[120px_minmax(0,1fr)]">
                    <div className="text-[11px] text-zinc-500">
                      <div className="font-medium text-zinc-300">{segmentTitle}</div>
                      {duration && <div className="mt-0.5">{duration}</div>}
                    </div>
                    <div className="space-y-1.5 text-xs leading-relaxed">
                      {scene && (
                        <div>
                          <span className="mr-1 text-zinc-500">场景</span>
                          <span className="text-zinc-300">{scene}</span>
                        </div>
                      )}
                      {action && (
                        <div>
                          <span className="mr-1 text-zinc-500">动作</span>
                          <span className="text-zinc-200">{action}</span>
                        </div>
                      )}
                      {dialogue && (
                        <div>
                          <span className="mr-1 text-zinc-500">对白</span>
                          <span className="text-zinc-300">{dialogue}</span>
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function progressToSection(progress: BlueprintSectionProgress): BlueprintViewSection {
  return {
    section_id: progress.section_id,
    title: progress.title || "蓝图片段",
    display_type: "progress",
    blocks: progress.display_blocks ?? (
      progress.summary_text
        ? [{ type: "paragraph", text: progress.summary_text }]
        : []
    ),
  }
}

export function BlueprintViewer({
  open,
  projectId,
  onClose,
}: {
  open: boolean
  projectId?: string | null
  onClose: () => void
}) {
  const {
    status,
    blueprint,
    viewModel,
    outlineMarkdown,
    sectionProgress,
    tree,
    validation,
    loading,
    error,
    load,
  } = useBlueprintStore()

  const sections = useMemo(() => {
    const viewSections = viewModel?.sections ?? []
    if (viewSections.length > 0) return viewSections
    const progressSections = Object.values(sectionProgress)
      .sort((a, b) => (a.section_index ?? 999) - (b.section_index ?? 999))
      .map(progressToSection)
    if (progressSections.length > 0) return progressSections
    return treeToSections(tree)
  }, [sectionProgress, tree, viewModel?.sections])

  if (!open) return null

  const header = viewModel?.header
  const title = header?.title || blueprint?.theme_title || tree?.title || "项目蓝图"
  const badges = [
    ...(header?.badges ?? []),
    blueprint?.version ? `v${blueprint.version}` : "",
    blueprintStatusLabel(status),
  ].map(String).filter(Boolean).slice(0, 8)
  const hasBlueprint = Boolean(blueprint || sections.length > 0)

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-10">
      <div className="flex max-h-[calc(100dvh-16px)] w-[calc(100vw-16px)] max-w-5xl flex-col overflow-hidden rounded-lg border border-white/10 bg-[var(--studio-panel)] shadow-2xl sm:max-h-[86vh] sm:w-full">
        <div className="flex shrink-0 items-center justify-between border-b border-white/10 px-4 py-3">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-zinc-100">{title}</div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {badges.map((badge) => (
                <span key={badge} className="rounded border border-white/10 bg-white/[0.04] px-1.5 py-0.5 text-[10px] text-zinc-400">
                  {badge}
                </span>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => projectId && void load(projectId)}
              disabled={loading || !projectId}
              className="rounded-md px-2 py-1 text-xs text-zinc-400 hover:bg-white/10 hover:text-zinc-100 disabled:opacity-40"
            >
              {loading ? "读取中..." : "刷新"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md px-2 py-1 text-xs text-zinc-400 hover:bg-white/10 hover:text-zinc-100"
            >
              关闭
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
          {loading && <div className="text-sm text-zinc-500">读取蓝图中...</div>}
          {!loading && error && (
            <div className="rounded border border-red-900/60 bg-red-950/30 px-3 py-2 text-sm text-red-200">{error}</div>
          )}
          {!loading && !error && !hasBlueprint && (
            <div className="rounded border border-white/10 bg-white/[0.03] px-4 py-8 text-center text-sm text-zinc-500">
              暂无项目蓝图。开始制作视频后，模型会先生成项目蓝图。
            </div>
          )}
          {!loading && !error && hasBlueprint && (
            <div className="space-y-4">
              {(outlineMarkdown || blueprint?.short_summary || tree?.content) && (
                <section className="border-b border-white/10 pb-4">
                  <div className="mb-2 text-xs font-medium text-zinc-400">{outlineMarkdown ? "整体大纲" : "摘要"}</div>
                  <MarkdownView compact>{outlineMarkdown || blueprint?.short_summary || tree?.content || ""}</MarkdownView>
                </section>
              )}
              {(sections ?? []).map((section, index) => {
                const markdown = sectionMarkdown(section)
                const displayLabel = blueprintDisplayTypeLabel(section.display_type)
                if (!markdown && section.display_type !== "script") return null
                return (
                  <section key={section.section_id || index} className="border-b border-white/10 pb-4 last:border-b-0">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <div className="text-xs font-medium text-zinc-400">{section.title || "蓝图片段"}</div>
                      {displayLabel && (
                        <span className="rounded bg-white/[0.04] px-1.5 py-0.5 text-[10px] text-zinc-500">{displayLabel}</span>
                      )}
                    </div>
                    {section.display_type === "script" ? (
                      <ScriptSection section={section} />
                    ) : (
                      <MarkdownView compact>{markdown}</MarkdownView>
                    )}
                  </section>
                )
              })}
              {validation && (
                <section className="border-b border-white/10 pb-4 last:border-b-0">
                  <div className="mb-2 text-xs font-medium text-zinc-400">校验</div>
                  <MarkdownView compact>{textValue(validation.summary) || "蓝图校验已完成。"}</MarkdownView>
                </section>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
