"use client"

import { memo } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { cn } from "@/lib/utils"

interface Props {
  children: string
  className?: string
  compact?: boolean
}

// ---- lightweight syntax highlighting (no external dependency) ----

const HIGHLIGHTERS: Record<string, (src: string) => string> = {
  py: (s) => highlightGeneric(s, PY_RULES),
  python: (s) => highlightGeneric(s, PY_RULES),
  js: (s) => highlightGeneric(s, JS_RULES),
  javascript: (s) => highlightGeneric(s, JS_RULES),
  ts: (s) => highlightGeneric(s, TS_RULES),
  typescript: (s) => highlightGeneric(s, TS_RULES),
  tsx: (s) => highlightGeneric(s, TSX_RULES),
  jsx: (s) => highlightGeneric(s, TSX_RULES),
  json: (s) => highlightGeneric(s, JSON_RULES),
  bash: (s) => highlightGeneric(s, BASH_RULES),
  sh: (s) => highlightGeneric(s, BASH_RULES),
  shell: (s) => highlightGeneric(s, BASH_RULES),
  yaml: (s) => highlightGeneric(s, YAML_RULES),
  yml: (s) => highlightGeneric(s, YAML_RULES),
  markdown: (s) => highlightGeneric(s, MD_RULES),
  md: (s) => highlightGeneric(s, MD_RULES),
  sql: (s) => highlightGeneric(s, SQL_RULES),
}

interface Rule { pattern: RegExp; cls: string }
type RuleSet = { keywords?: Set<string>; rules: Rule[] }

const makeRule = (pattern: RegExp, cls: string): Rule => ({ pattern, cls })

const KWD = (cls: string) => cls + " "
const NUM = "text-amber-300 "
const STR = "text-emerald-300 "
const CMT = "text-gray-500 italic "
const FUN = "text-sky-300 "
const PUN = "text-gray-400 "

