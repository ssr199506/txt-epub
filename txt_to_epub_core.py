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
import threading
import struct

from ebooklib import epub

# Pillow 可选（封面裁剪）
try:
    from PIL import Image, ImageTk

    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

# ---------------------------------------------------------------------------
# Rust 匹配引擎桥接（Beta：仅替换 parse_txt / _parse_chunk 的「逐行匹配」内核）
# ---------------------------------------------------------------------------
# 设计：正则一律来自 JSON 规则（exportTxtTocRule..json）或内置 TOC_RE，经 toc_pattern
# 传给 Rust 引擎；Rust 侧不硬编码任何正则。GUI / build_epub / 切口合并等其余逻辑逐字复用。
import subprocess

# 预编译好的内核二进制放在 Beta 根目录（不依赖 target/ 构建缓存，删掉 target/ 也能直接跑）。
# 重新编译后产物在 rust/target/release/parse_txt_rust.exe，复制覆盖本文件同目录的 parse_txt_rust.exe 即可生效。
_RUST_BIN = Path(__file__).resolve().parent / "parse_txt_rust.exe"

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
    """解析 TXT 文件，按章节分割，返回 [(标题, 内容), ...]（兼容旧调用方）。

    Beta 版内部走轻量索引路径：Rust 先回传「标题+字节偏移」索引，这里再按需把正文
    seek 读回，契约与原版一致（保留以便批量/并行等旧路径复用）。
    """
    if text_str is not None:
        fd, tmp = tempfile.mkstemp(suffix=".utf8.txt", prefix="txt_epub_")
        os.close(fd)
        # universal newline 写入：把可能的 CRLF 归一成 LF，与 Rust 字节偏移对齐
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text_str)
        src = tmp
    else:
        src = txt_path
    temp, index = parse_txt_index(
        src,
        encoding if text_str is None else "utf-8",
        toc_pattern,
        already_utf8=(text_str is not None),
    )
    try:
        chapters = [(e["title"], read_chapter(temp, e)) for e in index]
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass
        if text_str is not None:
            try:
                os.remove(src)
            except OSError:
                pass
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


