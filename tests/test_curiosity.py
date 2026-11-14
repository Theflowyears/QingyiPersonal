"""回归锁：好奇度系统。

设计前提（用户定的口径）：
  · 好奇必须是**她的**，种子只能来自她自己的档案、她提的愿望、群里真实热度、
    以及「她说过要查却没能查到」的那些 —— 不许随机造话题；
  · 行动产物默认只在私聊可见；主动分享限 (b)：允许她在 QQ 私聊主动跟主人讲一条，
    但必须有频率上限，否则变成打扰；
  · 查完的东西走知识库判定写入库，不是聊完就忘。
"""
from contextlib import redirect_stdout
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import curiosity as cu       # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cur_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        cu._reset_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        cu._reset_cache()
        shutil.rmtree(self.dir, ignore_errors=True)


class SeedTests(Base):
    def test_seed_from_her_own_curious_list(self):
        seeds = cu.seeds_from_persona({"curious": ["肉鸽藏品怎么排", "荒坂塔什么来头"]})
        self.assertEqual(len(seeds), 2)
        self.assertEqual(seeds[0]["seed_from"], "persona_curious")

    def test_seed_from_wishes_marks_stronger(self):
        wishes = [{"text": "我想搞懂荒坂塔的建筑原型", "kind": "know", "id": "w1"}]
        s = cu.seeds_from_wishes(wishes)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["weight"], cu.W_WISH)

    def test_want_wishes_are_requests_not_curiosity(self):
        # 现网真实形状：「想要」是请主人办事（换头像、拍照），不是想弄懂什么。
        # 把它们当好奇去联网，查回来必然对不上，还白占行动名额。
        wishes = [
            {"text": "爸爸，把我头像里那件换成深蓝色卫衣吧！", "kind": "want", "id": "w1"},
            {"text": "我想搞懂肉鸽藏品怎么排", "kind": "know", "id": "w2"},
        ]
        got = cu.seeds_from_wishes(wishes)
        self.assertEqual([x["topic"] for x in got], ["我想搞懂肉鸽藏品怎么排"])

    def test_deferred_question_becomes_a_seed(self):
        # 她说「等我翻一下」然后什么都没查到 —— 这条信号以前被丢掉
        cu.record_deferred("赤合金到底有什么用", reason="查不到")
        got = [x["topic"] for x in cu.all_topics()]
        self.assertIn("赤合金到底有什么用", got)

    def test_no_seeds_no_topics(self):
        self.assertEqual(cu.all_topics(), [])


class ScoringTests(Base):
    def test_mention_accumulates(self):
        cu.note_mention("荒坂塔", weight=cu.W_MENTION)
        a = cu.score_at("荒坂塔")
        cu.note_mention("荒坂塔", weight=cu.W_MENTION)
        self.assertGreater(cu.score_at("荒坂塔"), a)

    def test_score_decays_over_days(self):
        cu.note_mention("荒坂塔", weight=10.0)
        now = time.time()
        s0 = cu.score_at("荒坂塔", now)
        s30 = cu.score_at("荒坂塔", now + 86400 * 30)
        self.assertLess(s30, s0)

    def test_satisfied_topic_saturates_instead_of_rerunning(self):
        # 查明白之后短期内不该又去查一遍，那会变成复读机
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 1)
        cu.mark_satisfied("荒坂塔", ["kb:abcd"])
        self.assertFalse(cu.should_act("荒坂塔", now=time.time()))
        # 饱和期过了、而且她确实又 interest 起来，才允许再查。
        # 只等时间不够：兴趣分按半衰期衰减，掉下门槛本来就不该再动手。
        later = time.time() + (cu.SATURATION_DAYS + 1) * 86400
        self.assertFalse(cu.should_act("荒坂塔", now=later),
                         "分数已衰减到门槛下，不该行动")
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 5, now=later)
        self.assertTrue(cu.should_act("荒坂塔", now=later), "过了饱和期就该能再查")

    def test_below_threshold_does_not_act(self):
        cu.note_mention("随便一个话题", weight=1.0)
        self.assertFalse(cu.should_act("随便一个话题", now=time.time()))

    def test_faded_state_after_long_idle(self):
        # 很久以前起过念头、一直没行动也没被再提 → 消退（但记录不删）
        stale = time.time() - 86400 * (cu.FADE_DAYS + 5)
        cu.note_mention("旧话题", weight=cu.ACT_THRESHOLD + 1, now=stale)
        cu.sweep(now=time.time())
        self.assertEqual(cu.topic("旧话题")["state"], cu.STATE_FADED)
        self.assertIsNotNone(cu.topic("旧话题"), "消退不该把记录删掉")


