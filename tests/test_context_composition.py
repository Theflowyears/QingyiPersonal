from collections import deque
from pathlib import Path
import tempfile
import sys
import os
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bot'))
import qq_openclaw_bridge as b


class RecallQueryTests(unittest.TestCase):
    def test_current_inbox_message_does_not_take_up_recall_budget_twice(self):
        history = [("user", "昨天一起玩卫戍协议"), ("assistant", "记得你选的阵容"),
                   ("user", "今晚还玩吗")]
        self.assertEqual(b.build_recall_query(history, "今晚还玩吗"),
                         "今晚还玩吗 昨天一起玩卫戍协议 记得你选的阵容")

    def test_local_affection_query_does_not_retrieve_previous_search_topic(self):
        history = [("user", "帮我查卫戍协议"), ("assistant", "还没查到能对上的东西")]
        self.assertEqual(b.build_recall_query(history, "查询好感度宝宝"), "查询好感度宝宝")


class CompactionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_turns_arriving_during_compaction_remain_verbatim(self):
        key='private-user-770022'
        b.histories[key]=deque([('user','old'+str(i)) for i in range(20)],maxlen=40)
        self.addCleanup(b.histories.pop,key,None)
        self.addCleanup(b.private_memory_summary.pop,key,None)
        async def summary(*args,**kwargs):
            b.histories[key].extend([('user','新到的一句'),('assistant','这句的新答复')])
            return '已归纳旧段，不含新句。'
        with patch.object(b,'call_openclaw',side_effect=summary), \
             patch.object(b,'save_histories'),patch.object(b,'save_private_memory'):
            await b.compact_private_memory(key,force=True)
        remaining=list(b.histories[key])
        self.assertIn(('user','old4'),remaining)
        self.assertIn(('user','新到的一句'),remaining)
        self.assertIn(('assistant','这句的新答复'),remaining)
        self.assertEqual(len(remaining),18)


if __name__=='__main__':unittest.main()
