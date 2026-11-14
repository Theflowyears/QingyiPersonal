"""回归锁：好奇度接到身上以后，桥这一侧的接线。

curiosity.py 自己那 33 条测的是账本算术；这一份测的是「她的好奇怎么变成动作」，
因为最容易出错的地方恰好是接缝：

  · 种子只能从她自己的档案和愿望里长（不许我们替她好奇）；
  · 自己去查必须过**同一道**相关性门 —— 群里那两起事故就是两套门禁不一致，
    公开补话的过了门、私下查的没过，于是字典页被当成知识记了进去；
  · 主动分享是 (b) 口径：只私聊给主人、限速、而且「取走即清」不能白丢在断线上；
  · 配额为 0 时整条行动链要一步都不出去（上线初期就是这个口径）。
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)


# 测试夹具自declare：桥的 MASTER_QQ_IDS 在 import 时求值，
# 必须在 import 前把主人号声明好（生产默认值已改为空，见脱敏基线）。
os.environ.setdefault("MASTER_QQ_IDS", "1000000001")

import qq_openclaw_bridge as b        # noqa: E402
import curiosity as cu                # noqa: E402
import web_search as wsm              # noqa: E402
import knowledge_base as kbm          # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def bridge_src():
    with open(os.path.join(BOT, "qq_openclaw_bridge.py"),
              encoding="utf-8") as f:
        return f.read()


class Harness(unittest.TestCase):
    """把网络、知识库、模型、发消息四件事全部换成假的。

    接线测试只该验证「她的好奇按什么顺序经过哪些门」，
    真发一次请求的话，测的就变成当天的网速了。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="curw_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        cu._reset_cache()
        # 默认把配额放开，各用例再按需关回去
        self._quota = cu.MAX_PER_DAY
        self._share_cap = cu.SHARE_MAX_PER_DAY
        cu.MAX_PER_DAY = 5
        cu.SHARE_MAX_PER_DAY = 5
        self.saved = []
        self.searched = []
        self.sent = []
        self.hub = {"results": None, "relevant": True, "save": True, "kb": []}
        self.ws_backup = dict(b._BRIDGE_WS)
        b._BRIDGE_WS["ws"] = object()
        self._restore = []
        self._patch()

    def swap(self, obj, name, fn):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, fn)

    def tearDown(self):
        for obj, name, orig in reversed(self._restore):
            setattr(obj, name, orig)
        b._BRIDGE_WS.clear()
        b._BRIDGE_WS.update(self.ws_backup)
        cu.MAX_PER_DAY = self._quota
        cu.SHARE_MAX_PER_DAY = self._share_cap
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        cu._reset_cache()

    def _patch(self):
        hub = self.hub

        async def fake_search(q, limit=4, **kw):
            self.searched.append(str(q))
            if hub["results"] is not None:
                return hub["results"]
            return {"ok": True, "source": "fake", "results": [
                {"title": "荒坂塔",
                 "snippet": "荒坂公司是 2077 里的巨型企业，荒坂塔是它在夜之城的"
                            "总部大楼，剧情里被多次攻破。",
                 "url": "u", "siteName": "s", "dateLastCrawled": "2026-11-01"}]}

        def fake_relevance(q, rs):
            return bool(hub["relevant"]) and bool(rs)

        async def fake_judge(key, title, summary, source="", llm=None):
            self.saved.append((str(key), str(summary)[:30]))
            return {"saved": bool(hub["save"]), "reason": ""}

        self.swap(wsm, "search", fake_search)
        self.swap(wsm, "relevance", fake_relevance)
        self.swap(kbm, "judge_and_remember", fake_judge)
        self.swap(kbm, "load", lambda: hub["kb"])

    def stub_llm(self, reply):
        async def fake(msgs, **kw):
            return reply
        self.swap(b, "call_openclaw", fake)

    def stub_send(self):
        async def fake(ws, user_id, text, **kw):
            self.sent.append((str(ws), str(user_id), str(text)))
            return True
        self.swap(b, "send_private_msg", fake)


