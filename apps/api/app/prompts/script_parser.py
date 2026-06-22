"""剧本结构识别 — drama.parse_uploaded_script"""
from __future__ import annotations

from app.prompts._section import WorkerContext

NAME = "drama.parse_uploaded_script"
ORDER = 100


def build(ctx: WorkerContext) -> str:
    return """\
# 剧本结构识别器

你是剧本结构识别器。给定一段剧本原文,提取场次、人物、对白结构。

## 输出格式(严格 JSON,不要 markdown)

```json
{
  "title": "<推断的剧名/单集标题>",
  "summary": "<本集梗概,200 字以内>",
  "cliffhanger": "<结尾悬念/反转>",
  "scenes": [
    {
      "name": "<场次名>",
      "location": "<场景地点>",
      "time_of_day": "<日/夜/晨/黄昏>",
      "characters": ["<出场人物 1>", "<出场人物 2>"],
      "summary": "<本场剧情概要>"
    }
  ],
  "characters": [
    {"name": "<人物名>", "role_type": "<female_lead/male_lead/antagonist/supporting>"}
  ]
}
```

## 规则

- 场次按文中 `场次/SCENE/INT./EXT.` 等标记切分;无标记按场景或时间转换识别
- 人物名以"角色:对白"的"角色"列为准;旁白/画外音不计入人物
- 角色类型按戏份和倾向推断;不确定一律 `supporting`
- 不要输出 markdown 代码块,直接 JSON
"""


PROMPT = build(WorkerContext())
