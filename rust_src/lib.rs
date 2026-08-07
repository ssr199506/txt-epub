//! 匹配引擎（Rust 侧，纯 Rust / fancy-regex，零 C 依赖、零弹窗）。
//!
//! 只做一件事：按调用方传入的 `pattern`（来自 `exportTxtTocRule..json` 的规则字符串，
//! 或原版内置 `TOC_RE.pattern`）对文本逐行匹配、切分章节。
//! **正则一律由 Python 侧从 JSON 传入，Rust 不硬编码任何正则。**
//!
//! 复刻原工程 `parse_txt` 语义：逐行 `compiled.match(line)`（锚定行首，无 `re.M`）。
//!
//! ── 为什么需要「翻译层」以及如何做到零 C ──
//! fancy-regex 是纯 Rust 回溯引擎，支持定长环视（含定宽后顾）与全部前顾，
//! 但**不支持变长后顾**（编译期报 `LookBehindNotConst`，与标准 `regex` 一致）。
//! 而原工程运行在 Python 3.12，其 `re` 已原生支持变长后顾（PEP 679）。
//! 于是某些 JSON 规则（如晋江 `(?<=[\s　]{0,4})`）在 fancy-regex 下编译失败。
//!
//! 关键洞察：原版用 `re.match`（行首锚定），后顾在「行首 pos 0」被求值——pos 0 前无字符，
//! 因此变长后顾的语义被完全确定：
//!   - min-0 后顾 `(?<=[\s　]{0,4})` 在 pos 0 为 0 宽成功 → **等价「去掉它」**（剥离）；
//!   - min≥1 后顾 `(?<=[　\s])` 在 pos 0 永远失败 → **整条规则原版亦从不匹配（死规则）**；
//!   - 负后顾 `(?<!X)` 在 pos 0 「前无 X」恒为真 → **等价「去掉它」**（剥离）。
//! 只要后顾是「前导」（其前只有 0 宽结构，如 `^`/标志/其它环视），该结论恒成立。
//!
//! 故翻译层做法：先尝试直接用 fancy-regex 编译（TOC_RE、定宽环视、前顾等最忠实）；
//! 若失败，扫描 pattern 中的「前导」后顾，按形状（纯空白类 + 最小重复数）判定——
//!   min-0/负 → 剥离；min≥1/非纯空白/非前导 → 标记死规则（返回 None，绝不静默误匹配）。
//! 剥离后的规则交给 fancy-regex 编译，逐位等价复刻原版 `re.match`。
//! 全程纯 Rust、零 C 编译、零弹窗。
//!
//! 翻译层的完整实现见 [`translator`](crate::translator) 模块（独立的 `src/translator.rs`）。
//!
//! 两个入口一一对应原版：
//! - `parse_chapters`  ← `parse_txt`（含候选行预筛：strip 后 <80 字且非空）
//! - `parse_chunk`     ← `_parse_chunk`（无预筛；非首段收集首个标题前内容作 overflow）

use fancy_regex::Regex;
pub mod translator;

