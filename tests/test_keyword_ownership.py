from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class KeywordOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_discussing_bot_in_third_person_is_not_directed_request(self):
        gid='770099'
        event={'self_id':9,'user_id':'9000000003','group_id':gid,'message_id':100,
               'sender':{'card':'阿甲'},'message':[{'type':'text','data':{'text':'主人又在给清漪加功能了'}}]}
        async def no(*a,**kw):return False
        with patch.object(b,'record_group_message'),patch.object(b,'is_ignored_sender',return_value=False), \
             patch.object(b,'TRIGGER_WORDS',['清漪']),patch.object(b,'handle_admin_commands',side_effect=no), \
             patch.object(b,'handle_sticker_command',side_effect=no), \
             patch.object(b,'_maybe_cancel_draw',side_effect=no),patch.object(b,'handle_draw_command',side_effect=no), \
             patch.object(b,'handle_sing_command',side_effect=no),patch.object(b,'handle_voice_command',side_effect=no), \
             patch('affection.judge_and_bump',return_value=(0,'')),patch('affection.set_nick'), \
             patch.object(b,'schedule_auto_reply') as scheduled, \
             patch.object(b,'_direct_group_reply',new=AsyncMock()) as direct:
            await b.handle_group_message(None,event)
        self.assertEqual(direct.await_count,0)
        self.assertEqual(scheduled.call_count,1)


if __name__=='__main__':unittest.main()
