# TXT → EPUB 转换工具（Rust 解析内核 · 大规模批处理路线）

> 文档最后更新：2026-08-03（管道 + 批处理落地 · release 部署提速 4.5~5.8× · 阶段 C 评估判定不做）

一个把纯文本小说（TXT）转换成 EPUB 电子书的桌面小工具：自动按章节切分、批量处理、编码实时预览、章节拖拽排序、封面裁剪。内核历经「纯 Python → Rust 加速 → raw_offsets 实验（已否决）→ 管道/批处理（前进方向）」四段演进，踩坑与方向见下文。


---

## 一、项目状态一瞥（30 秒速览）

| 维度 | 状态 |
|------|------|
| **当前可用版本** | v3（本仓库根目录），GUI 默认走 **管道路径**：Python 解码 → 内存 UTF-8 → stdin → Rust 切章（零 temp，已落地，与 Legacy 写-temp 架构逐位一致）；Legacy 写 temp 作 fallback |
| **已否决的方案** | `raw_offsets`（Rust 直接解 GBK 原始字节）——因 `encoding_rs` 与 Python `gbk` codec 对非法字节不一致而**判死刑**（详见「踩坑实录」坑 2c） |
| **当前瓶颈** | ~~每文件一次 `subprocess` 启动 + temp 落盘~~ **已消除**（管道零 temp + 批处理单进程常驻均已落地，见 6.1 / 8.1 / 8.2）；现瓶颈为 Rust 解析本身（CPU 密集）与 Python 解码（约 22% 全链路，解码红线所致） |
| **下一步方向** | ① 管道架构 ✅ ② 单进程批处理 ✅（serve + rayon，31 本 5.83s）→ ③ 阶段 C（PyO3/C 内核化）**已评估判定不做**（收益仅 ~4%，见 8.3）；余留事项：真机 GUI 点测、更大语料（18+ 本已验 31 本）复验 |
| **性能基准（release 构建，2026-08-03 实测）** | 31 本 / 7595 章 / 181MB UTF-8：serve 批量 **5.83s**、逐文件 pipe **12.15s**；⚠️ 早期 README 数据为 debug 构建（36MB exe），慢约 4.5~5.8×，勿再引用 |
| **回退保障** | 历史版本全部归档于 `versions/`，可随时回退（见下节） |

**一句话因果主线**：`v1 纯 py` → `v2 Rust 加速（成功）` → `raw_offsets 实验（失败：解码权不能交给 Rust）` → **教训固化 + 管道/批处理方向**。README 后半部分（踩坑实录 + 前进方向）就是这条主线的完整展开。

---

## 二、版本演进与回退

本仓库保留三个递进版本，全部未删除，可随时回退：

| 版本 | 位置 | 说明 | 关键特征 |
|------|------|------|----------|
| v1 纯 py 初始版 | `versions/1_pure_python/` | 7-25 最早版本 | 纯 Python 解码+分章+打包，不依赖 Rust、不写临时文件 |
| v2 Rust 加速版 | `versions/2_rust_accel/` | 引入 Rust 后的可用快照（改 raw_offsets 之前） | Python 调 `parse_txt_rust.exe` 解析，核心逻辑已验证 |
| v3 当前最终版 | 仓库根目录（本目录） | 最新成品 | 管道路径（Python 解码 → stdin → Rust 切章，零 temp）默认 + Legacy 写 temp 作 fallback；含 `--serve` 批处理引擎（阶段 B）；`parse_txt_rust.exe` 为 **release 构建**（5.2MB，2026-08-03 重新部署） |

演进关系：`v1 纯 py` → `v2 引入 Rust 加速` → `v3 加 raw_offsets 分支`。

- 想用最朴素无依赖的版本 → 用 `versions/1_pure_python/`
- 想退回「Rust 加速但没动 raw_offsets」的稳定版 → 用 `versions/2_rust_accel/`
- 日常使用 / 最新功能 → 用仓库根目录（v3）

---

## 三、主要功能

- **TXT → EPUB 转换**：基于正则规则自动识别章节标题并切分正文。
- **批量处理**：可一次选择多本 TXT 连续转换。
- **编码实时预览**：选择文件后即时预览前 800 字，支持 utf-8 / gbk / 其他常见编码，乱码可先预览再转换。
- **章节预览与排序**：列出识别出的章节，支持鼠标拖拽或按钮调整顺序。
- **封面支持**：可选图片作封面，带裁剪预览（基于 Pillow）。
- **实时进度条**：转换过程实时显示进度。
- **拖拽加载**：支持把 TXT 文件直接拖进窗口（基于 tkinterdnd2）。
- **Rust 内核加速**：章节匹配内核由 `parse_txt_rust.exe` 承担，实测比纯 Python 快数倍；GUI 为「异步轻量索引（先只建索引）+ 后按需 pack」，大文件不卡 UI、内存恒定。
- **并行强化（保留路径）**：大文件自动按体积切分，用多进程并行解析提速；CPU 核心不足 2 个时自动回退单进程串行。

---

## 四、快速开始

### 1. 安装依赖

```bash
pip install EbookLib Pillow tkinterdnd2
```

- **Python 3**（已在 CPython 3.12 下运行；需桌面图形环境，依赖 Tk）。
- GUI 启动时会自动检查缺失依赖，缺少则尝试自动 `pip install`，失败时按提示手动安装即可。

### 2. 运行 GUI

```bash
python txt_to_epub_gui_2.py
```

