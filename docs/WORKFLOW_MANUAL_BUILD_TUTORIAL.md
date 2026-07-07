# 工作流手动搭建教程 / Manual Workflow Building Tutorial

## English Overview

This tutorial explains how to build a workflow from the current OpenReel Studio
frontend and how the visible buttons map to the backend workflow protocol.

Use the workflow panel to edit reusable templates. Use the workflow run dock on
the canvas to add a runtime instance, fill this run's inputs, run one step, run
the next ready step, run all remaining steps, pause, delete, or inspect step
output.

The main frontend concepts are:

- `Input`: defines values the user fills before a run, such as plot, duration,
  segment length, style, or a source video node.
- `Generate Text`: asks the model to write or transform plain text.
- `Extract Collection`: extracts a list of structured objects, such as
  characters, scenes, shots, segments, or keyframes. Users write natural
  language; the backend injects the structured output contract.
- `Split Segments`: creates a duration-based segment plan.
- `Loop`: repeats child steps for each item in a collection.
- `Text/Image/Video/Audio Node`: creates or updates a visible canvas product.
- `Plugin Action`: runs an installed workflow plugin.
- `Review`: checks quality or consistency.

Recommended build order:

1. Create the `Input` step and define required fields.
2. Add a text step that turns the inputs into a complete script or task brief.
3. Add collection steps for unknown counts, such as characters or segments.
4. Add loops over those collections.
5. Place visible product nodes inside or after the loops.
6. Add dynamic references only after stable matching fields such as `name` and
   `reuse_key` exist.
7. Save the workflow, then run it step by step once before using run-all.

For dynamic image references, do not manually connect every future image.
Instead, make the upstream collection and downstream plan share stable fields
such as `name` or `reuse_key`, then let the workflow spec select matching
candidate images at runtime with `references`.

The full detailed tutorial is provided in Chinese below.

## 中文正文

这篇教程面向想在前端自己搭建 workflow 的用户。它按当前前端已经有的按钮、右侧栏和后端 `openreel.workflow.authoring.v1` 协议来写，目标是让用户知道每一步该点哪里、填什么、最终在后端会变成什么执行关系。

## 先理解两个界面

OpenReel 里有两个和 workflow 相关的区域：

| 区域 | 用来做什么 | 典型按钮 |
| --- | --- | --- |
| 流程面板 / 搭建流程 | 编辑一个可复用流程模板。这里改的是“以后怎么跑”的规则。 | `新建流程`、`导入流程`、`保存流程`、`保存为模板`、`下载模板` |
| 画布里的流程运行栏 | 在当前项目里添加一个流程实例、填写本次输入、运行步骤。这里改的是“这一次怎么跑”。 | `添加流程`、`运行一步`、`一键执行`、`暂停`、`删除` |

一个简单判断：

- 想改流程结构、节点顺序、提示词、依赖关系，去 `搭建流程`。
- 想填剧情、时长、风格并开始生成，去画布底部的 `流程运行`。
- 一个模板可以添加多次运行；每个运行实例都有自己的输入和运行进度。

## 前端按钮地图

### 顶部流程栏

进入流程面板后，顶部会看到这些控件：

| 按钮/输入 | 含义 |
| --- | --- |
| 模板下拉框 | 选择一个内置流程或我的流程。内置流程会显示 `内置 · ...`，用户模板会显示 `我的 · ...`。 |
| `流程名称` | 当前模板的用户可见名称。 |
| `新建流程` | 从空白流程开始搭建。 |
| `导入流程` | 导入一个 JSON workflow spec。适合导入别人给你的模板，或导入高级协议能力。 |
| `刷新` | 重新读取模板列表。 |
| `保存流程` / `已保存` | 保存当前正在编辑的流程草稿。 |
| `保存为模板` / `更新模板` | 把当前流程保存到用户模板目录，之后可在前端选择。 |
| `下载模板` | 下载当前用户模板 JSON。内置模板需要先保存成用户模板再下载。 |
| `返回流程` | 从导入或生成的临时预览回到正常流程选择。 |

### 左侧工具箱

左侧 `工具箱` 分成三类。搜索框可以按名称搜索步骤。

处理动作：

