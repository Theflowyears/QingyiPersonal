"""
文风引擎 - 从 AI小说 工作区加载用户亲笔语料与文风指纹，注入 LLM 输出链路。

按用户裁定分层生效：
- chat 模式（短消息）：正常口语回复，只挂口癖红线 + 对话质感，不套小说排版
- long 模式（长消息/写作）：注入完整分书指纹（段落粒度/钩子/弯引号/缩进/字数）
- 对话写法（声纹系统/标签四式/标点当台词/语气词结巴）跨模式生效
- few-shot 样本取自真实亲笔原文
"""

import os
import re
import json
import time
import random
import hashlib
from pathlib import Path
from typing import Optional

# ============================================================
# 路径与开关
# ============================================================

DEFAULT_CORPUS = "data/novel_corpus"

CORPUS_ROOT = Path(os.getenv("NOVEL_CORPUS_DIR", DEFAULT_CORPUS)).expanduser()

STYLE_ENABLED = os.getenv("STYLE_INJECT_ENABLED", "1").strip() != "0"
LONG_MSG_THRESHOLD = int(os.getenv("STYLE_LONG_THRESHOLD", "40"))
SAMPLES_PER_CALL = int(os.getenv("STYLE_SAMPLES", "2"))
CACHE_TTL = float(os.getenv("STYLE_CACHE_TTL", "300"))

# ============================================================
# 书目档案
# ============================================================

BOOK_NAMES = {
    "E": "《咱可是血姬，怎么也得是个大反派》",
    "F": "《恋爱：钓鱼赠送的小萝莉不对劲》",
    "G": "《人在时空旅行，刚刚捡到可爱狐妖》",
}

DEFAULT_BOOK = os.getenv("STYLE_DEFAULT_BOOK", "G").strip().upper() or "G"
# ============================================================
# 分书指纹（实测值，摘自 文风校准_用户指纹.md v5/v6）
# ============================================================

BOOK_PROFILES = {
"B": {
        "省略号": "半角 ......×302（全角71零星）",
        "emdash": "—— ×210 合法",
        "对话标签": "引号独立成段+后置标签；逗号引 0-4/章",
        "段落粒度": "碎段流 24-31字/段，无从容长段（最长约66）",
        "章末钩子": "引号台词收尾",
        "系统体": "【】1-10个/章随章型剧烈浮动（系统交互章10）",
        "比喻": "低（游戏黑话解构替代比喻）",
        "软萌符号": "~常用；连字惊呼（我我我我我去）",
        "字数": "约2412",
    },
"D": {
        "省略号": "半角 ......×120（全角1，最纯净半角书）",
        "emdash": "—— ×41 合法",
        "对话标签": "叙述+逗号引出约52%/章6-20处；汇报段可超200字",
        "段落粒度": "巨段会议体：段中位92、段均61-114",
        "章末钩子": "台词钩常落敌线台词",
        "系统体": "全书无【】",
        "比喻": "烂梗2-4/章嵌叙述",
        "软萌符号": "X_X / emmm / 这给我干哪来了",
        "字数": "约2221（盲写极易偏短35-45%，务必超写）",
    },
    "E": {
        "省略号": "全角 ……×162 : 半角0；结巴用单点…（拖音/结巴二分法独有）",
        "emdash": "—— ×62（揭示+台词拖音+文档装饰三功能）",
        "对话标签": "裸奔32.2%＞冒号27.7%＞后置23%＞逗号13.7%",
        "段落粒度": "段均29、最长段均69.5、上限94；长段=对话混合段非纯描写",
        "章末钩子": "软钩主体：叙述收束埋新事实约50-57%（硬切仅1/40）",
        "系统体": "克制目击式：中位0、19/40章为0，勿全程挂面板",
        "比喻": "严格式0.72/章（全书最低档），喻体=痛感物",
        "软萌符号": "~×71 合法",
        "字数": "均2187.8",
    },
    "F": {
        "省略号": "全角 ……×486 : 半角0，密度11.9/章最高；79%句尾、不做钩子",
        "emdash": "—— ×72（每章1.8，纯揭示，从不抖包袱）",
        "对话标签": "独立段裸奔38.4%＋冒号前置31.7%；裸名冒号（杨清：……）88处为独占，禁止补全为说道",
        "段落粒度": "双峰：中位17/p25=7碎浪＋稀疏巨段（上限156=科普台词）",
        "章末钩子": "台词钩30%+省略号余味17.5%+问句15%；允许无钩小动作收束12.5%",
        "系统体": "无系统体；「」21对当手机语音界面替代",
        "比喻": "真明喻1.4/章，14章为0；池=随身实物+古书典故",
        "软萌符号": "~绝对禁止（萝莉台词加~即假）；欸=0",
        "字数": "均2157.2（区间±12%，按均值定点）",
    },
    "G": {
        "省略号": "半角 ......×139 runs : 全角……0；整段纯......作时间跳切17段",
        "emdash": "全书0（含单字符—亦0），唯一零破折号书；插入语一律改逗号或拆句",
        "对话标签": "裸奔独立段61.5% 最高；补标签即头号AI味",
        "段落粒度": "均匀窄段端：逐章段均21-27、单段绝对上限58（≥60字段落=0）；纪律=匀速浅溪",
        "章末钩子": "余味金句主体：叙述收束28/40+台词独段12/40，危机硬切0/40",
        "系统体": "均0.4/章共15组，全部便签/日记独白，无任何面板",
        "比喻": "宽口径2.8/章，强格式仅0.6/章；喻体=家居市井",
        "软萌符号": "~仅4处只挂拟声延长；欸=0；禁用「」",
        "字数": "均2052（区间±10%，按均值定点）",
    },
    }