class SeedSourceTests(Harness):
    # ── 种子来源 ────────────────────────────────
    def test_seeds_only_come_from_her_own_records(self):
        import persona_growth as pg
        import whisper as wp
        # 档案与愿望墙本身各有测试，这里只验「种子从哪两处来」这一根线
        self.swap(pg, "load", lambda: {"curious": ["荒坂塔什么来头"]})
        self.swap(wp, "snapshot", lambda: {"wishes": [
            {"id": "w1", "text": "我想搞懂肉鸽藏品怎么排", "kind": "know"}]})
        specs = b._curiosity_specs(cu)
        topics = " ".join(s["topic"] for s in specs)
        self.assertIn("荒坂塔", topics, specs)
        self.assertIn("肉鸽藏品", topics, specs)
        self.assertTrue(all(s["seed_from"] in ("persona_curious", "wish")
                            for s in specs), specs)

    def test_no_group_message_mining_for_seeds(self):
        # 「群里聊了很多所以我对它好奇」不是她的好奇，是我们替她好奇。
        # 种子的来源必须只有那两处，这条锁住别让以后顺手加了统计热词。
        body = bridge_src()
        body = body[body.index("def _curiosity_specs"):]
        body = body[:body.index("def _curiosity_learn")]
        self.assertNotIn("group_message_cache", body)


class ActionTests(Harness):
    # ── 查：同一道门禁 ─────────────────────────
    def test_lookup_saves_when_relevant(self):
        found, keys = run(b._curiosity_lookup("荒坂塔的建筑原型"))
        self.assertTrue(found)
        self.assertEqual(len(self.saved), 1, self.saved)
        self.assertTrue(keys and keys[0].startswith("kb:"))

    def test_irrelevant_results_are_not_remembered(self):
        # 就是屠洪刚《你》那一类：搜到了但对不上，一个字都不许记
        self.hub["relevant"] = False
        found, keys = run(b._curiosity_lookup("你cos谁"))
        self.assertFalse(found)
        self.assertEqual(self.saved, [])
        self.assertEqual(keys, [])

    def test_her_decline_stops_the_remaining_queries(self):
        # 她判「不值得记」以后别再换词重试 —— 那是替她找理由
        self.hub["save"] = False
        found, keys = run(b._curiosity_lookup("荒坂塔的建筑原型是什么"))
        self.assertTrue(found)
        self.assertEqual(keys, [])
        self.assertEqual(len(self.searched), 1, self.searched)

    def test_learn_records_satisfaction_and_deferred_separately(self):
        cu.note_mention("话题甲", weight=cu.ACT_THRESHOLD + 3)
        b._curiosity_learn("话题甲", True, ["kb:话题甲"])
        self.assertEqual(cu.topic("话题甲")["state"], cu.STATE_SATISFIED)
        b._curiosity_learn("话题乙", False)
        rec = cu.topic("话题乙")
        self.assertEqual(rec["seed_from"], "deferred", "承认不知道才是好奇的来源")
        self.assertEqual(rec["mentions"], 1)

    def test_zero_quota_performs_no_search_at_all(self):
        cu.MAX_PER_DAY = 0
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 20)
        acted = run(b._curiosity_act(cu))
        self.assertEqual(acted, 0)
        self.assertEqual(self.searched, [], "配额 0 就不该碰网络")

    def test_failed_lookup_counts_a_fail_not_a_seed(self):
        self.hub["relevant"] = False
        cu.note_mention("查不到的东西", weight=cu.ACT_THRESHOLD + 2)
        acted = run(b._curiosity_act(cu))
        self.assertEqual(acted, 1)
        rec = cu.topic("查不到的东西")
        self.assertEqual(int(rec.get("fails") or 0), 1, rec)
        self.assertEqual(cu.score_at("查不到的东西"),
                         cu.score_at("查不到的东西"),)
        self.assertTrue(cu.top_candidates(limit=3), '首次失败应保留已有的重试机会')
        for _ in range(cu.MAX_FAILS - 1):
            cu.record_failed('查不到的东西', reason='仍没找到')
        self.assertEqual(cu.top_candidates(limit=3), [], '达到失败上限才退出行动队列')


