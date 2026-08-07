# -*- coding: utf-8 -*-
"""bench_worker.py — TXT→EPUB 三版本性能测试 · 单 run 执行器。

用法：
  python bench_worker.py --version v1|v2|v3 --version-dir <abs>
      --sample <abs> --encoding utf-8|gb18030 --scene parse|full|batch|detect
      [--mode sub|pipe|serve] [--out <abs>] [--repeat-idx N]

约定：
- 独立进程执行（subprocess 隔离，杜绝 import 污染）。
- 结果以 stdout 最后一行 `__RESULT__ {json}` 输出；其余日志走 stderr。
- 所有文件只写 --out 目录内，不落任何散落文件。
"""
import argparse
import datetime
import importlib.util
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

SAMPLE_IDS = ["01", "02", "03", "04", "05", "06"]


def log(msg):
    print(f"[worker] {msg}", file=sys.stderr, flush=True)


def load_core(version_dir):
    """从版本目录独立加载 txt_to_epub_core（含同目录兄弟模块）。"""
    vpath = str(Path(version_dir).resolve())
    core_path = os.path.join(vpath, "txt_to_epub_core.py")
    sys.path.insert(0, vpath)
    try:
        spec = importlib.util.spec_from_file_location("bench_core", core_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def load_encoding_detect(version_dir):
    vpath = str(Path(version_dir).resolve())
    sys.path.insert(0, vpath)
    try:
        spec = importlib.util.spec_from_file_location("bench_encdet", os.path.join(vpath, "encoding_detect.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


class MemSampler:
    """后台采样线程：自身 RSS 峰值 + parse_txt_rust.exe 子进程统计。"""

    def __init__(self, pid):
        import psutil
        self.psutil = psutil
        self.proc = psutil.Process(pid)
        self.peak_rss = 0
        self.exe_pids = set()
        self.exe_peak_rss = 0
        self._stop = False
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop:
            try:
                self.peak_rss = max(self.peak_rss, self.proc.memory_info().rss)
                for p in self.psutil.process_iter(["name", "pid"]):
                    try:
                        if p.info["name"] and "parse_txt_rust" in p.info["name"]:
                            self.exe_pids.add(p.info["pid"])
                            try:
                                self.exe_peak_rss = max(self.exe_peak_rss, p.memory_info().rss)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.005)

    def stop(self):
        self._stop = True
        if self._t:
            self._t.join(timeout=1.0)
        return self.peak_rss, len(self.exe_pids), self.exe_peak_rss


def env_baseline():
    import psutil
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    return {"cpu_pct": round(cpu, 1), "mem_free_gb": round(mem.available / 1073741824, 2)}


def run_parse(core, ver, sample, encoding):
    """S1 切章：统一预读解码（decode 阶段三版本同码），parse 阶段各版本真实内核。"""
    import time
    t0 = time.perf_counter()
    with open(sample, "rb") as f:
        data = f.read()
    text = data.decode(encoding, errors="ignore")
    decode_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    if ver == "v1":
        # v1 走原生自解码路径（与 S2/真实使用一致）。注意：若传 text_str=text
        # （errors=ignore 整文件预解码串），v1 的 splitlines 解析会因丢字节破坏
        # 行边界而少切 1~3 章——非真实切章能力差异。故 S1 对 v1 不传 text_str，
        # 使其自解码（与 convert_single 一致）；decode 开销见上方 decode_s 单列。
        chapters = core.parse_txt(Path(sample), encoding)
        n = len(chapters)
        titles = [chapters[i][0] for i in range(min(5, n))]
        temp = None
    else:
        temp, index = core.parse_txt_index(text_str=text)
        n = len(index)
        titles = [index[i]["title"] for i in range(min(5, n))]
        if ver == "v2" and temp is not None:
            try:
                p = Path(str(temp))
                if p.exists():
                    p.unlink()
            except Exception:
                pass
    parse_s = time.perf_counter() - t1
    return {"decode_s": round(decode_s, 4), "parse_s": round(parse_s, 4),
            "chapter_count": n, "first_titles": titles}


def run_full(core, ver, sample, encoding, out_dir, repeat_idx):
    """S2 完整转换：convert_single 全链路（真实体验）。"""
    import time
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(sample).stem
    out_epub = out_dir / f"{stem}_r{repeat_idx}.epub"
    t0 = time.perf_counter()
    result = core.convert_single(
        Path(sample), out_epub, encoding,
        book_title=Path(sample).stem, author="",
    )
    wall = time.perf_counter() - t0
    if not result.success:
        return {"ok": False, "error": str(result.error), "wall_s": round(wall, 4),
                "chapter_count": 0, "output_size": 0, "epub_valid": False}
    # EPUB 完整性校验
    epub_valid = False
    try:
        with zipfile.ZipFile(out_epub) as zf:
            names = zf.namelist()
            epub_valid = ("mimetype" in names) and any(n.startswith("META-INF/") for n in names)
    except Exception:
        epub_valid = False
    size = out_epub.stat().st_size if out_epub.exists() else 0
    # 测完即删 EPUB，避免产物堆积（仅保留 size/valid 统计）
    try:
        if out_epub.exists():
            out_epub.unlink()
    except Exception:
        pass
    return {"ok": True, "wall_s": round(wall, 4), "chapter_count": result.chapter_count,
            "output_size": size, "epub_valid": epub_valid, "epub_path": str(out_epub)}


def run_batch(core, ver, mode, files, encoding, sample_ids):
    """S3 批量：sub=逐本子进程 / pipe=逐本管道 / serve=常驻进程。"""
    import time
    t0 = time.perf_counter()
    per_book = {}
    if mode == "serve":
        assert ver == "v3"
        res = core.batch_parse_index(files, encoding)
        for (fp, idx), sid in zip(res, sample_ids):
            per_book[sid] = {"chapter_count": len(idx)}
    else:
        for fp, sid in zip(files, sample_ids):
            t1 = time.perf_counter()
            if ver == "v1":
                chapters = core.parse_txt(Path(fp), encoding)
                per_book[sid] = {"chapter_count": len(chapters)}
            elif mode == "sub":
                temp, index = core.parse_txt_index(fp, encoding)
                per_book[sid] = {"chapter_count": len(index)}
                if temp is not None:
                    try:
                        p = Path(str(temp))
                        if p.exists():
                            p.unlink()
                    except Exception:
                        pass
            else:  # pipe
                temp, index = core.parse_txt_index(fp, encoding)
                per_book[sid] = {"chapter_count": len(index)}
            per_book[sid]["book_s"] = round(time.perf_counter() - t1, 4)
    wall = time.perf_counter() - t0
    return {"wall_s": round(wall, 4), "per_book": per_book}


def run_detect(encmod, sample):
    """A1 附加观测：v3 encoding_detect 自动检测耗时。"""
    import time
    t0 = time.perf_counter()
    enc = encmod.detect_encoding(sample)
    dt = time.perf_counter() - t0
    return {"detect_s": round(dt, 4), "detected": enc}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--version-dir", required=True)
    ap.add_argument("--sample", default="")
    ap.add_argument("--encoding", default="utf-8")
    ap.add_argument("--scene", required=True, choices=["parse", "full", "batch", "detect"])
    ap.add_argument("--mode", default="", choices=["", "sub", "pipe", "serve"])
    ap.add_argument("--out", default="")
    ap.add_argument("--repeat-idx", default="1")
    ap.add_argument("--run-id", default="")
    args = ap.parse_args()

    t_start = datetime.datetime.now().isoformat(timespec="seconds")
    base = env_baseline()
    sampler = MemSampler(os.getpid())
    sampler.start()

    result = {
        "run_id": args.run_id,
        "ts_start": t_start,
        "scene": args.scene,
        "version": args.version,
        "mode": args.mode or None,
        "sample": Path(args.sample).stem if args.sample else None,
        "sample_id": (Path(args.sample).stem.split("_")[0] if args.sample else None),
        "repeat_idx": int(args.repeat_idx),
        "ok": True,
        "error": None,
        "env_baseline": base,
    }

    try:
        if args.scene == "detect":
            encmod = load_encoding_detect(args.version_dir)
            r = run_detect(encmod, args.sample)
            result.update(r)
        else:
            core = load_core(args.version_dir)
            if args.scene == "parse":
                r = run_parse(core, args.version, args.sample, args.encoding)
                result.update(r)
                result["wall_s"] = round(r["decode_s"] + r["parse_s"], 4)
            elif args.scene == "full":
                r = run_full(core, args.version, args.sample, args.encoding, args.out, args.repeat_idx)
                result.update(r)
                result.setdefault("wall_s", r.get("wall_s"))
            elif args.scene == "batch":
                # S3：批量输入为 5 本 UTF-8（sample 参数传分号分隔的路径列表）
                paths = [p for p in args.sample.split("|") if p]
                sids = [Path(p).stem.split("_")[0] for p in paths]
                r = run_batch(core, args.version, args.mode, paths, "utf-8", sids)
                result.update(r)
                result["batch_n"] = len(paths)
                result["sample_ids"] = sids
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"

    peak_rss, exe_count, exe_peak = sampler.stop()
    result["peak_rss_mb"] = round(peak_rss / 1048576, 1)
    result["exe_proc_count"] = exe_count
    result["exe_peak_rss_mb"] = round(exe_peak / 1048576, 1) if exe_peak else 0
    result["ts_end"] = datetime.datetime.now().isoformat(timespec="seconds")

    print("__RESULT__ " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
