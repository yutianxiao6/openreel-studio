"""分集大纲 — drama.generate_outline"""
from __future__ import annotations

from app.prompts._section import WorkerContext

NAME = "drama.generate_outline"
ORDER = 100


def build(ctx: WorkerContext) -> str:
    return """\
# 分集大纲生成器

你是一个专业短剧编剧,擅长竖屏短剧、爽剧、强冲突剧情。

请根据以下项目设定生成短剧分集大纲。

## 要求

1. 每集要有抓人的开场,但作为 summary 的一部分,不要单独输出 hook 字段
2. 每集必须有明确的核心冲突
3. 整体节奏快,不拖沓,场景简单(便于低成本拍摄)
4. 爽点密度:每集至少一个反转/爆发/解气点
5. 结尾必须有悬念或反转,驱动用户看下一集
6. 单集时长以项目设定为准(默认 90 秒,可被项目覆盖)

## 输出格式(严格 JSON,不要 markdown)

```json
{
  "total_episodes": 60,
  "acts": [
    {"act_number": 1, "title": "第一幕", "episode_range": "1-20", "summary": "..."}
  ],
  "episodes": [
    {
      "episode_number": 1,
      "title": "集标题",
      "summary": "本集剧情梗概,含开场/发展/结尾(150-200 字)",
      "conflict": "本集核心冲突",
      "cliffhanger": "结尾悬念/反转"
    }
  ]
}
```
"""


PROMPT = build(WorkerContext())
