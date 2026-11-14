# -*- coding: utf-8 -*-
"""R03 实体、人物、时间与词法的多线索融合打分初召回。

R01（retrieval_objects）把五个库投影成同一种检索描述并做硬过滤；这一层接住
**调用方传来的、已过硬过滤的统一对象**，只做打分、排序、截断。设计边界：

- 纯计算，真只读：本模块不 open 任何文件、不碰任何库，DATA_DIR 的调用时解析
  留在 R01，这里连路径都不出现，自然也没有「import 时冻路径」的问题。
  不 import retrieval_objects：R01 还在 §19.5 只观察阶段，它的静态锁禁止
  bot/ 里任何模块把它接进链路；R03 同样只观察，由调用方把 R01 的 items 喂进来。
- 词法只走系统唯一分词口径 text_tokens（jieba 载她的专属词典 + 汉字二元组 +
  停用词）。不许长第二套分词器：项目实测过统一分词器收益甚微，再长一套口径
  只会把「存了但搜不到」的老问题请回来。
- 分数只是检索排序线索，**不是命题真实性百分比**（R01 已强调）。R01 带来的
  质量信号（grounded/sourced/verified 等）原样保留在 match.source_parts，
  不混进融合分——证据质量是渲染层与矛盾判定（R05）的事，不是初召回排序的事。
- 不做联想遍历（R02/R04）：只对给定候选集打分，不扩散一条边；
  不判矛盾（R05）：不比较任何两条命题，conflicts 永远不产生。

四类线索的原始分都在 [0,1]，乘以 WEIGHTS 得到 match.score_parts 里的贡献，
融合分 = 各贡献之和。权重是排序偏好不是概率：实体最强（查询里显式给出的
指称），人物/词法次之，时间最弱（近期略占优，但旧记忆永不归零——归档不等于
假，历史仍可回顾，X16）。
"""
import math
import time

import text_tokens

CLUES = ('entity', 'person', 'time', 'lexical')
WEIGHTS = {'entity': 3.0, 'person': 2.0, 'time': 1.0, 'lexical': 2.0}
# 时间线索的半衰期：每过 7 天衰减一半，是缓坡不是悬崖。
TIME_HALF_LIFE_SECONDS = 7 * 86400.0
_DEFAULT_BUDGET = 32


def _budget(ctx):
    """与 R01._budget 同口径：0 是「这条都不要」，不是「没填」。

    R03 不 import R01（只观察阶段的静态锁），口径在这里手工对齐；
    用 or 兜底会把 0 悄悄变成 32。
    """
    value = ctx.get('retrieval_budget')
    return _DEFAULT_BUDGET if value is None else int(value)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _item_text(item):
    """词法/实体匹配用的文本 = 转述 + 原文引用。

    match.clues 里是库标签（belief/group/群号/类目），不是她说的话，
    不进词法匹配，免得「group」这种标签把分数摊平；实体线索单独看标签。
    """
    parts = [str(item.get('restatement') or '')]
    for span in (item.get('evidence_spans') or []):
        if isinstance(span, dict):
            parts.append(str(span.get('quote') or ''))
    return ' '.join(p for p in parts if p)


def _referent_names(ctx):
    """recent_referents 允许是字符串，或带 name/text/aliases 的字典。"""
    names = []
    for ref in (ctx.get('recent_referents') or []):
        if isinstance(ref, dict):
            candidates = [ref.get('name'), ref.get('text')] + list(ref.get('aliases') or [])
        else:
            candidates = [ref]
        for cand in candidates:
            value = str(cand or '').strip()
            if value and value not in names:
                names.append(value)
    return names


def _entity(item, ctx, text):
    """实体线索：查询上下文显式给出的指称，在条目文本或库标签里被点名。

    用子串包含而不是分词：专名（「提弗洛斯」）一旦被切错就再也认不出来，
    显式指称值得一次字面核对。词级重叠归词法线索，不重复计分。
    """
    names = _referent_names(ctx)
    if not names:
        return 0.0
    haystack = text.casefold()
    tags = ' '.join(str(c) for c in (item.get('match') or {}).get('clues') or []).casefold()
    hit = sum(1 for name in names
              if name.casefold() in haystack or name.casefold() in tags)
    return hit / len(names)


