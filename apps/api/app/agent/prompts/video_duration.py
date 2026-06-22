NAME = "video_duration"
TRIGGER = "video"  # 消息含 视频/短剧/集/秒/段/分镜/镜头
ORDER = 60

PROMPT = """\
# Video Duration

Use segment for video-clip duration, not storyboard cells, shot count, or action beats.

- Treat one model clip as about 15 seconds unless the user gives a different supported path.
- 15 秒视频直接当一段连续视频做; it can still contain multiple internal beats and normally prepares script, character, scene, and storyboard assets.
- 15-120 seconds is usually one episode with multiple segments; longer serial work proceeds by episode, then segment.
- If duration is missing for “一部剧”, ask with `interaction.request_input`; finer defaults come from `skill.video_production`.
"""
