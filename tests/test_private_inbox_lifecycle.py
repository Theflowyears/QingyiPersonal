import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class InboxLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_first_turn_does_not_discard_next_queued_turn(self):
        uid='774433'
        a={'user_id':uid,'message_id':1,'message':[{'type':'text','data':{'text':'先说一句'}}]}
        c={'user_id':uid,'message_id':2,'message':[{'type':'text','data':{'text':'再说一句'}}]}
        started=asyncio.Event(); release=asyncio.Event();done=[]
        async def receive(ws,event):
            done.append(event['message_id'])
            if event['message_id']==1:
                started.set();await release.wait();raise RuntimeError('first failed')
        with patch.object(b,'_handle_private_message',side_effect=receive):
            first=asyncio.create_task(b.handle_private_message(None,a))
            await started.wait()
            second=asyncio.create_task(b.handle_private_message(None,c))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first,second)
        self.assertEqual(done,[1,2])
        self.assertNotIn(uid,b._PRIVATE_INBOX)

    async def test_connection_cancellation_cleans_the_drain(self):
        uid='774434'
        event={'user_id':uid,'message_id':1,'message':[{'type':'text','data':{'text':'测试'}}]}
        started=asyncio.Event(); canceled=asyncio.Event()
        async def receive(ws,event):
            started.set()
            try:await asyncio.Event().wait()
            finally:canceled.set()
        with patch.object(b,'_handle_private_message',side_effect=receive):
            task=asyncio.create_task(b.handle_private_message(None,event))
            await started.wait();task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(canceled.is_set())
        self.assertNotIn(uid,b._PRIVATE_INBOX)


if __name__=='__main__':unittest.main()
