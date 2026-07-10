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

function clipEnd(clip) {
  return clip.startFrame + clip.durationFrames
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
      mediaId: el.dataset.mediaId || "",
      trackId: el.dataset.trackId || "",
      syncGroupId: el.dataset.syncGroupId || "",
      start: Number(el.dataset.start || 0),
      duration: Number(el.dataset.duration || 0),
      sourceOffset: Number(el.dataset.sourceOffset || 0),
      sourceDuration: Number(el.dataset.sourceDuration || 0),
      startFrame: Number(el.dataset.startFrame || 0),
      durationFrames: Number(el.dataset.durationFrames || 0),
      sourceInFrame: Number(el.dataset.sourceInFrame || 0),
      fadeInFrames: Number(el.dataset.fadeInFrames || 0),
      fadeOutFrames: Number(el.dataset.fadeOutFrames || 0),
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

async function dragClipToTrack(page, locator, targetTrackId, deltaX = 0) {
  const box = await locator.boundingBox()
  const target = await page.locator(`[data-openreel-track-id="${targetTrackId}"]`).boundingBox()
  if (!box || !target) throw new Error(`Could not drag clip to ${targetTrackId}`)
  const x = box.x + box.width / 2
  await page.mouse.move(x, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(x + deltaX, target.y + target.height / 2, { steps: 10 })
  await page.mouse.up()
}

async function probeSnapGuide(page, locator, deltaX) {
  const box = await locator.boundingBox()
  if (!box) throw new Error("Clip has no snap probe box")
  const x = box.x + box.width / 2
  const y = box.y + box.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.move(x + deltaX, y, { steps: 5 })
  const guide = await page.locator('[data-openreel-snap-guide="true"]').count()
  const frame = await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-snap-guide-frame")
  await page.mouse.up()
  return { visible: guide > 0, frame: Number(frame || -1) }
}

async function marqueeSelectSecondCut(page) {
  const timeline = page.locator('[data-openreel-timeline-scroll="true"]')
  const secondVideo = page.locator('[data-clip-kind="video"]').nth(1)
  const secondAudio = page.locator('[data-clip-kind="audio"]').nth(1)
  const timelineBox = await timeline.boundingBox()
  const videoBox = await secondVideo.boundingBox()
  const audioBox = await secondAudio.boundingBox()
  if (!timelineBox || !videoBox || !audioBox) throw new Error("Marquee targets unavailable")
  const startX = Math.min(timelineBox.x + timelineBox.width - 20, videoBox.x + videoBox.width + 80)
  const startY = videoBox.y + videoBox.height / 2
  const endX = videoBox.x + 12
  const endY = audioBox.y + audioBox.height - 4
  await page.mouse.move(startX, startY)
  await page.mouse.down()
  await page.mouse.move(endX, endY, { steps: 8 })
  const visible = await page.locator('[data-openreel-marquee="true"]').isVisible().catch(() => false)
  await page.mouse.up()
  return visible
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
    let latestSequenceSpec = null
    await page.route("**/api/video-editor/**/sequence", async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      if (!url.pathname.endsWith("/sequence")) {
        await route.continue()
        return
      }
      if (request.method() === "GET") {
        if (!latestSequenceSpec) {
          await route.fulfill({ status: 200, contentType: "application/json", body: "null" })
          return
        }
        const now = new Date().toISOString()
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            project_id: PROJECT_ID,
            node_id: NODE_ID,
            revision: mockedSequenceRevision,
            spec: latestSequenceSpec,
            created_at: now,
            updated_at: now,
          }),
        })
        return
      }
      if (request.method() !== "PUT") {
        await route.continue()
        return
      }
      const body = request.postDataJSON()
      latestSequenceSpec = body.spec
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
      left.startFrame === right.startFrame &&
      left.durationFrames === right.durationFrames &&
      left.sourceInFrame === right.sourceInFrame &&
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
    await page.waitForTimeout(500)
    const splitClipCount = await page.locator("[data-openreel-timeline-clip]").count()
    if (splitClipCount !== 4) throw new Error(`Expected four clips after split, got ${splitClipCount}: ${JSON.stringify(await readClips(page))}`)
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
      videoParts[0].startFrame + videoParts[0].durationFrames === videoParts[1].startFrame &&
      videoParts[0].sourceInFrame + videoParts[0].durationFrames === videoParts[1].sourceInFrame &&
      videoParts.every((clip) => clip.sourceOffset + clip.duration <= clip.sourceDuration + 0.03)
    )

    await page.keyboard.press("Control+z")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 2)
    const undoRestoredBeforeSplit = (await readClips(page)).length === 2
    await page.keyboard.press("Control+Shift+z")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)
    const redoRestoredSplit = (await readClips(page)).length === 4

    await panel.getByRole("button", { name: "选择 (V)", exact: true }).click()
    await page.keyboard.press("b")
    const rippleModeActivated = await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-trim-mode") === "ripple"
    await resizeEdge(page, page.locator('[data-clip-kind="video"]').first(), "end", -84)
    await page.waitForTimeout(250)
    clips = await readClips(page)
    let trimmedVideoParts = clips.filter((clip) => clip.kind === "video").sort((a, b) => a.startFrame - b.startFrame)
    let trimmedAudioParts = clips.filter((clip) => clip.kind === "audio").sort((a, b) => a.startFrame - b.startFrame)
    const rippleDeltaFrames = trimmedVideoParts[0].durationFrames - videoParts[0].durationFrames
    const rippleTrimSemantics = (
      rippleModeActivated &&
      rippleDeltaFrames < 0 &&
      trimmedVideoParts[0].startFrame + trimmedVideoParts[0].durationFrames === trimmedVideoParts[1].startFrame &&
      trimmedVideoParts[1].startFrame === videoParts[1].startFrame + rippleDeltaFrames &&
      trimmedVideoParts[1].sourceInFrame === videoParts[1].sourceInFrame &&
      aligned(trimmedVideoParts[0], trimmedAudioParts[0]) &&
      aligned(trimmedVideoParts[1], trimmedAudioParts[1])
    )
    await page.keyboard.press("Control+z")
    await page.waitForTimeout(500)
    const rippleUndoState = await readClips(page)
    if (rippleUndoState.find((clip) => clip.kind === "video")?.durationFrames !== videoParts[0].durationFrames) {
      throw new Error(`Ripple undo mismatch: ${JSON.stringify(rippleUndoState)}`)
    }

    await resizeEdge(page, page.locator('[data-clip-kind="video"]').nth(1), "start", 84)
    await page.waitForTimeout(250)
    clips = await readClips(page)
    trimmedVideoParts = clips.filter((clip) => clip.kind === "video").sort((a, b) => a.startFrame - b.startFrame)
    trimmedAudioParts = clips.filter((clip) => clip.kind === "audio").sort((a, b) => a.startFrame - b.startFrame)
    const rippleIncomingSourceDelta = trimmedVideoParts[1].sourceInFrame - videoParts[1].sourceInFrame
    const rippleIncomingTrimSemantics = (
      rippleIncomingSourceDelta > 0 &&
      trimmedVideoParts[0].durationFrames === videoParts[0].durationFrames &&
      trimmedVideoParts[1].startFrame === videoParts[1].startFrame &&
      trimmedVideoParts[1].durationFrames === videoParts[1].durationFrames - rippleIncomingSourceDelta &&
      aligned(trimmedVideoParts[1], trimmedAudioParts[1])
    )
    await page.keyboard.press("Control+z")
    await page.waitForTimeout(500)
    const rippleIncomingUndoState = await readClips(page)
    if (rippleIncomingUndoState.filter((clip) => clip.kind === "video")[1]?.durationFrames !== videoParts[1].durationFrames) {
      throw new Error(`Ripple incoming undo mismatch: ${JSON.stringify(rippleIncomingUndoState)}`)
    }

    await page.keyboard.press("n")
    const rollingModeActivated = await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-trim-mode") === "rolling"
    await resizeEdge(page, page.locator('[data-clip-kind="video"]').first(), "end", 42)
    await page.waitForTimeout(250)
    clips = await readClips(page)
    trimmedVideoParts = clips.filter((clip) => clip.kind === "video").sort((a, b) => a.startFrame - b.startFrame)
    trimmedAudioParts = clips.filter((clip) => clip.kind === "audio").sort((a, b) => a.startFrame - b.startFrame)
    const rollingDeltaFrames = trimmedVideoParts[0].durationFrames - videoParts[0].durationFrames
    const originalSequenceEndFrame = videoParts[1].startFrame + videoParts[1].durationFrames
    const rollingTrimSemantics = (
      rollingModeActivated &&
      rollingDeltaFrames > 0 &&
      trimmedVideoParts[0].startFrame + trimmedVideoParts[0].durationFrames === trimmedVideoParts[1].startFrame &&
      trimmedVideoParts[1].sourceInFrame === videoParts[1].sourceInFrame + rollingDeltaFrames &&
      trimmedVideoParts[1].durationFrames === videoParts[1].durationFrames - rollingDeltaFrames &&
      trimmedVideoParts[1].startFrame + trimmedVideoParts[1].durationFrames === originalSequenceEndFrame &&
      aligned(trimmedVideoParts[0], trimmedAudioParts[0]) &&
      aligned(trimmedVideoParts[1], trimmedAudioParts[1])
    )
    await page.keyboard.press("Control+z")
    await page.waitForFunction((expectedFrame) => (
      Number(document.querySelector('[data-clip-kind="video"]')?.dataset.durationFrames || 0) === expectedFrame
    ), videoParts[0].durationFrames)

    await resizeEdge(page, page.locator('[data-clip-kind="video"]').nth(1), "start", -42)
    await page.waitForTimeout(250)
    clips = await readClips(page)
    trimmedVideoParts = clips.filter((clip) => clip.kind === "video").sort((a, b) => a.startFrame - b.startFrame)
    trimmedAudioParts = clips.filter((clip) => clip.kind === "audio").sort((a, b) => a.startFrame - b.startFrame)
    const rollingIncomingDeltaFrames = trimmedVideoParts[1].startFrame - videoParts[1].startFrame
    const rollingIncomingTrimSemantics = (
      rollingIncomingDeltaFrames < 0 &&
      trimmedVideoParts[0].durationFrames === videoParts[0].durationFrames + rollingIncomingDeltaFrames &&
      trimmedVideoParts[0].startFrame + trimmedVideoParts[0].durationFrames === trimmedVideoParts[1].startFrame &&
      trimmedVideoParts[1].sourceInFrame === videoParts[1].sourceInFrame + rollingIncomingDeltaFrames &&
      trimmedVideoParts[1].durationFrames === videoParts[1].durationFrames - rollingIncomingDeltaFrames &&
      trimmedVideoParts[1].startFrame + trimmedVideoParts[1].durationFrames === originalSequenceEndFrame &&
      aligned(trimmedVideoParts[0], trimmedAudioParts[0]) &&
      aligned(trimmedVideoParts[1], trimmedAudioParts[1])
    )
    await page.keyboard.press("Control+z")
    await page.waitForFunction((expectedFrame) => (
      Number(document.querySelectorAll('[data-clip-kind="video"]')[1]?.dataset.startFrame || 0) === expectedFrame
    ), videoParts[1].startFrame)

    await page.keyboard.press("v")
    await page.locator('[data-clip-kind="video"]').nth(1).click()
    const exactDurationFrames = videoParts[1].durationFrames - 10
    await page.getByLabel("片段持续帧").fill(String(exactDurationFrames))
    await page.waitForTimeout(800)
    clips = await readClips(page)
    trimmedVideoParts = clips.filter((clip) => clip.kind === "video").sort((a, b) => a.startFrame - b.startFrame)
    trimmedAudioParts = clips.filter((clip) => clip.kind === "audio").sort((a, b) => a.startFrame - b.startFrame)
    const exactPersistedClip = latestSequenceSpec?.clips?.find((clip) => clip.id === trimmedVideoParts[1].clipId)
    const exactFrameInputs = (
      trimmedVideoParts[1].durationFrames === exactDurationFrames &&
      trimmedVideoParts[1].startFrame === videoParts[1].startFrame &&
      trimmedVideoParts[1].sourceInFrame === videoParts[1].sourceInFrame &&
      aligned(trimmedVideoParts[1], trimmedAudioParts[1]) &&
      exactPersistedClip?.duration_frames === exactDurationFrames
    )
    await panel.getByRole("button", { name: "选择 (V)", exact: true }).click()
    await page.keyboard.press("Control+z")
    await page.waitForFunction((expectedFrame) => (
      Number(document.querySelectorAll('[data-clip-kind="video"]')[1]?.dataset.durationFrames || 0) === expectedFrame
    ), videoParts[1].durationFrames)

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
    await page.locator('[data-clip-kind="audio"]').first().click()
    await page.getByLabel("音频轨道音量 A1", { exact: true }).fill("-6")
    await page.getByRole("button", { name: "独奏轨道 A1", exact: true }).click()
    await page.getByLabel("淡入时长").fill("24")
    await page.waitForTimeout(900)
    const audioControls = await page.evaluate(() => {
      const audioClip = document.querySelector('[data-clip-kind="audio"]')
      const waveform = audioClip?.querySelector('[data-openreel-real-waveform]')
      const trackGain = document.querySelector('[data-openreel-track-gain="true"]')
      const trackSolo = document.querySelector('[data-openreel-track-solo="true"]')
      return {
        trackGainDb: Number(trackGain?.value || 0),
        trackSolo: trackSolo?.getAttribute("aria-label") === "取消独奏轨道 A1",
        fadeInFrames: Number(audioClip?.dataset.fadeInFrames || 0),
        waveformGainDb: Number(waveform?.dataset.waveformGainDb || 0),
        waveformFadeIn: Number(waveform?.dataset.waveformFadeIn || 0),
      }
    })
    const persistedAudioTrack = latestSequenceSpec?.tracks?.find((track) => track.id === "a1")
    const persistedAudioClip = latestSequenceSpec?.clips?.find((clip) => clip.track_id === "a1" && clip.source_in_frame === 0)
    const audioControlsPersisted = (
      audioControls.trackGainDb === -6 &&
      audioControls.trackSolo &&
      audioControls.fadeInFrames === 24 &&
      audioControls.waveformGainDb === -6 &&
      audioControls.waveformFadeIn > 0.9 &&
      persistedAudioTrack?.gain_db === -6 &&
      persistedAudioTrack?.solo === true &&
      persistedAudioClip?.fade_in_frames === 24
    )
    const initialTimelineScale = Number(await timeline.getAttribute("data-px-per-second"))
    const timelineLabelWidth = Number(await timeline.getAttribute("data-track-label-width"))
    const seekWithTimelineRuler = async (seconds) => {
      const scrollLeft = await timeline.evaluate((element) => element.scrollLeft)
      await page.mouse.click(
        timelineBox.x + timelineLabelWidth + seconds * initialTimelineScale - scrollLeft,
        timelineBox.y + 12,
      )
      await page.waitForTimeout(250)
      return page.evaluate(() => Number(document.querySelector("[data-openreel-preview-video]")?.volume || 0))
    }
    const fadeStartVolume = await seekWithTimelineRuler(0.5)
    const fadeStartState = await page.evaluate(() => {
      const clip = document.querySelector('[data-clip-kind="audio"]')
      const video = document.querySelector("[data-openreel-preview-video]")
      return {
        fadeInFrames: Number(clip?.dataset.fadeInFrames || 0),
        videoCurrentTime: Number(video?.currentTime || 0),
        volume: Number(video?.volume || 0),
        muted: Boolean(video?.muted),
      }
    })
    const fullGainVolume = await seekWithTimelineRuler(1.5)
    const audioPreviewMixApplied = (
      fadeStartVolume > 0 &&
      fadeStartVolume < fullGainVolume * 0.75 &&
      Math.abs(fullGainVolume - Math.pow(10, -6 / 20)) < 0.03
    )
    await page.locator('[data-clip-kind="audio"]').first().click()
    await page.evaluate(() => document.activeElement?.blur())
    await page.keyboard.press("g")
    const audioGainShortcut = await page.evaluate(() => document.activeElement?.matches('[data-openreel-clip-gain="true"]') === true)
    await panel.getByText("OpenReel Edit", { exact: true }).click()

    const sourceVideoItem = page.locator('[data-openreel-media-item="true"][data-media-type="video"]').first()
    await sourceVideoItem.click()
    await page.getByLabel("源监视器播放头", { exact: true }).fill("24")
    await panel.getByText("OpenReel Edit", { exact: true }).click()
    await page.keyboard.press("i")
    await page.getByLabel("源监视器播放头", { exact: true }).fill("119")
    await panel.getByText("OpenReel Edit", { exact: true }).click()
    await page.keyboard.press("o")
    const sourceMarksApplied = await page.evaluate(() => {
      const monitor = document.querySelector('[data-openreel-source-monitor="true"]')
      return monitor?.getAttribute("data-source-in-frame") === "24" &&
        monitor?.getAttribute("data-source-out-frame") === "120" &&
        monitor?.getAttribute("data-source-cursor-frame") === "119"
    })

    await panel.getByRole("button", { name: "添加视频轨道", exact: true }).click()
    await panel.getByRole("button", { name: "添加音频轨道", exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('[data-openreel-track-row="true"]').length === 4)
    await page.getByLabel("重命名轨道 V2", { exact: true }).fill("补充画面")
    await page.getByLabel("重命名轨道 A2", { exact: true }).fill("补充声音")
    await page.waitForTimeout(800)
    const dynamicTracksPersisted = (
      latestSequenceSpec?.tracks?.length === 4 &&
      latestSequenceSpec.tracks.some((track) => track.id === "v2" && track.name === "补充画面") &&
      latestSequenceSpec.tracks.some((track) => track.id === "a2" && track.name === "补充声音")
    )

    const moveBefore = (await readClips(page)).find((clip) => clip.kind === "video" && clip.trackId === "v1")
    await dragClipToTrack(page, page.locator('[data-clip-kind="video"][data-track-id="v1"]').first(), "v2")
    await page.waitForFunction(() => document.querySelector('[data-clip-kind="video"]')?.dataset.trackId === "v2")
    let multiTrackClips = await readClips(page)
    const movedAcrossTrack = multiTrackClips.find((clip) => clip.clipId === moveBefore?.clipId)
    const linkedAfterTrackMove = multiTrackClips.find((clip) => clip.kind === "audio" && clip.syncGroupId === movedAcrossTrack?.syncGroupId)
    const crossTrackMovePreservedSource = Boolean(
      moveBefore && movedAcrossTrack && linkedAfterTrackMove &&
      movedAcrossTrack.trackId === "v2" &&
      movedAcrossTrack.sourceInFrame === moveBefore.sourceInFrame &&
      movedAcrossTrack.durationFrames === moveBefore.durationFrames &&
      movedAcrossTrack.startFrame === linkedAfterTrackMove.startFrame &&
      movedAcrossTrack.sourceInFrame === linkedAfterTrackMove.sourceInFrame
    )
    await panel.getByRole("button", { name: "选择 (V)", exact: true }).click()
    await page.keyboard.press("Control+z")
    await page.waitForFunction((clipId) => document.querySelector(`[data-clip-id="${clipId}"]`)?.dataset.trackId === "v1", moveBefore.clipId)

    await panel.getByRole("button", { name: "目标轨道 V2", exact: true }).click()
    await panel.getByRole("button", { name: "目标轨道 A2", exact: true }).click()
    await page.locator('[data-openreel-media-item="true"][data-media-type="video"]').first().click()
    const insertFrame = Math.round(2 * videoParts[0].durationFrames / videoParts[0].duration)
    await seekWithTimelineRuler(2)
    await page.keyboard.press(",")
    await page.waitForTimeout(800)
    multiTrackClips = await readClips(page)
    const insertedVideo = multiTrackClips.find((clip) => clip.kind === "video" && clip.trackId === "v2")
    const insertedAudio = multiTrackClips.find((clip) => clip.kind === "audio" && clip.trackId === "a2")
    const shiftedVideo = multiTrackClips.find((clip) => (
      clip.kind === "video" && clip.trackId === "v1" && clip.sourceInFrame === videoParts[1].sourceInFrame
    ))
    const splitRightVideo = multiTrackClips.find((clip) => (
      clip.kind === "video" && clip.trackId === "v1" && clip.sourceInFrame === videoParts[0].sourceInFrame + insertFrame
    ))
    const splitRightAudio = multiTrackClips.find((clip) => (
      clip.kind === "audio" && clip.syncGroupId === splitRightVideo?.syncGroupId
    ))
    const insertEditSemantics = Boolean(
      insertedVideo && insertedAudio && shiftedVideo && splitRightVideo && splitRightAudio &&
      aligned(insertedVideo, insertedAudio) &&
      insertedVideo.startFrame === insertFrame &&
      insertedVideo.sourceInFrame === 24 &&
      insertedVideo.durationFrames === 96 &&
      aligned(splitRightVideo, splitRightAudio) &&
      splitRightVideo.startFrame === insertFrame + insertedVideo.durationFrames &&
      shiftedVideo.startFrame === videoParts[1].startFrame + insertedVideo.durationFrames &&
      latestSequenceSpec?.clips?.some((clip) => clip.id === insertedVideo.clipId && clip.track_id === "v2")
    )
    if (insertedVideo || insertedAudio) {
      await page.keyboard.press("Control+z")
      await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)
    }

    await panel.getByRole("button", { name: "目标轨道 V1", exact: true }).click()
    const imageMedia = page.locator('[data-openreel-media-item="true"][data-media-type="image"]').first()
    const imageMediaId = await imageMedia.getAttribute("data-media-id")
    await imageMedia.click()
    await seekWithTimelineRuler(2)
    await page.keyboard.press(".")
    await page.waitForFunction((mediaId) => Boolean(document.querySelector(`[data-media-id="${mediaId}"][data-clip-kind="video"]`)), imageMediaId)
    await page.waitForTimeout(800)
    multiTrackClips = await readClips(page)
    const overwriteClip = multiTrackClips.find((clip) => clip.mediaId === imageMediaId && clip.trackId === "v1")
    const overwrittenSourceParts = multiTrackClips
      .filter((clip) => clip.kind === "video" && clip.trackId === "v1" && clip.mediaId === videoParts[0].mediaId)
      .sort((left, right) => left.startFrame - right.startFrame)
    const overwriteEditSemantics = Boolean(
      overwriteClip && overwrittenSourceParts.length === 3 &&
      overwrittenSourceParts[0].startFrame === 0 &&
      clipEnd(overwrittenSourceParts[0]) === overwriteClip.startFrame &&
      overwrittenSourceParts[1].startFrame === overwriteClip.startFrame + overwriteClip.durationFrames &&
      overwrittenSourceParts[1].sourceInFrame === videoParts[0].sourceInFrame + overwrittenSourceParts[1].startFrame &&
      overwrittenSourceParts[2].startFrame === videoParts[1].startFrame &&
      latestSequenceSpec?.clips?.some((clip) => clip.id === overwriteClip.clipId && clip.track_id === "v1")
    )
    await page.keyboard.press("Control+z")
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)

    await page.getByRole("button", { name: "锁定轨道 V2", exact: true }).click()
    await page.getByRole("button", { name: "隐藏轨道 V2", exact: true }).click()
    await page.getByRole("button", { name: "关闭同步锁 A2", exact: true }).click()
    await page.getByRole("button", { name: "静音轨道 A2", exact: true }).click()
    await page.getByRole("button", { name: "上移轨道 V1", exact: true }).click()
    await page.waitForTimeout(800)
    const trackControlsPersisted = (
      latestSequenceSpec?.tracks?.find((track) => track.id === "v2")?.locked === true &&
      latestSequenceSpec?.tracks?.find((track) => track.id === "v2")?.visible === false &&
      latestSequenceSpec?.tracks?.find((track) => track.id === "a2")?.sync_locked === false &&
      latestSequenceSpec?.tracks?.find((track) => track.id === "a2")?.muted === true &&
      latestSequenceSpec?.tracks?.find((track) => track.id === "v1")?.order === 1
    )
    const lockedMoveBefore = (await readClips(page)).find((clip) => clip.kind === "video" && clip.trackId === "v1")
    await dragClipToTrack(page, page.locator('[data-clip-kind="video"][data-track-id="v1"]').first(), "v2")
    await page.waitForTimeout(250)
    const lockedTrackRejectedMove = (await readClips(page))
      .find((clip) => clip.clipId === lockedMoveBefore?.clipId)?.trackId === "v1"

    await panel.getByRole("button", { name: "添加视频轨道", exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('[data-openreel-track-row="true"]').length === 5)
    await page.getByRole("button", { name: "删除轨道 V3", exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('[data-openreel-track-row="true"]').length === 4)
    await page.keyboard.press("Control+z")
    await page.waitForFunction(() => document.querySelectorAll('[data-openreel-track-row="true"]').length === 5)
    await page.keyboard.press("Control+Shift+z")
    await page.waitForFunction(() => document.querySelectorAll('[data-openreel-track-row="true"]').length === 4)
    const dynamicTrackHistory = true

    await panel.getByRole("button", { name: "选择 (V)", exact: true }).click()
    await page.locator('[data-clip-kind="video"]').first().click()
    const linkedSelection = await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-selected-clip-count") === "2"
    await page.locator('[data-clip-kind="video"]').first().click({ modifiers: ["Alt"] })
    const independentSelection = (
      await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-selected-clip-count") === "1" &&
      await page.locator('[data-clip-kind="video"]').first().getAttribute("data-selected") === "true" &&
      await page.locator('[data-clip-kind="audio"]').first().getAttribute("data-selected") === "false"
    )
    await page.keyboard.down("Alt")
    await dragHorizontally(page, page.locator('[data-clip-kind="video"]').first(), 42)
    await page.keyboard.up("Alt")
    await page.waitForTimeout(200)
    const independentMoveClips = await readClips(page)
    const independentMove = (
      independentMoveClips.find((clip) => clip.kind === "video")?.startFrame > 0 &&
      independentMoveClips.find((clip) => clip.kind === "audio")?.startFrame === 0
    )
    await page.keyboard.press("Control+z")
    await page.waitForFunction(() => (
      document.querySelectorAll('[data-clip-kind="video"]').length === 2 &&
      Number(document.querySelector('[data-clip-kind="video"]')?.dataset.startFrame || -1) === 0 &&
      Number(document.querySelector('[data-clip-kind="audio"]')?.dataset.startFrame || -1) === 0
    ))
    await page.locator('[data-clip-kind="video"]').first().click({ modifiers: ["Alt"] })
    await page.locator('[data-clip-kind="video"]').nth(1).click({ modifiers: ["Control"] })
    const additiveSelection = await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-selected-clip-count") === "3"
    const marqueeVisible = await marqueeSelectSecondCut(page)
    await page.waitForTimeout(100)
    const marqueeSelection = (
      marqueeVisible &&
      await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-selected-clip-count") === "2" &&
      await page.locator('[data-clip-kind="video"]').nth(1).getAttribute("data-selected") === "true" &&
      await page.locator('[data-clip-kind="audio"]').nth(1).getAttribute("data-selected") === "true"
    )
    await page.keyboard.press("s")
    const snappingDisabled = await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-snapping-enabled") === "false"
    await page.keyboard.press("s")
    const snappingEnabled = await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-snapping-enabled") === "true"
    await page.locator('[data-clip-kind="video"]').first().click()
    const snapGuide = await probeSnapGuide(page, page.locator('[data-clip-kind="video"]').first(), 5)
    const visibleSnapGuide = snapGuide.visible && [0, videoParts[1].startFrame].includes(snapGuide.frame)
    const snapGuideCleared = await page.locator('[data-openreel-snap-guide="true"]').count() === 0

    await page.keyboard.press("ArrowUp")
    const previousEditFrame = Number(await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-current-frame"))
    await page.keyboard.press("ArrowDown")
    const nextEditFrame = Number(await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-current-frame"))
    const editPointNavigation = previousEditFrame === 0 && nextEditFrame === videoParts[1].startFrame
    await page.keyboard.press("j")
    await page.waitForTimeout(260)
    await page.keyboard.press("k")
    const reverseShuttleFrame = Number(await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-current-frame"))
    await page.keyboard.press("l")
    await page.waitForTimeout(260)
    await page.keyboard.press("k")
    const forwardShuttleFrame = Number(await page.locator('[data-openreel-timeline-scroll="true"]').getAttribute("data-current-frame"))
    const shuttleShortcuts = reverseShuttleFrame < nextEditFrame && forwardShuttleFrame > reverseShuttleFrame

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
      const strips = videoClips.map((clip) => clip.querySelector("[data-openreel-frame-strip]"))
      const sourceFramesPerClip = videoClips.map((clip) => Array.from(clip.querySelectorAll("[data-openreel-timeline-frame]"))
        .map((element) => Number(element.dataset.frameIndex || -1)))
      const timelineFramesPerClip = videoClips.map((clip) => Array.from(clip.querySelectorAll("[data-openreel-timeline-frame]"))
        .map((element) => Number(element.dataset.timelineFrame || -1)))
      const all = sourceFramesPerClip.flat()
      const consecutive = timelineFramesPerClip.every((frames) => frames.every((frame, index) => index === 0 || frame === frames[index - 1] + 1))
      return {
        uniqueFrames: new Set(all).size,
        leftMax: sourceFramesPerClip[0]?.length ? Math.max(...sourceFramesPerClip[0]) : -1,
        rightMin: sourceFramesPerClip[1]?.length ? Math.min(...sourceFramesPerClip[1]) : -1,
        everyFrame: strips.every((strip) => strip?.dataset.everyFrame === "true"),
        virtualized: strips.every((strip) => strip?.dataset.virtualized === "true"),
        totalClipFrames: strips.reduce((sum, strip) => sum + Number(strip?.dataset.totalClipFrames || 0), 0),
        renderedFrameCount: strips.reduce((sum, strip) => sum + Number(strip?.dataset.renderedFrameCount || 0), 0),
        maxRenderedPerClip: Math.max(0, ...strips.map((strip) => Number(strip?.dataset.renderedFrameCount || 0))),
        consecutive,
        realWaveforms: Array.from(document.querySelectorAll("[data-openreel-real-waveform]"))
          .filter((element) => Number(element.dataset.waveformBuckets || 0) > 0).length,
      }
    })
    const detailedFramesVisible = frameDetail.everyFrame && frameDetail.uniqueFrames >= 10 && frameDetail.consecutive
    const frameVirtualizationEffective = (
      frameDetail.virtualized &&
      frameDetail.renderedFrameCount < frameDetail.totalClipFrames &&
      frameDetail.maxRenderedPerClip <= 100
    )
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

    await panel.getByRole("button", { name: "关闭编辑器", exact: true }).click()
    await panel.waitFor({ state: "hidden" })
    await page.evaluate(({ nodeId, videoUrl }) => {
      window.dispatchEvent(new CustomEvent("openreel:edit-video-node", {
        detail: { nodeId, title: "Video editor verification", videoUrl },
      }))
    }, { nodeId: NODE_ID, videoUrl: VIDEO_URL })
    await panel.waitFor({ state: "visible" })
    await page.waitForFunction(() => document.querySelectorAll('[data-openreel-track-row="true"]').length === 4)
    await page.waitForFunction(() => document.querySelectorAll("[data-openreel-timeline-clip]").length === 4)
    const sequenceReopenPersisted = await page.evaluate(() => {
      const v2 = document.querySelector('[data-openreel-track-id="v2"]')
      const a2 = document.querySelector('[data-openreel-track-id="a2"]')
      const names = Array.from(document.querySelectorAll('[aria-label^="重命名轨道"]')).map((input) => input.value)
      return Boolean(
        v2?.getAttribute("data-track-locked") === "true" &&
        v2?.getAttribute("data-track-visible") === "false" &&
        a2?.getAttribute("data-track-sync-locked") === "false" &&
        a2?.getAttribute("data-track-muted") === "true" &&
        names.includes("补充画面") &&
        names.includes("补充声音")
      )
    })
    await page.locator('[data-clip-kind="video"]').nth(1).click()
    await page.getByLabel("源素材入点帧", { exact: true }).fill("24")
    await page.getByLabel("源素材出点帧", { exact: true }).fill("120")

    if (SCREENSHOT_PATH) {
      await page.evaluate(() => document.activeElement?.blur())
      await page.keyboard.press("n")
      await page.evaluate(() => {
        const inspectorScroller = document.querySelector('[data-openreel-inspector-pane="true"] .overflow-y-auto')
        if (inspectorScroller) inspectorScroller.scrollTop = inspectorScroller.scrollHeight
      })
      await page.waitForTimeout(120)
      await page.screenshot({ path: SCREENSHOT_PATH, fullPage: false })
      await page.waitForTimeout(300)
      await page.screenshot({ path: SCREENSHOT_PATH, fullPage: false })
    }

    const integerFrameTruth = [...videoParts, ...audioParts].every((clip) => (
      Number.isInteger(clip.startFrame) &&
      Number.isInteger(clip.durationFrames) &&
      Number.isInteger(clip.sourceInFrame) &&
      clip.durationFrames >= 1
    ))
    const result = {
      ok: initialAligned && movedTogether && clampedAtTimelineStart && maxStretchBounded && trimmedTogether && restoredToSourceBound && startTrimmedTogether && sourceStartBounded && splitSemantics && integerFrameTruth && undoRestoredBeforeSplit && redoRestoredSplit && linkedSelection && independentSelection && independentMove && additiveSelection && marqueeSelection && snappingDisabled && snappingEnabled && visibleSnapGuide && snapGuideCleared && editPointNavigation && shuttleShortcuts && rippleTrimSemantics && rippleIncomingTrimSemantics && rollingTrimSemantics && rollingIncomingTrimSemantics && exactFrameInputs && normalDeleteKeepsGap && rippleDeleteClosesGap && audioControlsPersisted && audioPreviewMixApplied && audioGainShortcut && sourceMarksApplied && dynamicTracksPersisted && crossTrackMovePreservedSource && insertEditSemantics && overwriteEditSemantics && trackControlsPersisted && lockedTrackRejectedMove && dynamicTrackHistory && sequenceReopenPersisted && zoomExpanded && zoomAnchorStable && detailedFramesVisible && frameVirtualizationEffective && realWaveformsVisible && layoutSupportsTracks && playbackResponsive && consoleErrors.length === 0,
      initialAligned,
      movedTogether,
      clampedAtTimelineStart,
      maxStretchBounded,
      trimmedTogether,
      restoredToSourceBound,
      startTrimmedTogether,
      sourceStartBounded,
      splitSemantics,
      integerFrameTruth,
      undoRestoredBeforeSplit,
      redoRestoredSplit,
      linkedSelection,
      independentSelection,
      independentMove,
      additiveSelection,
      marqueeSelection,
      snappingDisabled,
      snappingEnabled,
      visibleSnapGuide,
      snapGuideFrame: snapGuide.frame,
      snapGuideCleared,
      editPointNavigation,
      reverseShuttleFrame,
      forwardShuttleFrame,
      shuttleShortcuts,
      rippleTrimSemantics,
      rippleDeltaFrames,
      rippleIncomingTrimSemantics,
      rippleIncomingSourceDelta,
      rollingTrimSemantics,
      rollingDeltaFrames,
      rollingIncomingTrimSemantics,
      rollingIncomingDeltaFrames,
      exactFrameInputs,
      normalDeleteKeepsGap,
      rippleDeleteClosesGap,
      audioControlsPersisted,
      audioControls,
      audioPreviewMixApplied,
      audioGainShortcut,
      sourceMarksApplied,
      fadeStartVolume,
      fadeStartState,
      fullGainVolume,
      dynamicTracksPersisted,
      crossTrackMovePreservedSource,
      insertEditSemantics,
      overwriteEditSemantics,
      trackControlsPersisted,
      lockedTrackRejectedMove,
      dynamicTrackHistory,
      sequenceReopenPersisted,
      zoomExpanded,
      zoomAnchorStable,
      zoomBefore,
      zoomAfter,
      detailedFramesVisible,
      frameVirtualizationEffective,
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
