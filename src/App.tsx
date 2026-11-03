import { useEffect, useRef, useState } from "react"
import {
  ApiError,
  clearSession,
  downloadReport,
  getToken,
  getUsername,
  getVideo,
  listVideos,
  removeVideo,
  cancelAnalysis,
  retryAnalysis,
  startAnalysis,
  subscribeEvents,
} from "./api"
import { CommentsPanel } from "./components/CommentsPanel"
import { ImportWizard } from "./components/ImportWizard"
import { LoginCard } from "./components/LoginCard"
import { Opportunities } from "./components/Opportunities"
import { Overview } from "./components/Overview"
import { Sidebar } from "./components/Sidebar"
import { EMPTY_FILTER, type CommentFilter, type Report, type VideoDetail, type VideoSummary } from "./types"

type View = "overview" | "opportunities" | "comments" | "import"

const PAGE_COPY: Record<View, [string, string]> = {
  overview: ["总览", "看这条视频的评论规模、分类和主题。"],
  opportunities: ["内容机会", "按分数列出前 8 条选题。题目和拍法可以改，评论数量和摘录不能改。"],
  comments: ["评论明细", "按关键词、标签、主题和是否被引用筛选。"],
  import: ["导入", "上传后先看清洗摘要，确认时由服务端重新解析。"],
}

const THINKING_KEY = "pingjian.thinking-enabled"
const PROCESS_KEY = "pingjian.show-process"

function readPreference(key: string, fallback: boolean) {
  const value = localStorage.getItem(key)
  return value === null ? fallback : value === "true"
}

