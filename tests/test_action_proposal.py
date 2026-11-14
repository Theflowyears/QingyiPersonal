# -*- coding: utf-8 -*-
"""G01 回归锁：统一行动提案契约（人格内核路线 §6.4 / §7，统一方案 §11.1）。

行为测试覆盖：字段完整性（缺什么给什么码、多条缺失一次给全）、等待是
合法提案、硬约束不可被动机分抵消、私聊收件人—话题主体一致性（阿甲场景：
绑定失败就放弃，绝不退而发给主人）、只读与对外发送提案的区分。

AST 静态锁钉死三件事：契约保持纯函数（不碰路径/账本/网络）、四个 worker
的决策点真的接了契约（不是死代码）、旧发送路径一行没改（consent/outbox
与 curiosity 的发送门仍是权威）。
"""
import ast
import asyncio
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import action_proposal as ap      # noqa: E402
import curiosity as cu            # noqa: E402
import bili_auto as ba            # noqa: E402
import proactive_dm as pdm        # noqa: E402
import social as soc              # noqa: E402

NOW = 2000.0


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _at(day_offset=0, hh=12, mm=0):
    """固定 UTC+8 日期，不让主机时区或当天抽签影响用例。"""
    return (datetime(2026, 11, 7, hh, mm, tzinfo=timezone(timedelta(hours=8)))
            + timedelta(days=day_offset)).timestamp()


def proposal(**over):
    """一份字段齐全、绑定正确的私聊提案（阿甲=1001，主人=9999）。"""
    row = ap.build(
        "proactive_dm", "private_dm", motive="联结", reason="她想找朋友聊聊",
        event_ids=["evt_1"], goal_id="", topic="八抽出货", topic_qq="1001",
        participants=["1001"], recipient_qq="1001", visibility="private",
        scope={"origin": "friend_dm"}, evidence=["evt_1"],
        deadline=NOW + 3600,
        budget={"quota_key": "PROACTIVE_DM_MAX_PER_DAY",
                "max_per_day": 4, "cooldown_seconds": 5400},
        success="私聊真的发出去", failure="生成/发送失败",
        abandon_when="她自愿不说", decision="act")
    row.update(over)
    return row


def check(row, friends=("1001", "9999")):
    return ap.validate(row, friends=list(friends) if friends is not None else None,
                       now=NOW)