class QuotaTests(Base):
    def setUp(self):
        Base.setUp(self)
        # 配额是模块级常量（import 时读 env），这里按用例显式覆盖，
        # 免得测试把「配额是几」当成隐式前提
        self._old_quota = cu.MAX_PER_DAY
        self.addCleanup(setattr, cu, "MAX_PER_DAY", self._old_quota)

    def test_daily_action_quota_is_enforced(self):
        cu.MAX_PER_DAY = 2
        for i in range(6):
            cu.note_mention("话题%02d" % i, weight=cu.ACT_THRESHOLD + 5)
        used = sum(1 for i in range(6)
                   if cu.claim_action("话题%02d" % i, now=time.time()))
        self.assertEqual(used, 2, "配额 2 就该只放行 2 次")
        self.assertEqual(cu.quota_left(now=time.time()), 0)

    def test_quota_is_exhausted_then_denies(self):
        cu.MAX_PER_DAY = 1
        cu.note_mention("占位", weight=cu.ACT_THRESHOLD + 5)
        self.assertTrue(cu.claim_action("占位", now=time.time()))
        cu.note_mention("再多一个", weight=cu.ACT_THRESHOLD + 5)
        self.assertEqual(cu.quota_left(now=time.time()), 0)
        self.assertFalse(cu.claim_action("再多一个", now=time.time()))

    def test_quota_resets_next_day(self):
        cu.MAX_PER_DAY = 1
        now = time.time()
        for tag, ts in (("昨天", now), ("今天", now + 86400 * 2)):
            cu.note_mention(tag, weight=cu.ACT_THRESHOLD + 5)
            self.assertTrue(cu.claim_action(tag, now=ts), tag)
            self.assertEqual(cu.quota_left(now=ts), 0, tag)

    def test_accumulate_only_when_disabled(self):
        # CURIOSITY_MAX_PER_DAY=0 是上线初期的默认口径：只攒分不动手
        cu.MAX_PER_DAY = 0
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 9)
        self.assertTrue(cu.should_act("荒坂塔", now=time.time()))
        self.assertFalse(cu.claim_action("荒坂塔", now=time.time()))
        self.assertIsInstance(cu.quota_left(now=time.time()), int)


