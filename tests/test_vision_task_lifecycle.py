import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b
import vision_notes as vn


class VisionTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_analysis_is_removed_even_after_its_waiter_is_cancelled(self):
        started=asyncio.Event()
        release=asyncio.Event()
        async def describe(*args,**kwargs):
            started.set()
            await release.wait()
            return '困惑'
        with patch.object(vn,'note_for',return_value=None), \
             patch.object(b,'_describe_image',side_effect=describe), \
             patch.object(b,'_vision_budget_ok',return_value=lambda:True), \
             patch.object(b,'_VISION_LIMIT',asyncio.Semaphore(2)), \
             patch.object(b,'_VISION_INFLIGHT',{}):
            outer=asyncio.create_task(b._note_images_async(['one.gif'],'group:test'))
            await started.wait()
            inner=list(b._VISION_INFLIGHT.values())[0]
            outer.cancel()
            await asyncio.gather(outer,return_exceptions=True)
            release.set()
            await inner
            await asyncio.sleep(0)
            self.assertEqual(b._VISION_INFLIGHT,{})


if __name__=='__main__':unittest.main()
