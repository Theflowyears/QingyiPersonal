"""面板「执行结果」列表到底给不给人看最新那条。

主人报「改完设置以后好像没生效」，先按他说的查路径：命令进了 inbox、机器人
ledger 里写着 `done 已执行并保存`、运行值和磁盘逐键相等 —— 参数是真的生效了。
骗他的是面板这一行：

    website/ceylan_bot_control.py:  return list(reversed(ops[:30]))

`ops` 是按时间**升序**排的，`[:30]` 取的是最老的 30 条。生产 ledger 攒到 31 条
那天起（实测就是今天：31 条，最新一条 70 秒前，而列表里最新的一条 889 秒前），
刚保存的那条回执永远不出现在列表里 —— 面板看起来就是「改了没反应」。
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "website")

SCRIPT = textwrap.dedent('''
    import json, os, sys, tempfile, time
    # 生产上是 website/ 里那三个软链把 bot/ 的模块接过来的，本地没有软链，
    # 所以这里照生产的样子把 bot/ 也加进搜索路径。
    sys.path.insert(0, os.environ["WEB"])
    sys.path.append(os.environ["BOT"])
    import ceylan_bot_control as ctl

    root = tempfile.mkdtemp()
    ctl.BOT_STATE_DIR = __import__("pathlib").Path(root)
    led = {}
    base = time.time()
    for i in range(40):
        led["%032x" % i] = {"id": "%032x" % i, "time": base + i,
                            "status": "done", "message": "已执行并保存"}
    with open(os.path.join(root, "ledger.json"), "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False)

    ops = ctl._read_local_ledger()
    print(json.dumps({"n": len(ops),
                      "newest_id": max(led, key=lambda k: led[k]["time"]),
                      "top_id": ops[0]["id"] if ops else "",
                      "times": [o["created"] for o in ops[:3]]}, ensure_ascii=False))
''')


class LedgerWindowTests(unittest.TestCase):
    def run_script(self):
        env = dict(os.environ)
        env["WEB"] = WEB
        env["BOT"] = os.path.join(ROOT, "bot")
        env["PYTHONIOENCODING"] = "utf-8"
        got = subprocess.run([sys.executable, "-c", SCRIPT], env=env, cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 0, got.stderr[-600:])
        return json.loads(got.stdout.strip().splitlines()[-1])

    def test_list_shows_the_newest_receipts_not_the_oldest(self):
        out = self.run_script()
        self.assertEqual(out["top_id"], out["newest_id"],
                         "列表第一条必须是刚那条回执，不能是最老的那条")

    def test_list_is_capped_but_still_recent(self):
        out = self.run_script()
        self.assertEqual(out["n"], 30)
        self.assertGreater(out["times"][0], out["times"][-1])


if __name__ == "__main__":
    unittest.main()