class ShareTests(Harness):
    def _satisfied(self, topic="荒坂塔"):
        cu.note_mention(topic, weight=cu.ACT_THRESHOLD + 3)
        run(b._curiosity_act(cu))          # 走真链路：查 → 判 → 满足
        self.assertEqual(cu.topic(topic)["state"], cu.STATE_SATISFIED)
        return topic

    def test_sends_one_line_to_master_only_in_private(self):
        topic = self._satisfied()
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "荒坂塔是荒坂公司在夜之城的总部。",
                           "status": "active"}]
        self.stub_llm("荒坂塔原来是荒坂公司在夜之城的总部，"
                      "被攻破好多次还没倒，挺硬的。")
        self.stub_send()
        ok = run(b._curiosity_share(cu))
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertEqual(self.sent[0][1], str(next(iter(b.MASTER_QQ_IDS))))
        self.assertNotIn("搜索", self.sent[0][2])
        self.assertNotIn("查了", self.sent[0][2])

    def test_nothing_is_consumed_when_the_socket_is_down(self):
        # 取走即清：先确认发得出去再取，否则一次断线就把她想说的那句弄丢了
        topic = self._satisfied()
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "荒坂公司在夜之城的总部。", "status": "active"}]
        self.stub_llm("荒坂塔是夜之城那栋。")
        self.stub_send()
        b._BRIDGE_WS["ws"] = None
        self.assertFalse(run(b._curiosity_share(cu)))
        self.assertTrue(cu.take_pending_share(), "断线不该把待发的那条吃掉")

    def test_declined_share_does_not_burn_the_daily_slot(self):
        topic = self._satisfied()
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "荒坂公司在夜之城的总部。", "status": "active"}]
        self.stub_llm("[[不说]]")
        self.stub_send()
        self.assertFalse(run(b._curiosity_share(cu)))
        self.assertEqual(self.sent, [])
        self.assertEqual(cu.shares_today(), 0, "没发出去的那条不算打扰")
        self.assertIsNone(cu.share_candidate(), "但她决定不讲的话题不该一直排着")

    def test_her_declining_marks_it_spoken(self):
        # [[不说]] 是她自己的决定：这条话题算讲过了，以后不再提
        topic = self._satisfied()
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "夜之城的总部大楼。", "status": "active"}]
        self.stub_llm("[[不说]]")
        self.stub_send()
        self.assertFalse(run(b._curiosity_share(cu)))
        self.assertTrue(cu.topic(topic).get("shared"),
                        "她自己决定不讲，却还被反复提")
        self.assertIsNone(cu._data().get("pending_share"))
        self.assertEqual(self.sent, [])

    def test_system_failure_keeps_the_topic_instead_of_muting_her(self):
        # 生成失败（模型挂了/被过滤）不是她的决定，不能算她讲过 ——
        # 否则一次故障就把一件她真搞懂的事永久闭嘴了
        topic = self._satisfied()
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "夜之城的总部大楼。", "status": "active"}]
        self.stub_llm("唔…我脑子突然卡壳了，你再说一遍嘛")     # 兜底句
        self.stub_send()
        self.assertFalse(run(b._curiosity_share(cu)))
        self.assertFalse(cu.topic(topic).get("shared"),
                         "系统故障把她的好奇心永久闭嘴了")
        p = cu._data().get("pending_share") or {}
        self.assertEqual(p.get("topic"), topic, "话题没被放回队列")
        self.assertEqual(int(p.get("tries") or 0), 1)
        self.assertEqual(self.sent, [])

    def test_gives_up_after_max_tries(self):
        topic = self._satisfied()
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "夜之城的总部大楼。", "status": "active"}]
        self.stub_llm("")
        self.stub_send()
        old = cu.MAX_SHARE_TRIES
        cu.MAX_SHARE_TRIES = 2
        try:
            run(b._curiosity_share(cu))
            run(b._curiosity_share(cu))
        finally:
            cu.MAX_SHARE_TRIES = old
        self.assertTrue(cu.topic(topic).get("shared"),
                        "到顶了还一直占着唯一的队列位置")
        self.assertIsNone(cu._data().get("pending_share"))

    def test_two_failure_kinds_are_distinguishable_in_the_log(self):
        # 日志必须能分开这两件事，不然下次仍然只能猜
        src = bridge_src()
        seg = src[src.index("async def _curiosity_share"):]
        seg = seg[:seg.index("async def curiosity_worker")]
        self.assertIn("她自己决定不说", seg)
        self.assertIn("生成失败", seg)

    def test_no_fact_means_no_message(self):
        # 查到了但她判「不值得记」→ 手里没有具体内容，别硬讲
        self.hub["save"] = False
        topic = "荒坂塔的建筑原型是什么"
        cu.note_mention(topic, weight=cu.ACT_THRESHOLD + 3)
        run(b._curiosity_act(cu))
        self.assertEqual(cu.topic(topic)["state"], cu.STATE_SATISFIED)
        self.stub_llm("那个啊，挺有意思的。")
        self.stub_send()
        self.assertFalse(run(b._curiosity_share(cu)))
        self.assertEqual(self.sent, [])

    def test_second_share_needs_a_new_topic(self):
        topic = self._satisfied("荒坂塔")
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "夜之城的总部大楼。", "status": "active"}]
        self.stub_llm("荒坂塔那栋我搞清楚了。")
        self.stub_send()
        self.assertTrue(run(b._curiosity_share(cu)))
        self.assertFalse(run(b._curiosity_share(cu)), "同一条不能讲两遍")

    def test_share_disabled_switch(self):
        topic = self._satisfied()
        self.hub["kb"] = [{"key": topic, "title": "荒坂塔",
                           "summary": "夜之城的总部大楼。", "status": "active"}]
        self.stub_llm("说一句。")
        self.stub_send()
        old = b.CURIOSITY_SHARE_ENABLED
        b.CURIOSITY_SHARE_ENABLED = False
        try:
            self.assertFalse(run(b._curiosity_share(cu)))
            self.assertEqual(self.sent, [])
        finally:
            b.CURIOSITY_SHARE_ENABLED = old