或用 `run_gui.bat` 一键启动（内部 `cd /d "%~dp0"` 锁定工作目录到脚本所在目录，避免因 cwd 不同导致 `exportTxtTocRule..json` / `parse_txt_rust.exe` 找不到）。

> 需要本地有图形桌面环境（Tk / TkinterDnD）。纯服务器无显示环境无法启动界面。

### 3. 使用步骤

1. **选择 TXT**：点「选择 TXT 文件」或直接把文件拖入窗口；批量可点「批量处理」多选。
2. **确认编码**：看「编码实时预览」前 800 字是否正常显示。若乱码，切换编码下拉（如改为 `gbk`）再确认。
3. **检查章节**：在「章节预览」里核对章节切分是否正确，可拖拽调整顺序。
4. **（可选）加封面**：选择一张图片并裁剪。
5. **开始转换**：点击转换，进度条走完即在 TXT 同目录（或指定输出目录）生成 `.epub`。

### 4. 章节识别规则

章节切分由 `exportTxtTocRule..json` 中的正则规则驱动，内置多组规则并按优先级排序，例如：

- `目录` / `目录(去空白)`：目录页中的条目
- `通用规则`：通用章节前缀
- `晋江` / `晋江2` / `晋江常用`：晋江网文常见标题格式（如 `◎xxx`、顶格短标题）
- `#28 数字转中文`：数字章节转中文等场景

规则加载后在 `parse_txt` 中按优先级依次匹配，命中即作为一个新章节的起点。如需自定义，可参照 JSON 结构增删规则（每条含 `name` 与 `rule` 等字段）。

> ⚠️ **文件名是两个点**（`exportTxtTocRule..json`），必须与脚本放在同一目录（`gui_2.py` 按 `Path(__file__).parent` 相对查找，非写死路径）。

---

## 五、文件结构

| 文件 | 说明 |
|------|------|
| `txt_to_epub_gui_2.py` | 图形界面主程序（入口）：异步轻量索引 + 按需 pack + Rust 内核开关；并行解析调度、拖拽、预览、进度条 |
| `txt_to_epub_core.py` | 转换内核：`parse_txt`（解析切章）、`build_epub`（生成 EPUB）、`convert_single`（单本转换）；v3 新增 `parse_txt_index`（轻量索引）、`read_chapter` / `pack_chapters`（按偏移按需取正文）、`_index_with_rust_pipe`（管道索引，零 temp）；阶段 B 新增 `_index_with_rust_serve` + `batch_parse_index`（单常驻进程批量索引） |
| `parse_txt_rust.exe` | Rust 解析内核（编译产物）；GUI 通过子进程调用，正则编译为 `CompiledEngine` 复用 |
| `rust/` | Rust 内核源码（`src/`：`lib.rs` 切章逻辑、`main.rs` CLI、`translator.rs` 后顾翻译层、`probe.rs`；`tests/` 单元/对照测试；`Cargo.toml`/`Cargo.lock`） |
| `exportTxtTocRule..json` | 章节标题识别规则集（正则），**注意文件名是两个点，需与脚本放在同一目录** |
| `versions/` | 历史版本归档：`1_pure_python/`（纯 py 初始版，原 `source/`）、`2_rust_accel/`（Rust 加速版，原 `backup_pre_raw/`，含 exe + `rust_src/` + 两个 py，回滚安全副本） |
| `test_data/` | 测试样本（如《从零开始》精校版 txt） |
| `run_gui.bat` | 一键启动 GUI 的便捷脚本（`cd /d "%~dp0"` 锁定工作目录） |

> 运行时不依赖其他数据文件；`validate_corpus.py` + `validate_report.json` 为 2026-08-03 全量校验工具与报告（parity 校验，可复用）。

---

## 六、当前架构（背景）

本工具目前是**双轨并行**：默认走 Rust 轻量索引路径（v3 核心），同时保留 7-25 的 Python 分块并行路径（大文件/兼容场景）。

### 6.1 默认路径：管道索引（Python 解码 → stdin → Rust 切章，零 temp）+ 按需 pack（v3 核心）

```
选择 TXT
  → Python 按原编码流式解码 → 内存持有 UTF-8 字节（不落盘 temp）
  → 经 stdin 喂给 parse_txt_rust.exe：读 UTF-8 文本，正则切章，输出轻量索引 JSON（title/start/end 字节偏移）
  → GUI 只持有索引 + 内存 UTF-8 缓冲（不载入全本正文对象），预览/双击时 read_chapter 从内存按偏移切片
  → 转换时 pack_chapters 按偏移切片物化 xhtml → 组装 EPUB（仅打包阶段落一次盘，内存恒定）
```

- Rust 侧 `--mode parse` 从 `stdin` 读 UTF-8（`rust/src/main.rs` 98-117 行早已支持）；Python 侧 `_index_with_rust_pipe` + `Utf8Buffer`（内存 UTF-8 虚拟 source）实现零 temp 索引（见 8.1 实现状态）。
- **Legacy fallback**：当 Rust 进程级失败，`parse_txt_index` 自动回退「写 UTF-8 temp 文件 → Rust 读 temp」旧路径，对外行为不变。
- Rust 内核：`CompiledEngine` 封装已编译正则（`Option<Regex>`），满足 `Sync + Send` 可跨线程/跨进程共享。
- 调度是 **Rust 优先**：规则先交给 Rust 翻译层匹配；仅 Rust 进程级失败时回退 Python `re` 保底（`txt_to_epub_core.py` 顶部注释 + `parse_txt_index` 实现）。
- 大文件不卡 UI：先只建索引，正文按需读。

