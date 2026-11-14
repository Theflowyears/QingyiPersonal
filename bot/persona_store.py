"""人格记忆库（SQLite）。

为什么换掉 JSON：
  原来 self.json 每次都要整个解析，条目涨到几万条时读一次就要上百毫秒，
  而且检索是纯线性扫描字符串。SQLite 能按索引取、只读需要的列，
  启动时也不用重新分词（分词结果存在 tokens 列里）。

设计取舍：
  · 分词结果（tokens）在写入时算好存下来，读取零成本
  · IDF 由 df 表实时统计，检索用「词命中 × IDF × 重要性」
  · embedding 列可选，存 1024 维 float32 的 BLOB；
    默认不参与检索——每轮调一次 embedding 要 0.7 秒，会吃掉响应预算，
    只在需要语义重排时才用
"""
import json
import math
import os
import re
import sqlite3
import time

def _dir():
    return os.getenv("PERSONA_DATA_DIR") or os.path.join(os.getenv("DATA_DIR", "./data"), "persona")


def _db_file():
    return os.path.join(_dir(), "persona.db")


def _legacy_file():
    return os.path.join(_dir(), "self.json")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    ts         REAL NOT NULL,
    importance REAL NOT NULL DEFAULT 2.0,
    hits       INTEGER NOT NULL DEFAULT 1,
    tokens     TEXT NOT NULL DEFAULT '',
    embedding  BLOB,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    evidence_spans   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);
CREATE INDEX IF NOT EXISTS idx_memory_ts ON memory(ts);
CREATE INDEX IF NOT EXISTS idx_memory_imp ON memory(importance);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_CAND_CAP = int(os.getenv("PERSONA_CAND_CAP", "2000"))
_conn = None
_conn_path = ""
_tok_cache = {"tokens": None, "df": None, "inv": None, "texts": None,
               "imps": None, "kinds": None, "n": 0, "built": 0.0}


def _jieba():
    try:
        import jieba
        return jieba
    except Exception:
        return None


def tokenize(text):
    """系统唯一分词口径（见 text_tokens）。

    以前这里注释写着「和群事实库同一套分词」，实际两件事都没做：
    没载她的专属词典 persona_dict.txt，也没过停用词表。
    同一句话在两个库里切成不同样子，倒排互相打脸 ——
    「主人喜欢X」这类条目按用户原话就检索不到。现在统一走 text_tokens。
    """
    try:
        import text_tokens
        return sorted(text_tokens.tokens(text))
    except Exception:
        pass
    # 兜底：新模块出问题时退回旧的本地写法，宁可口径不一致也别让检索空掉
    t = str(text or "")
    if not t:
        return []
    out = []
    j = _jieba()
    if j is not None:
        for w in j.cut(t):
            w = w.strip()
            if not w or len(w) < 2:
                continue
            if not re.search(r"[\w一-龥]", w, re.UNICODE):
                continue
            out.append(w.lower())
    for seg in re.findall(r"[一-龥]+", t):
        for k in range(len(seg) - 1):
            out.append(seg[k:k + 2])
    for w in re.findall(r"[A-Za-z0-9]{2,}", t):
        out.append(w.lower())
    return out


def _migrate_source_columns(connection):
    """M02 可回退迁移：旧库没有逐命题来源列，只加列、不动既有行。

    旧行的来源保持 '[]' —— 未知就是未知，不拿整理窗口的事件号回填成
    「这条记忆有依据」（那正是 X04 要修的假绿）。
    """
    present = {str(row[1]) for row in connection.execute("PRAGMA table_info(memory)")}
    for name, declaration in (("source_event_ids", "TEXT NOT NULL DEFAULT '[]'"),
                              ("evidence_spans", "TEXT NOT NULL DEFAULT '[]'")):
        if name not in present:
            connection.execute("ALTER TABLE memory ADD COLUMN %s %s" % (name, declaration))
    connection.commit()


