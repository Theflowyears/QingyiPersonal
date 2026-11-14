# -*- coding: utf-8 -*-
"""多人归属、引用链、纠错与会话隔离的行为回归。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bot"))

import conversation_state as cs
import claim_check as cc
import memory_depth as md
import conversation_plan as cp


def row(seq, actor, text, *, bot=False, reply=None, target=None):
    return {"seq": seq, "message_id": str(100 + seq), "event_id": "evt%d" % seq,
            "ts": 1000 + seq, "user_id": "BOT" if bot else actor,
            "nickname": "清漪" if bot else actor, "is_bot": bot,
            "text": text, "reply_to": str(100 + reply) if reply else None,
            "target_uid": target or "", "at_targets": []}


class ProjectionTests(unittest.TestCase):
    def test_session_aliases_resolve_to_the_same_scope(self):
        self.assertEqual(cs.normalize_session("group-7"), "group:7")
        self.assertEqual(cs.normalize_session("group:7"), "group:7")
        self.assertEqual(cs.normalize_session("private-user-9"), "private:9")

    def test_projection_keeps_speaker_addressee_and_source_separate(self):
        turn = row(2, "9", "你刚才说热", reply=1, target="BOT")
        old = row(1, "BOT", "今天热得离谱", bot=True, target="9")
        state = cs.analyze("group:7", [old, turn], turn, self_id="BOT")
        self.assertEqual(state["turn"]["speaker_id"], "9")
        self.assertEqual(state["turn"]["addressee_ids"], ["BOT"])
        self.assertEqual(state["turn"]["reply_to_event_id"], "evt1")
        self.assertEqual(state["turn"]["speaker_role"], "human")
        self.assertEqual(state["turn"]["source_kind"], "reported")
        self.assertEqual(state["referenced_events"][0]["speaker_id"], "BOT")

    def test_weather_claims_keep_their_original_holder(self):
        rows = [row(1, "BOT", "今天这天气热得离谱，你们那边还好吗", bot=True, target="9"),
                row(2, "9", "重庆今天好不容易出太阳了", target="BOT")]
        state = cs.analyze("group:7", rows, rows[-1], self_id="BOT")
        holders = [(c["holder_id"], c["predicate"]) for c in state["claims"]]
        self.assertIn(("BOT", "complain_hot"), holders)
        self.assertIn(("9", "sunny"), holders)
        self.assertNotIn(("9", "complain_hot"), holders)

    def test_correction_of_a_bot_claim_is_evidence_backed(self):
        rows = [row(1, "BOT", "今天热得离谱", bot=True, target="9"),
                row(2, "9", "重庆好不容易出太阳了", target="BOT"),
                row(3, "BOT", "出太阳还嫌热，主人晒糊涂了", bot=True, target="9"),
                row(4, "9", "那不是你在嫌热吗", reply=3, target="BOT")]
        state = cs.analyze("group:7", rows, rows[-1], self_id="BOT")
        self.assertEqual(state["correction"]["status"], "supported_correction")
        self.assertEqual(state["correction"]["holder_id"], "BOT")
        self.assertEqual(state["correction"]["evidence_event_id"], "evt1")

    def test_false_correction_is_not_blindly_accepted(self):
        rows = [row(1, "9", "今天热得离谱", target="BOT"),
                row(2, "9", "我没说热", target="BOT")]
        state = cs.analyze("group:7", rows, rows[-1], self_id="BOT")
        self.assertEqual(state["correction"]["status"], "disputed")

    def test_unrelated_correction_is_ambiguous_not_auto_agreement(self):
        rows = [row(1, "BOT", "今天吃什么", bot=True, target="9"),
                row(2, "9", "我没说那个", target="BOT")]
        state = cs.analyze("group:7", rows, rows[-1], self_id="BOT")
        self.assertEqual(state["correction"]["status"], "ambiguous")

    def test_context_keeps_reply_chain_when_topic_window_would_drop_it(self):
        rows = [row(1, "BOT", "旧话题的原句", bot=True, target="9")]
        rows += [row(i, str(i), "别的话题%d" % i) for i in range(2, 15)]
        rows.append(row(15, "9", "你刚才说什么", reply=1, target="BOT"))
        packed = cs.pack_context(cs.analyze("group:7", rows, rows[-1], self_id="BOT"),
                                 max_turns=4)
        self.assertIn("evt1", [x["event_id"] for x in packed])
        self.assertIn("evt15", [x["event_id"] for x in packed])

    def test_discussing_popularity_is_not_a_temperature_complaint(self):
        turn=cs.project_turn(row(1,'9','这个角色热度很高'),'group:7','BOT')
        self.assertNotIn('complain_hot',[c['predicate'] for c in cs.extract_claims(turn)])

    def test_quoting_someone_else_does_not_transfer_their_feeling(self):
        rows=[row(1,'9','我好困',target='BOT'),
              row(2,'BOT','你刚才说好困',bot=True,target='9')]
        state=cs.analyze('group:7',rows,rows[-1],self_id='BOT')
        claim=next(c for c in state['claims'] if c['source_event_id']=='evt2')
        self.assertEqual('9',claim['holder_id'])
        self.assertEqual('quoted',claim['modality'])

    def test_reply_chain_resolves_multiple_levels(self):
        rows=[row(1,'9','老话题'),row(2,'8','接上一条',reply=1),row(3,'9','再接',reply=2)]
        state=cs.analyze('group:7',rows,rows[-1],self_id='BOT')
        self.assertEqual(['evt2','evt1'],[r['event_id'] for r in state['referenced_events']])

    def test_nonweather_correction_support_does_not_invent_weather(self):
        rows=[row(1,'BOT','我好困',bot=True,target='9'),row(2,'9','不是你刚才说好困吗',target='BOT')]
        state=cs.analyze('group:7',rows,rows[-1],self_id='BOT')
        self.assertEqual('supported_correction',state['correction']['status'])
        self.assertNotIn('嫌热',cs.render_support(state))

    def test_unconfirmed_assistant_candidate_is_not_context_evidence(self):
        bad=dict(row(1,'BOT','今天好热',bot=True),delivery_status='failed')
        target=row(2,'9','你刚才说什么')
        state=cs.analyze('group:7',[bad,target],target,self_id='BOT')
        self.assertEqual([],state['claims'])
        self.assertNotIn('evt1',[x['event_id'] for x in cs.pack_context(state)])

    def test_same_topic_keeps_stable_topic_identity(self):
        rows=[row(1,'9','玛恩纳技能怎么用'),row(2,'8','玛恩纳的技能范围很大'),row(3,'9','玛恩纳技能持续多久')]
        first=cs.analyze('group:7',rows[:2],rows[1],self_id='BOT')
        second=cs.analyze('group:7',rows,rows[2],self_id='BOT')
        self.assertEqual(first['topic']['topic_id'],second['topic']['topic_id'])
        self.assertTrue(any('玛恩纳' in x for x in second['topic']['entities']))

    def test_unrelated_topic_is_not_packed_in_place_of_referenced_turns(self):
        rows=[row(1,'9','玛恩纳技能怎么用'),row(2,'8','晚饭吃炒饭'),row(3,'9','玛恩纳的技能范围呢',reply=1)]
        state=cs.analyze('group:7',rows,rows[-1],self_id='BOT')
        packed=cs.pack_context(state,max_turns=2)
        self.assertNotIn('evt2',[r['event_id'] for r in packed])

    def test_plan_keeps_facts_above_tsundere_style(self):
        rows = [row(1, "BOT", "热得离谱", bot=True, target="9"),
                row(2, "9", "那不是你在嫌热吗", target="BOT")]
        state = cs.analyze("group:7", rows, rows[-1], self_id="BOT")
        plan = cp.plan(rows[-1]["text"], scene="group", dialogue_state=state)
        text = cp.render(plan)
        self.assertIn("事实", text)
        self.assertIn("纠错", text)
        self.assertIn("不能继续指责", text)


class AttributionGuardTests(unittest.TestCase):
    CONTEXT = ("[14:37:45] 清漪（你）: 今天这天气热得离谱\n"
               "[14:39:39] 主人: 重庆今天好不容易出太阳了")

    def test_tuple_context_is_not_silently_treated_as_empty(self):
        context = (self.CONTEXT, 2, 2, 1, [])
        old = "[19:25:27] 甲: 一键跟随的小朋友\n[19:25:28] 乙: 我还在路上"
        verdict = cc.cross_check("乙那个一键跟随的小朋友", (old, 2, 2, 2, []))
        self.assertEqual(verdict["softened"], ["乙"])

    def test_hot_complaint_is_not_transferred_to_the_human(self):
        verdict = cc.cross_check("出太阳还嫌热，笨蛋主人是晒糊涂了", self.CONTEXT)
        self.assertIn("unsupported_claim", verdict["issues"])
        self.assertNotEqual(verdict["text"], "出太阳还嫌热，笨蛋主人是晒糊涂了")

    def test_long_context_identity_suffix_does_not_hide_attribution(self):
        context=cs.render_context(cs.analyze('group:7',[row(1,'BOT','今天热得离谱',bot=True,target='9'),
            row(2,'9','重庆出太阳了',target='BOT')],row(2,'9','重庆出太阳了'),self_id='BOT'))
        self.assertIn('unsupported_claim',cc.cross_check('出太阳还嫌热，笨蛋主人是晒糊涂了',context)['issues'])

    def test_valid_correction_does_not_trigger_another_regeneration(self):
        state={'correction':{'status':'supported_correction'}}
        verdict=cc.cross_check('对哦，是我接反了。',self.CONTEXT,state)
        self.assertEqual([],verdict['issues'])

    def test_self_admission_of_heat_is_not_stripped(self):
        reply='是我刚才在嫌热，你说的是放晴。'
        self.assertEqual(reply,cc.cross_check(reply,self.CONTEXT)['text'])

    def test_correct_response_to_sunshine_is_untouched(self):
        reply = "重庆终于放晴啦？今天可算能晒晒太阳了。"
        self.assertEqual(cc.cross_check(reply, self.CONTEXT)["text"], reply)


class BridgeWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_prompt_receives_structured_state_and_active_memory(self):
        import qq_openclaw_bridge as b
        from collections import deque
        from unittest.mock import AsyncMock
        key="group-7"
        rows=[row(1,"BOT","今天热得离谱",bot=True,target="9"),
              row(2,"9","重庆今天好不容易出太阳了",target="BOT")]
        seen=[]
        async def model(messages,**kw):
            seen.append(messages); return "重庆终于放晴啦。"
        def spawn(coro): coro.close()
        for d in (b.histories,b._PENDING_DELIVERIES,b.FLIRT_BURST,b.STICKER_WISH):
            self.addCleanup(d.pop,key,None)
        b.group_message_cache["7"]=deque(rows,maxlen=250)
        self.addCleanup(b.group_message_cache.pop,"7",None)
        import persona_store
        def close_store():
            if persona_store._conn is not None:
                persona_store._conn.close(); persona_store._conn=None; persona_store._conn_path=""
        self.addCleanup(close_store)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root, mock.patch.dict(
                os.environ,{"DATA_DIR":root,"PERSONA_DATA_DIR":root}), \
             mock.patch.object(b,"call_openclaw",side_effect=model), \
             mock.patch.object(b,"get_persona_context",return_value=""), \
             mock.patch.object(b,"save_histories"),mock.patch.object(b,"_mark_talk"), \
             mock.patch.object(b,"_fatigue_touch"),mock.patch.object(b,"_spawn",side_effect=spawn), \
             mock.patch.object(b,"STICKER_PROB",0), \
             mock.patch.object(b,"_hold_bubble",new=AsyncMock(return_value=False)), \
             mock.patch("memory_runtime.recall_for_turn",return_value={"text":"\n【主动召回测试材料】","selected":[],"status":"sufficient","provenance":{}}):
            b.histories[key]=deque(maxlen=24)
            await b.ask_openclaw(key,rows[-1]["text"],group_id="7",speaker_id="9",speaker_nick="主人")
        blob="\n".join(str(m.get("content")) for m in seen[0])
        self.assertIn("嫌热是清漪先前的表达",blob)
        self.assertIn("主动召回测试材料",blob)
        self.assertIn("event=evt1",blob)


class SemanticBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_repair_is_rechecked_before_return(self):
        import qq_openclaw_bridge as b
        import dialogue_semantics as ds
        from unittest.mock import AsyncMock
        state={'turn':{'event_id':'e1','speaker_id':'9','text':'放晴了'},'turns':[],
               'correction':{'status':'supported_correction'}}
        import persona_store
        def close_store():
            if persona_store._conn is not None:
                persona_store._conn.close();persona_store._conn=None;persona_store._conn_path=''
        self.addCleanup(close_store)
        self.addCleanup(b.histories.pop,'group-7',None)
        results=iter([{'response_verdict':'unsupported'},{'response_verdict':'supported'}])
        async def review(*args):return next(results)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root,mock.patch.dict(os.environ,{'DATA_DIR':root,'PERSONA_DATA_DIR':root}), \
             mock.patch.object(b,'call_openclaw',new=AsyncMock(side_effect=['你嫌热','对，是我接反了'])), \
             mock.patch.object(ds,'review',side_effect=review), \
             mock.patch.object(b,'get_persona_context',return_value=''), \
             mock.patch.object(b,'save_histories'),mock.patch.object(b,'_spawn'), \
             mock.patch.object(b,'_hold_bubble',new=AsyncMock(return_value=False)), \
             mock.patch.object(b,'STICKER_PROB',0), \
             mock.patch.object(b,'_mark_talk'), \
             mock.patch('conversation_state.analyze',return_value=state), \
             mock.patch('memory_runtime.recall_for_turn',return_value={'text':'','selected':[],'status':'not_found','provenance':{}}):
            b.histories['group-7']=[]
            out=await b.ask_openclaw('group-7','放晴了',group_id='7',speaker_id='9')
        self.assertEqual('对，是我接反了',out)

class JournalIsolationTests(unittest.TestCase):
    def test_missing_group_history_never_falls_back_to_private_history(self):
        rows = [{"session": "private-user-9", "user": "私聊专属内容", "bot": "私聊回应"}]
        with mock.patch("persona_growth.read_new_journal", return_value=rows):
            self.assertEqual(md._journal_recent(6, "group-7"), [])
            self.assertEqual(md._shallow_block("太阳", "group-7"), "")

    def test_group_alias_reads_the_same_normalized_history(self):
        rows = [{"session": "group:7", "speaker_id": "9", "speaker_name": "主人",
                 "user": "出太阳了", "bot": "那挺好"}]
        with mock.patch("persona_growth.read_new_journal", return_value=rows):
            got = md._journal_recent(6, "group-7")
            shallow = md._shallow_block("太阳", "group-7")
        self.assertEqual(got[0]["user"], "出太阳了")
        self.assertIn("主人", shallow)
        self.assertNotIn("他：出太阳了", shallow)

    def test_journal_keeps_the_actual_speaker_and_message_evidence(self):
        import persona_growth as pg
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
                os.environ, {"DATA_DIR": root, "PERSONA_DATA_DIR": root}):
            pg.note_exchange("group-7", "出太阳了", "那挺好", events=["evt2"],
                             speaker_id="9", speaker_name="主人", target_uid="BOT",
                             message_event_id="evt2")
            got = pg.read_new_journal(0)
        self.assertEqual(got[-1]["speaker_id"], "9")
        self.assertEqual(got[-1]["target_uid"], "BOT")
        self.assertEqual(got[-1]["message_event_id"], "evt2")


if __name__ == "__main__":
    unittest.main()