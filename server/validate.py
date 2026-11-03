"""证据与排序。模型只交评论 ID，摘录和分数都由服务端从库里的全文算出来。"""

from __future__ import annotations

import re

from server.labels import LABELS, RESERVED_THEME_NAMES

GROUP_KEY = re.compile(r"^[\u4e00-\u9fff]{2,8}$")
THEME_NAME = re.compile(r"^[\u4e00-\u9fff]{2,8}$")
NOT_ENOUGH = "没有足够的评论形成选题"


def clip(text: object, limit: int) -> str:
    return str(text or "").strip()[:limit]


def sanitize_labels(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    chosen: list[str] = []
    for label in LABELS:
        if label in raw or str(label) in [str(item) for item in raw]:
            chosen.append(label)
    return chosen


def sanitize_group_key(labels: list[str], raw: object) -> str | None:
    """只有需求或问题需要归并短词。其他标签即使模型写了，也丢掉。"""
    if "need" not in labels and "question" not in labels:
        return None
    text = str(raw or "").strip()
    if GROUP_KEY.fullmatch(text):
        return text
    return None


def theme_name_ok(name: str) -> bool:
    text = (name or "").strip()
    return bool(THEME_NAME.fullmatch(text)) and text not in RESERVED_THEME_NAMES


def score_parts(count: int, evidence: float, feasibility: int, cost: int) -> dict:
    """count 和 evidence 是事实，feasibility 和 cost 是模型推断。

    countNorm 把样本条数封顶在 20，避免一条超大主题把分数打满。
    cost 做分母，所以制作成本越高，排序越靠后。cost 至少为 1。
    """
    count_norm = min(max(count, 0), 20) / 20
    evidence = min(max(evidence, 0.0), 1.0)
    score = round(100 * count_norm * evidence * feasibility / cost)
    return {"countNorm": count_norm, "evidence": evidence, "score": int(score)}


def continuous_excerpt(full_text: str, limit: int = 40) -> str | None:
    """摘录必须是全文里连续的一段，最长 40 字。对不上原文就不算证据。"""
    text = re.sub(r"\s+", " ", full_text or "").strip()
    if not text:
        return None
    excerpt = text[:limit]
    if excerpt and excerpt in text:
        return excerpt
    return None


def verify_fact(claim: dict, allowed_ids: set[str], comments_by_id: dict[str, dict]) -> dict:
    """事实引用必须落在该主题、当前用户、当前视频的评论上。

    comments_by_id 在调用前已经按 uid + videoId 滤过。不在里面的 ID 一律降级。
    降级后改成推断，并去掉评论 ID，页面才能显示「已降级」。
    """
    kind = str(claim.get("kind") or "")
    text = clip(claim.get("text"), 200)
    if kind not in {"fact", "inference", "hypothesis"}:
        kind = "inference"
    raw_ids = claim.get("commentIds") if isinstance(claim.get("commentIds"), list) else []
    comment_ids = [str(item) for item in raw_ids if str(item).strip()]
    if kind != "fact":
        return {
            "kind": kind,
            "text": text,
            "commentIds": [],
            "excerpts": [],
            "downgraded": False,
        }
    excerpts = []
    for comment_id in comment_ids:
        comment = comments_by_id.get(comment_id)
        if comment_id not in allowed_ids or comment is None:
            return _downgrade(text)
        excerpt = continuous_excerpt(comment.get("rawText") or comment.get("aiText") or "")
        if not excerpt:
            return _downgrade(text)
        excerpts.append({"commentId": comment_id, "text": excerpt})
    if not excerpts:
        return _downgrade(text)
    return {
        "kind": "fact",
        "text": text,
        "commentIds": [item["commentId"] for item in excerpts],
        "excerpts": excerpts,
        "downgraded": False,
    }


def _downgrade(text: str) -> dict:
    return {
        "kind": "inference",
        "text": text,
        "commentIds": [],
        "excerpts": [],
        "downgraded": True,
    }


def coerce_scale(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= number <= 5:
        return number
    return None
