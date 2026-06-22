"use client"

import { useEffect, useState } from "react"
import { getRuntimeConfigFile, validateRuntimeConfig, writeRuntimeConfigFile } from "@/lib/api"

interface FilePayload {
  raw_text: string
  parsed: unknown
  valid: boolean
  errors: string[]
  file_path: string
}

export function RawFileTab({ onSaved }: { onSaved: () => Promise<void> }) {
  const [data, setData] = useState<FilePayload | null>(null)
  const [draft, setDraft] = useState("")
  const [validating, setValidating] = useState(false)
  const [validateResult, setValidateResult] = useState<{ ok: boolean; errors: string[] } | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveResult, setSaveResult] = useState<{ ok: boolean; errors: string[] } | null>(null)

  const refresh = async () => {
    try {
      const r = await getRuntimeConfigFile<FilePayload>(false)
      setData(r)
      setDraft(r.raw_text)
      setValidateResult(null)
      setSaveResult(null)
    } catch (err) {
      setSaveResult({ ok: false, errors: [err instanceof Error ? err.message : String(err)] })
    }
  }

  useEffect(() => { refresh() }, [])

  const handleValidate = async () => {
    setValidating(true)
    try {
      const r = await validateRuntimeConfig(draft)
      setValidateResult(r)
    } finally {
      setValidating(false)
    }
  }

  const handleSave = async () => {
    if (!confirm("确定写入 runtime.jsonc？校验失败时文件和 DB 不动。")) return
    setSaving(true)
    setSaveResult(null)
    try {
      const r = await writeRuntimeConfigFile(draft)
      setSaveResult(r)
      if (r.ok) await onSaved()
    } catch (err) {
      setSaveResult({ ok: false, errors: [err instanceof Error ? err.message : String(err)] })
    } finally {
      setSaving(false)
    }
  }

  if (!data) return <div className="text-center text-gray-500 text-sm py-8">加载中…</div>

  const dirty = draft !== data.raw_text

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-[11px] text-gray-500 font-mono truncate">
          {data.file_path}
        </div>
        <div className="flex items-center gap-2">
          <button onClick={refresh}
            className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">
            重新读取
          </button>
          <button onClick={handleValidate} disabled={validating}
            className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-50">
            {validating ? "校验中…" : "校验"}
          </button>
          <button onClick={handleSave} disabled={saving || !dirty}
            className="text-xs px-3 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50">
            {saving ? "写入中…" : dirty ? "应用" : "无变更"}
          </button>
        </div>
      </div>

      <textarea
        value={draft}
        onChange={(e) => { setDraft(e.target.value); setValidateResult(null); setSaveResult(null) }}
        spellCheck={false}
        className="w-full h-[55vh] text-xs font-mono bg-gray-950 border border-gray-800 rounded p-3 text-gray-100 resize-none"
      />

      {validateResult && (
        <div className={`rounded border text-xs p-3 ${
          validateResult.ok
            ? "border-emerald-800 bg-emerald-950/40 text-emerald-200"
            : "border-red-800 bg-red-950/40 text-red-200"
        }`}>
          {validateResult.ok ? (
            <div>OK 校验通过</div>
          ) : (
            <>
              <div className="font-semibold mb-1">校验失败：</div>
              <ul className="list-disc pl-4 space-y-0.5">
                {validateResult.errors.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </>
          )}
        </div>
      )}

      {saveResult && !saveResult.ok && (
        <div className="rounded border border-red-800 bg-red-950/40 text-red-200 text-xs p-3">
          <div className="font-semibold mb-1">写入失败（文件和 DB 未变更）：</div>
          <ul className="list-disc pl-4 space-y-0.5">
            {saveResult.errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}
      {saveResult?.ok && (
        <div className="rounded border border-emerald-800 bg-emerald-950/40 text-emerald-200 text-xs p-3">
          OK 已写入并同步
        </div>
      )}
    </div>
  )
}
