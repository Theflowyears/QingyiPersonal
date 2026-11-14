"""从聊天文本里认出「这是一个 B 站视频链接」，别的什么都不干。

为什么要单独一个模块：链接是她从群里看到的，属于**外部输入**。
外部输入里最贵的一类错误是「你说什么 URL 我都去 fetch」——
那等于把内网端口开放给任何会打字的人。所以这里有一条白名单，
而且白名单是默认拒绝，不是默认允许：
    只认 bilibili.com / b23.tv 两个域名，其余一律不当链接处理。

纯解析、不联网：短链跳转要发请求，那部分留在 bili.py 里，
这样这个模块可以离线跑测试，也方便以后单独审。
"""
import re

# B 站BV号是 BV + 10 位 [0-9A-Za-z]；av号是 av + 数字。
# 别写成 {1,}：那会把 BV12 这种明显残缺的也认下来，然后去请求一轮拿到 404。
_BV_RE = re.compile(r"\bBV([0-9A-Za-z]{10})\b")
_AV_RE = re.compile(r"(?:^|[^\w])av(\d{1,12})(?:\D|$)")

# 默认拒绝：只有这两个域名算 B 站链接。
ALLOWED_HOSTS = ("bilibili.com", "b23.tv", "www.bilibili.com")

# 短链只允许这一种形状，别让她粘的任意图床链接混进来被我们跟随
_SHORT_RE = re.compile(r"https?://b23\.tv/([A-Za-z0-9]{4,16})", re.I)

_URL_RE = re.compile(r"https?://[^\s<>\"'，。、）)]+", re.I)


def _host_of(url):
    """从 URL 里取主机名，只取主机名 —— 不解析端口/路径，也不做 DNS。"""
    s_ = str(url or "").strip()
    if "://" not in s_:
        return ""
    rest = s_.split("://", 1)[1]
    host = re.split(r"[/?#@:]", rest, maxsplit=1)[0]
    return host.lower().strip(".")


def is_allowed(url):
    """这个 URL 的域名在不在白名单上。

    比的是「等于白名单项」或「以 .白名单项 结尾」：
    用 `in` 判会放行 evil.com/?x=bilibili.com，也会放行 mybilibili.com。
    """
    host = _host_of(url)
    if not host:
        return False
    for a in ALLOWED_HOSTS:
        if host == a or host.endswith("." + a):
            return True
    return False


def bvid_in(text):
    """文本里第一个 BV 号，规范化成大写 BV 开头的形式。"""
    m = _BV_RE.search(str(text or ""))
    return ("BV" + m.group(1)) if m else ""


def aid_in(text):
    m = _AV_RE.search(str(text or ""))
    return ("av" + m.group(1)) if m else ""


def short_links(text):
    """b23.tv 短链（真 ID 要跳转以后才知道）。"""
    return ["https://b23.tv/" + m.group(1)
            for m in _SHORT_RE.finditer(str(text or ""))]


def video_refs(text):
    """把一句话里能确定身份的视频引用挑出来。

    只认两种：URL 里的 BV/av，和正文里裸写的 BV 号。
    短链不在这里返回 —— 它得先跳转，交给 bili.py 处理，
    免得以调用者的以为「拿到引用」就等于「已经验证过域名」。
    """
    t = str(text or "")
    out = []
    for u in _URL_RE.findall(t):
        if not is_allowed(u):
            continue
        bid = bvid_in(u)
        if bid:
            _add(out, bid)
            continue
        a = aid_in(u)
        if a:
            _add(out, a)
    bid = bvid_in(t)
    if bid:
        _add(out, bid)
    return out


def _add(lst, ref):
    ref = str(ref or "").strip()
    if ref and ref not in lst:
        lst.append(ref)
    return lst


def from_final_url(url):
    """跳转以后那个真地址里的视频号。"""
    return bvid_in(url) or aid_in(url)
