# 全局调度器实现计划书（纯 Python 理想模型）

> 版本：v0.4（2026-08-07，基底决策定案：**从 main 分出，外科手术切除 3 处 Rust 委派，保留纯 Python 有益资产**）
> 状态：**计划阶段**——本文档只规划，不改任何代码
> 目标函数：**代价尽量小地完美实现用户抽象模型**（不成环的线 / 任意切分 / 编码分链 / 动态分配 / 传引用不传数据）
> 依据：用户理想模型 + 代码真源调研（txt_to_epub_core.py / txt_to_epub_gui_2.py / encoding_detect.py）

---

## 1. 背景与目标

### 1.1 用户的理想模型（唯一需求来源）

数据抽象结构是一条**不成环的线**：

1. **任意切分**：单段切出来只依赖紧邻数据；按编号拼接，任何切分方式都不会产生错误。
2. **等价实体**：1 万个 1MB 文件 与 1 千个 10MB 文件，是同一个抽象实体。
3. **编码分链**：同编码文件拼成一条链，四种编码 = 四条长短不一的链。
4. **动态分配**：跑完短的，把长的切开塞到空闲 worker 接着跑；进程数 = 核数。
5. **传引用不传数据**：跨进程只传任务单（路径+行号），正文由 worker 自读磁盘。

### 1.2 目标

- **纯 Python 实现**，Rust 降级为 worker 内可选加速器（不强依赖）。
- **复用现有纯函数组件**，只新写调度层；新代码全放独立包，不动原文件。
- 正确性优先：输出与现有 `convert_single` 完全一致（parity 红线）。

### 1.3 非目标

- 不做跨文件切段（文件级队列已解决负载均衡；理论自由，不实现）。
- 不做 Rust serve / raw 改造（现状保持）。
- 不改编码探测算法（复用 `encoding_detect.py`）。

### 1.4 基底决策（2026-08-07 评估结论）

**结论：从 `main` 分出 `pure-python-beta`，外科手术切除 `core.py` 内 3 处 Rust 委派，原样保留纯 Python 有益资产。**

评估依据（事实调研）：
- **Rust 污染面**（`main:txt_to_epub_core.py` 内部 3 处，散在核心函数）：
  - `_parse_chunk` 委派 Rust（576 行）
  - `parse_txt_index` 默认走 `_index_with_rust_serve`（787 行）
  - `pack_chapters` 调 `parse_txt_rust.exe --pack`（978 行）
- **纯 Python 有益资产**（独立文件，零 Rust import，原样保留即可）：
  - `hierarchy_rules.py` —— 统计匹配数实现分层目录（用户点名要保留）
  - `encoding_detect.py` —— 阶梯采样 + 择优（理想模型"编码分链"正好复用）
  - 多卷拆分、`pack_chapters` 的 manifest 同序切片修复（430e4a8，纯 Python）
- **关键事实**：v1.0.0 的 `_parse_chunk`（367 行）已是**纯 Python 实现**（逐行 `_compile_pattern` 匹配，零 Rust 调用）——即 Rust 委派的可直接替换源。

两种"纯净开始"方案的代价对比：

| | 方案 A（采用）：main + 切 Rust | 方案 B：v1.0.0 + 移植资产 |
|---|---|---|
| 无 Rust 执行 | ✅ 换回 3 个纯 Py 函数 | ✅ 天然无 Rust |
| 保留分层/编码探索 | ✅ 白捡两个独立文件 | ❌ 需手动复制 |
| 保留打包修复/多卷 | ✅ 原样在 | ❌ 散在 core.py 需重做（易漏） |
| 新增风险 | 极小（3 函数替换，v1.0.0 现成版） | 中（重做易错） |

**采用方案 A 的理由**：Rust 路线要废弃的是那 3 个委派调用，不是 main 上的所有改动；把它们剥掉后，main 即"纯净且带着有益探索的 py 起点"，代价最小，契合目标函数。

