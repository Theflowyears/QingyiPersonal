import asyncio
from collections import deque
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class NovelDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_user_message_prevents_repeating_proactive_question(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'DATA_DIR':tmp}):
            key='private-user-771166'
            b.histories[key]=deque([('assistant','最近忙啥呢？'),('user','刷抖音')],maxlen=24)
            self.addCleanup(b.histories.pop,key,None)
            with patch.object(b,'call_openclaw',new=AsyncMock(return_value='刷到什么有意思的了？')):
                result=await b._novel_answer('最近忙啥呢？','刷抖音',
                                           ['最近忙啥呢？'],[{'role':'user','content':'刷抖音'}])
            self.assertEqual(result,'刷到什么有意思的了？')

    def test_named_program_clip_is_a_lookup_candidate(self):
        self.assertTrue(b._should_web_search('刷到了后会友期的片段'))


if __name__=='__main__':unittest.main()
