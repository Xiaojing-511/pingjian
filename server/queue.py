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
        self._attempt_lock = threading.Lock()
        self._attempt_token = 0
        self._active_orchestrator = None
        self.orchestrator_factory = None

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
                    "progress": {"stage": "queued", "done": 0, "total": 1},
                    "updatedAt": utcnow(),
                },
            )
            fresh = dict(task)
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
        generation = int(task.get("queueOrder") or 0)
        self._attempt_token = generation
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_attempt,
            args=(task, generation, stop),
            name="pingjian-attempt-watch",
            daemon=True,
        )
        watcher.start()
        self._bind_notice(task, generation)
        outcome: object = "ok"
        try:
            try:
                append_event(self.store, task, "started", "", "开始分析")
                videos = self.store.find_all(VIDEOS, {"uid": task["uid"], "videoId": task["videoId"]})
                title = videos[0].get("title") if videos else ""
                if hasattr(self.model, "thinking_enabled"):
                    self.model.thinking_enabled = bool(task.get("thinkingEnabled", False))
                self._orchestrator().run(task_id, title or "")
            except AnalysisCancelled:
                outcome = "cancelled"
            except Exception as exc:
                outcome = exc
        finally:
            with self._attempt_lock:
                stop.set()
            if hasattr(self.model, "notice"):
                self.model.notice = None
        if outcome == "cancelled" or not self._owns(task, generation):
            return
        current = self._reload(task)
        if isinstance(outcome, Exception):
            if current.get("status") != "failed":
                message = str(outcome) or "分析失败"
                self.store.update_one(
                    TASKS,
                    {"uid": task["uid"], "taskId": task["taskId"]},
                    {"status": "failed", "error": message, "updatedAt": utcnow()},
                )
                append_event(self.store, task, "failed", "", message)
            return
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

    def _owns(self, task: dict, generation: int) -> bool:
        current = self._reload(task)
        return int(current.get("queueOrder") or 0) == generation

    def _watch_attempt(self, task: dict, generation: int, stop: threading.Event) -> None:
        """工具已经把任务写成失败、或用户已经重新排队时，不要让上一次编排继续占着队列。"""
        while True:
            if stop.wait(0.4):
                return
            with self._attempt_lock:
                if stop.is_set():
                    return
                current = self._reload(task)
                still_running = int(current.get("queueOrder") or 0) == generation and current.get("status") == "started"
            if still_running:
                continue
            self._abandon_active(generation)
            return

    def _abandon_active(self, generation: int) -> None:
        orchestrator = self._active_orchestrator
        if getattr(orchestrator, "attempt_token", None) != generation:
            return
        cancel = getattr(self.model, "request_cancel", None)
        if cancel:
            try:
                cancel()
            except Exception:
                pass
        abandon = getattr(orchestrator, "abandon", None)
        if abandon:
            try:
                abandon()
            except Exception:
                return

    def _bind_notice(self, task: dict, generation: int) -> None:
        if not hasattr(self.model, "notice"):
            return

        def notice(message: str) -> None:
            current = self._reload(task)
            if int(current.get("queueOrder") or 0) != generation or current.get("status") != "started":
                return
            stage = str((current.get("progress") or {}).get("stage") or "")
            append_event(self.store, current, "summary", stage, message)

        self.model.notice = notice

    def _orchestrator(self):
        if self.orchestrator_factory is not None:
            orchestrator = self.orchestrator_factory()
        elif self.orchestrator_name == "direct":
            orchestrator = DirectOrchestrator(self.store, self.model)
        else:
            orchestrator = DshOrchestrator(self.store, self.settings)
        try:
            orchestrator.attempt_token = self._attempt_token
        except Exception:
            pass
        self._active_orchestrator = orchestrator
        return orchestrator


def tool_names() -> tuple[str, ...]:
    return TOOL_ORDER
