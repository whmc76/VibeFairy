#!/usr/bin/env python3
"""
ClaudeFairy V2 — GUI 启动器

双击即用，风格与矩阵信使一致。
支持在 GUI 里切换任意项目目录，启动前自动写入 claudefairy.toml。

依赖: 仅 Python 标准库 (tkinter, subprocess, threading, queue, tomllib)
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from datetime import datetime
from pathlib import Path
from typing import Optional

# Python 3.11+ 内置 tomllib；旧版本用 tomli
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib          # type: ignore[no-redef]
    except ImportError:
        tomllib = None                   # type: ignore[assignment]

# ── 路径 ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent.resolve()
PYTHON      = sys.executable
CLAUDEFAIRY   = [PYTHON, "-m", "claudefairy", "run"]
TOML_PATH   = PROJECT_DIR / "claudefairy.toml"
TOML_EXAMPLE= PROJECT_DIR / "claudefairy.toml.example"

# ── 颜色 ─────────────────────────────────────────────────────────────────────
GREEN  = "#4CAF50"
RED    = "#F44336"
YELLOW = "#FFC107"
CIRCLE = "●"

# ── 单实例锁端口 ──────────────────────────────────────────────────────────────
_LOCK_PORT = 17655
_lock_sock: Optional[socket.socket] = None


def _acquire_single_instance() -> bool:
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", _LOCK_PORT))
        _lock_sock = s
        return True
    except OSError:
        s.close()
        return False


# ── TOML 读写 ─────────────────────────────────────────────────────────────────

def _read_toml() -> dict:
    """读取 claudefairy.toml，失败时返回空 dict。"""
    if tomllib is None:
        return {}
    src = TOML_PATH if TOML_PATH.exists() else TOML_EXAMPLE
    if not src.exists():
        return {}
    try:
        with open(src, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _current_target_path() -> str:
    """从现有配置读取第一个 target 的 path，找不到返回空串。"""
    data = _read_toml()
    projects = data.get("targets", {}).get("projects", [])
    if projects:
        return projects[0].get("path", "")
    return ""


def _write_target_toml(target_path: str) -> None:
    """把选择的目录写入 claudefairy.toml 的 targets 部分。

    策略：读取现有 toml（或 example），只替换 [[targets.projects]] 块，
    其余设置（budget/scout/retry/triage/notification 等）保持不变。
    """
    raw     = _read_toml()
    p       = Path(target_path)
    name    = p.name or "project"
    # 正规化为正斜杠（TOML 字符串跨平台）
    path_str = str(p).replace("\\", "/")

    # 更新内存中的 targets
    raw.setdefault("targets", {})
    raw["targets"]["projects"] = [
        {
            "name":        name,
            "path":        path_str,
            "description": f"Claude Code 工作目录: {name}",
            "allow_write": True,
            "primary":     True,
        }
    ]

    # 序列化成 TOML（手写，只覆盖 targets 块，其余保留原文）
    _patch_toml_targets(TOML_PATH, name, path_str)


def _patch_toml_targets(toml_path: Path, name: str, path_str: str) -> None:
    """在现有 toml 文件里替换 targets 块，兼容两种格式：
      - 直接 [[targets.projects]]（无 [targets] 头，标准 TOML array-of-tables）
      - [targets] + [[targets.projects]]（带显式 header）
    如果文件不存在，从 example 复制后再替换。
    """
    if not toml_path.exists():
        if TOML_EXAMPLE.exists():
            toml_path.write_text(TOML_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            toml_path.write_text(_minimal_toml(), encoding="utf-8")

    content = toml_path.read_text(encoding="utf-8")

    new_block = (
        "[[targets.projects]]\n"
        f'name = "{name}"\n'
        f'path = "{path_str}"\n'
        f'description = "Claude Code 工作目录: {name}"\n'
        "allow_write = true\n"
        "primary = true\n"
    )

    # 匹配：可选的 [targets] 行 + 一个或多个 [[targets.projects]] 块
    # 止于下一个顶层 [section]（不是 [[...]]）或文件末尾
    pattern = re.compile(
        r"(?:^\[targets\]\n)?(?:\[\[targets\.projects\]\].*?)+(?=^\[(?!\[)|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    if pattern.search(content):
        content = pattern.sub(new_block + "\n", content)
    else:
        content = content.rstrip() + "\n\n" + new_block

    toml_path.write_text(content, encoding="utf-8")


def _minimal_toml() -> str:
    return """\
