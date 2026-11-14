"""回归锁：影子校准进度。

「能不能进下一步」不能靠感觉。这里把条件写死成可核对的六条，
每条都带「要多少 / 现在多少 / 过没过」，而且：
  · 条件齐了也只说「可评审」—— stage3_ready 永远不由机器翻。
  · 合成/隔离事件不能算「驱动来源」，否则回放能把进度刷满。
  · 回放校验是 O(全表)，必须按 (processed, hash) 缓存，不能每次面板刷新都重跑。
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import emotion_shadow as sh        # noqa: E402
import emotion_core as core        # noqa: E402
import self_model as sm            # noqa: E402

ENV = ("DATA_DIR", "PERSONA_DATA_DIR", "EMOTION_SHADOW_ENABLED",
       "EMOTION_CALIB_MIN_HOURS", "EMOTION_CALIB_MIN_EVENTS",
       "EMOTION_CALIB_MAX_GAP_RATIO")


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="calib_")
        self._old = {k: os.environ.get(k) for k in ENV}
        os.environ["DATA_DIR"] = self.dir
        os.environ["PERSONA_DATA_DIR"] = os.path.join(self.dir, "persona")
        os.environ.pop("EMOTION_SHADOW_ENABLED", None)
        for k in ENV[4:]:
            os.environ.pop(k, None)
        os.makedirs(os.environ["PERSONA_DATA_DIR"], exist_ok=True)
        sh._CALIB_CACHE.update({"key": None, "ts": 0.0, "replay": None})

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sh._CALIB_CACHE.update({"key": None, "ts": 0.0, "replay": None})
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, now):
        # 先把 cognition.db 建出来：影子第一次 tick 遇到没有来源库时会只回报
        # 「等认知库」，不会创建影子库，后面的断言就会莫名其妙地空。
        sm.stats()
        first = sh.tick(now)
        self.assertTrue(first.get("initialized"), first)
        return first

    def feed(self, kind, ts, payload, source="", rule_ok=True):
        # ts 一律交给记账时刻：影子要求 0 < ts <= created，
        # 测试里如果拿 now+0.1 当发生时间，created 反而更早，
        # 事件会被隔离成「时间不可信」，进度条就永远数不到它。
        return sm.record_event(kind, ts=None, session="group:1",
                               payload=payload, source=source,
                               dedupe="calib:%s:%s" % (kind, ts))


class ProgressTests(Base):
    def test_named_observation_conditions_include_missing_evidence(self):
        now = time.time()
        self.start(now)
        rep = sh.status(now + 1)
        detail = rep["calibration_detail"]
        self.assertEqual(detail["total"], 10)
        self.assertEqual([c["key"] for c in detail["checks"]],
                         ["window", "events", "kinds", "replay", "continuity", "backlog",
                          "dimensions", "evidence", "freshness", "outcomes"])
        for c in detail["checks"]:
            self.assertIn("need", c)
            self.assertIn("now", c)
            self.assertIsInstance(c["ok"], bool)

    def test_machine_never_opens_stage3(self):
        now = time.time()
        self.start(now)
        rep = sh.status(now + 1)
        self.assertFalse(rep["stage3_ready"])
        self.assertFalse(rep["expression_control"])
        self.assertEqual(rep["calibration"], "collecting")
        self.assertFalse(rep["ready_for_review"])

    def test_low_thresholds_with_two_kinds_are_review_ready(self):
        os.environ["EMOTION_CALIB_MIN_HOURS"] = "0"
        os.environ["EMOTION_CALIB_MIN_EVENTS"] = "1"
        os.environ["EMOTION_CALIB_MAX_GAP_RATIO"] = "1"
        now = time.time()
        self.start(now)                    # 起点：游标压到当时最新（那时还没有事件）
        self.feed("group_message", now + 0.1, {"text": "谢谢你", "addressed_to_self": True})
        self.feed("tool_result", now + 0.2, {"tool": "search", "ok": True})
        self.feed("tool_result", now + 0.3, {"tool": "vision", "ok": False})
        out = sh.tick(now + 1)
        self.assertEqual(out["processed"], 3, out)
        rep = sh.status(now + 2)
        detail = rep["calibration_detail"]
        self.assertEqual(detail["passed"], detail["total"],
                         json.dumps(detail["checks"], ensure_ascii=False))
        self.assertEqual(rep["calibration"], "review_ready")
        self.assertTrue(rep["ready_for_review"])
        self.assertFalse(rep["stage3_ready"], "机器不能自己开门")

    def test_quarantine_events_do_not_count_as_drivers(self):
        os.environ["EMOTION_CALIB_MIN_HOURS"] = "0"
        os.environ["EMOTION_CALIB_MIN_EVENTS"] = "1"
        now = time.time()
        self.start(now)
        # legacy 来源会被隔离：量再大也不算「她被真实事件驱动」
        self.feed("group_message", now + 0.1, {"text": "旧档案"}, source="legacy")
        sh.tick(now + 1)
        rep = sh.status(now + 2)
        kinds = [c for c in rep["calibration_detail"]["checks"] if c["key"] == "kinds"][0]
        self.assertFalse(kinds["ok"], kinds)

    def test_replay_cache_is_keyed_by_progress_not_wall_clock(self):
        now = time.time()
        self.start(now)
        self.feed("group_message", now + 0.1, {"text": "再说一句"})
        sh.tick(now + 1)
        first = sh.status(now + 2)
        key = sh._CALIB_CACHE["key"]
        second = sh.status(now + 3)
        self.assertEqual(sh._CALIB_CACHE["key"], key, "同样条数应命中缓存")
        self.assertEqual(first["calibration_detail"]["replay"]["max_error"],
                         second["calibration_detail"]["replay"]["max_error"])
        # 记录真的坏了要能被查出：直接改一条 after_state
        path = os.path.join(os.environ["PERSONA_DATA_DIR"], "emotion_shadow.db")
        with closing(_conn(path)) as c:
            row = c.execute("SELECT after_state FROM shadow_event LIMIT 1").fetchone()
            bad = json.loads(row["after_state"])
            bad["valence"] = float(bad["valence"]) + 0.5
            c.execute("UPDATE shadow_event SET after_state=?", (json.dumps(bad),))
            c.commit()
        sh._CALIB_CACHE.update({"key": None, "ts": 0.0, "replay": None})
        third = sh.status(now + 4)
        self.assertFalse(third["calibration_detail"]["replay"]["ok"])
        self.assertGreater(third["calibration_detail"]["replay"]["max_error"], 0.1)
        self.assertEqual(third["calibration"], "collecting")

    def test_parameter_hash_mismatch_still_raises(self):
        now = time.time()
        self.start(now)
        self.feed("group_message", now + 0.1, {"text": "参数漂移测试"})
        sh.tick(now + 1)
        path = os.path.join(os.environ["PERSONA_DATA_DIR"], "emotion_shadow.db")
        with closing(_conn(path)) as c:
            c.execute("UPDATE shadow_run SET parameter_hash=? WHERE id=1",
                      ("0" * 64,))
            c.commit()
        with self.assertRaises(ValueError):
            sh.status(now + 1)

    def test_kernel_bounds_are_not_rewritten_by_calibration(self):
        before = core.parameter_hash()
        now = time.time()
        self.start(now)
        sh.status(now + 1)
        self.assertEqual(core.parameter_hash(), before,
                         "算进度不该动到冻结参数")


class FrontendWiringTests(unittest.TestCase):
    """面板得把「差哪几条」和「这四个数是什么意思」直接说给人看。"""

    @classmethod
    def setUpClass(cls):
        web = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "website")
        with io.open(os.path.join(web, "static", "console.js"), encoding="utf-8") as f:
            cls.js = f.read()
        with io.open(os.path.join(web, "console.html"), encoding="utf-8") as f:
            cls.html = f.read()
        with io.open(os.path.join(web, "static", "console.css"), encoding="utf-8") as f:
            cls.css = f.read()

    def test_calibration_container_exists(self):
        self.assertIn('id="emotion-shadow-calib"', self.html)

    def test_each_dimension_has_a_tooltip(self):
        for key in ("valence", "arousal", "control", "attention"):
            self.assertIn("%s:" % key, self.js, "%s 没有释义" % key)
        self.assertIn("data-tip", self.js)
        self.assertIn(".tip::after", self.css, "悬浮窗样式没落地，data-tip 就是死文本")
        self.assertIn(".tip:focus", self.css, "键盘也得能看到释义")

    def test_progress_is_rendered_from_checks_not_from_a_guess(self):
        start = self.js.index("function renderCalibration")
        blk = self.js[start:start + 1400]
        self.assertIn("checks.length", blk)
        self.assertIn("checks.filter", blk)
        self.assertIn("c.need", self.js[start:start + 2200])
        self.assertIn("c.now", self.js[start:start + 2200])

    def test_stage3_is_still_reported_as_human_decision(self):
        start = self.js.index("async function loadEmotionShadow")
        blk = self.js[start:self.js.index('/* ---------- persona ---------- */', start)]
        self.assertIn("阶段3尚未开放", blk)
        self.assertIn("可评审不等于已校准", blk)


if __name__ == "__main__":
    unittest.main()