def _person(item, ctx):
    """人物线索：speaker_person_id / topic_person_ids 对上条目的会话归属。

    R01 的投影只带会话级的人物身份（private:<QQ>）；事件级 actor 要等
    R02/R04 的边投影才拿得到，这里不假装匹配得更深。群号不是人物：
    group:<群号> 是地点不是人，不参与人物匹配。
    """
    persons = []
    speaker = str(ctx.get('speaker_person_id') or '').strip()
    if speaker:
        persons.append(speaker)
    for pid in (ctx.get('topic_person_ids') or []):
        value = str(pid or '').strip()
        if value and value not in persons:
            persons.append(value)
    if not persons:
        return 0.0
    session = str(item.get('source_session') or '')
    handles = ({seg for seg in session.split(':')[1:] if seg}
               if session.startswith('private:') else set())
    hit = sum(1 for pid in persons if pid in handles)
    return hit / len(persons)


def _time(item, ctx):
    """时间邻近：给了 time_constraints 窗口就以窗口为准，否则看离 as_of 多近。

    不知道发生时间的条目得 0，而不是被当成「刚刚发生」；
    硬过滤（R01）已挡掉未来证据与窗口外条目，这里的距离分支只是防御。
    """
    occurred = item.get('occurred_at')
    if not _is_number(occurred):
        occurred = item.get('recorded_at')
    if not _is_number(occurred):
        return 0.0
    constraints = ctx.get('time_constraints') or {}
    not_before = constraints.get('not_before')
    not_after = constraints.get('not_after')
    if _is_number(not_before) or _is_number(not_after):
        distance = 0.0
        if _is_number(not_before) and occurred < not_before:
            distance = not_before - occurred
        elif _is_number(not_after) and occurred > not_after:
            distance = occurred - not_after
        return math.pow(0.5, max(0.0, distance) / TIME_HALF_LIFE_SECONDS)
    as_of = ctx.get('as_of_time')
    if not _is_number(as_of) or as_of <= 0:
        return 0.0
    age = max(0.0, as_of - occurred)
    return math.pow(0.5, age / TIME_HALF_LIFE_SECONDS)


def _lexical(text, query_tokens):
    """词法线索：查询词集被条目覆盖的比例。分词与交集都只走 text_tokens。"""
    if not query_tokens:
        return 0.0
    item_tokens = text_tokens.tokens(text)
    return text_tokens.overlap(query_tokens, item_tokens) / len(query_tokens)


def score_item(item, ctx):
    """给一条 R01 统一对象打融合分。返回副本，绝不改调用方的投影。"""
    row = dict(item)
    match = dict(row.get('match') or {})
    text = _item_text(row)
    query_tokens = text_tokens.tokens(str(ctx.get('query_text') or ''))
    raw = {'entity': _entity(row, ctx, text),
           'person': _person(row, ctx),
           'time': _time(row, ctx),
           'lexical': _lexical(text, query_tokens)}
    parts = {clue: round(WEIGHTS[clue] * raw[clue], 4) for clue in CLUES}
    clues = [str(c) for c in (match.get('clues') or [])]
    for clue in CLUES:
        if raw[clue] > 0 and clue not in clues:
            clues.append(clue)
    # R01 的质量信号原样保留供溯源，但不进融合分（分数不是真实性百分比）。
    match['source_parts'] = dict(match.get('score_parts') or {})
    match['clues'] = clues
    match['score_parts'] = parts
    match['score'] = round(sum(parts.values()), 4)
    row['match'] = match
    return row


def _occurred(item):
    value = item.get('occurred_at')
    return float(value) if _is_number(value) else 0.0


def recall(items, ctx):
    """R03 初召回：对给定候选集融合打分、排序、按 retrieval_budget 截断。

    输入应是 R01 硬过滤后的 items（失效/越界/未来证据已在 R01 挡下）；
    这里不再过滤、不扩散、不判矛盾。排序确定性：分数降序，同分按发生时间
    降序，再同按 object_id——同样的输入永远给同样的序列。
    """
    started = time.time()
    ctx = ctx if isinstance(ctx, dict) else {}
    rows = [score_item(x, ctx) for x in (items or []) if isinstance(x, dict)]
    rows.sort(key=lambda r: (-r['match']['score'], -_occurred(r),
                             str(r.get('object_id') or '')))
    budget = max(0, _budget(ctx))
    selected = rows[:budget]
    coverage = {clue: sum(1 for r in rows if r['match']['score_parts'][clue] > 0)
                for clue in CLUES}
    return {
        'items': selected,
        'provenance': {
            'candidates': len(rows), 'selected': len(selected),
            'over_budget': len(rows) - len(selected), 'budget': budget,
            'weights': dict(WEIGHTS), 'clue_coverage': coverage, 'expanded': 0,
            'seconds': round(time.time() - started, 4),
            'limitations': ('R03 只做多线索融合打分初召回：无联想遍历(R02/R04)、'
                            '无矛盾判定(R05)；分数只是检索排序线索，'
                            '不是命题真实性百分比。',),
        },
    }