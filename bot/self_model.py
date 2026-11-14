"""她自己的「认知账本」：事件、经历、信念、承诺、目标、行动。

这是《人格内核路线》阶段 0 和 1 的落地：

  阶段 0（统一证据）
    · 真实发生过的事有稳定 ID，重复观察不会记成两件事；
    · 人物有稳定身份（QQ → person），话题主体和收件人是两个字段；
    · 每一个对外动作都留一条账：为什么产生、发给谁、到哪一步了；
    · 「发出」「Bot 接受」「对方看到」必须分开写 —— 发出请求不等于送达。

  阶段 1（自传体记忆）
    · 经历 / 信念 / 承诺 / 目标彼此关联，而不是几串短文本；
    · 信念只能「修订」，不能覆盖：修订必须带理由和证据；
    · 能回答「什么改变了你的想法」—— 答案来自修订记录与真实事件；
    · 旧档案保守迁移：来源标 legacy、时间未知就留空，不补造生平。

设计边界（对应路线文档第 3.2 节）：
    本模块是状态/事实的**唯一写入者**。LLM 只能通过这里的显式 API 提交候选，
    不能直接写「发生过什么 / 我已完成 / 我的人格变了」。

不用图数据库：对象少、关系浅，SQLite 的关联表足够，而且可断点回放。
"""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar

_SCHEMA = """
CREATE TABLE IF NOT EXISTS layer_revision (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, layer TEXT NOT NULL,
 content_hash TEXT NOT NULL, content TEXT NOT NULL, reason TEXT NOT NULL,
 ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_record (
 id TEXT PRIMARY KEY, recipient TEXT NOT NULL, action TEXT NOT NULL,
 decision TEXT NOT NULL, reason TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_revocation (
 consent_id TEXT PRIMARY KEY, reason TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS action_outbox (
 logical_key TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
 parts INTEGER NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_part (
 logical_key TEXT NOT NULL, part INTEGER NOT NULL, state TEXT NOT NULL,
 receipt TEXT NOT NULL DEFAULT '', PRIMARY KEY(logical_key, part)
);
CREATE TABLE IF NOT EXISTS evidence_retraction (
 event_id TEXT PRIMARY KEY, reason TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cognition_invalidation (
 kind TEXT NOT NULL, object_id TEXT NOT NULL, event_id TEXT NOT NULL,
 PRIMARY KEY(kind, object_id, event_id)
);
CREATE TABLE IF NOT EXISTS person (
    id          TEXT PRIMARY KEY,
    qq          TEXT UNIQUE,
    aliases     TEXT NOT NULL DEFAULT '[]',
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    ts          REAL NOT NULL,
    session     TEXT NOT NULL DEFAULT '',
    actor_qq    TEXT NOT NULL DEFAULT '',
    topic_qq    TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    visibility  TEXT NOT NULL DEFAULT 'private',
    payload     TEXT NOT NULL DEFAULT '{}',
    dedupe      TEXT UNIQUE,
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts);
CREATE INDEX IF NOT EXISTS idx_event_topic ON event(topic_qq);

CREATE TABLE IF NOT EXISTS episode (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    summary     TEXT NOT NULL,
    event_ids   TEXT NOT NULL DEFAULT '[]',
    perspective TEXT NOT NULL DEFAULT 'self',
    is_legacy   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS belief (
    id          TEXT PRIMARY KEY,
    proposition TEXT NOT NULL,
    subject_qq  TEXT NOT NULL DEFAULT '',
    confidence  REAL,
    status      TEXT NOT NULL DEFAULT 'active',
    source      TEXT NOT NULL DEFAULT '',
    first_ts    REAL NOT NULL,
    last_ts     REAL NOT NULL,
    is_legacy   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS belief_evidence (
    belief_id   TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    relation    TEXT NOT NULL DEFAULT 'support',
    weight      REAL NOT NULL DEFAULT 1.0,
    ts          REAL NOT NULL,
    PRIMARY KEY (belief_id, event_id, relation)
);

CREATE TABLE IF NOT EXISTS belief_revision (
    id          TEXT PRIMARY KEY,
    belief_id   TEXT NOT NULL,
    old_text    TEXT NOT NULL DEFAULT '',
    new_text    TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    event_ids   TEXT NOT NULL DEFAULT '[]',
    ts          REAL NOT NULL
);

-- M02：一条命题绑定一句原话。belief_evidence 只说「挂哪个事件」，
-- 这里说「挂的是那个事件里的哪一段」，quote/offset 可逐字复核。
CREATE TABLE IF NOT EXISTS claim_span (
    belief_id       TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    quote           TEXT NOT NULL,
    span_start      INTEGER NOT NULL,
    span_end        INTEGER NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    match_kind      TEXT NOT NULL DEFAULT 'model_quote',
    subject_ref     TEXT NOT NULL DEFAULT '',
    subject_basis   TEXT NOT NULL DEFAULT '',
    ts              REAL NOT NULL,
    PRIMARY KEY (belief_id, event_id, span_start, span_end)
);
CREATE INDEX IF NOT EXISTS idx_claim_span_event ON claim_span(event_id);

-- 无依据/暂缓的命题留在这里等人工或后续证据，绝不混进已核验信念（X05）。
CREATE TABLE IF NOT EXISTS claim_candidate (
    id                   TEXT PRIMARY KEY,
    restatement          TEXT NOT NULL,
    kind                 TEXT NOT NULL DEFAULT '',
    subject_ref          TEXT NOT NULL DEFAULT '',
    decision             TEXT NOT NULL,
    reasons              TEXT NOT NULL DEFAULT '[]',
    evidence             TEXT NOT NULL DEFAULT '[]',
    source_session       TEXT NOT NULL DEFAULT '',
    visibility           TEXT NOT NULL DEFAULT 'private',
    candidate_confidence REAL,
    state                TEXT NOT NULL DEFAULT 'pending_review',
    created              REAL NOT NULL,
    dedupe               TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS commitment (
    id          TEXT PRIMARY KEY,
    to_qq       TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    due_ts      REAL,
    status      TEXT NOT NULL DEFAULT 'open',
    created     REAL NOT NULL,
    closed      REAL,
    event_ids   TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS goal (
    id          TEXT PRIMARY KEY,
    owner_qq    TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    motive      TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT '',
    success     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    parent_event TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    closed      REAL
);

CREATE TABLE IF NOT EXISTS action (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    goal_id     TEXT NOT NULL DEFAULT '',
    target_qq   TEXT NOT NULL DEFAULT '',
    topic_qq    TEXT NOT NULL DEFAULT '',
    visibility  TEXT NOT NULL DEFAULT 'private',
    scope       TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'proposed',
    reason      TEXT NOT NULL DEFAULT '',
    event_ids   TEXT NOT NULL DEFAULT '[]',
    echo        TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_state ON action(state);

CREATE TABLE IF NOT EXISTS self_view_rev (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    text        TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    event_ids   TEXT NOT NULL DEFAULT '[]'
);

CREATE TRIGGER IF NOT EXISTS invalidate_reused_belief_evidence
AFTER INSERT ON belief_evidence
WHEN EXISTS (SELECT 1 FROM evidence_retraction WHERE event_id=NEW.event_id)
BEGIN
 UPDATE belief SET status='invalidated', confidence=NULL WHERE id=NEW.belief_id;
END;
CREATE TRIGGER IF NOT EXISTS invalidate_reused_goal
AFTER INSERT ON goal
WHEN EXISTS (SELECT 1 FROM evidence_retraction WHERE event_id=NEW.parent_event)
BEGIN
 UPDATE goal SET status='invalidated' WHERE id=NEW.id;
 INSERT OR IGNORE INTO cognition_invalidation VALUES('goal',NEW.id,NEW.parent_event);
END;
CREATE TRIGGER IF NOT EXISTS invalidate_reused_self_view
AFTER INSERT ON self_view_rev
BEGIN
 INSERT OR IGNORE INTO cognition_invalidation
 SELECT 'self_view_rev', NEW.id, r.event_id FROM evidence_retraction r
 JOIN json_each(NEW.event_ids) j ON j.value=r.event_id;
END;
CREATE TRIGGER IF NOT EXISTS invalidate_reused_episode
AFTER INSERT ON episode
BEGIN
 INSERT OR IGNORE INTO cognition_invalidation
 SELECT 'episode', NEW.id, r.event_id FROM evidence_retraction r
 JOIN json_each(NEW.event_ids) j ON j.value=r.event_id;
END;
CREATE TRIGGER IF NOT EXISTS invalidate_reused_commitment
AFTER INSERT ON commitment
BEGIN
 INSERT OR IGNORE INTO cognition_invalidation
 SELECT 'commitment', NEW.id, r.event_id FROM evidence_retraction r
 JOIN json_each(NEW.event_ids) j ON j.value=r.event_id;
END;
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- M05：派生来源 DAG 与失效传播（统一方案 §20.5）。边方向 derived→source；
-- 修正记录不可变只追加；对象状态单调（失效不洗回）；epoch 存 meta，
-- 快照与 epoch 推进在同一事务里落账。旧 cognition_invalidation 路径不动。
CREATE TABLE IF NOT EXISTS derivation_edge (
    derived_id   TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    derived_kind TEXT NOT NULL DEFAULT '',
    source_kind  TEXT NOT NULL DEFAULT '',
    basis        TEXT NOT NULL DEFAULT '',
    ts           REAL NOT NULL,
    PRIMARY KEY (derived_id, source_id)
);
CREATE TABLE IF NOT EXISTS correction_record (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT '',
    new_revision_id TEXT NOT NULL DEFAULT '',
    ts              REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS object_validity_state (
    object_id          TEXT PRIMARY KEY,
    status             TEXT NOT NULL,
    reasons            TEXT NOT NULL DEFAULT '[]',
    remaining_supports TEXT NOT NULL DEFAULT '[]',
    revoked_scopes     TEXT NOT NULL DEFAULT '[]',
    epoch              INTEGER NOT NULL,
    updated            REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invalidation_snapshot (
    epoch          INTEGER NOT NULL,
    snapshot_id    TEXT NOT NULL,
    correction_ids TEXT NOT NULL DEFAULT '[]',
    payload        TEXT NOT NULL DEFAULT '{}',
    ts             REAL NOT NULL,
    PRIMARY KEY (epoch, snapshot_id)
);
"""

