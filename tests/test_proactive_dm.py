"""回归锁：她主动私聊朋友这件事的「规则」，以及平台能力的真实边界。

私聊别人是高影响动作，所以这里不测「她说了什么」，只测**该不该说、该找谁**：
  · 只找好感 >15 且确实在好友列表里的人；
  · 每天 2~4 次，只在 UTC+8 09:00–23:00；
  · 有时间点、有最小间隔、同一个人几天内只找一次；
  · 有群聊线索时优先找刚说话的人，没有线索时按「好感高 + 最久没联系」兜底。

另外把 NapCat 的能力边界钉住：主动加好友、发说说这两个动作在 v4.15.19 上
返回 1404（不支持的 API）。这条锁的意义是**别让以后的人以为「再调调参数就行」**。
"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import proactive_dm as pdm        # noqa: E402
import social                     # noqa: E402
import sticker_store as ss        # noqa: E402


def _at(day_offset=0, hh=12, mm=0):
    """固定 UTC+8 日期，不让主机时区或当天抽签影响用例。"""
    return (datetime(2026, 11, 7, hh, mm, tzinfo=timezone(timedelta(hours=8)))
            + timedelta(days=day_offset)).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdm_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        pdm._cache.update({"sig": None, "data": None})
        self.friends = ["1001", "1002", "1003"]
        self.scores = {"1001": 60, "1002": 20, "1003": 10}

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        pdm._cache.update({"sig": None, "data": None})
        shutil.rmtree(self.dir, ignore_errors=True)


class WindowTests(Base):
    def test_before_9_is_blocked(self):
        d = pdm.decide(_at(hh=8), self.friends, self.scores)
        self.assertFalse(d["ok"])
        self.assertIn("9–23", d["reason"])

    def test_after_23_is_blocked(self):
        d = pdm.decide(_at(hh=23, mm=30), self.friends, self.scores)
        self.assertFalse(d["ok"])

    def test_midday_is_allowed(self):
        d = pdm.decide(_at(hh=12), self.friends, self.scores)
        # 可能因为「还没到时间点」被拦，那是另一条规则；这里只要求不是窗口问题
        self.assertNotIn("9–23", d.get("reason", ""))


class TimezoneTests(Base):
    def test_day_and_window_ignore_host_timezone(self):
        # 强制宿主 localtime 表现为 UTC；业务时钟仍须是 UTC+8。
        with mock.patch.object(pdm.time, "localtime", side_effect=time.gmtime):
            self.assertEqual(pdm._day(_at(hh=0)), "2026-11-07")
            self.assertEqual(pdm._local_seconds(_at(hh=9)), 9 * 3600)
            self.assertTrue(pdm._in_window(_at(hh=9)))
            self.assertFalse(pdm._in_window(_at(hh=8, mm=59)))
            self.assertTrue(pdm._in_window(_at(hh=22, mm=59)))
            self.assertFalse(pdm._in_window(_at(hh=23)))

    def test_stats_and_due_slots_use_the_same_utc8_clock(self):
        with mock.patch.object(pdm.time, "localtime", side_effect=time.gmtime), \
                mock.patch.object(pdm, "plan_day", return_value=[9 * 3600]):
            now = _at(hh=9)
            self.assertTrue(pdm.decide(now, self.friends, self.scores)["ok"])
            stats = pdm.stats(now)
            self.assertEqual(stats["day"], "2026-11-07")
            self.assertEqual(stats["slots"], ["09:00"])


class PlanTests(Base):
    def test_daily_plan_is_stable_across_restarts(self):
        day = "2026-11-07"
        a = pdm.plan_day(day)
        pdm._cache.update({"sig": None, "data": None})
        b = pdm.plan_day(day)
        self.assertEqual(a, b, "同一天重启一次就换一套时间点，她会反复「刚好该发了」")

    def test_daily_plan_has_the_right_count(self):
        for day in ("2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"):
            n = len(pdm.plan_day(day))
            self.assertGreaterEqual(n, max(1, pdm.MIN_PER_DAY))
            self.assertLessEqual(n, pdm.MAX_PER_DAY)

    def test_slots_are_inside_the_window(self):
        for off in pdm.plan_day("2026-11-11"):
            hh = off // 3600
            self.assertGreaterEqual(hh, pdm.WINDOW_START_HOUR)
            self.assertLess(hh, pdm.WINDOW_END_HOUR)


class CandidateTests(Base):
    def test_affinity_threshold_is_respected(self):
        # 1003 只有 10 分，不该被找
        d = pdm.decide(_at(hh=12), self.friends, self.scores)
        if d.get("ok"):
            self.assertNotEqual(d["uid"], "1003")

    def test_strangers_outside_friend_list_are_ignored(self):
        scores = dict(self.scores)
        scores["9999"] = 999          # 好感再高，不是好友就不算
        d = pdm.decide(_at(hh=12), ["1001"], scores)
        if d.get("ok"):
            self.assertEqual(d["uid"], "1001")

    def test_group_speaker_is_preferred(self):
        d = pdm.decide(_at(hh=12), self.friends, self.scores,
                       recent_speakers={"1002": time.time()})
        if d.get("ok"):
            self.assertEqual(d["uid"], "1002")
            self.assertIn("群里", d["reason"])

    def test_fallback_prefers_high_affinity(self):
        d = pdm.decide(_at(hh=12), self.friends, self.scores, {})
        if d.get("ok"):
            self.assertEqual(d["uid"], "1001")

    def test_recently_contacted_friend_is_skipped(self):
        pdm.mark_contacted("1001")
        d = pdm.decide(_at(hh=12), self.friends, self.scores, {})
        if d.get("ok"):
            self.assertNotEqual(d["uid"], "1001")


class QuotaAndClaimTests(Base):
    def test_claim_reserves_without_charging_sent_quota(self):
        now = _at(hh=12)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]):
            d = pdm.decide(now, self.friends, self.scores)
            self.assertTrue(d["ok"])
            self.assertIsNotNone(pdm.claim(d, now=now))
            self.assertEqual(pdm.stats(now)["n"], 0, "未发出不能扣已发送额度")
            self.assertFalse(pdm.decide(now, self.friends, self.scores)["ok"])
            self.assertTrue(pdm.release(now=now))
            self.assertEqual(pdm.stats(now)["n"], 0)
            self.assertTrue(pdm.decide(now, self.friends, self.scores)["ok"])

    def test_same_session_reload_keeps_active_reservation_across_days(self):
        now = _at(hh=12)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]), \
                mock.patch.object(pdm, "_RUNTIME_OWNER", "active-session", create=True):
            decision = pdm.decide(now, self.friends, self.scores)
            self.assertIsNotNone(pdm.claim(decision, now=now))
            for when in (now, _at(day_offset=2)):
                pdm._cache.update({"sig": None, "data": None})
                self.assertFalse(pdm.decide(when, self.friends, self.scores)["ok"])
                self.assertIsNone(pdm.claim(decision, now=when))
                self.assertEqual(pdm.stats(when)["n"], 0)
            self.assertTrue(pdm.done(now=now + 30))
            self.assertEqual(pdm.stats(now)["n"], 1)

    def test_fresh_session_releases_unknown_reservation_without_success_charge(self):
        for offset in (0, 2):
            with self.subTest(day_offset=offset), \
                    mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]):
                pdm._save(pdm._empty())
                now = _at(hh=12)
                with mock.patch.object(pdm, "_RUNTIME_OWNER", "old-session", create=True):
                    decision = pdm.decide(now, self.friends, self.scores)
                    self.assertIsNotNone(pdm.claim(decision, now=now))
                    pdm.mark_contacted("1003", now + 1)
                when = _at(day_offset=offset, hh=14)
                with mock.patch.object(pdm, "_RUNTIME_OWNER", "new-session", create=True):
                    pdm._cache.update({"sig": None, "data": None})
                    log = io.StringIO()
                    with redirect_stdout(log):
                        self.assertTrue(pdm.decide(when, self.friends, self.scores)["ok"])
                    self.assertIn("unknown", log.getvalue())
                    self.assertIsNone(pdm._load()["pending"])
                    self.assertFalse(pdm.done(now=when), "未知结果不能提交成功")
                    self.assertEqual(pdm.stats(now)["n"], 0)
                    self.assertEqual(pdm.stats(when)["n"], 0)
                    self.assertEqual(pdm.stats(now)["used"], 0)
                    self.assertEqual(pdm.stats(now)["declined"], 0)
                    self.assertEqual(pdm.stats(when)["last"], {"1003": now + 1})
                    pdm._cache.update({"sig": None, "data": None})
                    self.assertIsNone(pdm._load()["pending"])
                    decision = pdm.decide(when, self.friends, self.scores)
                    self.assertIsNotNone(pdm.claim(decision, now=when))
                    pdm._cache.update({"sig": None, "data": None})
                    self.assertFalse(pdm.decide(when, self.friends, self.scores)["ok"])
                    pdm.release()

    def test_legacy_ownerless_reservation_is_released_not_committed(self):
        now = _at(hh=12)
        data = pdm._empty()
        data["days"]["2026-11-07"] = {"slots": [12 * 3600], "used": [], "n": 0}
        data["pending"] = {"uid": "1001", "day": "2026-11-07", "slot": 0, "ts": now}
        pdm._save(data)
        pdm._cache.update({"sig": None, "data": None})
        self.assertFalse(pdm.done(now=now), "旧预留也不能猜成发送成功")
        self.assertTrue(pdm.decide(now, self.friends, self.scores)["ok"])
        self.assertEqual(pdm.stats(now)["n"], 0)
        self.assertEqual(pdm.stats(now)["last"], {})

    def test_done_commits_sent_quota_and_contact_once(self):
        now = _at(hh=12)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]):
            d = pdm.decide(now, self.friends, self.scores)
            pdm.claim(d, now=now)
            self.assertTrue(pdm.done(now=now + 30))
            self.assertEqual(pdm.stats(now)["n"], 1)
            self.assertEqual(pdm.stats(now)["last"]["1001"], now + 30)
            self.assertFalse(pdm.done(now=now + 60))
            self.assertEqual(pdm.stats(now)["n"], 1)

    def test_decline_consumes_opportunity_not_sent_budget_or_contact(self):
        now = _at(hh=12)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600, 14 * 3600]):
            d = pdm.decide(now, self.friends, self.scores)
            pdm.claim(d, now=now)
            self.assertTrue(pdm.decline(now=now + 1))
            pdm._cache.update({"sig": None, "data": None})
            self.assertFalse(pdm.decide(now + 600, self.friends, self.scores)["ok"])
            self.assertEqual(pdm.stats(now)["n"], 0)
            self.assertEqual(pdm.stats(now)["declined"], 1)
            self.assertEqual(pdm.stats(now)["last"], {})
            self.assertTrue(pdm.decide(_at(hh=14), self.friends, self.scores)["ok"])
            self.assertFalse(pdm.decline(now=now + 2))

    def test_release_does_not_erase_contact_during_generation(self):
        now = _at(hh=12)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]):
            d = pdm.decide(now, self.friends, self.scores)
            pdm.claim(d, now=now)
            pdm.mark_contacted("1001", now + 10)
            pdm.release(now=now + 20)
            self.assertEqual(pdm.stats(now)["last"]["1001"], now + 10)
            self.assertEqual(pdm.stats(now)["n"], 0)

    def test_release_across_midnight_does_not_modify_next_day(self):
        now = _at(hh=22)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]):
            d = pdm.decide(now, self.friends, self.scores)
            pdm.claim(d, now=now)
            next_day = _at(day_offset=1)
            pdm._day_state("2026-11-08")
            self.assertTrue(pdm.release(now=next_day))
            self.assertEqual(pdm.stats(now)["n"], 0)
            self.assertEqual(pdm.stats(next_day)["n"], 0)

    def test_success_across_midnight_charges_reserved_day_only(self):
        now = _at(hh=22)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]):
            d = pdm.decide(now, self.friends, self.scores)
            pdm.claim(d, now=now)
            next_day = _at(day_offset=1, hh=0)
            self.assertTrue(pdm.done(now=next_day))
            self.assertEqual(pdm.stats(now)["n"], 1)
            self.assertEqual(pdm.stats(next_day)["n"], 0)
            self.assertEqual(pdm.stats(next_day)["last"]["1001"], next_day)

    def test_old_day_state_without_declines_remains_usable(self):
        now = _at(hh=12)
        data = pdm._load()
        data["days"]["2026-11-07"] = {"slots": [12 * 3600], "used": [], "n": 0}
        pdm._save(data)
        pdm._cache.update({"sig": None, "data": None})
        d = pdm.decide(now, self.friends, self.scores)
        self.assertTrue(d["ok"])
        self.assertIsNotNone(pdm.claim(d, now=now))
        self.assertTrue(pdm.decline(now=now))
        self.assertEqual(pdm.stats(now)["declined"], 1)

    def test_claim_rejects_future_slot_or_second_reservation(self):
        now = _at(hh=12)
        with mock.patch.object(pdm, "plan_day", return_value=[14 * 3600]):
            d = {"ok": True, "uid": "1001", "reason": "测试"}
            self.assertIsNone(pdm.claim(d, now=now))
            self.assertIsNotNone(pdm.claim(d, now=_at(hh=14)))
            self.assertIsNone(pdm.claim(d, now=_at(hh=14)))

    def test_older_contact_event_does_not_rewind_cooldown(self):
        now = _at()
        pdm.mark_contacted("1001", now)
        pdm.mark_contacted("1001", now - 3600)
        self.assertEqual(pdm.stats(now)["last"]["1001"], now)

    def test_min_gap_blocks_a_second_dm(self):
        # 每天的时间点是按日期抽签抽的：赶上「所有点都排在 12 点以后」的那天，
        # decide 会先返回「还没到下一个时间点」，这条锁就变成在看日历。
        # 这里固定排一个已经到点的时刻，测的才是最小间隔那条规则本身。
        now = _at(hh=12, mm=40)
        with mock.patch.object(pdm, "plan_day", return_value=[12 * 3600]):
            pdm.mark_contacted("1002", now - 60)   # 一分钟前刚找过别人
            d = pdm.decide(now, self.friends, self.scores, {})
            self.assertFalse(d["ok"])
            self.assertIn("分钟", d["reason"])

    def test_disabled_switch(self):
        old = pdm.ENABLED
        pdm.ENABLED = False
        try:
            d = pdm.decide(_at(hh=12), self.friends, self.scores, {})
            self.assertFalse(d["ok"])
        finally:
            pdm.ENABLED = old


class CapabilityTests(unittest.TestCase):
    def test_unsupported_actions_are_marked_unsupported(self):
        # 这两个是实测 retcode=1404 的结果，别改成 True
        self.assertFalse(social.CAPABILITIES["qzone_publish"])
        self.assertFalse(social.CAPABILITIES["add_friend"])
        self.assertTrue(social.CAPABILITIES["set_signature"])
        self.assertTrue(social.CAPABILITIES["friend_list"])


class StickerLibraryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="stk_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        ss._index_cache.update({"data": None, "ts": 0.0})

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old

    def test_path_traversal_is_refused(self):
        # 面板按 id 取图，绝不能让它变成读任意文件
        for bad in ("../config.env", "..%2fconfig.env", "/etc/passwd", ""):
            self.assertEqual(ss.sticker_path(bad), "", bad)

    def test_unknown_id_returns_empty(self):
        self.assertEqual(ss.sticker_path("nope123"), "")

    def test_all_stickers_is_a_list(self):
        self.assertIsInstance(ss.all_stickers(10), list)

    def test_steal_quota_has_a_public_switch_and_counter(self):
        self.assertIsInstance(ss.steal_ok(), bool)
        before = ss._STEAL_LOG.get("n", 0)
        ss.note_stolen()
        self.assertEqual(ss._STEAL_LOG["n"], before + 1,
                         "收图不计数的话，日上限就是摆设")


class AddresseeTests(unittest.TestCase):
    """收件人和称呼必须对上。

    线上真实事故：消息发给了主人（好感 89，最高），正文却是「阿甲，八抽出货这运气
    是开了挂吧」。收件人 A、开口喊 B —— 这不是措辞问题，是把该跟 B 说的话发给了 A。
    """

    NAMES = {u"阿甲": "9000000004", u"阿乙": "1000000001",
             u"想不到": "9000000003"}

    def test_addressing_someone_else_is_caught(self):
        line = u"阿甲，八抽出货这运气是开了挂吧？我酸了。"
        self.assertEqual(
            pdm.addressee_conflict(line, [u"阿乙"], self.NAMES), u"阿甲")

    def test_addressing_the_recipient_is_fine(self):
        line = u"阿乙，八抽出货这运气是开了挂吧？"
        self.assertEqual(
            pdm.addressee_conflict(line, [u"阿乙"], self.NAMES), "")

    def test_recipient_aliases_cover_remark_and_nickname(self):
        # 同一个人可能有备注和群名片两个称呼，两个都不能算出错
        line = u"想不到，今天怎么没见你说话"
        self.assertEqual(
            pdm.addressee_conflict(line, [u"想不到", u"想不到…"], self.NAMES), "")

    def test_single_char_names_are_not_matched(self):
        # 「鱼」「月」这种单字太常见，按名字判会误杀一大片正常句子
        names = {u"月": "1", u"鱼": "2"}
        self.assertEqual(pdm.addressee_conflict(u"今天月亮真好看", [u"阿甲"], names), "")

    def test_empty_text_has_no_conflict(self):
        self.assertEqual(pdm.addressee_conflict("", [u"阿甲"], self.NAMES), "")


class ExcludeTests(Base):
    def test_excluded_uid_is_never_chosen(self):
        # 主人已有另一条主动分享线，这条朋友线不该再找他
        d = pdm.decide(_at(hh=12), self.friends, self.scores, {},
                       exclude={"1001"})
        if d.get("ok"):
            self.assertNotEqual(d["uid"], "1001")

    def test_exclude_only_removes_that_person(self):
        d = pdm.decide(_at(hh=12), self.friends, self.scores, {},
                       exclude={"1002"})
        if d.get("ok"):
            self.assertIn(d["uid"], {"1001", "1003"})


if __name__ == "__main__":
    unittest.main()