class EnvelopeTests(unittest.TestCase):
    """§7.1 信封字段必须显式；缺失给稳定 reason_code，不静默补默认值。"""

    def test_build_exposes_every_envelope_field(self):
        env = ap.build("w", "search")
        for field in ("worker", "kind", "motive", "reason", "event_ids",
                      "goal_id", "topic", "topic_qq", "participants",
                      "recipient_qq", "visibility", "scope", "evidence",
                      "deadline", "budget", "success", "failure",
                      "abandon_when", "decision", "gate_reasons",
                      "hard_blocks", "addressee_conflict", "motivation_score"):
            self.assertIn(field, env, field)

    def test_complete_proposal_is_act_with_no_reasons(self):
        got = check(proposal())
        self.assertEqual("act", got["decision"])
        self.assertEqual([], got["reasons"])
        self.assertFalse(got["hard_blocked"])

    def test_each_missing_field_gets_its_own_code(self):
        cases = [("budget", "missing_budget"),
                 ("success", "missing_success_definition"),
                 ("failure", "missing_failure_definition"),
                 ("abandon_when", "missing_abandon_condition"),
                 ("deadline", "missing_deadline"),
                 ("motive", "missing_motive"),
                 ("reason", "missing_reason"),
                 ("visibility", "missing_visibility"),
                 ("worker", "missing_worker"),
                 ("kind", "missing_kind")]
        for field, code in cases:
            with self.subTest(field=field):
                row = proposal()
                row[field] = None
                got = check(row)
                self.assertEqual("rejected", got["decision"])
                self.assertIn(code, got["reasons"])

    def test_multiple_missing_fields_are_all_named_at_once(self):
        row = proposal()
        for field in ("budget", "success", "failure", "abandon_when"):
            row[field] = None
        got = check(row)
        self.assertEqual("rejected", got["decision"])
        for code in ("missing_budget", "missing_success_definition",
                     "missing_failure_definition", "missing_abandon_condition"):
            self.assertIn(code, got["reasons"])

    def test_invalid_budget_values_get_codes(self):
        for budget, code in [
                ({"quota_key": "", "max_per_day": 4, "cooldown_seconds": 0},
                 "missing_quota_key"),
                ({"quota_key": "K", "max_per_day": "4", "cooldown_seconds": 0},
                 "invalid_budget_max_per_day"),
                ({"quota_key": "K", "max_per_day": -1, "cooldown_seconds": 0},
                 "invalid_budget_max_per_day"),
                ({"quota_key": "K", "max_per_day": 4},
                 "invalid_budget_cooldown")]:
            with self.subTest(code=code):
                got = check(proposal(budget=budget))
                self.assertEqual("rejected", got["decision"])
                self.assertIn(code, got["reasons"])

    def test_unknown_motive_kind_visibility_decision_are_named(self):
        for over, code in [({"motive": "孤独"}, "unknown_motive"),
                           ({"kind": "spam"}, "unknown_kind"),
                           ({"visibility": "world"}, "unknown_visibility"),
                           ({"decision": "maybe"}, "unknown_decision"),
                           ({"decision": ""}, "missing_decision")]:
            with self.subTest(code=code):
                got = check(proposal(**over))
                self.assertEqual("rejected", got["decision"])
                self.assertIn(code, got["reasons"])

    def test_no_source_events_is_an_honest_note_not_a_fake_id(self):
        got = check(proposal(event_ids=[]))
        self.assertEqual("act", got["decision"])
        self.assertIn("no_source_events", got["reasons"])
        self.assertEqual([], got["proposal"]["event_ids"])

    def test_goal_id_may_be_empty(self):
        self.assertEqual("act", check(proposal(goal_id=""))["decision"])
        self.assertEqual("act", check(proposal(goal_id=None))["decision"])


class WaitTests(unittest.TestCase):
    """§6.4：「等待/暂不行动」必须是合法提案结果，不是错误。"""

    def test_wait_is_a_legal_decision(self):
        got = check(proposal(decision="wait", topic_qq="", recipient_qq="",
                             gate_reasons=["quota_exhausted"]))
        self.assertEqual("wait", got["decision"])
        self.assertIn("quota_exhausted", got["reasons"])
        self.assertFalse(got["hard_blocked"])

    def test_wait_still_needs_explicit_fields(self):
        got = check(proposal(decision="wait", budget=None))
        self.assertEqual("rejected", got["decision"])
        self.assertIn("missing_budget", got["reasons"])

    def test_wait_without_gate_reasons_is_still_legal(self):
        got = check(proposal(decision="wait", topic_qq="", recipient_qq=""))
        self.assertEqual("wait", got["decision"])


class HardConstraintTests(unittest.TestCase):
    """§3.2 / §7.2 第 1 条：权限/隐私/能力缺失不能被高动机分抵消。"""

    def test_hard_block_cannot_be_outscored(self):
        got = check(proposal(hard_blocks=["capability_missing"],
                             motivation_score=99999.0))
        self.assertEqual("rejected", got["decision"])
        self.assertTrue(got["hard_blocked"])
        self.assertIn("capability_missing", got["reasons"])

    def test_every_hard_code_rejects_even_a_wait(self):
        for code in ap.HARD_BLOCK_CODES:
            with self.subTest(code=code):
                got = check(proposal(decision="wait", hard_blocks=[code]))
                self.assertEqual("rejected", got["decision"])
                self.assertTrue(got["hard_blocked"])

    def test_motivation_score_never_changes_the_decision(self):
        a = check(proposal(motivation_score=None))
        b = check(proposal(motivation_score=1e9))
        self.assertEqual(a["decision"], b["decision"])
        self.assertEqual(a["reasons"], b["reasons"])
        self.assertEqual(1e9, b["proposal"]["motivation_score"])  # 只登记


