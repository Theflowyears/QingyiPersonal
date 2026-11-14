# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"bot"))
import contract_sandbox as cs

class ContractSandboxTests(unittest.TestCase):
    def test_online_mode_requires_real_linux_isolation(self):
        with mock.patch.object(cs,"capabilities",return_value={"available":False,"reason":"no_user_namespace"}):
            out=cs.run(["test_memory_manifest"],online=True,timeout=5)
        self.assertEqual(out["status"],"not_verified")
        self.assertEqual(out["isolation"]["reason"],"no_user_namespace")

    def test_online_launcher_drops_mount_capabilities_and_hides_credentials(self):
        command=cs.command_for(['/usr/bin/python3','-c','pass'],Path('/tmp/probe'),timeout=10)
        self.assertIn('CapabilityBoundingSet=',command)
        self.assertIn('NoNewPrivileges=yes',command)
        self.assertIn('ProtectSystem=strict',command)
        self.assertIn('PrivateNetwork=yes',command)
        self.assertIn('RestrictNamespaces=yes',command)
        self.assertTrue(any('config.env' in arg for arg in command))

    def test_nested_worker_gets_a_writable_temp_directory(self):
        command=cs.command_for(['/usr/bin/python3','-c','pass'],Path('/tmp/probe'),timeout=10)
        root=str(Path('/tmp/probe').resolve())
        self.assertIn('TMPDIR='+root,command)
        self.assertIn('TEMP='+root,command)
        self.assertIn('TMP='+root,command)

    def test_production_changes_are_observed_not_test_violations(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"runtime.json";p.write_text("before")
            before=cs.snapshot(root)
            p.write_text("after")
            changes=cs.changes(before,cs.snapshot(root))
            verdict=cs.classify_changes(changes,online=True)
        self.assertEqual(verdict["status"],"concurrent_runtime_change")
        self.assertFalse(verdict["test_violation"])

    def test_offline_mode_still_rejects_any_protected_asset_change(self):
        verdict=cs.classify_changes([{"path":"x","kind":"modified"}],online=False)
        self.assertEqual(verdict["status"],"fail")
        self.assertTrue(verdict["test_violation"])

    def test_isolated_child_can_write_only_its_temp_root(self):
        with tempfile.TemporaryDirectory() as root:
            out=cs.run(["test_memory_manifest"],online=False,timeout=20,root=root)
        self.assertIn(out["status"],("pass","not_verified"))
        if out["status"]=="pass":
            self.assertTrue(Path(out["isolation"]["writable_root"]).is_relative_to(Path(root)))

    def test_symlink_escape_is_detected_before_execution(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            link=Path(root)/"escape";link.symlink_to(outside,target_is_directory=True)
            self.assertFalse(cs.writable_paths_safe(root))

class LinuxBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(cs.capabilities().get("available"),"Linux user/mount namespace unavailable")
    def test_child_cannot_write_project_or_escape_through_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            outside=Path(root)/"outside";outside.mkdir()
            link=Path(root)/"link";link.symlink_to(outside,target_is_directory=True)
            code=("from pathlib import Path\n"
                  "for p in [%r,%r]:\n"
                  " try:\n  (p/'escape.txt').write_text('x')\n except OSError: pass\n"
                  % (str(ROOT/"bot"),str(link)))
            result=cs.run_code(code,online=True,timeout=10)
        self.assertEqual(result["status"],"pass")
        self.assertFalse((ROOT/"bot"/"escape.txt").exists())
        self.assertFalse((outside/"escape.txt").exists())

    @unittest.skipUnless(cs.capabilities().get("available"),"Linux user/mount namespace unavailable")
    def test_child_can_write_its_own_sqlite_wal(self):
        code=("import os,sqlite3\n"
              "p=os.path.join(os.environ['DATA_DIR'],'x.db')\n"
              "c=sqlite3.connect(p);c.execute('pragma journal_mode=wal');c.execute('create table x(a)');c.commit();c.close()\n")
        result=cs.run_code(code,online=True,timeout=10)
        self.assertEqual(result["status"],"pass")


if __name__=="__main__":unittest.main()