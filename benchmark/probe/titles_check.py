# -*- coding: utf-8 -*-
"""titles_check.py — 正确性校验：三版本完整章节标题序列比对（不测性能）。

- 对 6 本样本 × 3 版本各跑一次切章，保存完整标题列表到 output/logs/titles_v{ver}_{sid}.json。
- 以 v3 为基准，diff 标题序列，输出差异摘要。
用法：python titles_check.py
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = {
    "v1": ROOT / "versions" / "v1_pure_python",
    "v2": ROOT / "versions" / "v2_rust_accel",
    "v3": ROOT / "versions" / "v3_current",
}
MANIFEST = json.load(open(ROOT / "probe" / "sample_manifest.json", encoding="utf-8"))
OUT = ROOT / "output" / "logs"
OUT.mkdir(parents=True, exist_ok=True)


def load_core(vdir):
    vpath = str(vdir.resolve())
    sys.path.insert(0, vpath)
    try:
        spec = importlib.util.spec_from_file_location(f"chk_{vdir.name}", str(vdir / "txt_to_epub_core.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def titles_of(core, ver, sample, encoding):
    if ver == "v1":
        chapters = core.parse_txt(Path(sample), encoding)
        return [c[0] for c in chapters]
    temp, index = core.parse_txt_index(str(sample), encoding)
    t = [e["title"] for e in index]
    if ver == "v2" and temp is not None:
        try:
            p = Path(str(temp))
            if p.exists():
                p.unlink()
        except Exception:
            pass
    return t


def main():
    cores = {v: load_core(d) for v, d in VERSIONS.items()}
    all_titles = {}
    for m in MANIFEST:
        sid = m["id"]
        sample = ROOT / "samples" / m["file"]
        for ver, core in cores.items():
            t = titles_of(core, ver, sample, m["encoding"])
            all_titles[f"{ver}_{sid}"] = t
            with open(OUT / f"titles_v{ver}_{sid}.json", "w", encoding="utf-8") as f:
                json.dump(t, f, ensure_ascii=False)
            print(f"  {ver} 样本{sid}: {len(t)} 章 已存 titles_v{ver}_{sid}.json")

    print()
    print("=" * 70)
    print("以 v3 为基准的标题序列 diff：")
    total_diff = 0
    for m in MANIFEST:
        sid = m["id"]
        base = all_titles[f"v3_{sid}"]
        for ver in ("v1", "v2"):
            t = all_titles[f"{ver}_{sid}"]
            n = min(len(base), len(t))
            diffs = [i for i in range(n) if base[i] != t[i]]
            d = len(diffs)
            total_diff += d
            if len(base) != len(t) or d:
                print(f"  样本{sid} {ver} vs v3: 章数 {len(t)} vs {len(base)}"
                      f"{' (不一致!)' if len(t)!=len(base) else ''} 标题差异 {d} 处")
                for i in diffs[:5]:
                    print(f"    @{i}: {ver}='{t[i][:30]}'  v3='{base[i][:30]}'")
                if len(base) != len(t):
                    extra = set(t) - set(base)
                    if extra:
                        print(f"    {ver} 独有标题 {len(extra)} 个，例: {list(extra)[:3]}")
            else:
                print(f"  样本{sid} {ver} vs v3: 完全一致（{len(t)} 章）")
    print(f"\n汇总：共 {total_diff} 处标题差异（相对 v3 基准）")


if __name__ == "__main__":
    main()