def conn():
    """按当前 DATA_DIR 取连接。

    路径必须在调用时算：这里原来只在 import 时算一次，还把连接缓存下来。
    后果不是线上问题（进程里 DATA_DIR 不变），而是测试 —— 换一个新的
    DATA_DIR 仍然读写第一个用例的库，于是「她的人格档案变了没有」这种判断
    拿到的是别人的档案，证据链的测试能假绿。
    """
    global _conn, _conn_path
    path = _db_file()
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
    _conn_path = path
    _conn.row_factory = sqlite3.Row
    _conn.executescript(_SCHEMA)
    _migrate_source_columns(_conn)
    try:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    _conn.commit()
    _invalidate()
    return _conn


def _invalidate():
    _tok_cache["tokens"] = None
    _tok_cache["df"] = None
    _tok_cache["built"] = 0.0


def _importance_of(text):
    """和 group_memory_store._importance 同一套启发式。"""
    t = str(text or "")
    s = 0.0
    if re.search(("主人|阿甲|阿乙|阿丙|阿丁|阿戊|阿己|清漪|爸爸|妈妈|女儿|家人"
 + ("|" + "|".join(re.escape(x) for x in os.getenv("PERSON_NAME_TOKENS", "").split(",") if x.strip()))
 if os.getenv("PERSON_NAME_TOKENS", "").strip() else ""), t):
        s += 1.8
    if re.search(r"喜欢|讨厌|害怕|难过|开心|心疼|生气|委屈|撒娇|担心|想念|答应|承诺"
                 r"|第一次|最重要|记得|不许|再也|永远|一直|以后|秘密", t):
        s += 1.6
    if re.search(r"\d", t):
        s += 0.4
    if len(t) >= 24:
        s += 0.6
    if re.match(r"^(在吗|在不在|哈哈+|嗯+|哦+|好的?|收到|早|晚安|早安|吃了吗|睡了)$", t.strip()):
        s -= 1.6
    if len(t) <= 8:
        s -= 0.5
    return max(0.0, min(s, 5.0))


def add(kind, text, ts=None, hits=1, source_event_ids=(), evidence_spans=()):
    """加一条记忆。返回 id；已存在相同文本则只加 hits。

    source_event_ids/evidence_spans 是**这一条**自己的来源，不是整轮整理窗口。
    同一条文本再次出现时合并各自的来源：同一命题的多次真实出现可以累积，
    但绝不把别的命题的事件号并进来。
    """
    t = str(text or "").strip()
    if not t:
        return None
    c = conn()
    ids = [str(x) for x in (source_event_ids or []) if str(x or '').strip()]
    spans = [dict(x) for x in (evidence_spans or []) if isinstance(x, dict)]
    row = c.execute("SELECT id, hits, source_event_ids, evidence_spans FROM memory "
                    "WHERE kind=? AND text=? LIMIT 1", (kind, t)).fetchone()
    if row:
        c.execute("UPDATE memory SET hits=hits+1 WHERE id=?", (row["id"],))
        if ids or spans:
            old_ids = json.loads(row["source_event_ids"] or "[]")
            old_spans = json.loads(row["evidence_spans"] or "[]")
            merged_ids = list(old_ids) + [x for x in ids if x not in old_ids]
            keys = {(s.get("event_id"), s.get("span_start"), s.get("span_end"))
                    for s in old_spans}
            merged_spans = list(old_spans) + [
                s for s in spans
                if (s.get("event_id"), s.get("span_start"), s.get("span_end")) not in keys]
            c.execute("UPDATE memory SET source_event_ids=?, evidence_spans=? WHERE id=?",
                      (json.dumps(merged_ids, ensure_ascii=False),
                       json.dumps(merged_spans, ensure_ascii=False), row["id"]))
        c.commit()
        return row["id"]
    cur = c.execute(
        "INSERT INTO memory(kind, text, ts, importance, hits, tokens,"
        " source_event_ids, evidence_spans) VALUES(?,?,?,?,?,?,?,?)",
        (kind, t, float(ts or time.time()), _importance_of(t), int(hits),
         " ".join(tokenize(t)), json.dumps(ids, ensure_ascii=False),
         json.dumps(spans, ensure_ascii=False)))
    c.commit()
    _invalidate()
    return cur.lastrowid


