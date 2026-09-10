"""MMY RenderCleaner —— 渲染序列帧透明区抖动清洗工具。

背景：Blender 色彩管理 Dither（默认 1.0）会在 PNG 的 alpha=0 透明区
写入 1~5 的随机 RGB 噪声，肉眼不可见但破坏 PNG 压缩（体积涨约 6 倍）。
本工具批量把 alpha==0 像素的 RGB 置 0（角色本体像素不动，画面无损）。

用法：
  GUI 模式：  python render_cleaner.py
  命令行模式：python render_cleaner.py --cli <根目录> [--no-backup] [--dry-run]
"""

import json
import math
import os
import random
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image

# 配置文件与程序本体同目录；打包成 onefile exe 后 __file__ 在临时解压目录，
# 必须改用 exe 自身位置，否则 config.json 不持久
if getattr(sys, 'frozen', False):
    CONFIG_PATH = Path(sys.executable).with_name('config.json')
else:
    CONFIG_PATH = Path(__file__).with_name('config.json')
SAMPLES_PER_UNIT = 5          # 扫描阶段每个单元抽样的帧数
IMAGE_EXTS = {'.png'}         # 只处理 PNG（JPG/BMP 无 alpha 无需处理）


# ---------------------------------------------------------------- 清洗核心

def analyze_png(path):
    """返回 (透明像素数, 脏像素数)。脏像素 = alpha==0 且 RGB 任一非零。"""
    img = Image.open(path)
    arr = np.asarray(img.convert('RGBA')).astype(np.int16)
    alpha = arr[..., 3]
    transparent = alpha == 0
    rgb_nonzero = arr[..., :3].max(axis=2) > 0
    dirty = transparent & rgb_nonzero
    return int(transparent.sum()), int(dirty.sum())


def clean_png(path):
    """把 alpha==0 像素的 RGB 置 0 并保存。

    Returns:
        (是否修改, 原始字节数, 新字节数)；无需修改时新旧字节数相同。
    """
    old_size = os.path.getsize(path)
    img = Image.open(path)
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    arr = np.asarray(img).astype(np.int16)
    alpha = arr[..., 3]
    transparent = alpha == 0
    rgb_nonzero = arr[..., :3].max(axis=2) > 0
    if not bool((transparent & rgb_nonzero).any()):
        return False, old_size, old_size
    arr[transparent, 0:3] = 0
    out = Image.fromarray(arr.astype(np.uint8), 'RGBA')
    out.save(path, 'PNG')
    return True, old_size, os.path.getsize(path)


# ---------------------------------------------------------------- 目录发现

def find_units(root):
    """确定清洗单元：优先取一级子目录中「递归含 PNG」的目录。

    选 hero 根目录 → 每个角色目录是一个单元；
    选某角色目录 → Render_Output（或变体目录）是单元；
    选的目录自身直接含帧且无子目录含帧 → 整目录作为单单元。
    备份目录（_dither_backup*）永远不作为单元。
    """
    root = Path(root)
    children = [child for child in sorted(root.iterdir())
                if child.is_dir() and not _is_backup_dir(child) and _has_png(child)]
    if children:
        return children
    if _has_png(root):
        return [root]
    return []


def _is_backup_dir(path):
    return path.name.startswith('_dither_backup')


def _has_png(dir_path):
    """递归判断目录内是否有 PNG，跳过备份目录。"""
    for cur, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if not d.startswith('_dither_backup')]
        if any(f.lower().endswith('.png') for f in files):
            return True
    return False


def iter_pngs(unit):
    """收集单元内全部 PNG，跳过备份目录。"""
    out = []
    for cur, dirs, files in os.walk(unit):
        dirs[:] = [d for d in dirs if not d.startswith('_dither_backup')]
        for f in files:
            if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                out.append(Path(cur) / f)
    return out


