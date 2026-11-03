"""DSH 编排。

代理只按顺序调用六个工具。工具真正的工作通过本机 HTTP 回到 FastAPI。
on_notification 里如果看到思考文本，整段通知直接丢掉，不写入事件表。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from server.analysis import StageError, execute_tool
from server.config import OPTIONAL_TOOLS, TOOL_ORDER
from server.events import append_event, list_events
from server.prompts import ORCHESTRATOR_SYSTEM
from server.store import TASKS

PATCH_PATH = Path(__file__).resolve().parents[1] / "dsh" / "strip-shell.patch.yml"


def fresh_session_id(task_id: str) -> str:
    """每次开跑都新建 session。重试和重启沿用 taskId，不能再 create 同一个 DSH session。"""
    return f"pj{task_id[:16]}{uuid.uuid4().hex[:16]}"


def dsh_status() -> str:
    try:
        import deepseek_harness  # noqa: F401
    except Exception as exc:
        return f"unavailable: {exc.__class__.__name__}"
    return "available"


def map_notification(note: object) -> dict | None:
    """把 DSH 通知收成一条可展示的事件。思考类通知返回 None。"""
    data = _as_data(note)
    event_type = _find_named(data, ("type", "eventType", "sessionUpdate", "updateType"))
    if event_type and any(word in event_type.lower() for word in ("think", "reason", "thought")):
        return None
    tool = _find_tool(data)
    if tool:
        if _is_result(data, event_type):
            if _is_error(data):
                return {"type": "failed", "stage": tool, "message": f"{tool} 返回失败"}
            return {"type": "stage_completed", "stage": tool, "message": f"{tool} 结束"}
        return {"type": "stage_started", "stage": tool, "message": f"{tool} 开始"}
    if event_type and "assistant" in event_type.lower() and not _has_thinking(data):
        text = _find_text(data)
        if text and 4 <= len(text) <= 180:
            return {"type": "summary", "stage": "", "message": text}
    return None


def should_record(existing: list[dict], mapped: dict | None) -> bool:
    """工具自己已经写过的阶段不再记第二遍。失败之后不能再补一条完成。"""
    if not mapped:
        return False
    if mapped["type"] == "summary":
        return True
    for event in existing:
        same_stage = event.get("stage") == mapped.get("stage")
        if event.get("type") == "failed" and same_stage and mapped["type"] == "stage_completed":
            return False
        if event.get("type") == mapped["type"] and same_stage:
            return False
    return True


def record_notification(store, task_id: str, note: object) -> None:
    try:
        mapped = map_notification(note)
        rows = store.find_all(TASKS, {"taskId": task_id})
        if not rows:
            return
        task = rows[0]
        existing = list_events(store, task["uid"], task_id=task_id)
        if not should_record(existing, mapped):
            return
        append_event(store, task, mapped["type"], mapped.get("stage") or "", mapped.get("message") or "")
    except Exception:
        # 通知解析失败不能把正在跑的工具打断，阶段事件仍由工具自己写入。
        return


class DirectOrchestrator:
    """测试用。生产环境走 DSH，但六个工具函数是同一套。"""

    def __init__(self, store, model):
        self.store = store
        self.model = model

    def run(self, task_id: str, title: str) -> None:
        del title
        for name in TOOL_ORDER:
            execute_tool(self.store, self.model, task_id, name, self._emit)

    def _emit(self, task: dict, event_type: str, stage: str, message: str) -> None:
        append_event(self.store, task, event_type, stage, message)


class DshOrchestrator:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def run(self, task_id: str, title: str) -> None:
        try:
            from deepseek_harness import DeepSeekHarness
        except Exception as exc:
            raise StageError(
                "DSH 运行时不可用。请在 Linux 容器里安装 deepseek-harness-sdk 后启动；"
                f"当前进程无法加载它（{exc.__class__.__name__}）。"
            ) from exc
        if not PATCH_PATH.is_file():
            raise StageError("缺少去掉 shell 的 DSH profile patch")
        # 子进程会继承这些变量。插件在执行工具时才读取令牌，所以不能写进镜像。
        os.environ["DSH_SYSTEM_PROMPT"] = ORCHESTRATOR_SYSTEM
        os.environ["PINGJIAN_TOOL_TOKEN"] = self.settings.tool_token
        os.environ["PINGJIAN_INTERNAL_BASE"] = self.settings.internal_base
        os.environ["DEEPSEEK_API_KEY"] = self.settings.deepseek_api_key
        os.environ["DSH_HOME"] = self.settings.dsh_home
        os.makedirs(self.settings.dsh_home, exist_ok=True)
        os.makedirs("/tmp/pingjian-workspace", exist_ok=True)
        user_prompt = json.dumps({"taskId": task_id, "title": title}, ensure_ascii=False)

        def on_notification(note: object) -> None:
            record_notification(self.store, task_id, note)

        with DeepSeekHarness(
            dsh_home=self.settings.dsh_home,
            cwd="/tmp/pingjian-workspace",
            profile="sdk-minimal",
            provider="deepseek-official",
            model=self.settings.deepseek_model,
            reasoning_effort="high",
            patches=(str(PATCH_PATH),),
            api_key=self.settings.deepseek_api_key,
        ) as harness:
            # 最终两句说明只存在于这次返回值里。这里不落库，避免把思考或自由发挥写成报告。
            harness.run(user_prompt, session_id=fresh_session_id(task_id), on_notification=on_notification)


def _as_data(note: object):
    if hasattr(note, "model_dump"):
        return note.model_dump()
    if isinstance(note, (dict, list, str, int, float, bool)) or note is None:
        return note
    return {"type": note.__class__.__name__}


def _has_thinking(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower().replace("_", "")
            if "thinking" in lowered or "reasoning" in lowered or lowered in {"thought", "chainofthought"}:
                return True
            if _has_thinking(item):
                return True
    if isinstance(value, list):
        return any(_has_thinking(item) for item in value)
    return False


def _find_tool(value) -> str | None:
    if isinstance(value, dict):
        for key in ("toolName", "tool", "name"):
            item = value.get(key)
            if isinstance(item, str) and (item in TOOL_ORDER or item in OPTIONAL_TOOLS):
                return item
        for key, item in value.items():
            lowered = str(key).lower()
            if "think" in lowered or "reason" in lowered:
                continue
            found = _find_tool(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_tool(item)
            if found:
                return found
    return None


def _find_named(value, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item:
                return item
        for item in value.values():
            found = _find_named(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_named(item, keys)
            if found:
                return found
    return ""


def _find_text(value) -> str:
    if isinstance(value, dict):
        for key in ("text", "message", "content"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return " ".join(item.split())
        for key, item in value.items():
            lowered = str(key).lower()
            if "think" in lowered or "reason" in lowered:
                continue
            found = _find_text(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_text(item)
            if found:
                return found
    return ""


def _is_result(value, event_type: str) -> bool:
    if event_type and "result" in event_type.lower():
        return True
    if isinstance(value, dict) and "isError" in value:
        return True
    return False


def _is_error(value) -> bool:
    if isinstance(value, dict):
        if value.get("isError") is True:
            return True
        status = str(value.get("status") or value.get("type") or "").lower()
        if status in {"error", "failed"}:
            return True
        return any(_is_error(item) for item in value.values())
    if isinstance(value, list):
        return any(_is_error(item) for item in value)
    return False
