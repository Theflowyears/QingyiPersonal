# -*- coding: utf-8 -*-
"""M03 人物与实体归一契约（stage1 / memory 轨，dev_requires=[A05, M02]）。

M02 的 resolve_subject() 明确写着「实体归一是 M03 的范围，这里不替它编一个
实体 ID」。本模块把这句话补上：人物称呼（raw QQ / 别名 / per_* 稳定 ID）归一
到唯一 canonical 人物引用；实体表面形式归一到调用方注册表里的 canonical 实体键。

纯函数契约：核心归一函数不读写账本、不发网络、不调模型、不建库建表建目录，
所有映射由调用方传入。唯一的例外是显式只读桥 read_person_directory()：它沿用
R01 的先例（库文件不存在就返回空，绝不隐式初始化，X15），只走 self_model 的
只读反查 _person_qq_map。注意 self_model.person_aliases() **不是**只读入口
（它经 person_id() upsert person 行），所以这里刻意不用它；别名表必须由调用方
传入。路径一律调用时解析，不在模块顶层冻常量。

三条硬边界：
- 绝不猜、绝不编造（X13）：主体只能来自真实 actor/topic 或已确认映射。
  解析不到就返回显式 status='unresolved' + 稳定 reason_code，subject_ref 留空；
  不在这里哈希 QQ 造 per_*，也不给未知表面形式编实体键。多义一律不猜。
- 虚构不混入现实（§5.4）：小说/扮演/塔罗/玩笑语境里，人物称呼不得绑定到现实
  人物；实体只允许绑定注册表里显式标记 fictional 的键。虚构实体在任何语境下
  都只归一为 entity，永远不会变成 person 引用。
- 口径兼容 M02：subject_kind 分 person/entity/unscoped 三种；来自冻结源窗口
  的绑定沿用 M02 的 subject_basis（source_actor/source_topic），unscoped 的
  basis 与 M02 完全一致。claim_provenance.py 已上线，本模块不改它，只复用
  它的 PERSON_MARKERS 与 M01 的 FICTION_CONTEXTS，不新造第二套词汇。

接进唯一写入 reducer 是 M06 的范围；这里先把归一的门本身定死。
"""
import os

from claim_provenance import PERSON_MARKERS
from cognition_admission import FICTION_CONTEXTS

SUBJECT_KINDS = ('person', 'entity', 'unscoped')
STATUSES = ('resolved', 'unresolved')
# 人物 basis：前两种来自调用方传入的已确认映射/别名表，后两种与 M02 同词。
PERSON_BASES = ('confirmed_mapping', 'alias_mapping', 'source_actor', 'source_topic')
ENTITY_BASES = ('entity_registry',)


def _result(kind, status, ref='', basis='', reason='', mention='', fictional=False, **extra):
    row = {'status': status, 'subject_kind': kind, 'subject_ref': ref,
           'subject_basis': basis, 'reason_code': reason, 'mention': mention,
           'fictional': bool(fictional)}
    row.update(extra)
    return row


def _person_ok(ref, qq, basis, mention):
    return _result('person', 'resolved', ref=ref, basis=basis, mention=mention, qq=qq)


def _person_no(reason, mention=''):
    return _result('person', 'unresolved', reason=reason, mention=mention, qq='')


def _person_index(persons):
    """{per_*: raw QQ} → 双向索引。方向与 self_model._person_qq_map 一致。

    不合格的行（键不是 per_*、QQ 不是纯数字）直接不参与归一：它们确认不了
    任何人，也不许反过来按格式猜一个身份。
    """
    by_id, by_qq = {}, {}
    for pid, qq in (persons or {}).items():
        pid, qq = str(pid or '').strip(), str(qq or '').strip()
        if not pid.startswith('per_') or not qq.isdigit():
            continue
        by_id[pid] = qq
        by_qq.setdefault(qq, []).append(pid)
    return by_id, by_qq


