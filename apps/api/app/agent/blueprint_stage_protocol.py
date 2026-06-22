"""Runtime protocol injected only while a semantic blueprint draft is active."""
from __future__ import annotations

from typing import Any

BLUEPRINT_STAGE_PROTOCOL_VERSION = "blueprint_stage_protocol_v1"


def build_blueprint_stage_protocol(*, max_append_nodes: int = 12) -> str:
    """Return the model-facing blueprint-stage workflow contract.

    This text is intentionally kept out of the stable system prefix. It enters
    context through blueprint tool results and runtime draft state, so normal
    chat turns keep the same cacheable prefix.
    """
    return (
        "### 蓝图阶段协议\n"
        "目标:把用户需求转成可审核的 semantic blueprint tree。顺序是:确认入口事实 -> "
        "start_tree_draft 记录草稿外壳和 blueprint.fields -> 读取 full 指南 -> 按指南补齐阻塞信息 -> "
        "append/update 内容节点 -> review/finalize。不要凭印象补流程。\n"
        "1. 先确认三项入口事实:优先复用最新用户消息、collected_facts、参考图和项目状态。"
        "episode_count 和 segment_seconds 是可推断规模事实，不是固定提问项；用户给出总时长时直接落实。"
        "15秒及以内默认 episode_count=1、segment_seconds=总秒数、单段连续，不询问多少集、是否分段或每段几秒。"
        "只有总时长/规模无法推断，或用户明确要长剧、多集、多段时，才补问制作规模。"
        "production_basis 表示文生视频、图生视频或模型判断；用户没说、也无法从参考图/分镜图/流程描述推断时再问。"
        "用户回答“模型判断/你决定”也算已确认授权；授权后 production_basis 可写 model_decide，"
        "episode_count 和 segment_seconds 要由模型选出具体数字，并在蓝图 brief 写清假设。"
        "三项未确认或未落实前，不开始蓝图草稿，不按自己的猜测选择制作路径，也不先读取具体流程指南。"
        "用户已经说明制作流程、参考素材使用方式"
        "或禁用某类生成时，以用户说明为准。开始草稿时必须把三项写入 blueprint.fields:"
        "episode_count、segment_seconds、production_basis，最终审核会按这些字段检查树和视频节点是否一致。"
        "start_tree_draft 只记录草稿外壳和入口字段，不表示可以立刻追加内容节点。\n"
        "2. 读取指南:通用视频蓝图/建树指南是默认必读资料。start_tree_draft 成功后，"
        "先检查 runtime 的指南复用缓存；缺少可复用 full 资料时，"
        "用 tool.search(category='guide') 搜索通用视频蓝图/建树指南，再 tool.describe 确认可用工具，"
        "最后 tool.execute 读取 full guide。读完通用指南后，再根据文生视频/图生视频、规模、用户指定流程"
        "和 guide 提示，自行搜索并读取合适的 skill/guide；只读和当前方案相关的资料。"
        "已读取的 guide/skill 是当前制作合同:按其中的步骤、字段、依赖、检查项来推理和建树，"
        "不能脱离指南另起流程；用户点名的 skill 或制作流程优先于默认指南。"
        "自由发挥只限用户和指南都未覆盖的创作细节，并要写清模型假设；"
        "若工具、安全、节点类型或用户最新指令导致无法照做，说明原因并用最接近的合法表达。\n"
        "3. 补齐阻塞信息:三项入口事实只确定规模和制作依据，不表示创作信息已经完备。读完通用/分支指南后，"
        "如果仍缺会影响蓝图结构或 prompt 质量的主题、角色、风格、画幅、参考素材、对白、声音或硬约束，"
        "再用 interaction.request_input 继续补问最多 6 个阻塞问题；开放问题不传 options，需要选择时才传 2-3 个 options。"
        "确定指南后按将要创建的产物反推缺项："
        "图生视频且参考图由模型生成时，至少要能写清主题/核心事件、主要人物或角色关系、场景/时代/视觉风格、"
        "关键动作节拍和结尾落点；缺到会导致人物图、场景图、分镜图或视频 prompt 只能写成空泛概括时，先补问。"
        "用户回答“你全权决定/模型发挥”时，直接把这些缺项写成模型假设，不继续追问。\n"
        "4. 建树:只有入口字段已写入、所需 full 指南已读取、阻塞信息已补问或转为模型假设后，"
        "才使用 append/update 添加内容节点。少量同父级节点可用 nodes 批量，单次最多 "
        f"{max_append_nodes} 个；默认 3-5 个一批。节点多时按 brief/assets/episode/segment/image/video 分批。"
        "parent_id 只表达分组和展示层级；生产顺序、参考关系和等待上游产物用 references/depends_on。"
        "视频请求通常需要最终 video 目标节点；分镜图、参考图、首尾帧、故事模板图都表达为 image 节点。"
        "15 秒单段通常只有一个 segment，内部节奏写进 segment content、分镜 prompt 或 cells。"
        "video.fields.production_path 必须和 blueprint.fields.production_basis 对齐。"
        "prompt 写法来自当前 skill 和用户自定义流程；需要复用的新写法写入 skill，不查独立 prompt 目录。\n"
        "5. 检查与修正:append/update 的 tool result 会返回 current_tree，优先用它检查完整草稿；"
        "必要时再用 blueprint.get 查看指定节点或完整树。检查规模、制作方法、节点位置、"
        "依赖关系、最终 video 目标和 prompt 证据是否一致。小问题用 update_tree_node 局部修正；"
        "根层级或制作路径整体错误时才 replacement 重建。必要的只读 review 之后再 finalize_tree_draft；"
        "确认前不运行图片或视频。"
    )


def blueprint_stage_protocol_payload(*, max_append_nodes: int = 12) -> dict[str, Any]:
    return {
        "version": BLUEPRINT_STAGE_PROTOCOL_VERSION,
        "max_append_nodes": max_append_nodes,
        "prompt": build_blueprint_stage_protocol(max_append_nodes=max_append_nodes),
        "cache_policy": "runtime_only; not part of the stable system/tool prefix",
    }
