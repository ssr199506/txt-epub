#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TXT→ePub 核心模块：解析、构建、转换结果
依赖：pip install EbookLib Pillow
"""
import io
import json
import os
import pickle
import re
import tempfile
from pathlib import Path
from uuid import uuid4
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from ebooklib import epub

# Pillow 可选（封面裁剪）
try:
    from PIL import Image, ImageTk

    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

# ---------------------------------------------------------------------------
# 老板御用正则 —— 章节标题匹配（内置默认）
# ---------------------------------------------------------------------------
TOC_RE = re.compile(
    r"(?im)^.{0,6}(?:[引楔]子|正文(?!完|结)|[引序前]言|[序终]章|扉页|"
    r"[上中下][部篇卷]|卷首语|后记|尾声|番外|={2,4}|"
    r"第\s{0,4}[\d〇零一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]+?"
    r"\s{0,4}(?:章|节(?!课)|卷|页[、 　]|集(?![合和])|部(?![分是门落])|篇(?!张))"
    r").{0,40}$|"
    r"^.{0,6}[\d〇零一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟a-z]{1,8}"
    r"[、. 　].{0,20}$"
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class ConversionResult:
    """单文件转换结果"""
    success: bool
    file_path: Path
    output_path: Path = None
    error: str = ""
    chapter_count: int = 0


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------
def load_toc_rules(json_path: Union[str, Path]) -> List[dict]:
    """从 JSON 文件加载章节识别规则（全部有 rule 内容的规则），排序：
    目录 → 目录(去空白) → 通用规则 → 其余按 serialNumber → 晋江相关 最后"""
    with open(json_path, encoding="utf-8") as f:
        rules = json.load(f)
    valid = [r for r in rules if r.get("rule")]

    def sort_key(r):
        name = r.get("name", "")
        if name == "目录":
            return -9998
        if "目录(去空白)" in name:
            return -9997
        if name == "通用规则":
            return -9996
        if "晋江" in name:
            return 9998
        return r.get("serialNumber", 99)

    valid.sort(key=sort_key)
    return valid


# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------
def _compile_pattern(
    pattern: Optional[Union[str, re.Pattern]] = None,
) -> re.Pattern:
    """将用户传入的 pattern 编译成正则对象，None 则使用内置 TOC_RE"""
    if pattern is None:
        return TOC_RE
    if isinstance(pattern, str):
        return re.compile(pattern)
    return pattern  # 已是 compiled Pattern


def parse_txt(
    txt_path: Path,
    encoding: str = "utf-8",
    toc_pattern: Optional[Union[str, re.Pattern]] = None,
    text_str: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """解析 TXT 文件，按章节分割，返回 [(标题, 内容), ...]

    toc_pattern：自定义正则（字符串或 compiled），None 则使用内置 TOC_RE
    text_str：可选，若提供则直接从字符串解析，跳过文件读取（避免重复 IO）
    """
    compiled = _compile_pattern(toc_pattern)
    chapters: List[Tuple[str, str]] = []
    buffer: List[str] = []
    curr_title = "前言"

    # 数据源：优先使用内存字符串，避免重复读盘
    if text_str is not None:
        lines = text_str.splitlines(keepends=True)
    else:
        with txt_path.open(encoding=encoding, errors="ignore") as f:
            lines = list(f)

    for line in lines:
        # 快速预过滤：标题行通常较短（strip 后 <80 字且非空），跳过正文行
        stripped = line.lstrip()
        is_candidate = len(stripped) < 80 and stripped != ""

        if is_candidate and compiled.match(line):
            if buffer:
                chapters.append((curr_title.strip(), "".join(buffer)))
                buffer = []
            curr_title = line.strip()
        else:
            buffer.append(line)

    # 保存最后一个章节
    if buffer or not chapters:
        chapters.append((curr_title.strip(), "".join(buffer)))

    return chapters


def _crop_cover_image(
    image_path: Optional[Path] = None,
    crop_box: Optional[Tuple[int, int, int, int]] = None,
    pil_image=None,
) -> Optional[bytes]:
    """裁剪封面图片并返回 JPEG bytes，crop_box = (left, top, right, bottom)

    可以从文件路径加载，也可以直接传入已打开的 PIL Image。
    """
    if not HAVE_PIL:
        return None
    try:
        if pil_image is not None:
            img = pil_image.convert("RGB")
        elif image_path is not None:
            img = Image.open(image_path).convert("RGB")
        else:
            return None
        if crop_box:
            l, t, r, b = crop_box
            img = img.crop((l, t, r, b))
        # 统一输出尺寸（保持比例）
        target = (600, 800)
        img.thumbnail(target, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def build_epub(
    chapters: List[Tuple[str, str]],
    book_title: str,
    author: str,
    lang: str = "zh-CN",
    cover_image: Optional[bytes] = None,
):
    """构建 EPUB 电子书对象

    cover_image: JPEG bytes 用于封面图，None 则不嵌入封面图
    """
    book = epub.EpubBook()
    book.set_identifier(str(uuid4()))
    book.set_title(book_title)
    book.set_language(lang)
    book.add_author(author)

    # CSS 样式 — 参照多看/专业中文epub排版规范
    style = (
        '@charset "utf-8";\n'
        "/* 基础排版 */\n"
        "body{\n"
        '  font-family:"宋体","Songti SC","Noto Serif CJK SC",'
        '"Source Han Serif CN",serif;\n'
        "  line-height:1.3;\n"
        "  text-align:justify;\n"
        "  margin:0 1%;\n"
        "  padding:0;\n"
        "}\n"
        "/* 章节标题 */\n"
        "h2.head{\n"
        '  font-family:"黑体","Heiti SC","Microsoft YaHei",sans-serif;\n'
        "  font-size:1.1em;\n"
        "  font-weight:bold;\n"
        "  text-align:left;\n"
        "  margin:1em 2em 2em 0;\n"
        "  padding:0;\n"
        "  color:#000;\n"
        "  line-height:1.4;\n"
        "}\n"
        "/* 正文段落 */\n"
        "p{\n"
        "  text-indent:2em;\n"
        "  line-height:1.3;\n"
        "  margin:0.3em 0;\n"
        "  text-align:justify;\n"
        "}\n"
        "/* 封面 */\n"
        ".cover-title{\n"
        '  font-family:"黑体","Heiti SC","Microsoft YaHei",sans-serif;\n'
        "  font-size:1.5em;\n"
        "  font-weight:bold;\n"
        "  text-align:center;\n"
        "  text-indent:0;\n"
        "  margin:40% 5% 0 5%;\n"
        "  line-height:1.6;\n"
        "}\n"
        ".cover-author{\n"
        '  font-family:"仿宋","FangSong SC","FangSong",serif;\n'
        "  font-size:1em;\n"
        "  text-align:center;\n"
        "  text-indent:0;\n"
        "  margin:2em 5% 0 5%;\n"
        "  line-height:1.4;\n"
        "}\n"
        ".cover-img{\n"
        "  text-align:center;\n"
        "  text-indent:0;\n"
        "  margin:30% 5% 0 5%;\n"
        "}\n"
        ".cover-img img{\n"
        "  max-width:100%;\n"
        "  height:auto;\n"
        "}"
    )
    book.add_item(
        epub.EpubItem(
            uid="style",
            file_name="style/default.css",
            media_type="text/css",
            content=style,
        )
    )

    # 封面页（有图片则显示图片，否则跳过封面）
    if cover_image:
        # 嵌入封面图片文件
        img_item = epub.EpubItem(
            uid="cover-img",
            file_name="Images/cover.jpg",
            media_type="image/jpeg",
            content=cover_image,
        )
        book.add_item(img_item)
        # 标记封面（供阅读器识别）
        book.add_metadata(None, "meta", "", {"name": "cover", "content": "cover-img"})
        cover_content = (
            '<html xmlns="http://www.w3.org/1999/xhtml">\n'
            '<head><title>Cover</title>'
            '<link href="style/default.css" rel="stylesheet" type="text/css"/>'
            '</head>\n'
            '<body>\n'
            '<div style="text-align:center;margin:40% 5% 0 5%;">'
            '<img src="Images/cover.jpg" style="max-width:100%;height:auto;" alt="cover"/>'
            '</div></body></html>'
        )
        cover = epub.EpubHtml(title="封面", file_name="cover.xhtml", lang=lang)
        cover.content = cover_content
        cover.add_link(href="style/default.css", rel="stylesheet", type="text/css")
        book.add_item(cover)
        spine = [cover]
    else:
        # 没有封面图，直接以第一章开头
        spine = []
    toc = []
    for idx, (title, text) in enumerate(chapters, 1):
        c = epub.EpubHtml(
            title=title, file_name=f"chap_{idx:03d}.xhtml", lang=lang
        )
        paragraphs = "\n".join(
            f"<p>{line.strip()}</p>"
            for line in text.splitlines()
            if line.strip()
        )
        c.content = f'<h2 class="head">{title}</h2>\n{paragraphs}'
        c.add_link(href="style/default.css", rel="stylesheet", type="text/css")
        book.add_item(c)
        toc.append(epub.Link(f"chap_{idx:03d}.xhtml", title, f"chap_{idx}"))
        spine.append(c)

    book.toc = tuple(toc)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    return book


def _unique_path(path: Path) -> Path:
    """如果路径已存在，自动追加数字后缀避免覆盖"""
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        new = parent / f"{stem}_{counter}{suffix}"
        if not new.exists():
            return new
        counter += 1


def convert_single(
    txt_path: Path,
    output_path: Path,
    encoding: str,
    book_title: str,
    author: str,
    toc_pattern: Optional[Union[str, re.Pattern]] = None,
    cover_image: Optional[bytes] = None,
) -> ConversionResult:
    """转换单个 TXT → EPUB，返回结果

    toc_pattern：章节识别正则（传给 parse_txt）
    cover_image：JPEG bytes 作为封面图，None 则不嵌入
    """
    try:
        chapters = parse_txt(txt_path, encoding, toc_pattern=toc_pattern)
        book = build_epub(
            chapters, book_title or txt_path.stem, author,
            cover_image=cover_image,
        )
        final_output = _unique_path(output_path)
        epub.write_epub(final_output, book)
        return ConversionResult(
            success=True,
            file_path=txt_path,
            output_path=final_output,
            chapter_count=len(chapters),
        )
    except Exception as e:
        return ConversionResult(
            success=False, file_path=txt_path, error=str(e)
        )


# ---------------------------------------------------------------------------
# 动态分块并行解析（修改计划新增）
# ---------------------------------------------------------------------------
def _count_lines(file_path, encoding="utf-8") -> int:
    """快速统计文件行数（O(N) 时间，O(1) 内存）"""
    count = 0
    with open(file_path, encoding=encoding, errors="ignore") as f:
        for _ in f:
            count += 1
    return count


def _parse_chunk(
    file_path: str,
    start_line: int,
    end_line: int,
    encoding: str,
    pattern_str: Optional[str],
    chunk_index: int,
    total_chunks: int,
) -> str:
    """
    子进程入口：解析文件的一个逻辑段。
    返回临时文件路径，内含 pickle 后的 (overflow: str, chapters: list)

    切口修正：非首段先收集第一个标题前的内容作为 overflow，
    在合并阶段拼接到前一段的最后一章末尾。
    """
    compiled = _compile_pattern(pattern_str)
    overflow_lines: List[str] = []
    chapters: List[Tuple[str, str]] = []
    buffer: List[str] = []
    curr_title = "前言"
    first_title_found = False

    with open(file_path, encoding=encoding, errors="ignore") as f:
        for i, line in enumerate(f):
            if i < start_line:
                continue
            if i >= end_line:
                break

            # 切口修正：非首段收集第一个标题前的内容
            if chunk_index > 0 and not first_title_found:
                if compiled.match(line):
                    first_title_found = True
                else:
                    overflow_lines.append(line)
                    continue

            # parse_txt 内核逻辑（不变）
            if compiled.match(line):
                if buffer:
                    chapters.append((curr_title.strip(), "".join(buffer)))
                    buffer = []
                curr_title = line.strip()
            else:
                buffer.append(line)

    # 非首段且完全无标题时，整段全部作为 overflow（不创建"前言"章节）
    if chunk_index > 0 and not first_title_found:
        overflow = "".join(overflow_lines) + "".join(buffer)
        chapters = []
    else:
        if buffer or not chapters:
            chapters.append((curr_title.strip(), "".join(buffer)))
        overflow = "".join(overflow_lines)

    temp_fd, temp_path = tempfile.mkstemp(suffix=f"_chunk{chunk_index}_{os.getpid()}.pkl")
    with os.fdopen(temp_fd, "wb") as f:
        pickle.dump((overflow, chapters), f)
    return temp_path
