import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import memory_governance as mg

class GovernanceTests(unittest.TestCase):
    def test_inventory_preserves_original_assets_and_does_not_promote_unknown_sources(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'DATA_DIR':root}):
            p=Path(root)/'memory'/'world_knowledge.json';p.parent.mkdir()
            raw=json.dumps({'topics':[{'key':'泰拉','summary':'小说中的世界','status':'active'}]}).encode()
            p.write_bytes(raw)
            report=mg.inventory()
            self.assertEqual(raw,p.read_bytes())
            self.assertEqual('world_knowledge',report['assets'][0]['category'])
            self.assertEqual('unknown',report['assets'][0]['source_quality'])
            self.assertEqual([],report['mutations'])

    def test_governance_export_is_derived_and_versioned(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'DATA_DIR':root}):
            p=Path(root)/'memory';p.mkdir()
            (p/'world_knowledge.json').write_text('{"topics":[]}',encoding='utf-8')
            result=mg.build_catalog()
            self.assertTrue(result['revision'])
            self.assertTrue((Path(root)/'ops'/'memory_catalog'/ (result['revision']+'.json')).is_file())
            self.assertTrue((p/'world_knowledge.json').is_file())

    def test_style_corpus_and_private_dialogues_are_never_combined_with_public_knowledge(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{'DATA_DIR':root}):
            (Path(root)/'chat_corpus').mkdir()
            (Path(root)/'chat_corpus'/'真人聊天.txt').write_text('隐私原句',encoding='utf-8')
            out=mg.inventory()
            self.assertNotIn('隐私原句',str(out))
            self.assertFalse(any(x['category']=='world_knowledge' for x in out['assets']))

if __name__=='__main__':unittest.main()
