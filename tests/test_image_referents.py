from collections import deque
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import qq_openclaw_bridge as b


class ImageReferentTests(unittest.TestCase):
    def setUp(self):
        self.gid = '771177'
        self.addCleanup(b.group_message_cache.pop, self.gid, None)
        self.addCleanup(b.group_image_cache.pop, self.gid, None)

    def test_actual_ingestion_timestamp_is_used_for_freshness(self):
        b.group_message_cache[self.gid] = deque([
            {'ts': time.time()-86400, 'user_id': '1', 'is_bot': False,
             'image_urls': ['old.jpg']}])
        self.assertEqual(b.recent_image_for(group_id=self.gid,within_s=1), '')

    def test_history_image_count_is_not_a_real_image_reference(self):
        b.group_message_cache[self.gid] = deque([
            {'ts': time.time(), 'user_id': '1', 'is_bot': False, 'image_urls': [1]}])
        self.assertEqual(b.recent_image_for(group_id=self.gid), '')

    def test_group_image_priority_is_not_reversed(self):
        now=time.time()
        b.group_image_cache[self.gid] = deque([
            {'ts_epoch':now,'user_id':'1','url':'mine.jpg'},
            {'ts_epoch':now,'user_id':'2','url':'other.jpg'}])
        refs=b.get_recent_group_image_urls(self.gid,user_id='1',max_images=2)
        self.assertEqual(refs[0], 'mine.jpg')

    def test_generic_look_at_it_does_not_inherit_someone_elses_image(self):
        b.group_message_cache[self.gid] = deque([
            {'ts': time.time(), 'user_id': '2', 'is_bot': False,
             'image_urls': ['other.jpg']}])
        self.assertEqual(b.recent_image_for(group_id=self.gid,speaker_id='1'), '')


if __name__ == '__main__':
    unittest.main()