class ShareGateTests(Base):
    """(b) 口径：允许她在 QQ 私聊主动跟主人讲一条，但必须限速。"""

    def setUp(self):
        Base.setUp(self)
        # 大多数用例只关心「间隔/同话题不重复」，先把日上限放开，
        # 免得 7 小时跨过日历零点被日上限抢先拒掉，测到的是另一回事
        self._old_share = cu.SHARE_MAX_PER_DAY
        self.addCleanup(setattr, cu, "SHARE_MAX_PER_DAY", self._old_share)
        cu.SHARE_MAX_PER_DAY = 9

    def _satisfy(self, name, weight=None, now=None):
        cu.note_mention(name, weight=weight or cu.ACT_THRESHOLD + 1, now=now)
        cu.claim_action(name, now=now or time.time())
        cu.mark_satisfied(name, ["kb:" + name], now=now or time.time())

    def test_share_allowed_after_satisfying(self):
        self._satisfy("荒坂塔")
        self.assertTrue(cu.should_share(now=time.time()))

    def test_share_respects_min_interval(self):
        self._satisfy("荒坂塔")
        cu.record_share("荒坂塔", now=time.time())
        self.assertFalse(cu.should_share(now=time.time()))
        soon = time.time() + 3600
        self.assertFalse(cu.should_share(now=soon))
        # 过了间隔还得有「没讲过的话题」才谈得上分享 —— 同一条不重复讲
        far = time.time() + (cu.SHARE_MIN_INTERVAL_HOURS + 1) * 3600
        self._satisfy("第二个话题", weight=cu.ACT_THRESHOLD + 5, now=far)
        self.assertTrue(cu.should_share(now=far))

    def test_share_daily_cap(self):
        # 这一条只测日上限：把最小间隔临时放开，
        # 否则同一刻的第二次分享会先被间隔门挡掉，测到的是另一回事
        cu.SHARE_MAX_PER_DAY = 2
        self.addCleanup(setattr, cu, "SHARE_MIN_INTERVAL_HOURS",
                        cu.SHARE_MIN_INTERVAL_HOURS)
        cu.SHARE_MIN_INTERVAL_HOURS = 0
        for i in range(cu.SHARE_MAX_PER_DAY):
            topic = "话题%d" % i
            self._satisfy(topic)
            self.assertTrue(cu.should_share(now=time.time()), topic)
            cu.record_share(topic, now=time.time())
        self.assertFalse(cu.should_share(now=time.time()), "日上限到了就该闭嘴")
        # 第二天又能讲，但要换话题
        tomorrow = time.time() + 86400 * 2
        self._satisfy("第三天的话题", now=tomorrow)
        self.assertTrue(cu.should_share(now=tomorrow))

    def test_same_topic_never_shared_twice(self):
        self._satisfy("荒坂塔")
        cu.record_share("荒坂塔", now=time.time())
        # 很久以后再次满足，同一条话题也不该再主动讲一遍
        far = time.time() + (cu.SATURATION_DAYS + 3) * 86400
        cu.mark_satisfied("荒坂塔", ["kb:y"], now=far)
        cand = cu.share_candidate(far)
        self.assertFalse(cand is not None and cand["topic"] == "荒坂塔",
                         "讲过的话题又排进分享队列了")

    def test_declined_share_frees_the_topic_but_not_the_quota(self):
        # 她自己决定不说的那条：不算打扰，不能扣掉每天唯一的名额，
        # 但也不该一直排在分享队列里反复来问
        self._satisfy("荒坂塔")
        cu.record_share("荒坂塔", now=time.time(), sent=False)
        self.assertEqual(cu.shares_today(now=time.time()), 0)
        self.assertIsNone(cu.share_candidate(now=time.time()))
        self._satisfy("另一个话题")
        self.assertTrue(cu.should_share(now=time.time()))

    def test_no_satisfied_topic_nothing_to_share(self):
        cu.note_mention("还没查的话题", weight=cu.ACT_THRESHOLD + 5)
        self.assertFalse(cu.should_share(now=time.time()))
        self.assertIsNone(cu.take_pending_share())

    def test_contentless_topic_cannot_hog_the_daily_slot(self):
        # 队列只有一个位置。查完却什么都没记住的那条要能排队，
        # 她当天唯一一次「跟主人说点什么」就浪费在讲不出所以然上
        self.assertFalse(cu.push_share("讲了个寂寞", detail="", linked_keys=[]))
        self.assertIsNone(cu._data().get("pending_share"))
        # 但自带内容（刷视频、看图）不算知识库键，照样能排
        self.assertTrue(cu.push_share("刷到一个视频", detail="UP主很会起标题",
                                      kind="watch"))
        self.assertEqual(cu._data()["pending_share"]["kind"], "watch")

    def test_pending_share_is_picked_up_and_cleared(self):
        self._satisfy("荒坂塔")
        self.assertIsNotNone(cu.take_pending_share())
        self.assertIsNone(cu.take_pending_share(), "取走即清，绝不重发")

    def test_topic_without_anything_remembered_is_not_shareable(self):
        # 查到了、但她判「不值得记」—— 脑子里没留下具体说法，没什么可讲
        cu.note_mention("荒坂塔的建筑原型", weight=cu.ACT_THRESHOLD + 3)
        cu.mark_satisfied("荒坂塔的建筑原型", [])
        self.assertIsNone(cu.share_candidate())
        self.assertFalse(cu.should_share())

    def test_contentless_topic_does_not_displace_a_real_one(self):
        # 线上真实跑出来过：先搞懂「荒坂塔」（记 1 条），
        # 再查「肉鸽藏品」（记 0 条），后者把前者顶出队列，结果两条都没讲成
        self._satisfy("荒坂塔的建筑原型")
        good = cu.take_pending_share()
        self.assertIsNotNone(good)
        cu.record_share("荒坂塔的建筑原型", sent=False)
        cu.mark_satisfied("肉鸽藏品怎么排", [])
        self.assertIsNone(cu.share_candidate())
        # 再查明白一条有内容的，它得能排上来
        cu.mark_satisfied("第二个查明白的", ["kb:二"])
        self.assertEqual(cu.share_candidate()["topic"], "第二个查明白的")

    def test_satisfying_a_brand_new_topic_persists(self):
        # 她自己问一句、当场查到答案：这条以前会写在一个已经和磁盘脱钩的
        # dict 上（_touch 落过盘之后 _data() 重读成了新对象），条目静默丢失
        cu.mark_satisfied("全新话题", ["kb:新"], now=time.time())
        self.assertIsNotNone(cu.topic("全新话题"), "新话题满足后账上却没有这条")
        self.assertEqual(cu.topic("全新话题")["linked_keys"], ["kb:新"])
        cu._reset_cache()
        self.assertIsNotNone(cu.topic("全新话题"), "没落盘")

    def test_pending_share_is_not_overwritten(self):
        self._satisfy("荒坂塔的建筑原型")
        first = cu._data().get("pending_share") or {}
        cu.mark_satisfied("别的也搞懂了", ["kb:别"])
        after = cu._data().get("pending_share") or {}
        self.assertEqual(after.get("topic"), first.get("topic"),
                         "一天只能说一条，别把先排上的顶掉")


class ShareRecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, cu, "SHARE_MAX_PER_DAY", cu.SHARE_MAX_PER_DAY)
        cu.SHARE_MAX_PER_DAY = 9

    def test_queue_overflow_refills_without_losing_video_content(self):
        self.assertTrue(cu.push_share("先排的", detail="第一段", kind="watch"))
        self.assertFalse(cu.push_share("后排的", detail="第二段", kind="watch"))
        first = cu.take_pending_share()
        self.assertEqual(first["topic"], "先排的")
        cu.record_share(first["topic"], sent=False)
        cu._reset_cache()
        second = cu.take_pending_share()
        self.assertIsNotNone(second, "队列满时不能把内容丢掉")
        self.assertEqual((second["topic"], second["detail"], second["kind"]),
                         ("后排的", "第二段", "watch"))

    def test_failed_share_can_be_requeued_with_origin_intact(self):
        scope = {"origin_session": "private-user-1001", "visibility": "private",
                 "requester_qq": "1001", "recipient_qq": "1001"}
        self.assertTrue(cu.push_share("私人话题", detail="不能发给别人", **scope))
        first = cu.take_pending_share(recipient_qq="1001")
        self.assertIsNotNone(first)
        self.assertTrue(cu.push_share(first["topic"], detail=first["detail"],
                                      linked_keys=first["linked_keys"],
                                      kind=first["kind"], tries=first["tries"] + 1,
                                      **{k: first[k] for k in scope}))
        cu._reset_cache()
        retry = cu.take_pending_share(recipient_qq="1001")
        self.assertEqual(retry["tries"], 1)
        self.assertEqual(retry["detail"], "不能发给别人")
        self.assertEqual({k: retry[k] for k in scope}, scope)
        self.assertEqual(cu.shares_today(), 0)

    def test_conflicting_scoped_updates_leave_payload_and_queue_atomic(self):
        scope = {"origin_session": "private-user-1001", "visibility": "private",
                 "requester_qq": "1001", "recipient_qq": "1001"}
        conflicts = (
            {"origin_session": "private-user-1002", "visibility": "private",
             "requester_qq": "1002", "recipient_qq": "1002"},
            {"recipient_qq": "1002"},
            {**scope, "visibility": "public"},
        )
        for i, conflicting_scope in enumerate(conflicts):
            with self.subTest(scope=conflicting_scope):
                cu._save(cu._empty())
                name = "同名私人话题%d" % i
                self.assertTrue(cu.push_share("占位%d" % i, detail="先排的"))
                self.assertFalse(cu.push_share(name, detail="A的秘密",
                                               linked_keys=["kb:A"], **scope))
                original = dict(cu.topic(name)["share_payload"])
                head = dict(cu._data()["pending_share"])
                self.assertFalse(cu.push_share(name, detail="B的新秘密",
                                               linked_keys=["kb:B"], kind="photo",
                                               **conflicting_scope))
                cu._reset_cache()
                self.assertEqual(cu.topic(name)["share_payload"], original)
                self.assertEqual(cu._data()["pending_share"], head)
                cu.record_share(cu.take_pending_share()["topic"], sent=False)
                own = cu.take_pending_share(recipient_qq="1001")
                self.assertEqual(own["detail"], "A的秘密")
                self.assertEqual(own["linked_keys"], ["kb:A"])
                self.assertEqual({k: own[k] for k in scope}, scope)
                cu.record_share(name, sent=False)

    def test_private_head_does_not_block_public_alternate_for_master(self):
        cu.push_share("A的私聊", detail="秘密", origin_session="private-user-1001",
                      requester_qq="1001", recipient_qq="1001")
        private_head = dict(cu._data()["pending_share"])
        self.assertFalse(cu.push_share("公开视频", detail="视频细节", kind="watch",
                                       origin_session="group-42", visibility="public"))
        self.assertEqual(cu.share_candidate(recipient_qq="9999")["topic"], "公开视频")
        self.assertEqual(cu.share_block_reason(recipient_qq="9999"), "")
        alternate = cu.take_pending_share(recipient_qq="9999")
        self.assertEqual((alternate["topic"], alternate["detail"], alternate["kind"]),
                         ("公开视频", "视频细节", "watch"))
        cu._reset_cache()
        self.assertEqual(cu._data()["pending_share"], private_head)
        self.assertIsNone(cu.take_pending_share(recipient_qq="9999"))
        cu.record_share(alternate["topic"], sent=False)
        self.assertFalse(cu.should_share(recipient_qq="9999"))
        self.assertEqual(cu.take_pending_share(recipient_qq="1001")["detail"], "秘密")
        self.assertEqual(cu.shares_today(), 0)

    def test_active_share_is_not_recovered_by_same_session_cache_reload(self):
        with mock.patch.object(cu, "_RUNTIME_OWNER", "active-session", create=True):
            cu.push_share("正在处理", detail="不能重复取", kind="watch")
            self.assertIsNotNone(cu.take_pending_share())
            cu._reset_cache()
            self.assertIsNone(cu.take_pending_share(now=time.time() + 2 * 86400))
            self.assertFalse(cu.should_share())
            self.assertEqual(cu.shares_today(), 0)

    def test_fresh_session_recovers_unknown_share_without_success_or_content_loss(self):
        scope = {"origin_session": "private-user-1001", "visibility": "private",
                 "requester_qq": "1001", "recipient_qq": "1001"}
        with mock.patch.object(cu, "_RUNTIME_OWNER", "old-session", create=True):
            cu.push_share("重启中的分享", detail="完整内容", linked_keys=["kb:keep"],
                          kind="photo", tries=2, **scope)
            taken = cu.take_pending_share(recipient_qq="1001")
            cu.push_share("新的队首", detail="也要保留", visibility="public")
        with mock.patch.object(cu, "_RUNTIME_OWNER", "new-session", create=True):
            cu._reset_cache()
            log = io.StringIO()
            with redirect_stdout(log):
                recovered = cu.topic("重启中的分享")
            self.assertFalse(recovered.get("share_taken"))
            self.assertFalse(recovered.get("shared"))
            self.assertEqual(cu.shares_today(), 0)
            self.assertEqual(recovered["share_payload"], taken)
            self.assertIn("unknown", log.getvalue())
            self.assertEqual(cu._data()["pending_share"]["topic"], "新的队首")
            cu.record_share(cu.take_pending_share(recipient_qq="9999")["topic"], sent=False)
            cu._reset_cache()
            self.assertIsNone(cu.take_pending_share(recipient_qq="9999"))
            retry = cu.take_pending_share(recipient_qq="1001")
            self.assertEqual(retry, taken)
            cu._reset_cache()
            self.assertIsNone(cu.take_pending_share(recipient_qq="1001"))
            self.assertEqual(cu.shares_today(), 0)

    def test_legacy_ownerless_taken_share_is_recovered_without_charging(self):
        cu.push_share("旧版占用", detail="旧内容", kind="watch")
        data = cu._data()
        data["topics"][cu._tid("旧版占用")]["share_taken"] = True
        data["pending_share"] = None
        cu._save(data)
        cu._reset_cache()
        retry = cu.take_pending_share()
        self.assertIsNotNone(retry)
        self.assertEqual((retry["topic"], retry["detail"]), ("旧版占用", "旧内容"))
        self.assertEqual(cu.shares_today(), 0)
        self.assertIsNone(cu.take_pending_share())

    def test_legacy_pending_without_topic_record_survives_abandoned_take(self):
        data = cu._data()
        data["pending_share"] = {"topic": "旧队列内容", "detail": "不能丢的旧视频",
                                 "linked_keys": [], "kind": "watch", "tries": 0}
        cu._save(data)
        with mock.patch.object(cu, "_RUNTIME_OWNER", "old-session", create=True):
            self.assertIsNotNone(cu.take_pending_share())
        with mock.patch.object(cu, "_RUNTIME_OWNER", "new-session", create=True):
            cu._reset_cache()
            retry = cu.take_pending_share()
            self.assertIsNotNone(retry)
            self.assertEqual(retry["detail"], "不能丢的旧视频")
            self.assertEqual(cu.shares_today(), 0)

    def test_private_origin_cannot_be_taken_by_unrelated_master(self):
        cu.push_share("私人话题", detail="秘密", origin_session="private-user-1001",
                      visibility="private", requester_qq="1001")
        self.assertIsNone(cu.take_pending_share())
        self.assertIsNone(cu.take_pending_share(recipient_qq="9999"))
        self.assertIsNotNone(cu.take_pending_share(recipient_qq="1001"))

    def test_public_origin_can_be_shared_with_master(self):
        cu.push_share("公开话题", detail="公开视频", origin_session="group-42",
                      visibility="public", requester_qq="1001")
        self.assertTrue(cu.should_share(recipient_qq="9999"))
        self.assertIsNotNone(cu.take_pending_share(recipient_qq="9999"))

    def test_scoped_private_share_gate_matches_take(self):
        cu.push_share("私聊话题", detail="秘密", origin_session="private-user-1001",
                      requester_qq="1001")
        self.assertFalse(cu.should_share(recipient_qq="9999"))
        self.assertTrue(cu.should_share(recipient_qq="1001"))

    def test_conflicting_recipient_update_is_rejected_when_origin_implies_owner(self):
        cu.push_share("来源已绑定", detail="A的内容", linked_keys=["kb:A"],
                      origin_session="private-user-1001", requester_qq="1001")
        original = dict(cu.topic("来源已绑定")["share_payload"])
        cu.take_pending_share(recipient_qq="1001")
        self.assertFalse(cu.push_share("来源已绑定", detail="B的内容",
                                       linked_keys=["kb:B"], recipient_qq="1002"))
        self.assertEqual(cu.topic("来源已绑定")["share_payload"], original)
        self.assertIsNone(cu.take_pending_share(recipient_qq="1002"))
        self.assertIsNone(cu.take_pending_share(recipient_qq="1001"))

    def test_public_retry_without_metadata_preserves_original_scope_and_content(self):
        cu.push_share("公开重试", detail="原视频内容", linked_keys=["kb:video"], kind="watch",
                      origin_session="group-42", visibility="public", requester_qq="1001")
        first = cu.take_pending_share(recipient_qq="9999")
        self.assertTrue(cu.push_share("公开重试", detail="原视频内容", linked_keys=["kb:video"],
                                      kind="watch", tries=1))
        retry = cu.take_pending_share(recipient_qq="9999")
        self.assertEqual(retry["visibility"], "public")
        self.assertEqual(retry["origin_session"], "group-42")
        self.assertEqual(retry["detail"], first["detail"])
        self.assertEqual(retry["linked_keys"], first["linked_keys"])
        self.assertEqual(retry["tries"], 1)

    def test_retry_without_scope_arguments_does_not_erase_private_origin(self):
        cu.push_share("私聊重试", detail="秘密", origin_session="private-user-1001",
                      visibility="private", requester_qq="1001")
        cu.take_pending_share(recipient_qq="1001")
        cu.push_share("私聊重试", detail="秘密", tries=1)
        self.assertIsNone(cu.take_pending_share(recipient_qq="9999"))
        retry = cu.take_pending_share(recipient_qq="1001")
        self.assertEqual(retry["origin_session"], "private-user-1001")
        self.assertEqual(retry["tries"], 1)

    def test_retry_with_only_recipient_cannot_drop_private_provenance(self):
        cu.push_share("部分参数重试", detail="秘密", origin_session="private-user-1001",
                      requester_qq="1001")
        cu.take_pending_share(recipient_qq="1001")
        cu.push_share("部分参数重试", detail="秘密", recipient_qq="9999", tries=1)
        self.assertIsNone(cu.take_pending_share(recipient_qq="9999"))

    def test_explicit_recipient_cannot_override_private_origin(self):
        cu.push_share("错位收件人", detail="秘密", origin_session="private-user-1001",
                      visibility="private", requester_qq="1001", recipient_qq="9999")
        self.assertIsNone(cu.take_pending_share(recipient_qq="9999"))

    def test_legacy_unscoped_share_has_backward_compatible_defaults(self):
        cu.push_share("她自己看的视频", detail="公开内容", kind="watch")
        pending = cu.take_pending_share(recipient_qq="9999")
        self.assertEqual(pending["visibility"], "private")
        self.assertEqual(pending["origin_session"], "")
        self.assertEqual(pending["requester_qq"], "")
        self.assertEqual(pending["recipient_qq"], "")

    def test_satisfied_overflow_preserves_scope_on_refill(self):
        cu.push_share("第一条", detail="公开视频")
        cu.mark_satisfied("私聊查证", ["kb:secret"], origin_session="private-user-1001",
                          visibility="private", requester_qq="1001", recipient_qq="1001")
        first = cu.take_pending_share()
        cu.record_share(first["topic"], sent=False)
        self.assertIsNone(cu.take_pending_share(recipient_qq="9999"))
        second = cu.take_pending_share(recipient_qq="1001")
        self.assertEqual(second["linked_keys"], ["kb:secret"])
        self.assertEqual(second["origin_session"], "private-user-1001")

    def test_shared_or_declined_topic_cannot_be_requeued_next_day(self):
        now = time.time()
        for sent, name in ((True, "已发送"), (False, "已拒绝")):
            cu.push_share(name, detail="原内容", now=now)
            cu.take_pending_share()
            cu.record_share(name, now=now, sent=sent)
            self.assertFalse(cu.push_share(name, detail="重写也不算新话题",
                                           now=now + 2 * 86400))
        self.assertIsNone(cu.take_pending_share())

    def test_later_research_does_not_enqueue_a_shared_topic_again(self):
        now = time.time()
        cu.mark_satisfied("已讲知识", ["kb:old"], now=now)
        cu.take_pending_share()
        cu.record_share("已讲知识", now=now)
        cu.mark_satisfied("已讲知识", ["kb:new"], now=now + 20 * 86400)
        self.assertIsNone(cu.take_pending_share(now=now + 20 * 86400))

    def test_old_pending_payload_loads_with_scope_defaults(self):
        data = cu._data()
        data["pending_share"] = {"topic": "旧队列", "detail": "旧内容",
                                 "linked_keys": [], "kind": "watch", "tries": 0}
        cu._save(data)
        cu._reset_cache()
        pending = cu.take_pending_share(recipient_qq="9999")
        self.assertEqual(pending["origin_session"], "")
        self.assertEqual(pending["visibility"], "private")
        self.assertEqual(pending["detail"], "旧内容")
        self.assertIsNone(cu.take_pending_share())

    def test_daily_cap_retains_content_for_next_day_without_bypassing_gate(self):
        cu.SHARE_MAX_PER_DAY = 1
        now = time.time()
        cu.record_share("昨天讲的", now=now)
        self.assertFalse(cu.push_share("没排上的", detail="还在这里", now=now))
        cu._reset_cache()
        self.assertFalse(cu.should_share(now=now))
        later = now + 2 * 86400
        self.assertTrue(cu.should_share(now=later))
        self.assertEqual(cu.take_pending_share(now=later)["detail"], "还在这里")

    def test_recording_inflight_share_does_not_remove_a_new_pending_share(self):
        cu.push_share("正在发送", detail="第一条")
        cu.take_pending_share()
        cu.push_share("刚排进来", detail="第二条")
        cu.record_share("正在发送", sent=False)
        self.assertEqual(cu.take_pending_share()["topic"], "刚排进来")

    def test_failed_share_survives_requeue_while_slot_is_occupied(self):
        cu.push_share("发送失败", detail="原内容", kind="photo")
        failed = cu.take_pending_share()
        cu.push_share("新排队", detail="新内容")
        self.assertFalse(cu.push_share(failed["topic"], detail=failed["detail"],
                                       kind=failed["kind"], tries=1))
        second = cu.take_pending_share()
        cu.record_share(second["topic"], sent=False)
        retry = cu.take_pending_share()
        self.assertEqual((retry["topic"], retry["detail"], retry["tries"]),
                         ("发送失败", "原内容", 1))


