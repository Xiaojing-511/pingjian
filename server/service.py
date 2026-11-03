"""导入、删除、重试和报告编辑。这些操作都先按 uid 收窄，再碰 videoId。"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from server.analysis import (
    analysis_block_reason,
    cited_ids,
    comment_is_low,
    dedupe_comments,
    public_comment,
    reset_analysis,
    stored_labels,
)
from server.clean import CleanError, normalize_mapping, parse_import, read_table
from server.config import PROMPT_VERSION
from server.events import append_event, list_events, utcnow
from server.store import COMMENTS, EVENTS, REPORTS, TASKS, VIDEOS, MissingConfig
from server.validate import clip

DUPLICATE_VIDEO = "该视频已导入，请先删除已有记录"
_FILENAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def preview_import(store, uid: str, filename: str, content: bytes, mapping: dict | None) -> dict:
    headers, _rows = read_table(filename, content)
    try:
        chosen = normalize_mapping(headers, mapping)
    except CleanError as exc:
        return _preview_error(headers, {}, exc)
    try:
        result = parse_import(filename, content, chosen)
    except CleanError as exc:
        return _preview_error(headers, chosen, exc)
    duplicate = False
    blocked = None
    try:
        duplicate = _one(store, VIDEOS, {"uid": uid, "videoId": result.video_id}) is not None
    except MissingConfig as exc:
        blocked = str(exc)
    return {
        "columns": headers,
        "mapping": chosen,
        "samples": result.samples,
        "summary": result.stats,
        "video": _public_preview(result),
        "duplicate": duplicate,
        "error": DUPLICATE_VIDEO if duplicate else None,
        "blocked": blocked,
    }


def commit_import(store, uid: str, filename: str, content: bytes, mapping: dict | None) -> dict:
    result = parse_import(filename, content, mapping)
    if _one(store, VIDEOS, {"uid": uid, "videoId": result.video_id}):
        raise AppError(409, DUPLICATE_VIDEO)
    safe_name = _safe_filename(filename)
    file_info = store.upload_import(uid, result.video_id, safe_name, content)
    now = utcnow()
    video = {
        "uid": uid,
        "videoId": result.video_id,
        "title": result.title,
        "author": result.author,
        "videoUrl": result.video_url,
        "publishTime": result.publish_time,
        "description": result.description,
        "hashtags": result.hashtags,
        "sourceFileName": safe_name,
        "mapping": mapping or {},
        "cleanStats": result.stats,
        "file": file_info,
        "importedAt": now,
        "updatedAt": now,
    }
    comments = [{**comment, "uid": uid, "videoId": result.video_id} for comment in result.comments]
    try:
        store.insert(VIDEOS, video)
        store.insert_many(COMMENTS, comments)
    except Exception:
        store.remove(VIDEOS, {"uid": uid, "videoId": result.video_id})
        store.remove(COMMENTS, {"uid": uid, "videoId": result.video_id})
        try:
            store.delete_import(file_info)
        except Exception:
            pass
        raise
    return public_video(video)


def list_videos(store, uid: str) -> list[dict]:
    videos = store.find_all(VIDEOS, {"uid": uid})
    items = []
    for video in videos:
        task = _one(store, TASKS, {"uid": uid, "videoId": video["videoId"]})
        item = public_video(video)
        item["status"] = task.get("status") if task else "imported"
        items.append(item)
    items.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return items


def video_detail(store, uid: str, video_id: str) -> dict:
    video = _require_video(store, uid, video_id)
    task = _one(store, TASKS, {"uid": uid, "videoId": video_id})
    report = _one(store, REPORTS, {"uid": uid, "videoId": video_id})
    return {
        "video": public_video(video),
        "task": public_task(store, task),
        "report": public_report(report),
        "events": list_events(store, uid, video_id=video_id, task_id=task["taskId"] if task else None),
        "analysisBlock": analysis_block_reason(video.get("cleanStats")),
    }


def delete_video(store, uid: str, video_id: str) -> None:
    video = _require_video(store, uid, video_id)
    for collection in (COMMENTS, TASKS, EVENTS, REPORTS, VIDEOS):
        store.remove(collection, {"uid": uid, "videoId": video_id})
    store.delete_import(video.get("file"))


def list_comment_page(
    store,
    uid: str,
    video_id: str,
    *,
    label: str | None,
    theme_id: str | None,
    keyword: str | None,
    cited: str | None,
    order: str,
    offset: int,
    limit: int,
) -> dict:
    _require_video(store, uid, video_id)
    comments = dedupe_comments(store.find_all(COMMENTS, {"uid": uid, "videoId": video_id}))
    report = _one(store, REPORTS, {"uid": uid, "videoId": video_id})
    used = cited_ids(report)
    if label == "unlabeled":
        comments = [
            item
            for item in comments
            if not comment_is_low(item) and not stored_labels(item) and not item.get("themeId")
        ]
    elif label == "low":
        comments = [item for item in comments if comment_is_low(item)]
    elif label:
        comments = [item for item in comments if label in stored_labels(item)]
    if theme_id:
        comments = [item for item in comments if item.get("themeId") == theme_id]
    if keyword:
        needle = keyword.casefold()
        comments = [
            item
            for item in comments
            if needle in (item.get("rawText") or "").casefold() or needle in (item.get("aiText") or "").casefold()
        ]
    if cited == "yes":
        comments = [item for item in comments if item["commentId"] in used]
    elif cited == "no":
        comments = [item for item in comments if item["commentId"] not in used]
    comments.sort(key=lambda item: (item.get("likeCount") or 0, -(item.get("orderIndex") or 0)), reverse=order != "asc")
    page = comments[offset : offset + limit]
    return {"total": len(comments), "items": [public_comment(item, used) for item in page]}


def request_analysis(store, queue, settings, uid: str, video_id: str, *, retry: bool, thinking_enabled: bool = False) -> dict:
    if not settings.deepseek_api_key:
        raise AppError(400, "缺少 DEEPSEEK_API_KEY")
    video = _require_video(store, uid, video_id)
    task = _one(store, TASKS, {"uid": uid, "videoId": video_id})
    if task and task.get("status") == "completed":
        raise AppError(409, "报告已完成，不能重新分析")
    if task and task.get("status") in {"queued", "started"}:
        return public_task(store, task)
    if task and task.get("status") == "failed" and not retry:
        raise AppError(409, "分析失败，请重试这一条任务")
    if retry and (not task or task.get("status") != "failed"):
        raise AppError(409, "只有失败的任务可以重试")
    reason = analysis_block_reason(video.get("cleanStats"))
    if reason:
        raise AppError(400, reason)
    now = utcnow()
    if task is None:
        task = {
            "uid": uid,
            "videoId": video_id,
            "taskId": uuid.uuid4().hex,
            "status": "queued",
            "error": None,
            "queueOrder": queue.next_order(),
            "promptVersion": PROMPT_VERSION,
            "model": settings.deepseek_model,
            "thinkingEnabled": thinking_enabled,
            "progress": {"stage": "queued", "done": 0, "total": 1},
            "stages": {},
            "cancelRequested": False,
            "eventSeq": 0,
            "createdAt": now,
            "updatedAt": now,
        }
        store.insert(TASKS, task)
    else:
        reset_analysis(store, task)
        fields = {
            "status": "queued",
            "error": None,
            "stages": {},
            "cancelRequested": False,
            "eventSeq": 0,
            "queueOrder": queue.next_order(),
            "progress": {"stage": "queued", "done": 0, "total": 1},
            "model": settings.deepseek_model,
            "thinkingEnabled": thinking_enabled,
            "promptVersion": PROMPT_VERSION,
            "updatedAt": now,
        }
        store.update_one(TASKS, {"uid": uid, "taskId": task["taskId"]}, fields)
        task.update(fields)
    append_event(store, task, "queued", "", "任务已排队")
    queue.submit(task["taskId"])
    fresh = _one(store, TASKS, {"uid": uid, "taskId": task["taskId"]})
    return public_task(store, fresh or task)


def cancel_analysis(store, model, uid: str, video_id: str) -> dict:
    """停掉排队中或正在跑的任务。进行中的模型请求会被关掉，下一批不再开始。"""
    _require_video(store, uid, video_id)
    task = _one(store, TASKS, {"uid": uid, "videoId": video_id})
    if not task or task.get("status") not in {"queued", "started"}:
        raise AppError(409, "当前没有正在进行的分析")
    now = utcnow()
    fields = {
        "status": "failed",
        "error": "已取消分析",
        "cancelRequested": True,
        "updatedAt": now,
    }
    store.update_one(TASKS, {"uid": uid, "taskId": task["taskId"]}, fields)
    task.update(fields)
    append_event(store, task, "failed", str((task.get("progress") or {}).get("stage") or ""), "已取消分析")
    request_cancel = getattr(model, "request_cancel", None)
    if request_cancel:
        request_cancel()
    fresh = _one(store, TASKS, {"uid": uid, "taskId": task["taskId"]})
    return public_task(store, fresh or task)


def update_report(store, uid: str, video_id: str, patch: dict) -> dict:
    report = _one(store, REPORTS, {"uid": uid, "videoId": video_id})
    if not report:
        raise AppError(404, "没有报告")
    cards = report.get("opportunities") or []
    card = next((item for item in cards if item.get("themeId") == patch.get("themeId")), None)
    if card is None:
        raise AppError(404, "没有这条选题")
    if patch.get("topic") is not None:
        card["topic"] = clip(patch["topic"], 30)
    if patch.get("structure") is not None:
        card["structure"] = [clip(part, 20) for part in list(patch["structure"])[:5]]
    if patch.get("targetNeed") is not None:
        card["targetNeed"] = clip(patch["targetNeed"], 40)
    if patch.get("validation") is not None:
        card["validation"] = clip(patch["validation"], 40)
    if patch.get("favorite") is not None:
        card["favorite"] = bool(patch["favorite"])
    for edit in patch.get("claims") or []:
        index = edit.get("index")
        claims = card.get("claims") or []
        if isinstance(index, int) and 0 <= index < len(claims) and edit.get("text") is not None:
            claims[index]["text"] = clip(edit["text"], 200)
    store.update_one(
        REPORTS,
        {"uid": uid, "videoId": video_id, "taskId": report["taskId"]},
        {"opportunities": cards, "updatedAt": utcnow()},
    )
    fresh = _one(store, REPORTS, {"uid": uid, "videoId": video_id})
    return public_report(fresh)


def load_export(store, uid: str, video_id: str) -> tuple[dict, dict, list[dict]]:
    video = _require_video(store, uid, video_id)
    report = _one(store, REPORTS, {"uid": uid, "videoId": video_id})
    if not report:
        raise AppError(404, "没有报告")
    comments = dedupe_comments(store.find_all(COMMENTS, {"uid": uid, "videoId": video_id}))
    return video, report, comments


def public_video(video: dict) -> dict:
    return {
        "videoId": video.get("videoId"),
        "title": video.get("title") or "",
        "author": video.get("author") or "",
        "videoUrl": video.get("videoUrl") or "",
        "publishTime": video.get("publishTime") or "",
        "description": video.get("description") or "",
        "hashtags": video.get("hashtags") or [],
        "sourceFileName": video.get("sourceFileName") or "",
        "cleanStats": video.get("cleanStats") or {},
        "importedAt": video.get("importedAt"),
        "updatedAt": video.get("updatedAt"),
    }


def public_task(store, task: dict | None) -> dict | None:
    if not task:
        return None
    ahead = 0
    if task.get("status") == "queued":
        queued = store.find_all(TASKS, {"status": "queued"})
        ahead = sum(1 for item in queued if int(item.get("queueOrder") or 0) < int(task.get("queueOrder") or 0))
    return {
        "taskId": task.get("taskId"),
        "videoId": task.get("videoId"),
        "status": task.get("status"),
        "error": task.get("error"),
        "progress": task.get("progress"),
        "promptVersion": task.get("promptVersion"),
        "model": task.get("model"),
        "thinkingEnabled": bool(task.get("thinkingEnabled", False)),
        "queueAhead": ahead,
        "updatedAt": task.get("updatedAt"),
    }


def public_report(report: dict | None) -> dict | None:
    if not report:
        return None
    hidden = {"uid", "_id"}
    return {key: value for key, value in report.items() if key not in hidden}


def _require_video(store, uid: str, video_id: str) -> dict:
    video = _one(store, VIDEOS, {"uid": uid, "videoId": video_id})
    if not video:
        raise AppError(404, "没有这个视频")
    return video


def _one(store, collection: str, query: dict) -> dict | None:
    rows = store.find_all(collection, query)
    return rows[0] if rows else None


def _public_preview(result) -> dict:
    return {
        "videoId": result.video_id,
        "title": result.title,
        "author": result.author,
        "videoUrl": result.video_url,
        "publishTime": result.publish_time,
        "description": result.description,
        "hashtags": result.hashtags,
    }


def _preview_error(headers: list[str], mapping: dict, exc: CleanError) -> dict:
    return {
        "columns": headers,
        "mapping": mapping,
        "samples": [],
        "summary": exc.stats,
        "video": None,
        "duplicate": False,
        "error": str(exc),
        "blocked": None,
    }


def _safe_filename(name: str) -> str:
    base = Path(name or "import.csv").name
    cleaned = _FILENAME.sub("_", base).strip("._")
    return (cleaned or "import.csv")[:80]
