NAME = "segment_rule"
TRIGGER = "video"
ORDER = 100

PROMPT = """\
# Segments

分段(segment)是视频片段级拆分，不是分镜/镜头拆分。

- Total or per-episode duration <=15s normally stays one continuous segment; 15 秒动作短片默认 1 个 segment with internal beats.
- Durations above 15s can be split around 15s unless the user asks otherwise; each segment gets story source, main scene, storyboard/keyframe plan, and video target.
- Multi-episode work writes each episode story first, then applies segment logic inside that episode.
- Storyboard grids, first/last frames, references, and story-template boards are `image` node methods, not backend modes.
- Put segment references in `fields.references`; read `skill.video_production` for finer defaults.
"""
