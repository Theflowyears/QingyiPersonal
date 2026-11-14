"""R01 多库统一只读检索对象：五个库投影成同一种描述，只读、不装懂。

行为测试覆盖统一方案 §19.1–19.3 的结果形状、§8.3 第 2 步硬过滤，以及
X14（来源范围与受众范围分开）、X15（读路径不隐式初始化）、X16（归档不等于假）。
AST 锁钉死「真只读」：不建库建目录、不在 import 时冻路径、不绕开 self_model 写认知库。
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

import retrieval_objects as ro
import self_model as sm

# 档案/信念不带 ts 落库时用的是真实 time.time()。as_of_time 必须晚于它们，
# 否则整库都会被当成「未来证据」挡掉，测试会绿得毫无意义。
NOW = 4000000000.0


def snapshot(root):
    """目录里有什么（含空目录）。用来证明读一次没顺手建任何东西。"""
    found = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            found.append(os.path.relpath(os.path.join(base, name), root) + "/")
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(found)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r01_", ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        environment = patch.dict(os.environ, {"DATA_DIR": self.root,
                                              "PERSONA_DATA_DIR": self.root,
                                              "QQ_DRY_RUN": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def ctx(self, **over):
        base = {"recipient_scope": "private:1001",
                "allowed_source_scopes": ["private:1001"],
                "as_of_time": NOW, "query_text": "她喜欢什么"}
        base.update(over)
        return ro.query_context(**base)

    def write_json(self, relative, payload):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def write_raw(self, relative, text):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def belief(self, quote="我喜欢冰可乐", text="对方明确自述：我喜欢冰可乐",
               session="private:1001", visibility="private", actor="1001", ts=1000.0):
        eid = sm.record_event("private_message", session=session, actor_qq=actor,
                              visibility=visibility, payload={"text": quote}, ts=ts,
                              dedupe="r01:" + session + ":" + quote)
        start = 0
        bid = sm.add_belief(text, subject_qq=actor, source="explicit_self_report",
                            ts=ts, spans=[{"event_id": eid, "quote": quote,
                                           "span_start": start, "span_end": start + len(quote),
                                           "match_kind": "model_quote"}])
        return eid, bid

    def items(self, ctx=None):
        return ro.retrieve_evidence(ctx if ctx is not None else self.ctx())["items"]

    def by_store(self, store, ctx=None):
        return [x for x in self.items(ctx) if x["store"] == store]

    def seed_stores(self):
        """五个库都存在但可以是空的：用来把「读不到」和「没有」分开。"""
        import persona_store as ps
        ps.conn()
        sm._db().close()
        self.write_json("memory/group_facts.json", {"groups": {}})
        self.write_json("memory/world_knowledge.json", {"topics": []})
        self.write_json("memory/vision_notes.json", {})

    def archive_ctx(self, **over):
        """显式授权读旧档案（§7.5 显式回忆可恢复访问）。"""
        base = {"allowed_source_scopes": ["private:1001", "persona", "cognition"]}
        base.update(over)
        return self.ctx(**base)


class ReadOnlyTests(StoreFixture):
    """X15：读路径可建表是缺陷，不是便利。"""

    def test_an_empty_data_dir_reports_absent_and_creates_nothing(self):
        before = snapshot(self.root)
        got = ro.retrieve_evidence(self.ctx())
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["items"])
        self.assertEqual(set(ro.STORES), {d["store"] for d in got["degradation"]})
        self.assertEqual({"absent"}, {d["reason"] for d in got["degradation"]})
        self.assertEqual(before, snapshot(self.root),
                         "只读检索顺手建了库/目录，读路径就不再是只读")

    def test_absent_and_unreadable_are_not_the_same_answer(self):
        self.write_raw("memory/group_facts.json", "{ this is not json")
        got = ro.retrieve_evidence(self.ctx())
        reasons = {d["store"]: d["reason"] for d in got["degradation"]}
        self.assertEqual("unreadable", reasons["group"])
        self.assertEqual("absent", reasons["persona"])
        self.assertNotEqual(reasons["group"], reasons["persona"],
                            "读不动被当成空，就等于声称她没有这段记忆")

    def test_reading_never_rewrites_the_asset(self):
        path = self.write_json("memory/group_facts.json", {"groups": {"771177": {"entries": [
            {"id": "g1", "topic": "阿甲", "text": "阿甲喜欢喝冰可乐", "ts": 1000.0}]}}})
        with open(path, "rb") as handle:
            before = handle.read()
        ro.retrieve_evidence(self.ctx(recipient_scope="group:771177",
                                      allowed_source_scopes=["group:771177"]))
        with open(path, "rb") as handle:
            self.assertEqual(before, handle.read(), "只读适配器改写了原始资产")

    def test_the_old_retrieval_path_still_reads_the_same_asset(self):
        self.write_json("memory/group_facts.json", {"groups": {"771177": {"entries": [
            {"id": "g1", "topic": "阿甲", "text": "阿甲喜欢喝冰可乐", "ts": 1000.0}]}}})
        ro.retrieve_evidence(self.ctx(recipient_scope="group:771177",
                                      allowed_source_scopes=["group:771177"]))
        import group_memory_store as gms
        self.assertEqual(["阿甲喜欢喝冰可乐"],
                         [e["text"] for e in gms.get_entries("771177")],
                         "旧检索路径必须继续可用（§19.5 只观察阶段）")


class ContextTests(StoreFixture):
    def test_query_context_exposes_every_field_of_the_contract(self):
        self.assertEqual(14, len(ro.QUERY_FIELDS))
        self.assertEqual(set(ro.QUERY_FIELDS), set(ro.query_context()))

    def test_unknown_fields_are_not_silently_accepted(self):
        self.assertNotIn("verified", ro.query_context(verified=True))

    def test_a_context_without_a_recipient_gets_no_green_light(self):
        got = ro.retrieve_evidence(ro.query_context(as_of_time=NOW))
        self.assertEqual("degraded", got["status"])
        self.assertEqual([], got["items"])
        self.assertEqual(["query_context"], got["missing_support"])
        self.assertIn("missing_recipient_scope", got["degradation"][0]["detail"])
        self.assertIn("no_allowed_source_scopes", got["degradation"][0]["detail"])

    def test_a_non_positive_as_of_time_is_reported(self):
        self.assertIn("invalid_as_of_time", ro.context_problems(ro.query_context(as_of_time=0)))

    def test_a_missing_field_is_named_not_defaulted(self):
        ctx = ro.query_context()
        del ctx["validity_epoch"]
        self.assertIn("missing_validity_epoch", ro.context_problems(ctx))


class CognitionProjectionTests(StoreFixture):
    def test_a_grounded_belief_carries_its_own_quote_and_source(self):
        eid, bid = self.belief()
        rows = self.by_store("cognition")
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(bid, row["object_id"])
        self.assertEqual("belief", row["object_kind"])
        self.assertEqual([eid], row["root_source_ids"])
        self.assertEqual(1, len(row["evidence_spans"]))
        self.assertEqual("我喜欢冰可乐", row["evidence_spans"][0]["quote"])
        self.assertEqual("valid", row["validity"])
        self.assertEqual("reported", row["epistemic_kind"])
        self.assertEqual("private:1001", row["source_session"])
        self.assertEqual("private", row["visibility"])

    def test_a_legacy_belief_is_not_washed_into_verified(self):
        sm.add_belief("旧背景里的一条", source="legacy", is_legacy=True)
        row = self.by_store("cognition", self.archive_ctx())[0]
        self.assertTrue(row["legacy"])
        self.assertEqual("unknown", row["validity"])
        self.assertEqual("inferred", row["epistemic_kind"])
        self.assertEqual([], row["root_source_ids"])
        self.assertEqual("archive", row["scope_basis"])

    def test_old_background_is_not_readable_without_an_explicit_grant(self):
        sm.add_belief("旧背景里的一条", source="legacy", is_legacy=True)
        got = ro.retrieve_evidence(self.ctx())
        self.assertEqual([], got["items"])
        self.assertEqual({"archive_not_granted": 1}, got["provenance"]["filtered"])
        self.assertIn("not_granted", {d["reason"] for d in got["degradation"]},
                      "有旧背景却读不到，必须说出来，不能静默当成她没有记忆")

    def test_old_background_is_never_handed_to_a_group_audience(self):
        sm.add_belief("旧背景里的一条", source="legacy", is_legacy=True)
        got = ro.retrieve_evidence(self.archive_ctx(recipient_scope="group:771177",
                                                    allowed_source_scopes=["group:771177",
                                                                           "cognition"]))
        self.assertEqual([], got["items"])

    def test_a_belief_whose_source_was_retracted_is_not_returned_at_all(self):
        eid, _bid = self.belief()
        self.assertTrue(sm.retract_evidence(eid, "看错了"))
        self.assertEqual([], self.by_store("cognition", self.archive_ctx()))
        self.assertIsNotNone(sm.get_event(eid), "撤回不等于删除原始观察")

    def test_a_group_belief_never_leaks_into_a_private_recipient_by_default(self):
        self.belief(session="group:771177", visibility="group", actor="22001")
        got = ro.retrieve_evidence(self.ctx())
        self.assertEqual([], [x for x in got["items"] if x["store"] == "cognition"])


class PersonaProjectionTests(StoreFixture):
    def test_a_sourced_persona_row_inherits_the_scope_of_its_real_source(self):
        import persona_store as ps
        eid, _bid = self.belief()
        ps.add("facts", "她喜欢冰可乐", source_event_ids=[eid],
               evidence_spans=[{"event_id": eid, "quote": "我喜欢冰可乐",
                                "span_start": 0, "span_end": 5}])
        row = self.by_store("persona")[0]
        self.assertEqual("valid", row["validity"])
        self.assertEqual([eid], row["root_source_ids"])
        self.assertEqual("private:1001", row["source_session"])
        self.assertEqual("source_event", row["scope_basis"])
        self.assertEqual("reported", row["epistemic_kind"])

    def test_an_unsourced_persona_row_stays_unknown_and_needs_an_explicit_grant(self):
        import persona_store as ps
        ps.add("facts", "旧档案里的一条")
        self.assertEqual([], self.by_store("persona"))
        row = self.by_store("persona", self.archive_ctx())[0]
        self.assertEqual("unknown", row["validity"])
        self.assertTrue(row["legacy"])
        self.assertEqual("archive", row["scope_basis"])
        self.assertEqual([], row["root_source_ids"], "无依据的条目不许被补造来源")

    def test_a_persona_row_whose_source_was_retracted_is_invalidated(self):
        import persona_store as ps
        eid, _bid = self.belief()
        ps.add("facts", "她喜欢冰可乐", source_event_ids=[eid])
        sm.retract_evidence(eid, "看错了")
        projected = [x for x in ro.fetch_retrieval_items(self.archive_ctx())["items"]
                     if x["store"] == "persona"]
        self.assertEqual("invalidated", projected[0]["validity"])
        self.assertEqual([], projected[0]["root_source_ids"])

    def test_a_retracted_persona_row_is_dropped_and_the_drop_is_explainable(self):
        """X06：撤回要贯通到 persona，不能只抑制 self_view。"""
        import persona_store as ps
        self.seed_stores()
        eid, _bid = self.belief()
        ps.add("facts", "她喜欢冰可乐", source_event_ids=[eid])
        sm.retract_evidence(eid, "看错了")
        got = ro.retrieve_evidence(self.archive_ctx())
        self.assertEqual([], [x for x in got["items"] if x["store"] == "persona"])
        self.assertEqual({"invalidated": 1}, got["provenance"]["filtered"])

    def test_persona_rows_are_private_even_when_the_query_is_public_facing(self):
        import persona_store as ps
        eid, _bid = self.belief()
        ps.add("likes", "冰可乐", source_event_ids=[eid])
        row = self.by_store("persona")[0]
        self.assertEqual("private", row["visibility"])
        self.assertEqual("persona_likes", row["object_kind"])
        group_view = ro.retrieve_evidence(self.ctx(recipient_scope="group:771177",
                                                   allowed_source_scopes=["group:771177",
                                                                          "private:1001"]))
        self.assertEqual([], [x for x in group_view["items"] if x["store"] == "persona"],
                         "私聊档案被摘给群受众，派生范围就放宽了")


class GroupProjectionTests(StoreFixture):
    def group_ctx(self, **over):
        base = {"recipient_scope": "group:771177", "allowed_source_scopes": ["group:771177"]}
        base.update(over)
        return self.ctx(**base)

    def test_an_archived_entry_is_still_returned_but_not_as_current(self):
        """X16：归档是「过期」，不是「假」。历史仍可回顾。"""
        self.write_json("memory/group_facts.json", {"groups": {"771177": {"entries": [
            {"id": "g1", "text": "阿甲以前住在北京", "ts": 1000.0, "status": "archived"},
            {"id": "g2", "text": "阿甲现在住在上海", "ts": 1500.0}]}}})
        rows = {x["restatement"]: x for x in self.by_store("group", self.group_ctx())}
        self.assertEqual(2, len(rows), "归档条目被当成假删掉了")
        self.assertFalse(rows["阿甲以前住在北京"]["current"])
        self.assertTrue(rows["阿甲现在住在上海"]["current"])

    def test_a_group_fact_from_another_group_is_out_of_scope(self):
        self.write_json("memory/group_facts.json", {"groups": {
            "771177": {"entries": [{"id": "g1", "text": "本群的事", "ts": 1000.0}]},
            "999": {"entries": [{"id": "g2", "text": "别的群的事", "ts": 1000.0}]}}})
        got = ro.retrieve_evidence(self.group_ctx())
        self.assertEqual(["本群的事"], [x["restatement"] for x in got["items"]])
        self.assertEqual({"out_of_scope": 1}, got["provenance"]["filtered"])


class KnowledgeProjectionTests(StoreFixture):
    def test_only_active_topics_are_projected_and_they_are_looked_up(self):
        self.write_json("memory/world_knowledge.json", {"topics": [
            {"key": "明日方舟", "summary": "一款塔防游戏", "ts": 1000.0,
             "evidence_url": "https://example.invalid/a", "verified": True},
            {"key": "卫戍协议", "summary": "被记错的那条", "ts": 1000.0,
             "status": "quarantined", "evidence_url": "https://example.invalid/b"},
            {"key": "老话题", "summary": "过期的那条", "ts": 1000.0, "status": "archive"}]})
        rows = self.by_store("knowledge")
        self.assertEqual(["一款塔防游戏"], [x["restatement"] for x in rows])
        self.assertEqual("looked_up", rows[0]["epistemic_kind"])
        self.assertEqual("public", rows[0]["visibility"])
        self.assertEqual(["https://example.invalid/a"], rows[0]["root_source_ids"])

    def test_public_knowledge_crosses_a_private_recipient_scope(self):
        self.write_json("memory/world_knowledge.json", {"topics": [
            {"key": "明日方舟", "summary": "一款塔防游戏", "ts": 1000.0,
             "evidence_url": "https://example.invalid/a"}]})
        self.assertEqual(1, len(self.by_store("knowledge")))

    def test_an_unsourced_summary_is_not_claimed_as_verified(self):
        self.write_json("memory/world_knowledge.json", {"topics": [
            {"key": "某话题", "summary": "没有来源的摘要", "ts": 1000.0}]})
        row = self.by_store("knowledge")[0]
        self.assertEqual("unknown", row["validity"])
        self.assertTrue(row["legacy"])


class VisionProjectionTests(StoreFixture):
    def note(self, key="v1", **over):
        rec = {"key": key, "ts": 1000.0, "session": "group:771177",
               "visibility": "group", "objects": ["猫", "沙发"],
               "scene": "客厅", "intent": "晒猫"}
        rec.update(over)
        self.write_json("memory/vision_notes.json", {key: rec})
        return rec

    def group_ctx(self):
        return self.ctx(recipient_scope="group:771177",
                        allowed_source_scopes=["group:771177"])

    def test_a_note_is_projected_from_its_real_fields(self):
        """笔记里没有 meaning/desc 字段；照想象读会静默丢掉全部看图记忆。"""
        self.note()
        rows = self.by_store("vision", self.group_ctx())
        self.assertEqual(1, len(rows))
        self.assertIn("晒猫", rows[0]["restatement"])
        self.assertIn("猫", rows[0]["restatement"])
        self.assertEqual("looked_up", rows[0]["epistemic_kind"])

    def test_a_note_with_only_objects_still_has_content(self):
        self.note(intent="", scene="", objects=["猫"])
        self.assertEqual(1, len(self.by_store("vision", self.group_ctx())))

    def test_the_live_tool_receipt_is_the_source_of_a_note(self):
        self.note()
        eid = sm.record_tool_result("vision", "v1", True, session="group:771177",
                                    run_id="run-live", visibility="group")
        row = self.by_store("vision", self.group_ctx())[0]
        self.assertEqual([eid], row["root_source_ids"])
        self.assertEqual("valid", row["validity"])
        self.assertFalse(row["legacy"])

    def test_a_verification_sample_is_never_the_source_of_a_note(self):
        self.note()
        sm.record_tool_result("vision", "v1", outcome="unknown", session="verify:r01",
                              run_id="run-synthetic", visibility="group")
        row = self.by_store("vision", self.group_ctx())[0]
        self.assertEqual([], row["root_source_ids"])
        self.assertEqual("unknown", row["validity"])
        self.assertTrue(row["legacy"], "自检样本冒充她的真实经历")

    def test_a_retracted_receipt_stops_being_a_source(self):
        self.note()
        eid = sm.record_tool_result("vision", "v1", True, session="group:771177",
                                    run_id="run-live", visibility="group")
        sm.retract_evidence(eid, "看错了图")
        self.assertEqual([], self.by_store("vision", self.group_ctx())[0]["root_source_ids"])

    def test_a_sensitive_note_stays_private_even_in_a_group(self):
        self.note(sensitive=True)
        got = ro.retrieve_evidence(self.group_ctx())
        self.assertEqual([], [x for x in got["items"] if x["store"] == "vision"])


class ScopeTests(unittest.TestCase):
    """X14：看见不等于可以转述；派生范围只能相同或更窄。"""

    def item(self, session, visibility):
        return ro._item("o1", "belief", "cognition", "一条命题",
                        source_session=session, visibility=visibility)

    def test_all_sources_of_a_derived_item_must_be_authorized(self):
        item=ro._item('mixed','belief','cognition','混合来源',source_session='private:1001',
                      source_sessions=['private:1001','private:2002'],visibility='private')
        self.assertFalse(ro.scope_allows(item,ro.query_context(recipient_scope='private:1001',allowed_source_scopes=['private:1001'])))

    def test_all_group_sources_must_match_the_actual_audience(self):
        item=ro._item('mixed','belief','cognition','跨群来源',source_session='group:7',
                      source_sessions=['group:7','group:8'],visibility='group')
        self.assertFalse(ro.scope_allows(item,ro.query_context(recipient_scope='group:7',allowed_source_scopes=['group:7'])))

    def test_a_private_source_is_never_retold_to_a_group_audience(self):
        ctx = ro.query_context(recipient_scope="group:771177",
                               allowed_source_scopes=["private:1001", "group:771177"])
        self.assertFalse(ro.scope_allows(self.item("private:1001", "private"), ctx))

    def test_a_group_source_reaches_a_private_recipient_only_when_explicitly_allowed(self):
        item = self.item("group:771177", "group")
        narrow = ro.query_context(recipient_scope="private:1001",
                                  allowed_source_scopes=["group:771177"])
        self.assertFalse(ro.scope_allows(item, narrow))
        widened = ro.query_context(recipient_scope="private:1001",
                                   allowed_source_scopes=["group:771177", "private:1001"])
        self.assertTrue(ro.scope_allows(item, widened))

    def test_a_source_outside_the_allowed_scopes_is_not_readable(self):
        ctx = ro.query_context(recipient_scope="group:771177",
                               allowed_source_scopes=["group:771177"])
        self.assertFalse(ro.scope_allows(self.item("group:999", "group"), ctx))

    def test_public_knowledge_needs_no_private_scope_grant(self):
        ctx = ro.query_context(recipient_scope="private:1001",
                               allowed_source_scopes=["private:1001"])
        self.assertTrue(ro.scope_allows(self.item("public:knowledge", "public"), ctx))

    def test_no_allowed_scopes_means_no_private_source(self):
        ctx = ro.query_context(recipient_scope="private:1001", allowed_source_scopes=[])
        self.assertFalse(ro.scope_allows(self.item("private:1001", "private"), ctx))


class HardFilterTests(unittest.TestCase):
    def item(self, **over):
        base = {"source_session": "private:1001", "visibility": "private",
                "occurred_at": 1000.0, "validity": "valid"}
        base.update(over)
        return ro._item("o1", "belief", "cognition", "一条命题", **base)

    def ctx(self, **over):
        base = {"recipient_scope": "private:1001", "allowed_source_scopes": ["private:1001"],
                "as_of_time": NOW}
        base.update(over)
        return ro.query_context(**base)

    def test_evidence_from_the_future_is_dropped(self):
        kept, dropped = ro.hard_filter([self.item(occurred_at=NOW + 60)], self.ctx())
        self.assertEqual([], kept)
        self.assertEqual({"future_evidence": 1}, dropped)

    def test_a_one_second_clock_skew_is_tolerated(self):
        kept, _dropped = ro.hard_filter([self.item(occurred_at=NOW + 0.5)], self.ctx())
        self.assertEqual(1, len(kept))

    def test_time_constraints_are_applied_to_occurrence_not_recording(self):
        items = [self.item(occurred_at=500.0), self.item(occurred_at=1500.0)]
        kept, dropped = ro.hard_filter(items, self.ctx(time_constraints={"not_before": 1000.0}))
        self.assertEqual([1500.0], [x["occurred_at"] for x in kept])
        self.assertEqual({"outside_time_constraints": 1}, dropped)

    def test_not_after_is_applied_too(self):
        kept, dropped = ro.hard_filter([self.item(occurred_at=1500.0)],
                                       self.ctx(time_constraints={"not_after": 1200.0}))
        self.assertEqual([], kept)
        self.assertEqual({"outside_time_constraints": 1}, dropped)

    def test_filtering_does_not_mutate_the_projection(self):
        item = self.item(current=False)
        before = json.dumps(item, sort_keys=True, ensure_ascii=False)
        kept, _dropped = ro.hard_filter([item], self.ctx())
        self.assertEqual(before, json.dumps(item, sort_keys=True, ensure_ascii=False))
        self.assertFalse(kept[0]["current"])


class ResultShapeTests(StoreFixture):
    def test_every_result_has_the_full_contract_shape(self):
        got = ro.retrieve_evidence(self.ctx())
        self.assertEqual({"status", "items", "paths", "missing_support", "conflicts",
                          "degradation", "provenance"}, set(got))
        self.assertIn(got["status"], ro.STATUSES)

    def test_paths_stay_empty_because_edge_projection_is_not_done(self):
        self.belief()
        self.assertEqual([], ro.retrieve_evidence(self.ctx())["paths"])

    def test_conflicting_is_never_claimed_without_the_contradiction_judge(self):
        self.belief(quote="我喜欢冰可乐", text="对方明确自述：我喜欢冰可乐")
        self.belief(quote="我不喜欢冰可乐", text="对方明确自述：我不喜欢冰可乐",
                    session="private:1001")
        got = ro.retrieve_evidence(self.ctx())
        self.assertNotEqual("conflicting", got["status"])
        self.assertEqual([], got["conflicts"])

    def test_the_provenance_says_what_is_not_done(self):
        self.belief()
        limits = " ".join(ro.retrieve_evidence(self.ctx())["provenance"]["limitations"])
        for undone in ("R02", "R03", "R05"):
            self.assertIn(undone, limits)
        self.assertEqual(0, ro.retrieve_evidence(self.ctx())["provenance"]["expanded"])

    def test_a_grounded_single_store_result_is_sufficient(self):
        self.seed_stores()
        self.belief()
        got = ro.retrieve_evidence(self.ctx())
        self.assertEqual("sufficient", got["status"], got["missing_support"])
        self.assertEqual([], got["missing_support"])

    def test_all_stores_present_but_empty_is_not_found(self):
        self.seed_stores()
        got = ro.retrieve_evidence(self.ctx())
        self.assertEqual("not_found", got["status"])
        self.assertEqual(["no_evidence"], got["missing_support"])
        self.assertEqual([], got["degradation"])

    def test_legacy_only_results_are_partial_and_name_the_gap(self):
        self.seed_stores()
        sm.add_belief("旧背景里的一条", source="legacy", is_legacy=True)
        got = ro.retrieve_evidence(self.archive_ctx())
        self.assertEqual("partial", got["status"])
        self.assertIn("verified_provenance", got["missing_support"])
        self.assertIn("original_quote", got["missing_support"])

    def test_nothing_readable_is_not_found_only_when_no_store_degraded(self):
        got = ro.retrieve_evidence(self.ctx())
        self.assertEqual("degraded", got["status"])
        self.assertNotEqual("not_found", got["status"],
                            "库读不到被说成「她没有记忆」")

    def test_an_over_budget_result_reports_the_overflow(self):
        import persona_store as ps
        self.seed_stores()
        eid, _bid = self.belief()
        for index in range(4):
            ps.add("facts", "第%d条事实" % index, source_event_ids=[eid])
        got = ro.retrieve_evidence(self.ctx(retrieval_budget=2))
        self.assertEqual(2, len(got["items"]))
        self.assertIn("over_budget", {d["reason"] for d in got["degradation"]})

    def test_a_zero_budget_returns_nothing_without_pretending_to_be_sufficient(self):
        self.seed_stores()
        self.belief()
        got = ro.retrieve_evidence(self.ctx(retrieval_budget=0))
        self.assertEqual([], got["items"])
        self.assertIn(got["status"], ("degraded", "not_found"))


class StaticLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BOT, "retrieval_objects.py"), encoding="utf-8") as handle:
            cls.source = handle.read()
        cls.tree = ast.parse(cls.source)

    def test_the_adapter_never_writes_anything(self):
        forbidden = {"makedirs", "mkdir", "remove", "unlink", "rmtree", "rename",
                     "executescript", "execute", "commit", "atomic_write_json",
                     "save", "add", "add_belief", "record_event"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden,
                                 "只读适配器里出现写操作 %s" % node.func.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else None
                    for keyword in node.keywords:
                        if keyword.arg == "mode":
                            mode = keyword.value
                    if isinstance(mode, ast.Constant):
                        self.assertNotIn("w", str(mode.value))
                        self.assertNotIn("a", str(mode.value))

    def test_cognition_is_only_read_through_the_authoritative_read_helpers(self):
        allowed = {"retrieval_beliefs", "tool_receipts", "source_window"}
        used = set()
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in ("self_model", "sm")):
                used.add(node.attr)
        self.assertEqual(set(), used - allowed,
                         "绕开 self_model 的只读入口访问认知库：%s" % sorted(used - allowed))

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

    def test_every_declared_store_has_an_adapter(self):
        self.assertEqual(set(ro.STORES), set(ro._ADAPTERS))

    def test_the_result_statuses_and_epistemic_kinds_stay_the_contract_ones(self):
        self.assertEqual(("sufficient", "partial", "conflicting", "not_found", "degraded"),
                         ro.STATUSES)
        self.assertEqual(("experienced", "reported", "looked_up", "inferred", "imagined"),
                         ro.EPISTEMIC_KINDS)

    def test_the_query_fields_are_the_fourteen_of_the_contract(self):
        self.assertEqual(("logical_turn_id", "request_attempt_id", "as_of_time", "query_text",
                          "recent_referents", "speaker_person_id", "topic_person_ids",
                          "recipient_scope", "allowed_source_scopes", "time_constraints",
                          "active_commitment_ids", "retrieval_budget", "index_revision",
                          "validity_epoch"), ro.QUERY_FIELDS)

    def test_only_the_audited_runtime_orchestrator_uses_the_adapter(self):
        """接线只能经统一编排入口，禁止其他模块绕过范围与失效检查。"""
        wanted = {"retrieval_objects", "retrieve_evidence", "fetch_retrieval_items"}
        offenders = []
        for folder in ("bot", "website"):
            for full in sorted((Path(ROOT) / folder).rglob("*.py")):
                if ("__pycache__" in full.parts or
                        full.name in ("retrieval_objects.py", "memory_runtime.py")):
                    continue
                tree = ast.parse(full.read_text(encoding="utf-8", errors="replace"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = {a.name.split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        names = {(node.module or "").split(".")[0]}
                    elif isinstance(node, ast.Call):
                        func = node.func
                        names = {func.id if isinstance(func, ast.Name) else
                                 getattr(func, "attr", "")}
                    else:
                        continue
                    if names & wanted:
                        offenders.append(str(full.relative_to(ROOT)))
                        break
        self.assertEqual([], offenders,
                         "检索必须经 memory_runtime 的权限、失效与证据编排")


if __name__ == "__main__":
    unittest.main()