class RecipientBindingTests(unittest.TestCase):
    """§7.1 阿甲场景：话题主体是甲，收件人必须是甲；绑定失败就放弃。

    校验依据是结构化 QQ 绑定，不是文本里有没有名字；任何情况下都
    不许把收件人改写成主人 —— 契约里没有「退而发给主人」这条路径。
    """

    def test_topic_subject_must_be_the_recipient(self):
        got = check(proposal(topic_qq="1001", recipient_qq="9999"))
        self.assertEqual("abandon", got["decision"])
        self.assertIn("recipient_topic_mismatch", got["reasons"])
        # 收件人原样保留：放弃就是放弃，绝不悄悄改发给主人或话题主体
        self.assertEqual("9999", got["proposal"]["recipient_qq"])
        self.assertEqual("1001", got["proposal"]["topic_qq"])

    def test_subject_not_in_friends_abandons_without_master_fallback(self):
        got = check(proposal(topic_qq="1001", recipient_qq="1001"),
                    friends=["9999"])
        self.assertEqual("abandon", got["decision"])
        self.assertIn("topic_not_in_friends", got["reasons"])
        self.assertEqual("1001", got["proposal"]["recipient_qq"])

    def test_unverified_binding_abandons(self):
        got = check(proposal(), friends=None)
        self.assertEqual("abandon", got["decision"])
        self.assertIn("friend_binding_unverified", got["reasons"])

    def test_binding_is_structural_not_textual(self):
        # 正文里一个名字都没有，QQ 错配照样放弃；
        # 正文里写着收件人的名字，QQ 错配也照样放弃。
        for text in ("八抽出货这运气是开挂了", "9999 你看这个"):
            with self.subTest(text=text):
                got = check(proposal(topic=text, topic_qq="1001",
                                     recipient_qq="9999"))
                self.assertEqual("abandon", got["decision"])
                self.assertIn("recipient_topic_mismatch", got["reasons"])

    def test_name_conflict_is_only_a_backstop(self):
        got = check(proposal(addressee_conflict="阿甲"))
        self.assertEqual("abandon", got["decision"])
        self.assertIn("addressee_name_conflict", got["reasons"])

    def test_dm_recipient_outside_friend_list_abandons(self):
        got = check(proposal(topic_qq="", recipient_qq="1001"),
                    friends=["2002"])
        self.assertEqual("abandon", got["decision"])
        self.assertIn("recipient_not_in_friends", got["reasons"])

    def test_act_dm_without_recipient_is_rejected(self):
        got = check(proposal(topic_qq="", recipient_qq=""))
        self.assertEqual("rejected", got["decision"])
        self.assertIn("missing_recipient", got["reasons"])

    def test_share_recipient_deferred_to_send_gate_is_a_note(self):
        got = check(proposal(kind="private_share", topic_qq="",
                             recipient_qq=""))
        self.assertEqual("act", got["decision"])
        self.assertIn("recipient_bound_at_send_gate", got["reasons"])

    def test_share_about_a_person_without_recipient_abandons(self):
        got = check(proposal(kind="private_share", topic_qq="1001",
                             recipient_qq=""))
        self.assertEqual("abandon", got["decision"])
        self.assertIn("recipient_topic_mismatch", got["reasons"])


