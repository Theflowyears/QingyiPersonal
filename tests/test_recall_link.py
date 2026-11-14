"""回归锁：检索链路（身份层稳定注入 / 跨库去重 / 使用反馈 / 上下文查询）。

这批要修的是仪表第一次跑就抓出来的结构性问题：
  · 真实消息 1306 条里 94.9% 落在 memory_depth 的「浅层」，
    而浅层不带任何人格档案 —— 她的 likes/facts/self_view 绝大多数轮次根本没进提示词；
  · 查询就是那一句 8.8 字的消息，平均只有 2.21 个实词、8.8% 一个都没有，
    「它/那个」这类指代进分词后直接丢，检索必然空；
  · 检索空了以后各库的兜底是「按 importance 塞旧的」或「取末尾 N 条」，
    于是她拿到一串不相关的旧条目，只能硬编 —— 提示词里再写一句「别报菜名」压症状；
  · 四个库之间零去重，同一条事实在 group_facts / persona / world_knowledge /
    member 各占一份预算。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

BOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)


class IdentityLayerTests(unittest.TestCase):
    """身份层必须每天稳定，才能名正言顺放进 system[0] 吃前缀缓存。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ident_")
        self._old = {k: os.environ.get(k) for k in ("DATA_DIR", "PERSONA_DATA_DIR")}
        os.environ["DATA_DIR"] = self.dir
        os.environ["PERSONA_DATA_DIR"] = self.dir
        # Paths resolve at call time. Reimporting creates detached connection
        # owners and lets cached consumers read a different module instance.
        self.ps = __import__("persona_store")
        self.pg = __import__("persona_growth")
        self.md = __import__("memory_depth")

    def tearDown(self):
        if self.ps._conn is not None:
            self.ps._conn.close()
            self.ps._conn = None
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir)

    def test_identity_block_carries_the_archive(self):
        self.ps.add("facts", "主人家在重庆")
        self.ps.add("likes", "被摸头")
        self.ps.add("experiences", "陪主人熬到两点")
        d = self.pg.load()
        d["self_view"] = "觉得自己记性差但很会等人"
        self.pg.save(d)
        block = self.md.identity_block()
        self.assertIn("被摸头", block)
        self.assertIn("重庆", block)
        self.assertIn("很会等人", block)

    def test_same_day_same_bytes_even_if_archive_changed(self):
        # 这是能不能进 system[0] 的唯一前提：当天字节稳定
        self.ps.add("likes", "吃西瓜")
        a = self.md.identity_block()
        self.ps.add("likes", "夜里陪聊")
        self.ps.add("facts", "主人做游戏策划")
        b = self.md.identity_block()
        self.assertEqual(a, b, "当天内容变了 → 前缀缓存立刻失效")

    def test_next_day_snapshot_refreshes(self):
        self.ps.add("likes", "吃西瓜")
        a = self.md.identity_block()
        # 隔天且档案确实变了：只换日期不改内容，刷新与不刷新结果相同，测不出来
        self.ps.add("likes", "被摸头")
        # 把快照日期拨到昨天，应当重算。
        # 路径一定要问模块自己要：_snap_path 用的是 PERSONA_DATA_DIR 根目录，
        # 手写「目录/persona/...」会静默找不到文件，测试假失败。
        snap = self.md._snap_path()
        self.assertTrue(os.path.isfile(snap), "identity_block 没落快照: %s" % snap)
        if os.path.isfile(snap):
            with open(snap, encoding="utf-8") as f:
                data = json.load(f)
            data["day"] = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
            with open(snap, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            self.md._IDENT_SNAP["day"] = None
        b = self.md.identity_block()
        self.assertNotEqual(a, b, "隔天了还端着昨天的快照")
        self.assertIn("被摸头", b, b)
        # 同一天内再怎么变都不该动（否则前缀缓存天天失效）
        self.ps.add("likes", "夜里陪聊")
        self.assertEqual(b, self.md.identity_block(), "当天字节不稳定")

    def test_shallow_query_still_gets_nothing_from_identity_by_design(self):
        # 浅层不带档案是对的（档案已经进 system 了），这里锁的是「别两头都不给」
        self.ps.add("likes", "被摸头")
        self.assertTrue(self.md.identity_block())

    def test_identity_is_bounded(self):
        for i in range(60):
            self.ps.add("facts", "事实编号%02d，内容足够长一点才不会被截掉" % i)
        self.assertLessEqual(len(self.md.identity_block()),
                             self.md.IDENTITY_MAX_CHARS + 40)


class RecallQueryTests(unittest.TestCase):
    """查询要带上下文，否则「它/那个」这种句子永远查不到东西。"""

    def test_pronoun_resolved_from_previous_turn(self):
        import qq_openclaw_bridge as br
        hist = [("user", "赤合金到底值不值得买"),
                ("assistant", "贵得要死，少用点。")]
        q = br.build_recall_query(hist, "它有什么用")
        self.assertIn("赤合金", q)

    def test_recent_turns_included_even_without_pronoun(self):
        import qq_openclaw_bridge as br
        hist = [("user", "明日方舟的集成战略怎么玩"), ("assistant", "先抓招募券。")]
        q = br.build_recall_query(hist, "藏品呢")
        self.assertIn("藏品", q)
        self.assertIn("集成战略", q)

    def test_no_history_just_returns_the_message(self):
        import qq_openclaw_bridge as br
        self.assertEqual(br.build_recall_query([], "今天好累"), "今天好累")

    def test_bounded_length(self):
        import qq_openclaw_bridge as br
        hist = [("user", "很长" * 40), ("assistant", "再长" * 40)] * 4
        self.assertLessEqual(len(br.build_recall_query(hist, "那个呢")), 160)


class DedupeTests(unittest.TestCase):
    """同一条事实别在四个库里各占一份预算。"""

    def test_duplicate_across_stores_keeps_highest_priority(self):
        import recall_link as rl
        cands = [
            {"store": "group_facts", "text": "主人家在重庆，读大三"},
            {"store": "world_knowledge", "text": "主人家在重庆"},
            {"store": "persona:facts", "text": "主人家在重庆读书"},
        ]
        kept = rl.dedupe(cands)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["store"], "world_knowledge")

    def test_priority_order_is_stable(self):
        import recall_link as rl
        self.assertLess(rl.PRIORITY.index("world_knowledge"),
                        rl.PRIORITY.index("persona:facts"))
        self.assertLess(rl.PRIORITY.index("member"),
                        rl.PRIORITY.index("group_facts"))

    def test_different_facts_not_merged(self):
        import recall_link as rl
        cands = [{"store": "group_facts", "text": "她怕黑"},
                 {"store": "persona:facts", "text": "主人家在重庆"}]
        self.assertEqual(len(rl.dedupe(cands)), 2)

    def test_empty_input_safe(self):
        import recall_link as rl
        self.assertEqual(rl.dedupe([]), [])
        self.assertEqual(rl.dedupe(None), [])


