//! 翻译层（JSON 正则 → fancy-regex 兼容正则）。
//!
//! 这是介于「JSON 规则字符串」与「Rust 匹配引擎」之间的一层轻量转换。
//! 它**不硬编码任何正则**，只识别传入 pattern 中「后顾（look-behind）」的**形状**，
//! 按规则改写后交给 fancy-regex 编译，从而让原工程 `exportTxtTocRule..json` 里的
//! 规则（含变长后顾）在纯 Rust 下也能逐位等价复刻 Python `re.match` 语义。
//!
//! ── 为什么需要它 ──
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
//! ── 做法 ──
//! `compile_re` 先尝试直接用 fancy-regex 编译（TOC_RE、定宽环视、前顾等最忠实）；
//! 若失败，扫描 pattern 中的「前导」后顾，按形状（纯空白类 + 最小重复数）判定——
//!   min-0/负 → 剥离；min≥1/非纯空白/非前导 → 标记死规则（返回 None，绝不静默误匹配）。
//! 剥离后的规则交给 fancy-regex 编译，逐位等价复刻原版 `re.match`。
//! 全程纯 Rust、零 C 编译、零弹窗。
//!
//! 本模块对「形状」通用：同形状规则（不同字符/参数）自动兼容，不写死任何具体字符串。

use fancy_regex::Regex;

/// Unicode 空白字符（与 Python `\s` 基本一致：含 `\t\n\v\f\r ` 及全角空格等）。
fn is_ws_char(c: char) -> bool {
    c.is_whitespace()
}

/// 判断字符类体（不含外层 `[]`）是否「纯空白」。含取反 `^`、POSIX `[:...:]`、非空白转义/字面 → 非纯空白。
/// 按 `char` 迭代，正确识别全角空格等多字节空白。
fn class_body_is_ws(body: &str) -> bool {
    if body.starts_with('^') {
        return false; // 取反类能匹配非空白 → 非「纯空白」
    }
    let mut chars = body.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.next() {
                Some('s') | Some('t') | Some('n') | Some('r') | Some('f') | Some('v') => continue,
                _ => return false, // \d \w \S \b 等 → 非纯空白
            }
        } else if c == '[' {
            return false; // POSIX 类如 [:space:] → 不当纯空白
        } else if c == ']' {
            continue; // 理论上 body 不含外层]，容忍
        } else if c == '-' {
            continue; // 孤立连字符或范围的一部分，单独出现时不构成非空白
        } else if chars.peek() == Some(&'-') {
            // 范围 x-y
            chars.next(); // 消费 '-'
            match chars.next() {
                Some(to) if is_ws_char(c) && is_ws_char(to) => continue,
                _ => return false,
            }
        } else if !is_ws_char(c) {
            return false;
        }
    }
    true
}

/// 解析后顾内部表达式后的「最小重复数」：无量词=1, *=0, +=1, ?=0, {m}=m, {m,}=m, {m,n}=m。
fn quant_min(q: &str) -> u32 {
    let q = q.trim();
    if q.is_empty() {
        return 1;
    }
    if q == "*" {
        return 0;
    }
    if q == "+" {
        return 1;
    }
    if q == "?" {
        return 0;
    }
    if q.starts_with('{') {
        if let Some(close) = q.find('}') {
            let inside = &q[1..close];
            if let Some(comma) = inside.find(',') {
                inside[..comma].parse::<u32>().unwrap_or(1)
            } else {
                inside.parse::<u32>().unwrap_or(1)
            }
        } else {
            1
        }
    } else {
        1
    }
}

/// 分析后顾内部：返回 (是否纯空白类, 最小重复数)。无法判定 → (false, 1) 交给上层判死。
fn inner_classify(inner: &str) -> (bool, u32) {
    let inner = inner.trim();
    let (body_opt, rep_min): (Option<String>, u32) = if let Some(_rest) = inner.strip_prefix('[') {
        if let Some(close) = inner.find(']') {
            let body = &inner[1..close];
            (Some(body.to_string()), quant_min(&inner[close + 1..]))
        } else {
            (None, 1)
        }
    } else if inner.starts_with('\\') {
        // 单个转义原子 \x（x 为单字母）
        let atom_len = 2.min(inner.len()); // \ + 1 字母
        let atom = &inner[..atom_len];
        let is_ws_atom = matches!(atom, "\\s" | "\\t" | "\\n" | "\\r" | "\\f" | "\\v");
        let after = &inner[atom_len..];
        if is_ws_atom {
            (Some(atom.to_string()), quant_min(after))
        } else {
            (None, quant_min(after))
        }
    } else {
        let ch = inner.chars().next();
        let after = match ch {
            Some(c) => &inner[c.len_utf8()..],
            None => "",
        };
        (ch.filter(|c| is_ws_char(*c)).map(|c| c.to_string()), quant_min(after))
    };
    match body_opt {
        Some(body) => (class_body_is_ws(&body), rep_min),
        None => (false, rep_min),
    }
}

