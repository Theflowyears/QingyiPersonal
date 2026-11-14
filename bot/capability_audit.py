# -*- coding: utf-8 -*-
"""能力五层状态与确定性回放；不把模块存在冒充真实使用。"""
import json
import os
import tempfile
import time
from pathlib import Path

import safe_store

CAPABILITIES=("context_attribution","active_memory","knowledge_boundary","expression_priority")


def _root():return Path(os.getenv("DATA_DIR","./data"))


def _report_path():return _root()/"ops"/"capability_evaluation.json"


def _evaluation():
    try:return json.loads(_report_path().read_text(encoding="utf-8"))
    except (OSError,ValueError):return {}


def binding_digest():
    import hashlib
    import engineering_contracts
    binding=engineering_contracts.binding()
    return hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()


def status():
    import memory_manifest
    manifests=memory_manifest.last(100)
    active=[r for r in manifests if any(x.get("source")=="memory_runtime" and x.get("present") for x in r.get("selected") or [])]
    digest=binding_digest()
    report=_evaluation()
    passed=bool(report and report.get('failed')==0 and report.get('total',0)>0
                and report.get('binding')==digest)
    exercised={
        'active_memory':bool(active),
        'context_attribution':any((r.get('runtime') or {}).get('dialogue_projected') for r in manifests),
        'knowledge_boundary':any(any(x.get('source')=='worldbook' and x.get('present')
                                     for x in r.get('selected') or []) for r in manifests),
        'expression_priority':any((r.get('runtime') or {}).get('expression_source')=='emotion'
                                  for r in manifests),
    }
    try:
        release=json.loads((_root()/'ops'/'capability_release.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        release={}
    rows={name:{'implemented':True,'wired':True,'exercised':exercised[name],
                'evaluated':passed,'released':bool(passed and release.get('binding')==digest
                                                   and release.get('verified') is True)}
          for name in CAPABILITIES}
    world={}
    try:
        import worldbook_store
        active_book=worldbook_store.WorldbookStore().load_active()
        world={"revision":active_book.get("revision"),"entries":len(active_book.get("entries") or []),
               "trust_level":active_book.get("trust_level"),"license_status":active_book.get("license_status")}
    except Exception:pass
    import memory_governance
    return {"ok":True,'governance':memory_governance.inventory(),
            'requests':manifests[-12:],"capabilities":rows,"recent_requests":len(manifests),
            "active_memory_requests":len(active),"evaluation":report,"worldbook":world,
            "definitions":{"implemented":"代码存在","wired":"真实入口调用","exercised":"真实请求使用",
                           "evaluated":"独立回放通过","released":"发布身份绑定确认"}}


def _evaluate_worker():
    import conversation_state as cs
    import claim_check as cc
    import memory_runtime as mr
    import self_model as sm
    cases=[]
    def check(name,expected,actual):
        cases.append({"name":name,"expected":expected,"actual":actual,"passed":expected==actual})
    hot={"seq":1,"event_id":"e1","message_id":"1","user_id":"BOT","is_bot":True,"text":"今天热得离谱","target_uid":"9"}
    sun={"seq":2,"event_id":"e2","message_id":"2","user_id":"9","text":"重庆终于出太阳了","target_uid":"BOT"}
    state=cs.analyze("group:7",[hot,sun],sun,self_id="BOT")
    check("天气命题持有者","BOT",state["claims"][0]["holder_id"])
    check("普通天气不推出嫌热",False,any(c["holder_id"]=="9" and c["predicate"]=="complain_hot" for c in state["claims"]))
    bad=cc.cross_check("出太阳还嫌热，主人晒糊涂了",cs.render_context(state),state)
    check("归属错误被识别",True,"unsupported_claim" in bad["issues"])
    correction=dict(sun,seq=3,event_id="e3",message_id="3",text="那不是你在嫌热吗",reply_to="1")
    corrected=cs.analyze("group:7",[hot,sun,correction],correction,self_id="BOT")
    check("有证据纠错","supported_correction",corrected["correction"]["status"])
    false=dict(sun,text="我没说热",seq=3,event_id="e3",message_id="3")
    disputed=cs.analyze("group:7",[dict(sun,text="今天热得离谱"),false],false,self_id="BOT")
    check("假纠错不盲认","disputed",disputed["correction"]["status"])
    check("无关键词仍生成查询",True,len(mr.build_queries("重庆终于出太阳了"))>=2)
    for idx,text in enumerate(("上次答应我的事呢","昨天我们去哪了","你还记得我喜欢什么吗","以前那次抽卡",
                               "上周说的作业","前天的约定","最近还在忙吗","那次见面之后")):
        check("回忆查询覆盖%d"%idx,True,len(mr.build_queries(text,speaker_id="9"))>=2)
    for idx,(a,b) in enumerate((("group-7","group:7"),("private-user-9","private:9"),
                                ("group:7","group:7"),("private:9","private:9"))):
        check("会话规范化%d"%idx,b,cs.normalize_session(a))
    for idx,text in enumerate(("我什么时候说要逃课","你别乱说","我不是那个意思","你冤枉我")):
        row=dict(sun,seq=10+idx,event_id="c%d"%idx,message_id="c%d"%idx,text=text)
        st=cs.analyze("group:7",[hot,row],row,self_id="BOT")
        check("纠错不会默认正常%d"%idx,True,st["correction"]["status"]!="normal")
    for idx,text in enumerate(("今天吃什么","帮我查天气","这张图挺好笑","游戏更新了")):
        check("普通聊天保留原句%d"%idx,text,mr.build_queries(text)[0]["text"])
    with tempfile.TemporaryDirectory(prefix="capability-eval-") as root:
        old=os.environ.get("DATA_DIR"),os.environ.get("PERSONA_DATA_DIR")
        os.environ["DATA_DIR"]=root;os.environ["PERSONA_DATA_DIR"]=root
        try:
            eid=sm.record_event("private_message",session="private:9",actor_qq="9",visibility="private",
                                payload={"text":"我喜欢冰可乐"},ts=1000,dedupe="eval:ice")
            bid=sm.add_belief("我喜欢冰可乐",subject_qq="9",source="explicit_self_report",ts=1000,
                              spans=[{"event_id":eid,"quote":"我喜欢冰可乐","span_start":0,"span_end":6}])
            for i in range(80):sm.add_belief("无关新记录%d"%i,source="evaluation",ts=2000+i,is_legacy=True)
            ctx=mr.context_for("private:9","冰可乐",speaker_id="9",now=4000000000,budget=4)
            out=mr.recall(ctx,candidate_budget=160)
            check("旧记忆针召回",True,bid in [x["object_id"] for x in out["items"]])
            check("召回真正渲染",True,"冰可乐" in out["text"])
            group=mr.recall(dict(ctx,recipient_scope="group:7",allowed_source_scopes=["group:7"]))
            check("私聊不进群",False,bid in [x["object_id"] for x in group["items"]])
            sm.retract_evidence(eid,"evaluation")
            retracted=mr.recall(ctx)
            check("撤回不复活",False,bid in [x["object_id"] for x in retracted["items"]])
            check("零预算不注入","",mr.recall(dict(ctx,retrieval_budget=0))["text"])
            check("候选上限如实报告",True,"candidate_budget" in out["provenance"])
            import worldbook_store as wb
            data={"version":1,"type":"lorebook","data":{"entries":[{"id":"lily","name":"铃兰","keywords":["铃兰"],"content":"铃兰是明日方舟干员。"}]}}
            release=wb.build_release([("04_干员.json",data)],"evaluation")
            store=wb.WorldbookStore();store.save_release(release);store.activate_revision(release["revision"])
            check("现实称呼不误认干员",[],store.search("铃兰今天开心吗"))
            check("明确作品语境可检索",1,len(store.search("明日方舟 铃兰")))
            check("知识不冒充亲历",True,"社区设定参考" in wb.render_context(store.search("明日方舟 铃兰")))
        finally:
            for key,value in zip(("DATA_DIR","PERSONA_DATA_DIR"),old):
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value
    import dialogue_semantics as ds
    for idx,(reply,verdict) in enumerate((('你嫌热','unsupported'),('你刚才理亏','unsupported'),
                                         ('我接反了','supported'),('你答应过','unsupported'))):
        actual={'response_verdict':verdict}
        checked=ds.validate({'claims':[],'response_verdict':'supported'},state)
        check('语义结果不能自行升级为事实%d'%idx,'proposed',checked['epistemic_status'])
        check('未绑定原文的语义主张不入选%d'%idx,0,len(checked['claims']))
    for idx,(speaker,text,expected) in enumerate((('9','热度很高',[]),('9','好热',['positive_assertion']),
                                                 ('BOT','我好困',['positive_assertion']),('9','我没说热',['negative']))):
        turn=cs.project_turn({'event_id':'p'+str(idx),'user_id':speaker,'is_bot':speaker=='BOT',
                              'text':text},'group:7','BOT')
        check('短句命题极性%d'%idx,expected,[c['polarity'] for c in cs.extract_claims(turn)])
    for idx,text in enumerate(('明天见','我去吃饭','你吃饭了吗','这张图很好笑','明天重庆天气','我在学习')):
        queries=mr.build_queries(text)
        check('普通对话原始查询保留%d'%idx,text,queries[0]['text'])
        check('普通查询不被强制时间过滤%d'%idx,{},queries[0]['time_constraints'])
    check('缺失事件不能形成证据',[],ds.validate({'claims':[{'event_id':'fake','quote':'x','holder_id':'9'}]},state)['claims'])
    check('伪造原文不能形成证据',[],ds.validate({'claims':[{'event_id':'e2','quote':'我嫌热','holder_id':'9'}]},state)['claims'])
    check('模型授权字段被丢弃',False,'authorization' in ds.validate({'authorization':True},state))
    report={"at":time.time(),"total":len(cases),"passed":sum(x["passed"] for x in cases),
            "failed":sum(not x["passed"] for x in cases),"cases":cases,
            "limitations":["确定性结构回放，不替代真实模型盲评","未生成或补造自然样本"]}
    return report


def evaluate():
    import subprocess
    import sys
    with tempfile.TemporaryDirectory(prefix='capability-worker-') as root:
        env = {key: value for key, value in os.environ.items()
               if key in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','LANG')}
        env.update(DATA_DIR=root, PERSONA_DATA_DIR=root, QQ_DRY_RUN='1',
                   TMPDIR=root, TEMP=root, TMP=root,
                   PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        process = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'],
                                 env=env, capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=120)
        if process.returncode:
            raise RuntimeError('capability worker failed: ' + process.stderr[-300:])
        report = json.loads(process.stdout.strip().splitlines()[-1])
    report['binding']=binding_digest()
    safe_store.atomic_write_json(str(_report_path()), report)
    return report


if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('worker invocation required')
    print(json.dumps(_evaluate_worker(), ensure_ascii=False))