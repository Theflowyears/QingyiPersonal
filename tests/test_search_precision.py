"""回归锁：联网查询值不值得说出口，必须在发出去之前判一次。

起因（2026-11-06 群里两起真实事故）：

事故一 20:02  群友「你cos谁」
    清漪「我就cos我自己呀，蓝发女仆装那套」 ← 这句已经答完了
    清漪「刚翻了下，这问题问得我一愣。」    ← 多余
    清漪「查了半天全是屠洪刚的《你》，跟cos没半毛钱关系。」

事故二 20:07  群友「这杀熟的神游就不能少用点赤合金吗」
    清漪「赤合金那是真贵啊」               ← 已经是完整回应
    清漪「刚翻了下，这字确实是七画。」      ← 把搜索中间态发到群里
    清漪「但赤合金那是游戏里的材料，跟笔画没关系啊，笨蛋。」

两道门各管一类：
  1) `query_anchor` —— 去掉代词与疑问框架后什么都不剩的句子根本不联网（纯问人）；
  2) `relevance` —— 搜回来的东西没提到问题里的实词，就一条都不补。
两起事故靠的第 2 道：结果里既没有 cos 也没有 赤合金/神游/杀熟。
"""
import os
import sys
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import web_search  # noqa: E402


# 事故一里真实拿到的那种结果：字面撞上「你」，内容跟 cos 无关
SONG_RESULTS = [
    {"title": "你（屠洪刚演唱歌曲）", "snippet": "《你》是屠洪刚演唱的歌曲，由易茗作词。", "url": "x"},
    {"title": "屠洪刚全部歌曲", "snippet": "屠洪刚，中国内地男歌手，代表作《你》《霸王别姬》。", "url": "y"},
]

NOBEL_RESULTS = [
    {"title": "2026年诺贝尔文学奖揭晓", "snippet": "瑞典文学院宣布，将2026年诺贝尔文学奖授予加拿大诗人安妮·卡森。", "url": "x"},
    {"title": "安妮·卡森是谁", "snippet": "安妮·卡森以跨界文体写作闻名，本次表彰她大胆而富有创造力的创作。", "url": "y"},
]

BUILD_RESULTS = [
    {"title": "这个英雄怎么出装", "snippet": "出装顺序：先出破甲，再补攻击速度，后期换成吸血刀。", "url": "x"},
]

STROKE_RESULTS = [
    {"title": "赤_百度汉语", "snippet": "赤的笔画顺序是横、竖、横、撇、点、撇、点，共七画。", "url": "x"},
    {"title": "赤字的意思", "snippet": "赤，部首赤，七画，本义为红色。", "url": "y"},
]


class AnchorTests(unittest.TestCase):
    """第一道门：这句里到底有没有「要往场外查的东西」。"""

    def test_bare_pronoun_ask_has_no_anchor(self):
        self.assertEqual(web_search.query_anchor("你呢"), "")

    def test_first_person_opinion_has_no_anchor(self):
        self.assertEqual(web_search.query_anchor("你觉得我怎么样"), "")

    def test_asking_her_outfit_has_no_anchor(self):
        self.assertEqual(web_search.query_anchor("你打算穿什么"), "")

    def test_cos_ask_leaves_only_the_verb(self):
        # 「你cos谁」去掉代词疑问词只剩 cos：词法层面判不出该不该查，
        # 这条锁的是「锚不会把代词当成实体」，真正拦住它的是 relevance。
        self.assertEqual(web_search.query_anchor("你cos谁"), "cos")

    def test_real_world_fact_keeps_anchor(self):
        self.assertIn("诺贝尔", web_search.query_anchor("2026年诺贝尔文学奖得主是谁"))

    def test_game_term_keeps_anchor(self):
        self.assertIn("出装", web_search.query_anchor("这个英雄怎么出装"))


class SearchableTests(unittest.TestCase):
    """第三道门：这一句到底值不值得为它跑一次联网。

    「你cos谁」「能不能成团」「你觉得我怎么样」的答案在对话里、在她自己身上，
    网上没有。事故里就是这句 `return _is_question(t)` 兜底把每个疑问句都送去了 Bing。
    """

    def test_ask_about_her_is_not_searchable(self):
        self.assertFalse(web_search.looks_searchable("你cos谁"))

    def test_opinion_ask_is_not_searchable(self):
        self.assertFalse(web_search.looks_searchable("你觉得我怎么样"))

    def test_feeling_about_situation_is_not_searchable(self):
        # 「新家感觉怎么样」问的是她自己的感受，不是世界事实。
        # 感觉/体验这类心理动词必须和 觉得/知道 一样被剥掉，否则「新家+感觉」
        # 凑成两个锚就被当成可查实体，她才会回一句「没问到准信儿，我去问问」。
        self.assertFalse(web_search.looks_searchable("新家感觉怎么样"))
        self.assertEqual(web_search.query_anchor("新家感觉怎么样"), "新家")

    def test_named_fact_is_searchable(self):
        self.assertTrue(web_search.looks_searchable("2026年诺贝尔文学奖得主是谁"))

    def test_game_term_is_searchable(self):
        self.assertTrue(web_search.looks_searchable("这个英雄怎么出装"))


