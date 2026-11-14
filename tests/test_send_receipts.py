from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b
import napcat_channel as nc


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_private_send_is_not_remembered_as_delivered(self):
        key='private-user-774411'
        b.histories.pop(key,None)
        self.addCleanup(b.histories.pop,key,None)
        ws=type('Wire',(),{'send':AsyncMock()})()
        with patch.object(b,'QQ_DRY_RUN',False),patch.object(nc.channel,'ws',ws), \
             patch.object(nc.channel,'call',new=AsyncMock(return_value={'status':'failed','retcode':100,'message':'rejected'})), \
             patch('self_model.record_action',return_value='action-test'), \
             patch('self_model.mark_action'),patch.object(b,'save_histories'):
            ok=await b.send_private_msg(ws,'774411','这条请别记成发出。')
        self.assertIs(ok,False)
        self.assertNotIn(('assistant','这条请别记成发出。'),list(b.histories.get(key) or []))

    async def test_observer_socket_wrapper_still_uses_the_bound_channel(self):
        real = type('Wire', (), {'send': AsyncMock()})()
        proxy = type('ObservedSocket', (), {'ws': real, 'send': real.send})()
        with patch.object(nc.channel, 'ws', real), \
             patch.object(nc.channel, 'call', new=AsyncMock(return_value={'status':'failed','retcode':100})):
            ok = await b._submit_chat(proxy, 'send_private_msg', {'user_id':1,'message':[]})
        self.assertIs(ok, False)
        self.assertEqual(real.send.await_count, 0)

    async def test_unknown_receipt_does_not_retry_with_another_socket_send(self):
        key='private-user-774422'
        b.histories.pop(key,None)
        self.addCleanup(b.histories.pop,key,None)
        ws=type('Wire',(),{'send':AsyncMock()})()
        with patch.object(b,'QQ_DRY_RUN',False),patch.object(nc.channel,'ws',ws), \
             patch.object(nc.channel,'call',new=AsyncMock(return_value=None)), \
             patch('self_model.record_action',return_value='action-test'), \
             patch('self_model.mark_action'),patch.object(b,'save_histories'):
            ok=await b.send_private_msg(ws,'774422','未知不是失败，别再发一遍。')
        self.assertIs(ok,False)
        self.assertEqual(ws.send.await_count,0)


if __name__=='__main__':unittest.main()