### 6.2 保留路径：Python 分块并行（7-25 设计，仍可用）

- 转换内核用 `concurrent.futures.ProcessPoolExecutor` 做多进程解析（`gui_2.py`）。
- 并行逻辑严格限制在内核之外，解析内核完全不动（三层隔离模型）：

  ```
  ┌─────────────────────────────┐
  │  Layer 3  编排层             │  文件遍历、段数决策、任务生成、结果收集
  │          不碰正则/编码/HTML  │
  ├─────────────────────────────┤
  │  Layer 2  切口修正层         │  子进程内前置过滤：找首个标题、切 overflow
  │          复用 parse_txt     │  （零修改内核）
  ├─────────────────────────────┤
  │  Layer 1  解析内核          │  parse_txt / build_epub 完全不变
  └─────────────────────────────┘
  ```

  > 新增代码只存在于 Layer 2 / Layer 3，内核完全隔离，不向内核传递任何新状态。
- 单文件按大小**自适应分块**（分段表）：

  | 文件大小 | 分片数 |
  |----------|--------|
  | < 5 MB | 1（不分片） |
  | 5 ~ 15 MB | 2 |
  | 15 ~ 50 MB | 4 |
  | 50 ~ 200 MB | 4（保守） |
  | > 200 MB | 6 |

- 环境检测 `os.cpu_count() > 1` 才启用并行；不满足则自动回退单进程串行。
- 子进程之间**无横向通信**，各自只写出本段结果（`_parse_chunk`）；合并由主进程按段序拼接（`_merge_chunks`，`gui_2.py`）。
- 批量时所有文件所有分块混进全局任务队列，`as_completed()` 动态收集，某文件分块全完成立即合并 → `build_epub`，不等其他文件。
- 进度条按「已完成文件数 / 总文件数」更新，小文件先跑完，大文件不阻塞调度。

### 6.3 为什么可以并行而不破坏结果（线性拓扑论证）

这套并行的合法性建立在**内核是无副作用纯函数**之上：

1. **单向数据流**：`文件 → 行计数 → 切分器 → 任务队列 → 子进程 → 临时文件 → 合并器 → EPUB 构建器`，没有任何环节把数据回传给前一步。
2. **无环依赖**：各阶段构成 DAG，拓扑排序无回边。
3. **确定性**：行计数固定、切分逻辑确定、正则匹配是纯函数、切口修正找「第一个匹配标题行」无歧义、合并是线性拼接；相同输入必得相同输出，无竞态。
4. **局部性**：每个组件只依赖输入参数，无共享内存、无全局可变状态。

**切口修正的安全性证明（摘要）**：对第 i 段（i>0），子进程找到第一个匹配标题行 L；L 之前的 `overflow` 行已被正则检验为非标题，全部拼回前一段末尾即恢复完整正文；逐段拼接后等价于对整文件的一次性扫描。∎

### 6.4 风险防护表（并行路径）

| 潜在风险 | 防护 |
|----------|------|
| UTF-8 多字节字符被切断 | 严格按行边界（`\n`）切分 |
| 切口落在标题行中间 | 按行切分，标题行完整 |
| overflow 误含标题 | overflow 定义为「首个标题之前」，已通过正则检验 |
| 某段无标题 | overflow = 整段内容，全部拼回前段，内容不丢 |
| 子进程返回顺序错乱 | `map()` 保序；批量模式用 `chunk_index` 显式排序 |
| 临时文件残留 | 合并后立即删除 / `tempfile` 自动清理 |
| 子进程异常 | `try/except` 捕获，自动回退单线程路径 |
| 内存暴涨 | 子进程独立内存，合并即释放；单段 ≤ 35 MB |

**回退保证**：单线程路径完整保留——`os.cpu_count() <= 1` 自动走原串行路径；并行子进程异常自动降级为单线程继续，不会丢数据或卡死。

---

## 七、⭐ 踩坑实录（本项目最宝贵的部分）

> 这一节把 Rust 重写切章内核踩过的所有坑固化下来，避免重蹈覆辙。**结论先行：让 Rust 直接解码 GBK/GB18030 原始字节是死胡同，正确路线是「管道」——Python 解码、stdin 喂 UTF-8 给 Rust 切章。**

### 7.0 失败决策树（因果链总览）

```
v1 纯 Python 解析 ──→ v2 Rust 加速（成功：Rust 切章快数倍）
                              │
                              ▼
                  v3 raw_offsets 实验（Rust 直接解原始字节，想消除 temp 文件）
                              │
            ┌─────────────────┼──────────────────────┐
            ▼                 ▼                      ▼
        坑0 decode_to_str   坑2a 每行 encode      坑2c encoding_rs 与 Python gbk
        API 用错→解码全空     →慢 2×（已修）        对非法字节不一致→章数错乱
        （已修）            坑2b BOM 偏移少3         ──► 根因级，不可小修消除
                              （已修）                    │
                                                         ▼
                                              raw_offsets 判死刑（保留 experimental、GUI 默认关闭）
                                                         │
                                     ┌───────────────────┴────────────────────┐
                                     ▼                                        ▼
                          教训 1：解码权不能交给 Rust                   教训 2：845GB/25万文件
                          （encoding_rs ≠ Python gbk）                 「写临时文件」架构必崩
                                     │                                        │
                                     └──────────────────┬─────────────────────┘
                                                        ▼
                                    🧭 前进方向：管道（stdin）→ 单进程批处理引擎
                                    （详见「八、前进方向」）
```

