"""回归锁：她说「给你加一分」，后台就真的加一分。

线上原话：「她明明说了『就加一分吧，八十五分』但实际后台查看我的好感度却并没有增加」。
查了日志：那句话没落盘。原因是 `apply_self_any` 只在「刚刚有人问过好感」时才工作，
而主人问的是「能不能涨一点呀」——没有「好感」两个字，于是她的承诺无处落地。

顺带锁住误伤：她自己说「等我三分钟」「这道菜加五分」不能改好感。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import affection as af       # noqa: E402

UID = "1000000001"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aff_")
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        af._applied.clear()
        af._pending.clear()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        af._applied.clear()
        af._pending.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def score(self):
        return af.get(UID)


class ParseTests(Base):
    def test_promise_up(self):
        got = af.parse_promise("就加一分吧，八十五分")
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 1)

    def test_promise_down(self):
        self.assertEqual(af.parse_promise("扣你两分")[0], -2)

    def test_promise_chinese_and_digits(self):
        self.assertEqual(af.parse_promise("再给你涨 3 分")[0], 3)
        self.assertEqual(af.parse_promise("那就减五分")[0], -5)

    def test_minutes_are_not_points(self):
        self.assertIsNone(af.parse_promise("等我三分钟"))

    def test_unrelated_points_are_not_affection(self):
        self.assertIsNone(af.parse_promise("这道菜加五分"),
                          "没有称呼方向的「加X分」不能当成好感")

    def test_long_text_is_not_a_promise(self):
        self.assertIsNone(af.parse_promise(
            "今天这个副本打了好久啊，说起来我给你加一分吧，下次再打"))

    def test_text_with_haogan_goes_the_other_route(self):
        self.assertIsNone(af.parse_promise("好感度加一分"),
                          "带「好感」的走 parse_self_report，别两条路都算")


class ApplyTests(Base):
    def test_promise_lands(self):
        before = self.score()
        got = af.apply_promise(UID, "就加一分吧")
        self.assertIsNotNone(got)
        self.assertEqual(self.score(), before + 1)
        e = af._load().get(UID) or {}
        hist = (e.get("history") or [])[-1]
        self.assertEqual(hist.get("by"), "promise")
        self.assertIn("加一分", hist.get("said") or "")

    def test_promise_is_capped(self):
        before = self.score()
        af.apply_promise(UID, "给你加九分")
        self.assertEqual(self.score(), before + 5, "单次最多 5 分")

    def test_self_any_with_target_uid_applies_it(self):
        """回归：主人只说了「能不能涨一点呀」，没提「好感」两个字。"""
        before = self.score()
        got = af.apply_self_any("就加一分吧，八十五分", uid=UID)
        self.assertIsNotNone(got)
        self.assertEqual(self.score(), before + 1)

    def test_self_any_without_uid_still_needs_a_question(self):
        before = self.score()
        self.assertIsNone(af.apply_self_any("就加一分吧，八十五分"))
        self.assertEqual(self.score(), before)

    def test_same_bubble_is_counted_once(self):
        before = self.score()
        af.apply_self_any("就加一分吧", uid=UID)
        af.apply_self_any("就加一分吧", uid=UID)
        self.assertEqual(self.score(), before + 1, "同一条发送路径会调两次")

    def test_score_never_exceeds_the_ceiling(self):
        d = af._load()
        e = af._entry(d, UID)
        e["score"] = 99
        af._save(d)
        af.apply_promise(UID, "给你加五分")
        self.assertLessEqual(self.score(), af.MAX_SCORE)


class MisfireTests(Base):
    """线上核对时抓到过：「等我三分钟」把好感改成了 3 分。

    原因是给「回复目标」传 uid 之后走了 allow_bare 的裸数字解析。
    """

    def test_minutes_never_touch_affection(self):
        before = self.score()
        af.apply_self_any("等我三分钟", uid=UID)
        self.assertEqual(self.score(), before)

    def test_plain_number_without_a_question_does_nothing(self):
        before = self.score()
        af.apply_self_any("八十五分", uid=UID)
        self.assertEqual(self.score(), before, "没被问就别自己报数改分")

    def test_promise_still_works_without_any_question(self):
        before = self.score()
        af.apply_self_any("再给你涨三分", uid=UID)
        self.assertEqual(self.score(), before + 3,
                         "承诺这条路不依赖「刚有人问过好感」")

    def test_time_phrases_are_not_promises(self):
        for text in ("等我三分钟", "再给我五分钟", "三分钟就好"):
            with self.subTest(text=text):
                self.assertIsNone(af.parse_promise(text))


class SafeRelationshipTests(Base):
    FOX = '771901'
    AYAN = '771902'

    def setUp(self):
        super().setUp()
        af.set_score(self.FOX, 86, why='test baseline')
        af.set_score(self.AYAN, 19, why='test baseline')
        af.set_nick(self.FOX, '阿戊')
        af.set_nick(self.AYAN, '阿甲')

    def test_game_title_and_lookup_instructions_never_set_relationship(self):
        text = '阿戊姐姐，具体数值没摸到，只查到仙剑三怎么看好感度，按键就能查看。'
        self.assertIsNone(af.parse_self_report(text))
        af.note_context(self.FOX, '查询好感度')
        af.apply_self_any(text, uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)

    def test_reporting_a_different_score_does_not_overwrite_stored_truth(self):
        for text in ('好感度是二十分', '二十分。', '现在是20分', '好感度-20'):
            with self.subTest(text=text):
                af.note_context(self.FOX, '查询好感度')
                af.apply_self_any(text, uid=self.FOX)
                self.assertEqual(af.get(self.FOX), 86)

    def test_explicit_relationship_target_wins_over_message_recipient(self):
        af.apply_self_any('行吧，那就给阿甲加到二十分。', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)
        self.assertEqual(af.get(self.AYAN), 20)

    def test_unknown_or_ambiguous_named_target_never_falls_back_to_recipient(self):
        af.apply_self_any('那就给陌生姐姐加到二十分。', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)
        af.set_nick('771903', '阿甲')
        af.apply_self_any('那就给阿甲加两分吧。', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)
        self.assertEqual(af.get(self.AYAN), 19)

    def test_question_negation_quote_and_unrelated_numbers_do_not_mutate(self):
        for text in ('是不是该给你加一点？', '不给你扣两分', '要是乖就给你加两分',
                     '他说“给你加两分”', '你说的好感度80分不是我说的',
                     '这道菜给你加一点盐', '好感度加两分，仙剑三挺好玩'):
            with self.subTest(text=text):
                before = af.get(self.FOX)
                af.apply_self_any(text, uid=self.FOX)
                if text == '好感度加两分，仙剑三挺好玩':
                    self.assertEqual(af.get(self.FOX), before + 2)
                else:
                    self.assertEqual(af.get(self.FOX), before)

    def test_autonomous_small_delta_and_abstract_expression_still_work(self):
        af.apply_self_any('哼，生气了，减一点', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 85)
        af.apply_self_any('好啦给你加回去', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)
        af.apply_self_any('给你加两点好感', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 88)

    def test_large_reset_requires_explicit_reason_not_a_wrong_report(self):
        af.apply_self_any('给你好感度降到20分', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)

    def test_named_banter_not_directed_at_her_has_no_keyword_gain(self):
        before = af.get(self.FOX)
        af.judge_and_bump(self.FOX, '抱抱阿甲', nick='阿戊')
        self.assertEqual(af.get(self.FOX), before)

    def test_keyword_fallback_and_her_explicit_delta_do_not_double_count(self):
        af.judge_and_bump(self.FOX, '谢谢你', nick='阿戊', turn_id='group:1:20')
        af.apply_self_any('给你加一分吧', uid=self.FOX, turn_id='group:1:20')
        self.assertEqual(af.get(self.FOX), 87)

    def test_unrelated_actions_with_numbers_do_not_change_relationship(self):
        for text in ('给你加三勺盐', '我加班三小时', '给你降到20级', '还你三块钱',
                     '你说得对，给你加一分吗？', '不想给你加两分', '给他加两分'):
            with self.subTest(text=text):
                af.apply_self_any(text, uid=self.FOX)
                self.assertEqual(af.get(self.FOX), 86)

    def test_refund_returns_only_real_recent_deduction(self):
        af.apply_self_any('给你扣两分', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 84)
        af.apply_self_any('刚才扣的两分还给你', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)
        af.apply_self_any('再还你两分', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)

    def test_deliberate_major_reassessment_requires_reason_and_explicit_object(self):
        af.apply_self_any('因为你反复泄露我的隐私，我决定把对你的好感度降到20分', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 20)

    def test_later_turn_does_not_undo_previous_legitimate_keyword_gain(self):
        af.judge_and_bump(self.FOX, '谢谢你', turn_id='group:1:20')
        af.apply_self_any('给你加一分吧', uid=self.FOX, turn_id='group:1:21')
        self.assertEqual(af.get(self.FOX), 89)

    def test_negated_and_open_ended_conditions_never_write(self):
        for text in ('没有给你加两分', '给你加两分就能通关', '加点辣椒',
                     '对陌生姐姐的好感加两分', '给主人加一分',
                     '对陌生姐姐好感加两分', '对她好感加两分',
                     '不能给你扣两分', '不是给你加两分'):
            with self.subTest(text=text):
                af.apply_self_any(text, uid=self.FOX)
                self.assertEqual(af.get(self.FOX), 86)

    def test_abstract_relationship_wording_keeps_autonomy(self):
        af.apply_self_any('那就给你加点好感度吧', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 87)

    def test_failed_save_cannot_claim_an_applied_change(self):
        from unittest.mock import patch
        with patch.object(af.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                af.apply_self_any('给你加两分吧', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)

    def test_same_turn_explicit_decision_blocks_later_keyword_fallback(self):
        af.apply_self_any('给你加一分吧', uid=self.FOX, turn_id='group:1:30')
        af.judge_and_bump(self.FOX, '谢谢你', turn_id='group:1:30')
        self.assertEqual(af.get(self.FOX), 87)

    def test_multiple_processes_do_not_lose_updates(self):
        import subprocess
        code = ('import sys; sys.path.insert(0,sys.argv[1]); import affection as a; '
                '[(a.bump(sys.argv[2],1,"parallel")) for _ in range(5)]')
        children = [subprocess.Popen([sys.executable, '-c', code, BOT, self.FOX],
                                      env=dict(os.environ)) for _ in range(2)]
        for child in children:
            self.assertEqual(child.wait(timeout=30), 0)
        self.assertEqual(af.get(self.FOX), 96)

    def test_named_target_in_previous_clause_is_not_the_recipient(self):
        af.apply_self_any('阿甲姐姐，那就给你加两分吧', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 86)
        self.assertEqual(af.get(self.AYAN), 21)

    def test_inactivity_alone_does_not_rewrite_her_relationship(self):
        data = af._load()
        data[self.FOX].update(day='2026-01-01', last=time.time()-20*86400)
        af._save(data)
        af.apply_self_any('给你加一分吧', uid=self.FOX)
        self.assertEqual(af.get(self.FOX), 87)

    def test_read_error_never_turns_existing_relationships_into_empty_store(self):
        import builtins
        from unittest.mock import patch
        real_open = builtins.open
        def denied(path, *args, **kwargs):
            if str(path) == af._file():
                raise PermissionError('transient read failure')
            return real_open(path, *args, **kwargs)
        with patch('builtins.open', side_effect=denied):
            with self.assertRaises(PermissionError):
                af.set_score(self.FOX, 80)
        self.assertEqual(af.get(self.FOX), 86)
        self.assertEqual(af.get(self.AYAN), 19)

    def test_algorithm_writes_actual_change_into_history(self):
        before = af.get(self.FOX)
        delta, _ = af.judge_and_bump(self.FOX, '谢谢宝宝', nick='阿戊')
        after = af.get(self.FOX)
        self.assertEqual(delta, after - before)
        history = af._load()[self.FOX]['history']
        self.assertEqual(history[-1]['by'], 'algo')
        self.assertEqual(history[-1]['from'], before)
        self.assertEqual(history[-1]['to'], after)


if __name__ == "__main__":
    unittest.main()