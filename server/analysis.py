"""六个分析工具。

DSH 只负责按顺序各调用一次。分批请求、计数、证据校验都在这里完成。
报告只采用 commit_report 校验后的结构，不采用编排代理的自由文本。
"""

from __future__ import annotations

from typing import Callable

from server.clean import is_low_information
from server.config import PROMPT_VERSION, TOOL_ORDER
from server.labels import LABEL_NAMES, LABELS
from server.prompts import (
    CLASSIFY_SYSTEM,
    CLUSTER_SYSTEM,
    DRAFT_SYSTEM,
    POSITIONING_SYSTEM,
    RANK_SYSTEM,
    RECLASSIFY_SYSTEM,
)
from server.store import COMMENTS, EVENTS, REPORTS, TASKS, VIDEOS
from server.validate import (
    clip,
    coerce_scale,
    continuous_excerpt,
    sanitize_group_key,
    sanitize_labels,
    score_parts,
    theme_name_ok,
    verify_fact,
)

Emit = Callable[[dict, str, str, str], None]
BATCH_SIZE = 40
CLUSTER_SAMPLE = 60
POSITION_SAMPLE = 30
MIN_VALID = 20
RECLASSIFY_MIN_UNLABELED = 10
RECLASSIFY_MIN_RATIO = 0.5
RECLASSIFY_CAP = 80
RECLASSIFY_SAMPLE = 12
SKIPPED_TOPICS = "没有足够的评论形成选题，已跳过排序和选题"


class StageError(Exception):
    pass


class AnalysisCancelled(Exception):
    """用户取消后，当前批次可以停下，但不能把取消写成另一条失败原因。"""


def execute_tool(store, model, task_id: str, name: str, emit: Emit) -> str:
    """跑一个工具。失败只写 failed，不写 stage_completed，页面才不会把失败显示成完成。"""
    if name not in TOOL_ORDER:
        raise StageError(f"未知工具 {name}")
    task = _task(store, task_id)
    _checkpoint(store, task)
    cached = (task.get("stages") or {}).get(name) or {}
    if cached.get("ok"):
        return str(cached.get("summary") or "")
    _require_previous(task, name)
    emit(task, "stage_started", name, _start_message(name))
    try:
        summary, data = HANDLERS[name](store, model, task, emit)
    except AnalysisCancelled:
        raise
    except Exception as exc:
        message = str(exc) or "这一步失败"
        emit(task, "failed", name, message)
        _save_task(store, task, {"status": "failed", "error": message})
        raise
    _checkpoint(store, task)
    stages = dict((_task(store, task_id).get("stages") or {}))
    stages[name] = {"ok": True, "summary": summary, "data": data}
    progress = {"stage": name, "done": 1, "total": 1}
    _save_task(store, task, {"stages": stages, "progress": progress, "error": None})
    emit(task, "stage_completed", name, summary)
    emit(task, "summary", name, summary)
    return summary


def analysis_block_reason(stats: dict | None) -> str | None:
    """有效评论不足时，定位、分类和后面的步骤都不要开始。"""
    stats = stats or {}
    valid = int(stats.get("valid") or 0)
    if valid >= MIN_VALID:
        return None
    return (
        f"有效评论 {valid} 条，少于 {MIN_VALID} 条，未开始分析。"
        f"原始记录 {int(stats.get('rawRows') or 0)}，"
        f"空内容 {int(stats.get('droppedEmpty') or 0)}，"
        f"非一级 {int(stats.get('droppedNotLevel1') or 0)}，"
        f"缺少评论 ID {int(stats.get('missingCommentId') or 0)}，"
        f"重复 {int(stats.get('duplicate') or 0)}，"
        f"低信息量 {int(stats.get('lowInformation') or 0)}，"
        f"有效 {valid}。"
        f"有效评论不足 {MIN_VALID} 条时，分类和选题不稳定，因此不会调用模型。"
    )


