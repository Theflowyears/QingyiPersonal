"""图像生成模块：调用 qwen-image-3.0 画图，落本地后交 NapCat 发送。

设计要点：
- 生成耗时较长（几十秒），调用方应先回一句"在画了"，再后台生成。
- 图片落盘到 data/generated/，用本地绝对路径发 QQ 最稳（URL 会过期）。
- 失败/违规由上层用角色口吻转述，本模块只返回结构化结果。
"""
import asyncio
import base64
import hashlib
import os
import re
import time
from datetime import datetime

import httpx

IMAGE_GEN_ENABLED = os.getenv("IMAGE_GEN_ENABLED", "1").strip() == "1"
IMAGE_GEN_URL = os.getenv(
    "IMAGE_GEN_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "multimodal-generation/generation",
).strip()
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL", "z-image-turbo").strip()
IMAGE_GEN_KEY = (
    os.getenv("IMAGE_GEN_API_KEY", "").strip()
    or os.getenv("LLM_TEXT_API_KEY", "").strip()
)
IMAGE_GEN_TIMEOUT = float(os.getenv("IMAGE_GEN_TIMEOUT", "180"))
# prompt_extend 会让服务端多跑一次扩写，实测吃掉 42.6s（59.3s -> 16.7s）。
# 我们自己已经把详细外观描述写进提示词了，不需要服务端再扩。
IMAGE_GEN_EXTEND = os.getenv("IMAGE_GEN_EXTEND", "0").strip() == "1"
# 尺寸：实测 1024x1024=5.1s，1328x1328=7.6s，原默认(2528x1696)=59.3s
IMAGE_GEN_SIZE = (os.getenv("IMAGE_GEN_SIZE", "1120*1440") or "").strip()

# ── 首选通道：灵珑AI（linlongs）gpt-image-2.5-flare ──
# 出图质量最好，但慢（十几秒到一分钟）。失败自动降级到智能体，再降级到 z-image-turbo。
FLARE_ENABLED = os.getenv("FLARE_ENABLED", "1").strip() == "1"
FLARE_URL = os.getenv("FLARE_URL", "https://linlongs.com/v1/images/generations").strip()
FLARE_MODEL = os.getenv("FLARE_MODEL", "gpt-image-2.5-flare").strip()
FLARE_KEY = (
    os.getenv("FLARE_API_KEY", "").strip()
    or os.getenv("LINLONGS_API_KEY", "").strip()
)
FLARE_SIZE = (os.getenv("FLARE_SIZE", "1024x1024") or "").strip()
# 实测正常出图 90~140 秒，150 太贴近上限：一旦超时会被判失败，
# 然后降级到别的通道再画一张，既多花钱又多等一倍时间。
FLARE_TIMEOUT = float(os.getenv("FLARE_TIMEOUT", "240"))
# 已放弃的 flare 任务：超时降级后，这些任务再回来也不要
_ABANDONED = set()   # 实测 29-88s，150 够；再久就是服务端卡住
FLARE_QUALITY = (os.getenv("FLARE_QUALITY", "") or "").strip()
# 异步接口：提交拿 task_id 再轮询，避免同步长连接被网关 524 掐断
FLARE_BASE = os.getenv("FLARE_BASE", "https://linlongs.com/v1").strip().rstrip("/")
# 分辨率锁死 1K：超过就是高价档（Medium ¥0.15 / High ¥0.2），预算上限 ¥0.15
FLARE_RESOLUTION = (os.getenv("FLARE_RESOLUTION", "1024x1024") or "1024x1024").strip()


# ── 提示词健全化 ─────────────────────────────────────
# 她的画风本来就偏唯美，但用户偶尔会提比较露骨的要求，
# 直接丢给图像模型会被拒（返回违规），她就没词了。
# 这里在**发出前**把画面往「得体、唯美、可公开」的方向改写：
#   · 露骨词换成同样氛围但穿着得体的说法
#   · 末尾补一句正向的画面约束
# 注意这是"把画面变健康"，不是想办法绕过审核——
# 真要出格的图，她会用角色口吻婉拒，而不是硬凑。

