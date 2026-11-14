"""体检：一句话说清楚服务器现在怎么样。给 /power 指令用，简明不堆数字。"""
import os
import time

_DATA = os.getenv("DATA_DIR", "./data")
_START = time.time()


def _safe(fn, default=""):
    try:
        return fn()
    except Exception:
        return default


def _count_files(sub, exts=None):
    d = os.path.join(_DATA, sub)
    try:
        names = os.listdir(d)
    except Exception:
        return 0, 0
    n = 0
    today = time.strftime("%Y%m%d")
    today_n = 0
    for f in names:
        if exts and not f.lower().endswith(exts):
            continue
        n += 1
        try:
            st = os.stat(os.path.join(d, f))
            if time.strftime("%Y%m%d", time.localtime(st.st_mtime)) == today:
                today_n += 1
        except Exception:
            pass
    return n, today_n


_PYNAMES = ("python.exe", "python", "python3", "python3.14")


def _our_procs():
    """属于本项目的 python 进程。"""
    try:
        import psutil
    except Exception:
        return []
    out = []
    for pr in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cwd", "create_time"]):
        try:
            if pr.info["name"] not in _PYNAMES:
                continue
            cwd = (pr.info.get("cwd") or "").replace("\\", "/")
            if "agent-qq-sandbox-bot" not in cwd:
                continue
            if int(pr.info["pid"]) == os.getpid():
                continue        # 别把"正在汇报的我"算进去
            mi = pr.info.get("memory_info")
            rss = int(mi.rss) if mi else 0
            # 4MB 级的空壳是启动失败的残留，不算在役
            if rss < 20 * 1024 * 1024:
                continue
            out.append(pr.info)
        except Exception:
            continue
    return out


def _procs():
    ps = _our_procs()
    mem = 0
    for i in ps:
        mi = i.get("memory_info")
        if mi:
            mem += mi.rss
    # 系统级 CPU：进程级 cpu_percent 第一次调用恒为 0，必须带采样间隔
    cpu = 0.0
    sys_pct = 0.0
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.15)
        sys_pct = psutil.virtual_memory().percent
    except Exception:
        pass
    return {"n": len(ps), "cpu": round(float(cpu), 1),
            "mem_mb": int(mem / 1024 / 1024), "sys_pct": round(float(sys_pct), 1)}


def _uptime_text():
    """用最老的进程算活了多久。"""
    ps = _our_procs()
    ct = [float(i.get("create_time") or 0) for i in ps if i.get("create_time")]
    oldest = min(ct) if ct else _START
    sec = int(time.time() - oldest)
    h, m = sec // 3600, (sec % 3600) // 60
    if h:
        return "%dh%02dm" % (h, m)
    return "%dm" % m if m else "刚起"


def report():
    """给用户看的体检摘要，2~3 行。"""
    p = _safe(_procs, {"n": 0, "cpu": 0.0, "mem_mb": 0})
    up = _safe(_uptime_text, "")

    img_n, img_today = _safe(lambda: _count_files("generated", (".png", ".jpg", ".jpeg", ".webp")), (0, 0))
    voc_n, _vt = _safe(lambda: _count_files("voice", (".wav", ".mp3", ".amr", ".silk")), (0, 0))
    st_n, _st = _safe(lambda: _count_files("stickers", (".png", ".jpg", ".gif", ".webp")), (0, 0))

    mem_n = 0
    try:
        import group_memory_store
        idx = group_memory_store._ensure_index() or {}
        mem_n = sum(len(g.get("entries") or {}) for g in idx.values())
    except Exception:
        pass

    llm = {}
    try:
        import llm_router
        llm = llm_router.usage_stats() or {}
    except Exception:
        pass

    tools = {}
    try:
        import toolkit
        tools = toolkit.stats() or {}
    except Exception:
        pass

    lines = []
    lines.append("🐟 我还行｜%d 进程｜CPU %s%%｜我吃 %dM（机器 %s%%）｜跑了 %s"
                 % (p["n"], p["cpu"], p["mem_mb"],
                    p.get("sys_pct") or 0, up))
    lines.append("说话 %d 次｜记忆 %d 条｜画图 %d 张（今天 %d）｜语音 %d 条"
                 % (int(llm.get("calls") or 0), mem_n, img_n, img_today, voc_n))
    lines.append("token %d｜缓存 %.0f%%｜工具 %d 次｜表情 %d 张"
                 % (int(llm.get("prompt") or 0),
                    float(llm.get("cached_pct") or 0),
                    int(tools.get("total") or 0), st_n))
    _lat = ""
    try:
        import reply_latency
        _lat = reply_latency.line()
    except Exception:
        pass
    if _lat:
        lines.append(_lat)
    _memuse = ""
    try:
        import memory_usage
        _memuse = memory_usage.line()
    except Exception:
        pass
    if _memuse:
        lines.append(_memuse)
    return "\n".join(lines)