class WorldTopicGateTests(Harness):
    """自主去查的那条路没有模型帮她判 [[不查]]，所以要有一条硬规则。

    现网真实数据里，她的 know 愿望 13 条中有 12 条问的是主人/爸爸/主人
    （「主人刚才打字是不是偷偷笑了一下」）—— 那些是要问人的，
    丢进搜索引擎回来的必然是垃圾，正是群里那两起事故的形状。
    """

    def test_person_topics_are_not_sent_to_the_web(self):
        for t in ("主人睡不着的原因。", "爸爸小时候最害怕的虫子是什么呀",
                  "主人刚才打字的时候，心里其实有一点点舍不得吗？"):
            self.assertFalse(wsm.searchable_topic(t), t)

    def test_world_topics_still_pass(self):
        for t in ("荒坂塔的建筑原型", "赛博朋克2077 荒坂塔",
                  "2026年诺贝尔文学奖得主"):
            self.assertTrue(wsm.searchable_topic(t), t)

    def test_block_reason_distinguishes_person_from_vague(self):
        # 这两件事被她混成一句日志，就是在替她编原因：
        # 「主人刚才是不是偷偷笑了」是要问人，「小狗是谁」是词太泛
        self.assertIn("熟人", wsm.topic_block_reason("主人睡不着的原因。"))
        self.assertIn("泛称", wsm.topic_block_reason("小狗是谁。"))
        self.assertEqual(wsm.topic_block_reason("荒坂塔的建筑原型"), "")

    def test_vague_topic_is_not_logged_as_a_person_question(self):
        import io
        import contextlib
        cu.note_mention("小狗是谁。", weight=cu.ACT_THRESHOLD + 5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run(b._curiosity_act(cu))
        out = buf.getvalue()
        self.assertIn("泛称", out, out)
        self.assertNotIn("想问谁", out, "把泛称说成问人，日志就在撒谎")
        self.assertEqual(self.searched, [])

    def test_person_topic_never_reaches_the_network(self):
        cu.note_mention("主人睡不着的原因。", weight=cu.ACT_THRESHOLD + 5)
        acted = run(b._curiosity_act(cu))
        self.assertEqual(acted, 0)
        self.assertEqual(self.searched, [], "问人的话题一次都不该联网")

    def test_rejected_topic_does_not_eat_the_daily_quota(self):
        # 当天只有一个名额时，「主人为什么睡不着」要是先占掉它，
        # 她真想查的东西就一轮都轮不上 —— 判据必须排在占名额之前
        cu.MAX_PER_DAY = 1
        cu.note_mention("主人睡不着的原因。", weight=cu.ACT_THRESHOLD + 9)
        cu.note_mention("荒坂塔的建筑原型", weight=cu.ACT_THRESHOLD + 1)
        acted = run(b._curiosity_act(cu))
        self.assertEqual(acted, 1, acted)
        self.assertEqual(self.searched and "荒坂塔" in self.searched[0], True,
                         self.searched)
        self.assertEqual(cu.quota_left(), 0)

    def test_person_topic_leaves_the_action_queue(self):
        # 不然它每轮都排第一，白占一个行动名额，别人永远轮不上
        name = "主人睡不着的原因。"
        cu.note_mention(name, weight=cu.ACT_THRESHOLD + 5)
        for _ in range(cu.MAX_FAILS):
            run(b._curiosity_act(cu))
        self.assertEqual(cu.top_candidates(limit=3), [])

    def test_lookup_gate_holds_even_when_called_directly(self):
        # 兴趣巡检也会直接调这条，门禁得长在同一处而不是只长在编排里
        found, keys = run(b._curiosity_lookup("主人今天有没有遇到想吐槽的小事"))
        self.assertFalse(found)
        self.assertEqual(keys, [])
        self.assertEqual(self.searched, [])


class WorkerRegistrationTests(unittest.TestCase):
    def test_worker_announces_even_when_it_does_nothing(self):
        # 默认配额是 0（只攒不动）。一个「安静地什么都不做」的 worker
        # 和一个「挂了」在日志里长得一模一样 —— 知识库空成 19 字节就是这么没被发现的。
        import contextlib
        import io

        n = {"c": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep(_seconds):
            n["c"] += 1
            if n["c"] >= 2:              # 第二轮尾部睡眠时把 worker 收掉
                raise asyncio.CancelledError()

        old_dir = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="curlog_")
        cu._reset_cache()
        asyncio.sleep = fake_sleep
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                try:
                    run(b.curiosity_worker())
                except asyncio.CancelledError:
                    pass
        finally:
            asyncio.sleep = real_sleep
            if old_dir is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = old_dir
            cu._reset_cache()
        self.assertIn("[好奇度] 种子", buf.getvalue(),
                      "worker 一轮不出声，就没人知道它到底在不在")

    def test_worker_registered_in_runtime_task_set(self):
        with open(os.path.join(BOT, "ceylan_runtime.py"), encoding="utf-8") as f:
            src = f.read()
        # 必须带 ws 注册。不带的话她永远发不出主动分享：
        # _BRIDGE_WS["ws"] 要到第一条来话才被填上，而主动开口的场景恰恰是没人找她。
        self.assertIn("ns['curiosity_worker'](ws)", src)

    def test_worker_symbols_exist(self):
        for name in ("curiosity_worker", "_curiosity_specs", "_curiosity_learn",
                     "_curiosity_lookup", "_curiosity_fact_text",
                     "_curiosity_act", "_curiosity_share"):
            self.assertTrue(hasattr(b, name), name)

    def test_search_lane_accounts_curiosity(self):
        # 她在对话里自己开口要查的那次，也必须记进同一个账本，
        # 否则「私下查的」和「当场查的」又是两套记忆
        src = bridge_src()
        lane = src[src.index("async def _lookup_then_reply"):]
        lane = lane[:lane.index("async def _kb_judge")]
        self.assertIn("_curiosity_learn(_ent, True", lane)
        self.assertIn("_curiosity_learn(_ent, False", lane)
        # 两条都得带上「当时是哪几句话」，不然她日后自己去查、去讲还是说不清起因
        self.assertEqual(lane.count("event_ids=_scene_events("), 2, lane)

    def test_interest_lane_shares_one_search_pipeline(self):
        # 兴趣那圈以前自己写了一遍搜索+入库，现在只准留一条链路
        src = bridge_src()
        lane = src[src.index("async def explore_group_interest"):]
        lane = lane[:lane.index("# ===== 悄悄话通道")]
        self.assertIn("await _curiosity_lookup(topic)", lane)
        self.assertNotIn("web_search.search(", lane)


if __name__ == "__main__":
    unittest.main()
