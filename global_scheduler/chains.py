"""编码分链：把预扫结果按编码聚成若干条逻辑链。

理想模型：同编码文件拼成一条链；四种编码 = 四条长短不一的链。
每条链内文件按 size 降序（D-A），使大任务先入全局队列。

模块本身不并行、不读正文，纯数据结构变换（无环、确定性）。
"""

from __future__ import annotations

from collections import defaultdict

from .tasks import Chain, FileMeta


def build_chains(files: list[FileMeta]) -> list[Chain]:
    """按编码分组，链内按 size 降序。"""
    by_enc: dict[str, list[FileMeta]] = defaultdict(list)
    for fm in files:
        by_enc[fm.encoding].append(fm)
    chains: list[Chain] = []
    for enc, fs in by_enc.items():
        fs_sorted = sorted(fs, key=lambda x: x.size, reverse=True)
        chains.append(Chain(encoding=enc, files=fs_sorted))
    # 链间按“总字节量”降序，便于报告与排障（不影响执行，执行是全局一池）
    chains.sort(key=lambda c: sum(f.size for f in c.files), reverse=True)
    return chains


def summarize(chains: list[Chain]) -> str:
    """生成人类可读的分链摘要。"""
    lines = [f"共 {len(chains)} 条编码链："]
    for c in chains:
        total = sum(f.size for f in c.files)
        total_mb = total / (1024 * 1024)
        lines.append(
            f"  [{c.encoding}] {len(c.files)} 文件, 总 {total_mb:.1f} MB"
        )
    return "\n".join(lines)