ACTION_STATES = ("proposed", "authorized", "generated", "submitted",
                 "accepted", "failed", "unknown")
# 收到回执只能从「已提交」往前走；不允许 failed 反过来变 accepted 这种幽灵复活
_ALLOWED = {
    "proposed": {"authorized", "failed", "unknown"},
    "authorized": {"generated", "failed", "unknown"},
    "generated": {"submitted", "failed", "unknown"},
    "submitted": {"accepted", "failed", "unknown"},
    "accepted": set(),
    "failed": set(),
    "unknown": set(),
}


def _db():
    root = os.getenv("PERSONA_DATA_DIR") or os.path.join(
        os.getenv("DATA_DIR", "./data"), "persona")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "cognition.db")
    conn = sqlite3.connect(path, timeout=8)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 冷账本转 WAL 需要独占；撞上别的连接持写锁时保持现有日志模式
            # 继续写，不能把这条观察立刻丢掉（V01-D4）。
            pass
        conn.executescript(_SCHEMA)
        return conn
    except BaseException:
        conn.close()
        raise


def _ro_db():
    """只读连接：账本不存在返回 None，绝不隐式建库建表（X15，V01-D1）。

    用 query_only 而不是 mode=ro：WAL 库的只读 URI 连接关闭时无法做 checkpoint，
    会留下 -wal/-shm 旁路文件；query_only 同样禁止一切写，且保持原有收尾行为。
    """
    root = os.getenv("PERSONA_DATA_DIR") or os.path.join(
        os.getenv("DATA_DIR", "./data"), "persona")
    path = os.path.join(root, "cognition.db")
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(path, timeout=8)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _sha(*parts):
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:14]


def _now(ts=None):
    return float(ts or time.time())


def _j(v):
    return json.dumps(v, ensure_ascii=False)


def _uj(s, default):
    try:
        got = json.loads(s or "")
        return got if isinstance(got, type(default)) else default
    except Exception:
        return default


# ── 人物 ──────────────────────────────────────────
def person_id(qq, alias=""):
    """QQ 号 → 稳定人物 ID。同一个人换了群名片，ID 不变。"""
    q = str(qq or "").strip()
    if not q.isdigit():
        return ""
    pid = "per_" + _sha(q)
    now = _now()
    try:
        c = _db()
        row = c.execute("SELECT aliases FROM person WHERE id=?", (pid,)).fetchone()
        aliases = _uj(row["aliases"], []) if row else []
        a = str(alias or "").strip()[:40]
        if a and a not in aliases:
            aliases = (aliases + [a])[-8:]
        c.execute(
            "INSERT INTO person(id,qq,aliases,first_seen,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET aliases=excluded.aliases, last_seen=excluded.last_seen",
            (pid, q, _j(aliases), now, now))
        c.commit()
        c.close()
    except Exception as e:
        print("[认知] 人物写入失败 " + type(e).__name__, flush=True)
    return pid


def person_aliases(qq):
    pid = person_id(qq)
    if not pid:
        return []
    try:
        c = _db()
        row = c.execute("SELECT aliases FROM person WHERE id=?", (pid,)).fetchone()
        c.close()
        return _uj(row["aliases"], []) if row else []
    except Exception:
        return []


# ── 事件 ──────────────────────────────────────────
def record_event(kind, ts=None, session="", actor_qq="", topic_qq="",
                 source="", visibility="private", payload=None, dedupe=""):
    """记一件真实发生过的事。同一个 dedupe 键重复提交只算一次。

    dedupe 的用处：群里同一条消息被三条路径看到、N 次重试读到同一个工具结果，
    都不该变成多份「证据」——那会让信念凭空变牢固。
    """
    k = str(kind or "").strip()[:24]
    if not k:
        return ""
    dk = str(dedupe or "").strip()[:160] or None
    now = _now()
    occurred = now if ts is None else float(ts)
    eid = "evt_" + _sha(k, dedupe or occurred, session, actor_qq, topic_qq)
    try:
        c = _db()
        if dk:
            row = c.execute("SELECT id FROM event WHERE dedupe=?", (dk,)).fetchone()
            if row:
                c.close()
                return row["id"]
        c.execute(
            "INSERT OR IGNORE INTO event(id,kind,ts,session,actor_qq,topic_qq,"
            "source,visibility,payload,dedupe,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (eid, k, occurred, str(session or "")[:80],
             person_id(actor_qq), person_id(topic_qq),
             str(source or "")[:24], str(visibility or "private")[:12],
             _j(payload or {}), dk, now))
        c.commit()
        c.close()
        return eid
    except Exception as e:
        print("[认知] 事件写入失败 " + type(e).__name__, flush=True)
        return ""


_TOOL_STATUS_UNSET = object()
_TOOL_EVIDENCE_ORIGIN = ContextVar("tool_evidence_origin", default=None)
_TOOL_OUTCOMES = {"success": True, "failure": False, "unknown": None}


@contextmanager
def tool_evidence_context(evidence_origin):
    """验证任务用 with tool_evidence_context('verification')；线程池须传播上下文。"""
    if evidence_origin not in ("live", "verification"):
        raise ValueError("evidence_origin must be live or verification")
    token = _TOOL_EVIDENCE_ORIGIN.set(evidence_origin)
    try:
        yield
    finally:
        _TOOL_EVIDENCE_ORIGIN.reset(token)


def record_tool_result(tool, ref, ok=_TOOL_STATUS_UNSET, *, session="", visibility="private",
                       reason="", ts=None, run_id=None, outcome=None, evidence_origin=None):
    """提交执行结果，不评估情绪。旧 bool 调用不变；未知必须显式 outcome='unknown'。

    显式来源 > ContextVar > 有分隔符的验证 session 前缀。工具名和执行 ID 去重，
    既不把同资源的两次执行合并，也不把同一次执行的重复观察当成新证据。
    """
    if outcome is None:
        if type(ok) is not bool:
            raise ValueError("tool result requires boolean ok or an explicit outcome")
        outcome = "success" if ok else "failure"
    if not isinstance(outcome, str) or outcome not in _TOOL_OUTCOMES:
        raise ValueError("outcome must be success, failure or unknown")
    status = _TOOL_OUTCOMES[outcome]
    if ok is not _TOOL_STATUS_UNSET and ok is not status:
        raise ValueError("ok conflicts with outcome")
    if evidence_origin is not None and evidence_origin not in ("live", "verification"):
        raise ValueError("evidence_origin must be live or verification")
    origin = evidence_origin or _TOOL_EVIDENCE_ORIGIN.get()
    if origin is None:
        verifying = re.match(r"^(?:verify|test|probe)(?:$|[:/_-])", str(session or ""), re.I)
        origin = "verification" if verifying else "live"
    import uuid
    execution = str(run_id or uuid.uuid4().hex)
    payload = {"tool": str(tool)[:24], "ok": status, "outcome": outcome,
               "ref": str(ref)[:80], "run_id": execution[:80], "reason": str(reason)[:80],
               "evidence_origin": origin}
    synthetic = origin == "verification"
    if synthetic:
        payload["synthetic"] = True
    return record_event(
        "tool_result", ts=ts, session=session,
        source="synthetic:tool" if synthetic else "tool_observation",
        visibility=visibility, payload=payload,
        dedupe="tool:" + _sha(_j([str(tool), execution])))


def get_event(eid):
    try:
        c = _ro_db()
        if c is None:
            return None
        row = c.execute("SELECT * FROM event WHERE id=?", (str(eid),)).fetchone()
        c.close()
        return dict(row) if row else None
    except Exception:
        return None


# ── 经历 ──────────────────────────────────────────
def record_episode(summary, event_ids=(), ts=None, perspective="self",
                   is_legacy=False):
    """一段经历：她参与了什么、结果如何。必须挂真实事件。"""
    summ = str(summary or "").strip()
    if not summ:
        return ""
    eids = [str(x) for x in (event_ids or []) if str(x or "").strip()]
    if not eids and not is_legacy:
        # 没有事件支撑的「经历」就是编的，宁可不要
        print("[认知] 拒绝无事件支撑的经历: " + summ[:30], flush=True)
        return ""
    pid = "epi_" + _sha(summ[:60], _now(ts), ",".join(eids))
    try:
        c = _db()
        c.execute("INSERT OR IGNORE INTO episode(id,ts,summary,event_ids,"
                  "perspective,is_legacy) VALUES(?,?,?,?,?,?)",
                  (pid, _now(ts), summ[:400], _j(eids),
                   str(perspective or "self")[:12], 1 if is_legacy else 0))
        c.commit()
        c.close()
        return pid
    except Exception as e:
        print("[认知] 经历写入失败 " + type(e).__name__, flush=True)
        return ""


