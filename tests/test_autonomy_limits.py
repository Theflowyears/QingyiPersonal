"""回归锁：自主额度能在「高级设置」里热改，改完真的生效，不用重启。

用户的原话：「把这个也做到高级设置面板让我可以直接调整热加载吧」。
这里锁住三个容易翻车的点：
  1) curiosity 的额度原来是 import 时定死的模块常量 —— 面板改了、环境变量改了
     都不会动，只能重启，而重启会断 QQ；
  2) 控制层的 apply 只改它自己命名空间里的键；bili_auto / curiosity 不在里面，
     不显式转发就只是把值写进了 settings.json 给你看；
  3) 面板加了输入框却没接进 AD_FIELDS —— 保存时那一项被静默丢掉，
     界面看着有、实际没提交。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "website")
for p in (BOT, WEB):
    if p not in sys.path:
        sys.path.insert(0, p) if p == BOT else sys.path.append(p)

import autonomy_limits as al      # noqa: E402
import bili_auto as ba            # noqa: E402
import curiosity as cu            # noqa: E402
import ceylan_control_core as core  # noqa: E402
import ceylan_control_runtime as ctl  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="autolim_")
        self._env = {k: os.environ.get(k) for k in
                     ("DATA_DIR", "CEYLAN_CONTROL_DIR") + al.KEYS}
        os.environ["DATA_DIR"] = self.dir
        for k in al.KEYS:
            os.environ.pop(k, None)
        al.reset()
        cu._reset_cache()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        al.reset()
        cu._reset_cache()
        shutil.rmtree(self.dir, ignore_errors=True)


class EnvAndUpdateTests(Base):
    def test_env_seeds_when_nothing_was_saved(self):
        with patch.dict(os.environ, {"CURIOSITY_MAX_PER_DAY": "7"}):
            al.reset()
            self.assertEqual(al.get_int("CURIOSITY_MAX_PER_DAY", 0), 7)

    def test_fallback_when_never_set(self):
        self.assertEqual(al.get_int("CURIOSITY_MAX_PER_DAY", 4), 4)
        self.assertEqual(al.get_float("BILI_AUTO_MIN_GAP_HOURS", 2.5), 2.5)

    def test_panel_value_wins_over_env(self):
        with patch.dict(os.environ, {"CURIOSITY_MAX_PER_DAY": "7"}):
            al.reset()
        al.update({"CURIOSITY_MAX_PER_DAY": 2})
        self.assertEqual(al.get_int("CURIOSITY_MAX_PER_DAY", 0), 2,
                         "面板保存的值必须压过 config.env")

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(al.update({"WHATEVER": 9}), {})
        self.assertNotIn("WHATEVER", al.snapshot())

    def test_types_are_coerced(self):
        al.update({"BILI_AUTO_MIN_GAP_HOURS": 1, "BILI_AUTO_MAX_PER_DAY": "3"})
        self.assertIsInstance(al.get_float("BILI_AUTO_MIN_GAP_HOURS", 0), float)
        self.assertEqual(al.get_int("BILI_AUTO_MAX_PER_DAY", 0), 3)


class ControlContractTests(unittest.TestCase):
    """控制层的白名单/范围就是面板能提交的边界。"""

    def _cmd(self, values):
        return {"action": "settings", "revision": "0" * 64, "values": values}

    def test_the_five_knobs_are_accepted(self):
        got = core.validate(self._cmd({
            "BILI_AUTO_MAX_PER_DAY": 5, "BILI_AUTO_MIN_GAP_HOURS": 2.0,
            "CURIOSITY_MAX_PER_DAY": 5, "CURIOSITY_SHARE_MAX_PER_DAY": 1,
            "CURIOSITY_SHARE_MIN_INTERVAL_HOURS": 6.0}))
        self.assertEqual(got["values"]["CURIOSITY_MAX_PER_DAY"], 5)

    def test_boundaries_are_inclusive(self):
        core.validate(self._cmd({"CURIOSITY_MAX_PER_DAY": 0}))
        core.validate(self._cmd({"CURIOSITY_MAX_PER_DAY": 20}))
        core.validate(self._cmd({"BILI_AUTO_MIN_GAP_HOURS": 0.5}))
        core.validate(self._cmd({"BILI_AUTO_MIN_GAP_HOURS": 12.0}))

    def test_out_of_range_is_refused_with_the_key_name(self):
        for values in ({"CURIOSITY_MAX_PER_DAY": 21},
                       {"CURIOSITY_MAX_PER_DAY": -1},
                       {"BILI_AUTO_MIN_GAP_HOURS": 0.1},
                       {"CURIOSITY_SHARE_MIN_INTERVAL_HOURS": 49}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError) as ctx:
                    core.validate(self._cmd(values))
                self.assertIn(list(values)[0], str(ctx.exception))

    def test_int_knobs_refuse_floats(self):
        with self.assertRaises(ValueError):
            core.validate(self._cmd({"CURIOSITY_MAX_PER_DAY": 2.5}))


class _FakeRT:
    """install() 会给 rt 挂一个 snapshot 钩子，只需要能赋值就行。"""

    @staticmethod
    def blocked(group_id):
        return False

    def snapshot(self):
        return None


class HotApplyTests(Base):
    """保存 → 运行中的额度真的变，而不是只写了个文件。"""

    def setUp(self):
        super().setUp()
        self.root = Path(self.dir) / "control-v4"
        self._old_root = ctl.ROOT
        self._old_opts = dict(ctl.OPTIONS)
        ctl.ROOT = self.root
        ctl.OPTIONS = dict(core.DEFAULTS)
        ctl.BUSY.clear()

    def tearDown(self):
        ctl.ROOT = self._old_root
        ctl.OPTIONS = self._old_opts
        super().tearDown()

    def _apply(self, values, ns=None):
        ns = dict(ns or {})
        rev = core.revision(ctl.settings(ns))
        return ctl.apply(ns, _FakeRT(),
                         {"action": "settings", "revision": rev, "values": values,
                          "id": "a" * 32})

    def test_apply_changes_the_live_value(self):
        al.update({"CURIOSITY_MAX_PER_DAY": 5})
        self._apply({"CURIOSITY_MAX_PER_DAY": 2})
        self.assertEqual(al.get_int("CURIOSITY_MAX_PER_DAY", 0), 2,
                         "面板保存后运行中的额度必须跟着变")
        saved = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["CURIOSITY_MAX_PER_DAY"], 2)

    def test_restart_replays_the_saved_values(self):
        self._apply({"BILI_AUTO_MAX_PER_DAY": 3})
        al.reset()                          # 假装进程重启
        al.LIVE.clear()

        async def stub(*a, **k):
            return None

        ns = {name: stub for name in (
            "handle_group_message", "handle_private_message", "periodic_group_review",
            "compact_group_memory_once", "run_agent_task",
            "call_openclaw", "call_mimo_vision",
            "send_group_msg", "send_private_msg", "build_group_recent_text")}
        ctl.install(ns, _FakeRT())
        self.assertEqual(al.get_int("BILI_AUTO_MAX_PER_DAY", 0), 3,
                         "重启后要从 settings.json 读回来，不能退回默认值")


class ConsumerTests(Base):
    """谁能真读到：刷 B 站、主动搜索、主动分享。"""

    def _claim_n_bili(self, n, now=None):
        import time
        now = now or time.mktime((time.localtime().tm_year, time.localtime().tm_mon,
                                  time.localtime().tm_mday, 10, 0, 0, 0, 0, -1))
        for i in range(n):
            ba.claim({"bvid": "BV%d" % i, "title": "第 %d 条" % i}, now + i)

    def test_bili_cap_comes_from_the_live_value(self):
        import time
        now = time.mktime((time.localtime().tm_year, time.localtime().tm_mon,
                           time.localtime().tm_mday, 10, 0, 0, 0, 0, -1))
        al.update({"BILI_AUTO_MAX_PER_DAY": 1, "BILI_AUTO_MIN_GAP_HOURS": 0.5})
        self._claim_n_bili(1, now)
        self.assertIn("当天已经刷够 1 条", ba.block_reason(now + 7200))
        al.update({"BILI_AUTO_MAX_PER_DAY": 3})
        self.assertEqual(ba.block_reason(now + 7200), "",
                         "把上限调大以后，同一时刻就应该能继续刷")

    def test_curiosity_quota_uses_the_live_value(self):
        al.update({"CURIOSITY_MAX_PER_DAY": 2})
        self.assertEqual(cu.quota_left(), 2)
        al.update({"CURIOSITY_MAX_PER_DAY": 5})
        self.assertEqual(cu.quota_left(), 5, "改完立刻按新额度算剩余")
        self.assertEqual(cu.stats()["max_per_day"], 5)

    def test_share_budget_is_separate_from_activity(self):
        al.update({"CURIOSITY_MAX_PER_DAY": 5, "CURIOSITY_SHARE_MAX_PER_DAY": 2})
        self.assertEqual(cu.share_max_per_day(), 2)
        self.assertEqual(cu.max_per_day(), 5)

    def test_bridge_reads_the_live_quota(self):
        with io.open(os.path.join(BOT, "qq_openclaw_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("cu.MAX_PER_DAY", src,
                         "直接读模块常量 = 面板改了也不生效")
        self.assertIn("cu.max_per_day()", src)


class PanelWiringTests(unittest.TestCase):
    def test_advanced_page_has_the_inputs(self):
        with io.open(os.path.join(WEB, "console.html"), encoding="utf-8") as f:
            html = f.read()
        for el_id in ("ad-bili-max", "ad-bili-gap", "ad-cur-max",
                      "ad-share-max", "ad-share-gap"):
            self.assertIn('id="%s"' % el_id, html, "高级设置里没有 %s" % el_id)

    def test_inputs_are_actually_submitted(self):
        with io.open(os.path.join(WEB, "static", "console.js"), encoding="utf-8") as f:
            js = f.read()
        start = js.index("var AD_FIELDS = [")
        block = js[start:js.index("];", start)]
        for key in ("BILI_AUTO_MAX_PER_DAY", "BILI_AUTO_MIN_GAP_HOURS",
                    "CURIOSITY_MAX_PER_DAY", "CURIOSITY_SHARE_MAX_PER_DAY",
                    "CURIOSITY_SHARE_MIN_INTERVAL_HOURS"):
            self.assertIn(key, block, "%s 没接进 AD_FIELDS，保存时会被丢掉" % key)

    def test_page_explains_the_effective_latency(self):
        with io.open(os.path.join(WEB, "console.html"), encoding="utf-8") as f:
            html = f.read()
        start = html.index('id="view-advanced"')
        block = html[start:start + 9000]
        self.assertIn("不用重启", block)
        self.assertIn("约 1 分钟", block, "刷 B站的生效延迟要写明，别让人以为改完立刻动")
        self.assertIn("约 1 小时", block, "自主搜索按心跳走，也要写明")


if __name__ == "__main__":
    unittest.main()