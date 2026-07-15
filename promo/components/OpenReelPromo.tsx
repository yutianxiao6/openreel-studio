import type {CSSProperties, ReactNode} from "react"
import {
  AbsoluteFill,
  Easing,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion"

const COLORS = {
  ink: "#07090d",
  panel: "#0e1219",
  cyan: "#4de3ff",
  cyanSoft: "#8beeff",
  violet: "#8b7cff",
  amber: "#ffca73",
  white: "#f5f7fb",
  muted: "#8992a3",
}

const fontFamily = 'Inter, "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif'

function clamp(value: number, input: [number, number], output: [number, number]) {
  return interpolate(value, input, output, {extrapolateLeft: "clamp", extrapolateRight: "clamp"})
}

function sceneOpacity(frame: number, start: number, end: number, fade = 16) {
  return Math.min(
    clamp(frame, [start, start + fade], [0, 1]),
    clamp(frame, [end - fade, end], [1, 0]),
  )
}

function BrandMark({size = 58}: {size?: number}) {
  return (
    <div style={{position: "relative", width: size, height: size}}>
      <div
        style={{
          position: "absolute",
          inset: 2,
          borderRadius: "50%",
          border: `${Math.max(3, size * 0.07)}px solid ${COLORS.cyan}`,
          boxShadow: `0 0 ${size * 0.5}px rgba(77,227,255,.32), inset 0 0 ${size * 0.25}px rgba(77,227,255,.14)`,
        }}
      />
      {[0, 90, 180, 270].map((rotation) => (
        <div
          key={rotation}
          style={{
            position: "absolute",
            left: "46%",
            top: "8%",
            width: "9%",
            height: "18%",
            borderRadius: 99,
            background: COLORS.white,
            transformOrigin: `50% ${size * 0.42}px`,
            transform: `rotate(${rotation}deg)`,
            opacity: 0.86,
          }}
        />
      ))}
      <div
        style={{
          position: "absolute",
          inset: "32%",
          borderRadius: "50%",
          background: COLORS.violet,
          boxShadow: "0 0 18px rgba(139,124,255,.7)",
        }}
      />
    </div>
  )
}

function WindowFrame({children, style}: {children: ReactNode; style?: CSSProperties}) {
  return (
    <div
      style={{
        overflow: "hidden",
        borderRadius: 24,
        border: "1px solid rgba(255,255,255,.12)",
        background: "rgba(11,14,20,.94)",
        boxShadow: "0 38px 110px rgba(0,0,0,.5)",
        ...style,
      }}
    >
      <div
        style={{
          height: 52,
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "0 20px",
          borderBottom: "1px solid rgba(255,255,255,.08)",
          background: "rgba(255,255,255,.025)",
        }}
      >
        {["#ff6b6b", "#ffd166", "#48d597"].map((color) => (
          <div key={color} style={{width: 11, height: 11, borderRadius: "50%", background: color, opacity: 0.75}} />
        ))}
        <div style={{marginLeft: 14, fontSize: 13, letterSpacing: ".12em", color: "#667084"}}>OPENREEL STUDIO</div>
      </div>
      {children}
    </div>
  )
}

function NodeCard({
  title,
  kind,
  color,
  style,
  children,
}: {
  title: string
  kind: string
  color: string
  style?: CSSProperties
  children?: ReactNode
}) {
  return (
    <div
      style={{
        position: "absolute",
        width: 250,
        minHeight: 132,
        borderRadius: 17,
        border: `1px solid ${color}55`,
        background: "linear-gradient(145deg,rgba(20,25,35,.98),rgba(10,13,19,.98))",
        boxShadow: `0 22px 65px rgba(0,0,0,.38), 0 0 34px ${color}16`,
        padding: 18,
        ...style,
      }}
    >
      <div style={{display: "flex", alignItems: "center", gap: 10}}>
        <div style={{width: 9, height: 9, borderRadius: 99, background: color, boxShadow: `0 0 12px ${color}`}} />
        <div style={{fontSize: 12, letterSpacing: ".14em", color: `${color}cc`}}>{kind}</div>
      </div>
      <div style={{marginTop: 12, fontSize: 20, fontWeight: 680, color: COLORS.white}}>{title}</div>
      <div style={{marginTop: 12}}>{children}</div>
    </div>
  )
}

function Lines({progress}: {progress: number}) {
  const path = "M 318 210 C 390 210, 390 125, 474 125 M 318 210 C 390 210, 390 294, 474 294 M 724 125 C 800 125, 800 210, 878 210 M 724 294 C 800 294, 800 210, 878 210"
  return (
    <svg width="1180" height="430" viewBox="0 0 1180 430" style={{position: "absolute", inset: 0, overflow: "visible"}}>
      <path d={path} fill="none" stroke="rgba(77,227,255,.13)" strokeWidth="3" />
      <path
        d={path}
        fill="none"
        stroke={COLORS.cyan}
        strokeWidth="3"
        strokeLinecap="round"
        pathLength="1"
        strokeDasharray="1"
        strokeDashoffset={1 - progress}
        style={{filter: "drop-shadow(0 0 8px rgba(77,227,255,.7))"}}
      />
    </svg>
  )
}

function HeroScene({frame}: {frame: number}) {
  const local = frame
  const {fps} = useVideoConfig()
  const settle = spring({frame: local, fps, config: {damping: 16, stiffness: 90}})
  const line2 = spring({frame: local - 28, fps, config: {damping: 18, stiffness: 100}})
  const chip = spring({frame: local - 58, fps, config: {damping: 15, stiffness: 130}})
  return (
    <AbsoluteFill style={{opacity: clamp(frame, [144, 160], [1, 0]), justifyContent: "center", alignItems: "center"}}>
      <div style={{display: "flex", alignItems: "center", gap: 20, transform: `translateY(${(1 - settle) * 36}px)`}}>
        <BrandMark size={72} />
        <div style={{fontSize: 24, fontWeight: 720, letterSpacing: ".04em", color: COLORS.white}}>OpenReel Studio</div>
      </div>
      <div style={{marginTop: 54, textAlign: "center"}}>
        <div
          style={{
            fontSize: 84,
            lineHeight: 1.06,
            fontWeight: 760,
            letterSpacing: "-.055em",
            color: COLORS.white,
            transform: `translateY(${(1 - settle) * 52}px)`,
          }}
        >
          把一个想法
        </div>
        <div
          style={{
            marginTop: 10,
            fontSize: 84,
            lineHeight: 1.06,
            fontWeight: 760,
            letterSpacing: "-.055em",
            background: `linear-gradient(90deg,${COLORS.cyanSoft},${COLORS.violet},${COLORS.amber})`,
            WebkitBackgroundClip: "text",
            color: "transparent",
            transform: `translateY(${(1 - line2) * 48}px)`,
          }}
        >
          变成一支完整视频
        </div>
      </div>
      <div
        style={{
          marginTop: 56,
          borderRadius: 999,
          border: "1px solid rgba(255,255,255,.14)",
          background: "rgba(255,255,255,.055)",
          padding: "16px 26px",
          fontSize: 18,
          color: "#b6bfce",
          transform: `scale(${0.88 + chip * 0.12})`,
          opacity: 0.72 + chip * 0.28,
        }}
      >
        对话式创作 · 节点工作流 · 专业剪辑
      </div>
    </AbsoluteFill>
  )
}

function WorkflowScene({frame}: {frame: number}) {
  const local = frame - 135
  const {fps} = useVideoConfig()
  const panel = spring({frame: local, fps, config: {damping: 18, stiffness: 95}})
  const userMessage = spring({frame: local - 18, fps, config: {damping: 18, stiffness: 120}})
  const agentMessage = spring({frame: local - 48, fps, config: {damping: 18, stiffness: 120}})
  const nodes = [70, 92, 112, 132].map((delay) => spring({frame: local - delay, fps, config: {damping: 17, stiffness: 120}}))
  const lineProgress = clamp(local, [110, 175], [0, 1])
  return (
    <AbsoluteFill style={{opacity: sceneOpacity(frame, 135, 350), justifyContent: "center", alignItems: "center"}}>
      <div style={{position: "absolute", top: 84, left: 110, fontSize: 18, letterSpacing: ".22em", color: COLORS.cyan}}>01 / 从一句话开始</div>
      <WindowFrame style={{width: 1570, height: 790, transform: `translateY(${(1 - panel) * 60}px) scale(${0.94 + panel * 0.06})`, opacity: panel}}>
        <div style={{display: "grid", gridTemplateColumns: "390px 1fr", height: "calc(100% - 52px)"}}>
          <div style={{position: "relative", padding: 28, borderRight: "1px solid rgba(255,255,255,.08)", background: "rgba(255,255,255,.018)"}}>
            <div style={{fontSize: 14, color: "#727c8d"}}>与 OpenReel Agent 对话</div>
            <div
              style={{
                marginTop: 40,
                marginLeft: 42,
                borderRadius: "18px 18px 4px 18px",
                padding: "16px 18px",
                background: COLORS.white,
                color: "#151922",
                fontSize: 17,
                lineHeight: 1.5,
                transform: `translateY(${(1 - userMessage) * 24}px)`,
                opacity: userMessage,
              }}
            >
              做一支 30 秒的未来城市动作短片
            </div>
            <div
              style={{
                marginTop: 24,
                display: "flex",
                gap: 13,
                transform: `translateY(${(1 - agentMessage) * 24}px)`,
                opacity: agentMessage,
              }}
            >
              <BrandMark size={38} />
              <div style={{flex: 1, borderRadius: "5px 17px 17px 17px", border: "1px solid rgba(77,227,255,.18)", background: "rgba(77,227,255,.055)", padding: 16, fontSize: 15, lineHeight: 1.6, color: "#b9c5d5"}}>
                已拆解创意，正在创建剧情、角色、分镜与视频节点。
                <div style={{marginTop: 14, height: 3, borderRadius: 99, background: "rgba(255,255,255,.08)", overflow: "hidden"}}>
                  <div style={{height: "100%", width: `${clamp(local, [54, 150], [0, 100])}%`, background: `linear-gradient(90deg,${COLORS.cyan},${COLORS.violet})`}} />
                </div>
              </div>
            </div>
          </div>
          <div style={{position: "relative", overflow: "hidden", background: "radial-gradient(circle at 50% 48%,rgba(77,227,255,.055),transparent 34%)"}}>
            <div style={{position: "absolute", inset: 0, opacity: 0.14, backgroundImage: "linear-gradient(rgba(255,255,255,.12) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.12) 1px,transparent 1px)", backgroundSize: "38px 38px"}} />
            <div style={{position: "absolute", left: 95, top: 95, width: 1180, height: 430}}>
              <Lines progress={lineProgress} />
              <NodeCard title="创意剧情" kind="TEXT" color={COLORS.cyan} style={{left: 68, top: 145, opacity: nodes[0], transform: `scale(${0.86 + nodes[0] * 0.14})`}}>
                <div style={{height: 6, width: "90%", borderRadius: 99, background: "#384154"}} />
                <div style={{height: 6, width: "64%", marginTop: 9, borderRadius: 99, background: "#293142"}} />
              </NodeCard>
              <NodeCard title="角色参考" kind="IMAGE" color={COLORS.violet} style={{left: 474, top: 60, opacity: nodes[1], transform: `scale(${0.86 + nodes[1] * 0.14})`}}>
                <div style={{height: 34, borderRadius: 8, background: "linear-gradient(120deg,#1c3045,#584f9c 52%,#172638)"}} />
              </NodeCard>
              <NodeCard title="宫格分镜" kind="IMAGE" color={COLORS.amber} style={{left: 474, top: 229, opacity: nodes[2], transform: `scale(${0.86 + nodes[2] * 0.14})`}}>
                <div style={{display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 4}}>{[0,1,2,3].map((item) => <div key={item} style={{height: 32, borderRadius: 4, background: `hsl(${205 + item * 15} 34% ${20 + item * 4}%)`}} />)}</div>
              </NodeCard>
              <NodeCard title="视频成片" kind="VIDEO" color="#69f0ae" style={{left: 878, top: 145, opacity: nodes[3], transform: `scale(${0.86 + nodes[3] * 0.14})`}}>
                <div style={{height: 34, borderRadius: 8, background: "linear-gradient(110deg,#16343d,#1a6e72,#564584)"}} />
              </NodeCard>
            </div>
          </div>
        </div>
      </WindowFrame>
    </AbsoluteFill>
  )
}

function VisualScene({frame}: {frame: number}) {
  const local = frame - 325
  const {fps} = useVideoConfig()
  const cards = [0, 18, 36].map((delay) => spring({frame: local - delay, fps, config: {damping: 17, stiffness: 100}}))
  const scan = clamp(local % 90, [0, 90], [-20, 120])
  const cardData = [
    {label: "角色一致性", tone: "linear-gradient(145deg,#101a2c,#3f3679 48%,#b06974)", accent: COLORS.violet},
    {label: "电影级分镜", tone: "linear-gradient(145deg,#10262d,#187079 48%,#d28d5d)", accent: COLORS.cyan},
    {label: "视频生成", tone: "linear-gradient(145deg,#182330,#284e65 44%,#684478)", accent: COLORS.amber},
  ]
  return (
    <AbsoluteFill style={{opacity: sceneOpacity(frame, 325, 535), justifyContent: "center"}}>
      <div style={{position: "absolute", top: 88, left: 120}}>
        <div style={{fontSize: 18, letterSpacing: ".22em", color: COLORS.violet}}>02 / 视觉生产</div>
        <div style={{marginTop: 18, fontSize: 58, fontWeight: 740, letterSpacing: "-.04em", color: COLORS.white}}>每一个节点，都是可见的创作成果</div>
      </div>
      <div style={{display: "flex", gap: 34, padding: "150px 118px 0"}}>
        {cardData.map((card, index) => (
          <div key={card.label} style={{position: "relative", flex: 1, height: 570, borderRadius: 26, overflow: "hidden", border: "1px solid rgba(255,255,255,.12)", background: card.tone, boxShadow: "0 34px 90px rgba(0,0,0,.42)", opacity: cards[index], transform: `translateY(${(1 - cards[index]) * 70}px) rotate(${(index - 1) * 1.2}deg)`}}>
            <div style={{position: "absolute", inset: 0, background: "radial-gradient(circle at 50% 28%,rgba(255,255,255,.2),transparent 23%),linear-gradient(180deg,transparent 40%,rgba(4,6,10,.86))"}} />
            <div style={{position: "absolute", left: `${scan}%`, top: 0, bottom: 0, width: 2, background: "rgba(255,255,255,.6)", boxShadow: "0 0 24px rgba(255,255,255,.7)", opacity: 0.32}} />
            <div style={{position: "absolute", top: 32, left: 32, borderRadius: 999, border: `1px solid ${card.accent}66`, background: "rgba(7,9,13,.48)", padding: "9px 14px", fontSize: 12, letterSpacing: ".14em", color: card.accent}}>GENERATING</div>
            <div style={{position: "absolute", left: 34, right: 34, bottom: 34}}>
              <div style={{fontSize: 28, fontWeight: 700, color: COLORS.white}}>{card.label}</div>
              <div style={{marginTop: 13, fontSize: 14, lineHeight: 1.6, color: "#aeb8c8"}}>模型配置、提示词与参考图保持在同一个可追溯节点中。</div>
            </div>
          </div>
        ))}
      </div>
    </AbsoluteFill>
  )
}

function Waveform() {
  const heights = [18,30,46,22,64,44,28,58,78,34,52,26,68,42,30,54,74,38,24,62,48,30,68,40,22,56,72,36,48,24,64,42,30,58,76,32,50,26,66,44]
  return <div style={{display: "flex", alignItems: "center", gap: 5, height: 74}}>{heights.map((height, index) => <div key={index} style={{width: 5, height, borderRadius: 99, background: index % 3 === 0 ? COLORS.cyan : "rgba(77,227,255,.42)"}} />)}</div>
}

function EditorScene({frame}: {frame: number}) {
  const local = frame - 510
  const {fps} = useVideoConfig()
  const enter = spring({frame: local, fps, config: {damping: 18, stiffness: 90}})
  const playhead = clamp(local, [35, 155], [7, 88])
  return (
    <AbsoluteFill style={{opacity: sceneOpacity(frame, 510, 675), justifyContent: "center", alignItems: "center"}}>
      <div style={{position: "absolute", top: 78, left: 110, fontSize: 18, letterSpacing: ".22em", color: COLORS.amber}}>03 / 专业剪辑</div>
      <WindowFrame style={{width: 1640, height: 820, transform: `translateY(${(1 - enter) * 70}px) scale(${0.95 + enter * 0.05})`, opacity: enter}}>
        <div style={{display: "grid", gridTemplateRows: "450px 1fr", height: "calc(100% - 52px)", background: "#090b0f"}}>
          <div style={{display: "grid", gridTemplateColumns: "290px 1fr 330px", borderBottom: "1px solid rgba(255,255,255,.1)"}}>
            <div style={{padding: 24, borderRight: "1px solid rgba(255,255,255,.08)"}}>
              <div style={{fontSize: 12, letterSpacing: ".15em", color: "#697384"}}>PROJECT</div>
              {["城市航拍.mp4", "角色近景.mp4", "动作镜头.mp4", "环境声.wav"].map((item, index) => (
                <div key={item} style={{marginTop: 18, display: "flex", alignItems: "center", gap: 12, fontSize: 14, color: index === 1 ? COLORS.white : "#858f9f"}}>
                  <div style={{width: 42, height: 28, borderRadius: 5, background: `linear-gradient(135deg,hsl(${195 + index * 24} 35% 22%),hsl(${250 + index * 16} 30% 34%))`}} />
                  {item}
                </div>
              ))}
            </div>
            <div style={{display: "flex", alignItems: "center", justifyContent: "center", padding: 26}}>
              <div style={{position: "relative", width: 690, aspectRatio: "16 / 9", overflow: "hidden", borderRadius: 6, background: "linear-gradient(140deg,#101c2d,#1d6572 44%,#735168 78%,#d0865b)", boxShadow: "0 24px 70px rgba(0,0,0,.55)"}}>
                <div style={{position: "absolute", inset: 0, background: "radial-gradient(circle at 60% 35%,rgba(255,220,180,.32),transparent 17%),linear-gradient(180deg,transparent 52%,rgba(4,7,12,.7))"}} />
                <div style={{position: "absolute", left: 34, bottom: 28, fontSize: 13, letterSpacing: ".14em", color: "rgba(255,255,255,.78)"}}>PROGRAM · 00:00:18:12</div>
              </div>
            </div>
            <div style={{padding: 24, borderLeft: "1px solid rgba(255,255,255,.08)"}}>
              <div style={{fontSize: 12, letterSpacing: ".15em", color: "#697384"}}>CLIP CONTROLS</div>
              {["缩放  108%", "位置 X  +2.4", "音量  -1.5 dB", "淡入  12 帧"].map((item, index) => (
                <div key={item} style={{marginTop: 19, paddingBottom: 14, borderBottom: "1px solid rgba(255,255,255,.06)", fontSize: 14, color: index === 2 ? COLORS.cyan : "#a2aaba"}}>{item}</div>
              ))}
            </div>
          </div>
          <div style={{position: "relative", padding: "28px 28px 24px 94px", overflow: "hidden"}}>
            <div style={{position: "absolute", left: 20, top: 28, bottom: 20, width: 60, borderRight: "1px solid rgba(255,255,255,.08)", color: "#657083", fontSize: 12}}>
              <div style={{marginTop: 36}}>V1</div><div style={{marginTop: 72}}>V2</div><div style={{marginTop: 72}}>A1</div>
            </div>
            <div style={{height: 26, display: "flex", justifyContent: "space-between", color: "#555f70", fontFamily: "monospace", fontSize: 11}}><span>00:00</span><span>00:05</span><span>00:10</span><span>00:15</span><span>00:20</span></div>
            <div style={{display: "grid", gridTemplateColumns: "1.15fr .85fr 1.2fr", gap: 4, height: 58}}>
              {["#1d5665", "#654b7a", "#855f4c"].map((color, index) => <div key={color} style={{position: "relative", borderRadius: 5, overflow: "hidden", background: color, border: "1px solid rgba(255,255,255,.12)"}}><div style={{position: "absolute", inset: 0, backgroundImage: "linear-gradient(90deg,transparent 48%,rgba(255,255,255,.13) 50%,transparent 52%)", backgroundSize: `${38 + index * 8}px 100%`}} /></div>)}
            </div>
            <div style={{marginTop: 7, display: "grid", gridTemplateColumns: ".7fr 1.25fr 1.25fr", gap: 4, height: 58}}>
              {["#4c3f66", "#325468", "#62485e"].map((color) => <div key={color} style={{borderRadius: 5, background: color, border: "1px solid rgba(255,255,255,.1)"}} />)}
            </div>
            <div style={{marginTop: 7, height: 74, borderRadius: 5, padding: "0 18px", background: "rgba(32,117,130,.22)", border: "1px solid rgba(77,227,255,.18)"}}><Waveform /></div>
            <div style={{position: "absolute", top: 48, bottom: 18, left: `${94 + playhead * 0.01 * (1640 - 122)}px`, width: 2, background: "#f4f7fb", boxShadow: "0 0 10px rgba(255,255,255,.45)"}}><div style={{position: "absolute", top: -7, left: -6, width: 14, height: 14, background: "#f4f7fb", transform: "rotate(45deg)"}} /></div>
          </div>
        </div>
      </WindowFrame>
    </AbsoluteFill>
  )
}

function ClosingScene({frame}: {frame: number}) {
  const local = frame - 650
  const {fps} = useVideoConfig()
  const enter = spring({frame: local, fps, config: {damping: 17, stiffness: 95}})
  return (
    <AbsoluteFill style={{opacity: sceneOpacity(frame, 650, 720, 10), alignItems: "center", justifyContent: "center"}}>
      <div style={{display: "flex", alignItems: "center", gap: 22, transform: `scale(${0.88 + enter * 0.12})`, opacity: enter}}><BrandMark size={84} /><div style={{fontSize: 34, fontWeight: 740, letterSpacing: ".02em", color: COLORS.white}}>OpenReel Studio</div></div>
      <div style={{marginTop: 46, fontSize: 62, fontWeight: 740, letterSpacing: "-.04em", color: COLORS.white, opacity: enter}}>从创意到成片，一个工作台。</div>
      <div style={{marginTop: 34, display: "flex", gap: 12, opacity: clamp(local, [24, 44], [0, 1])}}>
        {["Agent 驱动", "节点可见", "专业剪辑"].map((item) => <div key={item} style={{borderRadius: 999, border: "1px solid rgba(77,227,255,.24)", background: "rgba(77,227,255,.055)", padding: "11px 18px", fontSize: 14, color: COLORS.cyanSoft}}>{item}</div>)}
      </div>
    </AbsoluteFill>
  )
}

export function OpenReelPromo() {
  const frame = useCurrentFrame()
  const {durationInFrames} = useVideoConfig()
  const progress = frame / Math.max(1, durationInFrames - 1)
  const driftX = interpolate(frame, [0, durationInFrames], [-4, 6], {easing: Easing.inOut(Easing.quad)})

  return (
    <AbsoluteFill style={{fontFamily, background: COLORS.ink, color: COLORS.white, overflow: "hidden"}}>
      <AbsoluteFill style={{transform: `translateX(${driftX}px) scale(1.015)`, background: "radial-gradient(circle at 16% 18%,rgba(77,227,255,.10),transparent 25%),radial-gradient(circle at 82% 72%,rgba(139,124,255,.12),transparent 28%),linear-gradient(140deg,#080a0f,#0a0d13 52%,#07090d)"}} />
      <AbsoluteFill style={{opacity: 0.18, backgroundImage: "linear-gradient(rgba(255,255,255,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.045) 1px,transparent 1px)", backgroundSize: "72px 72px", maskImage: "linear-gradient(180deg,black,transparent 85%)"}} />
      <HeroScene frame={frame} />
      <WorkflowScene frame={frame} />
      <VisualScene frame={frame} />
      <EditorScene frame={frame} />
      <ClosingScene frame={frame} />
      <div style={{position: "absolute", left: 0, right: 0, bottom: 0, height: 4, background: "rgba(255,255,255,.07)"}}><div style={{height: "100%", width: `${progress * 100}%`, background: `linear-gradient(90deg,${COLORS.cyan},${COLORS.violet},${COLORS.amber})`, boxShadow: "0 0 18px rgba(77,227,255,.5)"}} /></div>
    </AbsoluteFill>
  )
}
