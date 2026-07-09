export const VIDEO_RESOLUTION_OPTIONS = [
  { label: "480", value: "480p", placeholder: false },
  { label: "720", value: "720p", placeholder: false },
  { label: "1080", value: "1080p", placeholder: false },
  { label: "2k", value: "2k", placeholder: true },
  { label: "4k", value: "4k", placeholder: false },
] as const

export type VideoResolutionValue = (typeof VIDEO_RESOLUTION_OPTIONS)[number]["value"]

export const VIDEO_IMAGE_TRANSPORT_OPTIONS = [
  { label: "Base64 / data URL", value: "data_url" },
  { label: "公网 URL", value: "public_url" },
] as const

export function videoSupportedResolutionsForModel(_modelName: string): string[] {
  return VIDEO_RESOLUTION_OPTIONS
    .filter((item) => !item.placeholder)
    .map((item) => item.value)
}

export function defaultVideoResolutionForModel(modelName: string): string {
  const supported = videoSupportedResolutionsForModel(modelName)
  return supported.includes("720p") ? "720p" : supported[0] ?? "720p"
}