### 7.1 坑 0：`encoding_rs` 流式解码 API 用错（最致命）

- 误用 `decoder.decode_to_str(input, &mut out, last)`。`decode_to_str` 接收 `&mut str`，**长度固定**；若 `out` 每轮 `clear()` 成长度 0，则往 0 长度缓冲写 → 读 0 字节、写 0 字节 → 解码永远空串 → 章节只产出 1 个 fallback（标题空 / 「前言」，偏移全 0）。
- 正确：`decoder.decode_to_string(input, &mut out, last)` 接收 `&mut String`，用 String 的**容量**（预开容量）当输出上限并增长 `len`。
- 单测 `raw_gbk_basic` 用极小输入蒙混通过，真实大文件立刻暴露。**必须在大文件上验证解码非空。**

### 7.2 坑 1：GBK 没有 BOM，BOM 处理是误诊

- 曾误判「解码为空是 BOM 处理问题」，把 `new_decoder()` 换成 `new_decoder_without_bom_handling()`。但对 GBK（无 BOM）二者**等价**，换成空操作，解码仍空。
- 真正的空解码根因是坑 0 的 `decode_to_str` 用错 API，不是 BOM。教训：先在小样本上打印 `consumed` / `decoded` 确认病根，别凭直觉改 API。

### 7.3 坑 2：raw_offsets 三连（原始字节偏移方案的根本缺陷）

尝试让 Rust 直接读原始字节、流式解码并记录**原始文件字节偏移**，以消除 UTF-8 temp 文件：

- **2a 性能：每行 `enc.encode()` 测原始字节长度 → 慢 2×**
  - 最初每行都调一次 `encoding_rs::encode(&line)` 求原始字节长度，几十万次分配 → raw 比 Legacy 还慢（gb18030 书 1.94s vs 0.87s）。
  - 修复：零分配 `raw_byte_len` 辅助——UTF-8 用 `line.len()`；GBK/GB18030 数 char（ASCII 1 字节、BMP 2 字节、astral 4 字节，退化情形才精确 `encode`）。
- **2b BOM 偏移少 3 字节**
  - `pending_raw_start` 初值误设为 0 而非 `bom_skip`，导致所有原始偏移比真实位置少 3 字节，BOM 被拽进第一章正文、整章错位。修复：`pending_raw_start = bom_skip`。
- **2c（根因级，无法小修消除）：`encoding_rs` 与 Python `gbk` codec 对非法字节不一致**
  - 同一文本同一正则，raw 切出的章数远少于 Legacy（冰封末世 raw=3 / leg=10；熊学派 14/63；阵问长生 8/17）。
  - 这是 raw 依赖 `encoding_rs` 的**固有偏差**，不是 bug 能消除 → 直接判定 raw_offsets 方案不可行，转向管道方案（见「前进方向」）。

### 7.4 坑 3：编码探测误判（测试陷阱，不是功能 bug）

- 测试用 `utf-8-else-gbk` 粗略探测器，对「非严格 UTF-8 但 GBK 能容错解码」的文件会误报成 GBK（GBK 解码几乎永不报错）→ 测试样本被喂错编码 → 输出乱码，误以为功能坏。
- 验证必须用**已知编码的受控样本**（自己写 GBK / UTF-8 / UTF-8-BOM 临时文件做 round-trip），不能信探测结果。

### 7.5 坑 4：`match_at_start` 忽略 `(?im)` 标志（既有行为）

- `re.find` 不 honor 多行/忽略大小写标志的 `^`/`$`。这是 Legacy 与 raw **共有**的 pre-existing 行为，不影响两者 parity，但解释部分标题匹配差异。若未来要精确对齐 `^`/`$` 语义，需改成预编译带标志的 `re.compile(pattern, re.I | re.M)`。

### 7.6 坑 5：encoding 透传 + GUI 清理护栏（保命）

- raw 模式下 `parse_txt_index` 返回**用户原书路径**作 `temp_path`（不是临时文件）。因此：
  - `read_chapter` 解码优先用章节埋点 `entry["encoding"]`，否则回退参数（兼容老 temp 路径）。
  - `pack_chapters` 的 `encoding` 为空时从 `index[0]["encoding"]` 自动取。
  - **GUI cleanup 护栏**：只删真 temp（前缀 `txt_epub_`），**绝不删用户原书**——否则解析下一本时 `os.remove(old)` 会删掉上一本用户原书，灾难级数据丢失。

### 7.7 坑 6：验证纪律——先小样本确定性，再上大规模

- 直接拿 18 本全量比对会跳过单本端到端、且被探测误判污染，误报「无回归」。
- 正确流程：受控样本（已知编码）→ 单本端到端（`read_chapter` 与 `pack` 产出逐字比对）→ 再小批量（18 本）逐章正文 parity。

### 7.8 坑 7：协作纪律——被用户三次打断的「死胡同」（进度反馈 / 先小样本 / 别兜圈子）

本次改造里有三处是我自己走进死胡同、被用户叫停的，**根因是工程纪律而非技术**，不写清楚下次必重演：

- **事件 A：长时间无进度反馈（用户打断「进度如何？怎么搞了这么久？」）**
  - 埋头调 raw_offsets 很久不回报进展 / 卡点，用户连续两次追问进度。
  - 教训：**长任务要主动分节点汇报**（卡在哪、下一步、预计还要多久），别等用户来问。