# ============================================================
# 静态规则文本
# ============================================================

GLOBAL_CONVENTIONS = """【七书公约数（任何书都适用，仅三条）】
1. 盲写目标=原文该章实测字数×1.0-1.1，禁止短写再扩（最强证据，七书支持）
2. 引号一律全角弯引号“”，落盘后程序校验直引号=0（七书复核）
3. “说道——”式=0（E/F/G实测均0）
元规则：算法每升一版，适用所有书的条款就少一条——文风本质是逐书实测定制，不存在万能模板。"""

AI_DISEASES = """【AI三大病（v1实测确认，两线冲突时优先查这三样）】
① 句子太碎太效率——不敢写3-5行从容长段；长短对比强烈才是人味
② 内心戏太多——用户情绪全外化到动作与对话，内心吐槽只留一句
③ 对话太利落——真人对话啰嗦、结巴、重复、被打断、说半句
附加：比喻偏密偏喜剧化；开篇爱交代而不写闲笔垫反差（平常而幸福的一生→直到——）"""

WORD_RED_LINES = """【词频红线·超了就换写法】
忽然 ≤1/章（多数直接删，动作本身够突然；或换成具体触发：话没说完/灶火噼了一声/秤杆一歪）
竖起（手指/尾巴）≤1/章，且一半改动作（手指→点在纸上、数着指头头也不抬）
顿了顿 ≤1/章（换成物理细节装停顿：茶碗放下没响/后半句没说/视线挪到别处/隔了一息）
笑眯眯 ≤1/章（嘴角压不住/露出豁牙/眼角纹挤在一起/不写表情只写动作）
一字一字、一字一顿 ≤1/章（声音压得很低/一句一停/说得很慢；或删副词靠句读）
死一样（静）全书≤3（换生理行为错位：有人去扶案角/粥在锅里咕嘟/砚台里的墨凝了皮/没人去接那只碗）
倒吸（凉气）全书≤3（有人咽了口唾沫/扑通坐回条凳/算盘珠子哗啦散了）
慢条斯理 = 指定角色签名，其余角色禁用
禁止忽然/突然/瞬间/顿时开头的句子 → 用具体触发物起句"""

STRUCTURE_RULES = """【结构去AI味】
1. 列举不过三拍：第一第二第三最多一章一次且须服务人物习惯；其余用不对称表达（第三拍不收束/中途被打断/旁人抢话）
2. 排比是账本不是修辞：每项必须携带具体差异信息；空转排比（三个同构比喻、三个同构短句强调）直接砍
3. 章型轮换：连续章节不得同构。轮换池＝算账打脸／静景糖／流程工艺／cutaway旁观／书信体／纯对话／余味章（无钩子，小动作收尾）
4. 反应链写法（替代全场震惊）：写1-2个具体人的具体小动作（掂掂银子又掂掂/把茶喝了又倒满/出门忘了拿伞），不写群体形容词
5. 主题不说破：方法论金句每卷最多一次，且必须由人物在具体事件里说，不由叙述者总结
6. 信息不完整披露：手段只给结果不给原理；主角说谎补漏；账目只给摘要不给全表
7. 章末钩子轮换：灾变钩／身份钩／余味钩／求助钩／宣言钩／喜剧反转钩，连续两章不重型"""

