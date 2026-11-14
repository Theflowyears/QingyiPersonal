"""Executable regressions for the reviewed enforcement and evidence consumers."""
import asyncio
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import qq_openclaw_bridge as b
import self_model as sm
import curiosity as cu
import persona_growth as pg
import persona_store as ps
import memory_depth as md
import phase_audit
from bot import engineering_contracts as contracts


class ReviewEnforcement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, DATA_DIR=str(self.root),
                                 PERSONA_DATA_DIR='', QQ_DRY_RUN='1', PYTHONUTF8='1')
        environment.start()
        self.addCleanup(environment.stop)
        cu._reset_cache()
        self.addCleanup(cu._reset_cache)
        self.addCleanup(self.close_store)
        for patcher in (
                patch.object(b, 'QQ_DRY_RUN', False),
                patch.object(b, 'MASTER_QQ_IDS', {'19'}),
                patch.object(b, 'CURIOSITY_SHARE_ENABLED', True),
                patch.object(b, 'get_system_prompt', return_value='offline'),
                patch.object(b, '_already_delivered', return_value=False),
                patch.object(b, '_record_delivered'),
                patch.object(b, '_world_note')):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def close_store():
        # The suite exercises both supported import spellings; close each actual
        # connection before Windows removes the isolated directory.
        from bot import persona_store as packaged
        for store in (ps, packaged):
            if store._conn is not None:
                store._conn.close()
                store._conn = None

    async def test_missing_group_image_fallback_and_next_text_do_not_deadlock(self):
        for image_name in ('', 'missing.png'):
            with self.subTest(image_name=image_name), \
                 patch.object(b, 'HOST_SHARE_DIR', str(self.root)), \
                 patch.object(b, '_chat_response', AsyncMock(return_value={'retcode': 0})) as transport, \
                 patch.object(b, 'record_bot_group_message'):
                await asyncio.wait_for(b.send_group_image(None, '19', image_name), timeout=1)
                self.assertTrue(await asyncio.wait_for(
                    b.send_group_msg(None, '19', '27', '我还在呢。', mention=False), timeout=1))
                self.assertEqual(2, transport.await_count)
                messages = [call.args[2]['message'] for call in transport.await_args_list]
                self.assertTrue(all(part['type'] == 'text' for msg in messages for part in msg))
                expected = '未找到图片：missing.png' if image_name else '未指定图片名。'
                self.assertIn(expected, messages[0][0]['data']['text'])
                self.assertIn('我还在呢。', messages[1][0]['data']['text'])

    async def test_group_text_precedes_withheld_sticker_receipt(self):
        await self.assert_text_precedes_sticker('group')

    async def test_group_quoted_text_precedes_withheld_sticker_receipt(self):
        await self.assert_text_precedes_sticker('group', quote=True)

    async def test_group_burst_precedes_withheld_sticker_receipt(self):
        await self.assert_text_precedes_sticker('group', burst=True)

    async def test_private_text_precedes_withheld_sticker_receipt(self):
        await self.assert_text_precedes_sticker('private')

    async def test_private_burst_precedes_withheld_sticker_receipt(self):
        await self.assert_text_precedes_sticker('private', burst=True)

    async def assert_text_precedes_sticker(self, scene, *, quote=False, burst=False):
        import sticker_store
        key = 'group-19' if scene == 'group' else 'private-user-19'
        text_sent = asyncio.Event()
        sticker_submitted = asyncio.Event()
        release_receipt = asyncio.Event()
        submissions = []
        tasks = set()

        async def transport(ws, action, params):
            media = any(part['type'] == 'image' for part in params['message'])
            submissions.append('sticker' if media else 'text')
            if media:
                sticker_submitted.set()
                await release_receipt.wait()
            else:
                text_sent.set()
            return {'retcode': 0}

        async def answer(*args, **kwargs):
            b.STICKER_WISH[key] = True
            if burst:
                b.FLIRT_BURST[key] = {'ts': b.time.time(), 'lines': ['我听见啦。']}
            return '我听见啦。'

        event = {'user_id': '19', 'self_id': 9, 'message_id': 123,
                 '_observations_recorded': True, 'sender': {'nickname': '甲'},
                 'message': [{'type': 'text', 'data': {'text': '晚上好'}}]}
        with ExitStack() as stack:
            used = stack.enter_context(patch.object(sticker_store, 'bump_used'))
            for patcher in (
                    patch.dict(b.STICKER_WISH, {}, clear=True),
                    patch.dict(b.FLIRT_BURST, {}, clear=True),
                    patch.object(b, '_BG_TASKS', tasks),
                    patch.object(b, '_sticker_last_ts', {}),
                    patch.object(b, 'AUTO_QUOTE_REPLY', quote),
                    patch.object(b, 'ask_openclaw', side_effect=answer),
                    patch.object(b, '_tool_plan', return_value={}),
                    patch.object(b, 'save_periodic_state'),
                    patch.object(b, 'record_bot_group_message'),
                    patch.object(b, '_chat_response', side_effect=transport),
                    patch.object(sticker_store, 'pick_random', return_value={'abs': 'fixture.png', 'id': 's'}),
                    patch.object(b, '_napcat_visible', return_value='https://invalid/fixture.png'),
                    patch.object(b, 'handle_sticker_command', AsyncMock(return_value=False)),
                    patch.object(b, '_maybe_cancel_draw', AsyncMock(return_value=False)),
                    patch.object(b, 'handle_draw_command', AsyncMock(return_value=False)),
                    patch.object(b, 'handle_sing_command', AsyncMock(return_value=False)),
                    patch.object(b, 'handle_voice_command', AsyncMock(return_value=False)),
                    patch('affection.judge_and_bump', return_value=(0, '')),
                    patch('affection.set_nick')):
                stack.enter_context(patcher)
            handler = asyncio.create_task(
                b._direct_group_reply(None, event, '19', '19', '晚上好', [], False, 123, None)
                if scene == 'group' else b._handle_private_message(None, event))
            try:
                await asyncio.wait_for(text_sent.wait(), timeout=1)
                await asyncio.wait_for(asyncio.shield(handler), timeout=1)
                await asyncio.wait_for(sticker_submitted.wait(), timeout=1)
                self.assertEqual(['text', 'sticker'], submissions)
                self.assertFalse(release_receipt.is_set())
                used.assert_not_called()
                release_receipt.set()
                await asyncio.gather(*tasks)
                used.assert_called_once_with('s')
            finally:
                handler.cancel()
                for task in list(tasks):
                    task.cancel()
                await asyncio.gather(handler, *tasks, return_exceptions=True)

    async def test_media_denial_blocks_real_entrypoints(self):
        import sticker_store
        image = self.root / 'fixture.png'
        image.write_bytes(b'fixture')
        sm.record_consent('19', 'send', 'deny', 'stop')
        ws = Mock(send=AsyncMock())
        with patch.object(b, 'HOST_SHARE_DIR', str(self.root)), \
             patch.object(sticker_store, 'pick_random', return_value={'abs': str(image), 'id': 's'}), \
             patch.object(b, '_napcat_visible', return_value='https://invalid/fixture.png'), \
             patch.object(b, '_sticker_last_ts', {}), \
             patch.object(b, '_chat_response', AsyncMock()) as transport, \
             patch.object(b, 'record_bot_group_message') as history:
            self.assertFalse(await b._maybe_send_sticker(ws, user_id='19'))
            self.assertFalse(await b._maybe_send_sticker(ws, group_id='19'))
            self.assertFalse(await b.send_group_image(ws, '19', image.name))
        ws.send.assert_not_awaited()
        transport.assert_not_awaited()
        history.assert_not_called()

    async def test_media_unknown_receipts_do_not_claim_delivery_or_resend(self):
        import sticker_store
        image = self.root / 'fixture.png'
        image.write_bytes(b'fixture')
        event = sm.record_event('group_message', session='group:19')
        import trigger_ledger
        trigger_ledger.reset()
        trigger_ledger.note('group:19', event)
        trigger_ledger.capture('group:19')
        ws = Mock(send=AsyncMock())
        with patch.object(b, 'HOST_SHARE_DIR', str(self.root)), \
             patch.object(sticker_store, 'pick_random', return_value={'abs': str(image), 'id': 's'}), \
             patch.object(sticker_store, 'bump_used') as used, \
             patch.object(b, '_napcat_visible', return_value='https://invalid/fixture.png'), \
             patch.object(b, '_sticker_last_ts', {}), \
             patch.object(b, '_chat_response', AsyncMock(return_value=None)) as transport, \
             patch.object(b, 'record_bot_group_message') as history:
            for _ in range(2):
                self.assertFalse(await b._maybe_send_sticker(ws, group_id='19'))
                self.assertFalse(await b.send_group_image(ws, '19', image.name))
        self.assertEqual(2, transport.await_count)
        ws.send.assert_not_awaited()
        used.assert_not_called()
        history.assert_not_called()
        with closing(sm._db()) as conn:
            self.assertEqual(2, conn.execute("SELECT count(*) FROM outbox_part WHERE state='unknown'").fetchone()[0])

    async def test_other_media_entrypoints_enforce_denial_and_unknown(self):
        import sticker_store
        import napcat_channel
        image = self.root / 'fixture.png'
        image.write_bytes(b'fixture')
        event = sm.record_event('group_message', session='group:19')
        import trigger_ledger
        trigger_ledger.reset()
        trigger_ledger.note('group:19', event)
        trigger_ledger.capture('group:19')
        ws = Mock(send=AsyncMock())
        deny = sm.record_consent('19', 'send', 'deny', 'stop')
        with patch.object(b, 'HOST_SHARE_DIR', str(self.root)), \
             patch.object(b, '_napcat_visible', return_value='https://invalid/fixture.png'), \
             patch.object(sticker_store, 'parse_command', return_value=('send', '')), \
             patch.object(sticker_store, 'pick_random', return_value={'abs': str(image), 'id': 's'}), \
             patch.object(sticker_store, 'bump_used') as used, \
             patch.object(napcat_channel.channel, 'call', AsyncMock(return_value=None)) as direct, \
             patch.object(b, '_chat_response', AsyncMock(return_value=None)) as transport, \
             patch.object(b, 'record_bot_group_message') as history:
            self.assertFalse(await b._send_image_file(ws, str(image), group_id='19'))
            await b.handle_sticker_command(ws, {}, '19', '27', '/斗图')
            self.assertFalse(await b.upload_group_file(ws, '19', image.name))
            transport.assert_not_awaited()
            direct.assert_not_awaited()
            sm.revoke_consent(deny, 'resume')
            for _ in range(2):
                self.assertFalse(await b._send_image_file(ws, str(image), group_id='19'))
                await b.handle_sticker_command(ws, {}, '19', '27', '/斗图')
                self.assertFalse(await b.upload_group_file(ws, '19', image.name))
        self.assertEqual(2, transport.await_count, 'identical image intent dedupes across entrypoints')
        direct.assert_not_awaited()
        ws.send.assert_not_awaited()
        history.assert_not_called()
        used.assert_not_called()

    def seed_share(self):
        eid = sm.record_event('tool_result', session='private-user-19')
        self.assertTrue(cu.push_share('真实视频', detail='确实看到了字幕内容', kind='watch',
                                     event_ids=[eid], origin_session='private-user-19',
                                     requester_qq='19', recipient_qq='19'))
        return eid

    async def test_unknown_share_never_requeues_even_after_restart(self):
        self.seed_share()
        with patch.object(b, 'call_openclaw', AsyncMock(side_effect=['这条视频的字幕挺有意思。', '换个说法再讲一次。'])) as llm, \
             patch.object(b, '_chat_response', AsyncMock(return_value=None)) as transport:
            self.assertFalse(await b._curiosity_share(cu, object()))
            cu._reset_cache()
            with patch.object(cu, '_RUNTIME_OWNER', 'restarted'):
                self.assertFalse(await b._curiosity_share(cu, object()))
        self.assertEqual(1, transport.await_count)
        self.assertEqual(1, llm.await_count)
        self.assertEqual('unknown', cu.topic('真实视频').get('share_delivery_state'))
        self.assertIsNone(cu.share_candidate(recipient_qq='19'))

    def test_crash_after_send_boundary_is_not_a_generation_retry(self):
        eid = self.seed_share()
        original = cu.take_pending_share(recipient_qq='19')
        key = cu.freeze_share_delivery('真实视频', '冻结的表达', '19')
        self.assertTrue(key)
        cu._reset_cache()
        with patch.object(cu, '_RUNTIME_OWNER', 'restarted'):
            self.assertIsNone(cu.take_pending_share(recipient_qq='19'))
            self.assertFalse(cu.push_share('真实视频', detail='换内容', event_ids=[eid]))
        payload = cu.topic('真实视频')['share_payload']
        self.assertEqual(original['intent_id'], payload['intent_id'])
        self.assertEqual([eid], payload['event_ids'])
        self.assertEqual('冻结的表达', payload['rendered_text'])

    async def test_definite_share_failure_retries_frozen_intent_and_evidence(self):
        eid = self.seed_share()
        with patch.object(b, 'call_openclaw', AsyncMock(side_effect=['这条视频的字幕挺有意思。', '不能重新生成这句。'])) as llm, \
             patch.object(b, '_chat_response', AsyncMock(side_effect=[{'retcode': 100}, {'retcode': 0}])) as transport:
            self.assertFalse(await b._curiosity_share(cu, object()))
            pending = cu._data().get('pending_share') or {}
            self.assertEqual([eid], pending.get('event_ids'))
            self.assertTrue(pending.get('intent_id'))
            intent_id = pending['intent_id']
            self.assertTrue(await b._curiosity_share(cu, object()))
        self.assertEqual(1, llm.await_count)
        self.assertEqual(transport.await_args_list[0].args[2], transport.await_args_list[1].args[2])
        with closing(sm._db()) as conn:
            actions = conn.execute("SELECT event_ids FROM action WHERE kind='dm'").fetchall()
            self.assertTrue(actions)
            self.assertTrue(all(json.loads(row['event_ids']) == [eid] for row in actions))
            self.assertEqual(2, conn.execute('SELECT count(*) FROM action_outbox WHERE logical_key LIKE ?',
                                            ('share:' + intent_id + ':%',)).fetchone()[0])

    async def test_generation_retry_preserves_provenance(self):
        eid = self.seed_share()
        with patch.object(b, 'call_openclaw', AsyncMock(return_value='')):
            self.assertFalse(await b._curiosity_share(cu, object()))
        pending = cu._data().get('pending_share') or {}
        self.assertEqual([eid], pending.get('event_ids'))
        self.assertTrue(pending.get('intent_id'))
        self.assertEqual('private-user-19', pending.get('origin_session'))

    async def test_denied_friend_dm_releases_real_reservation(self):
        import proactive_dm as pdm
        sm.record_consent('19', 'dm', 'deny', 'stop')
        now = 1800000000.0
        with patch.object(pdm, '_in_window', return_value=True), \
             patch.object(pdm, '_local_seconds', return_value=86399), \
             patch.object(pdm, 'ENABLED', True):
            self.assertIsNotNone(pdm.claim({'ok': True, 'uid': '19'}, now=now))
        with patch.object(pdm, 'release', wraps=pdm.release) as release, \
             patch.object(b, 'call_openclaw', AsyncMock()) as llm:
            self.assertFalse(await b._send_friend_dm(object(), '19', '朋友', '话题', pdm))
        release.assert_called_once()
        self.assertIsNone(pdm._load()['pending'])
        self.assertEqual(0, pdm.stats(now=now)['n'])
        llm.assert_not_awaited()

    def test_retraction_suppresses_all_consumers_and_persisted_identity_cache(self):
        eid = sm.record_event('private_message', payload={'text': 'source'})
        original = sm.get_event(eid)
        view = '我总喜欢把来源核对清楚再开口'
        sm.note_self_view(view, evidence=[eid])
        pg.save({'self_view': view, 'voice': {}})
        self.assertIn(view, md.identity_block())
        history = sm.layer_history('personality')
        self.assertTrue(sm.retract_evidence(eid, 'withdrawn'))
        self.assertEqual('', pg.load().get('self_view'))
        self.assertEqual('', sm.layer_history('personality')[0]['content'].get('self_view'))
        sm.revise_layer('identity', {'text': view}, 'restore withdrawn version')
        self.assertNotIn(view, sm.layer_history('identity')[0]['content'].get('text', ''))
        self.assertEqual('', ps.get_meta('self_view'))
        self.assertNotIn(view, md.identity_block())
        md._IDENT_SNAP.clear()
        md._IDENT_SNAP.update(day=None, text=None)
        self.assertNotIn(view, md.identity_block())
        pg.save({'self_view': view, 'voice': {}})
        self.assertEqual('', pg.load()['self_view'])
        self.assertEqual('', sm.layer_history('personality')[0]['content'].get('self_view'))
        self.assertIn(history[0], sm.layer_history('personality'))
        self.assertEqual(original, sm.get_event(eid))
        sm.note_self_view(view, reason='unproven restore')
        pg.save({'self_view': view})
        self.assertEqual('', pg.load()['self_view'])
        fresh = sm.record_event('private_message', payload={'text': 'new independent support'})
        sm.note_self_view(view, reason='recomputed', evidence=[fresh])
        pg.save({'self_view': view})
        self.assertEqual(view, pg.load()['self_view'])

    def test_audit_does_not_count_withdrawn_or_invalidated_grounding(self):
        eid = sm.record_event('private_message', ts=900, source='qq')
        bid = sm.add_belief('claim', evidence=[eid], ts=950)
        sm.note_self_view('view', evidence=[eid], ts=960)
        sm.record_action('dm', target_qq='19', topic_qq='19', evidence=[eid], ts=950)
        sm.retract_evidence(eid, 'withdrawn')
        report = phase_audit._readiness_cognition(self.root / 'persona' / 'cognition.db', 1000, 86400)
        self.assertEqual(0, report['action_evidence']['linked'])
        self.assertEqual(0, report['belief_grounding']['with_existing_evidence'])
        self.assertEqual(0, report['self_view_grounding']['with_direct_sources'])
        # Also reject status/invalidation independently of missing/retracted refs.
        eid2 = sm.record_event('private_message', ts=901, source='qq')
        bid2 = sm.add_belief('inactive claim', evidence=[eid2], ts=951)
        sid2 = sm.note_self_view('invalid view', evidence=[eid2], ts=961)
        with closing(sm._db()) as conn, conn:
            conn.execute("UPDATE belief SET status='invalidated' WHERE id=?", (bid2,))
            conn.execute('INSERT INTO cognition_invalidation VALUES(?,?,?)', ('self_view_rev', sid2, eid2))
        report = phase_audit._readiness_cognition(self.root / 'persona' / 'cognition.db', 1000, 86400)
        self.assertEqual(0, report['belief_grounding']['with_existing_evidence'])
        self.assertEqual(0, report['self_view_grounding']['with_direct_sources'])

    async def test_autonomous_public_video_stays_shareable_to_bound_master(self):
        await self.drain_watch('自主', 'autonomy', 'autonomous-run')
        payload = cu.topic('真实视频').get('share_payload') or {}
        self.assertEqual('public', payload.get('visibility'))
        self.assertEqual('19', payload.get('recipient_qq'))
        self.assertTrue(payload.get('event_ids'))
        self.assertEqual('public', sm.get_event(payload['event_ids'][0])['visibility'])
        self.assertIsNone(cu.take_pending_share(recipient_qq='27'))
        self.assertIsNotNone(cu.take_pending_share(recipient_qq='19'))

    async def test_bili_share_carries_exact_tool_event_and_private_recipient(self):
        import bili
        await self.drain_watch('private', 'private-user-27', 'actual-run')
        payload = cu.topic('真实视频').get('share_payload') or {}
        self.assertEqual('private-user-27', payload.get('origin_session'))
        self.assertEqual('27', payload.get('recipient_qq'))
        self.assertEqual('private', payload.get('visibility'))
        self.assertEqual(1, len(payload.get('event_ids') or []))
        event = sm.get_event(payload['event_ids'][0])
        self.assertEqual('tool_result', event['kind'])
        self.assertEqual('actual-run', json.loads(event['payload'])['run_id'])
        note = bili.note_for('BVfixture')
        self.assertEqual(payload['event_ids'][0], note.get('event_id'))
        self.assertIsNone(cu.take_pending_share(recipient_qq='19'))

    async def drain_watch(self, scene, session, run_id):
        import bili
        meta = {'ok': True, 'bvid': 'BVfixture', 'title': '真实视频', 'duration': 12}
        queue = [{'kind': 'feed', 'ref': 'BVfixture', 'meta': meta,
                  'scene': scene, 'session': session, 'run_id': run_id}]
        with patch.object(b, '_BILI_QUEUE', queue), \
             patch.object(bili, 'seen_before', return_value=False), \
             patch.object(bili, 'fetch_subtitles', AsyncMock(return_value={'ok': True})), \
             patch.object(bili, 'fetch_comments', AsyncMock(return_value={})), \
             patch.dict(os.environ, BILI_VISION_ENABLED='0'), \
             patch.object(b, '_bili_reaction', AsyncMock(return_value='我读到了字幕')), \
             patch.object(b, '_kb_judge', AsyncMock(return_value={})), \
             patch.object(pg, 'record_experience'):
            await b._bili_drain(bili)


