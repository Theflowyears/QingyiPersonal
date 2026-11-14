# -*- coding: utf-8 -*-
"""V02 按根源切分的记忆保留集：消融可确定性执行与复核，不补造、不洗层。

行为锁覆盖：按 root_source_ids 切分、多根源对象归入每个相关组、显式
unrooted 桶（迁移底线：绝不补造根源）、keep/ablate × any/all 四种口径、
同输入恒同输出且与传入顺序无关、legacy/invalidated/unknown 原层保持、
坏条目与未知根源点名报告。
AST 锁钉死「真只读、确定性」：不 import retrieval_objects（R01 只观察
阶段静态锁）、不碰文件/库/网络/时钟/随机、不在 import 时冻路径。
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import memory_retention as mr


def item(oid, roots=(), legacy=False, validity="valid", **over):
    """R01 投影形状的只读样本。不 import retrieval_objects：夹具的输入
    由调用方喂进来，测试也照这个口径手工构造投影结果。"""
    row = {"object_id": oid, "object_kind": "belief", "store": "cognition",
           "restatement": "命题" + str(oid), "revision": "r0",
           "source_session": "private:1001", "visibility": "private",
           "epistemic_kind": "reported", "occurred_at": 1000.0,
           "recorded_at": 1000.0, "root_source_ids": list(roots),
           "evidence_spans": [{"event_id": "e1", "quote": "原话",
                               "span_start": 0, "span_end": 2}],
           "legacy": legacy, "validity": validity, "current": True}
    row.update(over)
    return row


def fixture():
    """a←e1, b←e2, ab←e1+e2, dead←e2(已失效), old←无根源(legacy)。"""
    return [item("a", ["e1"]), item("b", ["e2"]), item("ab", ["e1", "e2"]),
            item("dead", ["e2"], validity="invalidated"),
            item("old", [], legacy=True, validity="unknown")]


def snapshot(root):
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


class SplitTests(unittest.TestCase):
    def test_single_root_items_group_under_their_root(self):
        got = mr.split_by_root(fixture())
        self.assertEqual(["a", "ab"], got["groups"]["e1"])
        self.assertEqual(["ab", "b", "dead"], got["groups"]["e2"])
        self.assertEqual(["e1", "e2"], sorted(got["groups"]))

    def test_a_multi_root_item_belongs_to_every_related_group(self):
        got = mr.split_by_root(fixture())
        self.assertEqual(["ab"], got["multi_root_items"])
        for root in ("e1", "e2"):
            self.assertIn("ab", got["groups"][root])

    def test_unrooted_items_go_to_the_explicit_bucket(self):
        got = mr.split_by_root(fixture())
        self.assertEqual(["old"], got["unrooted"])
        self.assertNotIn("old", got["groups"].get("e1", []))
        self.assertNotIn("old", got["groups"].get("e2", []))

    def test_a_missing_root_field_is_unrooted_not_a_fabricated_root(self):
        row = item("x")
        del row["root_source_ids"]
        got = mr.split_by_root([row])
        self.assertEqual(["x"], got["unrooted"])
        self.assertEqual({}, got["groups"], "无根源对象不许被补造出任何根源组")

    def test_blank_and_duplicate_roots_are_normalized_not_invented(self):
        got = mr.split_by_root([item("n", [" e1 ", "", "e1", None, "e2"])])
        self.assertEqual({"e1": ["n"], "e2": ["n"]}, got["groups"])
        self.assertEqual(["n"], got["multi_root_items"])

    def test_a_single_string_root_is_not_split_into_characters(self):
        row = item("s")
        row["root_source_ids"] = "e1"
        got = mr.split_by_root([row])
        self.assertEqual({"e1": ["s"]}, got["groups"])

    def test_groups_members_and_buckets_are_sorted(self):
        rows = [item("z", ["r2", "r1"]), item("a", ["r2"]), item("m", [])]
        got = mr.split_by_root(rows)
        self.assertEqual(["r1", "r2"], list(got["groups"]))
        self.assertEqual(["a", "z"], got["groups"]["r2"])
        self.assertEqual(["m"], got["unrooted"])
        self.assertEqual(["z"], got["multi_root_items"])

    def test_invalid_items_are_named_not_silently_dropped(self):
        rows = [None, 42, {}, {"object_id": "  "}, item("x"), item("x", ["e9"])]
        got = mr.split_by_root(rows)
        self.assertEqual([{"index": 0, "reason": "item_not_dict"},
                          {"index": 1, "reason": "item_not_dict"},
                          {"index": 2, "reason": "missing_object_id"},
                          {"index": 3, "reason": "missing_object_id"},
                          {"index": 5, "reason": "duplicate_object_id", "object_id": "x"}],
                         got["problems"])
        self.assertEqual({}, got["groups"], "重复 ID 的第二份描述不许悄悄顶掉第一份")
        self.assertEqual(["x"], got["unrooted"])
        self.assertEqual(1, got["item_count"])

    def test_empty_input_splits_to_empty_buckets(self):
        got = mr.split_by_root([])
        self.assertEqual({}, got["groups"])
        self.assertEqual([], got["unrooted"])
        self.assertEqual(0, got["item_count"])
        self.assertEqual([], got["problems"])


class KeepSelectionTests(unittest.TestCase):
    def test_keep_any_retains_a_multi_root_item_when_one_root_is_kept(self):
        got = mr.retention_set(fixture(), ["e1"], mode="keep", policy="any")
        self.assertEqual(["a", "ab", "old"], got["retained"])
        self.assertEqual(["b", "dead"], got["removed"])
        self.assertEqual("any", got["policy"])

    def test_keep_all_requires_every_root_to_be_kept(self):
        got = mr.retention_set(fixture(), ["e1"], mode="keep", policy="all")
        self.assertEqual(["a", "old"], got["retained"])
        self.assertEqual({"b": "root_not_kept", "ab": "root_not_kept",
                          "dead": "root_not_kept"}, got["removed_reasons"])
        both = mr.retention_set(fixture(), ["e1", "e2"], mode="keep", policy="all")
        self.assertEqual(["a", "ab", "b", "dead", "old"], both["retained"])

    def test_keep_with_no_roots_retains_only_the_unrooted_bucket(self):
        got = mr.retention_set(fixture(), [], mode="keep")
        self.assertEqual(["old"], got["retained"])
        self.assertEqual(4, got["removed_count"])

    def test_unrooted_is_kept_by_default_in_both_modes(self):
        for mode in ("keep", "ablate"):
            got = mr.retention_set(fixture(), ["e1"], mode=mode)
            self.assertIn("old", got["retained"],
                          "消融某个根源不该顺手抹掉她的旧背景")

    def test_keep_unrooted_false_excludes_the_bucket_explicitly(self):
        got = mr.retention_set(fixture(), ["e1"], mode="keep", keep_unrooted=False)
        self.assertNotIn("old", got["retained"])
        self.assertEqual("unrooted_excluded", got["removed_reasons"]["old"])
        self.assertFalse(got["keep_unrooted"])

    def test_a_single_string_roots_argument_is_one_root_not_characters(self):
        got = mr.retention_set(fixture(), "e1", mode="keep")
        self.assertEqual(["e1"], got["requested_roots"])
        self.assertEqual(["a", "ab", "old"], got["retained"])


class AblateSelectionTests(unittest.TestCase):
    def test_ablate_any_removes_every_derived_memory_of_the_root(self):
        got = mr.retention_set(fixture(), ["e1"], mode="ablate", policy="any")
        self.assertEqual(["b", "dead", "old"], got["retained"])
        self.assertEqual({"a": "root_ablated", "ab": "root_ablated"}, got["removed_reasons"])

    def test_ablate_all_spares_a_multi_root_item_until_every_root_is_ablated(self):
        one = mr.retention_set(fixture(), ["e1"], mode="ablate", policy="all")
        self.assertEqual(["ab", "b", "dead", "old"], one["retained"])
        self.assertEqual({"a": "root_ablated"}, one["removed_reasons"])
        both = mr.retention_set(fixture(), ["e1", "e2"], mode="ablate", policy="all")
        self.assertEqual(["old"], both["retained"])

    def test_ablate_with_no_roots_removes_nothing(self):
        got = mr.retention_set(fixture(), [], mode="ablate")
        self.assertEqual(["a", "ab", "b", "dead", "old"], got["retained"])
        self.assertEqual([], got["removed"])

    def test_ablate_any_equals_keep_complement_all(self):
        """口径对偶：剔除 S(any) ≡ 保留 全集−S(all)。两条路必须同一答案。"""
        rows = fixture()
        universe = set(mr.split_by_root(rows)["groups"])
        for subset in ({"e1"}, {"e2"}, {"e1", "e2"}, set()):
            ablated = mr.retention_set(rows, sorted(subset), mode="ablate", policy="any")
            kept = mr.retention_set(rows, sorted(universe - subset), mode="keep", policy="all")
            self.assertEqual(ablated["retained"], kept["retained"], subset)
            self.assertEqual(ablated["removed"], kept["removed"], subset)

    def test_ablate_all_equals_keep_complement_any(self):
        rows = fixture()
        universe = set(mr.split_by_root(rows)["groups"])
        for subset in ({"e1"}, {"e2"}, {"e1", "e2"}, set()):
            ablated = mr.retention_set(rows, sorted(subset), mode="ablate", policy="all")
            kept = mr.retention_set(rows, sorted(universe - subset), mode="keep", policy="any")
            self.assertEqual(ablated["retained"], kept["retained"], subset)

    def test_an_unknown_root_is_reported_not_a_silent_no_op(self):
        got = mr.retention_set(fixture(), ["nope"], mode="ablate")
        self.assertEqual(["nope"], got["unknown_roots"])
        self.assertEqual([], got["removed"])
        self.assertEqual(5, got["retained_count"])

    def test_the_unrooted_token_is_never_treated_as_a_real_root(self):
        got = mr.retention_set(fixture(), ["unrooted"], mode="ablate")
        self.assertIn({"reason": "unrooted_not_a_root"}, got["problems"])
        self.assertEqual([], got["requested_roots"])
        self.assertEqual([], got["unknown_roots"])
        self.assertIn("old", got["retained"])
        strict = mr.retention_set(fixture(), ["unrooted"], mode="ablate", keep_unrooted=False)
        self.assertEqual("unrooted_excluded", strict["removed_reasons"]["old"],
                         "桶名混进 roots 也不许变成对旧档案的根源级消融")


class PreservationTests(unittest.TestCase):
    def test_legacy_stays_legacy_and_keeps_its_empty_roots(self):
        got = mr.retention_set(fixture(), ["e1"], mode="ablate")
        old = [x for x in got["retained_items"] if x["object_id"] == "old"][0]
        self.assertTrue(old["legacy"])
        self.assertEqual("unknown", old["validity"])
        self.assertEqual([], old["root_source_ids"], "保留集不许给 legacy 补造根源")

    def test_invalidated_stays_invalidated_and_is_not_washed_into_valid(self):
        got = mr.retention_set(fixture(), ["e2"], mode="keep")
        dead = [x for x in got["retained_items"] if x["object_id"] == "dead"][0]
        self.assertEqual("invalidated", dead["validity"])
        self.assertIn("dead", got["retained"],
                      "有效性过滤是 R01 hard_filter 的职责，这里不越权静默丢条目")
        ablated = mr.retention_set(fixture(), ["e1"], mode="ablate")
        dead2 = [x for x in ablated["retained_items"] if x["object_id"] == "dead"][0]
        self.assertEqual("invalidated", dead2["validity"])

    def test_unknown_validity_is_not_promoted(self):
        got = mr.retention_set(fixture(), [], mode="ablate")
        by_id = {x["object_id"]: x for x in got["retained_items"]}
        self.assertEqual("unknown", by_id["old"]["validity"])
        self.assertEqual("valid", by_id["a"]["validity"])

    def test_retained_items_are_field_for_field_copies_of_the_input(self):
        rows = fixture()
        got = mr.retention_set(rows, ["e1", "e2"], mode="keep")
        originals = {x["object_id"]: x for x in rows}
        for copy_row in got["retained_items"]:
            self.assertEqual(originals[copy_row["object_id"]], copy_row)

    def test_inputs_are_not_mutated_by_split_or_selection(self):
        rows = fixture()
        before = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        mr.split_by_root(rows)
        mr.retention_set(rows, ["e1"], mode="keep")
        mr.retention_set(rows, ["e1"], mode="ablate", policy="all", keep_unrooted=False)
        self.assertEqual(before, json.dumps(rows, sort_keys=True, ensure_ascii=False))

    def test_retained_items_are_deep_copies_not_aliases(self):
        rows = fixture()
        got = mr.retention_set(rows, ["e1"], mode="keep")
        ab = [x for x in got["retained_items"] if x["object_id"] == "ab"][0]
        ab["root_source_ids"].append("fake")
        ab["evidence_spans"][0]["quote"] = "改过的原话"
        ab["validity"] = "valid"
        original = [x for x in rows if x["object_id"] == "ab"][0]
        self.assertEqual(["e1", "e2"], original["root_source_ids"])
        self.assertEqual("原话", original["evidence_spans"][0]["quote"])


class DeterminismTests(unittest.TestCase):
    def test_same_input_gives_the_same_output(self):
        rows = fixture()
        first = mr.retention_set(rows, ["e1"], mode="ablate")
        second = mr.retention_set(rows, ["e1"], mode="ablate")
        self.assertEqual(first, second)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(mr.split_by_root(rows), mr.split_by_root(rows))

    def test_input_order_does_not_change_the_answer(self):
        rows = fixture()
        baseline = mr.retention_set(rows, ["e1"], mode="ablate")
        for shuffled in (list(reversed(rows)), rows[2:] + rows[:2], rows[1::2] + rows[::2]):
            got = mr.retention_set(shuffled, ["e1"], mode="ablate")
            self.assertEqual(baseline, got)
            self.assertEqual(mr.split_by_root(rows), mr.split_by_root(shuffled))

    def test_root_order_and_duplicates_do_not_change_the_answer(self):
        rows = fixture()
        baseline = mr.retention_set(rows, ["e1", "e2"], mode="keep", policy="all")
        for roots in (["e2", "e1"], ["e1", "e1", "e2"], [" e2 ", "e1"]):
            self.assertEqual(baseline, mr.retention_set(rows, roots, mode="keep", policy="all"))

    def test_the_fingerprint_changes_when_the_selection_changes(self):
        rows = fixture()
        keep_e1 = mr.retention_set(rows, ["e1"], mode="keep")
        keep_e2 = mr.retention_set(rows, ["e2"], mode="keep")
        self.assertNotEqual(keep_e1["fingerprint"], keep_e2["fingerprint"])
        self.assertNotEqual(keep_e1["fingerprint"],
                            mr.retention_set(rows, ["e1"], mode="keep",
                                             keep_unrooted=False)["fingerprint"])

    def test_the_fingerprint_is_a_stable_short_hex_string(self):
        got = mr.retention_set(fixture(), ["e1"], mode="keep")
        self.assertRegex(got["fingerprint"], r"^[0-9a-f]{14}$")


class ContractTests(unittest.TestCase):
    def test_the_result_has_the_full_review_shape(self):
        got = mr.retention_set(fixture(), ["e1"], mode="keep")
        self.assertEqual({"mode", "policy", "keep_unrooted", "requested_roots",
                          "retained", "removed", "unknown_roots", "removed_reasons",
                          "retained_items", "retained_count", "removed_count",
                          "problems", "fingerprint"}, set(got))
        self.assertEqual(got["retained_count"], len(got["retained"]))
        self.assertEqual(got["removed_count"], len(got["removed"]))
        self.assertEqual(got["retained"], sorted(got["retained"]))
        self.assertEqual(got["removed"], sorted(got["removed"]))

    def test_retained_and_removed_partition_the_valid_items(self):
        rows = fixture()
        got = mr.retention_set(rows, ["e1"], mode="keep")
        self.assertEqual(sorted(x["object_id"] for x in rows),
                         sorted(got["retained"] + got["removed"]))
        self.assertEqual([], sorted(set(got["retained"]) & set(got["removed"])))
        self.assertEqual([x["object_id"] for x in got["retained_items"]], got["retained"])

    def test_an_unknown_mode_or_policy_raises_loudly(self):
        with self.assertRaises(ValueError):
            mr.retention_set(fixture(), ["e1"], mode="wash")
        with self.assertRaises(ValueError):
            mr.retention_set(fixture(), ["e1"], policy="maybe")

    def test_empty_input_selects_nothing_without_problems(self):
        got = mr.retention_set([], [], mode="keep")
        self.assertEqual([], got["retained"])
        self.assertEqual([], got["removed"])
        self.assertEqual([], got["problems"])

    def test_invalid_items_never_enter_the_retention_set(self):
        rows = [None, {"object_id": ""}, item("ok", ["e1"])]
        got = mr.retention_set(rows, ["e1"], mode="keep")
        self.assertEqual(["ok"], got["retained"])
        self.assertEqual([{"index": 0, "reason": "item_not_dict"},
                          {"index": 1, "reason": "missing_object_id"}], got["problems"])

    def test_no_filesystem_side_effects(self):
        temporary = tempfile.TemporaryDirectory(prefix="v02_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        with patch.dict(os.environ, {"DATA_DIR": temporary.name,
                                     "PERSONA_DATA_DIR": temporary.name}):
            before = snapshot(temporary.name)
            mr.split_by_root(fixture())
            mr.retention_set(fixture(), ["e1"], mode="ablate")
            self.assertEqual(before, snapshot(temporary.name),
                             "验证夹具顺手建了库/目录，就不再是只读")


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "memory_retention.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_only_pure_stdlib_helpers_are_imported(self):
        allowed = {"copy", "hashlib", "json"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             "只读确定性夹具引入了非纯依赖：%s" % sorted(names - allowed))

    def test_r01_is_not_imported_or_called(self):
        """R01 只观察阶段静态锁：items 由调用方喂进来，这里不接检索链路。"""
        wanted = {"retrieval_objects", "retrieve_evidence", "fetch_retrieval_items"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            elif isinstance(node, ast.Call):
                func = node.func
                names = {func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")}
            else:
                continue
            self.assertEqual(set(), names & wanted,
                             "V02 夹具接上了 R01 检索链路，违反只观察阶段锁")

    def test_no_write_io_or_network_calls(self):
        forbidden = {"open", "makedirs", "mkdir", "remove", "unlink", "rmtree",
                     "rename", "execute", "executescript", "commit", "save",
                     "write", "writelines", "dump", "getenv", "system", "popen",
                     "urlopen", "connect", "cursor", "record_event", "add_belief"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读夹具里出现写/IO 操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id,
                                 {"open", "eval", "exec", "compile", "__import__", "input"},
                                 "只读夹具里出现危险调用 %s" % node.func.id)

    def test_no_clock_randomness_or_uuid(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 {"time", "monotonic", "now", "today", "random",
                                  "randint", "choice", "shuffle", "uuid4", "urandom"},
                                 "确定性夹具引入了时钟/随机：%s" % node.func.attr)

    def test_no_path_is_frozen_at_import_time(self):
        for node in self.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath"),
                                     "模块级冻住了路径，换 DATA_DIR 的测试会读到别人的库")

    def test_the_contract_constants_stay_fixed(self):
        self.assertEqual("unrooted", mr.UNROOTED)
        self.assertEqual(("keep", "ablate"), mr.MODES)
        self.assertEqual(("any", "all"), mr.POLICIES)


if __name__ == "__main__":
    unittest.main()