[daemon]
log_level = "info"
log_dir = "data/logs"
db_path = "data/claudefairy.db"
scout_interval_secs = 3600
daily_report_time = "09:00"

[targets]
[[targets.projects]]
name = "project"
path = "."
description = ""
allow_write = true
primary = true

[budget]
daily_token_limit = 500000
per_task_token_limit = 50000
over_budget_mode = "report_only"

[triage]
max_retries = 3
model = "claude-sonnet-4-6"
timeout_secs = 60
queue_scan_interval_secs = 60

[notification]
quiet_hours_start = "23:00"
quiet_hours_end = "08:00"
"""


# ── DaemonRunner ──────────────────────────────────────────────────────────────

class DaemonRunner:
    """管理 ClaudeFairy daemon 子进程，把日志推进 log_queue。"""

    def __init__(self, log_queue: queue.Queue, status_cb):
        self._log_queue  = log_queue
        self._status_cb  = status_cb
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running    = False
        self._bot_ok     : Optional[bool] = None
        self._sched_ok   : Optional[bool] = None

    def _log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put((ts, text))

    def _push_status(self) -> None:
        self._status_cb(self._bot_ok, self._sched_ok)

    def _parse_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        msg = text
        try:
            d   = json.loads(text)
            msg = d.get("msg", text)
            text = f"[{d.get('level', 'INFO')}] {msg}"
        except (json.JSONDecodeError, TypeError):
            pass

        msg_l = msg.lower()
        if "telegram bot started" in msg_l:
            self._bot_ok = True;   self._push_status()
        elif "conflict" in msg_l:
            self._bot_ok = False;  self._push_status()
        elif "scheduler started" in msg_l:
            self._sched_ok = True; self._push_status()
        elif "shutting down" in msg_l or "stopped" in msg_l:
            self._bot_ok = False; self._sched_ok = False; self._push_status()

        self._log(text)

    def _reader_thread(self) -> None:
        try:
            for raw in self._proc.stdout:
                if not self._running:
                    break
                self._parse_line(raw)
        except Exception:
            pass
        self._proc.wait()
        self._running  = False
        self._bot_ok   = False
        self._sched_ok = False
        self._push_status()
        self._log(f"Daemon 已退出 (rc={self._proc.returncode})")

    def start(self, target_path: str) -> bool:
        """启动 daemon，返回 False 表示前置检查失败。"""
        if self._running:
            return True

        # 前置检查
        if not (PROJECT_DIR / ".env").exists():
            self._log("[错误] 缺少 .env 文件，请参考 .env.example 创建")
            return False

        # 写入选择的目标目录
        try:
            _write_target_toml(target_path)
            self._log(f"[配置] 工作目录已设为: {target_path}")
        except Exception as e:
            self._log(f"[错误] 无法写入 claudefairy.toml: {e}")
            return False

        (PROJECT_DIR / "data" / "logs").mkdir(parents=True, exist_ok=True)

        self._log("正在启动 ClaudeFairy daemon...")
        self._bot_ok   = None
        self._sched_ok = None
        self._push_status()

        try:
            self._proc = subprocess.Popen(
                CLAUDEFAIRY,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=os.environ.copy(),
            )
        except FileNotFoundError:
            self._log(f"[错误] Python 找不到: {PYTHON}")
            return False

        self._running = True
        self._thread  = threading.Thread(target=self._reader_thread, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if not self._running or self._proc is None:
            return
        self._log("正在停止 daemon...")
        self._running = False
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running


# ── ClaudeFairyApp (GUI) ────────────────────────────────────────────────────────

class ClaudeFairyApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClaudeFairy V2  ·  AI 守护进程")
        self.resizable(True, True)
        self.minsize(560, 420)
        self.configure(bg="#1a1a2e")

        self._log_queue: queue.Queue = queue.Queue()
        self._runner: Optional[DaemonRunner] = None
        self._bot_ok  : Optional[bool] = None
        self._sched_ok: Optional[bool] = None

        # 工作目录变量（从现有配置初始化）
        initial_path = _current_target_path() or str(Path.home())
        self._workdir_var = tk.StringVar(value=initial_path)

        self._build_ui()
        self._poll_queue()

    # ── UI 构建 ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # 标题栏
        title_bar = tk.Frame(self, bg="#1a1a2e", pady=8)
        title_bar.pack(fill=tk.X)
        tk.Label(
            title_bar, text="ClaudeFairy V2  ·  AI 守护进程",
            bg="#1a1a2e", fg="#e0e0e0", font=("Segoe UI", 12, "bold"),
        ).pack()
        tk.Label(
            title_bar, text="消息即任务  ·  自动分拣  ·  按钮审批  ·  安全执行",
            bg="#1a1a2e", fg="#555", font=("Segoe UI", 8),
        ).pack()

        # ── 配置区（项目目录选择）────────────────────────────────────────
        cfg_frame = tk.Frame(self, bg="#0d1b2a", pady=8, padx=14)
        cfg_frame.pack(fill=tk.X)
        cfg_frame.columnconfigure(1, weight=1)

        tk.Label(cfg_frame, text="项目目录:", bg="#0d1b2a", fg="#aaa",
                 font=("Segoe UI", 9), anchor="w").grid(
            row=0, column=0, sticky="w", padx=(0, 8))

        self._dir_entry = tk.Entry(
            cfg_frame,
            textvariable=self._workdir_var,
            bg="#1e2d3d", fg="#e0e0e0",
            insertbackground="#e0e0e0",
            relief=tk.FLAT, font=("Consolas", 9),
            bd=4,
        )
        self._dir_entry.grid(row=0, column=1, sticky="ew", ipady=3)

        self._browse_btn = tk.Button(
            cfg_frame, text="浏览…", width=7,
            bg="#37474f", fg="white", relief=tk.FLAT, cursor="hand2",
            font=("Segoe UI", 9),
            command=self._browse_workdir,
        )
        self._browse_btn.grid(row=0, column=2, padx=(6, 0))

        # ── 状态区 ────────────────────────────────────────────────────────
        sf = tk.Frame(self, bg="#16213e", pady=8, padx=14)
        sf.pack(fill=tk.X)
        sf.columnconfigure(2, weight=1)

        def _dot_row(row, label):
            tk.Label(sf, text=label, bg="#16213e", fg="#aaa",
                     width=9, anchor="w").grid(row=row, column=0, sticky="w")
            dot = tk.Label(sf, text=CIRCLE, bg="#16213e", fg=YELLOW,
                           font=("Segoe UI", 13))
            dot.grid(row=row, column=1, sticky="w", padx=(0, 4))
            lbl = tk.Label(sf, text="—", bg="#16213e", fg="#ccc", anchor="w")
            lbl.grid(row=row, column=2, sticky="w")
            return dot, lbl

        self._bot_dot,   self._bot_lbl   = _dot_row(0, "Bot:")
        self._sched_dot, self._sched_lbl = _dot_row(1, "调度器:")

        self._toggle_btn = tk.Button(
            sf, text="启动", width=8,
            bg=GREEN, fg="white", relief=tk.FLAT, cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            command=self._toggle_daemon,
        )
        self._toggle_btn.grid(row=0, column=3, rowspan=2, padx=(12, 4), sticky="e")

        # ── 日志区 ────────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg="#0f0f23")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self._log_text = scrolledtext.ScrolledText(
            log_frame,
            bg="#0f0f23", fg="#d4d4d4",
            font=("Consolas", 9),
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            padx=8, pady=6,
        )
        self._log_text.pack(fill=tk.BOTH, expand=True)
        self._log_text.tag_config("ts",   foreground="#505070")
        self._log_text.tag_config("info", foreground="#d4d4d4")
        self._log_text.tag_config("warn", foreground="#FFC107")
        self._log_text.tag_config("err",  foreground="#f07178")
        self._log_text.tag_config("ok",   foreground="#c3e88d")
        self._log_text.tag_config("task", foreground="#82aaff")

        # ── 底部按钮 ──────────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg="#1a1a2e", pady=6)
        btn_frame.pack(fill=tk.X)

        tk.Button(
            btn_frame, text="清空日志", width=10,
            bg="#37474f", fg="white", relief=tk.FLAT, cursor="hand2",
            command=self._clear_log,
        ).pack(side=tk.LEFT, padx=8)

        tk.Button(
            btn_frame, text="打开日志目录", width=12,
            bg="#37474f", fg="white", relief=tk.FLAT, cursor="hand2",
            command=self._open_log_dir,
        ).pack(side=tk.LEFT)

        tk.Button(
            btn_frame, text="打开项目目录", width=12,
            bg="#37474f", fg="white", relief=tk.FLAT, cursor="hand2",
            command=self._open_project_dir,
        ).pack(side=tk.LEFT, padx=6)

    # ── 目录选择 ──────────────────────────────────────────────────────────

    def _browse_workdir(self) -> None:
        current = self._workdir_var.get()
        initial = current if Path(current).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(
            title="选择 Claude Code 工作目录（项目根目录）",
            initialdir=initial,
            mustexist=True,
        )
        if chosen:
            self._workdir_var.set(chosen)

    # ── 日志写入 ──────────────────────────────────────────────────────────

    def _append_log(self, ts: str, text: str) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, f"{ts}  ", "ts")
        tl = text.lower()
        if any(w in tl for w in ("error", "错误", "failed", "fail", "exception", "traceback")):
            tag = "err"
        elif any(w in tl for w in ("warning", "warn", "conflict", "冲突")):
            tag = "warn"
        elif any(w in tl for w in ("started", "ok", "complete", "成功", "已启动", "已连接", "已设为")):
            tag = "ok"
        elif any(w in tl for w in ("task #", "task#", "任务", "triage", "分拣")):
            tag = "task"
        else:
            tag = "info"
        self._log_text.insert(tk.END, text + "\n", tag)
        self._log_text.config(state=tk.DISABLED)
        self._log_text.see(tk.END)

    def _clear_log(self) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.config(state=tk.DISABLED)

    # ── 队列轮询 ──────────────────────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                ts, text = self._log_queue.get_nowait()
                self._append_log(ts, text)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    # ── 状态回调 ──────────────────────────────────────────────────────────

    def _on_status(self, bot_ok: Optional[bool], sched_ok: Optional[bool]) -> None:
        self.after(0, lambda: self._apply_status(bot_ok, sched_ok))

    def _apply_status(self, bot_ok: Optional[bool], sched_ok: Optional[bool]) -> None:
        def _dot(dot, lbl, ok, yes_txt, no_txt):
            if ok is True:
                dot.config(fg=GREEN); lbl.config(text=yes_txt)
            elif ok is False:
                dot.config(fg=RED);   lbl.config(text=no_txt)
            else:
                dot.config(fg=YELLOW); lbl.config(text="启动中…")

        if bot_ok   is not None: self._bot_ok   = bot_ok
        if sched_ok is not None: self._sched_ok = sched_ok

        _dot(self._bot_dot,   self._bot_lbl,   self._bot_ok,   "已连接", "未连接")
        _dot(self._sched_dot, self._sched_lbl, self._sched_ok, "运行中", "已停止")

        running = self._runner and self._runner.is_running
        self._toggle_btn.config(text="停止" if running else "启动",
                                bg=RED      if running else GREEN)
        # 运行中禁止更改目录
        state = tk.DISABLED if running else tk.NORMAL
        self._dir_entry.config(state=state)
        self._browse_btn.config(state=state)
        self.title("ClaudeFairy V2  ·  " + ("运行中" if running else "已停止"))

    # ── 控制 ──────────────────────────────────────────────────────────────

    def _toggle_daemon(self) -> None:
        if self._runner and self._runner.is_running:
            self._stop_daemon()
        else:
            self._start_daemon()

    def _start_daemon(self) -> None:
        target = self._workdir_var.get().strip()
        if not target:
            messagebox.showwarning("ClaudeFairy", "请先选择项目目录")
            return
        if not Path(target).is_dir():
            messagebox.showerror("ClaudeFairy", f"目录不存在:\n{target}")
            return

        self._toggle_btn.config(state=tk.DISABLED)
        self._runner = DaemonRunner(self._log_queue, self._on_status)
        ok = self._runner.start(target)
        if not ok:
            self._toggle_btn.config(state=tk.NORMAL)
            return
        self._apply_status(None, None)
        self.after(1500, lambda: self._toggle_btn.config(state=tk.NORMAL))

    def _stop_daemon(self) -> None:
        if self._runner:
            self._runner.stop()
        self._apply_status(False, False)

    # ── 辅助 ──────────────────────────────────────────────────────────────

    def _open_log_dir(self) -> None:
        d = PROJECT_DIR / "data" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))

    def _open_project_dir(self) -> None:
        p = self._workdir_var.get()
        if p and Path(p).is_dir():
            os.startfile(p)

    def destroy(self) -> None:
        if self._runner:
            self._runner.stop()
        super().destroy()


# ── 入口 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not _acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "ClaudeFairy",
            "ClaudeFairy 启动器已在运行中！\n\n请先关闭已打开的窗口，再重新启动。"
        )
        root.destroy()
        return

    app = ClaudeFairyApp()
    app.after(300, app._start_daemon)
    app.mainloop()


if __name__ == "__main__":
    main()
