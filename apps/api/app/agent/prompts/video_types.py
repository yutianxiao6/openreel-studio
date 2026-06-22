NAME = "video_types"
TRIGGER = "video"  # 用户消息含视频类关键词
ORDER = 65  # 在 video_duration(60) 之后

PROMPT = """\
# Video Defaults

User-specified type, style, duration, aspect, and character facts win.

- Ask with `interaction.request_input` only when a missing preference strongly affects the result.
- Common defaults: ads are short and hook-first; promos center product/brand; tutorials often need narration/subtitles; MV follows music rhythm; short drama/comic drama can be episodic.
- When defaults are enough, write assumptions into a planning `text` node.
- Read `skill.video_production` for detailed type handling and do not let generic defaults override explicit user facts.
"""
