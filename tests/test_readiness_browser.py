"""Real Chromium regression checks for readiness and explicit cognition controls.

Run from the repository root:
    READINESS_BROWSER_TEST=1 venv/Scripts/python.exe -m unittest tests.test_readiness_browser -v

Only HTTP read fixtures are synthetic. The dashboard, CSS, JavaScript, session,
CSRF checks and cognition-control writes are real, against temporary local data.
These UI fixtures do not certify engineering contracts or organic live coverage.
Set READINESS_BROWSER_ARTIFACT_DIR to retain a mobile screenshot outside the repo.
"""
import base64
import copy
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / 'bot', ROOT / 'website'):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

TOKEN = 'isolated-readiness-csrf'
RECIPIENT = '900000000001'  # Invented fixture identity, never a real QQ contact.
ENGINEERING = ('consent_enforcement', 'action_idempotency_outbox',
               'evidence_retraction', 'layer_versioning', 'model_switch_behavior')
LIVE = ('action_evidence', 'action_identity', 'action_results',
        'belief_grounding', 'self_view_grounding', 'shadow_review')


def readiness_fixture(case):
    checks = [{'key': 'ledger_integrity', 'status': 'pass',
               'observed': {'phase0': 'ok', 'phase1': 'partial', 'phase2': 'ok'},
               'reason': 'Integrity does not authorize expression.'}]
    for key in LIVE:
        checks.append({'key': key, 'status': 'not_observed',
                       'observed': {'total': 0, 'excluded': 2},
                       'reason': 'No organic live samples in this isolated UI fixture.'})
    for key in ENGINEERING:
        checks.append({'key': key, 'status': 'pass',
                       'observed': {'status': 'pass', 'execution': {
                           'run': 3, 'skipped': 0, 'failures': 0, 'errors': 0}},
                       'reason': 'Isolated engineering evidence only; not live organic evidence.'})
    if case == 'blocked':
        checks[1].update(status='blocked', observed={'total': 17, 'linked': 3, 'missing': 14},
                         reason='Every recent action needs existing evidence.')
        checks[-1].update(status='not_verified', observed=None,
                          reason='Independent contract evidence unavailable: failed contracts')
    if case == 'stale':
        for check in checks:
            if check['key'] in ENGINEERING:
                check.update(status='not_verified', observed=None,
                             reason='Independent contract evidence unavailable: stale code/runtime evidence')
    if case == 'no-samples':
        for check in checks:
            check.update(status='not_observed', observed=None,
                         reason='No samples; absence is not a pass.')
    if case == 'mobile':
        checks[1].update(status='blocked', observed={
            'total': 123456789, 'linked': 123456788, 'missing': 1,
            'nested': {'artifact_sha256': 'abcdef0123456789' * 12,
                       'path': 'fixture/' + 'long-contract-metric-' * 12}},
            reason='<img src=x onerror="window.readinessXss=true"> & fixture reason ' * 4)
    now = int(time.time())
    history = copy.deepcopy(checks)
    history[1].update(status='blocked', observed={'total': 111, 'linked': 0, 'missing': 111},
                      reason='<img src=x onerror="window.readinessXss=true"> Historical defects remain; never repaired by a new boundary.')
    if case == 'mobile':
        history[1]['observed'] = copy.deepcopy(checks[1]['observed'])
        history[1]['reason'] += ' historical-long-reason-' * 30
    release = {'valid': True, 'deployed_at': now - 10800, 'reason': 'validated fixture release'}
    observation = {'status': 'not_observed', 'boundary_at': now - 10800,
                   'label': 'Current organic observation since max(rolling window start, validated deployment).',
                   'reason': 'Wait for real organic samples; never backfill history or authorize stage 3.'}
    if case == 'rolling':
        release['deployed_at'] = now - 172800
        observation['boundary_at'] = now - 86400
    if case in ('ready', 'invalid-ready'):
        for check in checks:
            check.update(status='pass', observed={'total': 7, 'missing': 0})
        observation['status'] = 'observed'
    if case == 'missing-release':
        release = None
    elif case == 'not-configured':
        release.update(valid=False, deployed_at=None, reason='missing')
        observation.update(status='not_configured', boundary_at=None)
    elif case == 'missing-observation':
        observation = None
    elif case == 'invalid-release':
        release.update(valid=False, reason='stale code/runtime boundary')
        observation.update(status='invalid_boundary', boundary_at=None)
    elif case == 'missing-boundary':
        observation.pop('boundary_at')
    elif case in ('invalid-boundary', 'invalid-ready'):
        observation['boundary_at'] = 'not-a-timestamp'
    elif case == 'future-boundary':
        observation['boundary_at'] = now + 3600
    elif case == 'before-deployment':
        observation['boundary_at'] = release['deployed_at'] - 1
    elif case == 'wrong-rolling-boundary':
        observation['boundary_at'] = now - 100
    elif case == 'invalid-deployment':
        release['deployed_at'] = None
    elif case == 'invalid-status':
        observation['status'] = 'invalid_boundary'
    elif case == 'string-valid':
        release['valid'] = 'true'
    blockers = [check['key'] for check in checks if check['status'] != 'pass']
    return {'ok': True, 'transport_ok': True, 'checked_at': now,
            'phase25': {'status': 'blocked' if blockers else 'review_ready',
                        'window_seconds': 86400, 'checks': checks, 'blockers': blockers,
                        'observation_release': release, 'observation': observation,
                        'history_checks': history,
                        'history_label': 'Historical rolling actions and revisions; all-time nonlegacy beliefs. Not backfilled or repaired.',
                        'history_boundary_at': now - 86400},
            'stage3': {'authorized': False, 'expression_control': False,
                       'stage3a': 'blocked', 'stage3b': 'blocked'}}