def _canonical(ref, by_id, by_qq):
    """把一个已确认的引用收敛到 canonical 形态：能落到 per_* 就落 per_*。

    返回 (ref, qq)；qq 撞上映射表里多个 per_* 时返回 (None, '')，交给上层
    报多义，不猜。源窗口只给了 raw QQ 而没有稳定 ID 时沿用 M02 的口径
    （stable or raw），raw QQ 本身就是 canonical 引用。
    """
    ref = str(ref or '').strip()
    if ref.startswith('per_'):
        return ref, by_id.get(ref, '')
    if ref.isdigit():
        pids = by_qq.get(ref) or []
        if len(pids) == 1:
            return pids[0], ref
        if len(pids) > 1:
            return None, ''
        return ref, ref
    return ref, ''


def _source_index(sources):
    """冻结源窗口 → {称呼形态: {(canonical 引用, basis)}}。

    与 M02 resolve_subject 相同的取数口径（actor_qq/actor_ref/topic_qq/topic_ref，
    stable or raw），但同一形态落到不同人时保留全部候选而不是后者覆盖前者：
    归一契约不猜。
    """
    table = {}
    for row in (sources or []):
        if not isinstance(row, dict):
            continue
        for key, ref_key, basis in (('actor_qq', 'actor_ref', 'source_actor'),
                                    ('topic_qq', 'topic_ref', 'source_topic')):
            raw = str(row.get(key) or '').strip()
            stable = str(row.get(ref_key) or '').strip()
            for form in (raw, stable):
                if form:
                    table[form] = table.get(form, set()) | {(stable or raw, basis)}
    return table


def _from_sources(form, table, by_id, by_qq, mention):
    pairs = table.get(form)
    if not pairs:
        return None
    refs = {ref for ref, _basis in pairs}
    if len(refs) > 1:
        return _person_no('ambiguous_source_subject', mention)
    ref = next(iter(refs))
    # 同一个人既当过 actor 又当过 topic 不算多义：引用唯一，basis 取字典序
    # 保证确定性（source_actor < source_topic）。
    basis = sorted({basis for _ref, basis in pairs})[0]
    canon, qq = _canonical(ref, by_id, by_qq)
    if canon is None:
        return _person_no('ambiguous_person_mapping', mention)
    return _person_ok(canon, qq, basis, mention)


def _alias_index(aliases, by_id, by_qq):
    """别名表 → {别名: 已确认的 canonical 人物集合}。

    别名值可以是 per_* / raw QQ，或它们的列表（一个称呼历史上指过谁就都列
    出来）。指向未确认引用的别名不产生候选：宁可报 alias_target_unconfirmed，
    也不把别名洗成身份。
    """
    index = {}
    for alias, refs in (aliases or {}).items():
        alias = str(alias or '').strip()
        if not alias:
            continue
        if isinstance(refs, str):
            refs = [refs]
        confirmed = set()
        for ref in (refs or []):
            canon, _qq = _canonical(str(ref or '').strip(), by_id, by_qq)
            # 只认映射表里真实存在的 per_*：_canonical 对未知 per_* 原样返回
            # （源窗口口径），但别名表不能拿一个没确认过的 ID 当身份。
            if canon and canon in by_id:
                confirmed |= {canon}
        index[alias] = confirmed
    return index