> 注：当前 `pure-python-beta` 分支仍停在 v1.0.0（36d6f8b），需在开工 M1 前先 `git reset --hard main` 再执行 3 处替换；替换后 `core.py` 不再 import / 调用 `parse_txt_rust.exe`。

---

## 2. 设计原则（红线）

| # | 原则 | 说明 |
|---|------|------|
| P1 | 单向数据流 | `文件 → 预扫 → 分链 → 任务队列 → worker → 聚合 → 打包`，不回传 |
| P2 | 无环依赖 | 各阶段 DAG，调度器不持有解码/切章状态 |
| P3 | 确定性 | worker 逻辑与 `_parse_chunk` 完全一致，同输入同输出 |
| P4 | 局部性 | worker 只依赖任务单参数，无共享状态 |
| P5 | **传引用不传数据** | 任务单 = 标量；正文 worker 自读 |
| P6 | parity 红线 | 解码永远在 Python 侧（worker 内） |
| P7 | 进程池常驻 | 启动一次全批量复用，171ms 启动税摊销到零 |
| P8 | **代价最小** | 新写代码尽量少、复用尽量多；不为锦上添花的功能加复杂度 |

---

## 3. 现状盘点（调研结论 2026-08-07）

### 3.1 可复用组件

| 组件 | 位置 | 接口 | 复用方式 |
|------|------|------|---------|
| 编码择优 | `encoding_detect.py:97` | `detect_encoding(path, max_stage)` | 直接 import |
| 行计数 | `txt_to_epub_core.py:539` | `_count_lines(file_path, encoding)` | 直接 import |
| **切段 worker** | `txt_to_epub_core.py`（替换后 = v1.0.0:367 纯 Python 版） | `_parse_chunk(path, start, end, enc, pat, idx, total) -> temp_path` | **直接提交进程池**（已是传引用单元，零 Rust） |
| 动态分段查表 | `txt_to_epub_gui_2.py:138` | `_get_optimal_chunks(file_size)` | import 使用 |
| 分块聚合+打包 | `txt_to_epub_gui_2.py:1869` | `_finish_file(...)` | 移植逻辑到新包（不改原函数） |
| 单文件全流程 | `txt_to_epub_core.py:501` | `convert_single(...)` | 兜底路径 |
| EPUB 打包 | `txt_to_epub_core.py:293/951/1000` | `build_epub`/`pack_chapters`/`build_epub_from_pack` | 直接 import |
| 目录分层 | `hierarchy_rules.py` | `build_hierarchy(index, rules)` | 直接 import |

### 3.2 现有缺陷（本计划要修）

| # | 缺陷 | 位置 | 修法 |
|---|------|------|------|
| D1 | `max_workers=min(6, cpu)` 写死 6 | gui:1853 | 调度器默认 `cpu_count()` |
| D2 | 小文件串行、大文件并行 | gui:1833-1851 | 全部进全局队列 |
| D3 | 池 `with` 用完即毁 | gui:1854 | 常驻池 |
| D4 | 无编码分链 | core:783 单一 enc | 预扫分组，4 链并进一池 |
| D5 | 无动态再切分 | gui:138 静态查表 | 运行时切尾部剩余区间 |
| D6 | 聚合埋在 GUI | gui:1869 | 移植到新包 `finish.py` |

---

## 4. 目标架构

```
                    ┌──────────────────────────────────────────┐
                    │  主进程（调度层，global_scheduler 包）    │
  文件列表 ──▶ 5.1 预扫 ──▶ 5.2 分链 ──▶ 5.3 任务生成 ──▶ 5.4 常驻池
    │           编码/行数           4 链          全局队列        │
    └──────────────┬───────────────────────────────────────────┘
             5.6 聚合+打包 ──▶ EPUB     5.4 动态再切分（D5）
```

---

## 5. 决策定案（原计划书 6 个决策点，全部拍板）

