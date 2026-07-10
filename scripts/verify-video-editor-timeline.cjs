#!/usr/bin/env node

const fs = require("node:fs")
const { chromium } = require("@playwright/test")

const env = process.env
const WEB_URL = env.WEB_URL || env.DRAMA_WEB_URL
const PROJECT_ID = env.PROJECT_ID || env.DRAMA_PROJECT_ID
const NODE_ID = env.NODE_ID || env.DRAMA_NODE_ID
const VIDEO_URL = env.VIDEO_URL || env.DRAMA_VIDEO_URL
const TIMEOUT_MS = Number(env.TIMEOUT_MS || env.VIDEO_EDITOR_VERIFY_TIMEOUT_MS || 60_000)
const SCREENSHOT_PATH = env.SCREENSHOT_PATH || ""
const HEADLESS = env.HEADED === "1" || env.HEADLESS === "0" ? false : true
const CHROME_PATH = env.CHROME_PATH || ""

function usage() {
  console.error([
    "Usage:",
    "  WEB_URL=http://127.0.0.1:3000 PROJECT_ID=<id> NODE_ID=<video-node-id> VIDEO_URL=<absolute-or-app-url> node scripts/verify-video-editor-timeline.cjs",
    "",
    "Optional:",
    "  TIMEOUT_MS=45000 SCREENSHOT_PATH=/tmp/video-editor.png HEADED=1 CHROME_PATH=/path/to/chrome",
    "",
    "This script does not start dev servers. Start web/API separately, then run it.",
  ].join("\n"))
}

function requireEnv() {
  const missing = []
  if (!WEB_URL) missing.push("WEB_URL")
  if (!PROJECT_ID) missing.push("PROJECT_ID")
  if (!NODE_ID) missing.push("NODE_ID")
  if (!VIDEO_URL) missing.push("VIDEO_URL")
  if (missing.length > 0) {
    usage()
    throw new Error(`Missing required env: ${missing.join(", ")}`)
  }
}

function closeEnough(a, b, tolerance = 4) {
  return Math.abs(a - b) <= tolerance
}

function closeTime(a, b, tolerance = 0.03) {
  return Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) <= tolerance
}

function pageUrl() {
  return `${WEB_URL.replace(/\/+$/, "")}/projects/${encodeURIComponent(PROJECT_ID)}`
}

async function readClips(page) {
  return page.evaluate(() => Array.from(document.querySelectorAll("[data-openreel-timeline-clip]")).map((el, index) => {
    const rect = el.getBoundingClientRect()
    return {
      index,
      kind: el.dataset.clipKind || "",
      clipId: el.dataset.clipId || "",
      syncGroupId: el.dataset.syncGroupId || "",
      start: Number(el.dataset.start || 0),
      duration: Number(el.dataset.duration || 0),
      sourceOffset: Number(el.dataset.sourceOffset || 0),
      sourceDuration: Number(el.dataset.sourceDuration || 0),
      text: (el.textContent || "").replace(/\s+/g, " ").trim(),
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
    }
  }))
}

function pickClipIndexes(clips) {
  const video = clips.find((clip) => clip.kind === "video")
  const audio = clips.find((clip) => clip.kind === "audio")
  if (!video || !audio) {
    throw new Error(`Could not find one video/image clip and one audio clip: ${JSON.stringify(clips)}`)
  }
  return { videoIndex: video.index, audioIndex: audio.index }
}

async function dragHorizontally(page, locator, deltaX) {
  const box = await locator.boundingBox()
  if (!box) throw new Error("Clip has no bounding box")
  const y = box.y + box.height / 2
  const x = box.x + box.width / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.move(x + deltaX, y, { steps: 6 })
  await page.mouse.up()
}

async function resizeEdge(page, locator, edge, deltaX) {
  const box = await locator.boundingBox()
  if (!box) throw new Error("Clip has no bounding box")
  const y = box.y + box.height / 2
  const x = edge === "start" ? box.x + 3 : box.x + box.width - 3
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.move(x + deltaX, y, { steps: 6 })
  await page.mouse.up()
}