def resolve_person(mention, persons=None, aliases=None, sources=None, context_kind=None):
    """把一个人物称呼归一到唯一 canonical 人物引用（per_* 优先）。

    persons: {per_*: raw QQ} 已确认人物映射（调用方传入，或经
             read_person_directory 只读取得）；
    aliases: {别名: 引用或引用列表} 已确认别名表；
    sources: M02 口径的冻结源记录列表（actor_qq/actor_ref/topic_qq/topic_ref）。
    返回 {status, subject_kind='person', subject_ref, subject_basis,
          reason_code, mention, fictional, qq}。unresolved 时 subject_ref 恒空。
    """
    text = str(mention or '').strip()
    if not text:
        return _person_no('empty_mention')
    # §5.4：扮演/小说/塔罗里的「主人」不是现实的主人。虚构语境的人物称呼
    # 一律不给现实人物绑定，哪怕映射表里确认过这个别名。
    if str(context_kind or '') in FICTION_CONTEXTS:
        return _person_no('fiction_person_not_fact', text)
    by_id, by_qq = _person_index(persons)
    table = _source_index(sources)
    # 源窗口优先：与 M02 同口径，依据来自真实 actor/topic（basis 沿用
    # source_actor/source_topic）；映射表负责 canonical 化与窗口外的兜底确认。
    if text.startswith('per_'):
        found = _from_sources(text, table, by_id, by_qq, text)
        if found:
            return found
        if text in by_id:
            return _person_ok(text, by_id[text], 'confirmed_mapping', text)
        # 未知 per_* 不许反哈希、不许按格式补造：查不到就是查不到。
        return _person_no('unknown_person_id', text)
    if text.isdigit():
        found = _from_sources(text, table, by_id, by_qq, text)
        if found:
            return found
        pids = by_qq.get(text) or []
        if len(pids) == 1:
            return _person_ok(pids[0], text, 'confirmed_mapping', text)
        if len(pids) > 1:
            return _person_no('ambiguous_person_mapping', text)
        return _person_no('unknown_qq', text)
    confirmed = _alias_index(aliases, by_id, by_qq).get(text)
    if confirmed is None:
        return _person_no('unknown_alias', text)
    if len(confirmed) == 1:
        pid = next(iter(confirmed))
        return _person_ok(pid, by_id[pid], 'alias_mapping', text)
    if len(confirmed) > 1:
        return _person_no('ambiguous_alias', text)
    return _person_no('alias_target_unconfirmed', text)


def _entity_index(registry):
    """实体注册表 → {表面形式(casefold): {canonical 键: fictional}}。

    registry 可以是记录列表 [{'key', 'aliases', 'fictional'}]，或
    {键: {'aliases': [...], 'fictional': bool}}。只做精确匹配（strip+casefold），
    不做模糊/包含匹配：模糊就是猜。键由调用方确认（世界知识键或虚构键），
    本模块不造键。
    """
    if isinstance(registry, dict):
        rows = []
        for key, value in registry.items():
            row = dict(value) if isinstance(value, dict) else {'aliases': value}
            row['key'] = key
            rows.append(row)
    else:
        rows = [row for row in (registry or []) if isinstance(row, dict)]
    index = {}
    for row in rows:
        key = str(row.get('key') or '').strip()
        if not key:
            continue
        forms = {key.casefold()}
        forms.update(str(alias or '').strip().casefold()
                     for alias in (row.get('aliases') or []) if str(alias or '').strip())
        for form in forms:
            index.setdefault(form, {})[key] = bool(row.get('fictional'))
    return index


def resolve_entity(surface, registry=None, context_kind=None):
    """把一个实体表面形式归一到注册表里的 canonical 实体键。只读，不造键。

    返回 {status, subject_kind='entity', subject_ref, subject_basis,
          reason_code, mention, fictional}。虚构实体（fictional=True）永远
    只归一为 entity；虚构语境里只允许绑定显式标记 fictional 的键，现实世界
    知识键不得在小说/扮演/塔罗里被当成事实消费（§5.4）。
    """
    text = str(surface or '').strip()
    if not text:
        return _result('entity', 'unresolved', reason='empty_surface')
    candidates = _entity_index(registry).get(text.casefold()) or {}
    if not candidates:
        return _result('entity', 'unresolved', reason='unknown_entity', mention=text)
    if len(candidates) > 1:
        return _result('entity', 'unresolved', reason='ambiguous_entity', mention=text)
    key, fictional = next(iter(candidates.items()))
    if str(context_kind or '') in FICTION_CONTEXTS and not fictional:
        return _result('entity', 'unresolved', reason='fiction_entity_not_fact', mention=text)
    return _result('entity', 'resolved', ref=key, basis='entity_registry',
                   mention=text, fictional=fictional)


def _unscoped(mention=''):
    # 与 M02 完全同口径：没有声明主体的世界知识命题 basis='unscoped'，引用留空。
    return _result('unscoped', 'resolved', basis='unscoped', mention=mention)