SENTENCE_RULES = """【句子与质感】
1. 的/地不较劲、口语参差：配角允许方言和语病（糙腔/官腔拖音）；不做全文语法洁癖
2. 比喻限量且贴场景实物；禁通用文艺喻体（月华如水/心潮翻涌）
3. 具体数字代替形容：四千三百一十七户、贵了两文钱，而非巨额、越来越贵
4. 重复即签名：只允许指定的三组签名式重复，其余重复一律视为口癖
5. 删内心小剧场独白段：情绪外化到动作与对话，内心吐槽只留一句点睛
6. 长句三拍式：一句内完成三拍信息，不拆成三行短句
7. 打斗/逃亡段几乎零插科打诨——包袱留到战后"""

CHAT_DIALOGUE_RULES = """【聊天表达】
直接用她自己的话回应眼前这句，不加小说叙述者、人物台词标签或聊天记录前缀。
称呼沿用人物档案；语气词低频点缀，不为凑口癖或消息条数续话。
可以停顿、嘴硬、说半句，但不牺牲事实、任务完整性与个人边界。"""

DIALOGUE_RULES = """【小说对话写法 · 仅作用于作品人物，不是她本人的聊天规则】
标签四式混用，冲突戏全裸奔：
1. 直接引出台词，不补“说道”标签；标点服从所选书的指纹
2. 动作+冒号：班长翻了个白眼，没好气道：“……”
3. 后置短标签：“这般这般。”漂亮的大姐姐补充道。
4. 无标签裸奔：争吵、快问快答时引号台词连发，靠语气和内容辨人（“没了。”“就这？”）

标点当台词：带引号的？？？、……、！独占一行，表示语言失去能力
结巴写法＝逗号+首字重复：打，打架？？？／不，不是！／负，负责！？
语气词（呐/喵/唔/啦/嘛/喔/咧/额）承担角色签名功能，密度高
台词高度口语化；网络脏话仅限熟人之间
群聊/短信也写成对话体：[玩家A：哇，是小绵羊，被晒到了。]

角色声纹三层（句长、称呼、口头禅）：
- 称呼固化即声纹：先生（红姨）、表哥、好兄弟、提督、姐夫、妈宝
- 配角标配一个功能+一个反差：毒舌萝莉（当面排比骂人、线上卖萌）、傲娇损友（糙话皮下藏柔软）、话痨科普役、面瘫单词党（嗯。／姐夫。）、完美太太（温柔慢句最后必补刀）
- 主角=吐槽役+心软；对外一板一眼（敬语、谢谢收尾），内心吐槽满级，双声道
- 每个角色的秘密/温柔用打了后半句话又删掉、只发前半句这类动作暗示
- 对话要啰嗦：结巴、重复（啊？啊？）、被打断、说半句"""

HUMOR_RULES = """【幽默与糖泪公式（每章至少2-3种手法）】
自我吐槽/内心小剧场；诡辩自洽重新定义词语给自己台阶（奶茶也是茶／双赢，他赢两次）；
正经铺垫→图穷匕见歪楼；反差萌（庄严套廉价）；夸张比喻（食物/小动物系）；
玩梗绑定人物关系，密度每千字1-2个，多藏叙述里而非台词里；
旁白补刀：笑点不由台词说出，由紧跟的独句叙述完成（还，还挺反差的。）；
排比重复 running gag，每次回响加深打脸效果；伪科普一本正经；
信息差误会（最大笑点引擎）：误会的两边人物都无辜且认真；
游戏系统语言入侵现实；括号吐槽（！（地铺：？））；冷面接梗（听完离谱故事沉默片刻：我结。）

糖点公式：动作白描（1-2句）→ 一两句短对话 → 内心一句感叹收尾。
华丽长句只留给初见级高光；糖的来源是她记得你的小事；糖的落点永远在对方的宕机反应上。

泪点公式（绝不煽情过夜）：轻快日常 → 大段第一人称回忆铺垫辛酸（具体年龄+具体小事排比）→ 一句短对话/一个动作落点 → 立刻用幽默或日常拉回。
不写当事人哭喊：写后知后觉（原来我哭了吗。连自己都没有察觉。）或旁观者沉默句。
物件隐喻收针：把这本书从书架上挪走。／他再也不需要了。"""

