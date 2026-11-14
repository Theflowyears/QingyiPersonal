"""回归锁：查证必须绑在「完整实体」上，而不是一批泛词的重叠上。

起因（2026-11-08 取证）：
  她自己要查「卫戍协议 多人联机」，提示词那边把它扩写成
  《卫戍协议》（Garrison Protocol），查询又降格成「卫戍」；
  回来的一批结果里混着《看门狗：军团》—— 它只是同时出现了
  「协议 / 多人 / 支持 / 模式 / 联机」这些泛词，旧 relevance 就放行了，
  整批结果被 active 写进知识库。她于是把别人的游戏当成了自己的设定。

这里锁四件事：
  1) plan：完整专名不被拆字、不被降格、不凭空补英文名；
  2) score_result / verify_page：逐条判实体、领域、回答要点，泛词重叠不算；
  3) verified_search：只返回通过判定的条目，同批无关页不许混进来；
  4) knowledge_base：证据字段可留存，隔离后的条目不再注入提示词。
"""
import asyncio
import inspect
import json
import os
import shutil
import sys
import tempfile
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import web_search  # noqa: E402
import curiosity as cu  # noqa: E402
import knowledge_base as kb  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def squash(text):
    import re
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower())


PLAN_TEXT = "卫戍协议多人联机怎么样"

# 《看门狗：军团》那种页：泛词全中，完整实体一次都没有
WATCHDOG = {
    "title": "《看门狗：军团》多人联机模式详解",
    "snippet": "本作支持最多四人在线合作，协议更新后新增多人玩法，"
               "模式与联机体验均有改动，支持跨平台组队。",
    "url": "https://example.invalid/watchdogs",
}
# 真正在讲《卫戍协议》的页
GATE = {
    "title": "《卫戍协议》多人联机玩法说明",
    "snippet": "《卫戍协议》支持多人联机协作守城，玩家可以四人组队布防。",
    "url": "https://example.invalid/gate",
}
# 实体对，但没有任何多人联机的证据
ENTITY_ONLY = {
    "title": "《卫戍协议》规则简介",
    "snippet": "《卫戍协议》是一款桌面推演游戏，玩家轮流布置防线。",
    "url": "https://example.invalid/gate-rules",
}
UNRELATED = {
    "title": "重庆今日天气",
    "snippet": "多云转晴，气温16到22度，适合出门。",
    "url": "https://example.invalid/weather",
}

GATE_SUMMARY = ("《卫戍协议》是支持多人联机协作的守城游戏，"
                "玩家需要四人组队布防，规则里还有协议轮换与联机匹配。")
WATCHDOG_SUMMARY = ("《看门狗：军团》支持多人联机，协议更新后新增合作模式，"
                    "玩家可以招募任何人加入队伍；站内还提到 Garrison Protocol。")