| 按钮 | 作用 | 后端 kind |
| --- | --- | --- |
| `输入` | 定义运行前要用户填写的内容，例如剧情、时长、风格。 | `input` |
| `生成文本` | 调用 LLM 生成、改写、总结、组织正文。 | `text` |
| `提取集合` | 从上游文本里提取多项对象，例如人物列表、场景列表、段落列表。 | `collection` |
| `分段拆分` | 按时长或规则拆段。 | `plan` |
| `遍历执行` | 对集合里的每一项重复执行子步骤。 | `loop` |

画布产物：

| 按钮 | 作用 | 后端 kind |
| --- | --- | --- |
| `文本节点` | 把上游文本作为用户可见文本节点放到画布。 | `canvas_text` |
| `图片节点` | 把上游内容生成或承接为画布图片节点。 | `image` |
| `视频节点` | 把上游内容生成或承接为画布视频节点。 | `video` |
| `音频节点` | 把上游内容生成或承接为画布音频节点。 | `audio` |

更多步骤：

| 按钮 | 作用 |
| --- | --- |
| `插件动作` | 调用 workflow plugin，例如视频关键帧提取。 |
| `质量检查` | 做审查或复核步骤。 |

添加步骤的常见方式：

- 点击左侧工具箱里的步骤，会在画布上创建节点。
- 右键画布可以从菜单创建节点。
- 选中一个步骤后，节点右侧的 `+` 用来添加下一步。
- 在 `遍历执行` 里面用 `添加到循环里` 添加循环子步骤。
- 拖动节点时会有自动对齐效果；节点排布只影响阅读，不等于执行依赖。

### 右侧详情栏

选中步骤后，右侧详情栏有这些标签：

| 标签 | 用来配置什么 |
| --- | --- |
| `设置` | 节点名称、步骤类型、运行方式、是否可跳过、跳过条件。 |
| `上下游` | 先读哪一步、输出格式、集合字段、画布产物内容来源、引用上游产物。 |
| `提示词` | 文本、集合、分段等处理步骤的自然语言提示词。 |
| `结果` | 当前运行结果、状态和输出预览。 |
| `说明` | 当前步骤的说明。 |

没有选中步骤时，右侧会显示流程设置。这里主要是模板级信息和高级协议字段，普通用户通常只需要填流程名称和步骤。

## 后端协议心智模型

前端搭建的流程最终会保存成 authoring spec，协议版本是：

```json
{
  "schema": "openreel.workflow.authoring.v1"
}
```

你不需要在普通提示词里写 JSON，但要理解这些概念：

| 概念 | 在前端叫什么 | 在协议里叫什么 | 含义 |
| --- | --- | --- | --- |
| 流程输入 | `输入` 节点里的输入内容 | `inputs` | 运行前用户要填的剧情、时长、风格等。 |
| 步骤 | 画布上的流程节点 | `steps[]` | 一个处理动作或一个画布产物。 |
| 执行依赖 | `先读取哪一步` | `needs` / `depends_on` | 当前步骤运行前必须先完成哪些步骤。 |
| 上下文读取 | `读取上下文` | `reads_from` / `context_refs` | 当前步骤需要参考哪些内容。 |
| 集合 | `提取集合` | `kind: collection` | 输出一组对象，例如多个人物。 |
| 循环 | `遍历执行` | `for_each` / `foreach` | 对集合里的每一项重复运行子步骤。 |
| 画布产物 | `文本节点`、`图片节点`、`视频节点`、`音频节点` | `output.canvas: true` | 用户最终在画布上看到和编辑的节点。 |
| 动态引用 | `把上游产物作为参考` | `references` / `reference_selectors` | 根据字段匹配上游产物，例如某段只引用出现的人物图。 |

核心原则：

- 流程模板描述“规则”，不是一次运行产生的内容。
- 画布上的 `text` / `image` / `video` / `audio` 节点是用户可见结果。
- `生成文本`、`提取集合`、`分段拆分` 这类处理步骤可以只留在流程运行态，不一定显示在画布上。
- 只要用 `文本节点`、`图片节点`、`视频节点`、`音频节点`，就代表这个步骤会产出用户可见画布节点。

## 推荐搭建顺序

