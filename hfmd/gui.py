"""Desktop window for people. Agents should use the download command instead."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from hfmd.curlsafe import PINNED_BUILD, Cancelled, kill_all
from hfmd.download import DownloadError, Progress, download_model


def run() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        root.title("hf-mirror 直连下载")
        root.geometry("760x560")
        root.minsize(640, 480)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.font = ("Microsoft YaHei UI", 10)
        root.option_add("*Font", self.font)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        self.model = tk.StringVar()
        self.concurrency = tk.StringVar(value="8")
        self.bandwidth = tk.StringVar(value="300")
        self.home = tk.StringVar(value=str(Path.home() / "hf-models"))
        self.revision = tk.StringVar(value="main")
        self.token = tk.StringVar()

        self._row(frame, 0, "模型", ttk.Entry(frame, textvariable=self.model))
        self._row(frame, 1, "并发数", ttk.Entry(frame, textvariable=self.concurrency, width=12))
        self._row(frame, 2, "总带宽 Mbps", ttk.Entry(frame, textvariable=self.bandwidth, width=12))
        home_row = ttk.Frame(frame)
        home_entry = ttk.Entry(home_row, textvariable=self.home)
        home_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(home_row, text="浏览", command=self._browse).pack(side=tk.LEFT, padx=(8, 0))
        self._row(frame, 3, "保存目录", home_row)
        self._row(frame, 4, "修订版本", ttk.Entry(frame, textvariable=self.revision, width=20))
        self._row(frame, 5, "Token（可选）", ttk.Entry(frame, textvariable=self.token, show="*"))

        hint = ttk.Label(
            frame,
            text=(
                f"直连 hf-mirror，使用内置 curl {PINNED_BUILD}。"
                "不读取系统代理、环境变量代理或 curlrc。"
                "带宽单位是 Mbps（兆比特/秒），300 Mbps 大约是 37.5 MB/s。"
                "文件保存到 目录/provider/model_name。"
            ),
            wraplength=700,
            justify=tk.LEFT,
        )
        hint.grid(row=6, column=0, columnspan=2, sticky="we", pady=(8, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(buttons, text="开始下载", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(buttons, text="取消", command=self._cancel, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))

        self.progress = ttk.Progressbar(frame, maximum=1000)
        self.progress.grid(row=8, column=0, columnspan=2, sticky="we", pady=(12, 4))
        self.status = tk.StringVar(value="就绪")
        ttk.Label(frame, textvariable=self.status).grid(row=9, column=0, columnspan=2, sticky="w")

        self.log = tk.Text(frame, height=14, wrap=tk.WORD, font=self.font)
        self.log.grid(row=10, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        frame.rowconfigure(10, weight=1)
        self.log.configure(state=tk.DISABLED)
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _row(self, frame: ttk.Frame, row: int, label: str, widget: tk.Widget) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
        widget.grid(row=row, column=1, sticky="we", pady=4)

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.home.get() or str(Path.home()))
        if chosen:
            self.home.set(chosen)

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            concurrency = int(self.concurrency.get().strip())
            bandwidth = float(self.bandwidth.get().strip())
        except ValueError:
            self._append("并发数和带宽必须是数字。")
            return
        self.cancel = threading.Event()
        self.start_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self._append(
            f"开始：{self.model.get().strip()}，并发 {concurrency}，带宽 {bandwidth} Mbps"
        )
        self.worker = threading.Thread(target=self._download, args=(concurrency, bandwidth), daemon=True)
        self.worker.start()

    def _download(self, concurrency: int, bandwidth: float) -> None:
        try:
            dest = download_model(
                self.model.get(),
                concurrency,
                bandwidth,
                Path(self.home.get()),
                revision=self.revision.get(),
                token=self.token.get(),
                progress=self._on_progress,
                cancel=self.cancel,
            )
        except Cancelled as exc:
            message = f"已取消：{exc}"
            self._later(lambda message=message: self._finish(message, failed=True))
        except DownloadError as exc:
            message = f"失败：{exc}"
            self._later(lambda message=message: self._finish(message, failed=True))
        except Exception as exc:  # noqa: BLE001 - show unexpected failures in the window
            message = f"失败：{exc}"
            self._later(lambda message=message: self._finish(message, failed=True))
        else:
            message = f"完成：{dest}"
            self._later(lambda message=message: self._finish(message, failed=False))

    def _on_progress(self, progress: Progress) -> None:
        self._later(lambda progress=progress: self._apply(progress))

    def _apply(self, progress: Progress) -> None:
        if progress.bytes_total > 0:
            self.progress.configure(value=min(1000, progress.bytes_done * 1000 / progress.bytes_total))
        percent = 0.0 if progress.bytes_total <= 0 else progress.bytes_done * 100 / progress.bytes_total
        self.status.set(
            f"{progress.phase}  {percent:.1f}%  "
            f"文件 {progress.files_done}/{progress.files_total}  "
            f"{progress.speed_mbps:.1f} Mbps  {progress.message}"
        )
        if progress.phase in {"start", "listing", "done"} or progress.files_done:
            self._append(progress.message)

    def _finish(self, message: str, failed: bool) -> None:
        self._append(message)
        self.status.set(message)
        if not failed:
            self.progress.configure(value=1000)
        self.start_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)

    def _cancel(self) -> None:
        self.cancel.set()
        kill_all()
        self._append("正在取消，已终止 curl 进程。")

    def _close(self) -> None:
        self.cancel.set()
        kill_all()
        self.root.destroy()

    def _append(self, message: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, message + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _later(self, callback) -> None:
        self.root.after(0, callback)
