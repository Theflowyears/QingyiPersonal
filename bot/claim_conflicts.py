# -*- coding: utf-8 -*-
"""M04 全库候选矛盾裁决（统一方案 §20.4 两段式 / §7.3 矛盾不是一律覆盖）。

X05/M02 把依据降到逐命题原句，M03 把主体归一到 canonical 引用；本模块在这
两者之上回答「两条候选是不是真的互相矛盾」。它**绝不删除/覆盖旧 belief**，
只产出裁决记录（verdict + 稳定 reason_code），供 M05 失效传播 / M08 巩固
后续消费。落账走唯一写入 reducer（M06），这里不碰。

两段式（§20.4）：
  第一段  按候选键 subject_id + predicate_family 检索疑似冲突，同键内全量
          两两比较（相邻有效期/同情景在第二段用时间规则细分），**不全库
          两两比较，也不只扫末尾 20/40 条**。
  第二段  依次判：不同主体→不比较；不同时间且可同时成立→历史变化；
          多值允许（如偏好）→并存；明确否定相反值→contradiction 候选；
          同一根源→转述重复；其余不确定→disputed/deferred。

predicate_family 是有限可解释类型（self_report_preference / current_activity
/ residence / relationship_statement / commitment），未知一律 unclassified，
绝不随意归到错误家族（§20.4）。

纯函数核心：adjudicate_* / classify_predicate / candidate_key 不读写账本、
不调模型、不发网络。唯一例外是显式只读桥 read_all_candidates()：沿用
entity_normalization.read_person_directory 与 valence_candidates.recent_readonly
的先例——sqlite 只读连接（mode=ro + PRAGMA query_only），库文件不存在返回
空列表，绝不建库建表建目录（X15）；路径调用时解析，不在顶层冻常量。
刻意不用 self_model.retrieval_beliefs()/pending_claims()：它们经 _db() 会
建目录建表（写入路径）且带 LIMIT 200/50，正是 M04 要摆脱的「只扫尾部」。

不用字面重叠率/向量相似度判矛盾（§7.3）：「喜欢」和「不喜欢」字面极像却必须
判 contradiction；「喜欢茶」和「喜欢咖啡」是多值偏好必须并存。值归一只做
**确定性的结构剥壳**（否定前缀 / 家族系动词 / 偏好情感词），剥出核心宾语
与否定极性，再按结构规则比较；语义 LLM 只能以 semantic_hint 形式提出
same/extends/contradicts/unknown 及来源，登记进记录，**绝不覆盖结构规则**。

与现有数据结构的口径差异（如实记录，不硬凑）：真实 belief 表只有
proposition/subject_qq/first_ts/last_ts/status，没有 predicate_family、
negation、qualifiers、valid_from/valid_to、epistemic_kind 列；claim_candidate
表也没有这些列。因此只读桥收集到的候选，其 predicate_family 由
classify_predicate() 对文本做有限关键词判定（未知→unclassified），
negation/qualifiers/有效期/认识类型留空（unknown），由裁决器在比较时按
文本前缀做确定性归一，绝不凭空补造有效期或认识类型。first_ts/last_ts 是
记录时间不是有效期，不当 valid_from/valid_to 用。
"""
import json
import os
import sqlite3
from pathlib import Path

# 有限可解释家族（§20.4）。unclassified 是兜底，不是第六种「关系」。
PREDICATE_FAMILIES = ('self_report_preference', 'current_activity', 'residence',
                      'relationship_statement', 'commitment')
UNCLASSIFIED = 'unclassified'
FAMILIES = PREDICATE_FAMILIES + (UNCLASSIFIED,)

# 稳定裁决集合。顺序即第二段判定优先级（见 adjudicate_pair）。
VERDICTS = ('not_comparable', 'equivalent', 'supplement', 'historical_change',
            'coexists', 'contradiction', 'duplicate', 'disputed', 'deferred')

# 语义 LLM 只能提出这四种关系 + 来源，登记不覆盖（§20.4）。
SEMANTIC_RELATIONS = ('same', 'extends', 'contradicts', 'unknown')

