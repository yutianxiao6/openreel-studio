export interface MediaProviderSummary {
  kind: string
  name: string
  model_name: string
  api_format?: string
  params?: Record<string, unknown>
  is_active?: boolean
  enabled?: boolean
}

export interface VideoProtocolModeSummary {
  min_images?: number | string | null
  max_images?: number | string | null
  min_total_media?: number | string | null
  max_total_media?: number | string | null
  min_media?: number | string | null
  max_media?: number | string | null
}

export interface VideoProtocolProfileSummary {
  match?: string
  model?: string
  modes?: Record<string, VideoProtocolModeSummary> | string[]
  supported_modes?: string[]
}

export interface VideoProtocolSummary {
  id: string
  model_names?: string[]
  model_profiles?: VideoProtocolProfileSummary[]
  modes?: Record<string, VideoProtocolModeSummary>
}

export function finiteProtocolNumber(value: unknown): number | undefined {
  if (value == null || value === "") return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

export function canonicalVideoMode(value: string): string {
  const mode = value.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_")
  if (["t2v", "txt2video", "text2video"].includes(mode)) return "text_to_video"
  if (["i2v", "image_to_video", "source_image", "single_image"].includes(mode)) return "first_frame"
  if (["first_last", "first_and_last_frame", "first_last_frames"].includes(mode)) return "first_last_frame"
  if (["reference_to_video", "reference_video", "omni_reference", "omni_reference_video"].includes(mode)) return "multimodal_reference"
  return mode
}

export function resolveVideoProvider(value: string, providers: MediaProviderSummary[]): MediaProviderSummary | undefined {
  const enabled = providers.filter((provider) => provider.kind === "video" && provider.enabled !== false)
  const selected = value.trim()
  if (selected) {
    return enabled.find((provider) => provider.name === selected || provider.model_name === selected)
  }
  return enabled.find((provider) => provider.is_active) || enabled[0]
}

export function videoProtocolForProvider(
  provider: MediaProviderSummary | undefined,
  protocols: VideoProtocolSummary[],
): VideoProtocolSummary | undefined {
  const protocolId = String(
    provider?.params?.video_protocol_id
    || provider?.params?.protocol_id
    || provider?.params?.protocol
    || "",
  ).trim()
  if (protocolId) {
    const exact = protocols.find((protocol) => protocol.id === protocolId)
    if (exact) return exact
  }
  const modelName = String(provider?.model_name || "").trim()
  if (!modelName) return undefined
  return protocols.find((protocol) => {
    if (protocol.model_names?.includes(modelName)) return true
    return (protocol.model_profiles || []).some((profile) => (profile.match || profile.model) === modelName)
  })
}

export function videoProfileForModel(
  protocol: VideoProtocolSummary | undefined,
  modelName: string,
): VideoProtocolProfileSummary | undefined {
  const name = modelName.trim()
  if (!protocol || !name) return undefined
  return (protocol.model_profiles || []).find((profile) => (profile.match || profile.model) === name)
}

export function videoModeEntriesForProvider(
  protocol?: VideoProtocolSummary,
  profile?: VideoProtocolProfileSummary,
): Map<string, VideoProtocolModeSummary> {
  const modes = protocol?.modes || {}
  const byCanonical = new Map<string, VideoProtocolModeSummary>()
  Object.entries(modes).forEach(([mode, config]) => {
    byCanonical.set(canonicalVideoMode(mode), config)
  })
  if (Array.isArray(profile?.modes)) {
    const allowed = new Set(profile.modes.map((mode) => canonicalVideoMode(String(mode))))
    Array.from(byCanonical.keys()).forEach((mode) => {
      if (!allowed.has(mode)) byCanonical.delete(mode)
    })
  } else if (profile?.modes && typeof profile.modes === "object") {
    const overrides = new Map<string, VideoProtocolModeSummary>()
    Object.entries(profile.modes).forEach(([mode, config]) => {
      const canonical = canonicalVideoMode(mode)
      overrides.set(canonical, { ...(byCanonical.get(canonical) || {}), ...config })
    })
    byCanonical.clear()
    overrides.forEach((config, mode) => byCanonical.set(mode, config))
  }
  if (profile?.supported_modes?.length) {
    const allowed = new Set(profile.supported_modes.map((mode) => canonicalVideoMode(mode)))
    Array.from(byCanonical.keys()).forEach((mode) => {
      if (!allowed.has(mode)) byCanonical.delete(mode)
    })
  }
  return byCanonical
}

export function videoModeConfig(
  protocol: VideoProtocolSummary | undefined,
  mode: string,
  profile?: VideoProtocolProfileSummary,
): VideoProtocolModeSummary | undefined {
  const entries = videoModeEntriesForProvider(protocol, profile)
  const canonical = canonicalVideoMode(mode)
  return entries.get(canonical) || entries.values().next().value
}

export function videoReferenceImageLimit(modeConfig: VideoProtocolModeSummary | undefined): number | undefined {
  const maxImages = finiteProtocolNumber(modeConfig?.max_images)
  const maxTotal = finiteProtocolNumber(modeConfig?.max_total_media ?? modeConfig?.max_media)
  return maxImages ?? maxTotal
}

export function videoReferenceImageLimitForProvider(
  provider: MediaProviderSummary | undefined,
  protocols: VideoProtocolSummary[],
  mode: string,
): number | undefined {
  const protocol = videoProtocolForProvider(provider, protocols)
  const profile = videoProfileForModel(protocol, String(provider?.model_name || ""))
  return videoReferenceImageLimit(videoModeConfig(protocol, mode, profile))
}
