"""Versioned, uncalibrated computational responses to attributable evidence only."""
import copy
import json
import os
from pathlib import Path
import hashlib
import math

import emotion_core as legacy

VERSION = 'shadow-v2'
# 识别词表属于「证据口径」，不是动力学参数：改它不改恢复速度/步长，
# 也不动已冻结的 parameter_hash（实时观察继续累计，不必重开一账本）。
VOCAB_VERSION = 'v2.1'
AFFECTION_EVIDENCE = (
    '喜欢你', '最喜欢你', '爱你', '谢谢', '辛苦了', '乖', '亲亲', '抱抱',
    '你好厉害', '你帮大忙', '帮我查清楚', '宝宝真好', '我真棒',
)
def _evidence_extra():
    """部署方补充识别词（EMOTION_EVIDENCE_EXTRA，逗号分隔）。

    识别词表属于人格数据：白板仓库只留通用说法，真实角色的口癖
    （例如特定自称）由部署方用这个变量补进来，不改冻结的 PARAMETERS。
    """
    raw = os.getenv('EMOTION_EVIDENCE_EXTRA', '')
    return tuple(x.strip() for x in raw.split(',') if x.strip())


AFFECTION_EVIDENCE_LIVE = AFFECTION_EVIDENCE + _evidence_extra()

REBUKE_EVIDENCE = (
    '你刚才说错了', '你说错了', '你理解错了', '不是这个意思', '别烦我',
    '闭嘴', '滚开', '滚', '讨厌你', '你真笨', '你真蠢',
)
PARAMETERS = copy.deepcopy(legacy.PARAMETERS)
# praise_evidence 在账本冻结哈希（parameter_hash）里。识别词属于人格数据：
# 部署方若要让实时影子账本延续旧哈希（不被 advance 的冻结守卫拒绝、
# 观察时钟不重置），在 DATA_DIR/personas/emotion_evidence.json 提供
# {"praise_evidence": [...]}，内容与账本 parameters 完全一致才允许落账——
# 哈希本身仍是守卫，改数据不改哈希一样大声拒。缺文件用仓库内通用默认。
def _praise_override():
    try:
        f = Path(os.environ.get('DATA_DIR', './data')) / 'personas' / 'emotion_evidence.json'
        if not f.is_file():
            return None
        v = json.loads(f.read_text(encoding='utf-8')).get('praise_evidence')
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return list(v)
    except Exception as e:
        print('[情感] emotion_evidence.json 读取失败(%s)，用仓库默认词表' % type(e).__name__, flush=True)
    return None


_praise = _praise_override() or ['谢谢你', '你帮大忙', '你好厉害', '谢谢我', '帮我查清楚了']
PARAMETERS.update(version=VERSION, directed_activation=0.02, tool_activation=0.01,
                  correction_valence=-0.025, correction_control=-0.015,
                  praise_evidence=_praise,
                  correction_evidence=['你刚才说错了', '你说错了', '你理解错了', '不是这个意思'])
DIMENSIONS = ('valence', 'arousal', 'control', 'attention')


SEMANTIC_VERSION = 'valence-integration-v1'
MIN_VALENCE_CONFIDENCE = 0.8
SEMANTIC_RULES = (
    '- praise：明确夸她、谢她、表达喜欢或亲近；\n'
    '- rebuke：明确责怪、否定她，或让她闭嘴/滚开；\n'
    '- neutral：普通交流、玩梗、吐槽事情，不是在评价她；\n'
    '- uncertain：反讽、引用、对象不明，或只凭一句话判不准。\n'
)