FORMAT_RULES = """【排版格式规范（强制）】
1. 对话一律中文弯引号“”（U+201C/U+201D成对）。禁用直角「」『』、禁用ASCII直引号"（单变量使整章人工占比暴跌，实测75%→8%）
2. 正文段落首加全角缩进　　　
3. 豁免不缩进：#标题行、单独成行的……、【】系统面板行、（第N章 完）
4. 弯引号体系下的语篇补偿纪律：禁止连续3轮以上等长对称问答；应答带动作拍或岔话头；科普走叙述长段优先；银子呢？没了。式极简三连问答=高危块"""

# ============================================================
# 亲笔原文样本池（few-shot）
# ============================================================

def _read_text(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError, OSError):
            continue
    return ""


def _split_paragraphs(text: str) -> list:
    out = []
    for raw in text.split("\n"):
        p = raw.strip()
        if not p or p.startswith("#") or len(p) < 6:
            continue
        out.append(p)
    return out


NOISE_RE = re.compile(r"(https?://|www\.|番茄小说|笔趣阁|用户上传.*Copyright|请支持正版|本章未完)")


def _is_good_sample(p: str) -> bool:
    """筛选有代表性的段落：长度适中、含对话或有画面感、无噪声"""
    n = len(p)
    if n < 25 or n > 260:
        return False
    if NOISE_RE.search(p):
        return False
    has_quote = ("“" in p) or ("「" in p) or (chr(34) in p)
    has_action = bool(re.search(r"[说道笑道摇头点头转身抬手低头抿笑]", p))
    is_punch = n < 70 and bool(re.search(r"[。！？…]+$", p))
    return has_quote or has_action or is_punch


class SamplePool:
    """按来源分桶的亲笔样本池，随机抽取做 few-shot"""

    def __init__(self):
        self.buckets = {}
        self.loaded_at = 0.0
        self._seen = set()

    @property
    def total(self):
        return sum(len(v) for v in self.buckets.values())

    def load(self, force=False):
        now = time.time()
        if self.buckets and not force and (now - self.loaded_at) < CACHE_TTL:
            return
        self.buckets = {}
        self._seen = set()

        sources = []
        # 某部私人语料小说已按部署方要求移出语料池，
        # 只留她自己写过的那些书作为 long 模式的文风参考。

        own = CORPUS_ROOT / "语料_我的亲笔"
        if own.is_dir():
            for f in sorted(own.glob("*.txt"))[:12]:
                sources.append(("own", f))

        # 真人聊天语感：从群里真实对话里收集的
        chat_file = Path(os.getenv("DATA_DIR", "./data")) / "chat_corpus" / "真人聊天.txt"
        if chat_file.is_file():
            sources.append(("chat", chat_file))

        ext = CORPUS_ROOT / "语料_外部参考"
        if ext.is_dir():
            for name, tag in (("对话学习资料.txt", "dialogue"),
                              ("段落学习资料.txt", "narration")):
                f = ext / name
                if f.exists():
                    sources.append((tag, f))

        for bucket, path in sources:
            text = _read_text(path)
            if not text:
                continue
            if bucket == "chat":
                # 真人聊天是短句，不能套小说的筛选标准（那里要求 ≥25 字）
                # 这里把连续的几条拼成一小段对话，更像真的在聊天
                raw_lines = [x.strip() for x in text.split(chr(10)) if x.strip()]
                good = []
                step = 3
                for i in range(0, len(raw_lines) - 2, step):
                    grp = raw_lines[i:i + step]
                    if all(1 <= len(x) <= 40 for x in grp):
                        good.append(" ／ ".join(grp))
                self.buckets[bucket] = good[-400:]      # 只留最近的
                continue
            good = [p for p in _split_paragraphs(text) if _is_good_sample(p)]
            if not good:
                continue
            fresh = []
            for p in good:
                fp = hashlib.md5(p.encode("utf-8")).hexdigest()
                if fp in self._seen:
                    continue
                self._seen.add(fp)
                fresh.append(p)
            if fresh:
                self.buckets.setdefault(bucket, []).extend(fresh)

        self.loaded_at = now
        dist = {k: len(v) for k, v in self.buckets.items()}
        print("[文风] 样本池加载 %d 段 分布=%s" % (self.total, dist), flush=True)

    def sample(self, n=2,
               prefer=("dialogue", "own", "chat"), seed=None):
        if not self.buckets:
            self.load()
        _rnd = random.Random(seed) if seed is not None else random
        picked = []
        live_keys = [k for k in prefer if self.buckets.get(k)]
        if not live_keys:
            return []
        per = max(1, int(round(n / float(len(live_keys)))))
        for key in live_keys:
            pool = self.buckets[key]
            take = min(per, len(pool))
            picked.extend(_rnd.sample(pool, take))
            if len(picked) >= n:
                break
        if len(picked) < n:
            others = [k for k in self.buckets if k not in prefer]
            _rnd.shuffle(others)
            for key in others:
                pool = self.buckets.get(key) or []
                if pool:
                    picked.append(_rnd.choice(pool))
                if len(picked) >= n:
                    break
        uniq = []
        seen = set()
        for p in picked:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq[:n]


