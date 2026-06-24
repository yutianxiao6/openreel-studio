---
name: video_production
description: 节点优先的视频制作工作流。用于补全、创建、修复或运行 text/image/video 制作节点，包含详细剧本、人物图、场景图、分镜图、图生视频、文生视频、分段分集、参考图依赖和媒体失败处理。
---

# 视频制作 Skill

## 模型摘要

- 直接操作 `text` / `image` / `video` 节点；用户和 Agent 共用同一画布，优先补全匹配的空/草稿节点；task 只是进度账本。
- 读完 skill 后，只补问阻塞事实；用户说“你决定/全权发挥/模型决定”时，把可执行假设写入剧本/规划 text 节点后继续。
- 默认成片路径：详细剧本 text -> 主要人物 image -> 分集/分段故事 text(需要时) -> 主场景 image -> 分镜 image -> video；15 秒完整成片也使用最小完整节点图。
- 复杂、多节点、媒体生成、修复或重试用 `task.create(items=..., mode="sequential")`；image/video 运行前先自查待运行节点的 prompt、fields、依赖和用户硬约束。
- 最终视频必须有具体 `aspect_ratio`；用户未给画幅且未授权模型决定时，先问 `16:9`、`9:16` 或“模型决定”。
- fields.content 固定格式包含 `# 故事剧本：《标题》`、`## 故事正文` 和 `## 对白`。
- 默认 15 秒为一段 segment；默认 2-3 分钟为一集 episode；1 集不创建分集节点，1 段不创建分段节点。
- 没有可用剧本时先写详细剧本；剧本只写故事情节、动作、对白、人物关系、情绪变化和因果推进。
- 剧本里不写运镜、景别、构图，也不写人物表、角色设定卡、场景规划、制作说明、时间戳、分镜格、图片提示词或视频提示词。
- 剧本可以自然分段表达故事；时间戳属于最终 video prompt；15 秒视频也要有完整故事。
- 人物图 prompt 使用“官方设定集角色视觉参考表”模板；严格参考提供的参考图，不可自行改动；写 `fields.aspect_ratio="16:9"`、`fields.resolution="2560x1440"`、`quality="high"`。
- 场景图 prompt 使用固定格式：无人物场景四视图设定图，2x2四宫格，同一环境四个机位/角度；包含 `格1 全景建立镜头` 到 `格4 道具细节/俯视布局`；只画环境和道具，不出现人物。
- 分镜默认每段 1 个宫格分镜 image；分镜 image 的 `fields.references` 指向本段故事、相关人物 image 和场景 image；自由选择 2x2、2x3 或 3x3 宫格。
- 每段创建 1 个 video；视频 prompt 使用时间戳、分镜第几格、镜头变化、景别、转场、动作连续性和节奏，并写 `duration_seconds`。
- 依赖统一写 `fields.references`：文字上下文用 `role:"context"`，参考图用 `role:"visual_reference"`，直接采用现有图片作为 image 输出用 `role:"source_image"`。
- `parent_node_id` 只做画布分组；图片能否传给媒体模型由 `fields.references` 决定。
- `node.run(video)` 提交异步视频任务并返回 `job_id`；最终结果以节点状态、output 和画布事件为准。
- 媒体后端失败时汇报 blocked/failed；先修原节点再重试，不说成已完成。
- 降低分辨率、换质量、修 prompt/依赖或重试失败图片时，在相关失败 image 原节点上 `node.update` 后 `node.run(action="force")`；不要新建替代节点，除非用户明确要求保留旧节点另做新版。
- 本工具返回的 guidance/model_summary 就是指南正文；`skill_path` 只做诊断来源，不作为 `file.read_text` 目标。

## 目标

在共享画布上补全、更新、创建或运行 `text` / `image` / `video` 节点。产物是真实节点图，不是蓝图草稿。普通问答或很小的单节点修改可以不建任务；复杂视频、多节点媒体、修复或重试使用通用 Task Tracking。

## 输入

- 先读取用户最新需求和必要画布状态；用户给出编号（如 `#3` / `3`）时直接 `node.get(node_id)`；标题、描述或不明确目标用 `node.list(query|regex)` 定位候选；不要依赖旧摘要。
- 保留用户明确给出的主题、总时长、集数、单段秒数、画幅、视觉风格、制作方法、参考素材和硬约束。
- 用户已创建的空节点、草稿节点和手写内容是待补全对象；匹配时先 `node.update`，再考虑新建。
- 用户指定其他 skill、自定义制作流程或明确改写流程时，以用户/自定义 skill 为准；本 skill 只提供默认视频制作方法。
- 用户只要求文生视频、不要图片预制作、快速抽象测试或只要文字时，走 direct T2V 或 text-only；direct T2V 仍用 text 节点记录主题、节拍、人物/场景文字设定和制作路径。
- 用户只要求视觉预制作时，只创建或运行 `text` 和 `image` 节点，不承诺最终视频。
- 最终视频必须有具体画幅；用户未给且未授权模型决定时，先问 `16:9`、`9:16` 或“模型决定”。
- 用户继续输入自定义修改时，先定位现有节点并局部更新；不要重建整棵节点图。
- 用户要求降低规格、重新生成或修复失败媒体时，先定位当前目标下失败/未完成的原节点；有匹配原节点就更新原节点，不新建替代节点。

