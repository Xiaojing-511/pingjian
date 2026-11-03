import { useEffect, useState } from "react"
import { listComments } from "../api"
import type { CommentFilter, CommentRow, Report } from "../types"
import { LABELS, labelName } from "../types"

export function CommentsPanel({
  videoId,
  report,
  filter,
  onFilter,
}: {
  videoId: string
  report: Report | null
  filter: CommentFilter
  onFilter: (filter: CommentFilter) => void
}) {
  const [rows, setRows] = useState<CommentRow[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState("")

  useEffect(() => {
    let stopped = false
    void listComments(videoId, filter)
      .then((result) => {
        if (stopped) return
        setRows(result.items)
        setTotal(result.total)
      })
      .catch((caught: Error) => {
        if (!stopped) setError(caught.message)
      })
    return () => {
      stopped = true
    }
  }, [videoId, filter])

  const themes = report?.themes ?? []
  return (
    <section className="card">
      <div className="filters">
        <input
          value={filter.q}
          placeholder="关键词"
          onChange={(event) => onFilter({ ...filter, q: event.target.value })}
        />
        <select
          value={filter.label}
          onChange={(event) => {
            const label = event.target.value
            onFilter({
              ...filter,
              label,
              themeId: label === "low" || label === "unlabeled" ? "" : filter.themeId,
            })
          }}
        >
          <option value="">全部标签</option>
          {LABELS.map((item) => (
            <option key={item.key} value={item.key}>
              {item.name}
            </option>
          ))}
          <option value="unlabeled">模型未打上标签</option>
          <option value="low">低信息量</option>
        </select>
        <select value={filter.themeId} onChange={(event) => onFilter({ ...filter, themeId: event.target.value })}>
          <option value="">全部主题</option>
          {themes.map((theme) => (
            <option key={theme.themeId} value={theme.themeId}>
              {theme.name}
            </option>
          ))}
        </select>
        <select
          value={filter.cited}
          onChange={(event) => onFilter({ ...filter, cited: event.target.value as CommentFilter["cited"] })}
        >
          <option value="">是否被引用</option>
          <option value="yes">被引用</option>
          <option value="no">未被引用</option>
        </select>
        <select
          value={filter.order}
          onChange={(event) => onFilter({ ...filter, order: event.target.value as CommentFilter["order"] })}
        >
          <option value="desc">点赞从高到低</option>
          <option value="asc">点赞从低到高</option>
        </select>
      </div>
      <p className="quiet">显示 {rows.length} / {total} 条</p>
      {error ? <p className="error">{error}</p> : null}
      <div className="table-wrap">
        <table className="comments-table">
          <thead>
            <tr>
              <th className="col-text">原文</th>
              <th className="col-likes">点赞</th>
              <th className="col-time">时间</th>
              <th className="col-labels">标签</th>
              <th>主题</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.commentId} id={`comment-${row.commentId}`}>
                <td className="col-text">
                  <div className="text-scroll" title={row.text}>
                    {row.text}
                  </div>
                </td>
                <td className="col-likes">{row.likeCount}</td>
                <td className="col-time">{row.createdAt || "—"}</td>
                <td className="col-labels">
                  {row.lowInformation ? "低信息量" : row.labels.map(labelName).join("、") || "未归类"}
                </td>
                <td>
                  {row.themeName || "—"}
                  {row.cited ? " · 已引用" : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