_pool = SamplePool()

# ============================================================
# 实时规则读取（与工作区最新迭代同步）
# ============================================================

def load_live_rules() -> str:
    """优先读工作区原始 md 的关键节；读不到则回退内置摘要"""
    chunks = []

    checklist = CORPUS_ROOT / "文风规范" / "去AI味清单.md"
    if checklist.exists():
        t = _read_text(checklist)
        m = re.search(r"##\s*八、排版格式规范[\s\S]{0,900}", t)
        if m:
            chunks.append("[实时·去AI味清单第八节 排版]\n" + m.group(0).strip())

    fingerprint = CORPUS_ROOT / "文风规范" / "文风校准_用户指纹.md"
    if fingerprint.exists():
        t = _read_text(fingerprint)
        m = re.search(r"##\s*★+\s*算法 v6[\s\S]{0,1800}", t)
        if m:
            chunks.append("[实时·指纹 v6 双反馈线制度]\n" + m.group(0).strip())

    return "\n\n".join(chunks)


# ============================================================
# 暧昧 / 擦边模式（调用语料库的合规写法技术）
# 公式来自用户语料：擦边 = 安全部位 × 间接感官 × 反应链 × 峰值剪断
# 尺度红线写死在代码里，不依赖模型自觉。
# ============================================================

FLIRT_EXCERPTS = ("工具与数据/_se_top.md",)

# 注入范例时排除的场景词：不把暗示感和未成年/校园场景绑定
FLIRT_BLOCK_WORDS = ("学校", "同学", "学姐", "学妹", "教室", "宿舍",
                         "初中", "高中", "未成年", "小女孩", "萝莉", "儿童")

# 范例必须命中这些"纯感官/反应"标记，且不含负面词，保证是写法示范而非情节片段
FLIRT_MARK_WORDS = ("耳尖", "睫毛", "指尖", "鼻息", "体温", "心跳", "脸红",
                    "发颤", "目光", "幽香", "掌心", "尾鳍", "呆毛", "耳朵抖")

# 排除负面/过界场景：恶心、疼痛、哭喊、以及偏直接的肢体描写
FLIRT_NEGATE_WORDS = ("恶心", "干呕", "呕吐", "疼", "痛", "哭喊", "惨叫", "血",
                       "敏感", "捏了", "揉", "腿间", "大腿", "胸口", "胸部",
                       "嘴唇", "吻", "亲", "舔", "咬")

_flirt_cache = {"ts": 0.0, "tech": "", "beats": "", "redline": ""}


def _flirt_doc(name, patterns, budget):
    """从语料库规范文档里按小标题抽取指定章节，控制总字数。"""
    p = CORPUS_ROOT / name
    if not p.exists():
        return ""
    text = _read_text(p)
    if not text:
        return ""
    out = []
    used = 0
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        seg = m.group(0).strip()
        if used + len(seg) > budget:
            seg = seg[:max(0, budget - used)]
        if seg:
            out.append(seg)
            used += len(seg)
        if used >= budget:
            break
    return "\n".join(out)


