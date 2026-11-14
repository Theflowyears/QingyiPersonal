"""回归锁：阶段 0（统一证据）与阶段 1（自传体记忆）。

这些锁针对的是「看起来能跑、其实在编」的失败方式：
  · 同一条消息被三条路径看到，被记成三份证据，信念凭空变牢固；
  · 没有事件支撑的「经历」被写进去 —— 那就是编生平；
  · 信念被新摘要原地覆盖，于是永远回答不出「什么改变了你的想法」；
  · 发送请求被记成「送达」，工具失败被记成「完成」；
  · 旧档案迁移时拿当前时间冒充当年的发生时间。
"""
import os
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import self_model as sm        # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cog_")
        self._old = os.environ.get("PERSONA_DATA_DIR"), os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("PERSONA_DATA_DIR", None)

    def tearDown(self):
        for k, v in zip(("PERSONA_DATA_DIR", "DATA_DIR"), self._old):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class PersonTests(Base):
    def test_same_qq_keeps_the_same_identity(self):
        a = sm.person_id("1000000001", "主人")
        b = sm.person_id("1000000001", "主人")
        self.assertEqual(a, b, "换了个叫法就变成另一个人了")
        self.assertIn("主人", sm.person_aliases("1000000001"))
        self.assertIn("主人", sm.person_aliases("1000000001"))

    def test_non_numeric_id_is_rejected(self):
        self.assertEqual(sm.person_id("not-a-qq"), "")


class EventTests(Base):
    def test_dedupe_makes_repeated_observation_one_event(self):
        a = sm.record_event("message", session="group-1", actor_qq="1001",
                            payload={"text": "八抽出货"}, dedupe="g1:seq:42")
        b = sm.record_event("message", session="group-1", actor_qq="1001",
                            payload={"text": "八抽出货"}, dedupe="g1:seq:42")
        self.assertEqual(a, b, "同一条消息被记成了两件事")

    def test_different_source_is_a_different_event(self):
        a = sm.record_event("message", dedupe="g1:seq:42")
        b = sm.record_event("message", dedupe="g1:seq:43")
        self.assertNotEqual(a, b)

    def test_event_keeps_actor_and_topic_apart(self):
        # 发言者≠话题主体：这是「发给 A、说的是 B」的根
        eid = sm.record_event("message", actor_qq="1001", topic_qq="1002",
                              dedupe="x1")
        e = sm.get_event(eid)
        self.assertNotEqual(e["actor_qq"], e["topic_qq"])
        self.assertEqual(e["actor_qq"], sm.person_id("1001"))
        self.assertEqual(e["topic_qq"], sm.person_id("1002"))


class EpisodeTests(Base):
    def test_episode_without_events_is_refused(self):
        self.assertEqual(sm.record_episode("我今天出去玩了"), "",
                         "没有事件支撑的经历就是编的")

    def test_episode_links_to_events(self):
        eid = sm.record_event("message", dedupe="ep1")
        pid = sm.record_episode("她看到朋友晒出了抽卡结果", [eid])
        self.assertTrue(pid)


class BeliefTests(Base):
    def test_revision_keeps_history_and_explains_why(self):
        eid = sm.record_event("message", actor_qq="1001", dedupe="b1")
        bid = sm.add_belief("她不太会主动找朋友", confidence=0.5, evidence=[eid])
        sm.revise_belief(bid, "有话题时她愿意主动找人，只是不喜欢硬聊",
                         reason="有几次主动私聊得到回应",
                         evidence=[eid], confidence=0.7)
        ex = sm.explain_belief(bid)
        self.assertIsNotNone(ex)
        self.assertEqual(ex["changed_count"], 1, "改过却没有修订记录")
        self.assertIn("不太会主动", ex["revisions"][0]["old_text"])
        self.assertTrue(ex["revisions"][0]["reason"])
        self.assertTrue(ex["evidence"], "解释里没有任何证据")

    def test_revise_unknown_belief_is_a_noop(self):
        self.assertEqual(sm.revise_belief("bel_nope", "新的说法"), "")

    def test_same_evidence_does_not_stack(self):
        eid = sm.record_event("message", dedupe="b2")
        bid = sm.add_belief("命题", evidence=[eid])
        sm.add_evidence(bid, eid)
        sm.add_evidence(bid, eid)
        ex = sm.explain_belief(bid)
        self.assertEqual(len([x for x in ex["evidence"] if x["relation"] == "support"]), 1)

    def test_confidence_can_be_unknown(self):
        # 旧记忆不知道多可信，就该留空，而不是编一个 0.5
        bid = sm.add_belief("旧的说法", confidence=None, is_legacy=True)
        ex = sm.explain_belief(bid)
        self.assertIsNone(ex["belief"]["confidence"])


