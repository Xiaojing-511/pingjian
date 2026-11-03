"""四个标签是全项目唯一的预设分类口径，页面和模型都用这一套。"""

from __future__ import annotations

LABELS = ("need", "question", "negative", "idea")

LABEL_NAMES = {
    "need": "明确需求",
    "question": "常见问题",
    "negative": "消极反馈",
    "idea": "内容灵感",
}

# 主题名不能和标签同名。中文名、英文 key、页面上的短称呼都算。
RESERVED_THEME_NAMES = set(LABEL_NAMES) | set(LABEL_NAMES.values()) | {
    "需求",
    "问题",
    "负面",
    "消极",
    "灵感",
    "其他",
    "负面反馈",
}
