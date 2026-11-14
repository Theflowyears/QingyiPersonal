# -*- coding: utf-8 -*-
"""从真实对话事件投影话题、指代与命题归属；不写入人格或认知账本。"""
import re
import time

_CORRECTION = re.compile(
    r"我没(?:说|讲|要|有|让|叫)|我什么时候(?:说|讲|要)|别乱说|谁说的"
    r"|你(?:冤枉|错怪|误会)我|我不是那个意思|我明明(?:只是|就是|在|是)"
    r"|那不是(?:你|清漪)|不是你(?:刚才|刚刚)?(?:说|讲)|你刚才不是说|你在嫌")
_CLAIMS = (
    ("complain_hot", re.compile(r"热得离谱|嫌热|好热|太热了|热死了|(?<=说)热(?!度|门|点)|(?<=讲)热(?!度|门|点)"), "很热"),
    ("sunny", re.compile(r"出太阳|放晴|天晴"), "出太阳"),
    ("complain_sun", re.compile(r"嫌晒|讨厌太阳|晒得烦|太阳好烦"), "嫌晒"),
    ("tired", re.compile(r"好累|累死了"), "很累"),
    ("sleepy", re.compile(r"好困|困死了"), "很困"),
)
_DENIAL = re.compile(r"我没(?:说|讲|要)|我什么时候(?:说|讲|要)|我没嫌|我没说热")


def normalize_session(value):
    text = str(value or "").strip()
    if text.startswith("private-user-"):
        return "private:" + text[len("private-user-"):]
    if text.startswith("group-"):
        return "group:" + text[len("group-"):]
    return text


def project_turn(row, session_id="", self_id=""):
    row = row if isinstance(row, dict) else {}
    bot = bool(row.get("is_bot")) or str(row.get("user_id") or "") == str(self_id)
    targets = [str(x) for x in (row.get("at_targets") or []) if str(x or "")]
    target = str(row.get("target_uid") or row.get("reply_to_user") or "")
    if target and target not in targets:
        targets.append(target)
    event_id = str(row.get("event_id") or row.get("message_id") or row.get("seq") or "")
    return {
        "event_id": event_id, "message_id": str(row.get("message_id") or ""),
        "session_id": normalize_session(session_id),
        "speaker_id": str(self_id or "BOT") if bot else str(row.get("user_id") or ""),
        "speaker_role": "assistant" if bot else "human",
        "speaker_name": str(row.get("nickname") or row.get("speaker_name") or ""),
        "occurred_at": row.get("ts"), "recorded_at": row.get("recorded_at") or row.get("ts"),
        "reply_to_message_id": str(row.get("reply_to") or ""),
        "reply_to_event_id": "", "addressee_ids": targets,
        "subject_ids": [], "text": str(row.get("text") or ""),
        "visibility": "group" if normalize_session(session_id).startswith("group:") else "private",
        "source_kind": "experienced" if bot else "reported",
        "delivery_status": row.get("delivery_status") or ("accepted" if bot else None),
        "seq": row.get("seq"),
    }


def extract_claims(turn):
    claims = []
    text = turn.get("text", "")
    for predicate, pattern, value in _CLAIMS:
        match = pattern.search(text)
        if not match:
            continue
        holder = turn['speaker_id']
        quoted = bool(re.search(r'你(?:刚才|刚刚|之前)?(?:说|讲)|你在', text))
        if quoted and len(turn.get('addressee_ids') or []) == 1:
            holder = turn['addressee_ids'][0]
        claims.append({"claim_id": "%s:%s" % (turn["event_id"], predicate),
                       "source_event_id": turn["event_id"], "holder_id": holder,
                       "subject_id": "", "predicate": predicate, "value": value,
                       "polarity": "negative" if _DENIAL.search(text) else "positive_assertion",
                       "modality": "quoted" if quoted else "uttered", "resolution_status": "supported_as_utterance",
                       "quote_span": {"start": match.start(), "end": match.end(),
                                      "text": match.group(0)}})
    return claims


