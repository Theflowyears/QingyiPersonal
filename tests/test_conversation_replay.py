import asyncio
from collections import deque
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b
import conversation_plan as cp


class RephraseTests(unittest.IsolatedAsyncioTestCase):
    async def test_eating_dirt_old_answer_is_rewritten_for_new_request(self):
        old='哼，真吃土我可不捞你……白饭倒是给你留一碗。'
        fresh='外快先别乱接，看看你手头哪项技能能用得上。'
        with patch.object(b,'call_openclaw',new=AsyncMock(return_value=fresh)):
            got=await b._novel_answer(old,'你真得出去给我赚点外快了，你主人没钱养不起你了（）',
                                     [old],[{'role':'user','content':'赚点外快'}])
        self.assertEqual(got,fresh)

    async def test_old_two_bubbles_are_rewritten_as_one_new_turn(self):
        old=['哼，真吃土我可不捞你……','白饭倒是给你留一碗。']
        fresh='先盘点能做什么，再挑靠谱的小活。'
        with patch.object(b,'call_openclaw',new=AsyncMock(return_value=fresh)):
            got=await b._novel_answer(chr(10).join(old),'赚点外快吧',old,[])
        self.assertEqual(got,fresh)

    async def test_second_repetition_is_silence_not_canned_replacement(self):
        old='这呆样，比我还像条咸鱼。'
        with patch.object(b,'call_openclaw',new=AsyncMock(return_value=old)):
            self.assertEqual(await b._novel_answer(old,'[图片]',[old],[]),'[[沉默]]')

    def test_same_image_joke_with_one_word_change_is_still_repetition(self):
        self.assertTrue(cp.repeat_reply('这呆样……跟我晒成鱼干时一个模子',
                                       ['这呆样……跟我晒成鱼干时一个模子呀']))

    def test_negation_is_new_information(self):
        self.assertFalse(cp.repeat_reply('我不怕丧尸，我怕氛围恐怖',
                                        ['我怕丧尸，我不怕氛围恐怖']))


class HistoryCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_recent_exchanges_are_not_summarized_away(self):
        key='private-user-887766'
        before=deque([('user','内容'+str(i)) if i%2==0 else ('assistant','答复'+str(i))
                      for i in range(12)],maxlen=24)
        b.histories[key]=before
        self.addCleanup(b.histories.pop,key,None)
        with patch.object(b,'call_openclaw',new=AsyncMock()) as model:
            self.assertFalse(await b.compact_private_memory(key))
        self.assertEqual(model.await_count,0)
        self.assertEqual(len(before),12)


if __name__=='__main__':
    unittest.main()
