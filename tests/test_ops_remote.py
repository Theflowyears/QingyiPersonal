# -*- coding: utf-8 -*-
"""传输层回归锁：不连真实服务器，只钉住判定口径。

最容易错的两处：把 preflight 的"结论退出码"当成执行失败，以及把采集失败
静默折成绿灯。这两条都会让自动驾驶基于假证据推进。
"""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import ops_remote as orm

# 硬闸：这些用例只钉判定口径，任何真连 SSH 的尝试都要立刻炸。
# 分发器测试第一版就是漏了这道闸，把测试报文发到了主人的 QQ 上。
_GUARD = None


def setUpModule():
    global _GUARD

    def _no_network(*a, **k):
        raise AssertionError("传输层测试不许连真实服务器")

    _GUARD = mock.patch.object(orm, "_connect", _no_network)
    _GUARD.start()


def tearDownModule():
    if _GUARD is not None:
        _GUARD.stop()


class FakeRemote(orm.Remote):
    """只替换传输，保留真实的命令拼装 —— 要钉的就是拼出来的那段代码。"""

    def __init__(self, python_result=(0, "{}"), run_result=(0, "{}")):
        super().__init__()
        self.python_result = python_result
        self.run_result = run_result
        self.calls = []

    def python(self, code, timeout=180, stdin=None):
        self.calls.append(("python", code, stdin))
        return self.python_result

    def run(self, command, timeout=120):
        self.calls.append(("run", command, None))
        return self.run_result


def health_payload(**over):
    base = {"at": "2026-11-11T12:00:00+0800", "qqbot": "active", "napcat": "running",
            "onebot_port": True, "disk_used_pct": 26.3, "disk_free_gb": 28.7,
            "mem_used_mb": 958, "mem_total_mb": 1736, "load": [0.03, 0.02, 0.0],
            "web": 200, "log_lines_scanned": 1200, "log_tags": {}}
    base.update(over)
    return base


class HealthTests(unittest.TestCase):
    def _health(self, payload):
        remote = FakeRemote(python_result=(0, json.dumps(payload)))
        return orm.health(remote)

    def test_all_green(self):
        res = self._health(health_payload())
        self.assertTrue(res["ok"])
        self.assertEqual(res["issues"], [])

    def test_each_failure_is_named(self):
        cases = [(health_payload(qqbot="failed"), "qqbot"),
                 (health_payload(napcat="exited"), "NapCat"),
                 (health_payload(onebot_port=False), "3001"),
                 (health_payload(web=502), "Web"),
                 (health_payload(disk_used_pct=95.0), "磁盘")]
        for payload, needle in cases:
            res = self._health(payload)
            self.assertFalse(res["ok"], needle)
            self.assertTrue(any(needle in i for i in res["issues"]), res["issues"])

    def test_disk_threshold_is_not_triggered_at_normal_usage(self):
        self.assertTrue(self._health(health_payload(disk_used_pct=89.9))["ok"])

    def test_collection_failure_is_not_green(self):
        remote = FakeRemote(python_result=(-1, "OSError: boom"))
        res = orm.health(remote)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "health_failed")

    def test_malformed_json_is_not_green(self):
        remote = FakeRemote(python_result=(0, "not json"))
        self.assertFalse(orm.health(remote)["ok"])

    def test_health_snippet_has_no_shell_interpolation(self):
        """采集跑在生产 root 下，不许出现 shell=True 或字符串拼命令。"""
        self.assertNotIn("shell=True", orm.HEALTH_SNIPPET)
        self.assertNotIn("os.system", orm.HEALTH_SNIPPET)


class PreflightTests(unittest.TestCase):
    def test_bad_profile_refused_without_running(self):
        remote = FakeRemote()
        res = orm.preflight(remote, "whatever")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "bad_profile")
        self.assertEqual(remote.calls, [])

    def test_conclusion_exit_codes_are_not_failures(self):
        for rc in (0, 1, 3, 4):
            remote = FakeRemote(run_result=(rc, json.dumps({"scope_status": "blocked", "tasks": []})))
            res = orm.preflight(remote, "evidence")
            self.assertTrue(res["ok"], "exit %d 被误当成执行失败" % rc)
            self.assertEqual(res["exit_code"], rc)

    def test_execution_failure_is_reported(self):
        for rc in (2, -1):
            remote = FakeRemote(run_result=(rc, "Traceback"))
            res = orm.preflight(remote, "evidence")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "preflight_failed")

    def test_report_is_extracted_from_noisy_output(self):
        payload = {"scope_status": "pass", "tasks": [{"id": "A01"}]}
        remote = FakeRemote(run_result=(1, "warning line\n" + json.dumps(payload) + "\ntrailing"))
        res = orm.preflight(remote, "evidence")
        self.assertTrue(res["ok"])
        self.assertEqual(res["tasks"], [{"id": "A01"}])

    def test_isolated_profile_is_allowed(self):
        remote = FakeRemote(run_result=(0, json.dumps({"scope_status": "pass"})))
        self.assertTrue(orm.preflight(remote, "isolated")["ok"])
        self.assertIn("--profile isolated", remote.calls[0][1])

    def test_preflight_runs_with_dry_run_and_persona_root(self):
        remote = FakeRemote(run_result=(0, json.dumps({"scope_status": "pass"})))
        orm.preflight(remote, "evidence")
        cmd = remote.calls[0][1]
        self.assertIn("QQ_DRY_RUN=1", cmd)
        self.assertIn("PERSONA_DATA_DIR=", cmd)
        self.assertIn("/data/persona", cmd)


