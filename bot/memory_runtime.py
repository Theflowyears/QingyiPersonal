# -*- coding: utf-8 -*-
"""统一记忆运行编排：范围过滤先于排序，召回与注入预算分离。"""
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import retrieval_objects
import recall_scoring
import association_edges
import ripple_conflicts
import ripple_evidence
import text_tokens

_TIME_HINTS = (("昨天", 86400, 172800), ("前天", 172800, 259200),
               ("上周", 604800, 1209600), ("最近", 0, 604800))


def build_queries(text, *, speaker_id="", state=None, now=None):
    text = str(text or "").strip()
    now = float(now or time.time())
    queries = [{"kind": "original", "text": text, "time_constraints": {}}]
    if not text:
        return queries
    entities = list((state or {}).get('topic',{}).get('entities') or [])
    if entities:
        queries.append({'kind':'referents','text':' '.join(entities[:8])+' '+text,'time_constraints':{}})
    for token in sorted(text_tokens.tokens(text)):
        if len(token) >= 2 and token not in entities:
            entities.append(token)
    if entities:
        queries.append({"kind":"topic", "text":" ".join(([str(speaker_id)] if speaker_id else [])+entities[:8]),
                        "time_constraints":{}})
    day = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0, second=0, microsecond=0)
    for word, low, high in _TIME_HINTS:
        if word in text:
            if word in ('昨天', '前天'):
                offset = 1 if word == '昨天' else 2
                start = day - timedelta(days=offset)
                end = start + timedelta(days=1)
            elif word == '上周':
                end = day - timedelta(days=day.weekday())
                start = end - timedelta(days=7)
            else:
                start = datetime.fromtimestamp(now-high, ZoneInfo('Asia/Shanghai'))
                end = datetime.fromtimestamp(now-low, ZoneInfo('Asia/Shanghai'))
            queries.append({'kind':'time', 'text':text,
                            'time_constraints':{'not_before':start.timestamp(), 'not_after':end.timestamp()}})
            break
    if re.search(r"还记得|记得吗|上次|以前|那次|答应|承诺", text):
        queries.append({"kind":"explicit_recall", "text":text, "time_constraints":{}})
    return queries


def _query_terms(queries):
    terms = set()
    for query in queries:
        terms.update(text_tokens.tokens(query.get("text", "")))
    return terms


def _item_terms(item):
    parts = [str(item.get("restatement") or "")]
    parts.extend(str(s.get("quote") or "") for s in item.get("evidence_spans") or [])
    return text_tokens.tokens(" ".join(parts))


def _candidate_rank(item, terms):
    overlap = len(terms & _item_terms(item))
    occurred = item.get("occurred_at")
    return (-overlap, -float(occurred or 0), str(item.get("object_id") or ""))


def _prepare_context(ctx, queries, candidate_budget):
    values = dict(ctx or {})
    values["query_text"] = " ".join(q["text"] for q in queries if q.get("text"))
    values["retrieval_budget"] = max(0, int(candidate_budget))
    constraints = [q['time_constraints'] for q in queries if q.get('time_constraints')]
    if constraints:
        values["time_constraints"] = {"not_before":min(c["not_before"] for c in constraints),
                                      "not_after":max(c["not_after"] for c in constraints)}
    return retrieval_objects.query_context(**values)


def context_for(session_id, query, *, speaker_id="", now=None, budget=4):
    import conversation_state
    scope=conversation_state.normalize_session(session_id)
    allowed=[scope]
    return retrieval_objects.query_context(query_text=query,speaker_person_id=str(speaker_id),
                                           recipient_scope=scope,allowed_source_scopes=allowed,
                                           as_of_time=float(now or time.time()),retrieval_budget=budget)


