"""encoding_detect.py — 编码择优（纯标准库、零依赖、与主体解耦）

范式：不做「探测」（用规则猜编码），让解码结果自己比赛（择优）。
中文网文主流编码就 utf-8 / gb18030 / big5 三种，全部解出来比「中文连贯性」，
最高者显著胜出即定案——天然规避旧「假高置信」病根
（gb18030 几乎把每个字节都映射成字形 → 永远 0 替换符 → 乱码也 conf=1.0）。

决策链
-------
1. BOM 优先：utf-8-sig 一眼定生死。
2. utf-8 严格自校验：能干净解码且有中文信号 → utf-8（utf-8 是自验证编码，干净≈真）。
   末尾截断（unexpected end of data）按 utf-8 处理。
   注意：utf-8 干净但无中文信号（纯英文元数据段）**不定案**，等更大样本
   ——否则「英文序在前 + gb18030 正文」会被 8KB 英文段误判成 utf-8。
3. 择优：utf-8(errors=replace) / gb18030 / big5 三路解码，比较全角标点密度。
   正确解码中文通顺、标点密集；错误解码是乱码、标点近零
   （实测 #14：正确 utf-8 7.69% vs 错读 gb18030 0.053% ≈ 104×）。
   最高密度需 ≥ 次高 3 倍 且 ≥ 0.05% 才定案，否则视为无中文信号、待续读。
4. 阶梯采样：8KB 定案多数；未定案续读 64KB→512KB（25万本批量无效读取低）。
5. 置信度诚实化：择优是确定性决策，不报「假 1.0」——严格证据 1.0，
   择优胜出 0.9，无中文信号模糊兜底 0.5。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 阶梯采样上限（绝大多书 8KB 内定案）；25万本批量时无效读取从「整读1MB」降到「8KB 为主」
_STAGES = (8 * 1024, 64 * 1024, 512 * 1024)

# 全角标点集合——正确解码时密集出现，乱码时几乎为零（中文连贯性判据）
_PUNCT = "，。！？、：；“”‘’（）《》【】…—"

# CJK 常见区段：基本汉字、扩展A、兼容汉字、CJK标点、全角ASCII
_CJK_RE = __import__("re").compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]"
)

_BOM_UTF8 = b"\xef\xbb\xbf"
_CONF_DECIDED = 0.9   # 择优显著胜出
_CONF_FALLBACK = 0.5  # 无中文信号模糊兜底
# 全角标点密度定案下限（0.05%）：低于此视为噪声而非中文信号
_MIN_PUNCT_DENSITY = 0.0005


def _has_cjk(text: str) -> bool:
    """样本里是否真的出现了中文信号——没有则不足以区分 utf-8 与 gbk。"""
    return bool(_CJK_RE.search(text))


def _punct_density(text: str) -> float:
    """全角标点密度（占比）——中文连贯性的简化代理指标。"""
    return sum(text.count(c) for c in _PUNCT) / max(1, len(text))


def _utf8_clean(raw: bytes) -> bool:
    """utf-8 是否可干净解码；仅末尾被截断（unexpected end of data）也判为干净。"""
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        return e.reason == "unexpected end of data"


def _pick(raw: bytes):
    """对一段字节样本择优定案；未定案（无中文信号）返回 (None, 0.5) 供上层续读。"""
    if raw[:3] == _BOM_UTF8:
        return "utf-8-sig", 1.0
    if _utf8_clean(raw):
        # utf-8 干净（含仅末尾被截断的采样）：安全解码检查中文信号。
        # errors="ignore" 跳过样本尾部被截断的半字符，避免「unexpected end of data」崩溃
        text = raw.decode("utf-8", errors="ignore")
        return ("utf-8", 1.0) if _has_cjk(text) else (None, _CONF_FALLBACK)
    # 三路解码比中文连贯性：正确编码标点密集，错误编码是乱码、标点近零
    dens = {
        enc: _punct_density(raw.decode(enc, errors="replace"))
        for enc in ("utf-8", "gb18030", "big5")
    }
    best_enc, best_d = max(dens.items(), key=lambda kv: kv[1])
    second_d = sorted(dens.values())[-2]
    if best_d >= second_d * 3 and best_d >= _MIN_PUNCT_DENSITY:
        return best_enc, _CONF_DECIDED
    return None, _CONF_FALLBACK


def detect_encoding_bytes(raw: bytes):
    """对一段字节样本判定 (编码, 置信度)。供单元测试与内部复用。

    bytes 级无法续读，无中文信号时给保守默认：utf-8 干净→utf-8，否则 gb18030。
    """
    enc, conf = _pick(raw)
    if enc is None:
        return ("utf-8", _CONF_FALLBACK) if _utf8_clean(raw) else ("gb18030", _CONF_FALLBACK)
    return enc, conf


def detect_encoding(path, max_stage: int = 512 * 1024):
    """读取文件、阶梯采样判定 (编码, 置信度)。

    默认最多读 512KB；绝大多数文件 8KB 内定案，25万本批量无效读取显著下降。
    未定案（无中文信号）逐级续读，末级仍无则按 utf-8 干净与否兜底。
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
            enc, conf = _pick(raw)
            if enc is None:
                if size == _STAGES[-1] or size >= max_stage:
                    enc = "utf-8" if _utf8_clean(raw) else "gb18030"
                    conf = _CONF_FALLBACK
                else:
                    continue  # 无中文信号且非末级：继续读更大样本
            return enc, conf
    return "gb18030", _CONF_FALLBACK


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
