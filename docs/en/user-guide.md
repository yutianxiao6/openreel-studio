# User guide

English · [简体中文](../zh-CN/user-guide.md) · [Documentation home](../README.en.md)

## The workspace

OpenReel Studio combines three related surfaces:

- **Chat**: describe goals, add constraints, request changes, and delegate multistep work.
- **Creation canvas**: inspect and edit user-facing text, image, video, and audio nodes.
- **Workflow panel**: select, build, and save reusable flows with steps, loops, and dependencies.

## Create a first project

1. Create a project with a recognizable title.
2. Describe the subject, duration, aspect ratio, and visual direction.
3. Upload references, a script, or existing media when available.
4. Answer blocking questions explicitly; let the agent decide non-blocking details when appropriate.

Example request:

> Create a 15-second, 16:9 Chinese fantasy short. Make character and scene references first, then a four-panel storyboard, and generate video after confirmation. Use a cool cinematic look and no narration.

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

1. Insert or overwrite source media onto a targeted track.
2. Move the playhead and split a clip into two real source ranges.
3. Trim clip edges without exceeding source-media bounds.
4. Arrange clips and use snapping for cuts, the playhead, and markers.
5. Verify that the filmstrip and waveform match the source range.
6. Adjust clip gain, track gain, mute, fades, and solo state.
7. Select a video clip before changing position, scale, rotation, opacity, or rectangular crop.
8. Export the sequence and use the result on the canvas.

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
