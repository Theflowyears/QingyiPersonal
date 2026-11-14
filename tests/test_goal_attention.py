# -*- coding: utf-8 -*-
"""G02 回归锁：承诺与目标进入注意（人格内核路线 §4.1 / §6.1–6.4 / §7.1 / §8.3）。

钉死的行为（每条判据正反例）：
  · 纯函数构建：now 必填、内容寻址 attention_id/attention_version/entry_id、同输入
    同输出、输入顺序无关、输入不被修改；超出 max_entries 逐条大声登记 budget；
  · 状态过滤：只有 open 承诺 / active 目标进注意，排除码与 C04 逐字一致；
  · urgency 三档边界（due_ts 恰好 now、now+window）；非数值期限如实登记不补造；
  · 排序确定性：承诺先于目标，承诺内 overdue>due_soon>open，再 due_ts（None 最后）、
    object_id；目标按 object_id；
  · 收件人绑定纪律（§7.1 阿甲场景）：to_qq 缺→unbound 绝不默认主人；目标 recipient 恒空；
  · motive_known 真假：承诺恒履约；未知动机如实登记，不拒绝真实目标也不冒充四类；
  · attention_version 随承诺/目标/urgency 变化立即变（§8.3），now 推进但条目未变则不变；
  · allow_wait 恒 True，输出没有任何 act/rejected 裁决字段（注意不是命令）；
  · M05 失效挡下逐字透传（invalidated/superseded/needs_reassessment/scope_revoked）；
  · proposal_seed 字段诚实：对缺字段种子调 ap.validate 得 rejected+missing_*（这是
    测试调 ap，不是被测模块调）。

AST 锁钉死「复用而非重造」：import 面只含 stdlib + M05/M06/A03/G01 冻结前置；必须
调用 cw._text/dag.read_gate/mm.digest 并引用 ap.MOTIVES；模块内绝不出现 ap.validate/
ap.observe/ap.build 调用（不替仲裁裁决、不替 worker 补全信封）；不 import bridge/
self_model/os/时钟/随机/网络/检索器；零写入、不开库、不解析路径、不冻路径、无墙钟；
build_attention 的 now 是无默认值 kwonly；LIMITATIONS 如实声明不裁决/不接线。

所有合成数据只在内存或 tempfile 隔离目录里，是测试夹具不是自然样本；绝不写真实 data/。
"""
import ast
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import goal_attention as ga        # noqa: E402
import action_proposal as ap       # noqa: E402
import invalidation_dag as dag     # noqa: E402
import cognition_writer as cw      # noqa: E402
import memory_manifest as mm       # noqa: E402

NOW = 4000000000.0
MASTER = "9999"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def snapshot_tree(root):
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


# ── 合成夹具（self_model commitment/goal 形状，参照 test_work_state）──


def commitment(oid="cmt_1", **over):
    row = {"id": oid, "to_qq": "per_1001", "text": "帮查荒坂塔资料",
           "due_ts": NOW + 3600, "status": "open", "event_ids": ["evt_1"]}
    row.update(over)
    return row


def goal(oid="gol_1", **over):
    row = {"id": oid, "text": "弄清荒坂塔原型", "motive": "求知",
           "owner_qq": "per_1001", "status": "active"}
    row.update(over)
    return row


def attention(**kw):
    base = dict(now=NOW, commitments=[commitment()], goals=[goal()],
                scope="private:1001")
    base.update(kw)
    return ga.build_attention(**base)


def entry_of(got, oid):
    return next(e for e in got["entries"] if e["object_id"] == oid)