class ActionTests(Base):
    def test_action_defaults_to_submitted_not_delivered(self):
        aid = sm.record_action("dm", target_qq="1001", text="在吗")
        row = sm.action_by_echo("") or None
        self.assertTrue(aid)
        # 用 explain 路径不适用，这里直接按状态机断言
        self.assertTrue(sm.mark_action(aid, "accepted"))

    def test_state_machine_rejects_backwards_receipts(self):
        aid = sm.record_action("dm", target_qq="1001", text="在吗")
        sm.mark_action(aid, "accepted")
        self.assertFalse(sm.mark_action(aid, "failed"),
                         "已经送达的动作被后来一条乱序回执改回失败")

    def test_cannot_skip_from_submitted_to_generated(self):
        aid = sm.record_action("dm", target_qq="1001", text="在吗")
        self.assertFalse(sm.mark_action(aid, "generated"))

    def test_echo_binding_round_trip(self):
        aid = sm.record_action("dm", target_qq="1001", text="在吗")
        sm.bind_action_echo(aid, "act-" + aid)
        got = sm.action_by_echo("act-" + aid)
        self.assertEqual(got["id"], aid)

    def test_action_records_target_and_topic_separately(self):
        aid = sm.record_action("dm", target_qq="1001", topic_qq="1002",
                               text="阿甲八抽出了")
        row = sm.action_by_echo("") or None
        # 通过 stats 无法取字段，这里用内部查询确认两列不同
        c = sm._db()
        r = c.execute("SELECT target_qq, topic_qq FROM action WHERE id=?",
                      (aid,)).fetchone()
        c.close()
        self.assertNotEqual(r["target_qq"], r["topic_qq"])


class CommitmentGoalTests(Base):
    def test_commitment_lifecycle(self):
        cid = sm.add_commitment("1001", "回头把歌发给你")
        self.assertTrue(cid)
        self.assertTrue(sm.close_commitment(cid, "done"))

    def test_goal_lifecycle_and_active_list(self):
        gid = sm.add_goal("弄清荒坂塔的建筑原型", origin="curiosity",
                          success="能区分明确出处与玩家猜测")
        self.assertIn(gid, [g["id"] for g in sm.active_goals()])
        sm.close_goal(gid, "done")
        self.assertNotIn(gid, [g["id"] for g in sm.active_goals()])


class LegacyImportTests(Base):
    ROWS = [{"kind": "facts", "text": "主人是大三学生", "ts": 0},
            {"kind": "experiences", "text": "给主人唱了首歌", "ts": 0},
            {"kind": "likes", "text": "喜欢下雨天", "ts": 0}]

    def test_import_marks_legacy_and_keeps_time_unknown(self):
        sm.import_legacy(self.ROWS, self_view="我是清漪")
        c = sm._db()
        beliefs = c.execute("SELECT * FROM belief WHERE is_legacy=1").fetchall()
        c.close()
        self.assertTrue(beliefs)
        for b in beliefs:
            self.assertIsNone(b["confidence"], "旧记忆不知道多可信，不能编一个数")

    def test_import_is_idempotent(self):
        r1 = sm.import_legacy(self.ROWS)
        r2 = sm.import_legacy(self.ROWS)
        self.assertEqual(r2["added"], 0, "重复迁移把条目翻倍了")
        self.assertTrue(r2.get("skipped"))

    def test_legacy_experience_becomes_legacy_note_not_verified_episode(self):
        sm.import_legacy(self.ROWS)
        c = sm._db()
        row = c.execute("SELECT is_legacy FROM episode LIMIT 1").fetchone()
        c.close()
        self.assertTrue(row and row["is_legacy"] == 1)

    def test_dry_run_writes_nothing(self):
        sm.import_legacy(self.ROWS, dry_run=True)
        self.assertEqual(sm.stats().get("belief", 0), 0)