def _new_book(book_title, author, lang="zh-CN", cover_image=None):
    """创建 EpubBook 骨架（标识/样式/封面），返回 (book, spine)。

    供 `build_epub`（章节内容在内存）与 `build_epub_from_pack`（内容在 xhtml 小文件）共用，
    保证两种打包路径的排版与目录结构完全一致。
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
        "}\n"
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
        img_item = epub.EpubItem(
            uid="cover-img",
            file_name="Images/cover.jpg",
            media_type="image/jpeg",
            content=cover_image,
        )
        book.add_item(img_item)
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
        spine = []
    return book, spine


def build_epub(
    chapters: List[Tuple[str, str]],
    book_title: str,
    author: str,
    lang: str = "zh-CN",
    cover_image: Optional[bytes] = None,
    volumes: Optional[List[Tuple[str, List[int]]]] = None,
):
    """构建 EPUB 电子书对象

    cover_image: JPEG bytes 用于封面图，None 则不嵌入封面图
    volumes: 由 detect_volumes() 返回的 [(卷名, [章节下标...]), ...]；
             提供时生成『卷→章』嵌套 TOC（长篇网文目录体验更好，借鉴 legado）；
             None 时退回扁平一级 TOC（与旧版一致）。
    """
    book, spine = _new_book(book_title, author, lang, cover_image)

    # 章节 EpubHtml 始终创建并加入 spine/book；TOC 组装方式才因 volumes 而不同
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

    if volumes:
        # 嵌套 TOC：每个卷作父节点，其下挂章节（EbookLib 用 (父, (子...)) 元组表达层级）
        nested = []
        for i, (vtitle, members) in enumerate(volumes):
            # 父节点指向该卷首章(members[0])，故首章不再作为子项列出，
            # 避免「父/子同 href」重复（通用去重，覆盖 default 与 legacy 两种引擎）。
            # 卷标记行被章节解析器当成同名章节时(members[0] 即卷名行)同样被排除，充当卷标题页。
            children = tuple(toc[idx] for idx in members
                             if idx != members[0] and chapters[idx][0] != vtitle)
            # 父节点指向该卷首章（真实存在的目标），卷名作为分组标题
            parent = epub.Link(f"chap_{members[0] + 1:03d}.xhtml", vtitle, f"vol_{i}")
            nested.append((parent, children))
        book.toc = tuple(nested)
    else:
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


def volume_ranges(n_items: int, max_per_volume: int):
    """返回每卷的 (start, end) 半开区间列表。

    max_per_volume <= 0 时整本作为一卷 [(0, n_items)]；
    否则按 max_per_volume 切块，末尾不足一卷也单独成卷。
    借鉴 legado 的 getSeparatedEpub()（大书按 size 分卷），
    此处以「每卷章数」为阈值，更确定、更易调试。
    """
    if max_per_volume <= 0:
        return [(0, n_items)]
    return [(s, min(s + max_per_volume, n_items))
            for s in range(0, n_items, max_per_volume)]


# 卷标记：第X卷/部/篇/集（阿拉伯/中文数字）。网文常以篇/集/部作分卷单位，
# 旧版只认「卷」会漏掉《吞噬星空》这类以篇/集分卷的书（借鉴参考项目的四类覆盖）。
_VOL_PAT = re.compile(r"第\s*[0-9零一二三四五六七八九十百千]+\s*[卷部篇集]")
# 卷名行允许的边界字符（书名号/括号/空格/全角空格）
_VOL_TRIM = str.maketrans("", "", "【】()（）[] \u3000\t\r")
# 正文里若出现这些字，说明那不是独立的卷标记行（如「第三卷第十四章有提到」）
_VOL_BODY_KW = ("章", "回", "节")
# 卷尾标记：含这些说明是『某卷结束』而非分卷起点（借鉴参考项目的 exclude 思路）
_VOL_END_KW = ("完", "终", "尾声", "结束")
# 叙述性标点：含这些说明是散文句子而非卷标题行（如「继续阅读第三篇——驯兽篇。」）
_VOL_PROSE_PUNCT = "。，、！？；：\"\"''（）【】《》〈〉—…·~～"


def _is_volume_line(line: str) -> bool:
    """判断一行是不是『独立的卷标记行』（而非正文里顺带提到的卷/篇/集/部）。

    例：「【第一卷】」「第二篇 战神罗峰」「第一篇 一夜觉醒 第一集 深夜觉醒」算；
    「第20章 暗金色圆球（第一集终章）」（含章、括号）不算；
    「继续阅读第三篇——驯兽篇。」（叙述句、含破折号句号）不算；
    「第一卷终」（卷尾标记）不算；散文片段「前三卷功法」因叙述词/长度被兜底排除。
    """
    stripped = line.strip()
    if not stripped:
        return False
    # 去掉书名号/括号/空格后做判定，但保留原行（带空格）作为卷名显示
    compact = stripped.translate(_VOL_TRIM)
    matches = list(_VOL_PAT.finditer(compact))
    if not matches:
        return False
    # 卷标记必须位于行首（strip 已去前导空白），否则是句中提及，非标题行
    if matches[0].start() > 0:
        return False
    # 含章节/回/节关键字 → 是章节标题行，不是卷
    if any(k in compact for k in _VOL_BODY_KW):
        return False
    # 含卷尾标记 → 是某卷结束，不是分卷起点
    if any(k in compact for k in _VOL_END_KW):
        return False
    # 含叙述性标点 → 散文句子，不是卷标题
    if any(p in compact for p in _VOL_PROSE_PUNCT):
        return False
    # 整行过长（>20 字）基本是正文段落，不是干净的卷标题
    if len(compact) > 20:
        return False
    return True


def detect_volumes(index, source, encoding):
    """按章节索引的字节偏移 + 源文本，扫描「第X卷」独立行做卷→章分组。

    返回 List[Tuple[vol_title, List[chap_index]]]（chap_index 为章节在 index/chapters
    中的下标，0 基）。用于单文件嵌套 TOC（卷→章），借鉴 legado 的 TableOfContents 层级模型。

    为何不用标题匹配：网文 TXT 常有角色属性表等被误判成「章」的条目，且多字节/CRLF 下
    逐标题 str.find 顺序匹配大面积失效（实测覆盖率仅 0.4）。故改为字节偏移对齐——
    直接拿 parse_txt_index 每章的 start 字节偏移换算成字符位置，与卷标记字符位置比较，
    完全不依赖标题文本，三种索引模式（管道 UTF-8 / raw 原编码 / legacy UTF-8 临时文件）均正确：
    - 管道模式：source 为 Utf8Buffer，offset 即其 .data 的 UTF-8 字节偏移；
    - raw 模式：source 为原文件路径，index 项带 encoding 字段，offset 为原编码字节偏移；
    - legacy：source 为 UTF-8 临时文件，index 无 encoding 字段，按 UTF-8 解码。

    安全性：无卷标记、或卷内无章节（异常）时返回空列表 —— 调用方据此退回扁平 TOC，
    与现版行为完全一致（零回归）。
    """
    if not index:
        return []
    # 1) 确定解码方式：raw 模式 index 项带 encoding；否则 UTF-8
    raw_enc = index[0].get("encoding") or "utf-8"
    # 2) 取源字节并解码为文本（字符空间）
    if isinstance(source, Utf8Buffer):
        src_bytes = source.data
    else:
        with open(source, "rb") as f:
            src_bytes = f.read()
    dec = src_bytes.decode(raw_enc, errors="replace")
    # 3) 每章的字符偏移：src 前 start 字节解码后得到的字符数
    chap_char = [len(src_bytes[:max(0, int(e["start"]))].decode(raw_enc, "replace"))
                 for e in index]
    # 4) 卷标记（仅取独立行；同一行若同时含 篇+集 等多种标记，只记一次）
    vol_marks = []  # (vol_name, char_pos)
    seen_lines = set()
    nl = "\n"
    for m in _VOL_PAT.finditer(dec):
        line_start = dec.rfind(nl, 0, m.start()) + 1
        line_end = dec.find(nl, m.start())
        if line_end == -1:
            line_end = len(dec)
        if line_start in seen_lines:
            continue
        line = dec[line_start:line_end].strip()
        if _is_volume_line(line):
            seen_lines.add(line_start)
            vol_marks.append((line, line_start))
    if not vol_marks:
        return []
    # 5) 归属：第 k 卷区间 [vstart, vnext)
    vol_starts = [v[1] for v in vol_marks]
    boundaries = vol_starts + [len(dec) + 1]
    groups = []
    for k, (vname, vstart) in enumerate(vol_marks):
        vnext = boundaries[k + 1]
        members = [ci for ci, cp in enumerate(chap_char) if vstart <= cp < vnext]
        groups.append((vname, members))
    # 6) 首个卷之前的章节归入「正文」组（如有）
    leading = [ci for ci, cp in enumerate(chap_char) if cp < vol_starts[0]]
    if leading:
        groups.insert(0, ("正文", leading))
    # 异常兜底：若所有卷都无章节（offset 错位等），退回扁平
    if all(not m for _, m in groups):
        return []
    return groups


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
    Beta 版：段内「逐行匹配」委派给 Rust 引擎（--mode chunk），切口修正逻辑保留在 Python。
    返回临时文件路径，内含 pickle 后的 (overflow: str, chapters: list)
    """
    pat = pattern_str if pattern_str else TOC_RE.pattern

    # 按行范围切片（与原版一致）
    lines = []
    with open(file_path, encoding=encoding, errors="ignore") as f:
        for i, line in enumerate(f):
            if i < start_line:
                continue
            if i >= end_line:
                break
            lines.append(line)
    text = "".join(lines)

    # 委派 Rust 引擎：非首段加 --first-chunk 外的切口收集由 Rust 端处理
    cmd = [str(_RUST_BIN), "--pattern", pat, "--mode", "chunk"]
    if chunk_index == 0:
        cmd.append("--first-chunk")
    try:
        # 以字节传入，避免 Windows 下 text=True 把 `\n` 改写成 `\r\n`
        res = subprocess.run(
            cmd, input=text.encode("utf-8"), capture_output=True
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"未找到 Rust 匹配引擎：{_RUST_BIN}（请先 cargo build --release）"
        ) from e
    if res.returncode != 0:
        raise RuntimeError((res.stderr or b"").decode("utf-8", "ignore") or "Rust 匹配引擎执行失败")
    data = json.loads(res.stdout)
    overflow = data["overflow"]
    chapters = [(t, c) for t, c in data["chapters"]]

    temp_fd, temp_path = tempfile.mkstemp(suffix=f"_chunk{chunk_index}_{os.getpid()}.pkl")
    with os.fdopen(temp_fd, "wb") as f:
        pickle.dump((overflow, chapters), f)
    return temp_path


