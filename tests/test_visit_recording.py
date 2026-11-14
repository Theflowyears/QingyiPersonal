"""回归锁：访问上报那条链路的口径（app.py 里真正跑的那份，不是 visit_log 单元）。

为什么要单独立一个文件：`website/app.py` 和 `bot/project_config.py` 同名，
在同一进程里 import 会拿错模块，所以这一段用子进程跑，PYTHONPATH 只给 website/。
锁住三件用户会直接看见的事：
  · 打开后台不算访客（现网 1000 条里 628 条是 /admin*，就是这么来的）；
  · 累计不跟明细一起被保留期删掉；
  · 面板 API 把累计和保留期分开报，别让人自己猜。
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
    import os, sys, json, datetime
    sys.path.insert(0, os.environ["WEB_DIR"])
    import app
    from visit_log import day_of

    con = app.app.test_client()

    def hit(path):
        con.get(path, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131",
                               "X-Forwarded-For": "203.0.113.%d" % (hit.n % 200)})
        hit.n += 1

    hit.n = 0
    for p in ("/", "/docs", "/about"):
        hit(p)
    # 后台自己：这些一条都不该算成访客
    con.post("/admin/login", data={"username": os.environ["CEYLAN_ADMIN_USER"],
                                   "password": "test-password"})
    for p in ("/admin", "/admin/dashboard", "/admin/login", "/admin/assets/console.js",
              "/admin/api/visits", "/api/status", "/static/x.js"):
        hit(p)

    d = json.loads(con.get("/admin/api/visits").data.decode("utf-8"))
    print("RESULT" + json.dumps({
        "stats": d.get("stats"),
        "meta": d.get("meta"),
        "trend_dates": (d.get("trend") or {}).get("dates"),
        "trend_values": (d.get("trend") or {}).get("values"),
        "today": day_of(datetime.datetime.now(datetime.timezone.utc).isoformat()),
    }, ensure_ascii=True))     # 子进程走管道打印中文会被本地代码页改成乱码
''')


class RecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="visitrec_")
        from werkzeug.security import generate_password_hash
        env = dict(os.environ)
        env.update({
            "WEB_DIR": WEB,
            "CEYLAN_DB_PATH": os.path.join(cls.dir, "visitors.db"),
            "CEYLAN_SECRET_KEY": "test-only",
            "CEYLAN_ADMIN_USER": "tester",
            "CEYLAN_ADMIN_HASH": generate_password_hash("test-password"),
            "PYTHONPATH": WEB,
            # 子进程输出走管道时按本地代码页（中文 Windows 是 cp936）编码，
            # 不指定就变成乱码，断言看着全错。
            "PYTHONIOENCODING": "utf-8",
        })
        env.pop("NO_PROXY", None)
        proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, cwd=WEB,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
        if proc.returncode != 0:
            raise AssertionError("子进程没跑起来：%s\n%s" % (proc.stdout[-2000:],
                                                            proc.stderr[-2000:]))
        line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT")]
        if not line:
            raise AssertionError("没拿到结果：%s" % proc.stdout[-2000:])
        cls.payload = json.loads(line[0][len("RESULT"):])

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_admin_traffic_is_not_visitors(self):
        s = self.payload["stats"]
        self.assertEqual(s["total"], 3,
                         "只有 3 次公开页面访问，后台那 7 条不该进库")
        self.assertEqual(s["ignored"], 0,
                         "新口径下后台访问根本不再入库，ignored 应该是 0")

    def test_numbers_that_differ_explain_themselves(self):
        """明细行数和累计对不上时，面板必须自己把差因说出来。"""
        meta = self.payload["meta"]
        self.assertIn("不计入访客", meta["stored"])
        self.assertIn("公开页面 3 条", meta["stored"])

    def test_every_number_uses_the_same_window(self):
        """卡片上的每个数字都得是同一个口径，否则同一天能对不上。

        现网真实翻车：累计剔了后台、今日没剔，卡片写 385、折线图当天只有 147。
        """
        s = self.payload["stats"]
        self.assertEqual((s["total"], s["today"], s["unique_ips"], s["lifetime"]),
                         (3, 3, 3, 3),
                         "总数/今日/独立 IP/累计 必须一起只数公开页面：%s" % s)
        self.assertEqual(sum(self.payload["trend_values"]), 3,
                         "折线图也要同一口径，不能画后台访问量")

    def test_lifetime_is_reported_and_starts_now(self):
        s = self.payload["stats"]
        self.assertEqual(s["lifetime"], 3)
        self.assertEqual(s["lifetime_since"], self.payload["today"])

    def test_trend_comes_from_the_counter_not_the_detail_rows(self):
        dates = self.payload["trend_dates"]
        self.assertEqual(len(dates), 7)
        self.assertEqual(self.payload["stats"]["lifetime"], 3)

    def test_meta_says_what_is_counted(self):
        meta = self.payload["meta"]
        self.assertIn("后台", meta["counts"])
        self.assertIn("累计 3 次", meta["lifetime"])


if __name__ == "__main__":
    unittest.main()
