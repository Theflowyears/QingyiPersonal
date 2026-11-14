"""回归锁：她查证到的东西到底进不进得了库。

这条锁的起因是取证 + 实测发现的两件事：
  1) `data/memory/world_knowledge.json` 线上是空的（`{"topics":[]}`），
     三个写入口都是通的，是门禁把合理内容挡死了；
  2) 具体被挡的原因很机械 —— `len(summary) < 80` 的长度硬门槛，
     以及 `looks_relevant` 用「整串 CJK 当一个词」，
     导致查询「荒坂塔原型」对正文里的「荒坂塔」判成毫不相干。

另外按已定的口径：时效性内容（天气、股价）**归档而不是丢弃**，
默认不注入，但仍在库里可以被显式查到。
"""
import json
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

import knowledge_base as kb        # noqa: E402


ARASAKA_SUMMARY = ("荒坂塔是游戏里荒坂公司的总部，立在夜之城中心；"
                   "玩家常吐槽它既是剧情里的权力象征又是任务列表里的靶子。")


class KbBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kbw_")
        self._old_file = kb._FILE
        kb._FILE = os.path.join(self.dir, "memory", "world_knowledge.json")
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})

    def tearDown(self):
        kb._FILE = self._old_file
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})
        shutil.rmtree(self.dir, ignore_errors=True)

    def raw(self):
        # 她一条都没记过时文件压根不存在 —— 那是正确行为，不是异常
        if not os.path.isfile(kb._FILE):
            return {"topics": []}
        with open(kb._FILE, encoding="utf-8") as f:
            return json.load(f)


class GateTests(KbBase):
    def test_real_knowledge_shorter_than_80_chars_is_accepted(self):
        # 43 字的一条，旧实现因为「<80 字」直接丢掉
        short = "荒坂塔是荒坂公司总部，在夜之城中心，剧情里的权力象征。"
        self.assertLess(len(short), 80)
        self.assertTrue(kb.remember("荒坂塔", "赛博朋克2077 荒坂塔", short))

    def test_substring_query_counts_as_relevant(self):
        # 「荒坂塔原型」整串不在正文里，但「荒坂塔」在 —— 二元组口径下该算相关
        self.assertTrue(kb.looks_relevant("荒坂塔原型", "赛博朋克2077 荒坂塔",
                                          ARASAKA_SUMMARY))

    def test_unrelated_result_still_rejected(self):
        self.assertFalse(kb.looks_relevant("赤合金多少钱", "明日方舟 - Arknights",
                                           "《明日方舟》是一款魔物主题的策略手游。"))

    def test_dictionary_junk_rejected(self):
        self.assertFalse(kb.remember(
            "这", "这（汉语文字）_百度百科",
            "这，汉语一级字，拼音为zhè或zhèi（口语），部首为辶，总笔画7画，"
            "繁体写作這，五笔编码YPI。其本义为迎，后演变为指示代词，"
            "用来指称比较近的人或事物，与那相对。"))

    def test_transient_goes_to_archive_not_thrown_away(self):
        # 天气会过期，但按定下的口径是「归档不删」
        ok = kb.remember("重庆天气", "重庆今日天气",
                         "今天重庆多云，气温16到22度，降雨概率两成，风力三级左右，"
                         "空气质量轻度污染，建议出门戴口罩。",
                         category="时事")
        self.assertTrue(ok)
        items = self.raw().get("topics") or []
        self.assertEqual(items[0].get("status"), "archive", items)

    def test_archive_is_not_injected(self):
        kb.remember("重庆天气", "重庆今日天气",
                    "今天重庆多云，气温16到22度，降雨概率两成，风力三级左右，空气质量差。")
        self.assertNotIn("重庆", kb.build_block(query="重庆天气怎么样"))

    def test_archive_still_retrievable_explicitly(self):
        kb.remember("重庆天气", "重庆今日天气",
                    "今天重庆多云，气温16到22度，降雨概率两成，风力三级左右。")
        got = kb.get("重庆天气")
        self.assertIsNotNone(got)
        self.assertEqual(got.get("status"), "archive")


