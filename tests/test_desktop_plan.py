import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))

import plan as PL
import store as ST
import runner as RN
from discovery import Connection

# 形状照用户机器上 gui.new.json 的真实键名裁剪（MuMu + 端游官方客户端 + 已有日常队列）
GUI = {'Configurations': {'Default': {
    'Gui': {'ThirdParty': {'ReportToPenguin': True, 'ReportToYituliu': True}},
    'TaskQueue': [
        {'$type': 'StartUpTask', 'TaskType': 'StartUp', 'Name': '开始任务', 'IsEnable': False},
        {'$type': 'RecruitTask', 'TaskType': 'Recruit', 'Name': '自动公招', 'IsEnable': True,
         'MaxTimes': 110, 'ExtraTagMode': 2, 'Level3PreferTags': [], 'PreferTagEnabled': True,
         'PreserveTagList': ['支援机械'], 'PreserveTagEnabled': True, 'RefreshLevel3': True,
         'ForceRefresh': True, 'Level3Choose': True, 'Level4Choose': True, 'Level5Choose': False,
         'Level6Choose': False, 'Level3Time': 540, 'Level4Time': 540,
         'Level3RecruitmentPermitReserveEnabled': False},
        {'$type': 'InfrastTask', 'TaskType': 'Infrast', 'Name': '基建维护', 'IsEnable': False,
         'Mode': 'Normal', 'UsesOfDrones': 'Money', 'DormThreshold': 30,
         'DormTrustEnabled': False, 'OriginiumShardAutoReplenishment': True,
         'DormFilterNotStationed': True, 'ReceptionMessageBoard': True,
         'ReceptionClueExchange': True, 'SendClue': True, 'ContinueTraining': False,
         'CustomFileType': 'user_defined', 'Filename': '', 'PlanSelect': -1,
         'RoomList': [{'Room': 'Mfg'}, {'Room': 'Trade'}, {'Room': 'Control'},
                      {'Room': 'Power'}, {'Room': 'Reception'}, {'Room': 'Office'},
                      {'Room': 'Dorm'}, {'Room': 'Processing'}, {'Room': 'Training'}]},
        {'$type': 'FightTask', 'TaskType': 'Fight', 'Name': '理智作战', 'IsEnable': False,
         'UseMedicine': False, 'MedicineCount': 999, 'UseStone': False, 'StoneCount': 0,
         'EnableTimesLimit': False, 'TimesLimit': 5, 'Series': 0, 'IsDrGrandet': False,
         'UseExpiringMedicine': False, 'MedicineExpireDays': 2, 'StagePlan': ['1-7'],
         'HideUnavailableStage': True, 'IsStageManually': False, 'StageResetMode': 'Current'},
        {'$type': 'MallTask', 'TaskType': 'Mall', 'Name': '信用收支', 'IsEnable': False,
         'Shopping': True, 'CreditFight': False, 'VisitFriends': True,
         'FirstList': '招聘许可', 'BlackList': '碳;家具;加急许可',
         'ShoppingIgnoreBlackListWhenFull': False, 'OnlyBuyDiscount': False,
         'ReserveMaxCredit': False},
        {'$type': 'AwardTask', 'TaskType': 'Award', 'Name': '领取奖励', 'IsEnable': False,
         'Award': True, 'Mail': False, 'FreeGacha': False, 'Orundum': False,
         'Mining': False, 'SpecialAccess': False, 'SignInEvent': False},
        {'$type': 'RoguelikeTask', 'TaskType': 'Roguelike', 'Name': '自动肉鸽', 'IsEnable': False,
         'Theme': 'JieGarden', 'Difficulty': 12, 'Mode': 'Investment', 'StartCount': 99999,
         'Investment': True, 'InvestCount': 999},
    ]}}}

CONN = Connection(adb_path=r'E:\YXArkNights-12.0\shell\adb.exe', address='127.0.0.1:16384',
                  config='MuMuEmulator12', touch_mode='MumuExtras', client_type='Official')


def pick(intents, task):
    return next(i for i in intents if i.task == task)