class SeedIdempotencyTests(Base):
    """整点重复灌种子不能变成计数器 —— 这是最容易把废话推过门槛的地方。"""

    def test_repeated_seeds_do_not_accumulate_score(self):
        specs = cu.seeds_from_persona(["荒坂塔什么来头"])
        cu.apply_seeds(specs)
        a = cu.score_at("荒坂塔什么来头")
        for _ in range(6):
            cu.apply_seeds(specs)
        self.assertAlmostEqual(cu.score_at("荒坂塔什么来头"), a, places=2)

    def test_repeated_seeds_do_not_inflate_mentions(self):
        specs = cu.seeds_from_wishes(
            [{"text": "我想搞懂肉鸽藏品", "kind": "know", "id": "w1"}])
        cu.apply_seeds(specs)
        cu.apply_seeds(specs)
        self.assertEqual(cu.topic("我想搞懂肉鸽藏品")["mentions"], 1)

    def test_different_wish_ids_are_different_seeds(self):
        cu.apply_seeds(cu.seeds_from_wishes(
            [{"text": "同一个话题", "kind": "know", "id": "w1"}]))
        cu.apply_seeds(cu.seeds_from_wishes(
            [{"text": "同一个话题", "kind": "know", "id": "w2"}]))
        self.assertEqual(cu.topic("同一个话题")["mentions"], 2)

    def test_top_candidates_sorted_and_gated(self):
        cu.note_mention("小事", weight=cu.W_MENTION)
        cu.note_mention("大事", weight=cu.ACT_THRESHOLD + 3)
        got = cu.top_candidates(limit=5)
        self.assertEqual([x["topic"] for x in got], ["大事"],
                         "没到门槛的不该排进行动候选")

    def test_satisfied_topic_excluded_until_saturation_passes(self):
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 3)
        cu.mark_satisfied("荒坂塔", ["kb:x"])
        self.assertEqual(cu.top_candidates(), [])
        later = time.time() + (cu.SATURATION_DAYS + 2) * 86400
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 9, now=later)
        self.assertEqual([x["topic"] for x in cu.top_candidates(now=later)],
                         ["荒坂塔"])