默认值：

- 一段 `segment`：15 秒。
- 一集 `episode`：2-3 分钟。
- 每段视频数：1 个 `video`。
- 人物图：只给主要人物创建。
- 场景图：每段一个主场景；同一地点跨段延续时复用。
- 分镜图：默认每段一张宫格分镜图。

## 工作流

1. 剧本 text
   - 没有可用剧本节点时，第一步先写故事剧本；已有空/草稿剧本节点时先补全它。
   - 剧本只写故事情节、动作、对白、人物关系、情绪变化和因果推进。
   - 剧本不写运镜、景别、构图，也不写人物表、角色设定卡、场景规划、制作说明、时间戳、分镜格、图片提示词或视频提示词。
   - 剧本可以自然分段表达故事；时间戳属于最终 video prompt；15 秒视频也要有完整故事。
   - `fields.content` 使用固定格式：

     ```markdown
     # 故事剧本：《标题》

     ## 故事正文
     （连续自然段，只写故事动作、对白前后情境、情绪变化和因果推进。）

     ## 对白
     - 人物：「台词」
     ```

     没有明确对白时，`## 对白` 下写 `无明确对白。`
   - 人物数量、角色外观、场景规划、分镜节拍和制作说明不要混进剧本正文；需要记录时放到任务摘要、后续 image 节点 prompt，或单独规划 `text` 节点。
   - `node.run(text)` 只保存已有内容；正文要先写入或补入 `fields.content`。
   - 15 秒完整成片需要完整起承转合，不把短视频写成单个画面描述。

2. 人物 image
   - 只为推动剧情的主要人物创建人物图；群演、路人和背景人物写在剧本或场景文字里。
   - 人物图参考上一步详细剧本，不从用户短句直接跳到人物图。
   - prompt 模板：官方设定集角色视觉参考表，真人摄影级质感，正面、侧面、背面全身三面图，毛孔级写实特写，服装和装备的详细部件，色彩搭配色板，边缘简短世界观文字，纯白背景图解风排版。
   - 严格参考提供的参考图，不可自行改动角色和背景。
   - 武器、重点服装、法器、道具等直接影响剧情理解的物件可以随主要人物一起表达，或单独建 image。
   - fields 写 `fields.aspect_ratio="16:9"`、`fields.resolution="2560x1440"`、`quality="high"`。
   - image 的 `fields.resolution` 写精确像素，不写 2k/4k/8k；16:9 常用 `2560x1440`，最高规格时写 `3840x2160`。
   - references：剧本 text 用 `role:"context"`；参考图片用 `role:"visual_reference"`。

3. 分集/分段 text
   - 多集时创建分集 text；1 集不创建分集节点。
   - 多段时创建分段 text；1 段不创建分段节点，直接使用总剧本或本集剧本作为本段故事来源。
   - 1 集时不创建分集节点；1 段时不创建分段节点。
   - 分集/分段正文仍只写故事、动作、对白、情绪和关键事件；不写时间戳、运镜、景别、图片提示词或视频提示词。

4. 场景 image
   - 场景按故事发生地点创建，不能按分镜逐镜头创建场景。
   - prompt 固定结构：`无人物场景四视图设定图，2x2四宫格，同一环境四个机位/角度`，也就是四宫格四视图。
   - 逐格写：`格1 全景建立镜头`、`格2 入口/主方向`、`格3 反向/侧向空间`、`格4 道具细节/俯视布局`。
   - 每格写清地点、空间结构、道具、光线、氛围和风格锚点；只画环境和道具，不出现人物。
   - references 指向本段故事；1 段时指向总剧本或本集剧本。

5. 分镜 image
   - 默认创建 1 个宫格分镜节点，prompt 写成一张 storyboard sheet。
   - 根据动作复杂度选择 2x2、2x3 或 3x3 宫格；2x2 适合运动平缓，3x3 适合动作、打斗、复杂调度。
   - 逐格写清格号、镜头内容、景别、构图、主体位置、动作、视线/运动方向和连续性。
   - 分镜 image 的 `fields.references` 指向本段故事、相关人物图和场景图；图片参考使用 `{ref:"node:<image_id>", role:"visual_reference"}`。
   - 只有用户明确要求单张分镜、关键帧、拆镜头、高质量单图，或自定义 skill 选择该流程时，才创建多个单镜头 image。

