"""证据链：她看到的消息 → 她因此做出的动作。

线上账本里 111 条动作、0 条能回答「你当时为什么发这句」。这几条测试锁的是
因果记录本身，不是发送逻辑。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BOT = Path(__file__).resolve().parents[1] / 'bot'
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import trigger_ledger as tl
import qq_openclaw_bridge as b
import self_model as sm


def _action_row(action_id):
    from contextlib import closing
    with closing(sm._db()) as c:
        row = c.execute("SELECT * FROM action WHERE id=?", (action_id,)).fetchone()
        return dict(row) if row else None


class SendEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """发出去的那句话，必须能追溯到当时触发它的那几条消息。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='evidence_')
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {
            'DATA_DIR': root, 'PERSONA_DATA_DIR': root, 'TRIGGER_LEDGER_TTL': '900'})
        environment.start()
        self.addCleanup(environment.stop)
        tl.reset()
        self.addCleanup(tl.reset)

    def _last_action(self, kind):
        from contextlib import closing
        with closing(sm._db()) as c:
            row = c.execute("SELECT * FROM action WHERE kind=? ORDER BY created DESC",
                            (kind,)).fetchone()
            return dict(row) if row else None

    async def test_a_group_reply_carries_the_messages_that_triggered_it(self):
        seen = sm.record_event('group_message', session='group:771177', actor_qq='22001',
                               visibility='group', payload={'text': '今天热化了'},
                               dedupe='g:771177:1')
        tl.note('group:771177', seen)
        tl.capture('group:771177')
        submit = AsyncMock(return_value=True)
        with patch.object(b, '_submit_chat', new=submit), \
                patch.object(b, 'record_bot_group_message'), \
                patch.object(b, 'QQ_DRY_RUN', False):
            ok = await b.send_group_msg(None, '771177', '22001', '是啊，我快熟了')
        self.assertTrue(ok)
        row = self._last_action('group')
        self.assertEqual(json.loads(row['event_ids']), [seen])
        self.assertEqual(row['topic_qq'], sm.person_id('22001'))

    async def test_a_private_reply_names_the_person_it_was_about(self):
        """线上 106 条聊天动作只有 53 条认得出对象：私聊全都没写 topic_qq。"""
        seen = sm.record_event('private_message', session='private:22001', actor_qq='22001',
                               visibility='private', payload={'text': '在吗'},
                               dedupe='p:22001:1')
        tl.note('private:22001', seen)
        tl.capture('private:22001')
        submit = AsyncMock(return_value=True)
        with patch.object(b, '_submit_chat', new=submit), \
                patch.object(b, 'QQ_DRY_RUN', False):
            ok = await b.send_private_msg(None, '22001', '在的呀')
        self.assertTrue(ok)
        row = self._last_action('dm')
        self.assertEqual(json.loads(row['event_ids']), [seen])
        self.assertEqual(row['target_qq'], sm.person_id('22001'))
        self.assertEqual(row['topic_qq'], sm.person_id('22001'))

    async def test_a_reply_with_no_trigger_in_window_claims_no_evidence(self):
        """没人在窗口里说过话却自己开口 —— 这时候硬编一条证据就是造假。"""
        submit = AsyncMock(return_value=True)
        with patch.object(b, '_submit_chat', new=submit), \
                patch.object(b, 'record_bot_group_message'), \
                patch.object(b, 'QQ_DRY_RUN', False):
            ok = await b.send_group_msg(None, '771177', None, '有人玩吗')
        self.assertTrue(ok)
        self.assertEqual(json.loads(self._last_action('group')['event_ids']), [])

    async def test_a_lane_with_its_own_basis_can_pass_that_evidence_in(self):
        """主动找朋友聊天的依据不是「刚才那句话」，而是她挑人的那件事。"""
        basis = sm.record_event('group_message', session='group:55', actor_qq='22001',
                                visibility='group', payload={'text': '阿甲今天抽卡出货了'})
        submit = AsyncMock(return_value=True)
        with patch.object(b, '_submit_chat', new=submit), \
                patch.object(b, 'QQ_DRY_RUN', False):
            ok = await b.send_private_msg(None, '22001', '听说你出货了', evidence=[basis])
        self.assertTrue(ok)
        self.assertEqual(json.loads(self._last_action('dm')['event_ids']), [basis])



class TriggerLedgerTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TRIGGER_LEDGER_TTL': '600'})
        self.env.start()
        self.addCleanup(self.env.stop)
        tl.reset()

    def test_a_message_she_sees_stays_available_as_the_trigger_for_a_while(self):
        tl.note('group:123', 'evt_a', ts=1000)
        self.assertEqual(tl.pending('group:123', now=1100), ['evt_a'])

    def test_an_old_message_is_not_claimed_as_the_trigger_for_a_later_reply(self):
        tl.note('group:123', 'evt_a', ts=1000)
        self.assertEqual(tl.pending('group:123', now=1000 + 601), [])

    def test_the_newest_trigger_is_the_last_one_she_saw(self):
        tl.note('private:9', 'evt_old', ts=1000)
        tl.note('private:9', 'evt_new', ts=1500)
        self.assertEqual(tl.pending('private:9', now=1501), ['evt_old', 'evt_new'])
        self.assertEqual(tl.latest('private:9'), 'evt_new')

    def test_the_same_message_recorded_twice_is_still_one_trigger(self):
        tl.note('group:123', 'evt_a', ts=1000)
        tl.note('group:123', 'evt_a', ts=1000)
        self.assertEqual(tl.pending('group:123', now=1001), ['evt_a'])

    def test_one_scene_does_not_leak_into_another(self):
        tl.note('group:123', 'evt_g', ts=1000)
        self.assertEqual(tl.pending('private:123', now=1001), [])


