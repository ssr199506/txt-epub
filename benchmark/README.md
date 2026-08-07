# TXT→EPUB 多版本性能测试 · 管线文档

> 本模块从 2026-08-07 的 txt-epub-perf 测试工程中抽取沉淀，作为「同一工具多版本性能公平对比」的可复用模板。
> 下次做类似任务（换版本、换样本集、换机器），按本文档流程操作即可，无需重新摸索。
> 本文档为唯一权威入口；脚本内部的坑点也在「已知坑」一节集中登记。

---

## 1. 目录约定（脚本依赖此结构，勿改）

```
benchmark/                        ← 测试模块根（位于主项目内，已 gitignore 相关产物）
├── README.md                     本文档
├── probe/                        所有脚本（必须放这层：脚本用 __file__.parent.parent 定位根）
│   ├── bench.py                  测试调度器（计划生成 + 执行 + 断点续跑）
│   ├── bench_worker.py           单次 run 的执行函数（run_parse/full/batch/detect）
│   ├── analyze.py                小样本聚合 → summary.csv + 控制台汇总
│   ├── analyze_full.py           全集聚合 → summary_full.csv + 全量报告
│   ├── titles_check.py           正确性校验（三版本章节标题序列 diff）
│   ├── preflight.py              运行前 39 项检查（文件/规则/依赖/环境）
│   ├── prep_full_manifest.py     复制 TXT 全集进项目 + 生成 manifest（--src 指定源）
│   └── cargo_v2_min.toml         v2 重建 Cargo.toml 的最小配置（仓库漏传 Cargo.toml 时用）
├── versions/                     git archive 物化的三版本（v1_pure_python / v2_rust_accel / v3_current）
├── samples/                      小样本集（只读副本）
├── samples_full/                 全集样本集（只读副本）
├── output/
│   ├── logs/                     JSONL 运行日志（append-only，断点续跑的依据）
│   └── summary*.csv              聚合结果
├── report/                       聚合报告（md）
└── _待我删除/                     海量数据 / 中间过程文件集中地，测完亲手删
```

脚本一律用 `ROOT = Path(__file__).resolve().parent.parent` 定位模块根，**不要移动脚本位置**。
`versions/`、`samples*`、`output/`、`report/` 运行时可重建，全部可从 `_待我删除/` 或 git 恢复。

---

## 2. 前置条件

| 项 | 要求 |
|---|---|
| 系统 Python | `C:\Users\32133\AppData\Local\Microsoft\WindowsApps\python.exe`（3.12.10，自带 ebooklib / PIL / psutil / tkinterdnd2） |
| cargo | 1.96+（`cargo build --release`） |
| 源仓库 | 本主项目 git，需有 tags：`v1.0.0` / `v2.0.0` / `v3.0.0` |
| 杀软 | 360 对无签名新编译 exe 启发式误报（QVM），预期弹窗；解法：源码重建换 hash、加信任区，或用 VirusTotal 验证 |
| 铁律 | **启动测试程序必须先获用户明确批准**；方案确认 ≠ 执行许可 |

---

## 3. 完整流程（7 步）

### 步骤 1：物化三版本（公平性前提）

```bash
# 在测试工作目录（新建 benchmark 同级的临时工程目录，如 txt-epub-perf_YYYY-MM-DD/）下：
mkdir -p versions
git -C <主项目> archive --format=tar v1.0.0 | tar -x -C versions/v1_pure_python
git -C <主项目> archive --format=tar v2.0.0 | tar -x -C versions/v2_rust_accel
git -C <主项目> archive --format=tar v3.0.0 | tar -x -C versions/v3_current
```

要点：
- 三个版本各自独立目录，**exe 与 core.py 同目录**，互不引用。
- 规则 JSON（`exportTxtTocRule..json`）与 TOC_RE 三版本 md5 必须一致——排除「规则变量」，只测引擎差异（preflight B 项自动校验）。

### 步骤 2：编译 Rust（v2 / v3）

```bash
# v3（源码带 Cargo.toml）
cd versions/v3_current/rust && cargo build --release

# v2（仓库漏传 Cargo.toml，用模板重建——fancy-regex 0.13 + serde_json，不引 rayon/encoding_rs）
cp probe/cargo_v2_min.toml versions/v2_rust_accel/rust_src/Cargo.toml
cd versions/v2_rust_accel/rust_src && cargo build --release

# 产物：versions/vN/parse_txt_rust.exe
```