# ── 信念：只能修订，不能覆盖 ────────────────────────
def add_belief(proposition, subject_qq="", confidence=None, source="",
               evidence=(), ts=None, is_legacy=False, spans=()):
    """写一条信念。给了 spans 就只挂 spans 里的事件。

    M02/X04：整理窗口读到 8 个事件不等于每条结论都有 8 份依据。逐命题 span
    存在时，belief_evidence 由 span 反推，整窗 evidence 一律忽略——否则
    「有事件号」又会被当成「有依据」。
    """
    prop = str(proposition or "").strip()
    if not prop:
        return ""
    bid = "bel_" + _sha(prop[:80], str(subject_qq or ""), _now(ts))
    now = _now(ts)
    rows = [s for s in (spans or []) if isinstance(s, dict) and s.get('event_id')]
    if rows:
        linked, seen = [], set()
        for span in rows:
            eid = str(span['event_id'])
            if eid not in seen:
                seen.add(eid)
                linked.append(eid)
    else:
        linked = [str(e) for e in (evidence or []) if e]
    try:
        c = _db()
        c.execute("INSERT OR IGNORE INTO belief(id,proposition,subject_qq,"
                  "confidence,status,source,first_ts,last_ts,is_legacy) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (bid, prop[:400], person_id(subject_qq),
                   None if confidence is None else float(confidence),
                   "active", str(source or "")[:24], now, now,
                   1 if is_legacy else 0))
        for eid in linked:
            c.execute("INSERT OR IGNORE INTO belief_evidence"
                      "(belief_id,event_id,relation,weight,ts) "
                      "VALUES(?,?,?,?,?)", (bid, eid, "support", 1.0, now))
        # 证据 quote 不截断，与 offset 保持同一口径（V01-D2）。
        for span in rows:
            c.execute("INSERT OR IGNORE INTO claim_span"
                      "(belief_id,event_id,quote,span_start,span_end,source_revision,"
                      "match_kind,subject_ref,subject_basis,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (bid, str(span['event_id']), str(span.get('quote') or ''),
                       int(span.get('span_start') or 0), int(span.get('span_end') or 0),
                       str(span.get('source_revision') or '')[:40],
                       str(span.get('match_kind') or 'model_quote')[:16],
                       str(span.get('subject_ref') or '')[:40],
                       str(span.get('subject_basis') or '')[:24], now))
        c.commit()
        c.close()
        return bid
    except Exception as e:
        print("[认知] 信念写入失败 " + type(e).__name__, flush=True)
        return ""


def quarantine_claim(restatement, kind="", reasons=(), evidence=(), subject_ref="",
                     source_session="", visibility="private", candidate_confidence=None,
                     decision="deferred", ts=None):
    """无依据或暂缓的命题留成隔离候选，不写进已核验信念（M02 / X05）。

    同一命题反复整理只留一条：dedupe 按文本+类型+判定，重复轮次不堆垃圾。
    """
    text = str(restatement or '').strip()
    if not text:
        return ''
    verdict = decision if decision in ('deferred', 'rejected') else 'deferred'
    key = _sha(text[:80], str(kind or ''), verdict)
    cid = 'clc_' + key
    try:
        c = _db()
        c.execute("INSERT OR IGNORE INTO claim_candidate"
                  "(id,restatement,kind,subject_ref,decision,reasons,evidence,"
                  "source_session,visibility,candidate_confidence,state,created,dedupe) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, text[:400], str(kind or '')[:24], str(subject_ref or '')[:40],
                   verdict, _j([str(r) for r in (reasons or []) if r]),
                   _j([dict(e) for e in (evidence or []) if isinstance(e, dict)]),
                   str(source_session or '')[:80],
                   visibility if visibility in ('private', 'group', 'public') else 'private',
                   None if candidate_confidence is None else float(candidate_confidence),
                   'pending_review', _now(ts), key))
        c.commit()
        c.close()
        return cid
    except Exception as e:
        print("[认知] 隔离候选写入失败 " + type(e).__name__, flush=True)
        return ''


def belief_spans(belief_id):
    """一条信念的逐命题原句来源，供审计与检索复核。"""
    try:
        c = _db()
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM claim_span WHERE belief_id=? ORDER BY event_id,span_start",
            (str(belief_id),)).fetchall()]
        c.close()
        return rows
    except Exception:
        return []


def pending_claims(limit=50, state="pending_review"):
    try:
        c = _ro_db()
        if c is None:
            return []
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM claim_candidate WHERE state=? ORDER BY created DESC LIMIT ?",
            (str(state), int(limit))).fetchall()]
        c.close()
        return rows
    except Exception:
        return []


def _person_qq_map(refs):
    """只读把 per_* 反查成 raw QQ。查不到留空，绝不写 person 行（X13/X15）。"""
    wanted = sorted({r for r in refs if str(r).startswith('per_')})
    if not wanted:
        return {}
    try:
        c = _ro_db()
        if c is None:
            return {}
        rows = c.execute("SELECT id,qq FROM person WHERE id IN (%s)"
                         % ','.join('?' * len(wanted)), wanted).fetchall()
        c.close()
        return {str(r['id']): str(r['qq'] or '') for r in rows}
    except Exception:
        return {}


def retrieval_beliefs(limit=200, *, query="", strict=False):
    """只读取出当前有效信念及其逐命题原句，供统一检索适配器（R01）。

    不写账本、不建人物。撤回/失效的信念不返回；它的证据事件若被撤回，
    对应 span 也一并剔除——旧投影不许把已撤回的依据复活（X06）。
    """
    c = None
    try:
        c = _ro_db()
        if c is None:
            return []
        sql = ("SELECT b.* FROM belief b WHERE b.status='active' AND NOT EXISTS "
               "(SELECT 1 FROM cognition_invalidation i WHERE i.kind='belief' "
               " AND i.object_id=b.id)")
        params = []
        terms = []
        if query:
            import text_tokens
            terms = [x for x in text_tokens.tokens(query) if len(x) >= 2][:12]
        if terms:
            sql += (" AND (" + " OR ".join(
                ["b.proposition LIKE ? ESCAPE '\\'"] * len(terms)) + ")")
            params.extend("%" + x.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                          for x in terms)
        sql += " ORDER BY b.last_ts DESC LIMIT ?"
        params.append(max(0, int(limit)))
        beliefs = [dict(r) for r in c.execute(sql, params).fetchall()]
        out = []
        for row in beliefs:
            spans = [dict(s) for s in c.execute(
                "SELECT * FROM claim_span WHERE belief_id=? ORDER BY event_id,span_start",
                (row['id'],)).fetchall()]
            refs = [str(r['event_id']) for r in c.execute(
                "SELECT event_id FROM belief_evidence WHERE belief_id=? AND relation='support'",
                (row['id'],)).fetchall()]
            live = []
            for eid in refs:
                usable = c.execute(
                    "SELECT e.payload FROM event e WHERE e.id=? AND NOT EXISTS "
                    "(SELECT 1 FROM evidence_retraction r WHERE r.event_id=e.id)",
                    (eid,)).fetchone()
                if usable:
                    payload = _uj(usable['payload'], {})
                    if not payload.get('synthetic') and payload.get('evidence_origin') != 'verification':
                        live.append(eid)
            row['evidence_event_ids'] = live
            row['evidence_spans'] = [s for s in spans if s['event_id'] in live]
            row['invalidated'] = not live and bool(refs)
            # 范围只能由真实来源事件决定，且取最窄的那个（§6.1 派生范围只能相同或更窄）。
            sessions, visibilities = [], []
            for eid in live:
                event = c.execute("SELECT session,visibility FROM event WHERE id=?",
                                  (eid,)).fetchone()
                if event:
                    sessions.append(str(event['session'] or ''))
                    visibilities.append(str(event['visibility'] or 'private'))
            row['source_sessions'] = sorted(set(s for s in sessions if s))
            row['visibility'] = ('private' if 'private' in visibilities
                                 else 'group' if 'group' in visibilities
                                 else 'private' if not visibilities else 'public')
            out.append(row)
        return out
    except Exception:
        if strict:
            raise
        return []
    finally:
        if c is not None:
            c.close()


