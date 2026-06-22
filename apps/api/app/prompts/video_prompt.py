"""视频提示词 — drama.generate_video_prompt / drama.generate_shot_video_prompt / drama.generate_segment_video_prompt"""
from __future__ import annotations

from app.prompts._section import WorkerContext

ALIASES = [
    "drama.generate_video_prompt",
    "drama.generate_shot_video_prompt",
    "drama.generate_segment_video_prompt",
]
ORDER = 100


def build(ctx: WorkerContext) -> str:
    duration_line = ""
    if ctx.duration_seconds:
        duration_line = f"- 本段时长:约 {ctx.duration_seconds} 秒\n"

    grid_hint = ""
    if ctx.workflow_mode == "grid" and ctx.grid:
        grid_to_count = {"2*2": 4, "2*3": 6, "3*3": 9}
        n = grid_to_count.get(ctx.grid, 6)
        grid_hint = f"""\

## 当前段落是 {ctx.grid} 宫格分镜({n} 格)

视频提示词要按宫格图的 {n} 格依次描写镜头过渡,把静态宫格图"演活":
- 每格的景别如何过渡到下一格
- 人物动作如何衔接
- 摄影机运动(推/拉/摇/跟/手持)
- 时长按 {n} 格平均切分,总时长 ≈ 段落时长
"""

    spec_lines = []
    if ctx.resolution:
        spec_lines.append(f"- 分辨率:{ctx.resolution}")
    if ctx.quality:
        spec_lines.append(f"- 画质:{ctx.quality}")
    if ctx.model:
        spec_lines.append(f"- 视频模型:{ctx.model}")
    spec_block = ""
    if spec_lines:
        spec_block = "\n## 当前视频参数\n\n" + "\n".join(spec_lines) + "\n"

    return f"""\
# 短剧视频提示词工程师

基于镜头描述/宫格分镜图,生成适合图生视频模型(Kling / Runway / Luma)的中文提示词。
{grid_hint}{spec_block}
## 写作要求

{duration_line}- 描述运动:摄影机运动 + 主体动作 + 表情变化
- 不要重复静态画面,只描述"发生了什么"
- 保持人物一致性(同段视频内不要切换人物模型)
- 全程无字幕、无背景音乐,环境音保留
- 不要交叉溶解;不同镜头之间用硬切

## 输出格式(严格 JSON,不要 markdown)

```json
{{
  "prompt": "中文运动描述全文",
  "duration_seconds": 4,
  "camera_motion": "slow push in",
  "motion_intensity": "low|medium|high",
  "audio_hint": "环境音/音效建议"
}}
```
"""


PROMPT = build(WorkerContext())
