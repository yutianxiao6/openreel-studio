import { resolveMediaUrl } from "@/lib/api"

const mediaIndexRequests = new Map<string, Promise<VideoEditorMediaIndex>>()
const waveformManifestRequests = new Map<string, Promise<VideoEditorWaveformManifest>>()
const waveformPageRequests = new Map<string, Promise<VideoEditorWaveformPage>>()

export interface VideoEditorRational {
  numerator: number
  denominator: number
}

export interface VideoEditorMediaIndex {
  schema_version: string
  cache_key: string
  frame_rate: VideoEditorRational
  time_base: VideoEditorRational
  width: number
  height: number
  duration_seconds: number
  frame_count: number
  variable_frame_rate: boolean
  audio: {
    present: boolean
    sample_rate: number | null
    channels: number | null
    channel_layout: string | null
  }
}

export interface VideoEditorWaveformManifest {
  schema_version: string
  cache_key: string
  sample_rate: number
  channels: number
  channel_layout: string | null
  total_samples: number
  duration_seconds: number
  peak: number
  levels: Array<{
    level: number
    samples_per_bucket: number
    bucket_count: number
  }>
}

export interface VideoEditorWaveformPage {
  cache_key: string
  level: number
  samples_per_bucket: number
  sample_rate: number
  channels: number
  bucket_count: number
  start_bucket: number
  minimum: number[][]
  maximum: number[][]
  rms: number[][]
}

export interface VideoEditorTrackSpec {
  id: string
  kind: "video" | "audio"
  name: string
  order: number
  locked: boolean
  sync_locked: boolean
  visible: boolean
  muted: boolean
  solo: boolean
  gain_db: number
}

export interface VideoEditorClipSpec {
  id: string
  track_id: string
  media_id: string
  timeline_start_frame: number
  duration_frames: number
  source_in_frame: number
  source_frame_count: number | null
  linked_group_id: string | null
  gain_db: number
  muted: boolean
  fade_in_frames: number
  fade_out_frames: number
}

export interface VideoEditorSequenceSpec {
  schema_version: "openreel.video_sequence.v1"
  settings: {
    frame_rate: VideoEditorRational
    width: number
    height: number
    audio_sample_rate: number
    audio_channels: number
  }
  tracks: VideoEditorTrackSpec[]
  clips: VideoEditorClipSpec[]
}

export interface VideoEditorSequenceDocument {
  project_id: string
  node_id: string
  revision: number
  spec: VideoEditorSequenceSpec
  created_at: string
  updated_at: string
}

async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : payload
    const message = typeof detail === "string"
      ? detail
      : detail && typeof detail === "object" && "message" in detail
        ? String((detail as { message?: unknown }).message || "请求失败")
        : `请求失败 (${response.status})`
    const error = new Error(message) as Error & { status?: number; detail?: unknown }
    error.status = response.status
    error.detail = detail
    throw error
  }
  return payload as T
}

function editorPath(projectId: string, nodeId: string, suffix: string): string {
  return resolveMediaUrl(
    `/api/video-editor/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}${suffix}`,
  )
}

export async function getVideoEditorMediaIndex(projectId: string, nodeId: string) {
  const key = `${projectId}:${nodeId}`
  const existing = mediaIndexRequests.get(key)
  if (existing) return existing
  const request = fetch(editorPath(projectId, nodeId, "/media-index"))
    .then((response) => readJson<VideoEditorMediaIndex>(response))
    .catch((error) => {
      mediaIndexRequests.delete(key)
      throw error
    })
  mediaIndexRequests.set(key, request)
  return request
}

export function getVideoEditorFrameTileUrl(
  projectId: string,
  nodeId: string,
  tileIndex: number,
  options: { columns?: number; rows?: number; frameWidth?: number; frameHeight?: number } = {},
): string {
  const columns = options.columns || 8
  const rows = options.rows || 4
  const frameWidth = options.frameWidth || 96
  const frameHeight = options.frameHeight || 54
  return editorPath(
    projectId,
    nodeId,
    `/frame-tiles/${tileIndex}?columns=${columns}&rows=${rows}&frame_width=${frameWidth}&frame_height=${frameHeight}`,
  )
}

export async function getVideoEditorWaveformManifest(projectId: string, nodeId: string) {
  const key = `${projectId}:${nodeId}`
  const existing = waveformManifestRequests.get(key)
  if (existing) return existing
  const request = fetch(editorPath(projectId, nodeId, "/waveform/manifest"))
    .then((response) => readJson<VideoEditorWaveformManifest>(response))
    .catch((error) => {
      waveformManifestRequests.delete(key)
      throw error
    })
  waveformManifestRequests.set(key, request)
  return request
}

export async function getVideoEditorWaveformPage(
  projectId: string,
  nodeId: string,
  options: { level: number; startBucket: number; limit: number },
) {
  const query = new URLSearchParams({
    level: String(options.level),
    start_bucket: String(options.startBucket),
    limit: String(options.limit),
  })
  const key = `${projectId}:${nodeId}:${query.toString()}`
  const existing = waveformPageRequests.get(key)
  if (existing) return existing
  const request = fetch(editorPath(projectId, nodeId, `/waveform?${query.toString()}`))
    .then((response) => readJson<VideoEditorWaveformPage>(response))
    .catch((error) => {
      waveformPageRequests.delete(key)
      throw error
    })
  waveformPageRequests.set(key, request)
  if (waveformPageRequests.size > 120) {
    const oldestKey = waveformPageRequests.keys().next().value
    if (oldestKey) waveformPageRequests.delete(oldestKey)
  }
  return request
}

export async function getVideoEditorSequence(projectId: string, nodeId: string) {
  return readJson<VideoEditorSequenceDocument | null>(
    await fetch(editorPath(projectId, nodeId, "/sequence")),
  )
}

export async function saveVideoEditorSequence(
  projectId: string,
  nodeId: string,
  expectedRevision: number,
  spec: VideoEditorSequenceSpec,
) {
  return readJson<VideoEditorSequenceDocument>(await fetch(editorPath(projectId, nodeId, "/sequence"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: expectedRevision, spec }),
  }))
}