def _topics(turns, session_id):
    import text_tokens
    topics=[]
    assignments={}
    for turn in turns:
        terms=set(x for x in text_tokens.tokens(turn['text']) if len(x)>=2)
        reply_topic=assignments.get(turn.get('reply_to_event_id'))
        ranked=[]
        for topic in topics:
            overlap=len(terms & topic['terms']) / max(1,min(len(terms),len(topic['terms'])))
            elapsed=max(0,float(turn.get('occurred_at') or 0)-float(topic.get('last_active_at') or 0))
            if reply_topic==topic['topic_id'] or (overlap>=0.25 and elapsed<=1800):
                ranked.append((5 if reply_topic==topic['topic_id'] else 2*overlap,topic))
        if ranked:
            topic=max(ranked,key=lambda x:x[0])[1]
        else:
            topic={'topic_id':'topic:'+session_id+':'+turn['event_id'],'terms':set(),
                   'event_ids':[],'participant_ids':[],'last_active_at':0}
            topics.append(topic)
        topic['terms'].update(terms)
        topic['event_ids'].append(turn['event_id'])
        if turn['speaker_id'] not in topic['participant_ids']:
            topic['participant_ids'].append(turn['speaker_id'])
        topic['last_active_at']=turn.get('occurred_at') or 0
        assignments[turn['event_id']]=topic['topic_id']
    return topics, assignments


def analyze(session_id, rows, target, *, self_id="", now=None):
    session_id = normalize_session(session_id)
    rows = list(rows or [])
    turns = [project_turn(r, session_id, self_id) for r in rows]
    turns = [r for r in turns if r['speaker_role'] != 'assistant' or r['delivery_status'] == 'accepted']
    by_message = {r['message_id']: r for r in turns if r['message_id']}
    for turn in turns:
        source = by_message.get(turn['reply_to_message_id'])
        if source:
            turn['reply_to_event_id'] = source['event_id']
            if not turn['addressee_ids']:
                turn['addressee_ids'] = [source['speaker_id']]
    by_event = {r['event_id']: r for r in turns if r['event_id']}
    current = project_turn(target, session_id, self_id)
    current = by_event.get(current['event_id'], current)
    if current["event_id"] not in by_event:
        turns.append(current)
        by_event[current["event_id"]] = current
    if current["reply_to_message_id"] in by_message:
        current["reply_to_event_id"] = by_message[current["reply_to_message_id"]]["event_id"]
    claims = [c for r in turns for c in extract_claims(r)]
    referenced = []
    cursor = current
    seen = set()
    while cursor.get("reply_to_event_id") and cursor["reply_to_event_id"] not in seen:
        eid = cursor["reply_to_event_id"]
        seen.add(eid)
        if eid not in by_event:
            break
        referenced.append(by_event[eid])
        cursor = by_event[eid]
    prior_bot = [r for r in turns if r["speaker_role"] == "assistant"
                 and r["event_id"] != current["event_id"]
                 and r.get("delivery_status") == "accepted"]
    last_bot = prior_bot[-1] if prior_bot else {}
    correction = {"status": "normal", "predicate": "", "holder_id": "",
                  "evidence_event_id": "", "rejected_premise": ""}
    if _CORRECTION.search(current["text"]):
        denied = _DENIAL.search(current["text"])
        own_negative = [c for c in extract_claims(current) if c["polarity"] == "negative"]
        predicate = own_negative[0]["predicate"] if own_negative else ""
        if denied and not predicate:
            tail = current["text"][denied.end():denied.end() + 10]
            predicate = next((p for p, pat, _ in _CLAIMS if pat.search(tail)), "")
        if not predicate:
            tail = current["text"]
            match = re.search(r"你在(.{0,8})", tail)
            if match:
                probe = dict(current, text=match.group(1))
                predicate = next((c["predicate"] for c in extract_claims(probe)), "")
        if not predicate:
            quoted = referenced + ([last_bot] if last_bot else [])
            candidates = [c for r in quoted for c in extract_claims(r)]
            predicate = candidates[0]["predicate"] if candidates else ""
        matches = [c for c in claims if c["predicate"] == predicate
                   and c['modality'] == 'uttered'
                   and c["polarity"] != "negative" and c["source_event_id"] != current["event_id"]]
        own = [c for c in matches if c["holder_id"] == current["speaker_id"]]
        bot = [c for c in matches if c["holder_id"] == str(self_id or "BOT")]
        if own:
            correction.update(status="disputed", predicate=predicate,
                              holder_id=current["speaker_id"],
                              evidence_event_id=own[-1]["source_event_id"])
        elif bot:
            correction.update(status="supported_correction", predicate=predicate,
                              holder_id=bot[-1]["holder_id"],
                              evidence_event_id=bot[0]["source_event_id"],
                              rejected_premise="%s:%s" % (current["speaker_id"], predicate))
        else:
            correction.update(status="ambiguous", predicate=predicate)
    topics, assignments = _topics(turns, session_id)
    chosen=next(t for t in topics if t['topic_id']==assignments[current['event_id']])
    participants=chosen['participant_ids']
    entities=sorted(chosen['terms'])[:16]
    topic_id=chosen['topic_id']
    return {"session_id": session_id, "turn": current, "turns": turns,
            "referenced_events": referenced, "last_assistant_turn": last_bot,
            "claims": claims, "correction": correction,
            'active_topics':[{'topic_id':t['topic_id'],'event_ids':t['event_ids'],
                              'participant_ids':t['participant_ids'],'last_active_at':t['last_active_at']}
                             for t in topics[-4:]],
            "topic": {"topic_id": topic_id, "participant_ids": participants,
                      'event_ids':chosen['event_ids'],
                      "entities": entities, "last_active_at": now or time.time(),
                      "open_question": current["text"] if current["text"].endswith(("?", "？")) else ""}}