# 逐条替换：左边是想避开的说法，右边是同氛围但穿好衣服的说法。
# 先长后短地匹配（「裸露」优先于「裸」），避免把词拆碎。
_TIDY = {
    "全裸": "披着丝质长衫", "赤裸": "披着薄纱", "一丝不挂": "裹着长巾",
    "裸体": "披着长衫", "光着身子": "披着外袍", "半裸": "衣领微敞",
    "比基尼": "绣花泳装外罩薄衫", "三点式": "运动背心",
    "内衣": "宽松长裙", "内裤": "素色短裙", "胸罩": "束腰外衣",
    "爆乳": "身姿匀称", "巨乳": "身姿匀称", "丰乳": "身姿匀称",
    "乳沟": "衣领", "胸部": "肩颈", "乳房": "肩颈", "露胸": "衣襟合拢",
    "翘臀": "背影", "屁股": "背影", "臀部": "背影",
    "大腿内侧": "裙摆", "腿根": "裙摆", "露大腿": "裙摆及膝",
    "裙子很短": "及膝短裙", "胸前特写": "半身特写", "胸前": "身前",
    "大腿": "裙摆", "裙底": "裙摆", "走光": "衣摆端正",
    "露点": "衣冠整齐", "露背": "削肩长裙", "露肩": "削肩长裙",
    "湿身": "水汽氤氲", "泡澡": "浴汤的热气", "沐浴": "水雾缭绕",
    "床戏": "并肩坐着", "做爱": "依偎", "性交": "依偎", "插入": "靠近",
    "高潮": "相视一笑", "呻吟声": "轻叹", "呻吟": "轻叹", "娇喘": "轻喘",
    "淫": "", "色情": "唯美", "情色": "唯美", "涩图": "插画",
    "黄图": "插画", "色气": "温婉", "色气向": "唯美向",
    "妩媚": "温婉", "媚": "温婉", "骚": "俏皮", "挑逗": "打趣",
    "诱惑": "俏丽", "撩人": "动人",
    "捆绑play": "系着丝带", "捆绑": "系着丝带", "调教": "说教",
    "鞭打": "训话", "SM": "", "露骨": "直白",
    "舔": "凑近", "吮": "凑近", "咬唇": "抿嘴",
    "床上": "窗边的软榻", "床单": "软垫", "被窝": "毯子",
    "R18": "唯美", "r18": "唯美", "18禁": "唯美",
    "NSFW": "唯美", "nsfw": "唯美", "绝对领域": "过膝长袜",
}

# 画面正向约束：把「别画什么」写成「要画什么」，模型对正向描述更听话
_SAFE_SUFFIX = ("\n\n画面要求：健康向的唯美插画，衣着得体，构图优雅，"
                "光线柔和，神情自然；人物穿戴整齐，不出现亲密接触镜头。")

# 改写后仍要彻底抹掉的高风险字眼（只在重试版本里用）
_RISKY = re.compile(r"裸|胸|乳|臀|屁股|大腿|腿根|裙底|走光|露点|内衣|胸罩|"
                    r"呻吟|娇喘|淫|色情|情色|涩|色气|骚|媚|舔|吮|捆绑|调教|"
                    r"R18|18禁|NSFW|露骨|床戏")
_TIDY_KEYS = sorted(_TIDY.keys(), key=len, reverse=True)


def tidy_prompt(text):
    """把可能被图像模型判定违规的描述，改写成得体版本。

    返回 (改写后的文本, 是否改过)。没改过就原样返回，正常需求一个字不动。
    """
    t = str(text or "")
    if not t:
        return t, False
    out = t
    hit = False
    for k in _TIDY_KEYS:
        if k and k in out:
            out = out.replace(k, _TIDY[k])
            hit = True
    if not hit:
        return t, False
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out + _SAFE_SUFFIX, True


def soften_prompt(text):
    """被拒之后用的「更稳妥」版本。

    比 tidy_prompt 更狠：风险词直接抹掉，只留人物和场景，
    并强制成半身像——保证这次能画出来。
    """
    t = str(text or "")
    # 先做普通替换（不带后缀），再抹掉残留
    for k in _TIDY_KEYS:
        if k and k in t:
            t = t.replace(k, _TIDY[k])
    t = _RISKY.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"[，,、]{2,}", "，", t).strip("，,、 ")
    if len(t) < 2:
        t = "少女的优雅肖像"
    return ("半身像，构图端正，衣着得体。画面内容：" + t[:200] +
            "\n\n画面要求：唯美插画风格，人物穿戴整齐，光线柔和，"
            "神情自然，姿态从容。")


def looks_refused(err):
    """模型是不是"拒答"而不是"技术故障"。

    拒答要换个说法安抚她，技术故障才该说"画砸了重试"。
    """
    e = str(err or "")
    keys = ("违规", "敏感", "审核", "不安全", "policy", "violation",
            "blocked", "risk", "content_filter", "safety", "prohibited",
            "not allowed", "unsupported content", "rejected")
    low = e.lower()
    return any(k.lower() in low for k in keys)


def _flare_cfg():
    """面板里保存的 flare 覆盖配置（改完即时生效）。"""
    import json
    try:
        path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return ((json.load(f) or {}).get("flare") or {})
    except Exception:
        pass
    return {}


