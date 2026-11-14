"""S05 效价候选人工标注基准：金标准只来自人工，候选 confidence 绝不当金标准。

统一方案 §9.3：人工标注小规模开发集 + 未参与调参的保留集（holdout），
按九类切片分别量 praise/rebuke precision、macro-F1、拒判覆盖、对象归属
错误率、置信度校准误差（样本不足显式 insufficient_data）、词法与候选分歧。

边界（沿用 S04 口径）：
- 本模块唯一可写连接必须落在 valence_candidates.db，且只写 annotation 表；
  candidate 表只读，实时影子/人格库绝不触碰。
- 本模块不生产任何自然样本：标注只记录人工判断；隔离评测集构造的反例
  必须显式 origin='isolated_eval'，与自然观察分开评测，绝不混算。
- dev/holdout 按 event_id 哈希稳定划分，标注时不接受调用者指定 split；
  holdout 一经标注不参与调参，所有查询/报告必须显式带 split。
- 同一 event_id 重复标注按修订处理：保留全部历史（rev 递增），
  评测与查询默认取每个事件的最新修订。
"""
import hashlib
import os
import sqlite3
import time
from pathlib import Path

import valence_candidates as vc

# 九类切片，稳定英文枚举（顺序对应方案 §9.3 的中文清单）。
SLICES = (
    "direct_praise",        # 直接夸奖
    "direct_rebuke",        # 直接责备
    "event_complaint",      # 事情吐槽（对事不对她）
    "third_person",         # 第三人对象
    "quotation",            # 引用
    "irony_joke",           # 玩笑/反讽
    "roleplay",             # 角色扮演
    "explicit_neutral",     # 明确中性
    "insufficient_context", # 语境不足
)
STANCES = vc.STANCES  # praise/rebuke/neutral/uncertain，与候选账本同口径
TARGETS = ("self", "other", "none")
SPLITS = ("dev", "holdout")
ORIGINS = ("natural", "isolated_eval")
# 校准误差最少样本量：不足时显式返回 insufficient_data，不硬算。
MIN_CALIBRATION_SAMPLES = 30
# macro-F1 的类别口径：uncertain 是拒判而非第四类，单列拒判覆盖。
_F1_CLASSES = ("praise", "rebuke", "neutral")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotation (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL,
 slice TEXT NOT NULL,
 gold_stance TEXT NOT NULL,
 gold_target TEXT NOT NULL,
 annotator TEXT NOT NULL,
 split TEXT NOT NULL,
 origin TEXT NOT NULL,
 annotated_at REAL NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 rev INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_annotation_event_rev ON annotation(event_id, rev);
CREATE INDEX IF NOT EXISTS idx_annotation_split ON annotation(split);
"""


def _root():
    # 调用时解析，不在模块顶层冻成常量（避免写脏真实 data/）。
    return Path(os.getenv("PERSONA_DATA_DIR") or
                str(Path(os.getenv("DATA_DIR", "./data")) / "persona"))


def _path():
    return _root() / "valence_candidates.db"


def _conn():
    # S04/S05 标注边界：唯一可写连接必须落在候选账本这一个文件上。
    # 若 _path 被改指向实时影子/人格库，这里直接拒绝，不静默越界。
    if _path().name != "valence_candidates.db":
        raise ValueError("annotation writes must stay in valence_candidates.db")
    _root().mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_path()), timeout=4)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=4000")
    return c


def _ensure(c):
    c.executescript(_SCHEMA)


def _readonly():
    """真只读连接；缺库返回 None，不建目录不建表。"""
    path = _path()
    if not path.is_file():
        return None
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def _tables(c):
    return {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def stable_split(event_id):
    """按 event_id 的 SHA-256 稳定划分：约 20% holdout，其余 dev。

    跨进程/跨重启确定（不用内建 hash），同一事件永远落同一侧；
    holdout 一经标注不参与调参，修订也不改变 split。
    """
    eid = str(event_id or "").strip()
    if not eid:
        raise ValueError("event_id required")
    first = hashlib.sha256(eid.encode("utf-8")).digest()[0]
    return "holdout" if first < 51 else "dev"  # 51/256 ≈ 19.9%


def annotate(event_id, *, slice_name, gold_stance, gold_target, annotator,
             origin, note="", at=None):
    """写入一条人工标注（金标准）。非法枚举直接拒绝，不落库。

    - split 由 stable_split(event_id) 决定，不接受调用者指定；
    - origin 必须显式声明 natural / isolated_eval，无默认值：
      构造反例只能进 isolated_eval，绝不冒充自然观察；
    - 同一 event_id 再次标注 = 修订：历史行全部保留，rev 递增，
      查询/评测默认取最新修订。
    """
    eid = str(event_id or "").strip()
    who = str(annotator or "").strip()[:80]
    if not eid:
        raise ValueError("event_id required")
    if not who:
        raise ValueError("annotator required")
    if slice_name not in SLICES:
        raise ValueError("invalid slice: %r" % (slice_name,))
    if gold_stance not in STANCES:
        raise ValueError("invalid gold_stance: %r" % (gold_stance,))
    if gold_target not in TARGETS:
        raise ValueError("invalid gold_target: %r" % (gold_target,))
    if origin not in ORIGINS:
        raise ValueError("invalid origin: %r" % (origin,))
    split = stable_split(eid)
    now = float(at if at is not None else time.time())
    c = _conn()
    try:
        _ensure(c)
        rev = int(c.execute(
            "SELECT COALESCE(MAX(rev),0)+1 FROM annotation WHERE event_id=?",
            (eid,)).fetchone()[0])
        c.execute(
            "INSERT INTO annotation(event_id,slice,gold_stance,gold_target,"
            "annotator,split,origin,annotated_at,note,rev) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (eid, slice_name, gold_stance, gold_target, who, split, origin,
             now, str(note or "")[:200], rev))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "event_id": eid, "split": split, "rev": rev,
            "origin": origin}


def query_annotations(split, *, origin=None, include_history=False, limit=500):
    """只读查询标注；必须显式带 split。缺库/缺表返回 missing_data。

    默认每个事件只返回最新修订；include_history=True 返回全部修订历史。
    """
    if split not in SPLITS:
        raise ValueError("split must be one of %r" % (SPLITS,))
    if origin is not None and origin not in ORIGINS:
        raise ValueError("invalid origin: %r" % (origin,))
    n = max(1, min(int(limit or 500), 2000))
    c = _readonly()
    if c is None:
        return {"ok": False, "status": "missing_data", "rows": []}
    try:
        if "annotation" not in _tables(c):
            return {"ok": False, "status": "missing_data", "rows": []}
        sql = "SELECT * FROM annotation WHERE split=?"
        args = [split]
        if origin is not None:
            sql += " AND origin=?"
            args.append(origin)
        if not include_history:
            sql += (" AND rev=(SELECT MAX(rev) FROM annotation b"
                    " WHERE b.event_id=annotation.event_id)")
        sql += " ORDER BY event_id, rev LIMIT ?"
        args.append(n)
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()
    return {"ok": True, "status": "ok", "split": split, "origin": origin,
            "rows": rows}


def _slice_hint(text, address_basis, lexical):
    """工作清单的切片提示：只是规则线索，绝不是金标准，人工可推翻。"""
    t = str(text or "")
    if any(q in t for q in ("「", "」", "“", "”", '"')):
        return "quotation"
    if any(m in t for m in ("皮上", "皮下", "扮演", "角色扮演", "戏里", "cosplay")):
        return "roleplay"
    if any(m in t for m in ("开玩笑", "反讽", "狗头", "[doge]", "（笑")):
        return "irony_joke"
    if str(address_basis or "") == "ambient":
        return "third_person"
    if lexical == "positive":
        return "direct_praise"
    if lexical == "negative":
        return "direct_rebuke"
    return "manual_review"


def annotation_worklist(limit=50):
    """导出待人工标注的候选清单。candidate 表只读（mode=ro），不改任何状态。

    候选 stance/confidence 仅作参考展示，不预填金标准；
    已有人工标注（含 isolated_eval 修订）的事件不再出现在清单里。
    """
    n = max(1, min(int(limit or 50), 200))
    c = _readonly()
    if c is None:
        return {"ok": False, "status": "missing_data", "rows": []}
    try:
        tables = _tables(c)
        if "candidate" not in tables:
            return {"ok": False, "status": "missing_data", "rows": []}
        done_ids = set()
        if "annotation" in tables:
            done_ids = {r[0] for r in c.execute(
                "SELECT DISTINCT event_id FROM annotation")}
        rows = c.execute(
            "SELECT * FROM candidate ORDER BY at ASC LIMIT 2000").fetchall()
    finally:
        c.close()
    out = []
    for row in rows:
        if row["event_id"] in done_ids:
            continue
        item = dict(row)
        item["text"] = str(item.get("text") or "")[:160]
        item["split"] = stable_split(row["event_id"])
        item["slice_hint"] = _slice_hint(
            row["text"], row["address_basis"], row["lexical"])
        out.append(item)
        if len(out) >= n:
            break
    return {"ok": True, "status": "ok", "rows": out,
            "note": "slice_hint 只是规则提示；金标准必须由人工给出，"
                    "候选 stance/confidence 仅供参考，不当金标准"}


def evaluate(golds, predictions, *, min_calibration_samples=MIN_CALIBRATION_SAMPLES):
    """纯计算评测：输入人工标注（每事件最新修订）+ 候选预测，不碰数据库。

    口径：
    - 金标准只取 gold_stance/gold_target（人工标注）；候选 confidence
      只进校准误差，绝不参与金标准；
    - praise/rebuke precision = 预测为该类的样本中金标准同类的占比；
    - macro-F1 = praise/rebuke/neutral 三类 F1 的算术平均；uncertain
      预测视为拒判（降低对应金标准类的召回），不算第四类；
    - 拒判覆盖 = 预测 uncertain 的占比；
    - 对象归属错误率 = 双方都给出 target 的样本中不一致的占比；
    - 校准误差 ECE：仅对非 uncertain 且 confidence 有效的预测，10 个
      等宽桶，sum(n_b/N * |acc_b - conf_b|)；样本不足显式 insufficient_data；
    - 词法与候选分歧：复用 valence_candidates._relation 的 hit/missed/
      mismatch/neutral 口径；
    - golds 必须全部同一 origin：natural 与 isolated_eval 绝不混算。
    """
    gold_map = {}
    origins = set()
    for g in golds:
        eid = str(g.get("event_id") or "").strip()
        stance = str(g.get("gold_stance") or "")
        origin = str(g.get("origin") or "")
        if not eid:
            raise ValueError("gold event_id required")
        if stance not in STANCES:
            raise ValueError("invalid gold_stance: %r" % (stance,))
        if origin not in ORIGINS:
            raise ValueError("gold origin must be explicit: %r" % (origin,))
        if eid in gold_map:
            raise ValueError("duplicate gold for %s; pass latest revision only" % eid)
        gold_map[eid] = g
        origins.add(origin)
    if len(origins) > 1:
        raise ValueError("natural and isolated_eval must be evaluated separately")
    origin = next(iter(origins)) if origins else None

    pred_map = {}
    for p in predictions:
        eid = str(p.get("event_id") or "").strip()
        stance = str(p.get("stance") or "")
        if not eid:
            raise ValueError("prediction event_id required")
        if stance not in STANCES:
            raise ValueError("invalid predicted stance: %r" % (stance,))
        if eid in pred_map:
            raise ValueError("duplicate prediction for %s" % eid)
        pred_map[eid] = p

    joined = [eid for eid in gold_map if eid in pred_map]
    n = len(joined)

    def _precision(cls):
        preds = [eid for eid in joined if pred_map[eid]["stance"] == cls]
        correct = sum(1 for eid in preds
                      if gold_map[eid]["gold_stance"] == cls)
        return (correct / len(preds) if preds else None), len(preds), correct

    praise_p, praise_n, praise_ok = _precision("praise")
    rebuke_p, rebuke_n, rebuke_ok = _precision("rebuke")

    per_class = {}
    for cls in _F1_CLASSES:
        tp = fp = fn = 0
        for eid in joined:
            g = gold_map[eid]["gold_stance"]
            p = pred_map[eid]["stance"]
            if p == cls and g == cls:
                tp += 1
            elif p == cls:
                fp += 1
            elif g == cls:
                fn += 1
        denom = 2 * tp + fp + fn
        per_class[cls] = {
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": (2 * tp / denom) if denom else 0.0,
            "support": tp + fn, "predicted": tp + fp,
        }
    macro_f1 = (sum(per_class[c]["f1"] for c in _F1_CLASSES) / 3
                if n else None)

    abstained = sum(1 for eid in joined
                    if pred_map[eid]["stance"] == "uncertain")

    target_checked = target_errors = target_missing = 0
    for eid in joined:
        gt = gold_map[eid].get("gold_target")
        pt = pred_map[eid].get("target")
        if gt in TARGETS and pt in TARGETS:
            target_checked += 1
            if gt != pt:
                target_errors += 1
        else:
            target_missing += 1

    # 置信度校准：只对非拒判且 confidence 有效的预测；不足不硬算。
    cal_pairs = []
    confidence_missing = 0
    for eid in joined:
        p = pred_map[eid]
        if p["stance"] == "uncertain":
            continue
        try:
            conf = float(p.get("confidence"))
        except (TypeError, ValueError):
            confidence_missing += 1
            continue
        if not 0.0 <= conf <= 1.0:
            confidence_missing += 1
            continue
        correct = 1.0 if p["stance"] == gold_map[eid]["gold_stance"] else 0.0
        cal_pairs.append((conf, correct))
    if len(cal_pairs) < max(1, int(min_calibration_samples)):
        calibration = {"status": "insufficient_data", "n": len(cal_pairs),
                       "min_required": max(1, int(min_calibration_samples))}
    else:
        bins = [[] for _ in range(10)]
        for conf, correct in cal_pairs:
            bins[min(9, int(conf * 10))].append((conf, correct))
        ece = 0.0
        detail = []
        for i, bucket in enumerate(bins):
            if not bucket:
                continue
            avg_conf = sum(x[0] for x in bucket) / len(bucket)
            acc = sum(x[1] for x in bucket) / len(bucket)
            ece += len(bucket) / len(cal_pairs) * abs(acc - avg_conf)
            detail.append({"bin": i, "n": len(bucket),
                           "avg_confidence": avg_conf, "accuracy": acc})
        calibration = {"status": "ok", "n": len(cal_pairs), "ece": ece,
                       "bins": detail}

    relation = {"hit": 0, "missed": 0, "mismatch": 0, "neutral": 0}
    for eid in joined:
        p = pred_map[eid]
        rel = vc._relation({"state": "done", "stance": p["stance"],
                            "lexical": str(p.get("lexical") or "none")})
        if rel in relation:
            relation[rel] += 1

    return {
        "origin": origin,
        "n_gold": len(gold_map), "n_pred": len(pred_map), "n_joined": n,
        "praise_precision": praise_p,
        "praise_predicted": praise_n, "praise_correct": praise_ok,
        "rebuke_precision": rebuke_p,
        "rebuke_predicted": rebuke_n, "rebuke_correct": rebuke_ok,
        "per_class": per_class, "macro_f1": macro_f1,
        "abstained": abstained,
        "abstention_rate": abstained / n if n else None,
        "target_checked": target_checked, "target_errors": target_errors,
        "target_missing": target_missing,
        "target_error_rate": (target_errors / target_checked
                              if target_checked else None),
        "confidence_missing": confidence_missing,
        "calibration": calibration,
        "lexical_relation": relation,
        "gold_source": "human_annotation",
        "note": "金标准只来自人工标注；候选 confidence 仅用于校准误差，不当金标准",
    }


def evaluation_report(split, *, origin="natural",
                      min_calibration_samples=MIN_CALIBRATION_SAMPLES):
    """只读评测报告：必须显式带 split；默认只算 natural 自然观察。

    isolated_eval 必须显式指定 origin 才单独评测，绝不混入自然统计。
    缺库/缺 annotation 表返回 missing_data，不建目录不建表。
    候选账本没有对象预测列，对象归属错误率在此口径下为 None
    （纯 evaluate 支持预测显式携带 target）。
    """
    if split not in SPLITS:
        raise ValueError("split must be one of %r" % (SPLITS,))
    if origin not in ORIGINS:
        raise ValueError("invalid origin: %r" % (origin,))
    c = _readonly()
    if c is None:
        return {"ok": False, "status": "missing_data"}
    try:
        tables = _tables(c)
        if "annotation" not in tables:
            return {"ok": False, "status": "missing_data"}
        golds = [dict(r) for r in c.execute(
            "SELECT * FROM annotation a WHERE split=? AND origin=? "
            "AND rev=(SELECT MAX(rev) FROM annotation b "
            " WHERE b.event_id=a.event_id)", (split, origin)).fetchall()]
        preds = []
        if "candidate" in tables:
            preds = [dict(r) for r in c.execute(
                "SELECT event_id,stance,confidence,lexical FROM candidate "
                "WHERE state='done'").fetchall()]
    finally:
        c.close()
    metrics = evaluate(golds, preds,
                       min_calibration_samples=min_calibration_samples)
    status = "ok" if metrics["n_joined"] else "insufficient_data"
    return {"ok": True, "status": status, "split": split, **metrics}