手动搭流程时，不要一开始就铺满所有节点。建议按这个顺序：

1. 先建 `输入`，把运行前必须问用户的东西定下来。
2. 建一个 `生成文本`，把用户输入整理成完整剧本或完整任务说明。
3. 建 `提取集合`，把未知数量的人物、场景、分段提取成列表。
4. 用 `遍历执行` 对集合逐项生成图片、文本或视频。
5. 用画布产物节点把真正要给用户看的结果落到画布。
6. 最后补动态引用和质量检查。

这样做的好处是：如果一开始不知道有几个人物、几段剧情、几张图，可以先让集合节点运行出来，再让循环自动展开。

## 示例：剧情转短视频工作流

目标：

用户输入剧情、视频总时长、分段秒数和视觉风格。流程生成完整剧本，按约 15 秒拆成多段，每段产出文本节点、分镜图和视频节点；人物图数量由剧本动态决定，后续每段只引用这一段出现的人物图。

### 第 1 步：新建流程

1. 打开项目。
2. 切到 `流程面板` 或 `搭建流程`。
3. 点击 `新建流程`。
4. 在 `流程名称` 里输入：`剧情转短视频工作流`。
5. 先不要急着保存，等输入节点配置好再保存。

### 第 2 步：添加输入节点

1. 在左侧工具箱点击 `输入`。
2. 选中这个节点，右侧会显示“这个节点决定运行前要输入哪些内容。”
3. 在 `输入内容` 里添加这些项：

| 输入项 | 输入方式 | 必填 | 说明 |
| --- | --- | --- | --- |
| 剧情内容 | 大段文字 | 是 | 输入完整剧情、梗概或要改编的文本。 |
| 视频总时长 | 数字 | 是 | 整个视频总秒数，例如 30、60、90。 |
| 分段秒数 | 数字 | 否 | 默认 15。 |
| 视觉风格 | 单行文本 | 否 | 例如写实、电影感、国风、赛博朋克。 |

如果界面里有快捷按钮，直接点 `剧情内容`、`视频总时长`、`分段秒数`、`视觉风格` 这些预设即可。

对应协议大致是：

```json
{
  "inputs": {
    "plot": { "type": "long_text", "label": "剧情内容", "required": true },
    "total_duration_seconds": { "type": "number", "label": "视频总时长", "required": true },
    "segment_seconds": { "type": "number", "label": "分段秒数", "default": 15 },
    "style": { "type": "text", "label": "视觉风格" }
  }
}
```

### 第 3 步：生成完整剧本

1. 在输入节点右侧点 `+`，添加 `生成文本`。
2. 把节点标题改成：`完整剧本`。
3. 在 `上下游` 里勾选 `输入` 作为上游。
4. 在 `提示词` 里写自然语言，不写 JSON。

建议提示词：

```text
根据用户输入的剧情内容、视频总时长和视觉风格，写一份完整短视频剧本正文。
要求包含故事起承转合、主要人物、关键场景、动作和情绪变化。
直接输出剧本正文，方便后续拆人物、拆场景和拆分段。
```

如果提示词编辑器里有插入按钮，可以插入 `输入 · 剧情内容`、`输入 · 视频总时长`、`输入 · 视觉风格`。这些插入项对应后端运行时的输入变量。

### 第 4 步：提取主要人物集合

1. 从 `完整剧本` 后面添加 `提取集合`。
2. 标题改成：`主要人物清单`。
3. 在 `上下游` 里勾选 `完整剧本`。
4. 在集合字段里添加这些列：

| 字段标识 | 类型 | 给用户看的列名 | 说明 |
| --- | --- | --- | --- |
| `name` | 文本 | 人物名 | 后续匹配人物图时最重要。 |
| `reuse_key` | 文本 | 匹配标识 | 用稳定英文或拼音标识同一个人物，例如 `hero_lina`。 |
| `appearance` | 文本 | 外貌 | 发型、年龄、服装、气质。 |
| `personality` | 文本 | 性格 | 性格和表演气质。 |
| `visual_prompt_brief` | 文本 | 视觉提示摘要 | 给人物图提示词使用。 |

提示词写：

