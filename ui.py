"""图形界面。表格即数据源，可导入表格或直接在表格里编辑"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import config as config_mod
import data
import output
from config import (
    DEFAULT_OUTPUT_DIR,
    FORMAT_CHOICES,
    NAMING_CHOICES,
    Config,
    load_config,
    save_config,
)
from signs import PROVINCES, diagnose

APP_NAME = "道路标志生成器"
VERSION = "1.0"
AUTHOR = "Kcx36"

UI_FONT = ("Microsoft YaHei UI", 9)
MUTED = "#6b6b6b"
ERR_COLOR = "#c0392b"
OK_COLOR = "#1e7b34"
STRIPE = "#f7f7f7"
ROW_BG = "#ffffff"
ROW_HEIGHT = 25

# 类型列的下拉选项：国家高速 → 各省高速 → 国省县乡道；也可以直接键入（如单字符「滇」）
TYPE_CHOICES = (
    ("国家高速",)
    + tuple(f"{abbr}高速" for abbr, _name, _en in PROVINCES)
    + ("国道", "省道", "县道", "乡道")
)

# (键, 表头, 宽度, 对齐, 是否随窗口伸展)
COLUMNS = (
    ("no", "序号", 44, "center", False),
    ("type", "类型", 88, "w", False),
    ("number", "编号", 80, "w", False),
    ("name", "名称", 152, "w", True),
    ("status", "状态", 224, "w", False),
)
# 可编辑的数据列：0 类型、1 编号、2 名称（表格首列是不可编辑的序号，末列是状态）
EDITABLE_COLUMNS = 3
STATUS_COLUMN = EDITABLE_COLUMNS


class Tooltip:
    """轻量悬停提示；text 可以是字符串，也可以是 (事件) -> 字符串 的函数。"""

    def __init__(self, widget: tk.Widget, text, delay: int = 600, tracking: bool = False):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip: tk.Toplevel | None = None
        self._job: str | None = None
        self._shown_for = ""
        widget.bind("<Motion>" if tracking else "<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None) -> None:
        if self._job:
            self.widget.after_cancel(self._job)
        self._job = self.widget.after(self.delay, lambda: self._show(event))

    def _hide(self, event=None) -> None:
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
        self._shown_for = ""

    def _show(self, event=None) -> None:
        self._job = None
        text = self.text(event) if callable(self.text) else self.text
        if not text or text == self._shown_for:
            return
        self._shown_for = text
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{self.widget.winfo_pointerx() + 14}+{self.widget.winfo_pointery() + 18}")
        tk.Label(
            tip, text=text, justify="left", background="#ffffe1", relief="solid",
            borderwidth=1, font=UI_FONT, padx=6, pady=4,
        ).pack()
        self.tip = tip


class App(tk.Tk):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.rows: dict[str, list[str]] = {}   # iid -> [类型, 编号, 名称]
        self.status: dict[str, str] = {}       # iid -> 状态全文（供提示显示）
        self.bad: dict[str, bool] = {}         # iid -> 是否无法生成
        self._queue: queue.Queue = queue.Queue()
        self._reply: queue.Queue = queue.Queue()
        self._running = False
        self._editor: tk.Widget | None = None
        self._edit_info: tuple[str, int] | None = None
        self._status_pool: list[tk.Label] = []   # 状态列的浮层标签（按需创建、复用）
        self._sel_bg: str | None = None
        self._seq = 0
        self._ensuring = False

        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("760x700")
        self.minsize(700, 560)
        self.option_add("*Font", UI_FONT)
        self._setup_style()
        self._build()

        self._load_cfg()
        self._log(f"{APP_NAME} {VERSION}　作者 {AUTHOR}")
        self._add_row()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(250, self._check_env_async)

    # ---------- 界面搭建 ----------

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "clam", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure(".", font=UI_FONT)
        style.configure("TButton", padding=(9, 4))
        style.configure("Primary.TButton", font=(UI_FONT[0], 10, "bold"), padding=(18, 6))
        style.configure("TLabelframe.Label", font=(UI_FONT[0], 9, "bold"))
        style.configure("Treeview", rowheight=25, font=UI_FONT)
        style.configure("Treeview.Heading", font=(UI_FONT[0], 9, "bold"), padding=(4, 4))

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_table()
        self._build_settings()
        self._build_actions()
        self._build_log()

    def _build_table(self) -> None:
        box = ttk.LabelFrame(self, text=" 标志列表 ", padding=(10, 8, 10, 10))
        box.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            box, columns=[c[0] for c in COLUMNS], show="headings",
            height=9, selectmode="extended",
        )
        for key, title, width, anchor, stretch in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=width, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background=STRIPE)
        self.tree.tag_configure("bad", foreground=ERR_COLOR)

        scroll = ttk.Scrollbar(box, orient="vertical", command=self._on_scroll)
        self.tree.configure(yscrollcommand=lambda first, last: (scroll.set(first, last), self._sync_status()))
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<F2>", self._on_edit_key)
        self.tree.bind("<Return>", self._on_edit_key)
        self.tree.bind("<Delete>", self._delete_selected)
        # 不用 FocusOut 提交：下拉列表展开时焦点会离开输入框，会误提交
        self.tree.bind("<Button-1>", lambda _e: self._commit_editor(), add="+")
        self.tree.bind("<MouseWheel>", lambda _e: self._after_scroll(), add="+")
        self.tree.bind("<Configure>", lambda _e: self._sync_status(), add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_status(), add="+")
        self.tree.bind("<KeyRelease>", lambda _e: self._sync_status(), add="+")
        Tooltip(self.tree, self._status_at, delay=500, tracking=True)

        bar = ttk.Frame(box)
        bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        for text, command in (
            ("导入表格…", self._import_table),
            ("粘贴数据…", self._paste_data),
            ("添加行", self._add_row),
            ("清空", self._clear_rows),
        ):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 6))

    def _build_settings(self) -> None:
        box = ttk.LabelFrame(self, text=" 输出设置 ", padding=(12, 8, 12, 10))
        box.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 0))
        box.columnconfigure(1, weight=1)

        self.ink_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.naming_var = tk.StringVar(value=NAMING_CHOICES["zh"])
        self.format_var = tk.StringVar(value=FORMAT_CHOICES["svg"])
        self.t2p_var = tk.BooleanVar(value=True)

        label = ttk.Frame(box)
        label.grid(row=0, column=0, sticky="w", pady=(2, 0))
        ttk.Label(label, text="Inkscape").pack(side="left")
        mark = ttk.Label(label, text="?", foreground="#0b61a4", cursor="hand2")
        mark.pack(side="left", padx=(2, 0))
        Tooltip(
            mark,
            "Inkscape 用于把文字转为曲线（转曲），并用于导出 PNG。\n"
            "程序会自动查找安装位置；找不到时可在这里手动指定 inkscape.exe。",
        )

        ttk.Entry(box, textvariable=self.ink_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 8), pady=(2, 0)
        )
        ttk.Button(box, text="浏览…", width=7, command=self._pick_inkscape).grid(
            row=0, column=2, columnspan=2, sticky="w", pady=(2, 0)
        )

        line = ttk.Frame(box)
        line.grid(row=1, column=0, columnspan=4, sticky="w", pady=(12, 0))
        ttk.Label(line, text="输出格式").pack(side="left")
        ttk.Combobox(
            line, textvariable=self.format_var, values=list(FORMAT_CHOICES.values()),
            state="readonly", width=9,
        ).pack(side="left", padx=(8, 34))
        ttk.Checkbutton(line, text="文字转曲", variable=self.t2p_var).pack(side="left")
        ttk.Label(line, text="命名格式").pack(side="left", padx=(34, 0))
        ttk.Combobox(
            line, textvariable=self.naming_var, values=list(NAMING_CHOICES.values()),
            state="readonly", width=7,
        ).pack(side="left", padx=(8, 0))

        ttk.Label(box, text="输出路径").grid(row=2, column=0, sticky="w", pady=(12, 2))
        ttk.Entry(box, textvariable=self.out_var).grid(
            row=2, column=1, sticky="ew", padx=(10, 8), pady=(12, 2)
        )
        ttk.Button(box, text="浏览…", width=7, command=self._pick_output).grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(12, 2)
        )

    def _build_actions(self) -> None:
        bar = ttk.Frame(self, padding=(10, 10, 10, 0))
        bar.grid(row=2, column=0, sticky="ew")

        self.go_button = ttk.Button(
            bar, text="生成标志", style="Primary.TButton", command=self._on_generate
        )
        self.go_button.pack(side="left")
        ttk.Button(bar, text="打开输出目录", command=self._open_output).pack(side="left", padx=(8, 0))

        self.state_label = ttk.Label(bar, text="就绪", foreground=MUTED)
        self.state_label.pack(side="right")
        self.progress = ttk.Progressbar(bar, length=140, mode="determinate")
        self.progress.pack(side="right", padx=(0, 10))

    def _build_log(self) -> None:
        wrapper = ttk.Frame(self, padding=(10, 6, 10, 10))
        wrapper.grid(row=3, column=0, sticky="ew")
        wrapper.columnconfigure(0, weight=1)

        self.log = tk.Text(
            wrapper, height=5, wrap="none", font=(UI_FONT[0], 9), background="#fcfcfc",
            relief="flat", highlightthickness=1, highlightbackground="#dcdcdc",
            padx=8, pady=5,
        )
        self.log.grid(row=0, column=0, sticky="ew")
        self.log.configure(state="disabled")

    # ---------- 配置 ----------

    def _load_cfg(self) -> None:
        self.out_var.set(self.cfg.output_dir or str(DEFAULT_OUTPUT_DIR))
        self.ink_var.set(self.cfg.inkscape_path)
        self.naming_var.set(NAMING_CHOICES.get(self.cfg.naming, NAMING_CHOICES["zh"]))
        self.format_var.set(FORMAT_CHOICES.get(self.cfg.formats, FORMAT_CHOICES["svg"]))
        self.t2p_var.set(self.cfg.text_to_path)

    def _sync_cfg(self) -> None:
        self.cfg.output_dir = self.out_var.get().strip() or str(DEFAULT_OUTPUT_DIR)
        self.cfg.naming = _reverse(NAMING_CHOICES, self.naming_var.get(), "zh")
        self.cfg.formats = _reverse(FORMAT_CHOICES, self.format_var.get(), "svg")
        self.cfg.inkscape_path = self.ink_var.get().strip()
        self.cfg.text_to_path = bool(self.t2p_var.get())

    def _check_env_async(self) -> None:
        """启动时后台检查：没配 Inkscape 就先自动探测，结果写进日志。"""
        exe = self.ink_var.get().strip()

        def work() -> None:
            found = exe
            if not found:
                config_mod.detect_inkscape.cache_clear()
                found = config_mod.detect_inkscape()
            fonts_text, _fonts_ok = config_mod.font_status_text()
            self._queue.put(("env", (fonts_text, found)))
            if not found:
                return
            # 取版本要冷启动一次 inkscape.exe（约 2 秒）
            self._queue.put(("inkver", config_mod.inkscape_version(found)))

        self._log("提示：双击单元格编辑，选中行按Delete删除。")
        threading.Thread(target=work, daemon=True).start()

    # ---------- 表格 ----------

    def _add_row(self, values: tuple[str, str, str] = ("", "", "")) -> str:
        self._commit_editor()
        self._seq += 1
        iid = f"r{self._seq}"
        self.rows[iid] = [values[0], values[1], values[2]]
        self.status[iid] = ""
        self.bad[iid] = False
        self.tree.insert("", "end", iid=iid, values=("", *self.rows[iid], ""))
        self._render_row(iid)
        self.tree.see(iid)
        return iid

    def _render_row(self, iid: str) -> None:
        values = self.rows[iid]
        if not any(values):
            ok, text = True, ""
        else:
            ok, text = diagnose(*values)
        self.status[iid] = text
        self.bad[iid] = bool(text) and not ok
        self._paint_row(iid)

    def _paint_row(self, iid: str) -> None:
        """刷新行内容与斑马纹。

        Treeview 只能整行着色，没法只让状态这一格变绿或变红，所以状态文字不写进
        单元格，而由 _sync_status 用浮层 Label 画。正在编辑的行不显示错误，
        避免边输入边报错。
        """
        index = self.tree.index(iid)
        bad = self.bad.get(iid, False) and not self._is_editing(iid)
        tags = ["odd"] if index % 2 else []
        if bad:
            tags.append("bad")
        self.tree.item(iid, values=(index + 1, *self.rows[iid], ""), tags=tuple(tags))
        self._sync_status()

    def _status_display(self, iid: str) -> tuple[str, str]:
        """状态列要显示的文字与颜色。"""
        if self._is_editing(iid):
            return "", ""
        text = self.status.get(iid, "")
        if not text:
            return "", ""
        if self.bad.get(iid):
            # 状态列窄，先显示错误类别，完整原因鼠标悬停看
            return text.split("：", 1)[0], ERR_COLOR
        return text, OK_COLOR

    def _row_background(self, iid: str) -> str:
        if iid in self.tree.selection():
            if self._sel_bg is None:
                self._sel_bg = (
                    ttk.Style(self).lookup("Treeview", "background", ["selected"]) or "#0078d7"
                )
            return self._sel_bg
        return STRIPE if self.tree.index(iid) % 2 else ROW_BG

    def _visible_rows(self) -> list[str]:
        """估算当前可见的行（多取两行，避免首尾缺标签）。"""
        kids = self.tree.get_children()
        total = len(kids)
        if not total:
            return []
        top = self.tree.yview()[0]
        span = max(1, self.tree.winfo_height() // ROW_HEIGHT + 1)
        start = max(0, min(total - 1, int(top * total) - 1))
        return kids[start : start + span + 2]

    def _sync_status(self) -> None:
        """把可见行的状态文字绘制到状态列上。"""
        selected = set(self.tree.selection())
        used = 0
        for iid in self._visible_rows():
            box = self.tree.bbox(iid, "status")
            if not box:
                continue
            x, y, width, height = box
            text, color = self._status_display(iid)
            if used >= len(self._status_pool):
                self._status_pool.append(self._new_status_label())
            label = self._status_pool[used]
            used += 1
            if not text:
                label.place_forget()
                continue
            label.iid = iid
            if iid in selected:
                # 选中行整行高亮，状态文字跟着用白字，避免绿字压在蓝底上
                label.configure(text=text, foreground=ROW_BG, background=self._row_background(iid))
            else:
                label.configure(text=text, foreground=color, background=self._row_background(iid))
            label.place(x=x + 6, y=y, width=max(width - 10, 20), height=height)
        for label in self._status_pool[used:]:
            label.place_forget()

    def _new_status_label(self) -> tk.Label:
        label = tk.Label(self.tree, anchor="w", font=UI_FONT, bd=0, highlightthickness=0)
        label.bind("<Button-1>", lambda _e, l=label: self._select_row(getattr(l, "iid", "")))
        Tooltip(label, lambda _e=None, l=label: self.status.get(getattr(l, "iid", ""), ""), delay=600)
        return label

    def _select_row(self, iid: str) -> None:
        self._commit_editor()
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._sync_status()

    def _on_scroll(self, *args) -> None:
        self.tree.yview(*args)
        self._sync_status()

    def _after_scroll(self, _event=None) -> None:
        self._commit_editor()
        self.after_idle(self._sync_status)

    def _renumber(self) -> None:
        for iid in self.tree.get_children():
            self._paint_row(iid)

    def _cell_at(self, event) -> tuple[str | None, int]:
        """返回 (行 iid, 数据列号)。

        表格显示列是「序号 / 类型 / 编号 / 名称 / 状态」，
        数据列只有类型/编号/名称三列，所以要把显示列号减 2：
        序号 -> -1，类型 -> 0，编号 -> 1，名称 -> 2，状态 -> 3。
        """
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None, -1
        iid = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not iid or not column:
            return None, -1
        return iid, int(column[1:]) - 2

    def _is_editing(self, iid: str) -> bool:
        return self._edit_info is not None and self._edit_info[0] == iid

    def _status_at(self, event) -> str:
        """悬停在状态列时显示完整原因（列宽固定，长文本会被裁掉）。"""
        iid, column = self._cell_at(event)
        if iid and column == STATUS_COLUMN and self.bad.get(iid) and not self._is_editing(iid):
            return self.status.get(iid, "")
        return ""

    def _on_double_click(self, event) -> None:
        iid, column = self._cell_at(event)
        if iid and 0 <= column < EDITABLE_COLUMNS:
            self._edit(iid, column)

    def _on_edit_key(self, event=None) -> None:
        iid = self.tree.focus()
        if iid:
            self._edit(iid, 0)
        return "break"

    def _tree_column(self, column: int) -> str:
        """数据列号 -> 表格里的列标识（表格最前面是「序号」列，所以要 +1）。"""
        return COLUMNS[column + 1][0]

    def _edit(self, iid: str, column: int) -> None:
        self._commit_editor()
        if column < 0 or column >= EDITABLE_COLUMNS:
            return
        self.tree.see(iid)
        self.update_idletasks()
        bbox = self.tree.bbox(iid, self._tree_column(column))
        if not bbox:
            return

        x, y, width, height = bbox
        self._edit_info = (iid, column)
        value = self.rows[iid][column]

        if column == 0:
            editor: tk.Widget = ttk.Combobox(
                self.tree, values=TYPE_CHOICES, state="normal", font=UI_FONT
            )
            editor.set(value)
        else:
            editor = ttk.Entry(self.tree, font=UI_FONT)
            editor.insert(0, value)
            editor.select_range(0, "end")

        editor.place(x=x, y=y, width=max(width, 84), height=height)
        editor.bind("<Return>", lambda _e: self._commit_editor("down"))
        editor.bind("<KP_Enter>", lambda _e: self._commit_editor("down"))
        editor.bind("<Tab>", lambda _e: self._commit_editor("right"))
        editor.bind("<Escape>", lambda _e: self._cancel_editor())
        editor.focus_set()
        self._editor = editor
        self._paint_row(iid)  # 编辑期间不显示错误提示

    def _commit_editor(self, move: str | None = None) -> None:
        editor = self._editor
        if editor is None or self._edit_info is None:
            return
        self._editor = None
        iid, column = self._edit_info
        self._edit_info = None
        value = editor.get().strip()
        editor.destroy()

        if iid in self.rows:
            self.rows[iid][column] = value
            self._render_row(iid)
            self._ensure_trailing_blank()
        if move:
            self.after(1, lambda: self._move_edit(iid, column, move))

    def _cancel_editor(self) -> None:
        editor, self._editor = self._editor, None
        info = self._edit_info
        self._edit_info = None
        if editor is not None:
            editor.destroy()
        if info and info[0] in self.rows:
            self._paint_row(info[0])

    def _move_edit(self, iid: str, column: int, direction: str) -> None:
        if direction == "right":
            if column + 1 < EDITABLE_COLUMNS:
                self._edit(iid, column + 1)
                return
            column = 0
        target = self.tree.next(iid)
        if target:
            self._edit(target, column)

    def _ensure_trailing_blank(self) -> None:
        """最后一行不是空行时，自动补一行空行，方便接着录入。"""
        if self._ensuring:
            return
        kids = self.tree.get_children()
        if kids and not any(self.rows.get(kids[-1], ())):
            return
        self._ensuring = True
        try:
            self._add_row()
        finally:
            self._ensuring = False

    def _delete_selected(self, event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        for iid in selected:
            self.rows.pop(iid, None)
            self.status.pop(iid, None)
            self.bad.pop(iid, None)
            self.tree.delete(iid)
        self._ensure_trailing_blank()
        self._renumber()

    def _collect_rows(self) -> list[tuple[str, str, str]]:
        return [
            tuple(self.rows[iid])  # type: ignore[misc]
            for iid in self.tree.get_children()
            if any(self.rows.get(iid, ()))
        ]

    def _set_rows(self, values: list[tuple[str, str, str]]) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.rows.clear()
        self.status.clear()
        self.bad.clear()
        for item in values:
            self._add_row(tuple(item))
        self._ensure_trailing_blank()
        self.tree.yview_moveto(0.0)

    def _clear_rows(self) -> None:
        if self._collect_rows() and not messagebox.askyesno("清空", "确定清空表格内容？"):
            return
        self._set_rows([])

    # ---------- 文件 ----------

    def _pick_output(self) -> None:
        initial = self.out_var.get().strip() or str(DEFAULT_OUTPUT_DIR)
        chosen = filedialog.askdirectory(
            title="选择输出目录", initialdir=initial if os.path.isdir(initial) else None
        )
        if chosen:
            self.out_var.set(os.path.normpath(chosen))

    def _pick_inkscape(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择 Inkscape 可执行文件",
            filetypes=[("Inkscape", "inkscape.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if chosen:
            self.ink_var.set(os.path.normpath(chosen))
            self._log(f"Inkscape：{self.ink_var.get()}")

    def _import_table(self) -> None:
        path = filedialog.askopenfilename(
            title="选择表格文件",
            filetypes=[
                ("表格文件", "*.xlsx *.xls *.csv"),
                ("Excel", "*.xlsx *.xls"),
                ("CSV", "*.csv"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        try:
            rows = data.read_table(path)
        except data.DataSourceError as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if not rows:
            messagebox.showinfo("提示", "文件里没有读到数据。")
            return
        if self._collect_rows() and not messagebox.askyesno(
            "导入", f"将用 {len(rows)} 行数据替换当前内容，继续？"
        ):
            return
        self._set_rows(rows)
        self._log(f"已导入 {len(rows)} 行：{Path(path).name}")

    def _paste_data(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("粘贴数据")
        dialog.transient(self)
        dialog.geometry("560x400")

        ttk.Label(
            dialog,
            text="每行一条记录，列之间用 Tab 或逗号分隔；也可以只写编号（如 G105）。",
            padding=(12, 10, 12, 6),
        ).pack(anchor="w")

        text = tk.Text(dialog, wrap="none", font=UI_FONT, height=14)
        text.pack(fill="both", expand=True, padx=12)

        bar = ttk.Frame(dialog, padding=(12, 8, 12, 10))
        bar.pack(fill="x")

        def confirm() -> None:
            rows = data.parse_pasted(text.get("1.0", "end"))
            dialog.destroy()
            if not rows:
                messagebox.showinfo("提示", "没有解析到数据。")
                return
            self._set_rows(self._collect_rows() + rows)
            self._log(f"已追加 {len(rows)} 行")
            self.tree.yview_moveto(1.0)

        ttk.Button(bar, text="添加到表格", command=confirm).pack(side="right")
        ttk.Button(bar, text="取消", command=dialog.destroy).pack(side="right", padx=6)
        dialog.grab_set()
        text.focus_set()

    def _open_output(self) -> None:
        path = Path(self.out_var.get().strip() or self.cfg.output_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("打不开", str(exc))
            return
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            webbrowser.open(path.as_uri())

    # ---------- 生成 ----------

    def _on_generate(self) -> None:
        if self._running:
            return
        self._commit_editor()
        rows = self._collect_rows()
        if not rows:
            messagebox.showinfo("提示", "表格里还没有数据。可导入表格、粘贴数据，或直接填写。")
            return

        self._sync_cfg()
        save_config(self.cfg)

        self._running = True
        self.go_button.configure(state="disabled")
        self.progress.configure(maximum=len(rows), value=0)
        self.state_label.configure(text="检查环境…")
        self._log(f"开始生成 {len(rows)} 条 → {self.cfg.output_dir}")
        threading.Thread(target=self._worker, args=(rows, self.cfg), daemon=True).start()

    def _worker(self, rows, cfg) -> None:
        try:
            reason = output.preflight(cfg)
            if reason:
                self._queue.put(("blocked", reason))
                return
            summary = output.run(
                rows, cfg,
                progress=lambda i, t, name, ok, message: self._queue.put(
                    ("progress", (i, t, name, ok, message))
                ),
                on_conflict=self._conflict_hook,
            )
        except Exception as exc:  # 后台异常也要回到主线程提示
            self._queue.put(("error", exc))
            return
        self._queue.put(("done", summary))

    def _conflict_hook(self, basename: str, existing: Path) -> str:
        """工作线程里问同名文件：交给主线程弹窗，等用户的选择。"""
        self._queue.put(("conflict", (basename, existing)))
        return self._reply.get()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    self._on_progress(payload)
                elif kind == "conflict":
                    self._reply.put(self._ask_conflict(*payload))
                elif kind == "blocked":
                    self._on_blocked(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    self._on_error(payload)
                elif kind == "env":
                    self._on_env(payload)
                elif kind == "inkver":
                    self._log(f"Inkscape版本：{payload or '未知'}")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _on_env(self, payload) -> None:
        fonts_text, exe = payload
        self._log(fonts_text)
        if not exe:
            self._log("未找到 Inkscape：文字转曲与 PNG 导出不可用，请在上方指定路径")
            return
        if not self.ink_var.get().strip():
            self.ink_var.set(exe)

    def _on_progress(self, payload) -> None:
        index, total, name, ok, message = payload
        self.progress.configure(value=index)
        self.state_label.configure(text=f"生成中 {index}/{total}")
        if ok:
            self._log(f"  ✔ {name}")
        elif message.startswith("已存在同名文件"):
            self._log(f"  – {name}（已存在，跳过）")
        else:
            self._log(f"  ✘ 第 {index} 条：{message}")

    def _on_done(self, summary) -> None:
        self._running = False
        self.go_button.configure(state="normal")
        self.state_label.configure(text="完成")
        text = (
            f"成功 {summary.ok_count}，跳过 {summary.skipped_count}，"
            f"失败 {summary.failed_count}，用时 {summary.elapsed:.1f} 秒。"
        )
        self._log(f"完成：{text}")
        if summary.failed:
            detail = "\n".join(f"第 {item.row_no} 条：{item.error}" for item in summary.failed[:8])
            if len(summary.failed) > 8:
                detail += f"\n…… 其余 {len(summary.failed) - 8} 条见日志"
            messagebox.showwarning("部分未生成", f"{text}\n\n{detail}")
        else:
            messagebox.showinfo("完成", f"{text}\n\n输出目录：{summary.output_dir}")

    def _on_error(self, exc: Exception) -> None:
        self._running = False
        self.go_button.configure(state="normal")
        self.state_label.configure(text="出错")
        self._log(f"出错：{exc}")
        messagebox.showerror("出错", str(exc))

    def _on_blocked(self, reason: str) -> None:
        self._running = False
        self.go_button.configure(state="normal")
        self.state_label.configure(text="已中止")
        self._log(f"未开始：{reason.splitlines()[0]}")
        messagebox.showerror("无法生成", reason)

    def _ask_conflict(self, basename: str, existing: Path) -> str:
        dialog = tk.Toplevel(self)
        dialog.title("文件已存在")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)

        ttk.Label(
            dialog, text=f"「{existing.name}」已存在，是否覆盖？", padding=(16, 16, 16, 4)
        ).pack(anchor="w")
        ttk.Label(
            dialog, text="选择「全部」后本次转换不再询问。", padding=(16, 0, 16, 12), foreground=MUTED
        ).pack(anchor="w")

        result = {"value": output.SKIP}
        bar = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        bar.pack(fill="x")

        def choose(value: str) -> None:
            result["value"] = value
            dialog.destroy()

        ttk.Button(bar, text="覆盖", command=lambda: choose(output.OVERWRITE)).pack(side="left")
        ttk.Button(bar, text="跳过", command=lambda: choose(output.SKIP)).pack(side="left", padx=6)
        ttk.Button(bar, text="全部覆盖", command=lambda: choose(output.OVERWRITE_ALL)).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(bar, text="全部跳过", command=lambda: choose(output.SKIP_ALL)).pack(
            side="left", padx=6
        )
        dialog.bind("<Escape>", lambda _e: choose(output.SKIP))

        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dialog.grab_set()
        dialog.wait_window()
        return result["value"]

    # ---------- 其他 ----------

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self._running and not messagebox.askyesno("退出", "正在生成，确定退出？"):
            return
        try:
            self._sync_cfg()
            save_config(self.cfg)
        except OSError:
            pass
        self.destroy()


def _reverse(mapping: dict[str, str], label: str, default: str) -> str:
    for key, text in mapping.items():
        if text == label:
            return key
    return default


def main() -> int:
    App(load_config()).mainloop()
    return 0
