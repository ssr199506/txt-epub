#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 C 必要性 profile：拆解 serve 全链路耗时构成。

三段计时（互不干扰，各测各的）：
1. Python 解码耗时：detect + gb18030/utf-8 → str → UTF-8 bytes（解码红线，PyO3 也省不掉）
2. 帧协议全链路：写帧 → Rust 解析+序列化 → 读回（含 IPC/序列化，PyO3 可省的部分）
3. Rust 插桩：stderr 输出 parse_cpu_ms / ser_cpu_ms / serve_wall_ms

用法：profile_serve.py <语料目录> [--exe 插桩版exe路径] [--limit N]
"""
import sys, os, glob, time, json, struct, argparse, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import txt_to_epub_core as core


def detect_encoding(path):
    with open(path, "rb") as f:
        head = f.read(200_000)
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--exe", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "rust_profile", "target", "release", "parse_txt_rust.exe"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.corpus, "*.txt")))
    if args.limit:
        files = files[: args.limit]
    print(f"[枚举] {len(files)} 个 txt")

    pat = core._pattern_to_str(None)
    total_bytes_in = 0

    # 段1：纯 Python 解码耗时（不调 Rust）
    t0 = time.perf_counter()
    decoded_list = []
    for f in files:
        enc = detect_encoding(f)
        data = core._decode_to_utf8_bytes(f, enc)
        decoded_list.append((f, enc, data))
        total_bytes_in += len(data)
    t_decode = time.perf_counter() - t0
    print(f"[段1] Python 解码: {t_decode:.3f}s  ({len(files)} 文件, 合计 UTF-8 {total_bytes_in/1e6:.1f} MB)")

    # 段2：帧协议全链路（插桩版 serve，读 stderr 拿 Rust 内部计时）
    t1 = time.perf_counter()
    proc = subprocess.Popen(
        [args.exe, "--serve", "--pattern", pat],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for f, enc, data in decoded_list:
        proc.stdin.write(struct.pack(">I", len(data)))
        proc.stdin.write(data)
    proc.stdin.close()
    out_lines = []
    for line in proc.stdout:
        out_lines.append(line)
    proc.stdout.close()
    err = proc.stderr.read().decode("utf-8", "replace")
    proc.stderr.close()
    proc.wait()
    t_serve = time.perf_counter() - t1
    print(f"[段2] 帧协议全链路(解码后): {t_serve:.3f}s  读回 {len(out_lines)} 行 JSON")

    # 段3：Rust 插桩统计
    prof = {}
    for kv in err.strip().split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            prof[k] = float(v)
    print(f"[段3] Rust 内部: {prof}")
    parse_ms = prof.get("parse_cpu_ms", 0)
    ser_ms = prof.get("ser_cpu_ms", 0)
    wall_ms = prof.get("serve_wall_ms", 0)
    total_cpu = parse_ms + ser_ms
    print()
    print("===== 结论 =====")
    print(f"全链路(含解码): {(t_decode + t_serve) * 1000:.0f} ms")
    print(f"  其中 Python 解码: {t_decode * 1000:.0f} ms ({t_decode / (t_decode + t_serve) * 100:.1f}%)")
    print(f"  帧协议链路(解码后): {t_serve * 1000:.0f} ms")
    print(f"Rust parse CPU 总和: {parse_ms:.0f} ms (多核并行, 非墙钟)")
    print(f"Rust JSON 序列化 CPU: {ser_ms:.0f} ms ({ser_ms / max(total_cpu, 1) * 100:.1f}% of Rust CPU)")
    print(f"Rust 处理端墙钟: {wall_ms:.0f} ms (含等待喂帧)")
    ipc_overhead = t_serve * 1000 - wall_ms
    print(f"IPC + 反序列化 + 调度: {ipc_overhead:.0f} ms ({ipc_overhead / (t_serve * 1000) * 100:.1f}% of 链路)")
    print(f"→ PyO3 理论可省上限: IPC {ipc_overhead:.0f}ms + JSON 序列化 {ser_ms:.0f}ms ≈ {ipc_overhead + ser_ms:.0f} ms")


if __name__ == "__main__":
    main()