```text
从完整剧本中提取主要人物。每个人物都要有稳定的人物名和 reuse_key。
reuse_key 后续会用于匹配人物图和分镜引用，同一个人物必须始终保持一致。
```

这里不需要让用户写 JSON。`提取集合` 节点会由后端自动注入结构化输出要求，模型按字段返回列表。

### 第 5 步：循环生成每个人物图

1. 从 `主要人物清单` 后面添加 `遍历执行`。
2. 标题改成：`逐个生成人物图`。
3. 在循环来源里选择 `主要人物清单` 的输出列表。
4. 如果要填路径，通常是 `output.main_characters` 或 `output.items`，取决于集合输出字段名；优先使用界面里的下拉选项。

在这个循环里面添加两个子步骤。

第一个子步骤：`生成文本`

- 标题：`人物图提示词`
- 上游：当前循环项、完整剧本、主要人物清单
- 提示词：

```text
根据当前人物条目和完整剧本，为这个人物写一段人物设定图提示词。
提示词要写清楚外貌、服装、气质、表情、光线、构图和视觉风格。
只写这个人物，不要混入其他人物。
```

第二个子步骤：`图片节点`

- 标题：`人物参考图`
- 在 `上下游` 里选择 `人物图提示词`。
- 在 `内容来源` 里选择 `人物图提示词` 的输出。
- 打开“运行时使用这个内容生成媒体”一类的选项。
- 在属性里填尺寸、比例、质量等媒体参数。尺寸必须用数字字段，不要把分辨率写到提示词里。

运行后，人物有几个，循环就会展开几个图片节点。

### 第 6 步：拆分视频段落

1. 从 `完整剧本` 后面添加 `分段拆分` 或 `提取集合`。
2. 标题改成：`分段清单`。
3. 上游选择 `完整剧本` 和 `输入`。
4. 输出字段建议：

| 字段标识 | 类型 | 说明 |
| --- | --- | --- |
| `segment_index` | 数字 | 第几段，从 1 开始。 |
| `start_second` | 数字 | 开始秒数。 |
| `end_second` | 数字 | 结束秒数。 |
| `duration_seconds` | 数字 | 本段时长。 |
| `segment_text` | 文本 | 本段完整剧情正文。 |
| `appearing_characters` | 数组 | 本段出现的人物名或 reuse_key。 |

提示词：

```text
根据完整剧本、视频总时长和分段秒数，把剧本拆成多个连续段落。
每段都要包含完整剧情正文、开始秒数、结束秒数、时长和出场人物。
出场人物必须使用主要人物清单里的 name 或 reuse_key，方便后续匹配人物参考图。
```

如果总时长是 30 秒、分段秒数是 15 秒，应该得到 2 段；如果总时长是 60 秒，应该得到 4 段。

### 第 7 步：循环制作每一段

1. 从 `分段清单` 后面添加 `遍历执行`。
2. 标题改成：`逐段制作`。
3. 循环来源选择 `分段清单` 的列表输出。

在循环里建议添加这些子步骤。

#### 7.1 本段剧本文本节点

添加 `文本节点`：

- 标题：`本段剧本`
- 内容来源：当前段落的 `segment_text`
- 作用：把每段剧情正文显示到画布，用户可以直接读和改。

#### 7.2 本段分镜规划

添加 `生成文本` 或 `提取集合`：

- 标题：`本段分镜规划`
- 上游：当前段落、完整剧本、主要人物清单、视觉风格
- 输出至少包含本段出场人物、画面、动作、镜头节奏。

提示词：

```text
根据当前段落剧情，为这一段规划分镜。
写清楚每个镜头的画面主体、动作、情绪、景别、镜头运动和出场人物。
出场人物必须沿用主要人物清单中的 name 或 reuse_key。
```

#### 7.3 分镜图提示词

添加 `生成文本`：

- 标题：`分镜图提示词`
- 上游：本段剧本、本段分镜规划、视觉风格
- 提示词：

```text
根据本段剧本和分镜规划，写一张分镜参考图提示词。
提示词要能生成清晰的画面参考，包含构图、人物位置、动作、表情、场景、光线和风格。
如果本段有多镜头，可以组织成一张宫格分镜图。
直接输出图片提示词正文。
```

#### 7.4 分镜图片节点

