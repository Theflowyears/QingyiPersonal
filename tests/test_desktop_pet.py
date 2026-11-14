import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))

import brain as BR
import sprites as SP
import store as ST
import voice as VOICE
import tempfile

import qingyi_desktop as QD
import plan as PL
import runner as RN
from discovery import Connection


class Sprites(unittest.TestCase):
    # 桌面端会切到的状态，一个都不能缺帧
    USED = ["idle", "talk", "think", "working", "done", "fail", "file",
            "confirm", "browse", "sleep", "drag", "yawn"]

    def test_fish_skin_covers_every_state_the_app_uses(self):
        for state in self.USED:
            self.assertGreaterEqual(len(SP.frames(state, skin="fish")), 60,
                                    f"{state} 的清漪帧不够动画")

    def test_fish_frames_really_are_transparent(self):
        from PIL import Image
        p = SP.frames("idle", skin="fish")[0]
        im = Image.open(p)
        self.assertIn("A", im.getbands())
        hist = im.getchannel("A").histogram()
        ratio = sum(hist[:16]) / (im.width * im.height)
        self.assertGreater(ratio, 0.5, f"背景没扣干净，透明占比只有 {ratio:.0%}")

    def test_manifest_names_the_animation_each_state_came_from(self):
        m = SP._manifest()
        self.assertEqual(m["working"]["source"], "工作状态-忙碌点按")
        self.assertEqual(m["done"]["source"], "工作状态-雀跃庆祝")
        self.assertGreaterEqual(m["idle"]["frames"], 60)
        # 全帧原速：delay_ms ≈ 源时长/帧数，不再人为放慢成幻灯片
        self.assertTrue(16 <= SP.delay_ms("idle") <= 120)
        self.assertAlmostEqual(
            SP.delay_ms("idle"),
            round(m["idle"]["source_seconds"] * 1000 / m["idle"]["frames"]), delta=2)

    def test_maid_skin_is_kept_as_a_second_option(self):
        self.assertTrue(SP.frames("working", 245, "maid"))
        self.assertTrue(SP.frames("done", 245, "maid"))

    def test_unknown_state_falls_back_to_idle_instead_of_blank(self):
        self.assertEqual([p.name for p in SP.frames("没有这个状态", skin="fish")],
                         [p.name for p in SP.frames("idle", skin="fish")])

    def test_missing_size_degrades_to_the_nearest_one(self):
        self.assertEqual(SP.pick_size(200), 245)
        self.assertEqual(SP.pick_size(60), 122)
        self.assertTrue(SP.frames("done", 999, "maid"))

    def test_asset_provenance_is_written_down(self):
        root = Path(__file__).resolve().parents[1] / 'desktop' / 'assets'
        self.assertTrue((root / 'SOURCE.txt').exists())
        self.assertTrue((root / 'LICENSE.deepseek-dafeiyu-pet').exists())
        text = (root / 'SOURCE.txt').read_text(encoding='utf-8')
        self.assertIn('个人使用', text)


class FakeStore:
    def __init__(self, allowed=True, why=''):
        self.allowed, self.why = allowed, why
        self.noted = 0
        self.asked = 0

    def can_bubble(self, now=None):
        self.asked += 1
        return self.allowed, self.why

    def note_bubble(self, now=None):
        self.noted += 1