def pack_context(state, *, max_turns=16):
    required = {state["turn"]["event_id"]}
    required.update(r["event_id"] for r in state.get("referenced_events") or [])
    last = state.get("last_assistant_turn") or {}
    if last.get("event_id"):
        required.add(last["event_id"])
    turns = list(state.get("turns") or [])
    relevant=set((state.get('topic') or {}).get('event_ids') or [])
    recent=[r for r in turns if r['event_id'] in relevant] if relevant else turns
    recent = recent[-max(0,int(max_turns)):] if max_turns>0 else []
    selected = {r["event_id"]: r for r in recent}
    for row in turns:
        if row["event_id"] in required:
            selected[row["event_id"]] = row
    return sorted(selected.values(), key=lambda r: (float(r.get("occurred_at") or 0), str(r["event_id"])))


def render_context(state, *, max_turns=16):
    lines = []
    for turn in pack_context(state, max_turns=max_turns):
        name = turn.get("speaker_name") or turn["speaker_id"]
        if turn["speaker_role"] == "assistant":
            name += "（你）"
            if turn["addressee_ids"]:
                name += " → " + ",".join(turn["addressee_ids"])
        stamp = time.strftime("%H:%M:%S", time.localtime(float(turn["occurred_at"]))) if turn.get("occurred_at") else ""
        lines.append("[%s] %s: %s [event=%s,QQ=%s]" %
                     (stamp, name, turn["text"], turn["event_id"], turn["speaker_id"]))
    return "\n".join(lines)


def render_support(state):
    correction = state.get("correction") or {}
    if correction.get("status") == "supported_correction":
        return ("【本轮事实与纠错 · 高于语气】\n"
                "原事件证明先前表达属于清漪，不属于当前对方。错的是先前的归属。"
                "先承认接反了，再接当前话题；不能继续指责对方理亏，也不能编造对方未表达的观点。"
                "\n证据事件：" + str(correction.get("evidence_event_id") or ""))
    if correction.get("status") == "disputed":
        return ("【本轮事实与纠错 · 高于语气】\n对方否认的内容有其原话支持。"
                "温和说明依据，不盲目认错，也不继续给对方加未表达的动机。")
    if correction.get("status") == "ambiguous":
        return ("【本轮事实与纠错 · 高于语气】\n纠正所指尚不明确。不要认定对方理亏；"
                "必要时只简短确认，不能补造旧发言。")
    claims = state.get("claims") or []
    hot = [c for c in claims if c["predicate"] == "complain_hot" and c["polarity"] != "negative"]
    sun = [c for c in claims if c["predicate"] == "sunny"]
    self_actor = (state.get('last_assistant_turn') or {}).get('speaker_id')
    if (hot and sun and hot[-1]['holder_id'] == self_actor
            and sun[-1]['holder_id'] == state['turn']['speaker_id']
            and hot[-1]['holder_id'] != sun[-1]['holder_id']):
        return ("【本轮事实 · 高于语气】嫌热是清漪先前的表达；当前对方只说当地出太阳。"
                "不能推出对方嫌热或嫌晒。回应放晴本身，不把责任推给对方。")
    return ""