"""命令行入口：纯 Python 全局调度批量 TXT→EPUB。

用法示例：
    python -m global_scheduler.cli -i D:/books/txt -o D:/books/epub -t "合集" -a "佚名"

设计：GUI 零改动（D-B 决策），本 CLI 是独立入口。所有逻辑在 global_scheduler 包内，
worker 为纯 Python 的 txt_to_epub_core._parse_chunk。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# 确保项目根（txt_to_epub_core 所在目录）在 sys.path，便于直接运行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from global_scheduler.scheduler import run  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="纯 Python 全局调度批量 TXT→EPUB（编码分链 + 进程池常驻）"
    )
    parser.add_argument("-i", "--input", nargs="+", required=True,
                        help="源目录或文件（可多个）")
    parser.add_argument("-o", "--output", required=True, help="输出目录")
    parser.add_argument("-t", "--title", default="", help="统一书名（留空则每文件用文件名）")
    parser.add_argument("-a", "--author", default="Unknown", help="作者")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help="进程数（默认=核数，可被 GS_MAX_WORKERS 覆盖）")
    parser.add_argument("--no-recursive", action="store_true",
                        help="不递归子目录")
    parser.add_argument("--max-stage", type=int, default=4,
                        help="编码探测阶梯采样轮数")
    args = parser.parse_args(argv)

    t0 = time.time()
    results = run(
        roots=args.input,
        output_dir=args.output,
        user_title=args.title,
        author=args.author,
        max_workers=args.workers,
        recursive=not args.no_recursive,
        max_stage=args.max_stage,
    )
    dt = time.time() - t0

    ok = [r for r in results if getattr(r, "success", False)]
    fail = [r for r in results if not getattr(r, "success", False)]
    print(f"\n=== 完成：{len(ok)} 成功 / {len(fail)} 失败，用时 {dt:.1f}s ===")
    for r in ok:
        ch = getattr(r, "chapter_count", "?")
        print(f"  ✅ {Path_like(r.file_path).name}  ({ch} 章) -> {Path_like(r.output_path).name}")
    for r in fail:
        print(f"  ❌ {Path_like(r.file_path).name}: {r.error}")
    return 0 if not fail else 1


def Path_like(p):
    from pathlib import Path
    return Path(p) if p else Path("<unknown>")


if __name__ == "__main__":
    raise SystemExit(main())