def retrieval_events(query, scopes, *, limit=80, as_of_time=None):
    import text_tokens
    terms=sorted(x for x in text_tokens.tokens(query) if len(x)>=2)[:16]
    if not terms or not scopes:
        return []
    c=_ro_db()
    if c is None:
        return []
    try:
        scope_sql=' OR '.join(['session=?']*len(scopes))
        text_sql=' OR '.join(["payload LIKE ? ESCAPE '\\'"]*len(terms))
        params=list(scopes)+['%'+x.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%' for x in terms]
        params += [time.time() if as_of_time is None else float(as_of_time), max(0,int(limit))]
        rows=c.execute("SELECT e.* FROM event e WHERE ("+scope_sql+") AND ("+text_sql+") "
                       "AND kind IN ('group_message','private_message') AND ts<=? "
                       "AND NOT EXISTS (SELECT 1 FROM evidence_retraction r WHERE r.event_id=e.id) "
                       "ORDER BY ts DESC LIMIT ?",params).fetchall()
        result=[]
        for row in rows:
            item=dict(row);payload=_uj(item['payload'],{})
            if payload.get('synthetic') or payload.get('evidence_origin')=='verification':
                continue
            item['text']=str(payload.get('text') or '')
            result.append(item)
        return result
    finally:
        c.close()


def tool_receipts(tool, limit=400):
    """只读取出某个工具的真实执行回执：ref -> [event_id]（R01 用）。

    看图笔记这类资产本身不存来源 ID，它的依据是「哪次工具执行看到了它」，
    那条回执在认知库里。这里只做反查，不写账本。合成/验证样本与已撤回的
    观察一律不给：拿它们当来源等于让自检脚本冒充她的真实经历（X06）。
    """
    out = {}
    try:
        c = _ro_db()
        if c is None:
            return {}
        rows = c.execute(
            "SELECT e.id, e.payload FROM event e WHERE e.kind='tool_result' "
            "AND NOT EXISTS (SELECT 1 FROM evidence_retraction r WHERE r.event_id=e.id) "
            "ORDER BY e.created DESC LIMIT ?", (int(limit),)).fetchall()
        c.close()
        for row in rows:
            payload = _uj(row['payload'], {})
            if str(payload.get('tool') or '') != str(tool):
                continue
            if payload.get('synthetic') or payload.get('evidence_origin') != 'live':
                continue
            ref = str(payload.get('ref') or '')
            if ref:
                out.setdefault(ref, []).append(row['id'])
        return out
    except Exception:
        return {}


def source_window(event_ids, now=None):
    """把一轮整理读到的事件冻结成只读源记录（§20.3 的「本次冻结窗口」）。

    只读：不建人物、不写账本（X15 读路径不做隐式初始化）。event.actor_qq 列
    存的已经是 per_* 稳定 ID（X13），所以这里把它当 ref，再只读反查 raw QQ，
    两种身份形态都摆出来但不混用。她自己的回复是 action 不是 event，关于她
    自身行动的命题在这个窗口里拿不到原句，按规矩进隔离候选，等 M06/M07。
    """
    out, seen = [], set()
    for eid in (event_ids or []):
        key = str(eid or '').strip()
        if not key or key in seen:
            continue
        seen.add(key)
        event = get_event(key)
        if not event:
            continue
        payload = _uj(event['payload'], {})
        text = str(payload.get('text') or '')
        out.append({
            'event_id': event['id'], 'kind': event['kind'], 'text': text,
            'source_revision': _sha(text, event['created']),
            'occurred_at': event['ts'], 'session': event['session'],
            'visibility': event['visibility'],
            'actor_ref': str(event['actor_qq'] or ''),
            'topic_ref': str(event['topic_qq'] or ''),
            'synthetic': bool(payload.get('synthetic')) or payload.get('evidence_origin') == 'verification',
            'retracted': not evidence_valid('event', event['id']),
        })
    qq = _person_qq_map([r['actor_ref'] for r in out] + [r['topic_ref'] for r in out])
    for row in out:
        row['actor_qq'] = qq.get(row['actor_ref'], '')
        row['topic_qq'] = qq.get(row['topic_ref'], '')
    return out


def ingest_explicit_belief(event_id):
    """Only a literal, addressed, unquoted first-person preference; never inference."""
    event = get_event(event_id)
    if not event or event['kind'] not in ('private_message', 'group_message'):
        return ''
    payload = _uj(event['payload'], {})
    text = str(payload.get('text') or '').strip()
    if (not evidence_valid('event', event_id) or payload.get('addressed_to_self') is not True
            or payload.get('quoted') or payload.get('roleplay')
            or not re.fullmatch(r'我(?:喜欢|不喜欢)[^。！？?！\n“”"「」]{1,40}[。]? ', text + ' ')):
        return ''
    # The proposition reports what was said, not objective truth about the speaker.
    with _transaction() as c:
        prior = c.execute("SELECT belief_id FROM belief_evidence WHERE event_id=?", (event_id,)).fetchone()
        if prior:
            return prior['belief_id']
    with _transaction() as c:
        person = c.execute('SELECT qq FROM person WHERE id=?', (event['actor_qq'],)).fetchone()
    if not person:
        return ''
    return add_belief('对方明确自述：' + text, subject_qq=person['qq'],
                      source='explicit_self_report', evidence=[event_id], ts=event['ts'])


def add_evidence(belief_id, event_id, relation="support", weight=1.0, ts=None):
    rel = relation if relation in ("support", "contradict") else "support"
    try:
        c = _db()
        c.execute("INSERT OR IGNORE INTO belief_evidence"
                  "(belief_id,event_id,relation,weight,ts) VALUES(?,?,?,?,?)",
                  (str(belief_id), str(event_id), rel, float(weight), _now(ts)))
        c.execute("UPDATE belief SET last_ts=? WHERE id=?", (_now(ts), str(belief_id)))
        c.commit()
        c.close()
        return True
    except Exception as e:
        print("[认知] 证据写入失败 " + type(e).__name__, flush=True)
        return False


def revise_belief(belief_id, new_text, reason="", evidence=(), confidence=None,
                  ts=None):
    """修订一条信念。旧文本进历史，绝不原地覆盖。

    这是「什么改变了你的想法」能回答得出来的唯一原因：
    变了什么、为什么变、依据哪几件事，都留在这里。
    """
    new = str(new_text or "").strip()
    if not new:
        return ""
    now = _now(ts)
    eids = [str(x) for x in (evidence or []) if str(x or "").strip()]
    try:
        c = _db()
        row = c.execute("SELECT * FROM belief WHERE id=?", (str(belief_id),)).fetchone()
        if not row:
            c.close()
            return ""
        rid = "brev_" + _sha(belief_id, now, new[:40])
        c.execute("INSERT INTO belief_revision(id,belief_id,old_text,new_text,"
                  "reason,event_ids,ts) VALUES(?,?,?,?,?,?,?)",
                  (rid, str(belief_id), str(row["proposition"]), new[:400],
                   str(reason or "")[:200], _j(eids), now))
        c.execute("UPDATE belief SET proposition=?, last_ts=?, confidence=?"
                  " WHERE id=?",
                  (new[:400], now,
                   None if confidence is None else float(confidence),
                   str(belief_id)))
        for eid in eids:
            c.execute("INSERT OR IGNORE INTO belief_evidence"
                      "(belief_id,event_id,relation,weight,ts) VALUES(?,?,?,?,?)",
                      (str(belief_id), eid, "support", 1.0, now))
        c.commit()
        c.close()
        return rid
    except Exception as e:
        print("[认知] 信念修订失败 " + type(e).__name__, flush=True)
        return ""


def explain_belief(belief_id, limit=20):
    """这条想法现在是什么、改过几次、每次为什么、依据哪些事件。"""
    try:
        c = _db()
        b = c.execute("SELECT * FROM belief WHERE id=?", (str(belief_id),)).fetchone()
        if not b:
            c.close()
            return None
        ev = c.execute(
            "SELECT be.relation, be.weight, be.ts, e.id, e.kind, e.ts AS event_ts,"
            " e.payload, e.actor_qq, e.topic_qq FROM belief_evidence be "
            "JOIN event e ON e.id=be.event_id WHERE be.belief_id=? "
            "AND NOT EXISTS (SELECT 1 FROM evidence_retraction r WHERE r.event_id=e.id) "
            "ORDER BY be.ts DESC LIMIT ?", (str(belief_id), int(limit))).fetchall()
        rv = c.execute("SELECT * FROM belief_revision WHERE belief_id=? "
                       "ORDER BY ts", (str(belief_id),)).fetchall()
        c.close()
        return {"belief": dict(b),
                "evidence": [dict(x) for x in ev],
                "revisions": [dict(x) for x in rv],
                "changed_count": len(rv)}
    except Exception as e:
        print("[认知] 解释失败 " + type(e).__name__, flush=True)
        return None


# ── 承诺 ──────────────────────────────────────────
def add_commitment(to_qq, text, due_ts=None, evidence=(), ts=None):
    t = str(text or "").strip()
    if not t:
        return ""
    cid = "cmt_" + _sha(t[:60], str(to_qq or ""), _now(ts))
    try:
        c = _db()
        c.execute("INSERT OR IGNORE INTO commitment(id,to_qq,text,due_ts,status,"
                  "created,event_ids) VALUES(?,?,?,?,?,?,?)",
                  (cid, person_id(to_qq), t[:300],
                   None if due_ts is None else float(due_ts), "open", _now(ts),
                   _j([str(x) for x in (evidence or []) if x])))
        c.commit()
        c.close()
        return cid
    except Exception as e:
        print("[认知] 承诺写入失败 " + type(e).__name__, flush=True)
        return ""


def close_commitment(cid, status="done", evidence=()):
    if status not in ("done", "withdrawn", "missed"):
        return False
    try:
        c = _db()
        eids = _uj((c.execute("SELECT event_ids FROM commitment WHERE id=?",
                              (str(cid),)).fetchone() or {"event_ids": "[]"})["event_ids"], [])
        eids += [str(x) for x in (evidence or []) if x]
        c.execute("UPDATE commitment SET status=?, closed=?, event_ids=? WHERE id=?",
                  (status, _now(), _j(eids[-20:]), str(cid)))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


# ── 目标 ──────────────────────────────────────────
def add_goal(text, owner_qq="", motive="", origin="", success="",
             parent_event="", ts=None):
    t = str(text or "").strip()
    if not t:
        return ""
    gid = "gol_" + _sha(t[:60], str(owner_qq or ""), _now(ts))
    try:
        c = _db()
        c.execute("INSERT OR IGNORE INTO goal(id,owner_qq,text,motive,origin,"
                  "success,status,parent_event,created) VALUES(?,?,?,?,?,?,?,?,?)",
                  (gid, person_id(owner_qq), t[:200], str(motive or "")[:200],
                   str(origin or "")[:40], str(success or "")[:200],
                   "active", str(parent_event or ""), _now(ts)))
        c.commit()
        c.close()
        return gid
    except Exception as e:
        print("[认知] 目标写入失败 " + type(e).__name__, flush=True)
        return ""


def close_goal(gid, status="done"):
    if status not in ("done", "abandoned", "blocked"):
        return False
    try:
        c = _db()
        c.execute("UPDATE goal SET status=?, closed=? WHERE id=?",
                  (status, _now(), str(gid)))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def active_goals(limit=10):
    try:
        c = _db()
        rows = c.execute("SELECT * FROM goal WHERE status='active' "
                         "ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── 行动与回执 ────────────────────────────────────
def record_action(kind, target_qq="", topic_qq="", visibility="private",
                  scope="", text="", reason="", goal_id="", evidence=(),
                  state="submitted", ts=None):
    """一条对外动作。默认状态就是 submitted —— 调到这里时消息已经发出去了。

    真正的「接受/失败」由回执另写，见 mark_action 与 _handle_api_reply。
    """
    st = state if state in ACTION_STATES else "submitted"
    aid = "act_" + _sha(kind, str(target_qq or ""), _now(ts), text[:40])
    now = _now(ts)
    try:
        c = _db()
        c.execute("INSERT INTO action(id,kind,goal_id,target_qq,topic_qq,"
                  "visibility,scope,text,state,reason,event_ids,echo,created,updated) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, str(kind or "")[:24], str(goal_id or ""),
                   person_id(target_qq), person_id(topic_qq),
                   str(visibility or "private")[:12], str(scope or "")[:60],
                   str(text or "")[:400], st, str(reason or "")[:200],
                   _j([str(x) for x in (evidence or []) if x]), "", now, now))
        c.commit()
        c.close()
        return aid
    except Exception as e:
        print("[认知] 行动写入失败 " + type(e).__name__, flush=True)
        return ""


def latest_event_id(kind):
    """最近一条某类真实事件的 id（只读）。用作主动动作的生成触发证据。"""
    try:
        c = _db()
        row = c.execute("SELECT id FROM event WHERE kind=? ORDER BY created DESC LIMIT 1",
                        (str(kind),)).fetchone()
        c.close()
        return row["id"] if row else ""
    except Exception:
        return ""


def bind_action_echo(action_id, echo):
    try:
        c = _db()
        c.execute("UPDATE action SET echo=? WHERE id=?", (str(echo or "")[:80],
                                                          str(action_id)))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def mark_action(action_id, state, result="", source="onebot_receipt"):
    """按状态机推进。乱序/倒退的回执不写，避免「失败后又变成已送达」。

    source 说的是这条终态从哪来的：QQ 的回包是 onebot_receipt，
    她自己查证的结果是 tool_result。将来接审计时，这两种可信度不一样。
    """
    st = str(state or "")
    if not action_id or st not in ACTION_STATES:
        return False
    try:
        c = _db()
        row = c.execute("SELECT state FROM action WHERE id=?",
                        (str(action_id),)).fetchone()
        if not row:
            c.close()
            return False
        cur = str(row["state"])
        if st != cur and st not in _ALLOWED.get(cur, set()):
            print("[认知] 忽略越级回执 %s: %s -> %s" % (action_id, cur, st), flush=True)
            c.close()
            return False
        result_row = c.execute("SELECT scope, visibility, target_qq, topic_qq FROM action WHERE id=?",
                               (str(action_id),)).fetchone()
        now = _now()
        c.execute("UPDATE action SET state=?, updated=? WHERE id=?",
                  (st, now, str(action_id)))
        if st in ("accepted", "failed", "unknown"):
            eid = "evt_" + _sha("action_result", action_id, st)
            c.execute("INSERT OR IGNORE INTO event(id,kind,ts,session,actor_qq,topic_qq,"
                      "source,visibility,payload,dedupe,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (eid, "action_result", now, result_row["scope"], "",
                       result_row["topic_qq"], str(source or "onebot_receipt")[:24],
                       result_row["visibility"],
                       _j({"action_id": str(action_id), "state": st,
                           "result": str(result or "")[:80]}),
                       "receipt:" + str(action_id) + ":" + st, now))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def action_by_echo(echo):
    try:
        c = _db()
        row = c.execute("SELECT * FROM action WHERE echo=?", (str(echo),)).fetchone()
        c.close()
        return dict(row) if row else None
    except Exception:
        return None


