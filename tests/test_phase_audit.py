"""回归锁：阶段 0/1/2 的只读验收。

为什么需要：本地 `data/` 不是生产数据（同步脚本永不动 data/），
所以「本地账本空」不能证明线上没跑，反过来也一样。
这个审计只读 cognition.db / emotion_shadow.db，输出计数与可验证的退出条件，
不打印任何聊天原文、QQ 号或 payload。
"""
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
import tempfile
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import phase_audit          # noqa: E402
import self_model as sm     # noqa: E402
import emotion_core as core  # noqa: E402
import emotion_shadow as shadow  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phaseaudit_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR", "PERSONA_DATA_DIR")}
        os.environ["DATA_DIR"] = self.dir
        os.environ.pop("PERSONA_DATA_DIR", None)
        self.root = os.path.join(self.dir, "persona")
        os.makedirs(self.root, exist_ok=True)
        self._clear()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def _clear(self):
        for name in ("cognition.db", "emotion_shadow.db"):
            p = os.path.join(self.root, name)
            if os.path.exists(p):
                os.remove(p)
        for suffix in ("-wal", "-shm"):
            for name in ("cognition.db", "emotion_shadow.db"):
                p = os.path.join(self.root, name + suffix)
                if os.path.exists(p):
                    os.remove(p)


class EmptyTests(Base):
    def test_empty_ledgers_are_reported_as_not_started_not_pass(self):
        rep = phase_audit.audit(root=self.root)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["phases"]["phase0"]["status"], "not_started")
        self.assertEqual(rep["phases"]["phase1"]["status"], "not_started")
        self.assertEqual(rep["phases"]["phase2"]["status"], "not_started")


