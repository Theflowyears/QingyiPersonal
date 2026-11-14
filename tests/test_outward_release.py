# -*- coding: utf-8 -*-
"""G04 回归锁：对外行动灰度与结果确认（统一方案 §11.1 / 待办 G04）。

钉死的行为（每条判据正反例）：
  · 灰度只认人工签字：无签字/形状坏/缺凭证/过期/撤回一律 observe-only；
    逐 kind 单独授权，批了私聊不等于批了签名；模块永不自己授权；
  · 结果确认：retcode=0/ok=True 最多 sent；delivered 必须显式确认；
    异常/缺失/读不懂的回执一律 unknown；unknown 永不重发（resend 恒 False）；
    失败不记完成；明确失败/拒绝/放弃是终态，之后的「成功」回执不能改写；
  · G01 裁决透传：硬约束 rejected、绑定失败 abandon、收件人不改写；
  · 未授权 kind 的结果照常确认但登记 kind_not_granted 供审计；
  · 纯函数：now 必填、内容寻址、同输入同输出、输入顺序无关、输入不被修改；
    空灰度/空结果合法（allow_wait 恒 True，不为凑次数发言）。

AST 锁钉死「复用而非重造」：import 面只含 stdlib + G01/A03 冻结前置；必须
调用 ap.validate 与 mm.digest；绝不出现 ap.observe/ap.build；不 import
bridge/self_model/os/时钟/随机/网络/发送器；零写入、无墙钟；now 是无默认值
kwonly；LIMITATIONS 如实声明不接线/不自授权/不冒充已测真实发送链。

所有合成数据只在内存里，是测试夹具不是自然样本；绝不写真实 data/。
"""
import ast
import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import outward_release as g04        # noqa: E402
import action_proposal as ap         # noqa: E402
import memory_manifest as mm         # noqa: E402

NOW = 4000000000.0
PEER = "1001"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def outward(kind="private_dm", **over):
    base = ap.build(
        "proactive_dm", kind, motive="联结", reason="把查到的资料分享给她",
        event_ids=["evt_1"], topic="荒坂塔", topic_qq=PEER,
        recipient_qq=PEER, visibility="private",
        deadline=NOW + 86400.0,
        budget={"quota_key": "PROACTIVE_DM_MAX_PER_DAY",
                "max_per_day": 2, "cooldown_seconds": 3600.0},
        success="对方收到并可以回应",
        failure="发送失败或回执不明",
        abandon_when="收件人绑定失败就放弃这条",
        decision="act")
    if kind in ("signature", "group_message"):
        base["visibility"] = "public" if kind == "signature" else "group"
        base["recipient_qq"] = ""
        base["topic_qq"] = ""
    base.update(over)
    return base


def signoff(kinds=("private_dm",), **over):
    granted = {k: {"by": "1000000001", "at": NOW - 60} for k in kinds}
    base = {"granted": granted, "revoked": []}
    base.update(over)
    return base


def record(kind="private_dm", receipt=None, prior=None, **over):
    row = {"proposal": outward(kind, **over), "receipt": receipt}
    if prior is not None:
        row["prior_result"] = prior
    return row


def confirm(records=None, **kw):
    base = dict(now=NOW, friends=[PEER])
    base.update(kw)
    return g04.confirm_results(records=records, **base)


