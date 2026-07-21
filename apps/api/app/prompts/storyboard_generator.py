"""分镜生成 — internal storyboard and shot runners."""
from __future__ import annotations

from app.prompts._section import WorkerContext

ALIASES = [
    "drama.generate_storyboard",
    "drama.generate_shot",
    "drama.generate_segment_shots",
]
ORDER = 100


def _grid_section(grid: str) -> str:
    grid_to_count = {"2*2": 4, "2*3": 6, "3*3": 9}
    n = grid_to_count.get(grid, 6)
    rows, cols = grid.split("*") if "*" in grid else ("2", "3")
    return f"""\
## 当前段落分镜规格:{grid}({n} 格)

**铁律:整段所有 {n} 格的描述写进 prompt 字段,产出一张宫格图,不要拆出多个独立提示词节点。**

prompt 字段必须严格按 1 行 1 列 → 1 行 2 列 → ... → {rows} 行 {cols} 列 顺序逐格写,
每格写明:景别(特写/中景/远景) + 人物动作 + 关键道具 + 人物位置 + 台词暗示。

## 镜头切换约束

- 不要交叉溶解
- 全程不得出现相同两个角色连续相同景别
- 双人/多人戏严格遵循 180 度轴线规则,避免越轴
- 同一角色服装一致、发型不变、五官稳定、不变形

## 收尾必加

每段提示词末尾固定附:"全程不要字幕,不要背景音乐,环境音保留。人物自然眨眼睛。
五官清晰、面部稳定、不扭曲、不变形、人体结构正常、比例自然、动作不僵硬、同一角色服装一致、发型不变。
光影戏剧化,8K 画质,人物设计高度精细,不要重复出现多个一样的角色。"
"""


def build(ctx: WorkerContext) -> str:
    if ctx.workflow_mode == "grid" and ctx.grid:
        grid_block = _grid_section(ctx.grid)
        output_format = """\
## 输出格式(严格 JSON)

```json
{
  "grid": "%s",
  "prompt": "1 行 1 列:中景,...; 1 行 2 列:特写,...; ... (整段全部 N 格逐格描写)",
  "cells": [
    {"row": 1, "col": 1, "shot_type": "中景", "content": "...", "dialogue": "...", "characters": ["..."]}
  ],
  "negative_prompt": "...",
  "aspect_ratio": "9:16",
  "style_tags": ["cinematic", "film grain"]
}
```

不要返回 shots[] 数组(那是老格式)。一段 = 一个对象,prompt 字段含全部格的描写。
""" % ctx.grid
    elif ctx.workflow_mode == "frames":
        grid_block = "## 当前段落:首尾帧模式(frames)\n\n切 1-3 个独立镜头,每镜配首帧+尾帧两张图。"
        output_format = """\
## 输出格式(严格 JSON)

```json
{
  "shots": [
    {
      "shot_number": 1,
      "shot_type": "特写/中景/远景",
      "camera_movement": "推/拉/摇/跟/手持",
      "duration_seconds": 5,
      "content": "画面内容",
      "dialogue": "台词",
      "characters": ["..."],
      "image_prompt": "中文图片提示词",
      "video_prompt": "中文视频提示词"
    }
  ]
}
```
"""
    else:
        grid_block = "## 输出逐镜分镜规划\n\n按场景输出镜头描述、人物动作和镜头运动。"
        output_format = """\
## 输出格式(严格 JSON)

```json
{
  "shots": [
    {
      "shot_number": 1,
      "shot_type": "特写/中景/远景",
      "camera_movement": "推/拉/摇/跟/手持",
      "duration_seconds": 5,
      "content": "画面内容",
      "dialogue": "台词",
      "characters": ["..."]
    }
  ]
}
```"""

    return f"""\
# 短剧分镜师

你是专业短剧分镜师,擅长竖屏短视频。

{grid_block}

{output_format}

## 生成职责边界

- 段落宫格分镜:产出整段一体化 prompt（所有格子写在一个 prompt 字段里），不拆成多个独立提示词节点
- 逐镜分镜:产出每镜独立描述的 shots 数组
- 单镜首帧/尾帧提示词:由首尾帧节点单独生成，不在分镜阶段产出
- 单个角色/场景的图片提示词:由人物/场景节点单独生成，分镜阶段只引用角色名/场景名，不重复生成

## 通用约束

- 优先近景和特写,适合竖屏观看
- 台词字幕位置清晰
- 场景切换流畅,节奏紧凑
- 人物名严格使用项目人物列表里的名字
- 不要交叉溶解;不同镜头之间用硬切
"""


PROMPT = build(WorkerContext(workflow_mode="grid", grid="2*3"))
