# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"bot"))
import worldbook_store as wb

COMMIT="3581471ef934ab7a0d8c99b165b0cf04a0984010"
def lorebook(name="基础设定", entries=None):
    return {"version":1,"type":"lorebook","data":{"id":"book","name":name,"entries":entries or [
        {"id":"e1","name":"罗德岛","enabled":True,"content":"罗德岛是泰拉的医药组织。"*20,
         "keywords":["罗德岛","Rhodes Island"],"role":"system","constantActive":True,
         "useRegex":True,"injectDepth":99,"position":"after_system_prompt"}]}}

class WorldbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=mock.patch.dict(os.environ,{"DATA_DIR":self.tmp.name});env.start();self.addCleanup(env.stop)

    def test_import_keeps_full_content_and_drops_execution_fields(self):
        rows=wb.parse_lorebook(lorebook(),source_file="00_基础设定.json",source_commit=COMMIT)
        self.assertEqual(len(rows),1)
        row=rows[0]
        self.assertEqual(row["trust_level"],"community_unverified")
        self.assertEqual(row["source_commit"],COMMIT)
        self.assertEqual(row["content"],lorebook()["data"]["entries"][0]["content"])
        self.assertNotIn("role",row);self.assertNotIn("constantActive",row)
        self.assertNotIn("useRegex",row)

    def test_revision_path_cannot_escape_the_version_store(self):
        store=wb.WorldbookStore(self.tmp.name)
        with self.assertRaises(ValueError):
            store.activate_revision('../../outside')

    def test_tampered_release_cannot_be_activated(self):
        release=wb.build_release([('00_基础设定.json',lorebook())],COMMIT)
        store=wb.WorldbookStore(self.tmp.name)
        store.save_release(release)
        path=store._release_path(release['revision'])
        data=json.loads(path.read_text(encoding='utf-8'))
        data['entries'][0]['content']='tampered'
        path.write_text(json.dumps(data),encoding='utf-8')
        self.assertFalse(store.activate_revision(release['revision']))

    def test_disabled_entries_are_preserved_but_not_retrieved(self):
        data=lorebook();data['data']['entries'][0]['enabled']=False
        release=wb.build_release([('03_组织.json',data)],COMMIT)
        store=wb.WorldbookStore(self.tmp.name);store.save_release(release);store.activate_revision(release['revision'])
        self.assertEqual([],store.search('明日方舟罗德岛'))

    def test_real_world_medical_words_do_not_inject_fiction(self):
        store=wb.WorldbookStore(self.tmp.name)
        release=wb.build_release([('00_基础设定.json',lorebook())],COMMIT)
        store.save_release(release);store.activate_revision(release['revision'])
        self.assertEqual([],store.search('今天感染了需要医药组织治疗'))

    def test_duplicate_entry_ids_are_rejected(self):
        data=lorebook(entries=[lorebook()["data"]["entries"][0]]*2)
        with self.assertRaises(ValueError): wb.parse_lorebook(data,source_file="x",source_commit=COMMIT)

    def test_release_diff_and_rollback_preserve_old_revision(self):
        a=wb.build_release([("00_基础设定.json",lorebook())],COMMIT)
        changed=lorebook();changed["data"]["entries"][0]["content"]="新版内容"
        b=wb.build_release([("00_基础设定.json",changed)],COMMIT)
        diff=wb.diff_releases(a,b)
        self.assertEqual(len(diff["changed"]),1)
        store=wb.WorldbookStore(self.tmp.name)
        self.assertTrue(store.save_release(a));self.assertTrue(store.activate_revision(a["revision"]))
        self.assertTrue(store.save_release(b));self.assertTrue(store.activate_revision(b["revision"]))
        self.assertEqual(store.load_active()["revision"],b["revision"])
        self.assertTrue(store.rollback(a["revision"]))
        self.assertEqual(store.load_active()["revision"],a["revision"])
        self.assertEqual(len(store.revisions()),2)

    def test_unrelated_query_does_not_dump_lore(self):
        store=wb.WorldbookStore(self.tmp.name);r=wb.build_release([("03_组织.json",lorebook())],COMMIT)
        store.save_release(r);store.activate_revision(r["revision"])
        self.assertEqual(store.search("今天吃什么"),[])
        self.assertEqual(store.search("罗德岛",domain_hint="明日方舟")[0]["canonical_name"],"罗德岛")

    def test_ambiguous_lily_requires_domain_context(self):
        entry=lorebook()["data"]["entries"][0];entry.update(id="e2",name="主人",keywords=["主人"],content="主人是明日方舟干员。")
        r=wb.build_release([("04_干员.json",lorebook("干员",[entry]))],COMMIT)
        store=wb.WorldbookStore(self.tmp.name);store.save_release(r);store.activate_revision(r["revision"])
        self.assertEqual(store.search("主人今天开心吗"),[])
        self.assertTrue(store.search("明日方舟 主人"))

    def test_render_never_claims_personal_experience_or_verification(self):
        row=wb.parse_lorebook(lorebook(),source_file="03_组织.json",source_commit=COMMIT)[0]
        text=wb.render_context([row])
        self.assertIn("社区设定参考",text);self.assertIn("未逐项核验",text)
        self.assertNotIn("你亲历",text);self.assertNotIn("你查证过",text)

if __name__=="__main__":unittest.main()