"""DeepSeek Chat Completions。

分类、聚类、排序、选题都从这里出去。定位也走这里。
inspect_import 和 commit_report 不调用模型。
JSON 解析不了，或没有覆盖全部输入 id，同一请求再试 1 次。
429 和 5xx 会按短退避再试几次。400 一类的请求错误不重试。
"""

from __future__ import annotations

import json
import threading
from typing import Callable

import httpx

from server.analysis import AnalysisCancelled
from server.config import DEFAULT_MODEL

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
TIMEOUT = httpx.Timeout(180, connect=10, write=30, pool=10)
# 这些状态是服务端临时忙或网关失败，请求多半没有执行完。
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ModelError(Exception):
    """停在哪一段、缺哪些 id，都留在异常里，任务失败时原样展示。"""

    def __init__(self, stage: str, missing_ids: list[str], detail: str, *, retryable: bool = False, limited: bool = False):
        self.stage = stage
        self.missing_ids = missing_ids
        self.detail = detail
        self.retryable = retryable
        # 连接超时只再试一次。503 这种马上返回的忙状态可以多试几次。
        self.limited = limited
        missing = ""
        if missing_ids:
            shown = "、".join(missing_ids[:12])
            extra = f" 等 {len(missing_ids)} 个" if len(missing_ids) > 12 else ""
            missing = f"，缺少 id：{shown}{extra}"
        super().__init__(f"模型调用失败，停在 {stage}{missing}。{detail}")


class DeepSeekClient:
    def __init__(self, api_key: str, model: str | None = None, *, retry_waits: tuple[float, ...] = (2, 4, 8), transport=None):
        self.api_key = api_key.strip()
        self.model = (model or "").strip() or DEFAULT_MODEL
        self.thinking_enabled = False
        self.retry_waits = retry_waits
        self._transport = transport
        self.notice: Callable[[str], None] | None = None
        self._cancel = threading.Event()
        self._clients_lock = threading.Lock()
        self._clients: set[httpx.Client] = set()

    def request_cancel(self) -> None:
        """关掉正在进行的模型请求，分类循环会在这一批停下。"""
        self._cancel.set()
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            client.close()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    def complete_json(
        self,
        *,
        stage: str,
        system: str,
        user: dict,
        required_ids: list[str] | None,
        items_key: str,
        id_field: str,
        max_tokens: int,
    ) -> dict:
        if not self.api_key:
            raise ModelError(stage, [], "未配置 DEEPSEEK_API_KEY")

        def post() -> str:
            if self._cancel.is_set():
                raise AnalysisCancelled("已取消分析")
            try:
                return self._post_chat(system=system, user=user, max_tokens=max_tokens)
            except AnalysisCancelled:
                raise
            except ModelError as exc:
                # 忙状态的退避已经在 _post_chat 里做完。这里只把阶段名从 request 改成当前这一段。
                raise ModelError(stage, [], exc.detail) from exc

        return complete_json_with(
            stage=stage,
            required_ids=required_ids,
            items_key=items_key,
            id_field=id_field,
            post=post,
        )

    def _post_chat(self, *, system: str, user: dict, max_tokens: int) -> str:
        attempts = len(self.retry_waits) + 1
        last_error: ModelError | None = None
        for index in range(attempts):
            if self._cancel.is_set():
                raise AnalysisCancelled("已取消分析")
            try:
                return self._post_once(system=system, user=user, max_tokens=max_tokens)
            except AnalysisCancelled:
                raise
            except ModelError as exc:
                last_error = exc
                give_up = not exc.retryable or index >= attempts - 1 or (exc.limited and index >= 1)
                if give_up:
                    raise ModelError("request", [], exc.detail) from exc
            self._tell("DeepSeek 繁忙，正在重试")
            if self._cancel.wait(self.retry_waits[index]):
                raise AnalysisCancelled("已取消分析")
        raise last_error or ModelError("request", [], "没有返回内容")

    def _tell(self, message: str) -> None:
        notice = self.notice
        if notice is None:
            return
        try:
            notice(message)
        except Exception:
            return

    def _post_once(self, *, system: str, user: dict, max_tokens: int) -> str:
        if self._cancel.is_set():
            raise AnalysisCancelled("已取消分析")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "stream": False,
            "thinking": {"type": "enabled" if self.thinking_enabled else "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        client_kwargs: dict = {"timeout": TIMEOUT}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        client = httpx.Client(**client_kwargs)
        with self._clients_lock:
            self._clients.add(client)
        try:
            response = client.post(DEEPSEEK_URL, headers=headers, json=body)
        except httpx.HTTPError as exc:
            if self._cancel.is_set():
                raise AnalysisCancelled("已取消分析") from exc
            raise ModelError("request", [], f"无法连接 DeepSeek：{exc}", retryable=True, limited=True) from exc
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            client.close()
        if self._cancel.is_set():
            raise AnalysisCancelled("已取消分析")
        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_STATUS
            raise ModelError(
                "request",
                [],
                f"DeepSeek 返回 {response.status_code}：{response.text[:300]}",
                retryable=retryable,
            )
        payload = response.json()
        message = ((payload.get("choices") or [{}])[0].get("message") or {})
        # reasoning_content 是思考文本，即使接口仍返回，也不拿来当 JSON。
        return str(message.get("content") or "")


def complete_json_with(
    *,
    stage: str,
    required_ids: list[str] | None,
    items_key: str,
    id_field: str,
    post: Callable[[], str],
) -> dict:
    """同一请求最多两次。第二次仍不完整，就把缺失 id 交回去。"""
    last_detail = "没有返回内容"
    last_missing: list[str] = []
    for _attempt in (1, 2):
        try:
            raw = post()
        except (ModelError, AnalysisCancelled):
            raise
        except Exception as exc:  # 网络错误不重试成无限循环，但同一次请求允许再试一次。
            last_detail = f"请求没有完成：{exc}"
            last_missing = []
            continue
        data, error = parse_json_content(raw)
        if error or data is None:
            last_detail = error or "JSON 无法解析"
            last_missing = []
            continue
        if required_ids is None:
            return data
        found = collect_ids(data, items_key, id_field)
        last_missing = [item_id for item_id in required_ids if item_id not in found]
        if last_missing:
            last_detail = "返回没有覆盖全部输入 id"
            continue
        return data
    raise ModelError(stage, last_missing, last_detail)


def parse_json_content(raw: str) -> tuple[dict | None, str | None]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None, "JSON 无法解析"
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None, "JSON 无法解析"
    if not isinstance(data, dict):
        return None, "JSON 根节点不是对象"
    return data, None


def collect_ids(data: dict, items_key: str, id_field: str) -> set[str]:
    rows = data.get(items_key) or []
    found: set[str] = set()
    if not isinstance(rows, list):
        return found
    for row in rows:
        if isinstance(row, dict) and row.get(id_field) is not None:
            found.add(str(row[id_field]))
    return found


