# -*- coding: utf-8 -*-
"""运维通道回归锁：它必须完全活在人设链路之外。

最要紧的一条不是"命令能解析"，而是"运维指令不会变成她记忆里主人说的话"。
所以这里既测功能，也测隔离：intercept 消费掉的消息不许触达私聊人格处理。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import ops_channel as oc


def private_event(text, user_id="1000000001"):
    return {"post_type": "message", "message_type": "private", "user_id": user_id,
            "self_id": "999", "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text}


class FakeChannel:
    def __init__(self, connected=True, status="ok"):
        self.connected = connected
        self.status = status
        self.sent = []

    async def call(self, action, params=None, timeout=15.0):
        self.sent.append((action, params))
        return {"status": self.status, "data": {"message_id": 1}}


class OpsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ops_channel_test_")
        self.addCleanup(self.tmp.cleanup)
        self._old_env = {k: os.environ.get(k) for k in
                         ("DATA_DIR", "MASTER_QQ_IDS", "OPS_CHANNEL_ENABLED", "OPS_OWNER_QQ")}
        os.environ["DATA_DIR"] = self.tmp.name
        os.environ["MASTER_QQ_IDS"] = "1000000001"
        os.environ["OPS_CHANNEL_ENABLED"] = "1"
        os.environ["OPS_OWNER_QQ"] = "1000000001"
        self.addCleanup(self._restore_env)
        self.channel = FakeChannel()
        patcher = mock.patch("napcat_channel.channel", self.channel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class CommandMatchTests(OpsTestBase):
    def test_owner_private_prefix_matches(self):
        self.assertEqual(oc.match_command(private_event("/ops status")), ("status", ""))
        self.assertEqual(oc.match_command(private_event("/ops approve M01 先做准入")),
                         ("approve", "M01 先做准入"))
        self.assertEqual(oc.match_command(private_event("/ops")), ("help", ""))

    def test_alternate_prefixes(self):
        self.assertEqual(oc.match_command(private_event("#ops plan")), ("plan", ""))
        self.assertEqual(oc.match_command(private_event("/运维 状态")), ("状态", ""))

    def test_non_owner_never_matches(self):
        self.assertIsNone(oc.match_command(private_event("/ops status", user_id="12345")))

    def test_group_never_matches(self):
        event = private_event("/ops status")
        event["message_type"] = "group"
        self.assertIsNone(oc.match_command(event))

    def test_normal_private_chat_never_matches(self):
        for text in ("在吗", "/opsx status", "ops status", "帮我看看 /ops 是什么", "今天好累"):
            self.assertIsNone(oc.match_command(private_event(text)), text)

    def test_disabled_flag_stops_matching(self):
        os.environ["OPS_CHANNEL_ENABLED"] = "0"
        self.assertIsNone(oc.match_command(private_event("/ops status")))

    def test_extract_text_both_forms(self):
        self.assertEqual(oc.extract_text(private_event("/ops hi")), "/ops hi")
        self.assertEqual(oc.extract_text({"message": "/ops hi", "raw_message": "/ops hi"}), "/ops hi")
        self.assertEqual(oc.extract_text({"message": [
            {"type": "text", "data": {"text": "/ops "}},
            {"type": "image", "data": {"file": "x.jpg"}},
            {"type": "text", "data": {"text": "plan"}}]}), "/ops plan")


class InterceptionIsolationTests(OpsTestBase):
    """核心隔离：运维指令不许进入人格链路。"""

    def test_intercept_consumes_ops_command(self):
        with mock.patch("napcat_channel.channel", self.channel):
            consumed = asyncio.run(oc.intercept(None, private_event("/ops status")))
        self.assertTrue(consumed)
        self.assertTrue(self.channel.sent)
        self.assertIn("【运维】", self.channel.sent[0][1]["message"])

    def test_intercept_passes_through_normal_chat(self):
        consumed = asyncio.run(oc.intercept(None, private_event("今天好累啊")))
        self.assertFalse(consumed)
        self.assertEqual(self.channel.sent, [])

    def test_intercept_does_not_touch_persona_ledgers(self):
        """运维指令处理前后，人格侧的账一个字节都不许变。"""
        persona_dir = os.path.join(self.tmp.name, "persona")
        os.makedirs(persona_dir, exist_ok=True)
        sentinel = os.path.join(persona_dir, "self_model.db")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("untouched")
        before = os.path.getmtime(sentinel)
        asyncio.run(oc.intercept(None, private_event("/ops pause 测试")))
        asyncio.run(oc.intercept(None, private_event("/ops approve M01")))
        with open(sentinel, encoding="utf-8") as f:
            self.assertEqual(f.read(), "untouched")
        self.assertEqual(os.path.getmtime(sentinel), before)
        self.assertFalse(os.path.exists(os.path.join(persona_dir, "proactive_dm.json")))
        self.assertFalse(os.path.exists(os.path.join(persona_dir, "trigger_ledger.json")))

    def test_handler_exception_never_leaks_to_persona(self):
        """处理炸了也要消费掉：漏回去就变成她记忆里的怪话。"""
        with mock.patch.object(oc, "handle_local", side_effect=RuntimeError("boom")):
            consumed = asyncio.run(oc.intercept(None, private_event("/ops status")))
        self.assertTrue(consumed)
        self.assertTrue(any("失败" in p["message"] for _, p in self.channel.sent))

    def test_send_failure_is_reported_not_raised(self):
        self.channel.status = "failed"
        consumed = asyncio.run(oc.intercept(None, private_event("/ops status")))
        self.assertTrue(consumed)

    def test_queued_instruction_says_when_it_will_be_picked_up(self):
        """维护端 只在巡检那几轮取队列，回复得说清等待上限，别让人以为马上办。

        曾经为了压延迟单开一个每 10 分钟的分发任务，空转也要开一次会话，
        光空跑就把 token 烧掉一大截 —— 那条路已经砍掉，改成搭巡检的车。
        """
        with mock.patch("napcat_channel.channel", self.channel):
            asyncio.run(oc.intercept(None, private_event("/ops 把 M01 的准入契约写了")))
        self.assertEqual(len(oc.pending_inbox()), 1)
        text = self.channel.sent[0][1]["message"]
        self.assertIn("巡检", text)
        self.assertIn("1/4/8/14/18/22", text)

    def test_round_hours_is_configurable_and_rejects_junk(self):
        with mock.patch.dict(os.environ, {"OPS_ROUND_HOURS": "2，9, bad, 25, 2"}):
            self.assertEqual(oc.round_hours(), [2, 9])
            self.assertEqual(oc.round_when_text(), "每天 2/9 点")

    def test_round_hours_defaults_to_the_six_fixed_rounds(self):
        with mock.patch.dict(os.environ, {"OPS_ROUND_HOURS": "nonsense"}):
            self.assertEqual(oc.round_hours(), [1, 4, 8, 14, 18, 22])
            self.assertEqual(oc.round_when_text(), "每天 1/4/8/14/18/22 点")

    def test_question_is_marked_not_a_task(self):
        with mock.patch("napcat_channel.channel", self.channel):
            asyncio.run(oc.intercept(None, private_event("/ops 现在是在做M05吗")))
        rows = oc.pending_inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meta"]["kind"], "question")
        self.assertIn("只回答不会动手", self.channel.sent[0][1]["message"])

    def test_command_is_marked_task(self):
        with mock.patch("napcat_channel.channel", self.channel):
            asyncio.run(oc.intercept(None, private_event("/ops 把 M01 的准入契约写了")))
        self.assertEqual(oc.pending_inbox()[0]["meta"]["kind"], "task")

    def test_cancel_removes_a_queued_instruction(self):
        job = oc.enqueue_inbox("/ops 把 M01 写了", source="qq", meta={"kind": "task"})
        reply, done = oc.handle_local("cancel", job, "1000000001")
        self.assertTrue(done)
        self.assertIn("已撤回", reply)
        self.assertEqual(oc.pending_inbox(), [])


class PlanViewTests(OpsTestBase):
    """三端共用一份分桶：批完立刻换组，三处不许各算一套。"""

    PLAN = {"at": "2026-11-12T00:05:32+0800",
            "frontier": [
                {"id": "G01", "kind": "ask", "title": "统一行动提案契约", "why": "算法改动"},
                {"id": "V01", "kind": "auto", "title": "验证", "why": "基础设施"},
                {"id": "R09", "kind": "ask", "title": "被拒的", "why": "算法改动"}],
            "waiting_on": [{"id": "memory_provenance", "need": "更多真实记忆写入"}]}

    def test_pending_moves_to_doable_once_approved(self):
        view = oc.plan_view(self.PLAN, {"approvals": {}})
        self.assertEqual(["V01"], view["doable"])
        self.assertEqual(["G01", "R09"], view["approval"])

        view = oc.plan_view(self.PLAN, {"approvals": {"G01": {"decision": "approve"}}})
        self.assertEqual(["G01", "V01"], view["doable"])
        self.assertEqual(["R09"], view["approval"])

    def test_rejected_disappears_from_both(self):
        view = oc.plan_view(self.PLAN, {"approvals": {"R09": {"decision": "reject"}}})
        self.assertNotIn("R09", view["doable"])
        self.assertNotIn("R09", view["approval"])
        self.assertEqual(["R09"], view["rejected"])

    def test_qq_plan_text_uses_the_live_ledger(self):
        oc.save_state({"approvals": {}, "enabled": True})
        path = os.path.join(self.tmp.name, "ops")
        with mock.patch.object(oc, "_dir", return_value=path):
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "plan.json"), "w", encoding="utf-8") as f:
                json.dump(self.PLAN, f, ensure_ascii=False)
            before, _ = oc.handle_local("plan", "", "1000000001")
            self.assertIn("G01", before.split("待你批准")[1])
            oc.record_decision("G01", "approve", by="1000000001")
            after, _ = oc.handle_local("plan", "", "1000000001")
        self.assertIn("G01", after.split("可推进")[1].split("待你批准")[0])


class LocalCommandTests(OpsTestBase):
    def test_pause_and_resume(self):
        reply, done = oc.handle_local("pause", "先别动算法", "1000000001")
        self.assertTrue(done)
        self.assertFalse(oc.autopilot_enabled())
        self.assertEqual(oc.load_state()["paused_reason"], "先别动算法")
        oc.handle_local("resume", "", "1000000001")
        self.assertTrue(oc.autopilot_enabled())

    def test_approve_and_reject_recorded(self):
        ok, msg = oc.record_decision("M01", "approve", "先做准入契约", by="1000000001")
        self.assertTrue(ok, msg)
        self.assertEqual(oc.load_state()["approvals"]["M01"]["decision"], "approve")
        ok, _ = oc.record_decision("S01", "reject", "再等等")
        self.assertTrue(ok)
        self.assertEqual(oc.load_state()["approvals"]["S01"]["decision"], "reject")

    def test_approve_accepts_full_width_comma(self):
        """主人手机上打的是中文逗号 —— 「M01，S01」必须拆成两个任务号。

        第一版只按 ASCII 空格拆，整串被当成一个不存在的任务号，
        主人的批准就这么静默丢了。
        """
        reply, done = oc.handle_local("approve", "M01，S01", "1000000001")
        self.assertTrue(done)
        approvals = oc.load_state()["approvals"]
        self.assertEqual(approvals["M01"]["decision"], "approve")
        self.assertEqual(approvals["S01"]["decision"], "approve")
        self.assertIn("M01", reply)
        self.assertIn("S01", reply)

    def test_second_id_is_not_swallowed_as_note(self):
        """「M01 S01 备注」里 S01 是任务号，不是备注的一部分。"""
        oc.handle_local("approve", "M01 S01 先做这两个", "1000000001")
        approvals = oc.load_state()["approvals"]
        self.assertEqual(set(approvals), {"M01", "S01"})
        self.assertEqual(approvals["M01"]["note"], "先做这两个")

    def test_bad_id_does_not_block_the_good_one(self):
        reply, _ = oc.handle_local("approve", "M01，ZZ99", "1000000001")
        self.assertIn("M01", oc.load_state()["approvals"])
        self.assertNotIn("ZZ99", oc.load_state()["approvals"])
        self.assertIn("ZZ99", reply)

    def test_reject_also_accepts_several_ids(self):
        oc.handle_local("reject", "M01、S01", "1000000001")
        approvals = oc.load_state()["approvals"]
        self.assertEqual(approvals["M01"]["decision"], "reject")
        self.assertEqual(approvals["S01"]["decision"], "reject")

    def test_approve_without_ids_says_how_to_use_it(self):
        reply, done = oc.handle_local("approve", "", "1000000001")
        self.assertTrue(done)
        self.assertIn("用法", reply)
        self.assertEqual(oc.load_state()["approvals"], {})

    def test_record_decision_tolerates_trailing_separator(self):
        """远端调用和旧客户端可能带着分隔符进来，这一层也要兜住。"""
        ok, msg = oc.record_decision("M01，", "approve")
        self.assertTrue(ok, msg)
        self.assertIn("M01", oc.load_state()["approvals"])

    def test_unknown_task_cannot_be_approved(self):
        ok, msg = oc.record_decision("ZZ99", "approve")
        self.assertFalse(ok)
        self.assertIn("目录", msg)
        self.assertEqual(oc.load_state()["approvals"], {})

    def test_bad_decision_rejected(self):
        ok, _ = oc.record_decision("M01", "maybe")
        self.assertFalse(ok)

    def test_approve_does_not_open_expression_control(self):
        """批准单个任务绝不等于放开整片表达接管。"""
        oc.record_decision("C06", "approve")
        state = oc.load_state()
        self.assertNotIn("expression_control", state)
        self.assertNotIn("stage3", state)

    def test_waiting_marked_and_cleared(self):
        oc.mark_waiting("C01", "还缺 2 次真实行动")
        self.assertIn("C01", oc.load_state()["waiting"])
        oc.clear_waiting("C01")
        self.assertNotIn("C01", oc.load_state()["waiting"])

    def test_status_without_health_snapshot_says_so(self):
        reply, done = oc.handle_local("status", "", "1000000001")
        self.assertTrue(done)
        self.assertIn("还没有跑过任何一轮", reply)
        self.assertIn("云端健康快照还没被自动驾驶写过", reply)

    def test_status_reads_health_snapshot(self):
        oc.save_health({"at": "2026-11-11T12:00:00+0800", "qqbot": "active",
                        "napcat": "running", "disk_free_gb": 28.7, "issues": []})
        reply, _ = oc.handle_local("status", "", "1000000001")
        self.assertIn("qqbot=active", reply)
        self.assertIn("28.7GB", reply)

    def test_plan_without_snapshot_says_so(self):
        reply, _ = oc.handle_local("plan", "", "1000000001")
        self.assertIn("还没有 plan 快照", reply)

    def test_free_text_goes_to_maintainer_inbox(self):
        reply, done = oc.handle_local("把", "M01 的准入契约写了", "1000000001")
        self.assertFalse(done)
        job = oc.enqueue_inbox("/ops 把 M01 的准入契约写了")
        self.assertTrue(job)
        self.assertEqual(len(oc.pending_inbox()), 1)

    def test_help_lists_commands(self):
        reply, done = oc.handle_local("help", "", "1000000001")
        self.assertTrue(done)
        for token in ("status", "approve", "pause", "confirm"):
            self.assertIn(token, reply)


class OutboxTests(OpsTestBase):
    def test_enqueue_and_pending(self):
        rid = oc.enqueue_outbox("【运维】测试报文")
        self.assertTrue(rid)
        rows = oc.pending_outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "【运维】测试报文")

    def test_empty_text_not_enqueued(self):
        self.assertEqual(oc.enqueue_outbox("   "), "")

    def test_worker_sends_and_marks_sent(self):
        oc.enqueue_outbox("第一条")
        oc.enqueue_outbox("第二条")

        async def run():
            task = asyncio.create_task(oc.outbox_worker(None))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not oc.pending_outbox():
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with mock.patch.object(oc, "DRAIN_GAP", 0):
            asyncio.run(run())
        self.assertEqual(len(self.channel.sent), 2)
        self.assertEqual(oc.pending_outbox(), [])
        self.assertEqual(oc.load_state()["counters"]["outbound"], 2)

    def test_long_text_is_chunked(self):
        oc.enqueue_outbox("字" * 2500)

        async def run():
            task = asyncio.create_task(oc.outbox_worker(None))
            for _ in range(300):
                await asyncio.sleep(0.01)
                if not oc.pending_outbox():
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with mock.patch.object(oc, "DRAIN_GAP", 0):
            asyncio.run(run())
        self.assertEqual(len(self.channel.sent), 3)
        self.assertEqual(sum(len(p["message"]) for _, p in self.channel.sent), 2500)

    def test_disconnected_channel_keeps_message_pending(self):
        self.channel.connected = False
        oc.enqueue_outbox("送不出去")

        async def run():
            task = asyncio.create_task(oc.outbox_worker(None))
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())
        self.assertEqual(len(oc.pending_outbox()), 1)

    def test_dead_letter_after_five_tries(self):
        oc.enqueue_outbox("一直失败")
        self.channel.status = "failed"
        for _ in range(6):
            rows = oc.pending_outbox()
            oc._mark_outbox([r["id"] for r in rows], "pending", "retry")
        rows = oc._read_jsonl(oc._outbox_path())
        self.assertEqual(rows[0]["state"], "pending")
        oc._mark_outbox([rows[0]["id"]], "dead", "gave_up")
        self.assertEqual(oc._read_jsonl(oc._outbox_path())[0]["state"], "dead")


class ChallengeTests(OpsTestBase):
    def test_confirm_is_one_time(self):
        token = oc.create_challenge("删除 30 天前的候选账")
        ok, desc = oc.confirm_challenge(token)
        self.assertTrue(ok)
        self.assertIn("候选账", desc)
        ok2, msg = oc.confirm_challenge(token)
        self.assertFalse(ok2)
        self.assertIn("用过", msg)

    def test_unknown_token_rejected(self):
        ok, _ = oc.confirm_challenge("deadbeef")
        self.assertFalse(ok)

    def test_expired_token_rejected(self):
        token = oc.create_challenge("过期操作", ttl_seconds=-1)
        ok, msg = oc.confirm_challenge(token)
        self.assertFalse(ok)
        self.assertIn("过期", msg)

    def test_confirm_command_routes(self):
        token = oc.create_challenge("清空 outbox")
        reply, done = oc.handle_local("confirm", token, "1000000001")
        self.assertTrue(done)
        self.assertIn("放行", reply)


class RoundReportTests(OpsTestBase):
    def test_save_round_and_read_back(self):
        rid = oc.save_round({"id": "r1", "at": "2026-11-11T12:00:00+0800",
                             "summary": "推进 D07", "advanced": ["D07"],
                             "waiting_on": ["action_evidence"]})
        self.assertEqual(rid, "r1")
        self.assertEqual(oc.load_state()["last_round"]["advanced"], ["D07"])
        reply, _ = oc.handle_local("report", "", "1000000001")
        self.assertIn("推进 D07", reply)
        self.assertIn("action_evidence", reply)

    def test_corrupt_state_is_preserved_not_overwritten(self):
        os.makedirs(oc._dir(), exist_ok=True)
        with open(oc._state_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        state = oc.load_state()
        self.assertTrue(state["enabled"])
        self.assertTrue(os.path.exists(oc._state_path() + ".corrupt"))


if __name__ == "__main__":
    unittest.main()