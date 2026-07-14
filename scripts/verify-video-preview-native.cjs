#!/usr/bin/env node

const fs = require("node:fs")
const path = require("node:path")
const { chromium } = require("@playwright/test")

const WEB_URL = process.env.WEB_URL || ""
const PROJECT_ID = process.env.PROJECT_ID || ""
const NODE_ID = process.env.NODE_ID || ""
const VIDEO_URL = process.env.VIDEO_URL || ""
const SCREENSHOT_PATH = process.env.SCREENSHOT_PATH || "/tmp/openreel-native-video-preview.png"
const TIMEOUT_MS = Number(process.env.TIMEOUT_MS || 45_000)

function required(name, value) {
  if (!value) throw new Error(`Missing required environment variable: ${name}`)
  return value
}

async function main() {
  required("WEB_URL", WEB_URL)
  required("PROJECT_ID", PROJECT_ID)
  required("NODE_ID", NODE_ID)
  required("VIDEO_URL", VIDEO_URL)

  const browser = await chromium.launch({
    headless: process.env.HEADED !== "1",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    page.setDefaultTimeout(TIMEOUT_MS)
    const browserErrors = []
    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text())
    })

    const pageUrl = `${WEB_URL.replace(/\/+$/, "")}/projects/${encodeURIComponent(PROJECT_ID)}`
    await page.goto(pageUrl, { waitUntil: "domcontentloaded" })
    const panel = page.locator(".openreel-video-edit-panel")
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await page.evaluate(({ nodeId, videoUrl }) => {
        window.dispatchEvent(new CustomEvent("openreel:edit-video-node", {
          detail: { nodeId, title: "Native preview verification", videoUrl },
        }))
      }, { nodeId: NODE_ID, videoUrl: VIDEO_URL })
      if (await panel.isVisible().catch(() => false)) break
      await page.waitForTimeout(500)
    }
    if (!await panel.isVisible().catch(() => false)) {
      throw new Error(`Video editor did not open: ${JSON.stringify(browserErrors)}`)
    }
    await page.waitForFunction(() => {
      const video = document.querySelector('[data-openreel-preview-video="true"]')
      return Boolean(video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.videoWidth > 0)
    })

    const fullResolutionState = await page.evaluate(() => {
      const monitor = document.querySelector('[data-openreel-preview-pane="true"]')
      const video = document.querySelector('[data-openreel-preview-video="true"]')
      return {
        engine: monitor?.getAttribute("data-video-preview-engine") || "",
        canvasCount: document.querySelectorAll('[data-openreel-program-canvas="true"]').length,
        crossOrigin: video?.getAttribute("crossorigin"),
        readyState: Number(video?.readyState || 0),
        videoWidth: Number(video?.videoWidth || 0),
        videoHeight: Number(video?.videoHeight || 0),
      }
    })
    if (fullResolutionState.engine !== "native") {
      throw new Error(`Expected native full-resolution preview: ${JSON.stringify(fullResolutionState)}`)
    }
    if (fullResolutionState.canvasCount !== 0 || fullResolutionState.crossOrigin !== null) {
      throw new Error(`Unexpected full-resolution Canvas/CORS path: ${JSON.stringify(fullResolutionState)}`)
    }

    const before = await page.locator('[data-openreel-preview-video="true"]').evaluate((video) => video.currentTime)
    await panel.getByRole("button", { name: "播放", exact: true }).click()
    await page.waitForFunction((startTime) => {
      const video = document.querySelector('[data-openreel-preview-video="true"]')
      return Boolean(video && !video.paused && video.currentTime > startTime + 0.2)
    }, before)
    const previewVideo = page.locator('[data-openreel-preview-video="true"]')
    const playbackState = await previewVideo.evaluate((video) => ({
      currentTime: video.currentTime,
      paused: video.paused,
    }))
    const extension = path.extname(SCREENSHOT_PATH)
    const screenshotBase = extension ? SCREENSHOT_PATH.slice(0, -extension.length) : SCREENSHOT_PATH
    const monitorScreenshotPath = `${screenshotBase}-monitor${extension || ".png"}`
    fs.mkdirSync(path.dirname(SCREENSHOT_PATH), { recursive: true })
    const monitorPng = await previewVideo.screenshot({ path: monitorScreenshotPath })
    const pixelProbe = await page.evaluate(async ({ encodedPng, playback }) => {
      const image = new Image()
      image.src = `data:image/png;base64,${encodedPng}`
      await image.decode()
      const canvas = document.createElement("canvas")
      canvas.width = 32
      canvas.height = 18
      const context = canvas.getContext("2d", { willReadFrequently: true })
      if (!context) throw new Error("Canvas 2D context unavailable for screenshot pixel probe")
      context.drawImage(image, 0, 0, canvas.width, canvas.height)
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data
      let luminance = 0
      let nonBlack = 0
      for (let index = 0; index < pixels.length; index += 4) {
        const value = (pixels[index] + pixels[index + 1] + pixels[index + 2]) / 3
        luminance += value
        if (value >= 8) nonBlack += 1
      }
      return {
        averageLuminance: luminance / (pixels.length / 4),
        nonBlackRatio: nonBlack / (pixels.length / 4),
        ...playback,
      }
    }, { encodedPng: monitorPng.toString("base64"), playback: playbackState })
    if (pixelProbe.nonBlackRatio < 0.08 || pixelProbe.averageLuminance < 3) {
      throw new Error(`Preview pixel probe is black: ${JSON.stringify(pixelProbe)}`)
    }

    await page.screenshot({ path: SCREENSHOT_PATH, fullPage: false })
    process.stdout.write(`${JSON.stringify({
      ok: true,
      fullResolutionState,
      pixelProbe,
      screenshot: SCREENSHOT_PATH,
      monitorScreenshot: monitorScreenshotPath,
      browserErrors,
    }, null, 2)}\n`)
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
