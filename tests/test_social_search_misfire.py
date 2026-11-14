"""社交/睡前闲聊不该莫名其妙联网查，更不该查不到就甩一篇「没办成」的交代。

生产现场（主人报的睡前对话）：
    她对着「今天开心吗」这种话去联网查，查回来对不上，于是郑重交代
    「主人，我没办成，翻到的东西对不上今天那句……」—— 把系统的中间状态
    当成回复砸进闲聊里。另一段是「这次没成……只翻到一堆很旧的问答……奥运……
    卡在材料太旧……」整段刷屏。

根子有两处，都在裁决层：
  1. `_needs_fresh_info` 的弱时效规则是「今天/最近 + 任意疑问词」，于是
     「今天开心吗」（今天 + 吗）被当成时效问题，抢在社交护栏前面 return True。
  2. `is_owed_request` 把「任何以 吗/呢/什么 结尾的句子」都算点名办事，于是
     社交问句查不到也走交货出口，逼模型写一篇失败汇报。

修法是认得出「这是在问感情/问熟人/寒暄」，在联网前和交货前各挡一道；
带实事名词（天气/夺冠/综艺/Mon3tr）或点名要查（帮我查/帮我写）的仍是正事。
"""
import asyncio
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import qq_openclaw_bridge as b             # noqa: E402

# 睡前/社交里真实会出现的话：问感情、问熟人、寒暄、撒娇。都不该联网。
SOCIAL = [
    "今天开心吗",
    "今天有没有想我",
    "开心吗",
    "你开心吗",
    "想我了吗",
    "你是不是喜欢我呀",
    "嘿嘿开心",
    "晚安啦笨蛋",
    "梦里见哦",
    "呜呜呜晚安",
    "困不困呀",
    "明天叫我起床好不好",
    "最近有没有想我",
    "今天有没有乖乖的",
]

# 这些是真要查/真要办的正事，不能被社交护栏一起挡掉。
FACTUAL = [
    "滔博夺冠了吗",
    "今天天气怎么样",
    "帮我查查明日方舟Mon3tr是什么",
    "后会友期，是一个综艺节目",
    "刷到了后会友期的片段",
    "2026年诺贝尔文学奖得主是谁",
]


class LocalStateSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_affection_and_self_state_queries_do_not_become_web_tasks(self):
        for text in ("查询好感度宝宝", "@清漪 查询好感度宝宝", "帮我查一下我的好感度",
                     "你对我的好感多少", "看看你现在的心情", "你记得我叫什么吗",
                     "查询一下我的好感度", "查下我的好感度", "查一查好感度宝宝"): 
            with self.subTest(text=text):
                self.assertFalse(b._explicit_lookup(text))
                self.assertFalse(b._should_web_search(text))
                self.assertFalse(b.is_owed_request(text))
                self.assertEqual(b._classify_intent(text)[0], "chat")

    def test_external_explanations_and_mixed_requests_still_search(self):
        for text in ("查询明日方舟好感度怎么提升", "搜索心理学中的好感度理论",
                     "查询好感度，另外帮我查重庆天气", "查询好感度并搜索明日方舟攻略", "查询原神好感度怎么提升",
                     "查询好感度，另外帮我查Python最新版本"): 
            with self.subTest(text=text):
                self.assertTrue(b._should_web_search(text))

    async def test_accidentally_scheduled_local_query_never_searches_or_reports_failure(self):
        import web_search
        with patch.object(web_search, "verified_search", new=AsyncMock(return_value={"ok": False, "results": []})) as search, \
                patch.object(b, "call_openclaw", new=AsyncMock(return_value="[[不查]]")) as model, \
                patch.object(b, "_world_note"), patch.object(b, "_curiosity_learn"), \
                patch.object(b, "send_private_msg", new=AsyncMock()) as send:
            await b._lookup_then_reply("private-user-1", "查询好感度宝宝", None,
                                       "1", "主人", "search", {},
                                       said={"text": "99分", "delivered": True})
        search.assert_not_awaited()
        model.assert_not_awaited()
        send.assert_not_awaited()


class SocialQuestionGuardTests(unittest.TestCase):
    def test_social_questions_are_recognised(self):
        for t in SOCIAL:
            self.assertTrue(b._is_social_question(t), t)

    def test_factual_questions_are_not_social(self):
        for t in FACTUAL:
            self.assertFalse(b._is_social_question(t), t)

    def test_explicit_requests_with_social_words_are_not_social(self):
        # 「帮我写个晚安故事」是办事，不是寒暄；点名要查的更是。
        for t in ("帮我写个晚安故事", "帮我查查明日方舟Mon3tr是什么",
                  "你去搜一下滔博夺冠了吗", "帮我写段睡前故事"):
            self.assertFalse(b._is_social_question(t), t)


class SocialDoesNotSearchTests(unittest.TestCase):
    def test_social_questions_do_not_trigger_web_search(self):
        for t in SOCIAL:
            self.assertFalse(b._should_web_search(t), t)

    def test_factual_questions_still_trigger_web_search(self):
        for t in FACTUAL:
            self.assertTrue(b._should_web_search(t), t)

    def test_existing_social_guards_still_hold(self):
        self.assertFalse(b._should_web_search("诶嘿"))
        self.assertFalse(b._should_web_search("我是谁"))
        for t in ("为什么你们都单抽六星", "怎么又是我加班，烦死了"):
            self.assertFalse(b._should_web_search(t), t)


