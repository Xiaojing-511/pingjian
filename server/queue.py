"""单进程队列。同一时刻只跑一个分析，其余任务保持 queued。"""

from __future__ import annotations

import queue
import threading

from server.analysis import AnalysisCancelled, reset_analysis
from server.config import PROMPT_VERSION, TOOL_ORDER
from server.dsh_runner import DirectOrchestrator, DshOrchestrator
from server.events import append_event, utcnow
from server.store import TASKS, VIDEOS


class AnalysisQueue:
    def __init__(self, store, settings, model, orchestrator: str, inline: bool):
        self.store = store
        self.settings = settings
        self.model = model
        self.orchestrator_name = orchestrator
        self.inline = inline
        self._items: queue.Queue[str] = queue.Queue()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._order_lock = threading.Lock()
        self._order = 0

    def next_order(self) -> int:
        with self._order_lock:
            self._order += 1
            return self._order

    def start(self) -> None:
        if self._thread:
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="pingjian-analysis", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def submit(self, task_id: str) -> None:
        if self.inline:
            self.run_one(task_id)
            return
        self._items.put(task_id)

    def recover(self) -> None:
        """进程重启后，没跑完的任务回到队尾，仍使用已经导入的评论。"""
        tasks = self.store.find_all(TASKS, {"promptVersion": PROMPT_VERSION})
        if tasks:
            self._order = max(int(item.get("queueOrder") or 0) for item in tasks)
        pending = [item for item in tasks if item.get("status") in {"queued", "started"}]
        pending.sort(key=lambda item: int(item.get("queueOrder") or 0))
        for task in pending:
            reset_analysis(self.store, task)
            self.store.update_one(
                TASKS,
                {"uid": task["uid"], "taskId": task["taskId"]},
                {
                    "status": "queued",
                    "error": None,
                    "stages": {},
                    "eventSeq": 0,
                    "progress": {"stage": "queued", "done": 0, "total": 1},
                    "updatedAt": utcnow(),
                },
            )
            fresh = dict(task)
            fresh["eventSeq"] = 0
            append_event(self.store, fresh, "summary", "", "服务重启，任务重新排队")
            append_event(self.store, fresh, "queued", "", "任务已排队")
            self._items.put(task["taskId"])

    def run_one(self, task_id: str) -> None:
        if hasattr(self.model, "clear_cancel"):
            self.model.clear_cancel()
        rows = self.store.find_all(TASKS, {"taskId": task_id})
        if not rows:
            return
        task = rows[0]
        if task.get("status") not in {"queued", "started"} or task.get("cancelRequested"):
            return
        previous = task.get("status")
        self.store.update_one(
            TASKS,
            {"uid": task["uid"], "taskId": task["taskId"], "status": previous},
            {"status": "started", "updatedAt": utcnow()},
        )
        task = self._reload(task)
        if task.get("status") != "started" or task.get("cancelRequested"):
            return
        append_event(self.store, task, "started", "", "开始分析")
        videos = self.store.find_all(VIDEOS, {"uid": task["uid"], "videoId": task["videoId"]})
        title = videos[0].get("title") if videos else ""
        try:
            if hasattr(self.model, "thinking_enabled"):
                self.model.thinking_enabled = bool(task.get("thinkingEnabled", False))
            self._orchestrator().run(task_id, title or "")
        except AnalysisCancelled:
            return
        except Exception as exc:
            current = self._reload(task)
            if current.get("status") != "failed":
                message = str(exc) or "分析失败"
                self.store.update_one(
                    TASKS,
                    {"uid": task["uid"], "taskId": task["taskId"]},
                    {"status": "failed", "error": message, "updatedAt": utcnow()},
                )
                append_event(self.store, task, "failed", "", message)
            return
        current = self._reload(task)
        commit_ok = ((current.get("stages") or {}).get("commit_report") or {}).get("ok")
        if commit_ok and current.get("status") != "failed":
            self.store.update_one(
                TASKS,
                {"uid": task["uid"], "taskId": task["taskId"]},
                {"status": "completed", "error": None, "updatedAt": utcnow()},
            )
            append_event(self.store, task, "completed", "commit_report", "分析完成")
            return
        if current.get("status") != "failed":
            message = "编排没有完成报告提交"
            self.store.update_one(
                TASKS,
                {"uid": task["uid"], "taskId": task["taskId"]},
                {"status": "failed", "error": message, "updatedAt": utcnow()},
            )
            append_event(self.store, task, "failed", "", message)

    def _loop(self) -> None:
        while not self._stop:
            try:
                task_id = self._items.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.run_one(task_id)
            finally:
                self._items.task_done()

    def _reload(self, task: dict) -> dict:
        rows = self.store.find_all(TASKS, {"uid": task["uid"], "taskId": task["taskId"]})
        return rows[0] if rows else task

    def _orchestrator(self):
        if self.orchestrator_name == "direct":
            return DirectOrchestrator(self.store, self.model)
        return DshOrchestrator(self.store, self.settings)


def tool_names() -> tuple[str, ...]:
    return TOOL_ORDER