class ContractRunnerIntegrity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, PERSONA_DATA_DIR=str(self.root),
                                 DATA_DIR=str(self.root), QQ_DRY_RUN='1', PYTHONUTF8='1')
        environment.start()
        self.addCleanup(environment.stop)
        self.artifact = self.root / 'engineering_contracts.json'
        self.artifact.write_text('previous-pass', encoding='utf-8')
        binding = patch.object(contracts, 'binding', return_value={'isolated': True})
        binding.start()
        self.addCleanup(binding.stop)

    def test_timeout_invalidates_prior_pass_before_subprocess(self):
        def timeout(*args, **kwargs):
            self.assertFalse(self.artifact.exists(), 'old pass survives while verification is running')
            raise subprocess.TimeoutExpired(args[0], 180)
        with patch.object(contracts.subprocess, 'run', side_effect=timeout):
            self.assertEqual(1, contracts.run())
        self.assertFalse(self.artifact.exists())

    def test_spawn_error_removes_prior_artifact(self):
        with patch.object(contracts.subprocess, 'run', side_effect=OSError('spawn failed')):
            self.assertEqual(1, contracts.run())
        self.assertFalse(self.artifact.exists())

    def test_exit_zero_without_structured_execution_or_with_skips_is_not_pass(self):
        structured_skip = ('CONTRACT_EXECUTION=' + json.dumps({
            'run': 1, 'skipped': 1, 'failures': 0, 'errors': 0,
            'expected_failures': 0, 'unexpected_successes': 0}) + '\n').encode()
        for output in (b'', b'Ran 1 test\nOK (skipped=1)\n', structured_skip):
            with self.subTest(output=output), patch.object(contracts.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, output, b'')):
                self.assertEqual(1, contracts.run())
                self.assertFalse(self.artifact.exists())
