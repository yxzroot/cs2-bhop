"""Safe local crash reporting for DragiWare."""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox


class CrashReporter:
    def __init__(self, app_name: str, app_version: str):
        self.app_name = app_name
        self.app_version = app_version
        self._lock = threading.Lock()
        self._handling = False
        self._previous_thread_hook = getattr(threading, "excepthook", None)

    def install(self) -> None:
        sys.excepthook = self.handle_exception
        if self._previous_thread_hook is not None:
            threading.excepthook = self.handle_thread_exception

    def report_tk_exception(self, exc_type, exc_value, exc_traceback) -> None:
        self.handle_exception(exc_type, exc_value, exc_traceback, source="Tkinter callback")

    def handle_thread_exception(self, args) -> None:
        self.handle_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            source=f"thread: {args.thread.name if args.thread else 'unknown'}",
        )

    def handle_exception(self, exc_type, exc_value, exc_traceback, source: str = "main thread") -> None:
        with self._lock:
            if self._handling:
                return
            self._handling = True
        try:
            report_path = self._write_report(exc_type, exc_value, exc_traceback, source)
            try:
                self._show_crash_dialog(report_path)
            except Exception:
                messagebox.showerror(
                    self.app_name,
                    "DragiWare encountered an unexpected error. "
                    "You can report it through the official issue tracker.\n\n"
                    f"A crash report was saved locally:\n{report_path}",
                )
        except Exception:
            pass

    def _show_crash_dialog(self, report_path: Path) -> None:
        parent = getattr(tk, "_default_root", None)
        owns_root = parent is None
        if owns_root:
            parent = tk.Tk()
            parent.withdraw()
        dialog = tk.Toplevel(parent)
        dialog.title("DragiWare Crash Report")
        dialog.resizable(False, False)
        dialog.configure(bg="#15121F")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        frame = tk.Frame(dialog, bg="#15121F", padx=24, pady=20)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="DragiWare encountered an unexpected error.\n"
                 "You can report it through the official issue tracker.",
            bg="#15121F",
            fg="#F7F2FF",
            justify="left",
            wraplength=430,
            font=("Segoe UI", 10),
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=f"A crash report was saved locally:\n{report_path}",
            bg="#15121F",
            fg="#B6AFC4",
            justify="left",
            wraplength=430,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(12, 18))
        actions = tk.Frame(frame, bg="#15121F")
        actions.pack(anchor="e")

        def close_dialog() -> None:
            dialog.destroy()
            if owns_root and parent is not None:
                parent.destroy()

        tk.Button(
            actions,
            text="Close",
            command=close_dialog,
            bg="#2A2338",
            fg="#F7F2FF",
            activebackground="#3A3050",
            activeforeground="#FFFFFF",
            relief="flat",
            padx=14,
            pady=6,
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            actions,
            text="Report Crash",
            command=lambda: (webbrowser.open(
                "https://github.com/yxzroot/cs2-bhop/issues/new"
            ), close_dialog()),
            bg="#B56CFF",
            fg="#160D22",
            activebackground="#D397FF",
            activeforeground="#160D22",
            relief="flat",
            padx=14,
            pady=6,
        ).pack(side="right")
        dialog.transient(parent)
        dialog.grab_set()
        dialog.wait_window()

    def _write_report(self, exc_type, exc_value, exc_traceback, source: str) -> Path:
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        report_directory = appdata / self.app_name / "crash reports"
        report_directory.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        filename = f"crash_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log"
        report_path = report_directory / filename
        details = self._redact("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
        exception_message = self._redact(str(exc_value))
        report = (
            f"Application: {self.app_name}\n"
            f"Version: {self.app_version}\n"
            f"Timestamp: {timestamp}\n"
            f"Source: {source}\n"
            f"Python: {platform.python_version()}\n"
            f"Platform: {platform.system()} {platform.release()}\n"
            f"Frozen: {getattr(sys, 'frozen', False)}\n"
            f"Exception type: {getattr(exc_type, '__name__', str(exc_type))}\n"
            f"Exception message: {exception_message}\n\n"
            f"Traceback:\n{details}"
        )
        report_path.write_text(report, encoding="utf-8")
        return report_path

    @staticmethod
    def _redact(value: str) -> str:
        home = str(Path.home())
        appdata = os.environ.get("APPDATA")
        for sensitive_path in (home, appdata):
            if sensitive_path:
                value = value.replace(sensitive_path, "<user-data>")
        return value


def install_crash_reporter(app_name: str, app_version: str) -> CrashReporter:
    reporter = CrashReporter(app_name, app_version)
    reporter.install()
    return reporter
