"""导出只读取已经保存、并且包含用户修改的报告。"""

from __future__ import annotations

import csv
import io

from server.labels import LABEL_NAMES


def render_markdown(video: dict, report: dict) -> str:
    stats = video.get("cleanStats") or {}
    counts = report.get("labelCounts") or {}
    lines = [
        f"# {video.get('title') or video.get('videoId')}",
        "",
        f"- 视频 ID：{video.get('videoId')}",
        f"- 作者：{video.get('author') or ''}",
        f"- 提示词版本：{report.get('promptVersion')}",
        "",
        "## 总览",
        "",
        f"- 原始记录：{stats.get('rawRows', 0)}",
        f"- 有效评论：{stats.get('valid', 0)}",
        f"- 主题数：{report.get('themeCount', 0)}",
        f"- 提问评论：{counts.get('question', 0)}",
        f"- 消极反馈：{counts.get('negative', 0)}",
        f"- 模型未打上标签：{report.get('unlabeled', 0)}",
        f"- 低信息量：{report.get('lowInformation', 0)}",
        "",
        "四个标签分开计数，相加不等于评论总数。",
        "",
    ]
    for label, name in LABEL_NAMES.items():
        lines.append(f"### {name}")
        lines.append("")
        matched = [theme for theme in report.get("themes") or [] if theme.get("label") == label]
        if not matched:
            lines.append("这一类没有形成主题。")
            lines.append("")
            continue
        total = matched[0].get("labelTotal", 0)
        lines.append(f"全量 {total} 条。")
        lines.append("")
        for theme in matched:
            lines.append(f"- {theme.get('name')}：高赞样本 {theme.get('sampleCount')} 条，分数 {theme.get('score')}")
            lines.append("")
    other_themes = [theme for theme in report.get("themes") or [] if theme.get("label") == "other"]
    if other_themes:
        lines.append("### 其他")
        lines.append("")
        for theme in other_themes:
            lines.append(f"- {theme.get('name')}：{theme.get('labelTotal', 0)} 条，分数 {theme.get('score')}")
        lines.append("")
    lines.append("## 内容机会")
    lines.append("")
    for card in report.get("opportunities") or []:
        star = "已收藏" if card.get("favorite") else "未收藏"
        lines.append(f"### {card.get('rank')}. {card.get('topic') or '未命名选题'}")
        lines.append("")
        lines.append(f"- 状态：{star}")
        lines.append(f"- 优先级：第 {card.get('rank')}，分数 {card.get('score')}（推断参与排序）")
        lines.append(f"- 执行成本：{card.get('cost')}（推断）")
        lines.append(f"- 目标需求：{card.get('targetNeed') or ''}")
        lines.append(f"- 验证方式：{card.get('validation') or ''}")
        lines.append(f"- 高赞样本：{card.get('sampleCount')} 条（事实），该标签全量 {card.get('labelTotal')} 条")
        lines.append("- 内容结构：")
        for part in card.get("structure") or []:
            lines.append(f"  - {part}")
        for claim in card.get("claims") or []:
            mark = "已降级" if claim.get("downgraded") else claim.get("kind")
            lines.append(f"- {mark}：{claim.get('text') or ''}")
            for excerpt in claim.get("excerpts") or []:
                lines.append(f"  - 摘录 {excerpt.get('commentId')}：{excerpt.get('text')}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_csv(video: dict, report: dict, comments: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "rowType",
            "videoId",
            "themeId",
            "topic",
            "structure",
            "targetNeed",
            "validation",
            "favorite",
            "score",
            "sampleCount",
            "labelTotal",
            "claimKind",
            "claimText",
            "downgraded",
            "commentId",
            "likes",
            "labels",
            "themeName",
            "text",
        ]
    )
    for card in report.get("opportunities") or []:
        claims = card.get("claims") or [{}]
        for claim in claims:
            writer.writerow(
                [
                    "opportunity",
                    video.get("videoId"),
                    card.get("themeId"),
                    card.get("topic"),
                    " / ".join(card.get("structure") or []),
                    card.get("targetNeed"),
                    card.get("validation"),
                    "yes" if card.get("favorite") else "no",
                    card.get("score"),
                    card.get("sampleCount"),
                    card.get("labelTotal"),
                    claim.get("kind", ""),
                    claim.get("text", ""),
                    "yes" if claim.get("downgraded") else "no",
                    " ".join(claim.get("commentIds") or []),
                    "",
                    "",
                    "",
                    "",
                ]
            )
    for comment in comments:
        writer.writerow(
            [
                "comment",
                video.get("videoId"),
                comment.get("themeId") or "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                comment.get("commentId"),
                comment.get("likeCount"),
                " ".join(comment.get("labels") or []),
                comment.get("themeName") or "",
                comment.get("rawText") or "",
            ]
        )
    return buffer.getvalue()