/// 扫描 pattern 中所有环视后顾的 span（含内部表达式）。跳过字符类/转义内部，避免误判。
/// 返回 (start, end_exclusive, is_negative, inner)。
pub(crate) fn scan_lookbehinds(pattern: &str) -> Vec<(usize, usize, bool, String)> {
    let b = pattern.as_bytes();
    let n = b.len();
    let mut out: Vec<(usize, usize, bool, String)> = Vec::new();
    let mut i = 0;
    while i + 3 < n {
        if b[i] == b'(' && b[i + 1] == b'?' && b[i + 2] == b'<' {
            let neg = b.get(i + 3) == Some(&b'!');
            // `(?<=` 与 `(?<!` 均为 4 字符前缀，后顾内容从 i+4 起
            let inner_start = i + 4;
            let mut depth = 0usize;
            let mut j = inner_start;
            let mut in_class = false;
            let mut closed = false;
            while j < n {
                let c = b[j];
                if in_class {
                    if c == b'\\' {
                        j += 2;
                        continue;
                    }
                    if c == b']' {
                        in_class = false;
                    }
                    j += 1;
                    continue;
                }
                if c == b'[' {
                    in_class = true;
                    j += 1;
                    continue;
                }
                if c == b'\\' {
                    j += 2;
                    continue;
                }
                if c == b'(' {
                    depth += 1;
                    j += 1;
                    continue;
                }
                if c == b')' {
                    if depth == 0 {
                        let inner = pattern[inner_start..j].to_string();
                        out.push((i, j + 1, neg, inner));
                        closed = true;
                        i = j + 1;
                        break;
                    }
                    depth -= 1;
                    j += 1;
                    continue;
                }
                j += 1;
            }
            if !closed {
                i += 1;
            }
        } else {
            i += 1;
        }
    }
    out
}

/// 判定 `prefix`（后顾之前的部分）是否全为 0 宽结构：`^` 与 `(?...)` 形式（标志/前顾/非捕获/后顾）。
/// 一旦出现消费性结构（字面/`.`/`[`/普通捕获组）→ 非前导。
fn is_zero_width_prefix(prefix: &str) -> bool {
    let b = prefix.as_bytes();
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        if c == b'^' {
            i += 1;
            continue;
        }
        if c == b'(' {
            if b.get(i + 1) == Some(&b'?') {
                i += 2;
                continue;
            }
            return false; // 普通捕获组（含内容）→ 消费性
        }
        return false; // 其它（字面/转义/字符类/`.`）→ 消费性
    }
    true
}

/// 翻译层：剥离 min-0/负「前导」后顾、标记 min≥1/非纯空白/非前导后顾为死规则。
/// 返回 None = 死规则（原版 re.match 亦从不匹配）。
pub(crate) fn transform_lookbehinds(pattern: &str) -> Option<String> {
    let lbs = scan_lookbehinds(pattern);
    if lbs.is_empty() {
        return Some(pattern.to_string());
    }
    let mut dead = false;
    let mut strips: Vec<(usize, usize)> = Vec::new();
    for (start, end, neg, inner) in &lbs {
        let leading = is_zero_width_prefix(&pattern[..*start]);
        if !leading {
            dead = true;
            break;
        }
        if *neg {
            strips.push((*start, *end)); // 负后顾在 pos 0 恒真 → 剥离
            continue;
        }
        let (all_ws, min_rep) = inner_classify(inner);
        if all_ws && min_rep == 0 {
            strips.push((*start, *end)); // min-0 后顾在 pos 0 为 0 宽成功 → 剥离等价
        } else {
            dead = true; // min≥1 或非纯空白：pos 0 无法成立 → 死规则
            break;
        }
    }
    if dead {
        return None;
    }
    if strips.is_empty() {
        return Some(pattern.to_string());
    }
    let mut s = pattern.to_string();
    let mut spans: Vec<(usize, usize)> = strips;
    spans.sort_by(|a, b| b.0.cmp(&a.0)); // 从后往前删，保持偏移有效
    for (a, b) in spans {
        s.replace_range(a..b, "");
    }
    Some(s)
}

/// 编译：先直接 fancy-regex（最忠实）；失败（变长后顾等）→ 走翻译层再编译；再失败 → None（死规则）。
pub fn compile_re(pattern: &str) -> Option<Regex> {
    if let Ok(r) = Regex::new(pattern) {
        return Some(r);
    }
    let t = transform_lookbehinds(pattern)?;
    Regex::new(&t).ok()
}
