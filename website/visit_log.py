"""访问计数的口径全在这里：什么算一次访客，以及旧记录被删掉后累计怎么不丢。

两件事都是被现网数据逼出来的：

1. 「累计访问量」以前数的是 `visits` 里近 30 天的行数。这张表按保留期删行，
   所以那个数字永远不可能超过保留期，等它涨到某个值就再也不动了 —— 它根本不是累计。
2. 上报时只排掉了 `/admin/api/`，于是管理员自己开后台、后台的 js/css 都被当成访客：
   现网 1000 条里 628 条是 `/admin*`（含 173 条 `/admin/login`），
   真正的站点首页只有 129 条。面板上那句「后台不记」当时是假的。

`visit_days` 是按天累加的计数器，不参与保留期删除，所以累计是累计。
第一次建表时从现存明细回填，历史不会凭空变成 0。
"""
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

# 后台、接口、静态资源、图标 —— 这些都不是「有人来逛网站」。
SKIP_PREFIXES = ("/admin", "/api/", "/static/", "/favicon")


def is_visitor_path(path):
    p = str(path if path is not None else "/")
    if not p.startswith("/"):
        p = "/" + p
    return not any(p.startswith(x) for x in SKIP_PREFIXES)


# SQL 侧用的同一个谓词。口径只有一处定义：Python 判一遍、SQL 再写一遍，
# 早晚分叉 —— 现网就出现过「累计剔掉后台、今日没剔」，于是 24 小时那格
# 报 385 次，而同一天折线图上只有 147 次。
# NULL 路径算访客（和 is_visitor_path(None) 一致）：SQLite 里 NULL NOT LIKE
# 结果是 NULL，不加这句会把老记录整批漏掉。
PUBLIC_SQL = "(path IS NULL OR (" + " AND ".join("path NOT LIKE ?" for _ in SKIP_PREFIXES) + "))"
PUBLIC_PARAMS = tuple(x + "%" for x in SKIP_PREFIXES)


def day_of(ts):
    """ISO 时间戳 → 中国标准时间的日期字符串。"""
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return str(ts or "")[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CST).date().isoformat()


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visit_days (
            day  TEXT PRIMARY KEY,
            hits INTEGER NOT NULL DEFAULT 0
        )
    """)
    row = conn.execute("SELECT 1 FROM visit_days LIMIT 1").fetchone()
    if row:
        return 0
    # 空表 = 第一次上线这段逻辑。已经删掉的没法追，但现存明细里的每一天要认。
    back = {}
    for path, ts in conn.execute("SELECT path, ts FROM visits").fetchall():
        if not is_visitor_path(path):
            continue
        back[day_of(ts)] = back.get(day_of(ts), 0) + 1
    for day, hits in back.items():
        conn.execute("INSERT OR REPLACE INTO visit_days(day, hits) VALUES(?,?)",
                     (day, hits))
    return len(back)


def bump(conn, ts):
    day = day_of(ts)
    if not day:
        return
    conn.execute("""
        INSERT INTO visit_days(day, hits) VALUES(?, 1)
        ON CONFLICT(day) DO UPDATE SET hits = hits + 1
    """, (day,))


def lifetime(conn):
    """(累计次数, 最早一天, 最新一天)。表是空的就把最早一天如实报成 None。"""
    row = conn.execute("SELECT COALESCE(SUM(hits),0), MIN(day), MAX(day) FROM visit_days").fetchone()
    return int(row[0] or 0), row[1], row[2]


def series(conn, days):
    """按给定日期列表取每天的计数，缺的那天如实给 0（而不是消失）。"""
    have = {r[0]: int(r[1]) for r in
            conn.execute("SELECT day, hits FROM visit_days WHERE day>=? AND day<=?",
                         (min(days), max(days))).fetchall()} if days else {}
    return [have.get(d, 0) for d in days]
