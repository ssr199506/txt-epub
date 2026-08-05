#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TXT→ePub 批量处理版（编码实时预览 & 章节预览 & 全编码支持 & 实时进度条）
依赖：pip install EbookLib tkinterdnd2
"""
import os
import pickle
import tempfile
import shutil
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4
import threading
import queue
from typing import Optional

# ===================================================================
# 第一步：依赖检查（在 tk 窗口创建之前完成）
# ===================================================================
def _check_deps():
    missing = []
    try:
        import tkinterdnd2  # noqa: F401
    except ImportError:
        missing.append("tkinterdnd2")
    try:
        import ebooklib  # noqa: F401
    except ImportError:
        missing.append("EbookLib")
    try:
        import PIL  # noqa: F401
    except ImportError:
        missing.append("Pillow")
    return missing


_missing = _check_deps()
if _missing:
    print(f"缺少依赖: {', '.join(_missing)}，正在自动安装…")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", *_missing],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        print("依赖安装成功")
    except Exception as exc:
        print(f"自动安装失败: {exc}")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "依赖安装失败",
            f"以下依赖自动安装失败:\n{', '.join(_missing)}\n\n"
            f"错误信息:\n{exc}\n\n"
            f"请手动运行:\n  pip install {' '.join(_missing)}"
        )
        root.destroy()
        sys.exit(1)

    _still_missing = _check_deps()
    if _still_missing:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "依赖检查失败",
            f"安装后仍无法导入: {', '.join(_still_missing)}，程序退出。"
        )
        root.destroy()
        sys.exit(1)

# ===================================================================
# 第二步：导入核心模块和拖拽支持
# ===================================================================
sys.path.insert(0, str(Path(__file__).resolve().parent))
from txt_to_epub_core import (  # noqa: E402
    TOC_RE,
    parse_txt,
    build_epub,
    build_epub_from_pack,
    volume_ranges,
    convert_single,
    ConversionResult,
    load_toc_rules,
    parse_txt_index,
    read_chapter,
    pack_chapters,
    Utf8Buffer,
    _count_lines,
    _parse_chunk,
    _crop_cover_image,
    HAVE_PIL,
)
from ebooklib import epub as _epub  # noqa: E402 — 依赖已在上方验证
from tkinterdnd2 import DND_FILES, TkinterDnD  # noqa: E402

# Pillow 用于封面裁剪预览
if HAVE_PIL:
    from PIL import Image, ImageTk

# ===================================================================
# 章节规则 JSON 路径
# ===================================================================
TOC_RULES_JSON = Path(__file__).resolve().parent / "exportTxtTocRule..json"


# ===================================================================
# 动态分块并行配置（自适应分段表）
# ===================================================================
_CHUNK_CONFIG = [
    (5 * 1024 * 1024,   1),   # < 5MB
    (15 * 1024 * 1024,  2),   # 5~15MB
    (50 * 1024 * 1024,  4),   # 15~50MB
    (200 * 1024 * 1024, 4),   # 50~200MB（保守 4 核）
    (float('inf'),       6),   # >200MB
]

def _get_optimal_chunks(file_size: int) -> int:
    """根据文件大小查表确定最优分段数"""
    for threshold, chunks in _CHUNK_CONFIG:
        if file_size < threshold:
            return chunks
    return 6

def _is_parallel_available() -> bool:
    """检测环境是否具备并行解析条件（至少 2 个逻辑核）"""
    return (os.cpu_count() or 1) > 1


# ===================================================================
# GUI 应用
# ===================================================================
class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.geometry("950x750")
        self.minsize(750, 600)
        self.resizable(True, True)
        self.configure(bg="#ffffff")

        # ---- 变量 ----
        self.txt_path = tk.StringVar()
        self.out_path = tk.StringVar()
        self.book_title = tk.StringVar()
        self.author = tk.StringVar(value="Unknown")
        self.encoding = tk.StringVar(value="utf-8")
        self.max_chapters_per_volume = tk.IntVar(value=0)  # 0 = 不拆分
        self._last_outputs = []  # 最近一次生成的输出路径（多卷时为多路径）
        self.batch_files: list[str] = []
        self._file_encodings: dict[str, str] = {}  # 批量模式下每文件的独立编码
        self.is_batch_mode = False
        self.chapters: list = []    # (标题, 正文, 索引项) 三元组；正文初始空、按需懒加载
        self._temp_path = None      # 轻量索引对应的 UTF-8 临时文件（read_chapter / pack 按偏移 seek）
        self._chapters_edited = False  # 是否发生过删除合并（发生则生成走 build_epub 回退）
        self._full_text = ""        # 正文预览的完整文本缓存
        self._preview_pos = 0       # 已加载到预览框的字符位置
        self._cached_encoding = ""  # 预览时使用的编码（用于检测缓存失效）

        # 进度
        self.progress_queue: queue.Queue = queue.Queue()

        # 封面裁剪状态
        self._cover_pil_image = None   # PIL Image 原始
        self._cover_tk_image = None    # PhotoImage 用于显示
        self._cover_img_x = 0          # 图片在 canvas 上的 x
        self._cover_img_y = 0          # 图片在 canvas 上的 y
        self._cover_scale = 1.0        # 显示缩放比
        self._cover_disp_w = 0         # 显示宽度
        self._cover_disp_h = 0         # 显示高度
        self._cover_sel_w = 0          # 裁剪框宽度
        self._cover_sel_h = 0          # 裁剪框高度
        self._cover_drag_start_x = 0   # 拖拽起始 x
        self._cover_drag_start_y = 0   # 拖拽起始 y
        self._cover_img_orig_x = 0     # 拖拽起始图片位置 x
        self._cover_img_orig_y = 0     # 拖拽起始图片位置 y

        # 删除撤销栈
        self._delete_history: list = []  # [(idx, (title,content), prev_chapter), ...]

        # 拖拽排序状态
        self._drag_sel = -1

        # 中键自动滚动状态
        self._auto_scroll_active = False
        self._auto_scroll_widget = None  # 当前滚动的控件
        self._auto_scroll_y = 0
        self._auto_scroll_speed = 0
        self._auto_scroll_after_id = None
        self._auto_scroll_marker = None
        # 删除确认浮窗
        self._delete_popup = None
        # 全局点击退出自动滚动（一次性绑定，永久生效）
        self.bind("<Button-1>", self._global_click_stop, add="+")
        self.bind("<Button-1>", self._global_click_close_popup, add="+")
        self.bind("<Button-2>", self._global_click_stop, add="+")
        self.bind("<Button-3>", self._global_click_stop, add="+")

        # ---- 加载章节规则 ----
        self.toc_rules: list[dict] = []
        try:
            if TOC_RULES_JSON.is_file():
                self.toc_rules = load_toc_rules(str(TOC_RULES_JSON))
        except Exception as e:
            print(f"章节规则加载失败: {e}")

        # ---- 拖拽绑定 ----
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self.on_drop)
        except Exception as e:
            print(f"⚠️  拖拽功能初始化失败: {e}")

        # ---- 可滚动主区域 ----
        main_frame = tk.Frame(self, bg="#ffffff")
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        canvas = tk.Canvas(main_frame, bg="#ffffff", highlightthickness=0)
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg="#ffffff")

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ---- 顶部按钮行 ----
        btn_frame = tk.Frame(scrollable_frame, bg="#ffffff")
        btn_frame.pack(pady=(20, 10))

        self.convert_btn = tk.Button(
            btn_frame,
            text="🚀 开始转换",
            command=self.run,
            font=("微软雅黑", 12, "bold"),
            bg="#ff6b6b", fg="#ffffff",
            activebackground="#ff8787",
            relief="raised", bd=2, padx=30, pady=10,
        )
        self.convert_btn.pack(side="left", padx=(0, 10))

        self.clear_btn = tk.Button(
            btn_frame,
            text="🧹 清空重置",
            command=self.clear_all,
            font=("微软雅黑", 12, "bold"),
            bg="#4ecdc4", fg="#ffffff",
            activebackground="#6cd3c5",
            relief="raised", bd=2, padx=30, pady=10,
        )
        self.clear_btn.pack(side="left", padx=(0, 10))

        self.batch_btn = tk.Button(
            btn_frame,
            text="📚 批量处理",
            command=self.select_batch_files,
            font=("微软雅黑", 12, "bold"),
            bg="#45b7d1", fg="#ffffff",
            activebackground="#5ac0d5",
            relief="raised", bd=2, padx=30, pady=10,
        )
        self.batch_btn.pack(side="left")

        # ---- 步骤提示 ----
        ttk.Label(
            scrollable_frame,
            text="点击上面的按钮开始转换，或按以下步骤操作：",
            font=("微软雅黑", 10),
        ).pack(pady=(0, 10))

        # ================================================================
        # ① 选择文件
        # ================================================================
        ttk.Label(
            scrollable_frame,
            text="① 选择 TXT 文件（支持拖拽）",
            font=("微软雅黑", 12, "bold"),
        ).pack(pady=6, anchor="w")
        f1 = ttk.Frame(scrollable_frame)
        f1.pack(fill="x", padx=20, pady=5)
        ttk.Entry(f1, textvariable=self.txt_path, width=80, state="readonly").pack(
            side="left", padx=(0, 6), fill="x", expand=True
        )
        ttk.Button(f1, text="浏览…", width=8, command=self.browse_txt).pack(side="right")

        # ================================================================
        # 批量文件列表（紧跟文件选择，双击切换预览无需上下滚动）
        # ================================================================
        ttk.Label(
            scrollable_frame,
            text="📚 批量文件列表（双击文件可单独预览）：",
            font=("微软雅黑", 12, "bold"),
        ).pack(pady=(6, 5), anchor="w")
        lf = ttk.Frame(scrollable_frame)
        lf.pack(fill="x", padx=20, pady=5)

        self.file_listbox = tk.Listbox(lf, height=5, font=("微软雅黑", 10))
        lf_scroll = ttk.Scrollbar(lf, orient="vertical", command=self.file_listbox.yview)
        self.file_listbox.configure(yscrollcommand=lf_scroll.set)
        self.file_listbox.pack(side="left", fill="both", expand=True)
        lf_scroll.pack(side="right", fill="y")
        self.file_listbox.bind("<Double-Button-1>", self.preview_selected_file)
        self.file_listbox.bind("<MouseWheel>", self._file_listbox_wheel)

        # ================================================================
        # ② 编码预览
        # ================================================================
        ttk.Label(
            scrollable_frame,
            text="② 编码实时预览（前 800 字）",
            font=("微软雅黑", 12, "bold"),
        ).pack(pady=6, anchor="w")
        f_enc = ttk.Frame(scrollable_frame)
        f_enc.pack(fill="x", padx=20, pady=5)
        self.cmb_enc = ttk.Combobox(
            f_enc,
            textvariable=self.encoding,
            values=["utf-8", "utf-16", "gb2312", "gbk", "gb18030", "big5", "shift_jis"],
            width=12, state="readonly",
        )
        self.cmb_enc.pack(side="left", padx=(0, 12))
        self.cmb_enc.bind("<<ComboboxSelected>>", self._on_encoding_changed)
        ttk.Label(f_enc, text="（乱码请换编码）").pack(side="left")

        pv_frame = ttk.Frame(scrollable_frame)
        pv_frame.pack(fill="both", expand=True, padx=20, pady=5)
        self.txt_preview = tk.Text(
            pv_frame, height=8, wrap="word", font=("微软雅黑", 10)
        )
        pv_scroll = ttk.Scrollbar(pv_frame, orient="vertical", command=self.txt_preview.yview)
        self.txt_preview.configure(yscrollcommand=pv_scroll.set)
        self.txt_preview.pack(side="left", fill="both", expand=True)
        pv_scroll.pack(side="right", fill="y")
        self.txt_preview.insert("1.0", "请先选择 TXT 文件")
        self.txt_preview.config(state="disabled")
        self.txt_preview.bind("<MouseWheel>", self._txt_wheel)
        self.txt_preview.bind("<Button-2>", lambda e: self._start_auto_scroll(e, self.txt_preview))

        # ================================================================
        # ③ 章节预览
        # ================================================================
        toc_header = ttk.Label(
            scrollable_frame,
            text="③ 章节预览（可拖拽或使用按钮调整顺序）",
            font=("微软雅黑", 12, "bold"),
        )
        toc_header.pack(pady=6, anchor="w")

        # 规则选择行
        toc_ctrl = ttk.Frame(scrollable_frame)
        toc_ctrl.pack(fill="x", padx=20, pady=5)

        ttk.Label(toc_ctrl, text="识别规则：").pack(side="left")
        self.toc_rule_var = tk.StringVar()
        self.cmb_toc_rule = ttk.Combobox(
            toc_ctrl,
            textvariable=self.toc_rule_var,
            width=35, state="readonly",
        )
        self.cmb_toc_rule.pack(side="left", padx=(0, 10))
        self.cmb_toc_rule.bind("<<ComboboxSelected>>", self._on_rule_changed)

        # 多卷拆分选项（借鉴 legado 的大书分卷；默认 0=不拆分）
        ttk.Label(toc_ctrl, text="  每卷章数：").pack(side="left")
        self.spin_vol = ttk.Spinbox(
            toc_ctrl, from_=0, to=2000, increment=50,
            textvariable=self.max_chapters_per_volume, width=6,
        )
        self.spin_vol.pack(side="left", padx=(0, 4))
        ttk.Label(toc_ctrl, text="（0=不拆分）").pack(side="left")

        self.parse_btn = tk.Button(
            toc_ctrl,
            text="🔍 解析章节",
            command=self._parse_chapters,
            font=("微软雅黑", 10, "bold"),
            bg="#95e1d3", fg="#333",
            relief="raised", bd=1, padx=12, pady=2,
        )
        self.parse_btn.pack(side="left")

        ttk.Label(toc_ctrl, text="（切换规则后自动重解析）").pack(side="left", padx=(10, 0))

        # 章节列表 + 操作按钮
        toc_body = ttk.Frame(scrollable_frame)
        toc_body.pack(fill="both", expand=True, padx=20, pady=5)

        # 章节列表（左侧）
        list_frame = ttk.Frame(toc_body)
        list_frame.pack(side="left", fill="both", expand=True)

        self.chapter_listbox = tk.Listbox(
            list_frame, height=8, font=("微软雅黑", 10),
            selectbackground="#a8d8ea", selectforeground="#333",
            activestyle="none",
        )
        ch_scroll = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.chapter_listbox.yview
        )
        self.chapter_listbox.configure(yscrollcommand=ch_scroll.set)
        self.chapter_listbox.pack(side="left", fill="both", expand=True)
        ch_scroll.pack(side="right", fill="y")

        # 拖拽排序事件
        self.chapter_listbox.bind("<Button-1>", self._ch_drag_start)
        self.chapter_listbox.bind("<B1-Motion>", self._ch_drag_motion)
        self.chapter_listbox.bind("<ButtonRelease-1>", self._ch_drag_end)

        # 鼠标滚轮独立滚动（阻止冒泡到外层 canvas）
        self.chapter_listbox.bind("<MouseWheel>", self._ch_listbox_wheel)

        # 双击章节跳转到正文预览对应位置
        self.chapter_listbox.bind("<Double-Button-1>", self._ch_double_click)

        # 中键点击切换自动滚动
        self.chapter_listbox.bind("<Button-2>", lambda e: self._start_auto_scroll(e, self.chapter_listbox))

        # 右键→删除浮窗
        self.chapter_listbox.bind("<Button-3>", self._ch_right_click_popup)

        # 上下移动按钮（右侧）
        btn_side = ttk.Frame(toc_body)
        btn_side.pack(side="right", fill="y", padx=(10, 0))

        ttk.Button(
            btn_side, text="▲  上移",
            command=self._move_chapter_up,
            width=8,
        ).pack(pady=(0, 6))

        ttk.Button(
            btn_side, text="▼  下移",
            command=self._move_chapter_down,
            width=8,
        ).pack(pady=(0, 6))

        self._undo_btn = ttk.Button(
            btn_side, text="↩  撤回",
            command=self._undo_delete,
            width=8,
        )
        self._undo_btn.pack(pady=(0, 6))
        self._undo_btn.config(state="disabled")  # 初始不可用

        ttk.Separator(btn_side, orient="horizontal").pack(fill="x", pady=6)

        ttk.Label(btn_side, text="章节数", font=("微软雅黑", 9)).pack()
        self.label_chapter_count = ttk.Label(
            btn_side, text="0", font=("微软雅黑", 14, "bold"), foreground="#ff6b6b"
        )
        self.label_chapter_count.pack()

        # ================================================================
        # ④ 设置元数据
        # ================================================================
        ttk.Label(
            scrollable_frame,
            text="④ 设置元数据",
            font=("微软雅黑", 12, "bold"),
        ).pack(pady=6, anchor="w")
        f2 = ttk.Frame(scrollable_frame)
        f2.pack(fill="x", padx=20, pady=5)
        ttk.Label(f2, text="书名：").grid(row=0, column=0, sticky="w", padx=(0, 5))
        ttk.Entry(f2, textvariable=self.book_title, width=40).grid(
            row=0, column=1, padx=6, sticky="ew"
        )
        ttk.Label(f2, text="作者：").grid(
            row=0, column=2, sticky="w", padx=(20, 5)
        )
        ttk.Entry(f2, textvariable=self.author, width=20).grid(
            row=0, column=3, padx=6, sticky="ew"
        )
        f2.columnconfigure(1, weight=1)

        # ================================================================
        # ⑥ 封面图片（可选）
        # ================================================================
        ttk.Label(
            scrollable_frame,
            text="⑥ 封面图片（可选）—— 拖入图片或点击选择，拖动图片调整裁剪区域",
            font=("微软雅黑", 12, "bold"),
        ).pack(pady=(20, 5), anchor="w")
        cover_frame = ttk.Frame(scrollable_frame)
        cover_frame.pack(fill="both", expand=True, padx=20, pady=5)

        self._cover_canvas = tk.Canvas(
            cover_frame,
            width=300, height=400,
            bg="#2d2d2d",
            highlightthickness=1,
            highlightbackground="#555",
            cursor="hand2",
        )
        self._cover_canvas.pack(side="left")

        # 封面提示文本（无图时显示）
        self._cover_canvas.create_text(
            150, 200, text="拖入图片\n设为首章封面",
            fill="#888", font=("微软雅黑", 14),
            justify="center",
            tags="hint",
        )

        # 封面右侧按钮
        cover_side = ttk.Frame(cover_frame)
        cover_side.pack(side="left", fill="y", padx=(10, 0))

        self._cover_sel_btn = ttk.Button(
            cover_side, text="📁 选择图片",
            command=self._cover_select_file,
        )
        self._cover_sel_btn.pack(pady=(0, 6))

        self._cover_clear_btn = ttk.Button(
            cover_side, text="🗑 移除封面",
            command=self._cover_clear,
        )
        self._cover_clear_btn.pack(pady=(0, 6))

        self._cover_status = ttk.Label(
            cover_side,
            text="未选择封面图片",
            font=("微软雅黑", 9),
            foreground="#888",
        )
        self._cover_status.pack(pady=(6, 0))

        # 裁剪框比例说明
        ttk.Label(
            cover_side,
            text="裁剪框比例 ≈ 2:3\n（标准书封比例）",
            font=("微软雅黑", 8),
            foreground="#999",
        ).pack(pady=(10, 0), anchor="w")

        # 注册拖拽
        self._cover_canvas.drop_target_register(DND_FILES)
        self._cover_canvas.dnd_bind("<<Drop>>", self._cover_on_drop)

        # 拖拽图片移动
        self._cover_canvas.tag_bind("cover_img", "<Button-1>", self._cover_drag_start)
        self._cover_canvas.tag_bind("cover_img", "<B1-Motion>", self._cover_drag_motion)
        self._cover_canvas.tag_bind("cover_img", "<ButtonRelease-1>", self._cover_drag_end)
        # 点击空白处也可以拖拽（绑定到整张 canvas）
        self._cover_canvas.bind("<Button-1>", self._cover_drag_start)
        self._cover_canvas.bind("<B1-Motion>", self._cover_drag_motion)
        self._cover_canvas.bind("<ButtonRelease-1>", self._cover_drag_end)

        # 缩放canvas时重绘
        self._cover_canvas.bind("<Configure>", self._cover_redraw)

        # ================================================================
        # ⑤ 输出路径
        # ================================================================
        ttk.Label(
            scrollable_frame,
            text="⑤ 输出路径（可选）",
            font=("微软雅黑", 12, "bold"),
        ).pack(pady=6, anchor="w")
        f3 = ttk.Frame(scrollable_frame)
        f3.pack(fill="x", padx=20, pady=5)
        ttk.Entry(f3, textvariable=self.out_path, width=80).pack(
            side="left", padx=(0, 6), fill="x", expand=True
        )
        ttk.Button(f3, text="浏览…", width=8, command=self.browse_out).pack(side="right")

        # ================================================================
        # 进度条
        # ================================================================
        ttk.Label(
            scrollable_frame,
            text="🔄 处理进度：",
            font=("微软雅黑", 12, "bold"),
        ).pack(pady=(20, 5), anchor="w", padx=20)
        pf = ttk.Frame(scrollable_frame)
        pf.pack(fill="x", padx=20, pady=5)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            pf, variable=self.progress_var, maximum=100, length=700
        )
        self.progress_bar.pack(fill="x", pady=5)
        self.progress_label = ttk.Label(pf, text="等待开始...", font=("微软雅黑", 10))
        self.progress_label.pack(anchor="w")

        # 底部留白
        tk.Label(scrollable_frame, text="", bg="#ffffff").pack(pady=20)

        # ---- 鼠标滚轮：组合框/列表框的滚动交给控件自己，不滚动画布 ----
        def _on_mousewheel(event):
            w = event.widget
            # 下拉弹出时 widget 可能是字符串而非控件对象，直接跳过
            if not hasattr(w, "winfo_class"):
                return
            if w.winfo_class() in (
                "TCombobox", "TEntry",
                "Listbox", "TListbox",
                "Text",
            ):
                return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ---- 填充规则下拉框 ----
        self._populate_rule_list()

        # ---- 启动进度轮询 ----
        self.after(100, self._poll_progress)

    # ================================================================
    # 规则下拉框
    # ================================================================
    def _populate_rule_list(self):
        names = ["默认(老板御用)"]
        for r in self.toc_rules:
            names.append(r["name"])
        self.cmb_toc_rule["values"] = names
        # 默认选中 "目录"
        for i, name in enumerate(names):
            if name == "目录":
                self.cmb_toc_rule.current(i)
                return
        self.cmb_toc_rule.current(0)

    def _get_selected_pattern(self):
        """返回当前选中的正则 pattern（字符串或 None）"""
        name = self.toc_rule_var.get()
        if not name or name == "默认(老板御用)":
            return None  # 使用内置 TOC_RE
        for r in self.toc_rules:
            if r["name"] == name:
                return r["rule"]
        return None

    # ================================================================
    # 章节解析
    # ================================================================
    def _parse_chapters(self, _event=None):
        """异步解析章节（轻量索引：只回传标题+偏移，正文按需懒加载，不冻 UI）。"""
        txt = Path(self.txt_path.get())
        if not txt.is_file():
            messagebox.showinfo("提示", "请先选择 TXT 文件")
            return

        pattern = self._get_selected_pattern()
        # 仅当 _full_text 的编码与当前编码一致时才复用内存文本（避免重复读盘/解码）
        text_for_parse = (
            self._full_text
            if self._full_text and self.encoding.get() == self._cached_encoding
            else None
        )
        self._parse_async(txt, self.encoding.get(), pattern, text_for_parse)

    def _parse_async(self, txt, enc, pattern, text_str):
        """后台线程跑轻量索引；解析期间列表框显示「解析中…」，不阻塞 UI。"""
        self.chapter_listbox.delete(0, tk.END)
        self.label_chapter_count.config(text="解析中…")
        def worker():
            try:
                if text_str is not None:
                    temp, index = parse_txt_index(text_str=text_str, toc_pattern=pattern)
                else:
                    # raw_offsets 模式已实装但因 encoding_rs 与 Python gbk codec 对非法字节解码不一致，
                    # 在 GBK 书上漏切章节、UTF-8 书上正文近半对不上（见 _parity.py，总一致率 71.7%），
                    # 存在真实回归，暂不启用；默认走 legacy（写 UTF-8 temp）路径，与 backup_pre_raw 行为一致。
                    temp, index = parse_txt_index(txt, enc, toc_pattern=pattern)
                self._parse_result = (temp, index, None)
            except Exception as e:
                self._parse_result = (None, None, e)
            self.after(0, self._on_parse_done)
        threading.Thread(target=worker, daemon=True).start()

    def _on_parse_done(self):
        temp, index, err = self._parse_result
        if err is not None:
            self.chapters = []
            self._temp_path = None
            self._chapters_edited = False
            self.label_chapter_count.config(text="0")
            messagebox.showerror("解析失败", f"章节解析出错：{err}")
            return
        # 清理上一轮的临时文件
        old = self._temp_path
        self._temp_path = temp
        # 护栏：raw 模式下 self._temp_path 是【用户原书】（非临时文件），绝不能删！
        # 只有本工具自己生成的 txt_epub_ 前缀临时文件才清理，避免误删源文件。
        if old and old != temp and not isinstance(old, Utf8Buffer) \
                and "txt_epub_" in os.path.basename(str(old)):
            try:
                os.remove(old)
            except OSError:
                pass
        self._chapters_edited = False
        # 3 元组：(标题, 正文(懒加载), 索引项)；正文留空，双击/生成时按需取
        self.chapters = [(e["title"], "", e) for e in index]
        self._refresh_chapter_listbox()

    def _get_body(self, idx):
        """按需读取某章正文（懒加载 + 缓存）；无索引项时回退空串。"""
        ch = self.chapters[idx]
        if ch[1]:
            return ch[1]
        entry = ch[2]
        if entry and self._temp_path:
            body = read_chapter(self._temp_path, entry)
            self.chapters[idx] = (ch[0], body, entry)
            return body
        return ""

    def _ensure_all_bodies(self):
        """编辑场景下生成前补齐所有正文（仅编辑过才调用，罕见路径）。"""
        for i, ch in enumerate(self.chapters):
            if not ch[1] and ch[2] and self._temp_path:
                self.chapters[i] = (ch[0], read_chapter(self._temp_path, ch[2]), ch[2])

    def _on_rule_changed(self, _event=None):
        """切换规则后自动重解析"""
        if not self.txt_path.get():
            return
        self._parse_chapters()

    def _refresh_chapter_listbox(self):
        """刷新章节列表框显示"""
        self.chapter_listbox.delete(0, tk.END)
        for idx, ch in enumerate(self.chapters, 1):
            title = ch[0]
            display = f"{idx}. {title}" if title else f"{idx}. (无标题)"
            self.chapter_listbox.insert(tk.END, display)
        self.label_chapter_count.config(text=str(len(self.chapters)))

    # ================================================================
    # 章节排序（按钮）
    # ================================================================
    def _move_chapter_up(self):
        sel = self.chapter_listbox.curselection()
        if not sel or sel[0] <= 0:
            return
        idx = sel[0]
        self.chapters[idx - 1], self.chapters[idx] = (
            self.chapters[idx],
            self.chapters[idx - 1],
        )
        self._refresh_chapter_listbox()
        self.chapter_listbox.selection_set(idx - 1)

    def _move_chapter_down(self):
        sel = self.chapter_listbox.curselection()
        if not sel or sel[0] >= len(self.chapters) - 1:
            return
        idx = sel[0]
        self.chapters[idx], self.chapters[idx + 1] = (
            self.chapters[idx + 1],
            self.chapters[idx],
        )
        self._refresh_chapter_listbox()
        self.chapter_listbox.selection_set(idx + 1)

    # ================================================================
    # 章节排序（鼠标拖拽）
    # ================================================================
    def _ch_drag_start(self, event):
        self._drag_sel = self.chapter_listbox.nearest(event.y)

    def _ch_drag_motion(self, event):
        # 让选中行跟随鼠标，提供视觉反馈
        idx = self.chapter_listbox.nearest(event.y)
        if 0 <= idx < len(self.chapters):
            self.chapter_listbox.selection_clear(0, tk.END)
            self.chapter_listbox.selection_set(idx)
            self.chapter_listbox.activate(idx)

    def _ch_drag_end(self, event):
        target = self.chapter_listbox.nearest(event.y)
        if (
            self._drag_sel < 0
            or target < 0
            or target >= len(self.chapters)
            or target == self._drag_sel
        ):
            self._drag_sel = -1
            return
        # 移动章节
        item = self.chapters.pop(self._drag_sel)
        self.chapters.insert(target, item)
        self._refresh_chapter_listbox()
        self.chapter_listbox.selection_set(target)
        self._drag_sel = -1

    def _ch_listbox_wheel(self, event):
        """章节列表框独立滚轮，阻止冒泡到外层 canvas"""
        self.chapter_listbox.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _txt_wheel(self, event):
        """文本预览框独立滚轮，滚动到底时异步加载更多（不阻塞滚动响应）"""
        self.txt_preview.yview_scroll(int(-1 * (event.delta / 120)), "units")
        if self.txt_preview.yview()[1] >= 0.95 and self._preview_pos < len(self._full_text):
            self.after(10, self._load_more_async)
        return "break"

    def _load_more_async(self):
        """滚动到底时的异步追加加载"""
        if self._preview_pos >= len(self._full_text):
            return
        self.txt_preview.config(state="normal")
        self._load_more_preview(chunk=4000)
        self.txt_preview.config(state="disabled")

    def _file_listbox_wheel(self, event):
        """批量文件列表独立滚轮"""
        self.file_listbox.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"



    # ================================================================
    # 中键自动滚动（通用：目录/正文预览 均可使用）
    # ================================================================
    def _global_click_stop(self, event):
        """任何点击都退出自动滚动（中键启动操作除外）"""
        if not self._auto_scroll_active:
            return
        if event.num == 2:
            return  # 中键启动滚动时不退出
        self._auto_scroll_stop()

    def _start_auto_scroll(self, event, widget):
        """中键点击启动自动滚动，widget 指定滚动的目标控件"""
        if self._auto_scroll_active:
            self._auto_scroll_stop()
            return

        if self._auto_scroll_marker:
            try:
                self._auto_scroll_marker.destroy()
            except tk.TclError:
                pass
            self._auto_scroll_marker = None

        marker = tk.Toplevel(self)
        marker.overrideredirect(True)
        marker.geometry("+%d+%d" % (event.x_root - 10, event.y_root - 10))
        marker.attributes("-topmost", True)
        marker.configure(bg="white")
        try:
            marker.attributes("-transparentcolor", "white")
        except Exception:
            pass
        marker.bind("<FocusOut>", lambda e: self._auto_scroll_stop())

        cv = tk.Canvas(marker, width=20, height=20, bg="white",
                       highlightthickness=0)
        cv.pack()
        cv.create_line(10, 0, 10, 20, fill="#e03131", width=2)
        cv.create_line(0, 10, 20, 10, fill="#e03131", width=2)
        cv.create_oval(3, 3, 17, 17, outline="#e03131", width=2)
        cv.create_oval(8, 8, 12, 12, fill="#e03131")

        self._auto_scroll_marker = marker
        self._auto_scroll_widget = widget
        self._auto_scroll_active = True
        self._auto_scroll_y = event.y_root
        self.config(cursor="crosshair")
        self.bind("<Escape>", self._auto_scroll_stop, add="+")
        self._auto_scroll_tick()

    def _auto_scroll_tick(self):
        if not self._auto_scroll_active:
            return
        x, y = self.winfo_pointerxy()
        delta = y - self._auto_scroll_y
        if abs(delta) < 15:
            speed = 0
        else:
            speed = delta / 50
            speed = max(-5, min(5, speed))
        if speed != 0 and self._auto_scroll_widget:
            try:
                self._auto_scroll_widget.yview_scroll(int(speed), "units")
                # 如果是正文预览，滚动后检查是否需要加载更多
                if self._auto_scroll_widget == self.txt_preview:
                    if self.txt_preview.yview()[1] >= 0.95 and self._preview_pos < len(self._full_text):
                        self.after(10, self._load_more_async)
            except Exception:
                pass
        self._auto_scroll_after_id = self.after(50, self._auto_scroll_tick)

    def _auto_scroll_stop(self, event=None):
        if not self._auto_scroll_active:
            return
        self._auto_scroll_active = False
        self._auto_scroll_widget = None
        self.config(cursor="")
        if self._auto_scroll_marker:
            self._auto_scroll_marker.destroy()
            self._auto_scroll_marker = None
        if self._auto_scroll_after_id:
            self.after_cancel(self._auto_scroll_after_id)
            self._auto_scroll_after_id = None

    # ================================================================
    # 删除确认浮窗（右键目录→弹出→左键确认）
    # ================================================================
    def _ch_right_click_popup(self, event):
        idx = self.chapter_listbox.nearest(event.y)
        if idx < 0 or idx >= len(self.chapters):
            return
        self.chapter_listbox.selection_clear(0, tk.END)
        self.chapter_listbox.selection_set(idx)
        if idx > 0:
            self._show_delete_popup(event, idx)

    def _show_delete_popup(self, event, idx):
        self._hide_delete_popup()
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg="#ff6b6b")
        lbl = tk.Label(
            popup, text="🗑 删除本标题",
            bg="#ff6b6b", fg="white",
            font=("微软雅黑", 9), padx=10, pady=4,
        )
        lbl.pack()
        # 左键点击浮窗 → 确认删除
        lbl.bind("<Button-1>", lambda e: self._confirm_delete(idx))
        popup.bind("<Button-1>", lambda e: self._confirm_delete(idx))
        popup.geometry("+%d+%d" % (event.x_root + 10, event.y_root + 10))
        self._delete_popup = popup

    def _hide_delete_popup(self):
        if self._delete_popup:
            try:
                self._delete_popup.destroy()
            except Exception:
                pass
            self._delete_popup = None

    def _confirm_delete(self, idx):
        self._hide_delete_popup()
        self._delete_chapter(idx)

    # 全局点击非浮窗区域关闭浮窗
    def _global_click_close_popup(self, event):
        if not self._delete_popup:
            return
        # 检查点击是否在浮窗或其子控件上
        w = event.widget
        while w:
            if w == self._delete_popup:
                return
            try:
                w = w.master
            except Exception:
                break
        self._hide_delete_popup()

    # ================================================================
    # 中键自动滚动（旧版方法保留，以防外部调用）

    # ================================================================
    # 双击章节跳转正文预览
    # ================================================================
    def _ch_double_click(self, event):
        """双击目录章节 → 异步加载并跳转到对应位置"""
        sel = self.chapter_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.chapters) or not self._full_text:
            return

        title = self.chapters[idx][0]

        # 在原文中定位章节标题
        pos = self._full_text.find(title.strip())
        if pos < 0:
            words = title.strip().split()
            if len(words) >= 2:
                pos = self._full_text.find(words[0])
                if pos >= 0:
                    snippet = self._full_text[pos:pos + len(title) + 20]
                    if title.strip() not in snippet:
                        pos = -1
        if pos < 0:
            return

        # 预计算行号（基于 _full_text，不受预览加载进度影响）
        line_num = self._full_text[:pos].count("\n") + 1
        # 需要加载到的位置：优先用下一章起点（偏移）估计，避免仅为求长度而懒加载正文
        entry = self.chapters[idx][2]
        if entry and idx + 1 < len(self.chapters) and self.chapters[idx + 1][2]:
            need = self.chapters[idx + 1][2]["start"] + 200
        elif entry:
            need = pos + len(self._get_body(idx)) + 200
        else:
            need = pos + 200

        if need <= self._preview_pos:
            self._scroll_to_line(line_num)
        else:
            self._load_to_pos_async(need, lambda: self._scroll_to_line(line_num))

    def _scroll_to_line(self, line_num):
        """跳转到指定行并高亮"""
        self.txt_preview.see(f"{line_num}.0")
        self.txt_preview.tag_remove("ch_hl", "1.0", "end")
        self.txt_preview.tag_add("ch_hl", f"{line_num}.0", f"{line_num}.0 lineend")
        self.txt_preview.tag_config("ch_hl", background="#fff3cd", foreground="#d6336c")
        self.after(3000, lambda: self.txt_preview.tag_remove("ch_hl", "1.0", "end"))

    def _load_to_pos_async(self, target_pos, callback):
        """异步加载文本直到 target_pos，完成后调用 callback"""
        if self._preview_pos >= target_pos or self._preview_pos >= len(self._full_text):
            callback()
            return

        self.txt_preview.config(state="normal")
        chunk = min(8000, target_pos - self._preview_pos, len(self._full_text) - self._preview_pos)
        self._load_more_preview(chunk=chunk)
        self.txt_preview.config(state="disabled")

        self.after(10, lambda: self._load_to_pos_async(target_pos, callback))

    def _delete_chapter(self, idx):
        """直接删除章节（内容合并到上一章），保存撤销信息"""
        if idx <= 0:
            return  # 第一章不能删，静默忽略
        # 合并前先懒加载涉及的章正文（轻量索引下正文初始为空）
        prev_body = self._get_body(idx - 1)
        curr_body = self._get_body(idx)
        # 保存撤销信息（保留原始元组，含索引项）
        curr = self.chapters[idx]
        prev = self.chapters[idx - 1]
        self._delete_history.append((idx, curr, prev))
        if len(self._delete_history) > 20:
            self._delete_history.pop(0)

        # 内容合并到上一章；索引项置空标记已编辑（生成时回退 build_epub）
        self.chapters[idx - 1] = (prev[0], prev_body + "\n" + curr_body, None)
        del self.chapters[idx]
        self._chapters_edited = True

        scroll_frac = self.chapter_listbox.yview()[0]
        self._refresh_chapter_listbox()
        self.chapter_listbox.yview_moveto(scroll_frac)
        if idx < len(self.chapters):
            self.chapter_listbox.selection_set(idx)
        elif len(self.chapters) > 0:
            self.chapter_listbox.selection_set(len(self.chapters) - 1)
        self._update_undo_btn()

    def _undo_delete(self):
        """撤回上一次删除，滚动到恢复位置"""
        if not self._delete_history:
            return
        idx, deleted_chapter, prev_chapter = self._delete_history.pop()
        # 恢复：合并后的章节在 idx-1
        self.chapters[idx - 1] = prev_chapter
        self.chapters.insert(idx, deleted_chapter)
        self._refresh_chapter_listbox()
        self.chapter_listbox.selection_set(idx)
        self.chapter_listbox.see(idx)  # 滚动到恢复位置
        self._update_undo_btn()

    def _update_undo_btn(self):
        """更新撤回按钮状态"""
        self._undo_btn.config(state="normal" if self._delete_history else "disabled")

    # ================================================================
    # 进度轮询
    # ================================================================
    def _poll_progress(self):
        try:
            while True:
                msg = self.progress_queue.get_nowait()
                tp = msg["type"]
                if tp == "progress":
                    self.progress_var.set(msg["value"])
                    self.progress_label.config(text=msg["text"])
                elif tp == "single_complete":
                    self._set_ui_idle()
                    self.progress_label.config(text="处理完成！")
                    messagebox.showinfo("🎉 搞定！", msg["detail"])
                elif tp == "batch_complete":
                    self._set_ui_idle()
                    self.progress_label.config(text="批量处理完成！")
                    _show_batch_result(msg["results"])
        except queue.Empty:
            pass
        self.after(100, self._poll_progress)

    def _set_ui_busy(self):
        self.config(cursor="wait")
        self.convert_btn.config(state="disabled", text="🔄 处理中...")
        self.clear_btn.config(state="disabled")
        self.batch_btn.config(state="disabled")

    def _set_ui_idle(self):
        self.config(cursor="")
        self.convert_btn.config(state="normal", text="🚀 开始转换")
        self.clear_btn.config(state="normal")
        self.batch_btn.config(state="normal")

    # ================================================================
    # 文件选择
    # ================================================================
    def browse_txt(self):
        f = filedialog.askopenfilename(filetypes=[("TXT 文件", "*.txt")])
        if f:
            self._switch_to_single(f)

    def browse_out(self):
        if self.is_batch_mode:
            d = filedialog.askdirectory(title="选择 EPUB 输出目录")
            if d:
                self.out_path.set(d)
        else:
            f = filedialog.asksaveasfilename(
                defaultextension=".epub",
                filetypes=[("ePub 电子书", "*.epub")],
            )
            if f:
                self.out_path.set(f)

    def select_batch_files(self):
        files = filedialog.askopenfilenames(
            title="选择多个 TXT 文件进行批量处理",
            filetypes=[("TXT 文件", "*.txt"), ("所有文件", "*.*")],
        )
        if files:
            self._switch_to_batch(list(files))

    # ================================================================
    # 模式切换
    # ================================================================
    def _switch_to_single(self, file_path: str):
        self.batch_files = []
        self.is_batch_mode = False
        self.file_listbox.delete(0, tk.END)

        self.txt_path.set(file_path)
        if not self.book_title.get():
            self.book_title.set(Path(file_path).stem)
        out_default = Path(file_path).with_suffix(".epub")
        self.out_path.set(str(out_default))
        self.refresh_preview()

        # 自动解析章节
        self._parse_chapters()

    def _switch_to_batch(self, files: list[str]):
        self.batch_files = files
        self.is_batch_mode = True
        # 初始化每文件的独立编码（保留原有编码设置，或继承全局 encoding）
        enc = self.encoding.get()
        self._file_encodings = {f: enc for f in files}
        self.file_listbox.delete(0, tk.END)
        for f in files:
            self.file_listbox.insert(tk.END, Path(f).name)

        first = files[0]
        self.txt_path.set(first)
        if not self.out_path.get():
            self.out_path.set(str(Path(first).parent / "epub_output"))
        self.refresh_preview()

    # ================================================================
    # 拖拽
    # ================================================================
    def on_drop(self, event):
        raw_files = self.tk.splitlist(event.data)
        txt_files = [f for f in raw_files if f.lower().endswith(".txt")]
        if not txt_files:
            messagebox.showwarning("格式错误", "请拖拽 .txt 文件！")
            return
        if len(txt_files) == 1:
            self._switch_to_single(txt_files[0])
        else:
            self._switch_to_batch(txt_files)

    # ================================================================
    # 预览
    # ================================================================
    def _on_encoding_changed(self, event=None):
        """编码切换：批量模式下只改当前文件的编码，不影响其他文件"""
        if self.is_batch_mode:
            current = self.txt_path.get()
            if current:
                self._file_encodings[current] = self.encoding.get()
        self.refresh_preview()
        if self.txt_path.get():
            self._parse_chapters()

    # ================================================================
    def refresh_preview(self, _=None):
        """刷新正文预览，先加载前 800 字，之后异步填充可视区域（不阻塞 UI）"""
        txt = Path(self.txt_path.get())
        if not txt.is_file():
            return
        enc = self.encoding.get()
        try:
            with txt.open(encoding=enc, errors="ignore") as f:
                self._full_text = f.read()
        except Exception as e:
            self._full_text = ""
        self._cached_encoding = enc  # 记录当前编码，供 _parse_chapters 校验缓存
        self.txt_preview.config(state="normal")
        self.txt_preview.delete("1.0", "end")
        self._preview_pos = 0
        self._load_more_preview(chunk=800)
        self.txt_preview.config(state="disabled")
        # 异步填充可视区域（不阻塞 UI 事件循环）
        if self._full_text:
            self.after(10, self._after_auto_load)

    def _load_more_preview(self, chunk=2000):
        """追加加载一段文本到预览框"""
        if self._preview_pos >= len(self._full_text):
            return
        end = min(self._preview_pos + chunk, len(self._full_text))
        self.txt_preview.insert("end", self._full_text[self._preview_pos:end])
        self._preview_pos = end

    def _after_auto_load(self):
        """递归异步填充可视区域（每次加载一块后让出事件循环）"""
        if self._preview_pos >= len(self._full_text):
            return
        # 可视区域已填满，暂停加载
        if self.txt_preview.yview()[1] >= 0.95:
            return

        self.txt_preview.config(state="normal")
        self._load_more_preview(chunk=4000)
        self.txt_preview.config(state="disabled")

        # 让出事件循环，10ms 后继续
        self.after(10, self._after_auto_load)

    def preview_selected_file(self, event=None):
        sel = self.file_listbox.curselection()
        if sel and self.batch_files:
            idx = sel[0]
            self.txt_path.set(self.batch_files[idx])
            # 恢复该文件的独立编码
            f = self.batch_files[idx]
            if f in self._file_encodings:
                self.encoding.set(self._file_encodings[f])
            self.refresh_preview()

    # ================================================================
    # 清空
    # ================================================================
    def clear_all(self):
        self.txt_path.set("")
        self.out_path.set("")
        self.book_title.set("")
        self.author.set("Unknown")
        self.encoding.set("utf-8")
        self.batch_files = []
        self._file_encodings.clear()
        self.is_batch_mode = False
        self.chapters = []
        self._full_text = ""
        self._preview_pos = 0
        self._cached_encoding = ""
        self.file_listbox.delete(0, tk.END)
        self.chapter_listbox.delete(0, tk.END)
        self.label_chapter_count.config(text="0")
        self.progress_var.set(0)
        self.progress_label.config(text="等待开始...")
        self.txt_preview.config(state="normal")
        self.txt_preview.delete("1.0", "end")
        self.txt_preview.insert("1.0", "请先选择 TXT 文件")
        self.txt_preview.config(state="disabled")
        self._cover_clear()
        self._delete_history.clear()
        self._update_undo_btn()
        messagebox.showinfo("已清空", "所有设置已重置")

    # ================================================================
    # 封面裁剪
    # ================================================================
    def _cover_select_file(self):
        f = filedialog.askopenfilename(
            title="选择封面图片",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.bmp *.gif *.webp")],
        )
        if f:
            self._cover_load_image(Path(f))

    def _cover_clear(self):
        self._cover_pil_image = None
        self._cover_tk_image = None
        self._cover_img_x = 0
        self._cover_img_y = 0
        self._cover_status.config(text="未选择封面图片", foreground="#888")
        self._cover_redraw()

    def _cover_on_drop(self, event):
        raw = event.data
        # tkinterdnd2 可能返回 {} 包裹的路径
        path = raw.strip("{}").strip()
        ext = Path(path).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"):
            self._cover_load_image(Path(path))

    def _cover_load_image(self, path: Path):
        if not HAVE_PIL:
            messagebox.showwarning(
                "缺少依赖",
                "请先安装 Pillow 以支持封面裁剪：\npip install Pillow",
            )
            return
        try:
            img = Image.open(path).convert("RGB")
            self._cover_pil_image = img
            self._cover_img_x = 0
            self._cover_img_y = 0
            self._cover_status.config(
                text=f"已加载: {path.name} ({img.width}×{img.height})",
                foreground="#2e7d32",
            )
            self._cover_redraw()
        except Exception as e:
            messagebox.showerror("加载失败", f"无法打开图片：{e}")

    def _cover_redraw(self, _event=None):
        self._cover_canvas.delete("all")
        cw = self._cover_canvas.winfo_width()
        ch = self._cover_canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        if self._cover_pil_image is None:
            self._cover_canvas.create_text(
                cw // 2, ch // 2, text="拖入图片\n设为首章封面",
                fill="#888", font=("微软雅黑", 14),
                justify="center", tags="hint",
            )
            return

        orig_w, orig_h = self._cover_pil_image.size
        cx, cy = cw // 2, ch // 2

        # 先算裁剪框（居中，2:3 比例）
        sel_w = int(cw * 0.55)
        sel_h = int(sel_w * 1.5)
        if sel_h > ch * 0.85:
            sel_h = int(ch * 0.85)
            sel_w = int(sel_h / 1.5)
        sel_x = cx - sel_w // 2
        sel_y = cy - sel_h // 2
        self._cover_sel_w = sel_w
        self._cover_sel_h = sel_h

        # 计算"最小覆盖缩放"——图片刚好遮满裁剪框，不留空白
        # 大图缩小、小图放大，总有一边与框对齐，另一边溢出留裁剪余地
        min_scale = max(sel_w / orig_w, sel_h / orig_h)
        disp_w = int(orig_w * min_scale)
        disp_h = int(orig_h * min_scale)
        # 限制最小显示尺寸（至少 50×50 像素，防止缩太离谱）
        if disp_w < 50 and disp_h < 50:
            factor = max(50 / disp_w, 50 / disp_h)
            disp_w = int(disp_w * factor)
            disp_h = int(disp_h * factor)
        self._cover_scale = min_scale
        self._cover_disp_w = disp_w
        self._cover_disp_h = disp_h

        # 缩放图片用于显示
        resized = self._cover_pil_image.resize((disp_w, disp_h), Image.LANCZOS)
        self._cover_tk_image = ImageTk.PhotoImage(resized)

        # 初始居中图片
        if self._cover_img_x == 0 and self._cover_img_y == 0:
            self._cover_img_x = cx - disp_w // 2
            self._cover_img_y = cy - disp_h // 2

        # 绘制图片
        self._cover_canvas.create_image(
            self._cover_img_x, self._cover_img_y,
            image=self._cover_tk_image, anchor="nw",
            tags="cover_img",
        )

        # 裁剪框外半透明遮罩
        self._cover_canvas.create_rectangle(
            0, 0, cw, sel_y,
            fill="#000", stipple="gray25",
            tags="overlay", outline="",
        )
        self._cover_canvas.create_rectangle(
            0, sel_y + sel_h, cw, ch,
            fill="#000", stipple="gray25",
            tags="overlay", outline="",
        )
        self._cover_canvas.create_rectangle(
            0, sel_y, sel_x, sel_y + sel_h,
            fill="#000", stipple="gray25",
            tags="overlay", outline="",
        )
        self._cover_canvas.create_rectangle(
            sel_x + sel_w, sel_y, cw, sel_y + sel_h,
            fill="#000", stipple="gray25",
            tags="overlay", outline="",
        )

        # 裁剪框边框（亮色）
        self._cover_canvas.create_rectangle(
            sel_x, sel_y, sel_x + sel_w, sel_y + sel_h,
            outline="#ff6b6b", width=3,
            tags="crop_rect",
        )

        # 把图片提到最上层
        self._cover_canvas.tag_raise("cover_img")

    def _cover_drag_start(self, event):
        if self._cover_pil_image is None:
            return
        self._cover_drag_start_x = event.x
        self._cover_drag_start_y = event.y
        self._cover_img_orig_x = self._cover_img_x
        self._cover_img_orig_y = self._cover_img_y
        self._cover_canvas.config(cursor="grabbing")

    def _cover_drag_motion(self, event):
        if self._cover_pil_image is None:
            return
        dx = event.x - self._cover_drag_start_x
        dy = event.y - self._cover_drag_start_y
        self._cover_img_x = self._cover_img_orig_x + dx
        self._cover_img_y = self._cover_img_orig_y + dy
        # 仅移动图片坐标，不重建 Canvas（遮罩/裁剪框位置固定，无需重绘）
        try:
            self._cover_canvas.coords("cover_img", self._cover_img_x, self._cover_img_y)
        except tk.TclError:
            self._cover_redraw()  # 容灾回退（极低概率）

    def _cover_drag_end(self, event):
        self._cover_canvas.config(cursor="hand2")

    def _cover_get_crop_bytes(self) -> Optional[bytes]:
        """从当前裁剪设置中提取封面 JPEG bytes"""
        if self._cover_pil_image is None or not HAVE_PIL:
            return None
        if self._cover_sel_w == 0 or self._cover_sel_h == 0:
            # 还没有绘制过，重新绘制一下
            self._cover_redraw()
        if self._cover_sel_w == 0 or self._cover_sel_h == 0:
            return None
        cw = self._cover_canvas.winfo_width()
        ch = self._cover_canvas.winfo_height()
        cx, cy = cw // 2, ch // 2

        sel_w = self._cover_sel_w
        sel_h = self._cover_sel_h
        sel_x = cx - sel_w // 2
        sel_y = cy - sel_h // 2

        # 原始图片尺寸
        orig_w, orig_h = self._cover_pil_image.size
        scale_x = orig_w / self._cover_disp_w
        scale_y = orig_h / self._cover_disp_h

        # 裁剪框对应原图区域
        left = int((sel_x - self._cover_img_x) * scale_x)
        top = int((sel_y - self._cover_img_y) * scale_y)
        right = int((sel_x + sel_w - self._cover_img_x) * scale_x)
        bottom = int((sel_y + sel_h - self._cover_img_y) * scale_y)

        # 限制在图片范围内
        left = max(0, left)
        top = max(0, top)
        right = min(orig_w, right)
        bottom = min(orig_h, bottom)

        if right <= left or bottom <= top:
            return None

        crop_box = (left, top, right, bottom)
        return _crop_cover_image(None, crop_box, pil_image=self._cover_pil_image)

    # ================================================================
    # 转换入口
    # ================================================================
    def run(self):
        if self.is_batch_mode:
            self._run_batch()
        else:
            self._run_single()

    def _run_single(self):
        """单文件转换入口：动态选择串行/并行路径"""
        txt = Path(self.txt_path.get())
        if not txt.is_file():
            messagebox.showerror("路径错误", "请先选择 TXT 文件！")
            return

        out = Path(self.out_path.get() or txt.with_suffix(".epub"))

        # 覆盖保护
        if out.exists():
            ok = messagebox.askyesno(
                "文件已存在",
                f"输出文件已存在:\n{out}\n\n是否覆盖？\n（选「否」将自动追加数字后缀）",
            )
            if not ok:
                from txt_to_epub_core import _unique_path
                out = _unique_path(out)

        self._set_ui_busy()
        self.progress_var.set(0)
        self.progress_label.config(text="正在生成 EPUB...")
        self.update()

        title = self.book_title.get().strip() or txt.stem
        author = self.author.get().strip() or "Unknown"
        cover_image = self._cover_get_crop_bytes()

        try:
            # 如果用户已手动解析/排序章节，优先使用（跳过并行路径）
            if self.chapters:
                ch_count = self._run_single_serial(
                    txt, out, title, author, cover_image
                )
            else:
                file_size = txt.stat().st_size
                optimal = _get_optimal_chunks(file_size)

                if optimal > 1 and _is_parallel_available():
                    try:
                        ch_count = self._run_single_parallel(
                            txt, out, title, author, cover_image, optimal
                        )
                    except Exception:
                        # 并行失败自动降级串行
                        ch_count = self._run_single_serial(
                            txt, out, title, author, cover_image
                        )
                else:
                    ch_count = self._run_single_serial(
                        txt, out, title, author, cover_image
                    )

            self._set_ui_idle()
            self.progress_var.set(100)
            self.progress_label.config(text="处理完成！")
            outs = self._last_outputs or [out]
            if len(outs) > 1:
                paths_text = "\n".join(str(p) for p in outs)
                detail = f"已拆分为 {len(outs)} 卷：\n{paths_text}\n\n章节数：{ch_count}"
            else:
                detail = f"EPUB 文件已生成：\n{outs[0]}\n\n章节数：{ch_count}"
            messagebox.showinfo("🎉 搞定！", detail)

        except Exception as e:
            self._set_ui_idle()
            self.progress_label.config(text="转换失败")
            messagebox.showerror("❌ 出错了", f"转换失败：{e}")

    def _write_volumes(self, out, title, author, cover_image, n_items, build_one):
        """按 self.max_chapters_per_volume 切块写多卷；返回输出路径列表。

        build_one(start, end, volume_title) -> EpubBook
        单卷（阈值<=0 或章节未超限）时返回 [out]，行为与原来完全一致。
        """
        ranges = volume_ranges(n_items, self.max_chapters_per_volume.get())
        if len(ranges) == 1:
            book = build_one(0, n_items, title)
            _epub.write_epub(out, book)
            return [out]
        outs = []
        for i, (s, e) in enumerate(ranges, 1):
            vtitle = f"{title} 第{i}卷"
            vout = out.with_name(f"{out.stem}_卷{i}{out.suffix}")
            book = build_one(s, e, vtitle)
            _epub.write_epub(vout, book)
            outs.append(vout)
        return outs

    def _run_single_serial(self, txt, out, title, author, cover_image):
        """串行路径：轻量索引未编辑 → Rust 按偏移打包 + 组装（内存恒定）；
        编辑过（合并/删除）→ 回退原 build_epub（按需补齐正文）。"""
        if self.chapters:
            # 未编辑且所有章节带索引项 → 轻量打包路径（生成时才物化 xhtml 小文件）
            if (not self._chapters_edited) and self._temp_path and \
                    all(ch[2] for ch in self.chapters):
                index = [ch[2] for ch in self.chapters]
                parts = tempfile.mkdtemp(prefix="epub_parts_")
                try:
                    manifest = pack_chapters(
                        self._temp_path, index, parts,
                        toc_pattern=self._get_selected_pattern(), book_title=title,
                    )
                    self._last_outputs = self._write_volumes(
                        out, title, author, cover_image, len(index),
                        lambda s, e, t: build_epub_from_pack(
                            parts, manifest, t, author, cover_image=cover_image,
                            chapters_subset=index[s:e],
                        ),
                    )
                finally:
                    shutil.rmtree(parts, ignore_errors=True)
                return len(index)
            # 编辑过 → 回退原 build_epub（按需补齐正文）
            self._ensure_all_bodies()
            chapters = [(ch[0], ch[1]) for ch in self.chapters]
            self._last_outputs = self._write_volumes(
                out, title, author, cover_image, len(chapters),
                lambda s, e, t: build_epub(chapters[s:e], t, author, cover_image=cover_image),
            )
            return len(chapters)
        else:
            pattern = self._get_selected_pattern()
            result = convert_single(
                txt, out, self.encoding.get(), title, author,
                toc_pattern=pattern,
                cover_image=cover_image,
            )
            if not result.success:
                raise RuntimeError(result.error)
            self._last_outputs = [result.output_path]
            return result.chapter_count

    def _run_single_parallel(self, txt, out, title, author, cover_image, num_chunks):
        """并行分块解析路径：文件拆段 → 多进程解析 → 合并 → build_epub"""
        enc = self.encoding.get()
        pattern = self._get_selected_pattern()
        total_lines = _count_lines(str(txt), enc)
        if total_lines == 0:
            return self._run_single_serial(txt, out, title, author, cover_image)

        # 按行均分
        chunk_size = total_lines // num_chunks
        tasks = []
        for i in range(num_chunks):
            start = i * chunk_size
            end = (i + 1) * chunk_size if i < num_chunks - 1 else total_lines
            tasks.append(
                (str(txt), start, end, enc, pattern, i, num_chunks)
            )

        workers = min(num_chunks, os.cpu_count() or 4)
        self.progress_label.config(text=f"正在并行解析（{num_chunks} 块 × {workers} 线程）...")
        self.update()

        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_parse_chunk, *task) for task in tasks
            ]
            temp_files = [f.result() for f in futures]

        all_chapters = self._merge_chunks(temp_files)

        self.progress_label.config(text="正在构建 EPUB...")
        self.update()

        self._last_outputs = self._write_volumes(
            out, title, author, cover_image, len(all_chapters),
            lambda s, e, t: build_epub(all_chapters[s:e], t, author, cover_image=cover_image),
        )
        return len(all_chapters)

    def _merge_chunks(self, temp_files):
        """合并各块解析结果：overflow 拼接到前一段末尾"""
        all_chapters = []
        for i, temp_path in enumerate(temp_files):
            try:
                with open(temp_path, "rb") as f:
                    overflow, chapters = pickle.load(f)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            if i == 0:
                all_chapters.extend(chapters)
            else:
                if overflow and all_chapters:
                    last_title, last_content = all_chapters[-1]
                    all_chapters[-1] = (last_title, last_content + overflow)
                all_chapters.extend(chapters)
        return all_chapters

    def _run_batch(self):
        if not self.batch_files:
            messagebox.showerror("错误", "请先选择要批量处理的文件！")
            return

        output_dir = Path(
            self.out_path.get() or Path(self.batch_files[0]).parent / "epub_output"
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        self._set_ui_busy()
        self.progress_var.set(0)
        self.progress_label.config(text="开始批量处理...")

        # 在主线程提取 GUI 变量（线程安全），传给工作线程
        enc = self.encoding.get()
        pattern = self._get_selected_pattern()
        user_title = self.book_title.get().strip()
        author = self.author.get()
        # 制作每文件编码字典的浅拷贝，工作线程只读副本，避免并发读写
        file_encodings_copy = dict(self._file_encodings)

        thread = threading.Thread(
            target=self._batch_worker_global,
            args=(output_dir, enc, pattern, user_title, author, file_encodings_copy),
            daemon=True,
        )
        thread.start()

    def _build_batch_tasks(self, output_dir, enc, pattern, user_title, author, file_encodings):
        """构建全局混合任务队列（跨文件），返回 (all_tasks, file_meta)

        file_encodings: 每文件独立编码的字典副本（工作线程只读）
        """
        all_tasks = []   # (path, start, end, enc, pattern, i, num, fid)
        file_meta = {}   # fid → {path, title, author, out, total_chunks, enc}

        for txt_file in self.batch_files:
            path = Path(txt_file)
            size = path.stat().st_size
            file_enc = file_encodings.get(txt_file, enc)  # 该文件的独立编码
            num = _get_optimal_chunks(size)
            total = _count_lines(str(path), file_enc)
            fid = str(uuid4())[:8]
            meta = {
                "path": path,
                "title": f"{user_title} - {path.stem}" if user_title else path.stem,
                "author": author,
                "out": output_dir / f"{path.stem}.epub",
                "total_chunks": num,
            }
            file_meta[fid] = meta

            chunk_sz = total // num if total > 0 else 0
            for i in range(num):
                start = i * chunk_sz
                end = (i + 1) * chunk_sz if i < num - 1 else total
                all_tasks.append(
                    (str(path), start, end, file_enc, pattern, i, num, fid)
                )

        return all_tasks, file_meta

    def _batch_worker_global(self, output_dir, enc, pattern, user_title, author, file_encodings):
        """批量全局动态调度：跨文件混合任务队列，单文件全块完成即合并输出"""
        all_tasks, file_meta = self._build_batch_tasks(
            output_dir, enc, pattern, user_title, author, file_encodings
        )
        total_files = len(file_meta)
        if total_files == 0:
            self.progress_queue.put({
                "type": "batch_complete",
                "results": [],
            })
            return

        # 分离小文件（optimal=1，走串行）和大文件分块任务
        serial_files = []
        parallel_tasks = []
        for fid, meta in file_meta.items():
            if meta["total_chunks"] <= 1:
                serial_files.append(fid)
            else:
                for t in all_tasks:
                    if t[7] == fid:
                        parallel_tasks.append(t)

        results = []
        file_results: dict = {}
        completed = set()
        failed = set()

        # 串行处理小文件
        for fid in serial_files:
            meta = file_meta[fid]
            self.progress_queue.put({
                "type": "progress",
                "value": len(completed) / total_files * 100,
                "text": f"串行处理: {meta['path'].name}",
            })
            result = convert_single(
                meta["path"], meta["out"],
                enc, meta["title"], meta["author"],
            )
            results.append(result)
            if result.success:
                completed.add(fid)
            else:
                failed.add(fid)

        # 并行处理大文件（全局混合调度）
        if parallel_tasks:
            max_workers = min(6, os.cpu_count() or 4)
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(_parse_chunk, *t[:7]): (t[7], t[5])
                    for t in parallel_tasks
                }
                for future in as_completed(future_map):
                    fid, idx = future_map[future]
                    if fid not in file_results:
                        file_results[fid] = {}
                    try:
                        temp_path = future.result()
                        file_results[fid][idx] = temp_path

                        # 检测文件是否全部分块完成
                        if len(file_results[fid]) == file_meta[fid]["total_chunks"]:
                            result = self._finish_file(
                                fid, file_meta[fid], file_results[fid]
                            )
                            results.append(result)
                            completed.add(fid)
                            self.progress_queue.put({
                                "type": "progress",
                                "value": len(completed) / total_files * 100,
                                "text": f"已完成 {len(completed)}/{total_files}",
                            })
                    except Exception as e:
                        if fid not in failed:
                            # 清理该文件已收集的临时文件，防止泄漏
                            for tmp_path in file_results.get(fid, {}).values():
                                try:
                                    os.remove(tmp_path)
                                except OSError:
                                    pass
                            failed.add(fid)
                            results.append(ConversionResult(
                                success=False,
                                file_path=file_meta[fid]["path"],
                                error=str(e),
                            ))
                            completed.add(fid)

        # 标记未完成的文件为失败
        for fid in file_meta:
            if fid not in completed:
                failed.add(fid)
                results.append(ConversionResult(
                    success=False,
                    file_path=file_meta[fid]["path"],
                    error="并行处理异常中断",
                ))

        self.progress_queue.put({
            "type": "batch_complete",
            "results": results,
        })

    def _finish_file(self, fid, meta, chunk_results):
        """完成单个文件：合并 → build_epub → 写入，返回 ConversionResult"""
        sorted_paths = [chunk_results[i] for i in sorted(chunk_results)]
        chapters = self._merge_chunks(sorted_paths)
        outs = self._write_volumes(
            meta["out"], meta["title"], meta["author"], None, len(chapters),
            lambda s, e, t: build_epub(chapters[s:e], t, meta["author"]),
        )
        return ConversionResult(
            success=True,
            file_path=meta["path"],
            output_path=outs[0],
            chapter_count=len(chapters),
        )


# ================================================================
# 结果展示
# ================================================================
def _show_batch_result(results: list[ConversionResult]):
    """弹窗展示批量处理结果"""
    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    if not failed:
        messagebox.showinfo(
            "🎉 批量处理完成！",
            f"全部 {len(success)} 个文件转换成功。",
        )
        return

    detail_lines = [f"成功：{len(success)}  失败：{len(failed)}\n"]
    if failed:
        detail_lines.append("\n--- 失败详情 ---")
        for r in failed:
            detail_lines.append(f"\n📄 {r.file_path.name}")
            detail_lines.append(f"  原因：{r.error}")

    detail = "".join(detail_lines)

    win = tk.Toplevel()
    win.title("批量处理结果")
    win.geometry("600x400")
    win.minsize(400, 250)

    text = tk.Text(win, wrap="word", font=("微软雅黑", 10))
    scroll = ttk.Scrollbar(win, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side="left", fill="both", expand=True, padx=10, pady=10)
    scroll.pack(side="right", fill="y", pady=10)
    text.insert("1.0", detail)
    text.config(state="disabled")

    ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 10))


# ===================================================================
# 启动
# ===================================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
