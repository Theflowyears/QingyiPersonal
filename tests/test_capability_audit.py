# -*- coding: utf-8 -*-
import os,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"bot"))
import capability_audit as ca

class CapabilityAuditTests(unittest.TestCase):
    def test_layers_are_not_collapsed_into_implementation(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ,{"DATA_DIR":root}):
            row=ca.status()["capabilities"]["active_memory"]
        self.assertEqual(set(row),{"implemented","wired","exercised","evaluated","released"})
        self.assertFalse(row["released"])

    def test_evaluation_reports_actual_denominators_and_failures(self):
        report=ca.evaluate()
        self.assertGreaterEqual(report["total"],50)
        self.assertEqual(report["total"],report["passed"]+report["failed"])
        self.assertIn("cases",report)
        self.assertTrue(all("expected" in x and "actual" in x for x in report["cases"]))

    def test_evaluation_does_not_change_process_environment(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ,{"DATA_DIR":root}):
            changes=[]
            original=os.environ.__class__.__setitem__
            def observe(env,key,value):
                if key in ('DATA_DIR','PERSONA_DATA_DIR'):changes.append(key)
                return original(env,key,value)
            with mock.patch.object(os.environ.__class__,'__setitem__',observe):
                ca.evaluate()
        self.assertEqual([],changes)

    def test_generic_manifest_does_not_exercise_unrelated_capabilities(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ,{'DATA_DIR':root}):
            import memory_manifest as mm
            mm.record('dm','private:9',[{'role':'user','content':'hello'}],[],[])
            rows=ca.status()['capabilities']
        self.assertFalse(rows['context_attribution']['exercised'])
        self.assertFalse(rows['knowledge_boundary']['exercised'])

    def test_plain_request_does_not_claim_emotion_expression_was_exercised(self):
        with tempfile.TemporaryDirectory() as root,mock.patch.dict(os.environ,{'DATA_DIR':root}):
            import memory_manifest as mm
            mm.record('dm','private:9',[{'role':'user','content':'hello'}],[],[],runtime={'expression_source':'surface'})
            rows=ca.status()['capabilities']
        self.assertFalse(rows['expression_priority']['exercised'])

    def test_stale_evaluation_is_not_valid_for_current_code(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ,{'DATA_DIR':root}):
            import json
            p=Path(root)/'ops'/'capability_evaluation.json';p.parent.mkdir()
            p.write_text(json.dumps({'total':35,'passed':35,'failed':0,'binding':'stale'}))
            rows=ca.status()['capabilities']
        self.assertFalse(rows['active_memory']['evaluated'])

    def test_manifest_exercises_only_when_material_was_present(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ,{"DATA_DIR":root}):
            import memory_manifest as mm
            mm.record("dm","private:9",[{"role":"user","content":"冰可乐"}],
                      [{"id":"m1","kind":"reported","text":"冰可乐","source":"memory_runtime"}],[])
            row=ca.status()["capabilities"]["active_memory"]
        self.assertTrue(row["exercised"])

if __name__=="__main__":unittest.main()