# ── AST 静态锁：复用冻结前置，零写入，无墙钟，不替仲裁裁决 ──────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "goal_attention.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def calls(self):
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.Call)]

    def call_name(self, call):
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        if isinstance(call.func, ast.Name):
            return call.func.id
        return ""

    def attr_pairs(self):
        return {(n.func.value.id, n.func.attr) for n in self.calls()
                if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}

    def imported(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        return names

    def test_import_surface_is_frozen_predecessors_plus_stdlib(self):
        allowed = {"copy", "json", "math", "cognition_writer", "invalidation_dag",
                   "memory_manifest", "action_proposal"}
        self.assertEqual(allowed, self.imported(),
                         "import 面变了：多出来的要解释，缺少的说明没复用前置")

    def test_it_reuses_m05_gate_a03_digest_m06_text_and_g01_motives(self):
        pairs = self.attr_pairs()
        for wanted in (("cw", "_text"), ("dag", "read_gate"), ("mm", "digest")):
            self.assertIn(wanted, pairs, "必须复用冻结前置 %s，不另长第二套" % (wanted,))
        attrs = {(n.value.id, n.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        self.assertIn(("ap", "MOTIVES"), attrs, "动机四类必须复用 G01 ap.MOTIVES")

    def test_no_banned_imports(self):
        banned = {"os", "time", "random", "datetime", "sqlite3", "pathlib", "socket",
                  "urllib", "requests", "http", "qq_openclaw_bridge", "self_model",
                  "llm_router", "retrieval_objects", "recall_scoring", "association_edges",
                  "memory_depth", "persona_growth", "emotion_shadow", "history_log",
                  "napcat_channel", "web_read", "bili"}
        self.assertEqual(set(), self.imported() & banned,
                         "G02 是纯输入契约，不该 import %s" % sorted(self.imported() & banned))

    def test_no_writer_or_db_calls(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "executemany", "commit", "connect",
                     "open", "save", "add", "add_belief", "record_event", "add_commitment",
                     "add_goal", "close_commitment", "close_goal", "record_action",
                     "write", "writelines", "dump", "touch", "write_text", "write_bytes",
                     "rollback"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), forbidden,
                             "只读投影里出现写操作/开库 %s" % self.call_name(call))

    def test_no_ap_arbitration_or_envelope_calls(self):
        """G02 不替仲裁裁决、不替 worker 补全信封：绝不调 ap.validate/observe/build。"""
        for call in self.calls():
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                self.assertFalse(call.func.value.id == "ap"
                                 and call.func.attr in ("validate", "observe", "build"),
                                 "G02 调用了 ap.%s（替仲裁/补全信封）" % call.func.attr)

    def test_no_upstream_entry_calls(self):
        wanted = {"render_evidence", "resolve_ripple", "assess_expansion", "expand_paths",
                  "retrieve_evidence", "recall", "advance", "apply_event", "appraise",
                  "propagate", "correction", "plan_emotion_revision",
                  "read_invalidation_state", "read_derivation_inputs", "commit",
                  "apply_consolidation", "reflection_plan", "build_snapshot",
                  "render_injection"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), wanted,
                             "G02 不该调用上游入口 %s" % self.call_name(call))

    def test_no_wall_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "random", "uniform", "randint", "choice",
                                  "shuffle", "now", "today", "monotonic", "strftime",
                                  "localtime", "gmtime", "perf_counter"),
                                 "G02 决策路径读了时钟/随机 %s" % node.func.attr)
        for token in ("time.time", "fromtimestamp", "isoformat"):
            self.assertNotIn(token, self.source, "出现了墙钟取时 %s" % token)

    def test_now_is_a_required_keyword_on_build_attention(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "build_attention")
        kw = [a.arg for a in fn.args.kwonlyargs]
        self.assertIn("now", kw, "build_attention 必须显式接收 now")
        self.assertIsNone(fn.args.kw_defaults[kw.index("now")],
                          "now 不许有默认值（无墙钟）")

    def test_no_sql_db_names_or_data_dir(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE ", "PRAGMA"):
                    self.assertNotIn(verb, upper, "G02 里出现 SQL：%s" % node.value[:60])
        for token in ("cognition.db", "emotion_shadow.db", "DATA_DIR", "sqlite"):
            self.assertNotIn(token, self.source, "纯输入契约不该碰库名/路径变量 %s" % token)

    def test_no_path_resolution_or_freezing(self):
        for call in self.calls():
            self.assertNotIn(self.call_name(call),
                             ("getenv", "abspath", "isfile", "exists"),
                             "G02 自身解析路径了；states/epoch 由调用方传入")
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径")

    def test_no_second_action_timing_engine(self):
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in ("should_act", "decide", "arbitrate", "schedule",
                             "when_to", "act_now", "appraise", "score", "rank"):
                    self.assertNotIn(word, node.name.lower(),
                                     "长出了第二套行动/打分判断 %s" % node.name)
        engine = {"propagate", "read_gate", "cache_key", "correction", "build_graph",
                  "check_edge", "render_evidence", "adjudicate", "validate", "observe"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name, engine, "重定义了冻结引擎函数 %s" % node.name)

    def test_no_date_based_caching(self):
        for token in ("strftime", "%Y-%m-%d", '"day"', "'day'"):
            self.assertNotIn(token, self.source, "出现了按日期缓存的口径 %s" % token)

    def test_the_contract_constants(self):
        self.assertEqual("g02-1", ga.ALGORITHM_VERSION)
        self.assertIsInstance(ga.MAX_ENTRIES, int)
        self.assertGreater(ga.MAX_ENTRIES, 0)
        self.assertEqual(86400.0, ga.DUE_SOON_WINDOW)
        self.assertIn(ga.COMMITMENT_MOTIVE, ap.MOTIVES)
        for code in ("missing_object_id", "missing_text", "commitment_not_open",
                     "goal_not_active", "unbound", "budget"):
            self.assertIn(code, ga.REASON_CODES)

    def test_limitations_are_honest(self):
        self.assertIsInstance(ga.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(ga.LIMITATIONS), 8)
        text = " ".join(ga.LIMITATIONS)
        for undone in ("不裁决", "不接", "不补全", "注意不是命令", "M05", "M06",
                       "零写入", "motivation_score", "主人"):
            self.assertIn(undone, text, "limitations 必须如实声明 %s" % undone)


# ── 行为锁：构建（确定性 / 有界 / 不伪造 / 注意不是命令）──────────


class AttentionTests(unittest.TestCase):
    def test_now_is_required(self):
        with self.assertRaises(TypeError):
            ga.build_attention()

    def test_bad_now_is_refused(self):
        for bad in (None, "x", True, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ga.build_attention(now=bad)

    def test_negative_epoch_is_refused(self):
        with self.assertRaises(ValueError):
            ga.build_attention(now=NOW, epoch=-1)

    def test_same_input_gives_the_same_output(self):
        self.assertEqual(canonical(attention()), canonical(attention()))

    def test_input_order_does_not_change_the_output(self):
        forward = attention(commitments=[commitment("c1"), commitment("c2")],
                            goals=[goal("g1"), goal("g2")])
        backward = attention(commitments=[commitment("c2"), commitment("c1")],
                             goals=[goal("g2"), goal("g1")])
        self.assertEqual(canonical(forward), canonical(backward))

    def test_inputs_are_not_mutated(self):
        rows = dict(commitments=[commitment()], goals=[goal()],
                    states={"cmt_1": {"status": "active"}})
        before = canonical(rows)
        ga.build_attention(now=NOW, **rows)
        self.assertEqual(before, canonical(rows))

    def test_output_is_a_deep_copy_not_aliased(self):
        shared = commitment()
        got = attention(commitments=[shared])
        got["entries"][0]["event_ids"].append("evt_forged")
        self.assertEqual(["evt_1"], shared["event_ids"], "输出别名了输入列表")

    def test_attention_id_is_content_addressed(self):
        got = attention()
        payload = {k: v for k, v in got.items() if k != "attention_id"}
        self.assertEqual("ga_" + mm.digest(canonical(payload))[:14], got["attention_id"])
        other = attention(commitments=[commitment(text="别的承诺")])
        self.assertNotEqual(got["attention_id"], other["attention_id"])

    def test_attention_version_is_entries_digest(self):
        got = attention()
        self.assertEqual(mm.digest(canonical(got["entries"]))[:16], got["attention_version"])

    def test_entry_id_is_content_addressed(self):
        got = attention()
        for entry in got["entries"]:
            core = {k: v for k, v in entry.items() if k != "entry_id"}
            self.assertEqual("ga_" + mm.digest(canonical(core))[:14], entry["entry_id"])

    def test_the_output_shape_is_the_contract_one(self):
        got = attention()
        self.assertEqual({"attention_id", "attention_version", "algorithm_version",
                          "now", "epoch", "scope", "status", "entries", "allow_wait",
                          "excluded", "degradation", "provenance", "limitations"}, set(got))
        self.assertEqual("g02-1", got["algorithm_version"])
        self.assertEqual(list(ga.LIMITATIONS), got["limitations"])
        self.assertTrue(got["provenance"]["attention_is_not_command"])
        self.assertEqual(cw._STATE_NOTE, got["provenance"]["state_layer_note"])

    def test_the_entry_shape_is_the_contract_one(self):
        got = attention()
        for entry in got["entries"]:
            self.assertEqual({"entry_id", "source_kind", "object_id", "text", "motive",
                              "motive_known", "urgency", "due_ts", "topic_qq",
                              "recipient_qq", "goal_id", "event_ids", "projection_epoch"},
                             set(entry))

    def test_allow_wait_always_true_and_no_verdict_fields(self):
        for got in (attention(), attention(commitments=[], goals=[]),
                    attention(max_entries=0)):
            self.assertIs(True, got["allow_wait"])
            for field in ("decision", "act", "rejected"):
                self.assertNotIn(field, got, "注意输出不该有裁决字段 %s" % field)
            for entry in got["entries"]:
                self.assertNotIn("decision", entry)

    def test_empty_attention_is_legal_wait(self):
        got = attention(commitments=[], goals=[])
        self.assertEqual([], got["entries"])
        self.assertIs(True, got["allow_wait"])
        self.assertEqual("ok", got["status"])

    def test_closed_commitment_and_inactive_goal_are_excluded(self):
        got = attention(commitments=[commitment(status="done")],
                        goals=[goal(status="done")])
        self.assertEqual([], got["entries"])
        self.assertIn({"object_id": "cmt_1", "source_kind": "commitment",
                       "reason_code": "commitment_not_open"}, got["excluded"])
        self.assertIn({"object_id": "gol_1", "source_kind": "goal",
                       "reason_code": "goal_not_active"}, got["excluded"])

    def test_default_status_is_open_and_active(self):
        c = commitment()
        del c["status"]
        g = goal()
        del g["status"]
        got = attention(commitments=[c], goals=[g])
        self.assertEqual(2, len(got["entries"]))

    def test_missing_id_and_text_use_c04_codes(self):
        got = attention(commitments=[commitment("cmt_x", text=""),
                                     {"id": "", "text": "无身份"}],
                        goals=[goal("gol_x", text="")])
        codes = {(x["object_id"], x["reason_code"]) for x in got["excluded"]}
        self.assertIn(("cmt_x", "missing_text"), codes)
        self.assertIn(("", "missing_object_id"), codes)
        self.assertIn(("gol_x", "missing_text"), codes)

    def test_urgency_three_tiers_and_boundaries(self):
        window = ga.DUE_SOON_WINDOW
        cases = [(NOW - 1, "overdue"), (NOW, "due_soon"),
                 (NOW + window, "due_soon"), (NOW + window + 1, "open"),
                 (None, "open")]
        for due, urgency in cases:
            with self.subTest(due=due):
                got = attention(commitments=[commitment(due_ts=due)], goals=[])
                self.assertEqual(urgency, got["entries"][0]["urgency"])

    def test_invalid_due_ts_is_registered_not_fabricated(self):
        got = attention(commitments=[commitment(due_ts="abc")], goals=[])
        entry = got["entries"][0]
        self.assertIsNone(entry["due_ts"])
        self.assertEqual("open", entry["urgency"])
        self.assertIn({"store": "commitment", "object_id": "cmt_1",
                       "reason": "invalid_due_ts"}, got["degradation"])
        self.assertEqual("degraded", got["status"])

    def test_commitments_sort_before_goals(self):
        got = attention(commitments=[commitment()], goals=[goal()])
        self.assertEqual(["commitment", "goal"], [e["source_kind"] for e in got["entries"]])

    def test_commitment_sort_urgency_then_due_then_oid(self):
        rows = [commitment("c_open", due_ts=NOW + ga.DUE_SOON_WINDOW + 500),
                commitment("c_none", due_ts=None),
                commitment("c_soon", due_ts=NOW + 100),
                commitment("c_over_late", due_ts=NOW - 200),
                commitment("c_over_early", due_ts=NOW - 100)]
        got = attention(commitments=rows, goals=[])
        self.assertEqual(["c_over_late", "c_over_early", "c_soon", "c_open", "c_none"],
                         [e["object_id"] for e in got["entries"]])

    def test_goals_sort_by_object_id(self):
        got = attention(commitments=[], goals=[goal("g_z"), goal("g_a"), goal("g_m")])
        self.assertEqual(["g_a", "g_m", "g_z"], [e["object_id"] for e in got["entries"]])

    def test_commitment_binding_to_qq(self):
        got = attention(commitments=[commitment(to_qq="per_2002")], goals=[])
        entry = got["entries"][0]
        self.assertEqual("per_2002", entry["topic_qq"])
        self.assertEqual("per_2002", entry["recipient_qq"])

    def test_unbound_commitment_never_defaults_to_master(self):
        got = attention(commitments=[commitment(to_qq="")], goals=[])
        entry = got["entries"][0]
        self.assertEqual("", entry["topic_qq"])
        self.assertEqual("", entry["recipient_qq"])
        self.assertNotEqual(MASTER, entry["recipient_qq"])
        self.assertIn({"store": "commitment", "object_id": "cmt_1",
                       "reason": "unbound"}, got["degradation"])
        self.assertEqual(1, len(got["entries"]), "未绑定承诺仍是真实承诺，保留进注意")

    def test_goal_recipient_always_empty(self):
        got = attention(commitments=[], goals=[goal(owner_qq="per_2002")])
        entry = got["entries"][0]
        self.assertEqual("per_2002", entry["topic_qq"])
        self.assertEqual("", entry["recipient_qq"])
        self.assertEqual("active", entry["urgency"])
        self.assertIsNone(entry["due_ts"])

    def test_commitment_motive_is_always_fulfillment_and_known(self):
        got = attention(commitments=[commitment()], goals=[])
        entry = got["entries"][0]
        self.assertEqual("履约", entry["motive"])
        self.assertTrue(entry["motive_known"])
        self.assertIn(entry["motive"], ap.MOTIVES)

    def test_goal_known_motive_is_carried(self):
        for motive in ap.MOTIVES:
            with self.subTest(motive=motive):
                got = attention(commitments=[], goals=[goal(motive=motive)])
                entry = got["entries"][0]
                self.assertEqual(motive, entry["motive"])
                self.assertTrue(entry["motive_known"])

    def test_goal_unknown_motive_is_registered_not_rejected_or_faked(self):
        got = attention(commitments=[], goals=[goal(motive="孤独")])
        entry = got["entries"][0]
        self.assertEqual("孤独", entry["motive"])
        self.assertFalse(entry["motive_known"])
        self.assertNotIn(entry["motive"], ap.MOTIVES)
        self.assertEqual(1, len(got["entries"]), "未知动机不拒绝真实目标")

    def test_event_ids_only_real_sources_not_fabricated(self):
        got = attention(commitments=[commitment(event_ids=["evt_1", "evt_2"])],
                        goals=[goal(parent_event="evt_9")])
        self.assertEqual(["evt_1", "evt_2"], entry_of(got, "cmt_1")["event_ids"])
        self.assertEqual(["evt_9"], entry_of(got, "gol_1")["event_ids"])
        empty = attention(commitments=[commitment(event_ids=[])], goals=[goal()])
        self.assertEqual([], entry_of(empty, "cmt_1")["event_ids"])
        self.assertEqual([], entry_of(empty, "gol_1")["event_ids"])

    def test_event_ids_accepts_m06_json_string(self):
        got = attention(commitments=[commitment(event_ids='["evt_1","evt_2"]')], goals=[])
        self.assertEqual(["evt_1", "evt_2"], got["entries"][0]["event_ids"])

    def test_duplicate_object_ids_are_deduped(self):
        got = attention(commitments=[commitment(), commitment(text="重复转述")], goals=[])
        self.assertEqual(1, len(got["entries"]))

    def test_budget_overflow_is_loud_not_silent(self):
        rows = [commitment("cmt_%02d" % i) for i in range(10)]
        got = attention(commitments=rows, goals=[])
        self.assertEqual(ga.MAX_ENTRIES, len(got["entries"]))
        drops = [x for x in got["excluded"] if x["reason_code"] == "budget"]
        self.assertEqual(10 - ga.MAX_ENTRIES, len(drops), "超限要逐条大声登记")

    def test_zero_budget_drops_all_loudly(self):
        got = attention(max_entries=0)
        self.assertEqual([], got["entries"])
        drops = [x for x in got["excluded"] if x["reason_code"] == "budget"]
        self.assertEqual(2, len(drops))

    def test_budget_keeps_commitments_before_goals(self):
        rows = [commitment("cmt_%02d" % i) for i in range(8)]
        got = attention(commitments=rows, goals=[goal("g1"), goal("g2")], max_entries=8)
        self.assertEqual(8, len(got["entries"]))
        self.assertTrue(all(e["source_kind"] == "commitment" for e in got["entries"]))
        drops = {x["object_id"] for x in got["excluded"] if x["reason_code"] == "budget"}
        self.assertEqual({"g1", "g2"}, drops)

    def test_version_changes_on_new_commitment(self):
        base = attention()
        more = attention(commitments=[commitment(), commitment("cmt_2", text="周五前把图发给他")])
        self.assertNotEqual(base["attention_version"], more["attention_version"])

    def test_version_changes_on_commitment_text(self):
        base = attention()
        other = attention(commitments=[commitment(text="改了内容")])
        self.assertNotEqual(base["attention_version"], other["attention_version"])

    def test_version_changes_on_new_goal(self):
        base = attention()
        more = attention(goals=[goal(), goal("gol_2", text="学会做咖啡")])
        self.assertNotEqual(base["attention_version"], more["attention_version"])

    def test_version_changes_immediately_on_urgency_flip(self):
        rows = [commitment(due_ts=NOW + 100)]
        before = attention(commitments=rows, goals=[], now=NOW)
        after = attention(commitments=rows, goals=[], now=NOW + 200)
        self.assertEqual("due_soon", before["entries"][0]["urgency"])
        self.assertEqual("overdue", after["entries"][0]["urgency"])
        self.assertNotEqual(before["attention_version"], after["attention_version"],
                            "urgency 变化必须立即生效，不许为缓存等到第二天")

    def test_version_stable_when_now_moves_but_entries_unchanged(self):
        rows = [commitment(due_ts=NOW + ga.DUE_SOON_WINDOW + 5000)]
        before = attention(commitments=rows, goals=[], now=NOW)
        after = attention(commitments=rows, goals=[], now=NOW + 10)
        self.assertEqual(before["attention_version"], after["attention_version"],
                         "版本是内容寻址，不是时钟；条目未变则版本不变")
        self.assertNotEqual(before["attention_id"], after["attention_id"])

    def test_clean_states_exclude_nothing(self):
        got = attention(states={})
        self.assertEqual([], got["excluded"])
        self.assertEqual([], got["degradation"])
        self.assertEqual("ok", got["status"])


# ── 行为锁：M05 失效纪律（失效来源绝不驱动注意，挡下原因逐字透传）──


class InvalidationTests(unittest.TestCase):
    def test_invalidated_commitment_never_enters_attention(self):
        got = attention(states={"cmt_1": {"status": "invalidated"}})
        self.assertEqual([], [e for e in got["entries"] if e["source_kind"] == "commitment"])
        self.assertIn({"object_id": "cmt_1", "source_kind": "commitment",
                       "reason_code": "invalidated"}, got["excluded"])

    def test_superseded_goal_is_excluded(self):
        got = attention(states={"gol_1": {"status": "superseded"}})
        self.assertEqual([], [e for e in got["entries"] if e["source_kind"] == "goal"])
        self.assertIn({"object_id": "gol_1", "source_kind": "goal",
                       "reason_code": "superseded"}, got["excluded"])

    def test_needs_reassessment_is_not_current(self):
        got = attention(states={"cmt_1": {"status": "needs_reassessment"}})
        self.assertIn({"object_id": "cmt_1", "source_kind": "commitment",
                       "reason_code": "needs_reassessment"}, got["excluded"])

    def test_scope_revoked_only_blocks_matching_scope(self):
        states = {"cmt_1": {"status": "active", "revoked_scopes": ["private:1001"]}}
        got = attention(states=states)
        self.assertIn({"object_id": "cmt_1", "source_kind": "commitment",
                       "reason_code": "scope_revoked"}, got["excluded"])
        other = attention(states=states, scope="group:g1")
        self.assertEqual(1, len([e for e in other["entries"]
                                 if e["source_kind"] == "commitment"]))

    def test_real_dag_propagation_excludes_the_commitment(self):
        edges = [{"derived_id": "cmt_1", "source_id": "evt_1",
                  "derived_kind": "commitment", "source_kind": "event"}]
        rec = dag.correction("observation_invalid", "evt_1", "撤回这条观察", ts=NOW)
        prop = dag.propagate(edges, [rec], epoch=0, states={}, now=NOW)
        states = {a["object_id"]: {"status": a["status"],
                                   "revoked_scopes": a["revoked_scopes"]}
                  for a in prop["affected"]}
        got = attention(states=states, epoch=prop["epoch"])
        self.assertEqual([], [e for e in got["entries"] if e["source_kind"] == "commitment"],
                         "源被撤回，派生承诺不得驱动注意")
        self.assertIn({"object_id": "cmt_1", "source_kind": "commitment",
                       "reason_code": "invalidated"}, got["excluded"])

    def test_unreadable_states_degrades_without_fabricating(self):
        got = attention(states="不是字典")
        self.assertIn({"store": "invalidation", "reason": "unreadable"}, got["degradation"])
        self.assertEqual("degraded", got["status"])
        self.assertEqual(2, len(got["entries"]), "状态不可读时如实降级，仍投影真实行")


# ── 行为锁：proposal_seed 诚实（测试调 ap.validate，不是模块调）──────


class SeedTests(unittest.TestCase):
    def setUp(self):
        ap._LAST = None

    def test_seed_carries_only_honest_fields(self):
        entry = entry_of(attention(), "cmt_1")
        seed = ga.proposal_seed(entry)
        self.assertEqual("goal_attention", seed["worker"])
        self.assertEqual("履约", seed["motive"])
        self.assertEqual(entry["text"], seed["reason"])
        self.assertEqual(entry["event_ids"], seed["event_ids"])
        self.assertEqual("", seed["goal_id"])
        self.assertEqual(entry["topic_qq"], seed["topic_qq"])
        self.assertEqual(entry["recipient_qq"], seed["recipient_qq"])
        self.assertEqual(entry["due_ts"], seed["deadline"])
        self.assertEqual("wait", seed["decision"])
        self.assertEqual(["attention_is_not_command"], seed["gate_reasons"])
        for absent in ("kind", "visibility", "budget", "success", "failure", "abandon_when"):
            self.assertNotIn(absent, seed, "G02 不编造 worker 的事实 %s" % absent)

    def test_seed_goal_id_is_object_id_for_goal(self):
        entry = entry_of(attention(), "gol_1")
        seed = ga.proposal_seed(entry)
        self.assertEqual("gol_1", seed["goal_id"])
        self.assertEqual("", seed["recipient_qq"])

    def test_seed_unknown_motive_left_empty(self):
        entry = entry_of(attention(commitments=[], goals=[goal(motive="孤独")]), "gol_1")
        seed = ga.proposal_seed(entry)
        self.assertEqual("", seed["motive"])

    def test_seed_validate_is_rejected_with_missing_codes(self):
        """对缺字段种子调 ap.validate 得 rejected+missing_* 是正确行为（G02 不冒充完整）。"""
        entry = entry_of(attention(), "cmt_1")
        result = ap.validate(ga.proposal_seed(entry), now=NOW)
        self.assertEqual("rejected", result["decision"])
        for code in ("missing_kind", "missing_budget", "missing_visibility",
                     "missing_success_definition", "missing_failure_definition",
                     "missing_abandon_condition"):
            self.assertIn(code, result["reasons"], code)

    def test_seed_without_deadline_is_missing_deadline(self):
        entry = entry_of(attention(commitments=[], goals=[goal()]), "gol_1")
        result = ap.validate(ga.proposal_seed(entry), now=NOW)
        self.assertEqual("rejected", result["decision"])
        self.assertIn("missing_deadline", result["reasons"])

    def test_seed_does_not_mutate_entry(self):
        entry = entry_of(attention(), "cmt_1")
        before = canonical(entry)
        ga.proposal_seed(entry)
        self.assertEqual(before, canonical(entry))

    def test_module_never_calls_ap_observe(self):
        """运行时双保险：build_attention/proposal_seed 之后 ap 留痕仍为空（没替仲裁登记）。"""
        got = attention()
        for entry in got["entries"]:
            ga.proposal_seed(entry)
        self.assertIsNone(ap.last(), "G02 调用了 ap.observe（替仲裁裁决）")


# ── 纯函数零写入：隔离 DATA_DIR，绝不碰真实 data/ ──────────────────


class PurityTests(unittest.TestCase):
    def test_pure_contract_creates_nothing_in_an_empty_data_dir(self):
        with tempfile.TemporaryDirectory(prefix="g02_", ignore_cleanup_errors=True) as root:
            with patch.dict(os.environ, {"DATA_DIR": root, "PERSONA_DATA_DIR": root,
                                         "QQ_DRY_RUN": "1"}):
                before = snapshot_tree(root)
                got = attention()
                self.assertEqual("ok", got["status"])
                self.assertEqual(before, snapshot_tree(root))
                self.assertEqual([], list(Path(root).iterdir()), "纯契约顺手建了目录")


if __name__ == "__main__":
    unittest.main()