import base64
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
# A modern local day is portable to Windows mktime (unlike epoch-day fixtures).
OBSERVED_AT = 1794103200
for directory in (ROOT / 'bot', ROOT / 'website'):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))


def diagnostic_fixture():
    """API-boundary fixture; the real-status golden path remains separate."""
    checks = [dict(key='check-%d' % i, label='观测条件 %d' % i,
                   need='有可核验证据', now='已记录', ok=True, status='pass')
              for i in range(10)]
    checks[8].update(label='注意负荷实现', now='未实现', ok=False, status='fail')
    checks[9].update(label='工具结果覆盖', now='证据不足', ok=False, status='unknown')
    return {
        'ok': True, 'mode': 'shadow', 'running': True, 'expression_control': False,
        'calibration': 'review_ready', 'ready_for_review': True, 'stage3_ready': False,
        'parameter_hash': 'fixture-parameters-v2',
        # Old counters disagree deliberately: never substitute them for diagnostics.
        'real_events': 999, 'observed_seconds': 999999, 'gap_seconds': 999,
        'state': dict(valence=0.2, arousal=0.1, control=0.3, attention=0.23),
        'timeline': [dict(kind='action_result', rule='receipt', reason='只是回执', at=1002),
                     dict(kind='consolidation', rule='excluded', reason='只是整理', at=1002)],
        'calibration_detail': dict(checks=checks, passed=8, total=10, progress=0.8),
        'diagnostics': {
            'received_events': 18, 'live_events': 12, 'effective_events': 5,
            'ignored_events': 7, 'excluded_events': 6, 'quarantined_events': 2,
            'legacy_excluded_events': 2, 'run_version': 'shadow-fixture-v2',
            'by_kind': {
                'group_message': dict(received=8, live=7, effective=3, ignored=4, excluded=1),
                'tool_result': dict(received=4, live=3, effective=2, ignored=1, excluded=1),
                'action_result': dict(received=2, live=2, effective=0, ignored=2, excluded=0),
                'consolidation': dict(received=2, live=0, effective=0, ignored=0, excluded=2),
                'legacy_note': dict(received=2, live=0, effective=0, ignored=0, excluded=2)},
            'by_rule': dict(message=3, tool_success=1, tool_failure=1, ignored=7, excluded=6),
            'tools': {'web_read': dict(success=1, failure=1, unknown=1)},
            'dimensions': {
                'valence': dict(implemented=True, nonzero_events=3, positive=2, negative=1,
                                min=-0.2, max=0.4, observed=True),
                'arousal': dict(implemented=True, nonzero_events=0, positive=0, negative=0,
                                min=0, max=0, observed=False),
                'control': dict(implemented=True, nonzero_events=2, positive=1, negative=1,
                                min=-0.1, max=0.2, observed=True),
                'attention': dict(implemented=False, nonzero_events=0, positive=0, negative=0,
                                  min=0, max=0, observed=False)},
            'freshness': dict(heartbeat_age_seconds=2, stale=False),
            'backlog': dict(known=True, pending=0, oldest_age_seconds=None, ledger_consistent=True),
            'continuity': dict(observed_seconds=3600, gap_seconds=60, open_gap_seconds=0,
                               gap_ratio=0.0164, current_streak_seconds=600),
            'evidence_replay': dict(ok=True, checked_events=12, max_error=0,
                                    source_missing=0, appraisal_mismatches=0)}}