def normalize_subject(mention, subject_kind='', persons=None, aliases=None,
                      registry=None, sources=None, context_kind=None):
    """统一入口：区分 person / entity / unscoped 三种主体类型。

    subject_kind 显式给出时按类型走对应的门；留空时按**已确认的形式**分派
    （per_*/纯数字/别名表命中 → 人物，注册表命中 → 实体），两边都命中就是
    跨类型多义，报 ambiguous_subject_kind，不猜。空称呼且未指定类型 →
    unscoped（M02 口径）。任何解析不到的情况都保持 unresolved。
    """
    kind = str(subject_kind or '').strip()
    text = str(mention or '').strip()
    if kind == 'person':
        return resolve_person(text, persons, aliases, sources, context_kind)
    if kind == 'entity':
        return resolve_entity(text, registry, context_kind)
    if kind == 'unscoped':
        # unscoped 是「没有主体」，不是「主体没解析出来」的遮羞布。
        if text:
            return _result('unscoped', 'unresolved', reason='subject_unresolved', mention=text)
        return _unscoped()
    if kind:
        return _result('', 'unresolved', reason='unknown_subject_kind', mention=text)
    if not text:
        return _unscoped()
    person = resolve_person(text, persons, aliases, sources, context_kind)
    entity = resolve_entity(text, registry, context_kind)
    if person['status'] == 'resolved' and entity['status'] == 'resolved':
        return _result('', 'unresolved', reason='ambiguous_subject_kind', mention=text)
    if person['status'] == 'resolved':
        return person
    if entity['status'] == 'resolved':
        return entity
    # 都解析不到时保留最有信息量的稳定码（多义/虚构门），其余统一落到
    # M02 的 subject_unresolved，方便上层用同一套原因码留账。
    for result in (person, entity):
        code = result['reason_code']
        if code.startswith('ambiguous') or code.startswith('fiction'):
            return result
    return _result('', 'unresolved', reason='subject_unresolved', mention=text)


def _claim_text(claim):
    for key in ('restatement', 'content', 'proposition', 'text'):
        value = str(claim.get(key) or '').strip()
        if value:
            return value
    return ''


def resolve_claim_subject(claim, sources, persons=None, aliases=None,
                          registry=None, context_kind=None):
    """M02 resolve_subject 的归一版：声明主体走归一，未声明保持 M02 口径。

    只处理 claim 里**显式声明**的 subject_ref/subject_qq，绝不从自由摘述里
    猜主体（X13）。未声明且摘述含「主人/爸爸/妈妈/对方」→ subject_unresolved
    （与 M02 同码）；否则 unscoped。context_kind 缺省时读 claim 自己的
    context_kind（M01 口径），虚构门随之生效。
    """
    cand = claim if isinstance(claim, dict) else {}
    if context_kind is None:
        context_kind = cand.get('context_kind')
    declared = str(cand.get('subject_ref') or cand.get('subject_qq') or '').strip()
    if declared:
        return normalize_subject(declared, '', persons, aliases, registry,
                                 sources, context_kind)
    if any(marker in _claim_text(cand) for marker in PERSON_MARKERS):
        return _result('', 'unresolved', reason='subject_unresolved')
    return _unscoped()


def read_person_directory(refs):
    """只读桥：把 per_* 稳定 ID 反查成 {per_*: raw QQ}，供 resolve_person 用。

    只走 self_model._person_qq_map（它自身注明只读、查不到留空、绝不写
    person 行）。库文件不存在直接返回空字典，绝不建库建表建目录（X15）；
    路径调用时解析。别名不从这里拿：person_aliases() 经 person_id() 会
    upsert person 行，是写入路径，归一契约不碰它，别名表由调用方传入。
    """
    wanted = sorted({str(ref or '').strip() for ref in (refs or [])
                     if str(ref or '').strip().startswith('per_')})
    if not wanted:
        return {}
    root = os.getenv('PERSONA_DATA_DIR') or os.path.join(
        os.getenv('DATA_DIR', './data'), 'persona')
    if not os.path.isfile(os.path.join(root, 'cognition.db')):
        return {}
    try:
        import self_model
        return {str(pid): str(qq or '')
                for pid, qq in self_model._person_qq_map(wanted).items()}
    except Exception:
        return {}