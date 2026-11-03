import { useEffect, useState } from "react";
import { listComments } from "../api";
import type { CommentRow, LabelKey, TaskEvent, VideoDetail } from "../types";
import { EMPTY_FILTER, LABELS, eventText, labelName } from "../types";

type TabKey = LabelKey | "other";
type OtherBucket = "" | "unlabeled" | "low";

export function Overview({
  detail,
  onRetry,
  onCancel,
  cancelling,
  showProcess,
}: {
  detail: VideoDetail;
  onRetry: () => void;
  onCancel: () => void;
  cancelling: boolean;
  showProcess: boolean;
}) {
  const { video, task, report, events } = detail;
  const stats = video.cleanStats;
  const counts = report?.labelCounts;
  const [labelKey, setLabelKey] = useState<TabKey>("need");
  const [otherBucket, setOtherBucket] = useState<OtherBucket>("unlabeled");
  const [themeId, setThemeId] = useState("");
  const lowCount = report?.lowInformation ?? stats.lowInformation ?? 0;
  const unlabeled =
    report?.unlabeled ?? Math.max(0, (report?.unclassified ?? 0) - lowCount);
  const otherThemes = (report?.themes ?? []).filter(
    (theme) => theme.label === "other",
  );
  const groupedOther = otherThemes.reduce(
    (sum, theme) => sum + (theme.labelTotal || 0),
    0,
  );
  const residualUnlabeled = Math.max(0, unlabeled - groupedOther);
  const otherCount = unlabeled + lowCount;
  const tabs: { key: TabKey; name: string; count: number }[] = [
    ...LABELS.map((item) => ({
      key: item.key,
      name: item.name,
      count: counts?.[item.key] || 0,
    })),
    { key: "other", name: "其他", count: otherCount },
  ];
  const peak = Math.max(0, ...tabs.map((item) => item.count));
  const maxCount = Math.max(1, peak);
  const isOther = labelKey === "other";
  const themes = isOther
    ? []
    : (report?.themes ?? []).filter((theme) => theme.label === labelKey);
  const activeTheme = themes.find((theme) => theme.themeId === themeId);
  const activeOtherTheme = otherThemes.find(
    (theme) => theme.themeId === themeId,
  );
  const selectedCount = isOther
    ? otherBucket === "low"
      ? lowCount
      : otherBucket === "unlabeled"
        ? residualUnlabeled
        : activeOtherTheme?.labelTotal || activeOtherTheme?.sampleCount || 0
    : (counts?.[labelKey] ?? 0);
  const showDrawer = isOther ? Boolean(otherBucket || themeId) : selectedCount > 0;
  const blocked = detail.analysisBlock;

  useEffect(() => {
    setLabelKey("need");
    setOtherBucket("unlabeled");
    setThemeId("");
  }, [video.videoId]);

  return (
    <div className="stack">
      <section className="">
        <div className="row-between">
          <div>
            <h2>视频标题：{video.title || video.videoId}</h2>
            <p className="muted">
              {[
                video.author,
                task
                  ? task.status === "queued"
                    ? `前面还有 ${task.queueAhead} 个任务`
                    : eventText(task.status)
                  : "尚未分析",
                task?.progress && task.status === "started"
                  ? `${task.progress.done}/${task.progress.total}`
                  : "",
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          </div>
          {!blocked && task?.status === "failed" ? (
            <button className="primary" type="button" onClick={onRetry}>
              重试
            </button>
          ) : null}
          {!blocked &&
          (task?.status === "queued" || task?.status === "started") ? (
            <button
              className="secondary"
              type="button"
              disabled={cancelling}
              onClick={onCancel}
            >
              {cancelling ? "正在取消" : "取消分析"}
            </button>
          ) : null}
        </div>
        {blocked ? (
          <p className="error" style={{ marginTop: 12 }}>
            {blocked}
          </p>
        ) : null}
        {task?.error && task.error !== blocked ? (
          <p className="error" style={{ marginTop: 12 }}>
            {task.error}
          </p>
        ) : null}
      </section>
      {report?.positioning ? (
        <section className="card">
          <h2>视频定位</h2>
          <p>
            {report.positioning.vertical} · {report.positioning.format} ·{" "}
            {report.positioning.topic}
          </p>
          {report.positioning.boundaryNotes.map((note) => (
            <p className="muted" key={note}>
              {note}
            </p>
          ))}
        </section>
      ) : null}
      <section className="card">
        <div className="metrics">
          <Metric label="原始记录" value={stats.rawRows ?? 0} />
          <Metric label="有效评论" value={stats.valid ?? 0} />
          <Metric label="主题数" value={report?.themeCount ?? 0} />
          <Metric label="提问评论" value={counts?.question ?? 0} />
          <Metric label="消极反馈" value={counts?.negative ?? 0} />
          <Metric
            label="未归类"
            value={report?.unclassified ?? stats.lowInformation ?? 0}
          />
        </div>
        <p className="quiet">四个标签分开计数，不能相加当作评论总数。</p>
      </section>
      {counts ? (
        <div className="category-block">
          <div className="tabs">
            {tabs.map((item) => (
              <button
                key={item.key}
                className={labelKey === item.key ? "active" : ""}
                type="button"
                onClick={() => {
                  setLabelKey(item.key);
                  setThemeId("");
                  if (item.key === "other") {
                    if (otherThemes[0]) {
                      setThemeId(otherThemes[0].themeId);
                      setOtherBucket("");
                    } else {
                      setThemeId("");
                      setOtherBucket(
                        residualUnlabeled > 0 ? "unlabeled" : "low",
                      );
                    }
                  }
                }}
              >
                {item.name}
                <span
                  className={
                    peak > 0 && item.count === peak
                      ? "tab-count peak"
                      : "tab-count"
                  }
                >
                  {item.count}
                </span>
              </button>
            ))}
          </div>
          <div className={showDrawer ? "category-layout" : undefined}>
            <article className="card">
              <h2>{isOther ? "其他" : labelName(labelKey)}</h2>
              <strong className="label-count">
                {isOther ? otherCount : counts[labelKey]}
              </strong>
              <div className="bar">
                <span
                  style={{
                    width: `${((isOther ? otherCount : counts[labelKey]) / maxCount) * 100}%`,
                    opacity: isOther
                      ? 0.35
                      : 1 -
                        LABELS.findIndex((item) => item.key === labelKey) * 0.2,
                  }}
                />
              </div>
              {isOther ? (
                <>
                  {otherThemes.length === 0 ? (
                    <p className="muted">还没有补充分类</p>
                  ) : null}
                  {otherThemes.map((theme) => (
                    <div className="theme-line" key={theme.themeId}>
                      <button
                        className={
                          !otherBucket && theme.themeId === themeId
                            ? "active"
                            : ""
                        }
                        type="button"
                        onClick={() => {
                          setThemeId(theme.themeId);
                          setOtherBucket("");
                        }}
                      >
                        {theme.name}
                      </button>
                      <span className="quiet">
                        {theme.labelTotal || theme.sampleCount} 条
                      </span>
                    </div>
                  ))}
                  <div className="theme-line">
                    <button
                      className={otherBucket === "unlabeled" ? "active" : ""}
                      type="button"
                      onClick={() => {
                        setOtherBucket("unlabeled");
                        setThemeId("");
                      }}
                    >
                      模型未打上标签
                    </button>
                    <span className="quiet">{residualUnlabeled} 条</span>
                  </div>
                  <div className="theme-line">
                    <button
                      className={otherBucket === "low" ? "active" : ""}
                      type="button"
                      onClick={() => {
                        setOtherBucket("low");
                        setThemeId("");
                      }}
                    >
                      低信息量
                    </button>
                    <span className="quiet">{lowCount} 条</span>
                  </div>
                </>
              ) : (
                <>
                  <p className="quiet">
                    全量 {themes[0]?.labelTotal ?? counts[labelKey]} 条
                  </p>
                  {themes.length === 0 ? (
                    <p className="muted">没有形成主题</p>
                  ) : null}
                  {themes.map((theme) => (
                    <div className="theme-line" key={theme.themeId}>
                      <button
                        className={theme.themeId === themeId ? "active" : ""}
                        type="button"
                        onClick={() =>
                          setThemeId((current) =>
                            current === theme.themeId ? "" : theme.themeId,
                          )
                        }
                      >
                        {theme.name}
                      </button>
                      <span className="quiet">
                        高赞样本 {theme.sampleCount} 条
                      </span>
                    </div>
                  ))}
                </>
              )}
            </article>
            {showDrawer ? (
              <CommentDrawer
                key={isOther ? `${otherBucket}:${themeId}` : `${labelKey}:${themeId}`}
                videoId={video.videoId}
                label={isOther ? otherBucket : labelKey}
                themeId={isOther ? (otherBucket ? "" : themeId) : themeId}
                themeName={
                  isOther
                    ? otherBucket
                      ? ""
                      : (activeOtherTheme?.name ?? "")
                    : (activeTheme?.name ?? "")
                }
                hideTheme={isOther ? Boolean(otherBucket) : false}
              />
            ) : null}
          </div>
        </div>
      ) : null}
      {showProcess ? (
        <EventLog key={video.videoId} events={events} status={task?.status} />
      ) : null}
    </div>
  );
}

function bucketTitle(label: LabelKey | OtherBucket): string {
  if (label === "unlabeled") return "模型未打上标签的评论";
  if (label === "low") return "低信息量评论";
  if (!label) return "这一类的评论";
  return `${labelName(label)}的评论`;
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <span className="quiet">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EventLog({
  events,
  status,
}: {
  events: TaskEvent[];
  status: string | undefined;
}) {
  const [open, setOpen] = useState(status !== "completed");
  const [seenStatus, setSeenStatus] = useState(status);
  if (seenStatus !== status) {
    setSeenStatus(status);
    if (status === "queued" || status === "started") setOpen(true);
    if (status === "completed") setOpen(false);
  }

  if (!events.length) return null;
  return (
    <section className="card">
      <button
        className="fold-head"
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <h2>执行过程</h2>
        <span className="quiet">{open ? "收起" : "展开"}</span>
      </button>
      {open
        ? events.map((event) => (
            <div className="event-line" key={event.seq}>
              <span>
                <span className="badge">{eventText(event.type)}</span>{" "}
                {event.message}
              </span>
              <span className="quiet">{event.stage}</span>
            </div>
          ))
        : null}
    </section>
  );
}

function CommentDrawer({
  videoId,
  label,
  themeId,
  themeName,
  hideTheme,
}: {
  videoId: string;
  label: LabelKey | OtherBucket;
  themeId: string;
  themeName: string;
  hideTheme: boolean;
}) {
  const [rows, setRows] = useState<CommentRow[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let stopped = false;
    setRows([]);
    setTotal(0);
    setLoading(true);
    setError("");
    void listComments(videoId, { ...EMPTY_FILTER, label, themeId })
      .then((result) => {
        if (stopped) return;
        setRows(result.items);
        setTotal(result.total);
      })
      .catch((caught: Error) => {
        if (!stopped) setError(caught.message);
      })
      .finally(() => {
        if (!stopped) setLoading(false);
      });
    return () => {
      stopped = true;
    };
  }, [videoId, label, themeId]);

  return (
    <section className="card comment-side">
      <div className="row-between">
        <h2>{themeName || bucketTitle(label)}</h2>
        <span className="quiet">
          {loading ? "加载中" : `${rows.length} / ${total} 条`}
        </span>
      </div>
      {error ? (
        <p className="error" style={{ marginTop: 12 }}>
          {error}
        </p>
      ) : null}
      <div className="comment-side-list">
        {rows.map((row) => (
          <article className="comment-entry" key={row.commentId}>
            <p>{row.text}</p>
            <p className="quiet">
              {row.likeCount} 赞{row.createdAt ? ` · ${row.createdAt}` : ""}
              {!hideTheme && !themeId && row.themeName
                ? ` · ${row.themeName}`
                : ""}
              {row.cited ? " · 已引用" : ""}
            </p>
          </article>
        ))}
        {!loading && !error && rows.length === 0 ? (
          <p className="muted">没有评论</p>
        ) : null}
      </div>
    </section>
  );
}