class CognitionTests(Base):
    def test_phase0_tracks_action_and_receipt(self):
        ev = sm.record_event("group_message", ts=1000, session="group:1",
                             actor_qq="123456", topic_qq="123456",
                             payload={"text": "今天聊什么"}, dedupe="g:1:mid:1")
        sm.record_tool_result("search", "topic", True, session="s", run_id="r1")
        aid = sm.record_action("group", target_qq="123456", topic_qq="123456",
                               scope="group:1", text="回复", reason="测试",
                               evidence=[ev], state="submitted")
        self.assertTrue(sm.mark_action(aid, "accepted", result="0"))
        rep = phase_audit.audit(root=self.root)
        p0 = rep["phases"]["phase0"]
        self.assertEqual(p0["status"], "pass", p0)
        self.assertEqual(rep["counts"]["cognition"]["action"], 1)
        self.assertEqual(rep["checks"]["cognition"]["action_receipts"], 1)
        self.assertEqual(rep["checks"]["cognition"]["time_order"]["violations"], 0)

    def test_events_without_actions_are_partial(self):
        sm.record_event("group_message", ts=1000, dedupe="g:1:mid:3")
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["phases"]["phase0"]["status"], "partial")

    def test_time_order_violation_fails_phase0(self):
        sm.record_event("group_message", ts=1000, dedupe="g:1:mid:1")
        c = sm._db()
        c.execute("INSERT INTO event(id,kind,ts,created) VALUES('bad','note',2000,1000)")
        c.commit()
        c.close()
        rep = phase_audit.audit(root=self.root)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["phases"]["phase0"]["status"], "fail")
        self.assertEqual(rep["checks"]["cognition"]["time_order"]["violations"], 1)

    def test_subsecond_skew_is_visible_but_not_a_failure(self):
        ev = sm.record_event("group_message", ts=1000, session="group:1",
                             actor_qq="123456", topic_qq="123456",
                             payload={"text": "你好"}, dedupe="g:1:mid:2")
        aid = sm.record_action("group", target_qq="123456", topic_qq="123456",
                               scope="group:1", text="回复", evidence=[ev])
        sm.mark_action(aid, "accepted", result="0")
        c = sm._db()
        c.execute("UPDATE event SET ts=created+0.005 WHERE id=?", (ev,))
        c.commit()
        c.close()
        rep = phase_audit.audit(root=self.root)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["phases"]["phase0"]["status"], "pass")
        self.assertEqual(rep["checks"]["cognition"]["time_order"]["violations"], 0)
        self.assertEqual(
            rep["checks"]["cognition"]["time_order"]["subsecond_skew"], 1)

    def test_legacy_beliefs_without_evidence_are_partial_not_fake(self):
        sm.import_legacy([{"kind": "belief", "text": "她喜欢猫",
                           "ts": 1000, "source": "legacy"}], dry_run=False)
        rep = phase_audit.audit(root=self.root)
        p1 = rep["phases"]["phase1"]
        self.assertEqual(p1["status"], "partial", p1)
        self.assertEqual(rep["counts"]["cognition"]["belief"] >= 1, True)
        self.assertEqual(rep["counts"]["cognition"]["belief_evidence"], 0)

    def test_report_contains_no_chat_text_or_qq(self):
        secret = "这是不该出现在审计里的私聊原文"
        qq = "987654321"
        ev = sm.record_event("private_message", ts=1000, actor_qq=qq,
                             payload={"text": secret}, dedupe="p:1")
        aid = sm.record_action("dm", target_qq=qq, scope="private:" + qq,
                               text=secret, evidence=[ev])
        sm.mark_action(aid, "accepted", result="0")
        encoded = json.dumps(phase_audit.audit(root=self.root),
                             ensure_ascii=False, allow_nan=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn(qq, encoded)


class ShadowTests(Base):
    def _one_event(self):
        sm.stats()                      # 先让 cognition.db 存在，否则首次 tick 只会等待
        now = time.time() - 1
        first = shadow.tick(now)
        self.assertTrue(first.get("initialized"), first)
        sm.record_event("group_message", ts=now, session="group:1",
                        payload={"text": "你好"}, dedupe="g:1:mid:9")
        out = shadow.tick(time.time())
        self.assertEqual(out["processed"], 1, out)

    def test_shadow_replay_and_no_jumps_pass(self):
        self._one_event()
        rep = phase_audit.audit(root=self.root)
        p2 = rep["phases"]["phase2"]
        self.assertEqual(p2["status"], "pass", p2)
        self.assertEqual(rep["counts"]["shadow"]["processed"], 1)
        self.assertEqual(rep["counts"]["shadow"]["shadow_events"], 1)
        self.assertEqual(rep["shadow_replay"]["max_error"], 0.0)
        self.assertEqual(rep["checks"]["shadow"]["delta_mismatches"], 0)

    def _batch(self, kind, payload, count, source=""):
        sm.stats()
        self.assertTrue(shadow.tick(1000).get("initialized"))
        for i in range(count):
            sm.record_event(kind, ts=1001, source=source, payload=payload,
                            dedupe="batch:%d" % i)
        with closing(sm._db()) as c, c:
            c.execute("UPDATE event SET created=ts")
        self.assertEqual(shadow.tick(1002, batch_size=500)["processed"], count)

    def _shadow_db(self):
        c = sqlite3.connect(os.path.join(self.root, "emotion_shadow.db"))
        c.row_factory = sqlite3.Row
        return c

    def _audit_without_crash(self):
        try:
            return phase_audit.audit(root=self.root)
        except Exception as exc:
            self.fail("审计必须显式报告失败，不能抛出 %s" % type(exc).__name__)

    def test_upper_bounds_are_normal_clips_not_unexplained_jumps(self):
        self._batch("private_message",
                    {"text": "谢谢你", "addressed_to_self": True}, 60)
        with closing(self._shadow_db()) as c:
            last = json.loads(c.execute("SELECT after_state FROM shadow_event "
                                        "ORDER BY source_rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(last["valence"], 1.0)
        self.assertEqual(last["control"], 1.0)
        self.assertEqual(last["attention"], 1.0)
        rep = phase_audit.audit(root=self.root, now=1003)
        self.assertTrue(rep["shadow_replay"]["ok"], rep)
        self.assertEqual(rep["checks"]["shadow"]["delta_mismatches"], 0)
        self.assertEqual(rep["phases"]["phase2"]["status"], "pass")

    def test_lower_bound_is_a_normal_clip_not_an_unexplained_jump(self):
        self._batch("tool_result", {"ok": False}, 30)
        with closing(self._shadow_db()) as c:
            last = json.loads(c.execute("SELECT after_state FROM shadow_event "
                                        "ORDER BY source_rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(last["control"], 0.0)
        rep = phase_audit.audit(root=self.root, now=1003)
        self.assertTrue(rep["shadow_replay"]["ok"], rep)
        self.assertEqual(rep["checks"]["shadow"]["delta_mismatches"], 0)
        self.assertEqual(rep["phases"]["phase2"]["status"], "pass")

    def test_frozen_legacy_kernel_still_audits_with_current_consumer(self):
        sm.stats()
        event = {"id": "legacy-run-event", "kind": "message", "ts": 1001,
                 "payload": {"text": "你好"}}
        with closing(sm._db()) as c, c:
            c.execute("INSERT INTO event(rowid,id,kind,ts,created) "
                      "VALUES(1,?,'private_message',1001,1001)", (event["id"],))
        ap = core.appraise(event)
        before = core.advance(core.initial_state(1000), 1001)
        after = core.apply_event(before, event, ap)
        with closing(self._shadow_db()) as c, c:
            c.executescript(shadow._SCHEMA)
            c.execute("INSERT INTO shadow_run "
                      "(id,parameter_hash,parameters,observation_started,last_tick,"
                      "observed_seconds,cursor,cursor_id,processed,real_events,state) "
                      "VALUES(1,?,?,1000,1002,2,1,?,1,1,?)",
                      (core.parameter_hash(), json.dumps(core.PARAMETERS), event["id"],
                       json.dumps(after)))
            c.execute("INSERT INTO shadow_event VALUES(1,?,1001,'private_message',?,?,?)",
                      (event["id"], json.dumps(ap), json.dumps(before), json.dumps(after)))
        rep = phase_audit.audit(root=self.root, now=1003)
        self.assertTrue(rep["shadow_replay"]["ok"], rep)
        self.assertEqual(rep["phases"]["phase2"]["status"], "pass")

    def test_real_before_state_tampering_still_fails_delta_check(self):
        self._one_event()
        with closing(self._shadow_db()) as c, c:
            before = json.loads(c.execute("SELECT before_state FROM shadow_event").fetchone()[0])
            before["attention"] = 0.25
            c.execute("UPDATE shadow_event SET before_state=?", (json.dumps(before),))
        rep = phase_audit.audit(root=self.root)
        self.assertTrue(rep["shadow_replay"]["ok"], rep)
        self.assertEqual(rep["checks"]["shadow"]["delta_mismatches"], 1)
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_clipped_after_state_tampering_still_fails(self):
        self._batch("tool_result", {"ok": False}, 30)
        with closing(self._shadow_db()) as c, c:
            row = c.execute("SELECT source_rowid,after_state FROM shadow_event "
                            "ORDER BY source_rowid DESC LIMIT 1").fetchone()
            after = json.loads(row["after_state"])
            after["control"] = 0.1
            c.execute("UPDATE shadow_event SET after_state=? WHERE source_rowid=?",
                      (json.dumps(after), row["source_rowid"]))
        rep = phase_audit.audit(root=self.root)
        self.assertFalse(rep["shadow_replay"]["ok"], rep)
        self.assertEqual(rep["checks"]["shadow"]["delta_mismatches"], 1)
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_ignored_events_are_checked_for_zero_impulse(self):
        self._batch("private_message", {"text": "你好"}, 1, source="synthetic")
        with closing(self._shadow_db()) as c, c:
            before = json.loads(c.execute("SELECT before_state FROM shadow_event").fetchone()[0])
            before["attention"] = 0.25
            c.execute("UPDATE shadow_event SET before_state=?", (json.dumps(before),))
        rep = phase_audit.audit(root=self.root)
        self.assertTrue(rep["shadow_replay"]["ok"], rep)
        self.assertEqual(rep["checks"]["shadow"]["delta_mismatches"], 1)
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_ignored_event_cannot_hide_a_replay_consistent_impulse(self):
        self._batch("private_message", {"text": "你好"}, 1, source="synthetic")
        with closing(self._shadow_db()) as c, c:
            row = c.execute("SELECT appraisal,after_state FROM shadow_event").fetchone()
            ap, after = json.loads(row["appraisal"]), json.loads(row["after_state"])
            ap["delta"]["attention"] = 0.1
            after["attention"] = 0.1
            c.execute("UPDATE shadow_event SET appraisal=?,after_state=?",
                      (json.dumps(ap), json.dumps(after)))
            c.execute("UPDATE shadow_run SET state=?", (json.dumps(after),))
        rep = phase_audit.audit(root=self.root)
        self.assertTrue(rep["shadow_replay"]["ok"], rep)
        self.assertEqual(rep["checks"]["shadow"]["delta_mismatches"], 1)
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_malformed_event_schema_is_an_explicit_failure(self):
        self._one_event()
        with closing(self._shadow_db()) as c:
            row = dict(c.execute("SELECT * FROM shadow_event").fetchone())
        before = json.loads(row["before_state"])
        ap = json.loads(row["appraisal"])
        missing_delta = dict(ap, delta={k: v for k, v in ap["delta"].items()
                                       if k != "valence"})
        cases = [
            ("appraisal", "{bad"), ("appraisal", "[]"), ("before_state", "null"),
            ("after_state", "[]"),
            ("after_state", json.dumps(dict(before, valence=float("inf")))),
            ("before_state", json.dumps({k: v for k, v in before.items() if k != "valence"})),
            ("before_state", json.dumps(dict(before, valence=None))),
            ("before_state", json.dumps(dict(before, valence="0"))),
            ("before_state", json.dumps(dict(before, valence=False))),
            ("before_state", json.dumps(dict(before, valence=float("nan")))),
            ("before_state", json.dumps(dict(before, at=float("inf")))),
            ("before_state", json.dumps(dict(before, traits={}))),
            ("appraisal", json.dumps(missing_delta)),
            ("appraisal", json.dumps(dict(ap, ignored="false"))),
            ("appraisal", json.dumps(dict(ap, delta=[]))),
            ("appraisal", json.dumps(dict(ap, delta=dict(ap["delta"], valence=float("inf"))))),
        ]
        for column, value in cases:
            with self.subTest(column=column, value=value):
                with closing(self._shadow_db()) as c, c:
                    c.execute("UPDATE shadow_event SET %s=?" % column, (value,))
                try:
                    rep = self._audit_without_crash()
                    self.assertEqual(rep["phases"]["phase2"]["status"], "fail")
                    self.assertGreater(rep["checks"]["shadow"].get("schema_errors", 0), 0)
                    json.dumps(rep, allow_nan=False)
                finally:
                    with closing(self._shadow_db()) as c, c:
                        c.execute("UPDATE shadow_event SET %s=?" % column, (row[column],))

    def test_broken_shadow_database_schema_is_an_explicit_failure(self):
        self._one_event()
        with closing(self._shadow_db()) as c, c:
            c.execute("DROP TABLE shadow_event")
        rep = self._audit_without_crash()
        self.assertFalse(rep["ok"], rep)
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")
        self.assertIn("error", rep["checks"]["shadow"])

    def test_run_state_schema_corruption_is_an_explicit_failure(self):
        self._one_event()
        with closing(self._shadow_db()) as c, c:
            state = json.loads(c.execute("SELECT state FROM shadow_run").fetchone()[0])
            state["traits"] = {}
            c.execute("UPDATE shadow_run SET state=?", (json.dumps(state),))
        rep = self._audit_without_crash()
        self.assertFalse(rep["ok"], rep)
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_parameter_drift_fails_phase2(self):
        self._one_event()
        import sqlite3
        p = os.path.join(self.root, "emotion_shadow.db")
        c = sqlite3.connect(p)
        c.execute("UPDATE shadow_run SET parameter_hash='bad' WHERE id=1")
        c.commit()
        c.close()
        rep = phase_audit.audit(root=self.root)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_current_versioned_shadow_is_audited_instead_of_archived_run(self):
        self._one_event()
        from pathlib import Path
        original = Path(self.root) / 'emotion_shadow.db'
        active = Path(self.root) / 'emotion_shadow_v2.db'
        original.rename(active)
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep['counts']['shadow']['processed'], 1)
        self.assertEqual(rep['phases']['phase2']['status'], 'pass')

    def test_stopped_consumer_cannot_pass_audit_with_zero_backlog(self):
        self._one_event()
        rep = phase_audit.audit(root=self.root, now=time.time() + 86400)
        self.assertEqual(rep['phases']['phase2']['status'], 'partial')
        self.assertFalse(rep['checks']['shadow']['heartbeat_fresh'])

    def test_unprocessed_backlog_is_partial(self):
        self._one_event()
        sm.record_event("group_message", ts=time.time(), session="group:1",
                        payload={"text": "又一条"}, dedupe="g:1:mid:10")
        rep = phase_audit.audit(root=self.root)
        p2 = rep["phases"]["phase2"]
        self.assertEqual(p2["status"], "partial", p2)
        self.assertGreater(rep["counts"]["shadow"]["backlog"], 0)

    def test_backlog_counts_rows_not_sparse_rowid_distance(self):
        self._one_event()
        with closing(sm._db()) as c, c:
            c.execute("INSERT INTO event(rowid,id,kind,ts,created) "
                      "VALUES(101,'sparse','group_message',1000,1000)")
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["counts"]["shadow"]["backlog"], 1)
        self.assertEqual(rep["phases"]["phase2"]["status"], "partial")

    def test_missing_source_is_unknown_partial_not_pass(self):
        self._one_event()
        os.remove(os.path.join(self.root, "cognition.db"))
        rep = phase_audit.audit(root=self.root)
        self.assertIsNone(rep["counts"]["shadow"]["backlog"])
        self.assertIsNone(rep["checks"]["shadow"].get("cursor_identity_ok"))
        self.assertEqual(rep["phases"]["phase2"]["status"], "partial")
        self.assertFalse(os.path.exists(os.path.join(self.root, "cognition.db")))

    def test_unreadable_source_is_unknown_partial_not_a_crash_or_pass(self):
        self._one_event()
        with closing(sm._db()) as c, c:
            c.execute("DROP TABLE event")
        rep = self._audit_without_crash()
        self.assertIsNone(rep["counts"]["shadow"]["backlog"])
        self.assertIsNone(rep["checks"]["shadow"].get("cursor_identity_ok"))
        self.assertEqual(rep["phases"]["phase2"]["status"], "partial")

    def test_cursor_identity_mismatch_fails_even_with_no_backlog(self):
        self._one_event()
        with closing(sm._db()) as c, c:
            c.execute("UPDATE event SET id='replacement'")
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["counts"]["shadow"]["backlog"], 0)
        self.assertFalse(rep["checks"]["shadow"].get("cursor_identity_ok", True))
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_deleted_cursor_fails_even_when_source_is_empty(self):
        self._one_event()
        with closing(sm._db()) as c, c:
            c.execute("DELETE FROM event")
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["counts"]["shadow"]["backlog"], 0)
        self.assertFalse(rep["checks"]["shadow"].get("cursor_identity_ok", True))
        self.assertEqual(rep["phases"]["phase2"]["status"], "fail")

    def test_initialized_empty_shadow_without_observation_is_not_started(self):
        sm.stats()
        shadow.tick(1000)
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["counts"]["shadow"]["backlog"], 0)
        self.assertEqual(rep["phases"]["phase2"]["status"], "not_started")

    def test_empty_shadow_with_observation_is_partial_not_pass(self):
        sm.stats()
        shadow.tick(1000)
        shadow.tick(1010)
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["counts"]["shadow"]["backlog"], 0)
        self.assertEqual(rep["counts"]["shadow"]["observed_seconds"], 10.0)
        self.assertEqual(rep["phases"]["phase2"]["status"], "partial")

    def test_quarantined_only_samples_are_partial_not_pass(self):
        self._batch("private_message", {"text": "你好"}, 1, source="synthetic")
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["counts"]["shadow"]["real_events"], 0)
        self.assertEqual(rep["phases"]["phase2"]["status"], "partial")

    def test_samples_without_observation_time_are_partial_not_pass(self):
        self._one_event()
        with closing(self._shadow_db()) as c, c:
            c.execute("UPDATE shadow_run SET observed_seconds=0")
        rep = phase_audit.audit(root=self.root)
        self.assertEqual(rep["phases"]["phase2"]["status"], "partial")

    def test_audit_does_not_modify_either_database(self):
        self._batch("tool_result", {"ok": False}, 30)
        paths = [os.path.join(self.root, name)
                 for name in ("cognition.db", "emotion_shadow.db")]
        before = []
        for path in paths:
            with open(path, "rb") as f:
                before.append(f.read())
        phase_audit.audit(root=self.root)
        for path, expected in zip(paths, before):
            with open(path, "rb") as f:
                self.assertEqual(f.read(), expected)


class CliTests(Base):
    def test_json_cli_is_machine_readable(self):
        import io
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = phase_audit.main(["--json", "--root", self.root])
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        got = json.loads(buf.getvalue())
        self.assertIn("phases", got)


if __name__ == "__main__":
    unittest.main()