def inspect_import(store, model, task: dict, emit: Emit) -> tuple[str, dict]:
    video = _video(store, task)
    reason = analysis_block_reason(video.get("cleanStats"))
    if reason:
        raise StageError(reason)
    comments = _comments(store, task)
    substantial = [item for item in comments if not comment_is_low(item)]
    low = len(comments) - len(substantial)
    summary = f"已导入 {len(comments)} 条评论，其中 {len(substantial)} 条会送给模型，{low} 条低信息量不参与分析"
    return summary, {"substantial": len(substantial), "lowInformation": low}


def classify_comments(store, model, task: dict, emit: Emit) -> tuple[str, dict]:
    _checkpoint(store, task)
    video = _video(store, task)
    comments = _comments(store, task)
    substantial = [item for item in comments if not comment_is_low(item)]
    substantial.sort(key=lambda item: _order_index(item))
    positioning = _position(store, model, task, video, substantial)
    _save_task(store, task, {"progress": {"stage": "classify_comments", "done": 0, "total": len(substantial)}})
    for start in range(0, len(substantial), BATCH_SIZE):
        _checkpoint(store, task)
        batch = substantial[start : start + BATCH_SIZE]
        payload = _complete(
            store,
            model,
            task,
            stage="classify",
            system=CLASSIFY_SYSTEM,
            user={"positioning": positioning, "comments": [_model_comment(item) for item in batch]},
            required_ids=[item["commentId"] for item in batch],
            items_key="items",
            id_field="id",
            max_tokens=4096,
        )
        by_id = {}
        for row in payload.get("items") or []:
            if isinstance(row, dict) and row.get("id") is not None:
                by_id[str(row["id"])] = row
        for comment in batch:
            row = by_id.get(comment["commentId"]) or {}
            labels = sanitize_labels(row.get("labels"))
            group_key = sanitize_group_key(labels, row.get("groupKey"))
            store.update_many(
                COMMENTS,
                {"uid": task["uid"], "videoId": task["videoId"], "commentId": comment["commentId"]},
                {"labels": labels, "groupKey": group_key, "themeId": None, "themeName": None},
            )
        done = min(start + len(batch), len(substantial))
        _save_task(store, task, {"progress": {"stage": "classify_comments", "done": done, "total": len(substantial)}})
        emit(task, "summary", "classify_comments", f"已分类 {done}/{len(substantial)} 条")
    summary = _classify_summary(_comments(store, task))
    return summary, {"positioning": positioning}


def cluster_topics(store, model, task: dict, emit: Emit) -> tuple[str, dict]:
    comments = _comments(store, task)
    themes: list[dict] = []
    for label in LABELS:
        members = [item for item in comments if label in (item.get("labels") or [])]
        if len(members) < 2:
            continue
        sample = sorted(members, key=_like_rank, reverse=True)[:CLUSTER_SAMPLE]
        payload = _complete(
            store,
            model,
            task,
            stage="cluster",
            system=CLUSTER_SYSTEM,
            user={"label": label, "labelName": LABEL_NAMES[label], "comments": [_model_comment(item) for item in sample]},
            required_ids=[item["commentId"] for item in sample],
            items_key="assignments",
            id_field="id",
            max_tokens=2500,
        )
        label_summary, grouped = _accept_themes(payload, sample)
        for index, grouped_theme in enumerate(grouped, start=1):
            theme_id = f"th_{label}_{index}"
            excerpts = _excerpts_for(grouped_theme["commentIds"], comments)
            evidence = len(excerpts) / len(grouped_theme["commentIds"])
            theme = {
                "themeId": theme_id,
                "label": label,
                "name": grouped_theme["name"],
                "summary": grouped_theme["summary"],
                "labelSummary": label_summary,
                "sampleCount": len(grouped_theme["commentIds"]),
                "labelTotal": len(members),
                "likeSum": _like_sum(grouped_theme["commentIds"], comments),
                "commentIds": grouped_theme["commentIds"],
                "excerpts": excerpts[:8],
                "evidence": evidence,
            }
            themes.append(theme)
            id_set = set(grouped_theme["commentIds"])
            for comment in sample:
                if comment["commentId"] not in id_set:
                    continue
                store.update_many(
                    COMMENTS,
                    {"uid": task["uid"], "videoId": task["videoId"], "commentId": comment["commentId"]},
                    {"themeId": theme_id, "themeName": grouped_theme["name"]},
                )
        emit(task, "summary", "cluster_topics", f"{LABEL_NAMES[label]} 形成 {len(grouped)} 个主题，全量 {len(members)} 条")
    for extra in _stage_data(store, task["taskId"], "reclassify_other").get("themes") or []:
        if isinstance(extra, dict) and extra.get("themeId"):
            themes.append(extra)
    if not themes:
        return SKIPPED_TOPICS, {"themes": [], "skipped": True, "skipReason": SKIPPED_TOPICS}
    return f"聚类完成，{len(themes)} 个主题", {"themes": themes, "skipped": False}


