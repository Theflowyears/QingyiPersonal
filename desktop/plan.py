"""把用户 MAA 里已有的日常配置，翻译成 MaaCore 认得的意图与参数。

两条硬规矩：
1. 参数只从你的 gui.new.json 继承，键名按 MAA 官方 integration.md 的口径映射；
   映射有把握的标 confirmed，靠推断的标 inferred —— 面板上分开显示，不混为一谈。
2. 花不可再生资源的一律要人点头（理智药、石头、加急、抽卡、肉鸽投入）。
   充值 / 买礼包 / 兑换码这类根本没有对应任务，白名单里没有就是没有。
"""
from __future__ import annotations

import hashlib
import json

CONFIRMED = "confirmed"   # 一对一继承，键名口径有官方文档背书
INFERRED = "inferred"     # 我推出来的映射，第一次用要你确认
DEFAULT = "default"       # 没传，走 MaaCore 自己的默认

# tier: auto = 已授权套餐里可以直接跑；confirm = 每次都要点；manual = 只列不自动排进套餐
AUTO, CONFIRM, MANUAL = "auto", "confirm", "manual"

FORBIDDEN = ("充值 / 购买源石", "购买礼包", "兑换码", "删除好友 / 清空好友", "合成玉消费")

# 会花掉不可再生资源的参数
SPEND_KEYS = {
    "Fight": ("medicine", "stone", "expiring_medicine"),
    "Recruit": ("expedite",),
    "Award": ("orundum", "recruit"),
    "Roguelike": ("starts_count", "investment_enabled", "investments_count"),
    "Copilot": ("loop_times", "use_sanity_potion"),
}

DAILY_ORDER = ("StartUp", "Recruit", "Infrast", "Fight", "Mall", "Award")


class Intent:
    def __init__(self, task, label, params=None, prov=None, tier=AUTO,
                 enabled_in_maa=False, note=""):
        self.task = task
        self.label = label
        self.params = dict(params or {})
        self.prov = dict(prov or {})
        self.tier = CONFIRM if tier == AUTO and self.spends else tier
        self.enabled_in_maa = bool(enabled_in_maa)
        self.note = note

    @property
    def spends(self):
        return [k for k in SPEND_KEYS.get(self.task, ()) if _spends(self.params.get(k))]

    def as_dict(self):
        return {"task": self.task, "label": self.label, "params": self.params,
                "prov": self.prov, "tier": self.tier, "spends": self.spends,
                "enabled_in_maa": self.enabled_in_maa, "note": self.note}

    def __repr__(self):
        return f"<Intent {self.task} {sorted(self.params)}>"