def integrated_appraisal(event, candidate=None):
    """Separate experiment only; never changes the frozen live appraise contract.

    A present but unresolved/uncertain label suppresses lexical valence rather
    than silently turning uncertainty into positive evidence.
    """
    result = appraise(event)
    result = copy.deepcopy(result)
    payload = legacy._payload(event)
    text = str(payload.get('text') or '').strip()
    exclusion = ''
    if result['ignored'] or event['kind'] != 'message':
        exclusion = 'unsupported_evidence'
    elif payload.get('addressed_to_self') is not True:
        exclusion = 'not_addressed'
    elif legacy._indirect(payload, text):
        exclusion = 'indirect_or_roleplay'
    stance = 'praise' if result['delta']['valence'] > 0 else 'rebuke' if result['delta']['valence'] < 0 else 'neutral'
    origin = 'lexical'
    if candidate is not None:
        origin = 'candidate'
        stance = candidate.get('stance', '')
        if not exclusion:
            if candidate.get('state') != 'done':
                exclusion = 'label_' + str(candidate.get('state') or 'pending')
            elif stance not in ('praise', 'rebuke'):
                exclusion = 'label_' + str(stance or 'uncertain')
            elif not candidate.get('model') or not candidate.get('reason'):
                exclusion = 'missing_provenance'
            else:
                try:
                    conf = float(candidate.get('confidence', 0))
                except (ValueError, TypeError):
                    conf = 0
                if not math.isfinite(conf) or not MIN_VALENCE_CONFIDENCE <= conf <= 1:
                    exclusion = 'low_confidence'
                else:
                    result['confidence'] = conf
    result['delta']['valence'] = 0.0
    result['delta']['control'] = 0.0
    if not exclusion and stance in ('praise', 'rebuke'):
        positive = stance == 'praise'
        result['delta']['valence'] = PARAMETERS['praise_valence' if positive else 'correction_valence']
        result['delta']['control'] = PARAMETERS['praise_control' if positive else 'correction_control']
    elif not exclusion:
        exclusion = 'no_valence_evidence'
    result.update(rule='integrated_' + stance, semantic_version=SEMANTIC_VERSION,
                  evidence_origin=origin, exclusion_reason=exclusion)
    if candidate is not None:
        result['candidate_evidence'] = {k: candidate.get(k) for k in
                                        ('event_id', 'state', 'stance', 'confidence', 'reason', 'model', 'updated')}
    return result


def parameter_hash():
    return hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def initial_state(at):
    return legacy.initial_state(at)


def advance(state, at):
    result = legacy._state_copy(state)
    target = max(result['at'], legacy._number(at, 'at'))
    elapsed = target - result['at']
    for key in DIMENSIONS:
        base = PARAMETERS['baseline'][key]
        result[key] = base + (result[key] - base) * math.exp(-elapsed / PARAMETERS['tau_seconds'][key])
    result['at'] = target
    return result


def appraise(event):
    result = legacy.appraise(event)
    result['version'] = VERSION
    if result['ignored']:
        return result
    payload = legacy._payload(event)
    if event['kind'] == 'message':
        text = payload.get('text', '').strip()
        if payload.get('addressed_to_self') is True and not legacy._indirect(payload, text):
            result['delta']['arousal'] = PARAMETERS['directed_activation']
            result.update(rule='directed_interaction', reason='Attributable interaction adds computational activation, not a reported feeling.')
            # 词表覆盖「明确对她说的」喜欢/夸奖与纠正/责备；引用、扮演、说别人都不动效价。
            if any(phrase in text for phrase in AFFECTION_EVIDENCE_LIVE):
                result.update(rule='direct_affection', confidence=0.8,
                              reason='Explicit self-addressed warmth outside quotation or roleplay.')
                result['delta']['valence'] = PARAMETERS['praise_valence']
                result['delta']['control'] = PARAMETERS['praise_control']
            elif any(phrase in text for phrase in REBUKE_EVIDENCE):
                result.update(rule='direct_rebuke', confidence=0.8,
                              reason='Explicit rebuke/correction addressed to her, not inferred mood.')
                result['delta']['valence'] = PARAMETERS['correction_valence']
                result['delta']['control'] = PARAMETERS['correction_control']
    elif event['kind'] == 'tool_result':
        result['delta']['arousal'] = PARAMETERS['tool_activation']
    return result


