# -*- coding: utf-8 -*-
"""五个常驻 JSON 库共用的两条防丢底线。

群事实/好奇度/成员档案/悄悄话/知识库原来各写各的读盘写盘，同一个坑踩五遍：
文件读损坏时直接退回空对象并缓存，下一次写盘就把原始字节永久覆盖成空；
写盘又不 fsync，断电可能只留个空壳。这里把「损坏先留底」和「原子落盘」
收成一处，测一次就管住五个库。留底用带时间戳的后缀，绝不覆盖上一次的留底。
"""
import json
import os
import time
import tempfile
import threading

_REPLACE_LOCK = threading.Lock()


def preserve_corrupt(path):
    """读不动的原始文件先搬走留底，绝不让下一次写盘把它覆盖成空。

    永不抛异常：留底失败也不能把调用方的回复链路弄崩。返回留底路径（失败/不存在返回 ""）。
    """
    try:
        if not os.path.isfile(path):
            return ""
        dest = path + ".corrupt"
        if os.path.exists(dest):
            dest = path + ".corrupt-%d" % int(time.time() * 1000)
        os.replace(path, dest)
        print("[防丢] 损坏文件已留底 " + os.path.basename(dest), flush=True)
        return dest
    except Exception as e:
        print("[防丢] 留底失败 " + type(e).__name__, flush=True)
        return ""


def atomic_write_json(path, data):
    """tmp 写满 → flush+fsync → 原子替换。断电也不会留半个主文件。"""
    tmp = None
    try:
        path = os.fspath(path)
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        with _REPLACE_LOCK:
            os.replace(tmp, path)
        return True
    except Exception as e:
        print("[防丢] 写盘失败 " + type(e).__name__ + " " + str(e)[:60], flush=True)
        return False
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)