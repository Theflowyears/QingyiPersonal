"""回归锁：她自己去刷 B 站。

这一条链路上线时只有一个入口：群里有人丢链接。于是「刷 B 站」实际上
等价于「别人发什么她看什么」——她从来没有自己打开过 B 站。
这里锁住补上的那个入口：从公开热门里挑一条，限时限量、优先她的兴趣、
看过的不再看、状态落盘。看视频本身仍走已经验证过的那条链路。
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import bili_auto as ba        # noqa: E402


def day_at(hour, minute=0, day_offset=0):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset,
                        hour, minute, 0, 0, 0, -1))


def item(bvid, title, category="游戏", duration=300, view=10000):
    return {"bvid": bvid, "title": title, "owner": "UP主",
            "category": category, "duration": duration, "view": view,
            "desc": "", "aid": 1, "cid": 1, "pubdate": 0}


class Base(unittest.TestCase):
    ENV = ("DATA_DIR", "BILI_AUTO", "BILI_AUTO_MAX_PER_DAY",
           "BILI_AUTO_MIN_GAP_HOURS", "BILI_AUTO_START_HOUR",
           "BILI_AUTO_END_HOUR")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="biliauto_")
        self._old = {k: os.environ.get(k) for k in self.ENV}
        os.environ["DATA_DIR"] = self.dir
        for k in self.ENV[1:]:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)


class WindowTests(Base):
    def test_disabled_says_why(self):
        os.environ["BILI_AUTO"] = "0"
        self.assertIn("关", ba.block_reason(day_at(10)))

    def test_night_is_blocked(self):
        for hour in (0, 8, 23):
            with self.subTest(hour=hour):
                self.assertTrue(ba.block_reason(day_at(hour)))

    def test_daytime_is_open(self):
        self.assertEqual(ba.block_reason(day_at(10)), "")
        self.assertEqual(ba.block_reason(day_at(22)), "")

    def test_daily_cap_and_gap(self):
        # 机制用显式额度测，别跟着默认值漂
        os.environ["BILI_AUTO_MAX_PER_DAY"] = "2"
        os.environ["BILI_AUTO_MIN_GAP_HOURS"] = "3"
        now = day_at(10)
        self.assertTrue(ba.claim(item("BV1", "第一条"), now))
        # 一小时后：不是日上限，是间隔没到
        self.assertIn("间隔", ba.block_reason(now + 3600))
        # 间隔够了，但当天第二个名额还在
        self.assertEqual(ba.block_reason(now + 4 * 3600), "")
        self.assertTrue(ba.claim(item("BV2", "第二条"), now + 4 * 3600))
        # 两个名额用完，当天不再刷
        self.assertIn("当天", ba.block_reason(now + 8 * 3600))
        # 第二天重新有额度
        self.assertEqual(ba.block_reason(day_at(10, day_offset=1)), "")

    def test_default_budget_fills_a_day_not_a_morning(self):
        """默认额度：一天 5 条、间隔 2 小时。

        以前是 2 条 / 3 小时 —— 上午起来刷两条，这一天就再没有「自己去看了」
        这回事。用户要的是多一点，不是无限：9–23 点的窗口按 2 小时间隔排得下 5 条。
        """
        now = day_at(9)
        for i in range(5):
            at = now + i * 2.5 * 3600
            self.assertEqual(ba.block_reason(at), "", "第 %d 条本应能刷" % (i + 1))
            self.assertTrue(ba.claim(item("BV%d" % i, "第 %d 条" % (i + 1)), at))
        self.assertIn("当天已经刷够 5 条", ba.block_reason(now + 13 * 3600))

    def test_claim_is_idempotent_for_same_video(self):
        now = day_at(10)
        self.assertTrue(ba.claim(item("BV1", "第一条"), now))
        self.assertFalse(ba.claim(item("BV1", "第一条"), now + 4 * 3600))
        self.assertEqual(ba.stats(now)["today"], 1)


class ChooseTests(Base):
    def test_interest_match_wins_over_generic_popular(self):
        items = [
            item("BVNEWS", "今日时事要闻", category="资讯"),
            item("BVTOOLONG", "十小时直播回放", duration=36000),
            item("BVSEEN", "明日方舟 抽卡实录", duration=600),
            item("BVMATCH", "明日方舟新角色抽卡概率实测", duration=600),
            item("BVOTHER", "生活区日常", category="生活", duration=300),
        ]
        got = ba.choose(items, ["明日方舟", "抽卡"],
                        seen=lambda b: b == "BVSEEN", now=day_at(10),
                        rng=random.Random(0))
        self.assertIsNotNone(got)
        self.assertEqual(got["bvid"], "BVMATCH")

    def test_news_categories_are_never_chosen(self):
        items = [item("BVNEWS", "突发新闻", category="资讯"),
                 item("BVWAR", "军事观察", category="军事")]
        self.assertIsNone(ba.choose(items, [], now=day_at(10),
                                    rng=random.Random(0)))

    def test_without_interest_it_still_picks_a_preferred_category(self):
        items = [item("BVNEWS", "突发新闻", category="资讯"),
                 item("BVANIME", "新番吐槽", category="动画"),
                 item("BVGAME", "游戏实况", category="游戏")]
        got = ba.choose(items, [], now=day_at(10), rng=random.Random(0))
        self.assertIn(got["bvid"], ("BVANIME", "BVGAME"))

    def test_seen_and_too_short_are_skipped(self):
        items = [item("BVSHORT", "十秒短片", duration=5),
                 item("BVSEEN", "看过的视频"),
                 item("BVOK", "没看过的视频")]
        got = ba.choose(items, [], seen={"BVSEEN"}, now=day_at(10),
                        rng=random.Random(0))
        self.assertEqual(got["bvid"], "BVOK")

    def test_all_seen_returns_none(self):
        items = [item("BV1", "甲"), item("BV2", "乙")]
        self.assertIsNone(ba.choose(items, [], seen={"BV1", "BV2"},
                                    now=day_at(10), rng=random.Random(0)))


class StateTests(Base):
    def test_claim_persists_across_reload(self):
        now = day_at(10)
        ba.claim(item("BV1", "第一条"), now)
        # 重新读盘：模拟重启
        got = ba.stats(now)
        self.assertEqual(got["today"], 1)
        self.assertIn("BV1", got["watched"])

    def test_old_days_are_pruned(self):
        now = day_at(10)
        ba.claim(item("BV1", "第一条"), now)
        path = os.path.join(self.dir, "persona", "bili_auto.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["days"]["2000-01-01"] = {"n": 9}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        ba.claim(item("BV2", "第二条"), day_at(10, day_offset=1))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("2000-01-01", data["days"])

    def test_watched_list_is_bounded(self):
        now = day_at(10)
        for i in range(120):
            ba.claim(item("BV%03d" % i, "第%d条" % i),
                     now + i * 4 * 3600)
        self.assertLessEqual(len(ba.stats(now + 500 * 3600)["watched"]), 80)


if __name__ == "__main__":
    unittest.main()