- **事件 B：没先小样本就 18 本全量（用户打断「你懂不懂测试的，先跑通一个小样本，再拿更大的样本测试啊」）**
  - 直接拿 18 本做全量 parity，跳过单本端到端、又被编码探测误判（坑 3）污染，一度误报「无回归」——其实 raw_offsets 已有真实回归（GBK 丢章、UTF-8 正文错位），**差点把带回归的版本当可用版发布**。
  - 教训：① 先小样本确定性、再上规模；② 被探测误判污染的全量结果是**假阴性**，不能信。这把坑 6 的纪律从「该做」升级为「被骂出来的硬要求」。

- **事件 C：GUI 沙箱验证兜圈子（用户打断「你在干什么？小收尾你闹出天大的动静？！你该不会是规则路径写死在代码里了吧？」）**
  - 收尾时用户说「行」让验证 v3 能启动，我在**无桌面的沙箱**里反复跑 `txt_to_epub_gui_2.py`：GUI 报错走 `messagebox.showerror` **弹窗、不进 stdout**，于是空输出 + exit 1；我于是层层下钻（py_compile → 两模块 import 全 OK → 建 Tk 窗 `WINDOW_OK` → 无缓冲跑 → grep 静默退出分支 → 读源码），绕了一大圈。
  - 其实 `py_compile` 通过 + 两模块 `import` 全 OK + 规则文件就位 + Tk 能建窗 这四点**已足够证明 v3 代码完整可跑**；GUI 在沙箱看不到窗口是显示环境问题、非代码 bug，早该在此停手给结论。
  - 教训（对应「别兜圈子」铁律）：验证类动作**前面的结论已够用时立即收口给结论**，不要为「查更彻底」反复重跑同类验证；沙箱无桌面时**不要用 GUI 启动来验证**，改用 `py_compile` / `import` / 单元断言。

### 7.9 当前状态与回滚

- 当前 Rust 内核**默认走管道路径**（Python 解码 → 内存 UTF-8 → stdin → Rust 切章，零 temp，已落地 2026-08-03），与 Legacy 写-temp 架构章数/标题/正文逐位一致；Legacy 写 temp 仅作 fallback。
- `raw_offsets` 分支保留为 experimental，GUI 默认关闭（`raw_offsets=False`）。
- 回滚安全副本：`versions/2_rust_accel/`（改 raw_offsets 之前的可用版本全套：exe + `rust_src/` + 两个 py）。

### 7.10 规模论证与决策来源（845 GB / 25 万文件）

**规模数据**：本工具终极服务目标是约 **845 GB / 253,142 个文件** 的 TXT 小说库。这个量级直接决定架构选型，而非文件少时「能跑就行」。

**为什么「写临时文件」架构在这个规模会崩（用户原话：绝对会崩）**
- 当前 Legacy 路径每本书落盘一份 UTF-8 temp（单本稳态约 3 MB，非一次性 57 MB）；但 25 万文件 = **25 万次 subprocess 启动 + 25 万次 temp 落盘/删除**，冗余 I/O 与进程启动开销是真正瓶颈，磁盘只会反复抖动。
- 关键洞察（来自外部 AI 评估、由用户带入）：**业内处理这种规模的成熟方案，不是「换语言」，而是换「流水线（Pipeline）」**——单纯把 Python 换成 Rust 只解决「解释器慢」，没解决「数据反复进出磁盘」这个架构级成本；25 万文件下，I/O 与进程调度才是墙。

**决策来源（管道方向是谁提的）**
- 管道架构方向（Python 解码 → stdin → Rust 切章，零 temp）**不是凭空设计，而是用户参考外部 AI 对该 845GB 规模的评估后，亲自提出的重构方案**，经我评估认同（低风险先做 stdin 1+2、常驻进程 3 延后）。这把「raw_offsets 死路」与「845GB 规模」串成同一根逻辑线：**Rust 碰解码不行（raw 死），全量落盘也不行（规模崩），正确解法是 Python 解码 + 管道直喂 Rust 切章。**

---

## 八、🧭 前进方向（可靠路线图）

> 三段递进，风险从低到高。**先做 A（低风险、立即消除 temp），再上 B（规模目标），C 视情况。** 每段都有明确验收标准（等价性验证契约），落地前先用小样本验证。

| 阶段 | 方案 | 解决什么 | 风险 | 状态 |
|------|------|----------|------|------|
| **A** | 管道架构（Python 解码 → stdin → Rust 切章） | 消除 temp 落盘 I/O，保持 Python 容错解码 | **低**（改动小，预期与 Legacy 100% parity） | **已实现**（步骤 1+2，2026-08-03） |
| **B** | 单进程 Rust 批处理引擎（rayon 全核 + 流式 JSONL） | 消灭 25 万次 subprocess 启动 + 吃满全核 | 中（需新增依赖与 `batch.rs` 模块） | 待实现 |
| **C** | Rust 常驻进程 / PyO3 内核化 | 进一步省每文件启动开销 / 免子进程 | 高（帧协议、并发隔离、FFI 安全） | 远期可选 |

### 8.1 阶段 A：管道架构（Python 解码 → stdin → Rust 切章，零 temp）

本仓库推荐**首先落地**的路线：让 Python 负责解码、Rust 只切章，彻底消除 temp 文件 I/O，并天然规避 `encoding_rs` 偏差（坑 2c 的教训：解码权不能交给 Rust）。