class BochaShapeTests(unittest.TestCase):
    """主源换成博查以后，结果形状必须还是那一套。"""

    RESP = {"code": 200, "data": {"webPages": {"value": [
        {"name": "特斯拉股价上涨3.5% 创7月中旬以来新高",
         "url": "https://x/1", "snippet": "11月2日 snippet",
         "summary": "每经AI快讯，11月2日，特斯拉股价上涨3.5%，报413.925美元/股。",
         "siteName": "网易新闻客户端", "datePublished": "2026-11-03T00:01:00Z"},
        {"name": "缺字段的脏条目", "url": "", "snippet": "没 url 就该丢掉"},
    ]}}}

    def test_normalizes_to_the_common_shape(self):
        items = web_search._norm_bocha(self.RESP, 5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "特斯拉股价上涨3.5% 创7月中旬以来新高")
        self.assertEqual(items[0]["site"], "网易新闻客户端")
        self.assertEqual(items[0]["date"], "2026-11-03")

    def test_prefers_summary_over_snippet(self):
        items = web_search._norm_bocha(self.RESP, 5)
        self.assertIn("413.925", items[0]["snippet"])

    def test_empty_response_yields_no_items(self):
        self.assertEqual(web_search._norm_bocha({}, 5), [])
        self.assertEqual(web_search._norm_bocha(None, 5), [])

    def test_block_shows_site_and_date(self):
        items = web_search._norm_bocha(self.RESP, 5)
        block = web_search.build_search_block(items)
        self.assertIn("2026-11-03", block)
        self.assertIn("网易新闻客户端", block)

    def test_relevance_matches_bocha_answer_about_the_price(self):
        items = web_search._norm_bocha(self.RESP, 5)
        self.assertTrue(web_search.relevance("特斯拉股价现在多少", items))
        # 同一批结果回答不了「这游戏真坑」
        self.assertFalse(web_search.relevance("这游戏真坑", items))


class QueryTests(unittest.TestCase):
    """查询词本身不能是虚词。

    实测（2026-11-06 云端抓 Bing）：拿整句话去查，Bing 会撞上句首的虚词，
    「这个英雄怎么出装」查回来的是「这个_百度百科」，
    「这杀熟的神游就不能少用点赤合金吗」查回来的是「这」的笔画 ——
    于是她在群里说出了「这字确实是七画」。
    """

    def test_query_never_leads_with_demonstrative(self):
        q = web_search.build_query("这个英雄怎么出装")
        self.assertNotIn("这个", q)
        self.assertIn("出装", q)

    def test_query_drops_pronoun_frame(self):
        q = web_search.build_query("这杀熟的神游就不能少用点赤合金吗")
        self.assertNotIn("这", q.split(" ")[0])
        self.assertIn("赤合金", q)

    def test_explicit_keywords_pass_through(self):
        # 她自己用 [[查: 诺贝尔文学奖]] 挑的词，不能被再削一遍
        self.assertIn("诺贝尔文学奖", web_search.build_query("诺贝尔文学奖"))

    def test_time_word_still_becomes_a_date(self):
        q = web_search.build_query("今天重庆天气怎么样")
        self.assertIn("重庆", q)
        self.assertRegex(q, r"\d{4}-\d{2}-\d{2}")


class RelevanceTests(unittest.TestCase):
    """第二道门：搜回来的东西是不是真在回答这个问题。"""

    def test_song_results_are_not_about_cosplay(self):
        self.assertFalse(web_search.relevance("你cos谁", SONG_RESULTS))

    def test_stroke_results_are_not_about_game_material(self):
        self.assertFalse(web_search.relevance("这杀熟的神游就不能少用点赤合金吗", STROKE_RESULTS))

    def test_generic_praise_has_nothing_to_match(self):
        self.assertFalse(web_search.relevance("赤合金那是真贵啊", STROKE_RESULTS))

    def test_nobel_results_answer_the_question(self):
        self.assertTrue(web_search.relevance("2026年诺贝尔文学奖得主是谁", NOBEL_RESULTS))

    def test_build_results_answer_the_question(self):
        self.assertTrue(web_search.relevance("这个英雄怎么出装", BUILD_RESULTS))

    def test_empty_input_is_not_relevant(self):
        self.assertFalse(web_search.relevance("明天北京限号吗", []))
        self.assertFalse(web_search.relevance("", NOBEL_RESULTS))


if __name__ == "__main__":
    unittest.main()