class KindScopeTests(unittest.TestCase):
    """只读提案与对外发送提案必须分得开；期限过了就放弃。"""

    def test_read_kinds_have_no_audience(self):
        for kind in ap.READ_KINDS:
            with self.subTest(kind=kind):
                row = proposal(kind=kind, motive="求知", visibility="internal",
                               topic_qq="", recipient_qq="")
                self.assertEqual("act", check(row)["decision"])
                bad = check(proposal(kind=kind, visibility="internal",
                                     topic_qq="", recipient_qq="1001"))
                self.assertIn("read_kind_has_recipient", bad["reasons"])
                bad2 = check(proposal(kind=kind, visibility="private",
                                      topic_qq="", recipient_qq=""))
                self.assertIn("visibility_kind_mismatch", bad2["reasons"])

    def test_send_kinds_cannot_be_internal(self):
        got = check(proposal(visibility="internal"))
        self.assertEqual("rejected", got["decision"])
        self.assertIn("visibility_kind_mismatch", got["reasons"])

    def test_signature_has_no_recipient(self):
        got = check(proposal(kind="signature", motive="创造表达",
                             visibility="public", topic_qq="",
                             recipient_qq="1001"))
        self.assertIn("unexpected_recipient", got["reasons"])

    def test_expired_deadline_abandons(self):
        got = check(proposal(deadline=NOW - 1))
        self.assertEqual("abandon", got["decision"])
        self.assertIn("deadline_expired", got["reasons"])


class PurityLockTests(unittest.TestCase):
    """AST 锁：契约是纯函数；旧发送路径一行没改。"""

    @classmethod
    def setUpClass(cls):
        with io.open(os.path.join(BOT, "action_proposal.py"), encoding="utf-8") as f:
            cls.src = f.read()
        cls.tree = ast.parse(cls.src)

    def test_contract_only_imports_time(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertEqual("time", alias.name.split(".")[0])
            if isinstance(node, ast.ImportFrom):
                self.fail("契约不该 from-import 任何模块: %s" % node.module)

    def test_no_path_or_ledger_or_network_dependencies(self):
        for marker in ("getenv", "self_model", "open(", "requests",
                       "sqlite", "napcat", "urllib"):
            self.assertNotIn(marker, self.src, marker)

    def test_build_and_validate_stay_pure(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in (
                    "build", "validate", "describe", "_envelope", "_result"):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        name = ast.dump(call.func)
                        self.assertNotIn("print", name, node.name)
                        self.assertNotIn("open", name, node.name)

    def test_observe_is_the_only_side_effect_entry(self):
        self.assertTrue(hasattr(ap, "observe"))
        self.assertTrue(hasattr(ap, "last"))

    def test_old_send_paths_are_untouched(self):
        # 发送/回执/联系记录仍是权威门禁，契约不许长进去
        import inspect
        for fn in (cu.take_pending_share, cu.freeze_share_delivery,
                   cu.finish_share_delivery, cu.record_share,
                   pdm.done, pdm.release, pdm.decline, pdm.mark_contacted,
                   soc._call, soc.get_friends):
            self.assertNotIn("action_proposal", inspect.getsource(fn),
                             fn.__name__)


class WiringStaticTests(unittest.TestCase):
    """AST 锁：四个 worker 的决策点真的调了契约，不是死代码。"""

    def _function(self, module, name):
        path = os.path.join(BOT, module + ".py")
        with io.open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                return node
        self.fail("%s.%s 不存在" % (module, name))

    def _calls_observe(self, node):
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                    and call.func.attr == "observe":
                return True
        return False

    def test_curiosity_decision_points_build_proposals(self):
        for name in ("claim_action", "push_share"):
            node = self._function("curiosity", name)
            self.assertTrue(self._calls_observe(node), name)

    def test_bili_auto_claim_builds_proposal(self):
        self.assertTrue(self._calls_observe(self._function("bili_auto", "claim")))

    def test_proactive_dm_decide_and_claim_build_proposals(self):
        decide = self._function("proactive_dm", "decide")
        self.assertTrue(any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                            and c.func.id == "_observe_decision"
                            for c in ast.walk(decide)))
        self.assertTrue(self._calls_observe(
            self._function("proactive_dm", "_observe_decision")))
        self.assertTrue(self._calls_observe(self._function("proactive_dm", "claim")))

    def test_social_set_signature_builds_proposal(self):
        self.assertTrue(self._calls_observe(
            self._function("social", "set_signature")))


class WorkerBase(unittest.TestCase):
    """行为接线测试的隔离底座：临时目录 + QQ_DRY_RUN，不碰真实 data/。"""

    ENV = ("DATA_DIR", "QQ_DRY_RUN", "BILI_AUTO", "PROACTIVE_DM_ENABLED")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="g01_prop_")
        self._old = {k: os.environ.get(k) for k in self.ENV}
        os.environ["DATA_DIR"] = self.dir
        os.environ["QQ_DRY_RUN"] = "1"
        os.environ.pop("BILI_AUTO", None)
        os.environ.pop("PROACTIVE_DM_ENABLED", None)
        cu._reset_cache()
        pdm._cache.update({"sig": None, "data": None})
        ap._LAST = None

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cu._reset_cache()
        pdm._cache.update({"sig": None, "data": None})
        ap._LAST = None
        shutil.rmtree(self.dir, ignore_errors=True)


