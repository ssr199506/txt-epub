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
//!
//! 阶段 B 新增常驻服务模式：
//!   --serve       → 从 stdin 循环读 [4字节大端长度][UTF-8文本] 帧，逐帧切章，每行输出一个
//!       结果 JSON（含 chapters）。进程**只启动一次**，由 Python 侧喂多文件、后台读结果。
//!       解码红线：文本已由 Python 侧解码为 UTF-8，Rust 只切章，绝不碰 GBK（规避坑 2c）。

use std::io::{Read, Write};
use std::process;
use std::sync::Arc;
use std::sync::mpsc::{sync_channel, SyncSender, Receiver};
use std::collections::BTreeMap;
use serde_json::{json, Value};
use txt_engine::{parse_chapters, parse_chunk, CompiledEngine};
use encoding_rs::Encoding;
use rayon::ThreadPoolBuilder;

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
    let mut encoding = String::new();
    let mut serve = false;

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
            "--encoding" => {
                encoding = args.get(i + 1).cloned().unwrap_or_default();
                i += 2;
            }
            "--serve" => {
                serve = true;
                i += 1;
            }
            _ => {
                i += 1;
            }
        }
    }

    if pack {
        match (source.clone(), index.clone(), out.clone()) {
            (Some(s), Some(ix), Some(o)) => {
                pack_chapters(&s, &ix, &o, &book_title, &encoding);
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

    // 阶段 B-α：常驻服务模式，进程只启一次，喂多文件切章
    if serve {
        run_serve(&pattern);
        return;
    }

    let text = if encoding.is_empty() {
        if let Some(f) = &source {
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
        }
    } else {
        String::new() // raw 模式：不读 UTF-8，直接吃原始字节
    };

    if mode == "chunk" {
        let (overflow, chapters) = parse_chunk(&text, &pattern, first_chunk);
        let arr: Vec<Value> = chapters.iter().map(|(t, c)| json!([t, c])).collect();
        println!("{}", json!({ "overflow": overflow, "chapters": arr }));
    } else if !encoding.is_empty() {
        // raw 模式：直接吃原始字节，流式解码 + 原始偏移索引，复用 CompiledEngine
        let src = match &source {
            Some(s) => s.clone(),
            None => { eprintln!("--encoding 需要 --source"); process::exit(2); }
        };
        let bytes = match std::fs::read(&src) {
            Ok(b) => b,
            Err(e) => { eprintln!("读取 {} 失败: {}", src, e); process::exit(1); }
        };
        let engine = CompiledEngine::new(&pattern);
        let chapters = engine.parse_chapters_raw(&bytes, &encoding);
        let arr: Vec<Value> = chapters
            .iter()
            .map(|c| json!({ "title": c.title, "start": c.start, "end": c.end }))
            .collect();
        println!(
            "{}",
            json!({ "source": src, "encoding": encoding, "chapters": arr })
        );
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
/// `encoding` 非空时为 raw 模式：按原始字节切片后按原编码解码（非 UTF-8 临时文件）。
fn pack_chapters(source: &str, index_path: &str, out_dir: &str, book_title: &str, encoding: &str) {
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
        let body = if encoding.is_empty() {
            String::from_utf8_lossy(slice).to_string()
        } else {
            let enc = Encoding::for_label(encoding.as_bytes()).unwrap_or(encoding_rs::UTF_8);
            let (cow, _, _) = enc.decode(slice);
            cow.to_string()
        };
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

/// 阶段 B（常驻服务模式，单进程 + 多核）。从 stdin 循环读 `[4字节大端长度 L][L字节 UTF-8 文本]` 帧，
/// 带序号交给 rayon 线程池并行切章，结果按序号顺序流式输出 JSONL，直到 stdin EOF。
/// 进程只启动一次，跨 25 万文件复用，彻底消灭「每文件启一次子进程」的 20~50ms × 25 万次开销。
///
/// 流水线（生产者-消费者，带背压）：
///   读线程(stdin 帧) → frame_tx(sync_channel 限容 1024) → rayon 线程池并行 parse_chapters
///        → res_tx(sync_channel 限容 1024) → 主线程按序号顺序输出 stdout
/// 背压保证「在途帧」受限（1024），不会把 845GB 全量加载进内存。
///
/// 解码红线：帧内文本必须是 Python 侧已解码的 UTF-8；Rust 绝不碰原始 GBK 字节（规避坑 2c）。
/// 编译一次：`CompiledEngine` 在 `Arc` 下跨线程共享（`Regex` 是 `Option<Regex>`，满足 `Sync`）。
fn run_serve(pattern: &str) {
    let engine = Arc::new(CompiledEngine::new(pattern));
    let (frame_tx, frame_rx): (SyncSender<(usize, String)>, Receiver<(usize, String)>) = sync_channel(1024);
    let (res_tx, res_rx): (SyncSender<(usize, String)>, Receiver<(usize, String)>) = sync_channel(1024);

    // 读线程：从 stdin 循环读帧，带序号发给处理端
    let reader = std::thread::spawn(move || {
        let mut stdin = std::io::stdin();
        let mut len_buf = [0u8; 4];
        let mut seq = 0usize;
        loop {
            if stdin.read_exact(&mut len_buf).is_err() {
                break;
            }
            let l = u32::from_be_bytes(len_buf) as usize;
            let mut buf = vec![0u8; l];
            if stdin.read_exact(&mut buf).is_err() {
                break;
            }
            let text = match String::from_utf8(buf) {
                Ok(t) => t,
                Err(_) => {
                    eprintln!("帧非 UTF-8，跳过");
                    continue;
                }
            };
            if frame_tx.send((seq, text)).is_err() {
                break;
            }
            seq += 1;
        }
        // reader 结束 → frame_tx drop → frame_rx 收 EOF
    });

    // 处理端：rayon 线程池并行切章，结果带序号发回
    let pool = ThreadPoolBuilder::new().build().unwrap();
    pool.scope(move |s| {
        while let Ok((seq, text)) = frame_rx.recv() {
            let engine = Arc::clone(&engine);
            let res_tx = res_tx.clone();
            s.spawn(move |_| {
                let chapters = engine.parse_chapters(&text);
                let arr: Vec<Value> = chapters
                    .iter()
                    .map(|c| json!({ "title": c.title, "start": c.start, "end": c.end }))
                    .collect();
                let line = json!({ "chapters": arr }).to_string();
                let _ = res_tx.send((seq, line));
            });
        }
    });
    // scope 结束：所有 spawn 完成；res_tx 在此 drop → res_rx 收 EOF

    // 主线程：按序号顺序流式输出（保证 Python 侧按序关联 chapters）
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let mut next = 0usize;
    let mut pending: BTreeMap<usize, String> = BTreeMap::new();
    for (seq, line) in res_rx {
        pending.insert(seq, line);
        while let Some(l) = pending.remove(&next) {
            if out.write_all(l.as_bytes()).is_err() || out.write_all(b"\n").is_err() {
                return;
            }
            next += 1;
        }
    }
    for (_, l) in pending {
        let _ = out.write_all(l.as_bytes());
        let _ = out.write_all(b"\n");
    }
    let _ = reader.join();
}
