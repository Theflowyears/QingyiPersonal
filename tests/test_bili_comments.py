"""回归锁：她刷到视频时能看见评论区在说什么，但只当「听说的」。

三条不能松：
  1) 不登录时 reply 接口是 -403（2026-11-07 复测：带 cookie code=0、返回 20 条）。
     没 cookie 必须返回「需要登录」，不能返回空列表让她以为「没人评论」。
  2) 评论是别人写的：进反应提示词时要标成未核实的外部内容，
     不写进知识库笔记 —— 她的记忆里只留「我刷到时看到几条」。
  3) 只用 GET。点赞、收藏、投币、写历史这些会动到账号的接口一个都不碰。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import bili        # noqa: E402
import qq_openclaw_bridge as b        # noqa: E402

REPLY = {"code": 0, "message": "0", "data": {
    "replies": [
        {"content": {"message": "UP主流蜜在这里\n https://evil.example/x"}},
        {"content": {"message": "第三节那个火候讲得真清楚，跟着做成功了"}},
        {"content": {"message": "太短了"}},
        {"content": {"message": "x" * 400}},
    ]}}
# 老的 x/v2/reply 把文字放在 content.content；两种形态都要能读出来，
# 否则 20 条评论会被解析成「评论区是空的」（2026-11-07 就是这么踩到的）。
REPLY_LEGACY = {"code": 0, "data": {"replies": [
    {"content": {"content": "这是老结构里的评论正文"}}]}}
BANNED = {"code": -403, "message": "access denied", "data": None}


class FakeResponse(object):
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeClient(object):
    def __init__(self, body):
        self.body = body
        self.calls = []

    async def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return FakeResponse(self.body)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bilicom_")
        self._old = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "BILI_MODE", "BILI_COOKIE")}
        os.environ["DATA_DIR"] = self.dir
        os.environ["BILI_MODE"] = "meta"
        os.environ.pop("BILI_COOKIE", None)
        bili.MODE = "meta"
        bili.COOKIE = ""
        bili._cache.clear()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        bili.MODE = os.environ.get("BILI_MODE", "off")
        bili.COOKIE = os.environ.get("BILI_COOKIE", "")
        bili._cache.clear()
        shutil.rmtree(self.dir, ignore_errors=True)


class FetchTests(Base):
    def test_without_cookie_it_says_need_login_and_sends_nothing(self):
        c = FakeClient(REPLY)
        got = asyncio.run(bili.fetch_comments(1234, client=c))
        self.assertFalse(got["ok"])
        self.assertIn("登录", got["reason"])
        self.assertEqual(c.calls, [])

    def test_cookie_returns_cleaned_comments(self):
        bili.COOKIE = "SESSDATA=x"
        c = FakeClient(REPLY)
        got = asyncio.run(bili.fetch_comments(1234, limit=3, client=c))
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["items"]), 3)
        self.assertNotIn("evil.example", " ".join(got["items"]))
        self.assertNotIn("\n", got["items"][0])
        self.assertTrue(all(len(x) <= bili.COMMENT_LEN for x in got["items"]))
        self.assertEqual(c.calls[0][1].get("oid"), 1234)

    def test_legacy_reply_shape_also_parses(self):
        bili.COOKIE = "SESSDATA=x"
        got = asyncio.run(bili.fetch_comments(1234, client=FakeClient(REPLY_LEGACY)))
        self.assertTrue(got["ok"], got.get("reason"))
        self.assertEqual(got["items"], ["这是老结构里的评论正文"])

    def test_banned_is_reported_as_failure_not_empty_comments(self):
        bili.COOKIE = "SESSDATA=x"
        got = asyncio.run(bili.fetch_comments(1234, client=FakeClient(BANNED)))
        self.assertFalse(got["ok"])
        self.assertIn("-403", got["reason"])

    def test_empty_reply_list_says_so(self):
        bili.COOKIE = "SESSDATA=x"
        got = asyncio.run(bili.fetch_comments(
            1234, client=FakeClient({"code": 0, "data": {"replies": []}})))
        self.assertFalse(got["ok"])
        self.assertIn("空", got["reason"])


class ReactionTests(Base):
    def setUp(self):
        super().setUp()
        self.prompts = []

        async def fake_llm(msgs, **kw):
            self.prompts.append(msgs)
            return "评论区比视频还热闹。"
        self._llm = b.call_openclaw
        b.call_openclaw = fake_llm

    def tearDown(self):
        b.call_openclaw = self._llm
        super().tearDown()

    def test_comments_enter_prompt_marked_as_unverified(self):
        meta = {"ok": True, "bvid": "BV1", "title": "教程", "owner": "UP",
                "duration": 300, "view": 10, "like": 1, "desc": ""}
        com = {"ok": True, "reason": "", "items": ["跟着做成功了"]}
        got = asyncio.run(b._bili_reaction(meta, {"ok": False, "reason": "没字幕"},
                                            com))
        self.assertTrue(got)
        prompt = self.prompts[0][1]["content"]
        self.assertIn("跟着做成功了", prompt)
        self.assertIn("没核实", prompt)

    def test_no_comments_leaves_the_section_out(self):
        meta = {"ok": True, "bvid": "BV1", "title": "教程", "owner": "UP",
                "duration": 300, "view": 10, "like": 1, "desc": ""}
        asyncio.run(b._bili_reaction(meta, {"ok": False, "reason": "没字幕"},
                                      {"ok": False, "reason": "需要登录 cookie",
                                       "items": []}))
        self.assertNotIn("评论区", self.prompts[0][1]["content"])


class NoteTests(Base):
    def test_note_keeps_count_not_comment_text(self):
        bili.COOKIE = "SESSDATA=x"
        meta = {"ok": True, "bvid": "BV1GJ411x7h7", "aid": 1, "cid": 2,
                "title": "教程", "owner": "UP", "duration": 300, "pubdate": 0,
                "desc": "", "view": 10, "like": 1, "pages": 1, "parts": []}
        rec = bili.save_note(meta, {"ok": False, "reason": "没字幕"}, "有想法",
                             "自主",
                             comments={"ok": True, "items": ["别写进我的记忆"]})
        self.assertEqual(rec["comments_seen"], 1)
        self.assertNotIn("别写进我的记忆", str(rec))


if __name__ == "__main__":
    unittest.main()