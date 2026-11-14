"""阶段0 统一证据：稳定身份、时间与范围、权威写入、撤回与观察。

这些锁只证明「证据链契约」本身，不证明任何认知层能力：
  · 同一个观察重复提交不能变成两份证据；
  · 事件 ID 必须由内容决定，换一进程/换一天仍然一致；
  · 动作的收件人、话题、范围必须随回执一起留下来；
  · cognition.db 只能由 self_model 写，其他模块只能只读取证；
  · 撤回证据不能改写原始观察，只能让下游失效；
  · 「验证/模拟」产生的观察必须自我标识，不能冒充亲历。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import self_model as sm          # noqa: E402
import phase_audit               # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stage0_")
        self._old = os.environ.get("PERSONA_DATA_DIR"), os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp
        os.environ.pop("PERSONA_DATA_DIR", None)

    def tearDown(self):
        for key, value in zip(("PERSONA_DATA_DIR", "DATA_DIR"), self._old):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cognition(self):
        return Path(self.tmp) / "persona" / "cognition.db"

    def rows(self, sql, args=()):
        import sqlite3
        connection = sqlite3.connect(str(self.cognition()))
        try:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, args)]
        finally:
            connection.close()


class StableIdentityTests(Base):
    def test_dedupe_is_enforced_by_the_database_not_just_by_python(self):
        first = sm.record_event("group_message", session="group:1", actor_qq="1001",
                                payload={"text": "a"}, dedupe="g:1:seq:7")
        second = sm.record_event("group_message", session="group:1", actor_qq="1001",
                                 payload={"text": "b"}, dedupe="g:1:seq:7")
        self.assertEqual(first, second, "同一个 dedupe 被记成了两件事")
        self.assertEqual(1, len(self.rows("SELECT id FROM event WHERE dedupe='g:1:seq:7'")))

    def test_event_id_is_deterministic_for_the_same_observation(self):
        first = sm.record_event("private_message", session="private:1001",
                                actor_qq="1001", payload={"text": "在吗"},
                                dedupe="p:1001:seq:3")
        with tempfile.TemporaryDirectory(prefix="stage0_other_") as other:
            os.environ["DATA_DIR"] = other
            second = sm.record_event("private_message", session="private:1001",
                                     actor_qq="1001", payload={"text": "在吗"},
                                     dedupe="p:1001:seq:3")
        os.environ["DATA_DIR"] = self.tmp
        self.assertEqual(first, second, "同一观察换一个账本就不是同一个 ID 了")

    def test_receipt_event_is_idempotent_per_action_and_state(self):
        action = sm.record_action("dm", target_qq="1001", topic_qq="1001",
                                  visibility="private", scope="dm:1001", text="在吗")
        self.assertTrue(sm.mark_action(action, "accepted", "ok"))
        self.assertTrue(sm.mark_action(action, "accepted", "ok"))
        receipts = self.rows(
            "SELECT id FROM event WHERE kind='action_result' AND payload LIKE ?",
            ('%"action_id": "{}"%'.format(action),))
        self.assertEqual(1, len(receipts), "同一终态被记成多条回执")


class TimeScopeTests(Base):
    def test_normal_writes_never_put_occurrence_after_creation(self):
        for index in range(3):
            sm.record_event("observation", dedupe="time:%d" % index)
        sm.record_tool_result("tarot", "run-1", ok=True, session="private:1001")
        report = phase_audit.audit(str(Path(self.tmp) / "persona"))
        checks = report["checks"]["cognition"]
        self.assertEqual(0, checks["time_order"]["violations"])
        self.assertEqual(0, checks["time_order"]["subsecond_skew"])

    def test_receipt_keeps_the_action_scope_not_the_receipt_context(self):
        action = sm.record_action("group", target_qq="1002", topic_qq="1001",
                                  visibility="group", scope="group:55",
                                  text="转告", evidence=["evt_x"])
        self.assertTrue(sm.mark_action(action, "failed", "blocked"))
        event = self.rows(
            "SELECT * FROM event WHERE kind='action_result' AND payload LIKE ?",
            ('%"action_id": "{}"%'.format(action),))[0]
        self.assertEqual("group:55", event["session"])
        self.assertEqual(sm.person_id("1001"), event["topic_qq"])
        self.assertEqual("group", event["visibility"])
        self.assertEqual("onebot_receipt", event["source"])

    def test_audit_exposes_actions_missing_topic_or_evidence(self):
        sm.record_action("dm", target_qq="1001", text="没有话题也没有证据")
        report = phase_audit.audit(str(Path(self.tmp) / "persona"))
        checks = report["checks"]["cognition"]
        self.assertEqual(0, checks["action_topic"])
        self.assertEqual(0, checks["action_evidence"])
        aid = sm.record_action("dm", target_qq="1001", topic_qq="1001",
                               evidence=["evt_1"], text="带话题和证据")
        self.assertTrue(aid)
        report = phase_audit.audit(str(Path(self.tmp) / "persona"))
        checks = report["checks"]["cognition"]
        self.assertEqual(1, checks["action_topic"])
        self.assertEqual(1, checks["action_evidence"])


class WriteInventoryTests(unittest.TestCase):
    WRITE_VERBS = ("INSERT INTO", "INSERT OR IGNORE INTO", "INSERT OR REPLACE INTO",
                   "UPDATE ", "DELETE FROM", "REPLACE INTO")
    TABLES = ("event", "episode", "belief", "belief_evidence", "belief_revision",
              "commitment", "goal", "action", "self_view_rev", "person",
              "evidence_retraction", "cognition_invalidation", "layer_revision",
              "claim_span", "claim_candidate",
              "derivation_edge", "correction_record", "object_validity_state",
              "invalidation_snapshot")

    def sources(self):
        files = []
        for folder in ("bot", "website"):
            files.extend(p for p in (ROOT / folder).rglob("*.py")
                         if "__pycache__" not in p.parts)
        files.append(ROOT / "launcher.py")
        return [p for p in files if p.is_file()]

    def test_only_self_model_writes_cognition_tables(self):
        offenders = []
        for path in self.sources():
            if path.name == "self_model.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), 1):
                if not any(verb in line for verb in self.WRITE_VERBS):
                    continue
                import re
                match = re.search(r'(?:INTO|UPDATE|FROM)\s+[`"]?(\w+)', line, re.I)
                if match and match.group(1).lower() in self.TABLES:
                    offenders.append("%s:%d %s" % (path.relative_to(ROOT), line_number,
                                                   line.strip()[:80]))
        self.assertEqual([], offenders, "绕开 self_model 直接写认知库")

    def test_cognition_readers_are_a_known_short_list(self):
        allowed = {"self_model.py", "phase_audit.py", "qingyi_preflight.py",
                   "emotion_shadow.py", "valence_candidates.py",
                   "audit_baseline.py", "retrieval_objects.py",
                   # M03 归一契约只经 self_model._person_qq_map 只读反查人物，
                   # 库 absent 返回空、绝不建库；只读性由本类写入锁 + 其自身 AST 锁证明。
                   "entity_normalization.py",
                   # M04 矛盾裁决的全库候选收集：mode=ro + query_only，
                   # 缺库返回空、绝不建库；只读性由本类写入锁 + 其自身 AST 锁证明。
                   "claim_conflicts.py",
                   # M05 失效账本与各库派生关系的只读桥：mode=ro + query_only，
                   # 缺库缺文件返回空/降级、绝不建库；只读性由写入锁 + 其自身 AST 锁证明。
                   "invalidation_dag.py",
                   # S03 情感重算输入只读桥：mode=ro + query_only 读 event/
                   # correction_record/evidence_retraction，缺库报 absent、绝不建库；
                   # 只读性由写入锁 + 其自身 AST 锁证明。
                   "emotion_revision.py",
                   # 人工标注挑候选来源：mode=ro 只读 event 文本，绝不建库/写库。
                   "annotation_queue.py",
                   # 证据介入：只读挑选候选/统计，写入统一走 self_model。
                   "evidence_repair.py", "memory_governance.py",
                   # 等待时间估算：mode=ro 只读 self_view_rev 速率，绝不写。
                   "ops_autopilot.py",
                   # S03/M05 候选回放与记忆整理：mode=ro + query_only 只读桥，
                   # 缺库报错不建库；只读性由本类写入锁证明。
                   "candidate_replay.py",
                   "memory_consolidation.py",
                   # L03 习惯/关系更新是纯计划契约：自身零开库，只经 M05
                   # read_invalidation_state 只读桥（mode=ro + query_only，缺库报
                   # absent、绝不建库）；docstring 提及 cognition.db 仅为声明尚无
                   # habit/relationship 表。只读性由本类写入锁 + 其自身 AST 锁证明。
                   "habit_relationship.py"}
        readers = {p.name for p in self.sources()
                   if "cognition.db" in p.read_text(encoding="utf-8", errors="replace")}
        self.assertEqual(set(), readers - allowed,
                         "新增了未登记的 cognition.db 读取者，需要证明它只读")


class RetractionObservationTests(Base):
    def test_retraction_invalidates_action_without_rewriting_it(self):
        event = sm.record_event("private_message", actor_qq="1001", dedupe="retract:act")
        action = sm.record_action("dm", target_qq="1001", topic_qq="1001",
                                  evidence=[event], text="在吗")
        before = self.rows("SELECT * FROM action WHERE id=?", (action,))[0]
        self.assertTrue(sm.retract_evidence(event, "source corrected"))
        after = self.rows("SELECT * FROM action WHERE id=?", (action,))[0]
        self.assertEqual(before["text"], after["text"])
        self.assertFalse(sm.evidence_valid("action", action))
        self.assertEqual(1, len(self.rows(
            "SELECT * FROM cognition_invalidation WHERE kind='action' AND object_id=?",
            (action,))))

    def test_verification_observations_are_self_identified_and_never_live(self):
        synthetic = sm.record_tool_result("tarot", "run-s", outcome="unknown",
                                          session="verify:stage0")
        live = sm.record_tool_result("tarot", "run-l", ok=True, session="private:1001")
        synthetic_payload = sm.get_event(synthetic)["payload"]
        live_payload = sm.get_event(live)["payload"]
        import json
        synthetic_payload = json.loads(synthetic_payload)
        live_payload = json.loads(live_payload)
        self.assertEqual("verification", synthetic_payload["evidence_origin"])
        self.assertTrue(synthetic_payload.get("synthetic"))
        self.assertEqual("live", live_payload["evidence_origin"])
        self.assertNotIn("synthetic", live_payload)
        self.assertEqual(None, synthetic_payload["ok"])
        self.assertEqual("unknown", synthetic_payload["outcome"])

    def test_retracted_observation_stays_readable_for_audit_but_unusable(self):
        event = sm.record_event("tool_result", payload={"tool": "tarot"},
                                dedupe="retract:obs")
        self.assertTrue(sm.retract_evidence(event, "misread the tool output"))
        self.assertIsNotNone(sm.get_event(event), "撤回不等于删除原始观察")
        self.assertFalse(sm.evidence_valid("event", event))


class IsolationFixtureTests(unittest.TestCase):
    def test_runner_redirects_every_data_path_before_tests_import(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_all", ROOT / "tests" / "run_all.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        env, workdir = module.isolated_env()
        for key in ("DATA_DIR", "PERSONA_DATA_DIR", "CEYLAN_CONTROL_DIR",
                    "CEYLAN_FUN_DIR", "CEYLAN_KEYS_DIR", "CEYLAN_OBSERVER_FILE",
                    "CEYLAN_BOT_SNAPSHOT", "CEYLAN_MODEL_ENV", "CEYLAN_TELEMETRY_DB",
                    "CEYLAN_DB_PATH", "CEYLAN_BOT_STATUS_FILE", "CEYLAN_CLIPBOARD_DB"):
            self.assertIn(key, env)
            if key != "PERSONA_DATA_DIR":
                self.assertTrue(env[key].startswith(workdir), key)
        self.assertEqual(os.pathsep.join([str(ROOT / "tests"), str(ROOT / "bot"), str(ROOT) ]),
                         env["PYTHONPATH"])


if __name__ == "__main__":
    unittest.main()