def flare_ready():
    c = _flare_cfg()
    return bool(FLARE_ENABLED and (c.get("api_key") or FLARE_KEY))


def flare_model():
    return str(_flare_cfg().get("model") or FLARE_MODEL).strip()


def flare_url():
    return str(_flare_cfg().get("base_url") or FLARE_URL).strip()


def flare_base():
    return str(_flare_cfg().get("base") or FLARE_BASE).strip().rstrip("/")


def flare_key():
    return str(_flare_cfg().get("api_key") or FLARE_KEY).strip()
IMAGE_GEN_MAX_BYTES = int(os.getenv("IMAGE_GEN_MAX_BYTES", str(12 * 1024 * 1024)))

_DEFAULT_DATA = os.getenv("DATA_DIR", "./data")
IMAGE_GEN_DIR = os.getenv(
    "IMAGE_GEN_DIR", os.path.join(_DEFAULT_DATA, "generated")
)


def _ensure_dir():
    try:
        os.makedirs(IMAGE_GEN_DIR, exist_ok=True)
    except Exception:
        pass


def _guess_ext(url, ctype=""):
    ct = (ctype or "").lower()
    if "image/png" in ct:
        return ".png"
    if "image/jpeg" in ct or "image/jpg" in ct:
        return ".jpg"
    if "image/webp" in ct:
        return ".webp"
    low = (url or "").lower().split("?")[0]
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if low.endswith(ext):
            return ext
    return ".png"


async def _download(url):
    """下载生成图，返回 (本地绝对路径, 字节数) 或 (None, 0)。"""
    _ensure_dir()
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code >= 400:
            print("[绘图] 下载失败 HTTP %s" % r.status_code, flush=True)
            return None, 0
        raw = r.content
        if not raw or len(raw) > IMAGE_GEN_MAX_BYTES:
            print("[绘图] 图片为空或过大 size=%d" % len(raw), flush=True)
            return None, 0
        ext = _guess_ext(url, r.headers.get("content-type", ""))
        digest = hashlib.md5(raw).hexdigest()[:12]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(IMAGE_GEN_DIR, "img_%s_%s%s" % (stamp, digest, ext))
        with open(path, "wb") as f:
            f.write(raw)
        return os.path.abspath(path), len(raw)
    except Exception as e:
        print("[绘图] 下载异常 " + type(e).__name__ + ": " + str(e)[:100], flush=True)
        return None, 0


def _runtime_cfg():
    """后台面板保存的覆盖配置优先，其次环境变量。改完无需重启。"""
    import json
    cfg = {}
    try:
        path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cfg = (json.load(f) or {}).get("image") or {}
    except Exception:
        cfg = {}
    return cfg

