"""全局调度器：进程池常驻 + 跨文件全局任务队列 + 动态收集。

这是理想模型的执行层：
- 进程池只起一次，全部任务复用（P7 启动税摊销到零）。
- 所有链的任务混合进一个全局队列；空闲 worker 自动取下一个，
  天然实现“跑完短链把长链切开塞空闲”（动态分配资源）。
- 进程数默认 = 核数（D-E），受内存税硬约束设经验上限（防止进程过多撑爆内存）。
- worker 是 txt_to_epub_core._parse_chunk（纯 Python，传引用任务单元，P5）。

动态再切分（M3，D5）与断点续跑（M3，D-D）在本版留扩展点，未启用。
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from txt_to_epub_core import _parse_chunk

from .chains import build_chains
from .finish import finish_file
from .preflight import preflight_scan, scan_files
from .tasks import Chain, FileMeta, build_all_tasks

# 进程内存税硬约束：每进程 ~30-50MB 底座，设经验上限避免撑爆内存。
# 真实场景（16-32 核、内存充足）远未触及；此处仅为安全护栏。
WORKER_HARD_CAP = int(os.environ.get("GS_WORKER_CAP", "32"))
WORKER_OVERRIDE = os.environ.get("GS_MAX_WORKERS")


def _meta_for(fm: FileMeta, output_dir: Path, user_title: str, author: str) -> dict:
    title = user_title if user_title else fm.path.stem
    out = output_dir / f"{fm.path.stem}.epub"
    return {
        "fid": fm.file_id,
        "path": fm.path,
        "title": title,
        "author": author,
        "out": out,
    }


def run(
    roots: list[str],
    output_dir: str,
    user_title: str = "",
    author: str = "Unknown",
    max_workers: int | None = None,
    recursive: bool = True,
    max_stage: int = 4,
) -> list:
    """端到端执行：预扫 → 分链 → 切分 → 全局调度 → 收尾。

    返回 ConversionResult 列表（与 gui 批量结果同构）。
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. 预扫 + 编码探测
    paths = scan_files(roots, recursive=recursive)
    if not paths:
        return []
    metas = preflight_scan(paths, max_stage=max_stage)

    # 2. 编码分链（链内 size 降序）
    chains: list[Chain] = build_chains(metas)

    # 3. 切分（填充 file_id / total_lines）
    tasks = build_all_tasks(chains)

    # 4. fid -> meta 映射
    fid_to_meta: dict[str, dict] = {}
    for chain in chains:
        for fm in chain.files:
            fid_to_meta[fm.file_id] = _meta_for(fm, out_dir, user_title, author)

    # 5. 进程池参数
    if max_workers is None:
        max_workers = int(WORKER_OVERRIDE) if WORKER_OVERRIDE else (os.cpu_count() or 4)
    max_workers = max(1, min(max_workers, WORKER_HARD_CAP))
    print(f"[调度] 检测到 {os.cpu_count() or 4} 逻辑核，启用 {max_workers} 进程（上限 {WORKER_HARD_CAP}）")

    # 6. 全局调度
    results: list = []
    file_results: dict[str, dict[int, str]] = {}
    completed: set[str] = set()

    # 单文件总块数缓存（用于判断是否整文件完成）
    total_chunks_of: dict[str, int] = {t.file_id: t.total_chunks for t in tasks}

    if not tasks:
        return []

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        future_map = {
            ex.submit(_parse_chunk, *t.submit_args()): (t.file_id, t.chunk_index)
            for t in tasks
        }
        for fut in as_completed(future_map):
            fid, idx = future_map[fut]
            try:
                temp_path = fut.result()
            except Exception as e:
                # 该块失败：标记文件失败，清理已收集块
                if fid not in completed:
                    for tp in file_results.get(fid, {}).values():
                        try:
                            os.remove(tp)
                        except OSError:
                            pass
                    file_results.pop(fid, None)
                    completed.add(fid)
                    results.append(__import__("txt_to_epub_core").ConversionResult(
                        success=False,
                        file_path=fid_to_meta.get(fid, {}).get("path"),
                        error=str(e),
                    ))
                continue

            file_results.setdefault(fid, {})[idx] = temp_path
            if len(file_results[fid]) == total_chunks_of[fid]:
                res = finish_file(fid_to_meta[fid], file_results[fid])
                results.append(res)
                completed.add(fid)
                file_results.pop(fid, None)

    # 任何未完成的文件记为失败
    for fid, meta in fid_to_meta.items():
        if fid not in completed:
            results.append(__import__("txt_to_epub_core").ConversionResult(
                success=False,
                file_path=meta["path"],
                error="调度异常中断（部分块未完成）",
            ))

    return results