def replace_by_prefix(kind, prefix, text, ts=None):
    """kind 里凡是开头命中 prefix 的旧条目全删掉，只留这一条新的。

    给「每人一条好感」这种**状态型记忆**用：状态变了就覆盖，
    别再往历史堆里叠——叠多了检索全是过期数字。
    """
    t = str(text or "").strip()
    if not t:
        return None
    c = conn()
    old = c.execute(
        "SELECT id FROM memory WHERE kind=? AND text LIKE ?",
        (kind, str(prefix) + "%")).fetchall()
    for r in old:
        c.execute("DELETE FROM memory WHERE id=?", (r["id"],))
    new_id = add(kind, t, ts=ts)
    c.commit()
    _invalidate()
    return new_id


def add_many(kind, items, ts=None, sources=None):
    """批量加记忆。sources 是「文本 → 这一条自己的来源」的映射。

    不给 sources 就是无来源写入（旧背景），绝不把整轮窗口摊给每条。
    """
    n = 0
    table = sources if isinstance(sources, dict) else {}
    for x in items or []:
        entry = table.get(str(x or '').strip()) or {}
        if add(kind, x, ts=ts,
               source_event_ids=entry.get('source_event_ids') or (),
               evidence_spans=entry.get('evidence_spans') or ()):
            n += 1
    return n


def sourced_items(kind, limit=0):
    """带逐命题来源的条目，供只读检索适配器使用（R01）。"""
    c = conn()
    sql = ("SELECT id, text, ts, source_event_ids, evidence_spans FROM memory "
           "WHERE kind=? ORDER BY id")
    args = [kind]
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    out = []
    for row in c.execute(sql, args):
        out.append({"id": row["id"], "text": row["text"], "ts": row["ts"],
                    "source_event_ids": json.loads(row["source_event_ids"] or "[]"),
                    "evidence_spans": json.loads(row["evidence_spans"] or "[]")})
    return out


def unsourced_items(limit=8):
    """还没绑逐命题来源的记忆，供人工标注面板挑样本。"""
    c = conn()
    rows = c.execute(
        "SELECT id, kind, text, ts FROM memory WHERE source_event_ids IN ('','[]') "
        "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def set_sources(memory_id, event_ids):
    """人工/自动把这一条记忆绑定到真实事件号。只改来源，不动 hits/文本。"""
    ids = [str(x) for x in (event_ids or []) if str(x or "").strip()]
    c = conn()
    row = c.execute("SELECT id FROM memory WHERE id=?", (memory_id,)).fetchone()
    if not row:
        return False
    c.execute("UPDATE memory SET source_event_ids=? WHERE id=?",
              (json.dumps(ids, ensure_ascii=False), memory_id))
    c.commit()
    return True


def by_id(memory_id):
    c = conn()
    row = c.execute("SELECT id, kind, text, ts, source_event_ids FROM memory WHERE id=?",
                    (memory_id,)).fetchone()
    return dict(row) if row else None


def delete_by_id(memory_id):
    """按 id 删一条记忆（人工清理无法绑定来源的 legacy）。返回是否删到。"""
    c = conn()
    cur = c.execute("DELETE FROM memory WHERE id=?", (memory_id,))
    c.commit()
    _invalidate()
    return cur.rowcount > 0


def items(kind, limit=0):
    c = conn()
    sql = "SELECT text FROM memory WHERE kind=? ORDER BY id"
    args = [kind]
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [r["text"] for r in c.execute(sql, args)]


def counts():
    c = conn()
    out = {}
    for r in c.execute("SELECT kind, COUNT(*) n FROM memory GROUP BY kind"):
        out[r["kind"]] = r["n"]
    return out


def get_meta(key, default=None):
    try:
        r = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not r:
            return default
        value = json.loads(r["value"])
        if key == 'self_view':
            import self_model
            value = self_model.consumable_self_view(value)
        return value
    except Exception:
        return default


def set_meta(key, value):
    c = conn()
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(value, ensure_ascii=False)))
    c.commit()


def delete(kind, text):
    c = conn()
    c.execute("DELETE FROM memory WHERE kind=? AND text=?", (kind, str(text)))
    c.commit()
    _invalidate()