添加 `图片节点`：

- 标题：`分镜图`
- 内容来源：`分镜图提示词`
- 上游依赖：`分镜图提示词`
- 参考图：引用这一段出现的人物参考图。

这里有一个关键点：人物图数量是动态的，某一段只应该引用本段出现的人物，而不是引用所有人物图。

当前前端已经有 `把上游产物作为参考` 的入口，可以添加直接引用；后端协议还支持按字段动态匹配。动态匹配的协议写法如下：

```json
{
  "references": {
    "appearing_character_images": {
      "source": "plan_frames.output.appearing_characters",
      "candidates": "main_character_images",
      "match_fields": ["name", "reuse_key", "character_id", "id", "title"],
      "role": "visual_reference"
    }
  }
}
```

含义：

- `source`：从当前段落或分镜规划里读取本段出场人物。
- `candidates`：候选图片来自“逐个生成人物图”这个循环产物。
- `match_fields`：用 `name`、`reuse_key` 等字段做匹配。
- `role`：匹配到的图片作为视觉参考传给图片节点。

如果 UI 里没有把 `candidates` 和 `match_fields` 做成单独表单，可以用两种方式：

1. 让工作流搭建模式生成或修补这个模板。
2. 在高级场景下导入包含 `references` 的 authoring spec。

普通用户不用在提示词里写这段 JSON；这是模板协议，不是节点提示词。

#### 7.5 视频提示词和视频节点

添加 `生成文本`：

- 标题：`本段视频提示词`
- 上游：本段剧本、本段分镜规划、分镜图、视觉风格
- 提示词：

```text
根据本段剧本、分镜规划、分镜图和视觉风格，写本段视频生成提示词。
提示词要包含主体、动作、镜头运动、情绪、场景、光线、风格和时长。
直接输出视频提示词正文。
```

再添加 `视频节点`：

- 标题：`本段视频`
- 内容来源：`本段视频提示词`
- 参考：`分镜图`
- 属性：填写本段时长、宽高、质量等媒体参数。

## 动态集合、循环和引用怎么配合

### 不知道有几个人物时

用这个结构：

```text
完整剧本
  -> 主要人物清单（提取集合）
    -> 逐个生成人物图（遍历执行）
      -> 人物图提示词
      -> 人物参考图（图片节点）
```

后端逻辑：

- `主要人物清单` 先运行，得到一个列表。
- `逐个生成人物图` 根据列表数量展开。
- 每一项都会生成自己的图片节点。

### 后续某一段只依赖其中几张人物图时

用这个结构：

```text
主要人物清单
  -> 逐个生成人物图

分段清单
  -> 逐段制作
    -> 本段分镜规划（写出 appearing_characters）
    -> 分镜图（按 appearing_characters 匹配人物参考图）
```

关键不是让用户手动连到某一张图，因为运行前还不知道有几张图。关键是让上游集合和下游规划都保留同一个稳定字段，例如：

```text
name = 林夏
reuse_key = lina
```

然后动态引用用 `reuse_key` 或 `name` 匹配。

## 插件节点怎么用

插件节点用于调用 `plugins/` 目录里的 workflow plugin。例如“提取关键帧”这类功能，推荐搭法是：

```text
输入视频（视频节点）
  -> 提取关键帧（插件动作）
    -> 关键帧集合
      -> 遍历执行
        -> 关键帧图片节点
```

注意：

- 上传视频时，先把视频变成画布 `视频节点`，再让插件节点读取这个视频节点。
- 如果插件输出的是一组图片地址，后续还要逐张展示，就用 `遍历执行` 把每张图变成单独的 `图片节点`。
- 如果后续步骤要选择其中某一张关键帧，最好给每张关键帧保留 `index`、`timestamp`、`label` 这类字段，方便匹配。

## 保存和运行

搭建完成后：

1. 点击顶部 `保存流程`。
2. 如果以后还要复用，点击 `保存为模板`。
3. 需要分享或备份时，点击 `下载模板`。
4. 回到 `创作画布`。
5. 打开画布底部 `流程` / `流程运行`。
6. 在模板下拉框选择刚保存的流程。
7. 点击 `添加流程`。
8. 点击运行实例里的输入步骤，在右侧详情栏填写本次运行输入。
9. 先用 `运行一步` 调试前几步。
10. 确认没问题后用 `一键执行`。
11. 一键执行过程中按钮会变成 `暂停`；需要暂停时点它。
12. 点击任一步骤可以在右侧详情栏查看输出。