# 多值家族：不同正值允许并存（偏好可以同时喜欢茶和咖啡；承诺可以有多条）。
MULTI_VALUED = ('self_report_preference', 'commitment')
# 单值家族：同一时刻只有一个值，重叠有效期里不同正值→矛盾候选。
SINGLE_VALUED = ('current_activity', 'residence', 'relationship_statement')
# 可随时间变化的家族：不相交有效期→历史变化而非矛盾。commitment 不在内
# （不同到期承诺是并存的多条承诺，不是「旧承诺失效」）。
TIME_CHANGEABLE = ('self_report_preference', 'current_activity', 'residence',
                   'relationship_statement')

# 家族判定关键词（有限、可解释、按优先级首命中）。kind 显式给出时优先。
_FAMILY_MARKERS = (
    ('commitment', ('承诺', '答应', '保证', '约定')),
    ('self_report_preference', ('喜欢', '讨厌', '厌恶', '偏好', '最爱', '反感')),
    ('residence', ('住在', '定居', '搬到', '居住')),
    ('current_activity', ('正在', '在上课', '在上班', '在做')),
    ('relationship_statement', ('朋友', '恋人', '主人', '女儿', '儿子', '同事', '认识')),
)

# 确定性结构剥壳词表。这些只做「剥前缀」，不做相似度，不判语义。
_NEGATION_PREFIXES = ('没有', '没', '不', '未', '别')  # 长词优先
# 偏好情感词：正极性 / 负极性。负极性剥壳后翻转否定极性，
# 于是「讨厌茶」与「喜欢茶」核心宾语同为「茶」、极性相反→矛盾。
_PREFERENCE_POSITIVE = ('喜欢', '最爱', '偏好', '爱')
_PREFERENCE_NEGATIVE = ('讨厌', '厌恶', '反感', '拒绝')
# 各家族系动词/引导词剥壳（仅用于把「住在北京」归一到核心「北京」）。
_FAMILY_LEADERS = {
    'residence': ('住在', '居住于', '定居于', '搬到', '住'),
    'current_activity': ('正在', '在'),
    'relationship_statement': ('是', '为'),
    'commitment': ('承诺', '答应', '保证', '约定'),
}


def _text(value):
    return str(value or '').strip()


def _value_of(claim):
    """M02 兼容口径：object_value/value 优先，回落到摘述/命题文本。"""
    for key in ('object_value', 'value', 'restatement', 'proposition', 'content', 'text'):
        got = _text(claim.get(key))
        if got:
            return got
    return ''


def _id_of(claim):
    return _text(claim.get('candidate_id') or claim.get('object_id')
                 or claim.get('id') or claim.get('belief_id'))


def _subject_of(claim):
    return _text(claim.get('subject_ref') or claim.get('subject_qq')
                 or claim.get('subject_id'))


def _qualifiers_of(claim):
    raw = claim.get('qualifiers') or []
    if isinstance(raw, str):
        raw = [raw]
    return tuple(sorted(_text(x) for x in raw if _text(x)))


def _root_of(claim):
    """同一根源＝转述/再提取，不算独立反例（§20.4 / §7.3）。"""
    root = _text(claim.get('root_event_id'))
    if root:
        return root
    for item in (claim.get('evidence') or []):
        if isinstance(item, dict):
            got = _text(item.get('event_id'))
            if got:
                return got
        elif _text(item):
            return _text(item)
    return ''


def _epistemic_of(claim):
    return _text(claim.get('epistemic_kind') or claim.get('epistemic_type'))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _interval_of(claim):
    """有效期 (from, to)，None 表示开口/未知。绝不拿记录时间冒充有效期。"""
    start = claim.get('valid_from')
    end = claim.get('valid_to')
    if end is None:
        end = claim.get('valid_until')
    return (float(start) if _number(start) else None,
            float(end) if _number(end) else None)


def _strip_prefix(text, prefixes):
    for prefix in prefixes:
        if text.startswith(prefix) and len(text) > len(prefix):
            return text[len(prefix):], prefix
    return text, ''


def _core_value(family, value, negation):
    """确定性剥壳→(核心宾语, 否定极性)。不做相似度、不调模型。

    先剥否定前缀（每剥一层翻转极性，处理「不是不喜欢」这类双重否定），
    偏好家族再剥情感词（负极性翻转），其余家族剥系动词。显式 negation
    字段（bool 或 M02 式否定词字符串）作为初始极性。
    """
    core = _text(value).casefold()
    if isinstance(negation, bool):
        neg = negation
    else:
        neg = bool(_text(negation))
    # 否定前缀剥壳（长词优先，循环处理叠加）。
    while True:
        core, hit = _strip_prefix(core, _NEGATION_PREFIXES)
        if not hit:
            break
        neg = not neg
    if family == 'self_report_preference':
        core, hit = _strip_prefix(core, _PREFERENCE_NEGATIVE)
        if hit:
            neg = not neg
        else:
            core, _hit = _strip_prefix(core, _PREFERENCE_POSITIVE)
    else:
        core, _hit = _strip_prefix(core, _FAMILY_LEADERS.get(family, ()))
    return core.strip(), neg


