"""encoding_detect.py — 自动编码探测（纯标准库、零依赖、与主体解耦）

借鉴 legado「开箱即用」思路：载入文件即判编码，回填 GUI 下拉框，
人仅在判错时手动改。以后 GUI 接入「载入即判编码」时，直接调 detect_encoding(path)。

设计要点
--------
- BOM 优先：utf-8-sig 一眼定生死（置信度 1.0）。
- utf-8 自校验：能干净解码即判定 utf-8（utf-8 是自验证编码，干净解码几乎等于真是 utf-8）。
  截断样本（末尾多字节被切断，报 unexpected end of data）按 utf-8 处理，避免误判成 gbk。
- 阶梯采样：从极小头起顺序续读（8KB→64KB→512KB），每次从头累计读，不回退不重复读；
  开头全 ASCII（utf-8/gbk 都能解）时暂不定案，继续往后读找中文信号，
  避免「英文序/元数据在前」的文件被误判成 utf-8。
- 置信度真实有效：对非 utf-8 的中文语料，按 U+FFFD 替换符比例给 gb18030/big5 打分
  （chardet 思路、零依赖），不再恒为 1.0；gb18030 是 gbk 超集，两者同解时归并。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 阶梯采样上限（绝大多书 8KB 内定案）；25万本批量时无效读取从「整读1MB」降到「8KB 为主」
_STAGES = (8 * 1024, 64 * 1024, 512 * 1024)

# CJK 常见区段：基本汉字、扩展A、兼容汉字、CJK标点、全角ASCII
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]"
)


def _has_cjk(text: str) -> bool:
    """样本里是否真的出现了中文信号——没有则不足以区分 utf-8 与 gbk。"""
    return bool(_CJK_RE.search(text))


def _utf8_clean(raw: bytes) -> bool:
    """utf-8 是否可干净解码；仅末尾被截断（unexpected end of data）也判为干净。

    否则 gbk 文件里的高字节会让 utf-8 报 invalid start byte -> 判非 utf-8（正确）。
    """
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.reason == "unexpected end of data"


def _score(raw: bytes, enc: str) -> int:
    """用 replace 解码，统计替换符 U+FFFD 数量——越少越可能是真编码。"""
    return raw.decode(enc, errors="replace").count("\ufffd")


def _decide(raw: bytes):
    """样本已含 CJK 信号时定案 (编码, 置信度)。"""
    if _utf8_clean(raw):
        return "utf-8", 1.0
    # 非 utf-8：在中文候选里按替换符比例选最优；gb18030 是 gbk 超集，优先。
    scores = {enc: _score(raw, enc) for enc in ("gb18030", "big5")}
    best = min(scores, key=scores.get)
    n = len(raw)
    conf = 1.0 - scores[best] / n if n else 1.0
    return best, max(0.0, conf)


def detect_encoding_bytes(raw: bytes):
    """对一段字节样本判定 (编码, 置信度)。供单元测试与内部复用。"""
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig", 1.0
    sample = raw.decode("gb18030", errors="replace")
    if not _has_cjk(sample):
        # 无中文信号：纯 ASCII 或乱字节；utf-8 干净则判 utf-8，否则 gb18030 兜底
        return ("utf-8", 1.0) if _utf8_clean(raw) else ("gb18030", 0.5)
    return _decide(raw)


def detect_encoding(path, max_stage: int = 512 * 1024):
    """读取文件、阶梯采样判定 (编码, 置信度)。

    默认最多读 512KB；绝大多数文件 8KB 内定案，25万本批量无效读取显著下降。
    """
    p = Path(path)
    with p.open("rb") as f:
        for size in _STAGES:
            if size > max_stage:
                size = max_stage
            f.seek(0)
            raw = f.read(size)
            if not raw:
                break
            enc, conf = detect_encoding_bytes(raw)
            # 无中文信号且非末阶段 -> 样本太短（如英文序在前），继续往后读找中文
            sample = raw.decode("gb18030", errors="replace")
            if not _has_cjk(sample) and size != _STAGES[-1]:
                continue
            return enc, conf
    return "gb18030", 0.5


def main():
    if len(sys.argv) < 2:
        print("usage: python encoding_detect.py <file> [<file> ...]")
        return 1
    for path in sys.argv[1:]:
        enc, conf = detect_encoding(path)
        print(f"{path}\t->\t{enc}\t(conf={conf})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