### 步骤 3：准备样本集 + manifest

- **小样本**：`samples/` 放 3~6 本不同编码/体积的书（只读副本），手写 `probe/sample_manifest.json`，schema：

```json
[{"id": "01", "file": "01_书名.txt", "orig_name": "书名.txt",
  "size_bytes": 123456, "size_mb": 1.18, "encoding": "utf-8",
  "md5": "...", "source": "原始路径"}]
```

- **全集**（大样本集）：`python probe/prep_full_manifest.py --src "D:\某目录"` —— 复制全部 `*.txt` 到 `samples_full/`（只读），整文件严格解码探测编码（utf-8→gb18030→big5，零截断误报），生成 `sample_manifest_full.json`。

### 步骤 4：生成测试计划（乱序可复现）

```bash
python probe/bench.py --dry          # 小样本：生成 bench_plan.json（126 run）
```

- 计划结构：`[{run_id, version, scene, sample, repeat_idx, warmup, ...}]`。
- **全局乱序**（seed 固定，如 20260807），把「每版本×每样本×3 重复」打散，排除环境漂移/缓存顺序干扰。
- 全集拆三份计划：`bench_plan_full_parse.json`（S1 切章 417 run）、`bench_plan_full_full.json`（S2 完整 417 run）、`bench_plan_full_batch.json`（S3 批量 18 run）。生成方式参照本次工程的 3 段构造代码（见本次报告附录/记忆），或按同 schema 手写。

### 步骤 5：运行前检查

```bash
python probe/preflight.py
```

39 项检查分五组：
- A 文件完整性：三版本目录、exe 存在与归属、样本 md5 与只读属性
- B 规则一致性：规则 JSON / TOC_RE md5 三版本一致
- C 运行时：系统 Python + ebooklib/PIL/psutil/tkinterdnd2
- D 探针：`bench.py --dry` 计划生成（乱序可复现）
- E 环境：磁盘空间、可用内存、**无残留 bench 进程**（psutil 查）、`output/logs` 为空

### 步骤 6：运行测试

```bash
# 小样本一条龙
python probe/bench.py --scene all

# 全集按场景分开跑（每场景一个后台任务，日志独立）
python probe/bench.py --manifest probe/sample_manifest_full.json --samples-dir samples_full \
    --plan probe/bench_plan_full_parse.json --log output/logs/bench_full_parse.jsonl --scene parse
# ... full / batch 同理
```

- 场景：`parse`（S1 切章）/ `full`（S2 完整转换，含打包，**EPUB 测完即删**）/ `batch`（S3 批量：v2 sub / v3 pipe / v3 serve）+ `detect`（编码探测）。
- 日志 JSONL append-only；`--resume` 按 run_id 跳过已完成 → **断点续跑幂等**。
- ⚠️ **每个场景只开一个任务**。会话中断后旧后台任务可能仍在写同一日志，会产生重复行——数据无害，聚合时按 run_id 去重即可（analyze_full.py 已内置）。
- ⚠️ 启动前必须用户批准。

### 步骤 7：聚合 + 出报告

```bash
python probe/analyze.py          # 小样本 → output/summary.csv
python probe/analyze_full.py     # 全集 → output/summary_full.csv + report/全量性能对比报告.md
```

- 统计口径：每 (scene, version, mode, sample_id) 取**中位数**；warmup 与失败 run 剔除。
- 全集报告含「每版本 wall_s = a + b·size_mb」分桶差分拟合 + 逐样本胜负 + 章数一致性 + 批量对照。

---

## 4. 脚本速查表

| 脚本 | 用途 | 关键参数 |
|---|---|---|
| `bench.py` | 调度：生成计划 + 执行 + 续跑 | `--scene parse/full/batch/detect/all`（可逗号组合）；`--dry` 只生成计划；`--resume` 续跑；`--limit N` 限量；`--manifest/--samples-dir/--plan/--log` 覆盖路径 |
| `bench_worker.py` | 单 run 执行函数（被 bench.py 调用） | run_parse / run_full / run_batch / run_detect |
| `analyze.py` | 小样本聚合 | `--csv` 输出路径 |
| `analyze_full.py` | 全集聚合 + 全量报告 | 无参（路径常量，按目录约定） |
| `titles_check.py` | 三版本章节标题序列 diff | 无参（对 6 样本 × 3 版本） |
| `preflight.py` | 运行前 39 项检查 | 无参（SYS_PY 若换机需改） |
| `prep_full_manifest.py` | 复制全集 + 生成 manifest | `--src <源目录>` |
| `cargo_v2_min.toml` | v2 重建 Cargo.toml 模板 | 复制为 rust_src/Cargo.toml |