# ---- 生成通道：builtin（直连模型）/ agent（云端智能体） ----
def _provider(role, default="builtin"):
    """读后台面板保存的通道设置，改完即时生效。"""
    import json
    try:
        path = os.path.join(os.getenv("DATA_DIR", "./data"), "models_config.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f) or {}
            p = str(((cfg.get("providers") or {}).get(role)) or "").strip().lower()
            if p in ("builtin", "agent"):
                return p
    except Exception:
        pass
    return str(os.getenv(role.upper() + "_PROVIDER", default)).strip().lower() or default


import re
import time


# ── 画风：默认不锁，交给模型自己选 ──
# 之前硬塞「二次元赛璐璐」效果不理想，反而压住了模型本身的水准。
# 现在只有用户明确点名风格时才注入参考，其余情况让它自由发挥。
_STYLE_PROFILE_FILE = os.path.join(
    os.getenv("DATA_DIR", "./data"), "memory", "art_style.json")

# 明确要求「某人的画风」「按那张图」才注入
_STYLE_REQUEST = re.compile(
    r"的画风|那种风格|那种画风|按那[张个]|照着那[张个]|参考那[张个]"
    r"|同上风格|上次那样|之前的画风|像你上次画"
)


def _load_style_profile():
    """读用户确认过的画风档案（由真实画作提取而来）。"""
    try:
        if os.path.isfile(_STYLE_PROFILE_FILE):
            import json as _json
            with open(_STYLE_PROFILE_FILE, encoding="utf-8") as f:
                prof = (_json.load(f) or {})
            desc = str(prof.get("desc") or "").strip()
            if len(desc) >= 15:
                return desc
    except Exception:
        pass
    return ""


def style_prefix(text):
    """画风前缀：默认空，让模型自行选择。

    只在用户明确说「用那种画风」时才注入已保存的风格档案。
    """
    t = str(text or "")
    if not _STYLE_REQUEST.search(t):
        return ""
    desc = _load_style_profile()
    if not desc:
        return ""
    return "画风要求（照这个风格来）：" + desc + "\n"


async def build_final_prompt(text):
    """在原始描述前面补上「画风」和「角色特征」，避免画错人、画风乱飘。"""
    raw = str(text or "").strip()
    parts = [style_prefix(raw)]
    meta = {"style": "参考档案" if style_prefix(raw) else "模型自选"}

    # 提到具体角色时，先去抓参考图提取特征
    try:
        import character_ref
        if character_ref.extract_characters(raw):
            t0 = time.time()
            block, cm = await character_ref.build_character_block(raw)
            meta["chars"] = cm
            meta["char_seconds"] = round(time.time() - t0, 1)
            if block:
                parts.append(block + "\n")
                print("[绘图] 角色参考已注入 %s" % (cm, ), flush=True)
    except Exception as e:
        print("[绘图] 角色参考不可用 " + type(e).__name__ + ": " + str(e)[:70], flush=True)

    parts.append("画面内容：" + raw)
    return "\n".join(p for p in parts if p), meta


async def _flare_async(prompt, model, key, base):
    """异步出图：提交任务 -> 轮询。返回 (url, seconds) 或 (None, err)。"""
    import asyncio as _aio
    _h = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    _body = {
        "model": model,
        "input": {
            "prompt": prompt[:1800],
            "size": FLARE_RESOLUTION,     # 锁 1K，省钱
            "num_images": 1,
            "output_format": "png",
        },
    }
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.post(base + "/predictions", headers=_h, json=_body)
    except Exception as e:
        return None, "提交异常 " + type(e).__name__
    if r.status_code >= 400:
        return None, "提交 HTTP %s %s" % (r.status_code, (r.text or "")[:120])
    try:
        tid = ((r.json().get("data") or {})).get("task_id")
    except Exception:
        tid = None
    if not tid:
        return None, "没拿到 task_id"
    print("[绘图] flare 已提交 task=%s，开始轮询" % tid[:12], flush=True)

    _deadline = t0 + FLARE_TIMEOUT
    # 轮询复用同一个连接：300 秒里每 3 秒新建一次客户端，光握手就白等不少时间
    _rc = httpx.AsyncClient(timeout=30, follow_redirects=True)
    # 自适应间隔：出图通常 30~140 秒，固定等 3 秒会在快完成时白等。
    # 前 20 秒 1 秒一次，20~60 秒 2 秒一次，之后 3 秒。
    _n = 0
    try:
        while time.time() < _deadline:
            _elapsed = time.time() - t0
            _wait = 1.0 if _elapsed < 20 else (2.0 if _elapsed < 60 else 3.0)
            await _aio.sleep(_wait)
            _n += 1
            try:
                rr = await _rc.get(base + "/predictions/" + tid, headers=_h)
                d = (rr.json() or {}).get("data") or {}
            except Exception:
                continue
            st = str(d.get("task_status") or "").lower()
            # 每 15 次报一次进度，事后能分辨「生成慢」还是「发送慢」
            if _n % 15 == 0:
                print("[绘图] flare 轮询中 %.0fs status=%s" % (time.time() - t0, st or "?"), flush=True)
            if st in ("success", "succeeded", "completed"):
                for it in (d.get("output") or []):
                    u = it.get("image") or it.get("url")
                    if u:
                        return u, round(time.time() - t0, 1)
                return None, "成功但没有图片"
            if st in ("failed", "fail", "error"):
                return None, "任务失败 " + str(d.get("task_status"))[:40]
    finally:
        try:
            await _rc.aclose()
        except Exception:
            pass
    # 超时不再直接标弃——图可能只是排队慢。转交「迟到回收器」继续等，
    # 出图后照样发（主人明确要求：降级了也不能把先出的图扔掉）。
    return None, "轮询超时 %.0fs|tid=%s" % (time.time() - t0, tid)


# ── 迟到回收器 ────────────────────────────────────────
# flare 轮询超时后任务其实还在服务端跑。以前直接标弃，结果图生成好了
# 也没人取——用户看到后台有图、群里却收不到。现在超时任务交给这里继续轮询，
# 出图后通过回调把图补发给对应的聊天。
_LATE_TASKS = {}          # tid -> {"prompt","callback","ts"}
_LATE_KEEP = float(os.getenv("FLARE_LATE_KEEP", "600"))   # 最多再等 10 分钟
_late_runner = None


async def _late_recycler():
    """后台低频轮询所有未完成的 flare 任务。"""
    import asyncio as _aio
    while True:
        try:
            now = time.time()
            for tid in list(_LATE_TASKS):
                job = _LATE_TASKS.get(tid)
                if not job or now - float(job.get("ts") or 0) > _LATE_KEEP:
                    _LATE_TASKS.pop(tid, None)
                    continue
                try:
                    base, key = job["base"], job["key"]
                    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                        rr = await c.get(base + "/predictions/" + tid,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + key})
                    d = rr.json()
                    st = str(((d.get("data") or {}).get("task_status") or "")).lower()
                    if st in ("success", "succeeded", "completed"):
                        url = ""
                        for it in ((d.get("data") or {}).get("output") or []):
                            url = it.get("image") or it.get("url") or ""
                            if url:
                                break
                        _LATE_TASKS.pop(tid, None)
                        if url:
                            path, size = await _download(url)
                            if path:
                                print("[绘图] 迟到回收成功 task=%s (%ds)" % (
                                    tid[:8], int(time.time() - job["ts"])), flush=True)
                                cb = job.get("callback")
                                if cb:
                                    try:
                                        await cb(path, url)
                                    except Exception as ce:
                                        print("[绘图] 迟到回调失败 " + type(ce).__name__, flush=True)
                    elif st in ("failed", "fail", "error"):
                        _LATE_TASKS.pop(tid, None)
                except Exception:
                    pass   # 网络抖一下不算失败，下轮再试
            await _aio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[绘图] 回收器异常 " + type(e).__name__, flush=True)
            await _aio.sleep(10)


