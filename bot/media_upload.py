"""给她发图的入口（面板/悄悄话）：安全校验 + 内容寻址落盘。

设计边界，逐条都是有意为之：
  · 只收真图：扩展名白名单 + 魔数嗅探。`.svg` 直接拒——SVG 能带脚本，
    存进静态目录等于给自己埋一个存储型 XSS；`.html/.php/.jsp/.exe` 之类更不用说。
  · 双重扩展名也拒（`photo.jpg.php`、`photo.php.jpg`）：一个靠扩展名骗过白名单，
    一个靠魔数骗不过嗅探，两条路都要堵。
  · 文件名不信客户端：一律用内容 sha1 生成。客户端名字里有中文、空格、`../`
    都不影响落盘路径，也不会有路径穿越。
  · 同一张图重复上传落到同一个文件（内容寻址），不会堆垃圾。
  · 存到 DATA_DIR/uploads/日期/，不在 Web 静态根里；面板通过接口读，不靠目录直链。
"""
import hashlib
import os
import re
import time

# 只允许真正的位图。svg 不在列：它可携带脚本，落进可访问目录就是存储型 XSS。
ALLOWED_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
               "webp": "image/webp", "gif": "image/gif"}
MAX_BYTES = int(os.getenv("MEDIA_MAX_BYTES", str(8 * 1024 * 1024)))
_STAMP = "%Y-%m-%d"


def _ext(name):
    n = str(name or "").strip().lower()
    if not n or "." not in n:
        return ""
    return n.rsplit(".", 1)[-1].strip()


def allowed_name(name):
    """扩展名层面先过一遍：只认白名单，且不接受带点的复合名。"""
    n = str(name or "").strip().lower()
    if not n:
        return False
    # 双重扩展名一律拒（photo.jpg.php / photo.php.jpg）
    base, _, tail = n.rpartition(".")
    if "." in base.replace("/", ""):
        # 名字里还有一段扩展名样式的东西，看最后一段是不是危险的
        parts = [p for p in base.split(".") if p]
        for p in parts:
            if p in ("php", "html", "htm", "js", "svg", "exe", "sh", "jsp", "py", "asp", "cgi"):
                return False
    return tail in ALLOWED_EXT


def sniff_kind(body):
    """按文件头判断真实类型，不看客户端说什么。"""
    b = bytes(body or b"")[:16]
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return ""


def validate(body, name):
    """返回 (ok, 原因)。任何一条不过就不收。"""
    if not body:
        return False, "空的"
    if len(body) > MAX_BYTES:
        return False, "大小超限（最多 %d MB）" % (MAX_BYTES // (1024 * 1024))
    if not allowed_name(name):
        return False, "类型不收（只允许 jpg/jpeg/png/webp/gif）"
    kind = sniff_kind(body)
    if not kind:
        return False, "内容不是图片或文件头无法识别"
    # 扩展名与真实类型还得对上，防止挂羊头卖狗肉
    ext = _ext(name)
    if ext == "jpeg":
        ext = "jpg"
    if ext != kind:
        return False, "扩展名与内容不符（%s vs %s）" % (ext, kind)
    return True, ""


def storage_root(storage_dir=None):
    if storage_dir:
        return os.path.abspath(storage_dir)
    root = os.getenv("DATA_DIR", "./data")
    return os.path.join(os.path.abspath(root), "uploads")


def save_upload(body, name, storage_dir=None):
    """落盘并返回绝对路径。文件名由内容哈希决定，绝不用客户端给的名字。"""
    ok, why = validate(body, name)
    if not ok:
        raise ValueError(why)
    kind = sniff_kind(body)
    day = time.strftime(_STAMP)
    digest = hashlib.sha1(bytes(body)).hexdigest()[:24]
    fname = "%s.%s" % (digest, kind)
    folder = os.path.join(storage_root(storage_dir), day)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, fname)
    if not os.path.isfile(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(bytes(body))
        os.replace(tmp, path)
    # 落盘后确认还在预期目录里（多一道，防拼接出格）
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(storage_root(storage_dir)) + os.sep):
        try:
            os.remove(path)
        except Exception:
            pass
        raise ValueError("落盘路径越界，已拒绝")
    return real


def token(path, storage_dir=None):
    """绝对路径 → 可以交给前端的相对标记（天/文件名）。

    前端永远拿不到也猜不出 uploads 之外的路径，回传时只认这个标记。
    """
    real = os.path.realpath(str(path or ""))
    root = os.path.realpath(storage_root(storage_dir))
    if not real.startswith(root + os.sep):
        return ""
    return real[len(root) + 1:].replace(os.sep, "/")


def resolve(mark, storage_dir=None):
    """把前端给的标记换回 uploads 内的真实路径；不信任任何输入。

    只接受 `YYYY-MM-DD/<sha1>.<白名单扩展名>` 这一种形状，逐段用正则过，
    再看 realpath 是否仍在 uploads 下。带 ..、绝对路径、多重扩展名一律拒。
    """
    s = str(mark or "").strip().replace("\\", "/")
    parts = s.split("/")
    if len(parts) != 2:
        return ""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
        return ""
    if not re.match(r"^[0-9a-f]{8,40}\.(jpg|jpeg|png|webp|gif)$", parts[1].lower()):
        return ""
    root = os.path.realpath(storage_root(storage_dir))
    real = os.path.realpath(os.path.join(root, parts[0], parts[1]))
    if not real.startswith(root + os.sep):
        return ""
    return real if os.path.isfile(real) else ""


def prune(days=30):
    """上传目录按天清理，别让主人的照片无限堆着。"""
    root = storage_root()
    cut = time.time() - max(1, int(days)) * 86400
    removed = 0
    try:
        for day in os.listdir(root):
            p = os.path.join(root, day)
            if not os.path.isdir(p):
                continue
            try:
                if os.path.getmtime(p) < cut:
                    for fn in os.listdir(p):
                        os.remove(os.path.join(p, fn))
                        removed += 1
                    os.rmdir(p)
            except Exception:
                continue
    except Exception:
        return 0
    return removed


def stats():
    root = storage_root()
    files = 0
    size = 0
    try:
        for _day, _sub, fns in os.walk(root):
            for fn in fns:
                files += 1
                try:
                    size += os.path.getsize(os.path.join(_day, fn))
                except Exception:
                    pass
    except Exception:
        pass
    return {"files": files, "bytes": size, "max_bytes": MAX_BYTES,
            "allowed": sorted(ALLOWED_EXT)}
