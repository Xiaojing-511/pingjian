"""HTTP 边界。页面能看到的状态都从这里读，分析工具走内部令牌，不走用户令牌。"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from server.analysis import execute_reclassify, execute_tool
from server.clean import CleanError
from server.cloudbase import CloudBaseError
from server.config import OPTIONAL_TOOLS, PROMPT_VERSION, TOOL_ORDER
from server.events import append_event, list_events
from server.export_report import render_csv, render_markdown
from server.service import (
    AppError,
    commit_import,
    delete_video,
    list_comment_page,
    list_videos,
    load_export,
    preview_import,
    public_task,
    cancel_analysis,
    request_analysis,
    update_report,
    video_detail,
)
from server.store import MissingConfig, TASKS


class EmailCodeIn(BaseModel):
    email: str


class EmailSessionIn(BaseModel):
    email: str
    verificationId: str
    code: str
    isUser: bool


class RefreshIn(BaseModel):
    refreshToken: str


class ReportClaimEdit(BaseModel):
    index: int
    text: str | None = None


class ReportPatch(BaseModel):
    themeId: str
    topic: str | None = None
    structure: list[str] | None = None
    targetNeed: str | None = None
    validation: str | None = None
    favorite: bool | None = None
    claims: list[ReportClaimEdit] | None = None


class ToolIn(BaseModel):
    taskId: str = Field(min_length=8)


class AnalyzeOptions(BaseModel):
    thinkingEnabled: bool = False


def register(app: FastAPI) -> None:
    @app.exception_handler(CloudBaseError)
    async def handle_cloudbase(_request: Request, exc: CloudBaseError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/health")
    def health(request: Request) -> dict:
        settings = request.app.state.settings
        return {
            "ok": True,
            "missing": settings.missing(),
            "model": settings.deepseek_model,
            "promptVersion": PROMPT_VERSION,
            "dsh": request.app.state.dsh_status(),
            "startupError": request.app.state.startup_error,
        }

    @app.post("/api/auth/email/code")
    def send_email_code(body: EmailCodeIn, request: Request) -> dict:
        email = _email(body.email)
        try:
            return request.app.state.auth.send_email_code(email)
        except Exception as exc:
            _fail(400, str(exc) or "验证码发送失败")

    @app.post("/api/auth/email/session")
    def email_session(body: EmailSessionIn, request: Request) -> dict:
        email = _email(body.email)
        verification_id = body.verificationId.strip()
        code = body.code.strip()
        if not verification_id:
            _fail(400, "请先获取验证码")
        if not re.fullmatch(r"\d{6}", code):
            _fail(400, "请输入 6 位验证码")
        try:
            return request.app.state.auth.complete_email(email, verification_id, code, body.isUser)
        except Exception as exc:
            _fail(400, str(exc) or "认证失败")

    @app.post("/api/auth/refresh")
    def refresh_session(body: RefreshIn, request: Request) -> dict:
        refresh_token = body.refreshToken.strip()
        if not refresh_token:
            _fail(401, "登录已过期，请重新登录")
        try:
            return request.app.state.auth.refresh_session(refresh_token)
        except Exception as exc:
            _fail(401, str(exc) or "登录已过期，请重新登录")

    @app.get("/api/me")
    def me(user: dict = Depends(get_user)) -> dict:
        return {"uid": user["uid"], "username": user.get("username") or ""}

    @app.post("/api/imports/preview")
    async def preview(request: Request, user: dict = Depends(get_user)) -> dict:
        filename, content, mapping = await _read_upload(request)
        try:
            return preview_import(request.app.state.store, user["uid"], filename, content, mapping)
        except (CleanError, MissingConfig) as exc:
            _raise(exc)

    @app.post("/api/imports")
    async def submit_import(request: Request, user: dict = Depends(get_user)) -> dict:
        filename, content, mapping = await _read_upload(request)
        try:
            return commit_import(request.app.state.store, user["uid"], filename, content, mapping)
        except (CleanError, MissingConfig, AppError) as exc:
            _raise(exc)

    @app.get("/api/videos")
    def videos(request: Request, user: dict = Depends(get_user)) -> dict:
        try:
            return {"items": list_videos(request.app.state.store, user["uid"])}
        except MissingConfig as exc:
            _raise(exc)

    @app.get("/api/videos/{video_id}")
    def video(video_id: str, request: Request, user: dict = Depends(get_user)) -> dict:
        try:
            return video_detail(request.app.state.store, user["uid"], video_id)
        except (AppError, MissingConfig) as exc:
            _raise(exc)

    @app.delete("/api/videos/{video_id}")
    def remove_video(video_id: str, request: Request, user: dict = Depends(get_user)) -> dict:
        try:
            delete_video(request.app.state.store, user["uid"], video_id)
        except (AppError, MissingConfig) as exc:
            _raise(exc)
        return {"ok": True}

    @app.post("/api/videos/{video_id}/analyze")
    def analyze(video_id: str, request: Request, body: AnalyzeOptions = AnalyzeOptions(), user: dict = Depends(get_user)) -> dict:
        return _queue_analysis(request, user["uid"], video_id, retry=False, thinking_enabled=body.thinkingEnabled)

    @app.post("/api/videos/{video_id}/retry")
    def retry(video_id: str, request: Request, body: AnalyzeOptions = AnalyzeOptions(), user: dict = Depends(get_user)) -> dict:
        return _queue_analysis(request, user["uid"], video_id, retry=True, thinking_enabled=body.thinkingEnabled)

    @app.post("/api/videos/{video_id}/cancel")
    def cancel(video_id: str, request: Request, user: dict = Depends(get_user)) -> dict:
        try:
            task = cancel_analysis(request.app.state.store, request.app.state.model, user["uid"], video_id)
        except (AppError, MissingConfig) as exc:
            _raise(exc)
        return {"task": task}

    @app.get("/api/videos/{video_id}/events")
    def events(video_id: str, request: Request, user: dict = Depends(get_user)) -> dict:
        try:
            detail = video_detail(request.app.state.store, user["uid"], video_id)
        except (AppError, MissingConfig) as exc:
            _raise(exc)
        return {"task": detail["task"], "events": detail["events"]}

    @app.get("/api/videos/{video_id}/stream")
    async def stream(video_id: str, request: Request, after: int = 0, user: dict = Depends(get_user)):
        store = request.app.state.store
        try:
            detail = video_detail(store, user["uid"], video_id)
        except (AppError, MissingConfig) as exc:
            _raise(exc)
        if not detail["task"]:
            _fail(404, "没有分析任务")

        async def generate():
            last = after
            started = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                events = await asyncio.to_thread(list_events, store, user["uid"], video_id, detail["task"]["taskId"])
                fresh = [item for item in events if item["seq"] > last]
                for item in fresh:
                    last = item["seq"]
                    yield _sse(item)
                task_rows = await asyncio.to_thread(store.find_all, TASKS, {"uid": user["uid"], "taskId": detail["task"]["taskId"]})
                status = task_rows[0]["status"] if task_rows else "failed"
                if status in {"completed", "failed"} and not fresh:
                    break
                if time.monotonic() - started > 1800:
                    break
                yield ": keepalive\n\n"
                await asyncio.sleep(0.7)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/videos/{video_id}/comments")
    def comments(
        video_id: str,
        request: Request,
        label: str | None = None,
        themeId: str | None = None,
        q: str | None = None,
        cited: str | None = None,
        order: str = "desc",
        offset: int = 0,
        limit: int = 200,
        user: dict = Depends(get_user),
    ) -> dict:
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        try:
            return list_comment_page(
                request.app.state.store,
                user["uid"],
                video_id,
                label=label,
                theme_id=themeId,
                keyword=q,
                cited=cited,
                order=order,
                offset=offset,
                limit=limit,
            )
        except (AppError, MissingConfig) as exc:
            _raise(exc)

    @app.patch("/api/videos/{video_id}/report")
    def patch_report(video_id: str, patch: ReportPatch, request: Request, user: dict = Depends(get_user)) -> dict:
        try:
            return update_report(request.app.state.store, user["uid"], video_id, patch.model_dump())
        except (AppError, MissingConfig) as exc:
            _raise(exc)

    @app.get("/api/videos/{video_id}/export.md")
    def export_md(video_id: str, request: Request, user: dict = Depends(get_user)) -> Response:
        video, report, _comments = _export_source(request, user["uid"], video_id)
        filename = quote(f"评见-{video_id}.md")
        return Response(
            render_markdown(video, report),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    @app.get("/api/videos/{video_id}/export.csv")
    def export_csv(video_id: str, request: Request, user: dict = Depends(get_user)) -> Response:
        video, report, comments = _export_source(request, user["uid"], video_id)
        filename = quote(f"评见-{video_id}.csv")
        return Response(
            "\ufeff" + render_csv(video, report, comments),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    @app.post("/internal/tools/{name}")
    def internal_tool(
        name: str,
        body: ToolIn,
        request: Request,
        x_pingjian_tool_token: str = Header(default=""),
    ) -> dict:
        settings = request.app.state.settings
        if not _token_ok(x_pingjian_tool_token, settings.tool_token):
            _fail(401, "工具令牌无效")
        if name not in TOOL_ORDER and name not in OPTIONAL_TOOLS:
            _fail(404, "未知工具")

        def emit(task: dict, event_type: str, stage: str, message: str) -> None:
            append_event(request.app.state.store, task, event_type, stage, message)

        try:
            if name in OPTIONAL_TOOLS:
                summary = execute_reclassify(request.app.state.store, request.app.state.model, body.taskId, emit)
            else:
                summary = execute_tool(request.app.state.store, request.app.state.model, body.taskId, name, emit)
        except Exception as exc:
            _fail(400, str(exc) or "工具执行失败")
        return {"summary": summary}


def get_user(request: Request, authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        _fail(401, "请先登录")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        _fail(401, "请先登录")
    cached = request.app.state.user_cache.get(token)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]
    try:
        user = request.app.state.auth.user_from_token(token)
    except Exception as exc:
        message = str(exc) or "登录已过期或无效"
        _fail(401, message)
    request.app.state.user_cache[token] = (now + 30, user)
    return user


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL.fullmatch(email):
        _fail(400, "请填写有效邮箱")
    return email


def _queue_analysis(request: Request, uid: str, video_id: str, *, retry: bool, thinking_enabled: bool) -> dict:
    try:
        task = request_analysis(
            request.app.state.store,
            request.app.state.queue,
            request.app.state.settings,
            uid,
            video_id,
            retry=retry,
            thinking_enabled=thinking_enabled,
        )
    except (AppError, MissingConfig) as exc:
        _raise(exc)
    return {"task": task or public_task(request.app.state.store, None)}


async def _read_upload(request: Request) -> tuple[str, bytes, dict | None]:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        _fail(400, "请上传 CSV 或 XLSX 文件")
    content = await upload.read()
    close = getattr(upload, "close", None)
    if close:
        await close()
    if not content:
        _fail(400, "文件是空的")
    if len(content) > 20 * 1024 * 1024:
        _fail(400, "文件超过 20MB")
    raw_mapping = form.get("mapping") or "{}"
    try:
        mapping = json.loads(raw_mapping)
    except json.JSONDecodeError:
        _fail(400, "字段映射不是 JSON")
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        _fail(400, "字段映射不是 JSON 对象")
    filename = getattr(upload, "filename", None) or "import.csv"
    return filename, content, mapping


def _export_source(request: Request, uid: str, video_id: str):
    try:
        return load_export(request.app.state.store, uid, video_id)
    except (AppError, MissingConfig) as exc:
        _raise(exc)


def _token_ok(given: str, expected: str) -> bool:
    if not given or not expected or len(given) != len(expected):
        return False
    return secrets.compare_digest(given, expected)


def _sse(event: dict) -> str:
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _raise(exc: Exception) -> None:
    if isinstance(exc, AppError):
        _fail(exc.status, str(exc))
    if isinstance(exc, MissingConfig):
        _fail(400, str(exc))
    if isinstance(exc, CleanError):
        _fail(400, str(exc))
    raise exc


def _fail(status: int, message: str) -> None:
    from fastapi import HTTPException

    raise HTTPException(status_code=status, detail=message)
