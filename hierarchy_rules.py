"""hierarchy_rules.py — 数据驱动的分层目录规则（激进备用方案，技术储备）

设计动机
--------
旧版 ``detect_volumes`` 是**硬编码优先级**：卷>部>篇>集>章，预设"哪些单位是卷"。
本模块反转范式，改为**数据驱动推断层级**，依据用户提出的命名法原则：

  1. 作者在一本书里只用一种命名法，章/回/集/卷不混用同级。
  2. 同时出现两种匹配 → 必为上下级（共存即层级）。
  3. 数量定高低：各候选单位独立行匹配数升序排，少=容器(高层)、多=章节(低层)，
     且容器计数须明显少于章节（``容器 <= 章节/ratio``，默认 ratio=2 即占比≤一半）
     才算明确层级；占比近 1（疑似切块）则退回扁平。
  4. 数量接近=异常信号（原书不会有）→ 多半是网站按"几字一节"切块导致"节"忽大忽小；
     此时出现"章"就认准章，把"节/异常单位"当干扰项排除。

为什么是低耦合小模块（单向无环）
--------------------------------
- 本模块**只读** ``index`` 这一既有的数据结构（``parse_txt_index`` 的产出），
  每个元素是 ``{"start": int, "end": int, "title": str}``。
- 它**只消费 ``title`` 字段**做类型判定，不碰源文件、不回头读磁盘、不碰编码，
  更不反向 import 主体 ``txt_to_epub_core`` 的运行函数。
- 返回形状与 ``core.detect_volumes`` 完全一致：``[(卷名, [成员下标]), ...]``，
  可直接喂给 ``core.build_epub(volumes=...)``；无 accepted 容器时返回 ``[]``
  （调用方据此退回扁平 TOC，零回归）。
- 依赖只发生在 ``convert_cli.py`` 编排层，库层零互 import。

怎么手动调（不满意时改这里）
--------------------------------
所有可调旋钮都在 ``DEFAULT_RULES`` 里：
  - ``ratio``        ：容器接受阈值（语义：容器计数 <= 章节计数/ratio，默认 2=容器占比≤一半）。
                       调大(如10)更严（只接受"少一个数量级"的明确层级），调小(如1.5)更松。
  - ``unit_level``   ：单位层级，仅用于"同行多标记去重"与"数量接近时 tie-break"，
                       数值越大越偏高(容器)层；不用于硬判谁是卷。
  - ``noise_when_chapter`` ：章出现时一律当干扰的单位（默认 ``["节"]``）。
  - ``max_len`` / ``body_kw`` / ``end_kw`` / ``prose_punct`` / ``trim`` ：
                       严格守卫参数（复用 ``_is_volume_line`` 思路）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 规则（全部参数化，手动改这里即可）
# ---------------------------------------------------------------------------

DEFAULT_RULES: Dict[str, object] = {
    # 候选分卷/章节单位集（顺序无关，优先级由 unit_level 决定）
    "units": ["卷", "部", "篇", "集", "章", "回", "节"],
    # 单位层级：数值越大=越高层(容器)。仅用于去重与 tie-break，不硬判层级。
    "unit_level": {"卷": 6, "部": 5, "篇": 4, "集": 3, "章": 2, "回": 1, "节": 0},
    # 容器接受阈值（语义：候选单位计数 <= 章节计数 / ratio，即容器须明显少于章节）。
    # 默认 2 = 容器至多占章节的一半。用途：拒绝"容器占比近 1"的切块异常
    # （如网站按几字一节切出大量『节』，使 节≈章），同时接受各类真书层级
    # （小书 2卷8章、稠密网文 200卷1000章 都不会被误杀）。
    # 注：用户原提"少一个数量级(10×)"作『明确层级』理想值，但 10× 会误杀
    # 分卷偏密的真书；切块异常其实已由 noise_when_chapter(节永不入容器) 挡掉，
    # 故此处用 0.5 占比兜底即可。若你确要更严，把 ratio 调大到 10。
    "ratio": 2,
    # 章锚定排干扰：存在「章」时这些单位一律当干扰(不建容器)。默认含「节」。
    # 若你的书真用「节」作分卷单位，把 "节" 从此列表移除即可。
    "noise_when_chapter": ["节"],
    # ---- 严格守卫（复用 _is_volume_line 思路）----
    "max_len": 20,                       # 去边界字符后整行超过此长度→非卷标题
    "body_kw": ["章", "回", "节"],       # 含这些→是章节标题行，非容器
    "end_kw": ["完", "终", "尾声", "结束"],  # 含这些→卷尾标记，非分卷起点
    "prose_punct": "。，、！？；：\"\"''（）【】《》〈〉—…·~～",
    "trim": "【】()（）[] \u3000\t\r",     # 判定前剥掉的边界字符
}


# ---------------------------------------------------------------------------
# 内部正则
# ---------------------------------------------------------------------------

_NUM = r"[0-9零一二三四五六七八九十百千]"
# 容器候选模式：只认 卷/部/篇/集（章/回/节 由 body_kw 排除，不放进来）
_VOL_PAT = re.compile(r"第\s*" + _NUM + r"+\s*([卷部篇集])")
# 裸匹配（含 章/回/节）用于计数与章锚定判定
_BARE_PAT = re.compile(r"第\s*" + _NUM + r"+\s*([卷部篇集章回节])")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _title(entry) -> str:
    """从 index 元素里取 title（兼容 dict 与对象两种形态）。"""
    if isinstance(entry, dict):
        return entry.get("title", "") or ""
    return getattr(entry, "title", "") or ""


def _compact(title: str, rules: dict) -> str:
    """剥掉边界字符并 strip，供守卫判定（卷名显示仍用原 title）。"""
    trans = {ord(c): None for c in rules["trim"]}
    return title.strip().translate(trans)


def _is_container_line(title: str, rules: dict) -> bool:
    """严格守卫：判断某行是否『独立的分卷标记行』（复用 _is_volume_line 思路）。

    例：『【第一卷】』『第二篇 战神罗峰』『第一篇 一夜觉醒 第一集 深夜觉醒』算；
        『第20章 暗金色圆球（第一集终章）』（含章、括号）不算；
        『继续阅读第三篇——驯兽篇。』（叙述句、含句号破折号）不算；
        『第一卷终』（卷尾）不算；
        『第九集团军，很强。』（含叙述逗号）不算——挡掉"集团"误抓"集"；
        『《断灭》第二卷尽皆施展！…』（含《》！）不算——挡掉正文叙述里的"第二卷"。
    """
    stripped = title.strip()
    if not stripped:
        return False
    compact = _compact(title, rules)
    matches = list(_VOL_PAT.finditer(compact))
    if not matches:
        return False
    if matches[0].start() > 0:          # 标记须位于行首（strip 已去前导空白）
        return False
    if any(k in compact for k in rules["body_kw"]):
        return False                    # 含章/回/节→章节标题行，非容器
    if any(k in compact for k in rules["end_kw"]):
        return False                    # 含卷尾标记→某卷结束，非分卷起点
    if any(p in compact for p in rules["prose_punct"]):
        return False                    # 含叙述性标点→散文句，非卷标题
    if len(compact) > rules["max_len"]:
        return False                    # 整行过长→正文段落
    return True


def _container_unit(title: str, rules: dict) -> Optional[str]:
    """若 title 是容器行，返回其单位（同行多标记按 unit_level 取最高层）；否则 None。

    处理"第一篇…第一集…"同行含多标记：取层级更高的「篇」而非「集」，实现去重。
    """
    if not _is_container_line(title, rules):
        return None
    compact = _compact(title, rules)
    units = [m.group(1) for m in _VOL_PAT.finditer(compact)]
    if not units:
        return None
    lvl = rules["unit_level"]
    return max(units, key=lambda u: lvl.get(u, 0))


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def analyze(index: Sequence, rules: Optional[dict] = None) -> dict:
    """扫描 index，返回每单位原始计数与层级判定，便于手动排查（不改任何结构）。

    返回字典含：raw_counts(裸计数) / container_candidates(严格守卫后的容器候选计数)
    / chapter_count(章节级条目数) / has_chapter_marker / accepted_containers
    / ratio。convert_cli 可打印它让用户看到"为什么这么分"。
    """
    rules = rules or DEFAULT_RULES
    raw_counts = {u: 0 for u in rules["units"]}
    container_units: Dict[str, int] = {}
    n_container = 0

    for entry in index:
        t = _compact(_title(entry), rules)
        u = _container_unit(_title(entry), rules)
        if u:
            container_units[u] = container_units.get(u, 0) + 1
            n_container += 1
        for m in _BARE_PAT.finditer(t):
            raw_counts[m.group(1)] = raw_counts.get(m.group(1), 0) + 1

    chapter_count = len(index) - n_container
    has_chapter = raw_counts.get("章", 0) > 0 or raw_counts.get("回", 0) > 0
    noise = set(rules["noise_when_chapter"]) if has_chapter else set()

    accepted: Dict[str, int] = {}
    for u, c in container_units.items():
        if u in noise:
            continue
        if c * rules["ratio"] <= chapter_count:   # 容器须比章节少 ratio 倍
            accepted[u] = c

    return {
        "raw_counts": raw_counts,
        "container_candidates": container_units,
        "chapter_count": chapter_count,
        "has_chapter_marker": has_chapter,
        "accepted_containers": accepted,
        "ratio": rules["ratio"],
    }


def build_hierarchy(index: Sequence, rules: Optional[dict] = None) -> List[Tuple[str, List[int]]]:
    """纯函数：只读 index 的 title 与顺序，按数量差定层级 + 章锚定排干扰建嵌套。

    返回 ``[(卷名, [成员下标]), ...]``，形状与 ``core.detect_volumes`` 一致，
    可直接喂 ``core.build_epub(volumes=...)``。无 accepted 容器时返回 ``[]``
    （调用方退回扁平 TOC，零回归）。

    归属规则（基于 index 顺序，等价 source 字节偏移定位，但无需重扫源）：
      - 第 k 个容器区间 [ci_k+1, ci_{k+1}) 内的章节级项归容器 k；
      - 首个容器之前的章节级项归入「正文」组（与 detect_volumes 一致）；
      - 空容器组（相邻卷标记等异常）丢弃；所有组皆空则退回 []。
    """
    rules = rules or DEFAULT_RULES
    info = analyze(index, rules)
    accepted = info["accepted_containers"]
    if not accepted:
        return []

    tags: List[Tuple[str, Optional[str]]] = []   # ("vol"/"chap", 单位|None)
    container_idx: List[int] = []
    for i, entry in enumerate(index):
        u = _container_unit(_title(entry), rules)
        if u and u in accepted:
            tags.append(("vol", u))
            container_idx.append(i)
        else:
            tags.append(("chap", None))

    n = len(index)
    boundaries = container_idx + [n]
    groups: List[Tuple[str, List[int]]] = []
    for k, ci in enumerate(container_idx):
        end = boundaries[k + 1]
        # 含 ci 自身作为 members[0]：使父节点指向卷标记行所在页（卷标题页），
        # 与 legacy 的 volumes 形状一致；build_epub 据此排除 members[0] 去重。
        members = [ci] + [j for j in range(ci + 1, end) if tags[j][0] == "chap"]
        groups.append((_title(index[ci]), members))

    # 首个容器之前的章节级项 → 正文组
    leading = [j for j in range(container_idx[0]) if tags[j][0] == "chap"]
    if leading:
        groups.insert(0, ("正文", leading))

    return groups if groups else []


# ---------------------------------------------------------------------------
# 自检（python hierarchy_rules.py 直接跑）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from encoding_detect import detect_encoding
    import txt_to_epub_core as core

    if len(sys.argv) < 2:
        print("用法: python hierarchy_rules.py 小说.txt")
        sys.exit(1)
    p = sys.argv[1]
    enc, _ = detect_encoding(p)
    _, index = core.parse_txt_index(p, enc)
    info = analyze(index)
    print(f"条目总数: {len(index)}")
    print(f"raw_counts(裸计数): {info['raw_counts']}")
    print(f"container_candidates(严格守卫后容器候选): {info['container_candidates']}")
    print(f"chapter_count(章节级): {info['chapter_count']}")
    print(f"has_chapter_marker: {info['has_chapter_marker']}")
    print(f"accepted_containers(接受为容器): {info['accepted_containers']} (ratio={info['ratio']})")
    groups = build_hierarchy(index)
    print(f"分组数: {len(groups)}")
    empty = [g for g in groups if not g[1]]
    print(f"空组(应为0): {len(empty)}")
    tot = sum(len(m) for _, m in groups)
    print(f"各组成员下标合计: {tot} (应=总条目数={len(index)})")
    for name, members in groups[:8]:
        print(f"  {name!r:40s} 章数={len(members)}")
