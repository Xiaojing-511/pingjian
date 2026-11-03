"""任务事件。只保存阶段和工具的开始、结束、摘要、完成、失败，不保存思考文本。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from server.store import EVENTS, TASKS

EVENT_TYPES = {
    "queued",
    "started",
    "stage_started",
    "stage_completed",
    "summary",
    "completed",
    "failed",
}

_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def append_event(store, task: dict, event_type: str, stage: str, message: str) -> dict:
    if event_type not in EVENT_TYPES:
        raise RuntimeError(f"未知事件 {event_type}")
    # message 由我们自己写。调用方不能把模型思考原文塞进来。
    text = " ".join(str(message or "").split())[:500]
    with _lock:
        rows = store.find_all(TASKS, {"uid": task["uid"], "taskId": task["taskId"]})
        if not rows:
            raise RuntimeError("任务不存在，不能写事件")
        current = rows[0]
        seq = int(current.get("eventSeq") or 0) + 1
        store.update_one(
            TASKS,
            {"uid": task["uid"], "taskId": task["taskId"]},
            {"eventSeq": seq, "updatedAt": utcnow()},
        )
        stored = {
            "uid": task["uid"],
            "videoId": task["videoId"],
            "taskId": task["taskId"],
            "seq": seq,
            "type": event_type,
            "stage": stage,
            "message": text,
            "createdAt": utcnow(),
        }
        store.insert(EVENTS, stored)
    return public_event(stored)


def list_events(store, uid: str, video_id: str | None = None, task_id: str | None = None) -> list[dict]:
    query: dict = {"uid": uid}
    if video_id:
        query["videoId"] = video_id
    if task_id:
        query["taskId"] = task_id
    rows = store.find_all(EVENTS, query)
    rows.sort(key=lambda item: item.get("seq") or 0)
    return [public_event(item) for item in rows]


def public_event(event: dict) -> dict:
    return {
        "seq": event.get("seq") or 0,
        "taskId": event.get("taskId"),
        "videoId": event.get("videoId"),
        "type": event.get("type"),
        "stage": event.get("stage") or "",
        "message": event.get("message") or "",
        "createdAt": event.get("createdAt"),
    }