def sample_dirty_ratio(unit, samples=SAMPLES_PER_UNIT):
    """抽样估计单元内脏帧比例与平均脏像素占比。"""
    pngs = iter_pngs(unit)
    if not pngs:
        return 0.0, 0
    picked = random.sample(pngs, min(samples, len(pngs)))
    dirty_frames = 0
    dirty_px = 0
    trans_px = 0
    for p in picked:
        try:
            trans, dirty = analyze_png(p)
        except OSError:
            continue
        trans_px += trans
        dirty_px += dirty
        if dirty > 0:
            dirty_frames += 1
    ratio = dirty_px / trans_px if trans_px else 0.0
    return ratio, dirty_frames


def fmt_size(num):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if num < 1024 or unit == 'TB':
            return f'{num:.1f}{unit}' if unit != 'B' else f'{int(num)}B'
        num /= 1024
    return f'{num:.1f}TB'


# ---------------------------------------------------------------- 批处理逻辑

class CleanJob:
    """一次清洗任务：扫描 → 备份 → 清洗 → 复检。纯逻辑，供 GUI 与 CLI 复用。"""

    def __init__(self, root, backup=True, dry_run=False, progress=None, log=None):
        self.root = Path(root)
        self.backup = backup
        self.dry_run = dry_run
        self.progress = progress or (lambda cur, total, msg: None)
        self.log = log or (lambda msg: None)
        self.cancelled = False

    def _backup_unit(self, unit):
        """备份到单元同级目录（不放清洗树内部，避免被后续扫描/上传误收）。"""
        unit = Path(unit)
        ts = time.strftime('%Y%m%d_%H%M%S')
        backup_root = unit.parent / f'_dither_backup_{ts}'
        target = backup_root / unit.name
        if target.exists():
            return None
        self.log(f'  备份 → {target}')
        shutil.copytree(unit, target)
        return target

    def scan(self):
        """返回 [(unit, png_count, size_bytes, dirty_ratio, dirty_frames)]"""
        units = find_units(self.root)
        results = []
        total = len(units)
        for i, unit in enumerate(units):
            if self.cancelled:
                break
            self.progress(i, total, f'扫描 {Path(unit).name}')
            pngs = iter_pngs(unit)
            size = sum(p.stat().st_size for p in pngs)
            ratio, dirty_frames = sample_dirty_ratio(unit)
            results.append((unit, len(pngs), size, ratio, dirty_frames))
        return results

    def run(self, units):
        """清洗给定单元。返回统计 dict。"""
        stats = {'files': 0, 'changed': 0, 'old_bytes': 0, 'new_bytes': 0,
                 'errors': 0, 'backups': []}
        unit_paths = [Path(u) for u in units]
        all_pngs = []
        for unit in unit_paths:
            pngs = iter_pngs(unit)
            if self.backup and not self.dry_run:
                target = self._backup_unit(unit)
                if target:
                    stats['backups'].append(str(target))
                elif self.backup:
                    self.log(f'  跳过备份（已存在同名备份）: {Path(unit).name}')
            all_pngs.extend((unit, p) for p in pngs)
        total = len(all_pngs)
        self.log(f'共 {total} 张 PNG 待处理')
        for i, (unit, p) in enumerate(all_pngs):
            if self.cancelled:
                self.log('已取消')
                break
            if i % 20 == 0 or i == total - 1:
                saved = stats['old_bytes'] - stats['new_bytes']
                self.progress(i, total,
                              f'[{Path(unit).name}] {i + 1}/{total} 已省 {fmt_size(max(0, saved))}')
            try:
                if self.dry_run:
                    trans, dirty = analyze_png(p)
                    changed = dirty > 0
                    old = new = os.path.getsize(p)
                else:
                    changed, old, new = clean_png(p)
                stats['files'] += 1
                if changed:
                    stats['changed'] += 1
                stats['old_bytes'] += old
                stats['new_bytes'] += new
            except OSError as exc:
                stats['errors'] += 1
                self.log(f'  [错误] {p}: {exc}')
        saved = max(0, stats['old_bytes'] - stats['new_bytes'])
        self.progress(total, total, f'完成：{stats["changed"]}/{stats["files"]} 张修改，省 {fmt_size(saved)}')
        return stats

    def verify(self, units, samples=SAMPLES_PER_UNIT):
        """复检：抽样确认透明区已无脏像素。返回 [(unit, clean_bool, ratio)]"""
        out = []
        for unit in units:
            ratio, _ = sample_dirty_ratio(unit)
            out.append((Path(unit), ratio <= 0.0, ratio))
        return out


