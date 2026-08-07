# -*- coding: utf-8 -*-
"""bench.py — TXT→EPUB 三版本性能测试 · 主调度器。

职责：
1. 读 probe/sample_manifest.json，构造全部测试 run（warmup + S1 + S2 + S3 + A1）。
2. 全局乱序（固定 seed=20260807，可复现），顺序写 probe/bench_plan.json。
3. 逐 run spawn bench_worker.py（独立进程），收集 __RESULT__ JSON。
4. 追加写 output/logs/bench_raw.jsonl（append-only，行号即坐标）。
5. 支持 --resume：跳过 JSONL 中已完成的 run_id。

用法：
  python bench.py [--resume] [--limit N] [--dry]
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = Path(__file__).resolve().parent
SYS_PY = r"C:\Users\32133\AppData\Local\Microsoft\WindowsApps\python.exe"
WORKER = PROBE / "bench_worker.py"
MANIFEST = PROBE / "sample_manifest.json"
PLAN_FILE = PROBE / "bench_plan.json"
LOG_FILE = ROOT / "output" / "logs" / "bench_raw.jsonl"
SAMPLES_DIR = ROOT / "samples"
SEED = 20260807
TIMEOUTS = {"parse": 180, "full": 300, "batch": 900, "detect": 60}

# S3 批量样本：由清单推导（全部 utf-8 样本），默认 6 样本时为 02-06
SCENE_FILTER = None  # None = 全部场景；否则为场景名集合


def derive_s3_ids(manifest):
    """S3 批量用全部 utf-8 样本（排除 gb18030，避免统一编码解错）。"""
    return [m["id"] for m in manifest if m["encoding"] == "utf-8"]


def load_manifest():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def build_plan(manifest, s3_ids):
    samples = {m["id"]: m for m in manifest}
    runs = []

    # warmup：三版本 × parse × 最小样本（固定最前，不计入统计）
    warm_id = "02" if "02" in samples else sorted(samples.keys())[0]
    for v in ("v1", "v2", "v3"):
        runs.append({
            "run_id": f"WARM-{v}-{warm_id}",
            "scene": "parse", "version": v, "sample_id": warm_id,
            "encoding": samples[warm_id]["encoding"], "repeat": 1, "mode": "",
            "warmup": True,
        })

    # S1 切章：3 版本 × 全部样本 × 3 次
    sids = [m["id"] for m in manifest]
    for v in ("v1", "v2", "v3"):
        for sid in sids:
            for r in (1, 2, 3):
                runs.append({
                    "run_id": f"S1-{v}-{sid}-{r}",
                    "scene": "parse", "version": v, "sample_id": sid,
                    "encoding": samples[sid]["encoding"], "repeat": r, "mode": "",
                    "warmup": False,
                })

    # S2 完整转换：3 版本 × 全部样本 × 3 次
    for v in ("v1", "v2", "v3"):
        for sid in sids:
            for r in (1, 2, 3):
                runs.append({
                    "run_id": f"S2-{v}-{sid}-{r}",
                    "scene": "full", "version": v, "sample_id": sid,
                    "encoding": samples[sid]["encoding"], "repeat": r, "mode": "",
                    "warmup": False,
                })

    # S3 批量：3 模式 × 3 次（全部 utf-8 样本）
    for mode in ("sub", "pipe", "serve"):
        for r in (1, 2, 3):
            runs.append({
                "run_id": f"S3-{mode}-{r}",
                "scene": "batch", "version": ("v2" if mode == "sub" else "v3"),
                "sample_id": "+".join(s3_ids), "encoding": "utf-8",
                "repeat": r, "mode": mode, "warmup": False,
            })

    # A1 附加：v3 detect × 2 样本 × 3 次
    for sid in ("01", "05"):
        if sid not in samples:
            continue
        for r in (1, 2, 3):
            runs.append({
                "run_id": f"A1-v3-{sid}-{r}",
                "scene": "detect", "version": "v3", "sample_id": sid,
                "encoding": samples[sid]["encoding"], "repeat": r, "mode": "",
                "warmup": False,
            })

    # 场景过滤（warmup 始终保留）
    if SCENE_FILTER:
        runs = [r for r in runs if r["scene"] in SCENE_FILTER or r.get("warmup")]

    # 乱序：warmup 保持最前，其余全局 shuffle
    head = [r for r in runs if r["warmup"]]
    tail = [r for r in runs if not r["warmup"]]
    rng = random.Random(SEED)
    rng.shuffle(tail)
    ordered = head + tail
    for i, r in enumerate(ordered):
        r["seq"] = i
    return ordered


def sample_args(manifest, run, samples_dir):
    m = {s["id"]: s for s in manifest}
    if run["scene"] == "batch":
        paths = [str(samples_dir / m[sid]["file"]) for sid in S3_SAMPLE_IDS]
        return "|".join(paths)
    sids = run["sample_id"].split("+")
    sid = sids[0]
    return str(samples_dir / m[sid]["file"])


def already_done(log_file):
    done = set()
    if log_file.exists():
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    done.add(obj["run_id"])
                except Exception:
                    pass
    return done


def exec_run(run, manifest, out_dir, samples_dir):
    sample = sample_args(manifest, run, samples_dir)
    cmd = [
        SYS_PY, str(WORKER),
        "--version", run["version"],
        "--version-dir", str(ROOT / "versions" / {
            "v1": "v1_pure_python", "v2": "v2_rust_accel", "v3": "v3_current"}[run["version"]]),
        "--sample", sample,
        "--encoding", run["encoding"],
        "--scene", run["scene"],
        "--mode", run.get("mode", ""),
        "--out", str(out_dir / run["version"]),
        "--repeat-idx", str(run["repeat"]),
        "--run-id", run["run_id"],
    ]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUTS[run["scene"]], encoding="utf-8", errors="replace")
        wall = time.perf_counter() - t0
        last = ""
        for ln in reversed(p.stdout.splitlines()):
            if ln.startswith("__RESULT__"):
                last = ln[len("__RESULT__ "):]
                break
        if last:
            obj = json.loads(last)
            obj["seq"] = run["seq"]
            obj["runner_wall_s"] = round(wall, 3)
            return obj
        return {
            "run_id": run["run_id"], "seq": run["seq"], "ok": False,
            "error": f"no result line (exit={p.returncode}): {p.stderr[-300:]}",
        }
    except subprocess.TimeoutExpired:
        return {"run_id": run["run_id"], "seq": run["seq"], "ok": False,
                "error": f"TIMEOUT >{TIMEOUTS[run['scene']]}s"}
    except Exception as e:
        return {"run_id": run["run_id"], "seq": run["seq"], "ok": False,
                "error": f"{type(e).__name__}: {e}"}


def main():
    global MANIFEST, PLAN_FILE, LOG_FILE, SAMPLES_DIR, S3_SAMPLE_IDS, SCENE_FILTER
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--manifest", default=str(MANIFEST), help="样本清单 JSON")
    ap.add_argument("--samples-dir", default=str(SAMPLES_DIR), help="样本文件目录")
    ap.add_argument("--plan", default=str(PLAN_FILE), help="计划输出 JSON")
    ap.add_argument("--log", default=str(LOG_FILE), help="结果日志 JSONL")
    ap.add_argument("--scene", default="all", help="parse|full|batch|detect|all 或逗号组合")
    args = ap.parse_args()

    MANIFEST = Path(args.manifest)
    PLAN_FILE = Path(args.plan)
    LOG_FILE = Path(args.log)
    SAMPLES_DIR = Path(args.samples_dir)
    manifest = load_manifest()
    S3_SAMPLE_IDS = derive_s3_ids(manifest)
    if args.scene != "all":
        SCENE_FILTER = set(args.scene.split(","))

    out_dir = ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    plan = build_plan(manifest, S3_SAMPLE_IDS)
    with open(PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    n_formal = sum(1 for r in plan if not r["warmup"])
    print(f"[bench] 清单={MANIFEST.name} 样本={len(manifest)} S3={len(S3_SAMPLE_IDS)}本")
    print(f"[bench] 计划共 {len(plan)} run（warmup {len(plan)-n_formal} + 正式 {n_formal}），乱序 seed={SEED}")

    done = already_done(LOG_FILE) if args.resume else set()
    if done:
        print(f"[bench] --resume：跳过 {len(done)} 个已完成 run")

    run_cnt = 0
    fail_cnt = 0
    for run in plan:
        if run["run_id"] in done:
            continue
        if args.limit and run_cnt >= args.limit:
            break
        if args.dry:
            print(f"  [dry] seq={run['seq']} {run['run_id']} {run['scene']} {run['version']} {run['sample_id']}")
            run_cnt += 1
            continue
        obj = exec_run(run, manifest, out_dir, SAMPLES_DIR)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        tag = "W" if run.get("warmup") else " "
        st = "OK " if obj.get("ok") else "FAIL"
        extra = ""
        if obj.get("ok"):
            if run["scene"] == "parse":
                extra = f"parse={obj.get('parse_s')}s ch={obj.get('chapter_count')}"
            elif run["scene"] == "full":
                extra = f"wall={obj.get('wall_s')}s ch={obj.get('chapter_count')} epub={obj.get('epub_valid')}"
            elif run["scene"] == "batch":
                extra = f"wall={obj.get('wall_s')}s n={obj.get('batch_n')}"
            else:
                extra = f"detect={obj.get('detect_s')}s -> {obj.get('detected')}"
        else:
            extra = str(obj.get("error"))[:80]
        run_cnt += 1
        if not obj.get("ok"):
            fail_cnt += 1
        print(f"  [{tag}] seq={run['seq']:3d} {run['run_id']:<16} {st} {extra}", flush=True)
        if run_cnt % 10 == 0:
            print(f"  ... 已跑 {run_cnt} run，失败 {fail_cnt}", flush=True)

    print(f"\n[bench] 完成：新增 {run_cnt} run，失败 {fail_cnt}")
    print(f"[bench] 日志：{LOG_FILE}")


if __name__ == "__main__":
    main()
