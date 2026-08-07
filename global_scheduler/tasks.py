"""任务模型与切分。

纯 Python，零 Rust 依赖。行数统计复用 txt_to_epub_core._count_lines；
分段不用 v1.0.0 的固定查表（_CHUNK_CONFIG），改按「甜点区」计算：
单 worker 一次持有约 SWEET_SPOT_BYTES 正文，段数 = ceil(文件大小 / 甜点区)，
大文件自然多段，并行度由全局队列按空闲 worker 动态分配。

设计红线（见 plan/global_scheduler_plan.md）：
- P5 传引用不传数据：TaskDescriptor 只携带路径+行号+编码等标量，正文由 worker 自读磁盘。
- D-A 任务全局按 size 降序生成（大文件先入队，避免长尾）。
- 单段只依赖紧邻数据（无环性），任意切分方式拼接都安全。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from txt_to_epub_core import _count_lines, TOC_RE

# ---- 甜点区分段（取代 v1.0.0 的固定查表 _CHUNK_CONFIG）----
# 单 worker 一次持有的理想正文量：太大则单段内存峰值高，太小则任务粒度碎、
# 合并开销占比上升。段数 = ceil(文件大小 / 甜点区)，大文件自然多段、无写死上限。
SWEET_SPOT_BYTES = int(os.environ.get("GS_SWEET_SPOT_MB", "256")) * 1024 * 1024


def get_optimal_chunks(file_size: int) -> int:
    """按甜点区计算分段数：至少 1 段；小文件不切，大文件按 256MB/段 递增。"""
    if file_size <= 0:
        return 1
    return max(1, (file_size + SWEET_SPOT_BYTES - 1) // SWEET_SPOT_BYTES)


@dataclass
class FileMeta:
    """单个源文件的预扫元数据。"""
    path: Path
    size: int
    encoding: str
    total_lines: int = 0          # 切分时填充（_count_lines）
    file_id: str = ""             # 切分时分配（uuid 前缀）


@dataclass
class Chain:
    """同编码文件组成的一条逻辑链。

    文件按 size 降序排列（D-A），使大任务先进入全局队列，缩短长尾。
    """
    encoding: str
    files: list[FileMeta] = field(default_factory=list)


@dataclass
class TaskDescriptor:
    """一个切分任务单元——纯标量，跨进程只传它，不传正文（P5）。

    worker 收到后自行 open(file_path) 读取 [start_line, end_line) 区间。
    字段顺序与 _parse_chunk(file_path, start_line, end_line, encoding,
    pattern_str, chunk_index, total_chunks) 完全对应，可直接星号展开提交。
    """
    file_path: str
    start_line: int
    end_line: int
    encoding: str
    pattern_str: str
    chunk_index: int
    total_chunks: int
    file_id: str

    def submit_args(self):
        """返回可直接传给 ProcessPoolExecutor.submit(_parse_chunk, *args) 的 7 元组。"""
        return (
            self.file_path, self.start_line, self.end_line,
            self.encoding, self.pattern_str,
            self.chunk_index, self.total_chunks,
        )


def split_file(fm: FileMeta, pattern_str: str | None = None) -> list[TaskDescriptor]:
    """把一个文件切成若干 TaskDescriptor。

    切分依据：文件大小 → 甜点区分段数；行数 → 均匀行边界。
    返回空列表表示文件无内容（total_lines == 0）。
    """
    if not pattern_str:
        pattern_str = TOC_RE.pattern
    total = _count_lines(str(fm.path), fm.encoding)
    fm.total_lines = total
    if total == 0:
        return []
    num = get_optimal_chunks(fm.size)
    if fm.file_id == "":
        import uuid
        fm.file_id = uuid.uuid4().hex[:8]
    chunk_sz = total // num
    tasks: list[TaskDescriptor] = []
    for i in range(num):
        start = i * chunk_sz
        end = (i + 1) * chunk_sz if i < num - 1 else total
        tasks.append(TaskDescriptor(
            file_path=str(fm.path),
            start_line=start,
            end_line=end,
            encoding=fm.encoding,
            pattern_str=pattern_str,
            chunk_index=i,
            total_chunks=num,
            file_id=fm.file_id,
        ))
    return tasks


def build_all_tasks(chains: list[Chain], pattern_str: str | None = None) -> list[TaskDescriptor]:
    """把全部链的任务展开成一个全局任务列表。

    顺序：链内按 size 降序（已在 Chain.files 中排好），链间按编码依次追加。
    全局列表交调度器统一入池——空闲 worker 自动取下一个，天然实现
    “跑完短链把长链切开塞空闲”（动态分配资源）。
    """
    tasks: list[TaskDescriptor] = []
    for chain in chains:
        for fm in chain.files:
            tasks.extend(split_file(fm, pattern_str))
    return tasks
