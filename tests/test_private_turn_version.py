from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class VersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_private_fragment_stops_stale_generated_reply(self):
        uid='774466';key='private-user-'+uid
        self.addCleanup(b._TURN_VERSIONS.pop,key,None)
        b._TURN_VERSIONS[key]=1
        async def model(*args,**kwargs):
            b._TURN_VERSIONS[key]=2
            return '这句已过时，别发。'
        event={'user_id':uid,'self_id':9,'sender':{'nickname':'甲'},'message_id':1,
               'message':[{'type':'text','data':{'text':'我想想'}}]}
        with patch.object(b,'ask_openclaw',side_effect=model), \
             patch.object(b,'send_private_msg',new=AsyncMock()) as send, \
             patch.object(b,'handle_sticker_command',new=AsyncMock(return_value=False)), \
             patch.object(b,'_maybe_cancel_draw',new=AsyncMock(return_value=False)), \
             patch.object(b,'handle_draw_command',new=AsyncMock(return_value=False)), \
             patch.object(b,'handle_sing_command',new=AsyncMock(return_value=False)), \
             patch.object(b,'handle_voice_command',new=AsyncMock(return_value=False)), \
             patch('affection.judge_and_bump',return_value=(0,'')),patch('affection.set_nick'), \
             patch('self_model.record_event'):
            await b._handle_private_message(None,event)
        self.assertEqual(send.await_count,0)


if __name__=='__main__':unittest.main()