def rank_opportunities(store, model, task: dict, emit: Emit) -> tuple[str, dict]:
    clustered = _stage_data(store, task["taskId"], "cluster_topics")
    if clustered.get("skipped") or not clustered.get("themes"):
        reason = str(clustered.get("skipReason") or SKIPPED_TOPICS)
        return reason, {"themes": [], "topIds": [], "skipped": True, "skipReason": reason}
    themes = list(clustered.get("themes") or [])
    payload = _complete(
        store,
        model,
        task,
        stage="rank",
        system=RANK_SYSTEM,
        user={
            "themes": [
                {
                    "id": theme["themeId"],
                    "label": theme["label"],
                    "name": theme["name"],
                    "summary": theme["summary"],
                    "count": theme["sampleCount"],
                    "likeSum": theme["likeSum"],
                }
                for theme in themes
            ]
        },
        required_ids=[theme["themeId"] for theme in themes],
        items_key="items",
        id_field="id",
        max_tokens=2000,
    )
    scored_rows = {}
    for row in payload.get("items") or []:
        if isinstance(row, dict) and row.get("id") is not None:
            scored_rows[str(row["id"])] = row
    scored: list[dict] = []
    for theme in themes:
        row = scored_rows.get(theme["themeId"]) or {}
        feasibility = coerce_scale(row.get("feasibility"))
        cost = coerce_scale(row.get("cost"))
        if feasibility is None or cost is None:
            raise StageError(f"模型调用失败，停在 rank，主题 {theme['themeId']} 的可行性或成本不在 1 到 5")
        parts = score_parts(theme["sampleCount"], float(theme["evidence"]), feasibility, cost)
        scored.append(
            {
                **theme,
                "feasibility": feasibility,
                "cost": cost,
                "reason": clip(row.get("reason"), 30),
                **parts,
            }
        )
    scored.sort(key=lambda item: (item["score"], item["likeSum"], item["themeId"]), reverse=True)
    top_ids = [item["themeId"] for item in scored[:8]]
    return f"已按分数排序，前 {len(top_ids)} 个进入选题", {"themes": scored, "topIds": top_ids}


def draft_actions(store, model, task: dict, emit: Emit) -> tuple[str, dict]:
    ranked = _stage_data(store, task["taskId"], "rank_opportunities")
    if ranked.get("skipped") or not ranked.get("themes"):
        reason = str(ranked.get("skipReason") or SKIPPED_TOPICS)
        return reason, {"items": [], "themes": [], "skipped": True, "skipReason": reason}
    by_id = {theme["themeId"]: theme for theme in ranked.get("themes") or []}
    chosen = [by_id[theme_id] for theme_id in ranked.get("topIds") or [] if theme_id in by_id]
    if not chosen:
        reason = SKIPPED_TOPICS
        return reason, {"items": [], "themes": [], "skipped": True, "skipReason": reason}
    user_themes = []
    for theme in chosen:
        given = _given_ids(theme)
        theme["givenCommentIds"] = given
        user_themes.append(
            {
                "themeId": theme["themeId"],
                "label": theme["label"],
                "name": theme["name"],
                "summary": theme["summary"],
                "count": theme["sampleCount"],
                "likeSum": theme["likeSum"],
                "commentIds": given,
            }
        )
    payload = _complete(
        store,
        model,
        task,
        stage="draft",
        system=DRAFT_SYSTEM,
        user={"themes": user_themes},
        required_ids=[theme["themeId"] for theme in chosen],
        items_key="items",
        id_field="themeId",
        max_tokens=4000,
    )
    return f"已生成 {len(chosen)} 条选题", {"items": payload.get("items") or [], "themes": chosen}