class PlanTests(unittest.TestCase):
    def test_heuristic_keeps_the_whole_proper_name(self):
        p = run(web_search.plan(PLAN_TEXT))
        self.assertEqual(p["entity"], "卫戍协议")
        self.assertIn("卫戍协议", p["query"])
        self.assertNotEqual(squash(p["query"]), "卫戍")

    def test_heuristic_never_turns_the_name_into_two_char_words(self):
        p = run(web_search.plan(PLAN_TEXT))
        for q in [p["query"]] + list(p["alternates"]):
            self.assertNotIn(q, ("卫戍", "协议", "多人", "联机"), p)

    def test_heuristic_does_not_invent_an_english_alias(self):
        p = run(web_search.plan(PLAN_TEXT))
        blob = json.dumps(p, ensure_ascii=False).lower()
        self.assertNotIn("garrison", blob)

    def test_llm_plan_is_used_when_injected(self):
        async def fake_llm(prompt):
            self.assertIn("卫戍协议", prompt)
            return ('{"entity": "卫戍协议", "domain": "多人联机", '
                    '"question": "卫戍协议多人联机怎么样", '
                    '"query": "卫戍协议 多人联机", "alternates": ["卫戍协议"]}')

        p = run(web_search.plan(PLAN_TEXT, context="刚才在聊多人联机", llm=fake_llm))
        self.assertEqual(p["entity"], "卫戍协议")
        self.assertEqual(p["domain"], "多人联机")
        self.assertIn("卫戍协议", p["query"])

    def test_llm_cannot_degrade_the_full_name(self):
        async def fake_llm(_):
            return ('{"entity": "卫戍", "domain": "多人联机", '
                    '"query": "卫戍 多人联机", "alternates": ["卫戍"]}')

        p = run(web_search.plan(PLAN_TEXT, llm=fake_llm))
        self.assertEqual(p["entity"], "卫戍协议")
        self.assertIn("卫戍协议", p["query"])
        self.assertNotIn("卫戍 多人联机", p["query"])
        for a in p["alternates"]:
            self.assertIn("卫戍协议", a)

    def test_llm_cannot_expand_in_an_english_alias(self):
        async def fake_llm(_):
            return ('{"entity": "《卫戍协议》（Garrison Protocol）", '
                    '"query": "Garrison Protocol"}')

        p = run(web_search.plan(PLAN_TEXT, llm=fake_llm))
        self.assertEqual(p["entity"], "卫戍协议")
        self.assertIn("卫戍协议", p["query"])
        self.assertNotIn("garrison", json.dumps(p, ensure_ascii=False).lower())

    def test_broken_llm_output_falls_back_to_heuristic(self):
        async def fake_llm(_):
            return "我觉得是卫戍协议吧"

        p = run(web_search.plan(PLAN_TEXT, llm=fake_llm))
        self.assertEqual(p["entity"], "卫戍协议")
        self.assertIn("卫戍协议", p["query"])

    def test_domain_is_only_filled_with_evidence(self):
        async def fake_llm(_):
            return ('{"entity": "卫戍协议", "domain": "股价", '
                    '"query": "卫戍协议 股价"}')

        p = run(web_search.plan("卫戍协议是什么", llm=fake_llm))
        self.assertEqual(p["domain"], "")

    def test_callers_can_check_a_query_keeps_the_full_entity(self):
        # 桥/好奇模块会把各自挑的词交回来，必须能判「这词有没有降格」
        p = run(web_search.plan(PLAN_TEXT))
        self.assertTrue(web_search.query_keeps_entity(p, "卫戍协议 多人联机"))
        self.assertFalse(web_search.query_keeps_entity(p, "卫戍"))
        self.assertFalse(web_search.query_keeps_entity(p, "协议 多人联机"))


class ScoreTests(unittest.TestCase):
    def plan(self, text=PLAN_TEXT):
        return run(web_search.plan(text))

    def test_watchdog_page_is_rejected_despite_generic_overlap(self):
        p = self.plan()
        # 取证：旧函数确实会放行同一页 —— 这条锁证明问题真实存在
        self.assertTrue(web_search.relevance(PLAN_TEXT, [WATCHDOG]))
        r = web_search.score_result(p, WATCHDOG)
        self.assertFalse(r["accepted"])
        self.assertFalse(r["entity_match"])
        self.assertTrue(r["reasons"])

    def test_page_with_full_entity_and_domain_is_accepted(self):
        r = web_search.score_result(self.plan(), GATE)
        self.assertTrue(r["accepted"], r)
        self.assertTrue(r["entity_match"])
        self.assertTrue(r["domain_match"])
        self.assertGreaterEqual(float(r["score"]), 0.8)

    def test_entity_without_domain_evidence_is_rejected(self):
        r = web_search.score_result(self.plan(), ENTITY_ONLY)
        self.assertTrue(r["entity_match"])
        self.assertFalse(r["domain_match"])
        self.assertFalse(r["accepted"])

    def test_entity_alone_does_not_pass_the_full_question(self):
        # 只把「卫戍」写进标题的页 —— 完整实体逐字不在，不能算命中
        only_half = {"title": "卫戍是什么意思", "snippet": "卫戍：驻防、防守。"}
        r = web_search.score_result(self.plan(), only_half)
        self.assertFalse(r["entity_match"])
        self.assertFalse(r["accepted"])

    def test_verify_page_returns_the_same_verdict_as_score_result(self):
        p = self.plan()
        good = web_search.verify_page(p, GATE["title"], GATE["snippet"])
        bad = web_search.verify_page(p, WATCHDOG["title"], WATCHDOG["snippet"])
        self.assertTrue(good["accepted"], good)
        self.assertFalse(bad["accepted"], bad)