> **逻辑闭环**：`raw_offsets` 的失败（坑 2c）恰好**反向论证了管道架构的必要性**——既然「让 Rust 处理解码」这条路因 `encoding_rs` 与 Python `gbk` 分歧而走不通，正确路线就是让 **Python 负责解码**（保留与原始 Python 版一致的容错行为），再通过 **stdin 管道** 把 UTF-8 文本交给 Rust 切章；既保百分百等价，又消除 temp I/O。

**设计**
- Python 流式解码源文件（GBK/UTF-8）→ 通过 `subprocess.Popen(..., stdin=PIPE)` 把 **UTF-8 字节流** 直接喂给 Rust 子进程 → Rust 只做 `parse_chapters`（切章），输出轻量索引 JSON 到 `stdout`，不写临时文件。
- 同时保留 Python 的容错解码（与 Legacy 逐位等价）和 Rust 的高效匹配，并消除 UTF-8 temp 的磁盘 I/O。
- 双击章节时仍需物化该章正文（只物化一章，不物化全本）供 `seek`/打包读取，或 Python 直接保留该章 UTF-8 片段。

**接口约定（已实现）**
- Rust 侧：接受 `--pattern` 与 `--mode index`，从 `stdin` 读 UTF-8 文本，输出 `{title, start, end}` 索引 JSON 到 `stdout`；偏移直接对应管道中的 UTF-8 字节位置（无需落盘文件）。
- Python 侧：`Popen` 写 `stdin`、读 `stdout` 取索引；如需回读正文，按偏移在 Python 已持有的 UTF-8 字符串上切片，或仅对单章解码。

**实施分期（按风险）**
- 步骤 1+2（stdin 喂文本、不落盘）：**已实现**（2026-08-03），本地样本（含 3464 章 GBK 大书 + 构造 GBK/UTF-8 小样本）与 Legacy 写-temp 架构 100% parity（章数 / 标题 / 正文逐字一致）。
- 步骤 3（Rust 常驻进程，省去每文件启动开销）：较高风险，需设计帧协议与并发隔离；建议先完成 1+2 并用进程池 / 并行铺满 25 万文件规模，再视情况上常驻进程。

**实现状态（2026-08-03，阶段 A 步骤 1+2 已落地）**
- Rust 侧 `--mode parse` 已支持从 `stdin` 读 UTF-8（`rust/src/main.rs` 98-117 行）；Python 侧新增 `_index_with_rust_pipe`（管道索引）+ `Utf8Buffer`（内存 UTF-8 缓冲，替代解析阶段的 temp 文件）。
- `parse_txt_index` 默认走管道：源文件解码为内存 UTF-8 bytes → 经 `stdin` 喂 Rust 切章；`read_chapter` / `pack_chapters` 识别 `Utf8Buffer` 从内存按偏移切片，**仅打包阶段落一次盘**。
- 完整 fallback 保留：`_index_with_rust_pipe` / `parse_txt_index` 在 Rust 进程级失败时回退写 temp 走旧路径，对外行为不变。
- 验证：本地可用样本（含 40MB / 3464 章 GBK 大书《从零开始》+ 构造 GBK/UTF-8 小样本）全部 parity OK；`pack_chapters` 经 `Utf8Buffer` 正确物化章节 xhtml。**2026-08-03 已用 31 本新语料全量复验封闭**（详见 8.4 验证状态）。

### 8.2 阶段 B：单进程 Rust 批处理引擎（845 GB / 25 万文件规模目标）

> **✅ 实现状态（2026-08-03 已落地并验证）**：阶段 B 的核心目标「单进程常驻 + 消灭每文件进程启动 + 多核并行」已实现。具体：`main.rs` 新增 `--serve` 常驻模式（从 stdin 循环读 `[4字节长度][UTF-8 文本]` 帧，rayon 线程池并行 `parse_chapters`，按序号流式输出 JSONL，带背压不加载全量进内存）；`core.py` 新增 `_index_with_rust_serve`（单常驻进程处理多文件，进程只启一次）+ `batch_parse_index`（批量入口，失败回退逐文件管道）。**验证**：3 文件（含 40MB/3464 章 GBK 大书《从零开始》+ 构造样本）经常驻进程处理，章数/标题/正文与 v3 管道版 **100% parity**，进程计数恒为 1。**解码红线守住**：帧内文本是 Python 侧已解码的 UTF-8，Rust 绝不碰 GBK 字节（规避坑 2c）。`Cargo.toml` 已加 `rayon`。
>
> **⚠️ 部署坑（2026-08-03 实跑发现）**：工程根 `parse_txt_rust.exe` 曾误用 `rust/target/debug/` 的 **debug 构建**（36MB，未优化）部署——debug 版 serve 全量 31 本需 26.18s、pipe 需 70.85s（README 早期数据即 debug 版）。换 **release 构建**（`cargo build --release`，5.2MB）后同批语料 serve **5.83s**、pipe **12.15s**，均约 **4.5~5.8× 提速**，parity 仍 100%（7595 章 0 mismatch）。**教训：部署前核对 exe 大小（release ≈5MB，debug ≈36MB），且所有性能数据须以 release 版复测为准。**
>
> **阶段 C 必要性实证（2026-08-03 profile）**：对 31 本（181MB UTF-8）全链路拆解——Python 解码 1.24s（22%）、Rust 解析 CPU 45.5s（16 核并行压至墙钟 ~4.1s）、JSON 序列化仅 **22ms**、IPC+反序列化+调度 212ms（4.9%）。**结论：JSON 序列化开销近乎为零（serde_json 极快），PyO3「免序列化」收益是虚的；IPC 部分理论可省上限仅 ~234ms（全链路 4%）**。README 早期 8.3 中 mmap 零拷贝收益亦因「19/31 语料为 GBK、解码红线锁死 Python」而砍半以上、SIMD 与 fancy-regex 回溯语义冲突基本不可行。**阶段 C（PyO3/C 内核化）判定为性价比存疑，不做**；真实提速点已由「release 构建」免费拿到（见上 ⚠️）。
>
> 本节整合自外部 AI 对 845GB 规模的评估方案，**已按本项目约束修正其中一处致命缺陷**（见下方⚠️）。它比阶段 A 更进了一步：直接消灭 25 万次进程启动。

