"""她什么时候在桌面上主动开口 —— 纯本地规则，每条都要能说出理由。

P1 没有云端链路，所以触发源只有三类：时间（今天还没清日常）、跑单结果、闸门拦下。
每一个「不开口」也都要有名字：是被别吵拦了、还是配额用完了、还是今天已经清过。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass


@dataclass
class Utterance:
    state: str          # sprites.STATE_ACTION 里的语义状态
    situation: str      # 喂给模型的「现在的情形」
    reason: str         # 为什么开口 / 为什么是兜底句
    proactive: bool = True


def _today(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def runs_by_day(ledger_rows: list[dict]) -> dict[str, list[dict]]:
    """按天归拢每一单的收尾记录，账本读不出来的行直接丢掉不猜。"""
    out: dict[str, list[dict]] = {}
    for rec in ledger_rows:
        if rec.get("event") != "run_finished":
            continue
        day = _today(float(rec.get("at") or 0))
        out.setdefault(day, []).append(rec)
    return out


def ran_ok_today(ledger_rows: list[dict], now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    for rec in runs_by_day(ledger_rows).get(_today(now), []):
        if rec.get("state") == "done":
            return True
    return False


def summarize_run(rec: dict) -> str:
    """把一条账本记录压成她能念出来的一句话素材（只用账本里真有的数）。"""
    chains = rec.get("chains") or []
    parts = []
    for c in chains:
        bits = [c.get("state") or "?"]
        if c.get("times_finished"):
            bits.append(f"{c['times_finished']} 局")
        if c.get("sanity_before") is not None and c.get("sanity_after") is not None:
            bits.append(f"理智 {c['sanity_before']}→{c['sanity_after']}")
        if c.get("seconds"):
            bits.append(f"{c['seconds']} 秒")
        if c.get("error"):
            bits.append(c["error"])
        parts.append(f"{c.get('task')}（{'，'.join(bits)}）")
    reasons = [str(n) for n in rec.get("notes") or [] if n]
    if rec.get("why"):
        reasons.append(str(rec["why"]))
    why = "；".join(dict.fromkeys(bit for reason in reasons for bit in reason.split("；") if bit))
    return f"这单 {rec.get('state')}：{'、'.join(parts) if parts else '一条任务都没跑起来'}" + (
        f"。原因：{why}" if why else "")


class Brain:
    def __init__(self, store, ledger_rows, now=None, propose_after_hour=12):
        self.store = store
        self.ledger_rows = ledger_rows
        self._now = now or time.time
        self.propose_after_hour = propose_after_hour
        self.last_state = ""
        self.last_reason = ""

    def _deny(self, why: str) -> None:
        self.last_state = "silent"
        self.last_reason = why

    def on_gate_blocked(self, blocked: list[str]) -> Utterance | None:
        """闸门拦下必须出声 —— 沉默的 worker 在这个项目里等于故障。"""
        if not blocked:
            return None
        self.last_state = "confirm"
        self.last_reason = "闸门拦下"
        return Utterance("confirm",
                         "你想开工清日常，但被拦住了。拦住你的：" + "；".join(blocked) +
                         "。把这个说给他听，一句讲一条，别催他。",
                         "闸门拦下：" + "；".join(blocked), proactive=False)

    def on_run_finished(self, run: dict) -> Utterance:
        state = {"done": "done", "fault": "fail", "stopped": "fail",
                 "stalled": "fail"}.get(run.get("state"), "done")
        self.last_state = state
        self.last_reason = "跑单结束"
        return Utterance(state, summarize_run(run) + "。把结果告诉他，数要说准，别加没发生的细节。",
                         "跑单结束：" + str(run.get("state")), proactive=False)

    def tick(self) -> Utterance | None:
        """周期检查：今天还没清、时间不早了、配额还在 —— 才开口。"""
        now = self._now()
        hour = time.localtime(now).tm_hour
        if hour < self.propose_after_hour:
            return self._silent(f"现在 {hour} 点，还没到 {self.propose_after_hour} 点")
        if ran_ok_today(self.ledger_rows, now):
            return self._silent("今天已经有一单是 done 的，不再催")
        ok, why = self.store.can_bubble(now)
        if not ok:
            return self._silent("主动开口被拦：" + why)
        self.last_state = "talk"
        self.last_reason = "今天还没清日常"
        return Utterance("talk",
                         "今天还没帮他清明日方舟的日常，理智大概攒着。提醒一句，别念第二遍。",
                         "今天没跑成过任何一单")

    def greet(self) -> Utterance:
        self.last_state = "talk"
        self.last_reason = "刚启动"
        return Utterance("talk", "主人刚打开电脑/刚把她叫出来，打个招呼，顺带说一句今天清没清。",
                         "启动问候", proactive=False)

    def _silent(self, reason: str) -> None:
        self._deny(reason)
        return None