class FailValveTests(Base):
    """自己查也没查到的话题不能永远排在第一 —— 那是死循环。"""

    def test_claimed_action_cannot_be_claimed_twice(self):
        self.addCleanup(setattr, cu, "MAX_PER_DAY", cu.MAX_PER_DAY)
        cu.MAX_PER_DAY = 9
        cu.note_self_asked("行动中的话题")
        self.assertTrue(cu.claim_action("行动中的话题"))
        self.assertEqual(cu.topic("行动中的话题")["state"], cu.STATE_ACT)
        self.assertFalse(cu.claim_action("行动中的话题"))
        self.assertEqual(cu.quota_left(), 8)

    def test_failed_claim_returns_to_candidates_until_failure_limit(self):
        self.addCleanup(setattr, cu, "MAX_PER_DAY", cu.MAX_PER_DAY)
        cu.MAX_PER_DAY = cu.MAX_FAILS + 1
        cu.note_self_asked("需要重试的话题")
        for attempt in range(cu.MAX_FAILS):
            self.assertTrue(cu.claim_action("需要重试的话题"))
            cu.record_failed("需要重试的话题", reason="工具失败")
            cu._reset_cache()
            rec = cu.topic("需要重试的话题")
            self.assertEqual(rec["fails"], attempt + 1)
            if attempt + 1 < cu.MAX_FAILS:
                self.assertEqual(rec["state"], cu.STATE_BUILD)
                self.assertEqual([c["topic"] for c in cu.top_candidates()],
                                 ["需要重试的话题"])
            else:
                self.assertEqual(rec["state"], cu.STATE_FADED)
                self.assertEqual(cu.top_candidates(), [])

    def test_repeated_failed_actions_stop_queueing(self):
        self.addCleanup(setattr, cu, "MAX_PER_DAY", cu.MAX_PER_DAY)
        cu.MAX_PER_DAY = 9
        cu.note_mention("查不到的话题", weight=cu.ACT_THRESHOLD + 3)
        for i in range(cu.MAX_FAILS):
            self.assertTrue(cu.should_act("查不到的话题"), i)
            self.assertTrue(cu.claim_action("查不到的话题"), i)
            cu.record_failed("查不到的话题", reason="没一条对得上")
        self.assertFalse(cu.should_act("查不到的话题"), "落空够几次就该放过它")
        self.assertEqual(cu.top_candidates(), [])

    def test_failed_action_does_not_add_interest_score(self):
        cu.note_mention("话题甲", weight=cu.ACT_THRESHOLD + 3)
        before = cu.score_at("话题甲")
        for _ in range(3):
            cu.record_failed("话题甲", reason="查不到")
        self.assertAlmostEqual(cu.score_at("话题甲"), before, places=2)

    def test_success_clears_fail_history(self):
        for _ in range(cu.MAX_FAILS):
            cu.record_failed("话题乙")
        cu.note_mention("话题乙", weight=cu.ACT_THRESHOLD + 3)
        self.assertFalse(cu.should_act("话题乙"))
        cu.mark_satisfied("话题乙", ["kb:y"])
        far = time.time() + (cu.SATURATION_DAYS + 2) * 86400
        cu.note_mention("话题乙", weight=cu.ACT_THRESHOLD + 9, now=far)
        self.assertTrue(cu.should_act("话题乙", now=far),
                        "查明白过的事，以前的落空不该一直压着")


