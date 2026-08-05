"""encoding_detect.py — 中文 TXT 文件编码自动探测（独立模块）

设计目标
--------
- 纯标准库、零依赖，可单独 ``import`` 使用。
- 为「载入文件后默认判断编码并展示、仅在判断错误时手动改」预留集成入口：
  以后 GUI 在载入文件时调用 :func:`detect_encoding` 即可拿到推荐编码，回填到
  编码下拉框，用户无需每次手填。这正是借鉴 legado「开箱即用」的思路。
- 与主体 ``txt_to_epub_core.py`` 解耦：本文件不 import 主体任何内容，可独立测试、
  独立移植。

API
---
- ``detect_encoding(path, sample_size=1_000_000) -> (encoding, confidence)``
- ``detect_encoding_bytes(raw, candidates=...)      -> (encoding, confidence)``

``confidence`` 为 0~1 的浮点，越接近 1 越可信；所有候选都失败时回退 ``gbk`` 并给 0.0。
"""

from pathlib import Path
from typing import Tuple

# 候选编码优先级：先试最严格的 utf-8，再退化到中文常见编码。
# gb18030 是 gbk 的超集（覆盖更多生僻字），故排在 gbk 前。
_CANDIDATES: Tuple[str, ...] = ("utf-8", "gb18030", "big5", "gbk")


def detect_encoding_bytes(raw: bytes, candidates: Tuple[str, ...] = _CANDIDATES) -> Tuple[str, float]:
    """对一段字节探测编码，返回 ``(encoding, confidence)``。

    confidence = 1 - (替换字符数 / 文本长度)，越接近 1 越可信。
    若所有候选都失败，回退 ``gbk``（中文小说最常见）并给 0.0。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", 1.0
    best_enc, best_score = "gbk", 0.0
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        n = len(text) or 1
        bad = text.count("\ufffd")
        score = 1.0 - bad / n
        if score > best_score:
            best_score, best_enc = score, enc
        # 完美命中直接返回，省一次解码
        if score >= 0.999:
            return enc, 1.0
    return best_enc, round(best_score, 3)


def detect_encoding(path, sample_size: int = 1_000_000) -> Tuple[str, float]:
    """对文件探测编码。

    只读前 ``sample_size`` 字节（对中文小说编码判定已足够，且避免大文件整读）。
    ``path`` 可为 ``str`` 或 ``Path``。
    """
    raw = Path(path).read_bytes()[:sample_size]
    return detect_encoding_bytes(raw)


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        enc, conf = detect_encoding(p)
        print(f"{p}\t->\t{enc} (confidence={conf})")
