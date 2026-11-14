# -*- coding: utf-8 -*-
"""M03 人物与实体归一：称呼落到唯一 canonical 引用，落不到就显式 unresolved。

行为测试覆盖：别名/raw QQ/per_* → canonical 人物引用；表面形式 → canonical
实体键；X13（绝不猜、绝不编造人物/实体键，多义不猜）；§5.4（小说/扮演/塔罗
的虚构实体不混入现实人物事实）；与 M02 resolve_subject 的 subject_basis 口径
兼容。AST 锁钉死纯函数契约：无写入、无顶层冻路径、不 import 写入 API，
只读桥只允许走 self_model._person_qq_map 且路径调用时解析。
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

import cognition_admission as ca
import claim_provenance as cp
import entity_normalization as en

PERSONS = {"per_aaa111": "1001", "per_bbb222": "2002"}
ALIASES = {"主人": "per_aaa111", "爸爸": "per_aaa111",
           "小明": ["per_aaa111", "per_bbb222"], "阿甲": "2002"}
REGISTRY = [{"key": "明日方舟", "aliases": ["方舟", "Arknights"], "fictional": False},
            {"key": "fiction:黛玉", "aliases": ["黛玉", "林妹妹"], "fictional": True}]


def source_row(**over):
    row = {"event_id": "evt_1", "text": "我最喜欢喝冰可乐了",
           "actor_qq": "1001", "actor_ref": "per_aaa111",
           "topic_qq": "", "topic_ref": ""}
    row.update(over)
    return row


def snapshot(root):
    """目录里有什么（含空目录）。用来证明只读桥没顺手建任何东西。"""
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


class PersonResolutionTests(unittest.TestCase):
    def resolve(self, mention, **over):
        params = {"persons": PERSONS, "aliases": ALIASES}
        params.update(over)
        return en.resolve_person(mention, **params)

    def test_an_alias_lands_on_the_unique_canonical_person(self):
        got = self.resolve("主人")
        self.assertEqual("resolved", got["status"])
        self.assertEqual("person", got["subject_kind"])
        self.assertEqual("per_aaa111", got["subject_ref"])
        self.assertEqual("1001", got["qq"])
        self.assertEqual("alias_mapping", got["subject_basis"])
        self.assertEqual("", got["reason_code"])

    def test_an_alias_pointing_at_a_raw_qq_is_canonicalized_to_per_id(self):
        got = self.resolve("阿甲")
        self.assertEqual("per_bbb222", got["subject_ref"])
        self.assertEqual("2002", got["qq"])

    def test_a_raw_qq_lands_on_the_confirmed_stable_id(self):
        got = self.resolve("2002")
        self.assertEqual("resolved", got["status"])
        self.assertEqual("per_bbb222", got["subject_ref"])
        self.assertEqual("confirmed_mapping", got["subject_basis"])

    def test_a_stable_id_resolves_with_its_raw_qq(self):
        got = self.resolve("per_aaa111")
        self.assertEqual("resolved", got["status"])
        self.assertEqual("per_aaa111", got["subject_ref"])
        self.assertEqual("1001", got["qq"])
        self.assertEqual("confirmed_mapping", got["subject_basis"])

    def test_an_unknown_stable_id_stays_unresolved_and_is_never_fabricated(self):
        got = self.resolve("per_zzz999")
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("unknown_person_id", got["reason_code"])
        self.assertEqual("", got["subject_ref"], "查不到的 per_* 不许按格式补造身份")
        self.assertEqual("", got["subject_basis"])

    def test_an_unknown_qq_stays_unresolved(self):
        got = self.resolve("9999")
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("unknown_qq", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_an_unknown_alias_stays_unresolved(self):
        got = self.resolve("群里某个路人")
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("unknown_alias", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_an_ambiguous_alias_is_not_guessed(self):
        got = self.resolve("小明")
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("ambiguous_alias", got["reason_code"])
        self.assertEqual("", got["subject_ref"], "多义称呼不许挑一个当答案")

    def test_an_alias_whose_target_is_unconfirmed_does_not_become_an_identity(self):
        got = self.resolve("神秘人", aliases={"神秘人": "per_zzz999"})
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("alias_target_unconfirmed", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_malformed_directory_rows_confirm_nobody(self):
        got = en.resolve_person("1001", persons={"1001": "per_aaa111",
                                                 "per_bbb222": "not-a-qq"})
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("unknown_qq", got["reason_code"])

    def test_one_qq_mapped_to_two_stable_ids_is_ambiguous_not_last_wins(self):
        got = en.resolve_person("1001", persons={"per_aaa111": "1001",
                                                 "per_ccc333": "1001"})
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("ambiguous_person_mapping", got["reason_code"])

    def test_an_empty_mention_is_reported_not_defaulted(self):
        got = self.resolve("   ")
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("empty_mention", got["reason_code"])

    def test_a_fiction_context_never_binds_a_real_person(self):
        for context in ca.FICTION_CONTEXTS:
            with self.subTest(context=context):
                got = self.resolve("主人", context_kind=context)
                self.assertEqual("unresolved", got["status"])
                self.assertEqual("fiction_person_not_fact", got["reason_code"])
                self.assertEqual("", got["subject_ref"],
                                 "扮演里的「主人」不是现实的主人（§5.4）")

    def test_resolution_does_not_mutate_the_caller_tables(self):
        persons, aliases = dict(PERSONS), {k: v for k, v in ALIASES.items()}
        sources = [source_row()]
        before = json.dumps([persons, aliases, sources], sort_keys=True, ensure_ascii=False)
        for mention in ("主人", "小明", "1001", "per_aaa111", "per_zzz999", "路人"):
            en.resolve_person(mention, persons, aliases, sources)
        self.assertEqual(before, json.dumps([persons, aliases, sources],
                                            sort_keys=True, ensure_ascii=False))


class PersonSourceTests(unittest.TestCase):
    """来自冻结源窗口的绑定沿用 M02 的 subject_basis 口径。"""

    def test_a_source_actor_qq_binds_to_the_stable_ref_with_m02_basis(self):
        got = en.resolve_person("1001", sources=[source_row()])
        self.assertEqual("resolved", got["status"])
        self.assertEqual("per_aaa111", got["subject_ref"])
        self.assertEqual("source_actor", got["subject_basis"])

    def test_a_source_topic_binds_with_the_topic_basis(self):
        got = en.resolve_person("2002", sources=[source_row(
            actor_qq="", actor_ref="", topic_qq="2002", topic_ref="per_bbb222")])
        self.assertEqual("per_bbb222", got["subject_ref"])
        self.assertEqual("source_topic", got["subject_basis"])

    def test_a_stable_id_can_be_confirmed_by_the_source_window_alone(self):
        got = en.resolve_person("per_aaa111", sources=[source_row()])
        self.assertEqual("resolved", got["status"])
        self.assertEqual("source_actor", got["subject_basis"])

    def test_a_raw_qq_source_is_upgraded_when_the_directory_confirms_it(self):
        got = en.resolve_person("1001", persons=PERSONS,
                                sources=[source_row(actor_ref="")])
        self.assertEqual("per_aaa111", got["subject_ref"])
        self.assertEqual("source_actor", got["subject_basis"])

    def test_a_source_only_raw_qq_stays_raw_like_m02(self):
        got = en.resolve_person("1001", sources=[source_row(actor_ref="")])
        self.assertEqual("1001", got["subject_ref"], "M02 口径：stable or raw")
        self.assertEqual("source_actor", got["subject_basis"])

    def test_the_same_form_pointing_at_two_people_is_ambiguous(self):
        got = en.resolve_person("1001", sources=[
            source_row(), source_row(event_id="evt_2", topic_qq="1001",
                                     topic_ref="per_bbb222")])
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("ambiguous_source_subject", got["reason_code"])

    def test_one_person_as_both_actor_and_topic_is_not_ambiguity(self):
        got = en.resolve_person("1001", sources=[
            source_row(), source_row(event_id="evt_2", topic_qq="1001",
                                     topic_ref="per_aaa111")])
        self.assertEqual("resolved", got["status"])
        self.assertEqual("per_aaa111", got["subject_ref"])
        self.assertEqual("source_actor", got["subject_basis"])


class EntityResolutionTests(unittest.TestCase):
    def test_a_surface_alias_lands_on_the_canonical_key(self):
        got = en.resolve_entity("方舟", REGISTRY)
        self.assertEqual("resolved", got["status"])
        self.assertEqual("entity", got["subject_kind"])
        self.assertEqual("明日方舟", got["subject_ref"])
        self.assertEqual("entity_registry", got["subject_basis"])
        self.assertFalse(got["fictional"])

    def test_the_canonical_key_matches_itself_and_latin_case_is_folded(self):
        self.assertEqual("明日方舟", en.resolve_entity("明日方舟", REGISTRY)["subject_ref"])
        self.assertEqual("明日方舟", en.resolve_entity("arknights", REGISTRY)["subject_ref"])

    def test_an_unknown_surface_never_gets_a_fabricated_key(self):
        got = en.resolve_entity("某个没注册的东西", REGISTRY)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("unknown_entity", got["reason_code"])
        self.assertEqual("", got["subject_ref"], "不许哈希/_slug_编一个实体键")

    def test_an_ambiguous_surface_is_not_guessed(self):
        registry = REGISTRY + [{"key": "fiction:另一个黛玉", "aliases": ["黛玉"]}]
        got = en.resolve_entity("黛玉", registry)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("ambiguous_entity", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_a_fictional_entity_resolves_in_a_fiction_context_but_stays_fictional(self):
        got = en.resolve_entity("林妹妹", REGISTRY, context_kind="tarot")
        self.assertEqual("resolved", got["status"])
        self.assertEqual("entity", got["subject_kind"])
        self.assertEqual("fiction:黛玉", got["subject_ref"])
        self.assertTrue(got["fictional"])

    def test_a_real_world_key_is_not_consumed_as_fact_inside_fiction(self):
        got = en.resolve_entity("方舟", REGISTRY, context_kind="roleplay")
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("fiction_entity_not_fact", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_a_fictional_entity_outside_fiction_keeps_its_fictional_flag(self):
        got = en.resolve_entity("黛玉", REGISTRY)
        self.assertEqual("resolved", got["status"])
        self.assertTrue(got["fictional"], "谈论小说人物可以，但层不许洗掉（§5.2）")
        self.assertEqual("entity", got["subject_kind"])
        self.assertFalse(got["subject_ref"].startswith("per_"),
                         "虚构实体永远不许变成人物引用")

    def test_a_mapping_shaped_registry_is_accepted(self):
        got = en.resolve_entity("方舟", {"明日方舟": {"aliases": ["方舟"]}})
        self.assertEqual("明日方舟", got["subject_ref"])

    def test_an_empty_surface_is_reported(self):
        got = en.resolve_entity("  ", REGISTRY)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("empty_surface", got["reason_code"])

    def test_resolution_does_not_mutate_the_registry(self):
        registry = json.loads(json.dumps(REGISTRY, ensure_ascii=False))
        before = json.dumps(registry, sort_keys=True, ensure_ascii=False)
        for surface in ("方舟", "黛玉", "没注册"):
            en.resolve_entity(surface, registry)
        self.assertEqual(before, json.dumps(registry, sort_keys=True, ensure_ascii=False))


class SubjectDispatchTests(unittest.TestCase):
    def test_explicit_kinds_go_to_their_own_gate(self):
        person = en.normalize_subject("主人", "person", persons=PERSONS, aliases=ALIASES)
        self.assertEqual("person", person["subject_kind"])
        entity = en.normalize_subject("方舟", "entity", registry=REGISTRY)
        self.assertEqual("entity", entity["subject_kind"])
        unscoped = en.normalize_subject("", "unscoped")
        self.assertEqual("unscoped", unscoped["subject_kind"])
        self.assertEqual("unscoped", unscoped["subject_basis"])
        self.assertEqual("", unscoped["subject_ref"])

    def test_unscoped_is_not_a_hideout_for_an_unresolved_mention(self):
        got = en.normalize_subject("主人", "unscoped", persons=PERSONS, aliases=ALIASES)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("subject_unresolved", got["reason_code"])

    def test_an_empty_mention_without_kind_is_unscoped_like_m02(self):
        got = en.normalize_subject("")
        self.assertEqual("resolved", got["status"])
        self.assertEqual("unscoped", got["subject_kind"])
        self.assertEqual("unscoped", got["subject_basis"])

    def test_auto_dispatch_uses_confirmed_forms_only(self):
        self.assertEqual("person", en.normalize_subject(
            "per_aaa111", persons=PERSONS)["subject_kind"])
        self.assertEqual("person", en.normalize_subject(
            "主人", persons=PERSONS, aliases=ALIASES)["subject_kind"])
        self.assertEqual("entity", en.normalize_subject(
            "方舟", registry=REGISTRY)["subject_kind"])

    def test_a_surface_confirmed_as_both_person_and_entity_is_not_guessed(self):
        got = en.normalize_subject("阿甲", persons=PERSONS, aliases=ALIASES,
                                   registry=REGISTRY + [{"key": "话题:阿甲",
                                                         "aliases": ["阿甲"]}])
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("ambiguous_subject_kind", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_a_roleplay_mention_binds_the_fictional_entity_not_the_real_person(self):
        registry = REGISTRY + [{"key": "fiction:扮演主人", "aliases": ["主人"],
                                "fictional": True}]
        got = en.normalize_subject("主人", persons=PERSONS, aliases=ALIASES,
                                   registry=registry, context_kind="roleplay")
        self.assertEqual("resolved", got["status"])
        self.assertEqual("entity", got["subject_kind"])
        self.assertEqual("fiction:扮演主人", got["subject_ref"])
        self.assertTrue(got["fictional"])
        self.assertNotIn("per_", got["subject_ref"], "虚构称呼不许落到现实人物")

    def test_an_unknown_kind_is_reported(self):
        got = en.normalize_subject("主人", "vibe")
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("unknown_subject_kind", got["reason_code"])

    def test_an_unconfirmed_mention_stays_unresolved_with_the_m02_code(self):
        got = en.normalize_subject("某个路人", persons=PERSONS, aliases=ALIASES,
                                   registry=REGISTRY)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("subject_unresolved", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_every_unresolved_result_keeps_the_ref_empty(self):
        cases = [en.resolve_person("per_zzz999", PERSONS),
                 en.resolve_person("9999", PERSONS),
                 en.resolve_person("小明", PERSONS, ALIASES),
                 en.resolve_entity("没注册", REGISTRY),
                 en.normalize_subject("路人", persons=PERSONS)]
        for got in cases:
            self.assertEqual("unresolved", got["status"])
            self.assertEqual("", got["subject_ref"])
            self.assertEqual("", got["subject_basis"])
            self.assertTrue(got["reason_code"], "unresolved 必须带稳定 reason_code")


class ClaimCompatTests(unittest.TestCase):
    """与 claim_provenance.resolve_subject 的口径兼容（不改 M02 本体）。"""

    def test_a_declared_actor_normalizes_to_the_same_ref_and_basis_as_m02(self):
        window = [source_row()]
        claim = {"restatement": "喜欢冰可乐", "subject_ref": "1001"}
        ref, basis, reasons = cp.resolve_subject(claim, window)
        self.assertEqual([], reasons)
        got = en.resolve_claim_subject(claim, window, persons=PERSONS)
        self.assertEqual("resolved", got["status"])
        self.assertEqual(ref, got["subject_ref"])
        self.assertEqual(basis, got["subject_basis"])

    def test_an_unscoped_claim_stays_unscoped_with_the_same_basis(self):
        claim = {"restatement": "明日方舟是一款塔防游戏"}
        _ref, basis, reasons = cp.resolve_subject(claim, [])
        self.assertEqual("unscoped", basis)
        self.assertEqual([], reasons)
        got = en.resolve_claim_subject(claim, [], registry=REGISTRY)
        self.assertEqual("unscoped", got["subject_kind"])
        self.assertEqual("unscoped", got["subject_basis"])
        self.assertEqual("", got["subject_ref"])

    def test_person_markers_without_a_real_subject_use_the_m02_reason_code(self):
        claim = {"restatement": "主人喜欢冰可乐"}
        _ref, _basis, reasons = cp.resolve_subject(claim, [])
        self.assertEqual(["subject_unresolved"], reasons)
        got = en.resolve_claim_subject(claim, [], persons=PERSONS, aliases=ALIASES)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("subject_unresolved", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_a_declared_but_unconfirmed_subject_is_not_fabricated(self):
        claim = {"restatement": "喜欢冰可乐", "subject_ref": "9999"}
        _ref, _basis, reasons = cp.resolve_subject(claim, [source_row()])
        self.assertEqual(["subject_unresolved"], reasons)
        got = en.resolve_claim_subject(claim, [source_row()], persons=PERSONS)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("", got["subject_ref"])

    def test_the_claim_context_kind_drives_the_fiction_gate(self):
        claim = {"restatement": "主人喜欢冰可乐", "subject_ref": "主人",
                 "context_kind": "roleplay"}
        got = en.resolve_claim_subject(claim, [], persons=PERSONS, aliases=ALIASES)
        self.assertEqual("unresolved", got["status"])
        self.assertEqual("fiction_person_not_fact", got["reason_code"])
        self.assertEqual("", got["subject_ref"])

    def test_a_subject_is_never_extracted_from_free_text(self):
        claim = {"restatement": "阿甲喜欢喝冰可乐"}
        got = en.resolve_claim_subject(claim, [], persons=PERSONS, aliases=ALIASES)
        self.assertEqual("unscoped", got["subject_kind"],
                         "摘述里出现别名不等于声明了主体，不许从自由文本猜（X13）")
        self.assertEqual("", got["subject_ref"])


class ReadBridgeTests(unittest.TestCase):
    """只读桥：库不存在返回空且绝不建库建目录；路径调用时解析。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m03_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_an_absent_ledger_returns_empty_and_creates_nothing(self):
        before = snapshot(self.root)
        self.assertEqual({}, en.read_person_directory(["per_aaa111"]))
        self.assertEqual(before, snapshot(self.root),
                         "只读桥顺手建了库/目录（X15）")

    def test_non_person_refs_never_touch_the_ledger(self):
        before = snapshot(self.root)
        self.assertEqual({}, en.read_person_directory(["1001", "方舟", ""]))
        self.assertEqual(before, snapshot(self.root))

    def test_a_seeded_person_row_is_read_back_as_per_id_to_qq(self):
        import self_model as sm
        pid = sm.person_id("1001", alias="主人")
        self.assertTrue(pid.startswith("per_"))
        got = en.read_person_directory([pid, "per_zzz999"])
        self.assertEqual({pid: "1001"}, got, "查不到的 per_* 留空，绝不写 person 行")
        resolved = en.resolve_person("主人", persons=got, aliases={"主人": pid})
        self.assertEqual(pid, resolved["subject_ref"])
        self.assertEqual("1001", resolved["qq"])

    def test_the_data_dir_is_resolved_at_call_time_not_import_time(self):
        import self_model as sm
        with tempfile.TemporaryDirectory(prefix="m03_other_",
                                         ignore_cleanup_errors=True) as other:
            with patch.dict(os.environ, {"DATA_DIR": other, "PERSONA_DATA_DIR": other}):
                pid = sm.person_id("2002")
                self.assertEqual({pid: "2002"}, en.read_person_directory([pid]))
            # 换回没有库的目录：如果路径在 import 时冻住，这里会读到别人的库。
            self.assertEqual({}, en.read_person_directory([pid]))


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "entity_normalization.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_the_contract_never_writes_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "connect",
                     "atomic_write_json", "save", "add", "update_meta",
                     "add_belief", "record_event", "record_tool_result",
                     "retract_evidence", "person_id", "person_aliases"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "归一契约里出现写操作 %s" % node.func.attr)

    def test_the_contract_never_opens_a_file(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual("open", node.func.id,
                                    "纯函数契约不直接开文件；只读桥走 self_model")

    def test_only_pure_modules_are_imported(self):
        allowed = {"os", "claim_provenance", "cognition_admission", "self_model"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertEqual(set(), names - allowed,
                             "归一契约 import 了写入/网络/模型 API：%s" % sorted(names - allowed))

    def test_self_model_is_lazy_and_only_the_read_helper_is_used(self):
        for node in self.tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ({a.name for a in node.names} if isinstance(node, ast.Import)
                         else {node.module or ""})
                self.assertNotIn("self_model", names,
                                 "self_model 只允许在只读桥里惰性 import")
        used = set()
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "self_model"):
                used.add(node.attr)
        self.assertEqual(set(), used - {"_person_qq_map"},
                         "绕开只读反查访问 self_model：%s（person_aliases 是写入路径）"
                         % sorted(used - {"_person_qq_map"}))

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

    def test_path_resolution_lives_only_inside_the_read_bridge(self):
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "read_person_directory":
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    self.assertNotIn(inner.func.attr, ("getenv", "join", "abspath", "isfile"),
                                     "%s 里解析了路径；核心归一必须是纯函数" % node.name)

    def test_the_contract_constants_stay_fixed(self):
        self.assertEqual(("person", "entity", "unscoped"), en.SUBJECT_KINDS)
        self.assertEqual(("resolved", "unresolved"), en.STATUSES)
        self.assertEqual(("confirmed_mapping", "alias_mapping",
                          "source_actor", "source_topic"), en.PERSON_BASES)
        self.assertEqual(("entity_registry",), en.ENTITY_BASES)

    def test_the_vocabularies_are_reused_not_reinvented(self):
        self.assertIs(ca.FICTION_CONTEXTS, en.FICTION_CONTEXTS,
                      "虚构语境词汇必须与 M01 同一份")
        self.assertIs(cp.PERSON_MARKERS, en.PERSON_MARKERS,
                      "人物称呼词汇必须与 M02 同一份")


if __name__ == "__main__":
    unittest.main()