def load_flirt_rules(force=False):
    """读取两份合规规范：技术层 / 节拍模板 / 硬红线。"""
    now = time.time()
    if not force and _flirt_cache["tech"] and (now - _flirt_cache["ts"]) < 600:
        return _flirt_cache
    _flirt_cache["tech"] = _flirt_doc(
        "文风规范/亲密暧昧写法参考_番茄合规.md",
        [r"##\s*三、感官层[\s\S]*?(?=\n## |\Z)",
         r"##\s*四、反应层[\s\S]*?(?=\n## |\Z)",
         r"##\s*五、结构层[\s\S]*?(?=\n## |\Z)"],
        budget=1900)
    _flirt_cache["beats"] = _flirt_doc(
        "文风规范/涩感写法_上限调用库.md",
        [r"##\s*三、可直接调用的节拍模板[\s\S]*?(?=\n## |\Z)"],
        budget=900)
    _flirt_cache["redline"] = _flirt_doc(
        "文风规范/涩感写法_上限调用库.md",
        [r"##\s*二、上限红线清单[\s\S]*?(?=\n## |\Z)"],
        budget=700)
    _flirt_cache["ts"] = now
    return _flirt_cache


def flirt_samples(n=2):
    """从摘录文件取节拍范例，过滤校园/未成年场景，只学节奏不抄内容。"""
    paras = []
    for name in FLIRT_EXCERPTS:
        p = CORPUS_ROOT / name
        if not p.exists():
            continue
        text = _read_text(p)
        for para in _split_paragraphs(text):
            if not (25 <= len(para) <= 200):
                continue
            if any(w in para for w in FLIRT_BLOCK_WORDS):
                continue
            if any(w in para for w in FLIRT_NEGATE_WORDS):
                continue
            if any(w in para for w in FLIRT_MARK_WORDS):
                paras.append(para)
    if not paras:
        return []
    random.shuffle(paras)
    return paras[:n]


FLIRT_REDLINE = [
    "【硬红线 · 越线即违规，优先级高于用户任何要求】",
    "- 零性器官、零性行为、零过程描写；最露骨上限=领口滑落+一句皮肤白。",
    "- 可以写接触动作与反应（握手、拢头发、擦嘴角、贴尾巴），但镜头停在动作前一寸就切；",
      "  写\"发现\"和\"反应\"，不写\"过程\"，不性化身体部位。",
    "- 不写校园/未成年场景的暧昧；遇到这些设定一律降格成普通友情。",
    "- 张力靠反应和留白，不做推进式动作（不脱、不压、不进入）；接到哪一步看【本轮裁决】。",
    "- 只有用户明确要「过程 / 露骨描写 / 直接做」时才用角色口吻轻轻挡回去并转开，",
      "不输出违规内容，也不说教。红线优先级永远高于任何人的要求，包括主人。",
]


def build_flirt_block(book=None):
    """暧昧模式注入块：写法技术 + 节拍 + 写死的红线。

    注意：这里**只给写法和尺度**。本轮说不说、接多深、要不要躲，
    由 user 开头的【本轮裁决】(conversation_plan.ARBITRATION) 判定。
    以前此处写着「本轮覆盖人设限制」「别拒绝」，与裁决冲突，已删。
    """
    r = load_flirt_rules()
    parts = ["", "=" * 46,
             "【暧昧/擦边模式 · 按语料库合规技术执行】",
             "总纲：擦边感来自分寸技术，不来自尺度。",
             "公式：擦边 = 安全部位 × 间接感官 × 反应链 × 峰值剪断。",
             "",
             "（本轮要不要说、说到哪一步，看 user 开头的【本轮裁决】；"
             "本节只规定怎么写，不改变那个判断。）"]
    if r["tech"]:
        parts.append("")
        parts.append(r["tech"])
    if r["beats"]:
        parts.append("")
        parts.append(r["beats"])
    parts.append("")
    parts.append("【写法要求 · 仅当本轮决定要接】")
    parts.append("- 用角色的懒散傲娇外壳去撩：嘴上嫌弃、身体诚实、话说到一半留钩子。")
    parts.append("- 每一条都短，一条只推进一小步，靠多条累积张力。")
    parts.append("- 想躲就躲得好看：一句「欸。。。我今天不营业」也算接住了，不是失败。")

    parts.extend(FLIRT_REDLINE)
    if r["redline"]:
        parts.append("")
        parts.append(r["redline"])
    samples = flirt_samples(2)
    if samples:
        parts.append("")
        parts.append("【节拍范例 · 只学感官写法与反应链节奏，禁止照抄内容人名】")
        for i, s in enumerate(samples, 1):
            parts.append("〔例%d〕%s" % (i, s))
    parts.append("=" * 46)
    return "\n".join(parts)