class SearchReceiptTests(unittest.IsolatedAsyncioTestCase):
    """她自己查的那件事：成败要有回执，起因要留下事件号。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='search_ev_')
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {
            'DATA_DIR': root, 'PERSONA_DATA_DIR': root, 'QQ_DRY_RUN': '1',
            'CURIOSITY_MAX_PER_DAY': '2', 'CURIOSITY_ACT_THRESHOLD': '1.0'})
        environment.start()
        self.addCleanup(environment.stop)
        import curiosity as cu
        cu._reset_cache()
        self.addCleanup(cu._reset_cache)

    def _last_action(self, kind):
        from contextlib import closing
        with closing(sm._db()) as c:
            row = c.execute("SELECT * FROM action WHERE kind=? ORDER BY created DESC",
                            (kind,)).fetchone()
            return dict(row) if row else None

    async def _act_on(self, topic, origin):
        import curiosity as cu
        import web_search
        with patch.object(cu, 'ACT_THRESHOLD', 1.0), patch.object(cu, 'MAX_PER_DAY', 2), \
                patch.object(web_search, 'topic_block_reason', lambda t: ''), \
                patch.object(b, '_curiosity_lookup',
                             new=AsyncMock(return_value=(True, ['key-1']))):
            cu._touch(topic, weight=4.0, seed_from='self_asked', event_ids=[origin])
            self.assertEqual(await b._curiosity_act(cu), 1)

    async def test_a_finished_search_leaves_a_result_event_pointing_at_itself(self):
        """以前这里直接 record_action(state='accepted')：
        自己声明成功，账本里没有任何一条独立回执 —— 和「发出去的消息」不是一回事。"""
        origin = sm.record_event('group_message', session='group:7001', actor_qq='22001',
                                 visibility='group', payload={'text': '猫为什么打呼噜'})
        await self._act_on('猫为什么打呼噜', origin)
        row = self._last_action('search')
        self.assertEqual(row['state'], 'accepted')
        from contextlib import closing
        with closing(sm._db()) as c:
            n = c.execute("SELECT COUNT(*) AS n FROM event WHERE kind='action_result' "
                          "AND payload LIKE ?", ('%' + row['id'] + '%',)).fetchone()['n']
        self.assertEqual(n, 1)

    async def test_the_search_and_the_goal_remember_what_made_her_curious(self):
        origin = sm.record_event('group_message', session='group:7001', actor_qq='22001',
                                 visibility='group', payload={'text': '猫为什么打呼噜'})
        await self._act_on('猫为什么打呼噜', origin)
        row = self._last_action('search')
        self.assertEqual(json.loads(row['event_ids']), [origin])
        from contextlib import closing
        with closing(sm._db()) as c:
            goal = c.execute("SELECT parent_event FROM goal ORDER BY created DESC LIMIT 1").fetchone()
        self.assertEqual(goal['parent_event'], origin)

    async def test_a_wish_born_search_with_no_chat_origin_still_carries_its_receipt(self):
        """她自己从愿望里长出来的好奇，没有「刚才那句群消息」当起因。
        但查证真发生了 —— 那条 curiosity_search 回执就是依据。以前 mark_satisfied
        只收 keys、把 result_event 丢了，于是主动分享那一步 event_ids 永远是空的，
        action_evidence 永远差这几条。"""
        import curiosity as cu
        import web_search
        with patch.object(cu, 'ACT_THRESHOLD', 1.0), patch.object(cu, 'MAX_PER_DAY', 2), \
                patch.object(web_search, 'topic_block_reason', lambda t: ''), \
                patch.object(b, '_curiosity_lookup',
                             new=AsyncMock(return_value=(True, ['key-1']))):
            cu._touch('荒坂塔的建筑原型', weight=4.0, seed_from='wish')
            self.assertEqual(await b._curiosity_act(cu), 1)
        pending = cu._data().get('pending_share') or {}
        refs = pending.get('event_ids') or []
        self.assertTrue(refs, '自主查证的回执在 mark_satisfied 处被丢了，分享没有依据')
        from contextlib import closing
        with closing(sm._db()) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM event WHERE id IN (%s)" % ','.join('?' * len(refs)),
                refs).fetchall()]
        self.assertEqual(len(rows), len(refs), '分享依据指向不存在的事件')
        receipts = [r for r in rows if r['kind'] == 'tool_result'
                    and json.loads(r['payload'] or '{}').get('tool') == 'curiosity_search']
        self.assertTrue(receipts, '自主查证至少要把那条 curiosity_search 回执挂上')
        for r in receipts:
            self.assertTrue(sm.evidence_valid('event', r['id']), '回执被作废了')
            self.assertNotEqual(json.loads(r['payload'] or '{}').get('evidence_origin'),
                                'verification', '真实查证不能记成验证样本')


class ProactiveEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """主动开口那两条路：她自己挑的话头也要留得下出处。"""

    def setUp(self):
        # affection/whisper 这些常驻模块会拿着 persona.db 的连接不放，
        # Windows 上目录就被删不掉。进程级连接在生产里是正常的，
        # 这里只是临时目录要能收干净。
        temporary = tempfile.TemporaryDirectory(prefix='proactive_ev_', ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {
            'DATA_DIR': root, 'PERSONA_DATA_DIR': root, 'QQ_DRY_RUN': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        import curiosity as cu
        cu._reset_cache()
        self.addCleanup(cu._reset_cache)
        self.origin = sm.record_event(
            'group_message', session='group:771177', actor_qq='22001',
            visibility='group', payload={'text': '我八抽出货了'})

    async def test_the_share_to_master_names_the_topic_that_made_her_curious(self):
        import curiosity as cu
        cu.push_share('猫为什么打呼噜', detail='因为喉咙在震动', kind='know',
                      event_ids=[self.origin])
        send = AsyncMock(return_value=True)
        with patch.object(b, 'CURIOSITY_SHARE_ENABLED', True), \
                patch.object(b, 'MASTER_QQ_IDS', {'1000000001'}), \
                patch.object(b, '_resolve_ws', lambda ws: object()), \
                patch.object(cu, 'share_block_reason', lambda **kw: ''), \
                patch.object(b, 'call_openclaw',
                             new=AsyncMock(return_value='猫打呼噜是喉咙在震呢')), \
                patch.object(b, 'send_private_msg', new=send):
            self.assertTrue(await b._curiosity_share(cu, object()))
        self.assertEqual(send.await_args.kwargs['evidence'], [self.origin])

    async def test_the_friend_dm_uses_his_own_group_message_as_its_basis(self):
        """线上事故的反面：她说「找阿甲聊」，依据必须就是阿甲在群里那句。"""
        from collections import deque
        b.group_message_cache['771177'] = deque([
            {"seq": 1, "ts": time.time(), "user_id": "22001", "nickname": "阿甲",
             "text": "我八抽出货了", "is_bot": False, "event_id": self.origin}], maxlen=50)
        import proactive_dm
        send = AsyncMock(return_value=True)
        with patch.object(b, 'send_private_msg', new=send), \
                patch.object(b, '_resolve_ws', lambda ws: object()), \
                patch.object(b, '_dm_private_history', new=AsyncMock(return_value='')), \
                patch.object(b, 'call_openclaw',
                             new=AsyncMock(return_value='你运气真好，分我点欧气')), \
                patch.object(b, '_world_note'):
            self.assertTrue(await b._send_friend_dm(
                object(), '22001', '阿甲', '他刚在群里说抽卡出货', proactive_dm))
        self.assertEqual(send.await_args.kwargs['evidence'], [self.origin])


class SelfViewEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """「什么改变了我」要能指回具体那几句对话，不能只指着「整点整理过一次」。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='selfview_ev_', ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {
            'DATA_DIR': root, 'PERSONA_DATA_DIR': root, 'QQ_DRY_RUN': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = root

