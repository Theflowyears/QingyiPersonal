import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import qq_openclaw_bridge as b
import history_log


class Wire:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.keys = ['private-user-771122', 'group-771122']
        for k in self.keys:
            b.histories.pop(k, None)
            self.addCleanup(b.histories.pop, k, None)
        b.group_message_cache.pop('771122', None)
        self.addCleanup(b.group_message_cache.pop, '771122', None)
        for name, value in [('QQ_DRY_RUN', False), ('save_histories', lambda: None),
                            ('save_periodic_state', lambda: None)]:
            p = patch.object(b, name, value)
            p.start()
            self.addCleanup(p.stop)
        async def accepted_wire(ws, action, params):
            await ws.send(json.dumps({'action': action, 'params': params}))
            return {'retcode': 0}
        transport = patch.object(b, '_chat_response', side_effect=accepted_wire)
        transport.start()
        self.addCleanup(transport.stop)
        p = patch('self_model.record_action', return_value='')
        p.start()
        self.addCleanup(p.stop)
        p = patch('affection.apply_self_any', return_value=None)
        p.start()
        self.addCleanup(p.stop)
        p = patch('affection.apply_self', return_value=None)
        p.start()
        self.addCleanup(p.stop)

    async def test_proactive_private_message_is_in_the_next_turn_history(self):
        ws = Wire()
        await b.send_private_msg(ws, '771122', '最近忙啥呢？', reason='主动私聊')
        self.assertEqual(list(b.histories['private-user-771122']),
                         [('assistant', '最近忙啥呢？')])
        self.assertEqual(history_log.tail('private-user-771122')[0]['content'], '最近忙啥呢？')

    async def test_identical_reply_is_not_sent_twice_in_group(self):
        ws = Wire()
        answer = '哼，真吃土我可不捞你……白饭倒是给你留一碗。'
        await b.send_group_msg(ws, '771122', '771123', answer, mention=False)
        await b.send_group_msg(ws, '771122', '771123', answer, mention=False)
        self.assertEqual(len(ws.messages), 1)

    async def test_private_repeated_bubble_is_not_sent_again(self):
        ws = Wire()
        await b.send_private_msg(ws, '771122', '那你们可得小心点，别被丧尸吓一跳。')
        await b.send_private_msg(ws, '771122', '那你们可得小心点，别被丧尸吓一跳。')
        self.assertEqual(len(ws.messages), 1)

    async def test_different_fact_is_not_mistaken_for_duplicate(self):
        ws = Wire()
        await b.send_private_msg(ws, '771122', '现在是八十四分。')
        await b.send_private_msg(ws, '771122', '现在是八十五分。')
        self.assertEqual(len(ws.messages), 2)


class ContextQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_program_name_is_preserved_in_lookup_with_followup(self):
        seen = []

        async def model(messages, **kwargs):
            seen.append((messages, kwargs))
            return '后会友期 综艺'

        with patch.object(b, 'call_openclaw', side_effect=model):
            q = await b._her_search_keywords('对呀', '阿甲',
                context='用户：刷到了后会友期的片段\n用户：后会友期，是一个综艺节目')
        self.assertEqual(q, '后会友期 综艺')
        self.assertIn('后会友期', str(seen[0][0]))
        self.assertTrue(seen[0][1].get('internal'))

    def test_new_named_program_is_a_lookup_opportunity_not_only_a_question(self):
        self.assertTrue(b._should_web_search('后会友期，是一个综艺节目'))
        self.assertFalse(b._should_web_search('诶嘿'))
        self.assertFalse(b._should_web_search('我是谁'))


class PrivateBurstTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_person_fragments_form_one_turn(self):
        rows = []

        async def receive(ws, event):
            rows.append(event)

        a = {'user_id': 771122, 'self_id': 9, 'message_id': 1,
             'message': [{'type': 'text', 'data': {'text': '我倒是不怕这个'}}]}
        c = {'user_id': 771122, 'self_id': 9, 'message_id': 2,
             'message': [{'type': 'text', 'data': {'text': '比较怕氛围恐怖'}}]}
        with patch.object(b, '_handle_private_message', side_effect=receive):
            await asyncio.gather(b.handle_private_message(None, a),
                                 b.handle_private_message(None, c))
        self.assertEqual(len(rows), 1)
        _, text, _, _ = b.parse_message(rows[0])
        self.assertIn('我倒是不怕这个', text)
        self.assertIn('比较怕氛围恐怖', text)


class PromptScopeTests(unittest.TestCase):
    def test_image_instructions_do_not_force_self_comparison(self):
        hint = b._img_reaction_hint('[图片]')
        self.assertNotIn('我也这样', hint)
        self.assertNotIn('不许说：「没看懂」', hint)
        self.assertIn('含义', hint)

    def test_image_analysis_does_not_turn_honest_uncertainty_into_a_catchphrase(self):
        text = '这张字太小，我看不清右下角，可以放大那块吗？'
        self.assertEqual(b._scrub_scene_answer(text, 'group'), text)


if __name__ == '__main__':
    unittest.main()