class InheritFromMaaConfig(unittest.TestCase):
    def setUp(self):
        self.intents = PL.build_intents(CONN, GUI)

    def test_fight_stage_comes_from_the_users_stage_plan_and_costs_nothing(self):
        f = pick(self.intents, 'Fight')
        self.assertEqual(f.params['stage'], '1-7')
        self.assertEqual(f.params['medicine'], 0)
        self.assertEqual(f.params['stone'], 0)
        self.assertEqual(f.tier, PL.AUTO)
        self.assertEqual(f.spends, [])
        self.assertEqual(f.prov['stage'], PL.CONFIRMED)

    def test_medicine_switch_is_translated_to_a_count_and_escalates_the_tier(self):
        gui = json.loads(json.dumps(GUI))
        for t in gui['Configurations']['Default']['TaskQueue']:
            if t['TaskType'] == 'Fight':
                t['UseMedicine'] = True
                t['MedicineCount'] = 3
        f = pick(PL.build_intents(CONN, gui), 'Fight')
        self.assertEqual(f.params['medicine'], 3)
        self.assertEqual(f.tier, PL.CONFIRM)
        self.assertIn('medicine', f.spends)

    def test_expiring_medicine_is_spending_and_requires_confirmation(self):
        gui = json.loads(json.dumps(GUI))
        fight = next(t for t in gui['Configurations']['Default']['TaskQueue']
                     if t['TaskType'] == 'Fight')
        fight.update(UseExpiringMedicine=True, MedicineCount=2)
        f = pick(PL.build_intents(CONN, gui), 'Fight')
        self.assertEqual(f.spends, ['expiring_medicine'])
        self.assertEqual(f.tier, PL.CONFIRM)
        self.assertNotIn('不吃药', f.note)

    def test_every_spend_key_escalates_auto_but_preserves_manual(self):
        for task, keys in PL.SPEND_KEYS.items():
            for key in keys:
                with self.subTest(task=task, key=key):
                    self.assertEqual(PL.Intent(task, task, {key: 1}).tier, PL.CONFIRM)
                    self.assertEqual(PL.Intent(task, task, {key: True}).tier, PL.CONFIRM)
                    self.assertEqual(PL.Intent(task, task, {key: 0}).tier, PL.AUTO)
                    self.assertEqual(PL.Intent(task, task, {key: 1}, tier=PL.MANUAL).tier,
                                     PL.MANUAL)

    def test_recruit_level_choices_are_marked_as_inferred_not_as_fact(self):
        r = pick(self.intents, 'Recruit')
        self.assertEqual(r.params['select'], [3, 4])
        self.assertEqual(r.params['confirm'], [3, 4])
        self.assertEqual(r.prov['select'], PL.INFERRED)
        self.assertEqual(r.params['preserve_tags'], ['支援机械'])
        self.assertEqual(r.prov['preserve_tags'], PL.CONFIRMED)
        self.assertEqual(r.params['recruitment_time'], {'3': 540, '4': 540})
        self.assertNotIn('expedite', r.params)  # 加急许可永远不默认打开

    def test_mall_lists_are_split_the_way_maa_stores_them(self):
        m = pick(self.intents, 'Mall')
        self.assertEqual(m.params['blacklist'], ['碳', '家具', '加急许可'])
        self.assertEqual(m.params['buy_first'], ['招聘许可'])
        self.assertTrue(m.params['shopping'])

    def test_infrast_carries_every_room_the_user_listed(self):
        i = pick(self.intents, 'Infrast')
        self.assertEqual(len(i.params['facility']), 9)
        self.assertEqual(i.params['drones'], 'Money')
        self.assertEqual(i.params['threshold'], 30)

    def test_award_does_not_touch_gacha_when_the_config_says_no(self):
        a = pick(self.intents, 'Award')
        self.assertFalse(a.params['orundum'])
        self.assertEqual(a.tier, PL.AUTO)

    def test_roguelike_is_listed_but_never_scheduled(self):
        r = pick(self.intents, 'Roguelike')
        self.assertEqual(r.tier, PL.MANUAL)
        self.assertNotIn(r, PL.daily_plan(self.intents))

    def test_no_whitelisted_task_can_do_the_forbidden_things(self):
        names = {i.task for i in self.intents}
        self.assertTrue(names <= set(PL.DAILY_ORDER) | {'Roguelike'})
        for word in PL.FORBIDDEN:
            self.assertNotIn(word, ' '.join(names))


