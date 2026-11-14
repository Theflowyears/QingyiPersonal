# -*- coding: utf-8 -*-
"""C04 工作状态快照与三层注入的回归锁（路线 §4.1 / §8.3；stage3/inject 前置契约）。

钉死的行为（每条判据正反例）：
  · 快照纯函数构建：now 必填、内容寻址 snapshot_id、同输入同输出、输入顺序无关、
    输入不被修改；注意负荷/工具预算/等待项各有界，超限大声登记 budget；
  · 情绪只投影内核已算好的四维状态（冻结 bounds 裁剪），缺/坏输入如实降级不补造；
    接口失败事件不改情绪投影（接口失败 ≠ 对朋友失望）；
  · 三层边界：身份层稳定可缓存（M05 五分量 key，含 epoch/修订，绝不按日期缓存）；
    状态层有界、携带 state_version/时间戳，新承诺/目标/显著状态变化使旧缓存立即
    失效（同一天甚至同一秒内生效，不等第二天）；证据层只认 R06 render_evidence
    输出形状，逐条带 verdict_refs，场景 ID 内容寻址可核对，绝不伪造经历；
  · 失效纪律：M05 read_gate 挡下的来源（invalidated/superseded/needs_reassessment/
    scope_revoked/stale_projection）不进任何注入层，构建后新发生的失效在注入时刻
    立即生效；
  · 缺输入降级不伪造：emotion/budget/identity/evidence 缺席一律 absent/degraded。

AST 锁钉死「复用而非重造」：import 面只含 stdlib + M05/M06/R06/A03/情感内核冻结
前置；必须调用 dag.read_gate/dag.cache_key/mm.digest/cw._text；不 import bridge/
memory_depth/self_model/os/sqlite3/时钟/随机/网络；零写入、不开库、不解析路径、
不冻路径；不调上游入口（render_evidence/resolve_ripple/advance/propagate/commit）；
不新增第二套「什么时候行动」判断；LIMITATIONS 如实声明 C05/表达/LLM 没做。

所有合成数据只出现在 tempfile 隔离目录或内存里，是测试夹具，不是自然样本；
绝不写真实 data/。端到端夹具经 M06 单一写入者与真实 R01→…→R06 链路产生输入
（测试可以 import 这些模块，被测模块 work_state 本身不 import 写入器/检索器）。
"""
import ast
import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import work_state as ws            # noqa: E402
import invalidation_dag as dag     # noqa: E402
import cognition_writer as cw      # noqa: E402
import emotion_dynamics as ed      # noqa: E402
import memory_manifest as mm       # noqa: E402
import ripple_conflicts as rc      # noqa: E402
import ripple_evidence as re6      # noqa: E402
import self_model as sm            # noqa: E402
import retrieval_objects as ro     # noqa: E402
import association_edges as ae     # noqa: E402
import recall_scoring as rs        # noqa: E402

NOW = 4000000000.0


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


# ── 合成夹具（内存里的测试数据，不是自然样本）─────────────────────


def emotion(at=NOW, **over):
    state = ed.initial_state(at)
    state.update(over)
    return state


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


def waiting(oid="pend_1", **over):
    row = {"pending_id": oid, "tool": "bili_search", "requested_at": NOW - 120}
    row.update(over)
    return row


def relation(subject="per_1001", affection=5):
    return {"subject_ref": subject, "affection": affection}


def identity(**over):
    row = {"text": "她是清漪，现代人语域，自称我。",
           "object_revision": "rev_1", "source_revision": "src_1"}
    row.update(over)
    return row


def snap(**kw):
    base = dict(now=NOW, emotion=emotion(), commitments=[commitment()],
                goals=[goal()], pending=[waiting()], relations=[relation()],
                budget={"limit": 5, "used": 2},
                events=[{"id": "evt_1", "kind": "group_message", "ts": NOW - 60}],
                scope="private:1001")
    base.update(kw)
    return ws.build_snapshot(**base)


def claim_item(oid, value, sources=(), validity="valid"):
    return {"object_id": oid, "object_kind": "belief", "store": "cognition",
            "root_source_ids": list(sources), "validity": validity,
            "subject_ref": "per_1", "restatement": value, "occurred_at": 1000.0,
            "evidence_spans": [{"event_id": s, "quote": value, "subject_ref": "per_1"}
                               for s in sources]}


def edge(a, b):
    return {"edge_id": "edge_%s_%s" % (a, b), "type": "shared_entity",
            "a": a, "a_kind": "belief", "b": b, "b_kind": "belief",
            "basis": {"reason": "shared_entity"}}


def evidence_two_scenes():
    s = claim_item("s", "喜欢茶", ["e1"])
    n = claim_item("n", "喜欢咖啡", ["e2"])
    resolved = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
    return re6.render_evidence(resolved, items=[s, n], seeds=[s])


def render(snapshot=None, **kw):
    base = dict(identity=identity(), evidence=evidence_two_scenes())
    base.update(kw)
    return ws.render_injection(snapshot if snapshot is not None else snap(), **base)


