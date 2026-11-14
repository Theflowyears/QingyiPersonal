"""搜索只对「明确冲着她来的指令」回复；她自己感兴趣的查询只静默写知识库。

生产现场（主人新规矩）：
  群里没人点她、她只是自己好奇去查的东西，不该专门回一句「等一下喵~」再
  补一句「没查到 / 查到了……」这种僵硬交代；能搜到接上话就按普通聊天说，
  搜不到就静默写库或丢弃，继续水群。只有 @ 她 / 私聊里明确要她查的，才走
  「先应一声、查到再交代」的回复链路。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import qq_openclaw_bridge as b  # noqa: E402


class SlowLookupRouteTests(unittest.TestCase):
    def test_directed_search_replies(self):
        self.assertEqual(b._slow_lookup_route("search", True), "reply")

    def test_non_directed_search_is_silent(self):
        self.assertEqual(b._slow_lookup_route("search", False), "silent")

    def test_directed_weather_and_divine_reply(self):
        self.assertEqual(b._slow_lookup_route("weather", True), "reply")
        self.assertEqual(b._slow_lookup_route("divine", True), "reply")

    def test_non_directed_weather_and_divine_are_dropped(self):
        self.assertEqual(b._slow_lookup_route("weather", False), "skip")
        self.assertEqual(b._slow_lookup_route("divine", False), "skip")

    def test_unknown_kind_is_skipped(self):
        self.assertEqual(b._slow_lookup_route("", True), "skip")


class SilentLookupWritesButNeverRepliesTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_search_writes_knowledge_without_any_message(self):
        import web_search
        sent = []
        kb = AsyncMock(return_value={"saved": True, "reason": "值得记"})
        with patch.object(b, "_call_name", return_value="主人"), \
                patch.object(b, "_lookup_is_owed", return_value=False), \
                patch.object(b, "_tool_plan", return_value={"cloud": False}), \
                patch.object(b, "search_query_or_fallback",
                             new=AsyncMock(return_value="量子计算")), \
                patch.object(b, "_search_plan",
                             new=AsyncMock(return_value={"query": "量子计算", "entity": "量子计算"})), \
                patch.object(web_search, "verified_search",
                             new=AsyncMock(return_value={
                                 "ok": True, "source": "bing", "checked": 3,
                                 "results": [{"title": "量子计算最新进展",
                                              "snippet": "量子比特数再创新高，纠错取得突破，研究团队称已接近实用门槛，"
                                                         "多家机构开始把量子处理器接入真实的计算任务进行验证。"}]})), \
                patch.object(b, "_kb_judge", new=kb), \
                patch.object(b, "_world_note"), \
                patch.object(b, "_curiosity_learn"), \
                patch.object(b, "send_group_msg", new=AsyncMock(side_effect=sent.append)), \
                patch.object(b, "send_private_msg", new=AsyncMock(side_effect=sent.append)), \
                patch.object(b, "call_openclaw", new=AsyncMock(return_value="不该被调用")) as model:
            await b._lookup_then_reply("group-9001", "量子计算有什么新进展", "923", "1", "群友",
                                       "search", {"preferred": "量子计算"}, said=None,
                                       context="", silent=True)
        self.assertEqual(sent, [], "静默查询不该发任何消息")
        model.assert_not_called()
        kb.assert_awaited()  # 搜到的内容仍要判一次、写进知识库


if __name__ == "__main__":
    unittest.main()