    async def _consolidate(self, events):
        import persona_growth as pg
        ev = events[0]
        pg.note_exchange('group:771177', '我八抽出货了', '我酸了', events=[ev])

        async def llm(prompt):
            return json.dumps({"self_view": "我酸的时候会直接说出口"})

        with patch.object(pg, 'CONSOLIDATE_MIN_ITEMS', 1):
            out = await pg.consolidate(llm)
        self.assertTrue(out.get("ok"), out)
        return ev

    async def test_a_revision_lists_the_conversations_behind_it(self):
        ev = sm.record_event('group_message', session='group:771177', actor_qq='22001',
                             visibility='group', payload={'text': '我八抽出货了'})
        await self._consolidate([ev])
        rev = sm.self_view_history()[0]
        self.assertIn(ev, json.loads(rev['event_ids'] or '[]'))

    async def test_the_audit_no_longer_calls_that_revision_aggregate_only(self):
        import os as _os
        from pathlib import Path
        import phase_audit
        ev = sm.record_event('group_message', session='group:771177', actor_qq='22001',
                             visibility='group', payload={'text': '我八抽出货了'})
        await self._consolidate([ev])
        got = phase_audit._readiness_cognition(
            Path(_os.environ['PERSONA_DATA_DIR']) / 'cognition.db',
            time.time(), 86400)['self_view_grounding']
        self.assertEqual(got['total'], 1)
        self.assertEqual(got['with_direct_sources'], 1)
        self.assertEqual(got['aggregate_only'], 0)


class BeliefGroundingTests(unittest.IsolatedAsyncioTestCase):
    """她整理时真学到的东西，要写成有依据的认知信念，不是只进 persona.db。

    线上 belief_grounding 永远 not_observed：203 条信念全是 legacy，
    belief_evidence=0 —— 因为整理吸收的事实/喜好/经历从不写认知账本。
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='belief_ev_', ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {
            'DATA_DIR': root, 'PERSONA_DATA_DIR': root, 'QQ_DRY_RUN': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = root

    async def _consolidate(self, events, got):
        import persona_growth as pg
        pg.note_exchange('group:771177', '猫为什么打呼噜', '因为喉咙在震呢', events=events)

        async def llm(prompt):
            return json.dumps(got)

        with patch.object(pg, 'CONSOLIDATE_MIN_ITEMS', 1):
            out = await pg.consolidate(llm)
        self.assertTrue(out.get('ok'), out)

    def _nonlegacy_beliefs(self):
        from contextlib import closing
        with closing(sm._db()) as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM belief WHERE is_legacy=0").fetchall()]

    async def test_absorbing_a_fact_with_real_sources_writes_a_grounded_belief(self):
        ev = sm.record_event('group_message', session='group:771177', actor_qq='22001',
                             visibility='group', payload={'text': '猫为什么打呼噜'})
        await self._consolidate([ev], {'facts': ['猫打呼噜是因为喉咙在震动']})
        beliefs = self._nonlegacy_beliefs()
        self.assertTrue(beliefs, '学到的事实没写进认知账本，belief_grounding 永远空')
        self.assertEqual(beliefs[0]['status'], 'active')
        from contextlib import closing
        with closing(sm._db()) as c:
            refs = [r['event_id'] for r in c.execute(
                "SELECT event_id FROM belief_evidence WHERE belief_id=?",
                (beliefs[0]['id'],)).fetchall()]
        self.assertIn(ev, refs, '信念必须挂真实来源事件，不能凭空断言')
        import phase_audit
        got = phase_audit._readiness_cognition(
            Path(self.root) / 'cognition.db', time.time(), 86400)['belief_grounding']
        self.assertGreaterEqual(got['nonlegacy'], 1)
        self.assertEqual(got['with_existing_evidence'], got['nonlegacy'])

    async def test_no_source_events_means_no_ungrounded_belief(self):
        # 没有真实来源事件时，事实仍可进 persona.db，但绝不写无依据的认知信念。
        await self._consolidate([], {'facts': ['猫打呼噜是因为喉咙在震动']})
        self.assertEqual(self._nonlegacy_beliefs(), [],
                         '没有证据就造信念，那是编生平')


class IngestWiringTests(unittest.TestCase):
    """入站那一头：用真的记账函数，而不是手工往触发账本里塞。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ingest_ev_', ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {
            'DATA_DIR': root, 'PERSONA_DATA_DIR': root, 'QQ_DRY_RUN': '1',
            'TRIGGER_LEDGER_TTL': '900'})
        environment.start()
        self.addCleanup(environment.stop)
        tl.reset()
        self.addCleanup(tl.reset)

    def _inbound(self, text='今天热化了', mid=555):
        event = {"group_id": "771177", "user_id": "22001", "self_id": "999",
                 "sender": {"card": "阿甲"}, "message_id": mid, "time": time.time(),
                 "message": [{"type": "text", "data": {"text": text}}]}
        b.record_group_message(event, text, [])
        return event

    def test_an_inbound_group_message_becomes_the_trigger_for_what_she_says_next(self):
        self._inbound()
        ids = tl.pending('group:771177')
        self.assertEqual(len(ids), 1)
        from contextlib import closing
        with closing(sm._db()) as c:
            kind = c.execute("SELECT kind FROM event WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual(kind['kind'], 'group_message')

    def test_the_cache_row_carries_the_same_event_id(self):
        """主动私聊那条路靠这个对应关系，不再拿文本去猜是哪句话。"""
        self._inbound()
        row = list(b.group_message_cache['771177'])[-1]
        self.assertEqual(row['event_id'], tl.pending('group:771177')[0])

    def test_the_same_message_replayed_is_still_one_trigger(self):
        self._inbound()
        self._inbound()
        self.assertEqual(len(tl.pending('group:771177')), 1)

    def test_a_group_message_addressed_to_her_with_an_explicit_preference_is_grounded(self):
        """群里明确对她说「我喜欢X」也要落地成有证据的信念。
        线上 203 条信念全是 legacy，就是因为只有私聊那条路接了自述摄取。"""
        event = {"group_id": "771177", "user_id": "22001", "self_id": "999",
                 "sender": {"card": "阿甲"}, "message_id": 777, "time": time.time(),
                 "message": [{"type": "at", "data": {"qq": "999"}},
                             {"type": "text", "data": {"text": "我喜欢猫"}}]}
        b.record_group_message(event, "我喜欢猫", [])
        from contextlib import closing
        with closing(sm._db()) as c:
            row = c.execute("SELECT id, is_legacy FROM belief "
                            "WHERE source='explicit_self_report'").fetchone()
        self.assertIsNotNone(row, '群聊明确自述没写进认知账本')
        self.assertEqual(row['is_legacy'], 0)
        trigger = tl.pending('group:771177')[0]
        with closing(sm._db()) as c:
            refs = [r['event_id'] for r in c.execute(
                "SELECT event_id FROM belief_evidence WHERE belief_id=?",
                (row['id'],)).fetchall()]
        self.assertIn(trigger, refs, '信念必须挂她真看到的那条群消息')

    def test_an_ambient_group_preference_not_addressed_to_her_is_not_ingested(self):
        """没对她说的闲聊不能当成对她的自述 —— 否则信念会被环境噪声灌满。"""
        self._inbound(text='我喜欢猫', mid=778)
        from contextlib import closing
        with closing(sm._db()) as c:
            n = c.execute("SELECT COUNT(*) AS n FROM belief "
                          "WHERE source='explicit_self_report'").fetchone()['n']
        self.assertEqual(n, 0)


class ColdOpenerEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """冷场主动起话题：依据是「这个群安静了 N 分钟」这条真实观察。

    以前 _start_topic_locked 算出了 _idle_min 却只塞进提示词，发出去那句
    不带任何 evidence —— 线上 3 条生成式群开场白就是这么变成空证据的。
    观察必须落账、并且就是 send_group_msg 的依据，不能事后编一条。
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='opener_ev_', ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        environment = patch.dict(os.environ, {
            'DATA_DIR': root, 'PERSONA_DATA_DIR': root, 'QQ_DRY_RUN': '1'})
        environment.start()
        self.addCleanup(environment.stop)
        saved = b.group_message_cache.get('771177')
        self.addCleanup(lambda: b.group_message_cache.__setitem__('771177', saved)
                        if saved is not None else b.group_message_cache.pop('771177', None))

    async def test_the_cold_opener_links_the_real_idle_observation(self):
        from collections import deque
        b.group_message_cache['771177'] = deque([
            {"seq": 1, "ts": time.time() - 1200, "user_id": "22001",
             "nickname": "阿甲", "text": "先下了", "is_bot": False}], maxlen=50)
        send = AsyncMock(return_value=True)
        with patch.object(b, 'call_openclaw', new=AsyncMock(return_value='群里好安静呀')), \
                patch.object(b, 'send_group_msg', new=send):
            ok = await b._start_topic_locked(object(), '771177')
        self.assertTrue(ok)
        self.assertEqual(send.await_count, 1, '开场白没真发出去，setup 有问题')
        ev = send.await_args.kwargs.get('evidence')
        self.assertTrue(ev, '冷场开话题没带依据，_idle_min 算了又丢')
        from contextlib import closing
        with closing(sm._db()) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM event WHERE kind='idle_topic'").fetchall()]
        self.assertEqual(len(rows), 1, '一次开场白只该落一条冷场观察')
        row = rows[0]
        self.assertEqual(ev, [row['id']], '发出去那句的依据必须就是这条观察')
        self.assertEqual(row['source'], 'idle_topic_worker')
        self.assertEqual(row['session'], 'group:771177')
        self.assertGreater(json.loads(row['payload'])['idle_minutes'], 0)
        self.assertTrue(sm.evidence_valid('event', row['id']))


if __name__ == '__main__':
    unittest.main()