# ── AST 静态锁：复用冻结前置，零写入，无墙钟，不接 bridge ──────────


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "work_state.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)
        cls.parents = {}
        for node in ast.walk(cls.tree):
            for child in ast.iter_child_nodes(node):
                cls.parents[child] = node

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
        allowed = {"copy", "json", "math", "cognition_writer", "emotion_dynamics",
                   "invalidation_dag", "memory_manifest", "ripple_evidence"}
        self.assertEqual(allowed, self.imported(),
                         "import 面变了：多出来的要解释，缺少的说明没复用前置")

    def test_it_reuses_m05_gate_cache_key_a03_digest_and_m06_text(self):
        pairs = self.attr_pairs()
        for wanted in (("dag", "read_gate"), ("dag", "cache_key"),
                       ("mm", "digest"), ("cw", "_text")):
            self.assertIn(wanted, pairs, "必须复用冻结前置 %s，不另长第二套" % (wanted,))

    def test_it_reuses_emotion_shape_and_r06_contract_constants(self):
        attrs = {(n.value.id, n.attr) for n in ast.walk(self.tree)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for wanted in (("ed", "DIMENSIONS"), ("ed", "PARAMETERS"),
                       ("re6", "MAX_SCENES"), ("re6", "MAX_ENTRIES"),
                       ("re6", "STATUSES"), ("cw", "_STATE_NOTE")):
            self.assertIn(wanted, attrs, "情绪形状/R06 预算/M06 carve-out 必须复用 %s" % (wanted,))
        self.assertEqual(re6.MAX_SCENES, ws.MAX_EVIDENCE_SCENES)
        self.assertEqual(re6.MAX_ENTRIES, ws.MAX_EVIDENCE_ENTRIES)

    def test_no_banned_imports(self):
        banned = {"os", "time", "random", "datetime", "sqlite3", "pathlib", "socket",
                  "urllib", "requests", "http", "qq_openclaw_bridge", "memory_depth",
                  "persona_growth", "self_model", "llm_router", "emotion_shadow",
                  "history_log", "napcat_channel", "web_read", "bili",
                  "retrieval_objects", "recall_scoring"}
        self.assertEqual(set(), self.imported() & banned,
                         "C04 是纯输入契约，不该 import %s" % sorted(self.imported() & banned))

    def test_no_writer_or_db_calls(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "executemany", "commit", "connect",
                     "open", "save", "add", "add_belief", "record_event", "add_commitment",
                     "add_goal", "close_commitment", "close_goal", "record_action",
                     "write", "writelines", "dump", "touch", "write_text", "write_bytes",
                     "rollback", "revise_layer", "identity_block"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), forbidden,
                             "只读投影里出现写操作/开库 %s" % self.call_name(call))

    def test_no_upstream_entry_calls(self):
        """输入由调用方传入：不自己渲染证据、不推进动力学、不做失效传播、不落账。"""
        wanted = {"render_evidence", "resolve_ripple", "assess_expansion", "expand_paths",
                  "retrieve_evidence", "recall", "advance", "apply_event", "appraise",
                  "integrated_appraisal", "propagate", "correction", "plan_emotion_revision",
                  "read_invalidation_state", "read_derivation_inputs", "commit",
                  "apply_consolidation", "reflection_plan"}
        for call in self.calls():
            self.assertNotIn(self.call_name(call), wanted,
                             "C04 不该调用上游入口 %s" % self.call_name(call))

    def test_no_wall_clock_or_randomness(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("time", "random", "uniform", "randint", "choice",
                                  "shuffle", "now", "today", "monotonic", "strftime",
                                  "localtime", "gmtime", "perf_counter"),
                                 "C04 决策路径读了时钟/随机 %s" % node.func.attr)
        for token in ("time.time", "fromtimestamp", "isoformat"):
            self.assertNotIn(token, self.source, "出现了墙钟取时 %s" % token)

    def test_now_is_a_required_keyword_on_build_snapshot(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "build_snapshot")
        kw = [a.arg for a in fn.args.kwonlyargs]
        self.assertIn("now", kw, "build_snapshot 必须显式接收 now")
        self.assertIsNone(fn.args.kw_defaults[kw.index("now")],
                          "now 不许有默认值（无墙钟）")

    def test_render_injection_reads_the_snapshot_not_a_clock(self):
        fn = next(n for n in ast.walk(self.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "render_injection")
        names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
        self.assertNotIn("now", names, "快路径只读已计算快照，不该再接收 now")

    def test_no_sql_db_names_or_data_dir(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                             "ALTER ", "REPLACE ", "PRAGMA"):
                    self.assertNotIn(verb, upper, "C04 里出现 SQL：%s" % node.value[:60])
        for token in ("cognition.db", "emotion_shadow.db", "DATA_DIR", "sqlite"):
            self.assertNotIn(token, self.source, "纯输入契约不该碰库名/路径变量 %s" % token)

    def test_no_path_resolution_or_freezing(self):
        for call in self.calls():
            self.assertNotIn(self.call_name(call),
                             ("getenv", "abspath", "isfile", "exists"),
                             "C04 自身解析路径了；states/epoch 由调用方传入")
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径")

    def test_no_date_based_caching(self):
        for token in ("strftime", "%Y-%m-%d", '"day"', "'day'"):
            self.assertNotIn(token, self.source, "出现了按日期缓存的口径 %s" % token)
        keys = [c for c in self.calls()
                if isinstance(c.func, ast.Attribute) and c.func.attr == "cache_key"]
        self.assertTrue(keys, "缓存 key 必须走 M05 cache_key")
        for call in keys:
            self.assertIsInstance(call.func.value, ast.Name)
            self.assertEqual("dag", call.func.value.id, "缓存 key 必须复用 M05")
            self.assertEqual(5, len(call.args),
                             "五分量（对象修订/源修订/epoch/范围/版本）一个都不能少")

    def test_no_second_action_timing_engine(self):
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for word in ("should_act", "decide", "arbitrate", "schedule",
                             "when_to", "act_now", "appraise", "score", "rank"):
                    self.assertNotIn(word, node.name.lower(),
                                     "长出了第二套行动/打分判断 %s" % node.name)
        engine = {"propagate", "read_gate", "cache_key", "correction", "build_graph",
                  "check_edge", "render_evidence", "adjudicate"}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name, engine, "重定义了冻结引擎函数 %s" % node.name)

    def test_no_retired_or_control_flags(self):
        for token in ("expression_control", "counts_as_live_observation", "_MOODS"):
            self.assertNotIn(token, self.source, "出现了全局静态锁禁止的标记 %s" % token)

    def test_the_contract_constants(self):
        self.assertEqual("c04-1", ws.ALGORITHM_VERSION)
        self.assertEqual(("identity", "state", "evidence"), ws.LAYERS)
        for name in ("MAX_PENDING", "MAX_COMMITMENTS", "MAX_GOALS", "MAX_RELATIONS",
                     "MAX_STATE_CHARS", "MAX_IDENTITY_CHARS"):
            value = getattr(ws, name)
            self.assertIsInstance(value, int)
            self.assertGreater(value, 0)
        for code in ("budget", "evidence_shape_invalid", "scene_id_mismatch",
                     "missing_verdict_refs", "commitment_not_open", "goal_not_active",
                     "identity_cache_unavailable", "stale_snapshot"):
            self.assertIn(code, ws.REASON_CODES)

    def test_limitations_are_honest(self):
        self.assertIsInstance(ws.LIMITATIONS, tuple)
        self.assertGreaterEqual(len(ws.LIMITATIONS), 8)
        text = " ".join(ws.LIMITATIONS)
        for undone in ("C05", "qq_openclaw_bridge", "表达", "LLM", "M05", "M06",
                       "R06", "身体", "日期"):
            self.assertIn(undone, text, "limitations 必须如实声明 %s" % undone)