class ThresholdShapeTests(Base):
    """门槛和种子权重的关系，比数值本身重要。

    现网踩过一次：ACT_THRESHOLD 被设成和 W_SELF_ASKED 一样的 4.0，
    于是分数 4.0*0.5^(t/14d) 在写入之后的任何时刻都 < 4.0 —— 种子一秒都没活过，
    配额给了也白给，看起来像「她不想动」，其实是配置自己把自己关掉了。
    """

    def test_threshold_is_below_the_strongest_single_seed(self):
        strongest = max(cu.W_SELF_ASKED, cu.W_WISH, cu.W_DEFERRED)
        self.assertLess(cu.ACT_THRESHOLD, strongest,
                        "门槛不低于最强种子，任何单颗种子都永远够不上行动")

    def test_a_fresh_seed_actually_qualifies(self):
        cu.note_self_asked("荒坂塔的建筑原型")
        self.assertTrue(cu.should_act("荒坂塔的建筑原型", now=time.time()))

    def test_a_seed_stays_qualifying_for_days_not_seconds(self):
        # 有效期至少要有一天，否则配额形同虚设
        cu.note_self_asked("肉鸽藏品怎么排")
        now = time.time()
        self.assertTrue(cu.should_act("肉鸽藏品怎么排", now=now))
        self.assertTrue(cu.should_act("肉鸽藏品怎么排", now=now + 86400),
                        "新种子连一天都撑不到，门槛一定贴着权重了")


