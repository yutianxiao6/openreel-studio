"use client"

interface ChangeItem {
  field: string
  label: string
  before: string
  after: string
}

function isImageUrl(value: string): boolean {
  return /\.(png|jpg|jpeg|gif|webp)(\?|$)/i.test(value) || value.startsWith("data:image/")
}

function ChangeRow({ c }: { c: ChangeItem }) {
  const isImg = c.field === "image" || isImageUrl(c.before) || isImageUrl(c.after)

  if (isImg) {
    return (
      <div>
        <div className="mb-1 text-zinc-500">{c.label}</div>
        <div className="flex items-start gap-2">
          <div className="max-w-[45%] rounded border border-red-400/20 bg-red-400/5 p-1">
            <div className="mb-0.5 text-[10px] text-red-400/60">之前</div>
            {c.before ? (
              <img src={c.before} alt="修改前" className="max-h-32 w-full rounded object-contain opacity-70" />
            ) : (
              <div className="flex h-16 items-center justify-center text-zinc-600">(无)</div>
            )}
          </div>
          <span className="shrink-0 pt-4 text-zinc-600">→</span>
          <div className="max-w-[45%] rounded border border-emerald-400/20 bg-emerald-400/5 p-1">
            <div className="mb-0.5 text-[10px] text-emerald-400/60">之后</div>
            {c.after ? (
              <img src={c.after} alt="修改后" className="max-h-32 w-full rounded object-contain" />
            ) : (
              <div className="flex h-16 items-center justify-center text-zinc-600">(无)</div>
            )}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-1 text-zinc-500">{c.label}</div>
      <div className="flex items-center gap-2">
        <span className="max-w-[42%] break-all rounded bg-red-400/10 px-2 py-0.5 text-red-300 line-through">
          {c.before || "(空)"}
        </span>
        <span className="shrink-0 text-zinc-600">→</span>
        <span className="max-w-[42%] break-all rounded bg-emerald-400/10 px-2 py-0.5 text-emerald-300">
          {c.after || "(空)"}
        </span>
      </div>
    </div>
  )
}

export function ChangeCard({ tool, changes }: { tool: string; changes: ChangeItem[] }) {
  if (!changes.length) return null

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-white/10 bg-[var(--studio-panel)] text-xs">
      {changes.map((c, i) => (
        <div key={i} className="border-b border-white/5 px-3 py-2 last:border-b-0">
          <ChangeRow c={c} />
        </div>
      ))}
    </div>
  )
}