def classify_predicate(text, kind=''):
    """把命题文本判到有限家族；未知→unclassified，绝不乱归（§20.4）。

    kind 显式命中家族时直接采用（调用方已确认）；否则按 _FAMILY_MARKERS
    优先级首命中。没有任何关键词命中就是 unclassified，不猜。
    """
    explicit = _text(kind)
    if explicit in PREDICATE_FAMILIES:
        return explicit
    body = _text(text)
    if not body:
        return UNCLASSIFIED
    for family, markers in _FAMILY_MARKERS:
        if any(marker in body for marker in markers):
            return family
    return UNCLASSIFIED


def _family_of(claim):
    declared = _text(claim.get('predicate_family'))
    if declared in PREDICATE_FAMILIES:
        return declared
    if declared == UNCLASSIFIED:
        return UNCLASSIFIED
    return classify_predicate(_value_of(claim), claim.get('kind')
                              or claim.get('predicate') or '')


def candidate_key(claim):
    """第一段候选键：subject_id + predicate_family（§20.4）。"""
    return (_subject_of(claim), _family_of(claim))


def _view(claim):
    cand = claim if isinstance(claim, dict) else {}
    family = _family_of(cand)
    value = _value_of(cand)
    core, neg = _core_value(family, value, cand.get('negation'))
    return {
        'id': _id_of(cand),
        'subject': _subject_of(cand),
        'family': family,
        'value': value,
        'core': core,
        'neg': neg,
        'qualifiers': _qualifiers_of(cand),
        'interval': _interval_of(cand),
        'time_expression': _text(cand.get('time_expression')),
        'root': _root_of(cand),
        'epistemic': _epistemic_of(cand),
    }


def _disjoint(left, right):
    """两段有效期可证明不相交（一方结束不晚于另一方开始）。开口不算。"""
    a_from, a_to = left
    b_from, b_to = right
    if a_to is not None and b_from is not None and a_to <= b_from:
        return True
    if b_to is not None and a_from is not None and b_to <= a_from:
        return True
    return False


def _has_interval(view):
    return view['interval'][0] is not None or view['interval'][1] is not None


def _record(verdict, reason, left, right, notes=(), hint=None):
    return {
        'verdict': verdict,
        'reason_code': reason,
        'reasons': [reason] + [n for n in notes if n and n != reason],
        'subject_ref': left['subject'] or right['subject'],
        'predicate_family': left['family'],
        'left_id': left['id'],
        'right_id': right['id'],
        'left_value': left['value'],
        'right_value': right['value'],
        'left_epistemic_kind': left['epistemic'],
        'right_epistemic_kind': right['epistemic'],
        'semantic_hint': hint,
    }


def _clean_hint(hint):
    """语义提示必须带关系与来源；不合格就登记为忽略，绝不参与判定。"""
    if not isinstance(hint, dict):
        return None, []
    relation = _text(hint.get('relation')).lower()
    source = _text(hint.get('source'))
    if relation not in SEMANTIC_RELATIONS:
        return None, ['semantic_hint_unknown_relation']
    if not source:
        return None, ['semantic_hint_unsourced']
    return {'relation': relation, 'source': source}, ['semantic_hint_recorded']


