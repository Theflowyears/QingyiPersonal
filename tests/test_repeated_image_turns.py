from collections import deque
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class RepeatedImageTurns(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_sticker_without_new_text_does_not_trigger_another_caption(self):
        key='private-user-776655'
        refs=['64fd7400fc77ccf0f77ab0e91c02a968.jpg']
        self.addCleanup(b.histories.pop,key,None)
        b.histories[key]=deque([('user','[图片]'),('assistant','卡住了？')],maxlen=24)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, patch.dict(os.environ,{'DATA_DIR':root}), \
             patch.object(b,'save_histories'), \
             patch.object(b,'call_mimo_vision',new=AsyncMock(return_value='这眼神，跟我刚睡醒似的。')), \
             patch.object(b,'image_url_to_openai_ref',new=AsyncMock(return_value='data:image/gif;base64,eA==')), \
             patch.object(b,'_spawn',side_effect=lambda coro:coro.close()):
            b._PENDING_DELIVERIES[key]={'text':'卡住了？','signal':{},'images':refs}
            b._record_delivered(key,'卡住了？')
            result=await b.ask_openclaw(key,'[图片]',image_urls=refs,
                                       speaker_id='776655',speaker_nick='阿甲')
        self.assertEqual(result,'[[沉默]]')

    async def test_alternating_two_seen_stickers_does_not_reset_the_repeat_guard(self):
        key='private-user-776656'
        self.addCleanup(b.histories.pop,key,None)
        self.addCleanup(b._DELIVERED_IMAGES.pop,key,None)
        self.addCleanup(b._PENDING_DELIVERIES.pop,key,None)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, patch.dict(os.environ,{'DATA_DIR':root}), \
             patch.object(b,'save_histories'), \
             patch.object(b,'call_mimo_vision',new=AsyncMock(return_value='这呆样，我也这样。')), \
             patch.object(b,'image_url_to_openai_ref',new=AsyncMock(return_value='data:image/gif;base64,eA==')), \
             patch.object(b,'_spawn',side_effect=lambda coro:coro.close()):
            for ref,answer in [('a.gif','卡住了？'),('b.gif','这张也挺呆。')]:
                b._PENDING_DELIVERIES[key]={'text':answer,'signal':{},'images':[ref]}
                b._record_delivered(key,answer)
            result=await b.ask_openclaw(key,'[图片]',image_urls=['a.gif'],speaker_id='776656')
        self.assertEqual(result,'[[沉默]]')


if __name__=='__main__':unittest.main()