class InjectionTruthTests(KbBase):
    def test_unrelated_active_entries_are_not_injected(self):
        kb.remember("荒坂塔","荒坂塔",ARASAKA_SUMMARY,category="作品设定")
        self.assertEqual(kb.build_block(query="今天晚饭吃什么"),"")

    def test_unverified_entry_is_not_labelled_as_checked(self):
        kb.remember("泰拉世界","泰拉","泰拉是明日方舟的架空世界，居民具有动物特征，文明依靠移动城邦躲避天灾。",category="作品设定",verified=False)
        block=kb.build_block(query="明日方舟泰拉是什么")
        self.assertIn("未核验",block)
        self.assertNotIn("这些是你查证过的",block)

    def test_data_dir_change_is_resolved_at_call_time(self):
        with tempfile.TemporaryDirectory() as other, mock.patch.object(kb,"_FILE",kb._DEFAULT_FILE), \
                mock.patch.dict(os.environ,{"DATA_DIR":other}):
            self.assertEqual(kb._path(),os.path.join(other,"memory","world_knowledge.json"))


class SelectedTruthTests(KbBase):
    def test_mixed_sources_do_not_label_unverified_row_as_verified(self):
        kb.remember('已核验条目','已核验条目',ARASAKA_SUMMARY,category='作品设定',verified=True)
        kb.remember('未核验条目','未核验条目','未核验条目是另一段没有完成核验的游戏设定资料，包含更多查询关键词。',category='作品设定',verified=False)
        block=kb.build_block(query='已核验条目 未核验条目 资料 关键词 设定 游戏')
        self.assertIn('含未核验条目',block)


class StructuredTests(KbBase):
    def test_entry_carries_category_provenance_and_status(self):
        kb.remember("荒坂塔", "赛博朋克2077 荒坂塔", ARASAKA_SUMMARY,
                    source="bocha", category="作品设定")
        it = self.raw()["topics"][0]
        self.assertEqual(it.get("category"), "作品设定")
        self.assertEqual(it.get("status"), "active")
        self.assertEqual(it.get("provenance"), "bocha")

    def test_legacy_file_still_loads_with_defaults(self):
        os.makedirs(os.path.dirname(kb._FILE), exist_ok=True)
        legacy = {"topics": [{"key": "主人的爱好", "title": "旧条目",
                              "summary": "她记得主人喜欢深夜一起打肉鸽。",
                              "ts": 1700000000.0, "source": "bing"}]}
        with open(kb._FILE, "w", encoding="utf-8") as f:
            json.dump(legacy, f, ensure_ascii=False)
        kb._CACHE.update({"sig": None, "items": [], "tokens": {}})
        items = kb.load()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "其他")
        self.assertEqual(items[0]["status"], "active")
        self.assertIn("主人", kb.build_block(query="主人喜欢什么"))

    def test_default_category_when_caller_does_not_say(self):
        kb.remember("荒坂塔", "赛博朋克2077 荒坂塔", ARASAKA_SUMMARY)
        self.assertTrue(self.raw()["topics"][0].get("category"))


