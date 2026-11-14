"""回归锁：「累计访问量」必须真的是累计，且后台自己的访问不算访客。

用户报的是「累计访问量不对吧」。查现网库对出来的原因有两条，都锁在这里：
  · 那个数字数的是 `visits` 近 30 天的行数，表按保留期删行 → 它涨到头就不再动；
  · 上报只排掉 `/admin/api/`，现网 1000 条里 628 条是 `/admin*`（含 173 条登录页），
    首页只有 129 条 —— 面板上那句「后台不记」当时是假的。
"""
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "website")
# 追加、不插到最前面：website/ 和 bot/ 各有一份同名的 project_config.py，
# 把 website 顶到 sys.path[0] 会让别的测试 import 到错的那一份。
if WEB not in sys.path:
    sys.path.append(WEB)

import visit_log as vl          # noqa: E402


DDL = """
CREATE TABLE visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    visit_id TEXT UNIQUE,
    ts TEXT NOT NULL,
    ip TEXT,
    path TEXT,
    ua TEXT
)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(DDL)

    def tearDown(self):
        self.conn.close()

    def add(self, path, ts, ip="1.1.1.1", vid=None):
        self.conn.execute(
            "INSERT INTO visits(visit_id, ts, ip, path, ua) VALUES(?,?,?,?,?)",
            (vid or "%s|%s" % (ip, ts), ts, ip, path, "Mozilla/5.0"))


class PathTests(Base):
    def test_admin_console_is_not_a_visitor(self):
        for p in ("/admin", "/admin/", "/admin/login", "/admin/dashboard",
                  "/admin/assets/console.js", "/admin/api/visits"):
            self.assertFalse(vl.is_visitor_path(p), p + " 是后台自己，不是访客")

    def test_public_site_still_counts(self):
        for p in ("/", "/index.html", "/docs", "/weboffice/", "/wp-login.php"):
            self.assertTrue(vl.is_visitor_path(p), p)

    def test_api_and_assets_are_out(self):
        for p in ("/api/status", "/static/app.js", "/favicon.ico"):
            self.assertFalse(vl.is_visitor_path(p))

    def test_day_uses_beijing_time(self):
        # UTC 16:09 已经是北京时间的第二天凌晨
        self.assertEqual(vl.day_of("2026-11-03T16:09:07.937620+00:00"), "2026-11-04")
        self.assertEqual(vl.day_of("2026-11-03T09:00:00+00:00"), "2026-11-03")


class TotalTests(Base):
    def test_backfill_skips_console_rows(self):
        self.add("/", "2026-11-03T01:00:00+00:00")
        self.add("/", "2026-11-03T02:00:00+00:00", ip="2.2.2.2")
        self.add("/admin/login", "2026-11-03T03:00:00+00:00")
        self.add("/admin/assets/console.js", "2026-11-03T04:00:00+00:00")
        vl.ensure_schema(self.conn)
        total, first, last = vl.lifetime(self.conn)
        self.assertEqual(total, 2, "后台那两条不能进累计")
        self.assertEqual((first, last), ("2026-11-03", "2026-11-03"))

    def test_retention_delete_does_not_change_the_total(self):
        self.add("/", "2026-11-03T01:00:00+00:00")
        vl.ensure_schema(self.conn)
        self.conn.execute("DELETE FROM visits")      # 30 天保留期干的活
        self.assertEqual(vl.lifetime(self.conn)[0], 1, "明细删了，累计还是累计")

    def test_bump_survives_and_accumulates_per_day(self):
        vl.ensure_schema(self.conn)
        for i in range(3):
            vl.bump(self.conn, "2026-11-05T0%d:00:00+00:00" % i)
        vl.bump(self.conn, "2026-11-06T01:00:00+00:00")
        self.assertEqual(vl.lifetime(self.conn)[0], 4)
        self.assertEqual(vl.series(self.conn, ["2026-11-05", "2026-11-06", "2026-11-07"]),
                         [3, 1, 0])

    def test_ensure_schema_is_idempotent(self):
        self.add("/", "2026-11-03T01:00:00+00:00")
        vl.ensure_schema(self.conn)
        vl.bump(self.conn, "2026-11-04T01:00:00+00:00")
        vl.ensure_schema(self.conn)                  # 重启后再跑一次
        self.assertEqual(vl.lifetime(self.conn)[0], 2, "不能把已建的计数表重新回填")

    def test_empty_db_reports_zero_not_crash(self):
        vl.ensure_schema(self.conn)
        self.assertEqual(vl.lifetime(self.conn), (0, None, None))
        self.assertEqual(vl.series(self.conn, []), [])


class SqlPythonAgreeTests(Base):
    """读侧的 SQL 谓词和写侧的 Python 判断必须是同一个口径。

    现网就分叉过一次：累计剔掉了后台，「今日」没剔 —— 同一天折线图上 147 次，
    卡片上写 385 次。差的就是那 655 条 /admin*。
    """

    PATHS = ["/", "/docs", "/robots.txt", None, "", "/admin", "/admin/login",
             "/admin/dashboard", "/admin/assets/console.js", "/admin/api/visits",
             "/api/status", "/static/app.js", "/favicon.ico", "/adminx", "/my/admin"]

    def test_sql_predicate_matches_the_python_one(self):
        self.conn.executemany("INSERT INTO visits(visit_id, ts, ip, path) VALUES(?,?,?,?)",
                              [("p%d" % i, "2026-11-05T01:00:00+00:00", "1.1.1.1", p)
                               for i, p in enumerate(self.PATHS)])
        got = {r[0] for r in self.conn.execute(
            "SELECT path FROM visits WHERE %s" % vl.PUBLIC_SQL, vl.PUBLIC_PARAMS)}
        want = {p for p in self.PATHS if vl.is_visitor_path(p)}
        self.assertEqual(got, want)

    def test_counts_do_not_double_book_the_console(self):
        for p in ("/", "/admin/login", "/admin"):
            self.add(p, "2026-11-05T01:00:00+00:00", ip="9.9.9.9", vid=p)
        rows = self.conn.execute("SELECT COUNT(*) FROM visits WHERE %s" % vl.PUBLIC_SQL,
                                 vl.PUBLIC_PARAMS).fetchone()[0]
        self.assertEqual(rows, 1, "两次后台自己不该算访客")


if __name__ == "__main__":
    unittest.main()
