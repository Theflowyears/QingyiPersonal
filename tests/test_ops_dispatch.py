# -*- coding: utf-8 -*-
"""分发器回归锁：QQ 指令怎么变成 维护端 的活，以及空转时怎么省钱。

两条最要命的：
  1. 队列空时必须打印 EMPTY 且退出码 0 —— 高频定时任务靠这个立刻收工，
     一旦这条坏了，每十分钟就要跑一次完整模型判断。
  2. 采集失败绝不能打印 EMPTY —— 那会被读成「没活干」，指令就静默丢了。

本文件写第一版时漏了 patch，直接连上生产发了 8 条测试报文给主人。
所以这里加了模块级硬闸：只要有人试图真连 SSH，整个测试模块立刻炸。
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import ops_dispatch as od
import ops_remote as orm


def _no_network(*a, **k):
    raise AssertionError("分发器测试不许连真实服务器：ops_remote._connect 被调用了")


_GUARD = None


def setUpModule():
    global _GUARD
    _GUARD = mock.patch.object(orm, "_connect", _no_network)
    _GUARD.start()


def tearDownModule():
    if _GUARD is not None:
        _GUARD.stop()


class FakeRemote:
    """按函数名给 canned 返回；记录调用顺序，方便断言「没连就拒绝」这类语义。"""

    def __init__(self, answers=None, plan=None):
        self.answers = answers or {}
        self.plan = plan
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call_ops(self, func, payload=None, timeout=90):
        self.calls.append((func, payload))
        if func not in self.answers:
            return {"ok": False, "error": "remote_failed"}
        ans = self.answers[func]
        return {"ok": True, "value": ans(payload) if callable(ans) else ans}

    def read_json(self, relpath, timeout=60):
        self.calls.append(("read_json", relpath))
        return self.plan


class DispatchTest(unittest.TestCase):
    """所有用例共用一个假远端；side_effect 保证换 self.remote 也生效。"""

    def setUp(self):
        self.remote = FakeRemote()
        patcher = mock.patch.object(od, "_remote", side_effect=lambda: self.remote)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, answers=None, plan=None):
        self.remote = FakeRemote(answers, plan)
        return self.remote

    def cli(self, argv):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = od.main(argv)
        return rc, buf.getvalue().strip()


def row(rid, text="干点活"):
    return {"id": rid, "text": text, "state": "pending", "source": "qq"}


class PeekTests(DispatchTest):
    def test_empty_queue_prints_empty_and_exits_zero(self):
        self.use({"pending_inbox": []})
        rc, out = self.cli(["--peek"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "EMPTY")

    def test_pending_queue_prints_json(self):
        self.use({"pending_inbox": [row("a1")]})
        rc, out = self.cli(["--peek"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["count"], 1)

    def test_collection_failure_is_not_silence(self):
        self.use({})
        rc, out = self.cli(["--peek"])
        self.assertEqual(rc, 3)
        self.assertNotEqual(out, "EMPTY")
        self.assertFalse(json.loads(out)["ok"])

    def test_peek_does_not_claim_anything(self):
        remote = self.use({"pending_inbox": [row("a1")]})
        od.peek()
        self.assertEqual([c[0] for c in remote.calls], ["pending_inbox"])


class ClaimTests(DispatchTest):
    def test_only_granted_ids_are_returned(self):
        """远端可能只放行一部分（别的任务已经抢走了），不能把没抢到的也当自己的。"""
        self.use({"pending_inbox": [row("a1"), row("a2")], "claim_inbox": ["a2"]})
        res = od.claim("ops-cron")
        self.assertTrue(res["ok"])
        self.assertEqual(res["claimed"], ["a2"])
        self.assertEqual([r["id"] for r in res["items"]], ["a2"])

    def test_empty_queue_does_not_call_claim(self):
        remote = self.use({"pending_inbox": []})
        res = od.claim()
        self.assertTrue(res["ok"])
        self.assertEqual(res["claimed"], [])
        self.assertEqual([c[0] for c in remote.calls], ["pending_inbox"])

    def test_claim_failure_is_reported(self):
        self.use({"pending_inbox": [row("a1")]})
        self.assertFalse(od.claim()["ok"])

    def test_owner_is_recorded(self):
        remote = self.use({"pending_inbox": [row("a1")], "claim_inbox": ["a1"]})
        od.claim("ops-nightly")
        self.assertEqual(remote.calls[1][1]["owner"], "ops-nightly")


class FinishReplyTests(DispatchTest):
    def test_finish_passes_result_and_note(self):
        remote = self.use({"finish_inbox": None})
        self.assertTrue(od.finish(["a1"], "failed", "本地测试没过")["ok"])
        self.assertEqual(remote.calls[0], ("finish_inbox",
                                           {"ids": ["a1"], "result": "failed",
                                            "note": "本地测试没过"}))

    def test_reply_goes_to_outbox_not_persona_send(self):
        remote = self.use({"enqueue_outbox": "o1"})
        res = od.reply("干完了")
        self.assertTrue(res["ok"])
        self.assertEqual(remote.calls[0][0], "enqueue_outbox")
        self.assertEqual(remote.calls[0][1]["text"], "干完了")

    def test_main_finish_splits_ids(self):
        remote = self.use({"finish_inbox": None})
        rc, _ = self.cli(["--finish", "a1,a2", "--result", "done", "--note", "ok"])
        self.assertEqual(rc, 0)
        self.assertEqual(remote.calls[0][1]["ids"], ["a1", "a2"])
        self.assertEqual(remote.calls[0][1]["note"], "ok")


class ContextTests(DispatchTest):
    def test_context_carries_state_plan_and_challenges(self):
        self.use({"load_state": {"enabled": True, "approvals": {"M01": "approve"}},
                  "pending_challenges": {"abcd1234": {"description": "清库"}}},
                 plan={"auto_advance": [{"id": "D07"}]})
        out = od.context()
        self.assertTrue(out["state_ok"])
        self.assertEqual(out["state"]["approvals"], {"M01": "approve"})
        self.assertEqual(out["plan"]["auto_advance"][0]["id"], "D07")
        self.assertIn("abcd1234", out["challenges"])

    def test_state_failure_is_flagged_not_hidden(self):
        """状态取不到时必须明说：空 approvals 会被误读成「主人从没批准过」。"""
        self.use({"pending_challenges": {}}, plan={})
        out = od.context()
        self.assertFalse(out["state_ok"])
        self.assertEqual(out["state"], {})


class ChallengeTests(DispatchTest):
    def test_challenge_creates_token_and_notifies_owner(self):
        remote = self.use({"create_challenge": "deadbeef", "enqueue_outbox": "o1"})
        res = od.challenge("删除旧备份目录")
        self.assertTrue(res["ok"])
        self.assertEqual(res["token"], "deadbeef")
        sent = [c for c in remote.calls if c[0] == "enqueue_outbox"]
        self.assertEqual(len(sent), 1)
        self.assertIn("/ops confirm deadbeef", sent[0][1]["text"])
        self.assertIn("删除旧备份目录", sent[0][1]["text"])

    def test_challenge_failure_does_not_send_a_token(self):
        remote = self.use({})
        self.assertFalse(od.challenge("删库")["ok"])
        self.assertEqual([c[0] for c in remote.calls], ["create_challenge"])

    def test_challenge_text_is_not_customer_service_voice(self):
        remote = self.use({"create_challenge": "deadbeef", "enqueue_outbox": "o1"})
        od.challenge("删除旧备份")
        text = [c for c in remote.calls if c[0] == "enqueue_outbox"][0][1]["text"]
        for bad in ("请注意", "建议您", "为您", "感谢您的耐心等待", "吾", "汝", "领命"):
            self.assertNotIn(bad, text)


class CliTests(DispatchTest):
    def test_unknown_mode_exits_two(self):
        rc, out = self.cli(["--nope"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error"], "unknown_mode")

    def test_default_mode_is_peek(self):
        self.use({"pending_inbox": []})
        rc, out = self.cli([])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "EMPTY")


class NoNetworkTests(unittest.TestCase):
    def test_module_guard_really_blocks_ssh(self):
        """防自己把上面那道闸改成永远通过。"""
        with self.assertRaises(AssertionError):
            orm._connect()


if __name__ == "__main__":
    unittest.main()