def commit_report(store, model, task: dict, emit: Emit) -> tuple[str, dict]:
    """不调用模型。事实对不上原文或评论 ID 时降级。没有主题时仍提交定位和分类。"""
    draft = _stage_data(store, task["taskId"], "draft_actions")
    classify = _stage_data(store, task["taskId"], "classify_comments")
    comments = _comments(store, task)
    if draft.get("skipped") or not draft.get("themes"):
        reason = str(draft.get("skipReason") or SKIPPED_TOPICS)
        _store_report(store, task, classify, comments, [], [], reason)
        return f"报告已提交。{reason}", {"downgraded": 0}
    themes = draft.get("themes") or []
    by_comment = {item["commentId"]: item for item in comments}
    drafts = {}
    for item in draft.get("items") or []:
        if isinstance(item, dict) and item.get("themeId") is not None:
            drafts[str(item["themeId"])] = item
    opportunities = []
    for rank, theme in enumerate(themes, start=1):
        item = drafts.get(theme["themeId"]) or {}
        allowed = set(theme.get("givenCommentIds") or [])
        claims = []
        raw_claims = item.get("claims") if isinstance(item.get("claims"), list) else []
        for claim in raw_claims[:4]:
            if isinstance(claim, dict):
                claims.append(verify_fact(claim, allowed, by_comment))
        structure = item.get("structure") if isinstance(item.get("structure"), list) else []
        opportunities.append(
            {
                "themeId": theme["themeId"],
                "topic": clip(item.get("topic"), 30),
                "structure": [clip(part, 20) for part in structure[:5]],
                "targetNeed": clip(item.get("targetNeed"), 40),
                "validation": clip(item.get("validation"), 40),
                "favorite": False,
                "score": theme["score"],
                "rank": rank,
                "feasibility": theme["feasibility"],
                "cost": theme["cost"],
                "reason": theme["reason"],
                "sampleCount": theme["sampleCount"],
                "labelTotal": theme["labelTotal"],
                "likeSum": theme["likeSum"],
                "evidence": theme["evidence"],
                "commentIds": list(theme["commentIds"]),
                "excerpts": theme["excerpts"],
                "claims": claims,
            }
        )
    ranked_themes = _stage_data(store, task["taskId"], "rank_opportunities").get("themes") or themes
    _store_report(store, task, classify, comments, ranked_themes, opportunities, None)
    downgraded = sum(1 for card in opportunities for claim in card["claims"] if claim.get("downgraded"))
    summary = f"报告已提交，{len(opportunities)} 条选题，{downgraded} 条事实已降级"
    return summary, {"downgraded": downgraded}


def execute_reclassify(store, model, task_id: str, emit: Emit) -> str:
    """分类之后的可选一轮。条件不够、重复调用或聚类已开始时不请求模型，也不把任务打成失败。"""
    task = _task(store, task_id)
    _checkpoint(store, task)
    stages = task.get("stages") or {}
    if not (stages.get("classify_comments") or {}).get("ok"):
        raise StageError("reclassify_other 不能在 classify_comments 完成前调用")
    refusal = _reclassify_refusal(store, task, stages)
    if refusal:
        emit(task, "summary", "reclassify_other", refusal)
        return refusal
    emit(task, "stage_started", "reclassify_other", "开始补充分类")
    try:
        summary, data = _reclassify(store, model, task, emit)
    except AnalysisCancelled:
        raise
    except Exception as exc:
        message = str(exc) or "这一步失败"
        emit(task, "failed", "reclassify_other", message)
        _save_task(store, task, {"status": "failed", "error": message})
        raise
    stages = dict((_task(store, task_id).get("stages") or {}))
    stages["reclassify_other"] = {"ok": True, "summary": summary, "data": data}
    _save_task(
        store,
        task,
        {"stages": stages, "progress": {"stage": "reclassify_other", "done": 1, "total": 1}, "error": None},
    )
    emit(task, "stage_completed", "reclassify_other", summary)
    emit(task, "summary", "reclassify_other", summary)
    return summary