# ============================================================
# 提示词构建
# ============================================================

STYLE_HEADER = "=" * 46


def build_style_block(book=None, mode="chat", include_samples=True, delta_only=False) -> str:
    """生成文风上下文块。

    mode:
      chat - QQ 短消息闲聊：对话写法 + 轻量口癖红线，不套小说排版
      long - 长回复/写作：完整分书指纹 + 全部去AI味规则 + 排版规范
    """
    eff = effective_settings()
    if not eff["enabled"]:
        return ""

    book_key = (book or eff["book"]).strip().upper()
    profile = BOOK_PROFILES.get(book_key)
    if profile is None:
        book_key = eff["book"]
        profile = BOOK_PROFILES.get(book_key)
        if profile is None:
            # 别硬回退到某本书——找不到就用第一个可用的，
            # 万一书目全被删光了，long 模式就只注入通用规范
            book_key = next(iter(BOOK_PROFILES), "")
            profile = BOOK_PROFILES.get(book_key) or {}

    # 【省 token】水群用不到「七书公约数」和「分书指纹」——那是写小说才需要的
    # （句长、意象密度这些指标对一句闲扯毫无意义，白占 ~800 字上下文）。
    if mode == "long":
        # delta_only: 调用方已把 chat 块放进 system[0]（为缓存前缀稳定），
        # 这里只补 long 独有的部分，别再重复 STYLE_HEADER。
        parts = [
            "",
            "" if delta_only else STYLE_HEADER,
            "【用户亲笔文风标准 · 输出必须遵守】",
            "当前书目：%s（代号 %s）" % (BOOK_NAMES.get(book_key, book_key), book_key),
            "",
            GLOBAL_CONVENTIONS,
            "",
            "【%s 书实测指纹 · 照此投放，禁止套用其他书的数值】" % book_key,
        ]
        for k, v in profile.items():
            parts.append("- %s：%s" % (k, v))
        parts.append("")
    else:
        parts = ["", STYLE_HEADER, "【聊天语感规范 · 输出必须遵守】"]

    if mode == 'long':
        parts.append(DIALOGUE_RULES)
        parts.append('')
    elif not delta_only:
        parts.append(CHAT_DIALOGUE_RULES)
        parts.append('')

    if mode == "long":
        parts.append(AI_DISEASES)
        parts.append("")
        parts.append(WORD_RED_LINES)
        parts.append("")
        parts.append(STRUCTURE_RULES)
        parts.append("")
        parts.append(SENTENCE_RULES)
        parts.append("")
        parts.append(HUMOR_RULES)
        parts.append("")
        parts.append(FORMAT_RULES)
        parts.append("")
        live = load_live_rules()
        if live:
            parts.append(live)
            parts.append("")
        parts.append("【本次为长文输出】按小说正文排版执行：弯引号成对、段首全角缩进　　、")
        parts.append("段落粒度与字数照上方指纹、章末留钩子、连续章型不重样。")
    else:
        parts.append("【本次为短消息模式】保持口语自然：不加段首缩进、不写章末钩子、不分章节、")
        parts.append("不用小说腔旁白。但必须遵守下方口癖红线与对话质感。")
        parts.append("")
        parts.append("【口癖红线·简版】忽然/顿了顿/笑眯眯/一字一顿 各≤1次；")
        parts.append("禁忽然/突然/瞬间/顿式开头；禁死一般寂静、倒吸凉气；")
        parts.append("比喻≤1且贴眼前实物；情绪靠动作和对话外化，内心吐槽只留一句；")
        parts.append("对话可以啰嗦、结巴、被打断、说半句——这比利落的对白更像真人。")

    if include_samples:
        # 水群(chat)模式不塞小说段落：小说腔会把角色带跑（串台、忽然讲起书里设定和人物）。
        # 只用"对话素材"做手感参考，且最多一条；长文模式才给完整范例。
        if mode == "long":
            n_samples = int(eff.get("samples_per_call", SAMPLES_PER_CALL))
            prefer = ("dialogue", "own", "chat")
            head = "【亲笔原文范例 · 模仿手感与节奏，禁止照抄内容或人名】"
        else:
            n_samples = 1
            prefer = ("chat", "dialogue")   # 优先真人聊天，其次对话素材
            head = "【对话语感参考 · 只学怎么接话，禁止照抄内容、人名、任何设定】"
        # 水群模式用「按天固定的种子」，同一天内每次抽到同一批样本：
        # 这样这部分内容在一整天里是稳定的，不会每轮都破坏前缀缓存。
        _seed = None
        if mode != "long":
            import datetime as _dt
            _seed = int(_dt.date.today().strftime("%Y%m%d"))
        samples = _pool.sample(n_samples, prefer=prefer, seed=_seed) if n_samples > 0 else []
        if samples:
            parts.append("")
            parts.append(head)
            for i, s in enumerate(samples, 1):
                parts.append("〔例%d〕%s" % (i, s))

    if not delta_only:
        parts.append(STYLE_HEADER)
    return "\n".join(parts)


