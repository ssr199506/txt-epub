"""encoding_detect.py — 中文 TXT 文件编码自动探测（独立模块，阶梯采样）

设计目标
--------
- 纯标准库、零依赖，可单独 ``import`` 使用。
- 为「载入文件后默认判断编码并展示、仅在判断错误时手动改」预留集成入口。
- 与主体 ``txt_to_epub_core.py`` 解耦：本文件不 import 主体任何内容。

阶梯采样（本模块核心优化，避免无谓整读大文件）
----------------------------------------------
不无脑整读：从极小头（8KB）起**顺序续读**（每次从上一位置继续，不回退、不重复读），
按「能严格解码的候选集合」判定：
  - 恰好 1 个候选能解        -> 定案，立即返回（绝大多数文件在此步完成）；
  - 0 个能解（开头是乱字节） -> 读更大样本重试；
  - ≥2 个能解（如开头全 ASCII，utf-8/gbk 都能解）-> 暂存，继续读找中文信号。
gb18030 是 gbk 超集，两者同时能解时归并为一，避免 gbk 文件被误判「歧义」而多读。
批量场景（25万本）下，99% 文件在首阶段 8KB 即定案，仅极少见歧义/全 ASCII 头才往下读。

API
---
- ``detect_encoding(path, stages=...)        -> (encoding, confidence)``
- ``detect_encoding_bytes(raw, candidates=...) -> (encoding, confidence)``
"""

from pathlib import Path
from typing import Tuple, Set

# 候选编码优先级：先试最严格的 utf-8，再退化到中文常见编码。
# gb18030 是 gbk 的超集（覆盖更多生僻字），故排在 gbk 前；gbk 仅作兜底。
_CANDIDATES: Tuple[str, ...] = ("utf-8", "gb18030", "big5", "gbk")

# 阶梯采样尺寸：先读极小头，判断不出再扩大。
# 绝大多数文件（utf-8 或含中文的 gbk）在前 8KB 内就能定案；
# 仅「开头全 ASCII 无信号」或极罕见歧义文件才需要往下读。
_STAGES: Tuple[int, ...] = (8 * 1024, 64 * 1024, 512 * 1024)


def detect_encoding_bytes(raw: bytes, candidates: Tuple[str, ...] = _CANDIDATES) -> Tuple[str, float]:
    """对一段字节探测编码，返回 ``(encoding, confidence)``。

    严格解码：能解码的候选即视为可行，confidence 恒为 1.0（严格解码不产生替换符）。
    若所有候选都失败，回退 ``gbk`` 并给 0.0。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", 1.0
    for enc in candidates:
        try:
            raw.decode(enc)
            return enc, 1.0
        except UnicodeDecodeError:
            continue
    return "gbk", 0.0


def _strict_ok(enc: str, raw: bytes) -> bool:
    """该编码能否严格解码这段字节（不产生 UnicodeDecodeError）。"""
    try:
        raw.decode(enc)
        return True
    except UnicodeDecodeError:
        return False


def detect_encoding(path, stages: Tuple[int, ...] = _STAGES) -> Tuple[str, float]:
    """对文件阶梯式探测编码，避免无谓整读大文件。

    从最小的 stage 起顺序续读（每次从上一位置继续，不回退、不重复读），
    计算「能严格解码的候选集合」来判定：
      - 恰好 1 个候选能解        -> 定案，立即返回；
      - 0 个能解（开头是乱字节） -> 读更大样本重试；
      - ≥2 个能解（如开头全 ASCII）-> 暂存，继续读更大样本找中文信号。
    gb18030 是 gbk 超集，两者同时能解时归并（去掉 gbk），避免 gbk 文件被误判歧义。
    走到最大 stage 仍歧义时，返回候选顺序最靠前者（utf-8 优先）作为保守默认。
    """
    fallback: Tuple[str, float] = ("gbk", 0.0)
    with open(Path(path), "rb") as f:
        for size in stages:
            raw = f.read(size)
            if not raw:
                break
            if raw.startswith(b"\xef\xbb\xbf"):
                return "utf-8-sig", 1.0
            ok: Set[str] = {c for c in _CANDIDATES if _strict_ok(c, raw)}
            if "gb18030" in ok:
                ok.discard("gbk")  # gb18030 是 gbk 超集，gbk 能解不提供新信息
            if len(ok) == 1:
                return next(iter(ok)), 1.0
            if ok:
                # 多候选能解：暂存候选顺序最靠前者，继续读更大样本
                fallback = (min(ok, key=lambda c: _CANDIDATES.index(c)), 1.0)
            # len(ok) == 0：读更大样本重试
    return fallback


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        enc, conf = detect_encoding(p)
        print(f"{p}\t->\t{enc} (confidence={conf})")