class CuriosityWiringTests(WorkerBase):
    def setUp(self):
        super().setUp()
        self._caps = (cu.MAX_PER_DAY, cu.SHARE_MAX_PER_DAY)
        cu.MAX_PER_DAY = 5
        cu.SHARE_MAX_PER_DAY = 5

    def tearDown(self):
        cu.MAX_PER_DAY, cu.SHARE_MAX_PER_DAY = self._caps
        super().tearDown()

    def test_claim_action_registers_a_readonly_search_proposal(self):
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 3)
        with redirect_stdout(io.StringIO()):
            ok = cu.claim_action("荒坂塔")
        self.assertIs(ok, True, "返回形状不变：仍是 bool")
        got = ap.last()
        self.assertEqual("curiosity", got["worker"])
        self.assertEqual("search", got["kind"])
        self.assertEqual("act", got["decision"])
        self.assertEqual("求知", got["proposal"]["motive"])
        self.assertEqual("internal", got["proposal"]["visibility"])
        self.assertEqual("", got["proposal"]["recipient_qq"])
        self.assertEqual("CURIOSITY_MAX_PER_DAY",
                         got["proposal"]["budget"]["quota_key"])
        self.assertTrue(got["proposal"]["success"])
        self.assertTrue(got["proposal"]["failure"])
        self.assertTrue(got["proposal"]["abandon_when"])

    def test_quota_exhaustion_is_registered_as_a_legal_wait(self):
        cu.MAX_PER_DAY = 0
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 3)
        with redirect_stdout(io.StringIO()):
            ok = cu.claim_action("荒坂塔")
        self.assertIs(ok, False)
        got = ap.last()
        self.assertEqual("wait", got["decision"])
        self.assertIn("quota_exhausted", got["reasons"])

    def test_push_share_registers_a_private_share_proposal(self):
        with redirect_stdout(io.StringIO()):
            ok = cu.push_share("荒坂塔", detail="荒坂塔是夜之城的总部。",
                               kind="know", event_ids=["evt_9"],
                               origin_session="private-user-1001",
                               visibility="private", requester_qq="1001",
                               recipient_qq="1001")
        self.assertIs(ok, True)
        got = ap.last()
        self.assertEqual("private_share", got["kind"])
        self.assertEqual("联结", got["proposal"]["motive"])
        self.assertEqual("1001", got["proposal"]["recipient_qq"])
        self.assertEqual("private-user-1001",
                         got["proposal"]["scope"]["origin_session"])
        self.assertIn("evt_9", got["proposal"]["event_ids"])
        self.assertEqual("CURIOSITY_SHARE_MAX_PER_DAY",
                         got["proposal"]["budget"]["quota_key"])
        self.assertEqual("act", got["decision"])

    def test_existing_recipient_gate_still_authoritative(self):
        with redirect_stdout(io.StringIO()):
            cu.push_share("荒坂塔", detail="内容", kind="know",
                          origin_session="private-user-1001",
                          visibility="private", requester_qq="1001",
                          recipient_qq="1001")
            self.assertIsNone(cu.take_pending_share(recipient_qq="9999"),
                              "来源绑定门禁不许被契约放宽")
            self.assertIsNotNone(cu.take_pending_share(recipient_qq="1001"))

    def test_contract_crash_is_loud_but_never_breaks_the_gate(self):
        cu.note_mention("荒坂塔", weight=cu.ACT_THRESHOLD + 3)
        log = io.StringIO()
        with patch.object(ap, "validate", side_effect=RuntimeError("boom")), \
                redirect_stdout(log):
            ok = cu.claim_action("荒坂塔")
        self.assertIs(ok, True, "契约故障不许拖垮既有门禁")
        self.assertIn("契约校验异常", log.getvalue(), "沉默视为故障")


