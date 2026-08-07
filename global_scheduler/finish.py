"""单文件收尾：合并各块结果 → 调纯 Python build_epub → 写 EPUB。

移植自 v1.0.0 gui_2._merge_chunks / _finish_file，逻辑不变，只改为包内函数。
- 切口修正（overflow 拼回前一段末尾）沿用原实现，保证与 convert_single 逐章一致（parity 红线 P6）。
- 不引入任何 Rust 路径。
"""

from __future__ import annotations

import os
import pickle
from pathlib import Path

from txt_to_epub_core import build_epub, ConversionResult
import ebooklib.epub as _epub


def merge_chunks(temp_paths: list[str]) -> list:
    """合并各块解析结果：overflow 拼接到前一段末尾。

    temp_paths 须按 chunk_index 升序。每块 pickle 存 (overflow: str, chapters: list)。
    返回扁平章节列表 [(title, content), ...]，可直接喂 build_epub。
    """
    all_chapters: list = []
    for i, temp_path in enumerate(temp_paths):
        try:
            with open(temp_path, "rb") as f:
                overflow, chapters = pickle.load(f)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        if i == 0:
            all_chapters.extend(chapters)
        else:
            if overflow and all_chapters:
                last_title, last_content = all_chapters[-1]
                all_chapters[-1] = (last_title, last_content + overflow)
            all_chapters.extend(chapters)
    return all_chapters


def finish_file(meta: dict, chunk_results: dict[int, str]) -> ConversionResult:
    """完成单个文件：合并 → build_epub → 写盘，返回 ConversionResult。

    meta: {"path", "title", "author", "out"}
    chunk_results: {chunk_index: temp_path}
    """
    sorted_paths = [chunk_results[i] for i in sorted(chunk_results)]
    try:
        chapters = merge_chunks(sorted_paths)
        book = build_epub(chapters, meta["title"], meta.get("author", "Unknown"))
        _epub.write_epub(meta["out"], book)
        return ConversionResult(
            success=True,
            file_path=meta["path"],
            output_path=meta["out"],
            chapter_count=len(chapters),
        )
    except Exception as e:
        # 清理已收集临时文件，防止泄漏
        for tp in chunk_results.values():
            try:
                os.remove(tp)
            except OSError:
                pass
        return ConversionResult(
            success=False,
            file_path=meta["path"],
            error=str(e),
        )