HANDLERS = {
    "inspect_import": inspect_import,
    "classify_comments": classify_comments,
    "cluster_topics": cluster_topics,
    "rank_opportunities": rank_opportunities,
    "draft_actions": draft_actions,
    "commit_report": commit_report,
}


def reset_analysis(store, task: dict) -> None:
    """重试和进程恢复都回到同一批已导入评论，清掉上一次的标签、报告和事件。"""
    blank = {"labels": [], "groupKey": None, "themeId": None, "themeName": None}
    store.update_many(COMMENTS, {"uid": task["uid"], "videoId": task["videoId"]}, blank)
    store.remove(REPORTS, {"uid": task["uid"], "videoId": task["videoId"]})
    store.remove(EVENTS, {"uid": task["uid"], "videoId": task["videoId"]})


def public_comment(comment: dict, cited: set[str]) -> dict:
    return {
        "commentId": comment["commentId"],
        "text": comment.get("rawText") or comment.get("aiText") or "",
        "likeCount": comment.get("likeCount") or 0,
        "createdAt": comment.get("createdAt") or "",
        "labels": comment.get("labels") or [],
        "themeId": comment.get("themeId"),
        "themeName": comment.get("themeName"),
        "lowInformation": comment_is_low(comment),
        "cited": comment["commentId"] in cited,
    }


def cited_ids(report: dict | None) -> set[str]:
    found: set[str] = set()
    if not report:
        return found
    for card in report.get("opportunities") or []:
        for claim in card.get("claims") or []:
            if claim.get("kind") == "fact" and not claim.get("downgraded"):
                found.update(str(item) for item in claim.get("commentIds") or [])
    return found


def _position(store, model, task: dict, video: dict, substantial: list[dict]) -> dict:
    sample = sorted(substantial, key=_like_rank, reverse=True)[:POSITION_SAMPLE]
    user = {
        "title": video.get("title") or "",
        "hashtags": video.get("hashtags") or [],
        "topComments": [_model_comment(item) for item in sample],
    }
    if video.get("description"):
        user["description"] = video["description"]
    payload = _complete(
        store,
        model,
        task,
        stage="positioning",
        system=POSITIONING_SYSTEM,
        user=user,
        required_ids=None,
        items_key="items",
        id_field="id",
        max_tokens=800,
    )
    notes = payload.get("boundaryNotes") if isinstance(payload.get("boundaryNotes"), list) else []
    return {
        "vertical": clip(payload.get("vertical"), 12),
        "format": clip(payload.get("format"), 12),
        "topic": clip(payload.get("topic"), 40),
        "boundaryNotes": [clip(note, 40) for note in notes[:4]],
    }


def _accept_themes(payload: dict, sample: list[dict]) -> tuple[str, list[dict]]:
    summary = clip(payload.get("summary"), 40)
    summaries: dict[str, str] = {}
    order: list[str] = []
    for item in payload.get("themes") or []:
        if not isinstance(item, dict) or len(order) == 5:
            continue
        name = str(item.get("name") or "").strip()
        if not theme_name_ok(name) or name in summaries:
            continue
        summaries[name] = clip(item.get("summary"), 30)
        order.append(name)
    allowed = {item["commentId"] for item in sample}
    assigned: dict[str, str] = {}
    for row in payload.get("assignments") or []:
        if not isinstance(row, dict):
            continue
        comment_id = str(row.get("id") or "")
        if comment_id not in allowed or comment_id in assigned:
            continue
        theme_name = str(row.get("theme") or "").strip()
        assigned[comment_id] = theme_name if theme_name in summaries else ""
    groups = {name: [] for name in order}
    for comment_id, theme_name in assigned.items():
        if theme_name:
            groups[theme_name].append(comment_id)
    # 不足 2 条的主题不建立。被丢掉的评论不再挂主题。
    kept = [name for name in order if len(groups[name]) >= 2]
    ranked = sorted(sample, key=_like_rank, reverse=True)
    themes = []
    for name in kept:
        id_set = set(groups[name])
        themes.append(
            {
                "name": name,
                "summary": summaries[name],
                "commentIds": [item["commentId"] for item in ranked if item["commentId"] in id_set],
            }
        )
    return summary, themes