# ── 自我认识修订 ──────────────────────────────────
def attach_action_evidence(action_id, event_ids):
    """单一写入者：给已存在的动作补挂真实证据事件。只改 event_ids。"""
    ids = [str(x) for x in (event_ids or []) if str(x or "").strip()]
    try:
        c = _db()
        cur = c.execute("UPDATE action SET event_ids=? WHERE id=?",
                        (_j(ids), action_id))
        c.commit()
        c.close()
        return cur.rowcount > 0
    except Exception:
        return False


def note_self_view(text, reason="", evidence=(), ts=None):
    t = str(text or "").strip()
    if not t:
        return ""
    rid = "svr_" + _sha(t[:60], _now(ts))
    try:
        c = _db()
        c.execute("INSERT OR IGNORE INTO self_view_rev(id,ts,text,reason,event_ids)"
                  " VALUES(?,?,?,?,?)",
                  (rid, _now(ts), t[:200], str(reason or "")[:200],
                   _j([str(x) for x in (evidence or []) if x])))
        c.commit()
        c.close()
        return rid
    except Exception:
        return ""


def self_view_history(limit=20):
    try:
        c = _db()
        rows = c.execute("SELECT * FROM self_view_rev WHERE NOT EXISTS "
                         "(SELECT 1 FROM cognition_invalidation i WHERE "
                         "i.kind='self_view_rev' AND i.object_id=self_view_rev.id) "
                         "ORDER BY ts DESC LIMIT ?",
                         (int(limit),)).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── 旧档案保守迁移 ────────────────────────────────
def import_legacy(rows, self_view="", dry_run=False):
    """把人格库里已有的条目搬进来，但**不假装知道它们是怎么来的**。

    旧条目只有「文本 + 写入时间」，没有当时的事件、也没有置信度。
    所以：
      · 时间用原记录的 ts，没有就 0（未知），不拿现在补；
      · 来源标 legacy，置信度留 None（不知道多可信）；
      · 不生成任何「经历」——没有事件支撑的经历是编的。
    幂等：用 meta 标记，重复调用不会翻倍。
    """
    try:
        c = _db()
        if not dry_run:
            done = c.execute("SELECT value FROM meta WHERE key='legacy_import_v1'").fetchone()
            if done:
                c.close()
                return {"ok": True, "skipped": True, "added": 0}
    except Exception:
        pass
    added = 0
    kind_map = {"facts": "fact", "likes": "preference", "dislikes": "preference",
                "curious": "curiosity", "experiences": "experience_text"}
    try:
        for r in (rows or []):
            text = str((r or {}).get("text") or "").strip()
            if not text:
                continue
            kind = kind_map.get(str((r or {}).get("kind") or ""), "legacy")
            ts = float((r or {}).get("ts") or 0) or None
            if kind == "experience_text":
                # 旧「经历」只有一句话，没有对应事件 —— 记为 legacy 事件本身，
                # 不当成已核实的经历
                added += 1
                if dry_run:
                    continue
                eid = record_event("legacy_note", ts=ts, source="legacy",
                                   payload={"text": text[:200], "kind": "experiences"},
                                   dedupe="legacy:exp:" + text[:80])
                if eid:
                    c2 = _db()
                    c2.execute("INSERT OR IGNORE INTO episode(id,ts,summary,"
                               "event_ids,perspective,is_legacy) VALUES(?,?,?,?,?,1)",
                               ("epi_" + _sha("legacy", text[:60]),
                                float(ts or 0), text[:400], _j([eid]), "self"))
                    c2.commit()
                    c2.close()
                continue
            added += 1
            if dry_run:
                continue
            add_belief(text, confidence=None, source="legacy",
                       is_legacy=True, ts=ts)
        if self_view and str(self_view).strip() and not dry_run:
            note_self_view(str(self_view).strip(), reason="legacy import",
                           ts=None)
        if not dry_run:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                      ("legacy_import_v1", _j({"ts": time.time(), "added": added})))
            c.commit()
        c.close()
    except Exception as e:
        print("[认知] 迁移失败 " + type(e).__name__ + ": " + str(e)[:80], flush=True)
        return {"ok": False, "added": added, "error": type(e).__name__}
    return {"ok": True, "added": added, "skipped": False}


def _archive_text(text):
    return " ".join(str(text or "").split())