class PlanIdentity(unittest.TestCase):
    def test_fingerprint_moves_when_a_spend_param_moves(self):
        a = PL.build_intents(CONN, GUI)
        gui = json.loads(json.dumps(GUI))
        for t in gui['Configurations']['Default']['TaskQueue']:
            if t['TaskType'] == 'Fight':
                t['UseStone'] = True
                t['StoneCount'] = 1
        b = PL.build_intents(CONN, gui)
        self.assertNotEqual(PL.fingerprint(a), PL.fingerprint(b))
        self.assertEqual(PL.fingerprint(a), PL.fingerprint(a))

    def test_changed_keys_names_the_param_not_the_task_only(self):
        a = PL.build_intents(CONN, GUI)
        approved = {i.task: dict(i.params) for i in a}
        approved['Fight']['medicine'] = 5
        out = PL.changed_keys(a, approved)
        self.assertTrue(any('Fight' in x and 'medicine' in x for x in out))


class ApprovedPlanStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ST.Store(Path(self.tmp) / 'state')

    def test_approve_records_both_the_fingerprint_and_the_params(self):
        intents = PL.build_intents(CONN, GUI)
        fp = self.store.approve(intents)
        self.assertEqual(fp, PL.fingerprint(intents))
        self.assertEqual(self.store.approved()['Fight']['stage'], '1-7')
        again = ST.Store(Path(self.tmp) / 'state')
        self.assertEqual(again.fingerprint(), fp)

    def test_bubble_quota_is_six_a_day_with_a_twenty_minute_gap(self):
        # 固定在一个中午，避免跑测试的时间点跨天使配额判断假失败
        now = time.mktime(time.strptime('2026-11-08 12:00:00', '%Y-%m-%d %H:%M:%S'))
        ok, _ = self.store.can_bubble(now)
        self.assertTrue(ok)
        self.store.note_bubble(now)
        self.assertFalse(self.store.can_bubble(now + 60)[0])
        ok, why = self.store.can_bubble(now + 1300)
        self.assertTrue(ok, why)
        for k in range(5):
            self.store.note_bubble(now + 1300 * (k + 1))
        self.assertIn('主动开口', self.store.can_bubble(now + 1300 * 8)[1])
        # 隔天配额重置，但间隔仍然算
        self.assertTrue(self.store.can_bubble(now + 86400)[0])

    def test_quiet_mode_blocks_until_the_day_is_over(self):
        self.store.mute_today()
        ok, why = self.store.can_bubble()
        self.assertFalse(ok)
        self.assertIn('别吵', why)


class FakeCore:
    def __init__(self, connect_ok=True, connect_ret=True, version='v5.23.2'):
        self.connect_ok = connect_ok        # AsstAsyncConnect 的返回值：调用发出去了吗
        self.connect_ret = connect_ret      # AsyncCallInfo 的回执：真连上了吗
        self.calls = []
        self.queue = []
        self.stopped = False
        self.version = version

    def load(self, *a, **k):
        self.calls.append(('load',))
        return self.version

    def set_instance_option(self, k, v):
        self.calls.append(('option', k, v))
        return True

    def set_connection_extras(self, n, v):
        self.calls.append(('extras', n))

    def connect(self, adb, addr, cfg):
        self.calls.append(('connect', adb, addr, cfg))
        return self.connect_ok

    def connect_result(self, timeout=30.0):
        return self.connect_ret, '耗时 4241ms'

    def append_task(self, name, params):
        self.calls.append(('task', name, params))
        return len([c for c in self.calls if c[0] == 'task'])

    def start(self):
        self.calls.append(('start',))
        return True

    def stop(self):
        self.stopped = True
        return True

    def events(self, limit=200):
        out, self.queue = self.queue[:limit], self.queue[limit:]
        return out


