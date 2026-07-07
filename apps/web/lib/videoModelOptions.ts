export const VIDEO_RESOLUTION_OPTIONS = [
  { label: "480", value: "480p", placeholder: false },
  { label: "720", value: "720p", placeholder: false },
  { label: "1080", value: "1080p", placeholder: false },
  { label: "2k", value: "2k", placeholder: true },
  { label: "4k", value: "4k", placeholder: true },
] as const

export type VideoResolutionValue = (typeof VIDEO_RESOLUTION_OPTIONS)[number]["value"]

export const VIDEO_MODEL_OPTIONS = [
  {
    label: "T8 Grok Video 3",
    modelName: "grok-video-3",
    apiFormat: "t8_grok_video_3",
    supportedResolutions: ["480p", "720p", "1080p"],
  },
  {
    label: "Grok 1.5 Video 15s",
    modelName: "grok-1.5-video-15s",
    apiFormat: "grok_1_5",
    supportedResolutions: ["480p", "720p"],
  },
  {
    label: "xAI Grok Imagine Video 1.5",
    modelName: "grok-imagine-video-1.5",
    apiFormat: "xai_video",
    supportedResolutions: ["480p", "720p"],
  },
  {
    label: "Seedance 2.0 Standard",
    modelName: "doubao-seedance-2-0-260128",
    apiFormat: "volcengine_ark",
    supportedResolutions: ["480p", "720p", "1080p"],
  },
  {
    label: "Seedance 2.0 Fast",
    modelName: "doubao-seedance-2-0-fast-260128",
    apiFormat: "volcengine_ark",
    supportedResolutions: ["480p", "720p"],
  },
  {
    label: "Seedance 2.0 Mini",
    modelName: "doubao-seedance-2-0-mini-260615",
    apiFormat: "volcengine_ark",
    supportedResolutions: ["480p", "720p"],
  },
] as const

export const VIDEO_API_FORMAT_OPTIONS = [
  { label: "Lingke Media Generate", value: "lingke_media_generate" },
  { label: "T8 Grok JSON Task", value: "t8_grok_video_3" },
  { label: "Grok 1.5 Multipart", value: "grok_1_5" },
  { label: "xAI Video", value: "xai_video" },
  { label: "Volcengine Ark", value: "volcengine_ark" },
] as const

export const VIDEO_IMAGE_TRANSPORT_OPTIONS = [
  { label: "Base64 / data URL", value: "data_url" },
  { label: "公网 URL", value: "public_url" },
] as const

export function isKnownVideoModel(modelName: string): boolean {
  return VIDEO_MODEL_OPTIONS.some((item) => item.modelName === modelName)
}

export function videoApiFormatForModel(modelName: string, fallback = "volcengine_ark"): string {
  return VIDEO_MODEL_OPTIONS.find((item) => item.modelName === modelName)?.apiFormat ?? fallback
}

export function videoSupportedResolutionsForModel(modelName: string): string[] {
  const option = VIDEO_MODEL_OPTIONS.find((item) => item.modelName === modelName)
  if (option) return [...option.supportedResolutions]
  return VIDEO_RESOLUTION_OPTIONS
    .filter((item) => !item.placeholder)
    .map((item) => item.value)
}

export function defaultVideoResolutionForModel(modelName: string): string {
  const supported = videoSupportedResolutionsForModel(modelName)
  return supported.includes("720p") ? "720p" : supported[0] ?? "720p"
}
