import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))

import maacore as mc
import discovery as ds


class FakeLib:
    """假 MaaCore.dll：只记账，不碰真 DLL。"""

    def __init__(self, version=b'v5.23.2'):
        self.calls = []
        self._version = version
        self.cb = None

    def _rec(self, name, *a):
        self.calls.append((name, a))
        return True

    def AsstSetUserDir(self, p): return self._rec('userdir', p)
    def AsstLoadResource(self, p): return self._rec('res', p)
    def AsstCreate(self): self.calls.append(('create', ())); return 1
    def AsstCreateEx(self, cb, arg):
        self.cb = cb
        self.calls.append(('createex', ()))
        return 7
    def AsstDestroy(self, h): return self._rec('destroy', h)
    def AsstSetInstanceOption(self, h, k, v): return self._rec('inst_opt', h, k, v)
    def AsstSetStaticOption(self, k, v): return self._rec('static_opt', k, v)
    def AsstSetConnectionExtras(self, n, j): return self._rec('extras', n, j)
    def AsstAsyncConnect(self, handle, adb, addr, cfg, block):
        return self._rec('connect', handle, adb, addr, cfg, block)
    def AsstAppendTask(self, h, name, params):
        self.calls.append(('append', name, params))
        return len([c for c in self.calls if c[0] == 'append'])
    def AsstSetTaskParams(self, h, tid, params): return self._rec('set_params', h, tid, params)
    def AsstStart(self, h): self.calls.append(('start', h)); return True
    def AsstStop(self, h): self.calls.append(('stop', h)); return True
    def AsstRunning(self, h): return False
    def AsstGetVersion(self): return self._version


class ParseEvent(unittest.TestCase):
    def test_task_chain_completed_becomes_chain_state(self):
        ev = mc.parse_event(mc.MSG_TASK_CHAIN_COMPLETED,
                            {'taskchain': 'Fight', 'uuid': 'u1', 'finished_tasks': [1, 2]})
        self.assertEqual(ev['kind'], 'chain')
        self.assertEqual(ev['state'], 'completed')
        self.assertEqual(ev['taskchain'], 'Fight')
        self.assertEqual(ev['finished'], [1, 2])

    def test_subtask_extra_info_keeps_what_and_details(self):
        ev = mc.parse_event(mc.MSG_SUB_TASK_EXTRA_INFO,
                            {'taskchain': 'Fight', 'what': 'FightTimes',
                             'details': {'times_finished': 3}})
        self.assertEqual(ev['kind'], 'extra')
        self.assertEqual(ev['what'], 'FightTimes')
        self.assertEqual(ev['details']['times_finished'], 3)

    def test_unknown_message_is_not_swallowed(self):
        ev = mc.parse_event(9999, {'whatever': 1})
        self.assertEqual(ev['kind'], 'other')
        self.assertEqual(ev['raw'], {'whatever': 1})

    def test_fault_messages(self):
        for msg in (mc.MSG_INTERNAL_ERROR, mc.MSG_INIT_FAILED, mc.MSG_SUB_TASK_ERROR):
            self.assertEqual(mc.parse_event(msg, {'what': 'x'})['kind'], 'fault')


