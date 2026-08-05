"""convert_cli.py — 无 GUI 的 TXT→EPUB 驱动插件（低耦合、可独立运行）

借鉴 legado 的对外接口思路：不打开界面，一条命令自动完成
「探测编码 → 解析章节 → 多卷拆分写出」。

耦合策略
--------
- 只 import 主体模块 ``txt_to_epub_core`` 的**公共 API**
  （``parse_txt`` / ``build_epub`` / ``volume_ranges`` / ``epub``），
  **不修改** 主体文件，保持低耦合。
- 多卷写出逻辑 ``write_volumes`` 收口在本文档；GUI 已有等价私有方法
  ``_write_volumes``，二者独立、互不污染（若日后想去重，可让 GUI 改调本函数）。
- 编码探测走独立模块 ``encoding_detect``，与本项目其它部分零耦合。

用法
----
    python convert_cli.py 小说.txt
    python convert_cli.py 小说.txt --out ./out --title "书名" --author "作者" --max-per-volume 500
    python convert_cli.py 小说.txt --encoding gbk          # 跳过自动探测，手动指定
    python convert_cli.py 小说.txt --cover cover.jpg       # 指定封面

高层 API
-------
    from convert_cli import convert_file
    outs = convert_file("小说.txt", max_per_volume=500)    # 返回输出路径列表
"""

from pathlib import Path
import argparse
import os
import sys

import txt_to_epub_core as core
from encoding_detect import detect_encoding
import hierarchy_rules


def write_volumes(out: Path, title: str, author: str, cover_image,
                  chapters, max_per_volume: int, volumes=None):
    """按 ``max_per_volume`` 把 chapters 切块写多卷；返回输出路径列表。

    ``chapters`` 为 ``[(标题, 正文), ...]``，build 复用主体 ``core.build_epub``
    （与 GUI 同一引擎）。
    ``volumes``：由 core.detect_volumes 返回的嵌套分组；单文件（不拆分）时
    据此生成『卷→章』嵌套 TOC；拆分时各物理卷内部用扁平 TOC（卷嵌套主要服务单文件场景）。
    单卷（<=0 或章节未超限）返回 ``[out]``，与原单卷行为完全一致；
    多卷时每卷文件名加「_卷N」后缀，且各卷独立嵌入封面，保证任意一卷可单独打开。
    """
    ranges = core.volume_ranges(len(chapters), max_per_volume)
    if len(ranges) == 1:
        book = core.build_epub(chapters, title, author, cover_image=cover_image,
                               volumes=volumes)
        core.epub.write_epub(out, book)
        return [out]
    outs = []
    for i, (s, e) in enumerate(ranges, 1):
        vtitle = f"{title} 第{i}卷"
        vout = out.with_name(f"{out.stem}_卷{i}{out.suffix}")
        # 拆分时各物理卷内部扁平 TOC（不跨物理卷拼接卷层级）
        book = core.build_epub(chapters[s:e], vtitle, author, cover_image=cover_image)
        core.epub.write_epub(vout, book)
        outs.append(vout)
    return outs


