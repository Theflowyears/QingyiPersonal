"""关系分数：报数只读，明确自主决定经对象绑定、事务与审计后写入。"""
import json
import os
import re
import time
import functools
import tempfile
import threading


_WRITE_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


def _transaction(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with _WRITE_LOCK:
            if getattr(_LOCK_STATE, 'depth', 0):
                return fn(*args, **kwargs)
            os.makedirs(_dir(), exist_ok=True)
            with open(os.path.join(_dir(), 'affection.lock'), 'a+b') as lock:
                lock.seek(0, 2)
                if not lock.tell():
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                _LOCK_STATE.depth = 1
                try:
                    return fn(*args, **kwargs)
                finally:
                    _LOCK_STATE.depth = 0
                    lock.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return wrapped


def _dir():
    """路径在调用时解析：DATA_DIR 在测试/多实例下会变，
    模块顶层冻成常量就会写脏真实 data/（AGENTS.md 里的老坑）。"""
    return os.path.join(os.getenv("DATA_DIR", "./data"), "relationship")


def _file():
    return os.path.join(_dir(), "affection.json")


def _nicks_file():
    return os.path.join(_dir(), "nicks.json")


def _cooldown_file():
    return os.path.join(_dir(), "affection_cooldown.json")

MIN_SCORE = -50
MAX_SCORE = 100
DAILY_CAP = 40          # 每个方向每天最多 ±40（算法兜底用；她本人说的不受此限）

# 初始关系走 AFFECTION_SEED（格式 "qq=分值,qq=分值"）与 MASTER_QQ_IDS 首项，
# 真实账号不写进公开仓库；缺配时不预设任何关系。
MASTER_ID = (os.getenv("MASTER_QQ_IDS", "").split(",")[0] or "").strip()
SEED = {}
for _kv in os.getenv("AFFECTION_SEED", "").split(","):
    _kv = _kv.strip()
    if "=" in _kv:
        _k, _, _v = _kv.partition("=")
        try:
            SEED[_k.strip()] = float(_v)
        except ValueError:
            pass

# ── 加分：这些是在对她好 ────────────────────────────
_PLUS = [
    (re.compile(r"谢谢|谢啦|感谢你|辛苦了|麻烦你了"), 2, "道谢"),
    (re.compile(r"可爱|好看|漂亮|真棒|厉害|牛|强|画得好|唱得好"), 2, "夸她"),
    (re.compile(r"喜欢|爱你|想你|舍不得|抱抱|摸摸|亲亲"), 3, "亲近"),
    (re.compile(r"陪你|一起|咱们|我们"), 1, "一起做事"),
    (re.compile(r"记得|你上次|你之前说过|还记得"), 2, "记得她说过的话"),
    (re.compile(r"别累着|休息|喝口水|早点睡|注意身体"), 3, "关心她"),
]

# ── 扣分：这些是在伤她 ─────────────────────────────
_MINUS = [
    (re.compile(r"真?丑|难看|画得烂|太难看了|好丑"), -4, "嫌她丑"),
    (re.compile(r"笨|蠢|傻|智障|废物|垃圾|没用"), -4, "骂她笨"),
    (re.compile(r"滚|闭嘴|烦死了|别烦我|讨厌你"), -5, "赶她走"),
    (re.compile(r"快点|赶紧|别磨蹭|磨叽|怎么这么慢"), -2, "催她"),
    (re.compile(r"别画了|别唱了|停下|算了"), -1, "打断她"),
    (re.compile(r"没用|白痴|菜|弱"), -3, "贬低"),
]

_cache = {"data": None, "ts": 0.0}

# ── 中文数字：她说「负二十」也得认 ──────────────────
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100}


def _cn2num(txt):
    """「二十三」→23，「负二十」→-20。只支持 ±一两百的小数字。"""
    t = str(txt or "").strip()
    if not t:
        return None
    neg = t.startswith(("负", "-", "−", "－", "+", "＋")) and t[0] not in ("+", "＋")
    t = t.lstrip("负-−－+＋")
    if t.isdigit():
        v = int(t)
        return -v if neg else v
    total, cur = 0, 0
    for ch in t:
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            total += (cur or 1) * _CN_UNIT[ch]
            cur = 0
        else:
            return None
    total += cur
    if total == 0 and not t:
        return None
    return -total if neg else total


# 她提到好感时后面跟的数字（阿拉伯或中文）。
# 半角 - 之外还得认全角 －/−：QQ 里输入法经常给全角。
_NUM = r"([-－−]?\d{1,3}|负[零〇一二三四五六七八九十百两]+|[零〇一二三四五六七八九十百两]+)"
_SELF_ABS = re.compile(r"好感(?:度)?[^0-9负\-－零〇一二三四五六七八九十百两]{0,8}" + _NUM)
_SELF_ABS2 = re.compile(_NUM + r"\s*分(?:的)?好感")
# 「涨/加/升/掉/降/扣」在前面 → 当增量算
_DELTA_UP = re.compile(r"(涨|加|升|多|添)")
_DELTA_DOWN = re.compile(r"(掉|降|扣|减|少)")
_DELTA_MARK = re.compile(r"(涨|加|升|掉|降|扣|减|多|少)")