def _model_comment(comment: dict) -> dict:
    # 只给这三项。昵称留在库里，不进模型，避免模型把称呼写进事实。
    likes = comment["likeCount"] if comment.get("likeComparable", True) else 0
    return {"id": comment["commentId"], "likes": likes, "text": (comment.get("aiText") or "")[:180]}


def _like_rank(comment: dict) -> tuple:
    comparable = 1 if comment.get("likeComparable", True) else 0
    likes = comment.get("likeCount") or 0 if comment.get("likeComparable", True) else -1
    return (comparable, likes, -int(comment.get("orderIndex") or 0))


def _like_sum(comment_ids: list[str], comments: list[dict]) -> int:
    by_id = {item["commentId"]: item for item in comments}
    total = 0
    for comment_id in comment_ids:
        comment = by_id.get(comment_id) or {}
        if comment.get("likeComparable", True):
            total += int(comment.get("likeCount") or 0)
    return total


def _excerpts_for(comment_ids: list[str], comments: list[dict]) -> list[dict]:
    by_id = {item["commentId"]: item for item in comments}
    excerpts = []
    for comment_id in comment_ids:
        comment = by_id.get(comment_id)
        if not comment:
            continue
        text = continuous_excerpt(comment.get("rawText") or "")
        if text:
            excerpts.append({"commentId": comment_id, "text": text})
    return excerpts


def _given_ids(theme: dict) -> list[str]:
    excerpt_ids = [item["commentId"] for item in theme.get("excerpts") or []]
    if excerpt_ids:
        return excerpt_ids[:3]
    return list(theme.get("commentIds") or [])[:3]


def stored_labels(comment: dict) -> list[str]:
    return [label for label in (comment.get("labels") or []) if label in LABELS]


def comment_is_low(comment: dict) -> bool:
    """低信息量既看导入时的标记，也看正文。标记没读回来时，短评论文本仍能对上。"""
    flag = comment.get("lowInformation")
    if isinstance(flag, str):
        if flag.strip().lower() in {"1", "true", "yes", "是"}:
            return True
        if flag.strip().lower() in {"0", "false", "no", "否"}:
            flag = False
    if flag:
        return True
    text = comment.get("aiText") or ""
    return bool(text) and is_low_information(text)


def _label_counts(comments: list[dict]) -> tuple[dict, int, int, int]:
    counts = {label: 0 for label in LABELS}
    unlabeled = 0
    low = 0
    for comment in comments:
        if comment_is_low(comment):
            low += 1
            continue
        labels = stored_labels(comment)
        if not labels:
            unlabeled += 1
            continue
        for label in labels:
            counts[label] += 1
    return counts, unlabeled, low, unlabeled + low


def _store_report(
    store,
    task: dict,
    classify: dict,
    comments: list[dict],
    themes: list[dict],
    opportunities: list[dict],
    analysis_limit: str | None,
) -> None:
    counts, unlabeled, low, unclassified = _label_counts(comments)
    report = {
        "uid": task["uid"],
        "videoId": task["videoId"],
        "taskId": task["taskId"],
        "promptVersion": PROMPT_VERSION,
        "model": task.get("model"),
        "positioning": classify.get("positioning"),
        "labelCounts": counts,
        "unlabeled": unlabeled,
        "lowInformation": low,
        "unclassified": unclassified,
        "analysisLimit": analysis_limit,
        "themeCount": len(themes),
        "themes": themes,
        "opportunities": opportunities,
    }
    store.remove(REPORTS, {"uid": task["uid"], "videoId": task["videoId"]})
    store.insert(REPORTS, report)


def _classify_summary(comments: list[dict]) -> str:
    substantial, unlabeled = _unlabeled_substantial(comments)
    labeled = len(substantial) - len(unlabeled)
    low = sum(1 for item in comments if comment_is_low(item))
    ratio = (len(unlabeled) / len(substantial)) if substantial else 0
    verdict = "可以考虑补分类" if _reclassify_allowed(len(substantial), len(unlabeled)) else "不要补分类"
    text = (
        f"分类完成，{labeled} 条打上了标签。"
        f"未打标签 {len(unlabeled)} 条，占有效评论 {round(ratio * 100)}%。"
        f"低信息量 {low} 条。{verdict}。"
    )
    if verdict != "可以考虑补分类":
        return text
    samples = sorted(unlabeled, key=_like_rank, reverse=True)[:RECLASSIFY_SAMPLE]
    shown = "；".join(
        text
        for text in (clip(item.get("aiText") or item.get("rawText") or "", 24) for item in samples)
        if text
    )
    if shown:
        text += f"样本：{shown}"
    return text