# ── 检索 ─────────────────────────────────────────────
def _load_tokens():
    """把全部词的倒排、正文、重要性载进内存。只在数据变动后重建。

    2 万条时全表扫要 200ms，每轮都付不起。
    建一次倒排（token -> [id]），之后按查询词直接取候选，只给候选打分。
    """
    if _tok_cache["tokens"] is not None:
        return _tok_cache
    t0 = time.time()
    c = conn()
    toks, df, inv, texts, imps, kinds = {}, {}, {}, {}, {}, {}
    n = 0
    for r in c.execute("SELECT id, kind, text, importance, tokens FROM memory"):
        i = r["id"]
        ws = [w for w in str(r["tokens"] or "").split() if w]
        toks[i] = set(ws)
        texts[i] = r["text"]
        imps[i] = float(r["importance"] or 0)
        kinds[i] = r["kind"]
        for w in ws:
            df[w] = df.get(w, 0) + 1
            inv.setdefault(w, []).append(i)
        n += 1
    _tok_cache.update({"tokens": toks, "df": df, "inv": inv, "texts": texts,
                       "imps": imps, "kinds": kinds, "n": max(1, n),
                       "built": time.time()})
    print("[人格库] 倒排载入 %d 条 / %d 词，%.0f ms"
          % (n, len(df), (time.time() - t0) * 1000), flush=True)
    return _tok_cache


def _idf(df, n, w):
    d = df.get(w, 0)
    if d <= 0:
        return 0.0
    return max(0.3, min(math.log(1.0 + n / float(d)), 4.0))


def search(query, kinds=("facts", "experiences"), limit=6):
    """按「词命中 × IDF × 重要性」挑最相关的几条。走倒排，零外部调用。"""
    q = str(query or "").strip()
    cache = _load_tokens()
    inv = cache["inv"] or {}
    texts, imps, kindsmap = cache["texts"] or {}, cache["imps"] or {}, cache["kinds"] or {}
    want = set(kinds)

    def fallback():
        c = conn()
        ph = ",".join("?" * len(kinds))
        sql = ("SELECT text FROM memory WHERE kind IN (%s) "
               "ORDER BY importance DESC, id DESC LIMIT ?" % ph)
        return [r["text"] for r in c.execute(sql, list(kinds) + [limit])]

    if not q:
        return fallback()

    qt = set(tokenize(q))
    if not qt:
        # 这句本身没实词（「笑死我了」）。以前这里直接 fallback 到
        # 「按 importance 排的老条目」，等于每次闲聊都固定端上同一批档案 ——
        # 她拿到一串跟眼前无关的旧事，只能硬编，提示词里再喊一句「别报菜名」。
        try:
            import recall_link as _rl
            if not _rl.query_searchable(q):
                return []
        except Exception:
            pass
        return fallback()

    # 候选：从最稀有的词开始取，够了就停。
    # 「主人」这种词能命中几千条，全捞出来打分要 100ms+，每轮都付不起。
    buckets = []
    for w in qt:
        got = inv.get(w)
        if got:
            buckets.append((len(got), got))
    buckets.sort(key=lambda x: x[0])
    cand = set()
    for _size, got in buckets:
        for i in got:
            cand.add(i)
            if len(cand) >= _CAND_CAP:
                break
        if len(cand) >= _CAND_CAP:
            break
    if not cand:
        return fallback()

    df, n = cache["df"], cache["n"]
    scored = []
    for i in cand:
        if kindsmap.get(i) not in want:
            continue
        common = qt & (cache["tokens"].get(i) or set())
        if not common:
            continue
        s = sum(_idf(df, n, w) for w in common) * 2.0
        s += imps.get(i, 0.0) * 0.6
        scored.append((s, texts.get(i) or ""))
    if not scored:
        return fallback()
    scored.sort(key=lambda x: -x[0])
    return [t for _s, t in scored[:limit]]


