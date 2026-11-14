# -*- coding: utf-8 -*-
"""自动驾驶决策回归锁。

这里钉的是**安全边界**，不是"能算出东西"：
算法轨道永远不自动推进、manual_review 永远要人批、等待数据不许被当成故障去修、
备份不许碰用户既有快照。决策错了比不决策更危险，所以这些必须有锁。
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import ops_autopilot as ap
import ops_remote as orm
import qingyi_roadmap

# 硬闸：决策内核的用例一律用假证据，真连生产就立刻炸。
_GUARD = None


def setUpModule():
    global _GUARD

    def _no_network(*a, **k):
        raise AssertionError("自动驾驶测试不许连真实服务器")

    _GUARD = mock.patch.object(orm, "_connect", _no_network)
    _GUARD.start()


def tearDownModule():
    if _GUARD is not None:
        _GUARD.stop()

IMPLEMENTED = ("A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08",
               "D01", "D02", "D03", "D04", "D05", "D06",
               "U01", "U02", "U03", "U04", "U05", "U06",
               # C01/C02 在目录里是 planned，但检测投影把它们标成 existing
               # （它们量的是既有能力，不是待建代码），这里照生产口径复刻。
               "C01", "C02")


def fake_preflight(implemented=IMPLEMENTED):
    tasks = []
    for task in qingyi_roadmap.catalog():
        tasks.append({"id": task["id"],
                      "implementation": "implemented" if task["id"] in implemented else "planned"})
    return {"ok": True, "scope_status": "blocked", "tasks": tasks, "checks": [],
            "authorization": {"stage3": False, "expression_control": False,
                              "deployment": False, "automatic_progress": False}}


def passing_isolated():
    return {"ok": True, "scope_status": "blocked",
            "checks": [{"id": "usage_idempotency", "status": "pass"},
                       {"id": "stage0_contracts", "status": "pass"}]}


def fake_readiness(action_missing=2, engineering_missing=True):
    checks = [{"key": "ledger_integrity", "status": "pass", "observed": {}},
              {"key": "action_evidence", "status": "blocked" if action_missing else "pass",
               "observed": {"linked": 94 - action_missing, "missing": action_missing, "total": 94}},
              {"key": "action_identity", "status": "pass", "observed": {}},
              {"key": "action_results", "status": "pass", "observed": {}},
              {"key": "belief_grounding", "status": "pass", "observed": {}},
              {"key": "self_view_grounding", "status": "pass", "observed": {}},
              {"key": "shadow_review", "status": "pass", "observed": {}}]
    for key in ap.ENGINEERING_GATES:
        checks.append({"key": key, "status": "not_verified" if engineering_missing else "pass",
                       "observed": None})
    return {"ok": True, "phase25": {"checks": checks,
                                   "blockers": [c["key"] for c in checks if c["status"] != "pass"]}}


class SafetyPolicyTests(unittest.TestCase):
    def test_auto_and_ask_tracks_do_not_overlap(self):
        self.assertEqual(ap.AUTO_TRACKS & ap.ASK_TRACKS, frozenset())

    def test_every_catalog_track_is_classified(self):
        """不许有轨道落在两张表之外 —— 那会静默变成"自动推进"。"""
        tracks = {t["track"] for t in qingyi_roadmap.catalog()}
        self.assertEqual(tracks - (ap.AUTO_TRACKS | ap.ASK_TRACKS), set())

    def test_manual_review_never_auto_even_with_approval(self):
        for task in qingyi_roadmap.catalog():
            if not task["manual_review"]:
                continue
            kind, _ = ap.classify_task(task, {})
            self.assertEqual(kind, "ask", task["id"])
            kind, _ = ap.classify_task(task, {task["id"]: {"decision": "approve"}})
            self.assertEqual(kind, "approved", task["id"])
            self.assertNotEqual(kind, "auto")

    def test_algorithm_tracks_require_approval(self):
        for task in qingyi_roadmap.catalog():
            if task["track"] in ap.ASK_TRACKS and not task["manual_review"]:
                kind, _ = ap.classify_task(task, {})
                self.assertEqual(kind, "ask", task["id"])

    def test_expression_stages_ask_even_on_safe_track(self):
        task = {"id": "X99", "title": "t", "stage": "stage3a", "track": "detection",
                "manual_review": False, "dev_requires": []}
        kind, _ = ap.classify_task(task, {})
        self.assertEqual(kind, "ask")

    def test_approval_unlocks_auto_advance(self):
        task = {"id": "M01", "title": "t", "stage": "stage1", "track": "memory",
                "manual_review": False, "dev_requires": []}
        self.assertEqual(ap.classify_task(task, {})[0], "ask")
        self.assertEqual(ap.classify_task(task, {"M01": {"decision": "approve"}})[0], "approved")
        self.assertEqual(ap.classify_task(task, {"M01": {"decision": "reject"}})[0], "rejected")

    def test_authorization_is_never_self_granted(self):
        plan = ap.build_plan({"preflight_evidence": fake_preflight(),
                              "readiness": fake_readiness(), "health": {"ok": True},
                              "state": {}})
        self.assertFalse(plan["authorization"]["stage3"])
        self.assertFalse(plan["authorization"]["expression_control"])
        self.assertFalse(plan["authorization"]["automatic_progress"])


class FrontierTests(unittest.TestCase):
    def test_frontier_matches_known_production_state(self):
        rows = ap.dev_frontier(fake_preflight())
        ids = {t["id"] for t in rows}
        self.assertEqual(ids, {"M01", "S01", "V02", "D07"})

    def test_frontier_excludes_tasks_with_unimplemented_deps(self):
        rows = ap.dev_frontier(fake_preflight())
        ids = {t["id"] for t in rows}
        for excluded in ("M02", "R01", "C03", "G01", "L01", "V01"):
            self.assertNotIn(excluded, ids)

    def test_nothing_implemented_leaves_only_zero_dep_tasks(self):
        rows = ap.dev_frontier(fake_preflight(implemented=()))
        self.assertTrue(rows)
        for task in rows:
            self.assertEqual(task["dev_requires"], [], task["id"])

    def test_delivery_tasks_come_first(self):
        actions = ap.next_actions(fake_preflight(), {})
        self.assertEqual(actions["auto_advance"][0]["id"], "D07")

    def test_limit_is_respected_and_rest_is_kept(self):
        actions = ap.next_actions(fake_preflight(), {}, limit=1)
        self.assertEqual(len(actions["auto_advance"]), 1)
        self.assertIsInstance(actions["auto_queue_rest"], list)

    def test_default_has_no_per_round_cap(self):
        """主人要的是"能推就推到推不动"，默认不攒着。"""
        approvals = {"M01": {"decision": "approve"}, "S01": {"decision": "approve"}}
        actions = ap.next_actions(fake_preflight(), approvals)
        self.assertEqual(len(actions["auto_advance"]), 3)
        self.assertEqual(actions["auto_queue_rest"], [])

    def test_env_can_put_the_cap_back(self):
        with mock.patch.dict(os.environ, {"OPS_MAX_TASKS_PER_ROUND": "1"}):
            actions = ap.next_actions(fake_preflight(), {"M01": {"decision": "approve"}})
        self.assertEqual(len(actions["auto_advance"]), 1)
        self.assertEqual(len(actions["auto_queue_rest"]), 1)

    def test_approved_algorithm_task_enters_auto_queue(self):
        actions = ap.next_actions(fake_preflight(), {"M01": {"decision": "approve"}})
        ids = [t["id"] for t in actions["auto_advance"]]
        self.assertIn("M01", ids)
        self.assertNotIn("M01", [t["id"] for t in actions["needs_approval"]])

    def test_rejected_task_is_not_proposed_again(self):
        actions = ap.next_actions(fake_preflight(), {"S01": {"decision": "reject"}})
        self.assertNotIn("S01", [t["id"] for t in actions["needs_approval"]])
        self.assertIn("S01", [t["id"] for t in actions["rejected"]])


class BlockerClassificationTests(unittest.TestCase):
    def test_engineering_gates_are_mechanically_fixable(self):
        res = ap.classify_blockers(fake_readiness(), fake_preflight(), {}, {"ok": True})
        ids = [f["id"] for f in res["safe_fixes"]]
        self.assertIn("engineering_evidence", ids)
        self.assertEqual(res["blocked"], [])

    def test_missing_natural_samples_are_waiting_not_fixable(self):
        res = ap.classify_blockers(fake_readiness(action_missing=2), fake_preflight(), {},
                                   {"ok": True})
        waiting = {w["id"]: w for w in res["waiting_on"]}
        self.assertIn("action_evidence", waiting)
        self.assertIn("2 次", waiting["action_evidence"]["need"])
        self.assertNotIn("action_evidence", [f["id"] for f in res["safe_fixes"]])

    def test_no_waiting_when_samples_complete(self):
        res = ap.classify_blockers(fake_readiness(action_missing=0, engineering_missing=False),
                                   fake_preflight(), passing_isolated(), {"ok": True})
        self.assertEqual(res["waiting_on"], [])
        self.assertEqual(res["safe_fixes"], [])
        self.assertEqual(res["blocked"], [])

    def test_isolated_probes_missing_becomes_fix(self):
        res = ap.classify_blockers(fake_readiness(), fake_preflight(), None, {"ok": True})
        ids = [f["id"] for f in res["safe_fixes"]]
        self.assertIn("usage_idempotency", ids)
        self.assertIn("stage0_contracts", ids)

    def test_failed_isolated_probe_is_blocked_not_auto_fixed(self):
        isolated = {"checks": [{"id": "usage_idempotency", "status": "blocked"},
                               {"id": "stage0_contracts", "status": "pass"}]}
        res = ap.classify_blockers(fake_readiness(), fake_preflight(), isolated, {"ok": True})
        self.assertIn("usage_idempotency", [b["id"] for b in res["blocked"]])
        self.assertNotIn("usage_idempotency", [f["id"] for f in res["safe_fixes"]])

    def test_down_service_becomes_restart_fix(self):
        health = {"ok": False, "issues": ["qqbot 服务不是 active（failed）"]}
        res = ap.classify_blockers(fake_readiness(), fake_preflight(), {}, health)
        self.assertIn("qqbot_down", [f["id"] for f in res["safe_fixes"]])
        self.assertEqual([f["action"] for f in res["safe_fixes"] if f["id"] == "qqbot_down"],
                         ["restart_qqbot"])

    def test_napcat_issue_is_not_blindly_restarted(self):
        """watchdog 的既有口径是只重启 qqbot，绝不自动重启 NapCat。"""
        health = {"ok": False, "issues": ["NapCat 容器不是 running（exited）"]}
        res = ap.classify_blockers(fake_readiness(), fake_preflight(), {}, health)
        self.assertEqual([f for f in res["safe_fixes"] if f["action"] == "restart_qqbot"], [])
        self.assertTrue(res["blocked"])


class NotificationTests(unittest.TestCase):
    def _plan(self, state=None):
        return ap.build_plan({"preflight_evidence": fake_preflight(),
                              "readiness": fake_readiness(), "health": {"ok": True},
                              "state": state or {}})

    def test_approval_proposal_lists_algorithm_tasks(self):
        plan = self._plan()
        notes = {n["kind"]: n for n in plan["notifications"]}
        self.assertIn("approval", notes)
        self.assertIn("M01", notes["approval"]["text"])
        self.assertIn("/ops approve", notes["approval"]["text"])

    def test_waiting_notification_explains_it_is_not_a_fault(self):
        plan = self._plan()
        notes = {n["kind"]: n for n in plan["notifications"]}
        self.assertIn("waiting", notes)
        self.assertIn("不是故障", notes["waiting"]["text"])

    def test_same_proposal_not_repeated_every_round(self):
        plan = self._plan()
        state = {}
        ap.mark_proposed(state, [t["id"] for t in plan["needs_approval"]])
        again = self._plan(state)
        self.assertNotIn("approval", {n["kind"] for n in again["notifications"]})

    def _quiet_plan(self, state=None):
        evidence = {"preflight_evidence": fake_preflight(
                        implemented=tuple(t["id"] for t in qingyi_roadmap.catalog())),
                    "readiness": fake_readiness(action_missing=0, engineering_missing=False),
                    "health": {"ok": True}, "state": state or {},
                    "preflight_isolated": {"checks": [
                        {"id": "usage_idempotency", "status": "pass"},
                        {"id": "stage0_contracts", "status": "pass"}]}}
        return ap.build_plan(evidence)

    def test_idle_round_still_reports(self):
        """沉默的 worker 视为故障：没东西可做也要出一声。"""
        plan = self._quiet_plan()
        self.assertEqual(plan["auto_advance"], [])
        self.assertEqual(plan["needs_approval"], [])
        kinds = [n["kind"] for n in plan["notifications"]]
        self.assertEqual(kinds, ["idle"])

    def test_waiting_is_not_repeated_every_hour(self):
        """按小时跑，同一件事每轮都发就是刷屏；内容没变就进冷却。"""
        first = self._plan()
        sig = [n for n in first["notifications"] if n["kind"] == "waiting"][0]["dedup"]["sig"]
        again = self._plan({"proposals": {"waiting": {"sig": sig, "at": time.time()}}})
        self.assertNotIn("waiting", {n["kind"] for n in again["notifications"]})

    def test_waiting_is_resent_when_the_set_changes(self):
        state = {"proposals": {"waiting": {"sig": "stale-set", "at": time.time()}}}
        self.assertIn("waiting", {n["kind"] for n in self._plan(state)["notifications"]})

    def test_waiting_is_resent_after_cooldown(self):
        first = self._plan()
        sig = [n for n in first["notifications"] if n["kind"] == "waiting"][0]["dedup"]["sig"]
        old = time.time() - (ap.WAITING_COOLDOWN_HOURS + 1) * 3600
        state = {"proposals": {"waiting": {"sig": sig, "at": old}}}
        self.assertIn("waiting", {n["kind"] for n in self._plan(state)["notifications"]})

    def test_idle_is_throttled_but_comes_back(self):
        now = time.time()
        state = {"proposals": {"idle": {"sig": "idle", "at": now}}}
        self.assertEqual(self._quiet_plan(state)["notifications"], [])
        old = now - (ap.IDLE_COOLDOWN_HOURS + 1) * 3600
        state = {"proposals": {"idle": {"sig": "idle", "at": old}}}
        self.assertEqual([n["kind"] for n in self._quiet_plan(state)["notifications"]], ["idle"])

    def test_push_window_mutes_everything_but_new_approvals(self):
        """巡检每小时跑，消息每 3 小时一次：窗口没开时只有新批准能立刻出去。"""
        state = {"proposals": {"last_push": {"at": time.time()}}}
        kinds = [n["kind"] for n in self._plan(state)["notifications"]]
        self.assertEqual(kinds, ["approval"])

    def test_push_window_reopens_after_interval(self):
        old = time.time() - (ap.notify_interval_hours() + 1) * 3600
        state = {"proposals": {"last_push": {"at": old}}}
        self.assertIn("waiting", {n["kind"] for n in self._plan(state)["notifications"]})

    def test_closed_window_stays_silent_when_nothing_happened(self):
        state = {"proposals": {"last_push": {"at": time.time()}}}
        self.assertEqual(self._quiet_plan(state)["notifications"], [])

    def test_push_notifications_records_the_window_only_on_success(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.call_ops.side_effect = lambda func, payload=None, **kw: {
            "enqueue_outbox": {"ok": True, "value": "o1"},
            "load_state": {"ok": True, "value": {"proposals": {}}},
            "save_state": {"ok": True, "value": True},
        }.get(func, {"ok": True, "value": None})
        with mock.patch("ops_remote.Remote", return_value=fake):
            sent = ap.push_notifications({"notifications": [{"kind": "waiting", "text": "x"}]})
        self.assertTrue(sent[0]["ok"])
        saved = [c[0][1]["state"] for c in fake.call_ops.call_args_list if c[0][0] == "save_state"]
        self.assertIn("last_push", saved[0]["proposals"])

    def test_failed_push_does_not_start_the_window(self):
        """没推出去就不能记成「已经说过」，否则那条通知永远丢了。"""
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.call_ops.side_effect = lambda func, payload=None, **kw: {
            "enqueue_outbox": {"ok": False, "error": "remote_failed"},
            "load_state": {"ok": True, "value": {"proposals": {}}},
            "save_state": {"ok": True, "value": True},
        }.get(func, {"ok": True, "value": None})
        with mock.patch("ops_remote.Remote", return_value=fake):
            ap.push_notifications({"notifications": [{"kind": "waiting", "text": "x"}]})
        saves = [c[0][1]["state"] for c in fake.call_ops.call_args_list if c[0][0] == "save_state"]
        self.assertEqual(saves, [])


class BackupEnv(unittest.TestCase):
    """只准备临时仓库和备份目录，本身不含用例。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ops_backup_test_")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        self.backups = os.path.join(self.tmp.name, "backups")
        os.makedirs(os.path.join(self.root, "bot"))
        os.makedirs(os.path.join(self.root, "data", "persona"))
        with open(os.path.join(self.root, "bot", "x.py"), "w", encoding="utf-8") as f:
            f.write("print(1)\n")
        with open(os.path.join(self.root, "config.env"), "w", encoding="utf-8") as f:
            f.write("SECRET=do-not-copy\n")
        with open(os.path.join(self.root, "data", "persona", "self.db"), "w", encoding="utf-8") as f:
            f.write("persona")
        self._old = {k: os.environ.get(k) for k in ("OPS_LOCAL_ROOT", "OPS_LOCAL_BACKUP_ROOT")}
        os.environ["OPS_LOCAL_ROOT"] = self.root
        os.environ["OPS_LOCAL_BACKUP_ROOT"] = self.backups
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

