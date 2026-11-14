# -*- coding: utf-8 -*-
"""R03 多线索融合打分初召回：四类线索各自贡献、分解透明、预算生效。

行为锁钉死：实体/人物/时间/词法分别能点亮各自的 score_parts，词法只走
text_tokens（系统唯一分词口径），融合分只是排序线索不是真实性百分比，
retrieval_budget 的 0 是「都不要」不是「没填」。
AST 锁钉死：纯计算真只读（不 open 不写）、import 面只有 math/time/text_tokens、
不在 import 时冻路径、不接进回复链路（§19.5 只观察阶段，与 R01 同纪律）。
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import recall_scoring as rs
import retrieval_objects as ro
import text_tokens as tt

NOW = 4000000000.0


def snapshot(root):
    """目录里有什么（含空目录）。用来证明打一次分没顺手建任何东西。"""
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


class ScoringFixture(unittest.TestCase):
    """纯打分测试：直接构造 R01 形状的统一对象，不需要任何库。"""

    def item(self, object_id="o1", text="一条命题", **over):
        base = {"source_session": "private:1001", "visibility": "private",
                "occurred_at": NOW - 3600.0, "validity": "valid"}
        base.update(over)
        return ro._item(object_id, "belief", "cognition", text, **base)

    def ctx(self, **over):
        base = {"recipient_scope": "private:1001",
                "allowed_source_scopes": ["private:1001"],
                "as_of_time": NOW}
        base.update(over)
        return ro.query_context(**base)

    def parts(self, item, ctx):
        return rs.score_item(item, ctx)["match"]["score_parts"]


class ClueTests(ScoringFixture):
    def test_an_explicit_referent_lights_the_entity_clue(self):
        item = self.item(text="阿甲喜欢喝冰可乐")
        row = rs.score_item(item, self.ctx(recent_referents=["阿甲"]))
        self.assertEqual(rs.WEIGHTS["entity"], row["match"]["score_parts"]["entity"])
        self.assertIn("entity", row["match"]["clues"])

    def test_entity_is_zero_without_a_matching_referent(self):
        item = self.item(text="阿甲喜欢喝冰可乐")
        self.assertEqual(0.0, self.parts(item, self.ctx())["entity"])
        self.assertEqual(0.0, self.parts(item, self.ctx(recent_referents=["提弗洛斯"]))["entity"])

    def test_entity_can_match_an_evidence_quote_or_a_store_tag(self):
        quoted = self.item(text="对方明确自述", evidence_spans=[{"quote": "我喜欢冰可乐"}])
        self.assertEqual(rs.WEIGHTS["entity"],
                         self.parts(quoted, self.ctx(recent_referents=["冰可乐"]))["entity"])
        tagged = self.item(text="一款塔防游戏",
                           match={"clues": ["knowledge", "明日方舟"], "score_parts": {}})
        self.assertEqual(rs.WEIGHTS["entity"],
                         self.parts(tagged, self.ctx(recent_referents=["明日方舟"]))["entity"])

    def test_the_speaker_lights_the_person_clue(self):
        item = self.item(source_session="private:1001")
        row = rs.score_item(item, self.ctx(speaker_person_id="1001"))
        self.assertEqual(rs.WEIGHTS["person"], row["match"]["score_parts"]["person"])
        self.assertIn("person", row["match"]["clues"])

    def test_a_topic_person_lights_the_person_clue_too(self):
        item = self.item(source_session="private:22001")
        self.assertEqual(rs.WEIGHTS["person"],
                         self.parts(item, self.ctx(topic_person_ids=["22001"]))["person"])

    def test_person_is_zero_when_nobody_matches(self):
        item = self.item(source_session="private:1001")
        self.assertEqual(0.0, self.parts(item, self.ctx(speaker_person_id="22001"))["person"])
        # 群号不是人物：group 会话不该和任何 QQ 对上。
        group_item = self.item(source_session="group:771177")
        self.assertEqual(0.0, self.parts(group_item, self.ctx(speaker_person_id="771177"))["person"])

    def test_recent_memories_score_higher_on_time(self):
        recent = self.item("new", occurred_at=NOW - 3600.0)
        old = self.item("old", occurred_at=NOW - 30 * 86400.0)
        ctx = self.ctx()
        self.assertGreater(self.parts(recent, ctx)["time"], self.parts(old, ctx)["time"])
        self.assertGreater(self.parts(old, ctx)["time"], 0.0,
                           "旧记忆的时间分被归零，等于把历史当成不可回顾")
        ranked = rs.recall([old, recent], ctx)["items"]
        self.assertEqual(["new", "old"], [x["object_id"] for x in ranked])

    def test_unknown_occurrence_time_scores_zero(self):
        item = self.item(occurred_at=None, recorded_at=None)
        self.assertEqual(0.0, self.parts(item, self.ctx())["time"],
                         "不知道发生时间被当成「刚刚发生」")

    def test_recorded_at_is_the_fallback_when_occurrence_is_unknown(self):
        item = self.item(occurred_at=None, recorded_at=NOW - 3600.0)
        self.assertGreater(self.parts(item, self.ctx())["time"], 0.0)

    def test_a_time_window_makes_in_window_items_fully_proximate(self):
        old = self.item(occurred_at=NOW - 300 * 86400.0)
        ctx = self.ctx(time_constraints={"not_before": NOW - 400 * 86400.0,
                                         "not_after": NOW})
        self.assertEqual(rs.WEIGHTS["time"], self.parts(old, ctx)["time"],
                         "查询明确要这段时间的事，窗口内就是全邻近，不该再按新旧打折")

    def test_lexical_overlap_goes_through_text_tokens(self):
        item = self.item(text="她喜欢喝冰可乐")
        ctx = self.ctx(query_text="冰可乐")
        query_tokens = tt.tokens("冰可乐")
        expected = tt.overlap(query_tokens, tt.tokens("她喜欢喝冰可乐")) / len(query_tokens)
        self.assertGreater(expected, 0.0)
        self.assertEqual(round(rs.WEIGHTS["lexical"] * expected, 4),
                         self.parts(item, ctx)["lexical"])

    def test_the_tokenizer_is_actually_called_at_runtime(self):
        item = self.item(text="她喜欢喝冰可乐")
        with patch.object(tt, "tokens", wraps=tt.tokens) as spy:
            rs.recall([item], self.ctx(query_text="冰可乐"))
        self.assertGreaterEqual(spy.call_count, 2,
                                "查询和条目都必须经过系统唯一分词器，不许另切一套")

    def test_disjoint_query_has_zero_lexical(self):
        item = self.item(text="她喜欢喝冰可乐")
        self.assertEqual(0.0, self.parts(item, self.ctx(query_text="明天会下雨吗"))["lexical"])


class FusionTests(ScoringFixture):
    def test_score_parts_are_a_transparent_breakdown(self):
        item = self.item(text="阿甲喜欢喝冰可乐", source_session="private:1001",
                         match={"clues": ["belief"], "score_parts": {"grounded": 1.0}})
        ctx = self.ctx(query_text="冰可乐", recent_referents=["阿甲"], speaker_person_id="1001")
        row = rs.score_item(item, ctx)
        parts = row["match"]["score_parts"]
        self.assertEqual(set(rs.CLUES), set(parts))
        for clue in rs.CLUES:
            self.assertGreater(parts[clue], 0.0, "线索 %s 应点亮" % clue)
            self.assertLessEqual(parts[clue], rs.WEIGHTS[clue])
        self.assertAlmostEqual(sum(parts.values()), row["match"]["score"], places=4)
        self.assertEqual("belief", row["match"]["clues"][0], "R01 的线索要保留")
        for clue in rs.CLUES:
            self.assertIn(clue, row["match"]["clues"])

    def test_fused_clues_outrank_a_single_clue(self):
        rich = self.item("rich", "阿甲喜欢喝冰可乐")
        poor = self.item("poor", "今天的天气不错")
        ctx = self.ctx(query_text="冰可乐", recent_referents=["阿甲"])
        ranked = rs.recall([poor, rich], ctx)["items"]
        self.assertEqual(["rich", "poor"], [x["object_id"] for x in ranked])
        self.assertGreater(ranked[0]["match"]["score"], ranked[1]["match"]["score"])
        scores = [x["match"]["score"] for x in ranked]
        self.assertEqual(sorted(scores, reverse=True), scores)

    def test_quality_signals_are_kept_but_not_scored(self):
        """分数不是真实性百分比：grounded 高低不改排序分，但原样保留供溯源。"""
        grounded = self.item("g", "她喜欢喝冰可乐",
                             match={"clues": ["belief"], "score_parts": {"grounded": 1.0}})
        ungrounded = self.item("u", "她喜欢喝冰可乐",
                               match={"clues": ["belief"], "score_parts": {"grounded": 0.0}})
        ctx = self.ctx(query_text="冰可乐")
        rows = {x["object_id"]: x for x in rs.recall([grounded, ungrounded], ctx)["items"]}
        self.assertEqual(rows["g"]["match"]["score"], rows["u"]["match"]["score"])
        self.assertEqual({"grounded": 1.0}, rows["g"]["match"]["source_parts"])
        self.assertEqual({"grounded": 0.0}, rows["u"]["match"]["source_parts"])

    def test_scoring_does_not_mutate_the_projection(self):
        item = self.item(text="阿甲喜欢喝冰可乐",
                         match={"clues": ["belief"], "score_parts": {"grounded": 1.0}})
        ctx = self.ctx(query_text="冰可乐", recent_referents=["阿甲"])
        before_item = json.dumps(item, sort_keys=True, ensure_ascii=False)
        before_ctx = json.dumps(ctx, sort_keys=True, ensure_ascii=False)
        rs.recall([item], ctx)
        self.assertEqual(before_item, json.dumps(item, sort_keys=True, ensure_ascii=False))
        self.assertEqual(before_ctx, json.dumps(ctx, sort_keys=True, ensure_ascii=False))

    def test_the_result_says_what_is_not_done(self):
        got = rs.recall([self.item()], self.ctx())
        limits = " ".join(got["provenance"]["limitations"])
        for undone in ("R02", "R04", "R05"):
            self.assertIn(undone, limits)
        self.assertIn("不是命题真实性百分比", limits)
        self.assertEqual(0, got["provenance"]["expanded"])
        self.assertNotIn("conflicts", got)
        self.assertNotIn("paths", got)


class BudgetTests(ScoringFixture):
    def items(self, count):
        return [self.item("o%d" % i, "第%d条事实" % i) for i in range(count)]

    def test_budget_truncates_and_reports(self):
        got = rs.recall(self.items(5), self.ctx(retrieval_budget=2))
        self.assertEqual(2, len(got["items"]))
        self.assertEqual(5, got["provenance"]["candidates"])
        self.assertEqual(2, got["provenance"]["selected"])
        self.assertEqual(3, got["provenance"]["over_budget"])

    def test_zero_budget_means_none_not_default(self):
        got = rs.recall(self.items(3), self.ctx(retrieval_budget=0))
        self.assertEqual([], got["items"])
        self.assertEqual(3, got["provenance"]["candidates"],
                         "预算为 0 时候选仍要可见，不能假装没有候选")

    def test_missing_budget_defaults_to_thirty_two(self):
        ctx = self.ctx()
        del ctx["retrieval_budget"]
        got = rs.recall(self.items(33), ctx)
        self.assertEqual(32, len(got["items"]))


class StoreIntegrationTests(unittest.TestCase):
    """端到端：R01 从真实资产投影，R03 只打分排序，谁都不许写。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r03_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def ctx(self, **over):
        base = {"recipient_scope": "group:771177",
                "allowed_source_scopes": ["group:771177"],
                "as_of_time": NOW, "query_text": "冰可乐",
                "recent_referents": ["阿甲"]}
        base.update(over)
        return ro.query_context(**base)

    def write_json(self, relative, payload):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def test_recall_ranks_real_r01_items_and_touches_nothing(self):
        path = self.write_json("memory/group_facts.json", {"groups": {"771177": {"entries": [
            {"id": "g1", "text": "阿甲喜欢喝冰可乐", "ts": NOW - 3600.0},
            {"id": "g2", "text": "阿甲以前住在北京", "ts": NOW - 30 * 86400.0}]}}})
        ctx = self.ctx()
        fetched = ro.retrieve_evidence(ctx)
        self.assertEqual(2, len(fetched["items"]))
        before = snapshot(self.root)
        with open(path, "rb") as handle:
            asset_before = handle.read()
        got = rs.recall(fetched["items"], ctx)
        self.assertEqual(2, len(got["items"]))
        first, second = got["items"]
        self.assertIn("冰可乐", first["restatement"])
        self.assertGreater(first["match"]["score_parts"]["lexical"], 0.0)
        self.assertEqual(0.0, second["match"]["score_parts"]["lexical"])
        self.assertGreater(first["match"]["score_parts"]["entity"], 0.0)
        self.assertGreater(second["match"]["score_parts"]["entity"], 0.0)
        self.assertGreater(first["match"]["score"], second["match"]["score"])
        self.assertEqual(before, snapshot(self.root), "打分顺手建了库/目录")
        with open(path, "rb") as handle:
            self.assertEqual(asset_before, handle.read(), "打分改写了原始资产")

    def test_an_empty_data_dir_stays_empty_through_recall(self):
        before = snapshot(self.root)
        fetched = ro.retrieve_evidence(self.ctx())
        self.assertEqual([], fetched["items"])
        got = rs.recall(fetched["items"], self.ctx())
        self.assertEqual([], got["items"])
        self.assertEqual(0, got["provenance"]["candidates"])
        self.assertEqual(before, snapshot(self.root))


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "recall_scoring.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_the_scorer_never_writes_or_opens_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "atomic_write_json",
                     "save", "add", "add_belief", "record_event",
                     "write", "writelines", "dump", "touch"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读打分器里出现写操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual("open", node.func.id,
                                    "R03 是纯计算层，连文件都不该打开")

    def test_imports_stay_pure_computation(self):
        allowed = {"math", "time", "text_tokens", "__future__"}
        used = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                used.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                used.add((node.module or "").split(".")[0])
        self.assertIn("text_tokens", used, "词法必须走系统唯一分词口径")
        self.assertEqual(set(), used - allowed,
                         "打分层引入了额外依赖（库/路径/第二套分词器）：%s" % sorted(used - allowed))

    def test_lexical_only_goes_through_text_tokens(self):
        called = set()
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "text_tokens"):
                called.add(node.func.attr)
        self.assertIn("tokens", called)
        self.assertIn("overlap", called)
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertNotIn(node.name, ("tokenize", "_tokens", "_tok", "tokens", "cut"),
                                 "打分层里长出了第二套分词器 %s" % node.name)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            for inner in ast.walk(value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径，换 DATA_DIR 的测试会读到别人的库")

    def test_the_clue_and_weight_contract(self):
        self.assertEqual(("entity", "person", "time", "lexical"), rs.CLUES)
        self.assertEqual(set(rs.CLUES), set(rs.WEIGHTS))
        for weight in rs.WEIGHTS.values():
            self.assertGreater(weight, 0.0)
        self.assertGreater(rs.TIME_HALF_LIFE_SECONDS, 0.0)

    def test_only_the_audited_runtime_orchestrator_imports_the_scorer(self):
        """真实接线只能经统一记忆编排，其他消费者不得绕过权限与证据链。"""
        offenders = []
        for folder in ("bot", "website"):
            for full in sorted((Path(ROOT) / folder).rglob("*.py")):
                if ("__pycache__" in full.parts or
                        full.name in ("recall_scoring.py", "memory_runtime.py")):
                    continue
                tree = ast.parse(full.read_text(encoding="utf-8", errors="replace"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = {a.name.split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        names = {(node.module or "").split(".")[0]}
                    else:
                        continue
                    if "recall_scoring" in names:
                        offenders.append(str(full.relative_to(ROOT)))
                        break
        self.assertEqual([], offenders,
                         "R03 必须经 memory_runtime 统一编排接入")


if __name__ == "__main__":
    unittest.main()