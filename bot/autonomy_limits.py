"""她每天自己能做多少事 —— 面板改、下一轮就生效，不用重启。

为什么要单独一份：这几个数字原来散在三处（config.env、bili_auto 的默认值、
curiosity 的模块常量）。更麻烦的是 curiosity 那两个是 import 时读进常量的，
环境变量改了、面板改了都不会动 —— 只能重启，而重启会断 QQ。

现在的优先级是固定的，只有一处可写：

    面板保存（settings.json → 控制层 apply / install）
  > config.env
  > 调用方自己给的默认值（get_int 的 fallback）

读的人一律用 get_int / get_float，不要再去读 os.environ：
那样面板改了也看不见。
"""
import os

KEYS = ("BILI_AUTO_MAX_PER_DAY",
        "BILI_AUTO_MIN_GAP_HOURS",
        "CURIOSITY_MAX_PER_DAY",
        "CURIOSITY_SHARE_MAX_PER_DAY",
        "CURIOSITY_SHARE_MIN_INTERVAL_HOURS")

_INT_KEYS = {"BILI_AUTO_MAX_PER_DAY", "CURIOSITY_MAX_PER_DAY",
             "CURIOSITY_SHARE_MAX_PER_DAY"}

# 只放「被显式设过」的值：没设过就交给调用方的 fallback，
# 这样单元测试里补丁模块常量仍然有效，不会被这里的默认值盖掉。
LIVE = {}


def _coerce(key, raw):
    try:
        if key in _INT_KEYS:
            return int(float(raw))
        return float(raw)
    except (TypeError, ValueError):
        print("[自主额度] %s=%r 读不懂，忽略" % (key, raw), flush=True)
        return None


def _seed_from_env():
    for key in KEYS:
        raw = os.getenv(key)
        if raw is None or str(raw).strip() == "":
            continue
        got = _coerce(key, raw)
        if got is not None:
            LIVE[key] = got


_seed_from_env()


def get_int(key, fallback=0):
    """当前生效的整数额度；没人设过就用 fallback。"""
    if key in LIVE:
        return int(LIVE[key])
    return int(fallback)


def get_float(key, fallback=0.0):
    if key in LIVE:
        return float(LIVE[key])
    return float(fallback)


def update(values):
    """控制层保存设置后把新值推进来。只认自己知道的键，别的一概不看。"""
    changed = {}
    for key, raw in (values or {}).items():
        if key not in KEYS:
            continue
        got = _coerce(key, raw)
        if got is not None:
            LIVE[key] = got
            changed[key] = got
    return changed


def snapshot():
    """面板只读用：现在生效的是哪些值（没设过的不出现）。"""
    return {k: LIVE[k] for k in KEYS if k in LIVE}


def reset():
    """测试用：清空后重新吃一遍环境变量。"""
    LIVE.clear()
    _seed_from_env()