面向终极规模（845 GB / 253,142 个文件），当前「Python 遍历 → 逐文件 `subprocess` 启 Rust」的架构有两大硬伤，必须升级：

1. **25 万次进程启动开销**：每次 `subprocess` 约 20–50 ms，乘 25 万 ≈ 1.4–12.6 小时纯浪费（外部 AI 评估量级约 2–3 小时），且每进程只服务一个文件、正则要重编译。
2. **单文件 Rust 进程只用一核**：当前每本书起一个 Rust 进程、内部单线程逐行扫，即便 Rust 快也只吃一个核。

目标架构（参考外部 AI 的纯 Rust 批处理方案，修正后）：

```
磁盘 (845GB / 253k .txt)
   │  walkdir 遍历（单线程推路径入有界 channel）
   ▼
Rust 批处理引擎（单一常驻进程）
   ├─ I/O 线程池（4~8 线程，memmap2 零拷贝读 / 小文件 Vec<u8>）
   ├─ 解析线程池（rayon，全核）每任务调 parse_chapters
   ├─ translator::compile_re 编译【一次】，Regex 跨线程共享（已是 Sync）
   └─ 结果聚合器：流式追加写出 JSONL（每行一文件，内存恒定）
   ▼
输出 index.jsonl（path / encoding / 章节偏移），Python GUI 只读它做预览，双击按偏移 seek 取正文
```

**采纳外部 AI 计划的合理部分**
- 单进程跑到底，消除 25 万次 `subprocess` 启动（相对本仓库现有架构最大的提速来源，远超「换语言」本身）。
- `rayon` 解析池吃满所有核；`translator::compile_re` 已是 `Sync`（`translator.rs:269`），编译一次、全池复用——与上文 CompiledEngine 描述一致，**无需改动 translator.rs**。
- I/O 线程池 + `memmap2` 逼近存储带宽；流式写 JSONL、内存恒定（与本项目「轻量索引 + 偏移按需读正文」一脉相承）。

**⚠️ 必须修正的致命缺陷（对应坑 2c）**
外部 AI 原方案写「Rust 把 `&[u8]` 直接 `from_utf8_lossy` 转 `&str`」——默认源文件是 UTF-8，**对 GBK/GB18030 为主的本库会直接乱码或丢章**。这正是坑 2c 死过一次的 `encoding_rs` 与 Python `gbk` 不一致问题：若让 Rust 用 `encoding_rs` 解 GBK，又会在非法字节上和 Python `gbk` codec 分叉，raw_offsets 的悲剧重演。

**修正后的解码边界**：即便在纯 Rust 批处理引擎里，**解码这一步仍交给 Python**（保留与原始 Python 版逐位等价的容错解码），通过 channel / stdin 把 **UTF-8 文本**喂给单进程 Rust 批处理引擎；或等价地，Rust 批处理引擎只吃 UTF-8 字节流，GBK→UTF-8 的转换留在 Python 侧。一句话：**调度和切章进 Rust，解码不出 Python**——否则退回坑 2c。

> 与阶段 A 同根递进：A 是「每文件一次进程、零 temp」的低风险验证步；B 是「单进程、零 temp、全核、零重复编译」的规模目标。两者不矛盾，B 是 A 的延伸。

**实施成本与不确定性（不夸大）**
- Rust 依赖 `rayon` **已加入**（并行切章）；`memmap2` / `walkdir` **未采用**——因解码红线要求 GBK→UTF-8 留在 Python 侧，Rust 不经文件系统遍历，改为 Python 遍历 + stdin 帧喂 `--serve`（等价达成「单进程常驻 + 全核」目标，且绕开坑 2c）。所需的常驻 CLI 入口已实现为 `--serve`（非独立 `--batch` 模块）。
- 吞吐量数字（NVMe 10–20 分钟 / HDD 1.5–2 小时）是**理论估算**，取决于实际存储介质与 845GB 语料所在磁盘；本仓库未实测，落地后须用 100 文件样本先验证无瓶颈。
- 风险与现有 raw 模式一致：个别文件编码既非 GBK 也非 UTF-8 → 走 `from_utf8_lossy` + 记 `encoding` 字段、失败不中断全量。

### 8.3 阶段 C（远期可选）：Rust 常驻进程 / PyO3 内核化

> **📌 评估结论（2026-08-03，经 31 本全量 profile 实证）**：**不做**。详见 8.2 的「阶段 C 必要性实证」——JSON 序列化开销近乎为零（22ms），IPC 可省上限仅 4%，mmap 收益被解码红线砍半、SIMD 与 fancy-regex 回溯语义冲突；且本机无 MSVC 工具链（Python 为 MSVC 构建、Rust 仅 GNU target），PyO3 路线前置需装数 GB VS Build Tools，性价比不成立。真实提速已由「release 构建替换 debug 部署」免费获得。本节保留供未来（若换纯 UTF-8 语料 / 瓶颈迁移）参考。

