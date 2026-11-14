"""给视频模型临时取件的签名链接。

B 站 CDN 需要 Referer/Cookie，模型端拿不到；先把文件落到本地，再通过
我们自己的公网入口发一个**不可猜、带 TTL**的链接。只服务登记过的文件，
不暴露目录，不提供上传，过期就删。
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

DEFAULT_TTL = 900


def _now():
    return time.time()


def _root():
    # DATA_DIR 在测试和多进程里会变，不能 import 时冻成常量
    return Path(os.getenv("DATA_DIR", "./data")) / "media_cache"


def _registry():
    return _root() / "registry.json"


def _secret():
    """签名密钥：优先环境变量；没有就在缓存目录生成一份 0600 的共享密钥。

    机器人进程和 Web 进程是两个进程，必须用同一把密钥，所以不能各自随机。
    """
    got = (os.getenv("CEYLAN_SECRET_KEY") or os.getenv("WEB_SECRET_KEY")
           or os.getenv("MEDIA_PUBLIC_SECRET") or "").strip()
    if got:
        return got
    path = _root() / ".secret"
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                return value
        _root().mkdir(parents=True, exist_ok=True)
        value = uuid.uuid4().hex + uuid.uuid4().hex
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value
    except Exception:
        return ""


def base_url():
    return (os.getenv("PUBLIC_MEDIA_BASE_URL") or os.getenv("MEDIA_PUBLIC_BASE_URL")
            or "").strip().rstrip("/")


def _load():
    try:
        got = json.loads(_registry().read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _save(data):
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = _registry().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _registry())


def _sig(stem, expires):
    key = _secret()
    if not key:
        return ""
    return hmac.new(key.encode("utf-8"), ("%s:%s" % (stem, expires)).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _inside(path):
    try:
        Path(path).resolve().relative_to(_root().resolve())
        return True
    except (ValueError, OSError):
        return False


def publish(path, ttl=DEFAULT_TTL, base_url=None, base=None):
    """登记一个文件并返回模型可读取的 URL（URL 里只有 token，没有文件名）。"""
    p = Path(str(path or "")).resolve()
    if not p.is_file() or p.stat().st_size <= 0:
        return {"ok": False, "reason": "empty_or_missing"}
    if not _inside(p):
        return {"ok": False, "reason": "outside media_cache: " + p.name}
    root = (base_url or base or globals()["base_url"]()).rstrip("/")
    if not root:
        return {"ok": False, "reason": "no_public_base"}
    if not _secret():
        return {"ok": False, "reason": "no_public_secret"}
    stem = uuid.uuid4().hex
    expires = int(_now() + max(1, int(ttl or DEFAULT_TTL)))
    token = "%s.%s.%s" % (stem, expires, _sig(stem, expires))
    data = _load()
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    now = int(_now())
    items = {k: v for k, v in items.items() if int(v.get("expires") or 0) > now}
    items[token] = {"path": str(p), "expires": expires, "name": p.name}
    _save({"items": items})
    return {"ok": True, "token": token, "stem": stem, "expires": expires,
            "url": "%s/media/qv/%s" % (root, token)}


def resolve(token, now=None):
    """校验签名与 TTL，返回文件路径；任何不合法都返回空串。"""
    token = str(token or "")
    try:
        stem, exp_s, sig = token.split(".", 2)
        expires = int(exp_s)
    except (ValueError, TypeError):
        return ""
    now = _now() if now is None else float(now)
    if expires <= now:
        return ""
    if not stem or not sig or not hmac.compare_digest(_sig(stem, expires), sig):
        return ""
    rec = (_load().get("items") or {}).get(token) or {}
    path = str(rec.get("path") or "")
    if int(rec.get("expires") or 0) != expires or not path:
        return ""
    if not _inside(path) or not Path(path).is_file():
        return ""
    return str(Path(path).resolve())


def cleanup(now=None):
    """删掉过期文件与登记；文件已经不在的登记也一并清掉。

    返回的是真正删掉的文件数。B 站那条链在模型看完后会立刻删临时文件，
    登记要等到 TTL 才过期 —— 不顺手清掉，注册表就会堆一串指向空文件的项。
    """
    now = _now() if now is None else float(now)
    data = _load()
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    keep, removed = {}, 0
    for key, rec in items.items():
        path = str(rec.get("path") or "")
        alive = _inside(path) and Path(path).is_file()
        if int(rec.get("expires") or 0) > now:
            if alive:
                keep[key] = rec
            continue
        try:
            if alive:
                Path(path).unlink()
            removed += 1
        except OSError:
            pass
    _save({"items": keep})
    return removed