class QuotaTests(KbBase):
    def test_category_quota_evicts_within_category_not_globally(self):
        cap = int(os.environ.get("KB_QUOTA_作品设定") or kb.DEFAULT_QUOTAS["作品设定"])
        for i in range(cap + 3):
            kb.remember("设定%d" % i, "荒坂塔设定 %d" % i,
                        "荒坂塔是荒坂公司总部，夜之城中心，剧情里的权力象征，编号 %d。" % i,
                        category="作品设定")
        items = self.raw()["topics"]
        same = [x for x in items if x["category"] == "作品设定"
                and x.get("status") == "active"]
        self.assertLessEqual(len(same), cap, same)
        # 最新的那几条要留着（淘汰旧的，不是淘汰新的）
        self.assertIn("设定%d" % (cap + 2), [x["key"] for x in same])

    def test_other_category_not_starved_by_one_big_category(self):
        # 以前 MAX_TOPICS=30 是全局的：一类灌满 30 条，别类就永远进不来
        cap = int(kb.DEFAULT_QUOTAS["作品设定"])
        for i in range(cap + 2):
            kb.remember("设定%d" % i, "荒坂塔 %d" % i,
                        "荒坂塔是荒坂公司总部，夜之城中心，剧情里的权力象征。",
                        category="作品设定")
        self.assertTrue(kb.remember("主人生日", "主人的生日",
                                    "她记得主人的生日在夏天，具体日子群友提过两次。",
                                    category="人物"))
        keys = [x["key"] for x in self.raw()["topics"]]
        self.assertIn("主人生日", keys)

    def test_total_stays_bounded(self):
        total = sum(kb.DEFAULT_QUOTAS.values())
        cats = list(kb.DEFAULT_QUOTAS)
        for i in range(total + 12):
            kb.remember("条%d" % i, "标题 %d" % i,
                        "荒坂塔是荒坂公司总部，夜之城中心，剧情里的权力象征。",
                        category=cats[i % len(cats)])
        active = [x for x in self.raw()["topics"] if x.get("status") == "active"]
        self.assertLessEqual(len(active), total)


class JudgeTests(KbBase):
    """值不值得记，交给她判一次 —— 但判不了也不能崩。"""

    def test_judge_can_decline(self):
        async def llm(p):
            return "[[不记]]"
        r = _run(kb.judge_and_remember("荒坂塔", "赛博朋克2077 荒坂塔",
                                       ARASAKA_SUMMARY, llm=llm))
        self.assertFalse(r.get("saved"), r)
        self.assertEqual(self.raw().get("topics"), [])

    def test_judge_category_is_honored(self):
        async def llm(p):
            return json.dumps({"keep": True, "category": "作品设定",
                               "fact": "荒坂塔是荒坂公司总部"}, ensure_ascii=False)
        r = _run(kb.judge_and_remember("荒坂塔", "赛博朋克2077 荒坂塔",
                                       ARASAKA_SUMMARY, llm=llm))
        self.assertTrue(r.get("saved"), r)
        self.assertEqual(self.raw()["topics"][0]["category"], "作品设定")

    def test_broken_judge_output_falls_back_instead_of_crashing(self):
        async def llm(p):
            return "我不知道该怎么说这段东西，反正挺长的"
        r = _run(kb.judge_and_remember("荒坂塔", "赛博朋克2077 荒坂塔",
                                       ARASAKA_SUMMARY, llm=llm))
        self.assertTrue(r.get("saved"), r)
        self.assertTrue(self.raw()["topics"][0].get("category"))

    def test_no_llm_passed_still_writes(self):
        # 调用方没给判定函数时不能因为判不了就不记 —— 静默失败最坑
        r = _run(kb.judge_and_remember("荒坂塔", "赛博朋克2077 荒坂塔", ARASAKA_SUMMARY))
        self.assertTrue(r.get("saved"), r)


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class NearDupTests(KbBase):
    """同一个问题换个说法再问一次，不该在库里多占一格。

    现网真实形状（拉到云端数据看过的）：「游戏《致命公司》(Lethal Company)…」
    和「游戏《致命公司》（Lethal Company）…」因为括号全半角不同各存了一条，
    加上第三条变体一共三条，来源还是同一个硬件检测网站。
    配额被重复条目吃掉，是真后果 —— 不是难看而已。
    """
    L1 = "游戏《致命公司》(Lethal Company)怎么玩"
    L2 = "游戏《致命公司》（Lethal Company）玩法"
    S = ("致命公司是一张地图上的合作恐怖游戏，玩家进设施里捡东西、"
         "按重量计钱，天亮前把货交上去才算过关。")

    def test_punctuation_variant_merges_instead_of_stacking(self):
        self.assertTrue(kb.remember(self.L1, self.L1, self.S, source="bocha"))
        self.assertTrue(kb.remember(self.L2, self.L2, self.S, source="bocha"))
        topics = self.raw()["topics"]
        self.assertEqual(len(topics), 1, [t["key"] for t in topics])
        self.assertEqual(topics[0]["key"], self.L2, "留下的是最新那条说法")

    def test_merge_keeps_first_seen_and_records_what_it_absorbed(self):
        kb.remember(self.L1, self.L1, self.S, source="bocha")
        kb.remember(self.L2, self.L2, self.S, source="bocha")
        t = self.raw()["topics"][0]
        self.assertEqual(t.get("merged_from"), self.L1, t)
        self.assertGreater(float(t.get("first_seen") or 0), 0)

    def test_different_questions_are_not_merged(self):
        # 宁可漏合不可错合：合错了是把两条不同知识变成一条，比占格严重得多
        a = "赤合金在明日方舟里怎么获得"
        bb = "明日方舟的干员等级上限是多少"
        sa = ("赤合金是集成战略里的贵重品材料，主要在藏品奖励和商店里换，"
              "掉率不高所以大家都嫌贵。")
        sb = ("干员等级上限跟着精英化阶段走，满级是精二之后的九十级，"
              "再往上要靠模组和专精另算。")
        self.assertTrue(kb.remember(a, a, sa, source="bocha"))
        self.assertTrue(kb.remember(bb, bb, sb, source="bocha"))
        self.assertEqual(len(self.raw()["topics"]), 2,
                         "两个不相干的问题被合成一条了")

    def test_single_word_keys_never_merge(self):
        # 只有一个实词的 key 没有词面可比性，拿它去合并等于按第一个字归堆
        self.assertTrue(kb.remember("荒坂塔原型", "荒坂塔原型", self.S, source="b"))
        self.assertTrue(kb.remember("荒坂塔", "荒坂塔", self.S, source="b"))
        self.assertEqual(len(self.raw()["topics"]), 2)

    def test_exact_key_still_overwrites(self):
        kb.remember(self.L1, self.L1, self.S, source="bocha")
        kb.remember(self.L1, self.L1, self.S + "补充一点。", source="bocha")
        self.assertEqual(len(self.raw()["topics"]), 1)


