"""预扫：收集源文件并逐文件探测编码。

- 复用独立模块 encoding_detect.detect_encoding（阶梯采样 + 择优，纯 Python）。
- 输出 FileMeta 列表（路径 / 大小 / 编码），不含行数（行数在切分时按需在 worker 侧或切分函数内算）。
- 探测可并行：复用同一进程池（D-F 决策），但本模块只负责单文件探测，并行由调度器在 M2 串起。

注意：本模块不引入任何 Rust 路径；encoding_detect 是纯 Python 独立文件。
"""

from __future__ import annotations

from pathlib import Path

import encoding_detect

from .tasks import FileMeta

DEFAULT_EXTS = (".txt",)


def scan_files(roots: list[str], exts=DEFAULT_EXTS, recursive: bool = True) -> list[Path]:
    """从给定根目录收集源文件。

    roots: 目录或文件混传；文件直接收，目录按 recursive 递归。
    """
    out: list[Path] = []
    for r in roots:
        p = Path(r)
        if p.is_file():
            if p.suffix.lower() in exts:
                out.append(p)
            continue
        if p.is_dir():
            if recursive:
                for f in sorted(p.rglob("*")):
                    if f.is_file() and f.suffix.lower() in exts:
                        out.append(f)
            else:
                for f in sorted(p.iterdir()):
                    if f.is_file() and f.suffix.lower() in exts:
                        out.append(f)
    # 去重并保持稳定顺序
    seen = set()
    uniq = []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def detect_one(path: Path, max_stage: int = 4) -> str:
    """探测单个文件的编码（纯 Python 阶梯采样）。"""
    enc, _detail = encoding_detect.detect_encoding(str(path), max_stage=max_stage)
    return enc


def preflight_scan(paths: list[Path], max_stage: int = 4) -> list[FileMeta]:
    """逐文件预扫，返回 FileMeta 列表。

    编码探测失败的文件回退 utf-8（与 convert_single 容错一致），不丢弃。
    """
    metas: list[FileMeta] = []
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        try:
            enc = detect_one(p, max_stage=max_stage)
        except Exception:
            enc = "utf-8"
        metas.append(FileMeta(path=p, size=size, encoding=enc))
    return metas