# ── AST 静态锁 ───────────────────────────────────────────────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "outward_release.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def calls(self):
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)]

    def attr_pairs(self):
        return {(n.func.value.id, n.func.attr) for n in self.calls()
                if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}

    def imported(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_import_surface_is_frozen_prerequisites_only(self):
        self.assertEqual(self.imported(),
                         {"copy", "json", "math", "action_proposal", "memory_manifest"})

    def test_reuses_g01_validate_and_a03_digest(self):
        pairs = self.attr_pairs()
        self.assertIn(("ap", "validate"), pairs)
        self.assertIn(("mm", "digest"), pairs)

    def test_never_registers_or_completes_envelopes_for_workers(self):
        pairs = self.attr_pairs()
        self.assertNotIn(("ap", "observe"), pairs)
        self.assertNotIn(("ap", "build"), pairs)

    def test_no_wall_clock_no_random_no_io_no_send_path(self):
        banned = {"os", "time", "random", "sqlite3", "pathlib", "subprocess",
                  "socket", "requests", "urllib", "bridge", "self_model",
                  "llm_router", "qq_openclaw_bridge", "napcat_channel",
                  "ops_channel"}
        self.assertFalse(self.imported() & banned)
        for token in ("time.time(", "random.", "open(", "DATA_DIR",
                      "os.environ", "send_private_msg", "set_signature"):
            self.assertNotIn(token, self.source)

    def test_now_is_required_kwonly_without_default(self):
        for name in ("release_gate", "confirm_results"):
            fn = next(n for n in ast.walk(self.tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            kwonly = {a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults)}
            self.assertIn("now", kwonly, name)
            self.assertIsNone(kwonly["now"], name)

    def test_limitations_declare_no_self_authorization_no_wiring(self):
        text = " ".join(g04.LIMITATIONS)
        for token in ("永不自己授权", "不接", "不冒充", "consent", "unknown"):
            self.assertIn(token, text)


# ── 灰度门：只认人工签字，逐 kind 单独授权 ───────────────────────


class ReleaseGateTests(unittest.TestCase):
    def test_now_is_required(self):
        with self.assertRaises(TypeError):
            g04.release_gate()
        with self.assertRaises(ValueError):
            g04.release_gate(now="今晚")

    def test_no_signoff_means_observe_only(self):
        got = g04.release_gate(now=NOW)
        self.assertFalse(got["active"])
        for kind in g04.OUTWARD_KINDS:
            row = got["per_kind"][kind]
            self.assertFalse(row["granted"], kind)
            self.assertIn("no_human_signoff", row["reasons"], kind)
        self.assertTrue(got["allow_wait"])

    def test_signoff_is_per_kind_not_blanket(self):
        got = g04.release_gate(now=NOW, signoff=signoff(("private_dm",)))
        self.assertTrue(got["active"])
        self.assertTrue(got["per_kind"]["private_dm"]["granted"])
        for kind in ("private_share", "signature", "group_message"):
            self.assertFalse(got["per_kind"][kind]["granted"], kind)
            self.assertIn("kind_not_granted", got["per_kind"][kind]["reasons"], kind)

    def test_missing_evidence_blocks_even_if_granted_flag_present(self):
        bad = signoff()
        bad["granted"]["private_dm"] = {"by": "", "at": NOW - 60}
        got = g04.release_gate(now=NOW, signoff=bad)
        self.assertFalse(got["active"])
        self.assertIn("missing_signoff_evidence",
                      got["per_kind"]["private_dm"]["reasons"])

    def test_expired_signoff_is_observe_only(self):
        expired = signoff()
        expired["granted"]["private_dm"]["expires_ts"] = NOW - 1
        got = g04.release_gate(now=NOW, signoff=expired)
        self.assertFalse(got["active"])
        self.assertIn("signoff_expired", got["per_kind"]["private_dm"]["reasons"])
        boundary = signoff()
        boundary["granted"]["private_dm"]["expires_ts"] = NOW + 1
        self.assertTrue(g04.release_gate(now=NOW, signoff=boundary)["active"])

    def test_revocation_closes_immediately(self):
        revoked = signoff(("private_dm", "signature"), revoked=["private_dm"])
        got = g04.release_gate(now=NOW, signoff=revoked)
        self.assertFalse(got["per_kind"]["private_dm"]["granted"])
        self.assertTrue(got["per_kind"]["signature"]["granted"])

    def test_unreadable_signoff_degrades_and_stays_closed(self):
        got = g04.release_gate(now=NOW, signoff="账本坏了")
        self.assertFalse(got["active"])
        self.assertTrue(any(d["reason"] == "unreadable" for d in got["degradation"]))

    def test_read_kinds_are_not_outward_scope(self):
        got = g04.release_gate(now=NOW, signoff=signoff(("search",)),
                               kinds=("search", "private_dm"))
        self.assertIn("not_outward_kind", got["per_kind"]["search"]["reasons"])
        self.assertFalse(got["per_kind"]["search"]["granted"])

    def test_gate_is_deterministic_and_content_addressed(self):
        a = g04.release_gate(now=NOW, signoff=signoff())
        b = g04.release_gate(now=NOW, signoff=signoff())
        self.assertEqual(canonical(a), canonical(b))
        self.assertTrue(a["release_id"].startswith("g4_"))
        json.dumps(a, allow_nan=False)

    def test_downstream_gates_stay_authoritative(self):
        got = g04.release_gate(now=NOW, signoff=signoff())
        gates = got["provenance"]["downstream_gates"]
        for name in ("consent", "outbox", "unknown_no_resend", "source_scope"):
            self.assertIn(name, gates)


# ── 结果确认：发出≠收到，unknown 永不升级 ────────────────────────


class ConfirmResultsTests(unittest.TestCase):
    def test_empty_records_is_legal(self):
        got = confirm(records=[])
        self.assertEqual(got["results"], [])
        self.assertTrue(got["allow_wait"])
        self.assertEqual(got["counts"]["total"] if "total" in got["counts"]
                         else sum(got["counts"].values()), 0)

    def test_retcode_zero_is_sent_not_delivered(self):
        got = confirm(records=[record(receipt={"retcode": 0})])
        row = got["results"][0]
        self.assertEqual(row["result"], "sent")
        self.assertFalse(row["resend"])

    def test_ok_true_is_sent_not_delivered(self):
        got = confirm(records=[record(receipt={"ok": True})])
        self.assertEqual(got["results"][0]["result"], "sent")

    def test_delivered_requires_explicit_confirmation(self):
        got = confirm(records=[
            record(receipt={"retcode": 0, "delivery_confirmed": True})])
        self.assertEqual(got["results"][0]["result"], "delivered")

    def test_exception_or_missing_receipt_is_unknown_and_never_resends(self):
        for receipt in ({"error": True}, None, {}, "回执坏了", {"retcode": "零"}):
            got = confirm(records=[record(receipt=receipt)])
            row = got["results"][0]
            self.assertEqual(row["result"], "unknown", receipt)
            self.assertFalse(row["resend"], receipt)

    def test_nonzero_retcode_is_failed_not_done(self):
        got = confirm(records=[record(receipt={"retcode": 100})])
        self.assertEqual(got["results"][0]["result"], "failed")

    def test_terminal_failure_cannot_be_upgraded_by_later_success(self):
        got = confirm(records=[
            record(receipt={"retcode": 0}, prior="failed")])
        row = got["results"][0]
        self.assertEqual(row["result"], "failed")
        self.assertIn("result_downgrade_blocked", row["reasons"])

    def test_terminal_rejection_and_abandon_are_final(self):
        for prior in ("rejected", "abandoned"):
            got = confirm(records=[
                record(receipt={"retcode": 0, "delivery_confirmed": True},
                       prior=prior)])
            self.assertEqual(got["results"][0]["result"], prior)

    def test_delivered_is_not_downgraded_by_later_unknown(self):
        got = confirm(records=[
            record(receipt=None, prior="delivered")])
        self.assertEqual(got["results"][0]["result"], "delivered")

    def test_unknown_may_become_sent_with_real_receipt(self):
        got = confirm(records=[
            record(receipt={"retcode": 0}, prior="unknown")])
        self.assertEqual(got["results"][0]["result"], "sent")

    def test_hard_block_is_rejected_with_verbatim_codes(self):
        got = confirm(records=[
            record(receipt={"retcode": 0}, hard_blocks=["consent_blocked"])])
        row = got["results"][0]
        self.assertEqual(row["result"], "rejected")
        self.assertIn("consent_blocked", row["reasons"])

    def test_recipient_binding_failure_is_abandoned_not_rerouted(self):
        got = confirm(records=[
            record(receipt={"retcode": 0}, recipient_qq="9999")])
        row = got["results"][0]
        self.assertEqual(row["result"], "abandoned")
        self.assertIn("recipient_topic_mismatch", row["reasons"])
        self.assertEqual(row["recipient_qq"], "9999")   # 绝不改写收件人

    def test_missing_friend_binding_abandons_never_reroutes(self):
        got = confirm([record(receipt={"retcode": 0})], friends=None)
        row = got["results"][0]
        self.assertEqual(row["result"], "abandoned")
        self.assertIn("friend_binding_unverified", row["reasons"])
        self.assertEqual(row["recipient_qq"], PEER)

    def test_read_kind_record_is_not_outward(self):
        read = ap.build("curiosity", "search", motive="求知", reason="查资料",
                        visibility="internal", deadline=NOW + 100,
                        budget={"quota_key": "CURIOSITY_MAX_PER_DAY",
                                "max_per_day": 3, "cooldown_seconds": 0},
                        success="找到资料", failure="查不到",
                        abandon_when="连续失败放弃", decision="act")
        got = confirm(records=[
            {"proposal": read, "receipt": {"retcode": 0}}])
        row = got["results"][0]
        self.assertEqual(row["result"], "unknown")
        self.assertIn("not_outward_kind", row["reasons"])

    def test_ungranted_kind_is_audited_not_hidden(self):
        gate = g04.release_gate(now=NOW, signoff=signoff(("signature",)))
        got = confirm(records=[record(receipt={"retcode": 0})],
                                  gate=gate)
        row = got["results"][0]
        self.assertEqual(row["result"], "sent")      # 事实就是事实
        self.assertIn("kind_not_granted", row["reasons"])

    def test_records_are_deduped_and_order_independent(self):
        rows = [record(receipt={"retcode": 0}),
                record("signature", receipt={"ok": True})]
        a = confirm(records=copy.deepcopy(rows))
        b = confirm(records=copy.deepcopy(list(reversed(rows))))
        self.assertEqual(canonical(a), canonical(b))
        dup = confirm(records=rows + [copy.deepcopy(rows[0])])
        self.assertEqual(len(dup["results"]), 2)

    def test_inputs_are_not_modified(self):
        rows = [record(receipt={"retcode": 0})]
        snapshot = canonical(rows)
        confirm(records=rows)
        self.assertEqual(canonical(rows), snapshot)

    def test_result_ids_are_content_addressed(self):
        got = confirm(records=[record(receipt={"retcode": 0})])
        self.assertTrue(got["results"][0]["result_id"].startswith("g4r_"))
        json.dumps(got, allow_nan=False)

    def test_counts_are_observable(self):
        got = confirm(records=[
            record(receipt={"retcode": 0}),
            record("signature", receipt=None),
            record("private_share", receipt={"retcode": 100})])
        self.assertEqual(got["counts"]["sent"], 1)
        self.assertEqual(got["counts"]["unknown"], 1)
        self.assertEqual(got["counts"]["failed"], 1)
        self.assertTrue(got["provenance"]["sent_is_not_delivered"])
        self.assertTrue(got["provenance"]["unknown_never_resends"])


if __name__ == "__main__":
    unittest.main()