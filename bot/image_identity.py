"""角色身份层：同一角色换了画风也要认得。

她按图片 sha / 表情 id 存「含义卡」，却没有「这是谁」的身份层 ——
于是祀换一张画风的图就成了陌生人，只会说「不敢打包票认得准」。
这里维护一份轻量身份表（名字 + 画风无关特征 + 别名），
在回复前给出「这句话/这张图的描述像哪个已知角色」的提示，注入给模型。

特征用二字组做模糊匹配（「绿色头发」「头上有角」「蓝色眼睛」），
不依赖 embedding，也不产生额外请求。
"""
import json
import os
import re
import time
from pathlib import Path

# 初始已知：她最近互动的角色（名字 -> 画风无关特征 / 别名）。可在数据文件里增删改。
DEFAULT_IDENTITIES = {
    "祀": {"features": ["绿色头发", "头上有角", "蓝色眼睛"], "aliases": []},
}

# 「这是X / 这也是X / 其实就是X」——带图时用户点名，用于自学习。
_NAME_MARK = re.compile(r"(?:这是|这也是|其实是|就是|名叫|她叫|他叫|叫)\s*([\u4e00-\u9fffA-Za-z]{2,8})")
_NOT_NAME = {"什么", "谁", "哪个", "怎么", "一个", "真的", "你的", "我的", "那个", "这个",
             "不是", "好像", "应该", "画风", "表情"}


def _path():
    base = os.getenv("PERSONA_DATA_DIR") or os.path.join(os.getenv("DATA_DIR", "./data"), "persona")
    return Path(base) / "image_identity.json"


def _load():
    p = _path()
    d = {}
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            d = {}
    for name, meta in DEFAULT_IDENTITIES.items():
        if name not in d:
            d[name] = {"name": name, "features": list(meta["features"]),
                       "aliases": list(meta.get("aliases") or []), "ts": 0}
    return d


def _save(d):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def remember(name, features=None, aliases=None, sha=None):
    name = str(name or "").strip()
    if not name:
        return
    d = _load()
    e = d.get(name) or {"name": name, "features": [], "aliases": [], "ts": 0}
    for f in (features or []):
        f = str(f).strip()
        if f and f not in e["features"]:
            e["features"].append(f)
    for a in (aliases or []):
        a = str(a).strip()
        if a and a not in e["aliases"]:
            e["aliases"].append(a)
    if sha:
        ex = [x for x in (e.get("examples") or []) if x != sha]
        e["examples"] = (ex + [sha])[-20:]
    e["ts"] = time.time()
    d[name] = e
    _save(d)


def _toks(s):
    s = str(s or "")
    if len(s) < 2:
        return set([s]) if s else set()
    return set(s[i:i + 2] for i in range(len(s) - 1))


def _feat_overlap(entry, text_toks):
    """有几条特征跟文本有共同的二字组（每条特征最多算一次）。"""
    n = 0
    for f in entry.get("features") or []:
        if _toks(f) & text_toks:
            n += 1
    return n


def candidates(text):
    """返回 [(score, name, entry)]，按可信度排序。名字/别名命中优先，其次特征重合≥2。"""
    t = str(text or "")
    if not t:
        return []
    tt = _toks(t)
    out = []
    for name, e in _load().items():
        score = 0
        if name and name in t:
            score += 5
        for a in e.get("aliases") or []:
            if a and a in t:
                score += 3
        if not score:
            ov = _feat_overlap(e, tt)
            if ov >= 2:
                score += ov
        if score:
            out.append((score, name, e))
    out.sort(key=lambda x: -x[0])
    return out


def hint_for(text):
    """给模型的提示：这张图/这句话像哪个已知角色。没有命中返回空串。"""
    cs = candidates(text)
    if not cs:
        return ""
    _score, name, e = cs[0]
    feats = "、".join((e.get("features") or [])[:4])
    return ("【你其实认得这个角色：%s%s。就算换了画风也大概率是同一个人，"
            "别说不认识、也别让她再解释一遍。】"
            % (name, ("（%s）" % feats) if feats else ""))


def learn_from_message(text, has_image):
    """带图时用户点名（「这也是祀呀」），把这个角色记进身份表。"""
    if not has_image:
        return
    m = _NAME_MARK.search(str(text or ""))
    if not m:
        return
    name = re.sub(r"[呀啊呢吧啦哈哦嘛哟嘞，。！？]+$", "", m.group(1)).strip()
    if name in _NOT_NAME or len(name) < 2:
        return
    remember(name)