class BackupTests(BackupEnv):
    def test_backup_copies_code_only(self):
        res = ap.local_code_backup()
        self.assertTrue(res["ok"])
        self.assertTrue(os.path.isfile(os.path.join(res["dir"], "bot", "x.py")))
        self.assertFalse(os.path.exists(os.path.join(res["dir"], "config.env")))
        self.assertFalse(os.path.exists(os.path.join(res["dir"], "data")))

    def test_backup_never_prunes_user_snapshots(self):
        user_snapshot = os.path.join(self.tmp.name, "_backup_2026-11-06_user")
        os.makedirs(user_snapshot)
        for i in range(5):
            os.makedirs(os.path.join(self.backups, "2026-01-0%d_old" % i))
        ap.local_code_backup(keep=2)
        self.assertTrue(os.path.isdir(user_snapshot))
        self.assertLessEqual(len(os.listdir(self.backups)), 2)

    def test_keep_zero_never_prunes(self):
        for i in range(4):
            os.makedirs(os.path.join(self.backups, "2026-01-0%d_old" % i))
        ap.local_code_backup(keep=0)
        self.assertEqual(len(os.listdir(self.backups)), 5)


class BackupBeforeFixTests(BackupEnv):
    """动手修之前必须留底；留底失败就不许修。

    apply_safe_fixes 会在生产上重生成 engineering_contracts.json，那是写操作。
    没有回滚点的写操作宁可不做 —— 这条是主人「记得留备份」的硬要求。
    """

    def _remote(self, backup_ok=True):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        return fake, mock.patch("ops_remote.backup_persona",
                                return_value={"ok": backup_ok, "dir": "/opt/qqbot_backups/ops-x",
                                              "error": "" if backup_ok else "backup_failed"})

    def test_no_fixes_skips_persona_snapshot(self):
        """每轮都堆一份 persona 快照会把磁盘吃光，没修复动作就不堆。"""
        fake, patcher = self._remote()
        with patcher as bp, mock.patch("ops_remote.Remote", return_value=fake):
            res = ap.backup_before_fixes({"safe_fixes": []})
        self.assertTrue(res["ok"])
        self.assertEqual(res["skipped"], "no_fixes")
        bp.assert_not_called()

    def test_fixes_trigger_persona_snapshot(self):
        fake, patcher = self._remote()
        with patcher as bp, mock.patch("ops_remote.Remote", return_value=fake):
            res = ap.backup_before_fixes({"safe_fixes": [{"id": "engineering_evidence",
                                                          "action": "run_engineering_contracts"}]})
        self.assertTrue(res["ok"])
        bp.assert_called_once()
        self.assertTrue(os.path.isdir(res["local"]["dir"]))

    def test_backup_failure_blocks_the_fix(self):
        fake, patcher = self._remote(backup_ok=False)
        with patcher, mock.patch("ops_remote.Remote", return_value=fake):
            res = ap.backup_before_fixes({"safe_fixes": [{"id": "x", "action": "restart_qqbot"}]})
        self.assertFalse(res["ok"])

    def test_connection_failure_blocks_the_fix(self):
        with mock.patch("ops_remote.Remote", side_effect=OSError("no route")):
            res = ap.backup_before_fixes({"safe_fixes": [{"id": "x", "action": "restart_qqbot"}]})
        self.assertFalse(res["ok"])
        self.assertFalse(res["persona"]["ok"])

    def test_main_skips_fixes_and_speaks_up_when_backup_fails(self):
        plan = {"safe_fixes": [{"id": "x", "action": "restart_qqbot"}],
                "blocked": [], "notifications": []}
        evidence = {"state": {}}
        buf = io.StringIO()
        with mock.patch.object(ap, "collect", return_value={
                 "errors": [], "warnings": [], "state": {},
                 "preflight_evidence": fake_preflight(), "readiness": fake_readiness(),
                 "health": {"ok": True}}), \
             mock.patch.object(ap, "build_plan", return_value=plan), \
             mock.patch.object(ap, "backup_before_fixes",
                               return_value={"ok": False, "local": {"ok": True},
                                             "persona": {"ok": False, "error": "disk_full"}}), \
             mock.patch.object(ap, "apply_safe_fixes") as fixes, \
             mock.patch.object(ap, "_ops_dir", return_value=self.tmp.name), \
             mock.patch("sys.stdout", buf):
            rc = ap.main(["--plan", "--fix"])
        self.assertEqual(rc, 0)
        fixes.assert_not_called()
        out = json.loads(buf.getvalue())
        self.assertTrue(out["fix_results"][0]["skipped"])
        self.assertIn("backup", [b["id"] for b in out["blocked"]])
        self.assertTrue(any("备份没做成" in n["text"] for n in out["notifications"]))