# ---------------------------------------------------------------------------
# 轻量索引模式（Beta：Rust 只回传标题+字节偏移，正文按需 seek / 生成时打包）
# ---------------------------------------------------------------------------
def _pattern_to_str(toc_pattern=None):
    """把 toc_pattern（None / 字符串 / compiled）规整成正则字符串，供 Rust 使用。"""
    if toc_pattern is None:
        return TOC_RE.pattern
    if isinstance(toc_pattern, re.Pattern):
        return toc_pattern.pattern
    return toc_pattern


def _stream_decode_to_utf8(src_path, encoding, dst_path, chunk=1 << 20):
    """把源文件按 encoding 流式解码成 UTF-8 临时文件，不占全量内存。

    行尾对齐原版 `parse_txt` 的文件分支（`open(...)` 默认 universal newline）：
    读取侧让 \\r\\n / \\r 统一翻译成 \\n，写入侧用 newline="" 防止再被翻回 \\r\\n，
    这样临时文件的行序与原版 `list(f)` 逐位一致，字节偏移才可信。
    """
    with open(src_path, encoding=encoding, errors="ignore") as fin, \
         open(dst_path, "w", encoding="utf-8", newline="") as fout:
        while True:
            block = fin.read(chunk)
            if not block:
                break
            fout.write(block)


