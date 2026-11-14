# -*- coding: utf-8 -*-
"""G03 回归锁：低风险检索整理草稿调度（统一方案 §11.1 / 待办 G03）。

钉死的行为（每条判据正反例）：
  · 纯函数构建：now 必填、内容寻址 candidate_id/schedule_id/schedule_version、
    同输入同输出、输入顺序无关、输入不被修改；空选择合法（allow_wait 恒 True）；
  · 只调度 READ_KINDS；对外发送逐条 send_kind_out_of_scope，绝不代发；
  · G01 裁决透传：硬约束 rejected、期限 abandon、worker wait 一律顺延且
    原因逐字透传；调度器不改写裁决、不替 worker 补字段；
  · 预算：配额用尽 quota_exhausted、冷却 cooldown_active（边界恰好到期放行）；
    共享配额逐次扣减；usage=None 全部顺延 usage_unavailable 并降级，
    绝不把读不到当成零用量；usage={} 是明确没有用量；
  · 公平性：单 worker 超名额 worker_fairness、总名额 slot_budget 逐条大声登记；
  · 失败：连续失败达阈值 failure_backoff，阈值以下照常参选，计数如实可观测；
  · 承诺提高优先级但不越权：overdue>due_soon>其余注意>无注意；硬约束/预算
    先于优先级；注意链接缺失如实登记不编造优先级；
  · 兴趣查询静默：入选恒 internal、无收件人，silent 恒 True，无对外文本；
  · schedule_version 只随入选集合变化，now 推进但选择未变则不变。

AST 锁钉死「复用而非重造」：import 面只含 stdlib + G01/A03 冻结前置；必须
调用 ap.validate 与 mm.digest；绝不出现 ap.observe/ap.build（不替 worker
登记/补全信封）；不 import bridge/self_model/os/时钟/随机/网络/检索器；
零写入、不开库、不解析路径、无墙钟；schedule 的 now 是无默认值 kwonly；
LIMITATIONS 如实声明不裁决/不接线/不代发。

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

import goal_draft_scheduler as gds    # noqa: E402
import action_proposal as ap          # noqa: E402
import goal_attention as ga           # noqa: E402
import memory_manifest as mm          # noqa: E402

NOW = 4000000000.0


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


# ── 合成夹具：worker 补全的完整 G01 只读信封 ─────────────────────


def proposal(worker="curiosity", kind="search", **over):
    base = ap.build(
        worker, kind, motive="求知", reason="想弄懂荒坂塔原型",
        event_ids=["evt_1"], topic="荒坂塔", visibility="internal",
        deadline=NOW + 86400.0,
        budget={"quota_key": "CURIOSITY_MAX_PER_DAY",
                "max_per_day": 3, "cooldown_seconds": 3600.0},
        success="找到可追溯的资料，或明确记下还没证实",
        failure="搜索/阅读失败，或没有可用结果",
        abandon_when="连续 3 次查不到就放弃这条",
        decision="act")
    base.update(over)
    return base


def candidate(worker="curiosity", kind="search", failures=0, link="", **over):
    return {"proposal": proposal(worker, kind, **over),
            "recent_failures": failures, "attention_entry_id": link}


def run(candidates=None, **kw):
    base = dict(now=NOW, candidates=candidates, usage={})
    base.update(kw)
    return gds.schedule(**base)


def codes(deferred_row):
    return deferred_row["reason_codes"]


def attention_payload(urgency="overdue", source_kind="commitment"):
    entry = {"entry_id": "ga_link1", "source_kind": source_kind,
             "urgency": urgency, "object_id": "cmt_1"}
    return {"entries": [entry]}


# ── AST 静态锁：复用冻结前置，零写入，无墙钟，不替仲裁裁决 ──────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "goal_draft_scheduler.py"), encoding="utf-8") as handle:
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
        self.assertIn("READ_KINDS", self.source)

    def test_never_registers_or_completes_envelopes_for_workers(self):
        pairs = self.attr_pairs()
        self.assertNotIn(("ap", "observe"), pairs)
        self.assertNotIn(("ap", "build"), pairs)

    def test_no_wall_clock_no_random_no_io_no_paths(self):
        banned = {"os", "time", "random", "sqlite3", "pathlib", "subprocess",
                  "socket", "requests", "urllib", "bridge", "self_model",
                  "llm_router", "qq_openclaw_bridge"}
        self.assertFalse(self.imported() & banned)
        for token in ("time.time(", "random.", "open(", "DATA_DIR",
                      "os.environ", "send_private_msg"):
            self.assertNotIn(token, self.source)

    def test_now_is_required_kwonly_without_default(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "schedule")
        kwonly = {a.arg: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults)}
        self.assertIn("now", kwonly)
        self.assertIsNone(kwonly["now"])

    def test_limitations_declare_no_arbitration_no_wiring_no_send(self):
        text = " ".join(gds.LIMITATIONS)
        for token in ("不裁决", "不接", "G04", "零写入", "静默"):
            self.assertIn(token, text)


# ── 纯函数与确定性 ───────────────────────────────────────────────


class PurityTests(unittest.TestCase):
    def test_now_is_required_and_numeric(self):
        with self.assertRaises(TypeError):
            gds.schedule(candidates=[])
        with self.assertRaises(ValueError):
            gds.schedule(now="tonight", candidates=[])

    def test_empty_selection_is_legal_wait(self):
        got = run([])
        self.assertEqual(got["selected"], [])
        self.assertEqual(got["deferred"], [])
        self.assertTrue(got["allow_wait"])
        self.assertTrue(got["silent"])
        self.assertEqual(got["status"], "ok")
        json.dumps(got, allow_nan=False)

    def test_same_input_same_output_and_order_independent(self):
        rows = [candidate("curiosity"), candidate("bili_auto", "watch",
                budget={"quota_key": "BILI_AUTO_MAX_PER_DAY",
                        "max_per_day": 2, "cooldown_seconds": 0})]
        a = run(copy.deepcopy(rows))
        b = run(copy.deepcopy(list(reversed(rows))))
        self.assertEqual(canonical(a), canonical(b))
        self.assertEqual(run(copy.deepcopy(rows)), a)

    def test_inputs_are_not_modified(self):
        rows = [candidate()]
        snapshot = canonical(rows)
        usage = {"CURIOSITY_MAX_PER_DAY": {"used": 1, "last_ts": NOW - 10}}
        attention = attention_payload()
        before = canonical(usage), canonical(attention)
        run(rows, usage=usage, attention=attention)
        self.assertEqual(canonical(rows), snapshot)
        self.assertEqual((canonical(usage), canonical(attention)), before)

    def test_duplicate_candidates_are_content_addressed_once(self):
        got = run([candidate(), candidate()])
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(got["deferred"], [])

    def test_ids_are_content_addressed(self):
        got = run([candidate()])
        cid = got["selected"][0]["candidate_id"]
        self.assertTrue(cid.startswith("gd_"))
        core = {"proposal": proposal(), "recent_failures": 0,
                "attention_entry_id": ""}
        self.assertEqual(cid, "gd_" + mm.digest(canonical(core))[:14])
        self.assertTrue(got["schedule_id"].startswith("gd_"))
        self.assertEqual(len(got["schedule_version"]), 16)

    def test_version_tracks_selection_not_clock(self):
        a = run([candidate()])
        b = run([candidate()], now=NOW + 60)
        self.assertEqual(a["schedule_version"], b["schedule_version"])
        self.assertNotEqual(a["schedule_id"], b["schedule_id"])
        c = run([])
        self.assertNotEqual(a["schedule_version"], c["schedule_version"])

    def test_non_dict_candidate_is_deferred_loudly(self):
        got = run(["不是候选", None])
        self.assertEqual(got["selected"], [])
        self.assertEqual(len(got["deferred"]), 2)
        for row in got["deferred"]:
            self.assertEqual(codes(row), ["invalid_candidate"])


# ── G01 裁决透传：硬约束先于一切，调度不改写裁决 ─────────────────


class VerdictPassThroughTests(unittest.TestCase):
    def test_hard_block_is_deferred_with_verbatim_codes(self):
        row = candidate(hard_blocks=["privacy_blocked"])
        got = run([row])
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["privacy_blocked"])

    def test_missing_fields_are_rejected_not_filled(self):
        row = candidate()
        row["proposal"]["budget"] = None
        got = run([row])
        self.assertEqual(got["selected"], [])
        self.assertIn("missing_budget", codes(got["deferred"][0]))

    def test_expired_deadline_is_abandoned(self):
        row = candidate(deadline=NOW - 1)
        got = run([row])
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["deadline_expired"])

    def test_worker_wait_is_legal_and_never_overridden(self):
        row = candidate(decision="wait", gate_reasons=["quota_exhausted"])
        got = run([row])
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["quota_exhausted"])
        self.assertTrue(got["allow_wait"])

    def test_send_kinds_are_out_of_scope(self):
        for kind in ap.SEND_KINDS:
            over = {"visibility": "private", "recipient_qq": "1001",
                    "topic_qq": "1001"}
            if kind in ("signature", "group_message"):
                over = {"visibility": "public" if kind == "signature" else "group",
                        "recipient_qq": ""}
            got = run([candidate("proactive_dm", kind, **over)])
            self.assertEqual(got["selected"], [], kind)
            self.assertEqual(codes(got["deferred"][0]), ["send_kind_out_of_scope"], kind)

    def test_read_kind_with_recipient_is_rejected_by_g01(self):
        got = run([candidate(recipient_qq="1001")])
        self.assertEqual(got["selected"], [])
        self.assertIn("read_kind_has_recipient", codes(got["deferred"][0]))


# ── 预算与冷却：事实来自调用方，读不到≠零用量 ────────────────────


class BudgetTests(unittest.TestCase):
    def test_quota_exhausted_is_deferred(self):
        usage = {"CURIOSITY_MAX_PER_DAY": {"used": 3, "last_ts": 0}}
        got = run([candidate()], usage=usage)
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["quota_exhausted"])
        self.assertEqual(got["budget"]["CURIOSITY_MAX_PER_DAY"]["remaining"], 0)

    def test_cooldown_boundary_is_exact(self):
        usage = {"CURIOSITY_MAX_PER_DAY": {"used": 0, "last_ts": NOW - 3600}}
        got = run([candidate()], usage=usage)
        self.assertEqual(len(got["selected"]), 1)      # 恰好到期 → 放行
        usage = {"CURIOSITY_MAX_PER_DAY": {"used": 0, "last_ts": NOW - 3599}}
        got = run([candidate()], usage=usage)
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["cooldown_active"])

    def test_shared_quota_is_decremented_per_selection(self):
        rows = [candidate("curiosity"), candidate("curiosity", "organize")]
        usage = {"CURIOSITY_MAX_PER_DAY": {"used": 2, "last_ts": 0}}
        got = run(rows, usage=usage, max_per_worker=2)
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(codes(got["deferred"][0]), ["quota_exhausted"])

    def test_unreadable_usage_defers_everything_and_degrades(self):
        got = run([candidate()], usage=None)
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["usage_unavailable"])
        self.assertEqual(got["status"], "degraded")
        self.assertIsNone(got["budget"]["CURIOSITY_MAX_PER_DAY"]["used"])
        self.assertTrue(any(d["reason"] == "unreadable" for d in got["degradation"]))

    def test_non_dict_usage_is_unreadable_not_zero(self):
        got = run([candidate()], usage="账本坏了")
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["usage_unavailable"])

    def test_empty_usage_is_explicit_zero(self):
        got = run([candidate()], usage={})
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["budget"]["CURIOSITY_MAX_PER_DAY"]["used"], 0)

    def test_conflicting_caps_take_the_conservative_minimum(self):
        rows = [candidate("curiosity"),
                candidate("curiosity", "organize",
                          budget={"quota_key": "CURIOSITY_MAX_PER_DAY",
                                  "max_per_day": 1, "cooldown_seconds": 0})]
        got = run(rows, max_per_worker=2)
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(codes(got["deferred"][0]), ["quota_exhausted"])
        self.assertEqual(got["budget"]["CURIOSITY_MAX_PER_DAY"]["max_per_day"], 1)
        self.assertTrue(any(d["reason"] == "quota_cap_conflict"
                            for d in got["degradation"]))

    def test_budget_view_is_observable(self):
        usage = {"CURIOSITY_MAX_PER_DAY": {"used": 1, "last_ts": NOW - 10}}
        got = run([candidate()], usage=usage)
        view = got["budget"]["CURIOSITY_MAX_PER_DAY"]
        self.assertEqual(view["used"], 1)
        self.assertEqual(view["max_per_day"], 3)
        self.assertEqual(view["remaining"], 2)
        self.assertEqual(view["cooldown_seconds"], 3600.0)
        self.assertEqual(view["last_ts"], NOW - 10)


# ── 公平性与名额：超限逐条大声登记，不静默截断 ───────────────────


class FairnessTests(unittest.TestCase):
    def test_worker_fairness_caps_one_worker(self):
        rows = [candidate("curiosity"), candidate("curiosity", "organize")]
        got = run(rows, max_slots=5)
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(codes(got["deferred"][0]), ["worker_fairness"])
        self.assertEqual(got["fairness"]["per_worker"], {"curiosity": 1})

    def test_slot_budget_is_loud(self):
        rows = [candidate("curiosity"), candidate("bili_auto", "watch",
                budget={"quota_key": "BILI_AUTO_MAX_PER_DAY",
                        "max_per_day": 2, "cooldown_seconds": 0})]
        got = run(rows, max_slots=1)
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(codes(got["deferred"][0]), ["slot_budget"])

    def test_zero_slots_selects_nothing_but_stays_legal(self):
        got = run([candidate()], max_slots=0)
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["slot_budget"])
        self.assertTrue(got["allow_wait"])

    def test_distinct_workers_share_slots_deterministically(self):
        rows = [candidate("bili_auto", "watch",
                budget={"quota_key": "BILI_AUTO_MAX_PER_DAY",
                        "max_per_day": 2, "cooldown_seconds": 0}),
                candidate("curiosity")]
        got = run(rows, max_slots=2)
        self.assertEqual([s["worker"] for s in got["selected"]],
                         ["bili_auto", "curiosity"])
        self.assertEqual(got["fairness"]["per_worker"],
                         {"bili_auto": 1, "curiosity": 1})


# ── 失败退避：如实报数，不无限重试也不静默丢弃 ───────────────────


class FailureTests(unittest.TestCase):
    def test_backoff_at_threshold(self):
        got = run([candidate(failures=gds.FAILURE_BACKOFF_THRESHOLD)])
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["failure_backoff"])
        self.assertEqual(got["failures"]["deferred_for_failures"], 1)

    def test_below_threshold_still_eligible(self):
        got = run([candidate(failures=gds.FAILURE_BACKOFF_THRESHOLD - 1)])
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(got["failures"]["deferred_for_failures"], 0)

    def test_invalid_failure_count_is_registered_not_fatal(self):
        got = run([candidate(failures="三次")])
        self.assertEqual(len(got["selected"]), 1)
        self.assertTrue(any(d["reason"] == "invalid_recent_failures"
                            for d in got["degradation"]))
        self.assertEqual(got["status"], "degraded")


# ── 注意优先级：承诺提优先但不越权（§11.1）──────────────────────


class AttentionPriorityTests(unittest.TestCase):
    def test_commitment_urgency_orders_tiers(self):
        def tiered(worker, link):
            return candidate(worker, link=link,
                             budget={"quota_key": "Q_" + worker,
                                     "max_per_day": 3, "cooldown_seconds": 0})
        rows = [tiered("w_open", "ga_open"), tiered("w_overdue", "ga_overdue"),
                tiered("w_soon", "ga_soon"), tiered("w_plain", "")]
        attention = {"entries": [
            {"entry_id": "ga_overdue", "source_kind": "commitment", "urgency": "overdue"},
            {"entry_id": "ga_soon", "source_kind": "commitment", "urgency": "due_soon"},
            {"entry_id": "ga_open", "source_kind": "commitment", "urgency": "open"}]}
        got = run(rows, attention=attention, max_slots=4, max_per_worker=1)
        self.assertEqual([s["worker"] for s in got["selected"]],
                         ["w_overdue", "w_soon", "w_open", "w_plain"])
        self.assertEqual([s["priority_tier"] for s in got["selected"]], [0, 1, 2, 3])

    def test_priority_never_overrides_hard_block(self):
        row = candidate(link="ga_overdue", hard_blocks=["consent_blocked"])
        got = run([row], attention=attention_payload())
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["consent_blocked"])

    def test_priority_never_overrides_quota(self):
        usage = {"CURIOSITY_MAX_PER_DAY": {"used": 3, "last_ts": 0}}
        got = run([candidate(link="ga_overdue")], usage=usage,
                  attention=attention_payload())
        self.assertEqual(got["selected"], [])
        self.assertEqual(codes(got["deferred"][0]), ["quota_exhausted"])

    def test_missing_link_is_registered_not_invented(self):
        got = run([candidate(link="ga_ghost")], attention=attention_payload())
        self.assertEqual(len(got["selected"]), 1)
        self.assertEqual(got["selected"][0]["priority_tier"], 3)
        self.assertTrue(any(d["reason"] == "attention_link_missing"
                            for d in got["degradation"]))

    def test_unreadable_attention_degrades_without_boost(self):
        got = run([candidate(link="ga_link1")], attention="坏了")
        self.assertEqual(got["selected"][0]["priority_tier"], 3)
        self.assertEqual(got["status"], "degraded")

    def test_real_g02_attention_feeds_priority(self):
        commitment = {"id": "cmt_1", "to_qq": "per_1001", "text": "帮查荒坂塔资料",
                      "due_ts": NOW - 10, "status": "open", "event_ids": ["evt_1"]}
        attention = ga.build_attention(now=NOW, commitments=[commitment],
                                       scope="private:1001")
        entry = attention["entries"][0]
        self.assertEqual(entry["urgency"], "overdue")
        got = run([candidate(link=entry["entry_id"])], attention=attention)
        self.assertEqual(got["selected"][0]["priority_tier"], 0)


# ── 静默与只读：入选恒 internal、无收件人、无对外文本 ─────────────


class SilenceTests(unittest.TestCase):
    def test_selected_are_internal_without_recipient(self):
        got = run([candidate(), candidate("bili_auto", "watch",
                budget={"quota_key": "BILI_AUTO_MAX_PER_DAY",
                        "max_per_day": 2, "cooldown_seconds": 0})], max_slots=2)
        for item in got["selected"]:
            self.assertEqual(item["proposal"]["visibility"], "internal")
            self.assertEqual(item["proposal"]["recipient_qq"], "")
            self.assertIn(item["kind"], ap.READ_KINDS)
        self.assertTrue(got["silent"])

    def test_output_carries_no_outward_message_path(self):
        got = run([candidate()])
        text = canonical(got)
        for token in ("send_private_msg", "outbox", "set_signature"):
            self.assertNotIn(token, text)
        self.assertIn("静默", " ".join(got["limitations"]))


if __name__ == "__main__":
    unittest.main()