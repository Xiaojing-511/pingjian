"""CloudBase HTTP API。

文档库使用默认实例和默认数据库。调用方永远带管理员 API Key，浏览器不持有这把钥匙。
集合只创建 pingjian_*，这里不会读写 users、players、cards 或 eatwhat_*。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from server.store import COLLECTIONS

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


class CloudBaseError(Exception):
    pass


class CloudBaseStore:
    def __init__(self, env_id: str, api_key: str):
        self.env_id = env_id
        self.api_key = api_key
        self.base = f"https://{env_id}.api.tcloudbasegateway.com"
        self.db = f"{self.base}/v1/database/instances/(default)/databases/(default)"
        self.http = httpx.Client(timeout=60)

    def ensure_collections(self) -> None:
        existing = set(self._collection_names())
        for name in COLLECTIONS:
            if name in existing:
                continue
            response = self.http.post(
                f"{self.db}/collections",
                headers=self._headers(),
                json={"collectionName": name},
            )
            if response.status_code == 409:
                continue
            self._raise(response, f"创建集合 {name} 失败")

    def find_all(self, collection: str, query: dict) -> list[dict]:
        # 文档库分页参数是 offset。skip 会被忽略，于是每一页都是同一批文档。
        def fetch_page(offset: int, page_size: int) -> list[dict]:
            response = self.http.get(
                f"{self.db}/collections/{collection}/documents",
                headers=self._headers(),
                params={
                    "limit": str(page_size),
                    "offset": str(offset),
                    "query": json.dumps(query, ensure_ascii=False),
                },
            )
            self._raise(response, f"读取 {collection} 失败")
            return _unwrap_docs(response.json())

        return collect_pages(fetch_page)

    def insert(self, collection: str, doc: dict) -> str:
        ids = self._insert_chunk(collection, [doc])
        return ids[0] if ids else ""

    def insert_many(self, collection: str, docs: list[dict]) -> None:
        # 文档库对单次批量有上限，按 20 条切开，避免一份长评论把整次导入打失败。
        for start in range(0, len(docs), 20):
            self._insert_chunk(collection, docs[start : start + 20])

    def update_one(self, collection: str, query: dict, fields: dict) -> None:
        self._patch(collection, query, fields)

    def update_many(self, collection: str, query: dict, fields: dict) -> None:
        self._patch(collection, query, fields)

    def remove(self, collection: str, query: dict) -> None:
        if not query or "uid" not in query:
            raise CloudBaseError("删除必须带 uid")
        response = self.http.post(
            f"{self.db}/collections/{collection}/documents/remove",
            headers=self._headers(),
            json={"query": query},
        )
        self._raise(response, f"删除 {collection} 失败")

    def upload_import(self, uid: str, video_id: str, filename: str, content: bytes) -> dict:
        object_id = f"pingjian/imports/{_safe(uid)}/{_safe(video_id)}/{_safe(filename)}"
        response = self.http.post(
            f"{self.base}/v1/storages/get-objects-upload-info",
            headers=self._headers(),
            json=[{"objectId": object_id}],
        )
        self._raise(response, "获取上传地址失败")
        payload = response.json()
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict) or item.get("code"):
            message = item.get("message") if isinstance(item, dict) else "上传地址为空"
            raise CloudBaseError(f"原始文件上传失败：{message}")
        # 只带签名要求的三个头。多加 Content-Type 有时会让 COS 签名对不上。
        put = self.http.put(
            item["uploadUrl"],
            content=content,
            headers={
                "Authorization": item.get("authorization") or "",
                "X-Cos-Security-Token": item.get("token") or "",
                "X-Cos-Meta-Fileid": item.get("cloudObjectMeta") or "",
            },
            timeout=120,
        )
        if put.status_code >= 400:
            raise CloudBaseError(f"原始文件上传失败：{put.status_code}")
        return {
            "objectId": item.get("objectId") or object_id,
            "cloudObjectId": item.get("cloudObjectId") or "",
        }

    def delete_import(self, file_info: dict | None) -> None:
        if not file_info:
            return
        body = []
        if file_info.get("cloudObjectId"):
            body.append({"cloudObjectId": file_info["cloudObjectId"]})
        elif file_info.get("objectId"):
            body.append({"objectId": file_info["objectId"]})
        if not body:
            return
        response = self.http.post(
            f"{self.base}/v1/storages/delete-objects",
            headers=self._headers(),
            json=body,
        )
        # 文件已经不在时，删除视频的数据库记录仍要继续。
        if response.status_code in {404, 400}:
            return
        if response.status_code >= 400:
            text = response.text[:200]
            if "not exist" in text.lower() or "不存在" in text:
                return
            self._raise(response, "删除原始文件失败")

    def _insert_chunk(self, collection: str, docs: list[dict]) -> list[str]:
        # 文档库要求对象包装，裸数组会被网关直接打成 Invalid request body。
        response = self.http.post(
            f"{self.db}/collections/{collection}/documents",
            headers=self._headers(),
            json={"data": docs},
        )
        self._raise(response, f"写入 {collection} 失败")
        return _unwrap_ids(response.json())

    def _patch(self, collection: str, query: dict, fields: dict) -> None:
        response = self.http.patch(
            f"{self.db}/collections/{collection}/documents",
            headers=self._headers(),
            json={"query": query, "data": {"$set": fields}},
        )
        self._raise(response, f"更新 {collection} 失败")

    def _collection_names(self) -> list[str]:
        response = self.http.get(f"{self.db}/collections", headers=self._headers())
        self._raise(response, "读取集合列表失败")
        payload = response.json()
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        names: list[str] = []
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("collections") or []
        if not isinstance(rows, list):
            return names
        for row in rows:
            if isinstance(row, str):
                names.append(row)
            elif isinstance(row, dict):
                names.append(str(row.get("name") or row.get("collectionName") or ""))
        return [name for name in names if name]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _raise(response: httpx.Response, prefix: str) -> None:
        if response.status_code < 400:
            return
        message = response.text[:300]
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("code") or message)
        raise CloudBaseError(f"{prefix}：{message}")


def collect_pages(fetch_page, page_size: int = 100, max_docs: int = 50000) -> list[dict]:
    """按 offset 翻页。如果下一页又回到已经见过的文档，立刻停，避免把同一页重复累加。"""
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while offset < max_docs:
        page = fetch_page(offset, page_size)
        if not page:
            break
        fresh: list[dict] = []
        repeated = False
        for doc in page:
            marker = str(doc.get("_id") or "")
            if not marker:
                marker = json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str)
            if marker in seen:
                repeated = True
                break
            seen.add(marker)
            fresh.append(doc)
        rows.extend(fresh)
        if repeated or len(page) < page_size:
            break
        offset += page_size
    return rows


def _unwrap_docs(payload: Any) -> list[dict]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for key in ("list", "documents", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return []
    if not isinstance(data, list):
        return []
    docs = []
    for item in data:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                continue
        if isinstance(item, dict):
            decoded = _decode_extended(item)
            if isinstance(decoded, dict):
                docs.append(decoded)
    return docs


def _unwrap_ids(payload: Any) -> list[str]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for key in ("insertedIds", "ids", "id"):
            value = data.get(key)
            if isinstance(value, list):
                return [str(item) for item in value]
            if isinstance(value, str):
                return [value]
    return []


def _decode_extended(value: Any) -> Any:
    """文档库按 MongoDB Extended JSON 返回数字和日期，页面不能直接渲染这种对象。"""
    if isinstance(value, list):
        return [_decode_extended(item) for item in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        key, inner = next(iter(value.items()))
        if key == "$oid":
            return str(inner)
        if key in {"$numberInt", "$numberLong"}:
            return _as_int(inner)
        if key in {"$numberDouble", "$numberDecimal"}:
            return _as_number(inner)
        if key == "$date":
            return _as_date(_decode_extended(inner))
        if key == "$undefined":
            return None
        if key == "$timestamp":
            decoded = _decode_extended(inner)
            if isinstance(decoded, dict):
                return _as_int(decoded.get("t") or 0)
            return decoded
    return {item_key: _decode_extended(item) for item_key, item in value.items()}


def _as_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _as_number(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return int(number)
    return number


def _as_date(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
    return value


def _safe(value: str) -> str:
    cleaned = _SAFE_SEGMENT.sub("_", value or "").strip("._")
    return cleaned[:80] or "file"