class BrainRules(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.b = BR.Brain(self.store, [], now=lambda: self.at)
        self.at = 0.0

    def test_no_proposal_before_noon(self):
        self.at = __import__('time').mktime(
            __import__('time').strptime('2026-11-08 09:00:00', '%Y-%m-%d %H:%M:%S'))
        self.assertIsNone(self.b.tick())
        self.assertIn('9 点', self.b.last_reason)

    def test_proposes_after_noon_when_nothing_ran(self):
        self.at = __import__('time').mktime(
            __import__('time').strptime('2026-11-08 15:00:00', '%Y-%m-%d %H:%M:%S'))
        ut = self.b.tick()
        self.assertIsNotNone(ut)
        self.assertTrue(ut.proactive)
        # 配额是「问一句」，扣额度由 Controller 在真正展示气泡时处理
        self.assertEqual(self.store.asked, 1)
        self.assertEqual(self.store.noted, 0)

    def test_quota_denial_is_reported_with_its_reason(self):
        self.store = FakeStore(allowed=False, why='今天已经主动开口 6 次')
        self.b = BR.Brain(self.store, [], now=lambda: self.at)
        self.at = __import__('time').mktime(
            __import__('time').strptime('2026-11-08 15:00:00', '%Y-%m-%d %H:%M:%S'))
        self.assertIsNone(self.b.tick())
        self.assertIn('主动开口 6 次', self.b.last_reason)

    def test_stops_nagging_once_a_run_succeeded_today(self):
        self.at = __import__('time').mktime(
            __import__('time').strptime('2026-11-08 15:00:00', '%Y-%m-%d %H:%M:%S'))
        self.b.ledger_rows = [{'event': 'run_finished', 'at': self.at, 'state': 'done'}]
        self.assertIsNone(self.b.tick())
        self.assertIn('done', self.b.last_reason)

    def test_a_failed_run_today_does_not_count_as_cleared(self):
        self.at = __import__('time').mktime(
            __import__('time').strptime('2026-11-08 15:00:00', '%Y-%m-%d %H:%M:%S'))
        self.b.ledger_rows = [{'event': 'run_finished', 'at': self.at, 'state': 'fault'}]
        self.assertIsNotNone(self.b.tick())

    def test_blocked_gates_always_speak_up(self):
        ut = self.b.on_gate_blocked(['MAA 界面正在占用这台模拟器'])
        self.assertEqual(ut.state, 'confirm')
        self.assertFalse(ut.proactive)
        self.assertIn('MAA 界面', ut.situation)
        self.assertIsNone(self.b.on_gate_blocked([]))

    def test_summary_only_uses_numbers_the_ledger_actually_has(self):
        text = BR.summarize_run({'state': 'done', 'why': 'AllTasksCompleted', 'chains': [
            {'task': 'Fight', 'state': 'done', 'times_finished': 6,
             'sanity_before': 120, 'sanity_after': 12, 'seconds': 540, 'error': ''},
            {'task': 'Mall', 'state': 'done', 'times_finished': 0, 'sanity_before': None,
             'sanity_after': None, 'seconds': 0, 'error': ''}]})
        self.assertIn('6 局', text)
        self.assertIn('120→12', text)
        self.assertIn('Mall', text)
        self.assertNotIn('None', text)

    def test_summary_retains_snapshot_notes_and_every_gate_cause(self):
        text = BR.summarize_run({'state': 'fault', 'chains': [],
                                 'notes': ['连不上模拟器', '没有连接回执']})
        self.assertIn('连不上模拟器', text)
        self.assertIn('没有连接回执', text)
        text = BR.summarize_run({'state': 'fault', 'why': '排队失败；参数没接受'})
        self.assertIn('排队失败', text)
        self.assertIn('参数没接受', text)


class ControllerRules(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctrl = object.__new__(QD.Controller)  # 不启动应用、计时器或设备探测
        self.ctrl.store = ST.Store(Path(tmp.name) / 'state')
        self.ctrl.logs = []
        self.ctrl.log = self.ctrl.logs.append
        self.ctrl.brain = BR.Brain(self.ctrl.store, [])
        self.ctrl.last_say = ('', '', '')
        self.shown = []
        self.ctrl.pet = types.SimpleNamespace(say=lambda lines, state:
                                              self.shown.append((lines, state)))
        self.jobs = []
        self.ctrl.pool = types.SimpleNamespace(start=self.jobs.append)
        self.ctrl.sink = QD._Speak()
        self.ctrl.sink.done.connect(self.ctrl._on_said)
        self.ctrl.conn = Connection(adb_path='fake-adb')
        self.ctrl.intents = [PL.Intent('Fight', '作战', {'medicine': 1}, tier=PL.CONFIRM),
                             PL.Intent('Roguelike', '肉鸽', {'starts_count': 9}, tier=PL.MANUAL)]

    def complete(self, job, lines=None):
        lines = ['现在有话说'] if lines is None else lines
        with patch.object(VOICE, 'say', return_value=(lines, '模型')):
            job.run()

    def test_approval_matches_only_the_executable_daily_plan(self):
        fp = self.ctrl.approve()
        self.assertEqual(fp, PL.fingerprint([self.ctrl.intents[0]]))
        self.assertEqual(set(self.ctrl.store.approved()), {'Fight'})
        ok, blocked = RN.check_gates(self.ctrl.store, self.ctrl.conn,
                                     PL.daily_plan(self.ctrl.intents), gui_probe=lambda: [])
        self.assertTrue(ok, blocked)

    def test_failed_gate_keeps_spending_approval_and_delivers_requested_notice(self):
        self.ctrl.approve()
        fp = self.ctrl.store.fingerprint()
        with patch.object(RN, 'check_gates', return_value=(False, ['设备没起来'])):
            self.ctrl._run_worker(PL.daily_plan(self.ctrl.intents), fp)
        self.ctrl.mute_today()
        self.complete(self.jobs[0])
        self.assertEqual(self.ctrl.store.fingerprint(), fp)
        self.assertEqual(self.shown, [(['现在有话说'], 'confirm')])
        self.assertEqual(self.ctrl.store['bubble_count'], 0)

    def test_proactive_quota_is_charged_when_displayed_not_when_dispatched(self):
        self.ctrl.ask(BR.Utterance('talk', '想提醒日常', '提醒'))
        self.assertEqual(self.ctrl.store['bubble_count'], 0)
        self.complete(self.jobs[0])
        self.assertEqual(self.shown, [(['现在有话说'], 'talk')])
        self.assertEqual(self.ctrl.store['bubble_count'], 1)

    def test_mute_today_cancels_inflight_proactive_speech(self):
        self.ctrl.ask(BR.Utterance('talk', '想提醒日常', '提醒'))
        self.ctrl.mute_today()
        self.complete(self.jobs[0])
        self.assertEqual(self.shown, [])
        self.assertEqual(self.ctrl.last_say, ('', '', ''))
        self.assertEqual(self.ctrl.store['bubble_count'], 0)
        self.assertIn('别吵', self.ctrl.brain.last_reason)

    def test_display_rechecks_both_daily_quota_and_minimum_gap(self):
        for count in (1, ST.BUBBLE_LIMIT):
            with self.subTest(count=count):
                self.ctrl.store.set(bubble_day='', bubble_count=0, bubble_last_at=0)
                self.ctrl.ask(BR.Utterance('talk', '想提醒日常', '提醒'))
                for _ in range(count):
                    self.ctrl.store.note_bubble()
                self.complete(self.jobs[-1])
                self.assertEqual(self.shown, [])
                self.assertEqual(self.ctrl.store['bubble_count'], count)

    def test_queued_proactive_results_cannot_spend_the_same_quota_slot(self):
        self.ctrl.ask(BR.Utterance('talk', '提醒甲', '提醒'))
        self.ctrl.ask(BR.Utterance('talk', '提醒乙', '提醒'))
        self.assertEqual(len(self.jobs), 2)
        self.complete(self.jobs[0])
        self.complete(self.jobs[1])
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(self.ctrl.store['bubble_count'], 1)

    def test_requested_gate_and_result_notices_bypass_mute_and_quota(self):
        self.ctrl.mute_today()
        for _ in range(ST.BUBBLE_LIMIT):
            self.ctrl.store.note_bubble()
        for state in ('confirm', 'fail', 'done'):
            self.ctrl.ask(BR.Utterance(state, '请求的通知', '跑单通知', False))
            self.complete(self.jobs[-1])
        self.assertEqual([state for _, state in self.shown], ['confirm', 'fail', 'done'])
        self.assertEqual(self.ctrl.store['bubble_count'], ST.BUBBLE_LIMIT)

    def test_model_silence_cannot_hide_requested_gate_or_result_notice(self):
        fake = types.ModuleType('llm_router')
        async def call_text(messages, **kwargs):
            return '[[沉默]]'
        fake.call_text = call_text
        self.ctrl.mute_today()
        with patch.object(VOICE, '_model_available', return_value=True), \
                patch.object(VOICE, '_persona', return_value='我'), \
                patch.dict(sys.modules, {'llm_router': fake}):
            for state in ('confirm', 'fail', 'done'):
                self.ctrl.ask(BR.Utterance(state, '请求的通知', '跑单通知', False))
                self.jobs[-1].run()
        self.assertEqual([state for _, state in self.shown], ['confirm', 'fail', 'done'])
        self.assertTrue(all(lines and '[[沉默]]' not in ''.join(lines)
                            for lines, _ in self.shown))
        self.assertEqual(self.ctrl.store['bubble_count'], 0)

    def test_intentional_silence_does_not_replace_bubble_or_charge_quota(self):
        self.ctrl.ask(BR.Utterance('talk', '没必要就不说', '提醒'))
        self.complete(self.jobs[0], lines=[])
        self.assertEqual(self.shown, [])
        self.assertEqual(self.ctrl.last_say, ('', '', ''))
        self.assertEqual(self.ctrl.store['bubble_count'], 0)


class VoiceFallback(unittest.TestCase):
    def test_no_key_says_so_and_still_produces_lines(self):
        with patch.object(VOICE, '_model_available', lambda: False):
            lines, source = VOICE.say('今天还没清日常', 'talk')
        self.assertTrue(lines)
        self.assertIn('没配模型', source)

    def test_model_failure_is_named_not_hidden(self):
        fake = types.ModuleType('llm_router')

        def get_text_model():
            class C: api_key = 'k'; base_url = 'u'; model = 'm'
            return C()

        async def call_text(messages, max_tokens=90, timeout=20):
            raise RuntimeError('连不上 api')
        fake.get_text_model = get_text_model
        fake.call_text = call_text
        with patch.object(VOICE, '_model_available', lambda: True), \
                patch.dict(sys.modules, {'llm_router': fake}):
            lines, source = VOICE.say('跑完了', 'done')
        self.assertTrue(lines)
        self.assertIn('模型调用失败', source)

    def test_long_or_empty_lines_are_dropped_and_two_is_the_cap(self):
        fake = types.ModuleType('llm_router')

        def get_text_model():
            class C: api_key = 'k'; base_url = 'u'; model = 'm'
            return C()

        async def call_text(messages, max_tokens=90, timeout=20):
            return '这条没事\n' + '长' * 60 + '\n第二条\n第三条\n\n'
        fake.get_text_model = get_text_model
        fake.call_text = call_text
        with patch.object(VOICE, '_model_available', lambda: True), \
                patch.dict(sys.modules, {'llm_router': fake}):
            lines, source = VOICE.say('跑完了', 'done')
        self.assertEqual(source, '模型')
        self.assertEqual(lines, ['这条没事', '第二条'])

    def test_silence_markers_are_an_empty_result_not_a_displayed_or_fallback_line(self):
        fake = types.ModuleType('llm_router')
        for marker in ('[[不说]]', '[[沉默]]', '「[[不说]]」', '[[沉默]]\n本来想说的话'):
            with self.subTest(marker=marker):
                async def call_text(messages, **kwargs):
                    return marker
                fake.call_text = call_text
                with patch.object(VOICE, '_model_available', return_value=True), \
                        patch.object(VOICE, '_persona', return_value='我'), \
                        patch.dict(sys.modules, {'llm_router': fake}):
                    lines, source = VOICE.say('没有必要可以不说', 'talk')
                self.assertEqual(lines, [])
                self.assertIn('沉默', source)

    def test_model_rules_explicitly_ban_unobserved_real_life_claims(self):
        fake = types.ModuleType('llm_router')
        messages_seen = []
        async def call_text(messages, **kwargs):
            messages_seen.extend(messages)
            return '我在呢'
        fake.call_text = call_text
        with patch.object(VOICE, '_model_available', return_value=True), \
                patch.object(VOICE, '_persona', return_value='我'), \
                patch.dict(sys.modules, {'llm_router': fake}):
            VOICE.say('在桌面待机')
        system = messages_seen[0]['content']
        for rule in ('未经观察', '吃饭', '睡觉', '现实经历', '[[不说]]', '[[沉默]]'):
            self.assertIn(rule, system)

    def test_persona_is_pulled_from_the_same_file_the_qq_side_uses(self):
        # 意图是「同源」：桌宠和 QQ 侧读同一个人格引擎，
        # 不断言具体字词（人格是数据，仓库白板后默认人格里没有'我'字口癖）。
        import whale_persona
        self.assertEqual(VOICE._persona(), whale_persona.build_persona_block())


class StorePersistence(unittest.TestCase):
    def test_state_round_trips_through_disk(self):
        tmp = tempfile.mkdtemp()
        s = ST.Store(Path(tmp) / 'state')
        s.set(maa_dir=r'E:\MAA-v5.23.2-win-x64', pet_size=306)
        again = ST.Store(Path(tmp) / 'state')
        self.assertEqual(again['maa_dir'], r'E:\MAA-v5.23.2-win-x64')
        self.assertEqual(again['pet_size'], 306)

    def test_a_corrupt_state_file_is_survivable_not_fatal(self):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / 'state'
        p.mkdir(parents=True)
        (p / 'config.json').write_text('{坏了一半', encoding='utf-8')
        s = ST.Store(p)
        self.assertEqual(s.fingerprint(), '')


if __name__ == '__main__':
    unittest.main()
