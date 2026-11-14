import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))
import self_model as sm


class ResultEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shadow_inputs_')
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name,
                                     'PERSONA_DATA_DIR': str(Path(self.tmp.name) / 'persona')})
        env.start()
        self.addCleanup(env.stop)

    def rows(self, kind):
        with closing(sm._db()) as c:
            return [dict(r) for r in c.execute('SELECT * FROM event WHERE kind=?', (kind,))]

    def test_receipt_creates_one_linked_event_without_claiming_human_response(self):
        aid = sm.record_action('dm', target_qq='123', text='你好', reason='分享')
        self.assertTrue(sm.mark_action(aid, 'accepted', 'ok'))
        self.assertTrue(sm.mark_action(aid, 'accepted', 'ok'))
        rows = self.rows('action_result')
        self.assertEqual(len(rows), 1)
        p = json.loads(rows[0]['payload'])
        self.assertEqual(p['action_id'], aid)
        self.assertEqual(p['state'], 'accepted')
        self.assertNotIn('read', p)
        self.assertNotIn('replied', p)
        with closing(sm._db()) as c:
            self.assertEqual(c.execute('SELECT reason FROM action WHERE id=?', (aid,)).fetchone()[0], '分享')

    def test_default_occurrence_time_never_follows_its_recording_time(self):
        eid = sm.record_tool_result('search', 'q-default', True)
        event = sm.get_event(eid)
        self.assertLessEqual(event['ts'], event['created'],
                             '两次取时把真实结果写成了未来事件')

    def test_tool_observation_preserves_explicit_failure_and_scope(self):
        self.assertTrue(callable(getattr(sm, 'record_tool_result', None)), '工具结果还没有事件入口')
        eid = sm.record_tool_result('search', 'q1', False, session='private:123',
                                    visibility='private', reason='timeout', ts=100)
        e = sm.get_event(eid)
        self.assertEqual(e['visibility'], 'private')
        self.assertIs(json.loads(e['payload'])['ok'], False)
        self.assertEqual(json.loads(e['payload'])['tool'], 'search')

    def test_same_execution_does_not_become_new_evidence(self):
        a = sm.record_tool_result('vision', 'image1', True, ts=100, run_id='run1',
                                  session='private:123')
        b = sm.record_tool_result('vision', 'image1', True, ts=101, run_id='run1',
                                  session='private:123')
        self.assertEqual(a, b)
        self.assertEqual(len(self.rows('tool_result')), 1)

    def test_new_execution_on_same_resource_is_recorded_separately(self):
        a = sm.record_tool_result('vision', 'image1', True, ts=100, run_id='run1',
                                  session='private:123')
        b = sm.record_tool_result('vision', 'image1', True, ts=101, run_id='run2',
                                  session='private:456')
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.rows('tool_result')), 2)
        self.assertEqual(sm.get_event(a)['session'], 'private:123')
        self.assertEqual(sm.get_event(b)['session'], 'private:456')

    def test_search_cache_reuses_execution_identity_but_refresh_gets_new_one(self):
        import asyncio
        import web_search as search
        from unittest.mock import AsyncMock
        async def run():
            search._cache.clear()
            with patch.object(search, 'SEARCH_ENABLED', True), patch.object(search, 'BOCHA_API_KEY', ''), \
                 patch.object(search, '_search_bing', new=AsyncMock(return_value={
                     'ok': True, 'results': [{'title': 't', 'snippet': 's'}], 'source': 'bing'})):
                a = await search.search('test1', session='group:123', visibility='group')
                b = await search.search('test1', session='private:456')
                search._cache.clear()
                c = await search.search('test1', session='private:456')
            search._cache.clear()
            return a, b, c
        a, b, c = asyncio.run(run())
        self.assertEqual(a['run_id'], b['run_id'])
        self.assertNotEqual(a['run_id'], c['run_id'])
        rows = self.rows('tool_result')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['session'], 'group:123')
        self.assertEqual(rows[0]['visibility'], 'group')

    def test_group_event_marks_only_explicit_self_addressing(self):
        import qq_openclaw_bridge as b
        gid = 'phase2_test_group'
        old_cache = b.group_message_cache.get(gid)
        old_seq = b.group_message_seq.get(gid)
        self.addCleanup(lambda: b.group_message_cache.pop(gid, None))
        self.addCleanup(lambda: b.group_message_seq.pop(gid, None))
        ev = {'group_id': gid, 'user_id': 123, 'self_id': 999,
              'message_id': 1, 'time': 100, 'sender': {'card': '测试'},
              'message': [{'type': 'at', 'data': {'qq': '999'}},
                          {'type': 'text', 'data': {'text': '谢谢你'}}]}
        with patch('member_memory.note'), patch.object(b, 'note_group_human_activity'):
            b.record_group_message(ev, '谢谢你', [])
        p = json.loads(self.rows('group_message')[0]['payload'])
        self.assertIs(p.get('addressed_to_self'), True)
        self.assertEqual(self.rows('group_message')[0]['ts'], 100)

    def test_reply_to_her_and_direct_name_are_attributed_without_claiming_quotes_as_feedback(self):
        import qq_openclaw_bridge as b
        gid = 'shadow-attribution-group'
        self.addCleanup(b.group_message_cache.pop, gid, None)
        self.addCleanup(b.group_message_seq.pop, gid, None)
        b.group_message_cache[gid].append({'message_id': 51, 'is_bot': True, 'user_id': 'BOT'})
        event = {'group_id': gid, 'user_id': 123, 'self_id': 999, 'message_id': 52,
                 'time': 100, 'sender': {'card': 'fixture'},
                 'message': [{'type': 'reply', 'data': {'id': '51'}},
                             {'type': 'text', 'data': {'text': '谢谢你'}}]}
        with patch('member_memory.note'), patch.object(b, 'note_group_human_activity'):
            b.record_group_message(event, '谢谢你', [])
        payload = json.loads(self.rows('group_message')[0]['payload'])
        self.assertTrue(payload['addressed_to_self'])
        self.assertEqual(payload['address_basis'], 'reply_to_self')
        event['message_id'] = 53
        event['message'] = [{'type': 'text', 'data': {'text': '清漪，你刚才说错了'}}]
        with patch.object(b, 'BOT_ALL_NAMES', ['清漪']), patch('member_memory.note'), patch.object(b, 'note_group_human_activity'):
            b.record_group_message(event, '清漪，你刚才说错了', [])
        named = json.loads(self.rows('group_message')[-1]['payload'])
        self.assertTrue(named['addressed_to_self'])
        self.assertEqual(named['address_basis'], 'direct_name')
        self.assertFalse(named['quoted'])

    def test_group_audio_and_drawing_keep_event_scope_at_the_bridge_boundary(self):
        import asyncio
        import qq_openclaw_bridge as b
        import asr, tts, image_gen
        from unittest.mock import AsyncMock
        audio = Path(self.tmp.name) / 'input.wav'
        audio.write_bytes(b'fixture')
        async def run():
            with patch.object(asr, 'configured', return_value=True), \
                 patch.object(asr, '_transcribe_impl', return_value={'ok': True, 'text': 'heard'}):
                await b.transcribe_voice_segments([{'path': str(audio)}], session='group:88', visibility='group')
            with patch.object(b, '_lyrics_for', new=AsyncMock(return_value='lyrics')), \
                 patch.object(tts, '_synth_impl', new=AsyncMock(return_value={'ok': False, 'error': 'unavailable'})), \
                 patch.object(image_gen, '_generate_impl', new=AsyncMock(return_value={'ok': False, 'error': 'unavailable'})), \
                 patch.object(b, 'send_group_msg', new=AsyncMock(return_value=True)):
                await b._sing_and_send(None, 'song', '', '88', '123', False)
                await b._draw_and_send(None, 'draw', '88', '123', False)
        asyncio.run(run())
        rows = self.rows('tool_result')
        self.assertEqual({json.loads(r['payload'])['tool'] for r in rows}, {'asr', 'tts', 'image_gen'})
        self.assertTrue(all(r['session'] == 'group:88' and r['visibility'] == 'group' for r in rows))

    def test_weather_read_is_a_scoped_tool_event(self):
        import asyncio
        import qq_openclaw_bridge as b
        from unittest.mock import AsyncMock
        response = type('WeatherResponse', (), {'status_code': 200,
            'json': lambda self: {'current_condition': [{'temp_C': '21'}]}})()
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = response
        with patch.object(b.httpx, 'AsyncClient', return_value=client):
            result = asyncio.run(b._fetch_weather('重庆', session='group:88', visibility='group'))
        self.assertEqual(result['now_temp'], '21')
        rows = self.rows('tool_result')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session'], 'group:88')
        self.assertEqual(json.loads(rows[0]['payload'])['tool'], 'weather')

    def test_private_batch_keeps_original_events_before_any_early_return(self):
        import asyncio
        import qq_openclaw_bridge as b
        from unittest.mock import AsyncMock
        async def run():
            original_sleep = asyncio.sleep
            async def quick_sleep(_seconds):
                await original_sleep(0)
            messages = [{'user_id': 882211, 'self_id': 999, 'message_id': mid, 'time': 100,
                         'message': [{'type': 'text', 'data': {'text': text}}]}
                        for mid, text in [(10, '第一条'), (11, '第二条')]]
            with patch.object(b.asyncio, 'sleep', new=quick_sleep), \
                 patch.object(b, '_handle_private_message', new=AsyncMock(return_value=None)):
                await asyncio.gather(*(b.handle_private_message(None, event) for event in messages))
        asyncio.run(run())
        rows = self.rows('private_message')
        self.assertEqual(len(rows), 2)
        self.assertEqual({json.loads(r['payload'])['text'] for r in rows}, {'第一条', '第二条'})

    def test_failed_structured_vision_attempt_is_recorded_with_session(self):
        import asyncio
        import qq_openclaw_bridge as b
        from unittest.mock import AsyncMock
        with patch.object(b, 'image_url_to_openai_ref', new=AsyncMock(return_value='data:image/png;base64,eA==')), \
             patch.object(b, 'call_mimo_vision', new=AsyncMock(return_value='unavailable')), \
             patch.object(b, 'call_openclaw', new=AsyncMock(return_value='')):
            asyncio.run(b._describe_image('', '', image_ref='image1', sensitive=True,
                                           session='private:123'))
        rows = self.rows('tool_result')
        self.assertEqual(len(rows), 1)
        self.assertIs(json.loads(rows[0]['payload'])['ok'], False)
        self.assertEqual(rows[0]['session'], 'private:123')

    def test_failed_bili_metadata_attempt_is_recorded_with_session(self):
        import asyncio
        import types
        import qq_openclaw_bridge as b
        from unittest.mock import AsyncMock
        fake = types.SimpleNamespace(seen_before=lambda _: False,
                                     fetch_meta=AsyncMock(return_value={'ok': False, 'reason': 'timeout'}))
        old = list(b._BILI_QUEUE)
        b._BILI_QUEUE[:] = [{'kind': 'bvid', 'ref': 'BV1GJ411x7h7', 'scene': 'group',
                            'session': 'group:123', 'run_id': 'bili-failed-run'}]
        try:
            asyncio.run(b._bili_drain(fake))
        finally:
            b._BILI_QUEUE[:] = old
        rows = self.rows('tool_result')
        self.assertEqual(len(rows), 1)
        self.assertIs(json.loads(rows[0]['payload'])['ok'], False)
        self.assertEqual(rows[0]['session'], 'group:123')
        self.assertEqual(rows[0]['visibility'], 'group')

    def test_bili_note_publishes_only_metadata_result_not_copyright_text(self):
        import bili
        with patch.object(bili, '_notes', {'sig': None, 'items': None}):
            rec = bili.save_note({'ok': True, 'bvid': 'BV1GJ411x7h7', 'title': '测试标题',
                                  'desc': '不应复制的简介'}, reaction='测试想法')
        self.assertIsNotNone(rec)
        rows = self.rows('tool_result')
        self.assertEqual(len(rows), 1)
        p = json.loads(rows[0]['payload'])
        self.assertEqual(p['tool'], 'bili')
        self.assertNotIn('测试想法', rows[0]['payload'])
        self.assertNotIn('不应复制的简介', rows[0]['payload'])

    def test_vision_result_does_not_expose_sensitive_note_text(self):
        import vision_notes as vn
        vn._reset_cache()
        self.addCleanup(vn._reset_cache)
        vn.save_note('private-photo', {'objects': ['私密对象'], 'sensitive': True},
                     source='vision', sensitive=True)
        rows = self.rows('tool_result')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['visibility'], 'private')
        self.assertNotIn('私密对象', rows[0]['payload'])


if __name__ == '__main__':
    unittest.main()