export function App() {
  const [username, setUsername] = useState(getToken() ? getUsername() : "")
  const [notice, setNotice] = useState("")
  const [videos, setVideos] = useState<VideoSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [view, setView] = useState<View>("overview")
  const [detail, setDetail] = useState<VideoDetail | null>(null)
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<CommentFilter>(EMPTY_FILTER)
  const [error, setError] = useState("")
  const [thinkingEnabled, setThinkingEnabled] = useState(() => readPreference(THINKING_KEY, false))
  const [showProcess, setShowProcess] = useState(() => readPreference(PROCESS_KEY, true))
  const [justImportedId, setJustImportedId] = useState("")
  const [cancelling, setCancelling] = useState(false)
  const seqRef = useRef(0)

  useEffect(() => {
    void fetch("/api/health")
      .then((response) => response.json())
      .then((body: { missing?: string[] }) => {
        const missing = body.missing ?? []
        setNotice(missing.length ? `服务已启动，还缺 ${missing.join("、")}` : "")
      })
      .catch(() => setNotice("分析服务没有启动"))
  }, [])

  useEffect(() => {
    if (!username) return
    void refreshVideos()
  }, [username])

  useEffect(() => {
    const status = detail?.task?.status
    const videoId = selectedId
    if (!videoId || (status !== "queued" && status !== "started")) return
    const controller = new AbortController()
    let stopped = false
    async function follow() {
      while (!stopped) {
        try {
          await subscribeEvents(
            videoId!,
            seqRef.current,
            (event) => {
              seqRef.current = Math.max(seqRef.current, event.seq)
              setDetail((current) => {
                if (!current || current.video.videoId !== videoId) return current
                const events = [...current.events.filter((item) => item.seq !== event.seq), event].sort((a, b) => a.seq - b.seq)
                return { ...current, events }
              })
              if (event.type === "completed" || event.type === "failed") void reload(videoId!)
            },
            controller.signal,
          )
          if (stopped) return
          const next = await getVideo(videoId!)
          seqRef.current = next.events.at(-1)?.seq ?? seqRef.current
          setDetail(next)
          const list = await listVideos()
          setVideos(list.items)
          const nextStatus = next.task?.status
          if (nextStatus !== "queued" && nextStatus !== "started") return
          await new Promise((resolve) => setTimeout(resolve, 800))
        } catch {
          if (stopped || controller.signal.aborted) return
          await new Promise((resolve) => setTimeout(resolve, 1000))
        }
      }
    }
    void follow()
    return () => {
      stopped = true
      controller.abort()
    }
  }, [selectedId, detail?.task?.status, detail?.task?.taskId])

  async function refreshVideos() {
    try {
      const result = await listVideos()
      setVideos(result.items)
      if (!selectedId && result.items[0]) await openVideo(result.items[0].videoId)
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        clearSession()
        setUsername("")
        return
      }
      setError(caught instanceof Error ? caught.message : "报告列表没有加载")
    }
  }

  async function rememberImport(videoId: string) {
    setJustImportedId(videoId)
    const result = await listVideos()
    setVideos(result.items)
    setSelectedId(videoId)
    const next = await getVideo(videoId)
    seqRef.current = next.events.at(-1)?.seq ?? 0
    setDetail(next)
  }

  async function reload(videoId: string) {
    const next = await getVideo(videoId)
    seqRef.current = next.events.at(-1)?.seq ?? 0
    setDetail(next)
    const result = await listVideos()
    setVideos(result.items)
  }

  async function openVideo(videoId: string) {
    setSelectedId(videoId)
    setError("")
    setView("overview")
    const next = await getVideo(videoId)
    seqRef.current = next.events.at(-1)?.seq ?? 0
    setDetail(next)
  }

  async function analyze(retry: boolean) {
    if (!selectedId) return
    setError("")
    try {
      if (retry) await retryAnalysis(selectedId, thinkingEnabled)
      else await startAnalysis(selectedId, thinkingEnabled)
      await reload(selectedId)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "分析没有开始")
    }
  }

  async function startImportedAnalysis() {
    const videoId = justImportedId
    if (!videoId) return
    setError("")
    try {
      await startAnalysis(videoId, thinkingEnabled)
      await openVideo(videoId)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "分析没有开始")
    }
  }

  async function cancel() {
    if (!selectedId || cancelling) return
    setCancelling(true)
    setError("")
    try {
      await cancelAnalysis(selectedId)
      await reload(selectedId)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "没有取消")
    } finally {
      setCancelling(false)
    }
  }

  function updateThinkingEnabled(value: boolean) {
    setThinkingEnabled(value)
    localStorage.setItem(THINKING_KEY, String(value))
  }

  function updateShowProcess(value: boolean) {
    setShowProcess(value)
    localStorage.setItem(PROCESS_KEY, String(value))
  }

  async function deleteVideo(video: VideoSummary) {
    const name = video.title || video.videoId
    if (!window.confirm(`删除「${name}」？评论、任务、报告和原始文件会一起删除。`)) return
    await removeVideo(video.videoId)
    const result = await listVideos()
    setVideos(result.items)
    if (selectedId === video.videoId) {
      setDetail(null)
      setSelectedId(result.items[0]?.videoId ?? null)
      if (result.items[0]) await openVideo(result.items[0].videoId)
    }
  }

  function openComments(next: Partial<CommentFilter>) {
    setFilter({ ...EMPTY_FILTER, ...next })
    setView("comments")
  }

  if (!username) return <LoginCard notice={notice} onSuccess={(name) => setUsername(name)} />

  const [title, description] = PAGE_COPY[view]
  const report = detail?.report ?? null
  return (
    <div className="shell">
      <Sidebar
        username={username}
        videos={videos}
        selectedId={selectedId}
        view={view}
        query={query}
        onQuery={setQuery}
        onOpen={(videoId) => void openVideo(videoId)}
        onImport={() => setView("import")}
        onWorkbench={() => setView(detail ? "overview" : "overview")}
        onDelete={(video) => void deleteVideo(video)}
        onLogout={() => {
          clearSession()
          setUsername("")
          setDetail(null)
          setVideos([])
        }}
      />
      <main className="main">
        <header className="topbar">
          <div>
            <h1>{title}</h1>
            <p className="muted">{description}</p>
          </div>
          <div className="top-actions">
            <details className="analysis-settings">
              <summary>分析设置</summary>
              <div className="settings-popover">
                <label className="setting-option">
                  <input type="checkbox" checked={thinkingEnabled} onChange={(event) => updateThinkingEnabled(event.target.checked)} />
                  <span><strong>开启思考模式</strong><small>可能增加分析耗时和 API 用量</small></span>
                </label>
                <label className="setting-option">
                  <input type="checkbox" checked={showProcess} onChange={(event) => updateShowProcess(event.target.checked)} />
                  <span><strong>展示分析过程</strong><small>显示步骤和摘要，不展示模型思考原文</small></span>
                </label>
              </div>
            </details>
            <input className="top-search" value={query} placeholder="搜索报告" onChange={(event) => setQuery(event.target.value)} />
            <button className="primary" type="button" onClick={() => setView("import")}>
              导入
            </button>
          </div>
        </header>
        {notice ? <p className="error banner">{notice}</p> : null}
        {error ? <p className="error banner">{error}</p> : null}
        {view !== "import" ? (
          <div className="view-bar">
            <div className="tabs">
              {(["overview", "opportunities", "comments"] as View[]).map((item) => (
                <button key={item} className={view === item ? "active" : ""} type="button" onClick={() => setView(item)}>
                  {PAGE_COPY[item][0]}
                </button>
              ))}
            </div>
            {report ? (
              <div className="export-actions">
                <button type="button" onClick={() => selectedId && void downloadReport(selectedId, "md")}>
                  导出 Markdown
                </button>
                <button type="button" onClick={() => selectedId && void downloadReport(selectedId, "csv")}>
                  导出 CSV
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {view === "import" ? (
          <ImportWizard
            onImported={(videoId) => void rememberImport(videoId)}
            onStartAnalysis={startImportedAnalysis}
            analysisBlock={detail && detail.video.videoId === justImportedId ? detail.analysisBlock : null}
          />
        ) : null}
        {view !== "import" && !detail ? <section className="card">还没有报告，先导入一份评论。</section> : null}
        {view === "overview" && detail ? (
          <Overview
            detail={detail}
            onRetry={() => void analyze(true)}
            onCancel={() => void cancel()}
            cancelling={cancelling}
            showProcess={showProcess}
          />
        ) : null}
        {view === "opportunities" && detail && report ? (
          <Opportunities
            videoId={detail.video.videoId}
            report={report}
            onSaved={(next: Report) => setDetail({ ...detail, report: next })}
            onOpenComments={(themeId) => openComments({ themeId })}
          />
        ) : null}
        {view === "opportunities" && detail && !report ? <section className="card">分析完成后才会出现选题。</section> : null}
        {view === "comments" && selectedId ? (
          <CommentsPanel videoId={selectedId} report={report} filter={filter} onFilter={setFilter} />
        ) : null}
      </main>
    </div>
  )
}