class SocialIsNotOwedTests(unittest.TestCase):
    def test_social_questions_are_not_owed_deliveries(self):
        for t in SOCIAL:
            self.assertFalse(b.is_owed_request(t), t)

    def test_factual_questions_are_still_owed(self):
        for t in ("滔博夺冠了吗", "今天天气怎么样", "帮我查查明日方舟Mon3tr是什么"):
            self.assertTrue(b.is_owed_request(t), t)

    def test_explicit_writing_request_with_social_word_is_still_owed(self):
        self.assertTrue(b.is_owed_request("帮我写个晚安故事"))

    def test_chatter_is_still_not_owed(self):
        for t in ("甜甜甜", "太高冷了我聊不起来呜呜", "哦", "[图片]", "呜呜呜呜呜"):
            self.assertFalse(b.is_owed_request(t), t)


class ReportFalsePositiveSearchTests(unittest.TestCase):
    """生产现场两条被劫持成搜索的闲聊。

    - 「不用啦，明天给你换个搜索方式」里的「搜索」是名词，被
      `_EXPLICIT_LOOKUP` 的「你…搜」误判成点名查询。
    - 「宝宝你现在对我最早的记忆是什么」被 `_needs_fresh_info` 的
      「现在 + 什么」判成时效问题；问的是她自己的记忆，不该联网。
    两条都被送去 waiting:search → 「等一下喵~」→「没查到」，
    正常的接话被换成了系统失败交代。
    """

    def test_search_noun_is_not_a_lookup_request(self):
        for t in ("不用啦，明天给你换个搜索方式", "搜索功能又坏了",
                  "你那个搜索方式不对", "给我换个搜索方式吧"):
            with self.subTest(text=t):
                self.assertFalse(b._explicit_lookup(t), t)
                self.assertFalse(b._should_web_search(t), t)

    def test_self_memory_question_does_not_search(self):
        for t in ("宝宝你现在对我最早的记忆是什么", "你现在记得我叫什么吗",
                  "你对我最早的记忆还有吗", "你还记得我们第一次见面吗"):
            with self.subTest(text=t):
                self.assertFalse(b._should_web_search(t), t)

    def test_real_lookup_requests_and_facts_still_search(self):
        for t in ("帮我搜索一下明日方舟攻略", "搜索心理学中的好感度理论",
                  "你去搜一下滔博夺冠了吗", "帮我查查重庆天气"):
            with self.subTest(text=t):
                self.assertTrue(b._should_web_search(t), t)
                self.assertTrue(b._explicit_lookup(t) or b._needs_fresh_info(t), t)


class FailedSocialSearchStaysSilentTests(unittest.IsolatedAsyncioTestCase):
    """就算一条社交问句不知怎么进了后台搜索，查不到也必须闭嘴。

    以前 owed=True 逼模型写「没办成」的整段交代；现在社交问句不是 owed，
    查不到（hint 为空）就直接 return，一个字都不发。
    """

    def setUp(self):
        self.key = "private-user-1000000001"
        for d in (b._OWED_DELIVERED, b._TURN_VERSIONS):
            self.addCleanup(d.clear)
        b._OWED_DELIVERED.clear()
        b._TURN_VERSIONS.pop(self.key, None)

    async def test_failed_search_on_social_question_sends_nothing(self):
        sent = []

        async def fake_private(ws, uid, body, **kw):
            sent.append(body)
            return True

        gen = AsyncMock(return_value="主人，我没办成，翻到的东西对不上今天那句。")
        said = {"text": "等我查查", "delivered": True, "version": 3}
        import web_search
        with patch.dict(b._TURN_VERSIONS, {self.key: 3}, clear=False), \
                patch.object(b, "_call_name", return_value="主人"), \
                patch.object(b, "_tool_plan", return_value={"cloud": False}), \
                patch.object(b, "search_query_or_fallback", new=AsyncMock(return_value="开心")), \
                patch.object(b, "_search_plan", new=AsyncMock(return_value={"query": "开心", "entity": "开心"})), \
                patch.object(web_search, "verified_search", new=AsyncMock(return_value={"ok": False, "results": [], "reason": "no_answer"})), \
                patch.object(b, "get_system_prompt", lambda: "人设"), \
                patch.object(b, "call_openclaw", new=gen), \
                patch.object(b, "send_private_msg", new=fake_private), \
                patch.object(b, "_BRIDGE_WS", {"ws": object()}), \
                patch.object(b, "_world_note", lambda *a, **k: None), \
                patch.object(b, "_curiosity_learn", lambda *a, **k: None):
            await b._lookup_then_reply(self.key, "今天开心吗", None, "1000000001",
                                       "阿舟", "search", {}, said=said)
        self.assertEqual(sent, [], "社交问句查不到，不该甩一篇失败交代")
        gen.assert_not_called()


if __name__ == "__main__":
    unittest.main()