class ActPlanTests(Base):
    def test_act_needs_keywords_and_returns_plan(self):
        plan = cu.act_plan("荒坂塔的建筑原型是什么")
        self.assertEqual(plan["topic"], "荒坂塔的建筑原型是什么")
        self.assertTrue(plan["queries"], plan)

    def test_her_own_words_are_the_primary_query(self):
        # 话题本来就是模型挑出来的具体说法，先照她的话查，拆词只是兜底
        plan = cu.act_plan("赛博朋克 2077 荒坂塔")
        self.assertEqual(plan["queries"][0], "赛博朋克 2077 荒坂塔")

    def test_queries_are_short_and_bounded(self):
        plan = cu.act_plan("我想搞懂赛博朋克2077里荒坂塔的建筑原型和剧情设定")
        self.assertLessEqual(len(plan["queries"]), cu.MAX_QUERIES_PER_ACT)
        for q in plan["queries"][1:]:
            self.assertLessEqual(len(q), 30)

    def test_plan_records_what_it_answered(self):
        plan = cu.act_plan("荒坂塔", answered=["荒坂塔是荒坂公司总部"])
        self.assertEqual(plan["answered"], ["荒坂塔是荒坂公司总部"])

    def test_empty_topic_still_yields_a_usable_plan(self):
        plan = cu.act_plan("   ")
        self.assertEqual(plan["topic"], "")
        self.assertIsInstance(plan["queries"], list)


if __name__ == "__main__":
    unittest.main()