def recall(ctx, *, candidate_budget=80, queries=None, max_expanded=2, max_chars=2400):
    """只读运行 R01→R03→R02→R04/R05→R06，返回可审计材料与请求清单。"""
    started=time.time()
    base=retrieval_objects.query_context(**(ctx or {}))
    queries=queries or build_queries(base["query_text"],speaker_id=base["speaker_person_id"],
                                     now=base["as_of_time"])
    candidate_budget=max(0,int(candidate_budget))
    prepared=_prepare_context(base,queries,candidate_budget)
    problems=retrieval_objects.context_problems(prepared)
    if problems:
        return {"status":"degraded","items":[],"paths":[],"conflicts":[],"scenes":[],
                "manifest":{},"selected":[],"dropped":[],"text":"","degradation":
                [{"store":"query","reason":"invalid_context","detail":",".join(problems)}],
                "provenance":{"pipeline":"R01-R02-R03-R04-R05-R06","queries":queries}}
    fetched=retrieval_objects.fetch_retrieval_items(prepared,candidate_query=prepared['query_text'])
    import self_model
    try:
        events=self_model.retrieval_events(prepared['query_text'],prepared['allowed_source_scopes'],
                                           limit=candidate_budget,as_of_time=prepared['as_of_time'])
        fetched['items'].extend({
            'object_id':'event:'+r['id'],'object_kind':'dialogue_event','store':'event',
            'revision':r['id'],'restatement':r['text'],'source_session':r['session'],
            'source_sessions':[r['session']],'visibility':r['visibility'],
            'scope_basis':'source_event','epistemic_kind':'reported','occurred_at':r['ts'],
            'recorded_at':r['created'],'root_source_ids':[r['id']],
            'evidence_spans':[{'event_id':r['id'],'quote':r['text'],'span_start':0,
                               'span_end':len(r['text']),'source_revision':r['id'],'match_kind':'exact'}],
            'validity':'valid','legacy':False,'current':True,
            'match':{'clues':['history'],'score_parts':{}}} for r in events)
    except Exception as exc:
        fetched['degradation'].append({'store':'event','reason':'unreadable','detail':type(exc).__name__})
    kept,filtered=retrieval_objects.hard_filter(fetched["items"],prepared)
    terms=_query_terms(queries)
    ranked_candidates=sorted(kept,key=lambda item:_candidate_rank(item,terms))
    candidates=ranked_candidates[:candidate_budget]
    truncated=len(ranked_candidates)>len(candidates)
    seed_ctx=dict(prepared,retrieval_budget=max(0,int(base["retrieval_budget"])))
    relevant=[item for item in candidates if terms & _item_terms(item)]
    ranked=recall_scoring.recall(relevant,seed_ctx)
    seeds=ranked['items']
    edges=association_edges.project_edges(candidates)
    edge_rows=edges.get("edges",edges) if isinstance(edges,dict) else edges
    resolved=ripple_conflicts.resolve_ripple(seeds,edge_rows,items=candidates,
                                             degradation=fetched["degradation"],
                                             max_expanded=max_expanded)
    rendered=ripple_evidence.render_evidence(resolved,items=candidates,seeds=seeds,
                                             context={"logical_turn_id":base["logical_turn_id"],
                                                      "request_attempt_id":base["request_attempt_id"]},
                                             max_entries=seed_ctx["retrieval_budget"])
    selected=[]; lines=[]; blocks=[]
    candidate_map={item['object_id']:item for item in candidates}
    for scene in rendered.get("scenes") or []:
        entries=scene.get("entries") or []
        for entry in entries:
            text=str(entry.get("restatement") or "").strip()
            if not text:
                continue
            kind=str(entry.get("epistemic_kind") or "inferred")
            label={"experienced":"亲历","reported":"对方说过","looked_up":"查到的资料",
                   "inferred":"尚未证实的旧记录","imagined":"想象材料"}.get(kind,kind)
            block='- [%s] %s' % (label,text)
            blocks.append({'id':entry['object_id'],'text':block,'score':1})
            selected.append({"id":entry["object_id"],"kind":kind,"text":text,
                             "source":"memory_runtime","score":1,
                             'source_session':candidate_map.get(entry['object_id'],{}).get('source_session',''),
                             'visibility':candidate_map.get(entry['object_id'],{}).get('visibility',''),
                             "root_source_ids":entry.get("root_source_ids") or []})
    import worldbook_store
    world_rows=worldbook_store.WorldbookStore().search(base["query_text"],limit=2)
    world_text=worldbook_store.render_context(world_rows)
    if world_text and seed_ctx["retrieval_budget"]>0:
        blocks.append({'id':'worldbook:'+world_rows[0]['knowledge_id'],'text':world_text,'score':1})
        selected.extend({"id":r["knowledge_id"],"kind":"looked_up","text":r["content"],
                         "source":"worldbook","score":1,"source_session":"public:knowledge",
                         "visibility":"public","root_source_ids":[r["source_commit"]]}
                        for r in world_rows)
    header='\n\n【本轮主动召回的真实材料】\n'
    footer='\n只在相关时自然使用；未证实记录不能当亲历，冲突与缺失不能补造。'
    import memory_manifest
    kept_blocks,drops=memory_manifest.compose_budget(blocks,max(0,int(max_chars))-len(header)-len(footer))
    kept_ids={x['id'] for x in kept_blocks}
    selected=[x for x in selected if x['id'] in kept_ids or (x['source']=='worldbook' and any(y['id'].startswith('worldbook:') for y in kept_blocks))]
    body=header+'\n'.join(x['text'] for x in kept_blocks)+footer if kept_blocks else ''
    provenance=dict(rendered.get("provenance") or {})
    provenance.update({"pipeline":"R01-R02-R03-R04-R05-R06","queries":queries,
                       "candidate_budget":candidate_budget,"candidate_count":len(kept),
                       "candidate_truncated":truncated,"filtered":filtered,
                       "recall":ranked["provenance"],"seconds":round(time.time()-started,4)})
    degradation=list(rendered.get("degradation") or [])
    if truncated:
        degradation.append({"store":"candidate","reason":"truncated",
                            "detail":"%d/%d"%(len(candidates),len(kept))})
    status=rendered.get("status","not_found")
    if degradation:
        status="degraded"
    return {"status":status,"items":seeds,"paths":rendered.get("paths",resolved.get("paths",[])),
            "conflicts":resolved.get("conflicts",[]),"scenes":rendered.get("scenes",[]),
            "manifest":rendered.get("manifest",{}),"selected":selected,"dropped":drops,
            "text":body,"degradation":degradation,"provenance":provenance}


def recall_for_turn(session_id,text,*,speaker_id="",self_id="",state=None,now=None,
                    explicit=False,budget=None):
    now=float(now or time.time())
    queries=build_queries(text,speaker_id=speaker_id,state=state,now=now)
    ctx=context_for(session_id,text,speaker_id=speaker_id,now=now,
                    budget=budget if budget is not None else (8 if explicit else 4))
    return recall(ctx,candidate_budget=160 if explicit else 80,queries=queries,
                  max_expanded=4 if explicit else 2)