# 「归还」：她说「好感度还你 / 还你一分 / 退回去 / 补回来」——
# 这类话没有「目标数值」，意思是「把刚才扣的还回去」。
# 必须单独识别，否则会被 parse_self_report 当成「没提到数字」而整个忽略。
_REFUND = re.compile(
    # 好感…还你 / 好感度…退你
    r"好感(?:度)?[^。！？,.!?\n]{0,10}(?:还|退|补|还回|退回)(?:给)?(?:你|您|他|她|回去|回来)"
    # 还你…好感 / 还你一分
    r"|(?:还|退|补)(?:给)?(?:你|您)[^。！？,.!?\n]{0,8}(?:好感|分)"
    # 好感度归你 / 好感度给你
    r"|好感(?:度)?[^。！？,.!?\n]{0,6}(?:归你|给你|归还)"
    # 就当我没说 / 收回那句话 / 不扣了
    r"|就当我没说|当我没说|(?:收回|撤回)(?:那句|刚才|这话)"
    r"|(?:不扣|不扣了|不加|不减)(?:了|啦)?(?:好感)?"
    # 光说「退给你 / 还给你 / 补给你」也认，但要带情绪语境，
    # 并且排除「还你钱 / 还你款」这种跟好感无关的。
    r"|(?:退|还|补)(?:给)?(?:你|您)(?!钱|款|账|元|块)"
    r"(?=[^。！？,.!?\n]{0,6}(?:好感|分|别哭|别闹|别气|消气|对不起|抱歉))"
    # 英文/谐音顺口：错在我 / 我的错 + 还回去
    r"|(?:错在我|我的错|我错了|算我错)[^。！？,.!?\n]{0,8}(?:还|退|补)"
)

_REFUND_BACK = re.compile(r"(还|退|补|归还|退回)")


def parse_refund(text):
    """她说要「把刚才扣的还回去」。返回 (显式数额或 None, 原文片段)。

    数额为 None 表示「原样还回去」——调用方按最近一次扣分补回。
    """
    t = str(text or "")
    if not t:
        return None
    m = _REFUND.search(t) or re.search(r'(?:刚才)?扣的[^。！？!?]{0,8}分(?:还|退|补)(?:给)?你', t)
    if not m:
        return None
    # 句子里如果带了数字（「还你三分」），就按那个数还
    win = t[max(0, m.start() - 6):min(len(t), m.end() + 8)]
    amt = None
    for n in _ANY_NUM.finditer(win):
        v = _cn2num(n.group(1))
        if v is not None and v != 0:
            amt = abs(v)
            break
    return amt, win.strip()[:60]


_ANY_NUM = re.compile(_NUM)


def _own_statement(text):
    t = str(text or '').strip()
    if re.search(r'[?？“”「」『』"`]|是不是|要不要|能不能|如果|要是|假如|他说|她说|你说|别人说|(?:不|没有|没)(?:会|想|用|能|是)?(?:给你|给他|给她)?(?:加|涨|扣|减|降)|别(?:加|扣|减)', t):
        return False
    return not re.search(r'盐|糖|调料|分钟|小时|游戏|仙剑|攻略|查询|查到|查好感|怎么.*好感|好感.*怎么看', t)


def _mutation(text):
    t = str(text or '').strip()
    if not _own_statement(t) and re.search(r'[?？“”「」『』"`]|是不是|如果|要是|假如|他说|她说|你说', t):
        return None
    clauses = re.split(r'[。！？!?\n，,；;]', t)
    for clause in clauses:
        clause = clause.strip()
        if not clause or not _own_statement(clause) or re.search(r'就能|才能|就会|才会', clause):
            continue
        m = re.search(r'(?<!刚才)(加|涨|升|扣|减|降|掉)(?!的)(?:你|您|他|她)?\s*(?:点|一点)?(?:好感度?|亲密度)?\s*(到|至|成)?\s*' + _NUM + r'\s*(?:点|分)?(?:的)?(?:好感度?|亲密度)?', clause)
        if m:
            tail = clause[m.end():]
            if tail and not re.match(r'^(?:吧|啦|呀|啊|哦|呢|哼|就|还|够|，|,|$)', tail):
                continue
            if not ('好感' in clause or '亲密' in clause or re.search(r'分|点', m.group(0))):
                continue
            value = _cn2num(m.group(3))
            if value is None:
                continue
            if not m.group(2):
                value = -abs(value) if m.group(1) in ('扣', '减', '降', '掉') else abs(value)
            return value, not bool(m.group(2)), clause[:100]
        abstract = parse_abstract_delta(clause)
        if abstract and re.search(r'(?:一点|一些|点|些|回去|回来)(?:好感度?|亲密度)?(?:吧|啦|呀|啊|哦|呢|嘛|哼)*$', clause):
            return abstract
    return None


