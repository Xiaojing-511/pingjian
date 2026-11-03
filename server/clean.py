"""CSV / XLSX 清洗。

提交时只认这里的结果。浏览器里的预览也调用同一套函数，避免两套规则走偏。
一条评论能不能留下来，按固定顺序只记一个原因：空内容、非一级、缺少评论 ID、重复。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Iterable

from openpyxl import load_workbook

# 样例导出正好是这 19 列。别名用来对付手动改过表头的文件。
# 视频描述不在这 19 列里，但分析定位会用到，所以额外识别。
FIELDS: list[tuple[str, str, tuple[str, ...]]] = [
    ("sourceVideoId", "视频ID", ("作品ID", "aweme_id", "video_id")),
    ("videoTitle", "视频标题", ("标题", "title")),
    ("videoUrl", "视频链接", ("视频地址", "作品链接", "分享链接", "video_url", "videoUrl")),
    ("videoAuthor", "视频作者", ("作者", "达人", "author")),
    ("videoPublishTime", "视频发布时间", ("发布时间",)),
    ("commentId", "评论ID", ("评论id", "评论编号", "comment_id", "cid")),
    ("rootCommentId", "根评论ID", ("根评论id", "root_comment_id")),
    ("parentId", "父评论ID", ("父评论id", "父级评论ID", "回复评论ID", "parent_id")),
    ("level", "层级", ("评论层级", "level")),
    ("content", "评论内容", ("内容", "评论", "content")),
    ("commentTime", "评论时间", ("时间", "comment_time")),
    ("likes", "点赞数", ("点赞", "赞", "likes")),
    ("replies", "回复数", ("回复", "replies")),
    ("isPinned", "是否置顶", ("置顶",)),
    ("nickname", "用户昵称", ("评论昵称", "昵称", "nickname")),
    ("isAuthor", "是否视频作者", ("是否作者",)),
    ("replyToNickname", "回复对象昵称", ("回复昵称", "被回复用户")),
    ("collectedAt", "采集时间", ("抓取时间",)),
    ("douyinId", "抖音号", ("作者抖音号", "抖音ID")),
    ("videoDescription", "视频描述", ("描述", "文案", "视频文案")),
]

REQUIRED = ("videoUrl", "commentId", "content")
FIELD_LABELS = {key: label for key, label, _aliases in FIELDS}

BRACKET_EMOJI = re.compile(r"\[[^\[\]\n]{1,16}\]")
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
HASHTAG = re.compile(r"#\s*([^#\s]+)")
GENERIC_REPLIES = {
    "同问",
    "好的",
    "收到",
    "知道了",
    "来了",
    "看看",
    "路过",
    "已关",
    "回复了",
    "回复您了",
}


class CleanError(Exception):
    def __init__(self, message: str, stats: dict | None = None):
        super().__init__(message)
        self.stats = stats or {}


@dataclass
class CleanResult:
    video_id: str
    title: str
    author: str
    video_url: str
    publish_time: str
    description: str
    hashtags: list[str]
    stats: dict
    comments: list[dict] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)


def suggest_mapping(headers: Iterable[str]) -> dict[str, str]:
    """先按正式列名对上，再用别名补空位。一个文件列只分配一次。"""
    remaining = [header.strip() for header in headers if header and header.strip()]
    mapping = {key: "" for key, _label, _aliases in FIELDS}
    for key, label, _aliases in FIELDS:
        if label in remaining:
            mapping[key] = label
            remaining.remove(label)
    for key, _label, aliases in FIELDS:
        if mapping[key]:
            continue
        alias_set = {alias.casefold() for alias in aliases}
        for header in list(remaining):
            if header.casefold() in alias_set:
                mapping[key] = header
                remaining.remove(header)
                break
    return mapping


def normalize_mapping(headers: list[str], mapping: dict | None) -> dict[str, str]:
    """手动映射覆盖自动识别。传空字符串表示这一列不使用。"""
    suggested = suggest_mapping(headers)
    if not mapping:
        return suggested
    header_set = set(headers)
    merged = dict(suggested)
    for key, _label, _aliases in FIELDS:
        if key not in mapping:
            continue
        chosen = str(mapping.get(key) or "").strip()
        if chosen and chosen not in header_set:
            raise CleanError(f"映射列不存在：{chosen}")
        merged[key] = chosen
    return merged


def read_table(filename: str, content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    # 样例文件后缀是 .csv，文件头却是 xlsx 的 PK。以内容而不是后缀决定解析器。
    if content[:2] == b"PK" or filename.lower().endswith((".xlsx", ".xlsm")):
        rows = _read_xlsx(content)
    else:
        rows = _read_csv(content)
    if not rows:
        raise CleanError("文件里没有评论")
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    return headers, rows


def parse_import(filename: str, content: bytes, mapping: dict | None = None) -> CleanResult:
    headers, rows = read_table(filename, content)
    chosen = normalize_mapping(headers, mapping)
    missing = [FIELD_LABELS[key] for key in REQUIRED if not chosen.get(key)]
    if missing:
        raise CleanError("缺少字段：" + "、".join(missing))
    mapped = [_apply_mapping(row, chosen) for row in rows]
    return clean_rows(mapped)


def clean_rows(rows: list[dict[str, str]]) -> CleanResult:
    """清洗顺序和产品流程图一致，每一行只进入一个计数桶。"""
    video_ids: list[str] = []
    seen_ids: set[str] = set()
    for row in rows:
        video_id = video_id_from_url(row.get("videoUrl", ""))
        if video_id and video_id not in seen_ids:
            seen_ids.add(video_id)
            video_ids.append(video_id)
    if not video_ids:
        raise CleanError("没有找到视频链接")
    if len(video_ids) > 1:
        raise CleanError("一份文件里只能有一个视频")
    video_id = video_ids[0]

    stats = {
        "rawRows": len(rows),
        "valid": 0,
        "duplicate": 0,
        "droppedEmpty": 0,
        "droppedNotLevel1": 0,
        "lowInformation": 0,
        "missingCommentId": 0,
    }
    # 同一评论 ID 先出现的一行留在这里。点赞只有严格更高时才替换。
    kept: dict[str, dict] = {}
    title = author = video_url = publish_time = description = ""

    for index, row in enumerate(rows):
        if not title:
            title = row.get("videoTitle", "")
        if not author:
            author = row.get("videoAuthor", "")
        if not video_url:
            video_url = row.get("videoUrl", "")
        if not publish_time:
            publish_time = row.get("videoPublishTime", "")
        if not description:
            description = row.get("videoDescription", "")

        ai_text = to_ai_text(row.get("content", ""))
        if not has_substance(ai_text):
            stats["droppedEmpty"] += 1
            continue
        if not is_level_one(row.get("level", ""), row.get("parentId", "")):
            stats["droppedNotLevel1"] += 1
            continue
        comment_id = row.get("commentId", "").strip()
        if not comment_id:
            stats["missingCommentId"] += 1
            continue

        comparable, likes = parse_likes(row.get("likes", ""))
        low_information = is_low_information(ai_text)
        comment = {
            "commentId": comment_id,
            "rawText": row.get("content", ""),
            "aiText": ai_text,
            "likeCount": likes,
            "likeComparable": comparable,
            "replyCount": _to_int(row.get("replies", "")),
            "createdAt": row.get("commentTime", ""),
            "isAuthor": row.get("isAuthor", "") == "是",
            "isPinned": row.get("isPinned", "") == "是",
            "lowInformation": low_information,
            "orderIndex": index,
            "labels": [],
            "groupKey": None,
            "themeId": None,
            "themeName": None,
        }
        previous = kept.get(comment_id)
        if previous is None:
            kept[comment_id] = comment
            continue
        stats["duplicate"] += 1
        # 无法比较，或者点赞没有严格更高，都留下文件里先出现的那条。
        if comparable and previous.get("likeComparable") and likes > previous["likeCount"]:
            kept[comment_id] = comment

    comments = list(kept.values())
    for comment in comments:
        if comment["lowInformation"]:
            stats["lowInformation"] += 1
        else:
            stats["valid"] += 1
    if not comments:
        raise CleanError("没有可保留的一级评论", stats)

    samples = [
        {
            "commentId": comment["commentId"],
            "likeCount": comment["likeCount"],
            "text": clip_text(comment["aiText"], 80),
            "lowInformation": comment["lowInformation"],
        }
        for comment in comments[:5]
    ]
    return CleanResult(
        video_id=video_id,
        title=title,
        author=author,
        video_url=video_url or f"https://www.douyin.com/video/{video_id}",
        publish_time=publish_time,
        description=description,
        hashtags=hashtags(title),
        stats=stats,
        comments=comments,
        samples=samples,
    )


def video_id_from_url(url: str) -> str | None:
    """视频 ID 只从链接里的 /video/数字 读取。

    导出表里的「视频ID」列经常被表格软件写成科学计数法，精度已经丢了，不能当主键。
    """
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else None


def is_level_one(level: str, parent_id: str) -> bool:
    parent = (parent_id or "").strip()
    normalized = (level or "").strip()
    if normalized and normalized != "1":
        return False
    if parent and parent != "0":
        return False
    return True


def to_ai_text(raw_text: str) -> str:
    text = URL_PATTERN.sub(" ", raw_text or "")
    text = text.replace("\ufffd", "")
    return re.sub(r"\s+", " ", text).strip()


def has_substance(ai_text: str) -> bool:
    stripped = re.sub(r"\s+", "", BRACKET_EMOJI.sub("", ai_text))
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", stripped))


def is_low_information(ai_text: str) -> bool:
    """低信息量评论会入库，但不会发给模型。标签留空，总览里计入未归类。"""
    compact = BRACKET_EMOJI.sub("", ai_text)
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", compact)
    if len(compact) < 2:
        return True
    if re.fullmatch(r"[嗯啊哦哈]+", compact):
        return True
    return compact in GENERIC_REPLIES


def parse_likes(raw: str) -> tuple[bool, int]:
    """返回 (能否比较, 点赞数)。空单元格当成 0，可以比较；「很多」这种文字不能比较。"""
    text = (raw or "").replace(",", "").replace("，", "").strip()
    if text == "":
        return True, 0
    wan = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[万wW]", text)
    if wan:
        return True, int(round(float(wan.group(1)) * 10000))
    try:
        return True, int(round(float(text)))
    except ValueError:
        return False, 0


def hashtags(title: str) -> list[str]:
    tags = []
    for match in HASHTAG.finditer(title or ""):
        tag = match.group(1).rstrip("，。！？、")
        if tag:
            tags.append(tag)
    return tags


def clip_text(text: str, limit: int) -> str:
    return (text or "")[:limit]


def _apply_mapping(row: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for key, _label, _aliases in FIELDS:
        header = mapping.get(key) or ""
        mapped[key] = (row.get(header, "") if header else "").strip()
    return mapped


def _read_csv(content: bytes) -> list[dict[str, str]]:
    text = _decode_csv(content)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CleanError("文件里没有评论")
    rows: list[dict[str, str]] = []
    for raw in reader:
        item, empty = _row_from_pairs((raw or {}).items())
        if not empty:
            rows.append(item)
    return rows


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _read_xlsx(content: bytes) -> list[dict[str, str]]:
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        iterator = sheet.iter_rows(values_only=True)
        header_row = next(iterator, None)
        if not header_row:
            raise CleanError("文件里没有工作表")
        headers = [_header_name(cell, index) for index, cell in enumerate(header_row)]
        rows: list[dict[str, str]] = []
        for raw in iterator:
            pairs = []
            for index, header in enumerate(headers):
                value = raw[index] if raw is not None and index < len(raw) else ""
                pairs.append((header, _cell_str(value)))
            item, empty = _row_from_pairs(pairs)
            if not empty:
                rows.append(item)
        return rows
    finally:
        workbook.close()


def _header_name(cell: object, index: int) -> str:
    name = _cell_str(cell)
    return name or f"__EMPTY_{index}"


def _row_from_pairs(pairs: Iterable[tuple[object, object]]) -> tuple[dict[str, str], bool]:
    item: dict[str, str] = {}
    empty = True
    for key, value in pairs:
        if key is None:
            continue
        name = str(key).strip()
        if not name or name.startswith("__EMPTY"):
            continue
        cell = _cell_str(value)
        if cell:
            empty = False
        item[name] = cell
    return item, empty


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 整数值不用科学计数法。评论 ID 如果在表格里已变成浮点，精度可能已经丢了。
        if value.is_integer():
            return str(int(value))
        return str(value).strip()
    return str(value).strip()


def _to_int(value: str) -> int:
    comparable, number = parse_likes(value)
    return number if comparable else 0