def convert_file(txt_path, out_dir=None, title=None, author="",
                 max_per_volume: int = 0, encoding=None, cover_image=None,
                 no_volume: bool = False, hierarchy: str = "default",
                 verbose: bool = True):
    """高层 API：把一个 TXT 转成 EPUB（可多卷、可嵌套 TOC）。返回输出路径列表。

    :param txt_path: 输入 TXT 路径
    :param out_dir:  输出目录（默认与源文件同目录）
    :param title:    书名（默认取文件名）
    :param author:   作者
    :param max_per_volume: 每卷最大章数（0=不拆分）
    :param encoding: 编码字符串；``None``/``"auto"`` 时自动探测
    :param cover_image: 封面 JPEG 字节（可选）
    :param no_volume: True 时禁用『卷→章』嵌套 TOC（强制扁平，与旧版一致）
    :param hierarchy: 嵌套目录引擎：``"default"``=数据驱动分层(hierarchy_rules，技术储备)；
                      ``"legacy"``=原硬编码卷识别(core.detect_volumes)
    """
    txt_path = Path(txt_path)
    if encoding is None or encoding == "auto":
        encoding, conf = detect_encoding(txt_path)
        if verbose:
            print(f"[编码] 自动探测: {encoding} (置信度 {conf})")
    else:
        if verbose:
            print(f"[编码] 使用指定: {encoding}")

    # 复用主体的轻量索引 + 按偏移读章。
    # 说明：主体的 parse_txt() 在 finally 里对管道模式返回的 Utf8Buffer 调用
    # os.remove() 会抛 TypeError 崩溃；这里直接走底层公共 API 自行清理，
    # 保持对主体文件零改动（低耦合原则）。
    temp, index = core.parse_txt_index(txt_path, encoding)
    chapters = [(e["title"], core.read_chapter(temp, e)) for e in index]
    if not isinstance(temp, core.Utf8Buffer):
        try:
            os.remove(temp)
        except OSError:
            pass
    if verbose:
        print(f"[章节] 解析到 {len(chapters)} 章")
    if not chapters:
        raise RuntimeError("未解析到任何章节，请检查目录规则或编码是否选错")

    # 单文件嵌套 TOC：按索引字节偏移对齐扫描卷标记分组（无卷则自动退回扁平）。
    # 直接用 parse_txt_index 返回的 index + source（Utf8Buffer/路径），
    # 三种索引模式（管道/raw/legacy）均正确，且不受标题误匹配影响。
    volumes = None
    if not no_volume:
        if hierarchy == "default":
            # 数据驱动分层（hierarchy_rules）：只读 index['title'] 判类型，
            # 按"数量差定层级 + 章锚定排干扰"建嵌套，零重扫源文件。
            volumes = hierarchy_rules.build_hierarchy(index)
            if verbose:
                info = hierarchy_rules.analyze(index)
                if info["accepted_containers"]:
                    print(f"[目录] 数据驱动分层(default)：接受容器 {info['accepted_containers']}；"
                          f"{len(volumes)} 个分组（章节级 {info['chapter_count']}）")
                else:
                    print(f"[目录] 数据驱动分层(default)：未发现明确层级，退回扁平 TOC")
        else:
            # 原硬编码卷识别（legacy）：保留作备用，行为与原版一致。
            volumes = core.detect_volumes(index, temp, encoding)
            if verbose and volumes:
                print(f"[目录] 嵌套 TOC(legacy)：{len(volumes)} 个分组（卷/正文）")

    title = title or txt_path.stem
    out_dir = Path(out_dir) if out_dir else txt_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{title}.epub"

    outs = write_volumes(out, title, author, cover_image, chapters,
                          max_per_volume, volumes=volumes)
    if verbose:
        if len(outs) == 1:
            print(f"[完成] 单卷 -> {outs[0]}")
        else:
            print(f"[完成] 共 {len(outs)} 卷:")
            for p in outs:
                print(f"        {p}")
    return outs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="TXT → EPUB（无 GUI，自动探测编码，支持多卷拆分）")
    ap.add_argument("txt", help="输入 TXT 文件路径")
    ap.add_argument("--out", help="输出目录（默认与源文件同目录）")
    ap.add_argument("--title", help="书名（默认取文件名）")
    ap.add_argument("--author", default="", help="作者")
    ap.add_argument("--encoding", default="auto",
                    help="编码：auto（默认）/ utf-8 / gbk / gb18030 / big5 ...")
    ap.add_argument("--max-per-volume", type=int, default=0,
                    help="每卷最大章数（0=不拆分，默认）")
    ap.add_argument("--no-volume", action="store_true",
                    help="禁用『卷→章』嵌套目录，强制扁平（与旧版一致）")
    ap.add_argument("--hierarchy", choices=["default", "legacy"], default="default",
                    help="嵌套目录引擎：default=数据驱动分层(技术储备) / legacy=原硬编码卷识别")
    ap.add_argument("--cover", help="封面图路径（JPEG）")
    args = ap.parse_args(argv)

    cover = Path(args.cover).read_bytes() if args.cover else None

    try:
        outs = convert_file(
            args.txt, out_dir=args.out, title=args.title, author=args.author,
            max_per_volume=args.max_per_volume, encoding=args.encoding,
            cover_image=cover, no_volume=args.no_volume,
            hierarchy=args.hierarchy,
        )
    except Exception as e:  # noqa: BLE001 — CLI 层统一兜底报错
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    return 0 if outs else 1


if __name__ == "__main__":
    sys.exit(main())