class RankingTests(unittest.TestCase):
    """打分要吃「用过没有」和「多久没用」，不能只吃静态重要度。"""

    def test_used_entry_outranks_never_used_at_equal_relevance(self):
        import recall_link as rl
        now = time.time()
        a = {"ts": now - 86400 * 40, "relevance": 5.0, "importance": 2.0,
             "used": 6, "last_used": now - 86400}
        b = {"ts": now - 86400 * 40, "relevance": 5.0, "importance": 2.0,
             "used": 0, "last_used": 0}
        self.assertGreater(rl.final_score(a), rl.final_score(b))

    def test_recency_decays_exponentially_not_linearly(self):
        import recall_link as rl
        now = time.time()
        base = {"relevance": 5.0, "importance": 2.0, "used": 1, "last_used": now}
        fresh = dict(base, ts=now)
        old = dict(base, ts=now - 86400 * 120)
        self.assertGreater(rl.final_score(fresh), rl.final_score(old))
        # 但衰减不该把时间压成唯一因素：新鲜但无关的，不该赢过相关但旧一点的
        rel_old = dict(base, relevance=20.0, ts=now - 86400 * 60)
        irr_new = dict(base, relevance=0.2, ts=now)
        self.assertGreater(rl.final_score(rel_old), rl.final_score(irr_new))

    def test_missing_fields_do_not_crash(self):
        import recall_link as rl
        self.assertIsInstance(rl.final_score({"relevance": 1.0}), float)
        self.assertIsInstance(rl.final_score({}), float)


class FallbackTests(unittest.TestCase):
    """查不到就别硬给：宁可少注入，也不要拿旧条目凑数。"""

    def test_zero_content_query_is_flagged_not_searchable(self):
        import recall_link as rl
        self.assertFalse(rl.query_searchable("笑死我了"))
        self.assertTrue(rl.query_searchable("赤合金到底有什么用"))

    def test_degraded_context_marks_no_memory(self):
        import recall_link as rl
        out = rl.render([], budget=600)
        self.assertIn(rl.NO_MEMORY_MARK, out)
        self.assertLess(len(out), 200)

    def test_budget_is_honored(self):
        import recall_link as rl
        cands = [{"store": "group_facts", "text": "条目" + "很长" * 30} for _ in range(9)]
        out = rl.render(cands, budget=300)
        self.assertLessEqual(len(out), 340)


if __name__ == "__main__":
    unittest.main()