async function measureAnimationFrames(page, durationMs = 1000) {
  return page.evaluate((duration) => new Promise((resolve) => {
    let frames = 0
    const startedAt = performance.now()
    const tick = (now) => {
      frames += 1
      if (now - startedAt >= duration) {
        resolve({ frames, duration: now - startedAt, fps: frames * 1000 / (now - startedAt) })
        return
      }
      requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  }), durationMs)
}

async function main() {
  requireEnv()
  if (!Number.isFinite(TIMEOUT_MS) || TIMEOUT_MS < 10_000) {
    throw new Error("TIMEOUT_MS must be at least 10000")
  }

  let browser
  const deadline = setTimeout(async () => {
    console.error(`Timed out after ${TIMEOUT_MS}ms`)
    try {
      await browser?.close()
    } finally {
      process.exit(124)
    }
  }, TIMEOUT_MS)

  const cleanup = async () => {
    clearTimeout(deadline)
    await browser?.close().catch(() => undefined)
  }

  process.once("SIGINT", async () => {
    await cleanup()
    process.exit(130)
  })
  process.once("SIGTERM", async () => {
    await cleanup()
    process.exit(143)
  })

  try {
    const launchOptions = {
      headless: HEADLESS,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if (CHROME_PATH && fs.existsSync(CHROME_PATH)) {
      launchOptions.executablePath = CHROME_PATH
    }
    browser = await chromium.launch(launchOptions)
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
    page.setDefaultTimeout(10_000)
    const consoleErrors = []
    page.on("console", (msg) => {
      if (msg.type() === "error") {
        consoleErrors.push(msg.text())
        console.error(`[browser error] ${msg.text()}`)
      }
    })

    let mockedSequenceRevision = 0
    await page.route("**/api/video-editor/**/sequence", async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      if (request.method() !== "PUT" || !url.pathname.endsWith("/sequence")) {
        await route.continue()
        return
      }
      const body = request.postDataJSON()
      mockedSequenceRevision = Math.max(mockedSequenceRevision, Number(body.expected_revision || 0)) + 1
      const now = new Date().toISOString()
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          project_id: PROJECT_ID,
          node_id: NODE_ID,
          revision: mockedSequenceRevision,
          spec: body.spec,
          created_at: now,
          updated_at: now,
        }),
      })
    })

    const nodesLoaded = page.waitForResponse((response) => (
      response.url().includes(`/api/projects/${PROJECT_ID}/nodes`) && response.ok()
    ), { timeout: 12_000 }).catch(() => null)

    await page.goto(pageUrl(), { waitUntil: "domcontentloaded", timeout: 15_000 })
    await nodesLoaded

    const mediaIndexLoaded = page.waitForResponse((response) => (
      response.ok() && response.url().includes("/api/video-editor/") && response.url().includes("/media-index")
    ), { timeout: 30_000 })
    const initialFrameTileLoaded = page.waitForResponse((response) => (
      response.ok() && response.url().includes("/api/video-editor/") && response.url().includes("/frame-tiles/")
    ), { timeout: 30_000 })
    const realWaveformLoaded = page.waitForResponse((response) => (
      response.ok() && response.url().includes("/api/video-editor/") && response.url().includes("/waveform?")
    ), { timeout: 30_000 })
    await page.evaluate(({ nodeId, videoUrl }) => {
      window.dispatchEvent(new CustomEvent("openreel:edit-video-node", {
        detail: { nodeId, title: "Video editor verification", videoUrl },
      }))
    }, { nodeId: NODE_ID, videoUrl: VIDEO_URL })

    const panel = page.locator(".openreel-video-edit-panel")
    await panel.waitFor({ state: "visible", timeout: 12_000 })
    const clipLocator = page.locator("[data-openreel-timeline-clip]")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length >= 2, null, {
      timeout: 12_000,
    })
    await page.waitForFunction(() => {
      const clips = Array.from(document.querySelectorAll("[data-openreel-timeline-clip]"))
      if (clips.length < 2) return false
      return clips.slice(0, 2).every((el) => {
        const duration = Number(el.dataset.duration || 0)
        const sourceDuration = Number(el.dataset.sourceDuration || 0)
        return sourceDuration > 0 && Math.abs(duration - sourceDuration) < 0.05
      })
    }, null, { timeout: 12_000 })
    await Promise.all([mediaIndexLoaded, initialFrameTileLoaded, realWaveformLoaded])
    await page.waitForFunction(() => {
      const indexes = Array.from(document.querySelectorAll('[data-clip-kind="video"] [data-openreel-timeline-frame]'))
        .map((element) => Number(element.dataset.frameIndex || -1))
      return new Set(indexes).size >= 6
    }, null, { timeout: 12_000 })

    let clips = await readClips(page)
    const { videoIndex, audioIndex } = pickClipIndexes(clips)
    let video = clips[videoIndex]
    let audio = clips[audioIndex]
    const aligned = (left, right) => (
      closeEnough(left.x, right.x) &&
      closeEnough(left.width, right.width) &&
      closeTime(left.start, right.start) &&
      closeTime(left.duration, right.duration) &&
      closeTime(left.sourceOffset, right.sourceOffset) &&
      left.syncGroupId && left.syncGroupId === right.syncGroupId
    )
    const initialAligned = aligned(video, audio)
    const sourceDuration = video.sourceDuration
    const videoClip = page.locator('[data-clip-kind="video"]').first()
    const audioClip = page.locator('[data-clip-kind="audio"]').first()

    await dragHorizontally(page, audioClip, 80)
    await page.waitForTimeout(250)
    clips = await readClips(page)
    video = clips.find((clip) => clip.kind === "video")
    audio = clips.find((clip) => clip.kind === "audio")
    const movedTogether = aligned(video, audio) && video.start > 0.5

    await dragHorizontally(page, videoClip, -2000)
    await page.waitForTimeout(250)
    clips = await readClips(page)
    video = clips.find((clip) => clip.kind === "video")
    audio = clips.find((clip) => clip.kind === "audio")
    const clampedAtTimelineStart = aligned(video, audio) && closeTime(video.start, 0)

    await resizeEdge(page, videoClip, "end", 2000)
    await page.waitForTimeout(200)
    clips = await readClips(page)
    video = clips.find((clip) => clip.kind === "video")
    audio = clips.find((clip) => clip.kind === "audio")
    const maxStretchBounded = aligned(video, audio) && closeTime(video.duration, sourceDuration) && video.duration <= sourceDuration + 0.03

    await resizeEdge(page, videoClip, "end", -168)
    await page.waitForTimeout(200)
    clips = await readClips(page)
    video = clips.find((clip) => clip.kind === "video")
    audio = clips.find((clip) => clip.kind === "audio")
    const trimmedTogether = aligned(video, audio) && video.duration < sourceDuration - 1

    await resizeEdge(page, audioClip, "end", 2000)
    await page.waitForTimeout(200)
    clips = await readClips(page)
    video = clips.find((clip) => clip.kind === "video")
    audio = clips.find((clip) => clip.kind === "audio")
    const restoredToSourceBound = aligned(video, audio) && closeTime(video.duration, sourceDuration)

    await resizeEdge(page, videoClip, "end", -168)
    await resizeEdge(page, videoClip, "start", 84)
    await page.waitForTimeout(200)
    clips = await readClips(page)
    video = clips.find((clip) => clip.kind === "video")
    audio = clips.find((clip) => clip.kind === "audio")
    const startTrimmedTogether = aligned(video, audio) && video.sourceOffset > 0.5 && closeTime(video.start, video.sourceOffset)

    await resizeEdge(page, audioClip, "start", -2000)
    await page.waitForTimeout(200)
    clips = await readClips(page)
    video = clips.find((clip) => clip.kind === "video")
    audio = clips.find((clip) => clip.kind === "audio")
    const sourceStartBounded = aligned(video, audio) && closeTime(video.start, 0) && closeTime(video.sourceOffset, 0)

    await panel.getByRole("button", { name: "切割", exact: true }).click()
    const splitBox = await videoClip.boundingBox()
    if (!splitBox) throw new Error("Video clip has no split target")
    await page.mouse.click(splitBox.x + splitBox.width * 0.55, splitBox.y + splitBox.height / 2)
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)
    clips = await readClips(page)
    const videoParts = clips.filter((clip) => clip.kind === "video").sort((a, b) => a.start - b.start)
    const audioParts = clips.filter((clip) => clip.kind === "audio").sort((a, b) => a.start - b.start)
    const splitSemantics = (
      videoParts.length === 2 &&
      audioParts.length === 2 &&
      aligned(videoParts[0], audioParts[0]) &&
      aligned(videoParts[1], audioParts[1]) &&
      closeTime(videoParts[0].start + videoParts[0].duration, videoParts[1].start) &&
      closeTime(videoParts[0].sourceOffset + videoParts[0].duration, videoParts[1].sourceOffset) &&
      videoParts.every((clip) => clip.sourceOffset + clip.duration <= clip.sourceDuration + 0.03)
    )

    await page.keyboard.press("Control+z")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 2)
    const undoRestoredBeforeSplit = (await readClips(page)).length === 2
    await page.keyboard.press("Control+Shift+z")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)
    const redoRestoredSplit = (await readClips(page)).length === 4

    await panel.getByRole("button", { name: "选择", exact: true }).click()
    await page.locator('[data-clip-kind="video"]').first().click()
    await page.keyboard.press("Delete")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 2)
    let deleteResult = await readClips(page)
    const normalDeleteKeepsGap = (
      deleteResult.length === 2 &&
      aligned(deleteResult.find((clip) => clip.kind === "video"), deleteResult.find((clip) => clip.kind === "audio")) &&
      deleteResult.every((clip) => clip.start > 0.5)
    )
    await page.keyboard.press("Control+z")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)

    await page.locator('[data-clip-kind="video"]').first().click()
    await page.keyboard.press("Shift+Delete")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 2)
    deleteResult = await readClips(page)
    const rippleDeleteClosesGap = (
      deleteResult.length === 2 &&
      aligned(deleteResult.find((clip) => clip.kind === "video"), deleteResult.find((clip) => clip.kind === "audio")) &&
      deleteResult.every((clip) => closeTime(clip.start, 0) && clip.sourceOffset > 0.5)
    )
    await page.keyboard.press("Control+z")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)

    const timeline = page.locator('[data-openreel-timeline-scroll="true"]')
    const timelineBox = await timeline.boundingBox()
    if (!timelineBox) throw new Error("Timeline has no bounding box")
    const anchorX = timelineBox.x + timelineBox.width * 0.5
    const readZoomAnchor = () => page.evaluate((clientX) => {
      const element = document.querySelector('[data-openreel-timeline-scroll="true"]')
      const rect = element.getBoundingClientRect()
      const pxPerSecond = Number(element.dataset.pxPerSecond || 0)
      const labelWidth = Number(element.dataset.trackLabelWidth || 0)
      return {
        pxPerSecond,
        time: (clientX - rect.left + element.scrollLeft - labelWidth) / pxPerSecond,
      }
    }, anchorX)
    const zoomBefore = await readZoomAnchor()
    await page.mouse.move(anchorX, timelineBox.y + 80)
    await page.mouse.wheel(0, -360)
    await page.waitForTimeout(250)
    const zoomAfter = await readZoomAnchor()
    const zoomExpanded = zoomAfter.pxPerSecond > zoomBefore.pxPerSecond * 1.5
    const zoomAnchorStable = Math.abs(zoomAfter.time - zoomBefore.time) < 0.08
    await page.mouse.wheel(0, -1400)
    await page.waitForFunction(() => (
      Array.from(document.querySelectorAll('[data-clip-kind="video"] [data-openreel-frame-strip]'))
        .every((element) => element.dataset.everyFrame === "true")
    ), null, { timeout: 12_000 })
    await page.waitForFunction(() => {
      const waveforms = Array.from(document.querySelectorAll("[data-openreel-real-waveform]"))
      return waveforms.length >= 2 && waveforms.every((element) => Number(element.dataset.waveformBuckets || 0) > 0)
    }, null, { timeout: 12_000 })
    await page.waitForTimeout(250)
    const frameDetail = await page.evaluate(() => {
      const videoClips = Array.from(document.querySelectorAll('[data-clip-kind="video"]'))
      const perClip = videoClips.map((clip) => Array.from(clip.querySelectorAll("[data-openreel-timeline-frame]"))
        .map((element) => Number(element.dataset.frameIndex || -1)))
      const all = perClip.flat()
      return {
        uniqueFrames: new Set(all).size,
        leftMax: perClip[0]?.length ? Math.max(...perClip[0]) : -1,
        rightMin: perClip[1]?.length ? Math.min(...perClip[1]) : -1,
        everyFrame: videoClips.every((clip) => clip.querySelector("[data-openreel-frame-strip]")?.dataset.everyFrame === "true"),
        realWaveforms: Array.from(document.querySelectorAll("[data-openreel-real-waveform]"))
          .filter((element) => Number(element.dataset.waveformBuckets || 0) > 0).length,
      }
    })
    const detailedFramesVisible = frameDetail.everyFrame && frameDetail.uniqueFrames >= 200 && frameDetail.leftMax < frameDetail.rightMin
    const realWaveformsVisible = frameDetail.realWaveforms >= 2
    const layout = await page.evaluate(() => {
      const rect = (selector) => {
        const box = document.querySelector(selector)?.getBoundingClientRect()
        return box ? { x: box.x, y: box.y, width: box.width, height: box.height } : null
      }
      return {
        panel: rect(".openreel-video-edit-panel"),
        mediaBin: rect('[data-openreel-media-bin="true"]'),
        preview: rect('[data-openreel-preview-pane="true"]'),
        inspector: rect('[data-openreel-inspector-pane="true"]'),
        timeline: rect('[data-openreel-timeline-scroll="true"]'),
        previewVideo: rect('[data-openreel-preview-video="true"]'),
      }
    })
    const layoutSupportsTracks = Boolean(
      layout.panel && layout.mediaBin && layout.preview && layout.inspector && layout.timeline &&
      layout.mediaBin.x < layout.preview.x && layout.preview.x < layout.inspector.x &&
      layout.timeline.height > layout.preview.height,
    )

    const baselineProbe = await measureAnimationFrames(page)

    await page.evaluate(() => {
      window.__openreelFrameProbe = { frames: 0, startedAt: performance.now(), finishedAt: 0 }
      window.__openreelLongTasks = []
      window.__openreelLongTaskObserver = new PerformanceObserver((list) => {
        window.__openreelLongTasks.push(...list.getEntries().map((entry) => entry.duration))
      })
      window.__openreelLongTaskObserver.observe({ type: "longtask" })
      const tick = () => {
        const probe = window.__openreelFrameProbe
        if (!probe || performance.now() - probe.startedAt >= 1000) {
          if (probe) probe.finishedAt = performance.now()
          return
        }
        probe.frames += 1
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    await panel.getByRole("button", { name: "播放", exact: true }).click()
    await page.waitForTimeout(1150)
    const playbackProbe = await page.evaluate(() => window.__openreelFrameProbe)
    const playbackQuality = await page.evaluate(() => {
      window.__openreelLongTaskObserver?.disconnect()
      const video = document.querySelector("[data-openreel-preview-video]")
      const quality = video && typeof video.getVideoPlaybackQuality === "function"
        ? video.getVideoPlaybackQuality()
        : null
      return {
        longTasks: window.__openreelLongTasks || [],
        totalVideoFrames: quality?.totalVideoFrames || 0,
        droppedVideoFrames: quality?.droppedVideoFrames || 0,
      }
    })
    const playbackFps = playbackProbe && playbackProbe.finishedAt > playbackProbe.startedAt
      ? playbackProbe.frames * 1000 / (playbackProbe.finishedAt - playbackProbe.startedAt)
      : 0
    const baselineFps = baselineProbe.fps || 0
    const longTaskTotalMs = playbackQuality.longTasks.reduce((sum, value) => sum + value, 0)
    const playbackResponsive = longTaskTotalMs < 250 && playbackQuality.droppedVideoFrames <= Math.max(2, playbackQuality.totalVideoFrames * 0.12)
    const pauseButton = panel.getByRole("button", { name: "暂停", exact: true })
    if (await pauseButton.isVisible().catch(() => false)) await pauseButton.click()

    if (SCREENSHOT_PATH) {
      await page.screenshot({ path: SCREENSHOT_PATH, fullPage: false })
    }

    const result = {
      ok: initialAligned && movedTogether && clampedAtTimelineStart && maxStretchBounded && trimmedTogether && restoredToSourceBound && startTrimmedTogether && sourceStartBounded && splitSemantics && undoRestoredBeforeSplit && redoRestoredSplit && normalDeleteKeepsGap && rippleDeleteClosesGap && zoomExpanded && zoomAnchorStable && detailedFramesVisible && realWaveformsVisible && layoutSupportsTracks && playbackResponsive && consoleErrors.length === 0,
      initialAligned,
      movedTogether,
      clampedAtTimelineStart,
      maxStretchBounded,
      trimmedTogether,
      restoredToSourceBound,
      startTrimmedTogether,
      sourceStartBounded,
      splitSemantics,
      undoRestoredBeforeSplit,
      redoRestoredSplit,
      normalDeleteKeepsGap,
      rippleDeleteClosesGap,
      zoomExpanded,
      zoomAnchorStable,
      zoomBefore,
      zoomAfter,
      detailedFramesVisible,
      realWaveformsVisible,
      frameDetail,
      layoutSupportsTracks,
      layout,
      baselineFps: Number(baselineFps.toFixed(1)),
      playbackFps: Number(playbackFps.toFixed(1)),
      playbackResponsive,
      longTaskCount: playbackQuality.longTasks.length,
      longTaskTotalMs: Number(longTaskTotalMs.toFixed(1)),
      totalVideoFrames: playbackQuality.totalVideoFrames,
      droppedVideoFrames: playbackQuality.droppedVideoFrames,
      sourceDuration,
      videoParts,
      audioParts,
      consoleErrors,
      screenshot: SCREENSHOT_PATH || null,
    }
    console.log(JSON.stringify(result, null, 2))
    if (!result.ok) process.exitCode = 1
  } finally {
    await cleanup()
  }
}

main().catch(async (error) => {
  console.error(error && error.stack ? error.stack : String(error))
  process.exit(1)
})