# ---------------------------------------------------------------- GUI

class App:
    def __init__(self, root_tk):
        self.tk = root_tk
        self.tk.title('MMY RenderCleaner —— 透明区抖动清洗')
        self.tk.geometry('980x640')
        self.config = self._load_config()
        self.job = None
        self.worker = None
        self.queue = []
        self.scan_results = []
        self._build_ui()
        self._enable_drag_drop()
        self.tk.after(100, self._poll_queue)

    # ---- 配置
    def _load_config(self):
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---- UI
    def _build_ui(self):
        pad = {'padx': 6, 'pady': 4}
        top = ttk.Frame(self.tk)
        top.pack(fill='x', **pad)
        ttk.Label(top, text='根目录：').pack(side='left')
        self.path_var = tk.StringVar(value=self.config.get('last_root', ''))
        ttk.Entry(top, textvariable=self.path_var).pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(top, text='浏览…', command=self._pick_dir).pack(side='left', padx=2)
        ttk.Button(top, text='扫描', command=self.start_scan).pack(side='left', padx=2)

        opts = ttk.Frame(self.tk)
        opts.pack(fill='x', **pad)
        self.backup_var = tk.BooleanVar(value=self.config.get('backup', True))
        ttk.Checkbutton(opts, text='清洗前备份（复制到根目录 _dither_backup_时间戳/）',
                        variable=self.backup_var).pack(side='left')
        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text='仅检测不修改（dry-run）', variable=self.dry_var).pack(side='left', padx=12)

        mid = ttk.Frame(self.tk)
        mid.pack(fill='both', expand=True, **pad)
        cols = ('unit', 'frames', 'size', 'ratio', 'dirty_frames', 'est_save')
        self.tree = ttk.Treeview(mid, columns=cols, show='headings', selectmode='extended')
        for col, text, w in [
            ('unit', '清洗单元（目录）', 380), ('frames', 'PNG数', 70),
            ('size', '体积', 90), ('ratio', '抽样脏像素占比', 110),
            ('dirty_frames', '抽样脏帧', 80), ('est_save', '预计可省', 90),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor='w')
        self.tree.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(mid, command=self.tree.yview)
        sb.pack(side='left', fill='y')
        self.tree.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(self.tk)
        btns.pack(fill='x', **pad)
        ttk.Button(btns, text='全选', command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side='left')
        ttk.Button(btns, text='反选', command=self._invert_selection).pack(side='left', padx=4)
        self.clean_btn = ttk.Button(btns, text='开始清洗（选中项）', command=self.start_clean)
        self.clean_btn.pack(side='left', padx=12)
        ttk.Button(btns, text='复检选中项', command=self.start_verify).pack(side='left')

        self.progress = ttk.Progressbar(self.tk, mode='determinate')
        self.progress.pack(fill='x', **pad)
        self.status_var = tk.StringVar(value='就绪。选择根目录后点「扫描」。')
        ttk.Label(self.tk, textvariable=self.status_var).pack(fill='x', padx=6)
        self.log_text = tk.Text(self.tk, height=10, state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=6, pady=4)

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if d:
            self.path_var.set(os.path.normpath(d))

    # ---- 拖拽支持（Windows 原生 WM_DROPFILES，零第三方依赖）
    def _enable_drag_drop(self):
        """把整个窗口注册为文件拖放目标。仅 Windows 生效，其他平台静默跳过。"""
        if sys.platform != 'win32' or getattr(self, '_drag_hooked', False):
            return
        try:
            import ctypes
            from ctypes import wintypes
            self._drop_refs = (ctypes, wintypes)  # 持引用防回调被 GC
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32

            shell32.DragQueryFileW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, wintypes.LPWSTR, ctypes.c_uint]
            shell32.DragQueryFileW.restype = ctypes.c_uint
            shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
            # HDROP 是 64 位句柄，不设原型会被 ctypes 默认按 c_int 转换 → OverflowError
            shell32.DragFinish.argtypes = [ctypes.c_void_p]
            shell32.DragFinish.restype = None

            # tkinter 的 winfo_id 是子窗口，真正的顶层 HWND 要取父级；
            # 窗口尚未显示时可能拿不到（0），延迟重试
            hwnd = user32.GetParent(self.tk.winfo_id())
            if not hwnd:
                self.tk.after(300, self._enable_drag_drop)
                return

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                wintypes.WPARAM, wintypes.LPARAM)
            SetWindowLongPtr = getattr(user32, 'SetWindowLongPtrW', None) \
                or user32.SetWindowLongW
            # 显式声明 64 位安全原型：默认 c_int 会截断指针（返回值与入参都是）
            SetWindowLongPtr.argtypes = [
                wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            SetWindowLongPtr.restype = ctypes.c_ssize_t
            CallWindowProc = user32.CallWindowProcW
            CallWindowProc.argtypes = [
                ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                wintypes.WPARAM, wintypes.LPARAM]
            CallWindowProc.restype = ctypes.c_ssize_t

            def _wndproc(h, msg, wp, lp):
                if msg == 0x0233:  # WM_DROPFILES
                    # 回调内绝不允许：①异常外泄 ②调用任何 tkinter API——
                    # after() 会重入 _tkinter 的 Tcl 调用（ENTER_TCL/LEAVE_TCL），
                    # 打乱主循环 GIL 线程状态 → Fatal Python error 崩溃。
                    # 因此这里只做纯 ctypes 查询，再用独立线程把结果 marshal 回主线程。
                    paths = []
                    try:
                        count = shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                        buf = ctypes.create_unicode_buffer(1024)
                        for i in range(count):
                            shell32.DragQueryFileW(wp, i, buf, 1024)
                            paths.append(buf.value)
                    except Exception:
                        pass
                    try:
                        shell32.DragFinish(wp)
                    except Exception:
                        pass
                    if paths:
                        threading.Thread(
                            target=self._marshal_drop, args=(paths,),
                            daemon=True).start()
                    return 0
                return CallWindowProc(self._old_wndproc, h, msg, wp, lp)

            self._wndproc_ref = WNDPROC(_wndproc)  # 保持回调引用
            proc_addr = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
            self._old_wndproc = SetWindowLongPtr(hwnd, -4, proc_addr)  # GWL_WNDPROC
            self._drag_hooked = True
            shell32.DragAcceptFiles(hwnd, True)
        except Exception as exc:  # noqa: BLE001
            self.log_text.configure(state='normal')
            self.log_text.insert('end', f'[提示] 拖拽功能初始化失败（不影响其他功能）: {exc}\n')
            self.log_text.configure(state='disabled')

    def _marshal_drop(self, paths):
        """在独立线程里把拖入结果转交主线程（跨线程 after 走 _tkinter 安全排队）。"""
        try:
            self.tk.after(0, lambda p=paths: self._on_drop_paths(p))
        except RuntimeError:
            pass  # 应用退出中

    def _on_drop_paths(self, paths):
        """拖放入口：取第一个路径。目录直接用；文件用其所在目录。"""
        if not paths:
            return
        p = os.path.normpath(paths[0])
        if os.path.isdir(p):
            self.path_var.set(p)
            self._log(f'拖入目录: {p}')
            self.start_scan()
        elif os.path.isfile(p):
            d = os.path.dirname(p)
            self.path_var.set(d)
            self._log(f'拖入文件，使用其所在目录: {d}')
            self.start_scan()

    def _invert_selection(self):
        sel = set(self.tree.selection())
        all_items = set(self.tree.get_children())
        self.tree.selection_set(all_items - sel)

    def _log(self, msg):
        self.queue.append(('log', msg))

    def _set_status(self, msg):
        self.queue.append(('status', msg))

    def _poll_queue(self):
        while self.queue:
            kind, payload = self.queue.pop(0)
            try:
                self._handle(kind, payload)
            except Exception as exc:  # noqa: BLE001
                self.log_text.configure(state='normal')
                self.log_text.insert('end', f'[UI错误] {kind}: {exc}\n')
                self.log_text.see('end')
                self.log_text.configure(state='disabled')
        self.tk.after(100, self._poll_queue)

    def _busy(self, busy):
        state = 'disabled' if busy else 'normal'
        self.clean_btn.configure(state=state)

    # ---- 动作
    def start_scan(self):
        root_dir = self.path_var.get().strip()
        if not os.path.isdir(root_dir):
            messagebox.showerror('错误', '请选择有效目录')
            return
        self.config['last_root'] = root_dir
        self.config['backup'] = bool(self.backup_var.get())
        self._save_config()
        self._busy(True)
        self.tree.delete(*self.tree.get_children())
        self.scan_results = []
        job = CleanJob(root_dir, progress=lambda c, t, m: self._progress(c, t, m),
                       log=self._log)
        self.job = job

        def work():
            try:
                results = job.scan()
                self.queue.append(('scan_done', results))
            except Exception as exc:  # noqa: BLE001
                self._log(f'扫描失败: {exc}')
                self.queue.append(('done', None))

        threading.Thread(target=work, daemon=True).start()

    def _progress(self, cur, total, msg):
        def apply():
            if total:
                self.progress.configure(maximum=total, value=cur)
            self.status_var.set(msg)
        self.queue.append(('ui', apply))

    def _selected_units(self):
        return [self.scan_results[int(i)] for i in self.tree.selection()]

    def start_clean(self):
        chosen = self._selected_units()
        if not chosen:
            messagebox.showinfo('提示', '请先扫描并勾选要清洗的目录')
            return
        dirty_units = [r for r in chosen if r[3] > 0 or r[4] > 0]
        if not dirty_units and not messagebox.askyesno(
                '确认', '选中项抽样均未发现噪声，仍要全量清洗吗？'):
            return
        job = CleanJob(self.path_var.get().strip(), backup=bool(self.backup_var.get()),
                       dry_run=bool(self.dry_var.get()),
                       progress=lambda c, t, m: self._progress(c, t, m), log=self._log)
        self.job = job
        self._busy(True)

        def work():
            try:
                stats = job.run([r[0] for r in chosen])
                self.queue.append(('clean_done', (job, stats)))
            except Exception as exc:  # noqa: BLE001
                self._log(f'清洗失败: {exc}')
                self.queue.append(('done', None))

        threading.Thread(target=work, daemon=True).start()

    def start_verify(self):
        chosen = self._selected_units()
        if not chosen:
            messagebox.showinfo('提示', '请先扫描并勾选目录')
            return
        job = CleanJob(self.path_var.get().strip(), dry_run=True,
                       progress=lambda c, t, m: self._progress(c, t, m), log=self._log)
        self.job = job
        self._busy(True)

        def work():
            try:
                results = job.verify([r[0] for r in chosen])
                self.queue.append(('verify_done', results))
            except Exception as exc:  # noqa: BLE001
                self._log(f'复检失败: {exc}')
                self.queue.append(('done', None))

        threading.Thread(target=work, daemon=True).start()

    # ---- 后台结果回填
    def _finish_job(self):
        self._busy(False)
        self.progress.configure(value=0)

    def _handle(self, kind, payload):
        if kind == 'log':
            self.log_text.configure(state='normal')
            self.log_text.insert('end', payload + '\n')
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        elif kind == 'status':
            self.status_var.set(payload)
        elif kind == 'scan_done':
            self.scan_results = payload
            self.tree.delete(*self.tree.get_children())
            for i, (unit, frames, size, ratio, dirty_frames) in enumerate(payload):
                est = int(size * ratio * 0.85) if ratio > 0 else 0
                tag = 'dirty' if dirty_frames > 0 else 'clean'
                self.tree.insert('', 'end', iid=str(i), tags=(tag,), values=(
                    Path(unit).name, frames, fmt_size(size),
                    f'{ratio * 100:.1f}%', dirty_frames, fmt_size(est) if est else '—'))
            self.tree.tag_configure('dirty', foreground='#c0392b')
            self.tree.tag_configure('clean', foreground='#27ae60')
            self._set_status(f'扫描完成：{len(payload)} 个单元（红色=发现噪声）')
            self._finish_job()
        elif kind == 'clean_done':
            job, stats = payload
            saved = max(0, stats['old_bytes'] - stats['new_bytes'])
            self._log(f'清洗完成：{stats["changed"]}/{stats["files"]} 张被修改，'
                      f'{fmt_size(stats["old_bytes"])} → {fmt_size(stats["new_bytes"])}'
                      f'（省 {fmt_size(saved)}），错误 {stats["errors"]}')
            for b in stats['backups']:
                self._log(f'备份位于: {b}')
            messagebox.showinfo('完成', f'清洗完成：{stats["changed"]} 张修改，节省 {fmt_size(saved)}')
            self._finish_job()
            self.start_scan()
        elif kind == 'verify_done':
            for unit, clean, ratio in payload:
                self._log(f'[复检] {unit.name}: {"✅ 干净" if clean else f"❌ 仍有噪声 {ratio * 100:.1f}%"}')
            self._set_status('复检完成，详见日志')
            self._finish_job()
        elif kind == 'done':
            self._finish_job()
        elif kind == 'ui':
            payload()


def run_gui():
    root_tk = tk.Tk()
    try:
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')
    except tk.TclError:
        pass
    App(root_tk)
    root_tk.mainloop()


# ---------------------------------------------------------------- CLI

def run_cli(argv):
    root_dir = argv[0]
    no_backup = '--no-backup' in argv
    dry = '--dry-run' in argv
    if not os.path.isdir(root_dir):
        print(f'无效目录: {root_dir}')
        return 2
    job = CleanJob(root_dir, backup=not no_backup, dry_run=dry,
                   progress=lambda c, t, m: print(f'\r{m}', end='', flush=True),
                   log=print)
    print(f'扫描 {root_dir} …')
    results = job.scan()
    print()
    for unit, frames, size, ratio, dirty_frames in results:
        mark = 'DIRTY' if dirty_frames else 'clean'
        print(f'[{mark:5}] {Path(unit).name:40} {frames:5} 张  {fmt_size(size):>9}  脏像素 {ratio * 100:5.1f}%')
    if dry:
        print('（dry-run：未做任何修改）')
        return 0
    dirty = [r[0] for r in results if r[3] > 0 or r[4] > 0]
    if not dirty:
        print('全部干净，无需清洗。')
        return 0
    print(f'开始清洗 {len(dirty)} 个单元 …')
    stats = job.run(dirty)
    saved = max(0, stats['old_bytes'] - stats['new_bytes'])
    print(f'完成：{stats["changed"]}/{stats["files"]} 张修改，'
          f'{fmt_size(stats["old_bytes"])} → {fmt_size(stats["new_bytes"])}（省 {fmt_size(saved)}），错误 {stats["errors"]}')
    for b in stats['backups']:
        print(f'备份: {b}')
    print('复检 …')
    for unit, clean, ratio in job.verify(dirty):
        print(f'[复检] {Path(unit).name}: {"OK 干净" if clean else f"仍有噪声 {ratio * 100:.1f}%"}')
    return 0


if __name__ == '__main__':
    args = sys.argv[1:]
    if args and args[0] == '--cli':
        sys.exit(run_cli(args[1:]))
    else:
        run_gui()
