# -*- coding: utf-8 -*-
"""「她看到了什么」到「她因此做了什么」之间的那根线。

线上账本原来长这样：483 条动作，0 条带证据。问她「你为什么发这句」，
除了翻聊天记录没有任何办法回答 —— 阶段2.5 的准入门就是卡在这。
发一条消息的路径有几十条（回复、接话、气泡拆分、后台查证……），
逐个传参数一定会漏，所以这里按「场景」记：
入站时把事件号挂到场景上，发话时按场景取回最近那几笔。

TTL 是这套记录能不能信的关键。不做时间窗的话，一条三天前的消息
会永远挂在「触发器」上，每次发言都把它当证据抄一遍 —— 那等于没记。

这份账本必须落盘：原来纯内存，进程一重启 _SCENES 清空，发话取不到任何
触发证据，动作没依据、整点整理也取不到来源事件，belief/self_view/action
三个准入门跟着一起空。落盘后重启读回，TTL 仍在读取时生效（过期不复活）。
"""
import json
import os
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar

_INPUT_EVIDENCE = ContextVar('generation_evidence', default=None)

_MAX_PER_SCENE = 12
_MAX_SCENES = 256
_SCENES = OrderedDict()
_LOCK = threading.RLock()
_loaded = False


def _file():
    return os.path.join(os.getenv("DATA_DIR", "./data"), "memory",
                        "trigger_ledger.json")


def _ttl():
    return float(os.getenv("TRIGGER_LEDGER_TTL", "900"))


def _load():
    """ lazy 读回盘上的场景账。损坏先留底（.corrupt），不当空覆盖。"""
    global _loaded
    with _LOCK:
        if _loaded:
            return
        _loaded = True
        path = _file()
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
        except Exception as e:
            # 读不出来不等于「没有」：把损坏原件留底，别让它被下一次写盘抹掉。
            try:
                os.replace(path, path + ".corrupt")
            except Exception:
                pass
            print("[触发账本] 读取失败，已留底 .corrupt " + type(e).__name__, flush=True)
            return
        if not isinstance(got, dict):
            return
        for scene, pairs in got.items():
            if not isinstance(pairs, list):
                continue
            items = OrderedDict()
            for pair in pairs:
                if (isinstance(pair, list) and len(pair) == 2
                        and isinstance(pair[0], str)):
                    try:
                        items[pair[0]] = float(pair[1])
                    except (TypeError, ValueError):
                        continue
            if items:
                _SCENES[str(scene)] = items
        while len(_SCENES) > _MAX_SCENES:
            _SCENES.popitem(last=False)


def _persist():
    """写穿到盘：tmp + fsync + 原子替换，重启不丢刚记下的触发。"""
    path = _file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {scene: [[eid, ts] for eid, ts in items.items()]
                for scene, items in _SCENES.items()}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("[触发账本] 写盘失败 " + type(e).__name__, flush=True)
        return False


def capture(scene):
    """Freeze inputs in this task, never consult newer arrivals at delivery."""
    value = (str(scene), tuple(pending(scene)))
    _INPUT_EVIDENCE.set(value)
    return list(value[1])


def captured(scene):
    value = _INPUT_EVIDENCE.get()
    return list(value[1]) if value and value[0] == str(scene) else []


def reset():
    global _loaded
    with _LOCK:
        _SCENES.clear()
        _INPUT_EVIDENCE.set(None)
        _loaded = True   # 重置后不要再从盘里读回旧账
        try:
            path = _file()
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


def note(scene, event_id, ts=None):
    """她看到这个事件了。重复记同一个事件不加分、也不改变顺序。"""
    key = str(scene or "")
    eid = str(event_id or "")
    if not key or not eid:
        return False
    now = time.time() if ts is None else float(ts)
    with _LOCK:
        _load()
        items = _SCENES.setdefault(key, OrderedDict())
        if eid in items:
            del items[eid]          # 重记 = 推到最新，但不重复占位
        items[eid] = now
        while len(items) > _MAX_PER_SCENE:
            items.popitem(last=False)
        while len(_SCENES) > _MAX_SCENES:
            _SCENES.popitem(last=False)
        _persist()
    return True


def _alive(key, now):
    items = _SCENES.get(key)
    if not items:
        return []
    horizon = now - _ttl()
    return [(eid, ts) for eid, ts in items.items() if ts >= horizon]


def pending(scene, now=None):
    """还在触发窗口内的事件号，按她看到的先后排。"""
    at = time.time() if now is None else float(now)
    with _LOCK:
        _load()
        return [eid for eid, _ts in _alive(str(scene or ""), at)]


def latest(scene):
    """最近看到的那一条，**不看窗口**。

    主动找朋友聊天这种事没有「刚才是谁触发了我」，但它的依据确实是
    「那个人最近一次跟我说话」—— 那是她开口的理由，不该被窗口抹掉。
    """
    with _LOCK:
        _load()
        items = _SCENES.get(str(scene or ""))
        if not items:
            return ""
        return max(items.items(), key=lambda kv: kv[1])[0]


def stats():
    now = time.time()
    with _LOCK:
        _load()
        return {"scenes": len(_SCENES),
                "in_window": sum(len(_alive(key, now)) for key in list(_SCENES)),
                "ttl_seconds": _ttl()}