def adjudicate_pair(left, right, semantic_hint=None):
    """第二段：对一对同键候选给出稳定 verdict + reason_code。纯函数。

    判定顺序（§20.4，含「同根源不算独立反例」的前置守卫）：
      主体缺失/不同 → deferred/not_comparable
      值缺失 → deferred
      家族未分类/不同 → deferred/not_comparable
      命题全等：同根源→duplicate；独立→equivalent（认识类型差异留痕）
      同根源但命题分歧 → disputed（转述/再提取不算独立反例，绝不判矛盾）
      不相交有效期/不同时间且可变化 → historical_change
      补充（核心宾语包含或限定词超集） → supplement
      多值家族双方肯定 → coexists
      核心宾语相同极性相反 → contradiction（明确否定相反值）
      单值家族重叠期不同正值 → contradiction
      其余 → disputed
    semantic_hint 只登记，绝不覆盖以上任何结构判定。
    """
    a, b = _view(left), _view(right)
    hint, hint_notes = _clean_hint(semantic_hint)

    def out(verdict, reason, notes=()):
        return _record(verdict, reason, a, b, tuple(notes) + tuple(hint_notes), hint)

    # ── 主体门 ──
    if not a['subject'] or not b['subject']:
        return out('deferred', 'subject_missing')
    if a['subject'] != b['subject']:
        return out('not_comparable', 'different_subject')
    # ── 值门（空值连家族都判不了，先于家族门）──
    if not a['core'] or not b['core']:
        return out('deferred', 'missing_value')
    # ── 家族门 ──
    if a['family'] == UNCLASSIFIED or b['family'] == UNCLASSIFIED:
        return out('deferred', 'family_unclassified')
    if a['family'] != b['family']:
        return out('not_comparable', 'different_family')

    same_proposition = (a['core'] == b['core'] and a['neg'] == b['neg']
                        and a['qualifiers'] == b['qualifiers'])
    # ── 等价 / 转述重复 ──
    if same_proposition:
        if a['root'] and a['root'] == b['root']:
            return out('duplicate', 'same_root_restatement')
        notes = []
        if a['epistemic'] and b['epistemic'] and a['epistemic'] != b['epistemic']:
            # §7.3：转述/推断 vs 明确自述，保留认识类型差异，不按「更近」覆盖。
            notes.append('epistemic_kind_differs')
        return out('equivalent', 'equivalent_independent', notes)
    # ── 同根源守卫：转述/再提取不算独立反例，绝不升级为矛盾 ──
    if a['root'] and a['root'] == b['root']:
        return out('disputed', 'same_root_not_independent')

    # ── 时间变化 ──
    if a['family'] in TIME_CHANGEABLE:
        if _disjoint(a['interval'], b['interval']):
            return out('historical_change', 'disjoint_validity')
        if (not _has_interval(a) and not _has_interval(b)
                and a['time_expression'] and b['time_expression']
                and a['time_expression'] != b['time_expression']):
            return out('historical_change', 'distinct_time_expression')

    # ── 补充（非破坏性；包含关系只用于 supplement，绝不用于判矛盾）──
    if a['neg'] == b['neg']:
        if a['core'] != b['core'] and (a['core'] in b['core'] or b['core'] in a['core']):
            return out('supplement', 'value_extends')
        if a['core'] == b['core'] and a['qualifiers'] != b['qualifiers']:
            if set(a['qualifiers']) < set(b['qualifiers']) or set(b['qualifiers']) < set(a['qualifiers']):
                return out('supplement', 'qualifier_extends')

    # ── 多值并存 ──
    if a['family'] in MULTI_VALUED and not a['neg'] and not b['neg']:
        return out('coexists', 'multi_value_coexists')

    # ── 明确否定相反值：核心宾语相同、极性相反 → 矛盾候选 ──
    if a['core'] == b['core'] and a['neg'] != b['neg']:
        return out('contradiction', 'explicit_negation_opposite')
    # ── 单值家族重叠期不同正值 → 矛盾候选 ──
    if (a['family'] in SINGLE_VALUED and not a['neg'] and not b['neg']
            and a['core'] != b['core']):
        return out('contradiction', 'single_valued_value_conflict')

    # ── 其余不确定 ──
    return out('disputed', 'value_conflict_unresolved')


def collect_conflict_pairs(candidates):
    """第一段：按候选键分组，同键内全量两两配对（不扫尾部、不全库两两）。

    返回 [(left, right), ...]，按 (键, 左 id, 右 id) 确定性排序。
    不同键（不同主体或不同家族）不配对。
    """
    groups = {}
    for claim in (candidates or []):
        if not isinstance(claim, dict):
            continue
        groups.setdefault(candidate_key(claim), []).append(claim)
    pairs = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda c: (_id_of(c), _value_of(c)))
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                pairs.append((rows[i], rows[j]))
    return pairs