def _unlabeled_substantial(comments: list[dict]) -> tuple[list[dict], list[dict]]:
    substantial = [item for item in comments if not comment_is_low(item)]
    unlabeled = [item for item in substantial if not stored_labels(item)]
    return substantial, unlabeled


def _reclassify_allowed(substantial_count: int, unlabeled_count: int) -> bool:
    if substantial_count <= 0:
        return False
    return unlabeled_count >= RECLASSIFY_MIN_UNLABELED and unlabeled_count / substantial_count >= RECLASSIFY_MIN_RATIO


def _reclassify_refusal(store, task: dict, stages: dict) -> str | None:
    if stages.get("cluster_topics"):
        return "聚类已经开始，不再补分类，未调用模型"
    if (stages.get("reclassify_other") or {}).get("ok"):
        return "已经补分类过，不再调用模型"
    substantial, unlabeled = _unlabeled_substantial(_comments(store, task))
    if not _reclassify_allowed(len(substantial), len(unlabeled)):
        return "未达到补分类条件，未调用模型"
    return None


def _accept_other_categories(store, task: dict, payload: dict, sample: list[dict], comments: list[dict]) -> list[dict]:
    """补分类自己命名，最多留下 3 个、每个至少 2 条的类别。不写回四个预设标签。"""
    summaries: dict[str, str] = {}
    order: list[str] = []
    for item in payload.get("categories") or []:
        if not isinstance(item, dict) or len(order) == 3:
            continue
        name = str(item.get("name") or "").strip()
        if not theme_name_ok(name) or name in summaries:
            continue
        summaries[name] = clip(item.get("summary"), 30)
        order.append(name)
    allowed = {item["commentId"] for item in sample}
    assigned: dict[str, str] = {}
    for row in payload.get("assignments") or []:
        if not isinstance(row, dict):
            continue
        comment_id = str(row.get("id") or "")
        if comment_id not in allowed or comment_id in assigned:
            continue
        category = str(row.get("category") or "").strip()
        assigned[comment_id] = category if category in summaries else ""
    groups = {name: [] for name in order}
    for comment_id, category in assigned.items():
        if category:
            groups[category].append(comment_id)
    kept = [name for name in order if len(groups[name]) >= 2]
    ranked = sorted(sample, key=_like_rank, reverse=True)
    themes = []
    kept_ids: set[str] = set()
    for index, name in enumerate(kept, start=1):
        id_set = set(groups[name])
        kept_ids.update(id_set)
        comment_ids = [item["commentId"] for item in ranked if item["commentId"] in id_set]
        excerpts = _excerpts_for(comment_ids, comments)
        theme_id = f"th_other_{index}"
        themes.append(
            {
                "themeId": theme_id,
                "label": "other",
                "name": name,
                "summary": summaries[name],
                "labelSummary": summaries[name],
                "sampleCount": len(comment_ids),
                "labelTotal": len(comment_ids),
                "likeSum": _like_sum(comment_ids, comments),
                "commentIds": comment_ids,
                "excerpts": excerpts[:8],
                "evidence": (len(excerpts) / len(comment_ids)) if comment_ids else 0,
            }
        )
        for comment_id in comment_ids:
            store.update_many(
                COMMENTS,
                {"uid": task["uid"], "videoId": task["videoId"], "commentId": comment_id},
                {"themeId": theme_id, "themeName": name},
            )
    for comment in sample:
        if comment["commentId"] in kept_ids:
            continue
        store.update_many(
            COMMENTS,
            {"uid": task["uid"], "videoId": task["videoId"], "commentId": comment["commentId"]},
            {"themeId": None, "themeName": None},
        )
    return themes


