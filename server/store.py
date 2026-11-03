"""存储接口。

生产环境写入 CloudBase 的 pingjian_* 集合。测试使用内存实现，规则与线上相同：
查询和删除都要带 uid，换一个用户的视频 ID 读不到别人的数据。
"""

from __future__ import annotations

import threading
import uuid
from typing import Protocol

VIDEOS = "pingjian_videos"
COMMENTS = "pingjian_comments"
TASKS = "pingjian_tasks"
EVENTS = "pingjian_events"
REPORTS = "pingjian_reports"
COLLECTIONS = (VIDEOS, COMMENTS, TASKS, EVENTS, REPORTS)


class MissingConfig(Exception):
    def __init__(self, keys: list[str]):
        self.keys = keys
        super().__init__("缺少 " + "、".join(keys))


class Store(Protocol):
    def find_all(self, collection: str, query: dict) -> list[dict]: ...

    def insert(self, collection: str, doc: dict) -> str: ...

    def insert_many(self, collection: str, docs: list[dict]) -> None: ...

    def update_one(self, collection: str, query: dict, fields: dict) -> None: ...

    def update_many(self, collection: str, query: dict, fields: dict) -> None: ...

    def remove(self, collection: str, query: dict) -> None: ...

    def upload_import(self, uid: str, video_id: str, filename: str, content: bytes) -> dict: ...

    def delete_import(self, file_info: dict | None) -> None: ...


def _match(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        if doc.get(key) != expected:
            return False
    return True


class MemoryStore:
    """测试和本地核对用的同一套集合划分，不代替 CloudBase。"""

    def __init__(self) -> None:
        self.docs: dict[str, list[dict]] = {name: [] for name in COLLECTIONS}
        self.files: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def find_all(self, collection: str, query: dict) -> list[dict]:
        with self._lock:
            return [dict(doc) for doc in self.docs[collection] if _match(doc, query)]

    def insert(self, collection: str, doc: dict) -> str:
        with self._lock:
            stored = dict(doc)
            stored.setdefault("_id", uuid.uuid4().hex)
            self.docs[collection].append(stored)
            return stored["_id"]

    def insert_many(self, collection: str, docs: list[dict]) -> None:
        for doc in docs:
            self.insert(collection, doc)

    def update_one(self, collection: str, query: dict, fields: dict) -> None:
        with self._lock:
            for doc in self.docs[collection]:
                if _match(doc, query):
                    doc.update(fields)
                    return

    def update_many(self, collection: str, query: dict, fields: dict) -> None:
        with self._lock:
            for doc in self.docs[collection]:
                if _match(doc, query):
                    doc.update(fields)

    def remove(self, collection: str, query: dict) -> None:
        if not query or "uid" not in query:
            raise RuntimeError("删除必须带 uid")
        with self._lock:
            self.docs[collection] = [doc for doc in self.docs[collection] if not _match(doc, query)]

    def upload_import(self, uid: str, video_id: str, filename: str, content: bytes) -> dict:
        object_id = f"pingjian/imports/{uid}/{video_id}/{filename}"
        with self._lock:
            self.files[object_id] = content
        return {"objectId": object_id, "cloudObjectId": object_id}

    def delete_import(self, file_info: dict | None) -> None:
        if not file_info:
            return
        object_id = file_info.get("objectId") or file_info.get("cloudObjectId")
        with self._lock:
            self.files.pop(object_id, None)


class UnavailableStore:
    """没配 CloudBase 密钥时仍能启动，真正读写才返回缺哪一项。"""

    def __init__(self, keys: list[str]):
        self.keys = keys

    def _fail(self) -> None:
        raise MissingConfig(self.keys)

    def find_all(self, collection: str, query: dict) -> list[dict]:
        self._fail()
        return []

    def insert(self, collection: str, doc: dict) -> str:
        self._fail()
        return ""

    def insert_many(self, collection: str, docs: list[dict]) -> None:
        self._fail()

    def update_one(self, collection: str, query: dict, fields: dict) -> None:
        self._fail()

    def update_many(self, collection: str, query: dict, fields: dict) -> None:
        self._fail()

    def remove(self, collection: str, query: dict) -> None:
        self._fail()

    def upload_import(self, uid: str, video_id: str, filename: str, content: bytes) -> dict:
        self._fail()
        return {}

    def delete_import(self, file_info: dict | None) -> None:
        self._fail()