def adopt_late_task(tid, prompt, base, key, callback):
    """登记一个超时未完成的 flare 任务，出图后回调 callback(path, url)。"""
    global _late_runner
    if not tid:
        return
    _LATE_TASKS[str(tid)] = {"prompt": prompt, "base": base, "key": key,
                             "callback": callback, "ts": time.time()}
    import asyncio as _aio
    try:
        loop = _aio.get_running_loop()
        if _late_runner is None or _late_runner.done():
            _late_runner = loop.create_task(_late_recycler())
    except RuntimeError:
        pass


# ── 兜底：qwen-image（阿里云直连，不走中转）─────────────
# 端点必须是 multimodal-generation/generation，而且要**同步**调用：
#   这个 key 会被 dashscope 拒绝 async（403 AccessDenied）。
# 模型名是 qwen-image-3.0——叫 qwen-image-3 的那个在服务端根本不存在。
QWENIMG_ENABLED = int(os.getenv("QWENIMG_ENABLED", "1") or 0)
QWENIMG_BASE = os.getenv(
    "QWENIMG_BASE_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
).strip()
QWENIMG_MODEL = os.getenv("QWENIMG_MODEL", "qwen-image-3.0").strip()
QWENIMG_KEY = os.getenv("QWENIMG_API_KEY", "").strip()
QWENIMG_TIMEOUT = float(os.getenv("QWENIMG_TIMEOUT", "200"))


def qwenimg_ready():
    return bool(QWENIMG_ENABLED and QWENIMG_KEY and QWENIMG_MODEL and QWENIMG_BASE)


async def _qwenimg_sync(prompt, model, key, base):
    """同步出图。返回 (url, seconds) 或 (None, err)。

    实测 78 秒出图，比 flare 慢但比走 Agent 稳，作为二级兜底。
    """
    _h = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    _body = {"model": model, "input": {"messages": [
        {"role": "user", "content": [{"text": str(prompt)[:1800]}]}]}}
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=QWENIMG_TIMEOUT,
                                     follow_redirects=True) as c:
            r = await c.post(base, headers=_h, json=_body)
    except Exception as e:
        return None, "请求异常 " + type(e).__name__
    if r.status_code >= 400:
        return None, "HTTP %s %s" % (r.status_code, (r.text or "")[:100])
    try:
        j = r.json() or {}
    except Exception:
        return None, "返回不是 JSON"
    # 图片在 output.choices[0].message.content[].image
    for ch in ((j.get("output") or {}).get("choices") or []):
        for it in ((ch.get("message") or {}).get("content") or []):
            u = it.get("image") or it.get("url")
            if u:
                return u, round(time.time() - t0, 1)
    return None, "成功但没有图片"


