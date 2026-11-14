# -*- coding: utf-8 -*-
"""真实记忆运行编排：权限先行、旧记忆可召回、Ripple 真正接通。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))
import memory_runtime as mr
import retrieval_objects as ro
import self_model as sm


class MemoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memory_runtime_")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp.name,
                                           "PERSONA_DATA_DIR": self.tmp.name,
                                           "QQ_DRY_RUN": "1"})
        env.start(); self.addCleanup(env.stop)

    def ctx(self, query="我喜欢冰可乐", **kw):
        values = dict(query_text=query, recipient_scope="private:9",
                      allowed_source_scopes=["private:9"], speaker_person_id="9",
                      as_of_time=4000000000, retrieval_budget=4)
        values.update(kw)
        return ro.query_context(**values)

    def belief(self, text="我喜欢冰可乐", ts=1000):
        eid = sm.record_event("private_message", session="private:9", actor_qq="9",
                              visibility="private", payload={"text": text}, ts=ts,
                              dedupe="runtime:"+text)
        bid = sm.add_belief(text, subject_qq="9", source="explicit_self_report", ts=ts,
                            spans=[{"event_id":eid,"quote":text,"span_start":0,
                                    "span_end":len(text),"match_kind":"model_quote"}])
        return eid,bid

    def test_old_relevant_memory_survives_a_thousand_new_irrelevant_rows(self):
        eid,bid=self.belief("我最喜欢冰可乐",1000)
        for i in range(1000):
            sm.add_belief("无关新记录%d" % i,source="test",ts=2000+i,is_legacy=True)
        out=mr.recall(self.ctx("冰可乐"),candidate_budget=1200)
        self.assertIn(bid,[x["object_id"] for x in out["items"]])
        self.assertFalse(out["provenance"]["candidate_truncated"])

    def test_candidate_search_does_not_read_only_the_newest_final_budget_rows(self):
        _,bid=self.belief("很早以前提过冰可乐",1000)
        for i in range(40):
            sm.add_belief("后来无关记录%d" % i,source="test",ts=2000+i,is_legacy=True)
        with mock.patch.object(sm,"retrieval_beliefs",wraps=sm.retrieval_beliefs) as reader:
            out=mr.recall(self.ctx("冰可乐"),candidate_budget=80)
        self.assertTrue(any(call.kwargs.get("query") for call in reader.call_args_list))
        self.assertIn(bid,[x["object_id"] for x in out["items"]])

    def test_group_cannot_recall_private_memory(self):
        _,bid=self.belief("私聊专属偏好")
        out=mr.recall(self.ctx("专属偏好",recipient_scope="group:7",
                               allowed_source_scopes=["group:7"]))
        self.assertNotIn(bid,[x["object_id"] for x in out["items"]])
        self.assertTrue(out["provenance"]["filtered"].get("out_of_scope"))

    def test_retracted_memory_cannot_be_revived(self):
        eid,bid=self.belief("撤回前喜欢冰可乐")
        sm.retract_evidence(eid,"用户撤回")
        out=mr.recall(self.ctx("冰可乐"))
        self.assertNotIn(bid,[x["object_id"] for x in out["items"]])

    def test_every_turn_builds_queries_without_memory_keywords(self):
        queries=mr.build_queries("重庆终于出太阳了",speaker_id="9")
        self.assertEqual(queries[0]["kind"],"original")
        self.assertEqual(queries[0]["text"],"重庆终于出太阳了")
        self.assertTrue(any(q["kind"]=="topic" for q in queries))

    def test_pipeline_returns_evidence_and_real_rendered_material(self):
        eid,bid=self.belief("我喜欢冰可乐")
        out=mr.recall(self.ctx("冰可乐"))
        self.assertIn("scenes",out)
        self.assertIn("manifest",out)
        self.assertIn("冰可乐",out["text"])
        self.assertIn(bid,[x["id"] for x in out["selected"]])
        self.assertEqual(out["provenance"]["pipeline"],"R01-R02-R03-R04-R05-R06")

    def test_worldbook_material_is_kept_separate_from_autobiographical_memory(self):
        import worldbook_store as wb
        data={"version":1,"type":"lorebook","data":{"entries":[{"id":"rhodes","name":"罗德岛",
              "keywords":["罗德岛"],"content":"罗德岛是明日方舟中的医药组织。"}]}}
        release=wb.build_release([("03_组织.json",data)],"commit")
        store=wb.WorldbookStore();store.save_release(release);store.activate_revision(release["revision"])
        out=mr.recall(self.ctx("明日方舟罗德岛是什么",recipient_scope="group:7",allowed_source_scopes=["group:7"]))
        self.assertIn("社区设定参考",out["text"])
        self.assertTrue(all(x["kind"]!="experienced" for x in out["selected"]))

    def test_unrelated_memory_is_not_injected_just_because_it_is_recent(self):
        self.belief('我喜欢冰可乐')
        out=mr.recall(self.ctx('今天研究天文学'))
        self.assertEqual('',out['text'])

    def test_explicit_recall_can_find_old_content_beyond_recent_candidate_limit(self):
        _,bid=self.belief('我以前住在重庆',1000)
        for i in range(100):
            self.belief('后来的无关经历%d'%i,2000+i)
        out=mr.recall(self.ctx('还记得我以前住在重庆吗'),candidate_budget=40)
        self.assertIn(bid,[x['object_id'] for x in out['items']])

    def test_time_query_is_a_calendar_day_not_a_rolling_interval(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,11,14,17,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        query=next(q for q in mr.build_queries('昨天说的话',now=now) if q['kind']=='time')
        self.assertEqual(datetime(2026,11,13,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp(),query['time_constraints']['not_before'])
        self.assertEqual(datetime(2026,11,14,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp(),query['time_constraints']['not_after'])

    def test_corrupt_cognition_store_is_reported_unreadable_not_empty(self):
        from pathlib import Path
        Path(self.tmp.name,'cognition.db').write_bytes(b'not a sqlite database')
        out=mr.recall(self.ctx('冰可乐'))
        self.assertTrue(any(d.get('store')=='cognition' and d.get('reason')=='unreadable' for d in out['degradation']))

    def test_verification_events_cannot_support_runtime_memory(self):
        eid=sm.record_event('private_message',session='private:9',actor_qq='9',visibility='private',
                            payload={'text':'我喜欢冰可乐','synthetic':True},ts=1000,dedupe='synthetic')
        bid=sm.add_belief('我喜欢冰可乐',subject_qq='9',ts=1000,
                         spans=[{'event_id':eid,'quote':'我喜欢冰可乐','span_start':0,'span_end':6}])
        out=mr.recall(self.ctx('冰可乐'))
        self.assertNotIn(bid,[x['object_id'] for x in out['items']])

    def test_original_history_can_be_recalled_without_a_promoted_belief(self):
        eid=sm.record_event('private_message',session='private:9',actor_qq='9',visibility='private',
                            payload={'text':'上次我们一起讨论过玛恩纳的技能'},ts=1000,dedupe='raw-history')
        out=mr.recall(self.ctx('以前讨论玛恩纳技能的那次'))
        self.assertTrue(any(eid in x.get('root_source_ids',[]) for x in out['selected']))
        self.assertIn('玛恩纳',out['text'])

    def test_sender_is_not_permission_to_read_another_private_archive(self):
        ctx=mr.context_for('private:9','以前的事',speaker_id='9')
        self.assertEqual(['private:9'],ctx['allowed_source_scopes'])

    def test_query_context_uses_current_topic_for_implicit_references(self):
        state={'turn':{'text':'它的技能呢'},'topic':{'entities':['玛恩纳']}}
        queries=mr.build_queries('它的技能呢',state=state)
        self.assertTrue(any('玛恩纳' in q['text'] for q in queries))

    def test_character_budget_drops_whole_entries_and_records_reason(self):
        self.belief('我喜欢冰可乐还有一段很长的真实偏好说明'*20)
        ctx=self.ctx('冰可乐')
        out=mr.recall(ctx,max_chars=100)
        self.assertLessEqual(len(out['text']),100)
        self.assertTrue(any(x['reason']=='budget' for x in out['dropped']))

    def test_zero_budget_means_no_injection(self):
        self.belief()
        out=mr.recall(self.ctx(retrieval_budget=0))
        self.assertEqual(out["text"],"")
        self.assertEqual(out["items"],[])

    def test_missing_store_is_degraded_not_no_memory(self):
        out=mr.recall(self.ctx())
        self.assertEqual(out["status"],"degraded")
        self.assertTrue(out["degradation"])


if __name__=="__main__":
    unittest.main()