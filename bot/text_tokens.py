"""全系统唯一的中文分词器。三套各自一套的日子到此为止。

为什么要统一：同一条记忆在三个库里被切成不同的样子 ——
  · group_memory_store._tokens：jieba（**载她的专属词典 persona_dict.txt**）+ 停用词 + 汉字二元组
  · persona_store.tokenize：jieba 但**没载词典、没停用词**
  · knowledge_base._tok：只有二元组、**根本没有 jieba**（注释却写着「跟群记忆一致」）
后果就是「存了但搜不到」：专名「提弗洛斯」在 persona 库里被切成通用词，
在知识库里只能靠二元组碰运气；同一句话在不同库得分不同，排序互相打脸。

分词口径沿用群库那份（它有实测数据：二元组+词级混合 R@1 82.5%，
纯 jieba 75.0%，纯二元组 80.0%），其他两库向它对齐，而不是反过来。
"""
import os
import re

_JIEBA = {"tried": False, "mod": None}
_STOPWORDS = None

_CJK_RE = re.compile(r"[一-龥]+")
_LATIN_RE = re.compile(r"[A-Za-z0-9]{2,}")
_WORD_CHAR_RE = re.compile(r"[\w一-龥]", re.UNICODE)
_ASCII1_RE = re.compile(r"[A-Za-z0-9]")


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def jieba_mod():
    """惰性加载 jieba 并载入她的专属词典；缺库就返回 None，上层自动退回二元组。"""
    if not _JIEBA["tried"]:
        _JIEBA["tried"] = True
        try:
            import jieba as _j
            path_ = os.path.join(_here(), "persona_dict.txt")
            if os.path.isfile(path_):
                try:
                    # 传文件对象而不是路径：jieba 自己 open 完不关，
                    # 每次进程启动都会留一个 ResourceWarning 的句柄泄漏
                    with open(path_, encoding="utf-8") as fh:
                        _j.load_userdict(fh)
                except Exception:
                    pass
            _JIEBA["mod"] = _j
        except Exception:
            _JIEBA["mod"] = None
    return _JIEBA["mod"]


def stopwords():
    global _STOPWORDS
    if _STOPWORDS is not None:
        return _STOPWORDS
    path_ = os.path.join(_here(), "hit_stopwords.txt")
    words = set()
    try:
        with open(path_, encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if w and not w.startswith("#"):
                    words.add(w)
    except Exception:
        pass
    _STOPWORDS = words
    return words


# 分词结果缓存：一轮里同一个查询串会被多个库反复切词（群事实/知识库/人格/去重
# 各切一遍），而 jieba.cut 是这段预处理里唯一明显的 CPU 成本。按原串缓存，
# 命中就跳过切词；返回副本，避免调用方就地改动污染缓存。
_TOK_CACHE = {}
_CW_CACHE = {}
_CACHE_MAX = 4096


def tokens(text):
    """jieba 词 + 汉字二元组 + 英数整词，去停用词。

    单字一律丢（除非是字母/数字）—— 单字在中文里基本是虚词，
    留着只会让「的/了/是」这种高频字把分数摊平。
    """
    t = str(text or "")
    if not t:
        return set()
    cached = _TOK_CACHE.get(t)
    if cached is not None:
        return set(cached)
    stop = stopwords()
    out = set()

    jb = jieba_mod()
    if jb is not None:
        for w in jb.cut(t):
            w = str(w).strip()
            if not w or w in stop:
                continue
            if not _WORD_CHAR_RE.search(w):
                continue
            if len(w) == 1 and not _ASCII1_RE.match(w):
                continue
            out.add(w if _ASCII1_RE.match(w) else w.lower())

    for seg in _CJK_RE.findall(t):
        if len(seg) == 1:
            out.add(seg)
        for k in range(len(seg) - 1):
            bg = seg[k:k + 2]
            if bg not in stop:
                out.add(bg)

    for w in _LATIN_RE.findall(t):
        if w.lower() not in stop:
            out.add(w.lower())
    if len(_TOK_CACHE) > _CACHE_MAX:
        _TOK_CACHE.clear()
    _TOK_CACHE[t] = out
    return set(out)


def content_words(text):
    """只取「实词」：词级 + 英数，不含二元组。

    给写入端存 aliases 用 —— 别名要的是用户原话里真正的名词，
    二元组当别名会把「今天」「我们」这类也存进去。
    """
    t = str(text or "")
    if not t:
        return []
    cached = _CW_CACHE.get(t)
    if cached is not None:
        return list(cached)
    stop = stopwords()
    out = []
    jb = jieba_mod()
    if jb is not None:
        for w in jb.cut(t):
            w = str(w).strip()
            if len(w) < 2 or w in stop:
                continue
            if not _WORD_CHAR_RE.search(w):
                continue
            lw = w.lower()
            if lw not in out:
                out.append(lw)
    for w in _LATIN_RE.findall(t):
        lw = w.lower()
        if lw not in stop and lw not in out:
            out.append(lw)
    res = out[:8]
    if len(_CW_CACHE) > _CACHE_MAX:
        _CW_CACHE.clear()
    _CW_CACHE[t] = res
    return list(res)


def overlap(a, b):
    """两个词集的交集大小（打分函数们统一用它，别再各自写一遍）。"""
    if not a or not b:
        return 0
    return len(a & b)
