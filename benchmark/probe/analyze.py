# -*- coding: utf-8 -*-
"""analyze.py — 聚合 bench_raw.jsonl → summary.csv + 控制台汇总（中位数）。

用法：python analyze.py [--csv output/summary.csv]
"""
import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "output" / "logs" / "bench_raw.jsonl"


def load_runs():
    runs = []
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except Exception:
                pass
    return runs


def median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 4) if vals else None


def agg(runs):
    """按 (scene, version, mode, sample_id) 聚合，中位数。"""
    groups = {}
    for r in runs:
        if r.get("warmup") or not r.get("ok"):
            continue
        key = (r["scene"], r["version"], r.get("mode") or "", r.get("sample_id") or "-")
        groups.setdefault(key, []).append(r)
    out = {}
    for key, rs in groups.items():
        scene, ver, mode, sid = key
        rec = {"scene": scene, "version": ver, "mode": mode, "sample_id": sid,
               "n": len(rs), "ok": sum(1 for r in rs if r.get("ok"))}
        for field in ("wall_s", "decode_s", "parse_s", "detect_s",
                      "chapter_count", "output_size", "peak_rss_mb", "exe_proc_count"):
            rec[field] = median([r.get(field) for r in rs])
        # epub 有效性比例（S2 用）
        rec["epub_valid_ratio"] = round(
            sum(1 for r in rs if r.get("epub_valid")) / len(rs), 2) if rs else None
        # 附全部原始值（用于报告）
        rec["raw_wall"] = [r.get("wall_s") for r in rs]
        out[key] = rec
    return out


def print_tables(aggd):
    order = ["S1", "S2", "S3", "A1"]
    print("=" * 78)
    print("S1 切章（parse，decode 统一预读）—— 中位数")
    print(f"{'版本':<4}{'样本':<6}{'decode_s':>10}{'parse_s':>10}{'wall_s':>10}{'章数':>8}{'峰值MB':>8}")
    for k in sorted(aggd):
        if k[0] != "parse":
            continue
        r = aggd[k]
        print(f"{r['version']:<4}{r['sample_id']:<6}{r['decode_s'] or 0:>10}{r['parse_s'] or 0:>10}"
              f"{r['wall_s'] or 0:>10}{r['chapter_count'] or 0:>8}{r['peak_rss_mb'] or 0:>8}")

    print()
    print("=" * 78)
    print("S2 完整转换（full，convert_single 端到端）—— 中位数")
    print(f"{'版本':<4}{'样本':<6}{'wall_s':>10}{'章数':>8}{'输出KB':>10}{'峰值MB':>8}{'epub有效':>8}")
    for k in sorted(aggd):
        if k[0] != "full":
            continue
        r = aggd[k]
        print(f"{r['version']:<4}{r['sample_id']:<6}{r['wall_s'] or 0:>10}{r['chapter_count'] or 0:>8}"
              f"{(r['output_size'] or 0)//1024:>10}{r['peak_rss_mb'] or 0:>8}{r['epub_valid_ratio'] or 0:>8}")

    print()
    print("=" * 78)
    print("S3 批量（5 本 UTF-8 一批）—— 中位数 wall_s")
    print(f"{'模式':<8}{'wall_s':>10}{'峰值MB':>8}{'exe进程':>8}")
    for k in sorted(aggd):
        if k[0] != "batch":
            continue
        r = aggd[k]
        print(f"{r['mode']:<8}{r['wall_s'] or 0:>10}{r['peak_rss_mb'] or 0:>8}{r['exe_proc_count'] or 0:>8}")

    print()
    print("=" * 78)
    print("A1 附加：v3 编码择优 detect 耗时—— 中位数")
    for k in sorted(aggd):
        if k[0] != "detect":
            continue
        r = aggd[k]
        print(f"v3 样本{r['sample_id']}: detect_s={r['detect_s']}")

    print()
    print("=" * 78)
    print("正确性：S1 三版本章数一致性与首章标题（不一致会在 P6 完整比对中标出）")
    for sid in ("01", "02", "03", "04", "05", "06"):
        cnts = {}
        for k in sorted(aggd):
            if k[0] == "parse" and k[3] == sid:
                cnts[k[1]] = aggd[k]["chapter_count"]
        tag = "✓" if len(set(cnts.values())) == 1 else "✗ 不一致!"
        print(f"  样本{sid}: {cnts} {tag}")


def to_csv(aggd, path):
    fields = ["scene", "version", "mode", "sample_id", "n", "ok", "wall_s",
              "decode_s", "parse_s", "detect_s", "chapter_count", "output_size",
              "peak_rss_mb", "exe_proc_count", "epub_valid_ratio"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in sorted(aggd):
            row = dict(aggd[k])
            row.update({"scene": k[0], "version": k[1], "mode": k[2], "sample_id": k[3]})
            w.writerow({fld: row.get(fld) for fld in fields})
    print(f"\nsummary.csv 已写入：{path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "output" / "summary.csv"))
    args = ap.parse_args()
    runs = load_runs()
    print(f"共读取 {len(runs)} 条日志（warmup/失败不计入聚合）")
    aggd = agg(runs)
    print_tables(aggd)
    to_csv(aggd, args.csv)


if __name__ == "__main__":
    main()
