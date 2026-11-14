"""回归锁：桥接层不能再把「卫戍协议」降成「卫戍」再去搜。

三道线：
  1) 模型计划里截短的实体，会被原话里的完整专名顶回来；
  2) 她自己给的短词只有在保留完整实体时才作主查询；
  3) 自主好奇拿短词查不到时，换回完整实体；摘要对得上、正文跑题的页面不许当知识。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BOT = Path(__file__).resolve().parents[1] / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import qq_openclaw_bridge as b     # noqa: E402
import web_search as ws           # noqa: E402


class PlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_entity_from_model_is_restored_to_full_name(self):
        raw = ('{"entity":"卫戍","domain":"多人联机","question":"怎么打",'
               '"query":"卫戍 多人联机","alternates":["卫戍"]}')
        with patch.object(b, "call_openclaw", AsyncMock(return_value=raw)):
            plan = await b._search_plan("卫戍协议多人联机怎么打")
        self.assertEqual(plan["entity"], "卫戍协议")
        self.assertIn("卫戍协议", plan["query"])
        self.assertEqual(plan["domain"], "多人联机")

    async def test_short_preferred_query_is_not_used(self):
        with patch.object(b, "call_openclaw", AsyncMock(return_value="not json")):
            plan = await b._search_plan("卫戍协议多人联机怎么打",
                                        preferred="卫戍")
        self.assertIn("卫戍协议", plan["query"])
        self.assertNotEqual(plan["query"], "卫戍")

    async def test_preferred_query_keeps_entity_when_complete(self):
        with patch.object(b, "call_openclaw", AsyncMock(return_value="not json")):
            plan = await b._search_plan("卫戍协议多人联机怎么打",
                                        preferred="卫戍协议 联机")
        self.assertEqual(plan["query"], "卫戍协议 联机")


class VerifiedWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdogs_page_never_leaks_into_results(self):
        async def fake_search(query, limit=6, **_kw):
            return {"ok": True, "source": "fake", "results": [
                {"title": "育碧确认《看门狗:军团》多人模式跨平台联机",
                 "snippet": "《看门狗:军团》多人模式支持跨平台联机，协议与玩法说明。"},
            ]}

        plan = ws.plan_heuristic("卫戍协议多人联机怎么打")
        got = await ws.verified_search(plan, search_fn=fake_search)
        self.assertFalse(got["ok"])
        self.assertEqual(got["results"], [])
        self.assertEqual(got["reason"], "entity_mismatch")
        self.assertEqual(got["source"], "fake")

    async def test_matching_page_is_accepted_with_evidence(self):
        async def fake_search(query, limit=6, **_kw):
            return {"ok": True, "results": [
                {"title": "明日方舟 卫戍协议 多人联机玩法说明",
                 "snippet": "卫戍协议支持多人联机，双方布防推进，攻略与阵容见下。"},
            ]}

        plan = ws.plan_heuristic("卫戍协议多人联机怎么打")
        got = await ws.verified_search(plan, search_fn=fake_search)
        self.assertTrue(got["ok"], got)
        self.assertIn("卫戍协议", got["results"][0]["title"])
        self.assertTrue(got["results"][0]["verification"]["accepted"])


class CuriosityWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_query_is_replaced_and_page_mismatch_falls_back(self):
        import curiosity as cu
        import web_read as wr

        plan = {"entity": "卫戍协议", "domain": "多人联机",
                "question": "卫戍协议多人联机怎么打",
                "query": "卫戍协议 多人联机", "alternates": []}
        seen = {}

        async def fake_verified(plan_obj, **kw):
            seen.update(plan_obj)
            return {"ok": True, "source": "bing", "checked": 3, "results": [
                {"title": "明日方舟 卫戍协议 玩法", "url": "https://example.com/a",
                 "snippet": "卫戍协议是明日方舟的多人联机玩法，双方轮流布置干员推进，"
                            "需要根据对方阵容调整站位和技能释放时机。"},
            ]}

        async def fake_page(url, **_kw):
            # 摘要对得上，正文其实是另一个游戏
            return {"ok": True, "title": "看门狗军团攻略",
                    "text": "艾登皮尔斯潜入伦敦，看门狗军团的多人模式与黑客玩法。" * 5}

        judged = {}

        async def fake_judge(_kb, key, title, summary, source):
            judged.update(key=key, summary=summary)
            return {"saved": True}

        with patch.object(cu, "act_plan", return_value={"queries": ["卫戍"]}), \
             patch.object(b, "_search_plan", AsyncMock(return_value=plan)), \
             patch.object(ws, "verified_search", fake_verified), \
             patch.object(wr, "fetch_page", fake_page), \
             patch.object(b, "_kb_judge", fake_judge), \
             patch.object(b, "_world_note", lambda *a, **k: None):
            found, keys = await b._curiosity_lookup("卫戍协议多人联机怎么打")

        self.assertTrue(found)
        self.assertEqual(keys, ["kb:卫戍协议"])
        self.assertEqual(seen["entity"], "卫戍协议")
        self.assertEqual(seen["query"], "卫戍协议 多人联机")
        self.assertEqual(judged["key"], "卫戍协议")
        self.assertNotIn("艾登", judged["summary"])


if __name__ == "__main__":
    unittest.main()
