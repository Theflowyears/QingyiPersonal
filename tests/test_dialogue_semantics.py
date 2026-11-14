import json
import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import dialogue_semantics as ds

class SemanticEvidenceTests(unittest.TestCase):
    def state(self):
        return {'turn':{'event_id':'e2','speaker_id':'9','text':'重庆终于出太阳了'},
                'turns':[{'event_id':'e1','speaker_id':'BOT','speaker_role':'assistant','text':'天气热得离谱'},
                         {'event_id':'e2','speaker_id':'9','speaker_role':'human','text':'重庆终于出太阳了'}]}

    def test_nonexistent_event_cannot_support_a_claim(self):
        data={'claims':[{'event_id':'invented','quote':'热得离谱','holder_id':'9','predicate':'嫌热','status':'supported'}]}
        out=ds.validate(data,self.state())
        self.assertEqual([],out['claims'])
        self.assertIn('unknown_event',out['rejected'])

    def test_quote_must_be_in_original_event(self):
        data={'claims':[{'event_id':'e2','quote':'我嫌热','holder_id':'9','predicate':'嫌热','status':'supported'}]}
        self.assertEqual([],ds.validate(data,self.state())['claims'])

    def test_model_cannot_reassign_direct_speech_to_another_holder(self):
        data={'claims':[{'event_id':'e1','quote':'热得离谱','holder_id':'9','predicate':'嫌热','status':'supported'}]}
        out=ds.validate(data,self.state())
        self.assertEqual([],out['claims'])
        self.assertIn('holder_not_supported',out['rejected'])

    def test_supported_claim_keeps_the_actual_source_span(self):
        data={'claims':[{'event_id':'e1','quote':'热得离谱','holder_id':'BOT','predicate':'嫌热','status':'supported'}]}
        claim=ds.validate(data,self.state())['claims'][0]
        self.assertEqual({'start':2,'end':6,'text':'热得离谱'},claim['quote_span'])

    def test_model_confidence_never_becomes_permission_or_fact(self):
        out=ds.validate({'authorization':True,'claims':[],'response_verdict':'supported'},self.state())
        self.assertNotIn('authorization',out)
        self.assertEqual('proposed',out['epistemic_status'])

class SemanticCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_result_is_not_treated_as_verified(self):
        async def model(messages,**kw):return 'not json'
        out=await ds.review('你嫌热',SemanticEvidenceTests().state(),model)
        self.assertEqual('degraded',out['status'])
        self.assertEqual('unknown',out['response_verdict'])

    async def test_messages_are_material_not_system_instructions(self):
        async def model(messages,**kw):
            self.assertNotIn('重庆终于出太阳了',messages[0]['content'])
            self.assertIn('重庆终于出太阳了',messages[1]['content'])
            return json.dumps({'claims':[],'response_verdict':'unsupported','reason':'错误归属'})
        out=await ds.review('你嫌热',SemanticEvidenceTests().state(),model)
        self.assertEqual('unsupported',out['response_verdict'])

if __name__=='__main__':unittest.main()
