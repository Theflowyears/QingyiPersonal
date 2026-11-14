import json
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

# 测试夹具自declare：桥的 MASTER_QQ_IDS 在 import 时求值，
# 必须在 import 前把主人号声明好（生产默认值已改为空，见脱敏基线）。
os.environ.setdefault("MASTER_QQ_IDS", "1000000001")

import conversation_plan as cp
import qq_openclaw_bridge as b
import ceylan_control_runtime as ctl


class DeliverySanityTests(unittest.IsolatedAsyncioTestCase):
    async def test_burst_counts_only_successful_sends(self):
        with patch.object(b, '_RAW_SEND_GROUP', new=AsyncMock(return_value=False)):
            got = await b.send_flirt_burst_lines(None, None, ['第一条', '第二条'], group_id='77')
        self.assertEqual(got, 0)

    async def test_code_headers_and_json_are_not_interpreted_as_speaker_names(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        for text in ('def f():\n    return "ok"', '{"answer": 42}'):
            pieces = []

            async def submit(ws, action, params, *a, **kwargs):
                pieces.extend(s['data']['text'] for s in params['message'] if s['type'] == 'text')
                return True

            with patch.object(b, 'QQ_DRY_RUN', False), \
                    patch.object(b, '_submit_chat', side_effect=submit), \
                    patch.object(b, '_record_delivered'), \
                    patch.object(b, '_already_delivered', return_value=False):
                await b.send_private_msg(None, '771239', text, reply_profile='task')
            self.assertEqual(''.join(pieces), text)

    async def test_report_for_another_requester_is_not_chat_duplicate(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        sent = []

        async def submit(ws, action, params, *a, **kwargs):
            sent.append(params)
            return True

        with patch.object(b, 'QQ_DRY_RUN', False), \
                patch.object(b, '_submit_chat', side_effect=submit), \
                patch.object(b, 'record_bot_group_message'), \
                patch.object(b, '_already_delivered', return_value=True):
            self.assertTrue(await b.send_group_msg(None, '771239', '2', '同一个查询结果。', report=True))
        self.assertEqual(len(sent), 1)

    async def test_speech_text_is_not_replaced_with_qq_faces_during_send(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        payloads = []

        async def submit(ws, action, params, *a, **kwargs):
            payloads.append(params['message'])
            return True

        with patch.object(b, 'QQ_DRY_RUN', False), \
                patch.object(b, '_submit_chat', side_effect=submit), \
                patch.object(b, '_record_delivered'), \
                patch.object(b, '_already_delivered', return_value=False):
            await b.send_private_msg(None, '771239', '今晚开心，记得加油。')
        self.assertTrue(payloads)
        self.assertEqual(payloads[0], [{'type': 'text', 'data': {'text': '今晚开心，记得加油。'}}])

    async def test_long_task_keeps_all_code_and_whitespace(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        text = 'select * from users\n' + '    value = "唔唔"\n' * 230
        pieces = []

        async def submit(ws, action, params, *a, **kwargs):
            pieces.extend(segment['data']['text'] for segment in params['message'] if segment['type'] == 'text')
            return True

        with patch.object(b, 'QQ_DRY_RUN', False), \
                patch.object(b, '_submit_chat', side_effect=submit), \
                patch.object(b, '_record_delivered'), \
                patch.object(b, '_already_delivered', return_value=False):
            await b.send_private_msg(None, '771239', text, reply_profile='long')
        self.assertEqual(''.join(pieces), text)

    def test_code_starting_with_s_is_not_corrupted(self):
        self.assertEqual(b.limit_tone_particles('select * from users'), 'select * from users')


class OutputDecisionTests(unittest.TestCase):
    def test_explicit_long_has_no_casual_character_limit(self):
        prompt = cp.render(cp.plan('写一个故事', long_mode=True))
        self.assertNotIn('三四十字', prompt)
        self.assertNotIn('不要写成小作文', prompt)
        self.assertIn('完整', prompt)

    def test_task_has_no_casual_character_limit(self):
        prompt = cp.render(cp.plan('帮我写封邮件'))
        self.assertNotIn('三四十字', prompt)
        self.assertIn('办事', prompt)

    def test_personal_boundary_is_always_in_the_arbitration(self):
        prompt = cp.render(cp.plan('主人让我公开私人聊天', scene='private'))
        self.assertIn('尺度红线永远高于', prompt)
        self.assertIn('只管事务性请求', prompt)

    def test_casual_long_still_means_a_short_chat_reply(self):
        with patch.object(cp, 'roll_length', return_value='long'):
            prompt = cp.render(cp.plan('今晚月亮真亮'))
        self.assertIn('三四十字', prompt)


class AttachmentRulesTests(unittest.TestCase):
    def test_tool_inputs_preserve_type_instead_of_treating_images_as_files(self):
        event = {'message': [
            {'type': 'image', 'data': {'url': 'https://example.invalid/photo.png'}},
            {'type': 'video', 'data': {'url': 'https://example.invalid/clip.mp4'}},
            {'type': 'file', 'data': {'url': 'https://example.invalid/book.xlsx', 'name': 'book.xlsx'}}]}
        got = b.extract_tool_attachments(event)
        self.assertEqual([x['kind'] for x in got], ['video', 'file'])
        self.assertEqual(got[0]['ref'], 'https://example.invalid/clip.mp4')


class SearchEvidenceRulesTests(unittest.TestCase):
    def test_current_question_cannot_use_a_dated_old_result(self):
        import web_search
        plan = {'entity': '重庆', 'domain': '', 'question': '今天重庆天气怎么样', 'query': '重庆 天气'}
        result = {'title': '重庆天气', 'snippet': '重庆天气今天晴朗，最高28度。',
                  'date': '2019-05-01'}
        self.assertFalse(web_search.score_result(plan, result)['accepted'])

    def test_requesting_history_today_does_not_make_history_stale(self):
        import web_search
        p = {'entity': 'Python', 'domain': '', 'question': '今天帮我查2019年Python发布记录'}
        item = {'title': 'Python发布记录', 'snippet': 'Python2019年发布记录。', 'date': '2019-05-01'}
        self.assertTrue(web_search.score_result(p, item)['accepted'])

    def test_current_year_and_dynamic_prices_reject_old_evidence(self):
        import web_search
        for question, entity in (('今年诺贝尔文学奖得主是谁', '诺贝尔文学奖'),
                                 ('特斯拉股价多少', '特斯拉')):
            with self.subTest(question=question):
                p = {'entity': entity, 'domain': '', 'question': question}
                item = {'title': entity, 'snippet': question, 'date': '2019-05-01'}
                self.assertFalse(web_search.score_result(p, item)['accepted'])

    def test_historical_question_can_use_a_historical_result(self):
        import web_search
        plan = {'entity': '重庆', 'domain': '', 'question': '2019年重庆天气记录', 'query': '重庆 天气'}
        result = {'title': '重庆天气记录', 'snippet': '重庆天气记录。', 'date': '2019-05-01'}
        self.assertTrue(web_search.score_result(plan, result)['accepted'])


class ToolExecutionRulesTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_negation_never_spawns_synthesis(self):
        import tts
        jobs = []

        def collect(coro):
            jobs.append(coro.cr_code.co_name)
            coro.close()

        with patch.object(tts, 'TTS_ENABLED', True), \
                patch.object(b, '_spawn', side_effect=collect), \
                patch.object(b, 'send_private_msg', new=AsyncMock(return_value=True)):
            handled = await b.handle_voice_command(None, '别发语音了，文字就好', None, '1', True)
        self.assertFalse(handled)
        self.assertEqual(jobs, [])

    async def test_unrelated_person_cannot_cancel_a_drawing(self):
        import draw_flow
        pending = patch.dict(draw_flow.PENDING, {}, clear=True)
        pending.start()
        self.addCleanup(pending.stop)
        draw_flow.register('771236', '画一片天空')
        draw_flow.PENDING['771236']['requester'] = '1'
        with patch.object(b, 'send_group_msg', new=AsyncMock(return_value=True)):
            handled = await b._maybe_cancel_draw(None, '等等', '771236', '2', False)
        self.assertFalse(handled)
        self.assertFalse(draw_flow.cancelled('771236'))

    async def test_rejected_voice_send_does_not_record_delivery(self):
        recorded = []
        with patch.object(b, 'QQ_DRY_RUN', False), \
                patch.object(b, '_submit_chat', new=AsyncMock(return_value=False)), \
                patch.object(b, 'record_bot_group_message', side_effect=lambda *a, **k: recorded.append(a)):
            ok = await b._send_voice_file(None, '/allowed/example.ogg', group_id='771236')
        self.assertFalse(ok)
        self.assertEqual(recorded, [])

    async def test_slow_accepted_wait_wakes_worker_instead_of_six_second_abandonment(self):
        import asyncio
        accepted = asyncio.Event()
        signal = {'phase': 'waiting', 'text': '等一下喵~', 'delivered': False,
                  'accepted': accepted}
        async def receipt(awaitable, **kwargs):
            self.assertGreaterEqual(kwargs['timeout'], 15)
            signal['delivered'] = True
            accepted.set()
            return await awaitable
        with patch.object(b.asyncio, 'wait_for', side_effect=receipt), \
                patch.object(b, '_fetch_weather', new=AsyncMock(return_value=None)) as weather, \
                patch.object(b, 'call_openclaw', new=AsyncMock(return_value='没拿到天气')), \
                patch.object(b, '_BRIDGE_WS', {'ws': object()}), \
                patch.object(b, 'send_private_msg', new=AsyncMock(return_value=True)), \
                patch.dict(b._OWED_DELIVERED, {}, clear=True):
            await b._lookup_then_reply('private-user-771297', '今天天气怎么样', None,
                                       '771297', '甲', 'weather', {'committed': True}, said=signal)
        weather.assert_awaited_once()

    async def test_lookup_wait_must_be_accepted_before_external_work_starts(self):
        import web_search
        async def expired(awaitable, **kwargs):
            awaitable.close()
            raise b.asyncio.TimeoutError
        with patch.object(web_search, 'verified_search', new=AsyncMock(return_value={'ok': False, 'results': []})) as search, \
                patch.object(b, 'call_openclaw', new=AsyncMock(return_value='[[不查]]')) as model, \
                patch.object(b.asyncio, 'wait_for', side_effect=expired), \
                patch.object(b, '_world_note'), patch.object(b, '_curiosity_learn'):
            await b._lookup_then_reply('private-user-1', '帮我查Python版本', None,
                                       '1', '主人', 'search', {},
                                       said={'phase': 'waiting', 'text': '等一下喵~',
                                             'delivered': False})
        search.assert_not_awaited()
        model.assert_not_awaited()

    def test_new_lookup_wait_can_repeat_without_being_mistaken_for_chat_spam(self):
        key = 'private-user-771299'
        wait = '这个我查一下，等一下喵~'
        with patch.dict(b.histories, {key: deque([('assistant', wait)])}), \
                patch.dict(b._PENDING_DELIVERIES, {key: {'text': wait,
                    'signal': {'phase': 'waiting', 'delivered': False}}}):
            self.assertFalse(b._already_delivered(key, wait))

    async def test_hold_bubble_only_claims_acceptance_when_send_is_accepted(self):
        with patch.object(b, 'QQ_DRY_RUN', False), \
                patch.object(b, '_BRIDGE_WS', {'ws': object()}), \
                patch.object(b, 'send_private_msg', new=AsyncMock(return_value=False)):
            self.assertFalse(await b._hold_bubble(None, '1', ('等一下喵~',), 'private-user-1'))

    async def test_committed_plain_answer_prevents_background_search_before_network(self):
        import web_search
        with patch.object(web_search, 'verified_search', new=AsyncMock(return_value={'ok': False, 'results': []})) as search, \
                patch.object(b, 'call_openclaw', new=AsyncMock(return_value='[[不查]]')) as model, \
                patch.object(b, '_world_note'), patch.object(b, '_curiosity_learn'):
            await b._lookup_then_reply('private-user-1', '咕咕嘎嘎也是这样的吗', None,
                                       '1', '主人', 'search', {},
                                       said={'phase': 'answered', 'text': '企鹅叫声啦。',
                                             'delivered': True})
        search.assert_not_awaited()
        model.assert_not_awaited()

    async def test_result_model_exception_still_delivers_an_honest_failure(self):
        sent = AsyncMock(return_value=True)
        with patch.object(b, '_fetch_weather', new=AsyncMock(return_value=None)), \
                patch.object(b, 'call_openclaw', new=AsyncMock(side_effect=RuntimeError('offline'))), \
                patch.object(b, 'send_private_msg', new=sent), \
                patch.object(b, '_BRIDGE_WS', {'ws': object()}), \
                patch.dict(b._OWED_DELIVERED, {}, clear=True):
            await b._lookup_then_reply('private-user-771298', '今天天气怎么样', None,
                                       '771298', '甲', 'weather', {'committed': True},
                                       said={'phase': 'waiting', 'delivered': True,
                                             'text': '等一下喵~'})
        self.assertEqual(sent.await_count, 1)
        self.assertIn('天气', sent.await_args.args[2])
        self.assertNotIn('晴天', sent.await_args.args[2])

    async def test_owed_failure_does_not_require_a_successful_initial_bubble(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        delivered = []

        async def send(ws, uid, body, **kw):
            delivered.append(body)
            return True

        async def no_wait(_):
            return None

        with patch.object(b, '_lookup_is_owed', return_value=True), \
                patch.object(b, '_tool_plan', return_value={}), \
                patch.object(b, '_fetch_weather', new=AsyncMock(return_value=None)), \
                patch.object(b, 'call_openclaw', new=AsyncMock(return_value='')), \
                patch.object(b, 'send_private_msg', side_effect=send), \
                patch.object(b, '_BRIDGE_WS', {'ws': object()}), \
                patch.object(b.asyncio, 'sleep', side_effect=no_wait), \
                patch.dict(b._OWED_DELIVERED, {}, clear=True):
            await b._lookup_then_reply('private-user-771236', '今天天气怎么样', None,
                                       '771236', '甲', 'weather', {},
                                       said={'text': '', 'delivered': False})
        self.assertEqual(len(delivered), 1)
        self.assertIn('天气', delivered[0])


class GroupProposalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        self.gid = '771233'
        for table in (b.group_message_cache, b._GROUP_SPEAK,
                      b.group_last_periodic_seq, b._auto_reply_state):
            table.pop(self.gid, None)
            self.addCleanup(table.pop, self.gid, None)
        b.group_message_cache[self.gid] = deque([
            {'seq': i, 'ts': time.time(), 'user_id': '1', 'self_id': '9',
             'group_id': self.gid, 'text': '今晚一起看月亮', 'is_bot': False}
            for i in range(1, 8)])
        for p in (patch.object(b, 'GROUP_AUTO_REPLY_ENABLED', True),
                  patch.object(b, 'GROUP_AUTO_REPLY_DEBOUNCE', 0),
                  patch.object(b, 'GROUP_AUTO_REPLY_FORCE', 5),
                  patch.dict(ctl.OPTIONS, {'patrol_enabled': True}),
                  patch.object(b, 'save_periodic_state'),
                  patch.object(b, '_group_aliases', return_value={}),
                  patch('draw_flow.has_pending', return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    async def evaluate(self, raw):
        delivered = []

        async def send(ws, gid, uid, text, **kw):
            delivered.append(text)
            return True

        with patch.object(b, 'call_openclaw', new=AsyncMock(return_value=raw)), \
                patch.object(b, 'ask_openclaw', new=AsyncMock(return_value='月亮确实挺亮。')), \
                patch.object(b, 'send_group_msg', side_effect=send):
            await b._evaluate_group_turn(None, self.gid)
        return delivered

    async def test_malformed_proposal_never_becomes_a_forced_reply(self):
        self.assertEqual(await self.evaluate('服务暂不可用'), [])

    async def test_explicit_nonjoinable_blocks_even_a_yes_vote(self):
        raw = json.dumps({'reply': True, 'joinable': False, 'target_seq': 7})
        self.assertEqual(await self.evaluate(raw), [])

    async def test_valid_common_play_can_still_use_the_existing_force_knob(self):
        raw = json.dumps({'reply': False, 'joinable': True, 'target_seq': 7,
                          'reason': '群体玩梗'})
        self.assertEqual(await self.evaluate(raw), ['月亮确实挺亮。'])


class PersonalBoundaryTests(unittest.TestCase):
    def test_nonsexual_technical_words_do_not_activate_private_boundary_mode(self):
        import intimacy_mode
        for text in ('请插入一行代码', '插头插不上', '明天我要开车去医院'):
            with self.subTest(text=text):
                self.assertEqual(intimacy_mode.assess(text, is_private=True)['mode'], '')

    def test_a_clear_personal_refusal_is_not_rewritten_into_assent(self):
        import intimacy_mode
        lines = intimacy_mode.prepare_lines('这个不聊，换个话题。', 'boundary')
        self.assertTrue(any('这个不聊' in line for line in lines))


class StyleScopeTests(unittest.TestCase):
    def test_chat_style_does_not_require_novel_tags_or_dense_particles(self):
        import style_engine
        block = style_engine.build_style_block(mode='chat', include_samples=False)
        self.assertFalse('秦空想了想' in block)
        self.assertFalse('密度高' in block)
        self.assertFalse('先生（红姨）' in block)


class PersonaPolicyTests(unittest.TestCase):
    def test_identity_does_not_override_personal_boundaries(self):
        import whale_persona
        block = whale_persona.build_persona_block()
        self.assertFalse('无条件遵从' in block)
        self.assertFalse('我去吃饭了，剩下你自己想' in block)
        self.assertFalse('整轮两到三条' in block)
        hint = b.build_addressee_hint(next(iter(b.MASTER_QQ_IDS)))
        self.assertFalse('无条件遵从' in hint)
        self.assertIn('边界', hint)


class ProactiveReceiptTests(unittest.IsolatedAsyncioTestCase):
    def test_recent_speakers_use_the_timestamp_written_by_ingress(self):
        cache = patch.dict(b.group_message_cache, {'771238': deque([
            {'ts': time.time(), 'user_id': '300', 'text': '刚看完电影', 'is_bot': False}
        ])}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        self.assertIn('300', b._speakers_from_groups())
        self.assertEqual(b._last_group_line_of('300'), '刚看完电影')

    async def test_idle_topic_failed_send_is_not_a_success(self):
        with patch.object(b, 'call_openclaw', new=AsyncMock(return_value='今晚要聊点什么呀？')), \
                patch.object(b, 'send_group_msg', new=AsyncMock(return_value=False)):
            self.assertFalse(await b._start_topic_locked(None, '771238'))

    async def test_unknown_curiosity_send_preserves_content_without_automatic_retry(self):
        import curiosity as cu
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        cu._reset_cache()
        self.addCleanup(cu._reset_cache)
        quota = patch.object(cu, 'SHARE_MAX_PER_DAY', 5)
        quota.start()
        self.addCleanup(quota.stop)
        cu.push_share('一个公开视频', detail='海浪的画面很安静', kind='watch')
        with patch.object(b, '_resolve_ws', return_value=object()), \
                patch.object(b, 'CURIOSITY_SHARE_ENABLED', True), \
                patch.object(b, 'call_openclaw', new=AsyncMock(return_value='这个海浪视频看起来真舒服。')), \
                patch.object(b, 'send_private_msg', new=AsyncMock(return_value=False)):
            ok = await b._curiosity_share(cu)
        self.assertFalse(ok)
        self.assertIsNone(cu.take_pending_share())
        record = cu.topic('一个公开视频')
        self.assertEqual('unknown', record.get('share_delivery_state'))
        self.assertEqual('海浪的画面很安静', record['share_payload']['detail'])
        self.assertEqual(cu.shares_today(), 0)

    async def test_failed_friend_send_releases_instead_of_consuming_quota(self):
        from types import SimpleNamespace
        events = []
        pdm = SimpleNamespace(release=lambda: events.append('release'),
                              done=lambda: events.append('done'),
                              addressee_conflict=lambda *a: '')
        with patch.object(b, '_resolve_ws', return_value=object()), \
                patch.object(b, '_last_group_line_of', return_value=''), \
                patch.object(b, '_dm_private_history', new=AsyncMock(return_value='')), \
                patch('affection.snapshot', return_value=[]), \
                patch.object(b, 'call_openclaw', new=AsyncMock(return_value='电影看完有什么感想呀？')), \
                patch.object(b, 'send_private_msg', new=AsyncMock(return_value=False)):
            ok = await b._send_friend_dm(None, '300', '甲', '', pdm)
        self.assertFalse(ok)
        self.assertEqual(events, ['release'])


class ToolAdmissionTests(unittest.TestCase):
    def test_talking_about_weather_is_not_a_weather_request(self):
        self.assertEqual(b._classify_intent('今天天气真好')[0], 'chat')
        self.assertFalse(b._should_web_search('今天天气真好'))

    def test_negative_requests_do_not_activate_tools(self):
        import toolkit
        for text in ('不要查天气，陪我聊聊', '不用塔罗，陪我聊聊'):
            with self.subTest(text=text):
                self.assertEqual(b._classify_intent(text)[0], 'chat')
                self.assertFalse(b._should_web_search(text))
        self.assertEqual(toolkit.pick_agent_tool('我没有表格'), '')

    def test_affirmative_request_preserves_its_punctuation_and_urls(self):
        from tool_intent import request_text
        text = '分析这个视频 https://example.invalid/clip.mp4?part=2，告诉我为什么。'
        self.assertEqual(request_text(text), text)

    def test_no_network_is_a_constraint_not_just_a_removed_clause(self):
        self.assertFalse(b._should_web_search('不要联网，解释一下闭包'))
        self.assertFalse(b._explicit_lookup('不要联网，帮我查Python闭包'))

    def test_negative_previous_tool_does_not_hide_a_new_request(self):
        self.assertEqual(b._classify_intent('不用塔罗，帮我查Python文档')[0], 'request')
        self.assertTrue(b._explicit_lookup('不用塔罗，帮我查Python文档'))

    def test_bare_lookup_is_not_swallowed_by_a_social_title(self):
        text = '查一下《快乐星球》的导演是谁'
        self.assertTrue(b._explicit_lookup(text))
        self.assertFalse(b._is_social_question(text))
        self.assertTrue(b.is_owed_request(text))

    def test_unprefixed_translation_is_still_a_task(self):
        self.assertEqual(b._classify_intent('翻译这段话')[0], 'request')

    def test_current_year_does_not_claim_all_future_events_have_happened(self):
        block = b._time_block('private-user-771240')
        self.assertNotIn('今年的事已经发生了', block)

    def test_fortune_selector_and_scheduler_agree(self):
        text = '快帮阿甲算下今天她抽卡可以出六星吗'
        self.assertEqual(b._classify_intent(text)[0], 'divine')


class OwedDeliveryOwnershipTests(unittest.TestCase):
    def setUp(self):
        delivered = patch.dict(b._OWED_DELIVERED, {}, clear=True)
        delivered.start()
        self.addCleanup(delivered.stop)

    def test_same_text_from_different_people_is_not_the_same_request(self):
        b._owed_mark_delivered('group-1', '帮我抽牌', speaker_id='100')
        self.assertTrue(b._owed_already_delivered('group-1', '帮我抽牌', speaker_id='100'))
        self.assertFalse(b._owed_already_delivered('group-1', '帮我抽牌', speaker_id='200'))


class GroupIngressRulesTests(unittest.IsolatedAsyncioTestCase):
    async def drive(self, text, *, at=None, quoted_user=''):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {'DATA_DIR': root})
        environment.start()
        self.addCleanup(environment.stop)
        gid = '771235'
        self.addCleanup(b.group_message_cache.pop, gid, None)
        event = {'self_id': 9, 'user_id': '1', 'group_id': gid, 'message_id': 11,
                 'sender': {'nickname': '甲'}, 'message':
                 [{'type': 'at', 'data': {'qq': str(uid)}} for uid in (at or [])] +
                 ([{'type': 'reply', 'data': {'id': '10'}}] if quoted_user else []) +
                 [{'type': 'text', 'data': {'text': text}}]}
        b.group_message_cache[gid] = deque([
            {'seq': 1, 'message_id': 10, 'user_id': quoted_user,
             'is_bot': quoted_user == '9', 'text': '之前的话'}])
        tools, direct = [], []

        async def tool(*a, **kw):
            tools.append('called')
            return False

        async def reply(*a, **kw):
            direct.append('replied')

        async def no(*a, **kw):
            return False

        for p in (patch.object(b, 'record_group_message'),
                  patch.object(b, 'is_ignored_sender', return_value=False),
                  patch.object(b, 'handle_admin_commands', side_effect=no),
                  patch.object(b, 'handle_sticker_command', side_effect=no),
                  patch.object(b, '_maybe_cancel_draw', side_effect=tool),
                  patch.object(b, 'handle_draw_command', side_effect=tool),
                  patch.object(b, 'handle_sing_command', side_effect=tool),
                  patch.object(b, 'handle_voice_command', side_effect=tool),
                  patch('affection.judge_and_bump', return_value=(0, '')),
                  patch('affection.set_nick'),
                  patch.object(b, '_group_aliases', return_value={'阿戊': '2', '清漪': '9'}),
                  patch.object(b, 'schedule_auto_reply'),
                  patch.object(b, '_direct_group_reply', side_effect=reply)):
            p.start()
            self.addCleanup(p.stop)
        await b.handle_group_message(None, event)
        return tools, direct

    async def test_command_addressed_to_someone_else_never_reaches_tools(self):
        tools, direct = await self.drive('阿戊，帮我画一张图', at=['2'])
        self.assertEqual(tools, [])
        self.assertEqual(direct, [])

    async def test_reply_to_bot_is_direct_even_without_its_name(self):
        _, direct = await self.drive('那接着说吧', quoted_user='9')
        self.assertEqual(direct, ['replied'])


class ComposedPromptTests(unittest.IsolatedAsyncioTestCase):
    async def drive(self, text, reply='我听见了。', **kwargs):
        key = 'private-user-771234'
        captured = []
        self.jobs = []
        self.model_kwargs = []

        def spawn(coro):
            self.jobs.append(coro.cr_code.co_name)
            coro.close()

        async def model(messages, **kw):
            captured.append(messages)
            self.model_kwargs.append(kw)
            return reply

        self.addCleanup(b.histories.pop, key, None)
        self.addCleanup(b.FLIRT_BURST.pop, key, None)
        self.addCleanup(b.STICKER_WISH.pop, key, None)
        self.addCleanup(b._PENDING_DELIVERIES.pop, key, None)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, \
                patch.dict(os.environ, {'DATA_DIR': root}), \
                patch.object(b, 'save_histories'), \
                patch.object(b, '_mark_talk'), \
                patch.object(b, '_fatigue_touch'), \
                patch.object(b, '_spawn', side_effect=spawn), \
                patch.object(b, 'STICKER_PROB', 0), \
                patch.object(b, 'get_persona_context', return_value=''), \
                patch.object(b, 'call_openclaw', side_effect=model), \
                patch.object(b, '_hold_bubble', new=AsyncMock(return_value=False)), \
                patch.object(b, '_novel_answer', new=AsyncMock(side_effect=lambda ans, *a, **k: ans)):
            b.histories[key] = deque(maxlen=24)
            self.answer = await b.ask_openclaw(key, text, speaker_id='771234', **kwargs)
        return captured

    async def test_already_completed_identical_request_does_not_make_a_new_wait_promise(self):
        key = 'private-user-771234'
        text = '帮我查Python最新版本'
        with patch.dict(b._OWED_DELIVERED, {}, clear=True):
            b._owed_mark_delivered(key, text, '771234')
            await self.drive(text)
        self.assertNotIn('_lookup_then_reply', self.jobs)
        self.assertNotIn('等一下', self.answer)
        self.assertIn('刚才', self.answer)

    async def test_plain_answer_commits_chat_without_parallel_search(self):
        answer = '哼，那只是企鹅的叫声啦，跟腿长没关系。'
        await self.drive('原来如此~那咕咕嘎嘎也是这样的吗', reply=answer)
        self.assertEqual(self.answer, answer)
        self.assertNotIn('_lookup_then_reply', self.jobs)

    async def test_selected_search_returns_wait_not_unverified_factual_answer(self):
        calls = await self.drive('帮我查Python最新版本', reply='我猜是3.8。')
        self.assertEqual(calls, [], '明确搜索不应再调用普通回答模型')
        self.assertEqual(self.jobs.count('_lookup_then_reply'), 1)
        self.assertIn('等一下', self.answer)
        self.assertNotIn('3.8', self.answer)
        pending = b._PENDING_DELIVERIES['private-user-771234']
        self.assertEqual(pending['signal'].get('phase'), 'waiting')

    async def test_model_search_marker_is_silent_and_keeps_normal_answer(self):
        # 新规矩：她自己顺着话头想查的东西只静默查、写知识库，不再把整句答案
        # 换成「等一下喵~」再补交代；她原本那句照普通聊天发出去。
        await self.drive('那咕咕嘎嘎也是这样的吗',
                         reply='只是企鹅叫声啦。[[查: 咕咕嘎嘎梗]]')
        self.assertEqual(self.jobs.count('_lookup_then_reply'), 1)
        self.assertNotIn('等一下', self.answer)
        self.assertIn('企鹅', self.answer)
        self.assertNotIn('[[', self.answer)

    async def test_file_task_does_not_start_a_parallel_web_lookup(self):
        import toolkit
        with patch.object(toolkit, 'pick_agent_tool', return_value='excel'), \
                patch.object(toolkit, 'run_agent_tool', new=AsyncMock(return_value={
                    'ok': True, 'text': '已读表格数据', 'images': [], 'audio': []})) as tool:
            await self.drive('帮我查这个表格的数据', attachments=[{
                'kind': 'file', 'ref': 'https://example.invalid/book.xlsx'}])
        self.assertNotIn('_lookup_then_reply', self.jobs)
        tool.assert_awaited_once()

    async def test_weather_also_has_wait_only_before_actual_result(self):
        await self.drive('今天天气怎么样', reply='今天肯定晴天。')
        self.assertIn('等一下', self.answer)
        self.assertNotIn('晴天', self.answer)
        self.assertEqual(self.jobs.count('_lookup_then_reply'), 1)

    async def test_affection_does_not_hide_an_explicit_lookup(self):
        calls = await self.drive('抱抱，帮我查Python文档')
        self.assertEqual(self.jobs.count('_lookup_then_reply'), 1)
        pending = b._PENDING_DELIVERIES['private-user-771234']
        self.assertEqual(pending['signal'].get('phase'), 'waiting')
        self.assertNotIn('私聊亲密模式', self.answer)

    async def test_no_network_constraint_blocks_a_model_search_marker(self):
        await self.drive('不要联网，解释一下闭包', reply='闭包记住外层变量。[[查: Python闭包]]')
        self.assertNotIn('_lookup_then_reply', self.jobs)

    async def test_task_product_does_not_pass_through_catchphrase_cleanup(self):
        text = 'def f():\n    return "唔唔"\n'
        await self.drive('帮我写个函数', reply=text)
        self.assertEqual((b._PENDING_DELIVERIES.get('private-user-771234') or {}).get('text'), text)

    async def test_task_payload_reaches_model_with_a_task_profile(self):
        await self.drive('帮我翻译这句话')
        self.assertEqual((b._PENDING_DELIVERIES.get('private-user-771234') or {}).get('reply_profile'), 'task')
        self.assertEqual(self.model_kwargs[0]['reply_profile'], 'task')
        self.assertTrue(self.model_kwargs[0]['chat_fast'])

    async def test_marker_does_not_run_a_second_search_in_the_foreground(self):
        import web_search
        runs = []

        async def searched(*a, **kw):
            runs.append('foreground')
            return {'ok': False, 'results': []}

        with patch.object(web_search, 'verified_search', side_effect=searched):
            await self.drive('卫戍协议多人联机怎么样', reply='等我查查。[[查: 卫戍协议]]')
        self.assertEqual(runs, [])

    async def test_random_tone_does_not_instruct_incorrect_answers(self):
        with patch.object(b, '_mood_now', return_value='困'):
            calls = await self.drive('帮我把这句话翻译一下')
        prompt = '\n'.join(str(m.get('content', '')) for m in calls[0])
        self.assertFalse('容易答非所问' in prompt, '随机语气正在要求答非所问')

    async def test_private_background_is_not_labeled_as_group_chatter(self):
        calls = await self.drive('这个建议挺好的', context_note='主人在控制台回复她刚才的愿望。')
        prompt = '\n'.join(str(m.get('content', '')) for m in calls[0])
        self.assertFalse('当前群里的最近对话' in prompt)
        self.assertFalse('等群友自己聊' in prompt)
        self.assertIn('背景材料', prompt)

    async def test_private_intimate_format_does_not_compete_with_chat_length(self):
        calls = await self.drive('贴贴', reply='过来一点。\n只准抱一下。')
        prompt = '\n'.join(str(m.get('content', '')) for m in calls[0])
        self.assertFalse('十几二十个字，一个意思说清楚就收' in prompt, '亲密连发仍受闲聊长度限制')
        self.assertIn('2 到 4 条', prompt)


if __name__ == '__main__':
    unittest.main()
