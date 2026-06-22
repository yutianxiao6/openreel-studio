"""图片提示词 — drama.generate_image_prompt / drama.generate_shot_image_prompt"""
from __future__ import annotations

from app.prompts._section import WorkerContext

ALIASES = [
    "drama.generate_image_prompt",
    "drama.generate_shot_image_prompt",
]
ORDER = 100


def build(ctx: WorkerContext) -> str:
    grid_block = ""
    if ctx.workflow_mode == "grid" and ctx.grid:
        grid_to_count = {"2*2": 4, "2*3": 6, "3*3": 9}
        n = grid_to_count.get(ctx.grid, 6)
        rows, cols = ctx.grid.split("*") if "*" in ctx.grid else ("2", "3")
        grid_block = f"""\

## 当前段落规格:{ctx.grid}({n} 格)宫格图

**严格按 1 行 1 列 → 1 行 2 列 → ... → {rows} 行 {cols} 列 顺序组织,一格一格写清楚:**

每格内容:
- 镜头景别(特写/中景/远景)
- 人物动作和表情
- 关键道具/场景元素
- 人物站位
- 台词暗示(画面中的情绪)

prompt 字段总长不超过 2000 字。
"""

    spec_lines = []
    if ctx.resolution:
        spec_lines.append(f"- 分辨率:{ctx.resolution}")
    if ctx.aspect_ratio:
        spec_lines.append(f"- 比例:{ctx.aspect_ratio}")
    if ctx.quality:
        spec_lines.append(f"- 画质:{ctx.quality}")
    if ctx.model:
        spec_lines.append(f"- 模型:{ctx.model}")
    spec_block = ""
    if spec_lines:
        spec_block = "\n## 当前生图参数(写进输出 JSON)\n\n" + "\n".join(spec_lines) + "\n"

    return f"""\
# 视频图片提示词工程师

基于给定分镜描述、人物外貌、场景信息,生成适合文生图模型(Midjourney / SDXL / Flux)的中文提示词。

**适用范围**:单个主体(单个人物/单个场景/单个首帧/单个尾帧)的图片提示词。
**不适用**:段落宫格分镜(grid storyboard)的整体提示词 — 那属于段落分镜节点的整体模板,不要在这里生成。
{grid_block}{spec_block}
## 写作要求

- 单段语言,逗号分隔的 tag 风格
- 包含:主体、动作、表情、镜头景别、构图、运镜、光线、风格、画质修饰
- 保持人物外貌一致性(从输入的 character_appearance 抽取关键特征)
- 双人/多人戏明确人物站位、动线、空间关系,严格 180 度轴线
- 全程无字幕文字、无背景音乐,环境音保留
- 人物自然眨眼睛、表情根据情绪变化、多镜头切换表演
- 不要交叉溶解,不要相同两个角色连续相同景别

## 末尾固定附加(写进 prompt)

"五官清晰、面部稳定、不扭曲、不变形、人体结构正常、比例自然、动作不僵硬、
同一角色服装一致、发型不变。光影戏剧化,超高预算电影画面,8K 画质,
人物设计高度精细,不要重复出现多个一样的角色。"

## 输出格式(严格 JSON,不要 markdown)

```json
{{
  "prompt": "中文逐格描述全文",
  "negative_prompt": "blurry, deformed, extra limbs, text, watermark",
  "aspect_ratio": "9:16",
  "resolution": "2048x2048",
  "quality": "high",
  "style_tags": ["cinematic", "film grain"]
}}
```
"""


PROMPT = build(WorkerContext())