def backfill_legacy_provenance(rows=None, apply=False):
    """给旧信念登记「这句话出自哪条旧档案」，不伪造经历、不冒充支持。

    relation='origin' 只说明文本来源；它不把旧信念变成已验证事实，
    也不改 is_legacy/source/confidence。对不上档案的旧信念保持无证据，
    并在 meta 里留下名单，等以后找到真实来源再说。
    """
    if rows is None:
        import persona_store
        rows = persona_store.all_rows(with_ids=True)
    archive = []
    for row in (rows or []):
        text = _archive_text((row or {}).get("text"))
        if text:
            archive.append({"id": (row or {}).get("id"),
                            "kind": str((row or {}).get("kind") or ""),
                            "text": text,
                            "ts": float((row or {}).get("ts") or 0)})
    exact = {}
    for row in archive:
        exact.setdefault(row["text"], []).append(row)
    try:
        c = _db()
        beliefs = [dict(r) for r in c.execute(
            "SELECT id, proposition, first_ts FROM belief WHERE is_legacy=1 "
            "AND NOT EXISTS (SELECT 1 FROM belief_evidence be "
            "WHERE be.belief_id=belief.id) ORDER BY id")]
        c.close()
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "linked": 0,
                "unmatched": [], "dry_run": not apply}
    linked = matched = 0
    matched_ids, unmatched, multi, failed = [], [], [], []
    for belief in beliefs:
        target = _archive_text(belief.get("proposition"))
        candidates = exact.get(target) or [
            row for row in archive
            if len(target) >= 6 and row["text"].startswith(target)]
        if not candidates:
            unmatched.append(belief["id"])
            continue
        candidates.sort(key=lambda row: (
            row["ts"] if row["ts"] > 0 else float("inf"),
            row["id"] if isinstance(row["id"], int) else 0))
        if len(candidates) > 1:
            multi.append(belief["id"])
        if not apply:
            matched += 1
            matched_ids.append(belief["id"])
            continue
        row = candidates[0]
        ts = row["ts"] if row["ts"] > 0 else float(belief.get("first_ts") or 0)
        eid = record_event(
            "legacy_note", ts=ts, source="legacy_archive",
            payload={"text": row["text"][:200], "kind": row["kind"],
                     "archive": "persona.db#memory:%s" % row["id"]},
            dedupe="legacy:belief:" + belief["id"])
        if not eid:
            failed.append(belief["id"])
            continue
        try:
            with _transaction() as c2:
                c2.execute("INSERT OR IGNORE INTO belief_evidence"
                           "(belief_id,event_id,relation,weight,ts) VALUES(?,?,?,?,?)",
                           (belief["id"], eid, "origin", 1.0, ts))
        except Exception:
            failed.append(belief["id"])
            continue
        linked += 1
        matched_ids.append(belief["id"])
    if apply:
        try:
            with _transaction() as c3:
                total_origin = int(c3.execute(
                    "SELECT COUNT(DISTINCT belief_id) FROM belief_evidence "
                    "WHERE relation='origin'").fetchone()[0] or 0)
                c3.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                           ("legacy_provenance_v1", _j({
                               "ts": time.time(), "linked_this_run": linked,
                               "total_origin": total_origin,
                               "unmatched": unmatched, "multi": multi,
                               "failed": failed})))
        except Exception:
            pass
    return {"ok": not failed, "dry_run": not apply, "scanned": len(beliefs),
            "linked": matched if not apply else linked,
            "matched_ids": matched_ids, "unmatched": unmatched,
            "multi": multi, "failed": failed}


@contextmanager
def _transaction():
    """New controls fail loudly, roll back atomically, and always close."""
    c = _db()
    try:
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def revise_layer(layer, content, reason):
    """Append content-addressed state history, without enabling expression control."""
    if layer not in ('identity', 'boundary', 'personality', 'work_state'):
        raise ValueError('unknown cognition layer')
    if not str(reason).strip():
        raise ValueError('revision reason required')
    if isinstance(content, dict):
        if layer == 'personality' and content.get('self_view'):
            content = {**content, 'self_view': consumable_self_view(content['self_view'])}
        if layer == 'identity' and content.get('text'):
            content = {**content, 'text': consumable_identity(content['text'])}
    raw = _canonical(content)
    digest = hashlib.sha256((layer + '\n' + raw).encode('utf-8')).hexdigest()
    with _transaction() as c:
        previous = c.execute('SELECT content_hash FROM layer_revision WHERE layer=? '
                             'ORDER BY seq DESC LIMIT 1', (layer,)).fetchone()
        if previous and previous['content_hash'] == digest:
            return digest
        c.execute('INSERT INTO layer_revision(layer,content_hash,content,reason,ts) '
                  'VALUES(?,?,?,?,?)', (layer, digest, raw, str(reason), time.time()))
    return digest


def layer_history(layer, limit=20):
    with _transaction() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM layer_revision WHERE layer=? '
                                          'ORDER BY seq DESC LIMIT ?', (layer, int(limit)))]
    for row in rows:
        row['content'] = json.loads(row['content'])
    return rows


def record_consent(recipient, action, decision, reason):
    """Record an explicit scoped decision; friendliness is not consent."""
    import uuid
    if decision not in ('allow', 'deny') or not all(
            str(x).strip() for x in (recipient, action, reason)):
        raise ValueError('explicit recipient, action, decision and reason required')
    ident = 'consent_' + uuid.uuid4().hex
    with _transaction() as c:
        c.execute('INSERT INTO consent_record VALUES(?,?,?,?,?,?)',
                  (ident, str(recipient), str(action), decision, str(reason), time.time()))
    return ident


def revoke_consent(consent_id, reason):
    if not str(reason).strip():
        raise ValueError('revocation reason required')
    with _transaction() as c:
        if not c.execute('SELECT 1 FROM consent_record WHERE id=?', (consent_id,)).fetchone():
            return False
        c.execute('INSERT OR IGNORE INTO consent_revocation VALUES(?,?,?)',
                  (consent_id, str(reason), time.time()))
    return True


def consent_decision(recipient, action):
    with _transaction() as c:
        decisions = {r['decision'] for r in c.execute(
            'SELECT decision FROM consent_record s WHERE recipient=? AND action=? '
            'AND NOT EXISTS (SELECT 1 FROM consent_revocation r WHERE r.consent_id=s.id)',
            (str(recipient), str(action)))}
    return 'deny' if 'deny' in decisions else 'allow' if 'allow' in decisions else 'unknown'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def prepare_outbox(logical_key, payload, parts=1):
    """Reserve immutable logical send intent. This does NOT authorize sending."""
    if not isinstance(logical_key, str) or not logical_key.strip():
        raise ValueError('logical key required')
    if type(parts) is not int or parts < 1:
        raise ValueError('positive part count required')
    raw = _canonical(payload)
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    with _transaction() as c:
        row = c.execute('SELECT * FROM action_outbox WHERE logical_key=?',
                        (logical_key,)).fetchone()
        if row:
            if row['digest'] != digest or row['parts'] != parts:
                raise ValueError('logical key payload conflict')
        else:
            c.execute('INSERT INTO action_outbox VALUES(?,?,?,?,?)',
                      (logical_key, digest, raw, parts, time.time()))
            c.executemany('INSERT INTO outbox_part(logical_key,part,state) VALUES(?,?,?)',
                          [(logical_key, n, 'pending') for n in range(parts)])
    return digest


def claim_outbox(logical_key, part=0):
    """Durably mark submitted BEFORE I/O. Ambiguous sends are never reclaimable."""
    with _transaction() as c:
        return c.execute("UPDATE outbox_part SET state='submitted' WHERE logical_key=? "
                         "AND part=? AND state='pending'", (logical_key, part)).rowcount == 1


def record_outbox_receipt(logical_key, part, state, receipt=''):
    if state not in ('accepted', 'failed', 'unknown'):
        raise ValueError('invalid receipt state')
    with _transaction() as c:
        row = c.execute('SELECT * FROM outbox_part WHERE logical_key=? AND part=?',
                        (logical_key, part)).fetchone()
        if not row:
            return False
        if row['state'] != 'submitted':
            return row['state'] == state and row['receipt'] == str(receipt)
        c.execute('UPDATE outbox_part SET state=?,receipt=? WHERE logical_key=? AND part=?',
                  (state, str(receipt), logical_key, part))
        return True


