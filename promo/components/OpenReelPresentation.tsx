"use client"

import {AnimatePresence, motion} from "framer-motion"
import {useCallback, useEffect, useMemo, useRef, useState} from "react"

type SlideProps = {step: number}

const ease = [0.22, 1, 0.36, 1] as const
const reveal = {
  initial: {opacity: 0, y: 28, scale: 0.97},
  animate: {opacity: 1, y: 0, scale: 1},
  exit: {opacity: 0, y: -18, scale: 0.985},
  transition: {duration: 0.55, ease},
}

function BrandMark({size = 54}: {size?: number}) {
  return (
    <div className="relative shrink-0" style={{width: size, height: size}}>
      <div className="absolute inset-[3%] rounded-full border-[3px] border-cyan-300 shadow-[0_0_28px_rgba(77,227,255,.32),inset_0_0_16px_rgba(77,227,255,.16)]" />
      {[0, 90, 180, 270].map((rotation) => (
        <span
          key={rotation}
          className="absolute left-[46%] top-[8%] h-[18%] w-[9%] rounded-full bg-white/90"
          style={{transformOrigin: `50% ${size * 0.42}px`, transform: `rotate(${rotation}deg)`}}
        />
      ))}
      <span className="absolute inset-[32%] rounded-full bg-violet-400 shadow-[0_0_16px_rgba(139,124,255,.75)]" />
    </div>
  )
}

function SlideLabel({index, children}: {index: string; children: string}) {
  return (
    <div className="absolute left-[6%] top-[7%] flex items-center gap-3 text-[clamp(10px,1vw,17px)] font-semibold uppercase tracking-[0.22em] text-cyan-300/80">
      <span>{index}</span><span className="h-px w-8 bg-cyan-300/35" /><span>{children}</span>
    </div>
  )
}

function WindowShell({children}: {children: React.ReactNode}) {
  return (
    <div className="flex h-full flex-col overflow-hidden rounded-[clamp(12px,1.4vw,24px)] border border-white/10 bg-[#0c1017]/95 shadow-[0_32px_100px_rgba(0,0,0,.52)]">
      <div className="flex h-[clamp(28px,3.2vw,50px)] shrink-0 items-center gap-2 border-b border-white/[0.08] bg-white/[0.025] px-4">
        <span className="h-2.5 w-2.5 rounded-full bg-red-400/80" />
        <span className="h-2.5 w-2.5 rounded-full bg-amber-300/80" />
        <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/80" />
        <span className="ml-3 text-[clamp(7px,.75vw,12px)] tracking-[0.16em] text-slate-500">OPENREEL STUDIO</span>
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  )
}