def parse_self_report(text, allow_bare=False):
    """从**她自己的话**里读出「好感度应该是多少」。

    返回 (数值, 是增量?, 原文片段)；读不出来返回 None。

    「好感」前后各留一段窗口找数字——她两种说法都有：
        「好感度：负二十」  （数字在后）
        「给你加 5 点好感度」（数字在前）
    数字旁边有「涨/加/掉/降/扣」就是增量，否则当作绝对值。
    """
    t = str(text or "")
    # 「好感度」三个字不是必须的：她会说「顶多给你涨到十三」「就二十吧」这种省略句。
    # 调用方传 allow_bare=True 时跳过这个门槛（由 apply_self 决定什么时候允许）。
    if not t or not _own_statement(t):
        return None
    direct = _mutation(t)
    if direct and '好感' in t:
        return direct
    has_ref = "好感" in t
    if not has_ref and not allow_bare:
        return None
    if has_ref:
        base = t.index("好感")
        lo = max(0, base - 14)
        hi = min(len(t), base + 18)
        win = t[lo:hi]
    else:
        # 她省略了主语（「顶多给你涨到十三」「就二十吧」）：
        # 整句都是窗口，但只认带方向词/「X分」的句子，避免把随便一个数字当好感。
        base = 0
        lo = 0
        hi = len(t)
        win = t
        # 方向词里要排除「到/至/成/为」这种纯目标值说法单独出现的情况？不用——
        # 「涨到十三」「降到二十」本身就带涨/降，_ABS_MARK 会把 delta 判回绝对值。
        _BARE_OK = (re.search(r"(涨|加|升|掉|降|扣|减|多|少|变成|改为|算是|就算)", t)
                    or re.search(r"[零〇一二三四五六七八九十百两\d]{1,5}\s*(?:分|吧|呗|啦|呀)[^0-9零〇一二三四五六七八九十百两]{0,2}$", t.strip())
                    or re.search(r"[零〇一二三四五六七八九十百两\d]{1,5}\s*分", t))
        if not _BARE_OK:
            return None

    best = None
    for m in _ANY_NUM.finditer(win):
        raw = m.group(1)
        val = _cn2num(raw)
        if val is None:
            continue
        # 数字离「好感」的距离，取最近的
        dist = abs((lo + m.start()) - base)
        if best is None or dist < best[0]:
            best = (dist, m.start(), m.end(), val, raw)

    if best is None:
        return None

    _dist, ns, ne, val, _raw = best
    # 增量判定：数字前后 6 个字内出现方向词
    near = win[max(0, ns - 7):min(len(win), ne + 3)]
    near_wo_num = near.replace(_raw, "")
    up = bool(_DELTA_UP.search(near_wo_num))
    down = bool(_DELTA_DOWN.search(near_wo_num))
    is_delta = up or down

    # 「涨到 X / 降到 X / 变成 X / 改为 X」都是**目标值**，不是增量。
    # 以前漏了这个：她说「顶多给你涨到十三」被当成 +13，分数直接翻车。
    _ABS_MARK = re.compile(r"(?:到|至|成|为|作|为是)")
    if is_delta and _ABS_MARK.search(near_wo_num):
        is_delta = False   # 有「到/至/成/为」就是绝对值
    if is_delta and down and not up:
        # 「掉五 / 降三分」= 往下走，val 取负
        val = -abs(val)
    elif is_delta:
        val = abs(val)
    snippet = win[max(0, ns - 8):min(len(win), ne + 6)].strip()
    return val, is_delta, snippet[:60]


# 昵称缓存：发消息那一刻还不知道是谁，靠这里记着。
# 落盘是因为「她点名」时要靠它反查 QQ 号——进程重启就丢的话，
# 「阿乙那家伙十二分」这种句子又会记到别人头上。
_nicks = {}
_nicks_loaded = False


