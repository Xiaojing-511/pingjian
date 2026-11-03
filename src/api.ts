import type { CommentFilter, CommentRow, PreviewResult, TaskEvent, VideoDetail, VideoSummary } from "./types"

const TOKEN_KEY = "pingjian.token"
const USER_KEY = "pingjian.user"
const REFRESH_KEY = "pingjian.refresh"
const EXPIRES_KEY = "pingjian.expires"
const REFRESH_SKEW_MS = 2 * 60 * 1000
const DEFAULT_ACCESS_SECONDS = 2 * 60 * 60

type SessionPayload = {
  accessToken: string
  username: string
  refreshToken?: string
  expiresIn?: number
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function readStored(key: string): string {
  const current = localStorage.getItem(key)
  if (current) return current
  const legacy = sessionStorage.getItem(key)
  if (!legacy) return ""
  localStorage.setItem(key, legacy)
  sessionStorage.removeItem(key)
  return legacy
}

export function getToken(): string {
  return readStored(TOKEN_KEY)
}

export function getUsername(): string {
  return readStored(USER_KEY)
}

export function setSession(token: string, username: string, refreshToken = "", expiresIn = 0) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, username)
  if (refreshToken) localStorage.setItem(REFRESH_KEY, refreshToken)
  const seconds = expiresIn > 0 ? expiresIn : DEFAULT_ACCESS_SECONDS
  localStorage.setItem(EXPIRES_KEY, String(Date.now() + seconds * 1000))
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(USER_KEY)
}

export function clearSession() {
  for (const store of [localStorage, sessionStorage]) {
    store.removeItem(TOKEN_KEY)
    store.removeItem(USER_KEY)
    store.removeItem(REFRESH_KEY)
    store.removeItem(EXPIRES_KEY)
  }
}

let refreshing: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  if (refreshing) return refreshing
  refreshing = refreshSessionOnce().finally(() => {
    refreshing = null
  })
  return refreshing
}

async function refreshSessionOnce(): Promise<boolean> {
  const refreshToken = localStorage.getItem(REFRESH_KEY) ?? ""
  if (!refreshToken) return false
  let response: Response
  try {
    response = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refreshToken }),
    })
  } catch {
    return false
  }
  if (!response.ok) {
    if (response.status === 400 || response.status === 401) clearSession()
    return false
  }
  const payload = (await response.json()) as SessionPayload
  setSession(payload.accessToken, payload.username || getUsername(), payload.refreshToken || refreshToken, payload.expiresIn ?? 0)
  return true
}

async function ensureFreshToken() {
  if (!getToken()) return
  if (!localStorage.getItem(REFRESH_KEY)) return
  const expiresAt = Number(localStorage.getItem(EXPIRES_KEY) ?? "0")
  if (expiresAt && expiresAt - Date.now() > REFRESH_SKEW_MS) return
  await refreshSession()
}

export async function api<T>(path: string, init: RequestInit = {}, allowRefresh = true): Promise<T> {
  if (allowRefresh && !path.startsWith("/api/auth/")) await ensureFreshToken()
  const headers = new Headers(init.headers)
  const token = getToken()
  if (token) headers.set("Authorization", `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json")
  }
  const response = await fetch(path, { ...init, headers })
  if (response.status === 401 && allowRefresh && !path.startsWith("/api/auth/")) {
    const renewed = await refreshSession()
    if (renewed) return api(path, init, false)
    clearSession()
  }
  if (!response.ok) {
    let detail = "请求失败"
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) detail = payload.detail
    } catch {
      detail = response.statusText || detail
    }
    throw new ApiError(response.status, detail)
  }
  const type = response.headers.get("content-type") ?? ""
  if (type.includes("text/markdown") || type.includes("text/csv")) return (await response.text()) as T
  return (await response.json()) as T
}

export async function sendEmailCode(email: string) {
  return api<{ verificationId: string; expiresIn: number; isUser: boolean }>("/api/auth/email/code", {
    method: "POST",
    body: JSON.stringify({ email }),
  })
}

export async function completeEmail(email: string, verificationId: string, code: string, isUser: boolean) {
  return api<SessionPayload>("/api/auth/email/session", {
    method: "POST",
    body: JSON.stringify({ email, verificationId, code, isUser }),
  })
}

export async function listVideos() {
  return api<{ items: VideoSummary[] }>("/api/videos")
}

export async function getVideo(videoId: string) {
  return api<VideoDetail>(`/api/videos/${videoId}`)
}

export async function previewImport(file: File, mapping: Record<string, string>) {
  const form = new FormData()
  form.set("file", file)
  form.set("mapping", JSON.stringify(mapping))
  return api<PreviewResult>("/api/imports/preview", { method: "POST", body: form })
}

export async function commitImport(file: File, mapping: Record<string, string>) {
  const form = new FormData()
  form.set("file", file)
  form.set("mapping", JSON.stringify(mapping))
  return api<VideoSummary>("/api/imports", { method: "POST", body: form })
}

export async function startAnalysis(videoId: string, thinkingEnabled: boolean) {
  return api<{ task: VideoDetail["task"] }>(`/api/videos/${videoId}/analyze`, {
    method: "POST",
    body: JSON.stringify({ thinkingEnabled }),
  })
}

export async function retryAnalysis(videoId: string, thinkingEnabled: boolean) {
  return api<{ task: VideoDetail["task"] }>(`/api/videos/${videoId}/retry`, {
    method: "POST",
    body: JSON.stringify({ thinkingEnabled }),
  })
}

export async function cancelAnalysis(videoId: string) {
  return api<{ task: VideoDetail["task"] }>(`/api/videos/${videoId}/cancel`, { method: "POST" })
}

export async function removeVideo(videoId: string) {
  return api<{ ok: boolean }>(`/api/videos/${videoId}`, { method: "DELETE" })
}

export async function listComments(videoId: string, filter: CommentFilter) {
  const params = new URLSearchParams()
  if (filter.q) params.set("q", filter.q)
  if (filter.label) params.set("label", filter.label)
  if (filter.themeId) params.set("themeId", filter.themeId)
  if (filter.cited) params.set("cited", filter.cited)
  params.set("order", filter.order)
  params.set("limit", "300")
  return api<{ total: number; items: CommentRow[] }>(`/api/videos/${videoId}/comments?${params}`)
}

export async function saveOpportunity(videoId: string, patch: Record<string, unknown>) {
  return api<NonNullable<VideoDetail["report"]>>(`/api/videos/${videoId}/report`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  })
}

export async function downloadReport(videoId: string, kind: "md" | "csv") {
  const text = await api<string>(`/api/videos/${videoId}/export.${kind}`)
  const blob = new Blob([text], { type: kind === "md" ? "text/markdown" : "text/csv" })
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = `评见-${videoId}.${kind}`
  link.click()
  URL.revokeObjectURL(url)
}

// EventSource 不能带 Authorization，所以进度流用 fetch 自己读 SSE。
export async function subscribeEvents(
  videoId: string,
  after: number,
  onEvent: (event: TaskEvent) => void,
  signal: AbortSignal,
) {
  await ensureFreshToken()
  const response = await fetch(`/api/videos/${videoId}/stream?after=${after}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
    signal,
  })
  if (!response.ok || !response.body) throw new ApiError(response.status, "进度连接中断")
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  while (!signal.aborted) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const chunks = buffer.split("\n\n")
    buffer = chunks.pop() ?? ""
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((item) => item.startsWith("data:"))
      if (!line) continue
      onEvent(JSON.parse(line.slice(5).trim()) as TaskEvent)
    }
  }
}