# ── 行为锁：快照构建（确定性 / 有界 / 不伪造）─────────────────────


class SnapshotTests(unittest.TestCase):
    def test_now_is_required(self):
        with self.assertRaises(TypeError):
            ws.build_snapshot()

    def test_bad_now_is_refused(self):
        for bad in (None, "x", True, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ws.build_snapshot(now=bad)

    def test_same_input_gives_the_same_snapshot_id(self):
        self.assertEqual(canonical(snap()), canonical(snap()))

    def test_input_order_does_not_change_the_snapshot(self):
        forward = snap(commitments=[commitment("c1"), commitment("c2")],
                       goals=[goal("g1"), goal("g2")],
                       pending=[waiting("p1"), waiting("p2")],
                       events=[{"id": "e1", "ts": NOW - 10}, {"id": "e2", "ts": NOW - 20}])
        backward = snap(commitments=[commitment("c2"), commitment("c1")],
                        goals=[goal("g2"), goal("g1")],
                        pending=[waiting("p2"), waiting("p1")],
                        events=[{"id": "e2", "ts": NOW - 20}, {"id": "e1", "ts": NOW - 10}])
        self.assertEqual(canonical(forward), canonical(backward))

    def test_inputs_are_not_mutated(self):
        rows = dict(commitments=[commitment()], goals=[goal()], pending=[waiting()],
                    relations=[relation()], events=[{"id": "e1", "ts": NOW - 10}],
                    emotion=emotion(), budget={"limit": 5, "used": 2})
        before = canonical(rows)
        ws.build_snapshot(now=NOW, **rows)
        self.assertEqual(before, canonical(rows))

    def test_snapshot_id_is_content_addressed(self):
        got = snap()
        payload = {k: v for k, v in got.items() if k != "snapshot_id"}
        self.assertEqual("ws_" + mm.digest(canonical(payload))[:14], got["snapshot_id"])
        other = snap(commitments=[commitment(text="别的承诺")])
        self.assertNotEqual(got["snapshot_id"], other["snapshot_id"])

    def test_the_snapshot_shape_is_the_contract_one(self):
        got = snap()
        self.assertEqual({"snapshot_id", "algorithm_version", "now", "epoch", "scope",
                          "status", "attention", "budget", "emotion", "pending",
                          "commitments", "goals", "relations", "state_version",
                          "excluded", "degradation", "provenance"}, set(got))
        self.assertEqual(got["provenance"]["state_layer_note"], cw._STATE_NOTE)
        self.assertEqual(list(ws.LIMITATIONS), got["provenance"]["limitations"])

    def test_emotion_is_projected_and_clipped_to_frozen_bounds(self):
        got = snap(emotion=emotion(valence=5.0, attention=-2.0))
        self.assertEqual(1.0, got["emotion"]["valence"])
        self.assertEqual(0.0, got["emotion"]["attention"])
        self.assertEqual(0.0, got["attention"]["load"])
        self.assertEqual("emotion_kernel", got["attention"]["basis"])

    def test_emotion_absent_degrades_without_fabricating(self):
        got = snap(emotion=None)
        self.assertIsNone(got["emotion"])
        self.assertIsNone(got["attention"]["load"])
        self.assertEqual("absent", got["attention"]["basis"])
        self.assertIn({"store": "emotion", "reason": "absent"}, got["degradation"])
        self.assertEqual("degraded", got["status"])

    def test_malformed_emotion_is_unreadable_not_fabricated(self):
        got = snap(emotion={"valence": "x"})
        self.assertIsNone(got["emotion"])
        self.assertIn({"store": "emotion", "reason": "unreadable"}, got["degradation"])

    def test_tool_failure_events_do_not_change_emotion(self):
        """接口失败 ≠ 对朋友失望：事件只计注意旁证，绝不改情绪投影。"""
        failed = [{"id": "t1", "kind": "tool_result", "ts": NOW - 10,
                   "payload": {"status": "failed"}}]
        got = snap(events=failed)
        self.assertEqual(canonical(snap()["emotion"]), canonical(got["emotion"]))
        self.assertEqual(1, got["attention"]["recent_events"])

    def test_recent_events_are_counted_inside_the_window_only(self):
        got = snap(events=[{"id": "e1", "ts": NOW - 60}, {"id": "e2", "ts": NOW - 600}])
        self.assertEqual(1, got["attention"]["recent_events"])

    def test_future_event_is_excluded_loudly(self):
        got = snap(events=[{"id": "e1", "ts": NOW + 10}])
        self.assertEqual(0, got["attention"]["recent_events"])
        self.assertIn({"object_id": "e1", "layer": "attention",
                       "reason_code": "future_event"}, got["excluded"])

    def test_pending_waiting_seconds_and_cap(self):
        rows = [waiting("pend_%02d" % i, requested_at=NOW - 10 * i) for i in range(10)]
        got = snap(pending=rows)
        self.assertEqual(ws.MAX_PENDING, len(got["pending"]))
        drops = [x for x in got["excluded"]
                 if x["layer"] == "pending" and x["reason_code"] == "budget"]
        self.assertEqual(10 - ws.MAX_PENDING, len(drops), "超限要逐条大声登记")
        first = next(r for r in got["pending"] if r["object_id"] == "pend_00")
        self.assertEqual(0.0, first["waiting_seconds"])
        last = next(r for r in got["pending"] if r["object_id"] == "pend_07")
        self.assertEqual(70.0, last["waiting_seconds"])

    def test_pending_missing_id_or_bad_time_is_excluded(self):
        got = snap(pending=[{"pending_id": "", "tool": "x", "requested_at": NOW},
                            {"pending_id": "p2", "requested_at": "abc"},
                            {"pending_id": "p3", "requested_at": NOW + 5}])
        self.assertEqual([], got["pending"])
        codes = {(x["object_id"], x["reason_code"]) for x in got["excluded"]
                 if x["layer"] == "pending"}
        self.assertEqual({("", "missing_object_id"), ("p2", "invalid_requested_at"),
                          ("p3", "future_request")}, codes)

    def test_budget_math_and_absent(self):
        got = snap(budget={"limit": 5, "used": 2})
        self.assertEqual({"limit": 5, "used": 2, "remaining": 3, "exhausted": False},
                         got["budget"])
        over = snap(budget={"limit": 5, "used": 7})
        self.assertEqual(0, over["budget"]["remaining"])
        self.assertTrue(over["budget"]["exhausted"])
        absent = snap(budget=None)
        self.assertIsNone(absent["budget"])
        self.assertIn({"store": "budget", "reason": "absent"}, absent["degradation"])

    def test_closed_commitment_and_inactive_goal_are_excluded(self):
        got = snap(commitments=[commitment(status="done")], goals=[goal(status="done")])
        self.assertEqual([], got["commitments"])
        self.assertEqual([], got["goals"])
        self.assertIn({"object_id": "cmt_1", "layer": "commitment",
                       "reason_code": "commitment_not_open"}, got["excluded"])
        self.assertIn({"object_id": "gol_1", "layer": "goal",
                       "reason_code": "goal_not_active"}, got["excluded"])

    def test_commitment_overdue_flag_and_missing_fields(self):
        got = snap(commitments=[commitment(due_ts=NOW - 10),
                                commitment("cmt_2", due_ts=None),
                                commitment("cmt_3", text=""),
                                {"id": "", "text": "无身份"}])
        rows = {r["object_id"]: r for r in got["commitments"]}
        self.assertTrue(rows["cmt_1"]["overdue"])
        self.assertFalse(rows["cmt_2"]["overdue"])
        codes = {(x["object_id"], x["reason_code"]) for x in got["excluded"]
                 if x["layer"] == "commitment"}
        self.assertIn(("cmt_3", "missing_text"), codes)
        self.assertIn(("", "missing_object_id"), codes)

    def test_duplicate_commitments_are_deduped(self):
        got = snap(commitments=[commitment(), commitment(text="重复转述")])
        self.assertEqual(1, len(got["commitments"]))

    def test_relations_are_capped_and_signal_is_carried_not_invented(self):
        rows = [relation("per_%02d" % i, affection=i) for i in range(10)]
        rows.append({"subject_ref": "per_x"})
        got = snap(relations=rows)
        self.assertEqual(ws.MAX_RELATIONS, len(got["relations"]))
        drops = [x for x in got["excluded"]
                 if x["layer"] == "relation" and x["reason_code"] == "budget"]
        self.assertEqual(11 - ws.MAX_RELATIONS, len(drops))
        unknown = snap(relations=[{"subject_ref": "per_x"}])
        self.assertIsNone(unknown["relations"][0]["affection_signal"])


# ── 行为锁：M05 失效纪律（失效来源不进任何注入层）─────────────────


class InvalidationTests(unittest.TestCase):
    def test_invalidated_commitment_never_enters_the_snapshot(self):
        got = snap(states={"cmt_1": {"status": "invalidated"}})
        self.assertEqual([], got["commitments"])
        self.assertIn({"object_id": "cmt_1", "layer": "commitment",
                       "reason_code": "invalidated"}, got["excluded"])

    def test_superseded_goal_is_excluded(self):
        got = snap(states={"gol_1": {"status": "superseded"}})
        self.assertEqual([], got["goals"])
        self.assertIn({"object_id": "gol_1", "layer": "goal",
                       "reason_code": "superseded"}, got["excluded"])

    def test_needs_reassessment_relation_is_not_current_fact(self):
        got = snap(states={"per_1001": {"status": "needs_reassessment"}})
        self.assertEqual([], got["relations"])
        self.assertIn({"object_id": "per_1001", "layer": "relation",
                       "reason_code": "needs_reassessment"}, got["excluded"])

    def test_scope_revoked_pending_is_excluded_for_matching_scope(self):
        states = {"pend_1": {"status": "active", "revoked_scopes": ["private:1001"]}}
        got = snap(states=states)
        self.assertEqual([], got["pending"])
        self.assertIn({"object_id": "pend_1", "layer": "pending",
                       "reason_code": "scope_revoked"}, got["excluded"])
        other = snap(states=states, scope="group:g1")
        self.assertEqual(1, len(other["pending"]), "范围收紧只挡匹配的范围")

    def test_real_dag_propagation_plan_feeds_the_snapshot(self):
        edges = [{"derived_id": "cmt_1", "source_id": "evt_1",
                  "derived_kind": "commitment", "source_kind": "event"}]
        rec = dag.correction("observation_invalid", "evt_1", "撤回这条观察", ts=NOW)
        prop = dag.propagate(edges, [rec], epoch=0, states={}, now=NOW)
        states = {a["object_id"]: {"status": a["status"],
                                   "revoked_scopes": a["revoked_scopes"]}
                  for a in prop["affected"]}
        got = snap(states=states, epoch=prop["epoch"])
        self.assertEqual([], got["commitments"], "源被撤回，派生承诺不得进快照")
        self.assertIn({"object_id": "cmt_1", "layer": "commitment",
                       "reason_code": "invalidated"}, got["excluded"])

    def test_epoch_advance_drops_stale_snapshot_at_render(self):
        got = render(snap(), epoch=1)
        self.assertIn({"store": "work_state", "reason": "stale_snapshot"},
                      got["degradation"])
        self.assertIn({"object_id": "cmt_1", "layer": "commitment",
                       "reason_code": "stale_projection"}, got["excluded"])
        self.assertNotIn("帮查荒坂塔资料", got["state"]["text"])

    def test_fresh_invalidation_applies_at_render_without_rebuild(self):
        got = render(snap(), states={"cmt_1": {"status": "invalidated"}})
        self.assertIn({"object_id": "cmt_1", "layer": "commitment",
                       "reason_code": "invalidated"}, got["excluded"])
        self.assertNotIn("帮查荒坂塔资料", got["state"]["text"])

    def test_invalidated_evidence_entry_is_excluded_at_render(self):
        got = render(states={"s": {"status": "invalidated"}})
        ids = {e["object_id"] for sc in got["evidence"]["scenes"] for e in sc["entries"]}
        self.assertEqual({"n"}, ids)
        self.assertIn({"object_id": "s", "layer": "evidence",
                       "reason_code": "invalidated"}, got["excluded"])

    def test_clean_states_exclude_nothing(self):
        got = snap(states={})
        self.assertEqual([], got["excluded"])
        self.assertEqual("ok", got["status"])


# ── 行为锁：三层边界与缓存失效即时性 ──────────────────────────────


class InjectionTests(unittest.TestCase):
    def test_three_layers_are_present(self):
        got = render()
        for layer in ws.LAYERS:
            self.assertIn(layer, got)
        self.assertEqual(got["snapshot_id"], snap()["snapshot_id"])

    def test_identity_text_is_carried_verbatim(self):
        got = render()
        self.assertEqual(identity()["text"], got["identity"]["text"])
        self.assertTrue(got["identity"]["cacheable"])

    def test_identity_is_truncated_loudly(self):
        got = render(identity=identity(text="长" * 600))
        self.assertTrue(got["identity"]["truncated"])
        self.assertLessEqual(len(got["identity"]["text"]), ws.MAX_IDENTITY_CHARS + 1)

    def test_identity_absent_degrades_without_fabricating(self):
        got = render(identity=None)
        self.assertEqual("", got["identity"]["text"])
        self.assertIn({"store": "identity", "reason": "absent"}, got["degradation"])
        self.assertEqual("degraded", got["status"])

    def test_identity_cache_hits_on_second_render(self):
        first = render()
        second = render(cache=first["cache"])
        self.assertTrue(second["cache_hits"]["identity"])
        self.assertEqual(first["identity"]["text"], second["identity"]["text"])
        self.assertEqual(first["identity"]["cache_key"], second["identity"]["cache_key"])

    def test_identity_cache_invalidated_by_epoch_within_the_same_day(self):
        first = render()
        second = render(cache=first["cache"], epoch=1)
        self.assertFalse(second["cache_hits"]["identity"],
                         "epoch 推进必须立即失效身份缓存，不许按日期缓存")
        self.assertNotEqual(first["identity"]["cache_key"], second["identity"]["cache_key"])

    def test_identity_cache_invalidated_by_source_revision(self):
        first = render()
        second = render(identity=identity(source_revision="src_2"), cache=first["cache"])
        self.assertFalse(second["cache_hits"]["identity"])

    def test_identity_without_revisions_is_not_cacheable_loudly(self):
        got = render(identity=identity(object_revision=""))
        self.assertFalse(got["identity"]["cacheable"])
        self.assertIn({"store": "identity", "reason": "cache_key_unavailable"},
                      got["degradation"])
        self.assertIn({"object_id": "identity", "layer": "identity",
                       "reason_code": "identity_cache_unavailable"}, got["excluded"])
        self.assertEqual(identity()["text"], got["identity"]["text"], "内容照渲，只是不缓存")

    def test_identity_without_scope_is_not_cacheable(self):
        got = render(snap(scope=""))
        self.assertFalse(got["identity"]["cacheable"])

    def test_identity_stays_stable_across_emotion_swings(self):
        """今天不想说 ≠ 人格变冷：情绪摆动只动状态层，身份层纹丝不动。"""
        happy = render(snap(emotion=emotion(valence=0.8)))
        quiet = render(snap(emotion=emotion(valence=-0.8)), cache=happy["cache"])
        self.assertEqual(happy["identity"]["text"], quiet["identity"]["text"])
        self.assertTrue(quiet["cache_hits"]["identity"])
        self.assertNotEqual(happy["state"]["text"], quiet["state"]["text"])
        self.assertNotIn("效价", quiet["identity"]["text"])

    def test_state_layer_carries_version_and_timestamp(self):
        got = render()
        self.assertEqual(snap()["state_version"], got["state"]["state_version"])
        self.assertEqual(NOW, got["state"]["now"])
        self.assertEqual(snap()["snapshot_id"], got["state"]["snapshot_id"])

    def test_new_commitment_invalidates_state_cache_immediately(self):
        first = render(snap())
        second = render(snap(commitments=[commitment(),
                                          commitment("cmt_2", text="周五前把图发给他")],
                             now=NOW + 60), cache=first["cache"])
        self.assertFalse(second["cache_hits"]["state"],
                         "新承诺必须立即生效，不许为缓存等到第二天")
        self.assertIn("周五前把图发给他", second["state"]["text"])
        self.assertNotEqual(first["state"]["state_version"], second["state"]["state_version"])
        self.assertTrue(second["cache_hits"]["identity"], "身份层不受状态变化牵连")

    def test_new_goal_invalidates_state_cache_immediately(self):
        first = render(snap())
        second = render(snap(goals=[goal(), goal("gol_2", text="学会做咖啡")]),
                        cache=first["cache"])
        self.assertFalse(second["cache_hits"]["state"])
        self.assertIn("学会做咖啡", second["state"]["text"])

    def test_emotion_change_invalidates_state_cache(self):
        first = render(snap())
        second = render(snap(emotion=emotion(valence=-0.5)), cache=first["cache"])
        self.assertFalse(second["cache_hits"]["state"])

    def test_same_snapshot_hits_the_state_cache(self):
        shared = snap()
        first = render(shared)
        second = render(shared, cache=first["cache"])
        self.assertTrue(second["cache_hits"]["state"])
        self.assertEqual(first["state"]["text"], second["state"]["text"])

    def test_state_budget_truncates_loudly_and_keeps_commitments_first(self):
        shared = snap(commitments=[commitment("cmt_1", text="第一个承诺"),
                                   commitment("cmt_2", text="第二个承诺")])
        got = render(shared, max_state_chars=45)
        self.assertTrue(got["state"]["truncated"])
        self.assertGreaterEqual(got["state"]["dropped_lines"], 1)
        self.assertIn("第一个承诺", got["state"]["text"])
        self.assertNotIn("第二个承诺", got["state"]["text"])
        self.assertLessEqual(len(got["state"]["text"]), 45)

    def test_zero_state_budget_is_loud_not_silent(self):
        got = render(max_state_chars=0)
        self.assertTrue(got["state"]["truncated"])
        self.assertEqual("", got["state"]["text"])
        self.assertGreater(got["state"]["dropped_lines"], 0)

    def test_state_cache_invalidated_by_budget_change(self):
        first = render(max_state_chars=1200)
        second = render(cache=first["cache"], max_state_chars=40)
        self.assertFalse(second["cache_hits"]["state"])

    def test_cache_input_is_not_mutated_and_output_is_new(self):
        cache = {"identity": {}, "state": None}
        before = canonical(cache)
        got = render(cache=cache)
        self.assertEqual(before, canonical(cache))
        self.assertIsNot(cache, got["cache"])
        self.assertTrue(got["cache"]["identity"])

    def test_render_does_not_mutate_the_snapshot(self):
        shared = snap()
        before = canonical(shared)
        got = render(shared)
        got["state"]["text"] = "mutated"
        got["evidence"]["scenes"] = []
        self.assertEqual(before, canonical(shared))

    def test_render_requires_a_real_snapshot(self):
        for bad in (None, {}, {"snapshot_id": "ws_x"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ws.render_injection(bad)

    def test_absent_state_inputs_are_honest_not_fabricated(self):
        got = render(snap(emotion=None, budget=None), evidence=None)
        self.assertIn("缺输入，不编造", got["state"]["text"])
        self.assertIn("工具预算：缺输入，未知", got["state"]["text"])
        self.assertEqual("degraded", got["status"])

    def test_pending_is_rendered_as_waiting_not_completed(self):
        got = render()
        self.assertIn("结果未回，不算完成", got["state"]["text"])
        self.assertIn("已等 120 秒", got["state"]["text"])


# ── 行为锁：证据层只认 R06 形状，不伪造经历 ───────────────────────


class EvidenceTests(unittest.TestCase):
    def test_real_r06_output_passes_through_with_verdict_refs(self):
        ev = evidence_two_scenes()
        got = render(evidence=ev)
        self.assertEqual(2, len(got["evidence"]["scenes"]))
        self.assertEqual("sufficient", got["evidence"]["status"])
        for scene in got["evidence"]["scenes"]:
            for entry in scene["entries"]:
                self.assertIsInstance(entry["verdict_refs"], list)
                self.assertTrue(entry["verdict_refs"])
        source = {e["object_id"]: e for sc in ev["scenes"] for e in sc["entries"]}
        for scene in got["evidence"]["scenes"]:
            for entry in scene["entries"]:
                self.assertEqual(source[entry["object_id"]]["verdict_refs"],
                                 entry["verdict_refs"], "verdict 引用必须逐字来自 R06")

    def test_restatement_is_verbatim_not_fabricated(self):
        got = render()
        texts = {e["object_id"]: e["restatement"]
                 for sc in got["evidence"]["scenes"] for e in sc["entries"]}
        self.assertEqual({"s": "喜欢茶", "n": "喜欢咖啡"}, texts)

    def test_evidence_absent_degrades_without_fabricating(self):
        got = render(evidence=None)
        self.assertEqual([], got["evidence"]["scenes"])
        self.assertEqual("degraded", got["evidence"]["status"])
        self.assertIn({"store": "evidence", "reason": "absent"}, got["degradation"])

    def test_non_dict_evidence_is_rejected_loudly(self):
        got = render(evidence=["经历一", "经历二"])
        self.assertEqual([], got["evidence"]["scenes"])
        self.assertIn({"object_id": "", "layer": "evidence",
                       "reason_code": "evidence_shape_invalid"}, got["excluded"])
        self.assertIn({"store": "evidence", "reason": "invalid_shape"}, got["degradation"])

    def test_missing_skeleton_or_bad_status_is_rejected(self):
        ev = evidence_two_scenes()
        broken = {k: v for k, v in ev.items() if k != "manifest"}
        got = render(evidence=broken)
        self.assertEqual([], got["evidence"]["scenes"])
        bogus = dict(ev, status="bogus")
        got = render(evidence=bogus)
        self.assertEqual([], got["evidence"]["scenes"])

    def test_forged_scene_id_is_rejected(self):
        ev = evidence_two_scenes()
        ev["scenes"][0]["scene_id"] = "scene_forged"
        got = render(evidence=ev)
        keys = {sc["scene_key"] for sc in got["evidence"]["scenes"]}
        self.assertNotIn(ev["scenes"][0]["scene_key"], keys)
        self.assertIn({"object_id": "scene_forged", "layer": "evidence",
                       "reason_code": "scene_id_mismatch"}, got["excluded"])

    def test_entry_without_verdict_refs_is_dropped(self):
        ev = evidence_two_scenes()
        target = ev["scenes"][0]["entries"][0]["object_id"]
        del ev["scenes"][0]["entries"][0]["verdict_refs"]
        got = render(evidence=ev)
        ids = {e["object_id"] for sc in got["evidence"]["scenes"] for e in sc["entries"]}
        self.assertNotIn(target, ids)
        self.assertIn({"object_id": target, "layer": "evidence",
                       "reason_code": "missing_verdict_refs"}, got["excluded"])

    def test_entry_without_object_id_is_dropped(self):
        ev = evidence_two_scenes()
        ev["scenes"][0]["entries"][0]["object_id"] = ""
        got = render(evidence=ev)
        self.assertIn({"object_id": "", "layer": "evidence",
                       "reason_code": "missing_object_id"}, got["excluded"])

    def test_entry_budget_drops_loudly(self):
        got = render(max_entries=1)
        self.assertEqual(1, got["evidence"]["counts"]["entries"])
        drops = [x for x in got["excluded"]
                 if x["layer"] == "evidence" and x["reason_code"] == "budget"]
        self.assertEqual(1, len(drops))
        self.assertEqual("partial", got["evidence"]["status"])

    def test_scene_budget_drops_loudly(self):
        got = render(max_scenes=1)
        self.assertEqual(1, len(got["evidence"]["scenes"]))
        drops = [x for x in got["excluded"]
                 if x["layer"] == "evidence" and x["reason_code"] == "budget"]
        self.assertTrue(drops)

    def test_zero_entry_budget_drops_all_loudly(self):
        got = render(max_entries=0)
        self.assertEqual([], got["evidence"]["scenes"])
        drops = [x for x in got["excluded"]
                 if x["layer"] == "evidence" and x["reason_code"] == "budget"]
        self.assertEqual(2, len(drops))

    def test_degraded_r06_status_is_passed_through(self):
        s = claim_item("s", "喜欢茶", ["e1"])
        resolved = rc.resolve_ripple([s], [], degradation=[
            {"store": "group", "reason": "absent", "detail": ""}])
        ev = re6.render_evidence(resolved, items=[s], seeds=[s])
        got = render(evidence=ev)
        self.assertEqual("degraded", got["evidence"]["status"])
        self.assertEqual(1, len(got["evidence"]["scenes"]), "降级不抹掉可用证据")
        self.assertEqual(ev["degradation"], got["evidence"]["degradation"])

    def test_conflicting_r06_yields_no_scenes(self):
        s = claim_item("s", "喜欢冰可乐", ["e1"])
        n = claim_item("n", "讨厌冰可乐", ["e2"])
        resolved = rc.resolve_ripple([s], [edge("s", "n")], items=[s, n])
        ev = re6.render_evidence(resolved, items=[s, n], seeds=[s])
        got = render(evidence=ev)
        self.assertEqual("conflicting", got["evidence"]["status"])
        self.assertEqual([], got["evidence"]["scenes"])

    def test_evidence_output_is_a_deep_copy(self):
        ev = evidence_two_scenes()
        before = canonical(ev)
        got = render(evidence=ev)
        got["evidence"]["scenes"][0]["entries"][0]["restatement"] = "mutated"
        got["evidence"]["scenes"][0]["entries"][0]["verdict_refs"][0]["verdict"] = "mutated"
        self.assertEqual(before, canonical(ev))


# ── 端到端：隔离 DATA_DIR，经 M06 单一写入者与真实 R06 链路 ────────


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="c04_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def seed_event(self, text, dedupe, ts=1000.0):
        return sm.record_event("private_message", session="private:1001",
                               actor_qq="1001", visibility="private",
                               payload={"text": text}, ts=ts, dedupe=dedupe)

    def rows(self, table):
        with closing(sm._db()) as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM %s" % table)]

    def group_facts(self, entries):
        path = os.path.join(self.root, "memory", "group_facts.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"groups": {"g1": {"entries": entries}}}, handle, ensure_ascii=False)

    def fact(self, fid, text, eid, ts=1000.0):
        return {"id": fid, "text": text, "ts": ts, "source_event_ids": [eid],
                "status": "active",
                "evidence_spans": [{"event_id": eid, "quote": text,
                                    "subject_ref": "per_1",
                                    "subject_basis": "source_actor"}]}

    def real_evidence(self, budget=2):
        ctx = ro.query_context(recipient_scope="private:1001",
                               allowed_source_scopes=["private:1001", "group:g1"],
                               as_of_time=NOW, query_text="阿甲喜欢什么",
                               recent_referents=["阿甲"])
        fetched = ro.retrieve_evidence(ctx)
        edges = ae.project_edges(fetched["items"], fetched["degradation"])["edges"]
        recalled = rs.recall(fetched["items"], dict(ctx, retrieval_budget=budget))
        resolved = rc.resolve_ripple(recalled["items"], edges, items=fetched["items"],
                                     degradation=fetched["degradation"])
        return re6.render_evidence(resolved, items=fetched["items"],
                                   seeds=recalled["items"])

    def commitment_via_m06(self, oid, text, eid):
        window = sm.source_window([eid])
        candidate = {"object_kind": "commitment", "object_id": oid, "content": text,
                     "occurred_at": 1000.0, "recorded_at": 1001.0,
                     "source_event_ids": [eid], "epistemic_type": "experienced",
                     "visibility": "private", "source_session": "private:1001",
                     "subject_ref": window[0]["actor_ref"], "to_qq": "1001",
                     "accepted_by_self": True, "completion_criteria": text,
                     "evidence": [{"event_id": eid, "quote": text}]}
        return cw.commit(candidate, window, now=NOW)


class IntegrationTests(StoreFixture):
    def test_pure_contract_creates_nothing_in_an_empty_data_dir(self):
        before = snapshot_tree(self.root)
        got = render()
        self.assertEqual("ok", got["status"])
        self.assertEqual(before, snapshot_tree(self.root))
        self.assertEqual([], list(Path(self.root).iterdir()), "纯契约顺手建了目录")

    def test_m06_committed_commitment_flows_into_the_snapshot(self):
        eid = self.seed_event("帮我查一下荒坂塔", "c04:c1")
        projection = self.commitment_via_m06("c04c1", "帮我查一下荒坂塔", eid)
        self.assertEqual("accept", projection["decision"])
        rows = self.rows("commitment")
        self.assertEqual(1, len(rows))
        got = snap(commitments=rows)
        self.assertEqual(1, len(got["commitments"]))
        self.assertEqual(rows[0]["id"], got["commitments"][0]["object_id"])
        self.assertEqual("帮我查一下荒坂塔", got["commitments"][0]["text"])
        injected = render(got)
        self.assertIn("帮我查一下荒坂塔", injected["state"]["text"])

    def test_state_kind_stays_out_of_the_cognition_ledger(self):
        """M06 carve-out：临时状态不落认知账本，只进工作状态层（_STATE_NOTE 同口径）。"""
        eid = self.seed_event("现在有点忙", "c04:s1")
        window = sm.source_window([eid])
        candidate = {"object_kind": "state", "object_id": "c04s1", "content": "现在有点忙",
                     "occurred_at": 1000.0, "recorded_at": 1001.0,
                     "source_event_ids": [eid], "epistemic_type": "experienced",
                     "visibility": "private", "source_session": "private:1001",
                     "subject_ref": window[0]["actor_ref"], "valid_until": NOW + 3600,
                     "evidence": [{"event_id": eid, "quote": "现在有点忙"}]}
        live = cw.commit(candidate, window, now=NOW)
        self.assertEqual({}, live["object_ids"])
        self.assertIn(cw._STATE_NOTE, live["reasons"])
        got = snap()
        self.assertEqual(cw._STATE_NOTE, got["provenance"]["state_layer_note"])

    def test_goal_row_flows_into_the_snapshot(self):
        sm.add_goal("弄清荒坂塔原型", owner_qq="1001", motive="求知", ts=1000.0)
        rows = sm.active_goals()
        self.assertEqual(1, len(rows))
        got = snap(goals=rows)
        self.assertEqual(1, len(got["goals"]))
        self.assertIn("弄清荒坂塔原型", render(got)["state"]["text"])

    def test_real_invalidation_transaction_excludes_the_commitment(self):
        eid = self.seed_event("帮我查一下荒坂塔", "c04:c2")
        projection = self.commitment_via_m06("c04c2", "帮我查一下荒坂塔", eid)
        cid = projection["object_ids"]["commitment"]
        edge_row = {"derived_id": cid, "source_id": eid,
                    "derived_kind": "commitment", "source_kind": "event",
                    "basis": "c04.test"}
        sm.write_invalidation_transaction({"ts": NOW, "expected_epoch": 0,
                                           "edges": [edge_row], "corrections": []})
        st = dag.read_invalidation_state()
        rec = dag.correction("observation_invalid", eid, "撤回这条观察", ts=NOW + 10)
        prop = dag.propagate(st["edges"], [rec], epoch=st["epoch"],
                             states=st["states"], now=NOW + 10)
        sm.write_invalidation_transaction({"ts": NOW + 10, "expected_epoch": st["epoch"],
                                           "edges": [], "corrections": [rec],
                                           "propagation": prop})
        after = dag.read_invalidation_state()
        self.assertEqual("invalidated", after["states"][cid]["status"])
        got = snap(commitments=self.rows("commitment"),
                   states=after["states"], epoch=after["epoch"])
        self.assertEqual([], got["commitments"], "真实失效账本说失效，就不得进注入")
        self.assertIn({"object_id": cid, "layer": "commitment",
                       "reason_code": "invalidated"}, got["excluded"])

    def test_real_r06_chain_feeds_the_evidence_layer(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1"),
                          self.fact("f2", "喜欢咖啡", "ge2", ts=1001.0)])
        ev = self.real_evidence()
        got = render(evidence=ev)
        self.assertEqual(2, len(got["evidence"]["scenes"]))
        ids = {e["object_id"] for sc in got["evidence"]["scenes"] for e in sc["entries"]}
        self.assertEqual({"group:g1:f1", "group:g1:f2"}, ids)
        for scene in got["evidence"]["scenes"]:
            for entry in scene["entries"]:
                self.assertIn("verdict_refs", entry)

    def test_the_full_pipeline_rewrites_no_asset(self):
        self.group_facts([self.fact("f1", "喜欢茶", "ge1")])
        eid = self.seed_event("帮我查一下荒坂塔", "c04:c3")
        self.commitment_via_m06("c04c3", "帮我查一下荒坂塔", eid)
        before = snapshot_tree(self.root)
        ev = self.real_evidence()
        got = render(snap(commitments=self.rows("commitment")), evidence=ev)
        self.assertTrue(got["state"]["text"])
        self.assertEqual(before, snapshot_tree(self.root),
                         "快照/注入链路顺手写了资产，就不再是只读投影")

    def test_data_dir_switch_does_not_affect_the_pure_contract(self):
        with tempfile.TemporaryDirectory(prefix="c04_other_") as other:
            with patch.dict(os.environ, {"DATA_DIR": other, "PERSONA_DATA_DIR": other}):
                got = render()
                self.assertEqual("ok", got["status"])
                self.assertEqual([], list(Path(other).iterdir()))


if __name__ == "__main__":
    unittest.main()