class BiliAutoWiringTests(WorkerBase):
    ITEM = {"bvid": "BV1GJ411x7h7", "title": "明日方舟新角色抽卡实测",
            "category": "游戏", "duration": 613, "view": 123456}

    def test_claim_registers_a_readonly_watch_proposal(self):
        with redirect_stdout(io.StringIO()):
            ok = ba.claim(self.ITEM, _at(hh=10))
        self.assertIs(ok, True)
        got = ap.last()
        self.assertEqual("bili_auto", got["worker"])
        self.assertEqual("watch", got["kind"])
        self.assertIn("watch", ap.READ_KINDS)
        self.assertEqual("act", got["decision"])
        self.assertEqual("internal", got["proposal"]["visibility"])
        self.assertEqual("", got["proposal"]["recipient_qq"])
        self.assertEqual("BILI_AUTO_MAX_PER_DAY",
                         got["proposal"]["budget"]["quota_key"])
        self.assertIn("no_source_events", got["reasons"],
                      "claim 时还没有笔记事件，如实登记而不是补造 ID")

    def test_duplicate_video_is_a_legal_wait(self):
        with redirect_stdout(io.StringIO()):
            self.assertTrue(ba.claim(self.ITEM, _at(hh=10)))
            ok = ba.claim(self.ITEM, _at(hh=14))
        self.assertIs(ok, False)
        got = ap.last()
        self.assertEqual("wait", got["decision"])
        self.assertIn("already_watched", got["reasons"])