建议第一次运行时不要直接一键执行完整流程。先跑：

1. 输入
2. 完整剧本
3. 主要人物清单
4. 分段清单

确认人物数量、段落数量和字段正确后，再跑图片和视频节点。

## 自查清单

保存模板前检查这些点：

- `输入` 里每个必填项都有清晰名称、输入方式和提示。
- `生成文本` 节点的提示词只写自然语言任务，不要求普通用户手写 JSON。
- `提取集合` 节点有明确字段，例如 `name`、`reuse_key`、`segment_text`。
- `遍历执行` 的来源是一个集合输出，不是普通文本。
- 画布上要展示给用户看的内容使用 `文本节点`、`图片节点`、`视频节点` 或 `音频节点`。
- 图片、视频、音频的宽高、时长、质量等属性写在节点属性里，不写在上游文本字段里。
- 后续要动态引用某张图时，上游集合和下游步骤使用同一套稳定匹配字段。
- 运行一次后，流程运行栏里的步骤状态、画布节点和连线应该一致。

## 常见错误

### 把所有内容都写进一个提示词

不推荐。应该拆成：

```text
生成文本 -> 提取集合 -> 遍历执行 -> 画布产物
```

这样后续才能引用某一段、某个人物、某张图。

### 在提示词里要求模型输出 JSON

普通用户不要这样写。集合节点已经有字段表，后端会自动要求模型按字段结构化输出。用户提示词只描述任务。

需要 JSON 的地方是 workflow 模板文件本身，不是普通节点提示词。

### 直接让分镜图依赖所有人物图

如果每段只出现一部分人物，不要让每个分镜都引用所有人物图。应该让本段规划输出 `appearing_characters`，再通过动态引用选择匹配的人物图。

### 只生成一个图片列表，不生成单独图片节点

如果后续步骤需要引用单张图，应该把集合里的每张图变成单独 `图片节点`。一个图片列表文本不等于多个可引用的画布图片节点。

### 把执行顺序当成画布位置

节点排布只是阅读顺序。真正控制执行的是 `先读取哪一步` / `needs`。移动节点不会自动改变依赖关系。

## 一个最小 authoring spec 示例

下面是一个精简版协议示例，用来说明前端搭建结果在后端大概长什么样。普通用户不需要手写它；当 UI 暂时没有暴露某个高级字段时，可以通过导入模板或工作流搭建模式生成类似结构。

