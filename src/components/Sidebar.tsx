import type { VideoSummary } from "../types"
import { statusText } from "../types"

export function Sidebar({
  username,
  videos,
  selectedId,
  view,
  query,
  onQuery,
  onOpen,
  onImport,
  onWorkbench,
  onDelete,
  onLogout,
}: {
  username: string
  videos: VideoSummary[]
  selectedId: string | null
  view: string
  query: string
  onQuery: (value: string) => void
  onOpen: (videoId: string) => void
  onImport: () => void
  onWorkbench: () => void
  onDelete: (video: VideoSummary) => void
  onLogout: () => void
}) {
  const visible = videos.filter((video) => (video.title || video.videoId).includes(query.trim()))
  return (
    <aside className="sidebar">
      <div className="brand">评见</div>
      <button className={view !== "import" ? "nav-button active" : "nav-button"} type="button" onClick={onWorkbench}>
        工作台
      </button>
      <button className={view === "import" ? "nav-button active" : "nav-button"} type="button" onClick={onImport}>
        导入
      </button>
      <div className="report-scroll">
        <input className="side-search" value={query} placeholder="搜索报告" onChange={(event) => onQuery(event.target.value)} />
        {visible.length === 0 ? (
          <p className="quiet" style={{ padding: "0 16px" }}>{videos.length === 0 ? "还没有报告" : "没有匹配的报告"}</p>
        ) : null}
        {visible.map((video) => (
          <div key={video.videoId} className={selectedId === video.videoId ? "report-row active" : "report-row"}>
            <button className="report-open" type="button" onClick={() => onOpen(video.videoId)}>
              <div className="report-title">{video.title || video.videoId}</div>
              <div className="quiet">{statusText(video.status)}</div>
            </button>
            <button className="text-button quiet" type="button" onClick={() => onDelete(video)}>
              删除
            </button>
          </div>
        ))}
      </div>
      <div className="side-foot">
        <span className="quiet">{username}</span>
        <button className="text-button" type="button" onClick={onLogout}>
          退出
        </button>
      </div>
    </aside>
  )
}
