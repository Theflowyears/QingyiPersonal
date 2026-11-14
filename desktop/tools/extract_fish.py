"""把清漪的 webm 动画抽成桌宠用的 PNG 帧序列（带 alpha），并写 manifest.json。

素材来自 shenliuyu18/dafeiyu-pet 的 assets/anims/*.webm —— 那是 VP9-with-alpha，
必须显式 `-c:v libvpx-vp9` 解码，否则 ffmpeg 走普通 vp9 解码器会把 alpha 丢掉，
抽出来就是一圈黑底的废图。这个坑踩过一次，所以整条流水线留在这里。

用法：
    venv\\Scripts\\python.exe desktop\\tools\\extract_fish.py <anims目录> [高度]
只本机个人使用，不随任何分发打包。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

DST = Path(__file__).resolve().parents[1] / "assets" / "fish"
MIN_DELAY_MS = 16  # 全帧抽取、按源帧率原速放；再快也不超过 ~60fps，免得 QTimer 在 Windows 上抖

MAP = {
    "idle": "待机呼吸休闲",
    "talk": "碎碎念-对屏碎碎念",
    "think": "工作状态-思考冒泡",
    "working": "工作状态-忙碌点按",
    "done": "工作状态-雀跃庆祝",
    "fail": "工作状态-垂头叹气冒汗",
    "file": "工作状态-清点归档",
    "confirm": "点击回应-傲娇生气",
    "browse": "东张西望",
    "sleep": "原地小憩沉眠",
    "drag": "被鼠标拖拽悬空反馈",
    "yawn": "哈欠连天",
}


def _ffmpeg() -> str:
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except ImportError:
        pass
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("没有 ffmpeg：pip install imageio-ffmpeg，或把 ffmpeg 加进 PATH")
    return found


def _duration(ffmpeg: str, src: str) -> float:
    err = subprocess.run([ffmpeg, "-hide_banner", "-i", src],
                         capture_output=True).stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", err)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def extract(anims_dir: str, height: int = 240) -> int:
    ffmpeg = _ffmpeg()
    work = Path(os.environ.get("TEMP", ".")) / "dafeiyu-frames"
    manifest = {"note": "帧图抽自 shenliuyu18/dafeiyu-pet 的 VP9-alpha webm；"
                        "全帧抽取，delay_ms = 源时长/帧数（原速），source_seconds 是源动画真实时长",
                "states": {}}
    failed = 0
    for state, name in MAP.items():
        src = os.path.join(anims_dir, name + ".webm")
        if not os.path.exists(src):
            print(f"{state}: 源不存在 {name}.webm")
            failed += 1
            continue
        out = DST / state
        tmp = work / state
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error",
                               "-c:v", "libvpx-vp9", "-i", src,
                               "-vf", f"scale=-2:{height}", str(tmp / "%04d.png")],
                              capture_output=True)
        files = sorted(tmp.glob("*.png"))
        if not files:
            print(f"{state}: 抽帧失败 " +
                  proc.stderr.decode("utf-8", "replace")[:160])
            failed += 1
            continue
        idxs = range(len(files))  # 一帧不丢，源 24fps 原速放
        sec = _duration(ffmpeg, src)
        delay = max(MIN_DELAY_MS, round(sec * 1000 / len(files))) if sec else MIN_DELAY_MS
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.png"):
            old.unlink()
        sample = sorted({round(i * (len(files) - 1) / 11) for i in range(12)})
        alphas = []
        for n, i in enumerate(idxs):
            im = Image.open(files[i]).convert("RGBA")
            im.save(out / f"{n:03d}.png")
            if n in sample:
                alphas.append(float((np.asarray(im)[:, :, 3] < 16).mean()))
        ratio = sum(alphas) / len(alphas) if alphas else 0.0
        manifest["states"][state] = {"frames": len(files), "delay_ms": delay,
                                     "source": name, "source_seconds": round(sec, 2),
                                     "transparent_ratio": round(ratio, 3)}
        print(f"{state:8s} {len(files)}帧  {delay}ms/帧 ({1000/delay:.0f}fps)  "
              f"透明 {ratio:.0%}  源 {sec:.2f}s")
        if ratio < 0.2:
            print(f"         ↑ 透明占比这么低，八成是解码器没走 libvpx-vp9，alpha 丢了")
            failed += 1
    (DST / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    total = sum(p.stat().st_size for p in DST.rglob("*.png"))
    print(f"写出 manifest + {total/1e6:.1f} MB 帧图到 {DST}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("anims_dir", help="dafeiyu-pet 的 assets/anims 目录")
    ap.add_argument("height", nargs="?", type=int, default=240)
    args = ap.parse_args()
    sys.exit(extract(args.anims_dir, args.height))