def _reclassify(store, model, task: dict, emit: Emit) -> tuple[str, dict]:
    comments = _comments(store, task)
    _, unlabeled = _unlabeled_substantial(comments)
    chosen = sorted(unlabeled, key=_like_rank, reverse=True)[:RECLASSIFY_CAP]
    positioning = _stage_data(store, task["taskId"], "classify_comments").get("positioning") or {}
    payload = _complete(
        store,
        model,
        task,
        stage="reclassify",
        system=RECLASSIFY_SYSTEM,
        user={"positioning": positioning, "comments": [_model_comment(item) for item in chosen]},
        required_ids=[item["commentId"] for item in chosen],
        items_key="assignments",
        id_field="id",
        max_tokens=4096,
    )
    themes = _accept_other_categories(store, task, payload, chosen, comments)
    names = "、".join(theme["name"] for theme in themes) or "没有形成类别"
    summary = f"补分类完成，{len(themes)} 个类别：{names}"
    return summary, {"sent": len(chosen), "themes": themes}


def dedupe_comments(comments: list[dict]) -> list[dict]:
    """同一条评论如果被存了两份，只留一份。进度分母用这个数，才能和有效评论对上。"""
    best: dict[str, dict] = {}
    for item in comments:
        comment_id = str(item.get("commentId") or "")
        if not comment_id:
            continue
        current = best.get(comment_id)
        if current is None or _comment_richness(item) > _comment_richness(current):
            best[comment_id] = item
    rows = list(best.values())
    rows.sort(key=lambda item: (_order_index(item), str(item.get("commentId"))))
    return rows


def _comments(store, task: dict) -> list[dict]:
    rows = store.find_all(COMMENTS, {"uid": task["uid"], "videoId": task["videoId"]})
    return dedupe_comments(rows)


def _complete(store, model, task: dict, **kwargs):
    _checkpoint(store, task)
    return model.complete_json(**kwargs)


def _checkpoint(store, task: dict) -> None:
    current = _task(store, task["taskId"])
    task.clear()
    task.update(current)
    if current.get("cancelRequested") or current.get("status") not in {"queued", "started"}:
        raise AnalysisCancelled(str(current.get("error") or "已取消分析"))


def _order_index(item: dict) -> int:
    try:
        return int(item.get("orderIndex") or 0)
    except (TypeError, ValueError):
        return 0


def _comment_richness(item: dict) -> tuple:
    labels = item.get("labels") or []
    return (
        1 if labels else 0,
        1 if item.get("themeId") else 0,
        1 if item.get("groupKey") else 0,
    )


def _video(store, task: dict) -> dict:
    rows = store.find_all(VIDEOS, {"uid": task["uid"], "videoId": task["videoId"]})
    if not rows:
        raise StageError("找不到这个视频的导入记录")
    return rows[0]


def _task(store, task_id: str) -> dict:
    rows = store.find_all(TASKS, {"taskId": task_id})
    if not rows:
        raise StageError("找不到分析任务")
    return rows[0]


def _stage_data(store, task_id: str, name: str) -> dict:
    task = _task(store, task_id)
    stage = (task.get("stages") or {}).get(name) or {}
    data = stage.get("data") or {}
    return data if isinstance(data, dict) else {}


def _save_task(store, task: dict, fields: dict) -> None:
    from datetime import datetime, timezone

    fields = {**fields, "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    store.update_one(TASKS, {"uid": task["uid"], "taskId": task["taskId"]}, fields)
    task.update(fields)


def _require_previous(task: dict, name: str) -> None:
    index = TOOL_ORDER.index(name)
    stages = task.get("stages") or {}
    for previous in TOOL_ORDER[:index]:
        if not (stages.get(previous) or {}).get("ok"):
            raise StageError(f"{name} 不能在 {previous} 完成前调用")


def _start_message(name: str) -> str:
    return {
        "inspect_import": "开始核对导入结果",
        "classify_comments": "开始定位并分类",
        "cluster_topics": "开始聚类高赞样本",
        "rank_opportunities": "开始计算排序",
        "draft_actions": "开始写选题",
        "commit_report": "开始校验并提交报告",
    }[name]
