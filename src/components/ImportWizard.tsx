import { useState } from "react"
import { ApiError, commitImport, previewImport } from "../api"
import { IMPORT_FIELDS, type PreviewResult } from "../types"

const STAT_LABELS: { key: keyof NonNullable<PreviewResult["summary"]>; label: string }[] = [
  { key: "rawRows", label: "原始记录" },
  { key: "valid", label: "有效评论" },
  { key: "duplicate", label: "重复" },
  { key: "droppedEmpty", label: "空内容" },
  { key: "droppedNotLevel1", label: "非一级" },
  { key: "lowInformation", label: "低信息量" },
  { key: "missingCommentId", label: "缺少评论 ID" },
]

export function ImportWizard({
  onImported,
  onStartAnalysis,
  analysisBlock,
}: {
  onImported: (videoId: string) => void
  onStartAnalysis: () => void | Promise<void>
  analysisBlock?: string | null
}) {
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const [importedId, setImportedId] = useState("")
  const [starting, setStarting] = useState(false)

  async function startAnalysis() {
    setStarting(true)
    try {
      await onStartAnalysis()
    } finally {
      setStarting(false)
    }
  }

  async function runPreview(nextFile: File, nextMapping: Record<string, string>) {
    setPending(true)
    setError("")
    setImportedId("")
    try {
      const result = await previewImport(nextFile, nextMapping)
      setPreview(result)
      setMapping(result.mapping || {})
      if (result.blocked) setError(result.blocked)
    } catch (caught) {
      setPreview(null)
      setError(caught instanceof ApiError ? caught.message : "预览失败")
    } finally {
      setPending(false)
    }
  }

  async function confirm() {
    if (!file) return
    setPending(true)
    setError("")
    try {
      const video = await commitImport(file, mapping)
      setImportedId(video.videoId)
      onImported(video.videoId)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "导入失败")
    } finally {
      setPending(false)
    }
  }

  const blocked = Boolean(preview?.duplicate || preview?.blocked || preview?.error)
  return (
    <div className="stack">
      <section className="card">
        <h2>上传文件</h2>
        <p className="muted">支持 CSV 和 XLSX。确认时服务端会重新解析，不采用浏览器里的中间结果。</p>
        <label className="field">
          评论文件
          <input
            type="file"
            accept=".csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={(event) => {
              const next = event.target.files?.[0]
              if (!next) return
              setFile(next)
              void runPreview(next, {})
            }}
          />
        </label>
      </section>
      {preview ? (
        <section className="card">
          <h2>字段映射</h2>
          <div className="mapping" style={{ marginTop: 12 }}>
            {IMPORT_FIELDS.map((field) => (
              <label key={field.key}>
                {field.label}
                {field.required ? " *" : ""}
                <select
                  value={mapping[field.key] ?? ""}
                  onChange={(event) => {
                    const next = { ...mapping, [field.key]: event.target.value }
                    setMapping(next)
                    if (file) void runPreview(file, next)
                  }}
                >
                  <option value="">不使用</option>
                  {preview.columns.map((column) => (
                    <option key={column} value={column}>
                      {column}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
        </section>
      ) : null}
      {preview?.samples?.length ? (
        <section className="card">
          <h2>样例行</h2>
          {preview.samples.map((sample) => (
            <div className="theme-line" key={sample.commentId}>
              <span>
                {sample.commentId} · {sample.lowInformation ? "低信息量 · " : ""}
                {sample.text}
              </span>
              <span className="quiet">{sample.likeCount} 赞</span>
            </div>
          ))}
        </section>
      ) : null}
      {preview?.summary ? (
        <section className="card">
          <h2>清洗摘要</h2>
          <div className="metrics" style={{ marginTop: 12 }}>
            {STAT_LABELS.map((item) => (
              <div key={item.key}>
                <span className="quiet">{item.label}</span>
                <strong>{preview.summary[item.key] ?? 0}</strong>
              </div>
            ))}
          </div>
          {preview.video ? <p className="muted">视频 ID {preview.video.videoId} · {preview.video.title}</p> : null}
          {preview.duplicate ? <p className="error">该视频已导入，请先删除已有记录</p> : null}
          {preview.error && !preview.duplicate ? <p className="error">{preview.error}</p> : null}
          <div className="card-actions" style={{ marginTop: 16 }}>
            <button className="primary" type="button" disabled={pending || blocked || !preview.video} onClick={() => void confirm()}>
              {pending ? "处理中" : "确认导入"}
            </button>
          </div>
        </section>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
      {importedId ? (
        <section className="card">
          <p>已导入 {importedId}。</p>
          {analysisBlock ? (
            <p className="error" style={{ marginTop: 12 }}>{analysisBlock}</p>
          ) : (
            <div className="card-actions" style={{ marginTop: 16 }}>
              <button className="primary" type="button" disabled={pending || starting} onClick={() => void startAnalysis()}>
                {starting ? "正在开始" : "开始分析"}
              </button>
            </div>
          )}
        </section>
      ) : null}
    </div>
  )
}