class RoundLockTests(unittest.TestCase):
    """按小时排的巡检会和上一轮重叠，锁要挡得住重叠，也要能从崩溃里接管。

    一轮要改代码、跑全量测试、再上云，可能超过一小时。两轮同时动手会把
    对方改到一半的东西一起发出去；而锁若永远卡死，一次异常退出就让巡检
    永久停摆 —— 后者更难发现，所以必须有 TTL 接管。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ops_lock_")
        self.addCleanup(self.tmp.cleanup)
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.tmp.name
        self.addCleanup(self._restore)
        ap.release_round_lock()

    def _restore(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old

    def test_second_round_is_refused_while_first_holds(self):
        self.assertTrue(ap.acquire_round_lock()[0])
        # 把锁推到宽限期之外，模拟"另一轮在一小时后撞上来"
        with open(ap._lock_path(), "w", encoding="utf-8") as f:
            json.dump({"at": time.time() - 120, "holder": "other-run"}, f)
        ok, info = ap.acquire_round_lock()
        self.assertFalse(ok)
        self.assertIn("age_minutes", info)

    def test_relock_in_the_same_round_is_idempotent(self):
        """22:00 那轮就死在这：拿到锁两分钟后又检查一次，把自己关在了门外。"""
        self.assertTrue(ap.acquire_round_lock()[0])
        ok, info = ap.acquire_round_lock()
        self.assertTrue(ok, info)
        self.assertTrue(info.get("renewed"))

    def test_lock_can_be_released_and_retaken(self):
        self.assertTrue(ap.acquire_round_lock()[0])
        self.assertTrue(ap.release_round_lock())
        self.assertTrue(ap.acquire_round_lock()[0])

    def test_stale_lock_is_taken_over(self):
        self.assertTrue(ap.acquire_round_lock()[0])
        with open(ap._lock_path(), "w", encoding="utf-8") as f:
            json.dump({"at": time.time() - 4 * 3600, "holder": "dead-run"}, f)
        ok, info = ap.acquire_round_lock()
        self.assertTrue(ok, info)

    def test_release_when_absent_is_false_not_raised(self):
        self.assertFalse(ap.release_round_lock())

    def test_cli_lock_busy_exits_six(self):
        self.assertTrue(ap.acquire_round_lock()[0])
        with open(ap._lock_path(), "w", encoding="utf-8") as f:
            json.dump({"at": time.time() - 120, "holder": "other-run"}, f)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = ap.main(["--lock"])
        self.assertEqual(rc, 6)
        self.assertFalse(json.loads(buf.getvalue())["ok"])


class PushBookkeepingTests(unittest.TestCase):
    """没发出去的待批不许记账；发出去的才算已经问过。"""

    def _fake(self, ok=True):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.call_ops.side_effect = lambda func, payload=None, **kw: {
            "enqueue_outbox": {"ok": ok, "value": "o1" if ok else None, "error": "" if ok else "x"},
            "load_state": {"ok": True, "value": {}},
            "save_state": {"ok": True, "value": True},
            "save_health": {"ok": True, "value": True},
        }.get(func, {"ok": True, "value": True})
        return fake

    def test_push_alone_does_not_mark_approvals_as_proposed(self):
        plan = {"health": {}, "notifications": [{"kind": "approval", "ids": ["G01"], "text": "x"}]}
        with mock.patch("ops_remote.Remote", return_value=self._fake()), \
             mock.patch.object(ap, "mark_proposed") as marker:
            ap.push_plan(plan, {"state": {}})
        marker.assert_not_called()

    def test_sent_approval_is_recorded(self):
        fake = self._fake()
        plan = {"notifications": [{"kind": "approval", "ids": ["G01"], "text": "x"}]}
        with mock.patch("ops_remote.Remote", return_value=fake):
            ap.push_notifications(plan)
        saved = [c[0][1]["state"] for c in fake.call_ops.call_args_list if c[0][0] == "save_state"]
        self.assertIn("G01", saved[0]["proposals"])
        self.assertNotIn("last_push", saved[0]["proposals"])

    def test_failed_approval_is_not_recorded(self):
        fake = self._fake(ok=False)
        plan = {"notifications": [{"kind": "approval", "ids": ["G01"], "text": "x"}]}
        with mock.patch("ops_remote.Remote", return_value=fake):
            ap.push_notifications(plan)
        saves = [c[0][1]["state"] for c in fake.call_ops.call_args_list if c[0][0] == "save_state"]
        self.assertEqual(saves, [])


class RoundDigestTests(unittest.TestCase):
    """一轮做完要主动说一声。公告在开头，收尾不能没有。"""

    def test_digest_lists_what_was_done(self):
        msg = ap.round_digest({"advanced": ["M03", "M06"],
                               "deploy": {"deployed": True, "remote_hash_ok": 284,
                                          "service": "active", "backup": "/opt/b"}})
        self.assertIn("做完了", msg)
        self.assertIn("M03", msg)
        self.assertIn("已上云", msg)
        self.assertIn("/opt/b", msg)

    def test_nothing_happened_means_no_message(self):
        self.assertEqual(ap.round_digest({"advanced": [], "deploy": {"deployed": False}}), "")

    def test_save_round_pushes_it(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.call_ops.side_effect = lambda func, payload=None, **kw: {
            "save_round": {"ok": True, "value": "r1"},
            "enqueue_outbox": {"ok": True, "value": "o1"},
        }.get(func, {"ok": True, "value": None})
        import ops_channel
        with mock.patch("ops_remote.Remote", return_value=fake), \
             mock.patch.object(ops_channel, "save_round", return_value="r1"):
            res = ap.save_round({"advanced": ["M03"], "deploy": {"deployed": True}})
        self.assertEqual("o1", res["notified"])
        texts = [c[0][1]["text"] for c in fake.call_ops.call_args_list
                 if c[0][0] == "enqueue_outbox"]
        self.assertTrue(any("做完了" in t for t in texts), texts)


class CollectResilienceTests(unittest.TestCase):
    def test_missing_remote_state_is_warning_not_fatal(self):
        """运维模块还没上云时也要能算出计划，但必须出声。"""
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.call_ops.return_value = {"ok": False, "error": "remote_failed"}
        with mock.patch("ops_remote.Remote", return_value=fake), \
             mock.patch("ops_remote.health", return_value={"ok": True}), \
             mock.patch("ops_remote.preflight", return_value=fake_preflight()), \
             mock.patch("ops_remote.readiness", return_value=fake_readiness()):
            evidence = ap.collect(with_isolated=False)
        self.assertEqual(evidence["errors"], [])
        self.assertTrue(any("state_unavailable" in w for w in evidence["warnings"]))
        self.assertEqual(evidence["state"], {})

    def test_connection_failure_is_reported_as_error(self):
        with mock.patch("ops_remote.Remote", side_effect=OSError("no route")):
            evidence = ap.collect(with_isolated=False)
        self.assertTrue(any("connect" in e for e in evidence["errors"]))


if __name__ == "__main__":
    unittest.main()