| 决策点 | 定案 | 代价考量 |
|--------|------|---------|
| **D-A 任务排序** | 全局按文件大小**降序**生成任务（大先入队） | 大任务早接单，避免长尾；代价≈一次排序 |
| **D-B 集成范围** | **独立 CLI 包**，GUI 完全不动 | GUI 81KB 不动=零回归风险；CLI 可独立测试 |
| **D-C 磁盘限流** | 不加限流；留 `GS_IO_CAP` 环境变量开关 | SSD 场景不需要；开关代价 5 行 |
| **D-D 断点续跑** | **轻量版**：预扫清单落盘 JSON + 输出存在即跳过；不做完整 checkpoint | 完整 checkpoint 复杂度高收益低；轻量版约 20 行 |
| **D-E worker 数** | 默认 `cpu_count()`；`GS_MAX_WORKERS` 可覆盖 | 一行代码，可调 |
| **D-F 预扫并行** | **复用同一常驻池并行预扫**（行计数入池） | 池反正要起，预扫并行代价≈0；万级文件行计数不再串行 |

---

## 6. 新包结构（global_scheduler/）

```
github-txt-epub_2026-07-25/
├── global_scheduler/          # 新包（唯一新增代码位置）
│   ├── __init__.py
│   ├── preflight.py           # 预扫器（~90 行）
│   ├── chains.py              # 分链器（~40 行）
│   ├── tasks.py               # 任务模型 + 任务生成（~70 行）
│   ├── scheduler.py           # 调度主循环 + 动态再切分（~170 行）
│   ├── finish.py              # 聚合 + 打包移植（~60 行）
│   └── cli.py                 # CLI 入口（~70 行）
├── plan/global_scheduler_plan.md
└── (原文件全部不动)
```

---

## 7. 数据结构（dataclass，全部 frozen/标量）

```python
# tasks.py
@dataclass(frozen=True)
class FileMeta:          # 预扫产物
    file_id: int
    path: str
    size: int
    encoding: str
    line_count: int
    title: str
    author: str

@dataclass(frozen=True)
class Chain:             # 分链产物
    chain_id: int
    encoding: str
    files: tuple[FileMeta, ...]     # 按 size 降序

@dataclass(frozen=True)
class TaskDescriptor:    # 任务单（纯标量 → pickle 开销≈0）
    file_id: int
    file_path: str
    start_line: int
    end_line: int
    encoding: str
    pattern_str: str | None
    chunk_index: int
    total_chunks: int
```

- `_parse_chunk(*[t.file_id 排除])` 直接接受前 7 个字段 → 提交池时解包即可。
- `task_to_args(t) -> tuple` 兼容 `_parse_chunk` 签名（去掉 file_id 之外的字段即可，实际 `_parse_chunk` 参数顺序一致）。

---

## 8. 模块设计（函数签名级）

### 8.1 preflight.py

```python
def scan_files(paths: list[str], pool: ProcessPoolExecutor) -> list[FileMeta]:
    """并行预扫：detect_encoding + _count_lines 丢进 pool；返回 FileMeta 列表（含 file_id 编号）"""
def load_manifest(path) -> list[FileMeta]: ...   # D-D：清单落盘/载入
def save_manifest(metas, path) -> None: ...
```

- 异常文件（探测失败）：记 `encoding=None` 进兜底名单，不中断。

### 8.2 chains.py

```python
def build_chains(metas: list[FileMeta]) -> list[Chain]:
    """按 encoding 分组（utf-8/gb18030/big5/other），组内 size 降序；返回 0~4 条链"""
```

### 8.3 tasks.py

```python
SPLIT_THRESHOLD = 200_000   # 行数超过则初始静态切段

def gen_initial_tasks(chains: list[Chain]) -> list[TaskDescriptor]:
    """全部链的任务混入一个列表：文件 < 阈值 → 1 个任务；
       文件 ≥ 阈值 → 按 _get_optimal_chunks(file_size) 切成多段任务。
       任务整体按 start_line=0 的文件的 size 降序排列（大文件先入池）。"""
def resplit_tail(meta: FileMeta, done_chunks: set[int]) -> TaskDescriptor | None:
    """动态再切分：返回该文件未完成行区间的后半段新任务（见 §9.2）"""
```

