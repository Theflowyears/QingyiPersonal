import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class InterleavedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_simultaneous_sends_do_not_both_pass_dedupe(self):
        delivered=[]
        class Wire:
            async def send(self,raw):
                await asyncio.sleep(0)
                delivered.append(json.loads(raw))
        b.histories.pop('private-user-888001',None)
        self.addCleanup(b.histories.pop,'private-user-888001',None)
        async def accepted_wire(ws, action, params):
            await ws.send(json.dumps({'action': action, 'params': params}))
            return {'retcode': 0}
        with patch.object(b, '_chat_response', side_effect=accepted_wire), \
             patch.object(b,'QQ_DRY_RUN',False),patch.object(b,'save_histories'), \
             patch('self_model.record_action',return_value=''),patch('affection.apply_self',return_value=None), \
             patch('history_log.append',return_value=True):
            await asyncio.gather(b.send_private_msg(Wire(),'888001','别被丧尸吓一跳。'),
                                 b.send_private_msg(Wire(),'888001','别被丧尸吓一跳。'))
        self.assertEqual(len(delivered),1)


if __name__=='__main__':unittest.main()
