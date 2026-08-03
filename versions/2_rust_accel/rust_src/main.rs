//! CLI 入口：被 Python 侧桥接层当作子进程调用。
//!
//! 设计要点（最小改动 + 轻量索引）：
//! - 不硬编码正则，`--pattern` 来自 JSON 规则或内置 TOC_RE。
//! - 纯 UTF-8 工作：Python 侧把源文件按原编码流式解码成 UTF-8 临时文件后，把**路径**传给本程序，
//!   本程序读该 UTF-8 文件（规避 Rust 不支持 gbk 的问题，也复用原版 errors="ignore" 语义）。
//!
//! 三种模式：
//!   [默认] parse  → 读 --source(UTF-8) 逐行匹配，输出 **轻量索引** JSON：
//!       {"source": "<路径>", "chapters": [{"title","start","end"}, ...]}
//!       start/end 是字节偏移，指向 --source 文件（双击预览/打包按需 seek 读取，不回传正文）。
//!   chunk         → 兼容旧批量并行路径：读 --source/--input/stdin，输出带正文的
//!       {"overflow": "...", "chapters": [[标题, 内容], ...]}（legacy）。
//!   pack          → 读 --source(UTF-8) + --index(索引JSON)，把每章物化为最终 xhtml 小文件，
//!       并写出 manifest.json（打包指南）。Python 侧按 manifest 组装 EPUB，不再流式读源。

use std::io::Read;
use std::process;
use serde_json::{json, Value};
use txt_engine::{parse_chapters, parse_chunk};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut pattern = String::new();
    let mut mode = "parse".to_string();
    let mut first_chunk = false;
    let mut source: Option<String> = None;
    let mut pack = false;
    let mut index: Option<String> = None;
    let mut out: Option<String> = None;
    let mut book_title = String::new();

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--pattern" => {
                pattern = args.get(i + 1).cloned().unwrap_or_default();
                i += 2;
            }
            "--mode" => {
                mode = args.get(i + 1).cloned().unwrap_or_else(|| "parse".into());
                i += 2;
            }
            "--first-chunk" => {
                first_chunk = true;
                i += 1;
            }
            "--source" | "--input" => {
                source = args.get(i + 1).cloned();
                i += 2;
            }
            "--pack" => {
                pack = true;
                i += 1;
            }
            "--index" => {
                index = args.get(i + 1).cloned();
                i += 2;
            }
            "--out" => {
                out = args.get(i + 1).cloned();
                i += 2;
            }
            "--book-title" => {
                book_title = args.get(i + 1).cloned().unwrap_or_default();
                i += 2;
            }
            _ => {
                i += 1;
            }
        }
    }

    if pack {
        match (source.clone(), index.clone(), out.clone()) {
            (Some(s), Some(ix), Some(o)) => {
                pack_chapters(&s, &ix, &o, &book_title);
                return;
            }
            _ => {
                eprintln!("--pack 需要 --source --index --out");
                process::exit(2);
            }
        }
    }

    if pattern.is_empty() {
        eprintln!("--pattern 必填（匹配正则，来自 JSON 规则或内置 TOC_RE）");
        process::exit(2);
    }

    let text = if let Some(f) = &source {
        match std::fs::read_to_string(f) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("读取 {} 失败: {}", f, e);
                process::exit(1);
            }
        }
    } else {
        let mut s = String::new();
        if std::io::stdin().read_to_string(&mut s).is_err() {
            eprintln!("读取 stdin 失败");
            process::exit(1);
        }
        s
    };

    if mode == "chunk" {
        let (overflow, chapters) = parse_chunk(&text, &pattern, first_chunk);
        let arr: Vec<Value> = chapters.iter().map(|(t, c)| json!([t, c])).collect();
        println!("{}", json!({ "overflow": overflow, "chapters": arr }));
    } else {
        let chapters = parse_chapters(&text, &pattern);
        let arr: Vec<Value> = chapters
            .iter()
            .map(|c| json!({ "title": c.title, "start": c.start, "end": c.end }))
            .collect();
        println!(
            "{}",
            json!({ "source": source.unwrap_or_default(), "chapters": arr })
        );
    }
}

/// 把每章按字节偏移从源文件切片，物化为最终 xhtml 小文件，并写出 manifest.json（打包指南）。
fn pack_chapters(source: &str, index_path: &str, out_dir: &str, book_title: &str) {
    let bytes = match std::fs::read(source) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("读取源文件 {} 失败: {}", source, e);
            process::exit(1);
        }
    };
    let idx_text = match std::fs::read_to_string(index_path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("读取索引 {} 失败: {}", index_path, e);
            process::exit(1);
        }
    };
    let v: Value = match serde_json::from_str(&idx_text) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("索引 JSON 解析失败: {}", e);
            process::exit(1);
        }
    };
    let chapters = match v["chapters"].as_array() {
        Some(c) => c,
        None => {
            eprintln!("索引缺少 chapters 数组");
            process::exit(1);
        }
    };
    if std::fs::create_dir_all(out_dir).is_err() {
        eprintln!("创建输出目录失败: {}", out_dir);
        process::exit(1);
    }

    let mut manifest_chapters: Vec<Value> = Vec::with_capacity(chapters.len());
    for (i, ch) in chapters.iter().enumerate() {
        let title = ch["title"].as_str().unwrap_or("").to_string();
        let start = ch["start"].as_u64().unwrap_or(0) as usize;
        let end = ch["end"].as_u64().unwrap_or(0) as usize;
        let s = start.min(bytes.len());
        let e = end.min(bytes.len());
        let slice = if s <= e { &bytes[s..e] } else { &bytes[0..0] };
        let body = String::from_utf8_lossy(slice);
        // 与原版 build_epub 一致：不转义，按非空行包 <p>
        let paragraphs: Vec<String> = body
            .lines()
            .map(|l| l.trim())
            .filter(|l| !l.is_empty())
            .map(|l| format!("<p>{}</p>", l))
            .collect();
        let content = format!("<h2 class=\"head\">{}</h2>\n{}", title, paragraphs.join("\n"));
        let file_name = format!("chap_{:03}.xhtml", i + 1);
        let out_path = std::path::Path::new(out_dir).join(&file_name);
        if std::fs::write(&out_path, content.as_bytes()).is_err() {
            eprintln!("写入 {} 失败", file_name);
            process::exit(1);
        }
        manifest_chapters.push(json!({ "file": file_name, "title": title }));
    }
    let manifest = json!({ "book_title": book_title, "chapters": manifest_chapters });
    let manifest_path = std::path::Path::new(out_dir).join("manifest.json");
    if std::fs::write(&manifest_path, manifest.to_string()).is_err() {
        eprintln!("写入 manifest.json 失败");
        process::exit(1);
    }
}
