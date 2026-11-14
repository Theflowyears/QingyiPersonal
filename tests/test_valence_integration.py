import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import emotion_shadow as shadow
import valence_candidates as candidates


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, PERSONA_DATA_DIR=self.tmp.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        with closing(sqlite3.connect(Path(self.tmp.name) / 'cognition.db')) as c, c:
            c.execute('CREATE TABLE event(id TEXT PRIMARY KEY,kind TEXT,ts REAL,created REAL,session TEXT,source TEXT,payload TEXT)')

    def add(self, eid='old', text='这次处理得很靠谱', **extra):
        payload = dict(text=text, addressed_to_self=True, address_basis='private', **extra)
        with closing(sqlite3.connect(Path(self.tmp.name) / 'cognition.db')) as c, c:
            c.execute('INSERT INTO event VALUES(?,?,?,?,?,?,?)', (eid, 'private_message', 100, 101, 'private:1', 'onebot', json.dumps(payload)))
        candidates.enqueue(eid, at=100, session='private:1', kind='private_message', text=text, address_basis='private')

    def label(self, stance='praise', confidence=.95):
        asyncio.run(candidates.drain(limit=10, judge=lambda *_: dict(stance=stance, confidence=confidence, reason='明确肯定', model='test-model')))

    def test_history_and_late_label_replay_without_live_pollution(self):
        self.add()
        shadow.tick(now=200)
        before = shadow.status(now=200)
        self.assertIn('valence_samples', before)
        self.assertEqual(before['valence_samples']['stats']['included'], 0)
        self.label()
        after = shadow.status(now=200)
        report = after['valence_samples']
        self.assertEqual(report['stats']['included'], 1)
        self.assertGreater(report['state']['valence'], 0)
        self.assertEqual(before['state'], after['state'])
        self.assertEqual(after['processed'], 0)
        self.assertEqual(report, shadow.status(now=200)['valence_samples'])

    def test_original_gates_and_uncertainty_override_lexical(self):
        self.add('quote', text='谢谢你', quoted=True)
        self.add('neutral', text='谢谢你')
        self.label(stance='uncertain')
        report = shadow.valence_samples(now=200)
        self.assertEqual(report['stats']['included'], 0)
        self.assertEqual(report['state']['valence'], 0)
        self.assertTrue(all(r['exclusion_reason'] for r in report['rows']))

    def test_semantic_rebuke_replaces_lexical_praise_once(self):
        self.add(text='谢谢你啊这次真让人失望')
        self.label(stance='rebuke')
        report = shadow.valence_samples(now=200)
        self.assertEqual(report['stats']['included'], 1)
        self.assertGreater(report['rows'][0]['rule_appraisal']['delta']['valence'], 0)
        self.assertLess(report['rows'][0]['appraisal']['delta']['valence'], 0)
        self.assertLess(report['state']['valence'], 0)
        self.assertEqual(report['state']['processed'], 1)

    def test_roleplay_and_missing_source_rejected(self):
        self.add(is_roleplay=True)
        self.label()
        self.assertEqual(shadow.valence_samples(now=200)['rows'][0]['exclusion_reason'], 'indirect_or_roleplay')
        with closing(sqlite3.connect(Path(self.tmp.name) / 'cognition.db')) as c, c:
            c.execute('DELETE FROM event')
        self.assertEqual(shadow.valence_samples(now=200)['rows'][0]['exclusion_reason'], 'source_missing')

    def test_lexical_and_candidate_overlap_preserves_live_evidence_replay(self):
        shadow.tick(now=50)
        self.add(text='谢谢你')
        shadow.tick(now=200)
        original = shadow.status(now=200)
        self.label()
        updated = shadow.status(now=200)
        self.assertEqual(updated['state'], original['state'])
        self.assertEqual(updated['timeline'], original['timeline'])
        self.assertTrue(updated['diagnostics']['evidence_replay']['ok'])
        self.assertTrue(shadow.verify_replay()['ok'])
        self.assertEqual(updated['valence_samples']['stats']['included'], 1)
        self.assertEqual(updated['valence_samples']['state']['processed'], 1)

    def test_historical_truncated_source_matches_candidate_prefix(self):
        text = '这次处理得很靠谱' + '长消息内容' * 60
        self.add(text=text)
        with closing(sqlite3.connect(Path(self.tmp.name) / 'cognition.db')) as c, c:
            payload = dict(text=text[:200], addressed_to_self=True, address_basis='private')
            c.execute('UPDATE event SET payload=?', (json.dumps(payload),))
        self.label()
        report = shadow.valence_samples(now=200)
        self.assertEqual(report['stats']['included'], 1)
        with closing(sqlite3.connect(Path(self.tmp.name) / 'valence_candidates.db')) as c, c:
            c.execute("UPDATE candidate SET text='不同前缀' || substr(text,5)")
        self.assertEqual(shadow.valence_samples(now=200)['rows'][0]['exclusion_reason'], 'source_mismatch')

    def test_mismatched_source_and_low_confidence_rejected(self):
        self.add()
        self.label(confidence=.2)
        self.assertEqual(shadow.valence_samples(now=200)['stats']['included'], 0)
        with closing(sqlite3.connect(Path(self.tmp.name) / 'valence_candidates.db')) as c, c:
            c.execute("UPDATE candidate SET confidence=.99,text='伪造原句'")
        row = shadow.valence_samples(now=200)['rows'][0]
        self.assertEqual(row['exclusion_reason'], 'source_mismatch')


if __name__ == '__main__':
    unittest.main()