def _decode_to_utf8_bytes(src_path, encoding, chunk=1 << 20):
    """内存版解码：源文件按 encoding 流式解码成 UTF-8 bytes（universal-newline 与
    `_stream_decode_to_utf8` 完全对齐），用于管道模式：Python 持有 UTF-8 bytes 经 stdin 喂 Rust，零落盘。
    单本解析时内存峰值 = 该本 UTF-8 体积（管道模式固有取舍，仅持有单本，不并发占全量）。"""
    out = bytearray()
    with open(src_path, encoding=encoding, errors="ignore") as fin:
        while True:
            block = fin.read(chunk)
            if not block:
                break
            out += block.encode("utf-8")
    return bytes(out)


def _index_with_re(source, rx):
    """纯 Python 轻量索引：逐字复刻原版 parse_txt 的匹配内核。

    rx 已经 `re.compile` 成功（Python 能处理的规则），无需启动 Rust 子进程；
    产出的 {title,start,end} 偏移索引与 Rust / 原版完全等价、可互换。

    关键：匹配行(标题)不进入 buffer —— 与原版 `if matched: ... else buffer.append` 一致，
    否则会把标题行算进正文、凭空多出假章节并导致字节偏移漂移。
    """
    chapters = []
    curr_title = "前言"
    buffer = []  # list[(字节起始, 行str)] —— 仅非标题行
    with open(source, "r", encoding="utf-8") as f:
        offset = 0
        for line in f:
            stripped = line.lstrip()
            is_candidate = bool(stripped) and len(stripped) < 80
            matched = is_candidate and bool(rx.match(line))
            if matched:
                if buffer:
                    body_start = buffer[0][0]
                    last = buffer[-1]
                    body_end = last[0] + len(last[1].encode("utf-8"))
                    chapters.append({"title": curr_title.strip(), "start": body_start, "end": body_end})
                    buffer = []
                curr_title = line.strip()
            else:
                buffer.append((offset, line))
            offset += len(line.encode("utf-8"))
    if buffer or not chapters:
        if buffer:
            body_start = buffer[0][0]
            last = buffer[-1]
            body_end = last[0] + len(last[1].encode("utf-8"))
        else:
            body_start = body_end = 0
        chapters.append({"title": curr_title.strip(), "start": body_start, "end": body_end})
    return chapters