const PY_RULES: RuleSet = {
  keywords: new Set(["def","class","return","if","elif","else","for","while","import","from","as","try","except","finally","with","yield","raise","pass","break","continue","and","or","not","in","is","None","True","False","async","await","lambda","nonlocal","global","assert","del"]),
  rules: [
    makeRule(/("""[\s\S]*?"""|'''[\s\S]*?''')/g, STR),
    makeRule(/("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g, STR),
    makeRule(/#[^\n]*/g, CMT),
    makeRule(/\b\d+\.?\d*\b/g, NUM),
    makeRule(/[a-zA-Z_]\w*(?=\s*\()/g, FUN),
    makeRule(/@[a-zA-Z_]\w*/g, "text-purple-300 "),
  ],
}

const JS_RULES: RuleSet = {
  keywords: new Set(["function","const","let","var","return","if","else","for","while","do","switch","case","break","continue","try","catch","finally","throw","new","typeof","instanceof","this","async","await","class","extends","import","export","default","from","of","in","true","false","null","undefined","yield","static","get","set","delete","void"]),
  rules: [
    makeRule(/(`(?:[^`\\]|\\.)*`)/g, STR),
    makeRule(/("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g, STR),
    makeRule(/(\/\/[^\n]*)/g, CMT),
    makeRule(/(\/\*[\s\S]*?\*\/)/g, CMT),
    makeRule(/\b\d+\.?\d*\b/g, NUM),
    makeRule(/[a-zA-Z_$]\w*(?=\s*\()/g, FUN),
  ],
}

const TS_RULES: RuleSet = { ...JS_RULES }

const TSX_RULES: RuleSet = {
  keywords: new Set([...(JS_RULES.keywords || []), "interface","type","enum","implements","readonly","keyof","typeof","as","is","infer","namespace","declare","abstract","private","public","protected"]),
  rules: [...JS_RULES.rules],
}

const JSON_RULES: RuleSet = {
  rules: [
    makeRule(/("(?:[^"\\]|\\.)*")\s*:/g, "text-sky-300 "),
    makeRule(/("(?:[^"\\]|\\.)*")/g, STR),
    makeRule(/\b\d+\.?\d*\b/g, NUM),
    makeRule(/\b(true|false|null)\b/g, "text-purple-300 "),
  ],
}

const BASH_RULES: RuleSet = {
  rules: [
    makeRule(/(#.*)/g, CMT),
    makeRule(/("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g, STR),
    makeRule(/(\b(?:cd|ls|cat|echo|git|npm|pnpm|yarn|docker|curl|wget|mkdir|rm|mv|cp|export|source|python|node|uv|pip|npx|grep|find|sed|awk|chmod|ssh|scp|tar|gzip)\b)/g, "text-emerald-300 "),
    makeRule(/(--?[a-zA-Z0-9_-]+)/g, "text-amber-300 "),
    makeRule(/\b(\d+)\b/g, NUM),
  ],
}

const YAML_RULES: RuleSet = {
  rules: [
    makeRule(/(#.*)/g, CMT),
    makeRule(/("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g, STR),
    makeRule(/\b(true|false|null|yes|no)\b/g, "text-purple-300 "),
    makeRule(/(^[a-zA-Z_][\w-]*(?=\s*:))/gm, "text-sky-300 "),
  ],
}

const MD_RULES: RuleSet = {
  rules: [
    makeRule(/(#{1,6}\s.*$)/gm, "text-sky-300 font-semibold"),
    makeRule(/(\*\*[^*]+\*\*|__[^_]+__)/g, "text-white font-semibold"),
    makeRule(/(\*[^*]+\*|_[^_]+_)/g, "text-gray-300 italic"),
    makeRule(/(`[^`]+`)/g, "text-amber-200 "),
    makeRule(/(\[[^\]]+\]\([^)]+\))/g, "text-indigo-300 "),
  ],
}

const SQL_RULES: RuleSet = {
  keywords: new Set(["SELECT","FROM","WHERE","INSERT","INTO","UPDATE","DELETE","CREATE","TABLE","ALTER","DROP","INDEX","JOIN","LEFT","RIGHT","INNER","OUTER","ON","AS","AND","OR","NOT","NULL","IN","LIKE","BETWEEN","ORDER","BY","GROUP","HAVING","LIMIT","OFFSET","UNION","ALL","DISTINCT","CASE","WHEN","THEN","ELSE","END","EXISTS","SET","VALUES","PRIMARY","KEY","FOREIGN","REFERENCES","CASCADE","BEGIN","COMMIT","ROLLBACK","TRANSACTION","VACUUM","EXPLAIN","ANALYZE","select","from","where","insert","into","update","delete","create","table","alter","drop","index","join","left","right","inner","outer","on","as","and","or","not","null","in","like","between","order","by","group","having","limit","offset","union","all","distinct","case","when","then","else","end","exists","set","values","primary","key","foreign","references","cascade","begin","commit","rollback","transaction","vacuum","explain","analyze"]),
  rules: [
    makeRule(/("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g, STR),
    makeRule(/(--[^\n]*)/g, CMT),
    makeRule(/\b\d+\.?\d*\b/g, NUM),
  ],
}

function highlightGeneric(src: string, rs: RuleSet): string {
  // Escape HTML entities first
  let out = src.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")

  // Tokenize: apply rules in order, protecting already-tagged spans
  const tagged: boolean[] = new Array(out.length).fill(false)

  const applyRule = (regex: RegExp, cls: string) => {
    regex.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = regex.exec(out)) !== null) {
      const start = m.index
      const end = start + m[0].length
      if (tagged.slice(start, end).some(Boolean)) continue
      const span = `<span class="${cls}">${m[0]}</span>`
      const before = out.slice(0, start)
      const after = out.slice(end)
      const fill = new Array(span.length).fill(true)
      out = before + span + after
      tagged.splice(start, end - start, ...fill.slice(0, span.length))
      regex.lastIndex = start + span.length
    }
  }

  // Keywords first (word boundaries)
  if (rs.keywords && rs.keywords.size > 0) {
    const kwPattern = new RegExp(
      `\\b(${[...rs.keywords].map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join("|")})\\b`,
      "g",
    )
    applyRule(kwPattern, KWD + "text-amber-100")
  }

  for (const rule of rs.rules) {
    applyRule(rule.pattern, rule.cls)
  }

  return out
}

function highlightCode(code: string, lang?: string): string {
  const h = lang ? HIGHLIGHTERS[lang.toLowerCase()] : undefined
  if (h) return h(code)
  // Unknown language — just escape
  return code.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

/** Markdown renderer tuned for chat / detail modal use.
 *  - GFM tables / strikethrough / task lists supported
 *  - Code blocks: lightweight syntax highlighting (no heavy dependency)
 *  - Links open in new tab
 *  - Lists, blockquotes, headings sized for inline reading
 */
function MarkdownViewImpl({ children, className, compact }: Props) {
  return (
    <div
      className={cn(
        "markdown-view text-sm leading-relaxed text-gray-100",
        compact ? "[&>*+*]:mt-1.5" : "[&>*+*]:mt-2",
        className,
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => <h1 className="text-base font-semibold text-gray-50">{children}</h1>,
          h2: ({ children }) => <h2 className="text-[15px] font-semibold text-gray-50">{children}</h2>,
          h3: ({ children }) => <h3 className="text-sm font-semibold text-gray-50">{children}</h3>,
          p: ({ children }) => <div className="text-gray-100">{children}</div>,
          strong: ({ children }) => <strong className="text-white font-semibold">{children}</strong>,
          em: ({ children }) => <em className="text-gray-200 italic">{children}</em>,
          a: ({ href, children }) => (
            <a
              href={href ?? "#"}
              target="_blank"
              rel="noopener noreferrer"
              className="text-indigo-300 hover:text-indigo-200 underline underline-offset-2"
            >
              {children}
            </a>
          ),
          ul: ({ children }) => <ul className="list-disc list-outside pl-5 space-y-0.5 text-gray-100">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal list-outside pl-5 space-y-0.5 text-gray-100">{children}</ol>,
          li: ({ children }) => <li className="text-gray-100">{children}</li>,
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-indigo-500/60 pl-3 text-gray-300 italic">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="border-gray-700/60 my-2" />,
          code: ({ inline, className, children, ...props }: React.ComponentPropsWithoutRef<"code"> & { inline?: boolean }) => {
            const codeStr = String(children).replace(/\n$/, "")
            const lang = className?.replace("language-", "") ?? undefined
            const highlighted = highlightCode(codeStr, lang)

            if (inline) {
              return (
                <code
                  className="px-1 py-0.5 rounded bg-gray-700/60 text-amber-200 font-mono text-[12.5px]"
                  {...props}
                >
                  {children}
                </code>
              )
            }

            return (
              <pre className="bg-black/60 border border-gray-800 rounded-md overflow-hidden text-[12.5px] group/pre">
                {lang ? (
                  <div className="flex items-center justify-between px-3 py-1 bg-gray-900/60 border-b border-gray-800/60">
                    <span className="text-[10px] text-gray-500 uppercase tracking-wider">{lang}</span>
                  </div>
                ) : null}
                <div className="p-3 overflow-x-auto">
                  <code
                    className={cn("font-mono text-gray-100", className)}
                    dangerouslySetInnerHTML={{ __html: highlighted }}
                    {...props}
                  />
                </div>
              </pre>
            )
          },
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-[12.5px]">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-gray-700 px-2 py-1 bg-gray-800/60 text-left text-gray-200 font-medium">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-gray-700 px-2 py-1 text-gray-100">{children}</td>
          ),
          img: ({ src, alt }) => (
            <img
              src={src ?? ""}
              alt={alt ?? ""}
              className="rounded max-w-full my-1"
              onError={(e) => {
                // 历史消息里失效的图(项目被 reset / 文件被清),让它静默消失,
                // 别让浏览器 re-render 时不停重试,把后端日志刷屏。
                const el = e.currentTarget
                el.style.display = "none"
              }}
            />
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}

export const MarkdownView = memo(MarkdownViewImpl)