def fake_search(batches):
    """按调用顺序回放结果批；调用参数记下来给测试断言。"""
    calls = []

    async def _fn(q, limit=None, **kwargs):
        calls.append(q)
        i = min(len(calls) - 1, len(batches) - 1)
        return {"ok": True, "results": [dict(x) for x in batches[i]],
                "source": "fake"}

    return _fn, calls


class VerifiedSearchTests(unittest.TestCase):
    def test_rejected_pages_never_mix_into_the_batch(self):
        fn, _ = fake_search([[WATCHDOG, GATE, UNRELATED]])
        r = run(web_search.verified_search(PLAN_TEXT, search_fn=fn))
        self.assertTrue(r["ok"], r)
        titles = [x["title"] for x in r["results"]]
        self.assertEqual(titles, [GATE["title"]])
        self.assertEqual(r["checked"], 3)
        self.assertEqual(r["source"], "fake")

    def test_whole_batch_of_wrong_entity_reports_entity_mismatch(self):
        fn, _ = fake_search([[WATCHDOG, UNRELATED]])
        r = run(web_search.verified_search(PLAN_TEXT, search_fn=fn))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "entity_mismatch")
        self.assertEqual(r["results"], [])

    def test_alternate_is_retried_at_most_once(self):
        fn, calls = fake_search([[WATCHDOG], [ENTITY_ONLY], [GATE]])
        r = run(web_search.verified_search(PLAN_TEXT, search_fn=fn))
        self.assertFalse(r["ok"])
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(all("卫戍协议" in q for q in calls), calls)
        self.assertEqual(r["results"], [])

    def test_alternate_keeps_the_full_name_and_can_rescue(self):
        fn, calls = fake_search([[WATCHDOG], [GATE]])
        r = run(web_search.verified_search(PLAN_TEXT, search_fn=fn))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual([x["title"] for x in r["results"]], [GATE["title"]])
        self.assertIn("卫戍协议", calls[1])

    def test_no_results_at_all_is_no_answer(self):
        async def empty(q, limit=None, **kwargs):
            return {"ok": False, "results": [], "error": "空结果"}

        r = run(web_search.verified_search(PLAN_TEXT, search_fn=empty))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_answer")
        self.assertEqual(r["results"], [])


class CuriosityPlanTests(unittest.TestCase):
    def test_topic_is_not_split_into_two_char_words(self):
        p = cu.act_plan("卫戍协议 多人联机")
        self.assertEqual(p["queries"][0], "卫戍协议 多人联机")
        for q in p["queries"]:
            self.assertNotIn(q, ("卫戍", "协议", "多人", "联机"), p)

    def test_every_query_keeps_the_full_entity(self):
        p = cu.act_plan("卫戍协议多人联机怎么玩")
        self.assertTrue(p["queries"])
        for q in p["queries"]:
            self.assertIn("卫戍协议", q, p)

    def test_existing_primary_query_contract_is_kept(self):
        p = cu.act_plan("赛博朋克 2077 荒坂塔")
        self.assertEqual(p["queries"][0], "赛博朋克 2077 荒坂塔")


class KbBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kbe_")
        self._old_env = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = self.dir
        self._old_file = kb._FILE
        kb._FILE = os.path.join(self.dir, "memory", "world_knowledge.json")
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})

    def tearDown(self):
        kb._FILE = self._old_file
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})
        if self._old_env is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_env
        shutil.rmtree(self.dir, ignore_errors=True)

    def raw_topics(self):
        if not os.path.isfile(kb._FILE):
            return []
        with open(kb._FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("topics") or []


class EvidenceTests(KbBase):
    def test_remember_keeps_evidence_fields(self):
        ok = kb.remember("卫戍协议", "《卫戍协议》多人联机说明", GATE_SUMMARY,
                         source="fake", category="作品设定",
                         evidence_url="https://example.invalid/gate",
                         evidence=GATE["snippet"], verified=True)
        self.assertTrue(ok)
        it = kb.get("卫戍协议")
        self.assertIsNotNone(it)
        self.assertEqual(it["evidence_url"], "https://example.invalid/gate")
        self.assertEqual(it["evidence"], GATE["snippet"])
        self.assertIs(it["verified"], True)

    def test_evidence_fields_survive_a_load_save_round(self):
        kb.remember("卫戍协议", "《卫戍协议》多人联机说明", GATE_SUMMARY,
                    evidence_url="https://example.invalid/gate",
                    evidence=GATE["snippet"], verified=False)
        kb.save(kb.load())
        it = kb.get("卫戍协议")
        self.assertEqual(it["evidence_url"], "https://example.invalid/gate")
        self.assertIs(it["verified"], False)

    def test_rewriting_without_evidence_does_not_erase_it(self):
        # 判定通道/兜底路径重写同一条时不传 evidence，旧证据必须留着
        kb.remember("卫戍协议", "《卫戍协议》多人联机说明", GATE_SUMMARY,
                    evidence_url="https://example.invalid/gate",
                    evidence=GATE["snippet"], verified=True)
        kb.remember("卫戍协议", "《卫戍协议》多人联机说明", GATE_SUMMARY)
        it = kb.get("卫戍协议")
        self.assertEqual(it["evidence_url"], "https://example.invalid/gate")
        self.assertEqual(it["evidence"], GATE["snippet"])
        self.assertIs(it["verified"], True)


class QuarantineTests(KbBase):
    def test_quarantined_item_is_not_injected(self):
        self.assertTrue(kb.remember("卫戍协议", "《卫戍协议》多人联机说明",
                                    GATE_SUMMARY, category="作品设定"))
        self.assertTrue(kb.remember("荒坂塔", "赛博朋克2077 荒坂塔",
                                    "荒坂塔是荒坂公司总部，立在夜之城中心。",
                                    category="作品设定"))
        self.assertIn("卫戍协议", kb.build_block(query="卫戍协议"))

        self.assertTrue(kb.quarantine("卫戍协议", "实体不符：卫戍协议≠看门狗军团"))

        self.assertNotIn("卫戍协议", kb.build_block(query="卫戍协议"))
        self.assertEqual("", kb.build_block(query="保底还有别的知识"))
        self.assertIn("荒坂塔", kb.build_block(query="荒坂塔"))
        self.assertIsNone(kb.get("卫戍协议"))
        self.assertEqual(kb.get("卫戍协议", include_quarantined=True)["status"],
                         "quarantined")
        rec = [t for t in self.raw_topics() if t.get("key") == "卫戍协议"][0]
        self.assertEqual(rec["status"], "quarantined")
        self.assertIn("看门狗", rec["quarantine_reason"])


class MigrationTests(KbBase):
    def seed(self):
        self.assertTrue(kb.remember("卫戍协议", "《卫戍协议》多人联机说明",
                                    WATCHDOG_SUMMARY, category="作品设定"))
        self.assertTrue(kb.remember("荒坂塔", "赛博朋克2077 荒坂塔",
                                    "荒坂塔是荒坂公司总部，立在夜之城中心。",
                                    category="作品设定"))

    def test_default_is_a_dry_run(self):
        p = inspect.signature(kb.migrate_quarantine_garrison).parameters
        self.assertIs(p["dry_run"].default, True)

    def test_dry_run_reports_without_touching_the_store(self):
        self.seed()
        report = kb.migrate_quarantine_garrison()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["matched"], 1, report)
        self.assertEqual(report["quarantined"], 0)
        self.assertIsNotNone(kb.get("卫戍协议"))

    def test_migration_quarantines_only_the_mismatched_record(self):
        self.seed()
        report = kb.migrate_quarantine_garrison(dry_run=False)
        self.assertEqual(report["quarantined"], 1, report)
        self.assertIsNone(kb.get("卫戍协议"))
        self.assertIsNotNone(kb.get("荒坂塔"))
        self.assertNotIn("卫戍协议", kb.build_block(query="卫戍协议"))
        again = kb.migrate_quarantine_garrison(dry_run=False)
        self.assertEqual(again["quarantined"], 0, again)


if __name__ == "__main__":
    unittest.main()