### 8.4 scheduler.py（核心）

```python
def run_batch(paths: list[str], out_dir: str, workers: int | None = None) -> BatchResult:
    workers = workers or int(os.environ.get("GS_MAX_WORKERS") or os.cpu_count() or 4)
    with ProcessPoolExecutor(max_workers=workers) as pool:      # 常驻（P7）
        metas = preflight.scan_files(paths, pool)               # 预扫入池（D-F）
        chains = chains.build_chains(metas)
        tasks = tasks.gen_initial_tasks(chains)
        futures = {pool.submit(_parse_chunk, *task_args(t)): t for t in tasks}
        # 主循环 + 动态再切分（§9）
    return BatchResult(ok=..., failed=..., skipped=...)
```

- **进度**：完成文件数 / 总数，回调接口（GUI 未来可挂，现在 CLI 打印）。
- **跳过**：`out_dir/<书名>.epub` 已存在 → 跳过（D-D 轻量续跑）。

### 8.5 finish.py

```python
def finish_file(meta: FileMeta, chunk_paths: dict[int, str], out_dir: str) -> ConversionResult:
    """移植 gui:_finish_file 逻辑：按 chunk_index 排序读 pickle → overflow 归位 →
       复用 core 打包链（build_epub_from_pack 等）。纯函数，可单测。"""
```

### 8.6 cli.py

```
python -m global_scheduler.cli <输入目录> --out <输出目录> [--workers N] [--manifest path]
```

- 输入目录：`*.txt` 递归收集；`--manifest` 载入已有预扫清单（续跑用）。
- 输出：每文件一个 EPUB + `summary.json`（成功/失败/跳过清单）。

---

## 9. 调度主循环（伪代码）

### 9.1 主循环

```
futures = {submit(任务): 任务 for 任务 in initial_tasks}
pending: dict[file_id, set[chunk_index]]      # 每文件未收块号
results: dict[file_id, dict[chunk_index, temp_path]]

while futures:
    done, _ = wait(futures, timeout=0.5)
    if done:
        for f in done:
            t = futures.pop(f)
            try: temp = f.result()
            except: 重试 1 次 → 仍败：file 进 failed，降级 convert_single 兜底
            results[t.file_id][t.chunk_index] = temp
            pending[t.file_id].discard(t.chunk_index)
            if not pending[t.file_id]:
                finish_file(metas[t.file_id], results[t.file_id], out_dir)   # 文件全块完成即打包
    if not futures:                            # 队列空
        for chain in chains:                   # 找最大未完成文件
            t = tasks.resplit_tail(biggest_unfinished(chain), done_chunks)
            if t:
                futures[submit(t)] = t
                break                          # 每次只补一个，避免风暴
```

### 9.2 动态再切分 resplit_tail（对应用户"跑完短的切开长的塞空闲"）

- **触发**：`futures` 空（所有已分发任务完成）且该链仍有未完成大文件。
- **操作**：取"行数最大且未完成"的文件，其未完成行区间 `[cur_start, end]`（`cur_start` = 已收最大块号对应的切分起点），将 `[cur_start, end]` 从中间劈开，**后半段**作为新任务提交（`chunk_index = 新编号`，`total_chunks` 更新）。
- **正确性**：切口修正逻辑与 `_parse_chunk` 的 overflow 归位完全一致——新段仍按"找第一个匹配标题行"处理，前半段 overflow 并入。P3 确定性不破坏。
- **防风暴**：每次循环只补 1 个任务；`futures` 非空即停。

### 9.3 正确性关键

- 同一文件的所有块最终按 `chunk_index` 排序归位 → 等价整文件扫描（README_full 6.3 证明）。
- 动态再切分的段与静态段**共享同一归位协议**（overflow 拼接），无特殊分支。

---

## 10. 异常与兜底