async def generate(prompt, late_cb=None, *, session="", visibility="private", run_id=None,
                   evidence_origin=None):
    """生成/下载 I/O 收口：保留拒绝分类，内部供应商回退不重复记同一执行。"""
    from toolkit import _record_tool_execution
    res = {"ok": False, "kind": "network", "error": "绘图未完成"}
    reason = ""
    try:
        res = await _generate_impl(prompt, late_cb=late_cb)
        if isinstance(res, dict) and not res.get("ok") and looks_refused(res.get("error")):
            res["kind"] = "refused"
    except Exception as exc:
        reason = "exception:" + type(exc).__name__
        res = {"ok": False, "kind": "network", "error": type(exc).__name__}
    finally:
        _record_tool_execution("image_gen", prompt, res, evidence="artifact", session=session,
                               visibility=visibility, run_id=run_id,
                               evidence_origin=evidence_origin, reason=reason)
    return res


async def _generate_impl(prompt, late_cb=None):
    """生成一张图。

    返回 dict：
      成功: {ok: True, url, path, bytes, seconds}
      失败: {ok: False, error, kind}
        kind ∈ config / network / api / download / disabled
    """
    prompt = (prompt or "").strip()
    if not IMAGE_GEN_ENABLED:
        return {"ok": False, "kind": "disabled", "error": "绘图功能未开启"}
    if not prompt:
        return {"ok": False, "kind": "config", "error": "没有描述"}

    # 先做一遍健全化：露骨的描述改成得体说法，免得被图像模型直接拒掉
    prompt, _tidy_hit = tidy_prompt(prompt)
    if _tidy_hit:
        print("[绘图] 提示词已健全化，改写后 %d 字" % len(prompt), flush=True)

    # 拼最终提示词：画风 + 角色特征 + 画面内容
    _final, _meta = await build_final_prompt(prompt)
    _meta["tidied"] = _tidy_hit

    _cfg = _runtime_cfg()
    _model = str(_cfg.get("model") or IMAGE_GEN_MODEL)
    _url = str(_cfg.get("base_url") or IMAGE_GEN_URL)
    _key = str(_cfg.get("api_key") or IMAGE_GEN_KEY)

    # ── 通道优先级：flare → 智能体 → z-image-turbo ──
    # 主人在面板里把「生成通道」设成 agent 时才优先走智能体；
    # 其余情况一律 flare 首选，挂了再往下退。

    # ① 首选：灵珑AI gpt-image-2.5-flare（异步轮询，避免 524）
    if flare_ready() and _provider("image") != "agent":
        _f_model = flare_model()
        _f_base = flare_base()
        _f_key = flare_key()
        _t_sub = time.time()
        _url_img, _info = await _flare_async(_final, _f_model, _f_key, _f_base)
        # 超时且带 tid → 交给回收器；图最终出来了会走 late_cb 补发
        if not _url_img and isinstance(_info, str) and "tid=" in _info and late_cb:
            try:
                _adopt_late_from_info(_info, _final, _f_base, _f_key, late_cb)
            except Exception as _ae:
                print("[绘图] 登记迟到回收失败 " + type(_ae).__name__, flush=True)
        _wait_s = round(time.time() - _t_sub, 1)
        if _url_img:
            _t_dl = time.time()
            _path, _size = await _download(_url_img)
            print("[绘图] 耗时拆分：等待出图 %.1fs + 下载 %.1fs = 本模块 %.1fs"
                  % (_wait_s, time.time() - _t_dl, time.time() - _t_sub), flush=True)
            if _path:
                print("[绘图] flare 完成 %s (%.2f MB, %ss)"
                      % (_path, _size / 1024 / 1024, _info), flush=True)
                return {"ok": True, "url": _url_img, "path": _path,
                        "bytes": _size, "seconds": float(_info or 0),
                        "provider": "flare", "meta": _meta,
                        "final_prompt": _final}
        # 只有真正失败才降级；524 那种「上游还在画」不会再触发第二张图
        print("[绘图] flare 未成：%s，降级" % str(_info)[:80], flush=True)

    # ①b 兜底：qwen-image（阿里云直连，同步接口）
    if qwenimg_ready() and _provider("image") != "agent":
        _u2, _i2 = await _qwenimg_sync(_final, QWENIMG_MODEL, QWENIMG_KEY, QWENIMG_BASE)
        if _u2:
            _p2, _s2 = await _download(_u2)
            if _p2:
                print("[绘图] qwen-image 完成 %s (%.2f MB, %ss)"
                      % (_p2, _s2 / 1024 / 1024, _i2), flush=True)
                return {"ok": True, "url": _u2, "path": _p2,
                        "bytes": _s2, "seconds": float(_i2 or 0),
                        "provider": "qwen-image", "meta": _meta,
                        "final_prompt": _final}
        print("[绘图] qwen-image 未成：%s，继续降级" % str(_i2)[:80], flush=True)

    # ② 次选：云端智能体
    if _provider("image") == "agent" or FLARE_ENABLED:
        try:
            import qwen_agent
            _ar = await qwen_agent.run(
                "画一张图，只要图片不要解释：" + prompt[:400], timeout=170, title="draw")
            if _ar.get("ok") and _ar.get("images"):
                _path, _size = await qwen_agent.download(_ar["images"][0], IMAGE_GEN_DIR)
                if _path:
                    print("[绘图] 走 Agent 通道 %s (%d bytes, %.1fs)"
                          % (_path, _size, _ar.get("seconds") or 0), flush=True)
                    return {"ok": True, "url": _ar["images"][0], "path": _path,
                            "bytes": _size, "seconds": _ar.get("seconds") or 0,
                            "provider": "agent"}
            print("[绘图] Agent 无产出，回退直连: " + str(_ar.get("error"))[:60], flush=True)
        except Exception as _ae:
            print("[绘图] Agent 异常，回退直连 " + type(_ae).__name__, flush=True)

    if not _key:
        return {"ok": False, "kind": "config", "error": "绘图密钥未配置"}

    _params = {"prompt_extend": IMAGE_GEN_EXTEND}
    if IMAGE_GEN_SIZE:
        _params["size"] = IMAGE_GEN_SIZE

    payload = {
        "model": _model,
        "input": {
            "messages": [
                {"role": "user", "content": [{"text": _final}]}
            ]
        },
        "parameters": _params,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + _key,
    }

    t0 = time.time()
    print("[绘图] 开始 model=%s 画风=%s 提示词=%d 字" % (_model, _meta.get("style"), len(_final)), flush=True)
    try:
        async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT, follow_redirects=True) as c:
            r = await c.post(_url, headers=headers, json=payload)
    except Exception as e:
        print("[绘图] 请求异常 " + type(e).__name__ + ": " + str(e)[:120], flush=True)
        return {"ok": False, "kind": "network", "error": type(e).__name__}

    if r.status_code >= 400:
        body = (r.text or "")[:300]
        print("[绘图] HTTP %s %s" % (r.status_code, body), flush=True)
        return {"ok": False, "kind": "api", "error": "HTTP %s: %s" % (r.status_code, body)}

    url = ""
    try:
        data = r.json()
        choices = (data.get("output") or {}).get("choices") or []
        content = ((choices[0] or {}).get("message") or {}).get("content") or []
        for item in content:
            # 有的模型（如 z-image-turbo）不返 type 字段，只看有没有 image
            if isinstance(item, dict) and item.get("image"):
                url = item.get("image") or ""
                break
        if not url:
            msg = (data.get("output") or {}).get("message") or ""
            print("[绘图] 返回里没有图片 url=%s" % str(data)[:200], flush=True)
            return {"ok": False, "kind": "api", "error": msg or "没有返回图片"}
    except Exception as e:
        print("[绘图] 解析失败 " + str(e)[:100], flush=True)
        return {"ok": False, "kind": "api", "error": "解析失败"}

    path, size = await _download(url)
    seconds = round(time.time() - t0, 1)
    if not path:
        return {"ok": False, "kind": "download", "error": "图片下载失败", "url": url}

    print("[绘图] 完成 %s (%d bytes, %.1fs)" % (path, size, seconds), flush=True)
    return {"ok": True, "url": url, "path": path, "bytes": size, "seconds": seconds,
            "meta": _meta, "final_prompt": _final}


