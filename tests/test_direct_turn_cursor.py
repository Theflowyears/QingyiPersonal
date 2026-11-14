from collections import deque
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class DirectCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_reply_does_not_consume_messages_arriving_while_generating(self):
        gid='771199'
        b.group_message_cache[gid]=deque([
            {'seq':10,'message_id':100,'ts':time.time(),'is_bot':False,
             'user_id':'1','text':'@清漪 赚点外快','nickname':'甲','image_urls':[]}])
        b.group_message_seq[gid]=10
        b.group_last_periodic_seq[gid]=0
        for d in (b.group_message_cache,b.group_message_seq,b.group_last_periodic_seq,b._GROUP_SPEAK):
            self.addCleanup(d.pop,gid,None)
        async def generate(*args,**kwargs):
            b.group_message_cache[gid].append(
                {'seq':11,'message_id':101,'ts':time.time(),'is_bot':False,
                 'user_id':'2','text':'我也有个问题','nickname':'乙','image_urls':[]})
            b.group_message_seq[gid]=11
            return '先看看你有什么能接的小活。'
        with patch.object(b,'ask_openclaw',side_effect=generate), \
             patch.object(b,'send_group_msg',new=AsyncMock(return_value=True)), \
             patch.object(b.msg_enhance,'reply_group_msg',new=AsyncMock()), \
             patch.object(b,'AUTO_QUOTE_REPLY',False),patch.object(b,'_tool_plan',return_value={}), \
             patch.object(b,'save_periodic_state'),patch.object(b,'STICKER_PROB',0):
            await b._direct_group_reply(None,{'sender':{'nickname':'甲'}},gid,'1',
                                       '赚点外快',[],True,100,None)
        self.assertEqual(b.group_last_periodic_seq[gid],10)


if __name__=='__main__':unittest.main()