class BackupTests(unittest.TestCase):
    def test_backup_uses_online_sqlite_backup_not_file_copy(self):
        remote = FakeRemote(python_result=(0, json.dumps(
            {"dir": "/opt/qqbot_backups/ops-x", "backed_up": ["self.db"], "failed": [],
             "backup_root_bytes": 10, "backup_dirs": 1})))
        res = orm.backup_persona(remote)
        self.assertTrue(res["ok"])
        code = remote.calls[0][1]
        self.assertIn("s.backup(d)", code)
        self.assertIn("mode=ro", code)

    def test_backup_never_prunes_by_default(self):
        remote = FakeRemote(python_result=(0, json.dumps(
            {"dir": "d", "backed_up": [], "failed": [], "backup_root_bytes": 0, "backup_dirs": 0})))
        orm.backup_persona(remote)
        code = remote.calls[0][1]
        self.assertNotIn("rmtree", code)
        self.assertNotIn("os.remove", code)
        self.assertNotIn("unlink", code)

    def test_partial_backup_failure_is_not_ok(self):
        remote = FakeRemote(python_result=(0, json.dumps(
            {"dir": "d", "backed_up": ["a.db"], "failed": ["b.db:OperationalError"],
             "backup_root_bytes": 0, "backup_dirs": 1})))
        self.assertFalse(orm.backup_persona(remote)["ok"])


class RestartTests(unittest.TestCase):
    def test_restart_verifies_afterwards(self):
        remote = FakeRemote(run_result=(0, "active"))
        with mock.patch.object(orm.time, "sleep"):
            res = orm.restart_qqbot(remote)
        self.assertTrue(res["ok"])
        self.assertEqual(len(remote.calls), 2)
        self.assertIn("is-active", remote.calls[1][1])

    def test_restart_that_stays_down_is_not_ok(self):
        remote = FakeRemote(run_result=(0, "failed"))
        with mock.patch.object(orm.time, "sleep"):
            res = orm.restart_qqbot(remote)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not_active_after_restart")


class JsonTransferTests(unittest.TestCase):
    def test_write_json_is_atomic(self):
        remote = FakeRemote(python_result=(0, "ok"))
        self.assertTrue(remote.write_json("ops/plan.json", {"a": 1}))
        code = remote.calls[0][1]
        self.assertIn("os.replace(tmp, p)", code)
        self.assertIn("fsync", code)

    def test_write_json_passes_payload_via_stdin_not_shell(self):
        remote = FakeRemote(python_result=(0, "ok"))
        remote.write_json("ops/plan.json", {"text": "'; rm -rf / #"})
        self.assertEqual(json.loads(remote.calls[0][2]), {"text": "'; rm -rf / #"})
        self.assertNotIn("rm -rf", remote.calls[0][1])

    def test_read_json_missing_file_returns_none(self):
        remote = FakeRemote(python_result=(0, "null"))
        self.assertIsNone(remote.read_json("ops/plan.json"))

    def test_read_json_failure_returns_none(self):
        remote = FakeRemote(python_result=(-1, "boom"))
        self.assertIsNone(remote.read_json("ops/plan.json"))


class CallOpsTests(unittest.TestCase):
    """远端只许调白名单函数，而且拒绝要发生在连接之前。"""

    def _remote(self):
        remote = orm.Remote()
        remote.python = mock.Mock(return_value=(0, json.dumps({"ok": True})))
        return remote

    def test_unlisted_function_is_refused_without_executing(self):
        remote = self._remote()
        res = remote.call_ops("__import__", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "func_not_allowed")
        remote.python.assert_not_called()

    def test_payload_travels_via_stdin(self):
        remote = self._remote()
        remote.call_ops("record_decision", {"task_id": "M01", "decision": "approve"})
        code = remote.python.call_args[0][0]
        stdin = remote.python.call_args[1]["stdin"]
        self.assertNotIn("M01", code)
        self.assertEqual(json.loads(stdin), {"task_id": "M01", "decision": "approve"})

    def test_remote_failure_is_reported_not_silenced(self):
        remote = self._remote()
        remote.python.return_value = (1, "Traceback: no module named ops_channel")
        res = remote.call_ops("load_state")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "remote_failed")

    def test_bad_json_is_not_read_as_success(self):
        remote = self._remote()
        remote.python.return_value = (0, "not json")
        self.assertEqual(remote.call_ops("load_state")["error"], "bad_json")


if __name__ == "__main__":
    unittest.main()