```json
{
  "schema": "openreel.workflow.authoring.v1",
  "id": "plot_to_video_workflow",
  "title": "剧情转短视频工作流",
  "inputs": {
    "plot": { "type": "long_text", "label": "剧情内容", "required": true },
    "total_duration_seconds": { "type": "number", "label": "视频总时长", "required": true },
    "segment_seconds": { "type": "number", "label": "分段秒数", "default": 15 },
    "style": { "type": "text", "label": "视觉风格" }
  },
  "steps": [
    {
      "id": "input",
      "title": "运行输入",
      "kind": "input"
    },
    {
      "id": "full_script",
      "title": "完整剧本",
      "kind": "text",
      "needs": ["input"],
      "prompt": {
        "task": "根据剧情内容、总时长和视觉风格写完整短视频剧本。",
        "output": "直接输出剧本正文。"
      }
    },
    {
      "id": "main_characters",
      "title": "主要人物清单",
      "kind": "collection",
      "needs": ["full_script"],
      "output": { "key": "main_characters" },
      "output_schema": {
        "items_key": "main_characters",
        "fields": [
          { "id": "name", "type": "string", "label": "人物名", "required": true },
          { "id": "reuse_key", "type": "string", "label": "匹配标识", "required": true },
          { "id": "visual_prompt_brief", "type": "string", "label": "视觉提示摘要" }
        ]
      },
      "prompt": {
        "task": "从剧本中提取主要人物，并为每个人物生成稳定 reuse_key。"
      }
    },
    {
      "id": "main_character_images",
      "title": "逐个生成人物图",
      "kind": "loop",
      "for_each": "main_characters.output.main_characters",
      "item_name": "character",
      "steps": [
        {
          "id": "character_image_prompt",
          "title": "人物图提示词",
          "kind": "text",
          "prompt": {
            "task": "根据当前人物条目写人物设定图提示词。",
            "output": "直接输出图片提示词正文。"
          }
        },
        {
          "id": "character_image",
          "title": "人物参考图",
          "kind": "image",
          "needs": ["character_image_prompt"],
          "output": { "canvas": true, "type": "image" }
        }
      ]
    },
    {
      "id": "segments",
      "title": "分段清单",
      "kind": "collection",
      "needs": ["full_script", "main_characters"],
      "output": { "key": "segments" },
      "output_schema": {
        "items_key": "segments",
        "fields": [
          { "id": "segment_index", "type": "number", "label": "段落序号" },
          { "id": "segment_text", "type": "string", "label": "段落剧情" },
          { "id": "appearing_characters", "type": "array", "label": "出场人物" }
        ]
      },
      "prompt": {
        "task": "按分段秒数把完整剧本拆成连续段落，并列出每段出场人物。"
      }
    },
    {
      "id": "segment_work",
      "title": "逐段制作",
      "kind": "loop",
      "for_each": "segments.output.segments",
      "item_name": "segment",
      "steps": [
        {
          "id": "segment_script_node",
          "title": "本段剧本",
          "kind": "canvas_text",
          "output": { "canvas": true, "type": "text" }
        },
        {
          "id": "segment_plan",
          "title": "本段分镜规划",
          "kind": "collection",
          "needs": ["segment_script_node"],
          "output": { "key": "frames" },
          "output_schema": {
            "items_key": "frames",
            "fields": [
              { "id": "frame_index", "type": "number", "label": "镜头序号" },
              { "id": "frame_text", "type": "string", "label": "镜头画面" },
              { "id": "appearing_characters", "type": "array", "label": "出场人物" }
            ]
          },
          "prompt": {
            "task": "根据当前段落规划分镜，并列出每个镜头出现的人物 name 或 reuse_key。"
          }
        },
        {
          "id": "storyboard_prompt",
          "title": "分镜图提示词",
          "kind": "text",
          "needs": ["segment_plan"],
          "prompt": {
            "task": "根据当前段落写分镜图提示词。",
            "output": "直接输出图片提示词正文。"
          }
        },
        {
          "id": "storyboard_image",
          "title": "分镜图",
          "kind": "image",
          "needs": ["storyboard_prompt", "main_character_images"],
          "references": {
            "appearing_character_images": {
              "source": "segment_plan.output.appearing_characters",
              "candidates": "main_character_images",
              "match_fields": ["name", "reuse_key", "id", "title"],
              "role": "visual_reference"
            }
          },
          "output": { "canvas": true, "type": "image" }
        }
      ]
    }
  ]
}
```

这个示例表达了三个关键能力：

- 人物图数量由 `主要人物清单` 动态决定。
- 每段数量由 `分段清单` 动态决定。
- 每段分镜图按 `appearing_characters` 从人物图候选里动态挑选参考图。

## 当前 UI 和协议能力边界

当前 UI 已经能直接配置：

- 输入项、输入类型、必填、默认值和说明。
- 处理步骤、画布产物步骤、插件步骤。
- 上下游依赖。
- 集合字段。
- 循环来源。
- 画布产物的内容来源。
- 直接引用上游产物。
- 保存、导入、下载模板。
- 多个流程实例并行运行、单步运行、一键运行、暂停和删除。

后端协议已经支持但 UI 仍属于高级用法的部分：

- 通过 `candidates`、`source`、`match_fields` 做动态引用选择。
- 复杂 loop 嵌套和跨循环实例引用。
- 更细的 `extensions`、`required_capabilities`、`required_extensions`。

遇到这些高级场景时，推荐做法是：

1. 先用 UI 搭出主体结构。
2. 保存为模板。
3. 用工作流搭建模式或导入 spec 补齐高级字段。
4. 回到前端运行一次，用流程运行栏和画布节点检查结果。
