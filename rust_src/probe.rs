//! 诊断探针（不参与交付）：复刻 parse_chapters 的数据流，分三段计时 + 抓最慢行。
//! 仅用于定位性能瓶颈，不改任何匹配逻辑。
use std::io::Read;
use std::time::Instant;
use txt_engine::{match_at_start, parse_chapters, splitlines_keepends};
use txt_engine::translator::compile_re;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut pattern = String::new();
    let mut input: Option<String> = None;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--pattern" => {
                pattern = args.get(i + 1).cloned().unwrap_or_default();
                i += 2;
            }
            "--input" => {
                input = args.get(i + 1).cloned();
                i += 2;
            }
            _ => i += 1,
        }
    }

    let text = if let Some(f) = input {
        std::fs::read_to_string(&f).unwrap_or_else(|e| panic!("read {}: {}", f, e))
    } else {
        let mut s = String::new();
        std::io::stdin().read_to_string(&mut s).unwrap();
        s
    };

    // 段1：compile_re（每文件仅一次；拆分只把这一个函数挪到了 translator.rs）
    let t0 = Instant::now();
    let re = compile_re(&pattern);
    let t_compile = t0.elapsed();

    // 段2：splitlines（原版 str.splitlines(keepends=True)）
    let t1 = Instant::now();
    let lines = splitlines_keepends(&text);
    let t_split = t1.elapsed();

    // 段3：逐行热路径（与 parse_chapters 完全相同：预筛 + match_at_start）
    let t2 = Instant::now();
    let mut candidates = 0u64;
    let mut matched = 0u64;
    let mut slow: Vec<(u128, usize, String)> = Vec::new();
    if let Some(r) = &re {
        for line in &lines {
            let stripped = line.trim_start();
            let is_cand = stripped.chars().count() < 80 && !stripped.is_empty();
            if is_cand {
                candidates += 1;
                let lt = Instant::now();
                let m = match_at_start(r, line);
                let d = lt.elapsed().as_micros();
                if m {
                    matched += 1;
                }
                if d > 1000 {
                    let snip: String = line.trim().chars().take(50).collect();
                    slow.push((d, line.chars().count(), snip));
                }
            }
        }
    }
    let t_match = t2.elapsed();

    // 对照：直接调 parse_chapters 的总时间
    let t3 = Instant::now();
    let ch = parse_chapters(&text, &pattern);
    let t_total = t3.elapsed();

    println!("=== PROBE(parse_txt_probe) ===");
    println!("pattern len : {} chars", pattern.len());
    println!("re compiled : {}", re.is_some());
    println!("compile_re  : {:?}  <-- 每文件仅调用 1 次（拆分挪走的正是它）", t_compile);
    println!("splitlines  : {:?}  lines={}", t_split, lines.len());
    println!("match loop  : {:?}  candidates={} matched={}", t_match, candidates, matched);
    println!("parse_chapters total: {:?}  chapters={}", t_total, ch.len());
    println!("=> 热路径占比: {:.1}%",
        t_match.as_nanos() as f64 / (t_compile + t_split + t_match).as_nanos() as f64 * 100.0);
    slow.sort_by(|a, b| b.0.cmp(&a.0));
    println!("--- 最慢的候选行 (>=1000us) 共 {} 个，列前 10 ---", slow.len());
    for (d, len, snip) in slow.iter().take(10) {
        println!("   {} us | {} chars | '{}'", d, len, snip);
    }
}