---

## 5. 公平性原则（不可妥协）

1. 三版本各自独立物化目录 + **各自编译**的 exe，绝不用一个 exe 测三个版本。
2. 规则 JSON / TOC_RE md5 三版本一致（preflight 校验），只测引擎差异。
3. 每版本 × 每样本 × 3 重复，**全局乱序**（固定 seed 可复现），统计用中位数抗离群。
4. 固定开销 + 每 MB 速率线性拟合 → 求盈亏平衡体积点（如 v1/v2 ≈ 45MB：大于该体积 Rust 计算优势才盖过 exe 启动成本）。
5. 样本只读（chmod 444），防误改破坏对照。

---

## 6. 已知坑（全部实踩过，照着避）

| # | 现象 | 根因 / 规避 |
|---|---|---|
| 1 | v1 切章数比 v2/v3 少 1~3 章 | S1 对 v1 **必须走原生自解码路径**；传 `errors="ignore"` 预解码串给 splitlines 会丢字节。改法见本次 bench_worker.py run_parse 的 v1 分支 |
| 2 | `bench.py` 报 `global` 语法错 | `global MANIFEST, PLAN_FILE, ...` 声明必须放 `main()` 开头，不能放引用之后 |
| 3 | 日志行数 > 计划数，聚合 n 偏大 | 旧后台任务无 `--resume` 重跑同一 run_id 写重复行。**聚合前按 run_id 去重**（analyze_full.py 已内置） |
| 4 | 全集线性拟合出「负固定开销」假象 | 样本体积跨度太窄（如全在 4.7–9.8MB）时最小二乘数值不稳。改用分桶差分（最低/最高 25% 中位数），报告**不给假数字**，必要时引用跨度更大的小样本模型 |
| 5 | `output/vN/` 堆大量孤儿 EPUB | run_full 测完即删，但**中断的任务会留**。清理：`python -c` 或手动删；批量超阈值时安全机制要求确认（→ 数据集中放 `_待我删除/` 让用户亲手删） |
| 6 | `tasklist` 输出 GBK 解码异常 | 残留进程检查用 psutil，不用 tasklist 管道 |
| 7 | manifest 生成脚本 `\U` 转义报错 | 文档串写 `r"..."` raw 字符串 |
| 8 | 小样本 v1 必赢、v2/v3 显得「负优化」 | Rust exe 启动固定成本 ~0.4–0.5s，小文件上盖过计算优势，**符合模型预期**，不是负优化。看结论要分场景：单本小文件 / 超大单本 / 批量 |
| 9 | `prep_full_manifest.py` 硬编码路径 | 已模板化：ROOT 自动定位，`--src` 覆盖源目录。换机只需改 preflight.py 的 `SYS_PY` |

---

## 7. 数据管理纪律

- 样本只读；测试产物全部在 `benchmark/` 或工程目录内，**不散落桌面/临时目录**。
- 海量数据与中间过程文件（样本副本、日志、孤儿 EPUB、计划 JSON、过程文档）→ 集中 `_待我删除/`，**测完用户亲手删**。
- 已产出的正确数据视为资产：跑前快照备份、跑中只追加/checkpoint、绝不删除已有正确结果、绝不改原始源文件。
- 实验性改动在副本上进行，不碰原/基线文件。
- 启动测试 / 批量程序：先出方案计划书 → 用户确认 → preflight → **用户批准** → 才执行。

---

## 8. 本次实测结论速查（2026-08-07，46 本 / 400MB）

- 单本场景（8MB 级）：v1 最快（S1 0.27s / S2 2.64s），v3 居中，v2 最慢——v1 零进程零搬运，exe 启动成本是 v2/v3 的固定负担。
- v3 相对 v2 的修复是实的（stdin 零落盘 + serve 常驻，批量快 5.7 倍），但从未与 v1 同场竞技（v1 没有进程成本）。
- 真实场景（海量 10MB 级小说）：**v1 + 批量脚本是更优解**；v3 的可辩护优势只剩 fancy-regex 规则能力与编码自动检测。
- 后续改进方向（raw 模式解码下沉 Rust）：见 `reports/后续改进方向.md`。