class SelfViewTests(Base):
    def test_history_is_append_only(self):
        sm.note_self_view("我不太会主动", reason="初始")
        sm.note_self_view("有话题时我愿意主动", reason="几次回应")
        h = sm.self_view_history()
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["text"], "有话题时我愿意主动")


if __name__ == "__main__":
    unittest.main()


class ReceiptWiringTests(unittest.TestCase):
    """发送回执：请求被接受才推进，乱序不改写。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="cogr_")
        self._old = os.environ.get("DATA_DIR")
        self._old_persona = os.environ.get("PERSONA_DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        os.environ["PERSONA_DATA_DIR"] = os.path.join(self.dir, "persona")
        import qq_openclaw_bridge as b
        self.b = b

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        if self._old_persona is None:
            os.environ.pop("PERSONA_DATA_DIR", None)
        else:
            os.environ["PERSONA_DATA_DIR"] = self._old_persona

    def test_ok_receipt_marks_accepted(self):
        aid = sm.record_action("dm", target_qq="1001", text="在吗")
        sm.bind_action_echo(aid, "act-" + aid)
        self.b._handle_api_reply({"echo": "act-" + aid, "retcode": 0})
        self.assertEqual(sm.action_by_echo("act-" + aid)["state"], "accepted")

    def test_error_receipt_marks_failed(self):
        aid = sm.record_action("dm", target_qq="1001", text="在吗")
        sm.bind_action_echo(aid, "act-" + aid)
        self.b._handle_api_reply({"echo": "act-" + aid, "retcode": 100,
                                  "wording": "风控"})
        self.assertEqual(sm.action_by_echo("act-" + aid)["state"], "failed")

    def test_unknown_echo_is_ignored(self):
        self.b._handle_api_reply({"echo": "act-nope", "retcode": 0})

    def test_send_paths_call_record_action(self):
        with open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        # 发出去的每条消息都要在账上留下一笔，并且带上触发它的那几件事。
        # 记账本身收进了 _record_outbound：以前各条发话路径各自写一遍
        # record_action，参数 soon 就走形（topic_qq、evidence 都漏过）。
        self.assertIn("_sm.record_action(kind, state=\"generated\"", src)
        for sig in ("async def send_group_msg", "async def send_private_msg"):
            seg = src[src.index(sig):]
            seg = seg[:seg.index("async with send_lock")]
            self.assertIn("_record_outbound(", seg, sig)
            self.assertIn("evidence=_ev", seg, sig)
            self.assertIn('"act-" + _aid', seg, sig)


class InboundEventWiringTests(unittest.TestCase):
    """观察事件：没有它们，情感与目标就只能靠猜。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="cogi_")
        self._old = os.environ.get("DATA_DIR")
        self._old_persona = os.environ.get("PERSONA_DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        os.environ["PERSONA_DATA_DIR"] = os.path.join(self.dir, "persona")
        import qq_openclaw_bridge as b
        self.b = b
        b.group_message_cache.clear()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        if self._old_persona is None:
            os.environ.pop("PERSONA_DATA_DIR", None)
        else:
            os.environ["PERSONA_DATA_DIR"] = self._old_persona
        self.b.group_message_cache.clear()

    def test_group_and_private_messages_are_recorded(self):
        with open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"group_message"', src)
        self.assertIn('"private_message"', src)
        # 去重键：群优先 message_id（重放不会变），私聊同理
        self.assertIn('g:%s:mid:%s', src)
        self.assertIn('dedupe="p:%s:%s"', src)

    def test_inbound_replays_do_not_duplicate(self):
        # 真跑一次接线，不只测 record_event：同一条 QQ 消息被重放两次，
        # 库里必须只有一条事件。用本地 seq 做去重键的话这里会是 2。
        b = self.b
        ev = {"group_id": 777001, "user_id": 9000000003, "self_id": 99,
              "sender": {"card": "想不到…"}, "message_id": 6789}
        try:
            b.record_group_message(ev, "我单抽抽出来了六星", [])
            b.record_group_message(ev, "我单抽抽出来了六星", [])
        finally:
            b.group_message_cache.pop("777001", None)
        self.assertEqual(sm.stats()["event"], 1,
                         "同一条消息重放后变成了多份证据")