class _DocumentAttributes(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.attributes = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'html':
            self.attributes.update(attrs)


DRIVER = r'''<script>
window.addEventListener('load', async function(){
  var $ = function(id){return document.getElementById(id);};
  var text = function(id){return $(id).textContent;};
  function assert(ok, why){if(!ok)throw new Error(why);}
  function waitFor(check, why){return new Promise(function(resolve,reject){
    var start=performance.now(), timer=setInterval(function(){
      try{if(check()){clearInterval(timer);resolve();return;}}
      catch(e){clearInterval(timer);reject(e);return;}
      if(performance.now()-start>11000){clearInterval(timer);reject(new Error(why));}
    },40);
  });}
  async function settle(){
    await waitFor(function(){return !/读取中|未提供/.test(text('readiness-title'));},
      'readiness never settled: '+text('readiness-title'));
  }
  async function phase(name){
    await fetch('/local-readiness-phase/'+name,{method:'POST'});
    $('readiness-refresh').click();
    await settle();
  }
  function rows(){return Array.from(document.querySelectorAll('#readiness-checks .rd-row'));}
  function row(key){return rows().find(function(r){return r.querySelector('b').textContent.includes('（'+key+'）');});}
  function unauthorized(){
    var stage=text('readiness-stage');
    assert(stage.includes('授权=否') && stage.includes('接管表达=未开'), 'fixture was presented as stage3 authorization: '+stage);
  }
  function mark(name){document.documentElement.dataset[name]='passed';}
  try{
    document.querySelector('[data-view="qingyi"]').click();
    document.querySelector('[data-qingyi-tab="evidence"]').click();
    await settle();
    ASSERTIONS
    assert(!window.readinessXss, 'ledger text executed as HTML');
    assert(!(window.readinessErrors || []).length, 'uncaught browser errors: '+JSON.stringify(window.readinessErrors));
    $('readiness-title').closest('.card').scrollIntoView({block:'start'});
    mark('readinessComplete');
  }catch(e){document.documentElement.dataset.readinessError=e.stack || e.message;}
});
</script>'''


@unittest.skipUnless(os.getenv('READINESS_BROWSER_TEST') == '1',
                     'explicit isolated local Chromium regression tests')
class ReadinessBrowserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='readiness_browser_')
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        environment = patch.dict(os.environ, {
            'DATA_DIR': str(self.tmp), 'PERSONA_DATA_DIR': str(self.tmp / 'persona'),
            'QQ_DRY_RUN': '1',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.chrome = next((p for p in (
            os.getenv('CHROME_BIN'), shutil.which('chromium'), shutil.which('google-chrome'),
            'C:/Program Files/Google/Chrome/Application/chrome.exe',
            'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
        ) if p and Path(p).is_file()), None)
        self.assertIsNotNone(self.chrome, 'Chromium is required when READINESS_BROWSER_TEST=1')

    def run_browser(self, case, assertions, mobile=False):
        from flask import Flask, jsonify, redirect, request, session
        from werkzeug.serving import make_server
        import ceylan_dashboard
        import phase_audit

        app = Flask(__name__, static_folder=None)
        app.secret_key = 'readiness-browser-fixture-only'
        state = {'phase': case}
        self.writes = []
        self.reads = []
        self.audit_calls = []

        def audit(**kwargs):
            self.audit_calls.append(state['phase'])
            if state['phase'] == 'endpoint503':
                raise RuntimeError('isolated fixture unavailable')
            result = readiness_fixture(state['phase'])
            if state['phase'] == 'endpoint-error':
                result.update(ok=False, error='fixture audit error')
            return copy.deepcopy(result)

        audit_patch = patch.object(phase_audit, 'audit_readiness', side_effect=audit)
        audit_patch.start()
        self.addCleanup(audit_patch.stop)

        @app.before_request
        def fixtures():
            path = request.path
            if path == '/admin/api/bot/cognition-control':
                self.writes.append({'body': request.get_json(silent=True),
                                    'csrf': request.headers.get('X-CSRF-Token'),
                                    'admin': session.get('ceylan_admin')})
                return None  # Exercise the real authenticated CSRF/validation/write route.
            if path == '/admin/api/bot/readiness':
                self.reads.append(state['phase'])
                return None  # Exercise real endpoint transport and 503 handling.
            if path == '/admin/api/me':
                return jsonify(ok=True, role='admin', username='isolated-fixture')
            if path == '/admin/api/bot/effective-config':
                return jsonify(control_csrf=TOKEN, settings={})
            if path == '/admin/api/bot/control':
                return jsonify(ok=True, fresh=True, state={'settings': {}, 'groups': []}, operations=[])
            if path.startswith('/admin/api/') or path == '/api/status':
                return jsonify({})  # No production data, tools, media or external transport.

        @app.get('/local-readiness-login')
        def login():
            session['ceylan_admin'] = True
            session['ceylan_role'] = 'admin'
            session['bot_control_csrf'] = TOKEN
            return redirect('/admin/dashboard')

        @app.post('/local-readiness-phase/<name>')
        def change_phase(name):
            state['phase'] = name
            return jsonify(ok=True)

        @app.get('/local-readiness-write-count')
        def write_count():
            return jsonify(count=len(self.writes))

        ceylan_dashboard.install(app, {})
        driver = DRIVER.replace('ASSERTIONS', assertions)

        @app.after_request
        def inject(response):
            if request.path == '/admin/dashboard':
                html = response.get_data(as_text=True)
                html = html.replace('<head>', '''<head><script>
window.readinessErrors=[];
window.addEventListener('error',function(e){if(e.message)window.readinessErrors.push(e.message);});
window.addEventListener('unhandledrejection',function(e){window.readinessErrors.push(String(e.reason));});
</script>''', 1)
                response.set_data(html.replace('</body>', driver + '</body>'))
            response.headers['Cache-Control'] = 'no-store'
            return response

        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop_server)
        command = [self.chrome, '--headless=new', '--disable-gpu', '--disable-extensions',
                   '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
                   '--force-device-scale-factor=1', '--user-data-dir=' + str(self.tmp / 'profile'),
                   '--window-size=' + ('390,1500' if mobile else '1600,1100'),
                   '--virtual-time-budget=30000', '--dump-dom']
        artifacts = os.getenv('READINESS_BROWSER_ARTIFACT_DIR')
        url = 'http://127.0.0.1:%s/local-readiness-login' % server.server_port
        if mobile:
            # Windows headless --window-size can screenshot a narrower surface than
            # its layout viewport. CDP fixes both to exactly 390 CSS pixels.
            return self.run_mobile_browser(command[:-2], url, artifacts)
        command.append(url)
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=90)
        attrs = _DocumentAttributes(result.stdout).attributes
        failure = (attrs.get('data-readiness-error') or result.stdout[-1500:]) + '\n' + result.stderr[-1500:]
        self.assertEqual(result.returncode, 0, failure)
        self.assertNotIn('data-readiness-error', attrs, failure)
        self.assertEqual(attrs.get('data-readiness-complete'), 'passed', failure)
        return attrs

    def run_mobile_browser(self, command, url, artifacts):
        import websocket
        process = subprocess.Popen(command + ['--remote-debugging-port=0', 'about:blank'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop_browser():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)

        self.addCleanup(stop_browser)
        port_file = self.tmp / 'profile' / 'DevToolsActivePort'
        deadline = time.monotonic() + 15
        while not port_file.is_file():
            self.assertIsNone(process.poll(), 'Chromium exited before CDP became ready')
            self.assertLess(time.monotonic(), deadline, 'CDP startup timed out')
            threading.Event().wait(0.05)
        port = port_file.read_text().splitlines()[0]
        with urlopen('http://127.0.0.1:%s/json/list' % port, timeout=10) as response:
            target = next(t for t in json.load(response) if t['type'] == 'page')
        socket = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=20,
                                             suppress_origin=True)
        self.addCleanup(socket.close)
        sequence = 0

        def cdp(method, **params):
            nonlocal sequence
            sequence += 1
            socket.send(json.dumps({'id': sequence, 'method': method, 'params': params}))
            while True:
                message = json.loads(socket.recv())
                if message.get('id') == sequence:
                    self.assertNotIn('error', message, message)
                    return message.get('result', {})

        def evaluate(expression):
            result = cdp('Runtime.evaluate', expression=expression, returnByValue=True)
            self.assertNotIn('exceptionDetails', result, result)
            return result.get('result', {}).get('value')

        cdp('Emulation.setDeviceMetricsOverride', width=390, height=1500,
            deviceScaleFactor=1, mobile=True)
        cdp('Page.navigate', url=url)
        deadline = time.monotonic() + 20
        attrs = {}
        while not (attrs.get('readinessComplete') or attrs.get('readinessError')):
            self.assertLess(time.monotonic(), deadline, 'mobile driver did not finish: %s' % attrs)
            attrs = evaluate('Object.assign({}, document.documentElement.dataset)') or {}
            threading.Event().wait(0.05)
        self.assertEqual(evaluate('innerWidth'), 390, 'mobile emulation must be exact')
        if artifacts:
            destination = Path(artifacts).resolve()
            self.assertTrue(destination.is_dir(), 'Create the artifact directory before running')
            self.assertNotIn(ROOT, destination.parents, 'Screenshots must stay outside the repository')
            evaluate("document.getElementById('readiness-title').closest('.card').scrollIntoView({block:'start'})")
            image = cdp('Page.captureScreenshot', format='png', captureBeyondViewport=False)
            (destination / 'readiness-mobile.png').write_bytes(base64.b64decode(image['data']))
        self.assertNotIn('readinessError', attrs, attrs.get('readinessError'))
        self.assertEqual(attrs.get('readinessComplete'), 'passed', attrs)
        return attrs

    def test_golden_engineering_pass_still_waits_for_live_coverage(self):
        self.run_browser('golden', r'''
    assert(text('readiness-title').includes('等待自然覆盖'), text('readiness-title'));
    assert(rows().length===12, 'missing checks');
    ['consent_enforcement','action_idempotency_outbox','evidence_retraction','layer_versioning','model_switch_behavior'].forEach(function(key){
      assert(row(key).querySelector('.tag.gr').textContent==='通过', key+' did not show engineering pass');
      assert(row(key).textContent.includes('not live organic evidence'), 'engineering/live distinction lost');
    });
    assert(row('action_evidence').querySelector('.tag.am').textContent==='暂无样本', 'no live samples shown as pass');
    assert(text('readiness-summary').includes('6 项未过'), 'waiting count incorrect');
    assert(text('readiness-window').includes('24 小时'), 'window missing');
    unauthorized();
''')
        self.assertEqual(self.writes, [])

    def test_blocked_checks_and_failed_engineering_are_not_green(self):
        self.run_browser('blocked', r'''
    assert(text('readiness-title').includes('存在工程阻塞'), text('readiness-title'));
    assert(row('action_evidence').querySelector('.tag.rd').textContent==='阻塞', 'blocked action became green');
    assert(row('action_evidence').textContent.includes('missing=14'), 'actual missing count absent');
    assert(row('model_switch_behavior').querySelector('.tag.am').textContent==='本账无法验证', 'engineering failure hidden');
    unauthorized();
''')

    def test_refresh_replaces_golden_passes_with_stale_artifact_blockers(self):
        self.run_browser('golden', r'''
    await phase('stale');
    assert(text('readiness-title').includes('存在工程阻塞'), text('readiness-title'));
    ['consent_enforcement','action_idempotency_outbox','evidence_retraction','layer_versioning','model_switch_behavior'].forEach(function(key){
      assert(!row(key).querySelector('.tag.gr'), 'stale engineering pass retained: '+key);
      assert(row(key).textContent.includes('stale code/runtime evidence'), 'stale artifact reason absent');
    });
    unauthorized();
''')
        self.assertIn('stale', self.reads)

    def test_503_and_json_error_clear_previous_successful_check_rows(self):
        self.run_browser('golden', r'''
    for(var name of ['endpoint503','endpoint-error']){
      await phase(name);
      assert(text('readiness-title').includes('读不到'), 'error was not shown: '+name);
      assert(text('readiness-summary').includes('暂不可读'), 'error summary absent');
      assert(rows().length===0 && !document.querySelector('#readiness-checks .tag.gr'), 'previous passes survived '+name);
      assert(!/读取中|加载中/.test(text('readiness-checks')), 'error left loading placeholder');
      assert(text('readiness-window')==='', 'old statistics window survived error');
      ['readiness-observation','readiness-engineering','readiness-coverage','readiness-clock','readiness-history-boundary'].forEach(function(id){
        assert(text(id)==='', 'stale metadata survived error: '+id);
      });
      assert($('readiness-history').hidden && !text('readiness-history-checks'), 'old historical passes survived error');
      assert(!text('readiness-stage').includes('授权=是'), 'error authorized stage3');
    }
    await phase('golden');
    assert(rows().length===12 && text('readiness-title').includes('等待自然覆盖'), 'manual recovery failed');
    unauthorized();
''')
        self.assertEqual(self.reads.count('endpoint503'), 3, 'safe reads should retry only twice')
        self.assertEqual(self.reads.count('endpoint-error'), 1)

    def test_no_samples_is_amber_not_a_pass_or_failure(self):
        self.run_browser('no-samples', r'''
    assert(text('readiness-title').includes('等待自然覆盖'), text('readiness-title'));
    assert(rows().length===12, 'empty fixture lost check definitions');
    rows().forEach(function(r){
      assert(r.querySelector('.tag.am').textContent==='暂无样本', 'no sample not distinguished');
      assert(r.querySelector('.rd-obs').textContent==='无读数', 'fabricated sample count');
    });
    assert(!document.querySelector('#readiness-checks .tag.gr,#readiness-checks .tag.rd'), 'no samples falsely passed/failed');
    unauthorized();
''')

    def test_long_nested_metrics_and_controls_fit_mobile_without_clipping(self):
        self.run_browser('mobile', r'''
    assert(innerWidth<=500, 'not a mobile viewport: '+innerWidth);
    var card=$('readiness-title').closest('.card'), bounds=card.getBoundingClientRect();
    assert(bounds.width>250, 'card collapsed');
    assert(bounds.left>=0 && bounds.right<=innerWidth+1,
      'readiness card exceeds mobile viewport: '+bounds.left+'..'+bounds.right+' / '+innerWidth);
    $('readiness-history').querySelector('summary').click();
    assert($('readiness-history').open, 'mobile history did not expand');
    Array.from(card.querySelectorAll('.rd-row,.rd-obs,b,form,label,input,select,button,output,details,summary,#readiness-observation,#readiness-clock,#readiness-engineering,#readiness-coverage,#readiness-history-boundary')).forEach(function(el){
      var r=el.getBoundingClientRect();
      assert(r.left>=bounds.left-1 && r.right<=bounds.right+1,
        'mobile element outside readiness card: '+el.tagName+'#'+el.id+' '+r.left+'..'+r.right+' card '+bounds.left+'..'+bounds.right);
      if(el.matches('.rd-row,.rd-obs,form'))assert(el.scrollWidth<=el.clientWidth+1, 'horizontal clipping: '+el.className+'#'+el.id);
    });
    var metric=row('action_evidence').querySelector('.rd-obs');
    assert(metric.textContent.includes('artifact_sha256=') && metric.textContent.includes('missing=1'), 'long nested metrics omitted');
    assert(metric.getBoundingClientRect().height>parseFloat(getComputedStyle(metric).lineHeight)*2, 'long metrics did not wrap');
    assert(getComputedStyle(metric).textOverflow!=='ellipsis', 'metric was ellipsized');
    var historyMetric=document.querySelector('#readiness-history-checks .rd-row:nth-child(2) .rd-obs');
    assert(historyMetric.textContent.includes('artifact_sha256=') && historyMetric.getBoundingClientRect().height>parseFloat(getComputedStyle(historyMetric).lineHeight)*2, 'historical metrics did not wrap');
    assert(!document.querySelector('#readiness-history-checks img'), 'history reason parsed as HTML');
    ['cognition-recipient','cognition-id','cognition-reason'].forEach(function(id){
      var input=$(id), style=getComputedStyle(input);
      assert(input.type==='text' && style.backgroundColor!=='rgb(255, 255, 255)',
        'cognition input fell back to unstyled white native control: '+id);
    });
    assert(!row('action_evidence').querySelector('img'), 'untrusted ledger reason parsed as markup');
    assert(row('action_evidence').textContent.includes('<img src=x'), 'escaped reason not visible');
    unauthorized();
''', mobile=True)

    def test_current_boundary_clock_and_five_gates_are_separate_from_history(self):
        self.run_browser('golden', r'''
    assert(text('readiness-engineering').includes('5/5'), 'five engineering gates not counted independently');
    assert(text('readiness-coverage').includes('0/5') && text('readiness-coverage').includes('5 项暂无样本'), 'organic coverage merged with engineering');
    var observation=text('readiness-observation');
    assert(/观测起点：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\+8/.test(observation), 'readable boundary missing: '+observation);
    assert(observation.includes('已历时：3 小时 0 分 0 秒'), 'elapsed time not based on audit timestamp: '+observation);
    var boundaryText=observation.match(/观测起点：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\+8/)[1];
    var parsedBoundary=Date.parse(boundaryText.replace(' ','T')+'+08:00');
    assert(Math.abs(Date.now()-parsedBoundary-10800000)<60000, 'boundary is not actually rendered in UTC+8');
    assert(observation.includes('validated fixture release') && observation.includes('never backfill history'), 'boundary reasons lost');
    assert(/本次读取时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\+8/.test(text('readiness-clock')), 'explicit current timestamp absent');
    assert(text('readiness-clock').includes('不是倒计时') && text('readiness-clock').includes('不保证'), 'time falsely promises readiness');
    var history=$('readiness-history');
    assert(!history.open && !history.hidden, 'history must start collapsed but discoverable');
    assert(text('readiness-history-summary').includes('12 项') && text('readiness-history-summary').includes('未过'), 'history counts absent from disclosure');
    history.querySelector('summary').click();
    var historic=document.querySelector('#readiness-history-checks .rd-row:nth-child(2)');
    assert(history.open && historic.textContent.includes('total=111') && historic.textContent.includes('missing=111'), 'real historic defect counts discarded');
    assert(historic.textContent.includes('<img src=x') && !historic.querySelector('img'), 'history reason is not safe visible text');
    assert(text('readiness-history-boundary').includes('UTC+8') && text('readiness-history-boundary').includes('all-time nonlegacy beliefs'), 'history boundary/scope lost');
    assert(!row('action_evidence').textContent.includes('missing=111'), 'old defects leaked into current checks');
    await phase('stale');
    assert(history.open, 'refresh discarded disclosure choice');
    assert(text('readiness-engineering').includes('0/5'), 'stale artifact retained five engineering passes');
    unauthorized();
''')
        self.assertEqual(self.writes, [])

    def test_missing_invalid_and_inconsistent_boundaries_fail_closed(self):
        self.run_browser('ready', r'''
    assert(text('readiness-title').includes('可提交评审'), 'valid ready fixture not rendered');
    for(var name of ['missing-release','not-configured','missing-observation','invalid-release','missing-boundary','invalid-boundary','future-boundary','before-deployment','wrong-rolling-boundary','invalid-deployment','invalid-status','string-valid','invalid-ready']){
      await phase(name);
      assert(/观测边界未配置|观测边界无效/.test(text('readiness-title')), 'boundary failed open: '+name+' '+text('readiness-title'));
      assert(!text('readiness-title').includes('可提交评审'), 'invalid boundary shown as ready: '+name);
      assert(text('readiness-observation').includes('不能建立当前发布观测') && !text('readiness-observation').includes('已历时：'), 'invented observation start/duration: '+name);
      assert(text('readiness-coverage').includes('未确认') && !text('readiness-coverage').includes('5/5'), 'unvalidated scope reported current organic passes: '+name);
      assert(rows().length===12, 'failclosed boundary discarded original admission checks: '+name);
      assert(document.querySelectorAll('#readiness-history-checks .rd-row').length===12, 'failclosed boundary discarded historical coverage: '+name);
      unauthorized();
    }
    await phase('golden');
    assert(text('readiness-observation').includes('已历时：3 小时') && text('readiness-title').includes('等待自然覆盖'), 'valid boundary did not recover');
''')

    def test_rolling_boundary_limits_duration_without_restarting_deployment(self):
        self.run_browser('rolling', r'''
    var observation=text('readiness-observation');
    assert(observation.includes('发布起点：') && observation.includes('观测起点：'), 'deployment/rolling boundary distinction missing');
    assert(observation.includes('已历时：1 天 0 小时 0 分 0 秒'), 'duration did not use max(window, deployment): '+observation);
    assert(text('readiness-window').includes('24 小时'), 'rolling duration absent');
    assert(text('readiness-history-boundary').includes('不等于全部历史下限'), 'all-time belief scope misleadingly bounded');
    unauthorized();
''')

    def test_ready_current_scope_never_hides_failed_history(self):
        self.run_browser('ready', r'''
    assert(text('readiness-title').includes('可提交评审'), 'current review verdict was replaced by history');
    assert(text('readiness-engineering').includes('5/5') && text('readiness-coverage').includes('5/5'), 'independent gate/coverage counts incorrect');
    $('readiness-history').querySelector('summary').click();
    var historic=document.querySelector('#readiness-history-checks .rd-row:nth-child(2)');
    assert(historic.querySelector('.tag.rd') && historic.textContent.includes('missing=111'), 'historical failure silently removed');
    assert(row('action_evidence').querySelector('.tag.gr'), 'history incorrectly replaced current passing check');
    unauthorized();
''')

    def test_controls_post_real_csrf_and_explicit_reasons_to_isolated_ledger(self):
        import self_model
        consent_id = self_model.record_consent(RECIPIENT, 'group', 'allow', 'fixture seed only')
        event_id = self_model.record_event('observation', source='synthetic:readiness-browser',
                                           payload={'synthetic': True, 'text': 'invented fixture'})
        assertions = r'''
    var form=$('cognition-control-form'), button=form.querySelector('button[type="submit"]');
    $('cognition-recipient').value=RECIPIENT;
    assert($('cognition-reason').required, 'explicit reason is not required');
    button.click();
    assert(!$('cognition-reason').validity.valid, 'empty reason passed native validation');
    var counts=await (await fetch('/local-readiness-write-count')).json();
    assert(counts.count===0, 'empty required reason sent a POST');
    async function submit(operation, id, reason){
      $('cognition-operation').value=operation; $('cognition-id').value=id;
      $('cognition-reason').value=reason; $('cognition-result').textContent='';
      button.click();
      await waitFor(function(){return text('cognition-result')!=='' && !button.disabled;}, 'control response never settled');
      assert(text('cognition-result').includes('已记录指令；未授权阶段3'), 'control did not confirm actual write: '+text('cognition-result'));
      await settle(); unauthorized();
    }
    await submit('consent','','fixture explicit denial');
    await submit('revoke_consent',CONSENT_ID,'fixture explicit revocation');
    await submit('retract_evidence',EVENT_ID,'fixture explicit evidence correction');
    var bad=await fetch('/admin/api/bot/cognition-control',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({operation:'consent',recipient:RECIPIENT,action:'dm',decision:'allow',reason:'fixture missing token'})});
    assert(bad.status===403, 'real route accepted missing CSRF');
    var empty=await fetch('/admin/api/bot/cognition-control',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':TOKEN},
      body:JSON.stringify({operation:'consent',recipient:RECIPIENT,action:'dm',decision:'allow',reason:'   '})});
    assert(empty.status===400, 'real route accepted whitespace reason');
    assert((await empty.json()).error==='explicit_reason_required', 'wrong validation failure');
'''
        for name, value in (('RECIPIENT', RECIPIENT), ('CONSENT_ID', consent_id),
                            ('EVENT_ID', event_id), ('TOKEN', TOKEN)):
            assertions = assertions.replace(name, json.dumps(value))
        self.run_browser('golden', assertions)
        self.assertEqual(len(self.writes), 5, 'writes must not be retried')
        for sent, operation, reason in zip(self.writes[:3],
                ('consent', 'revoke_consent', 'retract_evidence'),
                ('fixture explicit denial', 'fixture explicit revocation', 'fixture explicit evidence correction')):
            self.assertTrue(sent['admin'])
            self.assertEqual(sent['csrf'], TOKEN)
            self.assertEqual(sent['body']['operation'], operation)
            self.assertEqual(sent['body']['reason'], reason)
        self.assertEqual(self_model.consent_decision(RECIPIENT, 'dm'), 'deny')
        self.assertEqual(self_model.consent_decision(RECIPIENT, 'group'), 'unknown')
        self.assertIsNotNone(self_model.get_event(event_id), 'retraction must not delete the event')
        self.assertFalse(self_model.evidence_valid('event', event_id))
        self.assertGreaterEqual(len(self.reads), 4, 'successful controls must refresh readiness')
        self.assertTrue((self.tmp / 'persona' / 'cognition.db').is_file())


if __name__ == '__main__':
    unittest.main()
