# -*- coding: utf-8 -*-
"""证据介入：把缺证据的动作补挂真实来源、把自我修订的累积状态摊开给人看。

只读收集 + 显式修复，绝不补造：
- repair_action 只把动作挂到「同会话、发生在它之前的真实消息事件」上；
  找不到真实来源就返回 not_found，交给人工，不凭空挂。
- self_view_stats 只读 self_view_rev，告诉人「上次修订多久前、窗口内几条」。
"""
import json
import os
import time
from contextlib import closing

DIRECT_KINDS = ("message", "group_message", "private_message", "tool_result")


def _db():
    import sqlite3
    path = os.path.join(os.getenv("DATA_DIR", "./data"), "persona", "cognition.db")
    c = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def unlinked_actions(window=86400):
    """窗口内没挂有效证据的动作（空 refs / 悬空 / 不可用）。"""
    at = time.time()
    with closing(_db()) as c:
        events = {r["id"] for r in c.execute("SELECT id FROM event")}
        rows = c.execute(
            "SELECT id,kind,state,scope,event_ids,created FROM action "
            "WHERE (created>=? AND created<=?) OR (updated>=? AND updated<=?)",
            (at - window, at, at - window, at)).fetchall()
    out = []
    for r in rows:
        refs = json.loads(r["event_ids"] or "[]")
        ok = bool(refs) and all(e in events for e in refs)
        if not ok:
            out.append({"id": r["id"], "kind": r["kind"], "state": r["state"],
                        "scope": r["scope"], "created": r["created"], "refs": refs})
    return out


def repair_action(action_id):
    """把动作挂到真实触发事件。优先同会话入站消息；主动发送则挂生成请求。

    只读挑选候选，写入统一走 self_model（单一写入者）。找不到不补造。
    """
    with closing(_db()) as c:
        row = c.execute("SELECT scope, created FROM action WHERE id=?", (action_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "没有这个动作"}
        scope = row["scope"] or ""
        created = row["created"]
        ev = c.execute(
            "SELECT id FROM event WHERE session=? AND kind IN (%s) AND created<=? "
            "ORDER BY created DESC LIMIT 1" % ",".join("?" * len(DIRECT_KINDS)),
            (scope, *DIRECT_KINDS, created)).fetchone()
        if not ev:
            ev = c.execute(
                "SELECT id FROM event WHERE (session=? OR source='llm_router') "
                "AND kind NOT IN ('legacy_note') AND created<=? AND created>=? "
                "ORDER BY created DESC LIMIT 1", (scope, created, created - 120)).fetchone()
    if not ev:
        return {"ok": False, "message": "找不到真实来源事件，需人工介入"}
    import self_model
    ok = self_model.attach_action_evidence(action_id, [ev["id"]])
    if not ok:
        return {"ok": False, "message": "写入失败"}
    return {"ok": True, "message": "已补挂真实来源 %s" % ev["id"], "event_id": ev["id"]}


def self_view_stats(window=86400):
    at = time.time()
    with closing(_db()) as c:
        total = c.execute("SELECT COUNT(*) FROM self_view_rev").fetchone()[0]
        last = c.execute("SELECT MAX(ts) FROM self_view_rev").fetchone()[0]
        inwin = c.execute("SELECT COUNT(*) FROM self_view_rev WHERE ts>=?",
                          (at - window,)).fetchone()[0]
    return {"total": total, "in_window": inwin,
            "last_ts": last, "last_age_hours": round((at - last) / 3600, 1) if last else None}