def _load_nicks():
    global _nicks_loaded
    if _nicks_loaded:
        return
    _nicks_loaded = True
    try:
        if os.path.isfile(_nicks_file()):
            with open(_nicks_file(), encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k, v in d.items():
                    if v:
                        _nicks[str(k)] = str(v)[:24]
    except Exception:
        pass


def set_nick(uid, nick):
    """记下这个人的群昵称，写记忆 / 反查 QQ 号时用。"""
    n = str(nick or "").strip()
    if n and len(n) <= 24:
        _load_nicks()
        if _nicks.get(str(uid)) == n:
            return
        _nicks[str(uid)] = n
        try:
            os.makedirs(_dir(), exist_ok=True)
            with open(_nicks_file(), "w", encoding="utf-8") as f:
                json.dump(_nicks, f, ensure_ascii=False)
        except Exception:
            pass


def get_nick(uid):
    _load_nicks()
    return _nicks.get(str(uid), "")


def _named_ids(text, aliases=None):
    _load_nicks()
    matches = {}
    for uid, nick in _nicks.items():
        if len(str(nick)) >= 2 and str(nick) in str(text):
            matches[str(uid)] = len(str(nick))
    for name, uid in (aliases or {}).items():
        if len(str(name)) >= 2 and str(name) in str(text):
            matches[str(uid)] = max(matches.get(str(uid), 0), len(str(name)))
    return matches


def resolve_uid_by_name(text):
    """她话里点了谁的名字，就返回那个人的 QQ 号。

    「阿乙那家伙，十二分。」→ 9000000002
    长名字优先：「阿戊」比「白子」更准。
    """
    _load_nicks()
    t = str(text or "")
    if not t:
        return None
    hit = None
    for uid, nick in _nicks.items():
        n = str(nick or "").strip()
        if len(n) < 2:
            continue
        if n in t:
            if hit is None or len(n) > hit[0]:
                hit = (len(n), str(uid))
    return hit[1] if hit else None


# 用户刚提过「好感」，她紧跟着那句可以省略主语（「行吧，给你涨三分」）
_pending = {}
PENDING_SECONDS = 120


def note_context(uid, text):
    """记录：这个人刚问过好感度。她下一句可以省掉主语。"""
    if "好感" in str(text or ""):
        _pending[str(uid)] = time.time()


def _pending_fresh(uid):
    ts = _pending.get(str(uid)) or 0
    return (time.time() - ts) < PENDING_SECONDS


def _consume_pending(uid):
    _pending.pop(str(uid), None)


# 前置那截不能吞掉 + - － ＋：以前它允许 '-' 当前缀，正则回溯后
# 「-16分。」被解析成 +16——负号整个丢了。现在符号只能进捕获组。
_BARE = re.compile(r"^[^0-9+＋-－负零〇一二三四五六七八九十百两]{0,6}"
                   r"([+＋-－]?\d{1,3}|负[零〇一二三四五六七八九十百两]+|[零〇一二三四五六七八九十百两]+)"
                   r"[^0-9]{0,4}$")


def parse_bare_number(text):
    """整句基本就一个数字：「负二十。」「-16分。」——问过好感之后她常这么答。

    _BARE 因为回溯偶尔会把开头的 '-' 当垃圾前缀吃掉（返回 '16'），
    所以这里补一道：句子本身以 +/-/－/− 开头的话，用 _ANY_NUM 重抓一次，
    保证符号不丢。
    """
    t = str(text or "").strip()
    m = _BARE.match(t)
    raw = m.group(1) if m else None
    # 句首就是符号但捕获组没带上 → 肯定被回溯吞了
    if raw and t[0] in "+-－−" and raw[0] not in "+-－−负":
        m2 = _ANY_NUM.search(t)
        if m2:
            raw = m2.group(1)
    if not raw:
        return None
    val = _cn2num(raw)
    if val is None:
        return None
    return val, False, t[:40]


_DELTA_ONLY = re.compile(r"(涨|加|升|掉|降|扣|减)[^0-9负零〇一二三四五六七八九十百两]{0,3}" + _NUM)


def parse_delta_only(text):
    """没有「好感」二字，但带方向词+数字：「给你涨三分」。

    注意「涨/加/降 + 到/至/成/为」是**目标值**（「给你加到99」=定在99），
    不是增量。以前这里一律返回 delta=True，pending 分支抢在
    parse_self_report 前面把「加到99」算成了 +99，分数直接顶到 100。
    """
    t = str(text or "")
    m = _DELTA_ONLY.search(t)
    if not m:
        return None
    # 「到/至/成/为」紧跟方向词 → 这是绝对值，交回上层按目标值处理
    gap = t[m.end(1):m.start(2)]
    if re.search(r"[到至成为作]", gap):
        return None
    val = _cn2num(m.group(2))
    if val is None:
        return None
    down = bool(_DELTA_DOWN.search(m.group(1)))
    val = -abs(val) if down else abs(val)
    return val, True, t[max(0, m.start() - 4):m.end() + 3].strip()[:60]


# 「我对[uid]的好感是」——uid 编进锚定串：同一个人在不同群的昵称不一样
# （主人一会儿叫「阿舟」一会儿叫「阿舟」），
# 拿名字当键会一人多条；拿 uid 当键才能做到**每人永远只有一条**。
_AFF_KEY = "我对[%s]的好感是"


def _remember(uid, nick, score, said):
    """把「她说了什么」同步进记忆，让后面几轮她还记得自己定的数。

    好感是**状态**不是历史：分数变了就覆盖旧条目，每人只留最新一条。
    以前每变动一次叠一条，检索出来全是过期数字（主人三条、阿甲三条）。
    """
    try:
        import persona_store as _ps
        who = nick or ("QQ" + str(uid))
        key = _AFF_KEY % uid
        line = "%s %s %.0f 分（我自己说的：%s）" % (key, who, round(score), str(said)[:30])
        _ps.replace_by_prefix("facts", key, line)
    except Exception as _re:
        print("[好感·记忆] 写入失败 " + type(_re).__name__, flush=True)


_applied = {}


def _audit(decision, uid, text, reason, **values):
    record = dict(ts=time.time(), decision=decision, uid=str(uid),
                  text=str(text)[:300], reason=reason, **values)
    with open(os.path.join(_dir(), 'affection_audit.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def _explicit_score(uid, text, got, nick='', turn_id=''):
    value, delta, snippet = got
    d = _load()
    e = _entry(d, str(uid))
    cur = float(e.get('score') or 0)
    recent = next((h for h in reversed(e.get('history') or [])
                   if turn_id and h.get('turn_id') == turn_id and h.get('by') == 'algo'
                   and abs(cur-h['to']) < 0.001), None)
    if turn_id and any(h.get('turn_id') == turn_id and h.get('by') in ('self', 'promise', 'refund')
                       for h in e.get('history') or []):
        return None
    base = recent['from'] if recent else cur
    target = base + max(-5, min(5, float(value))) if delta else float(value)
    if delta and abs(float(value)) > 5:
        _audit('bounded', uid, text, 'single_decision_delta_limit', proposed=value)
    if not delta and abs(target - base) > 10:
        deliberate = bool(re.search(r'(?:因为|由于).{4,}[，,].*(?:我决定|我决定).*(?:对你|对.{2,12})(?:的)?好感', str(text)))
        if not deliberate:
            _audit('rejected', uid, text, 'large_absolute_reset', before=cur, proposed=target)
            return None
    target = max(MIN_SCORE, min(MAX_SCORE, target))
    if any(h.get('text') == str(text).strip() and time.time()-h.get('ts',0) < 5
           for h in e.get('history') or []):
        return None
    source = 'promise' if parse_promise(text) else 'self'
    e.update(score=target, last=time.time(), said=snippet, source=source, why='她明确决定调整')
    history = list(e.get('history') or [])
    history.append(dict(ts=time.time(), **{'from':cur, 'to':target}, by=source, said=snippet,
                        replaces_algo=bool(recent), decision_base=base, turn_id=turn_id, text=str(text).strip()))
    e['history'] = history[-100:]
    _save(d)
    _audit('applied', uid, text, 'explicit_delta' if delta else 'explicit_target',
           before=cur, after=target, decision_base=base)
    _remember(uid, nick, target, snippet)
    return dict(uid=str(uid), score=target, delta=target-cur, source='self', said=snippet)


_ABS_UP = re.compile(r"(加|涨|升|补|还)[^0-9零〇一二三四五六七八九十百两]{0,3}(一点|一些|点|回去|回来|些)")
_ABS_DOWN = re.compile(r"(减|降|扣|掉|少)[^0-9零〇一二三四五六七八九十百两]{0,3}(一点|一些|点|些)")


def parse_abstract_delta(text):
    """她没报数字、只表态度的抽象增减：「加一点」「减一点」「给你加回去」。

    返回 (delta, True, 片段) 或 None。只认短句，长句里出现要带“对谁/态度”线索，
    免得把「这道菜加一点盐」当成好感。
    """
    t = str(text or "").strip()
    if not t or len(t) > 16:
        return None
    up = _ABS_UP.search(t)
    down = _ABS_DOWN.search(t)
    if not up and not down:
        return None
    if len(t) > 8 and not re.search(r"你|您|主人|我|好感|吧|啦|哼|嘛|宝贝|乖", t):
        return None
    if down and not up:
        return -1, True, t[:30]
    if up and not down:
        return 1, True, t[:30]
    # 两种方向词都出现：按后出现的判方向
    return (-1 if down.start() > up.start() else 1), True, t[:30]


@_transaction
def set_score(uid, value, why="面板手动"):
    """面板直接设定绝对值（修复误读 / 手动校正）。"""
    d = _load()
    e = _entry(d, str(uid))
    cur = float(e.get("score") or 0)
    new = max(MIN_SCORE, min(MAX_SCORE, float(value)))
    e["score"] = new
    e["last"] = time.time()
    e["source"] = "panel"
    e["why"] = why
    e["said"] = why
    hist = e.get("history") if isinstance(e.get("history"), list) else []
    hist.append({"ts": time.time(), "from": round(cur, 1), "to": round(new, 1),
                 "by": "panel", "said": why})
    e["history"] = hist[-100:]
    _save(d)
    _audit('applied', uid, why, 'administrator_set', before=cur, after=new)
    return e


@_transaction
def apply_self(uid, text, nick="", allow_bare=False, turn_id=''):
    nick = nick or get_nick(uid)
    got = _mutation(text)
    if not got and _own_statement(text) and parse_refund(text):
        d = _load()
        e = _entry(d, str(uid))
        cut = next((h for h in reversed(e.get('history') or [])
                    if h.get('by') in ('self', 'promise') and h.get('to', 0) < h.get('from', 0)
                    and time.time() - h.get('ts', 0) < 1800), None)
        if not cut or cut.get('refunded'):
            return None
        amount, _ = parse_refund(text)
        available = cut['from'] - cut['to']
        back = min(10, available, amount if amount is not None else available)
        before = float(e.get('score') or 0)
        e['score'] = min(MAX_SCORE, before + back)
        cut['refunded'] = True
        e.setdefault('history', []).append(dict(ts=time.time(), **{'from':before, 'to':e['score']}, by='refund', said=str(text)[:100], turn_id=turn_id))
        e.update(source='self', last=time.time(), said=str(text)[:100], why='退回真实近期扣分')
        _save(d)
        _audit('applied', uid, text, 'refund', before=before, after=e['score'])
        return dict(uid=str(uid), score=e['score'], delta=e['score']-before, source='self', said=str(text)[:100])
    if not got:
        if '好感' in str(text):
            _audit('ignored', uid, text, 'report_or_unattributed_statement')
        return None
    key = (os.path.abspath(_file()), str(uid), str(text).strip())
    if time.time() - _applied.get(key, 0) < 5:
        return None
    result = _explicit_score(uid, text, got, nick, turn_id=turn_id)
    if result:
        _applied[key] = time.time()
        _consume_pending(uid)
    return result




# ── 她的承诺：说了「加一分」，就要真的加一分 ─────────────
# 以前只有「有人刚问过好感」时才认她的话。主人问的是「能不能涨一点呀」——
# 没带「好感」两个字，于是她那句「就加一分吧，八十五分」无处落地，
# 面板上的数字纹丝不动，聊天里却已经加过了。
_RE_PROMISE = re.compile(
    r"(就|那|再|顶多|最多|给你|给你加|给你涨)?\s*"
    r"(加|涨|扣|降|减)\s*(?:你|他|她)?\s*"
    r"([零〇一二两三四五六七八九十百\d]{1,3})\s*分(?!钟|享|之|开|手|期)")
_PROMISE_UP = ("加", "涨")
# 短句 + 有称呼/语气词，才当成好感承诺。
# 「这道菜加五分」这种没有方向的，宁可漏掉也不能误伤。
_RE_PROMISE_OK = re.compile(r"(就|那|再|你|主人|给你|我)")


def parse_promise(text):
    """从她的话里读「加X分 / 扣X分」，返回 (delta, 原文片段) 或 None。"""
    t = str(text or "").strip()
    if not t or len(t) > 48:
        return None
    if "好感" in t:
        return None          # 带「好感」的走 parse_self_report，别两条路都算
    m = _RE_PROMISE.search(t)
    if not m:
        return None
    if m.start() > 12:
        # 铺垫太长就不像在说好感（「今天副本打了好久…我给你加一分吧」）
        return None
    if not _RE_PROMISE_OK.search(t):
        return None
    n = _cn2num(m.group(3))
    if not n:
        return None
    delta = int(n) if m.group(2) in _PROMISE_UP else -int(n)
    return delta, m.group(0).strip()[:24]


@_transaction
def apply_promise(uid, text, nick=""):
    if not parse_promise(text):
        return None
    return apply_self(uid, text, nick=nick)


@_transaction
def apply_self_any(text, max_age=120, uid=None, aliases=None, turn_id=''):
    got = _mutation(text)
    if not got and _own_statement(text) and parse_refund(text):
        got = (0, True, str(text))
    if not got:
        if '好感' in str(text):
            _audit('ignored', uid or '', text, 'not_an_explicit_relationship_change')
        return None
    clause = got[2]
    target = str(uid or '')
    explicit = re.search(r'给([^，,。！？!?]{1,16}?)(?:的)?(?:好感度?)?(?:加|涨|升|扣|减|降|掉)', clause)
    explicit = explicit or re.search(r'对([^，,。！？!?]{1,16}?)(?:的)?好感', clause)
    named = _named_ids(explicit.group(1) if explicit and explicit.group(1) not in ('你', '您') else str(text), aliases)
    if explicit and explicit.group(1) not in ('你', '您'):
        if explicit.group(1) in ('他', '她') or len(named) != 1:
            _audit('rejected', target, text, 'unknown_or_ambiguous_named_target')
            return None
        target = next(iter(named))
    elif len(named) == 1:
        target = next(iter(named))
    elif len(named) > 1:
        _audit('rejected', target, text, 'ambiguous_named_target')
        return None
    if not target:
        candidates = [str(k) for k,v in _pending.items() if time.time()-v < max_age]
        if len(candidates) != 1:
            return None
        target = candidates[0]
    return apply_self(target, text, allow_bare=False, turn_id=turn_id)


def query_line(uid, nick=""):
    """塞进 prompt 的「准确数值」——她说出口的数字必须是这个。

    以前她是凭感觉编一个数（群里说「负二十」、面板显示 -23），
    两边永远对不上。现在把真值硬塞给她，编不了。
    """
    sc = get(uid)
    who = ("「" + nick + "」") if nick else "这个人"
    return ("\n\n【好感度真值 · 说数字必须用这个】\n"
            "你对%s的好感是 **%d** 分（%s）。\n"
            "如果被问「查询好感度」或要报数字，只能报 %d，不许四舍五入成别的数、"
            "也不许自己估一个。语气按这个分档来。"
            % (who, round(sc), _label(sc), round(sc)))


@_transaction
def ensure_seeded():
    """把初始设定落盘：主人亲近、阿丙负好感、其他人从 0 起。

    不落盘的话，面板上就看不到这两个人——他们没触发过加分/扣分，
    文件里压根没他们的条目。
    """
    d = _load()
    changed = False
    for uid, val in SEED.items():
        if str(uid) not in d:
            d[str(uid)] = {"score": float(val), "day": _today(),
                           "gain": 0.0, "loss": 0.0, "last": 0.0,
                           "why": "初始设定"}
            changed = True
    if changed:
        _save(d)
    return d


def _load():
    try:
        if os.path.isfile(_file()):
            with open(_file(), encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError('affection store must be an object')
            return d
        return {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print('[好感] 读取失败，拒绝用空账本覆盖 ' + type(exc).__name__, flush=True)
        raise
    raise ValueError('affection store must be an object')


def _save(d):
    try:
        os.makedirs(_dir(), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='affection-', suffix='.tmp', dir=_dir())
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, indent=1, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _file())
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception as e:
        print("[好感] 存盘失败 " + str(e)[:60], flush=True)
        raise


def _today():
    return time.strftime("%Y-%m-%d", time.localtime())


def _entry(d, uid):
    k = str(uid)
    e = d.get(k)
    if not isinstance(e, dict):
        e = {"score": float(SEED.get(k, 0)), "day": _today(),
             "gain": 0.0, "loss": 0.0, "last": 0.0, "why": ""}
        d[k] = e
    if e.get("day") != _today():
        e["day"] = _today()
        e["gain"] = 0.0
        e["loss"] = 0.0
    return e


def get(uid):
    """现在多少分。"""
    d = _load()
    e = d.get(str(uid))
    if not isinstance(e, dict):
        return float(SEED.get(str(uid), 0))
    return float(e.get("score") or 0)


@_transaction
def bump(uid, delta, reason="", turn_id=''):
    """加分/扣分。受范围、单次幅度、每日额度三重约束。"""
    if not delta:
        return 0.0
    d = _load()
    e = _entry(d, uid)
    cur = float(e.get("score") or 0)
    # 单次最多动 5
    delta = max(-5.0, min(5.0, float(delta)))
    # 当日额度
    if delta > 0:
        room = DAILY_CAP - float(e.get("gain") or 0)
        delta = min(delta, max(0.0, room))
        e["gain"] = float(e.get("gain") or 0) + max(0.0, delta)
    else:
        room = DAILY_CAP - float(e.get("loss") or 0)
        delta = -min(abs(delta), max(0.0, room))
        e["loss"] = float(e.get("loss") or 0) + abs(delta)
    new = max(MIN_SCORE, min(MAX_SCORE, cur + delta))
    e["score"] = new
    e["last"] = time.time()
    if reason:
        e["why"] = reason
    e['source'] = 'algo'
    history = list(e.get('history') or [])
    history.append(dict(ts=time.time(), **{'from':cur, 'to':new}, by='algo', said=reason, turn_id=turn_id))
    e['history'] = history[-100:]
    _save(d)
    _audit('applied', uid, reason, 'keyword_fallback', before=cur, after=new)
    return new


def judge(text):
    """按关键词算出这次该加还是该扣。返回 (delta, reason)。

    只认最明显的那一条，避免一句话被连加好几次。
    """
    t = str(text or "")
    if not t:
        return 0.0, ""
    worst = (0.0, "")
    for pat, val, why in _MINUS:
        if pat.search(t):
            worst = (val, why)
            break               # 扣分优先：先看有没有得罪她
    if worst[0]:
        return worst
    for pat, val, why in _PLUS:
        if pat.search(t):
            return (float(val), why)
    return 0.0, ""


# ── 冷却：同一个方面不许连着刷 ─────────────────────
# 没有冷却时，连说三句"谢谢"就能把当天额度打满，好感成了刷分游戏。
# 这里按 (人对, 原因) 记上次计分时间，冷却期内同一个原因不再计分。
COOLDOWN_SECONDS = float(os.getenv("AFFECTION_COOLDOWN_SECONDS", "600"))  # 10 分钟
_cd = {"data": None}


def _load_cd():
    try:
        if os.path.isfile(_cooldown_file()):
            with open(_cooldown_file(), encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                _cd["data"] = d
                return d
    except Exception:
        pass
    _cd["data"] = {}
    return _cd["data"]


def _save_cd(d):
    try:
        os.makedirs(_dir(), exist_ok=True)
        tmp = _cooldown_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _cooldown_file())
        _cd["data"] = d
    except Exception:
        pass


@_transaction
def judge_and_bump(uid, text, nick="", turn_id=''):
    """判定 + 冷却 + 计分，一条龙。返回 (实际变动, 原因描述)。

    调用方不要再用 judge() + bump() 两步，
    那样每次都绕过了冷却。
    """
    note_context(uid, text)
    if turn_id and any(h.get('turn_id') == turn_id for h in (_load().get(str(uid)) or {}).get('history', [])):
        return 0.0, ''
    if re.search(r'[“”「」『』"`]|他说|她说|别人说', str(text)):
        return 0.0, ''
    if re.search(r'(?:喜欢|抱抱|亲亲|夸夸|讨厌|骂|谢谢)\s*(?!你|我|清漪|宝宝|宝贝)[\u4e00-\u9fff]{2,}', str(text)):
        return 0.0, ''
    delta, reason = judge(text)
    if not delta or not reason:
        return 0.0, ""

    key = "%s|%s" % (str(uid), reason)
    now = time.time()
    cd = _load_cd()
    last = float(cd.get(key) or 0)
    if last and (now - last) < COOLDOWN_SECONDS:
        left = int((COOLDOWN_SECONDS - (now - last)) / 60)
        return 0.0, "%s(冷却 %d 分钟)" % (reason, max(1, left))

    cd[key] = now
    # 只保留最近 500 条，防止文件无限膨胀
    if len(cd) > 500:
        items = sorted(cd.items(), key=lambda x: -float(x[1] or 0))[:500]
        cd = dict(items)
    _save_cd(cd)

    before = get(uid)
    new_score = bump(uid, delta, reason, turn_id=turn_id)
    actual = new_score - before
    return actual, "%s -> %.0f" % (reason, new_score)


def cooldown_status(uid=None):
    """给面板/调试看：当前冷却中的项。"""
    cd = _load_cd()
    now = time.time()
    out = []
    for k, ts in cd.items():
        if uid and not k.startswith(str(uid) + "|"):
            continue
        left = COOLDOWN_SECONDS - (now - float(ts or 0))
        if left > 0:
            parts = k.split("|", 1)
            out.append({"uid": parts[0], "reason": parts[1] if len(parts) > 1 else "",
                        "left_sec": int(left)})
    out.sort(key=lambda x: -x["left_sec"])
    return out


def tier(score):
    """分档，只用来说话语气。"""
    if score >= 55:
        return "close"      # 亲近：软得多，顺着说
    if score >= 20:
        return "warm"       # 有好感
    if score > -10:
        return "normal"     # 基准
    if score > -30:
        return "cold"       # 不耐烦
    return "hostile"        # 很烦


_HINT = {
    "close": ("跟这个人你是有感情的，愿意多让着点。\n"
              "  可以主动贴一下、多说一句软话，顺着 TA 的话接。\n"
              "  但傲娇不能丢——嘴上占点便宜、嫌弃半句再说软话，这才是你。\n"
              "  例：「行行行，都听你的——就这一次啊。」「笨蛋，下次早点说。」"),
    "warm": ("这个人在你眼里还算顺眼。\n"
             "  愿意搭话，偶尔夸一句或者记着他之前说过什么。\n"
             "  傲娇照旧，只是嫌弃的比例少一点。"),
    "normal": ("普通关系。按你平常那样说话就好。"),
    "cold": ("这个人最近有点让你不耐烦。\n"
             "  回话短一点、怼两句、懒得解释。但别真伤人，更别拉黑式的冷漠——\n"
             "  你还是会回话，只是没耐心。"),
    "hostile": ("你挺烦这个人的。\n"
                "  语气冲、爱答不理、一句话噎回去。但他真有事还是管——\n"
                "  嘴上说「自己想办法」，手上还是会帮。这就是你的傲娇。"),
}


def hint(uid, nick=""):
    """给 prompt 用的一段话。

    以前没人记录就直接返回空串——于是她在群里被陌生人问「查询好感度」时
    **手里没有任何真值**，只能现场编一个，和面板对不上。
    现在不管有没有记录，都把真值（默认 0）塞给她，编不了。
    """
    if not str(uid or "").strip():
        return ""
    d = _load()
    k = str(uid)
    sc = get(uid)
    t = tier(sc)
    who = ("「" + nick + "」") if nick else "这个人"
    body = _HINT.get(t) or _HINT["normal"]
    head = ("\n\n【你对%s的好感】%s\n%s"
            % (who, _label(sc), body))
    if k not in d and k not in SEED:
        # 没打过交道的陌生人：只给真值，不编语气
        return query_line(uid, nick)
    return head + query_line(uid, nick)


def _label(sc):
    if sc >= 55:
        return "很亲近（%d）" % round(sc)
    if sc >= 20:
        return "有好感（%d）" % round(sc)
    if sc > -10:
        return "一般（%d）" % round(sc)
    if sc > -30:
        return "不太耐烦（%d）" % round(sc)
    return "挺烦的（%d）" % round(sc)


def snapshot():
    """给面板看的。"""
    d = ensure_seeded()
    out = []
    for k, e in d.items():
        if not isinstance(e, dict):
            continue
        sc = float(e.get("score") or 0)
        out.append({"uid": k, "score": round(sc), "tier": tier(sc),
                    "nick": get_nick(k),          # 群名片：面板认人靠它，别光看 QQ 号
                    "why": e.get("why") or "",
                    "said": (e.get("said") or "").replace("\n", " ").strip(),
                    "source": e.get("source") or "algo",
                    "last": e.get("last") or 0,
                    "history": (e.get("history") or [])[-5:]})
    out.sort(key=lambda x: -x["score"])
    return out


@_transaction
def reset(uid=None):
    d = _load()
    if uid is None:
        d.clear()
    else:
        d.pop(str(uid), None)
    _save(d)
    return True