def browser_fixtures():
    data = diagnostic_fixture()
    empty = copy.deepcopy(data)
    diag = empty['diagnostics']
    for key in ('received_events', 'live_events', 'effective_events', 'ignored_events',
                'excluded_events', 'quarantined_events', 'legacy_excluded_events'):
        diag[key] = 0
    for key in ('by_kind', 'by_rule', 'tools'):
        diag[key] = {}
    for dim in diag['dimensions'].values():
        dim.update(implemented=True, observed=False, nonzero_events=0,
                   positive=0, negative=0, min=0, max=0)
    diag['continuity'].update(observed_seconds=0, gap_seconds=0, open_gap_seconds=0,
                              gap_ratio=None, current_streak_seconds=0)
    diag['freshness']['heartbeat_age_seconds'] = 0
    diag['evidence_replay'].update(ok=None, checked_events=0, max_error=None)
    empty.update(timeline=[], ready_for_review=False, calibration='collecting')
    empty['calibration_detail'].update(checks=[dict(c, ok=False, status='not_observed', now='未观测')
                                             for c in data['calibration_detail']['checks'][:9]],
                                       passed=0, total=9, progress=0)
    ready = copy.deepcopy(data)
    for dim in ready['diagnostics']['dimensions'].values():
        dim.update(implemented=True, observed=True, nonzero_events=1)
    ready['calibration_detail'].update(checks=copy.deepcopy(data['calibration_detail']['checks'][:8]) + [
        dict(key='ninth', label='第九项检查', need='通过', now='通过', ok=True, status='pass')],
        passed=9, total=9, progress=1)
    ready['stage3_ready'] = True  # Contradictory backend flag cannot open stage 3.
    unimplemented = copy.deepcopy(ready)
    unimplemented['diagnostics']['dimensions']['attention']['implemented'] = False
    legacy = copy.deepcopy(ready)
    legacy.pop('diagnostics')
    inconsistent = copy.deepcopy(ready)
    inconsistent['calibration_detail'].update(passed=100, total=100, progress=1)
    unknown = copy.deepcopy(ready)
    diag = unknown['diagnostics']
    for key in list(diag):
        if key not in ('dimensions', 'freshness', 'backlog', 'continuity', 'evidence_replay'):
            diag[key] = None
    for section in ('freshness', 'backlog', 'continuity', 'evidence_replay'):
        diag[section] = dict.fromkeys(diag[section])
    diag['backlog']['known'] = False
    for dim in diag['dimensions'].values():
        dim.update(dict.fromkeys(dim))
    unknown['calibration_detail']['checks'][0].update(status='unknown', ok=True, now=None)
    unknown['calibration_detail'].update(passed=8, progress=1)
    stale = copy.deepcopy(ready)
    stale['diagnostics']['freshness'].update(heartbeat_age_seconds=180, stale=True)
    stale['diagnostics']['backlog'].update(pending=3, oldest_age_seconds=45, ledger_consistent=False)
    stale['diagnostics']['continuity'].update(open_gap_seconds=120, current_streak_seconds=0)
    xss = copy.deepcopy(data)
    attack = '<img class="shadow-xss" src=x onerror="window.__shadowXss=1">'
    diag = xss['diagnostics']
    diag['run_version'] = attack
    diag['by_kind'] = {attack: dict(received=1, live=1, effective=0, ignored=1, excluded=0)}
    diag['by_rule'] = {attack: 1}
    diag['tools'] = {attack: dict(success=1, failure=0, unknown=0)}
    xss['calibration_detail']['checks'][0].update(label=attack, need=attack, now=attack)
    xss['timeline'] = [dict(kind=attack, rule=attack, reason=attack, at=1002)]
    history = copy.deepcopy(data)
    history['retrospective'] = dict(
        mode='retrospective', counts_for_live=False, day='2026-11-08', events=42,
        effective_events=17, excluded_events=6, source_start=1794096000,
        source_end=1794103200, rule_version='history-fixture-v2',
        dimensions=copy.deepcopy(data['diagnostics']['dimensions']))
    history_empty = copy.deepcopy(history)
    history_empty['retrospective'].update(events=0, effective_events=0, excluded_events=0,
                                           source_start=None, source_end=None, dimensions={})
    history_error = copy.deepcopy(history)
    history_error['retrospective']['error'] = '账本暂不可读'
    compact = copy.deepcopy(history)
    for i, check in enumerate(compact['calibration_detail']['checks']):
        check.update(ok=i < 3, status='pass' if i < 3 else 'not_observed')
    compact['calibration_detail'].update(passed=3, total=10, progress=0.3)
    compact['ready_for_review'] = False
    xss['retrospective'] = copy.deepcopy(history['retrospective'])
    for key in ('day', 'source_start', 'source_end', 'rule_version'):
        xss['retrospective'][key] = attack
    xss_error = copy.deepcopy(xss)
    xss_error['retrospective']['error'] = attack
    return dict(data=data, empty=empty, ready=ready, unknown=unknown, stale=stale, xss=xss, xss_error=xss_error,
                history=history, history_empty=history_empty, history_error=history_error, compact=compact,
                legacy=legacy, unimplemented=unimplemented, inconsistent=inconsistent)


@unittest.skipUnless(os.getenv('EMOTION_BROWSER_TEST') == '1',
                     'explicit local headless-browser integration test')
class BrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flask import Flask, jsonify, redirect, request, session
        from werkzeug.serving import make_server
        import ceylan_dashboard
        import emotion_shadow
        import self_model
        cls.chrome = next((p for p in (shutil.which('google-chrome'),
                                      'C:/Program Files/Google/Chrome/Application/chrome.exe',
                                      'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
                           if p and Path(p).is_file()), None)
        if not cls.chrome:
            raise AssertionError('Chromium is required for explicit browser tests')
        cls.tmp = tempfile.TemporaryDirectory(prefix='shadow_browser_')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.env = patch.dict(os.environ, {'DATA_DIR': cls.tmp.name,
                                          'PERSONA_DATA_DIR': str(Path(cls.tmp.name) / 'persona')})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        with closing(self_model._db()):
            pass
        emotion_shadow.tick(now=OBSERVED_AT)
        self_model.record_event('<img id=shadow-xss src=x>', ts=OBSERVED_AT + 1,
                                payload={'text': 'test'}, dedupe='dom-safety')
        with closing(self_model._db()) as c, c:
            c.execute('UPDATE event SET created=? WHERE dedupe=?', (OBSERVED_AT + 1, 'dom-safety'))
        emotion_shadow.tick(now=OBSERVED_AT + 2)
        cls.real_status = staticmethod(emotion_shadow.status)
        cls.cases = browser_fixtures()
        cls.flags = dict(case='real', driver='', calls=0, affection_resets=0)
        app = Flask(__name__)
        app.secret_key = 'local-browser-test-only'

        def test_status(*args, **kwargs):
            cls.flags['calls'] += 1
            case = cls.flags['case']
            if case == 'error':
                raise RuntimeError('local failure fixture')
            if case == 'real':
                return cls.real_status(now=OBSERVED_AT + 2)
            return copy.deepcopy(cls.cases[case])

        cls.status_patch = patch.object(emotion_shadow, 'status', side_effect=test_status)
        cls.status_patch.start()
        cls.addClassCleanup(cls.status_patch.stop)

        @app.before_request
        def fixture_apis():
            if request.path == '/api/status':
                return jsonify({})
            if request.path == '/admin/api/bot/emotion-shadow' and cls.flags['case'] == 'html-error':
                return '<h1>gateway failure</h1>', 502
            if request.path == '/admin/api/bot/affection' and request.method == 'POST':
                if (request.get_json(silent=True) or {}).get('action') == 'reset':
                    cls.flags['affection_resets'] += 1
                return jsonify({})
            if request.path.startswith('/admin/api/bot/') and request.path != '/admin/api/bot/emotion-shadow':
                return jsonify({})

        @app.get('/local-browser-fixture')
        def fixture_login():
            session['ceylan_admin'] = True
            return redirect('/admin/dashboard')

        @app.post('/local-browser-case/<case>')
        def fixture_case(case):
            if case not in cls.cases and case not in ('error', 'html-error', 'real'):
                return jsonify(ok=False), 400
            cls.flags['case'] = case
            return jsonify(ok=True)

        ceylan_dashboard.install(app, {})

        @app.after_request
        def fixture_driver(resp):
            if request.path == '/admin/dashboard':
                resp.set_data(resp.get_data(as_text=True).replace('</body>', cls.flags['driver'] + '</body>'))
            return resp

        cls.server = make_server('127.0.0.1', 0, app, threaded=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)

    @classmethod
    def stop_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def run_browser(self, case, assertions, width=1280):
        # Catch UI mutations: missing != zero, receipt != driver, unknown != pass,
        # stale != running, and untrusted API strings != markup.
        self.flags.update(case=case, calls=0)
        self.flags['driver'] = '''<script>
window.addEventListener('load', async function(){
  function el(id){return document.getElementById('emotion-shadow-'+id);}
  function text(id){return el(id) ? el(id).textContent : '';}
  function assert(ok, why){if(!ok)throw new Error(why);}
  function has(id, expected){assert(text(id).includes(expected), id+': expected '+expected+'; got '+text(id));}
  function metric(key, expected){
    var node=document.querySelector('[data-shadow-metric="'+key+'"]');
    assert(node && node.textContent===expected, key+': expected '+expected+'; got '+(node && node.textContent));
  }
  function dimension(key){return document.querySelector('[data-shadow-dimension="'+key+'"]');}
  function waitFor(check){ return new Promise(function(resolve,reject){
    var start=Date.now(); var timer=setInterval(function(){
      if(check()){clearInterval(timer);resolve();}
      else if(Date.now()-start>8000){clearInterval(timer);reject(new Error('timed out'));}
    },40);
  }); }
  async function loaded(){await waitFor(function(){return !text('summary').includes('读取') && text('summary')!=='';});}
  async function refresh(next){
    if(next)await fetch('/local-browser-case/'+next,{method:'POST'});
    el('refresh').click(); await loaded();
  }
  window.alert=window.confirm=window.prompt=function(){throw new Error('native dialog opened');};
  try {
    document.querySelector('[data-view="qingyi"]').click();
    document.querySelector('[data-qingyi-tab="shadow"]').click(); await loaded();
    ASSERTIONS
    assert(!window.__shadowXss && !document.querySelector('.shadow-xss,#shadow-xss'), 'external markup executed');
    document.documentElement.dataset.shadowTest='passed';
  } catch(e){document.documentElement.dataset.shadowTestError=e.message;}
});
</script>'''.replace('ASSERTIONS', assertions)
        with tempfile.TemporaryDirectory(prefix='chromium_', dir=self.tmp.name) as profile:
            result = subprocess.run([
                self.chrome, '--headless=new', '--disable-gpu', '--disable-extensions',
                '--no-first-run', '--no-default-browser-check', '--window-size=%d,1000' % width,
                '--user-data-dir=' + profile, '--virtual-time-budget=16000', '--dump-dom',
                'http://127.0.0.1:%s/local-browser-fixture' % self.server.server_port],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=50)
        self.assertEqual(result.returncode, 0, result.stderr[-1000:])

        class ResultParser(HTMLParser):
            attrs = {}

            def handle_starttag(self, tag, attrs):
                if tag == 'html':
                    self.attrs = dict(attrs)

        parser = ResultParser()
        parser.feed(result.stdout)
        self.assertEqual(parser.attrs.get('data-shadow-test'), 'passed',
                         parser.attrs.get('data-shadow-test-error', result.stdout[-1500:]) + result.stderr[-500:])

    @contextmanager
    def interactive_chrome(self, width):
        """Real CDP input/emulation; no synthetic JS keyboard events."""
        import websocket
        self.flags.update(case='compact', calls=0, driver='''<script>
window.addEventListener('load',function(){document.querySelector('[data-view="qingyi"]').click();document.querySelector('[data-qingyi-tab="shadow"]').click();});
</script>''')
        with tempfile.TemporaryDirectory(prefix='cdp_', dir=self.tmp.name) as profile:
            process = subprocess.Popen([
                self.chrome, '--headless=new', '--disable-gpu', '--disable-extensions',
                '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0',
                '--user-data-dir=' + profile, 'about:blank'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ws = None
            try:
                port_file = Path(profile) / 'DevToolsActivePort'
                deadline = time.monotonic() + 15
                while not port_file.is_file():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('Chrome CDP did not start')
                    threading.Event().wait(0.05)
                port = port_file.read_text().splitlines()[0]
                with urlopen('http://127.0.0.1:%s/json/list' % port, timeout=10) as response:
                    target = next(t for t in json.load(response) if t['type'] == 'page')
                ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=20, suppress_origin=True)
                sequence = 0

                def command(method, **params):
                    nonlocal sequence
                    sequence += 1
                    ws.send(json.dumps(dict(id=sequence, method=method, params=params)))
                    while True:
                        message = json.loads(ws.recv())
                        if message.get('id') == sequence:
                            self.assertNotIn('error', message, message)
                            return message.get('result', {})

                def evaluate(expression):
                    result = command('Runtime.evaluate', expression=expression,
                                     returnByValue=True, awaitPromise=True)
                    self.assertNotIn('exceptionDetails', result, result)
                    return result.get('result', {}).get('value')

                command('Emulation.setDeviceMetricsOverride', width=width, height=1000,
                        deviceScaleFactor=1, mobile=width < 600)
                command('Page.navigate', url='http://127.0.0.1:%s/local-browser-fixture' % self.server.server_port)
                deadline = time.monotonic() + 15
                while not evaluate('''(function(){var n=document.getElementById('emotion-shadow-summary');
return !!n && n.textContent.includes('未校准');})()'''):
                    if time.monotonic() > deadline:
                        self.fail('Persona page did not finish loading in CDP Chrome')
                    threading.Event().wait(0.05)
                yield command, evaluate
            finally:
                if ws:
                    ws.close()
                process.terminate()
                process.wait(timeout=10)

    def test_real_keyboard_narrow_viewports_and_optional_screenshots(self):
        for width in (1280, 390, 320):
            with self.subTest(width=width), self.interactive_chrome(width) as (command, evaluate):
                self.assertEqual(evaluate('window.innerWidth'), width)
                self.assertTrue(evaluate("document.getElementById('emotion-shadow-details').hidden"))
                height = evaluate("document.querySelector('.shadow-card').getBoundingClientRect().height")
                self.assertLessEqual(height, 380 if width == 1280 else 620)
                self.assertTrue(evaluate('''document.querySelector('main').scrollWidth <= document.querySelector('main').clientWidth+2 &&
document.documentElement.scrollWidth <= innerWidth+2'''), '真实窄屏横向溢出')
                screenshot_dir = os.getenv('EMOTION_BROWSER_SCREENSHOTS')

                def screenshot(name):
                    if not screenshot_dir or width == 320:
                        return
                    path = Path(screenshot_dir)
                    self.assertTrue(path.is_absolute() and path.is_dir())
                    self.assertNotIn(ROOT, path.resolve().parents, 'screenshots must stay outside repo')
                    image = command('Page.captureScreenshot', format='png', captureBeyondViewport=False)
                    target = path / ('persona-%d-%s.png' % (width, name))
                    target.write_bytes(base64.b64decode(image['data']))
                    print('SCREENSHOT %s' % target, flush=True)

                screenshot('page')
                evaluate("document.querySelector('.shadow-card').scrollIntoView({block:'start'})")
                screenshot('collapsed')
                # A real Tab from the preceding refresh control must reach the accordion,
                # not invisible content. Enter/Space must toggle without losing focus.
                evaluate("document.getElementById('emotion-shadow-refresh').focus()")
                command('Input.dispatchKeyEvent', type='keyDown', key='Tab', code='Tab', windowsVirtualKeyCode=9)
                command('Input.dispatchKeyEvent', type='keyUp', key='Tab', code='Tab', windowsVirtualKeyCode=9)
                self.assertEqual(evaluate('document.activeElement.id'), 'emotion-shadow-toggle')
                command('Input.dispatchKeyEvent', type='keyDown', key='Enter', code='Enter', windowsVirtualKeyCode=13, text='\r')
                command('Input.dispatchKeyEvent', type='keyUp', key='Enter', code='Enter', windowsVirtualKeyCode=13)
                self.assertFalse(evaluate("document.getElementById('emotion-shadow-details').hidden"))
                panel_height = evaluate("document.getElementById('emotion-shadow-details').getBoundingClientRect().height")
                self.assertLessEqual(panel_height, 382)
                self.assertTrue(evaluate('''(function(){var n=document.getElementById('emotion-shadow-details');
return n.scrollWidth<=n.clientWidth+2 && n.scrollHeight>n.clientHeight;})()'''), '展开详情溢出或无法滚动')
                self.assertTrue(evaluate('''(function(){var n=Array.from(document.getElementById('emotion-shadow-values').children);
return n.length===4 && n.every(function(d){return Math.abs(d.getBoundingClientRect().top-n[0].getBoundingClientRect().top)<2;});})()'''), '真实窄屏四维换行')
                screenshot('expanded')
                command('Input.dispatchKeyEvent', type='keyDown', key='Tab', code='Tab', windowsVirtualKeyCode=9)
                command('Input.dispatchKeyEvent', type='keyUp', key='Tab', code='Tab', windowsVirtualKeyCode=9)
                self.assertEqual(evaluate('document.activeElement.id'), 'emotion-shadow-details')
                before = evaluate("document.getElementById('emotion-shadow-details').scrollTop")
                command('Input.dispatchKeyEvent', type='keyDown', key='PageDown', code='PageDown', windowsVirtualKeyCode=34)
                command('Input.dispatchKeyEvent', type='keyUp', key='PageDown', code='PageDown', windowsVirtualKeyCode=34)
                scrolled = evaluate('''new Promise(function(resolve){var start=performance.now();
(function check(){var n=document.getElementById('emotion-shadow-details');
if(n.scrollTop>0 || performance.now()-start>2000)resolve(n.scrollTop);else requestAnimationFrame(check);})();})''')
                self.assertGreater(scrolled, before, '键盘不能滚动详情')
                evaluate("document.getElementById('emotion-shadow-toggle').focus()")
                command('Input.dispatchKeyEvent', type='keyDown', key=' ', code='Space', windowsVirtualKeyCode=32)
                command('Input.dispatchKeyEvent', type='keyUp', key=' ', code='Space', windowsVirtualKeyCode=32)
                self.assertTrue(evaluate("document.getElementById('emotion-shadow-details').hidden"))
                print('CHROME width=%d collapsed=%.2fpx details=%.2fpx keyboard=passed' %
                      (width, height, panel_height), flush=True)

    def test_real_status_golden_path_and_refresh(self):
        real = self.real_status(now=OBSERVED_AT + 2)
        self.assertTrue(real['ok'])
        diag_assertion = ("has('diagnostics', '未提供');" if not real.get('diagnostics') else
                          "has('diagnostics', '有效驱动');")
        self.run_browser('real', '''
    assert(el('values').children.length===4, 'real state dimensions missing');
    has('summary', 'shadow'); has('summary', '未校准'); has('title', '影子观测检查');
    ''' + diag_assertion + '''
    await refresh();
    assert(el('values').children.length===4, 'real refresh lost state');
    assert(!text('values').includes('稳定'), 'unobserved state called stable');
    has('observation', '阶段3尚未开放');
''')
        self.assertGreaterEqual(self.flags['calls'], 2)

    def test_new_diagnostics_separate_drivers_from_receipts_and_exclusions(self):
        self.run_browser('data', '''
    has('title', '影子观测检查 8/10');
    metric('received_events','18'); metric('live_events','12'); metric('effective_events','5');
    metric('ignored_events','7'); metric('excluded_events','6'); metric('quarantined_events','2');
    metric('legacy_excluded_events','2');
    var rows=el('kinds').querySelectorAll('tbody tr');
    var receipt=Array.from(rows).find(function(r){return r.cells[0].textContent==='action_result';});
    var consolidation=Array.from(rows).find(function(r){return r.cells[0].textContent==='consolidation';});
    assert(receipt && receipt.cells[3].textContent==='0', 'receipt counted as emotional driver');
    assert(consolidation && consolidation.cells[3].textContent==='0', 'consolidation counted as driver');
    has('tools','web_read'); has('tools','成功 1'); has('tools','失败 1'); has('tools','未知 1');
    has('rules','tool_failure');
    var valence=text('dimension-evidence'), arousal=dimension('arousal').textContent;
    assert(valence.includes('数据来源') && valence.includes('驱动 3 次') && valence.includes('正向 2') &&
           valence.includes('负向 1') && valence.includes('-0.200') && valence.includes('0.400'), 'dimension evidence missing');
    assert(dimension('valence').textContent.includes('已驱动'), 'compact driver status missing');
    assert(arousal.includes('未观测') && !arousal.includes('稳定'), 'zero drives misrepresented');
    assert(dimension('attention').textContent.includes('未实现'), 'unimplemented dimension hidden');
    assert(!text('values').includes('稳定'), 'dimensions described as stable');
    assert(!document.querySelector('[data-tip*="被夸"],[data-tip*="话少"]'), 'unsupported tooltip causality');
    has('summary','未校准'); has('observation','未实现'); has('observation','阻止评审');
    has('freshness','2 秒'); has('backlog','待消费 0'); has('backlog','账本一致');
    has('continuity','当前断档 0 秒'); has('continuity','当前连续 600 秒');
    has('replay','最大误差 0'); has('replay','仅验证存储复现，不证明规则有效');
    has('version','shadow-fixture-v2');
    assert(!text('calib').includes('校准进度'), 'checks shown as calibration percentage');
    assert(el('calib').querySelector('[data-check-status="unknown"]'), 'unknown check hidden');
''')

    def test_empty_observations_keep_zero_distinct_from_missing(self):
        self.run_browser('empty', '''
    has('title','影子观测检查 0/9'); metric('received_events','0'); metric('effective_events','0');
    has('kinds','尚无种类统计'); has('tools','尚无工具结果'); has('rules','尚无规则统计');
    has('backlog','待消费 0'); has('backlog','最老等待 未提供');
    has('freshness','0 秒'); has('continuity','当前断档 0 秒');
    has('replay','未验证'); has('replay','检查事件 0'); has('replay','最大误差 未提供');
    assert(text('values').includes('未观测') && !text('values').includes('稳定'), 'empty != stable');
    assert(el('calib').querySelectorAll('[data-check-status="not_observed"]').length===9, 'not_observed coerced');
    has('observation','暂不可评审');
''')

    def test_unknown_is_not_zero_or_pass(self):
        self.run_browser('unknown', '''
    has('title','影子观测检查 8/9');
    metric('received_events','未提供'); metric('effective_events','未提供');
    has('kinds','未提供'); has('rules','未提供'); has('tools','未提供');
    has('freshness','未提供'); has('backlog','待消费 未知'); has('backlog','账本一致性 未提供');
    assert(!text('backlog').includes('待消费 0'), 'unknown backlog defaulted to zero');
    has('continuity','当前断档 未提供'); has('replay','未验证');
    has('replay','检查事件 未提供'); has('replay','最大误差 未提供');
    has('values','实现情况未提供'); has('observation','暂不可评审');
    assert(el('calib').querySelector('[data-check-status="unknown"]'), 'unknown became pass because ok=true');
    var bar=el('calib').querySelector('[role="progressbar"]');
    assert(bar && bar.getAttribute('aria-valuenow')==='8' && bar.getAttribute('aria-valuemax')==='9', 'progress ignores check status');
''')

    def test_ready_is_review_only_never_calibrated_or_stage3(self):
        self.run_browser('ready', '''
    has('title','影子观测检查 9/9'); has('summary','未校准');
    has('observation','可提交人工评审'); has('observation','不等于已校准');
    has('observation','阶段3尚未开放');
''')

    def test_missing_diagnostics_never_invents_counters_or_review_eligibility(self):
        self.run_browser('legacy', '''
    has('diagnostics','未提供'); metric('received_events','未提供'); metric('effective_events','未提供');
    has('dimension-evidence','维度诊断未提供'); has('backlog','待消费 未知'); has('continuity','当前断档 未提供');
    has('observation','暂不可评审');
    assert(!text('diagnostics').includes('999'), 'legacy counters substituted for diagnostics');
''')

    def test_unimplemented_dimension_blocks_even_when_every_check_passes(self):
        self.run_browser('unimplemented', '''
    has('title','影子观测检查 9/9'); has('observation','未实现'); has('observation','阻止评审');
    assert(!text('observation').includes('可提交人工评审'), 'unimplemented dimension allowed review');
''')

    def test_inconsistent_check_totals_fail_closed(self):
        self.run_browser('inconsistent', '''
    has('title','影子观测检查 9/9'); has('calib','检查汇总不一致');
    assert(!text('observation').includes('可提交人工评审'), 'inconsistent check totals allowed review');
''')

    def test_stale_heartbeat_open_gap_and_real_pending_block_review(self):
        self.run_browser('stale', '''
    has('summary','心跳已过期'); has('freshness','180 秒');
    has('backlog','待消费 3'); has('backlog','最老等待 45 秒'); has('backlog','账本不一致');
    has('continuity','当前断档 120 秒'); has('continuity','当前连续 0 秒');
    has('observation','暂不可评审');
''')

    def test_api_failure_clears_stale_panels_and_refresh_recovers(self):
        self.run_browser('data', '''
    metric('effective_events','5');
    for(var failure of ['error','html-error']){
      await refresh(failure);
      has('summary','暂不可读'); has('title','影子观测检查 未提供');
      assert(el('values').children.length===0 && el('timeline').children.length===0, 'stale state left after error');
      assert(!text('diagnostics').includes('web_read') && !text('observation').includes('可提交人工评审'), 'stale diagnostics left after error');
      await refresh('empty');
      has('title','影子观测检查 0/9'); metric('received_events','0');
    }
''')

    def test_persona_columns_precede_qingyi_shadow_and_settings(self):
        self.run_browser('data', '''
    document.querySelector('[data-view="persona"]').click();
    await waitFor(function(){return document.getElementById('persona-open-qingyi')._bound===true;});
    var groups=Array.from(document.querySelectorAll('#view-persona > .persona-section'));
    assert(groups.length===4, '人格页缺少四个清楚栏目');
    assert(groups.map(function(g){return g.querySelector('.grp').textContent.trim().split(' · ')[0];}).join('/')===
           '自我/互动关系/持续自我/生活记忆', '栏目顺序混乱');
    assert(!document.querySelector('#view-persona .shadow-card'), '影子观测卡仍留在人格页');
    assert(document.querySelector('#view-qingyi .shadow-card'), '影子观测卡未迁入清漪');
    assert(!document.querySelector('#view-persona #emotion-shadow-title') &&
           !document.querySelector('#view-persona #readiness-title'), '人格页仍藏影子或准入控件');
    assert(groups[0].contains(document.getElementById('pm-selfview')), '自我认知不在自我栏目');
    assert(groups[1].contains(document.getElementById('af-list')) && groups[1].contains(document.getElementById('whisper-in')),
           '高频互动未归组');
    assert(groups[3].contains(document.getElementById('ex-search')) && groups[3].contains(document.getElementById('pg-block')),
           '生活记忆未归组');
    var setup=document.getElementById('persona-settings');
    assert(setup && setup.hidden && setup.contains(document.getElementById('pf-name')) &&
           setup.contains(document.getElementById('rp-chars')), '低频设定未收起');
    var settingsToggle=document.getElementById('persona-settings-toggle');
    settingsToggle.click();
    assert(!setup.hidden && settingsToggle.getAttribute('aria-expanded')==='true', '设定无法展开');
    await refresh(); assert(!setup.hidden, '影子刷新破坏设定状态');
    settingsToggle.click(); assert(setup.hidden, '设定无法收起');
''')

    def test_affection_reset_uses_cancellable_inline_confirmation_not_native_dialog(self):
        self.flags['affection_resets'] = 0
        self.run_browser('data', '''
    document.querySelector('[data-view="persona"]').click();
    await waitFor(function(){return document.getElementById('persona-open-qingyi')._bound===true;});
    var reset=document.getElementById('af-reset'), panel=document.getElementById('af-reset-confirm');
    reset.click();
    assert(panel && !panel.hidden && reset.getAttribute('aria-expanded')==='true', '清零没有行内确认');
    document.getElementById('af-reset-no').click();
    assert(panel.hidden && document.activeElement===reset, '取消清零没有恢复焦点');
    reset.click(); document.getElementById('af-reset-yes').click();
    await waitFor(function(){return panel.hidden;});
''')
        self.assertEqual(self.flags['affection_resets'], 1)

    def test_default_shadow_height_and_compact_four_dimension_row(self):
        for width, limit in ((1280, 380), (390, 620), (320, 620)):
            with self.subTest(width=width):
                self.run_browser('data', '''
    var card=el('title').closest('.card'), h=card.getBoundingClientRect().height;
    assert(h<=LIMIT, '默认影子卡过高: '+h+'px');
    var dims=Array.from(el('values').children);
    assert(dims.length===4 && dims.every(function(n){return Math.abs(n.getBoundingClientRect().top-dims[0].getBoundingClientRect().top)<2;}),
           '四维不在同一紧凑行');
    assert(!text('values').includes('数据来源') && !text('values').includes('诊断范围'), '细节未移入折叠区');
    assert(document.documentElement.scrollWidth<=window.innerWidth+2 &&
           document.querySelector('main').scrollWidth<=document.querySelector('main').clientWidth+2, '人格页横向溢出');
'''.replace('LIMIT', str(limit)), width=width)

    def test_accordion_hides_complete_evidence_and_refresh_preserves_choice(self):
        self.run_browser('data', '''
    var toggle=el('toggle'), panel=el('details');
    assert(toggle && panel && toggle.tagName==='BUTTON' && toggle.getAttribute('aria-controls')===panel.id,
           '缺少自定义无障碍折叠按钮');
    assert(toggle.getAttribute('aria-expanded')==='false' && panel.hidden && panel.getClientRects().length===0,
           '影子详情未默认收起');
    for(var id of ['calib','diagnostics','timeline'])assert(panel.contains(el(id)), '证据不在折叠区: '+id);
    assert(!el('title').closest('.card').querySelector('details,select'), '使用原生折叠或选择框');
    await refresh('ready');
    assert(panel.hidden && toggle.getAttribute('aria-expanded')==='false', '刷新打开了已收起详情');
    toggle.click();
    assert(!panel.hidden && toggle.getAttribute('aria-expanded')==='true', '详情无法展开');
    assert(panel.getBoundingClientRect().height<=382 && panel.scrollHeight>panel.clientHeight &&
           getComputedStyle(panel).overflowY==='auto', '展开详情没有受限内部滚动');
    has('calib','第九项检查'); has('diagnostics','有效驱动'); has('timeline','只是回执');
    has('dimension-evidence','数据来源'); has('dimension-evidence','驱动 1 次');
    await refresh('stale');
    assert(!panel.hidden && toggle.getAttribute('aria-expanded')==='true', '刷新关闭了已展开详情');
    await refresh('error');
    assert(!panel.hidden, '请求失败破坏展开选择');
    toggle.click(); await refresh('data');
    assert(panel.hidden && toggle.getAttribute('aria-expanded')==='false', '恢复请求破坏收起选择');
''')

    def test_retrospective_is_backend_only_separate_from_live_and_handles_empty_error(self):
        self.run_browser('history', '''
    var history=el('retrospective');
    assert(history && el('details').contains(history), '今日历史回放不在详情内');
    has('retrospective','今日历史回放'); has('retrospective','已有账本追溯');
    has('retrospective','不计实时样本/时长'); has('retrospective','2026-11-08');
    has('retrospective','事件 42'); has('retrospective','有效驱动 17'); has('retrospective','排除 6');
    has('retrospective','history-fixture-v2'); has('retrospective','来源起止');
    has('retrospective','效价'); has('retrospective','驱动 3 次');
    metric('received_events','18'); metric('effective_events','5'); has('continuity','有效观测 3600 秒');
    assert(history.querySelectorAll('button,a,input').length===0, '历史回放主动触发动作');
    await refresh('history_empty'); has('retrospective','无可回放事件');
    assert(!text('retrospective').includes('事件 42'), '空回放遗留历史统计');
    await refresh('data'); has('retrospective','尚在准备');
    assert(!text('retrospective').includes('事件 0'), '未提供回放被伪装为零');
    await refresh('history_error'); has('retrospective','暂不可读'); has('retrospective','账本暂不可读');
    await refresh('error');
    assert(!text('retrospective').includes('事件 42'), 'API失败遗留历史结果');
''')

    def test_untrusted_dynamic_fields_are_text_and_mobile_layout_fits(self):
        attack = json.dumps('<img class="shadow-xss" src=x onerror="window.__shadowXss=1">')
        self.run_browser('xss', '''
    var attack=ATTACK;
    for(var id of ['kinds','rules','tools','calib','timeline','version','retrospective'])has(id,attack);
    assert(el('toggle') && el('details').hidden, 'XSS详情未默认隐藏');
    el('toggle').click();
    assert(!el('details').hidden && el('details').scrollWidth<=el('details').clientWidth+2, '展开XSS文本溢出');
    assert(!document.querySelector('.shadow-xss') && !window.__shadowXss, 'XSS executed');
    var card=el('title').closest('.card'), rect=card.getBoundingClientRect();
    assert(rect.width<=window.innerWidth && card.scrollWidth<=card.clientWidth+2, 'diagnostics overflow mobile card');
    assert(!card.querySelector('select,input,textarea'), 'native controls added');
    await refresh('xss_error'); has('retrospective',attack);
    assert(!document.querySelector('.shadow-xss') && !window.__shadowXss, 'history error XSS executed');
'''.replace('ATTACK', attack), width=390)


if __name__ == '__main__':
    unittest.main()