class MergeHistoryTests(KbBase):
    """历史重复条目回填合并：合并＝归档，不是删除。

    现网真实攒了 3 条「致命公司」、3 条「主人」、2 条「赤合金」，
    只修新写入的话，老账永远占着配额并且注入时反复说同一件事。
    """
    S = ("致命公司是一张地图上的合作恐怖游戏，玩家进设施里捡东西、"
         "按重量计钱，天亮前把货交上去才算过关。")
    S2 = ("主人在游戏设定里是一种花，也有人拿来当角色代号；"
          "查的时候要小心词典页和角色页混在一起。")

    def _seed_history(self):
        # 直接落盘，绕过 remember()：因为写入端已经有近似合并了，
        # 走正常入口根本攒不出「历史重复」。这里要复现的是
        # 那些在合并规则上线**之前**就写进去的老条目。
        raw = [
            {"key": u"游戏《致命公司》(Lethal Company)玩法",
             "title": u"致命公司玩法", "summary": self.S,
             "ts": time.time() - 30 * 86400, "source": "bing",
             "category": u"工具做法", "status": "active",
             "first_seen": time.time() - 30 * 86400},
            {"key": u"游戏《致命公司》（Lethal Company）机制",
             "title": u"致命公司机制", "summary": self.S,
             "ts": time.time() - 20 * 86400, "source": u"硬件站",
             "category": u"其他", "status": "active",
             "first_seen": time.time() - 20 * 86400},
            {"key": u"游戏《致命公司》怎么进设施好",
             "title": u"致命公司设施", "summary": self.S,
             "ts": time.time() - 10 * 86400, "source": "bocha",
             "category": u"工具做法", "status": "active",
             "first_seen": time.time() - 10 * 86400},
            {"key": u"主人是什么花", "title": u"主人的花", "summary": self.S2,
             "ts": time.time() - 5 * 86400, "source": "bing",
             "category": u"人物", "status": "active",
             "first_seen": time.time() - 5 * 86400},
        ]
        self.assertTrue(kb.save(raw))
        kb._CACHE.update({"sig": None, "items": None, "tokens": {}})
        return raw

    def test_dry_run_reports_without_writing(self):
        self._seed_history()
        before = self.raw()["topics"]
        res = kb.merge_duplicates(dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertGreaterEqual(res["would_archive"], 1, res)
        self.assertEqual(len(self.raw()["topics"]), len(before),
                         "dry_run 竟然写盘了")

    def test_merge_archives_redundant_and_keeps_one(self):
        self._seed_history()
        res = kb.merge_duplicates(dry_run=False)
        self.assertEqual(res["archived"], 1, res)
        topics = self.raw()["topics"]
        # 一条都不许少：合并是把重复的转成 archive，不是删掉
        self.assertEqual(len(topics), 4, [t["key"] for t in topics])
        lethal = [t for t in topics if "致命公司" in str(t.get("key"))]
        act = [t for t in lethal if str(t.get("status") or "active") == "active"]
        self.assertEqual(len(act), 2, [t.get("key") for t in lethal])
        for t in lethal:
            mi = str(t.get("merged_into") or "")
            if not mi:
                continue
            keeper = [x for x in topics if x.get("key") == mi]
            self.assertTrue(keeper, "%s 指向一个不存在的正本" % t.get("key"))
            self.assertEqual(str(keeper[0].get("status") or "active"), "active")

    def test_lexically_distant_same_topic_is_left_alone(self):
        # 这是有意的取舍：判据是词面 Jaccard，「(Lethal Company)玩法」和
        # 「怎么进设施好」说的是同一件事但词面差太远，宁可漏合也不能错合 ——
        # 错合会把两条真不同的知识变成一条，比多占一格严重得多
        self._seed_history()
        kb.merge_duplicates(dry_run=False)
        keys = [str(t.get("key")) for t in self.raw()["topics"]
                if str(t.get("status") or "active") == "active"]
        self.assertIn(u"游戏《致命公司》怎么进设施好", keys)

    def test_kept_entry_absorbs_earliest_time(self):
        self._seed_history()
        kb.merge_duplicates(dry_run=False)
        topics = self.raw()["topics"]
        keep = [t for t in topics if str(t.get("merged_from") or "").strip()]
        self.assertTrue(keep, "合并完没有任何正本记下自己被并掉了谁")
        k = keep[0]
        self.assertGreater(float(k.get("first_seen") or 0), 0)
        self.assertIn("致命公司", str(k.get("merged_from")), k)
        # 正本的最早记录时间必须往前挪，不能只留最新那次
        self.assertLess(float(k["first_seen"]), time.time() - 25 * 86400, k)

    def test_merge_is_idempotent(self):
        self._seed_history()
        first = kb.merge_duplicates(dry_run=False)
        again = kb.merge_duplicates(dry_run=False)
        self.assertEqual(again.get("archived"), 0, again)
        self.assertEqual(len(self.raw()["topics"]), 4)
        self.assertGreaterEqual(first["archived"], 1)

    def test_archive_only_never_remerged_into_archive(self):
        # 正本必须是还活着的那条，否则合完她在提示词里什么都想不起来
        self._seed_history()
        kb.merge_duplicates(dry_run=False)
        for t in self.raw()["topics"]:
            mi = str(t.get("merged_into") or "")
            if not mi:
                continue
            target = [x for x in self.raw()["topics"] if x.get("key") == mi]
            if target:
                self.assertEqual(str(target[0].get("status") or "active"), "active",
                                 "%s 被并进了一个已归档的正本" % t.get("key"))

    def test_unrelated_topics_are_left_alone(self):
        kb.remember("赤合金怎么获得", "赤合金", "赤合金是集成战略里的贵重品材料，"
                    "主要在藏品奖励和商店里换到。", source="bocha")
        kb.remember("诺贝尔文学奖得主", "2026 诺贝尔文学奖",
                    "瑞典文学院把今年的奖颁给了一位加拿大诗人。", source="bocha")
        res = kb.merge_duplicates(dry_run=False)
        self.assertEqual(res.get("archived"), 0, res)
        self.assertEqual(len(self.raw()["topics"]), 2)


if __name__ == "__main__":
    unittest.main()