/// 等价 Python `str.splitlines(keepends=True)`，覆盖 `\r\n`/`\r`/`\n` 及 Unicode 行分隔符，并保留行尾。
pub fn splitlines_keepends(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let n = chars.len();
    let mut out: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut i = 0;
    while i < n {
        let c = chars[i];
        if c == '\r' {
            if i + 1 < n && chars[i + 1] == '\n' {
                cur.push('\r');
                cur.push('\n');
                out.push(std::mem::take(&mut cur));
                i += 2;
                continue;
            }
            cur.push('\r');
            out.push(std::mem::take(&mut cur));
            i += 1;
            continue;
        } else if c == '\n' {
            cur.push('\n');
            out.push(std::mem::take(&mut cur));
            i += 1;
            continue;
        } else if matches!(
            c,
            '\u{000B}' | '\u{000C}' | '\u{001C}' | '\u{001D}' | '\u{001E}' | '\u{0085}'
                | '\u{2028}' | '\u{2029}'
        ) {
            cur.push(c);
            out.push(std::mem::take(&mut cur));
            i += 1;
            continue;
        } else {
            cur.push(c);
            i += 1;
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}


/// 复刻 `compiled.match(line)`：从字符串起点（pos 0）匹配。
/// `re.match` 只从字符串起点尝试；用 `find` 取最左匹配后断言起点为 0 即完全等价，
/// 即使规则含 `^...$`（`m` 行内行首）或后顾，也只会接受「从整行起点开始」的匹配。
///
/// 关键：行尾先剥掉 `\n`/`\r`。原版 `re.match` 在行尾换行「之前」用 `$` 锚定
/// （Python 的 `$` 恒匹配串尾或串尾换行之前）；而 fancy-regex 非多行模式的 `$` 只锚定
/// 整体串尾。剥掉行尾换行后，`$` 锚定到行内容末尾，与原版逐位等价。
pub fn match_at_start(re: &Regex, line: &str) -> bool {
    let l = line.trim_end_matches(['\n', '\r']);
    match re.find(l) {
        Ok(Some(m)) => m.start() == 0,
        _ => false,
    }
}

/// 章节在源文本中的「字节偏移」范围（body 部分，不含标题行）。
/// `start`/`end` 指向 UTF-8 源文件（或与之等价的 `_full_text` 解码串）的字节偏移，
/// 双击预览 / 打包 EPUB 时按偏移直接 `seek` 读取，无需回传正文。
pub struct ChapterRange {
    pub title: String,
    pub start: usize,
    pub end: usize,
}

/// `splitlines_keepends` 的切片版：返回每行 `(字节偏移, &str 切片)`，零拷贝。
/// 用于 `parse_chapters` 计算章节的字节坐标（小说文本天然逐行、标题独占一行）。
pub fn splitlines_slices<'a>(text: &'a str) -> Vec<(usize, &'a str)> {
    // 纯字节扫描：UTF-8 的多字节序列每个字节都 >= 0x80，绝不会与 b'\n' / b'\r' 相撞，
    // 因此按字节找行尾是安全的，且免去 chars().collect() 的巨额分配（40MB 文本可省数百 MB）。
    // 与原版 `lines = list(f)` 对齐：只按 \n（含 \r\n / 裸 \r）分行，不处理 \u2028 等扩展换行。
    let bytes = text.as_bytes();
    let n = bytes.len();
    if n == 0 {
        return Vec::new();
    }
    let mut out: Vec<(usize, &'a str)> = Vec::with_capacity(n / 24 + 16);
    let mut cur = 0usize; // 当前行起始字节偏移
    let mut i = 0usize;
    while i < n {
        let b = bytes[i];
        if b == b'\n' {
            out.push((cur, &text[cur..i + 1]));
            cur = i + 1;
            i += 1;
        } else if b == b'\r' {
            let end = if i + 1 < n && bytes[i + 1] == b'\n' {
                i + 2
            } else {
                i + 1
            };
            out.push((cur, &text[cur..end]));
            cur = end;
            i = end;
        } else {
            i += 1;
        }
    }
    if cur < n {
        out.push((cur, &text[cur..n]));
    }
    out
}

/// 复刻 `txt_to_epub_core.parse_txt` 的逐行匹配内核。
///
/// 与原文逐位等价的「候选预筛 + match_at_start」逻辑，但不再把正文塞回 `Vec<String>`，
/// 而是记录每个章节的 **字节偏移** `{title, start, end}`（body 范围，不含标题行），
/// 正文按需从源文件 `seek` 读取。原版把正文拼进 `buffer.concat()`；这里 buffer 只保留
/// `&str` 切片以计算偏移——两者对「标题/正文」的切分完全一致。
/// 已编译的匹配引擎：编译一次，可在大量文件间复用，是未来批处理 / 高并发的基础。
///
/// 内部持有翻译层编译出的 `Regex`（死规则为 `None`）。`Option<Regex>` 默认满足 `Sync + Send`，
/// 可安全跨线程共享（如未来用 `rayon` 并行遍历文件目录、各线程持有同一引擎）。
pub struct CompiledEngine {
    re: Option<Regex>,
}

impl CompiledEngine {
    pub fn new(pattern: &str) -> Self {
        CompiledEngine {
            re: translator::compile_re(pattern),
        }
    }

    /// 用已编译引擎匹配 `text`，等价于 [`parse_chapters`]，但不重复编译正则。
    pub fn parse_chapters(&self, text: &str) -> Vec<ChapterRange> {
        parse_chapters_with_re(text, &self.re)
    }
}

/// `parse_chapters` 的内部版本：`re` 由调用方传入（可来自 [`CompiledEngine`] 复用的编译结果）。
fn parse_chapters_with_re(text: &str, re: &Option<Regex>) -> Vec<ChapterRange> {
    let lines = splitlines_slices(text);
    let mut chapters: Vec<ChapterRange> = Vec::new();
    let mut buffer: Vec<(usize, &str)> = Vec::new(); // (字节起始, 行切片)
    let mut curr_title = "前言".to_string();
    let mut curr_title_start: usize = 0;

    for (line_start, line) in lines {
        // 候选预筛：等原版 `stripped = line.lstrip(); len(stripped) < 80 and stripped != ""`
        let stripped = line.trim_start(); // 等价 Python lstrip()
        // 长度判定只需知道「是否 < 80 字符」，take(80) 早停，避免对长正文行做全量计数
        let is_candidate = !stripped.is_empty() && stripped.chars().take(80).count() < 80;
        let matched = is_candidate && re.as_ref().map_or(false, |r| match_at_start(r, line));
        if matched {
            // 等原版：仅当 buffer 非空才落章；连续标题行时前一个标题被直接覆盖（不产生空章）
            if !buffer.is_empty() {
                let body_start = buffer.first().unwrap().0;
                let last = buffer.last().unwrap();
                let body_end = last.0 + last.1.len();
                chapters.push(ChapterRange {
                    title: curr_title.trim().to_string(),
                    start: body_start,
                    end: body_end,
                });
                buffer.clear();
            }
            curr_title = line.trim().to_string();
            curr_title_start = line_start;
        } else {
            buffer.push((line_start, line));
        }
    }
    // 保存最后一个章节（等原版：buffer 非空 或 尚无章节时）
    if !buffer.is_empty() || chapters.is_empty() {
        let body_start = if buffer.is_empty() {
            curr_title_start
        } else {
            buffer.first().unwrap().0
        };
        let body_end = if buffer.is_empty() {
            curr_title_start
        } else {
            let last = buffer.last().unwrap();
            last.0 + last.1.len()
        };
        chapters.push(ChapterRange {
            title: curr_title.trim().to_string(),
            start: body_start,
            end: body_end,
        });
    }
    chapters
}

pub fn parse_chapters(text: &str, pattern: &str) -> Vec<ChapterRange> {
    parse_chapters_with_re(text, &translator::compile_re(pattern))
}

/// 复刻 `txt_to_epub_core._parse_chunk` 的匹配 + 切口修正。
/// `first_chunk=true` 时不收集 overflow（首段整段参与切分）。
pub fn parse_chunk(
    text: &str,
    pattern: &str,
    first_chunk: bool,
) -> (String, Vec<(String, String)>) {
    let re = translator::compile_re(pattern);
    let lines = splitlines_keepends(text);
    let mut overflow_lines: Vec<String> = Vec::new();
    let mut chapters: Vec<(String, String)> = Vec::new();
    let mut buffer: Vec<String> = Vec::new();
    let mut curr_title = "前言".to_string();
    let mut first_title_found = false;

    for line in &lines {
        // 切口修正：非首段先收集首个标题前的内容
        if !first_chunk && !first_title_found {
            let is_title = re.as_ref().map_or(false, |r| match_at_start(r, line));
            if is_title {
                first_title_found = true;
            } else {
                overflow_lines.push(line.clone());
                continue;
            }
        }
        // 注意：_parse_chunk 内核「无」候选行预筛（与原版一致）
        let matched = re.as_ref().map_or(false, |r| match_at_start(r, line));
        if matched {
            if !buffer.is_empty() {
                chapters.push((curr_title.trim().to_string(), buffer.concat()));
                buffer.clear();
            }
            curr_title = line.trim().to_string();
        } else {
            buffer.push(line.clone());
        }
    }

    if !first_chunk && !first_title_found {
        // 整段无标题：全部作 overflow，不创建章节
        let overflow = overflow_lines.concat() + &buffer.concat();
        (overflow, Vec::new())
    } else {
        if !buffer.is_empty() || chapters.is_empty() {
            chapters.push((curr_title.trim().to_string(), buffer.concat()));
        }
        (overflow_lines.concat(), chapters)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toc_re_default() {
        let toc = r"(?im)^.{0,6}(?:[引楔]子|正文(?!完|结)|[引序前]言|[序终]章|扉页|第\s{0,4}[\d〇零一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]+?\s{0,4}(?:章|节(?!课)|卷|篇(?!张))).{0,40}$|^.{0,6}[\d〇零一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟a-z]{1,8}[、. 　].{0,20}$";
        // 标题之间必须有「非标题」正文，否则相邻标题只更新 curr_title 不产生章节（与原版一致）
        let text = "前言\n故事从这个安静的村庄开始。\n第一章 开篇\n少年踏上了漫长的旅程。\n第二章 发展\n风波在远方悄然酝酿。\n";
        let ch = parse_chapters(text, toc);
        let titles: Vec<&str> = ch.iter().map(|c| c.title.as_str()).collect();
        assert!(titles.iter().any(|t| t.contains("第一章")), "titles={:?}", titles);
        assert!(titles.iter().any(|t| t.contains("第二章")), "titles={:?}", titles);
    }

    #[test]
    fn variable_width_lookbehind_stripped() {
        let rule = r"(?<=[\s　]{0,4})(?:[◎].{1,30}|(?:内容|文章)?简介|前言|序章|楔子|正文(?!完|结)|终章|后记|尾声)[ 　]{0,4}$";
        let text = "前言\n故事从这个安静的村庄开始。\n◎符号章节名\n少年踏上了漫长的旅程。\n后记\n风波在远方悄然酝酿。\n";
        let ch = parse_chapters(text, rule);
        let titles: Vec<&str> = ch.iter().map(|c| c.title.as_str()).collect();
        assert!(titles.iter().any(|t| t.contains("前言")), "{:?}", titles);
        assert!(titles.iter().any(|t| t.contains("◎符号章节名")), "{:?}", titles);
        assert!(titles.iter().any(|t| t.contains("后记")), "{:?}", titles);
    }

    #[test]
    fn min1_lookbehind_is_dead() {
        let rule = r"(?<=[　\s])\d+\.?[ 　\t]{0,4}$";
        let text = "123\n456\n789\n";
        let ch = parse_chapters(text, rule);
        assert_eq!(ch.len(), 1, "chapters={:?}", ch);
        assert_eq!(ch[0].title, "前言");
    }

    #[test]
    fn negative_lookbehind_stripped() {
        let rule = r"(?<![0-9])\d+\.?[ 　\t]{0,4}$";
        let text = "第一章\n内容\n第2章\n内容2\n";
        let ch = parse_chapters(text, rule);
        let titles: Vec<&str> = ch.iter().map(|c| c.title.as_str()).collect();
        assert_eq!(ch.len(), 1, "titles={:?}", titles);
        assert_eq!(ch[0].title, "前言");
    }

}
