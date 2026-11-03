"""清洗、证据降级、用户隔离和一条完整分析。不访问真实的 CloudBase 或 DeepSeek。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from server.auth import AuthError, _auth_message
from server.config import Settings
from server.deepseek import ModelError, complete_json_with
from server.dsh_runner import fresh_session_id, map_notification, should_record
from server.main import create_app
from server.store import MemoryStore
from server.validate import score_parts, verify_fact

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "files" / "exportdou-6b7f0fec-f310-49a3-a302-8063863efc01.csv"


class FakeAuth:
    def send_email_code(self, email: str) -> dict:
        return {"verificationId": f"vid-{email}", "expiresIn": 600, "isUser": True}

    def complete_email(self, email: str, verification_id: str, code: str, is_user: bool) -> dict:
        del is_user
        name = email.split("@", 1)[0]
        if verification_id != f"vid-{email}" or code != "123456":
            raise AuthError("验证码不正确")
        return {
            "accessToken": f"t-{name}",
            "refreshToken": f"r-{name}",
            "expiresIn": 7200,
            "uid": f"uid-{name}",
            "username": email,
        }

    def refresh_session(self, refresh_token: str) -> dict:
        if not refresh_token.startswith("r-"):
            raise AuthError("登录已过期，请重新登录")
        name = refresh_token.split("-", 1)[1]
        return {
            "accessToken": f"t-{name}",
            "refreshToken": f"r-{name}-next",
            "expiresIn": 7200,
            "uid": f"uid-{name}",
            "username": f"{name}@example.com",
        }

    def user_from_token(self, token: str) -> dict:
        if not token.startswith("t-"):
            raise AuthError("登录已过期或无效")
        username = token[2:]
        return {"uid": f"uid-{username}", "username": username}


class ScriptedModel:
    """按评论原文给出固定 JSON。用来证明服务端会自己数条数、自己做证据校验。"""

    def __init__(self):
        self.seen: list[tuple[str, dict]] = []

    def complete_json(self, *, stage: str, system: str, user: dict, required_ids, items_key: str, id_field: str, max_tokens: int):
        del system, required_ids, items_key, id_field, max_tokens
        self.seen.append((stage, user))
        if stage == "positioning":
            return {
                "vertical": "女装测评",
                "format": "试穿",
                "topic": "拼多多女装是否值得买",
                "boundaryNotes": ["求链接算需求", "指出色差算负面"],
            }
        if stage == "classify":
            items = []
            for row in user["comments"]:
                text = row["text"]
                if "链接" in text or "哪里买" in text:
                    items.append({"id": row["id"], "labels": ["need"], "groupKey": "风衣链接"})
                elif "色差" in text:
                    items.append({"id": row["id"], "labels": ["negative"], "groupKey": "不该留下"})
                else:
                    items.append({"id": row["id"], "labels": []})
            return {"items": items}
        if stage == "reclassify":
            return {
                "categories": [{"name": "版型讨论", "summary": "在说衣服版型"}],
                "assignments": [{"id": row["id"], "category": "版型讨论"} for row in user["comments"]],
            }
        if stage == "cluster":
            theme = "同款链接" if user["label"] == "need" else "色差严重"
            summary = "想找到同款" if user["label"] == "need" else "实物和视频不一致"
            return {
                "summary": summary,
                "themes": [{"name": theme, "summary": summary}],
                "assignments": [{"id": row["id"], "theme": theme} for row in user["comments"]],
            }
        if stage == "rank":
            return {
                "items": [
                    {"id": theme["id"], "feasibility": 4, "cost": 2, "reason": "补一条购买指引"}
                    for theme in user["themes"]
                ]
            }
        if stage == "draft":
            return {
                "items": [
                    {
                        "themeId": theme["themeId"],
                        "topic": "下期补上购买方式",
                        "structure": ["先说结论", "展示色差", "给出搜索词"],
                        "targetNeed": "观众要能找到同款",
                        "validation": "看新评论里还求不求链接",
                        "claims": [
                            {"kind": "fact", "text": "有人在求同款", "commentIds": theme["commentIds"][:1]},
                            {"kind": "fact", "text": "这条热度是编的", "commentIds": ["not-a-real-id"]},
                            {"kind": "hypothesis", "text": "补上链接后求链接会变少", "commentIds": []},
                        ],
                    }
                    for theme in user["themes"]
                ]
            }
        raise AssertionError(stage)


def settings() -> Settings:
    return Settings(
        cloudbase_env_id="cloud1-test",
        cloudbase_api_key="memory",
        deepseek_api_key="test-key",
        deepseek_model="deepseek-flash",
        dsh_home="/tmp/pingjian-dsh-test",
        tool_token="token-for-test-1234567890",
        internal_base="http://127.0.0.1:8787",
    )


def build_client(model=None):
    store = MemoryStore()
    app = create_app(
        settings=settings(),
        store=store,
        auth=FakeAuth(),
        model=model or ScriptedModel(),
        orchestrator="direct",
        inline=True,
    )
    return TestClient(app), app, store


def auth_header(client: TestClient, username: str) -> dict[str, str]:
    email = f"{username}@example.com"
    sent = client.post("/api/auth/email/code", json={"email": email})
    assert sent.status_code == 200, sent.text
    response = client.post(
        "/api/auth/email/session",
        json={
            "email": email,
            "verificationId": sent.json()["verificationId"],
            "code": "123456",
            "isUser": sent.json()["isUser"],
        },
    )
    assert response.status_code == 200, response.text
    token = response.json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def pipeline_csv() -> bytes:
    lines = [
        "视频链接,视频标题,评论ID,评论内容,层级,父评论ID,点赞数",
        "https://www.douyin.com/video/555,秋季风衣怎么选,c1,求风衣链接,1,,10",
        "https://www.douyin.com/video/555,秋季风衣怎么选,c2,这件风衣哪里买,1,,8",
        "https://www.douyin.com/video/555,秋季风衣怎么选,c3,链接呢,1,,3",
        "https://www.douyin.com/video/555,秋季风衣怎么选,c4,色差太大了完全不是视频里那样,1,,6",
        "https://www.douyin.com/video/555,秋季风衣怎么选,c5,色差严重,1,,5",
        "https://www.douyin.com/video/555,秋季风衣怎么选,c6,哈哈,1,,1",
    ]
    for index in range(7, 22):
        lines.append(f"https://www.douyin.com/video/555,秋季风衣怎么选,c{index},随便聊聊这件衣服的版型{index},1,,1")
    return ("\n".join(lines) + "\n").encode("utf-8")


def short_csv() -> bytes:
    lines = [
        "视频链接,视频标题,评论ID,评论内容,层级,父评论ID,点赞数",
        "https://www.douyin.com/video/556,短视频,s1,求风衣链接,1,,10",
        "https://www.douyin.com/video/556,短视频,s2,哈哈,1,,1",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def neutral_csv() -> bytes:
    lines = ["视频链接,视频标题,评论ID,评论内容,层级,父评论ID,点赞数"]
    for index in range(1, 21):
        lines.append(f"https://www.douyin.com/video/777,随便聊聊,n{index},这件衣服的版型值得再看一眼{index},1,,1")
    return ("\n".join(lines) + "\n").encode("utf-8")


def mostly_labeled_csv() -> bytes:
    lines = ["视频链接,视频标题,评论ID,评论内容,层级,父评论ID,点赞数"]
    for index in range(1, 17):
        lines.append(f"https://www.douyin.com/video/888,大多能分类,k{index},求风衣链接,1,,2")
    for index in range(1, 5):
        lines.append(f"https://www.douyin.com/video/888,大多能分类,u{index},这件衣服的版型值得再看一眼,1,,1")
    return ("\n".join(lines) + "\n").encode("utf-8")


class AuthTests(unittest.TestCase):
    def test_email_code_rejects_a_bad_address_and_a_bad_code(self):
        client, _app, _store = build_client()
        rejected = client.post("/api/auth/email/code", json={"email": "not-an-email"})
        self.assertEqual(rejected.status_code, 400)

        sent = client.post("/api/auth/email/code", json={"email": "ana@example.com"})
        self.assertEqual(sent.status_code, 200)
        wrong = client.post(
            "/api/auth/email/session",
            json={
                "email": "ana@example.com",
                "verificationId": sent.json()["verificationId"],
                "code": "000000",
                "isUser": True,
            },
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("验证码", wrong.json()["detail"])

    def test_cloudbase_verification_mismatch_is_a_plain_message(self):
        message = _auth_message(
            {
                "message": "无效的验证码%!(EXTRA string=rpc error: code = InvalidArgument desc = verification code does not match the id)"
            },
            "",
        )
        self.assertEqual(message, "验证码不正确")

    def test_refresh_keeps_the_same_user_and_rotates_the_token(self):
        client, _app, _store = build_client()
        sent = client.post("/api/auth/email/code", json={"email": "ana@example.com"})
        logged = client.post(
            "/api/auth/email/session",
            json={
                "email": "ana@example.com",
                "verificationId": sent.json()["verificationId"],
                "code": "123456",
                "isUser": True,
            },
        )
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertEqual(logged.json()["expiresIn"], 7200)
        renewed = client.post("/api/auth/refresh", json={"refreshToken": logged.json()["refreshToken"]})
        self.assertEqual(renewed.status_code, 200, renewed.text)
        self.assertEqual(renewed.json()["uid"], "uid-ana")
        self.assertEqual(renewed.json()["refreshToken"], "r-ana-next")
        me = client.get("/api/me", headers={"Authorization": f"Bearer {renewed.json()['accessToken']}"})
        self.assertEqual(me.status_code, 200, me.text)

    def test_refresh_rejects_a_missing_token(self):
        client, _app, _store = build_client()
        rejected = client.post("/api/auth/refresh", json={"refreshToken": "  "})
        self.assertEqual(rejected.status_code, 401)

    def test_expired_refresh_token_asks_the_user_to_sign_in_again(self):
        message = _auth_message(
            {"error": "invalid_grant", "error_description": "refresh_token无效或已过期"},
            "",
        )
        self.assertEqual(message, "登录已过期，请重新登录")


class CleanTests(unittest.TestCase):
    def test_sample_file_uses_video_id_from_the_link(self):
        client, _app, _store = build_client()
        headers = auth_header(client, "ana")
        response = client.post(
            "/api/imports/preview",
            headers=headers,
            files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["video"]["videoId"], "7696144437814208872")
        stats = body["summary"]
        self.assertEqual(stats["rawRows"], 100)
        accounted = (
            stats["droppedEmpty"]
            + stats["droppedNotLevel1"]
            + stats["missingCommentId"]
            + stats["duplicate"]
            + stats["lowInformation"]
            + stats["valid"]
        )
        self.assertEqual(accounted, stats["rawRows"])
        self.assertGreater(stats["valid"], 0)

    def test_cleaning_rules_and_manual_mapping(self):
        from server.clean import clean_rows, parse_import

        text = "\n".join(
            [
                "视频链接,评论ID,评论内容,层级,父评论ID,点赞数",
                "https://www.douyin.com/video/123,a,先出现,1,,1",
                "https://www.douyin.com/video/123,a,后来的,1,,5",
                "https://www.douyin.com/video/123,b,第一条,1,,很多",
                "https://www.douyin.com/video/123,b,第二条,1,,9",
                "https://www.douyin.com/video/123,g,平局先出现,1,,3",
                "https://www.douyin.com/video/123,g,平局后出现,1,,3",
                "https://www.douyin.com/video/123,c,,1,,1",
                "https://www.douyin.com/video/123,d,这是一条回复,2,parent,1",
                "https://www.douyin.com/video/123,,有内容但没有评论ID,1,,1",
                "https://www.douyin.com/video/123,e,哈,1,,1",
                "https://www.douyin.com/video/123,f,同问,1,,1",
            ]
        )
        result = parse_import("comments.csv", text.encode("utf-8"), None)
        by_id = {item["commentId"]: item for item in result.comments}
        self.assertEqual(by_id["a"]["rawText"], "后来的")
        self.assertEqual(by_id["b"]["rawText"], "第一条")
        self.assertEqual(by_id["g"]["rawText"], "平局先出现")
        self.assertEqual(result.stats["duplicate"], 3)
        self.assertEqual(result.stats["droppedEmpty"], 1)
        self.assertEqual(result.stats["droppedNotLevel1"], 1)
        self.assertEqual(result.stats["missingCommentId"], 1)
        self.assertEqual(result.stats["lowInformation"], 2)
        self.assertEqual(result.stats["valid"], 3)
        self.assertTrue(by_id["e"]["lowInformation"])
        self.assertTrue(by_id["f"]["lowInformation"])

        book = Workbook()
        sheet = book.active
        sheet.append(["link", "cid", "body"])
        sheet.append(["https://www.douyin.com/video/9", "z1", "想要清单"])
        buffer = Path("/tmp/pingjian-map.xlsx")
        book.save(buffer)
        mapped = parse_import(
            "map.xlsx",
            buffer.read_bytes(),
            {"videoUrl": "link", "commentId": "cid", "content": "body"},
        )
        self.assertEqual(mapped.video_id, "9")
        self.assertEqual(mapped.comments[0]["commentId"], "z1")
        with self.assertRaises(Exception):
            clean_rows(
                [
                    {"videoUrl": "https://www.douyin.com/video/1", "commentId": "a", "content": "甲", "level": "1", "parentId": "", "likes": "1"},
                    {"videoUrl": "https://www.douyin.com/video/2", "commentId": "b", "content": "乙", "level": "1", "parentId": "", "likes": "1"},
                ]
            )


class EvidenceTests(unittest.TestCase):
    def test_score_uses_sample_count_not_label_total(self):
        parts = score_parts(10, 0.5, 4, 2)
        self.assertEqual(parts["countNorm"], 0.5)
        self.assertEqual(parts["score"], 50)

    def test_foreign_comment_id_is_downgraded(self):
        comments = {"c1": {"commentId": "c1", "rawText": "求风衣链接，颜色也想知道"}}
        good = verify_fact({"kind": "fact", "text": "有人在求链接", "commentIds": ["c1"]}, {"c1"}, comments)
        self.assertEqual(good["kind"], "fact")
        self.assertFalse(good["downgraded"])
        self.assertIn(good["excerpts"][0]["text"], "求风衣链接，颜色也想知道")
        self.assertLessEqual(len(good["excerpts"][0]["text"]), 40)
        bad = verify_fact({"kind": "fact", "text": "编造的热度", "commentIds": ["other"]}, {"c1"}, comments)
        self.assertEqual(bad["kind"], "inference")
        self.assertTrue(bad["downgraded"])
        self.assertEqual(bad["commentIds"], [])

    def test_json_retries_once_then_reports_missing_ids(self):
        calls = {"n": 0}

        def post() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                return "不是 JSON"
            return json.dumps({"items": [{"id": "a"}, {"id": "b"}]}, ensure_ascii=False)

        data = complete_json_with(
            stage="classify",
            required_ids=["a", "b"],
            items_key="items",
            id_field="id",
            post=post,
        )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(data["items"]), 2)

        def always_short() -> str:
            return json.dumps({"items": [{"id": "a"}]})

        with self.assertRaises(ModelError) as caught:
            complete_json_with(
                stage="classify",
                required_ids=["a", "b"],
                items_key="items",
                id_field="id",
                post=always_short,
            )
        self.assertEqual(caught.exception.stage, "classify")
        self.assertEqual(caught.exception.missing_ids, ["b"])

    def test_thinking_notifications_are_dropped(self):
        self.assertIsNone(map_notification({"type": "agent/thinking", "thinking": "secret chain"}))
        mapped = map_notification({"type": "tool/call", "toolName": "inspect_import", "thinking": "hidden"})
        self.assertEqual(mapped["type"], "stage_started")
        self.assertNotIn("hidden", json.dumps(mapped, ensure_ascii=False))
        existing = [{"type": "failed", "stage": "classify_comments", "message": "停住了"}]
        self.assertFalse(
            should_record(existing, {"type": "stage_completed", "stage": "classify_comments", "message": "完成"})
        )

    def test_each_run_gets_a_new_dsh_session_id(self):
        task_id = "3bb5088623b84a97a6dd2b87bb12df45"
        first = fresh_session_id(task_id)
        second = fresh_session_id(task_id)
        prefix = f"pj{task_id[:16]}"
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(prefix))
        self.assertTrue(second.startswith(prefix))
        self.assertNotEqual(first, f"pj{task_id[:32]}")
        self.assertNotEqual(second, f"pj{task_id[:32]}")


class FlowTests(unittest.TestCase):
    def test_import_analysis_edit_export_delete_are_isolated(self):
        client, app, store = build_client()
        ana = auth_header(client, "ana")
        bob = auth_header(client, "bob")
        imported = client.post("/api/imports", headers=ana, files={"file": ("wind.csv", pipeline_csv(), "text/csv")})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["videoId"], "555")
        again = client.post("/api/imports", headers=ana, files={"file": ("wind.csv", pipeline_csv(), "text/csv")})
        self.assertEqual(again.status_code, 409)
        self.assertIn("请先删除", again.json()["detail"])
        other = client.post("/api/imports", headers=bob, files={"file": ("wind.csv", pipeline_csv(), "text/csv")})
        self.assertEqual(other.status_code, 200, other.text)

        denied = client.get("/api/videos/555", headers=auth_header(client, "cara"))
        self.assertEqual(denied.status_code, 404)

        failed_model = _FailingModel()
        app.state.model = failed_model
        app.state.queue.model = failed_model
        failed = client.post("/api/videos/555/analyze", headers=ana)
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["task"]["status"], "failed")
        detail = client.get("/api/videos/555", headers=ana).json()
        types = [item["type"] for item in detail["events"]]
        self.assertIn("failed", types)
        self.assertNotIn("completed", types)
        self.assertNotIn("stage_completed", [item["type"] for item in detail["events"] if item["stage"] == "classify_comments"])
        self.assertTrue(all("thinking" not in item and "secret" not in item["message"] for item in detail["events"]))
        blocked = client.post("/api/videos/555/analyze", headers=ana)
        self.assertEqual(blocked.status_code, 409)

        app.state.model = app.state.queue.model = ScriptedModel()
        retried = client.post("/api/videos/555/retry", headers=ana)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["task"]["status"], "completed")
        report = client.get("/api/videos/555", headers=ana).json()["report"]
        self.assertEqual(report["promptVersion"], "mvp-5")
        self.assertEqual(report["labelCounts"]["need"], 3)
        self.assertEqual(report["labelCounts"]["negative"], 2)
        self.assertEqual(report["unlabeled"], 15)
        self.assertEqual(report["lowInformation"], 1)
        self.assertEqual(report["unclassified"], 16)
        self.assertIsNone(report["analysisLimit"])
        self.assertNotEqual(sum(report["labelCounts"].values()), 6)
        need = next(item for item in report["themes"] if item["label"] == "need")
        self.assertEqual(need["sampleCount"], 3)
        self.assertEqual(need["labelTotal"], 3)
        self.assertEqual(need["score"], 30)
        downgraded = [
            claim
            for card in report["opportunities"]
            for claim in card["claims"]
            if claim["downgraded"]
        ]
        self.assertGreaterEqual(len(downgraded), 1)
        self.assertEqual(downgraded[0]["commentIds"], [])
        facts = [
            claim
            for card in report["opportunities"]
            for claim in card["claims"]
            if claim["kind"] == "fact"
        ]
        self.assertTrue(facts)
        comments = {item["commentId"]: item for item in store.find_all("pingjian_comments", {"uid": "uid-ana", "videoId": "555"})}
        for claim in facts:
            for excerpt in claim["excerpts"]:
                self.assertIn(excerpt["text"], comments[excerpt["commentId"]]["rawText"])
                self.assertIn(excerpt["commentId"], comments)

        model = app.state.model
        for stage, user in model.seen:
            if stage in {"classify", "cluster", "positioning"}:
                rows = user.get("comments") or user.get("topComments") or []
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(set(row), {"id", "likes", "text"})
                    self.assertNotEqual(row["text"], "哈哈")

        saved = client.patch(
            "/api/videos/555/report",
            headers=ana,
            json={"themeId": report["opportunities"][0]["themeId"], "topic": "改成自己的选题", "favorite": True},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["opportunities"][0]["topic"], "改成自己的选题")
        self.assertTrue(saved.json()["opportunities"][0]["favorite"])
        self.assertEqual(saved.json()["opportunities"][0]["sampleCount"], report["opportunities"][0]["sampleCount"])
        exported = client.get("/api/videos/555/export.md", headers=ana)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("改成自己的选题", exported.text)
        self.assertIn("模型未打上标签：15", exported.text)
        self.assertIn("低信息量：1", exported.text)
        self.assertNotIn("test-key", exported.text)
        self.assertNotIn("secret chain", exported.text)
        csv_text = client.get("/api/videos/555/export.csv", headers=ana).text
        self.assertIn("改成自己的选题", csv_text)
        self.assertNotIn("uid-bob", csv_text)

        finished = client.post("/api/videos/555/analyze", headers=ana)
        self.assertEqual(finished.status_code, 409)
        removed = client.delete("/api/videos/555", headers=ana)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(client.get("/api/videos/555", headers=ana).status_code, 404)
        self.assertEqual(len(store.find_all("pingjian_comments", {"uid": "uid-ana", "videoId": "555"})), 0)
        self.assertEqual(len(store.find_all("pingjian_videos", {"uid": "uid-bob", "videoId": "555"})), 1)
        self.assertEqual(client.delete("/api/videos/555", headers=ana).status_code, 404)
        self.assertEqual(len(store.find_all("pingjian_videos", {"uid": "uid-bob", "videoId": "555"})), 1)

    def test_internal_tool_requires_token(self):
        client, _app, store = build_client()
        headers = auth_header(client, "ana")
        client.post("/api/imports", headers=headers, files={"file": ("wind.csv", pipeline_csv(), "text/csv")})
        store.insert(
            "pingjian_tasks",
            {
                "uid": "uid-ana",
                "videoId": "555",
                "taskId": "task-inspect-001",
                "status": "started",
                "stages": {},
                "eventSeq": 0,
                "promptVersion": "mvp-4",
            },
        )
        denied = client.post("/internal/tools/inspect_import", json={"taskId": "task-inspect-001"})
        self.assertEqual(denied.status_code, 401)
        accepted = client.post(
            "/internal/tools/inspect_import",
            json={"taskId": "task-inspect-001"},
            headers={"x-pingjian-tool-token": settings().tool_token},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertIn("送给模型", accepted.json()["summary"])

    def test_health_lists_missing_keys(self):
        bare = Settings(
            cloudbase_env_id="cloud1-4grqnt5f5b5d8f20",
            cloudbase_api_key="",
            deepseek_api_key="",
            deepseek_model="deepseek-flash",
            dsh_home="/tmp/pingjian-dsh-test",
            tool_token="token-for-test-1234567890",
            internal_base="http://127.0.0.1:8787",
        )
        app = create_app(settings=bare, store=MemoryStore(), auth=FakeAuth(), model=ScriptedModel(), orchestrator="direct", inline=True)
        with TestClient(app) as client:
            body = client.get("/api/health").json()
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertIn("CLOUDBASE_API_KEY", body["missing"])
        self.assertIn("DEEPSEEK_API_KEY", body["missing"])


    def test_too_few_valid_comments_never_call_the_model(self):
        client, app, _store = build_client()
        headers = auth_header(client, "mina")
        imported = client.post("/api/imports", headers=headers, files={"file": ("short.csv", short_csv(), "text/csv")})
        self.assertEqual(imported.status_code, 200, imported.text)
        refused = client.post("/api/videos/556/analyze", headers=headers)
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("有效评论 1 条", refused.json()["detail"])
        self.assertIn("低信息量 1", refused.json()["detail"])
        self.assertIn("不会调用模型", refused.json()["detail"])
        self.assertEqual(app.state.model.seen, [])
        detail = client.get("/api/videos/556", headers=headers).json()
        self.assertIsNone(detail["task"])
        self.assertIn("有效评论 1 条", detail["analysisBlock"])
        self.assertIn("原始记录 2", detail["analysisBlock"])

    def test_comment_buckets_split_unlabeled_and_low_information(self):
        client, _app, _store = build_client()
        headers = auth_header(client, "nina")
        client.post("/api/imports", headers=headers, files={"file": ("wind.csv", pipeline_csv(), "text/csv")})
        done = client.post("/api/videos/555/analyze", headers=headers)
        self.assertEqual(done.status_code, 200, done.text)
        unlabeled = client.get("/api/videos/555/comments?label=unlabeled", headers=headers).json()
        low = client.get("/api/videos/555/comments?label=low", headers=headers).json()
        self.assertEqual(unlabeled["total"], 15)
        self.assertEqual({item["commentId"] for item in unlabeled["items"]}, {f"c{index}" for index in range(7, 22)})
        self.assertEqual(low["total"], 1)
        self.assertEqual(low["items"][0]["commentId"], "c6")
        self.assertTrue(low["items"][0]["lowInformation"])

    def test_no_themes_skips_rank_and_draft_but_keeps_the_report(self):
        client, app, _store = build_client()
        headers = auth_header(client, "otto")
        imported = client.post("/api/imports", headers=headers, files={"file": ("neutral.csv", neutral_csv(), "text/csv")})
        self.assertEqual(imported.status_code, 200, imported.text)
        done = client.post("/api/videos/777/analyze", headers=headers)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["task"]["status"], "completed")
        report = client.get("/api/videos/777", headers=headers).json()["report"]
        self.assertEqual(report["opportunities"], [])
        self.assertEqual(report["unlabeled"], 20)
        self.assertIn("已跳过排序和选题", report["analysisLimit"])
        stages = [stage for stage, _user in app.state.model.seen]
        self.assertEqual(stages, ["positioning", "classify"])

    def test_reclassify_is_optional_and_only_sends_unlabeled_comments(self):
        client, app, store = build_client()
        headers = auth_header(client, "pia")
        client.post("/api/imports", headers=headers, files={"file": ("wind.csv", pipeline_csv(), "text/csv")})
        store.insert(
            "pingjian_tasks",
            {
                "uid": "uid-pia",
                "videoId": "555",
                "taskId": "task-reclassify-001",
                "status": "started",
                "stages": {},
                "eventSeq": 0,
                "promptVersion": "mvp-4",
            },
        )
        token = {"x-pingjian-tool-token": settings().tool_token}
        early = client.post("/internal/tools/reclassify_other", json={"taskId": "task-reclassify-001"}, headers=token)
        self.assertEqual(early.status_code, 400, early.text)
        self.assertEqual([stage for stage, _user in app.state.model.seen], [])

        inspected = client.post("/internal/tools/inspect_import", json={"taskId": "task-reclassify-001"}, headers=token)
        self.assertEqual(inspected.status_code, 200, inspected.text)
        classified = client.post("/internal/tools/classify_comments", json={"taskId": "task-reclassify-001"}, headers=token)
        self.assertEqual(classified.status_code, 200, classified.text)
        self.assertIn("可以考虑补分类", classified.json()["summary"])

        again = client.post("/internal/tools/reclassify_other", json={"taskId": "task-reclassify-001"}, headers=token)
        self.assertEqual(again.status_code, 200, again.text)
        calls = [user for stage, user in app.state.model.seen if stage == "reclassify"]
        self.assertEqual(len(calls), 1)
        sent = {row["id"] for row in calls[0]["comments"]}
        self.assertEqual(sent, {f"c{index}" for index in range(7, 22)})
        stored = {
            item["commentId"]: item
            for item in store.find_all("pingjian_comments", {"uid": "uid-pia", "videoId": "555"})
        }
        for comment_id in sent:
            self.assertEqual(stored[comment_id]["labels"], [])
            self.assertEqual(stored[comment_id]["themeName"], "版型讨论")
        self.assertEqual(stored["c1"]["labels"], ["need"])
        self.assertFalse(stored["c1"].get("themeName"))
        self.assertTrue(stored["c6"]["lowInformation"])
        self.assertFalse(stored["c6"].get("themeName"))

        second = client.post("/internal/tools/reclassify_other", json={"taskId": "task-reclassify-001"}, headers=token)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIn("不再调用模型", second.json()["summary"])
        self.assertEqual(len([stage for stage, _user in app.state.model.seen if stage == "reclassify"]), 1)

        clustered = client.post("/internal/tools/cluster_topics", json={"taskId": "task-reclassify-001"}, headers=token)
        self.assertEqual(clustered.status_code, 200, clustered.text)
        task = store.find_all("pingjian_tasks", {"taskId": "task-reclassify-001"})[0]
        other = [item for item in task["stages"]["cluster_topics"]["data"]["themes"] if item["label"] == "other"]
        self.assertEqual([item["name"] for item in other], ["版型讨论"])

        blocked = build_client()
        blocked_client, blocked_app, blocked_store = blocked
        blocked_headers = auth_header(blocked_client, "qia")
        blocked_client.post(
            "/api/imports",
            headers=blocked_headers,
            files={"file": ("labeled.csv", mostly_labeled_csv(), "text/csv")},
        )
        blocked_store.insert(
            "pingjian_tasks",
            {
                "uid": "uid-qia",
                "videoId": "888",
                "taskId": "task-reclassify-002",
                "status": "started",
                "stages": {},
                "eventSeq": 0,
                "promptVersion": "mvp-4",
            },
        )
        opened = blocked_client.post(
            "/internal/tools/inspect_import",
            json={"taskId": "task-reclassify-002"},
            headers=token,
        )
        self.assertEqual(opened.status_code, 200, opened.text)
        labeled = blocked_client.post(
            "/internal/tools/classify_comments",
            json={"taskId": "task-reclassify-002"},
            headers=token,
        )
        self.assertEqual(labeled.status_code, 200, labeled.text)
        self.assertIn("不要补分类", labeled.json()["summary"])
        refused = blocked_client.post(
            "/internal/tools/reclassify_other",
            json={"taskId": "task-reclassify-002"},
            headers=token,
        )
        self.assertEqual(refused.status_code, 200, refused.text)
        self.assertIn("未达到补分类条件", refused.json()["summary"])
        self.assertNotIn("reclassify", [stage for stage, _user in blocked_app.state.model.seen])


class CloudBaseStoreTests(unittest.TestCase):
    def test_insert_wraps_documents_in_data(self):
        from server.cloudbase import CloudBaseStore

        store = CloudBaseStore("cloud1-test", "key")
        captured: dict = {}

        class FakeResponse:
            status_code = 201
            text = ""

            def json(self):
                return {"insertedIds": ["doc-1"]}

        def post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

        store.http.post = post
        doc_id = store.insert("pingjian_videos", {"uid": "u1", "videoId": "v1"})
        self.assertEqual(doc_id, "doc-1")
        self.assertTrue(captured["url"].endswith("/collections/pingjian_videos/documents"))
        self.assertEqual(captured["json"], {"data": [{"uid": "u1", "videoId": "v1"}]})
        store.http.close()

    def test_unwrap_docs_decodes_extended_json_numbers(self):
        from server.cloudbase import _unwrap_docs

        docs = _unwrap_docs(
            {
                "data": {
                    "list": [
                        {
                            "_id": {"$oid": "abc123"},
                            "seq": {"$numberInt": "3"},
                            "likeCount": {"$numberLong": "12"},
                            "evidence": {"$numberDouble": "0.5"},
                            "cleanStats": {
                                "rawRows": {"$numberInt": "40"},
                                "valid": {"$numberInt": "36"},
                            },
                            "labelCounts": {
                                "need": {"$numberInt": "4"},
                                "question": {"$numberInt": "0"},
                            },
                            "importedAt": {"$date": "2026-10-31T08:00:00.000Z"},
                            "updatedAt": {"$date": {"$numberLong": "1730361600000"}},
                        }
                    ]
                }
            }
        )
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual(doc["_id"], "abc123")
        self.assertEqual(doc["seq"], 3)
        self.assertEqual(doc["likeCount"], 12)
        self.assertEqual(doc["evidence"], 0.5)
        self.assertEqual(doc["cleanStats"], {"rawRows": 40, "valid": 36})
        self.assertEqual(doc["labelCounts"]["need"], 4)
        self.assertEqual(doc["importedAt"], "2026-10-31T08:00:00.000Z")
        self.assertEqual(doc["updatedAt"], "2024-10-31T08:00:00+00:00")


class PageTests(unittest.TestCase):
    def test_repeated_page_is_not_counted_again(self):
        from server.cloudbase import collect_pages

        page = [{"_id": f"id-{index}"} for index in range(100)]

        def fetch(_offset: int, _page_size: int) -> list[dict]:
            return list(page)

        self.assertEqual(len(collect_pages(fetch)), 100)

    def test_offset_pages_are_all_kept(self):
        from server.cloudbase import collect_pages

        docs = [{"_id": f"id-{index}"} for index in range(250)]

        def fetch(offset: int, page_size: int) -> list[dict]:
            return docs[offset : offset + page_size]

        self.assertEqual([item["_id"] for item in collect_pages(fetch)], [item["_id"] for item in docs])


class CancelTests(unittest.TestCase):
    def test_cancel_stops_a_running_task_and_retry_can_start_again(self):
        client, _app, store = build_client()
        headers = auth_header(client, "nia")
        imported = client.post("/api/imports", headers=headers, files={"file": ("wind.csv", pipeline_csv(), "text/csv")})
        self.assertEqual(imported.status_code, 200, imported.text)
        store.insert(
            "pingjian_tasks",
            {
                "uid": "uid-nia",
                "videoId": "555",
                "taskId": "task-cancel-api",
                "status": "started",
                "stages": {},
                "eventSeq": 0,
                "progress": {"stage": "classify_comments", "done": 40, "total": 96},
                "promptVersion": "mvp-5",
            },
        )
        cancelled = client.post("/api/videos/555/cancel", headers=headers)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["task"]["status"], "failed")
        self.assertEqual(cancelled.json()["task"]["error"], "已取消分析")
        detail = client.get("/api/videos/555", headers=headers).json()
        self.assertIn("已取消分析", [item["message"] for item in detail["events"]])
        again = client.post("/api/videos/555/cancel", headers=headers)
        self.assertEqual(again.status_code, 409)

        retried = client.post("/api/videos/555/retry", headers=headers)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["task"]["status"], "completed")

    def test_classify_progress_uses_unique_comments_and_stops_when_cancelled(self):
        from server.analysis import AnalysisCancelled, execute_tool
        from server.events import append_event

        store = MemoryStore()
        for index in range(45):
            comment = {
                "uid": "uid-stop",
                "videoId": "555",
                "commentId": f"c{index}",
                "rawText": f"想要链接{index}",
                "aiText": f"想要链接{index}",
                "likeCount": index,
                "likeComparable": True,
                "lowInformation": False,
                "orderIndex": index,
                "labels": [],
            }
            store.insert("pingjian_comments", comment)
            store.insert("pingjian_comments", dict(comment))
        store.insert(
            "pingjian_videos",
            {"uid": "uid-stop", "videoId": "555", "title": "测试", "cleanStats": {"valid": 45}},
        )
        task = {
            "uid": "uid-stop",
            "videoId": "555",
            "taskId": "task-stop",
            "status": "started",
            "stages": {"inspect_import": {"ok": True, "summary": "ok", "data": {}}},
            "eventSeq": 0,
            "progress": {"stage": "inspect_import", "done": 1, "total": 1},
        }
        store.insert("pingjian_tasks", task)

        class StopModel:
            def __init__(self):
                self.classify_calls = 0

            def complete_json(self, **kwargs):
                if kwargs["stage"] == "positioning":
                    return {"vertical": "测试", "format": "口播", "topic": "测试主题", "boundaryNotes": []}
                if kwargs["stage"] == "classify":
                    self.classify_calls += 1
                    store.update_one(
                        "pingjian_tasks",
                        {"taskId": "task-stop"},
                        {"status": "failed", "error": "已取消分析", "cancelRequested": True},
                    )
                    return {"items": [{"id": row["id"], "labels": []} for row in kwargs["user"]["comments"]]}
                raise AssertionError(kwargs["stage"])

        model = StopModel()

        def emit(current, event_type, stage, message):
            append_event(store, current, event_type, stage, message)

        with self.assertRaises(AnalysisCancelled):
            execute_tool(store, model, "task-stop", "classify_comments", emit)
        self.assertEqual(model.classify_calls, 1)
        messages = [item["message"] for item in store.find_all("pingjian_events", {"uid": "uid-stop"})]
        self.assertIn("已分类 40/45 条", messages)
        self.assertNotIn("已分类 40/90 条", messages)
        stored = store.find_all("pingjian_tasks", {"taskId": "task-stop"})[0]
        self.assertEqual(stored["error"], "已取消分析")
        self.assertEqual(stored["progress"], {"stage": "classify_comments", "done": 40, "total": 45})


class _FailingModel:
    def complete_json(self, **kwargs):
        raise ModelError("classify", ["c1"], "JSON 无法解析")


if __name__ == "__main__":
    unittest.main()