def get_outbox(logical_key):
    with _transaction() as c:
        row = c.execute('SELECT * FROM action_outbox WHERE logical_key=?',
                        (logical_key,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['receipts'] = [dict(r) for r in c.execute(
            'SELECT * FROM outbox_part WHERE logical_key=? ORDER BY part', (logical_key,))]
    states = {r['state'] for r in result['receipts']}
    result['state'] = ('pending' if 'pending' in states else
                       'submitted' if 'submitted' in states else
                       next(iter(states)) if len(states) == 1 else 'partial')
    return result


def retract_evidence(event_id, reason):
    """Withdraw evidentiary use, never rewrite the original observation."""
    if not str(reason).strip():
        raise ValueError('retraction requires reason')
    with _transaction() as c:
        if not c.execute('SELECT 1 FROM event WHERE id=?', (event_id,)).fetchone():
            return False
        c.execute('INSERT OR IGNORE INTO evidence_retraction VALUES(?,?,?)',
                  (event_id, reason, time.time()))
        for kind in ('episode', 'commitment', 'self_view_rev', 'action'):
            for row in c.execute('SELECT id,event_ids FROM ' + kind).fetchall():
                if event_id in _uj(row['event_ids'], []):
                    c.execute('INSERT OR IGNORE INTO cognition_invalidation VALUES(?,?,?)',
                              (kind, row['id'], event_id))
        c.execute("UPDATE goal SET status='invalidated' WHERE parent_event=?", (event_id,))
        for row in c.execute('SELECT id FROM goal WHERE parent_event=?', (event_id,)):
            c.execute('INSERT OR IGNORE INTO cognition_invalidation VALUES(?,?,?)',
                      ('goal', row['id'], event_id))
        c.execute("UPDATE belief SET status='invalidated', confidence=NULL WHERE id IN "
                  "(SELECT belief_id FROM belief_evidence WHERE event_id=?)", (event_id,))
    # Append new consumed-state versions, never edit historical layer contents.
    # Repeating a retraction also repairs a previously interrupted refresh.
    for layer in ('personality', 'identity'):
        history = layer_history(layer, limit=1)
        if history:
            revise_layer(layer, history[0]['content'], 'evidence retraction')
    return True


def consumable_identity(text):
    text = str(text or '')
    with _transaction() as c:
        withdrawn = [r['text'] for r in c.execute(
            "SELECT s.text FROM self_view_rev s WHERE EXISTS (SELECT 1 FROM cognition_invalidation i "
            "WHERE i.kind='self_view_rev' AND i.object_id=s.id)")]
    # An identity summary is a derived aggregate: without field-level provenance
    # conservatively drop the aggregate rather than leave a partial assertion.
    return '' if any(view and view in text and not consumable_self_view(view)
                     for view in withdrawn) else text


def cognition_cache_token():
    """Retractions override stable daily caches, including persisted snapshots."""
    with _transaction() as c:
        rows = [tuple(r) for r in c.execute(
            'SELECT kind,object_id,event_id FROM cognition_invalidation ORDER BY kind,object_id,event_id')]
        rows.extend(tuple(r) for r in c.execute(
            'SELECT event_id,reason,ts FROM evidence_retraction ORDER BY event_id'))
    return hashlib.sha256(_canonical(rows).encode('utf-8')).hexdigest()


def consumable_self_view(text):
    """Keep raw metadata/history immutable; suppress withdrawn derived text on read/save.

    A later *valid* revision can justify the same text again. Merely restoring an
    old metadata/layer version cannot do so.
    """
    text = str(text or '')
    if not text:
        return text
    with _transaction() as c:
        rows = c.execute('SELECT id,event_ids FROM self_view_rev WHERE text=? ORDER BY rowid DESC',
                         (text,)).fetchall()
        invalid = {r['object_id'] for r in c.execute(
            "SELECT object_id FROM cognition_invalidation WHERE kind='self_view_rev'")}
        if any(row['id'] in invalid for row in rows):
            latest = rows[0]
            refs = _uj(latest['event_ids'], [])
            # A new empty revision is not recomputation from valid evidence.
            if latest['id'] in invalid or not refs or any(not c.execute(
                    'SELECT 1 FROM event WHERE id=? AND NOT EXISTS '
                    '(SELECT 1 FROM evidence_retraction WHERE event_id=event.id)',
                    (eid,)).fetchone() for eid in refs):
                return ''
    return text


def evidence_valid(kind, object_id):
    """Conservative invalidation marker; history remains available for audit."""
    # 纯读判定：走只读连接，缺库时没有任何有效证据（X15，V01-D1）。
    c = _ro_db()
    if c is None:
        return False
    try:
        if kind == 'event':
            return bool(c.execute('SELECT 1 FROM event WHERE id=? AND NOT EXISTS '
                                  '(SELECT 1 FROM evidence_retraction WHERE event_id=event.id)',
                                  (object_id,)).fetchone())
        if kind == 'belief':
            return bool(c.execute("SELECT 1 FROM belief WHERE id=? AND status='active'",
                                  (object_id,)).fetchone())
        if kind not in ('episode', 'commitment', 'self_view_rev', 'action', 'goal'):
            return False
        if not c.execute('SELECT 1 FROM ' + kind + ' WHERE id=?', (object_id,)).fetchone():
            return False
        return not bool(c.execute('SELECT 1 FROM cognition_invalidation WHERE kind=? '
                                 'AND object_id=?', (kind, object_id)).fetchone())
    finally:
        c.close()


def stats():
    out = {}
    try:
        c = _db()
        for t in ("person", "event", "episode", "belief", "commitment",
                  "goal", "action", "self_view_rev"):
            out[t] = c.execute("SELECT COUNT(*) n FROM %s" % t).fetchone()["n"]
        out["beliefs_legacy"] = c.execute(
            "SELECT COUNT(*) n FROM belief WHERE is_legacy=1").fetchone()["n"]
        out["actions_by_state"] = {
            r["state"]: r["n"] for r in c.execute(
                "SELECT state, COUNT(*) n FROM action GROUP BY state")}
        c.close()
    except Exception as e:
        out["error"] = type(e).__name__
    return out


# ── M06：单一认知写入事务（只新增，不改上面任何函数） ─────────────
def write_cognition_transaction(plan):
    """M06：在一个连接、一个 BEGIN IMMEDIATE 事务里写入多个认知对象。

    现有显式 API（add_belief/record_episode/…）各自开连接各自 commit，
    多对象写入中途失败会留下半条信念/半套证据。本函数是统一写入入口
    （cognition_writer.commit）的事务辅助：plan 里的对象要么全部落账，
    要么任一失败由 _transaction() 整体回滚。它不替代也不修改任何现有
    函数——旧写入器保持原样，改线接管是 M07/M08 的范围（§3.3 旁路建内核）。

    plan（dict，ts 加至少一个对象；未知键直接报错，不静默忽略）：
      ts         写入时间戳，同一事务所有行共用同一时间
      belief     {object_id, proposition, subject_ref, confidence, source, spans[]}
                 spans 必须非空：没有逐命题原句来源就不写已核验信念
                 （M02/X04）；belief_evidence 由 spans 反推，绝不收整窗事件。
      episode    {object_id, summary, event_ids[], perspective, ts(可选,发生时间)}
      commitment {object_id, to_qq, text, due_ts, event_ids[]}
      candidate  {restatement, kind, subject_ref, decision(deferred/rejected),
                  reasons[], evidence[], source_session, visibility,
                  candidate_confidence}

    幂等沿用现有口径：belief/episode/commitment 用 object_id 派生稳定 ID
    + INSERT OR IGNORE（与 episode/commitment/goal 的内容派生 ID 同一模式），
    同一候选重复提交不堆垃圾；claim_candidate 复用 quarantine_claim 的
    文本+类型+判定 dedupe 键，与旧路径写下的候选也互相去重。

    人物解析复用 person_id 的 SQL，但在事务内同一连接上执行：直接调
    person_id() 会开第二条连接，与 BEGIN IMMEDIATE 互相锁死。per_* 稳定
    ID 原样使用，不为它补建 person 行（X13/X15 的保守口径）。

    返回 {对象类型: id}。失败原样抛异常（此时已整体回滚）：沉默的写入
    路径视为故障，调用方必须能听见是哪一步关了门。
    """
    if not isinstance(plan, dict):
        raise ValueError('cognition plan must be a dict')
    known = ('belief', 'episode', 'commitment', 'candidate')
    if not any(plan.get(k) is not None for k in known):
        raise ValueError('cognition plan carries no object')
    for key in plan:
        if key not in known + ('ts',):
            raise ValueError('unknown cognition plan key: %s' % key)
    now = _now(plan.get('ts'))
    out = {}
    with _transaction() as c:
        def person(ref):
            # 与 person_id 相同的 upsert，但在本事务的连接上执行。
            ref = str(ref or '').strip()
            if ref.startswith('per_'):
                return ref
            if not ref.isdigit():
                return ''
            pid = 'per_' + _sha(ref)
            row = c.execute('SELECT aliases FROM person WHERE id=?', (pid,)).fetchone()
            aliases = _uj(row['aliases'], []) if row else []
            c.execute(
                'INSERT INTO person(id,qq,aliases,first_seen,last_seen) VALUES(?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET aliases=excluded.aliases, last_seen=excluded.last_seen',
                (pid, ref, _j(aliases), now, now))
            return pid

        belief = plan.get('belief')
        if belief is not None:
            prop = str(belief.get('proposition') or '').strip()
            ident = str(belief.get('object_id') or '').strip()
            spans = [s for s in (belief.get('spans') or [])
                     if isinstance(s, dict) and s.get('event_id')]
            if not prop or not ident or not spans:
                raise ValueError('verified belief requires object_id, proposition and spans')
            bid = 'bel_' + _sha(ident)
            confidence = belief.get('confidence')
            c.execute("INSERT OR IGNORE INTO belief(id,proposition,subject_qq,"
                      "confidence,status,source,first_ts,last_ts,is_legacy) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (bid, prop[:400], person(belief.get('subject_ref')),
                       None if confidence is None else float(confidence),
                       'active', str(belief.get('source') or '')[:24], now, now, 0))
            linked, seen = [], set()
            for span in spans:
                eid = str(span['event_id'])
                if eid not in seen:
                    seen.add(eid)
                    linked.append(eid)
            for eid in linked:
                c.execute("INSERT OR IGNORE INTO belief_evidence"
                          "(belief_id,event_id,relation,weight,ts) "
                          "VALUES(?,?,?,?,?)", (bid, eid, 'support', 1.0, now))
            # 证据 quote 不截断：offset 是全长口径，截了 quote 就成
            # 无法逐字复核的假证据（V01-D2）。
            for span in spans:
                c.execute("INSERT OR IGNORE INTO claim_span"
                          "(belief_id,event_id,quote,span_start,span_end,source_revision,"
                          "match_kind,subject_ref,subject_basis,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (bid, str(span['event_id']), str(span.get('quote') or ''),
                           int(span.get('span_start') or 0), int(span.get('span_end') or 0),
                           str(span.get('source_revision') or '')[:40],
                           str(span.get('match_kind') or 'model_quote')[:16],
                           str(span.get('subject_ref') or '')[:40],
                           str(span.get('subject_basis') or '')[:24], now))
            out['belief'] = bid

        episode = plan.get('episode')
        if episode is not None:
            summ = str(episode.get('summary') or '').strip()
            ident = str(episode.get('object_id') or '').strip()
            eids = [str(x) for x in (episode.get('event_ids') or []) if str(x or '').strip()]
            if not summ or not ident or not eids:
                raise ValueError('episode requires object_id, summary and real events')
            pid = 'epi_' + _sha(ident)
            occurred = episode.get('ts')
            c.execute("INSERT OR IGNORE INTO episode(id,ts,summary,event_ids,"
                      "perspective,is_legacy) VALUES(?,?,?,?,?,?)",
                      (pid, now if occurred is None else float(occurred), summ[:400],
                       _j(eids), str(episode.get('perspective') or 'self')[:12], 0))
            out['episode'] = pid

        commitment = plan.get('commitment')
        if commitment is not None:
            text = str(commitment.get('text') or '').strip()
            ident = str(commitment.get('object_id') or '').strip()
            if not text or not ident:
                raise ValueError('commitment requires object_id and text')
            cid = 'cmt_' + _sha(ident)
            due = commitment.get('due_ts')
            c.execute("INSERT OR IGNORE INTO commitment(id,to_qq,text,due_ts,status,"
                      "created,event_ids) VALUES(?,?,?,?,?,?,?)",
                      (cid, person(commitment.get('to_qq')), text[:300],
                       None if due is None else float(due), 'open', now,
                       _j([str(x) for x in (commitment.get('event_ids') or []) if x])))
            out['commitment'] = cid

        candidate = plan.get('candidate')
        if candidate is not None:
            text = str(candidate.get('restatement') or '').strip()
            if not text:
                raise ValueError('candidate requires restatement')
            verdict = candidate.get('decision')
            if verdict not in ('deferred', 'rejected'):
                raise ValueError('candidate decision must be deferred or rejected')
            kind = str(candidate.get('kind') or '')[:24]
            key = _sha(text[:80], kind, verdict)
            cid = 'clc_' + key
            visibility = candidate.get('visibility')
            confidence = candidate.get('candidate_confidence')
            c.execute("INSERT OR IGNORE INTO claim_candidate"
                      "(id,restatement,kind,subject_ref,decision,reasons,evidence,"
                      "source_session,visibility,candidate_confidence,state,created,dedupe) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (cid, text[:400], kind, str(candidate.get('subject_ref') or '')[:40],
                       verdict, _j([str(r) for r in (candidate.get('reasons') or []) if r]),
                       _j([dict(e) for e in (candidate.get('evidence') or []) if isinstance(e, dict)]),
                       str(candidate.get('source_session') or '')[:80],
                       visibility if visibility in ('private', 'group', 'public') else 'private',
                       None if confidence is None else float(confidence),
                       'pending_review', now, key))
            out['candidate'] = cid
    return out


