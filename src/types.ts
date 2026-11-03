export type LabelKey = "need" | "question" | "negative" | "idea"

export const LABELS: { key: LabelKey; name: string }[] = [
  { key: "need", name: "明确需求" },
  { key: "question", name: "常见问题" },
  { key: "negative", name: "消极反馈" },
  { key: "idea", name: "内容灵感" },
]

export const IMPORT_FIELDS: { key: string; label: string; required?: boolean }[] = [
  { key: "videoUrl", label: "视频链接", required: true },
  { key: "commentId", label: "评论ID", required: true },
  { key: "content", label: "评论内容", required: true },
  { key: "videoTitle", label: "视频标题" },
  { key: "videoAuthor", label: "视频作者" },
  { key: "videoPublishTime", label: "视频发布时间" },
  { key: "videoDescription", label: "视频描述" },
  { key: "level", label: "层级" },
  { key: "parentId", label: "父评论ID" },
  { key: "likes", label: "点赞数" },
  { key: "replies", label: "回复数" },
  { key: "commentTime", label: "评论时间" },
  { key: "isPinned", label: "是否置顶" },
  { key: "isAuthor", label: "是否视频作者" },
  { key: "sourceVideoId", label: "视频ID" },
  { key: "rootCommentId", label: "根评论ID" },
  { key: "nickname", label: "用户昵称" },
  { key: "replyToNickname", label: "回复对象昵称" },
  { key: "collectedAt", label: "采集时间" },
  { key: "douyinId", label: "抖音号" },
]

export interface CleanStats {
  rawRows?: number
  valid?: number
  duplicate?: number
  droppedEmpty?: number
  droppedNotLevel1?: number
  lowInformation?: number
  missingCommentId?: number
}

export interface VideoSummary {
  videoId: string
  title: string
  author: string
  videoUrl: string
  publishTime: string
  description: string
  hashtags: string[]
  sourceFileName: string
  cleanStats: CleanStats
  importedAt: string
  updatedAt: string
  status?: string
}

export interface TaskInfo {
  taskId: string
  videoId: string
  status: "queued" | "started" | "completed" | "failed"
  error: string | null
  progress: { stage: string; done: number; total: number } | null
  promptVersion: string
  model: string
  thinkingEnabled: boolean
  queueAhead: number
  updatedAt: string
}

export interface TaskEvent {
  seq: number
  taskId: string
  videoId: string
  type: string
  stage: string
  message: string
  createdAt: string
}

export interface Excerpt {
  commentId: string
  text: string
}

export interface Theme {
  themeId: string
  label: LabelKey | "other"
  name: string
  summary: string
  labelSummary?: string
  sampleCount: number
  labelTotal: number
  likeSum: number
  score?: number
  feasibility?: number
  cost?: number
  reason?: string
  evidence?: number
}

export interface Claim {
  kind: "fact" | "inference" | "hypothesis"
  text: string
  commentIds: string[]
  excerpts: Excerpt[]
  downgraded: boolean
}

export interface Opportunity {
  themeId: string
  topic: string
  structure: string[]
  targetNeed: string
  validation: string
  favorite: boolean
  score: number
  rank: number
  feasibility: number
  cost: number
  reason: string
  sampleCount: number
  labelTotal: number
  likeSum: number
  evidence: number
  commentIds: string[]
  excerpts: Excerpt[]
  claims: Claim[]
}

export interface Report {
  videoId: string
  taskId: string
  promptVersion: string
  model: string
  positioning: { vertical: string; format: string; topic: string; boundaryNotes: string[] } | null
  labelCounts: Record<LabelKey, number>
  unlabeled?: number
  lowInformation?: number
  unclassified: number
  analysisLimit?: string | null
  themeCount: number
  themes: Theme[]
  opportunities: Opportunity[]
}

export interface VideoDetail {
  video: VideoSummary
  task: TaskInfo | null
  report: Report | null
  events: TaskEvent[]
  analysisBlock?: string | null
}

export interface CommentRow {
  commentId: string
  text: string
  likeCount: number
  createdAt: string
  labels: LabelKey[]
  themeId: string | null
  themeName: string | null
  lowInformation: boolean
  cited: boolean
}

export interface PreviewResult {
  columns: string[]
  mapping: Record<string, string>
  samples: { commentId: string; likeCount: number; text: string; lowInformation: boolean }[]
  summary: CleanStats
  video: {
    videoId: string
    title: string
    author: string
    videoUrl: string
    description: string
    hashtags: string[]
  } | null
  duplicate: boolean
  error: string | null
  blocked: string | null
}

export interface CommentFilter {
  q: string
  label: string
  themeId: string
  cited: "" | "yes" | "no"
  order: "desc" | "asc"
}

export const EMPTY_FILTER: CommentFilter = { q: "", label: "", themeId: "", cited: "", order: "desc" }

export function labelName(key: string): string {
  return LABELS.find((item) => item.key === key)?.name ?? key
}

export function statusText(status: string | undefined): string {
  if (status === "queued") return "排队中"
  if (status === "started") return "分析中"
  if (status === "completed") return "已完成"
  if (status === "failed") return "失败"
  return "未分析"
}

export function eventText(type: string): string {
  const names: Record<string, string> = {
    queued: "排队",
    started: "开始",
    stage_started: "阶段开始",
    stage_completed: "阶段结束",
    summary: "摘要",
    completed: "完成",
    failed: "失败",
  }
  return names[type] ?? type
}

export function claimText(claim: Claim): string {
  if (claim.downgraded) return "已降级"
  if (claim.kind === "fact") return "事实"
  if (claim.kind === "hypothesis") return "假设"
  return "推断"
}