def build_prompt_from_text(text):
    """从用户口语里抠出画面描述。"""
    t = (text or "").strip()
    for pre in ("/画图", "/画画", "/绘图", "/生成图片", "/生图"):
        if t.lower().startswith(pre.lower()):
            t = t[len(pre):].lstrip(" :：,，")
            break
    for pre in ("帮我画", "给我画", "替我画", "麻烦画", "请画",
                "生成一张图", "生成一张", "生成图片", "来一张", "来张", "AI画"):
        if t.startswith(pre):
            t = t[len(pre):].lstrip(" :：,，")
            break
    # 通用：去掉句首「画」和紧跟着的量词（画一张 / 画只 / 画个 …）
    m = re.match(r"^画\s*(" + "张|个|幅|只|条|位|名|座|朵|棵|栋|匹|头|群|片|套|把" + r")?", t)
    if m and not t.startswith("画画"):
        t = t[m.end():].lstrip(" :：,，")
    # 去掉残留的单字量词（张阿米娅 -> 阿米娅，个图 -> 图）
    # 注意不剥「一只」「一张」这种，那是内容的一部分
    m2 = re.match(r"^(张|个|幅|只|条|位|名|座|朵|棵|栋|匹|头|群|片|套|把)\s*", t)
    if m2 and len(t) > m2.end():
        t = t[m2.end():]
    return t.strip()


