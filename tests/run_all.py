"""全量回归的隔离入口。

直接 `unittest discover` 会写脏真实 `data/`：多个 bot 模块在导入期就把
`./data` 冻成常量（ceylan_fun_runtime、ceylan_observer、ceylan_monitor…），
只设 DATA_DIR 不够。这里在每个子进程导入测试之前注入全部数据路径，
再逐模块运行；单模块 240 秒超时，避免 bridge 重连把整个套件挂住。

用法：`venv/Scripts/python.exe tests/run_all.py [模块名…]`
不带参数跑全部 test_*.py；浏览器套件仍按其自身环境变量决定是否跳过。
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
TIMEOUT = 240


def isolated_env():
    root = tempfile.mkdtemp(prefix='qingyi_tests_')
    data = os.path.join(root, 'data')
    os.makedirs(data, exist_ok=True)
    env = dict(os.environ)
    env.update({
        'DATA_DIR': data,
        'PERSONA_DATA_DIR': os.path.join(data, 'persona'),
        'CEYLAN_BOT_SNAPSHOT': os.path.join(data, 'runtime-status.json'),
        'CEYLAN_CONTROL_DB': os.path.join(data, 'bot_control.db'),
        'CEYLAN_CONTROL_DIR': os.path.join(data, 'control-v4'),
        'CEYLAN_FUN_DB': os.path.join(data, 'bot_fun.db'),
        'CEYLAN_FUN_DIR': os.path.join(data, 'fun-v6'),
        'CEYLAN_KEYS_DB': os.path.join(data, 'keys_v5.db'),
        'CEYLAN_KEYS_DIR': os.path.join(data, 'keys-v5'),
        'CEYLAN_MODEL_ENV': os.path.join(data, 'model.env'),
        'CEYLAN_OBSERVER_FILE': os.path.join(data, 'observer-status.json'),
        'CEYLAN_TELEMETRY_DB': os.path.join(data, 'bot_telemetry.db'),
        'CEYLAN_DB_PATH': os.path.join(data, 'visitors.db'),
        'CEYLAN_BOT_STATUS_FILE': os.path.join(data, 'bot_status.json'),
        'CEYLAN_CLIPBOARD_DB': os.path.join(data, 'clipboard.db'),
        'QQ_DRY_RUN': '1',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONPATH': os.pathsep.join([TESTS, os.path.join(REPO, 'bot'), REPO]),
    })
    return env, root


def modules(argv):
    if argv:
        return [name[:-3] if name.endswith('.py') else name for name in argv]
    return sorted(os.path.splitext(os.path.basename(path))[0]
                  for path in glob.glob(os.path.join(TESTS, 'test_*.py')))


def main(argv=None):
    env, workdir = isolated_env()
    summary = {'ok': 0, 'fail': 0, 'timeout': 0}
    failed = []
    started = time.time()
    for stem in modules(argv or []):
        begin = time.time()
        try:
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', stem],
                                    cwd=workdir, env=env, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=TIMEOUT)
            ok = result.returncode == 0
            detail = result.stderr.strip().splitlines()[-1:] or ['']
        except subprocess.TimeoutExpired:
            ok, detail = False, ['TIMEOUT %ss' % TIMEOUT]
        summary['timeout' if detail == ['TIMEOUT %ss' % TIMEOUT] else
                ('ok' if ok else 'fail')] += 1
        if not ok:
            failed.append((stem, detail[0][:200]))
        print('%-4s %-52s %5.1fs  %s' % ('ok' if ok else 'FAIL', stem,
                                         time.time() - begin, detail[0][:120]), flush=True)
    print(json.dumps(dict(summary, seconds=round(time.time() - started, 1))), flush=True)
    for stem, line in failed:
        print('FAILED %s :: %s' % (stem, line), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))