def search_within(items, query, keep=5):
    """只在给定的这批文本里按相关度排序。

    调用方可能只想要 facts 或只要 experiences，
    直接查库会混进另一种，所以按传入的集合打分。
    """
    arr = [str(x or "").strip() for x in (items or []) if str(x or "").strip()]
    if not arr:
        return []
    q = str(query or "").strip()
    if not q:
        return arr[-keep:]
    qt = set(tokenize(q))
    if not qt:
        return arr[-keep:]
    cache = _load_tokens()
    df, n = cache["df"], cache["n"]
    scored = []
    for t in arr:
        ms = set(tokenize(t))
        common = qt & ms
        if not common:
            continue
        sc = sum(_idf(df, n, w) for w in common) * 2.0
        sc += _importance_of(t) * 0.6
        scored.append((sc, t))
    if not scored:
        return arr[-keep:]
    scored.sort(key=lambda x: -x[0])
    return [t for _s, t in scored[:keep]]


# ── 迁移：self.json -> SQLite ─────────────────────────
_KINDS = ("facts", "experiences", "likes", "dislikes", "curious")


def migrate_from_json(force=False):
    """把旧的 self.json 一次性搬进 SQLite。已迁移过就跳过。"""
    if not os.path.isfile(_legacy_file()):
        return 0
    if not force and get_meta("migrated_from_json"):
        return 0
    try:
        d = json.load(open(_legacy_file(), encoding="utf-8"))
    except Exception:
        return 0
    n = 0
    for k in _KINDS:
        for x in (d.get(k) or []):
            if add(k, x):
                n += 1
    for k in ("self_view", "voice", "stats", "last_consolidated", "revision", "updated"):
        if k in d:
            set_meta(k, d[k])
    set_meta("migrated_from_json", time.time())
    print("[人格库] 从 self.json 迁移 %d 条" % n, flush=True)
    return n


def migrate_tokens(dry_run=False):
    """按新口径重算全部条目的 tokens 列。

    tokens 是写入时预存的（见 add / _load_tokens），分词口径一改，
    旧数据就永远按旧口径被检索 —— 这就是「存了但搜不到」的另一半。
    幂等：重算成一样就不写；只动 tokens 列，条数/ts/importance 都不碰。
    调用前请先备份 persona.db。
    """
    c = conn()
    rows = list(c.execute("SELECT id, kind, text, tokens FROM memory"))
    changed = 0
    for r in rows:
        want = " ".join(tokenize(r["text"] or ""))
        if str(r["tokens"] or "") == want:
            continue
        changed += 1
        if not dry_run:
            c.execute("UPDATE memory SET tokens=? WHERE id=?", (want, r["id"]))
    if not dry_run and changed:
        c.commit()
        _invalidate()
    total = len(rows)
    print("[人格库] tokens 迁移：应改 %d / 共 %d 条%s"
          % (changed, total, "（dry-run 未写）" if dry_run else ""), flush=True)
    return {"total": total, "rewritten": changed, "dry_run": bool(dry_run)}


def all_rows(kinds=None, with_ids=False):
    """只读导出：给认知账本的保守迁移/出处回填用（带 ts，不是只有文本）。

    with_ids=True 时额外返回档案行 id，供 belief_evidence 记录真实出处；
    默认形状保持不变，避免旧调用方把多余字段当成文本。
    """
    ks = tuple(kinds or _KINDS)
    ph = ",".join("?" * len(ks))
    columns = "id, kind, text, ts" if with_ids else "kind, text, ts"
    try:
        c = conn()
        rows = c.execute("SELECT %s FROM memory WHERE kind IN (%s) "
                         "ORDER BY id" % (columns, ph), list(ks)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def snapshot():
    """给面板看的完整档案（字段沿用旧的 self.json 结构）。"""
    out = {k: items(k) for k in _KINDS}
    for k in ("self_view", "voice", "stats", "last_consolidated", "revision", "updated"):
        v = get_meta(k)
        if v is not None:
            out[k] = v
    out["_file"] = _db_file()
    out["_legacy"] = _legacy_file()
    out["_counts"] = counts()
    return out


def stats():
    c = conn()
    total = c.execute("SELECT COUNT(*) n FROM memory").fetchone()["n"]
    has_emb = c.execute("SELECT COUNT(*) n FROM memory WHERE embedding IS NOT NULL").fetchone()["n"]
    cache = _load_tokens()
    return {"total": total, "kinds": counts(), "vocab": len(cache["df"]),
            "embedded": has_emb, "db": _db_file()}