function CoverSlide({step}: SlideProps) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
      <motion.div {...reveal} className="flex items-center gap-4">
        <BrandMark size={64} />
        <span className="text-[clamp(16px,1.6vw,27px)] font-semibold tracking-wide text-white">OpenReel Studio</span>
      </motion.div>
      <AnimatePresence>
        {step >= 1 && <motion.div key="line1" {...reveal} className="mt-[4.5%] text-[clamp(38px,5vw,82px)] font-bold leading-none tracking-[-0.055em] text-white">把一个想法</motion.div>}
        {step >= 2 && (
          <motion.div key="line2" {...reveal} className="mt-3 bg-gradient-to-r from-cyan-300 via-violet-400 to-amber-300 bg-clip-text text-[clamp(38px,5vw,82px)] font-bold leading-none tracking-[-0.055em] text-transparent">
            变成一支完整视频
          </motion.div>
        )}
        {step >= 3 && (
          <motion.div key="tags" {...reveal} className="mt-[4%] flex flex-wrap justify-center gap-2.5">
            {["对话式创作", "节点工作流", "专业剪辑"].map((item) => <span key={item} className="rounded-full border border-white/12 bg-white/[0.045] px-4 py-2 text-[clamp(9px,.9vw,15px)] text-slate-300">{item}</span>)}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

const nodes = [
  {title: "创意剧情", kind: "TEXT", color: "cyan", x: "11%", y: "42%"},
  {title: "角色参考", kind: "IMAGE", color: "violet", x: "42%", y: "22%"},
  {title: "宫格分镜", kind: "IMAGE", color: "amber", x: "42%", y: "60%"},
  {title: "视频成片", kind: "VIDEO", color: "emerald", x: "73%", y: "42%"},
]

const nodeTone: Record<string, string> = {
  cyan: "border-cyan-300/35 text-cyan-300 shadow-cyan-300/10",
  violet: "border-violet-400/35 text-violet-300 shadow-violet-400/10",
  amber: "border-amber-300/35 text-amber-300 shadow-amber-300/10",
  emerald: "border-emerald-300/35 text-emerald-300 shadow-emerald-300/10",
}

function WorkflowSlide({step}: SlideProps) {
  return (
    <div className="absolute inset-0">
      <SlideLabel index="01">从一句话开始</SlideLabel>
      <motion.div {...reveal} className="absolute inset-x-[6%] bottom-[8%] top-[14%]">
        <WindowShell>
          <div className="grid h-full grid-cols-[28%_1fr]">
            <div className="border-r border-white/[0.08] p-[7%]">
              <div className="text-[clamp(8px,.8vw,13px)] text-slate-500">与 OpenReel Agent 对话</div>
              <AnimatePresence>
                {step >= 1 && (
                  <motion.div key="user" {...reveal} className="ml-[8%] mt-[12%] rounded-2xl rounded-br-sm bg-white px-[6%] py-[5%] text-[clamp(10px,1vw,16px)] leading-relaxed text-slate-900 shadow-xl">
                    做一支 30 秒的未来城市动作短片
                  </motion.div>
                )}
                {step >= 2 && (
                  <motion.div key="agent" {...reveal} className="mt-[7%] flex items-start gap-3">
                    <BrandMark size={34} />
                    <div className="rounded-2xl rounded-tl-sm border border-cyan-300/20 bg-cyan-300/[0.055] p-[5%] text-[clamp(9px,.86vw,14px)] leading-relaxed text-slate-300">
                      已拆解创意，正在创建剧情、角色、分镜与视频节点。
                      <div className="mt-3 h-1 overflow-hidden rounded-full bg-white/[0.08]"><motion.div initial={{width: 0}} animate={{width: "88%"}} transition={{duration: 1.1, ease}} className="h-full bg-gradient-to-r from-cyan-300 to-violet-400" /></div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
            <div className="relative overflow-hidden bg-[radial-gradient(circle_at_center,rgba(77,227,255,.055),transparent_34%)]">
              <div className="absolute inset-0 opacity-15 [background-image:linear-gradient(rgba(255,255,255,.12)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.12)_1px,transparent_1px)] [background-size:32px_32px]" />
              {step >= 4 && (
                <motion.svg initial={{opacity: 0}} animate={{opacity: 1}} transition={{duration: .7}} className="absolute inset-0 h-full w-full" viewBox="0 0 1000 600" preserveAspectRatio="none">
                  <path d="M240 300 C330 300 330 170 430 170 M240 300 C330 300 330 430 430 430 M620 170 C710 170 710 300 790 300 M620 430 C710 430 710 300 790 300" fill="none" stroke="rgba(77,227,255,.46)" strokeWidth="3" strokeDasharray="8 9" />
                </motion.svg>
              )}
              <AnimatePresence>
                {step >= 3 && nodes.map((node, index) => (
                  <motion.div
                    key={node.title}
                    initial={{opacity: 0, scale: .82, y: 18}}
                    animate={{opacity: 1, scale: 1, y: 0}}
                    transition={{delay: index * .12, duration: .48, ease}}
                    className={`absolute w-[22%] rounded-xl border bg-[#10151e]/95 p-[2%] shadow-2xl ${nodeTone[node.color]}`}
                    style={{left: node.x, top: node.y, transform: "translateY(-50%)"}}
                  >
                    <div className="text-[clamp(6px,.65vw,10px)] tracking-[.15em]">● {node.kind}</div>
                    <div className="mt-[8%] text-[clamp(11px,1.15vw,19px)] font-semibold text-white">{node.title}</div>
                    <div className="mt-[9%] h-1.5 rounded-full bg-white/10"><div className="h-full w-2/3 rounded-full bg-current/40" /></div>
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          </div>
        </WindowShell>
      </motion.div>
    </div>
  )
}

function VisualSlide({step}: SlideProps) {
  const cards = [
    ["角色一致性", "从参考图锁定人物身份", "from-violet-900 via-violet-700/60 to-rose-700/50", "text-violet-300"],
    ["电影级分镜", "逐镜头控制画面与节奏", "from-cyan-950 via-cyan-700/60 to-amber-700/45", "text-cyan-300"],
    ["视频生成", "模型参数与提示词可追溯", "from-slate-900 via-blue-800/55 to-violet-700/50", "text-amber-300"],
  ]
  return (
    <div className="absolute inset-0">
      <SlideLabel index="02">视觉生产</SlideLabel>
      <motion.h2 {...reveal} className="absolute left-[6%] top-[14%] text-[clamp(28px,3.3vw,56px)] font-bold tracking-[-.045em] text-white">每一个节点，都是可见的创作成果</motion.h2>
      <div className="absolute inset-x-[6%] bottom-[9%] top-[27%] grid grid-cols-3 gap-[2.2%]">
        <AnimatePresence>
          {cards.map((card, index) => step >= index + 1 && (
            <motion.div key={card[0]} initial={{opacity: 0, y: 70, rotate: (index - 1) * 2}} animate={{opacity: 1, y: 0, rotate: (index - 1) * .7}} exit={{opacity: 0, y: 30}} transition={{duration: .65, ease}} className={`relative overflow-hidden rounded-[clamp(14px,1.5vw,26px)] border border-white/12 bg-gradient-to-br ${card[2]} shadow-[0_32px_80px_rgba(0,0,0,.38)]`}>
              <div className="absolute inset-0 bg-[radial-gradient(circle_at_52%_28%,rgba(255,255,255,.24),transparent_20%),linear-gradient(180deg,transparent_40%,rgba(5,7,11,.86))]" />
              <motion.div initial={{x: "-20%"}} animate={{x: "120%"}} transition={{duration: 2.4, repeat: Infinity, repeatDelay: 1.2, ease: "linear"}} className="absolute inset-y-0 w-px bg-white/60 shadow-[0_0_22px_rgba(255,255,255,.7)]" />
              <div className={`absolute left-[7%] top-[7%] rounded-full border border-current/35 bg-black/30 px-3 py-1.5 text-[clamp(6px,.65vw,10px)] tracking-[.16em] ${card[3]}`}>GENERATING</div>
              <div className="absolute inset-x-[8%] bottom-[8%]">
                <div className="text-[clamp(16px,1.55vw,27px)] font-semibold text-white">{card[0]}</div>
                <div className="mt-2 text-[clamp(8px,.82vw,13px)] leading-relaxed text-slate-300/75">{card[1]}</div>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  )
}

function EditorSlide({step}: SlideProps) {
  const bars = [18,30,46,22,64,44,28,58,72,34,52,26,68,42,30,54,70,38,24,62,48,30,68,40]
  return (
    <div className="absolute inset-0">
      <SlideLabel index="03">专业剪辑</SlideLabel>
      <motion.div {...reveal} className="absolute inset-x-[5.5%] bottom-[7%] top-[13%]">
        <WindowShell>
          <div className="grid h-[56%] grid-cols-[18%_1fr_20%] border-b border-white/[0.08]">
            <div className="border-r border-white/[0.08] p-[8%]">
              <div className="text-[clamp(6px,.7vw,11px)] tracking-[.14em] text-slate-500">PROJECT</div>
              {step >= 1 && ["城市航拍.mp4", "角色近景.mp4", "动作镜头.mp4", "环境声.wav"].map((item, index) => <motion.div key={item} initial={{opacity: 0, x: -18}} animate={{opacity: 1, x: 0}} transition={{delay: index * .1}} className="mt-[9%] flex items-center gap-2 text-[clamp(7px,.74vw,12px)] text-slate-400"><span className={`h-5 w-8 rounded bg-gradient-to-br ${index % 2 ? "from-violet-800 to-rose-700/60" : "from-cyan-900 to-blue-700/60"}`} />{item}</motion.div>)}
            </div>
            <div className="flex items-center justify-center p-[4%]">
              <div className="relative aspect-video w-[72%] overflow-hidden rounded-md bg-gradient-to-br from-slate-900 via-cyan-800/70 to-rose-700/60 shadow-[0_24px_65px_rgba(0,0,0,.6)]"><div className="absolute inset-0 bg-[radial-gradient(circle_at_60%_36%,rgba(255,230,190,.3),transparent_18%),linear-gradient(180deg,transparent_50%,rgba(4,7,12,.7))]" /><div className="absolute bottom-[7%] left-[6%] text-[clamp(6px,.65vw,10px)] tracking-[.15em] text-white/70">PROGRAM · 00:00:18:12</div></div>
            </div>
            <div className="border-l border-white/[0.08] p-[8%]">
              <div className="text-[clamp(6px,.7vw,11px)] tracking-[.14em] text-slate-500">CLIP CONTROLS</div>
              {step >= 3 && ["缩放 108%", "位置 X +2.4", "音量 -1.5 dB", "淡入 12 帧"].map((item, index) => <motion.div key={item} initial={{opacity: 0, x: 16}} animate={{opacity: 1, x: 0}} transition={{delay: index * .1}} className={`border-b border-white/[0.06] py-[8%] text-[clamp(7px,.78vw,12px)] ${index === 2 ? "text-cyan-300" : "text-slate-400"}`}>{item}</motion.div>)}
            </div>
          </div>
          <div className="relative h-[44%] px-[6%] py-[2.6%]">
            {step >= 2 && (
              <motion.div initial={{opacity: 0, y: 30}} animate={{opacity: 1, y: 0}} transition={{duration: .6, ease}} className="h-full">
                <div className="flex justify-between font-mono text-[clamp(5px,.58vw,9px)] text-slate-600"><span>00:00</span><span>00:05</span><span>00:10</span><span>00:15</span><span>00:20</span></div>
                <div className="mt-[1.5%] grid h-[20%] grid-cols-[1.15fr_.85fr_1.2fr] gap-1">{["bg-cyan-800/70", "bg-violet-700/65", "bg-amber-700/55"].map((tone) => <div key={tone} className={`rounded border border-white/10 ${tone} bg-[linear-gradient(90deg,transparent_48%,rgba(255,255,255,.1)_50%,transparent_52%)] [background-size:34px_100%]`} />)}</div>
                <div className="mt-1 grid h-[20%] grid-cols-[.7fr_1.25fr_1.25fr] gap-1">{["bg-violet-800/60", "bg-sky-800/55", "bg-rose-800/45"].map((tone) => <div key={tone} className={`rounded border border-white/10 ${tone}`} />)}</div>
                <div className="mt-1 flex h-[27%] items-center gap-1 rounded border border-cyan-300/15 bg-cyan-900/20 px-3">{bars.map((height, index) => <span key={index} className="w-1 rounded-full bg-cyan-300/65" style={{height: `${height}%`}} />)}</div>
              </motion.div>
            )}
            {step >= 3 && <motion.div initial={{left: "18%", opacity: 0}} animate={{left: "57%", opacity: 1}} transition={{duration: 1.3, ease}} className="absolute bottom-[5%] top-[9%] w-px bg-white shadow-[0_0_9px_rgba(255,255,255,.7)]"><span className="absolute -left-1.5 -top-1.5 h-3 w-3 rotate-45 bg-white" /></motion.div>}
          </div>
        </WindowShell>
      </motion.div>
    </div>
  )
}

function ClosingSlide({step}: SlideProps) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
      <motion.div {...reveal} className="flex items-center gap-5"><BrandMark size={78} /><span className="text-[clamp(20px,2.2vw,37px)] font-semibold text-white">OpenReel Studio</span></motion.div>
      <AnimatePresence>
        {step >= 1 && <motion.div key="closing-title" {...reveal} className="mt-[4%] text-[clamp(34px,4vw,68px)] font-bold tracking-[-.045em] text-white">从创意到成片，一个工作台。</motion.div>}
        {step >= 2 && <motion.div key="closing-tags" {...reveal} className="mt-[3%] flex gap-3">{["Agent 驱动", "节点可见", "专业剪辑"].map((item) => <span key={item} className="rounded-full border border-cyan-300/20 bg-cyan-300/[0.05] px-4 py-2 text-[clamp(8px,.85vw,14px)] text-cyan-200">{item}</span>)}</motion.div>}
      </AnimatePresence>
    </div>
  )
}

type DetailItem = {
  title: string
  description: string
  usage: string
  badge?: string
}

function DetailSlide({
  step,
  index,
  section,
  title,
  summary,
  items,
  accent = "cyan",
}: SlideProps & {
  index: string
  section: string
  title: string
  summary: string
  items: DetailItem[]
  accent?: "cyan" | "violet" | "amber" | "emerald"
}) {
  const accents = {
    cyan: {text: "text-cyan-300", border: "border-cyan-300/22", bg: "bg-cyan-300/[0.055]", dot: "bg-cyan-300"},
    violet: {text: "text-violet-300", border: "border-violet-300/22", bg: "bg-violet-300/[0.055]", dot: "bg-violet-300"},
    amber: {text: "text-amber-300", border: "border-amber-300/22", bg: "bg-amber-300/[0.055]", dot: "bg-amber-300"},
    emerald: {text: "text-emerald-300", border: "border-emerald-300/22", bg: "bg-emerald-300/[0.055]", dot: "bg-emerald-300"},
  }
  const tone = accents[accent]
  return (
    <div className="absolute inset-0">
      <div className={`absolute left-[5.5%] top-[6.5%] flex items-center gap-3 text-[clamp(9px,.82vw,14px)] font-semibold uppercase tracking-[.22em] ${tone.text}`}>
        <span>{index}</span><span className={`h-px w-8 ${tone.dot} opacity-35`} /><span>{section}</span>
      </div>
      <div className="absolute bottom-[10%] left-[5.5%] top-[16%] w-[31%]">
        <motion.h2 {...reveal} className="text-[clamp(30px,3.4vw,58px)] font-bold leading-[1.08] tracking-[-.05em] text-white">{title}</motion.h2>
        <motion.p {...reveal} transition={{...reveal.transition, delay: .08}} className="mt-[8%] max-w-md text-[clamp(10px,1vw,16px)] leading-[1.85] text-slate-400">{summary}</motion.p>
        <div className={`mt-[11%] rounded-xl border ${tone.border} ${tone.bg} p-[6%]`}>
          <div className={`text-[clamp(7px,.68vw,11px)] font-semibold uppercase tracking-[.16em] ${tone.text}`}>演示提示</div>
          <div className="mt-2 text-[clamp(8px,.8vw,13px)] leading-relaxed text-slate-400">每按一次空格展示一项。先讲能力，再读卡片底部的具体操作方法。</div>
        </div>
      </div>
      <div className="absolute bottom-[9%] right-[5.5%] top-[14%] grid w-[56%] grid-cols-2 gap-[2.2%]">
        <AnimatePresence>
          {items.map((item, itemIndex) => step >= itemIndex + 1 && (
            <motion.div
              key={item.title}
              initial={{opacity: 0, y: 34, scale: .965}}
              animate={{opacity: 1, y: 0, scale: 1}}
              exit={{opacity: 0, y: 16, scale: .98}}
              transition={{duration: .52, ease}}
              className="relative overflow-hidden rounded-[clamp(12px,1.25vw,21px)] border border-white/10 bg-gradient-to-br from-white/[0.055] to-white/[0.018] p-[6%] shadow-[0_24px_65px_rgba(0,0,0,.28)]"
            >
              <div className="flex items-start justify-between gap-3">
                <span className={`flex h-7 w-7 items-center justify-center rounded-lg border ${tone.border} ${tone.bg} text-[clamp(8px,.75vw,12px)] font-semibold ${tone.text}`}>{String(itemIndex + 1).padStart(2, "0")}</span>
                {item.badge && <span className="rounded-full border border-white/10 bg-black/25 px-2.5 py-1 text-[clamp(6px,.6vw,9px)] tracking-[.12em] text-slate-500">{item.badge}</span>}
              </div>
              <div className="mt-[5%] text-[clamp(14px,1.3vw,22px)] font-semibold text-white">{item.title}</div>
              <div className="mt-[3%] text-[clamp(8px,.77vw,12px)] leading-[1.65] text-slate-400">{item.description}</div>
              <div className={`absolute inset-x-[6%] bottom-[7%] rounded-lg border ${tone.border} ${tone.bg} px-[4%] py-[3%] text-[clamp(7px,.68vw,11px)] leading-relaxed text-slate-300`}>
                <span className={`mr-2 font-semibold ${tone.text}`}>怎么用</span>{item.usage}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  )
}

const productJourneyItems: DetailItem[] = [
  {title: "对话理解需求", description: "Agent 接收自然语言、附件和项目上下文，把模糊想法变成可执行创作任务。", usage: "新建项目后直接描述题材、时长、画幅和风格；不确定的部分可以让 Agent 自行发挥。", badge: "CHAT"},
  {title: "画布承载成果", description: "文本、图片、视频、音频都以可编辑节点呈现，产物和依赖关系始终可见。", usage: "切到创作画布，点击节点查看提示词、参考图、参数、运行结果和历史。", badge: "NODE-FIRST"},
  {title: "工作流组织生产", description: "复杂任务可按模板自动拆成输入、规划、生成、检查和交付步骤。", usage: "打开流程模板，填写运行输入，选择运行下一步或运行全部；需要时暂停并单步检查。", badge: "WORKFLOW"},
  {title: "时间线完成交付", description: "生成的视频继续进入帧准确剪辑器，完成裁剪、拼接、混音和导出。", usage: "在视频节点打开剪辑，拖入素材、完成粗剪，最后点击导出时间线成片。", badge: "EDITOR"},
]

const chatItems: DetailItem[] = [
  {title: "流式 Agent 对话", description: "回复、工具调用和节点变化实时出现，不必等整轮任务全部结束。", usage: "在左侧输入需求并发送；长任务可继续排队消息，也可以取消尚未完成的请求。", badge: "STREAM"},
  {title: "附件与项目资料", description: "可上传剧本、文本、图片和媒体，把外部资料放进当前项目上下文。", usage: "点击“上传”或把文件拖入聊天区；需要引用画布图片时先选中节点再发消息。", badge: "UPLOAD"},
  {title: "Plan Mode", description: "先只读分析方案，不立即改画布，适合重要项目先评估路径和风险。", usage: "输入 /plan 进入规划模式；确认方案后输入 /plan execute，再回到正常执行。", badge: "/PLAN"},
  {title: "会话式项目切换", description: "左侧项目栏集中管理项目会话，可直接新建、切换和删除；诊断与重置仍保留为控制命令。", usage: "从左侧项目栏管理项目；用 /help 查看控制命令；/doctor 诊断；全量重置必须按确认流程执行。", badge: "SESSION"},
]

const canvasItems: DetailItem[] = [
  {title: "四类通用节点", description: "text、image、video、audio 是统一创作单元，流程和手动操作共用同一套节点。", usage: "点击画布“+”或右键创建节点；也可从已有节点右侧“+”创建并自动连接下一步。", badge: "4 TYPES"},
  {title: "依赖与参考角色", description: "连线表达生产依赖；参考图可区分视觉参考、源图片和需要 LLM 看图理解的上下文。", usage: "拖动节点手柄连线，或在节点属性的 references 中选择上游；图片提示词需要看图时使用视觉上下文。", badge: "REFERENCES"},
  {title: "节点详情与重跑", description: "提示词、模型、比例、分辨率、时长、输出和错误都集中在独立详情面板。", usage: "点击节点打开详情，修改参数后保存并重新运行；失败只显示错误，不覆盖上一次成功预览。", badge: "INSPECTOR"},
  {title: "高效画布操作", description: "支持拖放布局、选择、多节点删除、缩放、小地图、对齐辅助和历史素材拖回画布。", usage: "滚轮缩放、拖动画布平移；框选多个节点批量移动；从“历史”面板把旧素材重新加入画布。", badge: "CANVAS"},
]

const imageItems: DetailItem[] = [
  {title: "文生图与参考图生成", description: "图片节点可独立生成，也可读取角色、场景、分镜和上传图保持视觉一致性。", usage: "填写提示词，选择模型、画幅、1K/2K/4K 和质量；把参考图片节点连接到当前图片节点后运行。", badge: "IMAGE"},
  {title: "多图历史与主图切换", description: "每次成功生成都会进入历史，失败不会清空当前图片；旧图可重新设为节点主预览。", usage: "打开节点历史，拖动或选择旧图片提升为当前主图；后续节点会读取当前主图作为参考。", badge: "HISTORY"},
  {title: "局部编辑与宫格操作", description: "支持图片编辑候选、宫格拆分、单格提取、放回宫格及融合输出。", usage: "从图片节点菜单选择编辑或宫格操作；先预览候选，确认后再提交，原图会自动归档。", badge: "EDIT"},
  {title: "完整预览与资产保存", description: "图片可在屏幕安全区完整放大查看，也能保存到项目或共享资产库。", usage: "点击图片打开全屏适配预览；使用“加入资产库”并选择分类，之后可跨流程复用。", badge: "ASSET"},
]

const videoItems: DetailItem[] = [
  {title: "文生视频与图生视频", description: "根据模型协议自动识别文字、单图、多参考图、首尾帧等生成模式。", usage: "在视频节点选择模型；没有图片时直接写提示词，有参考图时连接图片节点并选择对应生成模式。", badge: "T2V / I2V"},
  {title: "参数来自模型配置", description: "支持比例、分辨率、时长和参考图数量都从当前模型协议读取，不在界面硬编码。", usage: "先在设置中启用视频 Provider；节点属性会显示该模型真实支持的选项，未声明时长默认 5–15 秒。", badge: "PROTOCOL"},
  {title: "人物、场景与分镜参考", description: "最终视频可以同时引用人物、场景、道具、分镜和故事模板图，而不是只接一张分镜。", usage: "把需要参与生成的图片节点都写入 references，并为每张图设置清晰角色，运行前检查依赖连线。", badge: "MULTI-REF"},
  {title: "异步进度与失败保护", description: "长视频任务持续轮询状态；失败会保留已有成功视频和可编辑节点信息。", usage: "运行后可离开当前节点继续工作；回到节点查看进度或错误，修正参数后在同一节点强制重试。", badge: "ASYNC"},
]

const workflowItems: DetailItem[] = [
  {title: "选择与运行模板", description: "内置和用户模板统一列出，可预览步骤、依赖、输入和最终画布产物。", usage: "打开“流程模板”，选择模板，填写必填输入后点击运行下一步或运行全部。", badge: "TEMPLATE"},
  {title: "单步、连续与暂停", description: "运行 Dock 支持运行指定步骤、运行下一步、运行全部、暂停、删除实例和检查每步输出。", usage: "调试时单步运行；稳定流程用运行全部；发现内容偏离时先暂停，再修改节点或输入。", badge: "RUNTIME"},
  {title: "可视化编辑与保存", description: "可增删步骤、修改依赖、配置循环、输入表单、提示词模板和媒体节点属性。", usage: "进入搭建流程，点选步骤编辑属性；保存当前流程会更新用户模板，内置模板可随时恢复默认副本。", badge: "AUTHOR"},
  {title: "Workflow Build Mode", description: "高级用户可让 Agent 在受限工具面中编写、校验、预览和导出可复用 spec。", usage: "输入 /workflow，描述要搭建的流程；检查画布投影后保存模板，完成后输入 /workflow exit。", badge: "/WORKFLOW"},
]

const editorItems: DetailItem[] = [
  {title: "帧准确粗剪", description: "整数帧是时间线真相源，支持分割、普通裁剪、波纹裁剪、滚动编辑和精确时间码。", usage: "C 启用剃刀；拖动片段边缘裁剪；输入 HH:MM:SS:FF 精确定位；Shift+Delete 波纹删除。", badge: "FRAME"},
  {title: "多轨拼接", description: "动态视频/音频轨支持锁定、同步锁、显隐、静音、独奏、重排、插入和覆盖编辑。", usage: "把素材拖入目标轨；逗号执行插入、句号执行覆盖；S 开关吸附，Up/Down 跳转剪辑点。", badge: "MULTITRACK"},
  {title: "真实画面与波形", description: "时间轴按真实源帧显示缩略图，音轨按真实峰值和 RMS 显示波形，不重复第一帧。", usage: "滚轮在指针位置缩放；最大倍率可查看逐帧内容；拖动音量线和淡入淡出手柄调整声音。", badge: "FILMSTRIP"},
  {title: "基础精修与导出", description: "支持位置、缩放、旋转、透明度、裁剪、交叉溶解、音频交叉淡化和后台 FFmpeg 导出。", usage: "选中片段在检查器调整；在相邻切点添加转场；点击导出时间线成片，可查看进度或取消。", badge: "EXPORT"},
]

const assetItems: DetailItem[] = [
  {title: "项目与共享资产库", description: "生成结果、上传文件和精选素材可保存为项目资产或跨项目共享资产。", usage: "在节点菜单点击加入资产库；选择项目/共享范围和分类，之后从资产库直接添加到画布。", badge: "LIBRARY"},
  {title: "分类与整理", description: "资产支持人物、场景、道具、分镜、视频、音频等分类和文件夹移动。", usage: "打开资产库创建分类，把素材移动到目标分类；保持命名一致便于 Agent 和人工检索。", badge: "CATEGORY"},
  {title: "项目工程面板", description: "可按剧集、类型、阶段或状态查看节点，快速定位未运行、失败和已完成产物。", usage: "切到流程面板，选择布局方式；点击条目回到对应画布节点，复杂项目无需在大画布盲找。", badge: "PROJECT"},
  {title: "本地持久化", description: "SQLite 保存项目结构，storage/assets 保存媒体，刷新、重启和桌面端重开都能恢复。", usage: "正常关闭即可自动保存；重要剪辑序列依靠 revision 防止覆盖，恢复前可查看历史版本。", badge: "LOCAL"},
]

const configItems: DetailItem[] = [
  {title: "LLM 分档配置", description: "强模型、平衡模型、小模型分别服务主创作、常规生产和轻量审查，控制成本与质量。", usage: "设置 → LLM 模型，添加 API Base、Key、模型名和能力；再把 Provider 映射到 strong/balanced/small。", badge: "LLM"},
  {title: "图片/视频/音频 Provider", description: "媒体模型通过声明式协议配置，可接官方 API 或兼容中转站。", usage: "设置 → 对应 Provider，选择协议、填写基础地址和模型名；地址中的版本号按协议要求原样填写。", badge: "MEDIA"},
  {title: "节点级模型选择", description: "同一流程的不同图片或视频步骤可临时选择不同模型，而不污染可复用模板。", usage: "在节点详情或流程步骤属性中选择本次运行模型；未指定时使用当前启用的默认 Provider。", badge: "OVERRIDE"},
  {title: "Agent 行为与原始配置", description: "可调最大迭代、自动归档、Token 显示、默认视图，也能直接校验高级配置文件。", usage: "设置 → Agent 行为调整常用开关；高级配置先在原始文件页验证，再保存并重载。", badge: "SETTINGS"},
]

const reliabilityItems: DetailItem[] = [
  {title: "Token 与缓存监控", description: "每次模型调用尽量记录输入、输出、缓存命中、会话累计和上下文剩余估算。", usage: "聊天栏查看当前上下文；设置 → Agent 诊断查看调用级 usage 和缓存信息。", badge: "TOKENS"},
  {title: "Trace 与 Prompt Dump", description: "关键决策、工具调用、权限、压缩和失败都有项目隔离的诊断记录。", usage: "运行 /doctor 或打开 Agent 诊断，按 run 查看 Prompt、Tool Calls、结果和错误链。", badge: "TRACE"},
  {title: "权限与破坏性确认", description: "删除画布、全量重置等操作必须通过结构化确认，普通聊天文字不能越过确认协议。", usage: "看到确认卡时核对范围再提交；全量重置用 /reset full 后执行 /reset confirm。", badge: "SAFETY"},
  {title: "失败可恢复", description: "图片/视频失败不冲掉旧产物，工作流可从失败步骤继续，剪辑导出取消不破坏序列。", usage: "先查看节点下方错误和诊断；修复模型、输入或参考图后重跑当前步骤，不必重做整个项目。", badge: "RECOVERY"},
]

const extensionItems: DetailItem[] = [
  {title: "Skills", description: "把制作方法、提示词规则和审查标准写成可检索知识，避免塞进常驻系统提示词。", usage: "把自定义 markdown 放到 skills/workflows、skills/prompts 或 skills/review，Agent 会按任务搜索读取。", badge: "SKILL"},
  {title: "工作流插件", description: "插件可增加低频执行能力，例如视频关键帧提取，同时保持核心工具面稳定。", usage: "把插件放入 plugins 并声明 capability；在工作流步骤 extension_config 中使用，缺能力会提前报错。", badge: "PLUGIN"},
  {title: "Web 与 Docker 部署", description: "支持本地开发、生产 Docker Compose、Caddy 网关与持久化目录挂载。", usage: "开发用 bash start.sh；服务器用 docker-compose.prod.yml 构建 API、Web 和 Gateway。", badge: "SERVER"},
  {title: "桌面安装包", description: "Windows、Linux、macOS 可通过 Electron/PyInstaller 打包，并由 GitHub Release 自动分发。", usage: "普通用户下载对应平台安装包；也可用 npm installer 一键安装最新版本。", badge: "DESKTOP"},
]

const usageItems: DetailItem[] = [
  {title: "1. 配置模型", description: "先准备一个 LLM，再按需要配置图片、视频和音频 Provider。", usage: "逐个测试基础地址、模型名和协议；确认设置页显示启用状态后再开始正式项目。", badge: "STEP 1"},
  {title: "2. 新建并描述项目", description: "创建独立项目，把题材、目标平台、时长、画幅、风格和已有素材一次说明。", usage: "示例：做一支 30 秒 16:9 科幻产品片，使用上传的角色图，先出分镜再生成视频。", badge: "STEP 2"},
  {title: "3. 检查节点再生成", description: "先确认文本、人物、场景和分镜，再运行高成本图片与视频节点。", usage: "点击每个节点检查提示词和 references；需要人工确认的节点保持手动生成，确认后再运行。", badge: "STEP 3"},
  {title: "4. 剪辑、导出、归档", description: "把满意片段加入时间线完成粗剪，导出成片，并把可复用素材保存到资产库。", usage: "导出前检查分辨率、帧率、音量和黑场；成片会生成新视频节点，原剪辑序列继续保留。", badge: "STEP 4"},
]

const ProductJourneySlide = ({step}: SlideProps) => <DetailSlide step={step} index="01" section="产品全景" title="一套从需求到成片的完整生产链" summary="OpenReel 不是单一生成按钮，而是把 Agent、节点画布、工作流、模型和剪辑器放在同一个可追溯项目里。" items={productJourneyItems} />
const ChatSlide = ({step}: SlideProps) => <DetailSlide step={step} index="02" section="开始创作" title="先说需求，再逐步确认成果" summary="自然语言是入口，但每一次实际产出都会落到可见节点；既能快速自动化，也能在关键步骤人工接管。" items={chatItems} accent="violet" />
const CanvasDetailSlide = ({step}: SlideProps) => <DetailSlide step={step} index="03" section="节点画布" title="产物、参数与依赖都看得见" summary="画布是创作真相源。你可以直接编辑任何节点，也可以让 Agent 或工作流继续消费这些结果。" items={canvasItems} />
const ImageDetailSlide = ({step}: SlideProps) => <DetailSlide step={step} index="04" section="图片能力" title="生成、编辑、历史与复用" summary="人物、场景、道具、分镜、首尾帧和风格板都使用统一图片节点表达，操作一致、历史清晰。" items={imageItems} accent="violet" />
const VideoDetailSlide = ({step}: SlideProps) => <DetailSlide step={step} index="05" section="视频能力" title="按模型真实能力组织生成" summary="视频参数由协议和模型配置驱动；多参考图、异步任务和失败恢复都纳入同一节点生命周期。" items={videoItems} accent="amber" />
const WorkflowDetailSlide = ({step}: SlideProps) => <DetailSlide step={step} index="06" section="工作流" title="可复用，也可随时人工接管" summary="从选择模板到可视化编辑，再到受限的 Workflow Build Mode，复杂生产流程可以重复使用并持续迭代。" items={workflowItems} accent="emerald" />
const EditorDetailSlide = ({step}: SlideProps) => <DetailSlide step={step} index="07" section="剪辑器" title="真正能完成基础粗剪的时间线" summary="围绕裁剪和拼接两类核心工作构建，使用整数帧、真实缩略图和真实波形，操作习惯对齐 Premiere。" items={editorItems} accent="amber" />
const AssetSlide = ({step}: SlideProps) => <DetailSlide step={step} index="08" section="项目与资产" title="让素材从一次生成变成长期资产" summary="项目结构、媒体历史、分类资产库和工程面板共同承接复杂项目，不再依赖临时下载文件。" items={assetItems} accent="emerald" />
const ConfigSlide = ({step}: SlideProps) => <DetailSlide step={step} index="09" section="模型配置" title="模型可替换，协议可扩展" summary="LLM 与媒体 Provider 都可在运行时配置；模型能力进入节点属性，而不是散落在前端硬编码中。" items={configItems} accent="violet" />
const ReliabilitySlide = ({step}: SlideProps) => <DetailSlide step={step} index="10" section="可靠性" title="每一次调用都能解释、追踪和恢复" summary="Token、Trace、权限确认和失败恢复是产品能力，不是只在开发环境里存在的日志。" items={reliabilityItems} />
const ExtensionSlide = ({step}: SlideProps) => <DetailSlide step={step} index="11" section="扩展与部署" title="既能自己用，也能持续扩展" summary="Skills、工作流插件和多种部署方式让产品适配不同团队，不必修改核心 Agent 才能增加制作方法。" items={extensionItems} accent="emerald" />
const UsageSlide = ({step}: SlideProps) => <DetailSlide step={step} index="12" section="推荐用法" title="第一次使用，按这四步完成" summary="先把模型接通，再用一个独立项目完成从需求、节点检查、媒体生成到剪辑交付的完整闭环。" items={usageItems} accent="amber" />

const slides = [
  {title: "开场", maxStep: 3, component: CoverSlide},
  {title: "产品全景", maxStep: 4, component: ProductJourneySlide},
  {title: "开始创作", maxStep: 4, component: ChatSlide},
  {title: "节点画布", maxStep: 4, component: CanvasDetailSlide},
  {title: "节点工作流演示", maxStep: 4, component: WorkflowSlide},
  {title: "图片能力", maxStep: 4, component: ImageDetailSlide},
  {title: "视觉生成演示", maxStep: 3, component: VisualSlide},
  {title: "视频能力", maxStep: 4, component: VideoDetailSlide},
  {title: "工作流", maxStep: 4, component: WorkflowDetailSlide},
  {title: "剪辑器功能", maxStep: 4, component: EditorDetailSlide},
  {title: "剪辑器演示", maxStep: 3, component: EditorSlide},
  {title: "项目与资产", maxStep: 4, component: AssetSlide},
  {title: "模型配置", maxStep: 4, component: ConfigSlide},
  {title: "可靠性", maxStep: 4, component: ReliabilitySlide},
  {title: "扩展与部署", maxStep: 4, component: ExtensionSlide},
  {title: "推荐用法", maxStep: 4, component: UsageSlide},
  {title: "收尾", maxStep: 2, component: ClosingSlide},
]

export function OpenReelPresentation() {
  const [slideIndex, setSlideIndex] = useState(0)
  const [step, setStep] = useState(0)
  const stageRef = useRef<HTMLDivElement>(null)
  const slide = slides[slideIndex]
  const Slide = slide.component

  const next = useCallback(() => {
    if (step < slide.maxStep) setStep((value) => value + 1)
    else if (slideIndex < slides.length - 1) { setSlideIndex((value) => value + 1); setStep(0) }
  }, [slide.maxStep, slideIndex, step])

  const previous = useCallback(() => {
    if (step > 0) setStep((value) => value - 1)
    else if (slideIndex > 0) { const nextIndex = slideIndex - 1; setSlideIndex(nextIndex); setStep(slides[nextIndex].maxStep) }
  }, [slideIndex, step])

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen()
    else if (stageRef.current) void stageRef.current.requestFullscreen()
  }, [])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (["ArrowRight", "PageDown", " ", "Enter"].includes(event.key)) { event.preventDefault(); next() }
      else if (["ArrowLeft", "PageUp"].includes(event.key)) { event.preventDefault(); previous() }
      else if (event.key === "Home") { event.preventDefault(); setSlideIndex(0); setStep(0) }
      else if (event.key === "End") { event.preventDefault(); setSlideIndex(slides.length - 1); setStep(slides[slides.length - 1].maxStep) }
      else if (event.key.toLowerCase() === "f") { event.preventDefault(); toggleFullscreen() }
    }
    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [next, previous, toggleFullscreen])

  const totalBuilds = useMemo(() => slides.reduce((sum, item) => sum + item.maxStep + 1, 0), [])
  const currentBuild = useMemo(() => slides.slice(0, slideIndex).reduce((sum, item) => sum + item.maxStep + 1, 0) + step + 1, [slideIndex, step])
  const progress = currentBuild / totalBuilds

  return (
    <main className="flex min-h-screen items-center justify-center overflow-hidden bg-[#05070a] px-3 py-1 text-white sm:px-5 sm:py-2">
      <div ref={stageRef} className="group relative aspect-video w-full max-w-[min(1760px,calc(100vw-24px))] overflow-hidden rounded-xl border border-white/10 bg-[#07090d] font-sans shadow-[0_40px_140px_rgba(0,0,0,.65)] sm:rounded-2xl">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_14%_18%,rgba(77,227,255,.10),transparent_26%),radial-gradient(circle_at_82%_74%,rgba(139,124,255,.12),transparent_29%),linear-gradient(140deg,#080a0f,#0a0d13_52%,#07090d)]" />
        <div className="absolute inset-0 opacity-[.14] [background-image:linear-gradient(rgba(255,255,255,.045)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.045)_1px,transparent_1px)] [background-size:54px_54px] [mask-image:linear-gradient(180deg,black,transparent_88%)]" />

        <AnimatePresence mode="wait">
          <motion.div key={slideIndex} initial={{opacity: 0, x: 60}} animate={{opacity: 1, x: 0}} exit={{opacity: 0, x: -50}} transition={{duration: .45, ease}} className="absolute inset-0">
            <Slide step={step} />
          </motion.div>
        </AnimatePresence>

        <div className="absolute left-0 right-0 top-0 h-1 bg-white/[0.06]"><motion.div animate={{width: `${progress * 100}%`}} transition={{duration: .35, ease}} className="h-full bg-gradient-to-r from-cyan-300 via-violet-400 to-amber-300 shadow-[0_0_14px_rgba(77,227,255,.55)]" /></div>

        <div className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-3 bg-gradient-to-t from-black/80 via-black/35 to-transparent px-[3%] pb-[2.2%] pt-[5%] opacity-100 transition-opacity md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
          <div className="flex min-w-0 items-center gap-2">
            <div className="flex max-w-[38vw] items-center gap-1.5 overflow-hidden">
              {slides.map((item, index) => <button key={item.title} type="button" onClick={() => {setSlideIndex(index); setStep(0)}} className={`h-2 shrink-0 rounded-full transition-all ${index === slideIndex ? "w-6 bg-cyan-300" : "w-1.5 bg-white/25 hover:bg-white/50"}`} aria-label={`跳到${item.title}`} />)}
            </div>
            <span className="ml-2 text-[clamp(8px,.7vw,12px)] text-slate-400">{slideIndex + 1}/{slides.length} · {slide.title} · 动画 {step + 1}/{slide.maxStep + 1}</span>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={previous} disabled={slideIndex === 0 && step === 0} className="rounded-lg border border-white/10 bg-black/35 px-3 py-2 text-[clamp(8px,.75vw,12px)] text-slate-300 backdrop-blur hover:bg-white/10 disabled:opacity-30">← 上一步</button>
            <button type="button" onClick={next} disabled={slideIndex === slides.length - 1 && step === slide.maxStep} className="rounded-lg border border-cyan-300/20 bg-cyan-300/10 px-3 py-2 text-[clamp(8px,.75vw,12px)] text-cyan-100 backdrop-blur hover:bg-cyan-300/20 disabled:opacity-30">下一步 →</button>
            <button type="button" onClick={toggleFullscreen} className="rounded-lg border border-white/10 bg-black/35 px-3 py-2 text-[clamp(8px,.75vw,12px)] text-slate-300 backdrop-blur hover:bg-white/10">全屏 F</button>
          </div>
        </div>

        <div className="pointer-events-none absolute right-[2.5%] top-[2.8%] rounded-full border border-white/10 bg-black/25 px-3 py-1.5 text-[clamp(7px,.62vw,10px)] tracking-[.12em] text-slate-500 backdrop-blur">SPACE / → 推进 · ← 回退</div>
      </div>
    </main>
  )
}