def adjudicate(candidates, semantic_hints=None):
    """两段式全量裁决：收集同键对→逐对判定。只产出记录，不改输入。

    semantic_hints: {(left_id, right_id): hint}，键按 id 排序无关（内部规范化）。
    返回裁决记录列表，按 (subject, family, left_id, right_id) 确定性排序。
    """
    hints = {}
    for key, hint in (semantic_hints or {}).items():
        if isinstance(key, (tuple, list)) and len(key) == 2:
            hints[tuple(sorted(_text(x) for x in key))] = hint
    records = []
    for left, right in collect_conflict_pairs(candidates):
        pair_key = tuple(sorted((_id_of(left), _id_of(right))))
        records.append(adjudicate_pair(left, right, hints.get(pair_key)))
    records.sort(key=lambda r: (r['subject_ref'], r['predicate_family'],
                                r['left_id'], r['right_id']))
    return records


# ── 只读桥：全库候选收集（X15：缺库返回空，绝不建库建表建目录）──

def _cognition_path():
    root = os.getenv('PERSONA_DATA_DIR') or os.path.join(
        os.getenv('DATA_DIR', './data'), 'persona')
    return os.path.join(root, 'cognition.db')


def _json_list(raw):
    try:
        got = json.loads(raw or '[]')
        return got if isinstance(got, list) else []
    except Exception:
        return []


def read_all_candidates():
    """从 cognition.db 只读收集**全部** belief 与隔离候选，供第一段全库检索。

    sqlite 只读连接（mode=ro + PRAGMA query_only），无 LIMIT——早期条目
    不会因为排在末尾 20/40 之外被漏掉（M04 验收）。库文件不存在返回 []，
    绝不建库建表建目录；表不存在按空处理。路径调用时解析。

    候选字段口径与 M02 兼容：subject_ref / predicate_family / value /
    object_value / negation / qualifiers / valid_from / valid_to /
    time_expression / epistemic_kind / root_event_id / evidence。真实表没有
    的列留空（unknown），predicate_family 由 classify_predicate 文本判定，
    绝不拿 first_ts/last_ts（记录时间）冒充有效期。
    """
    path = _cognition_path()
    if not os.path.isfile(path):
        return []
    try:
        connection = sqlite3.connect(
            Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
    except sqlite3.Error:
        return []
    out = []
    try:
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'belief' in tables:
            for row in connection.execute('SELECT * FROM belief ORDER BY id'):
                out.append(_belief_candidate(connection, row, tables))
        if 'claim_candidate' in tables:
            for row in connection.execute('SELECT * FROM claim_candidate ORDER BY id'):
                out.append(_quarantine_candidate(row))
    except sqlite3.Error:
        return out
    finally:
        connection.close()
    return out


def _belief_candidate(connection, row, tables):
    belief_id = str(row['id'])
    evidence, root = [], ''
    if 'belief_evidence' in tables:
        refs = [str(r['event_id']) for r in connection.execute(
            "SELECT event_id FROM belief_evidence WHERE belief_id=? "
            "AND relation='support' ORDER BY ts, event_id", (belief_id,))]
        evidence = refs
        root = refs[0] if refs else ''
    proposition = str(row['proposition'] or '')
    return {
        'candidate_id': belief_id,
        'origin': 'belief',
        'subject_ref': str(row['subject_qq'] or ''),
        'predicate_family': classify_predicate(proposition),
        'value': proposition,
        'object_value': proposition,
        'negation': '',
        'qualifiers': [],
        'valid_from': None,
        'valid_to': None,
        'time_expression': '',
        'epistemic_kind': '',
        'root_event_id': root,
        'evidence': [{'event_id': eid} for eid in evidence],
        'status': str(row['status'] or ''),
        'recorded_at': row['last_ts'],
    }


def _quarantine_candidate(row):
    evidence = [e for e in _json_list(row['evidence']) if isinstance(e, dict)]
    root = ''
    for item in evidence:
        if _text(item.get('event_id')):
            root = _text(item['event_id'])
            break
    restatement = str(row['restatement'] or '')
    kind = str(row['kind'] or '')
    return {
        'candidate_id': str(row['id']),
        'origin': 'claim_candidate',
        'subject_ref': str(row['subject_ref'] or ''),
        'predicate_family': classify_predicate(restatement, kind),
        'value': restatement,
        'object_value': restatement,
        'negation': '',
        'qualifiers': [],
        'valid_from': None,
        'valid_to': None,
        'time_expression': '',
        'epistemic_kind': '',
        'root_event_id': root,
        'evidence': evidence,
        'status': str(row['state'] or ''),
        'recorded_at': row['created'],
    }