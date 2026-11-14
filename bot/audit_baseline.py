"""阶段0 本地审计基线：冻结「这份结论是在什么代码/解释器/数据上得到的」。

只读账本，不写 data/、不改任何状态。默认把 JSON 打到 stdout；
需要留档时显式 --out 指定路径（调用方自己负责放在哪里）。
"""
import argparse
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path


def _sha_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _data_root():
    return Path(os.getenv("PERSONA_DATA_DIR") or
                Path(os.getenv("DATA_DIR", "./data")) / "persona")


def _db_state(path):
    if not path.is_file():
        return {"present": False}
    stat = path.stat()
    return {"present": True, "sha256": _sha_file(path),
            "size": stat.st_size, "mtime": round(stat.st_mtime, 3)}


def snapshot(root=None, now=None):
    import engineering_contracts
    destination = Path(root) if root else _data_root()
    binding = engineering_contracts.binding()
    canonical = json.dumps(binding, sort_keys=True, allow_nan=False)
    return {
        "schema_version": 1,
        "created_at": float(now if now is not None else time.time()),
        "code": {
            "binding_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "python": sys.version,
            "platform": platform.platform(),
        },
        "data": {
            "root_digest": hashlib.sha256(str(destination.resolve()).encode()).hexdigest()[:16],
            "cognition": _db_state(destination / "cognition.db"),
            "shadow": _db_state(destination / os.getenv(
                "SHADOW_DB_NAME", "emotion_shadow_v2.db")),
        },
    }


def write(report, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                               allow_nan=False), encoding="utf-8")
    os.replace(temp, path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="阶段0 审计基线")
    parser.add_argument("--root", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = snapshot(args.root or None)
    if args.out:
        path = write(report, args.out)
        print("BASELINE_WRITTEN " + str(path))
        return 0
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())