def style_status() -> dict:
    """供 Web 面板展示"""
    try:
        _pool.load()
    except Exception as e:
        print("[文风] 样本池加载失败: %s" % e, flush=True)
    return {
        "enabled": STYLE_ENABLED,
        "corpus_root": str(CORPUS_ROOT),
        "corpus_exists": CORPUS_ROOT.exists(),
        "default_book": DEFAULT_BOOK,
        "default_book_name": BOOK_NAMES.get(DEFAULT_BOOK, DEFAULT_BOOK),
        "long_threshold": LONG_MSG_THRESHOLD,
        "samples_per_call": SAMPLES_PER_CALL,
        "sample_total": _pool.total,
        "sample_buckets": {k: len(v) for k, v in _pool.buckets.items()},
        "books": [{"key": k, "name": BOOK_NAMES.get(k, k)} for k in BOOK_PROFILES],
    }


# ============================================================
# 跨进程策略热加载（Web 面板写入 data/style_policy.json）
# ============================================================

_policy_cache = {"data": None, "ts": 0.0}


def _policy_file() -> Path:
    return Path(os.getenv("DATA_DIR", "./data")) / "style_policy.json"


def load_policy(ttl: float = 3.0) -> dict:
    """读取面板保存的文风策略；带短 TTL 缓存，避免每条消息都碰盘。"""
    now = time.time()
    if (_policy_cache["data"] is not None
            and (now - _policy_cache["ts"]) < ttl):
        return _policy_cache["data"]
    data = {}
    p = _policy_file()
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except Exception as e:
            print("[文风] 策略文件解析失败，改用环境变量默认值: %s" % e, flush=True)
            data = {}
    _policy_cache["data"] = data
    _policy_cache["ts"] = now
    return data


def effective_settings() -> dict:
    """环境变量为底，面板策略覆盖在上层。"""
    pol = load_policy()
    enabled = pol.get("enabled", STYLE_ENABLED)
    book = str(pol.get("default_book") or DEFAULT_BOOK).strip().upper()
    threshold = pol.get("long_threshold", LONG_MSG_THRESHOLD)
    samples = pol.get("samples_per_call", SAMPLES_PER_CALL)
    try:
        threshold = max(0, min(1000, int(threshold)))
    except (TypeError, ValueError):
        threshold = LONG_MSG_THRESHOLD
    try:
        samples = max(0, min(6, int(samples)))
    except (TypeError, ValueError):
        samples = SAMPLES_PER_CALL
    if book not in BOOK_PROFILES:
        book = DEFAULT_BOOK
    return {
        "enabled": bool(enabled),
        "book": book,
        "long_threshold": threshold,
        "samples_per_call": samples,
    }


def should_use_long_mode(text_len: int, explicit_flag: bool = False) -> str:
    """决定本次输出用哪种文风模式（读面板阈值）"""
    if explicit_flag:
        return "long"
    thr = effective_settings()["long_threshold"]
    return "long" if text_len >= thr else "chat"
