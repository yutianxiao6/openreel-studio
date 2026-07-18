# User guide

English · [简体中文](../zh-CN/user-guide.md) · [Documentation home](../README.en.md)

## The workspace

OpenReel Studio combines three related surfaces:

- **Chat**: describe goals, add constraints, request changes, and delegate multistep work.
- **Creation canvas**: inspect and edit user-facing text, image, video, and audio nodes.
- **Workflow panel**: select, build, and save reusable flows with steps, loops, and dependencies.

## Create a first project

1. Configure an LLM and the required media Providers before first use.
2. Create a project with a recognizable title.
3. Describe the subject, duration, aspect ratio, and visual direction.
4. Upload references, a script, or existing media when available.
5. Answer blocking questions explicitly; let the agent decide non-blocking details when appropriate.

Example request:

> Create a 15-second, 16:9 Chinese fantasy short. Make character and scene references first, then a four-panel storyboard, and generate video after confirmation. Use a cool cinematic look and no narration.

## Configure models for the first time

OpenReel Studio separates models into two groups:

- **LLMs** power Agent chat, reasoning, prompt preparation, review, and context compaction.
- **Media Providers** generate the actual image, video, or audio result.

Open Settings in the upper-right corner and configure at least one enabled LLM. Add an image, video, or audio Provider only for the media types you intend to generate.

![Normal frontend configuration for a video Provider](../assets/screenshots/model-config-video-provider.png)

The normal sequence is:

1. Enter a versioned API Base URL such as `https://api.example.test/v1`.
2. Enter the exact provider model ID and API key.
3. Select the protocol that matches the provider HTTP API.
4. Save and confirm that the entry appears in the node model selector.
5. Make one real node run using a short prompt and protocol defaults.

If the protocol is absent, do not select an approximately similar protocol. Add a Catalog entry from the provider API documentation first. See [Model configuration and provider protocols](./model-providers.md) for every field, complete image/video/audio examples, and troubleshooting.

Model, ratio, and resolution come from node or workflow-artifact frontend settings. A reusable Workflow Spec does not store Provider secrets or fix these deployment-specific values.

## Node types

| Node | Typical content |
| --- | --- |
| Text | Scripts, segments, prompts, reviews, and production notes. |
| Image | Characters, scenes, props, storyboards, first/last frames, and style references. |
| Video | Shots, segments, joined sequences, and final delivery. |
| Audio | Voice, music, sound effects, and audio separated from video. |

Parent relationships organize the UI. Production dependencies are represented by references. Updating an upstream image does not silently rewrite a downstream result; you decide when to run it again.

## Visual references

Images serve two different purposes:

1. **Vision context for an LLM** when the task must describe or reason about visible content.
2. **Generation input** for an image or video provider without first sending the pixels to an LLM.

Inspect reference roles in the node details. A video may depend on character, scene, and storyboard images together; connecting only the final storyboard can lose consistency anchors.

## Generation and history

- A node run uses its current prompt, model, and references.
- Every successful result is added to history.
- An older result can become the current preview and downstream reference.
- A failed attempt keeps the latest successful output visible and reports the error below it.
- Manual generation allows a workflow to prepare the node and prompt, then wait for your explicit media run.

Read the node error before retrying. Configuration errors, policy errors, and temporary network failures require different fixes.

## Workflows

When a request matches a template, select it, fill its inputs, and run it. Enter workflow build mode only when you need a new process that will be reused. Runtime deliverables still appear on the creation canvas rather than being hidden inside process nodes.

See the [Workflow guide](./workflows.md) for details.

## Image viewing and editing

- Click a preview to fit the complete image within the screen.
- Select an earlier history item when it should become the active result.
- Image edits create candidates; confirm a candidate before replacing the active output.
- Save reusable material to the asset library for later nodes and projects.

## Video editing

Choose **Edit** on a video node to open the timeline.

Basic sequence:

1. Drag image, video, or audio media directly from the media pool to the target track and inspect the placement ghost before dropping.
2. Drop at a clip boundary to snap; when the gap is too small, later clips move as a group instead of being cut.
3. Move the playhead and split a clip into two real source ranges.
4. Trim clip edges without exceeding source-media bounds.
5. Arrange clips and use snapping for cuts, the playhead, and markers.
6. Verify that the filmstrip and waveform match the source range.
7. Adjust clip gain, track gain, mute, fades, and solo state.
8. Select a video clip before changing position, scale, rotation, opacity, or rectangular crop.
9. Export the sequence; the rendered result appears as a new video node on the canvas.

Dragging imported media onto the timeline creates an editing-sequence relationship only. It does not create a source dependency edge on the creation canvas.

Visual controls apply to the selected video clip. They do not mutate unrelated media when no clip is selected.

## Project state and recovery

- Refreshing the page does not delete nodes or media history.
- `/clear` resets conversation and runtime context without deleting canvas nodes.
- A full project reset is destructive and requires explicit confirmation.
- Traces and diagnostic logs are for troubleshooting, not public project deliverables.

## Recommended habits

- Keep one coherent production in one project.
- Lock the story and visual baseline before spending on video generation.
- Save important references to the asset library and use descriptive node titles.
- Repair the smallest failed node instead of rerunning an entire flow without cause.
- Before delivery, verify aspect ratio, duration, frame rate, levels, and media rights.