- **Rust 常驻进程**：省去每文件进程启动开销，需设计帧协议与并发隔离，风险最高，建议在 A、B 落地且验证通过后再评估。
- **PyO3 内核化（备选路线，来自 7-25 未来展望）**：把 `parse_chapters` 编译成原生扩展模块，Rust 侧直接持有分块、并行遍历，返回章节结构给 Python。免子进程、免序列化。
  - **预期收益（对应阶段 A/B 仍存在的瓶颈——解释器循环、进程间序列化开销、无法利用 SIMD / 内存映射）**：分块用 **mmap 零拷贝**，不再把切片塞进进程间管道；章节匹配走 **SIMD / 批量扫描**，把解释器循环换成编译期展开的指令；切口修正层整体下沉到原生侧，合并结果仍严格满足「等效于单线程一次性解析」定理——线性拓扑与安全性证明不变。
  - **代价与权衡**：FFI 边界的越界 / 生命周期安全（尤其 Rust 侧持有 Python 对象引用时）；跨平台预编译分发的维护成本（需为各平台提供 wheel / 动态库）；Python 仍负责 GUI 编排与 EPUB 封装，原生内核只替换「解析」这一段。
  - **C 路线（ctypes / CFFI）备选**：以 `ctypes` / `CFFI` 暴露 `parse_chunk()` 入口，配合 mmap 读大文件、手写 SIMD 章节匹配；性能上限最高，但边界安全、跨平台编译、内存管理都要手写兜底。与 PyO3 二选一。
  - 与 B 是两条不同路线（B 走子进程批处理，PyO3 走进程内扩展），可按实际情况二选一或并行评估。

> 一句话：**现状的线性解耦不是过度设计，而是给未来的原生重写预留了干净的替换面**——等真的遇到超大文件瓶颈时，可以把最热的那段换成 C/Rust，而上层逻辑一行不动。

### 8.4 等价性验证契约（所有阶段的验收标准）

- 基准：原始 Python 版（`versions/1_pure_python/`）的 `parse_txt` 输出。
- 必须覆盖：① 章数一致；② 标题序列一致；③ **按偏移从原文件读回的正文**（归一化行尾后）与基准逐字一致——而非仅比对 JSON 结构。
- 流程：受控样本（已知编码）→ 单本端到端 → 小批量（18 本）逐章 parity → 才可上全量。

**阶段 A/B 验证状态（2026-08-03，release 构建）**：步骤 1+2 与阶段 B 均已落地。本地样本（含 3464 章 GBK 大书《从零开始》+ 构造样本）与 Legacy 写-temp 架构在「章数 / 标题序列 / 按偏移读回正文逐字一致」三项上 100% 满足；`read_chapter` / `pack_chapters` 经 `Utf8Buffer` 全链路正常。**30 本新语料复验（2026-08-03）**：31 个 txt（含 GBK×19 + UTF-8×12）全量经 `validate_corpus.py` 校验——serve 批量 vs 逐文件管道 **parity 100%**（7595 章，0 mismatch，章数/标题/起止偏移三者全等），serve 进程数 31→2、耗时 5.83s vs 12.15s。验收已封闭；余留：真机 GUI 点测（沙箱无桌面）。

---

## 九、常见问题

- **界面起不来 / 报缺少模块**：按提示 `pip install EbookLib Pillow tkinterdnd2`；tkinterdnd2 需本机有 Tk 图形环境。
- **章节识别不对**：检查 `exportTxtTocRule..json` 是否和脚本同目录；可在 GUI 里看章节预览，必要时调整规则或正文格式。
- **编码乱码**：先在编码预览里切到正确编码（常见 `utf-8` / `gbk`）再转换。
- **转换很慢**：大文件会走并行解析；核心数越多、文件越大收益越明显。
- **Rust 解析引擎未找到**：检查 `parse_txt_rust.exe` 是否与 `txt_to_epub_gui_2.py` 同目录，或是否被杀毒软件隔离（报错文案见 `txt_to_epub_core.py` 的「未找到 Rust 匹配引擎」）。

---

## 十、环境依赖

### Python 侧

- **Python 3**（已在 CPython 3.12 下运行；需桌面图形环境，依赖 Tk）。
- 第三方库：`EbookLib`、`Pillow`（内核）；`tkinterdnd2`（界面拖拽）。`tkinter` 为标准库自带。
- 图形界面启动时会自动检查缺失依赖并尝试 `pip install`。

### Rust 工具链（如需自行编译，可选）

- 源码在 `rust/`（`edition = "2021"`，对应 rustc 1.56+；推荐 GNU 工具链 `x86_64-pc-windows-gnu`）。
- 依赖：`fancy-regex 0.13`（复刻 Python 3.12 `re` 环视语义）、`serde_json 1`、`encoding_rs 0.8`（raw 模式用）。
- 重新编译：`cd rust && cargo build --release`，产物 `target/release/parse_txt_rust.exe` 复制到本目录同名文件即生效（见 `txt_to_epub_core.py` 顶部注释）。
- **预编译产物 `parse_txt_rust.exe` 已包含在仓库中，无需 Rust 环境即可运行 GUI / 转换。**

---

## 许可

个人学习 / 自用用途。如需二次分发或商用，请自行确认相关授权。