# 祈使式绘图意图。必须带请求结构，避免把"我小时候爱画画"这种陈述误判。
# 量词：一只猫、一条狗、一位角色…不能只认张/个/幅
_QTY = "张|个|幅|只|条|位|名|座|朵|棵|栋|匹|头|群|片|套|把"

# 祈使式绘图意图。必须带请求结构，避免把「我小时候爱画画」这种陈述误判。
_DRAW_PATTERNS = [
    r"^/(画图|画画|绘图|生图|生成图片)\s*\S",
    r"(帮我|给我|替我|麻烦|请)\s*(画|生成)",
    r"画一(?:%s)(?:\S|$)" % _QTY,
    r"画(?:%s)(?:\S|$)" % _QTY,
    r"(来|生成|出)\s*一?(?:%s)?\S.*(图|画)" % _QTY,
    r"(来|生成)\s*(一(?:%s)?)?图\S" % _QTY,
    r"AI\s*画\S",
    r"(能不能|可以|能|想)\s*(帮我)?画一?(?:%s)?" % _QTY,
    # 以「画」开头且后面跟着具体内容的。
    # 排除两类：
    #   ① 名词：画风/画笔/画面/画质/画家/画展/画册/画廊/画室/画法
    #   ② 动词补语：「画出来的」「画崩了」「画得」「画完了」「画错了」——
    #      这些是在评价某张画，不是在要她画。踩过的坑：
    #      群里聊画风时有人说「画出来的」，她被当成要图，直接开始画。
    r"^画(?!风|笔|面|质|家|展|册|廊|画|室|法|出|完|得|了|崩|错|的|成|像|好|坏|丑|美|大|小|很|挺)[^\s]{2,}",
    # 「画一下X」「画下X」
    r"画(一下|下)\S",
    # 「再画一张」「再画个」—— 结尾没有宾语也算指令
    r"再画[一二三几]?(?:%s)?" % _QTY,
]


# 「画饼」「画大饼」是俗语，不是在要图。
# 但「给我画个大饼」是真要——所以只在没有明确祈使词时才拦。
_IDIOM = re.compile(
    r"画(个|一?张|一?幅)?大?饼"        # 画个饼 / 画大饼 / 画一张饼
    r"|画饼充饥"
    r"|画地为牢"
)
# 叫别人别画，也是在说空话
_NEG_DRAW = re.compile(r"(别|不要|甭|少|不用|不必|没必要)\s*(给我|跟我|替我|在这)?\s*(画)")
_IMPERATIVE = re.compile(r"(帮我|给我|替我|替他|替她|麻烦|请|来一?张|出一?张)\s*(画|生成|绘)")


# 「生成了两张图」「生成过」「画完了」——这些是在说已经发生的事，不是要她画。
_DONE_DRAW = re.compile(
    r"(生成|画|出图|出)(了|过|完|成|好|错|崩|得)"
    r"|(已经|刚|刚才|刚刚|之前|上次|一次性|一口气)(生成|画|出)的?"
)


def _is_idiom(text):
    """这句话里的「画」是不是俗语，不是在要求作画。"""
    t = str(text or "")
    # 「别画了」「不用画」——这是叫停，不是点单
    if _NEG_DRAW.search(t):
        return True
    if not _IDIOM.search(t):
        return False
    if _IMPERATIVE.search(t):
        return False        # 「给我画个大饼」是真要画，不当俗语
    return True


def detect_intent(text):
    """判断这句话是不是在要求画图。返回抠出的描述（可能为空串）。"""
    import re
    t = (text or "").strip()
    if not t:
        return ""
    # 已完成/过去时的描述 → 不是新请求
    if _DONE_DRAW.search(t) and not re.search(r"(帮我|给我|替我|麻烦|请|再|来一?张|画一?张|画个)", t):
        return ""
    # 俗语优先排除：「画个饼」是在说空话，不是要图
    if _is_idiom(t):
        return ""
    low = t.lower()
    hit = False
    for pat in _DRAW_PATTERNS:
        if re.search(pat, t, re.I):
            hit = True
            break
    if not hit:
        return ""
    prompt = build_prompt_from_text(t)
    # 只有"画一下"没有内容时，仍算命中，由上层追问要画什么
    return prompt if prompt else "__ASK__"


def _adopt_late_from_info(info, prompt, base, key, cb):
    """'轮询超时 240s|tid=abc...' → 登记回收。"""
    tid = ""
    try:
        tid = str(info).split("tid=", 1)[1].strip()
    except Exception:
        pass
    if tid:
        adopt_late_task(tid, prompt, base, key, cb)
