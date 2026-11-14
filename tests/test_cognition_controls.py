import os
import tempfile
import unittest
from unittest.mock import patch
from bot import self_model as m


class CognitionControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, DATA_DIR=self.tmp.name,
                              PERSONA_DATA_DIR='', QQ_DRY_RUN='1')
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_persona_store_honors_explicit_persona_root(self):
        from bot import persona_store
        from pathlib import Path
        with patch.dict(os.environ, PERSONA_DATA_DIR=self.tmp.name + '/explicit'):
            persona_store.add('self_view', 'persisted identity')
            self.assertTrue(Path(self.tmp.name, 'explicit', 'persona.db').is_file())
            self.assertEqual(['persisted identity'], persona_store.items('self_view'))
        self.assertEqual([], persona_store.items('self_view'))
        persona_store._conn.close()
        persona_store._conn = None

    def test_outbox_concurrent_claim_has_exactly_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        m.prepare_outbox('race', {'text': 'once'})
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: m.claim_outbox('race'), range(16)))
        self.assertEqual(1, sum(results))
        self.assertEqual('submitted', m.get_outbox('race')['state'])

    def test_retracted_evidence_cannot_be_reused_as_current_support(self):
        eid = m.record_event('observation')
        m.retract_evidence(eid, 'wrong')
        bid = m.add_belief('new claim', evidence=[eid])
        self.assertNotEqual('active', m.explain_belief(bid)['belief']['status'])
        m.note_self_view('new view', evidence=[eid])
        self.assertEqual([], m.self_view_history())
        m.add_goal('new goal', parent_event=eid)
        self.assertEqual([], m.active_goals())

    def test_layer_revisions_are_immutable_content_hashes_not_schema_versions(self):
        self.assertTrue(hasattr(m, 'revise_layer'))
        first = m.revise_layer('identity', {'name': 'a'}, 'initial')
        second = m.revise_layer('identity', {'name': 'b'}, 'correction')
        self.assertNotEqual(first, second)
        self.assertEqual(first, m.revise_layer('identity', {'name': 'a'}, 'restore'))
        history = m.layer_history('identity')
        self.assertEqual(3, len(history))
        self.assertEqual({'name': 'a'}, history[0]['content'])
        self.assertEqual({'name': 'b'}, history[1]['content'])
        for layer in ('boundary', 'personality', 'work_state'):
            self.assertNotEqual(first, m.revise_layer(layer, {'name': 'a'}, 'initial'))
        with self.assertRaises(ValueError):
            m.revise_layer('schema', {}, 'not a layer')

    def test_consent_is_explicit_scoped_and_deny_wins_until_revoked(self):
        self.assertTrue(hasattr(m, 'consent_decision'))
        self.assertEqual('unknown', m.consent_decision('123', 'dm'))
        allow = m.record_consent('123', 'dm', 'allow', 'explicit request')
        self.assertEqual('allow', m.consent_decision('123', 'dm'))
        self.assertEqual('unknown', m.consent_decision('456', 'dm'))
        self.assertEqual('unknown', m.consent_decision('123', 'photo'))
        deny = m.record_consent('123', 'dm', 'deny', 'stop')
        m.record_consent('123', 'dm', 'allow', 'new request')
        self.assertEqual('deny', m.consent_decision('123', 'dm'))
        self.assertTrue(m.revoke_consent(deny, 'explicit withdrawal'))
        self.assertEqual('allow', m.consent_decision('123', 'dm'))
        self.assertTrue(m.revoke_consent(allow, 'withdrawn'))
        with self.assertRaises(ValueError):
            m.record_consent('123', 'dm', 'inferred', 'friendly')

    def test_outbox_claim_is_persistent_and_receipts_are_per_part(self):
        self.assertTrue(hasattr(m, 'prepare_outbox'))
        first = m.prepare_outbox('logical', {'text': 'hello'}, parts=2)
        self.assertEqual(first, m.prepare_outbox('logical', {'text': 'hello'}, parts=2))
        with self.assertRaises(ValueError):
            m.prepare_outbox('logical', {'text': 'changed'}, parts=2)
        self.assertTrue(m.claim_outbox('logical', 0))
        self.assertFalse(m.claim_outbox('logical', 0))
        self.assertTrue(m.record_outbox_receipt('logical', 0, 'accepted', 'r1'))
        self.assertEqual('pending', m.get_outbox('logical')['state'])
        self.assertTrue(m.claim_outbox('logical', 1))
        self.assertEqual('submitted', m.get_outbox('logical')['state'])
        self.assertFalse(m.claim_outbox('logical', 1))
        self.assertTrue(m.record_outbox_receipt('logical', 1, 'failed', 'r2'))
        self.assertEqual('partial', m.get_outbox('logical')['state'])
        self.assertFalse(m.record_outbox_receipt('logical', 1, 'accepted', 'late'))

    def test_retraction_preserves_event_and_invalidates_dependents(self):
        eid = m.record_event('observation', payload={'text': 'original'})
        original = m.get_event(eid)
        bid = m.add_belief('claim', evidence=[eid])
        gid = m.add_goal('check', parent_event=eid)
        sid = m.note_self_view('view', evidence=[eid])
        episode = m.record_episode('experience', [eid])
        cid = m.add_commitment('123', 'promise', evidence=[eid])
        self.assertTrue(hasattr(m, 'retract_evidence'))
        self.assertTrue(m.retract_evidence(eid, 'source corrected'))
        self.assertEqual(original, m.get_event(eid))
        self.assertEqual([], m.explain_belief(bid)['evidence'])
        self.assertEqual('invalidated', m.explain_belief(bid)['belief']['status'])
        self.assertEqual([], m.active_goals())
        self.assertEqual([], m.self_view_history())
        for kind, ident in [('goal', gid), ('self_view_rev', sid),
                            ('episode', episode), ('commitment', cid)]:
            self.assertFalse(m.evidence_valid(kind, ident))
        self.assertTrue(m.retract_evidence(eid, 'repeat'))
        self.assertFalse(m.retract_evidence('missing', 'no'))
