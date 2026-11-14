import asyncio
from collections import deque
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import qq_openclaw_bridge as b
import ceylan_control_runtime as ctl


class GroupDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gid = '771144'
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        b.group_message_cache.pop(self.gid, None)
        b._GROUP_SPEAK.pop(self.gid, None)
        b.group_last_periodic_seq.pop(self.gid, None)
        for d in (b.group_message_cache, b._GROUP_SPEAK, b.group_last_periodic_seq,
                  b._auto_reply_state):
            self.addCleanup(d.pop, self.gid, None)
        for p in (patch.dict(os.environ, {'DATA_DIR': self.tmp.name}),
                  patch.object(b, 'save_periodic_state', lambda: None),
                  patch.object(b, 'GROUP_AUTO_REPLY_ENABLED', True),
                  patch.dict(ctl.OPTIONS, {'patrol_enabled': True}),
                  patch('draw_flow.has_pending', return_value=False),
                  patch.object(b, '_group_aliases', return_value={'阿戊':'9000000004','阿甲':'9000000003'})):
            p.start()
            self.addCleanup(p.stop)
        gate = patch.object(b, 'GROUP_AUTO_REPLY_DEBOUNCE', 0)
        gate.start()
        self.addCleanup(gate.stop)

    def rows(self, text='阿戊棒棒！'):
        b.group_message_cache[self.gid] = deque([
            {'seq': 1, 'ts': time.time(), 'time':'21:14:22','user_id':'BOT',
             'text':'姐姐别怕，我现在脑子清醒得很。','is_bot':True},
            {'seq': 2, 'ts': time.time(), 'time':'21:14:34','user_id':'9000000003',
             'nickname':'烤苕皮！','text':text,'is_bot':False,'self_id':'9',
             'image_urls':[],'at_targets':[]}])

    async def test_third_party_praise_is_consumed_without_generation(self):
        self.rows()
        with patch.object(b, 'call_openclaw', new=AsyncMock()) as model:
            sent = await b._evaluate_group_turn(None, self.gid, source='patrol')
        self.assertFalse(sent)
        self.assertEqual(model.await_count, 0)
        self.assertEqual(b.group_last_periodic_seq[self.gid], 2)

    async def test_auto_and_patrol_consume_one_opportunity_once(self):
        self.rows('清漪，你觉得这个问题怎么解？')
        b.group_message_cache[self.gid][-1]['at_targets'] = ['9']
        ws = type('Wire', (), {'send': AsyncMock()})()
        async def decision(*args, **kwargs):
            await asyncio.sleep(0)
            return '{"reply":true,"target_seq":2,"reason":"她被问到"}'
        async def send(*args, **kwargs):
            b.record_bot_group_message(self.gid, '先把条件列出来。')
            return True
        with patch.object(b, 'call_openclaw', side_effect=decision), \
             patch.object(b, 'ask_openclaw', new=AsyncMock(return_value='先把条件列出来。')) as answer, \
             patch.object(b, 'send_group_msg', side_effect=send):
            await asyncio.gather(b._evaluate_group_turn(ws,self.gid,'auto'),
                                 b._evaluate_group_turn(ws,self.gid,'patrol'))
        self.assertEqual(answer.await_count, 1)

    async def test_trailing_bot_reply_does_not_consume_a_newer_human_question(self):
        self.rows('这个算法有什么优点？')
        b.group_last_periodic_seq[self.gid] = 1
        b.group_message_cache[self.gid].append(
            {'seq':3,'ts':time.time(),'user_id':'BOT','is_bot':True,
             'target_seq':1,'text':'这是回答前一人的。'})
        with patch.object(b,'call_openclaw',new=AsyncMock(return_value='{"reply":false,"target_seq":2}')) as model:
            await b._evaluate_group_turn(None,self.gid)
        self.assertEqual(model.await_count,1)

    async def test_arriving_new_topic_invalidates_old_candidate(self):
        self.rows('这个算法有什么优点？')
        async def decide(*args, **kwargs):
            b.group_message_cache[self.gid].append(
                {'seq':3,'ts':time.time(),'user_id':'1000000001','text':'换个话题','is_bot':False})
            return '{"reply":true,"target_seq":2,"reason":"问题"}'
        with patch.object(b, 'call_openclaw', side_effect=decide), \
             patch.object(b, 'ask_openclaw', new=AsyncMock()) as answer:
            self.assertFalse(await b._evaluate_group_turn(None, self.gid))
        self.assertEqual(answer.await_count, 0)


if __name__ == '__main__':
    unittest.main()