6. video
   - 每段创建 1 个最终 video 节点。
   - prompt 参考本段故事和分镜内容；prompt 文本只写创作描述，不写工具调用、节点 id、调试信息或制作解释。
   - 视频 prompt 必须包含时间戳、分镜第几格、镜头变化、景别、转场、动作连续性和节奏。
   - 如果需要看清图片，先用 `vision.view_image` 看分镜/参考图，不假装看过图。
   - 写入 `duration_seconds`、`aspect_ratio`、`production_path` 和真实 `fields.references`。
   - prompt 和 fields 完成后先自查；证据复杂或不确定时用 `agent.review`；再运行 `node.run`。

## 节点图

普通 15 秒视频的最小完整节点图：

```text
script_text
  character_image
  scene_image
  storyboard_image
  final_video
```

长视频推荐结构：

```text
project_script
  character_image_*
  episode_01_script        # 多集时才有
    segment_01_script
      scene_image_*
      storyboard_image
      segment_video
```

`task` 只做进度账本；生产依赖写入 `fields.references`：

- 人物图依赖详细剧本。
- 分集节点依赖详细剧本；分段节点依赖对应分集或详细剧本。
- 场景图依赖对应段落故事；1 段时依赖详细剧本或分集故事。
- 分镜图依赖段落故事、相关人物图和场景图。
- 视频依赖分镜图，必要时也引用人物图和场景图。

`fields.references` 语义：

- `{ref:"node:<text_id>", role:"context"}`：文字上下文。
- `{ref:"node:<image_id>", role:"visual_reference"}`：参考图片生成新图片或视频。
- `{ref:"node:<image_id>" 或 "asset:<id>" 或 URL/上传路径, role:"source_image"}`：image 节点直接采用现有图片作为本节点输出。

`parent_node_id` 只做画布分组；图片能否传给媒体模型由 `fields.references` 决定。

## 任务与检查

- 通用 Task Tracking：多节点制作、媒体生成、修复或重试先建 `task.create(items=..., mode="sequential")` checklist；每个阶段开始时 `task.update(status="in_progress")`，完成后用真实工具结果和节点状态更新任务。
- 运行 `node.run` 前先检查待运行批次的 prompt、fields、依赖和用户约束；发现问题先 `node.update` 修原节点，再 `node.run`。
- 修复旧 image 节点时，先按 `fields.purpose` 对照模板结构检查 prompt；不合格时同时修 `prompt`、`input_json.prompt`、`prompt_source="skill.video_production"`、resolution 和依赖。
- 修复失败图片链时按相关原节点依赖顺序处理：人物 image、场景 image、分镜 image；用户明确限定“只修分镜/只修这个节点”时才缩小范围。视频只依赖分镜不代表跳过分镜上游人物和场景。
- 已通过 `project.get_state`、`node.get` 或一次 `node.list` 拿到明确失败节点后，直接更新/重跑这些节点；不要反复查询同一状态。
- 复杂或不确定时调用 `agent.review` 做只读第二视角；review 阻塞时缩小证据重试一次或做明确自查，不无限重复。
- 没有证据的建议只作为参考；产出、完成和失败状态以真实工具结果、节点 status/output 和画布事件为准。
- 单个字段或依赖缺失时局部修复，不重建整棵节点图。

汇报完成或运行下游节点前确认：

- `text` 节点有非空 `fields.content`。
- 剧本、分集和分段 text 使用固定剧本格式，只写故事和对白。
- 只有主要人物创建了人物图。
- 人物图符合官方设定集角色视觉参考表，并写入 16:9 / 2560x1440 / high。
- 场景图是无人物四宫格四视图。
- 分镜是单节点宫格 storyboard sheet，逐格写清镜头内容和连续性。
- 视频 prompt 包含时间戳、分镜第几格、镜头变化、景别、转场和动作连续性。
- 用户确认的时长、画幅和路径写入可执行字段。
- 人物、场景、分镜、视频的 `fields.references` 使用真实上游 node id；参考生成和直接采用图片用 role 区分。
- 媒体后端失败时汇报 blocked/failed，不说成已完成。

## 工具返回说明

本工具返回的 `guidance` / `model_summary` 就是指南正文；`skill_path` 只做诊断来源，不作为 `file.read_text` 目标。需要重读时再次调用 `skill.video_production(detail="full")`。