class Bindings(unittest.TestCase):
    def setUp(self):
        self.lib = FakeLib()
        self.core = mc.MaaCore(dll_dir=r'D:\somewhere\MAA', user_dir=r'D:\somewhere\work',
                               lib=self.lib)

    def test_load_returns_version_and_loads_resources_from_dll_dir(self):
        self.assertEqual(self.core.load(), 'v5.23.2')
        names = [c[0] for c in self.lib.calls]
        self.assertIn('userdir', names)
        self.assertIn('res', names)
        self.assertEqual([c[1][0] for c in self.lib.calls if c[0] == 'res'],
                         [rb'D:\somewhere\MAA'])

    def test_connect_passes_the_handle_first_then_adb_address_config(self):
        self.core.connect(r'E:\YXArkNights-12.0\shell\adb.exe', '127.0.0.1:16384',
                          'MuMuEmulator12')
        call = [c for c in self.lib.calls if c[0] == 'connect'][-1]
        handle, adb, addr, cfg, block = call[1]
        self.assertEqual(handle, 7)  # AsstCreateEx 返回的那个句柄
        self.assertEqual((adb, addr, cfg), (rb'E:\YXArkNights-12.0\shell\adb.exe',
                                            b'127.0.0.1:16384', b'MuMuEmulator12'))
        self.assertTrue(block)

    def test_append_task_sends_utf8_json_params(self):
        self.core.create()
        tid = self.core.append_task('Mall', {'blacklist': ['碳', '家具'], 'shopping': True})
        self.assertEqual(tid, 1)
        name, params = [c for c in self.lib.calls if c[0] == 'append'][-1][1:]
        self.assertEqual(name, b'Mall')
        self.assertEqual(json.loads(params.decode('utf-8')),
                         {'blacklist': ['碳', '家具'], 'shopping': True})

    def test_callback_is_kept_alive_and_events_are_drained_once(self):
        self.core.create()
        self.assertIsNotNone(self.core._cb)
        self.core._on_callback(mc.MSG_TASK_CHAIN_START,
                               b'{"taskchain":"Infrast"}', None)
        self.core._on_callback(mc.MSG_ALL_TASKS_COMPLETED, b'{}', None)
        got = self.core.events()
        self.assertEqual([e['kind'] for e in got], ['chain', 'all_completed'])
        self.assertEqual(self.core.events(), [])

    def test_undecodable_callback_payload_still_leaves_a_trace(self):
        self.core.create()
        self.core._on_callback(mc.MSG_INTERNAL_ERROR, b'\xff\xfe not json', None)
        ev = self.core.events()[0]
        self.assertEqual(ev['kind'], 'fault')
        self.assertIn('_undecodable', ev['raw'])

    def test_load_without_dll_dir_says_what_is_missing(self):
        core = mc.MaaCore(lib=None, dll_dir=None)
        with self.assertRaises(RuntimeError) as ctx:
            core.load()
        self.assertIn('MaaCore.dll', str(ctx.exception))

    def test_connect_result_reads_the_async_receipt(self):
        self.core.create()
        self.core._on_callback(
            mc.MSG_ASYNC_CALL_INFO,
            b'{"async_call_id":1,"details":{"cost":4241,"ret":false},'
            b'"uuid":"","what":"Connect"}', None)
        ok, why = self.core.connect_result(timeout=1.0, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn('4241', why)

    def test_connect_result_accepts_a_real_success(self):
        self.core.create()
        self.core._on_callback(mc.MSG_ASYNC_CALL_INFO,
                               b'{"details":{"cost":812,"ret":true},"what":"Connect"}', None)
        ok, why = self.core.connect_result(timeout=1.0, sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertIn('812', why)

    def test_connect_result_times_out_instead_of_guessing(self):
        self.core.create()
        ok, why = self.core.connect_result(timeout=0.2, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn('没有连接回执', why)


class FindInstall(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        for name in ('MAA-v5.9.0-win-x64', 'MAA-v5.23.2-win-x64', 'MAA-tools'):
            d = self.root / name
            d.mkdir(parents=True)
        (self.root / 'MAA-v5.9.0-win-x64' / 'MaaCore.dll').write_bytes(b'x')
        (self.root / 'MAA-v5.23.2-win-x64' / 'MaaCore.dll').write_bytes(b'x')
        (self.root / 'MAA-tools' / 'readme.txt').write_text('x', encoding='utf-8')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_picks_the_highest_versioned_dir_that_has_the_dll(self):
        found = ds.find_maa_install(roots=[self.root])
        self.assertEqual(found.name, 'MAA-v5.23.2-win-x64')

    def test_preferred_path_wins_even_if_older(self):
        found = ds.find_maa_install(preferred=str(self.root / 'MAA-v5.9.0-win-x64'),
                                    roots=[self.root])
        self.assertEqual(found.name, 'MAA-v5.9.0-win-x64')

    def test_preferred_path_that_no_longer_exists_falls_back_to_scan(self):
        found = ds.find_maa_install(preferred=str(self.root / 'MAA-v4.0.0-win-x64'),
                                    roots=[self.root])
        self.assertEqual(found.name, 'MAA-v5.23.2-win-x64')

    def test_nothing_found_returns_none_instead_of_guessing(self):
        self.assertIsNone(ds.find_maa_install(roots=[self.root / 'MAA-tools']))


GUI_CFG = {
    'Configurations': {'Default': {'Gui': {'ConnectSettings': {
        'Config': 'MuMuEmulator12',
        'AdbPath': 'E:\\YXArkNights-12.0\\shell\\.\\adb.exe',
        'Address': '127.0.0.1:16384',
        'TouchMode': 'MumuExtras',
        'EnableAdbLite': False,
        'KillAdbOnExit': False,
        'Extras': {'MuMuEmulator12': {'IsEnabled': True,
                                      'EmulatorPath': 'E:\\YXArkNights-12.0',
                                      'InstanceIndex': 0}},
    }, 'RuntimeSettings': {'ClientType': 'Official'}}}}}


class ReadConnection(unittest.TestCase):
    def test_reads_the_values_the_user_already_configured(self):
        conn = ds.read_connection(GUI_CFG)
        self.assertEqual(conn.address, '127.0.0.1:16384')
        self.assertEqual(conn.config, 'MuMuEmulator12')
        self.assertEqual(conn.touch_mode, 'MumuExtras')
        self.assertEqual(conn.client_type, 'Official')
        self.assertIn('你的 MAA 配置', conn.source)

    def test_extras_are_carried_through_for_connection_setup(self):
        conn = ds.read_connection(GUI_CFG)
        self.assertEqual(conn.extras['MuMuEmulator12']['InstanceIndex'], 0)

    def test_missing_config_says_so_instead_of_pretending(self):
        conn = ds.read_connection({})
        self.assertIn('内置默认', conn.source)
        self.assertEqual(conn.address, '127.0.0.1:16384')

    def test_adb_path_that_does_not_exist_falls_back_to_path(self):
        conn = ds.read_connection(GUI_CFG)
        if not Path(conn.adb_path).exists():
            self.assertTrue(conn.adb_path != 'E:\\YXArkNights-12.0\\shell\\.\\adb.exe'
                            or '改用 PATH' in conn.source)


class EmulatorProbe(unittest.TestCase):
    """MuMuManager 的字段名是实测来的：is_android_started / is_process_started。"""

    def test_android_started_is_running(self):
        class Out:
            stdout = b'{"index":"0","name":"Mumu","is_android_started":true,' \
                     b'"is_process_started":true}'
        ok, why = ds.emulator_running('E:\\YXArkNights-12.0', run=lambda *a, **k: Out())
        self.assertTrue(ok)
        self.assertIn('安卓系统', why)

    def test_process_up_but_android_down_is_its_own_answer(self):
        class Out:
            stdout = b'{"name":"Mumu","is_android_started":false,"is_process_started":true}'
        ok, why = ds.emulator_running('E:\\YXArkNights-12.0', run=lambda *a, **k: Out())
        self.assertFalse(ok)
        self.assertIn('安卓系统还没起来', why)

    def test_vm_not_started_is_named_with_its_instance_name(self):
        class Out:
            stdout = '{"name":"明日方舟-MuMu模拟器12","is_android_started":false,'\
                     '"is_process_started":false}'.encode('utf-8')
        ok, why = ds.emulator_running('E:\\YXArkNights-12.0', run=lambda *a, **k: Out())
        self.assertFalse(ok)
        self.assertIn('明日方舟-MuMu模拟器12', why)
        self.assertNotIn('{', why)  # 不能把整坨 JSON 甩到面板上

    def test_unparsable_output_is_reported_not_guessed(self):
        class Out:
            stdout = b'[MW] cannot connect to service'
        ok, why = ds.emulator_running('E:\\YXArkNights-12.0', run=lambda *a, **k: Out())
        self.assertFalse(ok)
        self.assertIn('不是 JSON', why)

    def test_missing_manager_is_named(self):
        ok, why = ds.emulator_running('', run=lambda *a, **k: None)
        self.assertFalse(ok)
        self.assertIn('MuMuManager', why)


class AdbReachable(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.adb = Path(self.tmp) / 'adb.exe'
        self.adb.write_bytes(b'x')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _runner(chunks):
        seq = [type('Out', (), {'stdout': c, 'stderr': b''})() for c in chunks]
        return (lambda cmd, *a, **k: seq.pop(0)), seq

    def test_adb_is_called_without_the_host_option(self):
        # MuMu 自带的 adb 34 不认 -host，带上它每条理由都会变成假原因
        seen = []

        def run(cmd, *a, **k):
            seen.append(cmd)
            return type('Out', (), {'stdout': b'List of devices attached\n',
                                    'stderr': b''})()
        ds.adb_reachable(str(self.adb), '127.0.0.1:16384', run=run)
        self.assertTrue(all('-host' not in c for c in seen), seen)

    def test_address_listed_as_device_is_reachable(self):
        run, _ = self._runner([b'connected to 127.0.0.1:16384\n',
                               b'List of devices attached\n127.0.0.1:16384\tdevice\n'])
        ok, why = ds.adb_reachable(str(self.adb), '127.0.0.1:16384', run=run)
        self.assertTrue(ok, why)
        self.assertEqual(why, 'device')

    def test_offline_device_is_not_reachable(self):
        run, _ = self._runner([b'', b'127.0.0.1:16384\toffline\n'])
        ok, why = ds.adb_reachable(str(self.adb), '127.0.0.1:16384', run=run)
        self.assertFalse(ok)
        self.assertEqual(why, 'offline')

    def test_refused_connection_quotes_adbs_own_words(self):
        msg = 'cannot connect to 127.0.0.1:16384: 由于目标计算机积极拒绝，无法连接。'.encode('utf-8')
        run, _ = self._runner([msg, b'List of devices attached\n'])
        ok, why = ds.adb_reachable(str(self.adb), '127.0.0.1:16384', run=run)
        self.assertFalse(ok)
        self.assertIn('连不上 127.0.0.1:16384', why)
        self.assertIn('积极拒绝', why)

    def test_missing_adb_is_named_before_any_call(self):
        called = []
        ok, why = ds.adb_reachable(r'D:\没有\adb.exe', '127.0.0.1:16384',
                                   run=lambda *a, **k: called.append(1))
        self.assertFalse(ok)
        self.assertIn('adb 不在', why)
        self.assertEqual(called, [])

    def test_device_state_quotes_both_adb_and_muMu(self):
        chunks = [b'cannot connect to 127.0.0.1:16384', b'List of devices attached\n']
        seq = [type('Out', (), {'stdout': c, 'stderr': b''})() for c in chunks]
        mu = type('Out', (), {'stdout': '{"name":"明日方舟","is_android_started":false,'
                                        '"is_process_started":false}'.encode('utf-8'),
                              'stderr': b''})()

        def run(cmd, *a, **k):
            if 'MuMuManager' in str(cmd):
                return mu
            return seq.pop(0)
        conn = ds.Connection(adb_path=str(self.adb), address='127.0.0.1:16384')
        ok, why = ds.device_state(conn, 'E:\\YXArkNights-12.0', run=run)
        self.assertFalse(ok)
        self.assertIn('连不上 127.0.0.1:16384', why)
        self.assertIn('MuMu', why)


class ChildEnvironment(unittest.TestCase):
    """MuMuManager 自己也是 Qt5 程序：继承 QT_QPA_PLATFORM 会让它弹「找不到平台插件」。"""

    def test_qt_variables_are_stripped_before_spawning_children(self):
        import os
        with patch.dict(os.environ, {'QT_QPA_PLATFORM': 'offscreen',
                                     'QT_PLUGIN_PATH': r'D:\somewhere',
                                     'QT_LOGGING_RULES': 'qt5.key=true'}, clear=False):
            env = ds._child_env()
        self.assertNotIn('QT_QPA_PLATFORM', env)
        self.assertNotIn('QT_PLUGIN_PATH', env)
        self.assertNotIn('QT_LOGGING_RULES', env)
        self.assertIn('PATH', env)

    def test_child_env_is_case_insensitive_about_qt_prefix(self):
        import os
        with patch.dict(os.environ, {'qt_qpa_platform': 'offscreen'}, clear=False):
            env = ds._child_env()
        self.assertFalse([k for k in env if k.upper().startswith('QT_')])

    def test_runquiet_accepts_the_same_kwargs_as_subprocess_run(self):
        # 探针就是在这里踩过：调用方按 subprocess.run 的写法传 capture_output/timeout
        import os
        import sys
        with patch.dict(os.environ, {'QT_QPA_PLATFORM': 'offscreen'}, clear=False):
            out = ds._runquiet([sys.executable, '-c',
                                'import os;print(os.environ.get("QT_QPA_PLATFORM","<none>"))'],
                               capture_output=True, timeout=30)
        text = out.stdout.decode('utf-8', 'replace').strip()
        self.assertEqual(text, '<none>', text)


if __name__ == '__main__':
    unittest.main()