class Gates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ST.Store(Path(self.tmp) / 'state')
        self.intents = PL.build_intents(CONN, GUI)

    def test_maa_gui_holding_the_device_blocks_the_run(self):
        ok, blocked = RN.check_gates(self.store, CONN, self.intents,
                                     gui_probe=lambda: ['MAA.exe'])
        self.assertFalse(ok)
        self.assertTrue(any('MAA 界面' in b for b in blocked))

    def test_spend_params_that_were_never_approved_block_the_run(self):
        gui = json.loads(json.dumps(GUI))
        for t in gui['Configurations']['Default']['TaskQueue']:
            if t['TaskType'] == 'Fight':
                t['UseMedicine'] = True
        intents = PL.build_intents(CONN, gui)
        ok, blocked = RN.check_gates(self.store, CONN, intents, gui_probe=lambda: [])
        self.assertFalse(ok)
        self.assertTrue(any('Fight' in b for b in blocked))
        self.store.approve(intents)
        ok2, _ = RN.check_gates(self.store, CONN, intents, gui_probe=lambda: [])
        self.assertTrue(ok2)

    def test_a_blocked_device_is_reported_by_name(self):
        ok, blocked = RN.check_gates(self.store, CONN, self.intents,
                                     device_probe=lambda: (False, '虚拟机没起来'),
                                     gui_probe=lambda: [])
        self.assertFalse(ok)
        self.assertTrue(any('虚拟机没起来' in b for b in blocked))


class RunFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.core = FakeCore()
        self.lines = []
        self.runner = RN.Runner(self.core, self.tmp / 'ledger.jsonl',
                                log=lambda s: self.lines.append(s))
        self.intents = PL.daily_plan(PL.build_intents(CONN, GUI))
        self.store = ST.Store(self.tmp / 'state')
        self.store.approve(self.intents)
        self.runner.store = self.store

    def test_start_loads_connects_then_queues_every_task_in_order(self):
        run = self.runner.start(self.intents, CONN, 'fp1')
        kinds = [c[0] for c in self.core.calls]
        self.assertEqual(kinds[0], 'load')
        self.assertIn('connect', kinds)
        queued = [c[1] for c in self.core.calls if c[0] == 'task']
        self.assertEqual(queued, [i.task for i in self.intents])
        self.assertEqual(self.core.calls[-1], ('start',))
        self.assertEqual(run.state, 'running')

    def _spending_plan(self):
        fight = pick(self.intents, 'Fight')
        fight.params['medicine'] = 2
        fight.tier = PL.CONFIRM
        return self.store.approve(self.intents)

    def test_spending_approval_is_consumed_by_runner_on_accepted_start(self):
        fp = self._spending_plan()
        self.runner.start(self.intents, CONN, fp)
        self.assertEqual(self.runner.run.state, 'running')
        self.assertEqual(self.store.fingerprint(), '')
        self.assertEqual(self.store.approved()['Fight']['medicine'], 2)
        self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), '')
        ok, blocked = RN.check_gates(self.store, CONN, self.intents, gui_probe=lambda: [])
        self.assertFalse(ok)
        self.assertTrue(any('Fight' in why for why in blocked))

    def test_spending_approval_is_durable_at_start_without_poststart_saves(self):
        fp = self._spending_plan()
        save = self.store.save
        observed = []

        def save_before_start():
            if ('start',) in self.core.calls:
                raise OSError('accepted start must not depend on another save')
            save()

        def accept_start():
            self.core.calls.append(('start',))
            observed.append((self.store.fingerprint(),
                             ST.Store(self.tmp / 'state').fingerprint()))
            return True

        with patch.object(self.store, 'save', side_effect=save_before_start), \
                patch.object(self.core, 'start', side_effect=accept_start):
            try:
                run = self.runner.start(self.intents, CONN, fp)
            except OSError as exc:
                self.fail(f'Accepted core lost monitoring to a save failure: {exc}')
            self.assertEqual(run.state, 'running')
            self.assertGreater(run.last_event, 0)
            self.core.queue = [{'kind': 'all_completed'}]
            self.runner.pump()
        self.assertEqual(observed, [('', '')])
        self.assertEqual(run.state, 'done')
        self.assertEqual(self.runner.read()[-1]['state'], 'done')
        self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), '')

    def test_controller_monitors_accepted_start_despite_later_save_failure(self):
        import qingyi_desktop as QD

        fp = self._spending_plan()
        ctrl = object.__new__(QD.Controller)  # 不启动应用、计时器或设备探测
        ctrl.store, ctrl.runner, ctrl.conn = self.store, self.runner, CONN
        ctrl.maa_root = self.tmp
        ctrl.log = self.lines.append
        notices = []
        ctrl.ask = notices.append
        self.core.queue = [{'kind': 'all_completed'}]
        save = self.store.save

        def save_before_start():
            if ('start',) in self.core.calls:
                raise OSError('poststart disk failure')
            save()

        with patch.object(self.store, 'save', side_effect=save_before_start), \
                patch.object(RN, 'check_gates', return_value=(True, [])), \
                patch.object(QD, 'MaaCore', return_value=self.core), \
                patch.object(QD.time, 'sleep'):
            ctrl._run_worker(self.intents, fp)
        self.assertEqual(self.runner.run.state, 'done')
        self.assertEqual(self.runner.read()[-1]['state'], 'done')
        self.assertEqual(self.core.queue, [])
        self.assertEqual([n.state for n in notices], ['done'])
        self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), '')

    def test_reservation_save_failure_never_starts_core(self):
        save = self.store.save
        for committed in (False, True):
            with self.subTest(committed=committed):
                fp = self._spending_plan()
                self.core.calls.clear()
                self.lines.clear()
                attempts = []

                def fail_save():
                    attempts.append(self.store.fingerprint())
                    if committed:
                        save()
                    raise OSError('reservation disk failure')

                with patch.object(self.store, 'save', side_effect=fail_save):
                    try:
                        run = self.runner.start(self.intents, CONN, fp)
                    except OSError as exc:
                        self.fail(f'Reservation failure escaped instead of refusing start: {exc}')
                self.assertNotIn(('start',), self.core.calls)
                self.assertEqual(run.state, 'fault')
                self.assertEqual(self.runner.read()[-1]['state'], 'fault')
                self.assertEqual(attempts, ['', fp])
                self.assertEqual(self.store.fingerprint(), '')
                self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), fp)
                self.assertTrue(any('授权' in n and 'OSError' in n for n in run.notes))
                self.assertTrue(any('恢复' in ln and 'OSError' in ln for ln in self.lines))

    def test_reservation_save_failure_restores_approval_best_effort(self):
        fp = self._spending_plan()
        save = self.store.save
        attempts = []

        def fail_once():
            attempts.append(self.store.fingerprint())
            if len(attempts) == 1:
                raise OSError('reservation disk failure')
            save()

        with patch.object(self.store, 'save', side_effect=fail_once):
            try:
                run = self.runner.start(self.intents, CONN, fp)
            except OSError as exc:
                self.fail(f'Failed reservation did not preserve a retry: {exc}')
        self.assertEqual(run.state, 'fault')
        self.assertNotIn(('start',), self.core.calls)
        self.assertEqual(self.store.fingerprint(), fp)
        self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), fp)

    def test_rejected_start_restores_spending_approval_on_disk(self):
        fp = self._spending_plan()
        observed = []

        def reject_start():
            self.core.calls.append(('start',))
            observed.append((self.store.fingerprint(),
                             ST.Store(self.tmp / 'state').fingerprint()))
            return False

        with patch.object(self.core, 'start', side_effect=reject_start):
            run = self.runner.start(self.intents, CONN, fp)
        self.assertEqual(observed, [('', '')])
        self.assertEqual(run.state, 'fault')
        self.assertEqual(self.store.fingerprint(), fp)
        self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), fp)
        self.assertEqual(self.store.approved()['Fight']['medicine'], 2)
        self.assertEqual(self.runner.start(self.intents, CONN, fp).state, 'running')
        self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), '')

    def test_rejected_start_restore_failure_closes_memory_approval_and_logs(self):
        save = self.store.save
        for committed in (False, True):
            with self.subTest(committed=committed):
                fp = self._spending_plan()
                self.lines.clear()
                attempts = []

                def fail_restore():
                    attempts.append(self.store.fingerprint())
                    if len(attempts) == 1 or committed:
                        save()
                    if len(attempts) > 1:
                        raise OSError('restore disk failure')

                with patch.object(self.store, 'save', side_effect=fail_restore), \
                        patch.object(self.core, 'start', return_value=False):
                    try:
                        run = self.runner.start(self.intents, CONN, fp)
                    except OSError as exc:
                        self.fail(f'Rejected start restore failure escaped: {exc}')
                self.assertEqual(run.state, 'fault')
                self.assertIn('AsstStart', run.notes[-1])
                self.assertEqual(self.store.fingerprint(), '')
                self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(),
                                 fp if committed else '')
                self.assertTrue(any('授权' in ln and '恢复' in ln and 'OSError' in ln
                                    for ln in self.lines))
                self.assertFalse(RN.check_gates(self.store, CONN, self.intents,
                                               gui_probe=lambda: [])[0])

    def test_runner_rejects_reusing_a_consumed_spending_approval(self):
        fp = self._spending_plan()
        self.runner.start(self.intents, CONN, fp)
        self.runner.stop()
        self.core.calls.clear()
        run = self.runner.start(self.intents, CONN, fp)
        self.assertEqual(run.state, 'fault')
        self.assertNotIn(('start',), self.core.calls)
        self.assertTrue(any('确认' in note for note in run.notes))

    def test_nonspending_plan_does_not_consume_parameter_confirmation(self):
        fp = self.store.fingerprint()
        self.runner.start(self.intents, CONN, fp)
        self.assertEqual(self.store.fingerprint(), fp)

    def test_prestart_failures_preserve_spending_approval_for_retry(self):
        for phase in ('load', 'connect', 'connect_result', 'append_task', 'start'):
            with self.subTest(phase=phase):
                fp = self._spending_plan()
                kwargs = {'side_effect': RuntimeError('没加载')} if phase == 'load' else {
                    'return_value': (False, '没连上') if phase == 'connect_result' else False}
                with patch.object(self.core, phase, **kwargs), \
                        patch.object(self.store, 'save', wraps=self.store.save) as saves:
                    run = self.runner.start(self.intents, CONN, fp)
                self.assertEqual(run.state, 'fault')
                self.assertEqual(self.store.fingerprint(), fp)
                self.assertEqual(ST.Store(self.tmp / 'state').fingerprint(), fp)
                self.assertEqual(saves.call_count, 2 if phase == 'start' else 0)

    def test_spending_without_an_approval_store_cannot_start(self):
        fp = self._spending_plan()
        self.runner.store = None
        run = self.runner.start(self.intents, CONN, fp)
        self.assertEqual(run.state, 'fault')
        self.assertNotIn(('start',), self.core.calls)

    def test_a_dead_device_is_never_treated_as_connected(self):
        """AsstAsyncConnect 返回 True 只代表调用发出去了，回执 ret=false 才是真结果。"""
        self.core.connect_ret = False
        run = self.runner.start(self.intents, CONN, 'fp1')
        self.assertEqual(run.state, 'fault')
        self.assertTrue(any('连不上' in n for n in run.notes))
        self.assertTrue(any('4241' in n for n in run.notes))
        self.assertEqual([c for c in self.core.calls if c[0] == 'task'], [])
        self.assertNotIn(('start',), self.core.calls)
        rows = self.runner.read()
        self.assertEqual(rows[-1]['event'], 'run_finished')
        self.assertEqual(rows[-1]['state'], 'fault')

    def test_a_connect_call_that_never_left_is_named_as_such(self):
        self.core.connect_ok = False
        run = self.runner.start(self.intents, CONN, 'fp1')
        self.assertEqual(run.state, 'fault')
        self.assertTrue(any('没发出去' in n for n in run.notes))
        self.assertEqual([c for c in self.core.calls if c[0] == 'task'], [])

    def test_counts_and_sanity_come_from_callbacks_only(self):
        self.runner.start(self.intents, CONN, 'fp1')
        self.core.queue = [
            {'kind': 'chain', 'state': 'start', 'taskchain': 'Fight'},
            {'kind': 'extra', 'taskchain': 'Fight', 'what': 'SanityBeforeStage',
             'details': {'current_sanity': 128}},
            {'kind': 'extra', 'taskchain': 'Fight', 'what': 'FightTimes',
             'details': {'times_finished': 4}},
            {'kind': 'extra', 'taskchain': 'Fight', 'what': 'SanityBeforeStage',
             'details': {'current_sanity': 96}},
            {'kind': 'chain', 'state': 'completed', 'taskchain': 'Fight'},
        ]
        self.runner.pump()
        f = next(c for c in self.runner.run.chains if c.task == 'Fight')
        self.assertEqual((f.state, f.times_finished, f.sanity_before, f.sanity_after),
                         ('done', 4, 128, 96))
        self.assertTrue(any('局数=4' in ln for ln in self.lines))

    def test_all_tasks_completed_closes_the_run_with_a_ledger_line(self):
        self.runner.start(self.intents, CONN, 'fp1')
        self.core.queue = [{'kind': 'all_completed', 'taskchain': '', 'raw': {}}]
        self.runner.pump()
        self.assertEqual(self.runner.run.state, 'done')
        rows = self.runner.read()
        self.assertEqual(rows[-1]['event'], 'run_finished')
        self.assertEqual(len(rows[-1]['chains']), len(self.intents))

    def test_all_completed_preserves_failed_chain_and_partial_results(self):
        self.runner.start(self.intents, CONN, 'fp1')
        self.core.queue = [
            {'kind': 'chain', 'state': 'error', 'taskchain': 'Fight'},
            {'kind': 'chain', 'state': 'completed', 'taskchain': 'Mall'},
            {'kind': 'all_completed'},
        ]
        self.runner.pump()
        snap = self.runner.snapshot()
        self.assertEqual(snap['state'], 'fault')
        self.assertEqual(next(c for c in snap['chains'] if c['task'] == 'Fight')['state'],
                         'error')
        self.assertEqual(next(c for c in snap['chains'] if c['task'] == 'Mall')['state'],
                         'done')
        self.assertTrue(any('Fight' in n and 'TaskChainError' in n for n in snap['notes']))
        row = self.runner.read()[-1]
        self.assertEqual(row['state'], 'fault')
        self.assertEqual(row.get('notes'), snap['notes'])
        self.assertIn('TaskChainError', row['why'])

    def test_fault_notes_prevent_all_completed_from_claiming_success(self):
        self.runner.start(self.intents, CONN, 'fp1')
        self.core.queue = [{'kind': 'fault', 'what': '识别失败', 'raw': {}},
                           {'kind': 'all_completed'}]
        self.runner.pump()
        self.assertEqual(self.runner.run.state, 'fault')
        self.assertIn('识别失败', self.runner.read()[-1].get('notes', []))

    def test_later_callbacks_cannot_erase_a_chain_error(self):
        self.runner.start(self.intents, CONN, 'fp1')
        self.core.queue = [
            {'kind': 'chain', 'state': 'error', 'taskchain': 'Fight'},
            {'kind': 'chain', 'state': 'start', 'taskchain': 'Fight'},
            {'kind': 'chain', 'state': 'completed', 'taskchain': 'Fight'},
            {'kind': 'all_completed'},
        ]
        self.runner.pump()
        fight = next(c for c in self.runner.run.chains if c.task == 'Fight')
        self.assertEqual(fight.state, 'error')
        self.assertEqual(self.runner.run.state, 'fault')

    def test_stop_goes_through_maacore_and_marks_the_run(self):
        self.runner.start(self.intents, CONN, 'fp1')
        self.assertTrue(self.runner.stop())
        self.assertTrue(self.core.stopped)
        self.assertEqual(self.runner.run.state, 'stopped')

    def test_a_run_left_open_by_a_crash_is_marked_interrupted_not_done(self):
        self.runner.start(self.intents, CONN, 'fp1')
        fresh = RN.Runner(FakeCore(), self.tmp / 'ledger.jsonl', log=lambda s: None)
        fixed = fresh.recover_stale()
        self.assertEqual(len(fixed), 1)
        self.assertEqual(fresh.read()[-1]['event'], 'run_interrupted')
        self.assertEqual(fresh.recover_stale(), [])

    def test_silence_for_four_minutes_is_called_out_loudly(self):
        self.runner.start(self.intents, CONN, 'fp1')
        self.runner.run.last_event = time.time() - RN.STALE_AFTER - 5
        self.runner.pump()
        self.assertEqual(self.runner.run.state, 'stalled')
        self.assertTrue(any('卡住' in ln for ln in self.lines))


if __name__ == '__main__':
    unittest.main()
