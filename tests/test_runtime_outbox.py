import os
os.environ['QQ_DRY_RUN'] = '1'
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import qq_openclaw_bridge as b
import self_model as sm
import trigger_ledger as ledger


class RuntimeOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, DATA_DIR=self.tmp.name, PERSONA_DATA_DIR='', QQ_DRY_RUN='1')
        env.start()
        self.addCleanup(env.stop)
        ledger.reset()
        ledger.capture('private:19')

    async def test_unknown_never_resends(self):
        ledger.note('private:19', sm.record_event('private_message', dedupe='trigger-a'))
        ledger.capture('private:19')
        with patch.object(b, '_chat_response', AsyncMock(return_value=None)) as call:
            for _ in range(2):
                self.assertFalse(await b._submit_chat(None, 'send_private_msg', {'user_id':19}))
        self.assertEqual(call.await_count, 1)

    async def test_later_turn_can_send_identical_payload(self):
        with patch.object(b, '_chat_response', AsyncMock(return_value={'retcode':0})) as call:
            for event in ('a', 'b'):
                ledger.reset()
                ledger.note('private:19', event)
                ledger.capture('private:19')
                self.assertTrue(await b._submit_chat(None, 'send_private_msg', {'user_id':19}))
        self.assertEqual(call.await_count, 2)

    async def test_exception_is_unknown_receipt(self):
        with patch.object(b, '_chat_response', AsyncMock(side_effect=OSError('wire'))):
            self.assertFalse(await b._submit_chat(None, 'send_private_msg', {'user_id':19}, logical_key='exception'))
        self.assertEqual(sm.get_outbox('exception')['state'], 'unknown')

    async def test_deny_blocks_effect(self):
        sm.record_consent('19', 'send_private_msg', 'deny', 'explicit refusal')
        with patch.object(b, '_chat_response', AsyncMock()) as call:
            self.assertFalse(await b._submit_chat(None, 'send_private_msg', {'user_id':19}))
        call.assert_not_awaited()

    def test_evidence_snapshot_does_not_follow_arrivals(self):
        original = sm.record_event('private_message', dedupe='input')
        ledger.note('private:19', original)
        ledger.capture('private:19')
        ledger.note('private:19', sm.record_event('private_message', dedupe='later'))
        self.assertEqual(b._action_evidence('private:19'), [original])

    async def test_multipart_action_not_accepted_after_first_chunk(self):
        aid = sm.record_action('dm', state='generated', target_qq='19', text='whole')
        whole = {'text': 'whole'}
        with patch.object(b, '_chat_response', AsyncMock(side_effect=[{'retcode':0}, None])), patch.object(sm, 'mark_action', wraps=sm.mark_action) as mark:
            self.assertTrue(await b._submit_chat(None, 'send_private_msg', {'user_id':19, 'chunk':0}, aid,
                            logical_key='multi', part=0, parts=2, delivery_payload=whole))
            self.assertNotIn('accepted', [c.args[1] for c in mark.call_args_list])
            self.assertFalse(await b._submit_chat(None, 'send_private_msg', {'user_id':19, 'chunk':1}, aid,
                             logical_key='multi', part=1, parts=2, delivery_payload=whole))
        self.assertEqual(sm.get_outbox('multi')['state'], 'partial')
        self.assertEqual(mark.call_args_list[-1].args[1], 'unknown')

    async def test_raw_socket_uses_response_waiter(self):
        import napcat_channel as nc
        with patch.object(nc.channel, 'ws', None), patch.object(b, 'onebot_call', AsyncMock(return_value=None)) as waiter:
            self.assertIsNone(await b._chat_response(object(), 'send_private_msg', {}))
        waiter.assert_awaited_once()
