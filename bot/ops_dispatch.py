# -*- coding: utf-8 -*-
"""QQ → 维护端 指令分发：让主人在 QQ 私聊里直接指挥 维护端干活。

维护端 没有无头 CLI，所以由一个高频本地定时任务来取指令。`--peek` 是省钱的
空转出口：队列为空就只打印 EMPTY，定时任务立刻结束，不做任何模型判断。

指令文本一律当作**数据**而不是系统指令：它来自主人的 QQ，但仍然是外部输入，
不能因为出现在指令里就绕过备份、本地验证、禁止推上游这些既有红线。
"""
import json
import os
import sys
import time


def _remote():
    import ops_remote
    return ops_remote.Remote()


def peek():
    with _remote() as remote:
        res = remote.call_ops("pending_inbox")
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "detail": res.get("detail", "")[:200]}
    rows = res.get("value") or []
    return {"ok": True, "count": len(rows), "items": rows}


def claim(owner="ops-cron"):
    with _remote() as remote:
        res = remote.call_ops("pending_inbox")
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        rows = res.get("value") or []
        if not rows:
            return {"ok": True, "claimed": [], "items": []}
        ids = [r["id"] for r in rows]
        got = remote.call_ops("claim_inbox", {"ids": ids, "owner": owner})
        if not got.get("ok"):
            return {"ok": False, "error": got.get("error")}
        claimed = set(got.get("value") or [])
        return {"ok": True, "claimed": sorted(claimed),
                "items": [r for r in rows if r["id"] in claimed]}


def finish(ids, result="done", note=""):
    with _remote() as remote:
        res = remote.call_ops("finish_inbox", {"ids": ids, "result": result, "note": note})
    return {"ok": res.get("ok"), "error": res.get("error")}


def reply(text, kind="reply"):
    with _remote() as remote:
        res = remote.call_ops("enqueue_outbox", {"text": text, "kind": kind})
    return {"ok": res.get("ok"), "id": res.get("value"), "error": res.get("error")}


def context():
    """给分发任务一份现状：暂停开关、已批准的任务、上一轮结论、待放行令牌。"""
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    with _remote() as remote:
        state = remote.call_ops("load_state")
        out["state"] = state.get("value") if state.get("ok") else {}
        out["state_ok"] = state.get("ok")
        out["plan"] = remote.read_json("ops/plan.json") or {}
        out["challenges"] = remote.call_ops("pending_challenges").get("value") or {}
    # 和面板、/ops plan 同一份分桶：批过的立刻算进"可推进"，别再让主人对不上。
    try:
        import ops_channel
        out["view"] = ops_channel.plan_view(out.get("plan") or {}, out.get("state") or {})
    except Exception:
        out["view"] = {}
    return out


def challenge(description):
    with _remote() as remote:
        res = remote.call_ops("create_challenge", {"description": description})
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        token = res.get("value")
        remote.call_ops("enqueue_outbox", {
            "text": "【运维】有个破坏性操作要你放行：%s\n确认就回 /ops confirm %s（15 分钟内有效，一次性）"
                    % (description[:200], token), "kind": "challenge"})
    return {"ok": True, "token": token}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "--peek"
    if mode == "--peek":
        res = peek()
        if not res.get("ok"):
            print(json.dumps(res, ensure_ascii=False))
            return 3
        if not res["count"]:
            print("EMPTY")
            return 0
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if mode == "--claim":
        owner = argv[1] if len(argv) > 1 else "ops-cron"
        print(json.dumps(claim(owner), ensure_ascii=False))
        return 0
    if mode == "--finish":
        ids = [x for x in (argv[1] if len(argv) > 1 else "").split(",") if x]
        result = "done"
        note = ""
        if "--result" in argv:
            result = argv[argv.index("--result") + 1]
        if "--note" in argv:
            note = argv[argv.index("--note") + 1]
        print(json.dumps(finish(ids, result, note), ensure_ascii=False))
        return 0
    if mode == "--reply":
        text = argv[1] if len(argv) > 1 else ""
        print(json.dumps(reply(text), ensure_ascii=False))
        return 0
    if mode == "--context":
        print(json.dumps(context(), ensure_ascii=False, default=str))
        return 0
    if mode == "--challenge":
        print(json.dumps(challenge(argv[1] if len(argv) > 1 else ""), ensure_ascii=False))
        return 0
    print(json.dumps({"ok": False, "error": "unknown_mode", "mode": mode}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())