def _index_with_rust(source, pat):
    """re 编译不了的兜底：变长后行断言 / \\Q..\\E 等交给 Rust fancy-regex。"""
    try:
        res = subprocess.run(
            [str(_RUST_BIN), "--pattern", pat, "--source", source],
            capture_output=True,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"未找到 Rust 匹配引擎：{_RUST_BIN}（请先 cargo build --release）"
        ) from e
    if res.returncode != 0:
        raise RuntimeError(
            (res.stderr or b"").decode("utf-8", "ignore") or "Rust 匹配引擎执行失败"
        )
    return json.loads(res.stdout).get("chapters", [])


def _index_with_rust_pipe(utf8_bytes, pat):
    """管道模式：把已解码的 UTF-8 bytes 经 stdin 喂给 Rust 切章，零临时文件落盘。
    返回章节列表（偏移指向这段 UTF-8 字节流）。保留 Python 容错解码，规避 encoding_rs 与 gbk 分叉。"""
    try:
        p = subprocess.Popen(
            [str(_RUST_BIN), "--pattern", pat, "--mode", "parse"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = p.communicate(input=utf8_bytes)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"未找到 Rust 匹配引擎：{_RUST_BIN}（请先 cargo build --release）"
        ) from e
    if p.returncode != 0:
        raise RuntimeError(
            (err or b"").decode("utf-8", "ignore") or "Rust 匹配引擎执行失败"
        )
    return json.loads(out).get("chapters", [])


def _index_with_rust_serve(files, encoding, pat):
    """阶段 B-α：单个常驻 Rust 进程处理多文件，消灭「每文件启一次子进程」的开销。

    - files: 文件路径列表；encoding: 统一解码编码（GBK/UTF-8 等，批量同库通常同编码）；
      pat: 章节匹配正则。
    - 每个文件用 `_decode_to_utf8_bytes` 解码为 UTF-8 字节（GBK→UTF-8 在 Python 完成，保 100% parity），
      按帧 `[4字节大端长度][UTF-8 字节]` 写给常驻 `--serve` 进程。
    - 后台读线程持续消费 stdout 的 JSONL 结果行（避免管道缓冲死锁）；主线程写完全部帧后关闭 stdin，
      触发 Rust EOF 退出。
    - 返回 [(path, [chapters]), ...]，顺序与输入一致（serve 单线程 FIFO，结果与喂入序对应）。
    - 进程只启动一次；pattern 在 Rust 侧 `CompiledEngine` 编译一次、全程复用。
    """
    if not files:
        return []
    proc = subprocess.Popen(
        [str(_RUST_BIN), "--serve", "--pattern", pat],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    results: List[List[dict]] = []
    lock = threading.Lock()

    def reader():
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            with lock:
                results.append(obj.get("chapters", []))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for f in files:
        try:
            data = _decode_to_utf8_bytes(str(f), encoding)
        except Exception:
            data = b""
        try:
            proc.stdin.write(struct.pack(">I", len(data)) + data)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            break
    try:
        proc.stdin.close()
    except Exception:
        pass
    t.join()
    try:
        proc.wait(timeout=60)
    except Exception:
        proc.kill()
    return [
        (str(f), results[i] if i < len(results) else [])
        for i, f in enumerate(files)
    ]


def batch_parse_index(files, encoding, toc_pattern=None):
    """阶段 B-α 批量索引入口：单个常驻 Rust 进程处理多文件，返回 [(path, [chapters]), ...]。

    - 单文件预览仍走 `parse_txt_index`（管道，每文件启一次进程）；本函数用于「25 万文件批量生成索引」。
    - 默认走 `_index_with_rust_serve`（进程只启一次）；任何异常回退逐文件 `parse_txt_index`（管道），
      保证批量任务不因单个文件/引擎异常整体失败。
    - 返回的 chapters 结构与 `parse_txt_index` 一致（list of {title,start,end}），下游可写 JSONL 或
      按需 `Utf8Buffer(_decode_to_utf8_bytes(path, encoding))` 配 `read_chapter` 取正文。
    """
    pat = _pattern_to_str(toc_pattern)
    try:
        return _index_with_rust_serve(files, encoding, pat)
    except Exception:
        res = []
        for f in files:
            try:
                _, idx = parse_txt_index(str(f), encoding, toc_pattern=pat)
                res.append((str(f), idx))
            except Exception:
                res.append((str(f), []))
        return res


def _index_with_rust_raw(raw_path, encoding, pat):
    """raw 模式：直接吃原始字节（GBK/GB18030/UTF-8），流式解码 + 原始偏移索引，不写 UTF-8 temp。

    返回章节列表，每章额外埋入 `encoding`（供 read_chapter / pack_chapters 按原编码 seek 解码）。
    """
    try:
        res = subprocess.run(
            [str(_RUST_BIN), "--pattern", pat, "--source", str(raw_path), "--encoding", encoding],
            capture_output=True,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"未找到 Rust 匹配引擎：{_RUST_BIN}（请先 cargo build --release）"
        ) from e
    if res.returncode != 0:
        raise RuntimeError(
            (res.stderr or b"").decode("utf-8", "ignore") or "Rust 匹配引擎执行失败"
        )
    data = json.loads(res.stdout)
    chapters = data.get("chapters", [])
    enc = data.get("encoding", encoding)
    for ch in chapters:
        ch["encoding"] = enc
    return chapters


def _index_text_re(text_str, rx):
    """`_index_with_re` 的内存版：直接在已解码字符串上索引，偏移对应该串的 UTF-8 字节。"""
    chapters = []
    curr_title = "前言"
    buffer = []  # list[(字节起始, 行str)]
    offset = 0
    for line in text_str.splitlines(keepends=True):
        stripped = line.lstrip()
        is_candidate = bool(stripped) and len(stripped) < 80
        matched = is_candidate and bool(rx.match(line))
        if matched:
            if buffer:
                body_start = buffer[0][0]
                last = buffer[-1]
                body_end = last[0] + len(last[1].encode("utf-8"))
                chapters.append({"title": curr_title.strip(), "start": body_start, "end": body_end})
                buffer = []
            curr_title = line.strip()
        else:
            buffer.append((offset, line))
        offset += len(line.encode("utf-8"))
    if buffer or not chapters:
        if buffer:
            body_start = buffer[0][0]
            last = buffer[-1]
            body_end = last[0] + len(last[1].encode("utf-8"))
        else:
            body_start = body_end = 0
        chapters.append({"title": curr_title.strip(), "start": body_start, "end": body_end})
    return chapters


def parse_txt_index(txt_path=None, encoding="utf-8", toc_pattern=None,
                    already_utf8=False, text_str=None, raw_offsets=False):
    """返回 (source_path: Path, index: list[{title,start,end,encoding?}])。

    轻量索引：Rust 只回传「标题 + 指向源文件的字节偏移」，正文不回传。
    双击预览按偏移 seek 读取，生成 EPUB 时按偏移打包——全程不把全文塞进内存。

    - raw_offsets=True：直接吃**原始字节**（GBK/GB18030/UTF-8），流式解码 + 原始偏移索引，
      **不写 UTF-8 临时文件**（消除 I/O 黑洞，回到原版速度）。每章埋 `encoding`，供后续解码。
      仅 Rust 进程级失败时回退到「写 temp 的旧路径」保底。
    - text_str：已解码字符串，直接内存索引（零磁盘解码，最快）；仍写一份 UTF-8 temp
      供 read_chapter/pack 按偏移 seek（偏移基于 text_str 的 UTF-8 字节）。
    - already_utf8=True：txt_path 已是 UTF-8，跳过重复解码（修掉兼容层双重翻译的 I/O 黑洞）。
    - **Rust 优先**：所有规则先交给 Rust 翻译层匹配；仅 Rust 进程级失败时回退 Python re 保底。
    """
    pat = _pattern_to_str(toc_pattern)
    if raw_offsets and txt_path is not None and text_str is None:
        # raw 模式：直接吃原始字节，不写 temp（Rust 优先；失败则回退旧路径）
        try:
            index = _index_with_rust_raw(txt_path, encoding, pat)
            return Path(txt_path), index
        except Exception:
            pass  # 回退到下方「写 temp」的旧路径保底
    if text_str is not None:
        # 管道模式：text_str 直接编码为 UTF-8 bytes，经 stdin 喂 Rust 切章（零落盘）
        utf8_bytes = text_str.encode("utf-8")
        try:
            index = _index_with_rust_pipe(utf8_bytes, pat)
        except Exception:
            # Rust 进程级失败时才回退写 temp（仅保底）
            fd, tmp = tempfile.mkstemp(suffix=".utf8.txt", prefix="txt_epub_")
            os.close(fd)
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text_str)
            try:
                index = _index_with_rust(tmp, pat)
            except Exception:
                index = _index_text_re(text_str, re.compile(pat))
            return Path(tmp), index
        return Utf8Buffer(utf8_bytes), index

    # 解码源文件为 UTF-8 bytes（内存，不落盘）—— 管道模式核心：规避写 temp 的 I/O 黑洞
    if already_utf8:
        utf8_bytes = Path(txt_path).read_bytes()
    else:
        utf8_bytes = _decode_to_utf8_bytes(txt_path, encoding)
    # Rust 优先：UTF-8 bytes 经 stdin 喂 Rust 切章（保留 Python 容错解码，规避 encoding_rs 与 gbk 分叉）
    try:
        index = _index_with_rust_pipe(utf8_bytes, pat)
    except Exception:
        # Rust 进程级失败时回退写 temp（仅保底）
        fd, tmp = tempfile.mkstemp(suffix=".utf8.txt", prefix="txt_epub_")
        os.close(fd)
        _stream_decode_to_utf8(txt_path, encoding, tmp)
        try:
            index = _index_with_rust(tmp, pat)
        except Exception:
            index = _index_with_re(tmp, re.compile(pat))
        return Path(tmp), index
    return Utf8Buffer(utf8_bytes), index


class Utf8Buffer:
    """管道模式的『虚拟 source』：包装一段已解码的 UTF-8 bytes。
    parse_txt_index 在管道模式下返回 (Utf8Buffer, index) 取代 (temp_path, index)；
    read_chapter / pack_chapters 识别它，从内存按偏移切片，零落盘（仅打包阶段落一次盘）。"""
    __slots__ = ("data",)

    def __init__(self, data: bytes):
        self.data = data


def read_chapter(temp_path, entry, encoding="utf-8"):
    """按字节偏移读取某章正文（body）。entry = {title,start,end}。
    temp_path 可为文件路径（Legacy / raw 模式）或 Utf8Buffer（管道模式，从内存切片）。"""
    start = int(entry["start"]); end = int(entry["end"])
    if isinstance(temp_path, Utf8Buffer):
        # 管道模式：偏移指向内存 UTF-8 字节流，直接切片解码（与 Legacy 走 temp 文件 seek 等价）
        return temp_path.data[start:end].decode("utf-8", "ignore")
    with open(temp_path, "rb") as f:
        f.seek(start)
        data = f.read(max(0, end - start))
    # raw 模式：temp_path 可能是原始 GBK/GB18030 文件，必须用章节埋的 encoding 解码；
    # 老 temp 路径：entry 无 encoding 字段，回退参数（默认 utf-8，对应 UTF-8 临时文件）。
    return data.decode(entry.get("encoding") or encoding, "ignore")


def pack_chapters(temp_path, index, out_dir, toc_pattern=None, book_title="", encoding=""):
    """调用 Rust --pack：把每章按字节偏移物化为 xhtml 小文件 + manifest.json（打包指南）。

    - temp_path 为 Utf8Buffer（管道模式）：仅打包这一步把内存 UTF-8 落一次盘给 --pack 切片（解析阶段已零落盘）。
    - encoding 非空（raw 模式）：temp_path 是**原始文件**路径，Rust 按原始偏移切片后按原编码解码。
    - encoding 为空（旧 UTF-8 路径）：temp_path 是 UTF-8 临时文件路径，按 UTF-8 切片。
    返回 manifest.json 路径；Python 侧再据此组装 EPUB（按打包指南消费，不再流式读源）。
    """
    pat = _pattern_to_str(toc_pattern)
    # raw 模式：调用方未显式传 encoding 时，从索引首项埋的 encoding 自动取（整本同编码）。
    # 老 temp 路径：索引项无 encoding 字段 → 保持空 → Rust 按 UTF-8 处理 temp 文件。
    if not encoding and index:
        encoding = index[0].get("encoding", "")
    # 管道模式：Utf8Buffer 仅在此打包阶段落一次盘（解析阶段已零落盘），用完即删
    if isinstance(temp_path, Utf8Buffer):
        sfd, real_source = tempfile.mkstemp(suffix=".utf8.txt", prefix="txt_epub_")
        with os.fdopen(sfd, "wb") as f:
            f.write(temp_path.data)
        cleanup_source = real_source
    else:
        real_source = str(temp_path)
        cleanup_source = None
    idx_fd, idx_path = tempfile.mkstemp(suffix=".index.json", prefix="txt_epub_")
    try:
        with os.fdopen(idx_fd, "w", encoding="utf-8") as f:
            json.dump({"chapters": index}, f, ensure_ascii=False)
        os.makedirs(out_dir, exist_ok=True)
        cmd = [str(_RUST_BIN), "--pack", "--source", real_source,
               "--index", idx_path, "--out", str(out_dir), "--book-title", book_title]
        if encoding:
            cmd += ["--encoding", encoding]
        res = subprocess.run(cmd, capture_output=True)
    finally:
        try:
            os.remove(idx_path)
        except OSError:
            pass
        if cleanup_source is not None:
            try:
                os.remove(cleanup_source)
            except OSError:
                pass
    if res.returncode != 0:
        raise RuntimeError(
            (res.stderr or b"").decode("utf-8", "ignore") or "Rust 打包失败"
        )
    return Path(out_dir) / "manifest.json"


def build_epub_from_pack(out_dir, manifest_path, book_title, author,
                         lang="zh-CN", cover_image=None, chapters_subset=None,
                         volumes=None):
    """按 Rust --pack 产出的 manifest.json + xhtml 小文件组装 EPUB。

    章节正文已在 xhtml 文件中（最终内容），Python 只做「按打包指南组装」：
    读 manifest 顺序、逐个读 xhtml 喂给 ebooklib，内存恒定，不再流式读源。
    chapters_subset：多卷拆分时只组装该子集（list[chapter dict]），顺序须合法。
    volumes：同 build_epub，提供时生成『卷→章』嵌套 TOC（None 则扁平）。
    """
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    chapters = manifest["chapters"] if chapters_subset is None else chapters_subset
    book, spine = _new_book(book_title, author, lang, cover_image)
    toc = []
    for i, ch in enumerate(chapters, 1):
        c = epub.EpubHtml(title=ch["title"], file_name=ch["file"], lang=lang)
        with open(Path(out_dir) / ch["file"], encoding="utf-8") as cf:
            c.content = cf.read()
        c.add_link(href="style/default.css", rel="stylesheet", type="text/css")
        book.add_item(c)
        toc.append(epub.Link(ch["file"], ch["title"], f"chap_{i}"))
        spine.append(c)
    if volumes:
        nested = []
        for k, (vtitle, members) in enumerate(volumes):
            # 父节点指向首章(members[0])，首章不再作为子项列出，避免父/子同 href 重复
            children = tuple(toc[idx] for idx in members if idx != members[0])
            parent = epub.Link(chapters[members[0]]["file"], vtitle, f"vol_{k}")
            nested.append((parent, children))
        book.toc = tuple(nested)
    else:
        book.toc = tuple(toc)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    return book
