# -*- coding: utf-8 -*-
r"""prep_full_manifest.py — 把一批 TXT 复制进项目并生成全集 manifest（模板）。

- 源：--src 指定的目录（默认桌面"新建文件夹"）*.txt
- 目标：<ROOT>/samples_full/  （只读副本，集中管理）
- 编码探测：整文件严格解码（utf-8 -> gb18030 -> big5），零截断误报
- 产物：<PROBE>/sample_manifest_full.json

用法：python prep_full_manifest.py [--src D:\某目录\*.txt所在目录]
ROOT 由脚本自身定位（benchmark 根），无需改动代码。
"""
import os
import sys
import json
import glob
import hashlib
import shutil
import argparse
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)  # benchmark 根
SRC = r"D:\Users\32133\Desktop\新建文件夹"  # 默认源目录，可用 --src 覆盖
DST = os.path.join(ROOT, "samples_full")
PROBE = os.path.join(ROOT, "probe")


def detect_encoding(path):
    """整文件读入，严格解码探测，最可靠。"""
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "unknown"


def main():
    global SRC
    ap = argparse.ArgumentParser(description="复制 TXT 全集进项目并生成 manifest")
    ap.add_argument("--src", default=SRC, help="源目录（含 *.txt）")
    args = ap.parse_args()
    SRC = args.src

    os.makedirs(DST, exist_ok=True)
    files = sorted(glob.glob(os.path.join(SRC, "*.txt")),
                   key=lambda p: os.path.getsize(p))
    print(f"源目录共 {len(files)} 个 txt 文件")

    manifest = []
    for i, src in enumerate(files, 1):
        sid = f"{i:02d}"
        name = os.path.basename(src)
        data = open(src, "rb").read()
        enc = detect_encoding(src)
        dst = os.path.join(DST, f"{sid}_{name}")
        # 若已存在且内容一致则跳过复制（支持重跑）
        if os.path.exists(dst):
            with open(dst, "rb") as f:
                if f.read() == data:
                    os.chmod(dst, 0o444)
                    manifest.append({
                        "id": sid, "file": f"{sid}_{name}", "orig_name": name,
                        "size_bytes": len(data), "size_mb": round(len(data) / 1048576, 2),
                        "encoding": enc, "md5": hashlib.md5(data).hexdigest(),
                        "source": src,
                    })
                    print(f"  [{sid}] 已存在跳过 {name}  ({len(data)/1048576:.1f}MB {enc})")
                    continue
        with open(dst, "wb") as f:
            f.write(data)
        os.chmod(dst, 0o444)  # 只读，防误改
        manifest.append({
            "id": sid, "file": f"{sid}_{name}", "orig_name": name,
            "size_bytes": len(data), "size_mb": round(len(data) / 1048576, 2),
            "encoding": enc, "md5": hashlib.md5(data).hexdigest(),
            "source": src,
        })
        print(f"  [{sid}] 复制 {name}  ({len(data)/1048576:.1f}MB {enc})")

    out = os.path.join(PROBE, "sample_manifest_full.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    enc_cnt = {}
    for m in manifest:
        enc_cnt[m["encoding"]] = enc_cnt.get(m["encoding"], 0) + 1
    total_mb = sum(m["size_bytes"] for m in manifest) / 1048576
    print(f"\n清单写入：{out}")
    print(f"样本数：{len(manifest)}  总大小：{total_mb:.1f}MB  编码分布：{enc_cnt}")


if __name__ == "__main__":
    main()
