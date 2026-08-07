# -*- coding: utf-8 -*-
"""preflight.py — 正式测试前的运行前检查。

检查项：
A. 文件完整性：三版本目录、exe 存在与归属、样本 6 本 md5 与只读属性
B. 规则一致性：三版本 exportTxtTocRule..json md5、TOC_RE 一致
C. 运行时：系统 Python 3.12.10 + ebooklib/PIL/psutil/tkinterdnd2
D. 探针：bench.py --dry 计划生成（126 run，乱序可复现）
E. 环境：磁盘空间、可用内存、无残留测试进程、output/logs 为空

用法：python preflight.py
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYS_PY = r"C:\Users\32133\AppData\Local\Microsoft\WindowsApps\python.exe"
VERSIONS = {
    "v1": ROOT / "versions" / "v1_pure_python",
    "v2": ROOT / "versions" / "v2_rust_accel",
    "v3": ROOT / "versions" / "v3_current",
}
MANIFEST = json.load(open(ROOT / "probe" / "sample_manifest.json", encoding="utf-8"))

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{tag}] {name}  {detail}")
    return ok


def md5_file(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_head(p, n=2000):
    with open(p, "rb") as f:
        return hashlib.md5(f.read(n)).hexdigest()


print("=" * 72)
print("A. 文件完整性")
print("-" * 72)
# 三版本目录 + core
for v, d in VERSIONS.items():
    core = d / "txt_to_epub_core.py"
    check(f"{v} 目录存在", d.is_dir(), str(d))
    check(f"{v} core.py 存在", core.exists())
    gui = d / "txt_to_epub_gui_2.py"
    check(f"{v} gui.py 存在", gui.exists())
# exe
v2_exe = VERSIONS["v2"] / "parse_txt_rust.exe"
v3_exe = VERSIONS["v3"] / "parse_txt_rust.exe"
check("v2 exe 存在且≥4MB", v2_exe.exists() and v2_exe.stat().st_size > 4_000_000,
      f"{v2_exe.stat().st_size} bytes")
check("v3 exe 存在且≥4MB", v3_exe.exists() and v3_exe.stat().st_size > 4_000_000,
      f"{v3_exe.stat().st_size} bytes")
v1_exe = VERSIONS["v1"] / "parse_txt_rust.exe"
check("v1 无 exe（符合纯 Python 定位）", not v1_exe.exists())
# exe 分属不同版本（md5 应不同，避免误拷贝）
check("v2/v3 exe 是不同二进制", md5_file(v2_exe) != md5_file(v3_exe))
# Rust 工程完整性
check("v2 rust_src/Cargo.toml 存在", (VERSIONS["v2"] / "rust_src" / "Cargo.toml").exists())
check("v3 rust/Cargo.toml 存在", (VERSIONS["v3"] / "rust" / "Cargo.toml").exists())
# 样本
samples_dir = ROOT / "samples"
check("samples/ 有 6 本", len(list(samples_dir.glob("*.txt"))) == 6)
for m in MANIFEST:
    p = samples_dir / m["file"]
    ok = p.exists() and md5_file(p) == m["md5"]
    ro = (os.stat(p).st_mode & 0o222) == 0
    check(f"样本 {m['id']} {m['name']} md5+只读", ok and ro,
          f"{m['size_mb']}MB {m['encoding']}" + ("" if ro else " [非只读!]"))

print()
print("=" * 72)
print("B. 规则一致性")
print("-" * 72)
rule_md5s = {}
for v, d in VERSIONS.items():
    rule_md5s[v] = md5_file(d / "exportTxtTocRule..json")
check("三版本规则 JSON md5 一致", len(set(rule_md5s.values())) == 1,
      f"{rule_md5s}")
toc_md5s = {}
for v, d in VERSIONS.items():
    core = (d / "txt_to_epub_core.py").read_text(encoding="utf-8")
    import re
    m = re.search(r"TOC_RE = re\.compile\((.*?)\)", core, re.S)
    toc_md5s[v] = hashlib.md5(m.group(1).encode()).hexdigest() if m else "N/A"
check("三版本内置 TOC_RE 一致", len(set(toc_md5s.values())) == 1, f"{toc_md5s}")

print()
print("=" * 72)
print("C. 运行时")
print("-" * 72)
ver_out = subprocess.run([SYS_PY, "--version"], capture_output=True, text=True).stdout.strip()
check("系统 Python 可用", "3.12" in ver_out, ver_out)
for mod in ("ebooklib", "PIL", "psutil", "tkinterdnd2"):
    r = subprocess.run([SYS_PY, "-c", f"import {mod}"], capture_output=True, text=True)
    check(f"import {mod}", r.returncode == 0, r.stderr.strip()[:60] if r.returncode else "ok")

print()
print("=" * 72)
print("D. 探针计划验证（bench.py --dry）")
print("-" * 72)
r = subprocess.run([SYS_PY, str(ROOT / "probe" / "bench.py"), "--dry"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
plan_ok = "共 126 run" in r.stdout
check("计划生成 126 run", plan_ok, [ln for ln in r.stdout.splitlines() if "共" in ln][:1])
plan = json.load(open(ROOT / "probe" / "bench_plan.json", encoding="utf-8"))
formal = [x for x in plan if not x.get("warmup")]
first3 = [x["run_id"] for x in formal[:3]]
check("正式 run 已乱序（前 3 个不同版本/场景）",
      len(set(x.split("-")[0] + "-" + x.split("-")[1] for x in first3)) >= 2,
      str(first3))
check("warmup 恰 3 个且在最前",
      sum(1 for x in plan if x.get("warmup")) == 3 and all(x.get("warmup") for x in plan[:3]))

print()
print("=" * 72)
print("E. 环境")
print("-" * 72)
# 磁盘空间
import shutil
total, used, free = shutil.disk_usage(str(ROOT))
check("磁盘剩余空间充足", free > 5 * 1073741824, f"{free/1073741824:.1f} GB")
# 内存
try:
    import psutil
    mem = psutil.virtual_memory()
    check("可用内存 ≥ 1.5GB", mem.available > 1.5 * 1073741824, f"{mem.available/1073741824:.2f} GB")
    cpu = psutil.cpu_percent(interval=0.3)
    check("CPU 空闲率 ≥ 30%（非满载）", cpu < 70, f"当前占用 {cpu}%")
except Exception as e:
    check("内存/CPU 检查", False, str(e))
# 残留进程（用 psutil，规避 tasklist 的 GBK 编码问题）
try:
    import psutil as _ps
    procs = list(_ps.process_iter(["name", "cmdline"]))
    exe_resid = [p.info["name"] for p in procs
                 if p.info["name"] and "parse_txt_rust" in p.info["name"]]
    check("无 parse_txt_rust.exe 残留", not exe_resid, str(exe_resid))
    bench_resid = [p.info["name"] for p in procs
                   if p.info["cmdline"] and any("bench" in str(c) for c in p.info["cmdline"])]
    check("无 bench 调度/worker 进程残留", not bench_resid, str(bench_resid))
except Exception as e:
    check("残留进程检查", False, str(e))
# output 初始状态
jsonl = ROOT / "output" / "logs" / "bench_raw.jsonl"
check("bench_raw.jsonl 不存在（初始状态）", not jsonl.exists())
epub_any = list((ROOT / "output").glob("**/*.epub"))
check("output/ 无 EPUB 残留", not epub_any)

print()
print("=" * 72)
print(f"运行前检查结果：PASS={PASS}  FAIL={FAIL}")
if FAIL:
    print("⚠️ 存在 FAIL 项，请修复后再启动测试。")
else:
    print("✅ 全部通过，可以启动正式测试。")
print("=" * 72)
sys.exit(1 if FAIL else 0)