# ── M05：派生失效传播事务（只新增，不改上面任何函数） ─────────────
def write_invalidation_transaction(plan):
    """M05：修正记录 + 依赖边 + 失效状态 + epoch 推进，一个事务落账。

    计算在 invalidation_dag.propagate（纯函数）里完成；本函数只负责把
    计划原子写入：要么全部落账，要么整体回滚，绝不留下「写了失效表、
    epoch 没推进」或「推进了 epoch、快照没发布」的半截状态（§20.5 第5条）。

    plan（dict；未知键直接报错）：
      ts              写入时间戳
      expected_epoch  计算计划时读到的 epoch；与账本当前值不符立即拒绝
                      （乐观并发：两个提交撞车时后到者必须重算，不许
                      拿旧计划覆盖新失效）
      edges           新注册的 derived→source 边列表；逐条过环检查，
                      任何一条被拒就整体回滚
      corrections     correction() 产出的不可变记录；同 ID 幂等不重写
      propagation     propagate() 的完整计划；有 corrections 时必填，
                      且其 epoch 必须恰好等于 expected_epoch+1

    只注册边（无 corrections）不推进 epoch。返回 {epoch, edges_added,
    correction_ids, affected}。失败原样抛异常（此时已整体回滚）。
    """
    import invalidation_dag as dag
    if not isinstance(plan, dict):
        raise ValueError('invalidation plan must be a dict')
    known = ('ts', 'expected_epoch', 'edges', 'corrections', 'propagation')
    for key in plan:
        if key not in known:
            raise ValueError('unknown invalidation plan key: %s' % key)
    edges = [e for e in (plan.get('edges') or []) if isinstance(e, dict)]
    corrections = [c for c in (plan.get('corrections') or []) if isinstance(c, dict)]
    propagation = plan.get('propagation')
    if not edges and not corrections:
        raise ValueError('invalidation plan carries no edge or correction')
    if corrections and not isinstance(propagation, dict):
        raise ValueError('corrections require a propagation plan')
    now = _now(plan.get('ts'))
    with _transaction() as c:
        row = c.execute("SELECT value FROM meta WHERE key='validity_epoch'").fetchone()
        try:
            current = int(row['value']) if row else 0
        except (TypeError, ValueError):
            current = 0
        expected = plan.get('expected_epoch')
        if expected is None:
            if corrections:
                raise ValueError('corrections require expected_epoch')
            expected = current
        if int(expected) != current:
            raise ValueError('epoch_conflict: plan expected %s, ledger at %s'
                             % (expected, current))
        combined = [dict(r) for r in c.execute(
            'SELECT derived_id,source_id,derived_kind,source_kind,basis FROM derivation_edge')]
        for edge in edges:
            checked = dag.check_edge(combined, edge.get('derived_id'), edge.get('source_id'),
                                     edge.get('derived_kind', ''), edge.get('source_kind', ''))
            if not checked['ok']:
                raise ValueError('edge rejected: %s (%s -> %s)'
                                 % (checked['reason'], edge.get('derived_id'), edge.get('source_id')))
            combined.append(edge)
            c.execute('INSERT OR IGNORE INTO derivation_edge'
                      '(derived_id,source_id,derived_kind,source_kind,basis,ts) '
                      'VALUES(?,?,?,?,?,?)',
                      (str(edge.get('derived_id') or ''), str(edge.get('source_id') or ''),
                       str(edge.get('derived_kind') or '')[:24],
                       str(edge.get('source_kind') or '')[:24],
                       str(edge.get('basis') or '')[:40], now))
        for rec in corrections:
            if rec.get('kind') not in dag.CORRECTION_KINDS:
                raise ValueError('unknown correction kind: %s' % rec.get('kind'))
            if not str(rec.get('target_id') or '').strip() or not str(rec.get('reason') or '').strip():
                raise ValueError('correction requires target_id and reason')
            c.execute('INSERT OR IGNORE INTO correction_record'
                      '(id,kind,target_id,reason,scope,new_revision_id,ts) '
                      'VALUES(?,?,?,?,?,?,?)',
                      (str(rec.get('correction_id') or ''), rec['kind'],
                       str(rec['target_id']), str(rec['reason']),
                       str(rec.get('scope') or ''), str(rec.get('new_revision_id') or ''),
                       float(rec.get('ts') or now)))
        new_epoch = current
        affected = []
        if corrections:
            new_epoch = current + 1
            if int(propagation.get('epoch', -1)) != new_epoch:
                raise ValueError('propagation epoch mismatch: plan says %s, transaction would write %s'
                                 % (propagation.get('epoch'), new_epoch))
            for entry in (propagation.get('affected') or []):
                if not isinstance(entry, dict) or not str(entry.get('object_id') or '').strip():
                    raise ValueError('propagation entry requires object_id')
                if entry.get('status') not in dag.OBJECT_STATUSES:
                    raise ValueError('unknown object status: %s' % entry.get('status'))
                c.execute('INSERT INTO object_validity_state'
                          '(object_id,status,reasons,remaining_supports,revoked_scopes,epoch,updated) '
                          'VALUES(?,?,?,?,?,?,?) '
                          'ON CONFLICT(object_id) DO UPDATE SET status=excluded.status, '
                          'reasons=excluded.reasons, remaining_supports=excluded.remaining_supports, '
                          'revoked_scopes=excluded.revoked_scopes, epoch=excluded.epoch, '
                          'updated=excluded.updated',
                          (str(entry['object_id']), entry['status'],
                           _j([str(r) for r in (entry.get('reasons') or [])]),
                           _j([str(s) for s in (entry.get('remaining_supports') or [])]),
                           _j([str(s) for s in (entry.get('revoked_scopes') or [])]),
                           new_epoch, now))
                affected.append({'object_id': str(entry['object_id']), 'status': entry['status']})
            c.execute("INSERT INTO meta(key,value) VALUES('validity_epoch',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(new_epoch),))
            snapshot = propagation.get('snapshot') or {}
            c.execute('INSERT OR IGNORE INTO invalidation_snapshot'
                      '(epoch,snapshot_id,correction_ids,payload,ts) VALUES(?,?,?,?,?)',
                      (new_epoch, str(snapshot.get('snapshot_id') or ''),
                       _j([str(x) for x in (snapshot.get('correction_ids') or [])]),
                       _j(snapshot), now))
    return {'epoch': new_epoch, 'edges_added': len(edges),
            'correction_ids': [str(r.get('correction_id') or '') for r in corrections],
            'affected': affected}