def apply_event(state, event, appraisal=None):
    expected = appraise(event)
    if appraisal is not None and appraisal != expected:
        raise ValueError('appraisal must match versioned evidence rules')
    _, _, effective = legacy._event_header(event)
    result = advance(state, effective)
    for key in DIMENSIONS:
        low, high = PARAMETERS['bounds'][key]
        result[key] = min(high, max(low, result[key] + expected['delta'][key]))
    result['processed'] += 1
    return result


# --- S02「动力学与评估规则分别冻结」（纯增量：以上既有符号与口径一律不动） ---
# 设计理由：
# - 动力学锁 dynamics_parameter_hash 只回答「状态怎么恢复、冲击多大」：
#   tau_seconds/baseline/bounds、各类步长（directed_activation/tool_activation/
#   praise_*/correction_*/message_attention/tool_*）、固定特质 traits，以及
#   内核版本 version 与校准声明 calibration。证据词表不在其中。
# - 证据锁 appraisal_rule_hash 只回答「什么算对她的证据」：两个识别词表、
#   PARAMETERS 里的 praise/correction_evidence、SEMANTIC_RULES、证据口径版本
#   VOCAB_VERSION/SEMANTIC_VERSION，以及语义证据置信门槛 MIN_VALENCE_CONFIDENCE
#   （integrated_appraisal 的接受规则，属评估侧）。恢复时间尺度与步长不在其中。
# - VERSION 是内核（动力学）版本，归动力学锁；VOCAB_VERSION/SEMANTIC_VERSION
#   是证据/评估口径版本，归证据锁。改词表只需动 VOCAB_VERSION，两锁互不牵连。
# - 用显式白名单而不是「PARAMETERS 减词表」的黑名单：将来新增参数键必须显式
#   归边，不许悄悄溜进任何一把锁。两把锁的键集互斥、合起来恰好覆盖当前
#   PARAMETERS 全部键（tests/test_emotion_rule_freeze.py 钉死）。
# - 线上 parameter_hash() 仍是整个 PARAMETERS（含词表）的冻结口径，实时账本
#   继续用它累计，S02 不改它的计算与值；这里只把「改词表≠改动力学」形式化为
#   可机器验证的分别冻结。
_DYNAMICS_PARAMETER_KEYS = (
    'version', 'calibration', 'baseline', 'tau_seconds', 'bounds', 'traits',
    'message_attention', 'praise_valence', 'praise_control',
    'tool_control', 'tool_attention',
    'directed_activation', 'tool_activation',
    'correction_valence', 'correction_control',
)
_EVIDENCE_PARAMETER_KEYS = ('praise_evidence', 'correction_evidence')


def _canonical_sha256(payload):
    # 与冻结 parameter_hash 相同的 canonical JSON 配方；独立 helper，
    # 不复用也不改动 parameter_hash 本体。
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def dynamics_parameter_hash():
    """动力学冻结锁：只覆盖恢复与步长量；改证据词表不动它。"""
    return _canonical_sha256({key: PARAMETERS[key] for key in _DYNAMICS_PARAMETER_KEYS})


def appraisal_rule_hash():
    """评估/证据冻结锁：只覆盖词表与评估规则；改动力学步长不动它。"""
    return _canonical_sha256({
        'vocab_version': VOCAB_VERSION,
        'semantic_version': SEMANTIC_VERSION,
        'affection_evidence': list(AFFECTION_EVIDENCE),
        'rebuke_evidence': list(REBUKE_EVIDENCE),
        'semantic_rules': SEMANTIC_RULES,
        'praise_evidence': list(PARAMETERS['praise_evidence']),
        'correction_evidence': list(PARAMETERS['correction_evidence']),
        'min_valence_confidence': MIN_VALENCE_CONFIDENCE,
    })