def _spends(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return False


def _split_list(text) -> list:
    if not text:
        return []
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    return [x.strip() for x in str(text).replace("；", ";").split(";") if x.strip()]


def _task_of(queue, task_type: str) -> dict:
    for t in queue or []:
        if t.get("TaskType") == task_type or t.get("$type", "").endswith(task_type + "Task"):
            return t
    return {}


def _startup(cfg_conn, queue, third) -> Intent:
    return Intent("StartUp", "启动与登录",
                  {"client_type": cfg_conn.client_type},
                  {"client_type": CONFIRMED}, AUTO, True,
                  "只把游戏带到主界面，不做任何消耗")


def _recruit(cfg_conn, queue, third) -> Intent:
    t = _task_of(queue, "Recruit")
    if not t:
        return Intent("Recruit", "公开招募", tier=CONFIRM, note="你的 MAA 里没有公招任务，参数要你给")
    params, prov = {}, {}
    if t.get("MaxTimes") is not None:
        # GUI 的「招募次数上限」和 core 的 times 语义一致，但数值是你填的，仍标推断
        params["times"] = int(t["MaxTimes"])
        prov["times"] = INFERRED
    select = [lv for lv in (3, 4, 5, 6) if t.get(f"Level{lv}Choose")]
    if select:
        params["select"] = list(select)
        params["confirm"] = list(select)
        prov["select"] = prov["confirm"] = INFERRED
    if any(t.get(f"Level{lv}Time") for lv in (3, 4)):
        params["recruitment_time"] = {str(lv): int(t[f"Level{lv}Time"])
                                      for lv in (3, 4) if t.get(f"Level{lv}Time")}
        prov["recruitment_time"] = CONFIRMED
    if t.get("PreserveTagList") and t.get("PreserveTagEnabled", True):
        params["preserve_tags"] = list(t["PreserveTagList"])
        prov["preserve_tags"] = CONFIRMED
    if t.get("Level3PreferTags"):
        params["first_tags"] = list(t["Level3PreferTags"])
        prov["first_tags"] = CONFIRMED
    if t.get("ExtraTagMode") is not None:
        params["extra_tags_mode"] = int(t["ExtraTagMode"])
        prov["extra_tags_mode"] = CONFIRMED
    if "refresh" in {k.lower() for k in t}:
        params["refresh"] = bool(t.get("RefreshLevel3") or t.get("ForceRefresh"))
        prov["refresh"] = INFERRED
    if t.get("Level3RecruitmentPermitReserveEnabled"):
        params["level3_recruitment_permit_reserve"] = int(
            t.get("Level3RecruitmentPermitReserve") or 0)
        prov["level3_recruitment_permit_reserve"] = CONFIRMED
    return Intent("Recruit", "公开招募", params, prov, CONFIRM,
                  bool(t.get("IsEnable")),
                  "不碰加急许可（expedite 没传，默认关）；三星以下按你保留的词条跳过")


def _infrast(cfg_conn, queue, third) -> Intent:
    t = _task_of(queue, "Infrast")
    if not t:
        return Intent("Infrast", "基建维护", tier=CONFIRM, note="你的 MAA 里没有基建任务")
    params, prov = {}, {}
    rooms = [r.get("Room") for r in (t.get("RoomList") or []) if isinstance(r, dict) and r.get("Room")]
    if rooms:
        params["facility"] = rooms
        prov["facility"] = CONFIRMED
    if t.get("UsesOfDrones"):
        params["drones"] = str(t["UsesOfDrones"])
        prov["drones"] = CONFIRMED
    if t.get("DormThreshold") is not None:
        params["threshold"] = int(t["DormThreshold"])
        prov["threshold"] = CONFIRMED
    for src, dst in (("OriginiumShardAutoReplenishment", "replenish"),
                     ("DormFilterNotStationed", "dorm_notstationed_enabled"),
                     ("DormTrustEnabled", "dorm_trust_enabled"),
                     ("ReceptionMessageBoard", "reception_message_board"),
                     ("ReceptionClueExchange", "reception_clue_exchange"),
                     ("SendClue", "reception_send_clue"),
                     ("ContinueTraining", "continue_training")):
        if src in t:
            params[dst] = bool(t[src])
            prov[dst] = CONFIRMED
    if t.get("Filename"):
        params["filename"] = str(t["Filename"])
        prov["filename"] = CONFIRMED
    if t.get("Mode"):
        params["mode"] = str(t["Mode"])
        prov["mode"] = CONFIRMED
    return Intent("Infrast", "基建维护", params, prov, AUTO,
                  bool(t.get("IsEnable")), "只换班和收菜，不花钱买无人机")


def _fight(cfg_conn, queue, third) -> Intent:
    t = _task_of(queue, "Fight")
    if not t:
        return Intent("Fight", "作战", tier=CONFIRM, note="你的 MAA 里没有战斗任务")
    params, prov = {}, {}
    plan = [s for s in (t.get("StagePlan") or []) if str(s).strip()]
    if plan:
        params["stage"] = str(plan[0]).strip()
        prov["stage"] = CONFIRMED
    params["medicine"] = int(t.get("MedicineCount") or 0) if t.get("UseMedicine") else 0
    prov["medicine"] = CONFIRMED
    params["stone"] = int(t.get("StoneCount") or 0) if t.get("UseStone") else 0
    prov["stone"] = CONFIRMED
    if t.get("Series"):
        params["series"] = int(t["Series"])
        prov["series"] = CONFIRMED
    if t.get("EnableTimesLimit"):
        params["times"] = int(t.get("TimesLimit") or 0)
        prov["times"] = CONFIRMED
    if t.get("IsDrGrandet"):
        params["DrGrandet"] = True
        prov["DrGrandet"] = CONFIRMED
    if t.get("UseExpiringMedicine"):
        params["expiring_medicine"] = int(t.get("MedicineCount") or 0)
        params["medicine_expire_days"] = int(t.get("MedicineExpireDays") or 2)
        prov["expiring_medicine"] = prov["medicine_expire_days"] = CONFIRMED
    if params.get("stage") == "":
        params.pop("stage", None)
    tier = CONFIRM if any(_spends(params.get(k)) for k in SPEND_KEYS["Fight"]) else AUTO
    note = "不吃药不磕石头，理智刷空就停" if tier == AUTO else "这条会花掉理智药/合成石，每次都要你点"
    return Intent("Fight", f"作战 {params.get('stage') or '（没选关）'}".strip(),
                  params, prov, tier, bool(t.get("IsEnable")), note)


def _mall(cfg_conn, queue, third) -> Intent:
    t = _task_of(queue, "Mall")
    if not t:
        return Intent("Mall", "信用收支", tier=CONFIRM, note="你的 MAA 里没有商店任务")
    params = {}
    prov = {}
    for src, dst in (("Shopping", "shopping"), ("VisitFriends", "visit_friends"),
                     ("CreditFight", "credit_fight"), ("OnlyBuyDiscount", "only_buy_discount"),
                     ("ReserveMaxCredit", "reserve_max_credit"),
                     ("ShoppingIgnoreBlackListWhenFull", "force_shopping_if_credit_full")):
        if src in t:
            params[dst] = bool(t[src])
            prov[dst] = CONFIRMED
    if t.get("FirstList"):
        params["buy_first"] = _split_list(t["FirstList"])
        prov["buy_first"] = CONFIRMED
    if t.get("BlackList"):
        params["blacklist"] = _split_list(t["BlackList"])
        prov["blacklist"] = CONFIRMED
    return Intent("Mall", "信用收支", params, prov, AUTO, bool(t.get("IsEnable")),
                  "只收信用和逛好友，黑名单沿用你填的（" + "、".join(params.get("blacklist") or []) + "）")


def _award(cfg_conn, queue, third) -> Intent:
    t = _task_of(queue, "Award")
    params, prov = {}, {}
    for src, dst in (("Award", "award"), ("Mail", "mail"), ("FreeGacha", "recruit"),
                     ("Orundum", "orundum"), ("Mining", "mining"),
                     ("SpecialAccess", "specialaccess"), ("SignInEvent", "signinevent")):
        if src in t:
            params[dst] = bool(t[src])
            prov[dst] = CONFIRMED
    risky = _spends(params.get("orundum")) or _spends(params.get("recruit"))
    return Intent("Award", "领取奖励", params, prov,
                  CONFIRM if risky else AUTO, bool(t.get("IsEnable")),
                  "会动到合成玉/寻访，要你点" if risky else "只领免费产出，不碰合成玉")


def _roguelike(cfg_conn, queue, third) -> Intent:
    t = _task_of(queue, "Roguelike")
    if not t:
        return Intent("Roguelike", "集成战略", tier=MANUAL, enabled_in_maa=False,
                      note="你的 MAA 里没配肉鸽")
    params, prov = {}, {}
    if t.get("Theme"):
        params["theme"] = str(t["Theme"])
        prov["theme"] = CONFIRMED
    if t.get("Mode"):
        params["mode"] = str(t["Mode"])
        prov["mode"] = CONFIRMED
    if t.get("Difficulty") is not None:
        params["difficulty"] = int(t["Difficulty"])
        prov["difficulty"] = CONFIRMED
    if t.get("StartCount") is not None:
        params["starts_count"] = int(t["StartCount"])
        prov["starts_count"] = CONFIRMED
    return Intent("Roguelike", "集成战略", params, prov, MANUAL, bool(t.get("IsEnable")),
                  "烧理智且局数不可控，只列出来，永远要你点")


BUILDERS = {"StartUp": _startup, "Recruit": _recruit, "Infrast": _infrast,
            "Fight": _fight, "Mall": _mall, "Award": _award, "Roguelike": _roguelike}


def build_intents(conn, gui_cfg: dict) -> list[Intent]:
    queue = (gui_cfg.get("Configurations", {}).get("Default", {}) or {}).get("TaskQueue") or []
    third = (gui_cfg.get("Configurations", {}).get("Default", {}).get("Gui", {}) or {}).get(
        "ThirdParty") or {}
    out = []
    for task in DAILY_ORDER + ("Roguelike",):
        out.append(BUILDERS[task](conn, queue, third))
    return out


def daily_plan(intents: list[Intent]) -> list[Intent]:
    """今天的套餐：没被标 manual 的都算，enabled_in_maa 只影响面板上的排序说明。"""
    return [i for i in intents if i.tier != MANUAL]


def fingerprint(intents: list[Intent]) -> str:
    payload = json.dumps([[i.task, i.params, i.tier] for i in intents],
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def changed_keys(intents: list[Intent], approved: dict) -> list[str]:
    """和上次点头通过的版本比，哪些任务的参数变了 —— 面板要逐条列出来。"""
    out = []
    for i in intents:
        before = (approved or {}).get(i.task)
        if before is None:
            out.append(f"{i.task}：新增")
        elif before != i.params:
            keys = sorted({k for k in set(before) | set(i.params)
                           if before.get(k) != i.params.get(k)})
            out.append(f"{i.task}：" + "、".join(keys))
    return out
