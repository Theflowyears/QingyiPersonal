import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
sys.path.insert(0, str(BOT))
import qq_openclaw_bridge as b
import conversation_plan as cp

ECHO = '[16:45:51] 阿丙: 把你从卫上顶下来嘛'
ORIGINAL = '把你从卫上顶下来嘛'


class TranscriptEchoTests(unittest.TestCase):
    def test_timestamp_and_speaker_prefix_is_stripped(self):
        text, changed = b._strip_transcript_meta(ECHO, {'阿丙'})
        self.assertTrue(changed)
        self.assertEqual(text, ORIGINAL)

    def test_unknown_name_prefix_is_not_stripped(self):
        text, changed = b._strip_transcript_meta('注意：明天记得带伞', {'阿丙'})
        self.assertFalse(changed)
        self.assertEqual(text, '注意：明天记得带伞')

    def test_echo_of_the_same_message_is_detected(self):
        echo, cleaned, why = b._transcript_echo(ECHO, ORIGINAL, [ORIGINAL], [], {'阿丙'})
        self.assertTrue(echo, why)
        self.assertEqual(cleaned, ORIGINAL)

    def test_a_real_reply_is_not_echo(self):
        echo, cleaned, _why = b._transcript_echo('怎么，想抢我的位子？', ORIGINAL, [ORIGINAL], [], {'阿丙'})
        self.assertFalse(echo)
        self.assertEqual(cleaned, '怎么，想抢我的位子？')

    def test_timestamp_only_formatting_is_cleaned_not_blocked(self):
        echo, cleaned, _why = b._transcript_echo('[16:45:51] 这也能顶？', ORIGINAL, [ORIGINAL], [], {'阿丙'})
        self.assertFalse(echo)
        self.assertEqual(cleaned, '这也能顶？')


class ForceGuardTests(unittest.TestCase):
    def test_arbiter_says_not_joinable_blocks_force(self):
        proposal = {'reply': False, 'joinable': False, 'reason': '这是两人对话，不应插话'}
        candidate = {'reason': 'candidate', 'rows': [{'user_id': '1', 'text': 'x'}]}
        self.assertFalse(b._join_allowed(proposal, candidate, None, '999', {}))

    def test_other_recipient_row_blocks_force(self):
        row = {'user_id': '1', 'text': '推完你的号推我的', 'reply_to_user': '2'}
        candidate = {'reason': 'candidate', 'rows': [row]}
        self.assertFalse(b._join_allowed({'reply': True}, candidate, row, '999', {}))

    def test_normal_banter_still_allows_force(self):
        row = {'user_id': '1', 'text': '穿越回卫戍协议开服前了'}
        candidate = {'reason': 'candidate', 'rows': [row]}
        self.assertTrue(b._join_allowed({'reply': False, 'joinable': True}, candidate, row, '999', {}))


class SendGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='reply_guard_')
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root, 'PERSONA_DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        self.gid = '771177'
        b.group_message_cache[self.gid] = deque(maxlen=50)
        b.group_message_cache[self.gid].append({'seq': 1, 'user_id': '1', 'nickname': '阿丙',
                                                'text': ORIGINAL, 'is_bot': False, 'ts': 1})
        self.addCleanup(b.group_message_cache.pop, self.gid, None)

    async def test_send_drops_timestamped_echo_instead_of_posting_it(self):
        submit = AsyncMock(return_value=True)
        with patch.object(b, '_submit_chat', new=submit), \
                patch.object(b, 'record_bot_group_message'):
            ok = await b.send_group_msg(None, self.gid, '1', ECHO, mention=False)
        self.assertFalse(ok)
        submit.assert_not_awaited()

    async def test_send_strips_metadata_but_keeps_real_reply(self):
        submit = AsyncMock(return_value=True)
        with patch.object(b, 'QQ_DRY_RUN', False), \
                patch.object(b, '_submit_chat', new=submit), \
                patch.object(b, 'record_bot_group_message'):
            ok = await b.send_group_msg(None, self.gid, '1', '[16:45:51] 这也能顶？', mention=False)
        self.assertTrue(ok)
        sent = submit.await_args.args[2]['message']
        text = ''.join(seg.get('data', {}).get('text', '') for seg in sent if seg.get('type') == 'text')
        self.assertEqual(text, '这也能顶？')


if __name__ == '__main__':
    unittest.main()