class ProactiveDmWiringTests(WorkerBase):
    FRIENDS = ["1001", "1002"]
    SCORES = {"1001": 60, "1002": 20}

    def test_decide_registers_a_structurally_bound_private_dm(self):
        now = _at(hh=12)
        with patch.object(pdm, "plan_day", return_value=[12 * 3600]), \
                redirect_stdout(io.StringIO()):
            d = pdm.decide(now, self.FRIENDS, self.SCORES)
        self.assertTrue(d["ok"])
        self.assertLessEqual(set(d), {"ok", "uid", "reason", "score"},
                             "decide 返回形状不变")
        got = ap.last()
        self.assertEqual("proactive_dm", got["worker"])
        self.assertEqual("private_dm", got["kind"])
        self.assertEqual("act", got["decision"])
        self.assertEqual("联结", got["proposal"]["motive"])
        self.assertEqual(d["uid"], got["proposal"]["recipient_qq"])
        self.assertEqual(d["uid"], got["proposal"]["topic_qq"])
        self.assertNotIn("friend_binding_unverified", got["reasons"],
                         "decide 带着好友表，绑定必须真核验")
        self.assertEqual("PROACTIVE_DM_MAX_PER_DAY",
                         got["proposal"]["budget"]["quota_key"])

    def test_blocked_decide_is_a_legal_wait(self):
        with redirect_stdout(io.StringIO()):
            d = pdm.decide(_at(hh=8), self.FRIENDS, self.SCORES)
        self.assertFalse(d["ok"])
        got = ap.last()
        self.assertEqual("wait", got["decision"])
        self.assertIn("opportunity_gate_closed", got["reasons"])

    def test_claim_registers_reservation_and_keeps_pending_shape(self):
        now = _at(hh=12)
        with patch.object(pdm, "plan_day", return_value=[12 * 3600]), \
                redirect_stdout(io.StringIO()):
            d = pdm.decide(now, self.FRIENDS, self.SCORES)
            pending = pdm.claim(d, now=now)
        self.assertIsNotNone(pending)
        self.assertEqual({"uid", "reason", "ts", "day", "slot", "owner"},
                         set(pending), "claim 返回形状不变")
        got = ap.last()
        self.assertEqual("private_dm", got["kind"])
        self.assertEqual("act", got["decision"])
        self.assertEqual(d["uid"], got["proposal"]["recipient_qq"])
        self.assertIn("friend_binding_unverified", got["reasons"],
                      "预留阶段拿不到好友表，如实登记未核验")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(pdm.release(now=now))

    def test_master_exclusion_still_authoritative(self):
        now = _at(hh=12)
        with patch.object(pdm, "plan_day", return_value=[12 * 3600]), \
                redirect_stdout(io.StringIO()):
            d = pdm.decide(now, ["9999"], {"9999": 99}, exclude={"9999"})
        self.assertFalse(d["ok"], "主人排除门禁不许被契约放宽")
        self.assertEqual("wait", ap.last()["decision"])


class SocialWiringTests(WorkerBase):
    def _stub(self, retcode=0):
        async def fake_profile():
            return {"user_id": "1", "nickname": "清漪"}

        async def fake_call(action, params=None, timeout=15.0):
            return {"retcode": retcode, "message": ""}

        return patch.object(soc, "get_profile", fake_profile), \
            patch.object(soc, "_call", fake_call)

    def test_set_signature_registers_a_public_expression_proposal(self):
        p1, p2 = self._stub()
        with p1, p2, redirect_stdout(io.StringIO()):
            got = run(soc.set_signature("今天也想晒太阳"))
        self.assertTrue(got["ok"])
        self.assertEqual({"ok", "signature", "key"}, set(got),
                         "set_signature 返回形状不变")
        prop = ap.last()
        self.assertEqual("social", prop["worker"])
        self.assertEqual("signature", prop["kind"])
        self.assertEqual("创造表达", prop["proposal"]["motive"])
        self.assertEqual("public", prop["proposal"]["visibility"])
        self.assertEqual("", prop["proposal"]["recipient_qq"])
        self.assertEqual("act", prop["decision"])

    def test_empty_signature_is_a_legal_wait_and_shape_unchanged(self):
        with redirect_stdout(io.StringIO()):
            got = run(soc.set_signature("  "))
        self.assertEqual({"ok": False, "error": "签名为空"}, got)
        prop = ap.last()
        self.assertEqual("wait", prop["decision"])
        self.assertIn("empty_signature", prop["reasons"])

    def test_capability_missing_is_hard_rejected_but_path_unchanged(self):
        p1, p2 = self._stub()
        with p1, p2, patch.dict(soc.CAPABILITIES, {"set_signature": False}), \
                redirect_stdout(io.StringIO()):
            got = run(soc.set_signature("能力缺失也要出声"))
        self.assertTrue(got["ok"], "契约旁路观测，不改发送行为")
        prop = ap.last()
        self.assertEqual("rejected", prop["decision"])
        self.assertTrue(prop["hard_blocked"])
        self.assertIn("capability_missing", prop["reasons"])


if __name__ == "__main__":
    unittest.main()