| 场景 | 处理 |
|------|------|
| worker 异常 | 重试 1 次 → 仍败：`convert_single` 串行兜底（复用，不重写） |
| 编码探测失败 | utf-8 → gb18030 依次试 → 记 failed |
| 空文件 / 0 章 | 跳过并记录 |
| 输出已存在 | 跳过（D-D 续跑语义） |
| 池启动失败 | 全量降级串行循环 |

---

## 11. 内存与资源

| 位置 | 占用 | 说明 |
|------|------|------|
| 主进程 | 任务单 + 结果索引（章节元数据），不存正文 | O(任务数) 标量 |
| worker | 单段 ≤ 35MB | 现有约束不变 |
| 池 | N × 30-50MB（N=核数） | 16 核 ≈ 800MB |

---

## 12. parity 验证方案（复用 benchmark/probe）

1. **切分数一致性**：同文件 1/2/4/8 块，章节列表逐项一致（titles_check.py）。
2. **与 convert_single 一致性**：本调度器输出 vs `convert_single` 输出，章节标题/顺序逐字节一致。
3. **混合编码**：utf-8/gb18030/big5 各若干本批量，与逐本转换一致。
4. **动态再切分**：大文件（≥50 万行）+ 小文件混合，强制触发 resplit，验证 overflow 归位。
5. **异常样本**：非法字节 / 无标题 / 超长行 / 空文件。

---

## 13. 性能目标（16 核机器，海量中小文件）

| 指标 | 目标 | 依据 |
|------|------|------|
| 并行度 | 6 → 16 worker | 修 D1 |
| 小文件 | 串行 → 全进池 | 修 D2 |
| 启动税 | 每批一次 → 摊销到零 | P7 |
| 编码并行 | 单编码串批 → 4 链同池 | 修 D4 |
| 长尾 | 静态 → 动态再切分 | 修 D5 |
| 综合吞吐 | ≥ 现有 gui 批量的 2.5× | 16/6 worker + 小文件并行 |

---

## 14. 里程碑（每阶段独立可交付、可回退）

| 阶段 | 内容 | 新增代码 | 验收 |
|------|------|---------|------|
| M1 | 包骨架 + 数据结构 + 预扫/分链/任务生成 + 单测 | ~200 行 | 混合目录预扫分链正确；任务单序列化正确 |
| M2 | 常驻池调度主循环 + finish 移植 + CLI | ~300 行 | 10 本混合编码 parity 通过；与 convert_single 一致 |
| M3 | 动态再切分 + 续跑 + 异常兜底 | ~120 行 | 400MB 大文件+100 小文件混合无 idle；resplit 后 parity 一致 |
| M4 | 性能对标 + 文档 | ~50 行 | 16 核吞吐 ≥ 2.5×；README 更新 |

**总量**：新包约 500 行，复用约 8 个现有组件，原文件零改动。

---

## 15. 风险与对策

| 风险 | 对策 |
|------|------|
| Windows spawn 池创建慢 | 常驻只付一次；`if __name__ == "__main__"` 守卫 |
| 动态再切分边界正确性 | 与静态段共享 overflow 协议；验证矩阵覆盖 |
| 磁盘 IO 竞争 | SSD 无碍；`GS_IO_CAP` 开关备用 |
| 移植 _finish_file 出错 | 移植后先单测（finish_file 为纯函数）再联调 |
| 360 误报 Rust exe | 不强依赖 Rust；现状结论已定（信任区） |

---

## 16. 工程纪律

- 新代码全在 `global_scheduler/` 新包，**原文件零改动**（实验纪律）。
- parity 红线 P6 不破。
- 文档朴素直述（用户约定）。
- 测试沿用 `benchmark/probe/`。

---

## 17. 参考

- `README_full.md` §6.2-6.4（并行拓扑论证/风险防护）
- `benchmark/README.md`（测试管线）
- 工作区日志 2026-08-07（理想模型收敛 / 传引用 / 进程池常驻 / 瓶颈三税 / Rust 定位）
