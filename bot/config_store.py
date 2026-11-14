"""config.env 的机制化读写：原子落盘、重复键归一、留可回滚的代次。

为什么要有这个模块（对应「reversible effects」那条）：
以前面板改触发词是两处各写一遍的 `re.sub(..., count=1)` + `write_text`，
有两个真实问题：

  1. **非原子**。`write_text` 直接把原文件截断再写；进程正好在那一刻挂掉，
     她的 config.env 就成半截文件，下次起不来。备份靠的是人记得先 cp，
     那是纪律，不是机制。
  2. **重复键会静默吞掉改动**。`re.sub(count=1)` 改的是**第一处**，
     而 `launcher.load_env()` 是逐行赋值、**后出现的赢**。
     所以只要文件里同一个键出现两次，面板这次改得再成功，
     重启之后仍然回到老值 —— 用户看到的现象就是「我改了但没生效」。

这个模块把两件事都收进机制里：写之前先归一（同一个键只留一行、就地把值换掉），
写完原子替换，并且每次改都留一份带时间戳的代次，`restore()` 能回到上一版。
"""
import os
import re
import shutil
import time

GENERATIONS = int(os.getenv("CONFIG_ENV_GENERATIONS", "10"))
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def default_path():
    """服务器上的绝对路径优先，本地开发用仓库里那份。"""
    for p in ("/opt/qqbot/config.env",):
        if os.path.isfile(p):
            return p
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config.env")


def _hist_dir(path):
    return os.path.join(os.path.dirname(path), ".config.env.history")


def _stamp():
    """代次文件名。必须带亚秒和进程号：
    只用秒级时间戳的话，一秒内连改两次会写到同一个名字上，
    前一份被就地覆盖 —— 「留了可回滚的代次」当场变成只有一份，
    restore(which=-2) 直接越界。回滚机制在最需要它的时候失效。
    """
    base = "%s_%03d_%d" % (time.strftime("%Y%m%d_%H%M%S"),
                         int((time.time() % 1) * 1000), os.getpid())
    _stamp._n = getattr(_stamp, "_n", 0) + 1
    return "%s_%02d" % (base, _stamp._n)


def read_text(path=None):
    p = path or default_path()
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def read_values(path=None):
    """读成 dict，口径和 launcher.load_env 一致：后出现的赢（并且顺手去掉引号）。"""
    out = {}
    for line in read_text(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def duplicate_keys(path=None):
    """文件里出现两次以上的键。这是「改了没生效」的元凶，得能看见。"""
    seen, dup = set(), set()
    for line in read_text(path).splitlines():
        m = _KEY_RE.match(line.strip())
        if not m:
            continue
        k = m.group(1)
        if k in seen:
            dup.add(k)
        seen.add(k)
    return sorted(dup)


def _normalize(text, updates):
    """就地替换键值，并把重复的键收成一行。

    保留原有顺序和注释：这份文件是人家手写的配置，不是我们的产物。
    """
    lines = text.split("\n")
    done = set()
    out = []
    for i, line in enumerate(lines):
        m = _KEY_RE.match(line.strip())
        if not m:
            out.append(line)
            continue
        k = m.group(1)
        if k in updates:
            if k in done:
                continue            # 同一个键的第二行：丢掉，别再让「后面的赢」
            out.append("%s=%s" % (k, updates[k]))
            done.add(k)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in done:
            if out and out[-1].strip():
                out.append("")
            out.append("%s=%s" % (k, v))
    # 文件末尾留一个换行，别再制造「最后一行和下一行粘连」的坑
    while out and out[-1] == "" and len(out) > 1 and out[-2] == "":
        out.pop()
    return "\n".join(out)


def set_values(updates, path=None, reason=""):
    """改若干个键。返回 {ok, changed, dupes_fixed, backup, error}。"""
    p = path or default_path()
    res = {"ok": False, "changed": [], "dupes_fixed": [], "backup": "", "error": ""}
    if not updates:
        res["error"] = "没有要写的键"
        return res
    if not os.path.isfile(p):
        res["error"] = "配置文件不在：" + p
        return res
    try:
        old = read_text(p)
        res["dupes_fixed"] = duplicate_keys(p)
        new = _normalize(old, {str(k): str(v) for k, v in updates.items()})
        if new == old:
            res["ok"] = True
            res["changed"] = []
            return res
        # 先落一份代次，再动原文件：顺序反了的话，崩在中间就没得回滚了
        bak = snapshot(p)
        res["backup"] = bak
        tmp = p + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)      # 这文件里全是密钥，别因为改写把权限换了
        except Exception:
            pass
        res["ok"] = True
        res["changed"] = sorted(updates.keys())
        print("[配置] 写了 %s（%s）｜代次 %s"
              % (",".join(res["changed"]), reason or "无备注",
                 os.path.basename(bak)), flush=True)
        if res["dupes_fixed"]:
            print("[配置] 顺手归并了重复键: %s（以前这些键改完重启就被旧值盖回去）"
                  % ",".join(res["dupes_fixed"]), flush=True)
    except Exception as e:
        res["error"] = type(e).__name__ + ": " + str(e)[:120]
        print("[配置] 写入失败 " + res["error"], flush=True)
    return res


def snapshot(path=None, reason=""):
    """单独存一份当前代次。写文件之前也调它，回滚前想留档也调它。"""
    p = path or default_path()
    if not os.path.isfile(p):
        return ""
    try:
        hd = _hist_dir(p)
        os.makedirs(hd, exist_ok=True)
        bak = os.path.join(hd, "config.env.%s" % _stamp())
        shutil.copy2(p, bak)
        _prune_generations(hd)
        return bak
    except Exception:
        return ""


def _prune_generations(hd):
    try:
        gens = sorted(f for f in os.listdir(hd) if f.startswith("config.env."))
    except Exception:
        return
    for gone in gens[:-max(1, GENERATIONS)]:
        try:
            os.remove(os.path.join(hd, gone))
        except Exception:
            pass


def generations(path=None):
    p = path or default_path()
    hd = _hist_dir(p)
    try:
        return sorted(f for f in os.listdir(hd) if f.startswith("config.env."))
    except Exception:
        return []


def restore(path=None, which=-1):
    """回到某一份代次。默认上一版。"""
    p = path or default_path()
    gens = generations(p)
    if not gens:
        return {"ok": False, "error": "没有可回滚的代次"}
    # 先把目标算出来再留档。反过来写的话，新留的那一份会插到列表末尾，
    # 把 which=-1 顶成「当前文件本身」—— 于是回滚变成了原地复制，
    # 还汇报 ok，等于最坏情况下一条命令白敲。
    src = os.path.join(_hist_dir(p), gens[which])
    if not os.path.isfile(src):
        return {"ok": False, "error": "代次文件不存在: " + os.path.basename(src)}
    try:
        snapshot(p, reason="回滚前留档")
        shutil.copy2(src, p)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        return {"ok": True, "restored": src, "keys": sorted(read_values(p))}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:120]}
