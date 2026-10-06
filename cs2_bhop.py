"""Desktop controller for the CS2 bunny-hop helper."""
import ctypes
import hashlib
import json
import math
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
import urllib.request
import webbrowser
import zipfile
from ctypes import wintypes
from pathlib import Path

from build_config import (
    APP_DISPLAY_VERSION,
    APP_NAME,
    APP_VERSION,
    BUILD_CHANNEL,
    BUILD_FLAVOR,
    BUILD_TYPE,
)
from bhop_core import (
    CHANGELOG,
    BhopWorker,
    PRESETS,
    SettingsStore,
)
from crash_reporter import install_crash_reporter
from themes import THEME_NAMES, THEMES, ThemeManager
from updater import StableRelease, UpdateManager

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = ctypes.c_bool
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

SINGLE_INSTANCE_MUTEX = "Local\\DragiWareV2SingleInstance"
MOD_ALT = 0x1
MOD_CONTROL = 0x2
MOD_SHIFT = 0x4
MOD_WIN = 0x8
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
PYMEM_VERSION = "1.14.0"
PYMEM_WHEEL_NAME = f"pymem-{PYMEM_VERSION}-py3-none-any.whl"
PYMEM_SHA256 = "2b9cc64b49d0685f73d616ab1f638611f87e8d649869e7a556f050f677c42a7e"
PYMEM_METADATA_URL = f"https://pypi.org/pypi/Pymem/{PYMEM_VERSION}/json"


class WindowsMessage(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    )


user32_hotkey = ctypes.WinDLL("user32", use_last_error=True)
user32_hotkey.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32_hotkey.RegisterHotKey.restype = wintypes.BOOL
user32_hotkey.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32_hotkey.UnregisterHotKey.restype = wintypes.BOOL
user32_hotkey.GetMessageW.argtypes = (ctypes.POINTER(WindowsMessage), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32_hotkey.GetMessageW.restype = ctypes.c_int
user32_hotkey.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32_hotkey.PostThreadMessageW.restype = wintypes.BOOL


def rounded_rectangle(canvas, x1, y1, x2, y2, radius, **options):
    """Draw a smooth rounded rectangle using only built-in Tk canvas items."""
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, **options)
    canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, **options)
    canvas.create_oval(x1, y1, x1 + radius * 2, y1 + radius * 2, **options)
    canvas.create_oval(x2 - radius * 2, y1, x2, y1 + radius * 2, **options)
    canvas.create_oval(x1, y2 - radius * 2, x1 + radius * 2, y2, **options)
    canvas.create_oval(x2 - radius * 2, y2 - radius * 2, x2, y2, **options)


class RoundedPanel(tk.Canvas):
    """A resizable, rounded card with a regular Frame inside it.

    autosize: False keeps a stretched card, True wraps width and height around
    its children, and "height" wraps height only so the card can still fill the row.
    """
    def __init__(self, parent, color, height=100, radius=22, inset=18, autosize=False):
        super().__init__(parent, bg=parent["bg"], height=height, bd=0, highlightthickness=0)
        self.color, self.radius, self.inset, self.autosize = color, min(radius, 14), inset, autosize
        self.body = tk.Frame(self, bg=color)
        self.window = self.create_window(inset, inset, anchor="nw", window=self.body)
        self.bind("<Configure>", self._resize)
        if autosize:
            self.body.bind("<Configure>", self._fit_to_body)

    def _fit_to_body(self, _event=None):
        height = self.body.winfo_reqheight() + self.inset * 2
        if self.autosize is True:
            width = self.body.winfo_reqwidth() + self.inset * 2
            if int(self["width"] or 0) != width or int(self["height"] or 0) != height:
                self.configure(width=width, height=height)
        elif int(self["height"] or 0) != height:
            self.configure(height=height)

    def _resize(self, _event=None):
        width, height = max(self.winfo_width(), 2), max(self.winfo_height(), 2)
        self.delete("card")
        rounded_rectangle(self, 0, 0, width, height, min(self.radius, height // 2), fill=self.color, outline="", tags="card")
        self.tag_lower("card")
        self.coords(self.window, self.inset, self.inset)
        inner_width = max(1, width - self.inset * 2)
        inner_height = max(1, height - self.inset * 2)
        if self.autosize is True:
            return
        if self.autosize == "height":
            self.itemconfigure(self.window, width=inner_width)
            return
        self.itemconfigure(self.window, width=inner_width, height=inner_height)
        # A fixed card can still have a larger requested height after its
        # children have been laid out. Grow it to the content instead of
        # letting Tk silently crop the last row or control.
        self.after_idle(self._fit_fixed_content)

    def _fit_fixed_content(self):
        if self.autosize is not False or not self.winfo_exists():
            return
        required_height = self.body.winfo_reqheight() + self.inset * 2
        if required_height > self.winfo_height():
            self.configure(height=required_height)


class ThemeCard(tk.Canvas):
    """Palette-driven card used by the Themes page."""
    def __init__(self, parent, name, palette, selected, command):
        super().__init__(parent, bg=parent["bg"], height=154, bd=0, highlightthickness=0, cursor="hand2")
        self.name, self.palette, self.selected, self.command = name, palette, selected, command
        self.hovered = False
        self.body = tk.Frame(self, bg=palette["panel"])
        self.window = self.create_window(12, 12, anchor="nw", window=self.body)
        self.bind("<Configure>", self._resize)
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("card")
        width, height = max(self.winfo_width(), 2), max(self.winfo_height(), 2)
        fill = self.palette["alt"] if self.hovered or self.selected else self.palette["panel"]
        outline = self.palette["accent"] if self.selected else self.palette["line"]
        rounded_rectangle(self, 1, 1, width - 1, height - 1, 16, fill=fill, outline="", tags="card")
        self.configure(highlightthickness=2 if self.selected else 0, highlightbackground=outline)
        self.tag_lower("card")

    def _resize(self, _event=None):
        width = max(self.winfo_width() - 24, 1)
        self.itemconfigure(self.window, width=width)
        self.coords(self.window, 12, 12)
        self._draw()

    def _set_hover(self, hovered):
        self.hovered = hovered
        self._draw()

    def _click(self, _event=None):
        self.command(self.name)


class ScrollArea(tk.Frame):
    """Keeps a page reachable in a smaller window instead of clipping controls."""
    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.canvas.pack(fill="both", expand=True)
        self.body.bind("<Configure>", self._sync_scroll)
        self.canvas.bind("<Configure>", self._stretch)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))
        self.bind("<Destroy>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))

    def _sync_scroll(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 0, 0))

    def _stretch(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _wheel(self, event):
        try:
            if not self.canvas.winfo_exists() or self.canvas.winfo_height() >= self.body.winfo_reqheight():
                return
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            self.canvas.unbind_all("<MouseWheel>")


class RoundedButton(tk.Canvas):
    """A lightweight pill-shaped button, avoiding platform-specific ttk styling."""
    def __init__(self, parent, text, command, fill, foreground, width=140, height=34, hover_fill=None, pressed_fill=None):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], bd=0, highlightthickness=0, cursor="hand2")
        self.text, self.command = text, command
        self.fill, self.foreground, self.width, self.height = fill, foreground, width, height
        self.hover_fill = hover_fill or self._adjust_color(fill, 1.16)
        self.pressed_fill = pressed_fill or self._adjust_color(fill, 0.84)
        self.enabled, self.hovered, self.pressed = True, False, False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda _event: self._hover(True))
        self.bind("<Leave>", lambda _event: self._hover(False))
        self._draw()

    def _draw(self):
        self.delete("all")
        fill = self.fill if self.enabled else self._adjust_color(self.fill, 0.72)
        if self.pressed and self.enabled:
            fill = self.pressed_fill
        elif self.hovered and self.enabled:
            fill = self.hover_fill
        rounded_rectangle(self, 0, 0, self.width, self.height, min(13, self.height // 2), fill=fill, outline="")
        foreground = self.foreground if self.enabled else self._adjust_color(self.foreground, 0.62)
        self.create_text(self.width // 2, self.height // 2, text=self.text, fill=foreground, font=("Segoe UI Semibold", 9))

    @staticmethod
    def _adjust_color(color, factor):
        try:
            value = color.lstrip("#")
            if len(value) != 6:
                return color
            channels = [max(0, min(255, int(int(value[index:index + 2], 16) * factor))) for index in (0, 2, 4)]
            return "#%02x%02x%02x" % tuple(channels)
        except (TypeError, ValueError):
            return color

    def _click(self, _event):
        if self.enabled:
            self.pressed = True
            self._draw()
            self.after(75, self._release)

    def _release(self):
        if not self.winfo_exists():
            return
        self.pressed = False
        self._draw()
        if self.enabled:
            self.command()

    def _hover(self, hovered):
        self.hovered = hovered
        self._draw()

    def set_text(self, text):
        self.text = text
        self._draw()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()


class ToggleSwitch(tk.Canvas):
    """Compact animated switch used for the real on/off runtime controls."""
    def __init__(self, parent, command, initial=False, width=54, height=28, colors=None):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], bd=0, highlightthickness=0, cursor="hand2")
        self.command = command
        self.width, self.height = width, height
        self.value = bool(initial)
        self.target = self.value
        self.position = 1.0 if self.value else 0.0
        self.enabled = True
        self.hovered = False
        colors = colors or {}
        self.track_on = colors.get("track_on", "#a63c4e")
        self.track_off = colors.get("track_off", "#332b32")
        self.hover_on = colors.get("hover_on", "#c24a5b")
        self.hover_off = colors.get("hover_off", "#493940")
        self.thumb = colors.get("thumb", "#fff5f7")
        self.disabled = colors.get("disabled", "#272229")
        self._animation_id = None
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self._draw()

    def _set_hover(self, hovered):
        self.hovered = hovered
        self._draw()

    def _click(self, _event):
        if not self.enabled:
            return
        self.set_state(not self.value)
        self.after(75, self.command)

    def _draw(self):
        self.delete("all")
        track = self.track_on if self.position > 0.5 else self.track_off
        if self.hovered and self.enabled:
            track = self.hover_on if self.position > 0.5 else self.hover_off
        if not self.enabled:
            track = self.disabled
        rounded_rectangle(self, 1, 1, self.width - 1, self.height - 1, self.height // 2, fill=track, outline="")
        thumb = self.thumb if self.enabled else "#716872"
        thumb_diameter = max(12, self.height - 10)
        thumb_padding = 5
        travel = max(0, self.width - (thumb_padding * 2) - thumb_diameter)
        thumb_x = thumb_padding + self.position * travel
        self.create_oval(thumb_x, thumb_padding, thumb_x + thumb_diameter, self.height - thumb_padding, fill=thumb, outline="")

    def set_state(self, value, animate=True):
        self.target = bool(value)
        if self._animation_id:
            try:
                self.after_cancel(self._animation_id)
            except tk.TclError:
                pass
            self._animation_id = None
        if not animate:
            self.value = self.target
            self.position = 1.0 if self.value else 0.0
            self._draw()
            return
        self._animate()

    def _animate(self):
        target_position = 1.0 if self.target else 0.0
        distance = target_position - self.position
        if abs(distance) < 0.04:
            self.position = target_position
            self.value = self.target
            self._animation_id = None
            self._draw()
            return
        self.position += distance * 0.34
        self._draw()
        self._animation_id = self.after(16, self._animate)

    def set_text(self, text):
        """Keep the existing button-facing UI API while transitioning to a switch."""
        if text in ("Turn Off", "Debug: On"):
            self.set_state(True)
        elif text in ("Turn On", "Debug: Off"):
            self.set_state(False)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw()


class ToolTip:
    """Small delayed hover label for controls that benefit from explanation."""
    def __init__(self, widget, text):
        self.widget, self.text, self.tip, self.timer = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _schedule(self, _event=None):
        self.timer = self.widget.after(550, self._show)

    def _show(self):
        if self.tip or not self.widget.winfo_exists():
            return
        root = self.widget.winfo_toplevel()
        tooltip_bg = getattr(root, "TOOLTIP_BG", "#07100f")
        tooltip_text = getattr(root, "TOOLTIP_TEXT", "#d8f7ef")
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.configure(bg=tooltip_bg)
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip.geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg=tooltip_bg, fg=tooltip_text, font=("Segoe UI", 9), padx=9, pady=5).pack()

    def _hide(self, _event=None):
        if self.timer:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class StatusRing(tk.Canvas):
    """Fast, responsive text-only status display."""
    def __init__(self, parent, width=320, height=112, colors=None):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], bd=0, highlightthickness=0)
        self.requested_width, self.requested_height = width, height
        self.phase = 0
        colors = colors or {}
        self.text_color = colors.get("text", "#f3e6ea")
        self.detail_color = colors.get("detail", "#81767d")
        self.label, self.detail, self.accent = "READY", "Start a session when you are ready", colors.get("accent", "#ed5b6d")
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def set_state(self, label, detail, accent):
        self.label, self.detail, self.accent = label, detail, accent
        self._draw()

    def pulse(self):
        """Retained for the existing dashboard lifecycle; the tile is intentionally still."""
        return

    def _draw(self):
        self.delete("all")
        width = self.winfo_width() if self.winfo_width() > 1 else self.requested_width
        height = self.winfo_height() if self.winfo_height() > 1 else self.requested_height
        if self.label == "LIVE":
            detail = "CS2 connected"
        elif self.label == "SYNC":
            detail = "Looking for CS2"
        elif self.label == "READY":
            detail = "Start when ready"
        else:
            detail = self.detail
        center = width / 2
        self.create_text(center, height / 2 - 12, text=self.label, fill=self.text_color, font=("Segoe UI Semibold", 30))
        self.create_text(center, height / 2 + 24, text=detail, fill=self.detail_color, font=("Segoe UI", 9))


class BhopApp(tk.Tk):
    BG, SIDEBAR, PANEL, ALT = "#0b0a0d", "#0f0e12", "#171419", "#211b21"
    TEXT, MUTED, BLUE, GREEN, RED = "#f4f0f3", "#8e858e", "#c54556", "#9acdb7", "#ed5b6d"
    SUBTLE, LINE = "#5e565f", "#2b252c"
    NAV_ACTIVE, NAV_HOVER = "#2b171e", "#1b151b"

    def __init__(self):
        super().__init__()
        self.title("DragiWare — Bhop Control System")
        self.overrideredirect(True)
        # The cards contain real controls, so give the overlay enough width for
        # their copy and actions to breathe instead of forcing them together.
        self.geometry("1180x720")
        self.minsize(980, 620)
        self.attributes("-topmost", True)
        self.configure(bg=self.BG)
        self.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.events, self.stop_event = queue.Queue(), threading.Event()
        self.running, self.worker = False, None
        self.capturing_keybind = None
        self.closing = False
        self.menu_visible = True
        self.hotkey_registered = False
        self.hotkey_thread = None
        self.hotkey_thread_id = None
        self.hotkey_stop = threading.Event()
        self.hotkey_ready = threading.Event()
        self.local_hotkey_bound = False
        self.menu_events = queue.Queue()
        self.drag_origin = None
        self.fade_after_id = None
        self.startup_active = False
        self.startup_overlay = None
        self.startup_log = None
        self.startup_cursor = None
        self.startup_progress = None
        self.startup_progress_value = None
        self.startup_status = None
        self._boot_after_ids = set()
        self._boot_line_index = 0
        self._boot_char_index = 0
        self._boot_current_line = ""
        self._boot_status_index = 0
        self._boot_status_lines = ()
        self.setup_layer = None
        self.setup_thread = None
        self.setup_prompt_shown = False
        self.data_directory = self._resolve_data_directory()
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self._load_user_dependency_path()
        self.dependencies_ready = self._dependencies_ready()
        self.log_directory = self.data_directory / "bunnyhop logs"
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self.settings = SettingsStore(self.data_directory / "bhop_settings.json")
        initial_settings = self.settings.snapshot()
        self.menu_toggle_key = initial_settings["menu_toggle_key"]
        self.menu_toggle_modifiers = initial_settings["menu_toggle_modifiers"]
        self.theme_manager = ThemeManager(self.settings.snapshot().get("theme", "Dark"))
        self._apply_palette()
        self._theme_animation_id = None
        self._theme_finish_id = None
        self._theme_transition_generation = 0
        self._display_palette = self.theme_manager.palette
        executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
        self.updater = UpdateManager(APP_VERSION, self.data_directory, executable)
        self.update_info: StableRelease | None = None
        self.update_downloaded = None
        self.update_thread = None
        self.update_check_running = False
        self.update_dialog = None
        self.wallpaper_canvas = None
        self.wallpaper_source = None
        self.wallpaper_image = None
        self.wallpaper_image_id = None
        self.wallpaper_cache = {}
        values = self.settings.snapshot()
        self.toggle_key, self.exit_key = values["toggle_key"], values["exit_key"]
        self.session_log = self.log_directory / f"session_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log"
        self.session_log.write_text(f"{APP_NAME} {APP_VERSION} session started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
        self._build()
        self._center_overlay()
        self._register_menu_hotkey()
        self._sync_settings_to_ui()
        self._log(f"Settings loaded: {self.settings.path.name} ({values['preset']} preset).")
        self.bind_all("<KeyPress>", self._capture_keybind)
        self.bind_all("<ButtonPress>", self._ignore_mouse_key_capture)
        self.after(80, self._consume_events)
        self.after(30, self._poll_menu_hotkey)
        self._start_startup_animation(values)

    def _ignore_mouse_key_capture(self, _event):
        if self.capturing_keybind == "menu":
            return "break"

    def _boot_after(self, delay, callback, *args):
        """Schedule a boot callback without blocking Tk's event loop."""
        if self.closing:
            return None
        after_id = self.after(delay, callback, *args)
        self._boot_after_ids.add(after_id)
        return after_id

    def _start_startup_animation(self, settings_values):
        """Play the in-app cinematic boot sequence while the real UI is ready."""
        if self.startup_active or self.closing:
            return
        self.startup_active = True
        overlay = tk.Canvas(self, bg=self.BG, bd=0, highlightthickness=0)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.startup_overlay = overlay

        # Reuse the optional theme wallpaper behind the terminal card. It is
        # deliberately left at the edges so the boot screen stays readable.
        if self.wallpaper_source:
            try:
                overlay.create_image(590, 360, image=self.wallpaper_source, anchor="center", tags="boot-wallpaper")
            except tk.TclError:
                pass

        terminal = tk.Frame(overlay, bg=self.PANEL, highlightthickness=1, highlightbackground=self.LINE)
        terminal_window = overlay.create_window(590, 360, window=terminal, width=790, height=520)
        overlay.bind(
            "<Configure>",
            lambda event: (
                overlay.coords(terminal_window, event.width // 2, event.height // 2),
                overlay.coords("boot-wallpaper", event.width // 2, event.height // 2),
            ),
        )
        terminal.grid_columnconfigure(0, weight=1)
        terminal.grid_rowconfigure(2, weight=1)

        header = tk.Frame(terminal, bg=self.PANEL)
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 0))
        tk.Label(header, text="DRAGIWARE  /  SYSTEM BOOT", bg=self.PANEL, fg=self.RED, font=("Cascadia Mono", 9)).pack(side="left")
        tk.Label(header, text=f"{BUILD_CHANNEL.upper()}  /  {BUILD_TYPE.upper()}", bg=self.PANEL, fg=self.SUBTLE, font=("Cascadia Mono", 8)).pack(side="right")
        tk.Frame(terminal, bg=self.LINE, height=1).grid(row=1, column=0, sticky="ew", padx=28, pady=(16, 0))

        log_frame = tk.Frame(terminal, bg=self.PANEL)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=28, pady=(18, 0))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        self.startup_log = tk.Text(
            log_frame,
            bg=self.PANEL,
            fg=self.MUTED,
            insertbackground=self.RED,
            relief="flat",
            bd=0,
            highlightthickness=0,
            wrap="word",
            font=("Cascadia Mono", 9),
            padx=0,
            pady=0,
            state="normal",
        )
        self.startup_log.grid(row=0, column=0, sticky="nsew")
        self.startup_log.tag_configure("heading", foreground=self.TEXT, font=("Cascadia Mono", 10, "bold"))
        self.startup_log.tag_configure("accent", foreground=self.RED)
        self.startup_log.tag_configure("ok", foreground=self.GREEN)
        self.startup_log.tag_configure("deferred", foreground=self.WARNING)
        self.startup_log.insert("end", "DRAGIWARE BOOT SEQUENCE\n\n", "heading")

        footer = tk.Frame(terminal, bg=self.PANEL)
        footer.grid(row=3, column=0, sticky="ew", padx=28, pady=(16, 24))
        footer.grid_columnconfigure(0, weight=1)
        progress_wrap = tk.Frame(footer, bg=self.PANEL)
        progress_wrap.grid(row=0, column=0, sticky="ew")
        progress_wrap.grid_columnconfigure(0, weight=1)
        self.startup_progress = tk.Canvas(progress_wrap, height=5, bg=self.ALT, bd=0, highlightthickness=0)
        self.startup_progress.grid(row=0, column=0, sticky="ew")
        self.startup_progress.bind("<Configure>", self._draw_startup_progress)
        self.startup_progress_value = tk.Label(progress_wrap, text="0%", bg=self.PANEL, fg=self.SUBTLE, font=("Cascadia Mono", 8))
        self.startup_progress_value.grid(row=0, column=1, padx=(12, 0))
        status_row = tk.Frame(footer, bg=self.PANEL)
        status_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.startup_status = tk.Label(status_row, text="initializing…", bg=self.PANEL, fg=self.MUTED, font=("Cascadia Mono", 8), anchor="w")
        self.startup_status.pack(side="left")
        self.startup_cursor = tk.Label(status_row, text="▌", bg=self.PANEL, fg=self.RED, font=("Cascadia Mono", 9))
        self.startup_cursor.pack(side="right")

        self._boot_lines = (
            "> initializing dragiware core...",
            "> loading configuration...",
            "> scanning local assets...",
            "> initializing theme engine...",
            "> loading interface modules...",
            "> checking application integrity...",
            "> initializing network module...",
            "> preparing update check...",
            "> loading community services...",
            "> preparing user interface...",
            "> applying selected theme...",
            "> initializing animations...",
            "> finalizing modules...",
        )
        self._boot_pauses = (100, 70, 130, 90, 110, 160, 80, 120, 100, 80, 130, 90, 200)
        self._boot_line_index = 0
        self._boot_char_index = 0
        self._boot_status_index = 0
        self._boot_blink_cursor()
        self._boot_after(180, self._boot_type_line)

    def _boot_blink_cursor(self):
        if self.closing or not self.startup_overlay or not self.startup_overlay.winfo_exists():
            return
        current = self.startup_cursor.cget("fg")
        self.startup_cursor.configure(fg=self.PANEL if current == self.RED else self.RED)
        self._boot_after(420, self._boot_blink_cursor)

    def _boot_type_line(self):
        if self.closing or not self.startup_overlay or not self.startup_overlay.winfo_exists():
            return
        if self._boot_line_index >= len(self._boot_lines):
            self.startup_log.insert("end", "\n", "accent")
            self._boot_status_lines = self._boot_checks()
            self._boot_status_index = 0
            self.startup_status.configure(text="verifying local modules…")
            self._boot_after(220, self._boot_type_status)
            return
        if self._boot_char_index == 0:
            self._boot_current_line = self._boot_lines[self._boot_line_index]
        if self._boot_char_index < len(self._boot_current_line):
            self.startup_log.insert("end", self._boot_current_line[self._boot_char_index])
            self.startup_log.see("end")
            self._boot_char_index += 1
            delay_pattern = (6, 9, 5, 11, 7, 8, 6)
            delay = delay_pattern[(self._boot_char_index + self._boot_line_index) % len(delay_pattern)]
            self._boot_after(delay, self._boot_type_line)
            return
        self.startup_log.insert("end", "\n")
        self._boot_line_index += 1
        self._boot_char_index = 0
        self._boot_after(self._boot_pauses[self._boot_line_index - 1], self._boot_type_line)

    def _boot_checks(self):
        palette_ready = self.theme_name in THEME_NAMES and bool(self.theme_manager.palette)
        interface_ready = bool(getattr(self, "shell", None)) and self.winfo_exists()
        core_ready = bool(getattr(self, "settings", None)) and bool(getattr(self, "session_log", None))
        services_ready = self.dependencies_ready and bool(getattr(self, "updater", None))
        return (
            ("dragiware core", "OK" if core_ready else "DEFERRED", "ok" if core_ready else "deferred"),
            ("theme engine", "OK" if palette_ready else "DEFERRED", "ok" if palette_ready else "deferred"),
            ("interface", "OK" if interface_ready else "DEFERRED", "ok" if interface_ready else "deferred"),
            ("services", "OK" if services_ready else "DEFERRED", "ok" if services_ready else "deferred"),
        )

    def _boot_type_status(self):
        if self.closing or not self.startup_overlay or not self.startup_overlay.winfo_exists():
            return
        if self._boot_status_index >= len(self._boot_status_lines):
            self.startup_status.configure(text="loading…")
            self._boot_after(160, self._boot_progress_step, 0)
            return
        label, result, tag = self._boot_status_lines[self._boot_status_index]
        self.startup_log.insert("end", f"{label:<26}{result}\n", tag)
        self.startup_log.see("end")
        self._boot_status_index += 1
        self.startup_status.configure(text=f"{label}  /  {result.lower()}")
        self._boot_after(210 if result == "OK" else 300, self._boot_type_status)

    def _draw_startup_progress(self, _event=None):
        if not self.startup_progress or not self.startup_progress.winfo_exists():
            return
        width = max(self.startup_progress.winfo_width(), 2)
        self.startup_progress.delete("all")
        self.startup_progress.create_rectangle(0, 0, width, 5, fill=self.ALT, outline="")
        value = getattr(self, "_boot_progress_value", 0)
        self.startup_progress.create_rectangle(0, 0, width * value / 100, 5, fill=self.RED, outline="")

    def _boot_progress_step(self, value):
        if self.closing or not self.startup_overlay or not self.startup_overlay.winfo_exists():
            return
        self._boot_progress_value = min(100, value + 4)
        self._draw_startup_progress()
        self.startup_progress_value.configure(text=f"{self._boot_progress_value}%")
        self.startup_status.configure(text="finalizing modules…" if self._boot_progress_value < 100 else "system ready")
        if self._boot_progress_value < 100:
            delay = 30 + (self._boot_progress_value % 4) * 8
            self._boot_after(delay, self._boot_progress_step, self._boot_progress_value)
        else:
            self.startup_log.insert("end", "\nSYSTEM READY\n\n", "heading")
            self.startup_log.insert("end", "        D R A G I W A R E\n\n", "accent")
            self.startup_log.insert("end", "Ready.\n", "heading")
            self.startup_log.see("end")
            self.startup_status.configure(text="welcome back, user.")
            self._boot_after(850, self._finish_startup_animation)

    def _finish_startup_animation(self):
        if self.closing:
            return
        overlay = self.startup_overlay
        self.startup_active = False
        self.startup_overlay = None
        if overlay and overlay.winfo_exists():
            overlay.destroy()
        try:
            self.attributes("-alpha", 0.0)
            self._fade_menu(0.0, 1.0, 0)
        except tk.TclError:
            pass
        values = self.settings.snapshot()
        if not self.dependencies_ready:
            self.after(140, self._show_setup)
        if values.get("auto_check_updates", True):
            self.after(900, self._startup_update_check)

    @staticmethod
    def _resolve_data_directory():
        if getattr(sys, "frozen", False):
            appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            return appdata / "DragiWare"
        return Path(__file__).resolve().parent

    def _user_dependency_directory(self):
        return self.data_directory / "dependencies" / f"pymem-{PYMEM_VERSION}"

    def _load_user_dependency_path(self):
        dependency_directory = self._user_dependency_directory()
        if (dependency_directory / "pymem").is_dir() and str(dependency_directory) not in sys.path:
            sys.path.insert(0, str(dependency_directory))

    def _dependencies_ready(self):
        try:
            # Check the same imports the worker needs. This detects the copy
            # bundled into a release or a previously downloaded copy.
            import pymem
            import pymem.process
            return True
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    def _apply_palette(self, name=None, palette=None):
        if name:
            self.theme_manager.select(name)
        palette = palette or self.theme_manager.palette
        aliases = {
            "BG": "bg", "SIDEBAR": "sidebar", "PANEL": "panel", "ALT": "alt",
            "TEXT": "text", "MUTED": "muted", "SUBTLE": "subtle", "LINE": "line",
            "RED": "accent", "BLUE": "blue", "GREEN": "green", "WARNING": "warning",
            "DANGER": "danger", "ACCENT_SOFT": "accent_soft", "ACCENT_HOVER": "accent_hover",
            "BUTTON": "button", "BUTTON_HOVER": "button_hover", "INPUT": "input",
            "LOGO_BG": "logo_bg", "LOGO_PRIMARY": "logo_primary", "LOGO_SECONDARY": "logo_secondary",
            "LOGO_ACCENT": "logo_accent", "TOOLTIP_BG": "tooltip_bg", "TOOLTIP_TEXT": "tooltip_text",
        }
        for attribute, key in aliases.items():
            setattr(self, attribute, palette[key])
        self.NAV_ACTIVE = self.ACCENT_SOFT
        self.NAV_HOVER = self.ALT
        self.ACCENT = self.RED
        self.BACKGROUND = palette.get("background")
        self.theme_name = self.theme_manager.name

    @staticmethod
    def _blend_theme_color(start, end, progress):
        if not (isinstance(start, str) and isinstance(end, str) and start.startswith("#") and end.startswith("#")):
            return end if progress >= 1 else start
        try:
            channels = [
                round(int(start[index:index + 2], 16) + (int(end[index:index + 2], 16) - int(start[index:index + 2], 16)) * progress)
                for index in (1, 3, 5)
            ]
            return "#%02x%02x%02x" % tuple(channels)
        except (TypeError, ValueError):
            return end if progress >= 1 else start

    def _interpolated_palette(self, start, end, progress):
        return {key: self._blend_theme_color(start.get(key), end.get(key), progress) for key in end}

    def _update_theme_widgets(self, old_palette, palette):
        color_map = {old_palette[key]: palette[key] for key in palette if key in old_palette}

        def update(widget):
            if isinstance(widget, RoundedPanel):
                widget.color = palette.get("panel", widget.color)
                widget.body.configure(bg=widget.color)
                widget._resize()
            elif isinstance(widget, RoundedButton):
                for attribute in ("fill", "foreground", "hover_fill", "pressed_fill"):
                    value = getattr(widget, attribute, None)
                    if value in color_map:
                        setattr(widget, attribute, color_map[value])
                widget._draw()
            elif isinstance(widget, ToggleSwitch):
                for attribute in ("track_on", "track_off", "hover_on", "hover_off", "thumb", "disabled"):
                    value = getattr(widget, attribute, None)
                    if value in color_map:
                        setattr(widget, attribute, color_map[value])
                widget._draw()
            if isinstance(widget, ThemeCard):
                widget.palette = palette if widget.name == self._transition_target else THEMES.get(widget.name, widget.palette)
                widget.body.configure(bg=widget.palette.get("alt", widget.palette["panel"]))
                widget._draw()
            for option in ("bg", "fg", "activebackground", "activeforeground", "highlightbackground", "insertbackground"):
                try:
                    value = widget.cget(option)
                    value = str(value)
                    if value in color_map:
                        widget.configure(**{option: color_map[value]})
                except (tk.TclError, KeyError, TypeError, ValueError):
                    pass
            for child in widget.winfo_children():
                update(child)

        update(self)

    def _animate_theme(self, start, target_name, target, start_time, generation):
        if self.closing or generation != self._theme_transition_generation:
            return
        try:
            elapsed = time.perf_counter() - start_time
            progress = min(1.0, elapsed / 0.28)
            eased = 1 - (1 - progress) ** 3
            previous = self._display_palette
            palette = self._interpolated_palette(start, target, eased)
            self._display_palette = palette
            self._apply_palette(palette=palette)
            self._update_theme_widgets(previous, palette)
            for canvas in getattr(self, "_theme_preview_canvases", ()):
                if canvas.winfo_exists():
                    self._draw_theme_preview(canvas)
            if hasattr(self, "themes_page"):
                self.themes_page.update_idletasks()
            if progress < 1.0:
                self._theme_animation_id = self.after(16, self._animate_theme, start, target_name, target, start_time, generation)
                return
            self._theme_animation_id = None
            self.theme_manager.select(target_name)
            self._apply_palette()
            self._display_palette = self.theme_manager.palette
            self._theme_finish_id = self.after_idle(self._finish_theme_transition, target_name, generation)
        except (tk.TclError, TypeError, ValueError, RuntimeError) as error:
            if generation != self._theme_transition_generation:
                return
            self._theme_animation_id = None
            self.theme_manager.select(target_name)
            self._apply_palette()
            self._display_palette = self.theme_manager.palette
            self._log(f"Theme transition recovered after a widget update error: {error}")
            self._theme_finish_id = self.after_idle(self._finish_theme_transition, target_name, generation)

    def _finish_theme_transition(self, target_name, generation):
        if self.closing or generation != self._theme_transition_generation:
            return
        self._theme_finish_id = None
        try:
            target = THEMES[target_name]
            self._apply_palette(palette=target)
            self._display_palette = dict(target)
            def refresh_card(widget):
                if isinstance(widget, ThemeCard):
                    widget.selected = widget.name == target_name
                    widget.palette = THEMES[widget.name]
                    widget._draw()
                for child in widget.winfo_children():
                    refresh_card(child)
            refresh_card(self)
            for canvas in getattr(self, "_theme_preview_canvases", ()):
                if canvas.winfo_exists():
                    self._draw_theme_preview(canvas)
            self.update_idletasks()
            self._log(f"Theme loaded: {target_name}.")
        except (tk.TclError, KeyError, TypeError, ValueError, RuntimeError) as error:
            self._log(f"Theme transition finalization failed: {error}")

    def _rebuild_for_theme(self):
        active_tab = getattr(self, "active_tab", "dashboard")
        status_text = self.status.cget("text") if hasattr(self, "status") else "Ready to start"
        was_running = self.running
        if hasattr(self, "shell") and self.shell.winfo_exists():
            self.shell.destroy()
        self.nav_items = {}
        self.start_buttons, self.stop_buttons, self.toggle_buttons, self.status_labels = [], [], [], []
        self._build(announce=False)
        self.show_tab(active_tab if active_tab in self.pages else "dashboard")
        if was_running:
            for button in self.start_buttons:
                button.set_enabled(False)
            for button in self.stop_buttons:
                button.set_enabled(True)
        self._set_status(status_text, "success" if status_text.startswith("Attached") else "waiting" if status_text.startswith("Waiting") else "error" if "lost" in status_text.lower() else "idle")

    def _apply_theme(self, name):
        if name not in THEME_NAMES or name == self.theme_name:
            return
        self._theme_transition_generation += 1
        generation = self._theme_transition_generation
        if self._theme_animation_id:
            self.after_cancel(self._theme_animation_id)
            self._theme_animation_id = None
        if self._theme_finish_id:
            self.after_cancel(self._theme_finish_id)
            self._theme_finish_id = None
        start = dict(getattr(self, "_display_palette", self.theme_manager.palette))
        self.settings.update(theme=name)
        self._transition_target = name
        self._animate_theme(start, name, THEMES[name], time.perf_counter(), generation)

    def _show_setup(self):
        if self.setup_layer or self.closing:
            return
        self.setup_layer = tk.Frame(self, bg=self.BG)
        self.setup_layer.place(relx=0, rely=0, relwidth=1, relheight=1)
        card = tk.Frame(self.setup_layer, bg=self.PANEL, highlightthickness=1, highlightbackground=self.LINE)
        card.place(relx=0.5, rely=0.5, anchor="center", width=500, height=300)
        tk.Label(card, text="DRAGIWARE", bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=30, pady=(28, 0))
        tk.Label(card, text="First-run setup", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 22)).pack(anchor="w", padx=30, pady=(7, 0))
        self.setup_status = tk.Label(card, text="Checking system…", bg=self.PANEL, fg=self.MUTED, justify="left", anchor="w", font=("Cascadia Mono", 9))
        self.setup_status.pack(fill="x", padx=30, pady=(22, 0))
        action_row = tk.Frame(card, bg=self.PANEL)
        action_row.pack(fill="x", padx=30, pady=(22, 0))
        self.setup_action = RoundedButton(action_row, "Download component", self._setup_action, self.RED, self.TEXT, width=132, height=32)
        self.setup_action.pack(side="right")
        self.setup_quit = RoundedButton(action_row, "Exit", self.shutdown, self.BUTTON, self.TEXT, width=72, height=32)
        self.setup_quit.pack(side="right", padx=(0, 8))
        self._check_setup()
        if not self.dependencies_ready:
            self.after(180, self._ask_setup_download)

    def _check_setup(self):
        if not self.setup_layer or self.closing:
            return
        if self._dependencies_ready():
            self.dependencies_ready = True
            self.setup_status.configure(text="✓ Runtime\n✓ Dependencies\n\nStarting DragiWare…", fg=self.GREEN)
            self.setup_action.set_enabled(False)
            self.setup_quit.set_enabled(False)
            self.after(500, self._close_setup)
            return
        component = "pymem"
        action = "Download component"
        self.setup_status.configure(
            text=(
                "✓ Runtime\n"
                "↓ Hey buddy, you're missing some components here.\n\n"
                f'Here\'s what you need: "{component}"\n\n'
                "Want me to download it for you?"
            ),
            fg=self.MUTED,
        )
        self.setup_action.set_text(action)
        self.setup_action.set_enabled(True)

    def _ask_setup_download(self):
        if self.setup_prompt_shown or not self.setup_layer or self.closing:
            return
        if self._dependencies_ready():
            self._check_setup()
            return
        self.setup_prompt_shown = True
        should_download = messagebox.askyesno(
            title="DragiWare component setup",
            message=(
                "Should I do the downloading for the components, "
                "or are you gonna do it????????\n\n"
                "DragiWare will download the required component automatically "
                "and verify it before use."
            ),
            parent=self,
            default="yes",
        )
        if should_download:
            self._setup_action()
        else:
            self.setup_status.configure(
                text=(
                    "✓ Runtime\n"
                    '↓ Component "pymem" is still needed.\n\n'
                    "No problem — click Download component whenever you're ready."
                ),
                fg=self.MUTED,
            )

    def _setup_action(self):
        if self._dependencies_ready():
            self._check_setup()
            return
        if self.setup_thread and self.setup_thread.is_alive():
            return
        self.setup_action.set_enabled(False)
        self.setup_status.configure(text="✓ Runtime\n↓ Downloading required component…\n\nThis can take a moment.", fg=self.MUTED)
        self.setup_thread = threading.Thread(target=self._install_pymem, name="DragiWareSetup", daemon=True)
        self.setup_thread.start()

    def _install_pymem(self):
        error = None
        try:
            self._download_pymem()
        except Exception as exc:
            error = str(exc)
        try:
            self.after(0, self._finish_setup_install, error)
        except tk.TclError:
            pass

    def _download_pymem(self):
        """Download and verify the pinned pymem wheel into DragiWare data."""
        dependency_root = self.data_directory / "dependencies"
        dependency_root.mkdir(parents=True, exist_ok=True)
        final_directory = self._user_dependency_directory()
        if (final_directory / "pymem").is_dir():
            self._load_user_dependency_path()
            return

        request = urllib.request.Request(
            PYMEM_METADATA_URL,
            headers={"User-Agent": f"DragiWare/{APP_VERSION}"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            metadata = json.loads(response.read().decode("utf-8"))
        if metadata.get("info", {}).get("version") != PYMEM_VERSION:
            raise ValueError("The downloaded dependency metadata has an unexpected version.")

        wheel = next(
            (
                item for item in metadata.get("urls", [])
                if item.get("filename") == PYMEM_WHEEL_NAME and item.get("packagetype") == "bdist_wheel"
            ),
            None,
        )
        if not wheel or not wheel.get("url"):
            raise ValueError(f"The official pymem {PYMEM_VERSION} wheel was not found.")
        metadata_hash = wheel.get("digests", {}).get("sha256", "").lower()
        if metadata_hash != PYMEM_SHA256:
            raise ValueError("The dependency checksum in the official metadata did not match the pinned release.")

        wheel_path = dependency_root / f".{PYMEM_WHEEL_NAME}.part"
        staging_directory = dependency_root / f".{PYMEM_VERSION}.staging"
        try:
            download_request = urllib.request.Request(
                wheel["url"],
                headers={"User-Agent": f"DragiWare/{APP_VERSION}"},
            )
            digest = hashlib.sha256()
            with urllib.request.urlopen(download_request, timeout=30) as response, wheel_path.open("wb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest().lower() != PYMEM_SHA256:
                raise ValueError("The downloaded dependency failed checksum verification.")

            if staging_directory.exists():
                shutil.rmtree(staging_directory)
            staging_directory.mkdir(parents=True)
            with zipfile.ZipFile(wheel_path) as archive:
                staging_root = staging_directory.resolve()
                for member in archive.infolist():
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError("The dependency archive contains an unsafe path.")
                    target = (staging_directory / member_path).resolve()
                    if os.path.commonpath((str(staging_root), str(target))) != str(staging_root):
                        raise ValueError("The dependency archive contains an unsafe path.")
                archive.extractall(staging_directory)
            if not (staging_directory / "pymem").is_dir():
                raise ValueError("The downloaded dependency archive was incomplete.")

            if final_directory.exists():
                if final_directory.is_dir():
                    shutil.rmtree(final_directory)
                else:
                    final_directory.unlink()
            staging_directory.replace(final_directory)
            self._load_user_dependency_path()
        finally:
            if wheel_path.exists():
                wheel_path.unlink()
            if staging_directory.exists():
                shutil.rmtree(staging_directory)

    def _finish_setup_install(self, error):
        if error:
            self.setup_status.configure(text=f"✓ Runtime\n× Component download failed\n\n{error}", fg=self.RED)
            self.setup_action.set_enabled(True)
            self.setup_action.set_text("Retry")
            return
        self._check_setup()

    def _close_setup(self):
        if self.setup_layer and self.setup_layer.winfo_exists():
            self.setup_layer.destroy()
        self.setup_layer = None

    def _center_overlay(self):
        self.update_idletasks()
        width, height = self.winfo_width(), self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _register_menu_hotkey(self):
        values = self.settings.snapshot()
        self.menu_toggle_key = values["menu_toggle_key"]
        self.menu_toggle_modifiers = values["menu_toggle_modifiers"]
        self.hotkey_thread = threading.Thread(target=self._menu_hotkey_worker, name="DragiWareMenuHotkey", daemon=True)
        self.hotkey_thread.start()
        self.hotkey_ready.wait(0.6)
        if not self.hotkey_registered:
            if not self.local_hotkey_bound:
                self.bind_all("<KeyPress>", self._handle_local_menu_key, add="+")
                self.local_hotkey_bound = True
            self._log(f"Global {self._key_name(self.menu_toggle_key, self.menu_toggle_modifiers)} was unavailable; using the local overlay fallback.")

    def _menu_hotkey_worker(self):
        self.hotkey_thread_id = int(kernel32.GetCurrentThreadId())
        try:
            self.hotkey_registered = bool(
                user32_hotkey.RegisterHotKey(
                    None,
                    1,
                    self.menu_toggle_modifiers,
                    self.menu_toggle_key,
                )
            )
            self.hotkey_ready.set()
            if not self.hotkey_registered:
                return
            message = WindowsMessage()
            while not self.hotkey_stop.is_set():
                result = user32_hotkey.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == WM_HOTKEY and message.wParam == 1:
                    self.menu_events.put("toggle")
        finally:
            if self.hotkey_registered:
                user32_hotkey.UnregisterHotKey(None, 1)
            self.hotkey_registered = False
            self.hotkey_thread_id = None

    def _poll_menu_hotkey(self):
        if self.closing:
            return
        try:
            while True:
                self.menu_events.get_nowait()
                if not self.startup_active:
                    self._toggle_menu()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(30, self._poll_menu_hotkey)

    def _handle_local_menu_key(self, event):
        if (
            not self.startup_active
            and event.keycode == self.menu_toggle_key
            and self._event_modifiers(event) == self.menu_toggle_modifiers
        ):
            self._toggle_menu()
            return "break"

    def _stop_menu_hotkey(self):
        self.hotkey_stop.set()
        if self.hotkey_thread_id:
            user32_hotkey.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)
        if self.hotkey_thread and self.hotkey_thread.is_alive():
            self.hotkey_thread.join(timeout=0.6)
        self.hotkey_thread = None

    def _restart_menu_hotkey(self):
        self._stop_menu_hotkey()
        self.hotkey_stop = threading.Event()
        self.hotkey_ready = threading.Event()
        self.hotkey_registered = False
        self._register_menu_hotkey()

    def _toggle_menu(self):
        if self.startup_active:
            return
        if self.menu_visible:
            self._hide_menu()
        else:
            self._show_menu()

    def _show_menu(self):
        if self.menu_visible or self.closing:
            return
        self._cancel_fade()
        self.menu_visible = True
        self.deiconify()
        self.lift()
        self.focus_force()
        self._fade_menu(0.0, 1.0, 0)

    def _hide_menu(self):
        if not self.menu_visible or self.closing:
            return
        self._cancel_fade()
        self.menu_visible = False
        self._fade_menu(1.0, 0.0, 0, hide_when_done=True)

    def _cancel_fade(self):
        if self.fade_after_id:
            try:
                self.after_cancel(self.fade_after_id)
            except tk.TclError:
                pass
            self.fade_after_id = None

    def _fade_menu(self, current, target, step, hide_when_done=False):
        if self.closing:
            return
        current = current + (target - current) * 0.34
        if abs(target - current) < 0.04 or step >= 10:
            current = target
        try:
            self.attributes("-alpha", current)
        except tk.TclError:
            current = target
        if current == target:
            self.fade_after_id = None
            if hide_when_done:
                self.withdraw()
            return
        self.fade_after_id = self.after(16, self._fade_menu, current, target, step + 1, hide_when_done)

    def _begin_drag(self, event):
        self.drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_window(self, event):
        if not self.drag_origin:
            return
        x = event.x_root - self.drag_origin[0]
        y = event.y_root - self.drag_origin[1]
        self.geometry(f"+{x}+{y}")

    def _build(self, announce=True):
        shell = tk.Frame(self, bg=self.BG)
        shell_padding = 14 if self.BACKGROUND else 0
        shell.pack(fill="both", expand=True, padx=shell_padding, pady=shell_padding)
        self.shell = shell
        self._configure_wallpaper(shell)

        sidebar = tk.Frame(shell, bg=self.SIDEBAR, width=218)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=self.SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(24, 26))
        mark = tk.Canvas(brand, width=34, height=34, bg=self.LOGO_BG, bd=0, highlightthickness=0)
        mark.pack(side="left", padx=(0, 11))
        self._draw_logo(mark)
        brand_copy = tk.Frame(brand, bg=self.SIDEBAR)
        brand_copy.pack(side="left")
        tk.Label(brand_copy, text="dragiware", bg=self.SIDEBAR, fg=self.TEXT, font=("Segoe UI Semibold", 19)).pack(anchor="w")
        tk.Label(brand_copy, text="BHOP CONTROL SYSTEM", bg=self.SIDEBAR, fg=self.SUBTLE, font=("Segoe UI Semibold", 7)).pack(anchor="w", pady=(3, 0))
        tk.Frame(sidebar, bg=self.LINE, height=1).pack(fill="x", padx=18, pady=(0, 24))
        tk.Label(sidebar, text="WORKSPACE", bg=self.SIDEBAR, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=20, pady=(0, 9))
        self.nav_items = {}
        self._add_nav_item(sidebar, "dashboard", "⌂", "Overview", "Control center")
        self._add_nav_item(sidebar, "bhop", "◎", "BHOP", "Primary control")
        self._add_nav_item(sidebar, "activity", "≋", "Activity", "Session ledger")
        self._add_nav_item(sidebar, "socials", "↗", "Socials", "Find the team")
        self._add_nav_item(sidebar, "updates", "↻", "Updates", "Verified releases")
        self._add_nav_item(sidebar, "themes", "✦", "Themes", "Colors + preview")
        self._add_nav_item(sidebar, "settings", "⚙", "Settings", "Preferences")
        spacer = tk.Frame(sidebar, bg=self.SIDEBAR)
        spacer.pack(fill="both", expand=True)
        runtime = tk.Frame(sidebar, bg=self.LOGO_BG, highlightthickness=1, highlightbackground=self.LINE)
        runtime.pack(fill="x", padx=14, pady=(10, 12))
        self.sidebar_runtime_label = tk.Label(runtime, text="●  RUNTIME", bg=self.LOGO_BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8))
        self.sidebar_runtime_label.pack(anchor="w", padx=12, pady=(12, 6))
        self.sidebar_state_label = tk.Label(runtime, text="STANDBY", bg=self.LOGO_BG, fg=self.TEXT, font=("Segoe UI Semibold", 10))
        self.sidebar_state_label.pack(anchor="w", padx=12)
        self.sidebar_state_hint = tk.Label(runtime, text="Awaiting session", bg=self.LOGO_BG, fg=self.SUBTLE, font=("Segoe UI", 8))
        self.sidebar_state_hint.pack(anchor="w", padx=12, pady=(4, 12))
        sidebar_footer = tk.Frame(sidebar, bg=self.SIDEBAR)
        sidebar_footer.pack(side="bottom", fill="x", padx=20, pady=(0, 19))
        tk.Label(sidebar_footer, text=f"DRAGIWARE V{APP_DISPLAY_VERSION}", bg=self.SIDEBAR, fg=self.SUBTLE, font=("Segoe UI Semibold", 7)).pack(side="left")
        tk.Label(sidebar_footer, text=f"  •  {BUILD_CHANNEL.upper()} {BUILD_TYPE.upper()}", bg=self.SIDEBAR, fg=self.SUBTLE, font=("Segoe UI Semibold", 7)).pack(side="left")

        main = tk.Frame(shell, bg=self.BG)
        main.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(main, bg=self.BG, height=70)
        topbar.pack(fill="x", padx=26, pady=(10, 0))
        topbar.pack_propagate(False)
        title_group = tk.Frame(topbar, bg=self.BG)
        title_group.pack(side="left", anchor="sw", pady=(0, 13))
        self.page_eyebrow = tk.Label(title_group, text="CONTROL CENTER  /  OVERVIEW", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8))
        self.page_eyebrow.pack(anchor="w")
        self.page_title = tk.Label(title_group, text="DragiWare is ready.", bg=self.BG, fg=self.TEXT, font=("Segoe UI", 20))
        self.page_title.pack(anchor="w", pady=(5, 0))
        chrome = tk.Frame(topbar, bg=self.BG)
        chrome.pack(side="right", anchor="sw", pady=(0, 13))
        self.menu_hint = tk.Label(chrome, text="F5  /  MENU", bg=self.BG, fg=self.SUBTLE, font=("Cascadia Mono", 8))
        self.menu_hint.pack(side="left", padx=(0, 15), pady=7)
        self.top_status = tk.Label(chrome, text="●  READY", bg=self.ACCENT_SOFT, fg=self.RED, font=("Segoe UI Semibold", 8), padx=11, pady=6)
        self.top_status.pack(side="left")
        hide_button = tk.Button(chrome, text="—", command=self._hide_menu, bg=self.BG, fg=self.MUTED, activebackground=self.NAV_HOVER, activeforeground=self.TEXT, relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 11), padx=8, cursor="hand2")
        hide_button.pack(side="left", padx=(8, 0))
        close_button = tk.Button(chrome, text="×", command=self.shutdown, bg=self.BG, fg=self.RED, activebackground=self.ACCENT_SOFT, activeforeground=self.ACCENT_HOVER, relief="flat", bd=0, highlightthickness=0, font=("Segoe UI", 13), padx=8, cursor="hand2")
        close_button.pack(side="left")
        for widget in (topbar, title_group, self.page_eyebrow, self.page_title):
            widget.bind("<ButtonPress-1>", self._begin_drag)
            widget.bind("<B1-Motion>", self._drag_window)

        self.content = tk.Frame(main, bg=self.BG)
        self.content.pack(fill="both", expand=True, padx=26, pady=(0, 20))
        self.status_scroll = ScrollArea(self.content, self.BG)
        self.status_page = self.status_scroll.body
        self.bhop_scroll = ScrollArea(self.content, self.BG)
        self.bhop_page = self.bhop_scroll.body
        self.activity_scroll = ScrollArea(self.content, self.BG)
        self.activity_page = self.activity_scroll.body
        self.socials_scroll = ScrollArea(self.content, self.BG)
        self.socials_page = self.socials_scroll.body
        self.updates_scroll = ScrollArea(self.content, self.BG)
        self.updates_page = self.updates_scroll.body
        self.settings_scroll = ScrollArea(self.content, self.BG)
        self.settings_page = self.settings_scroll.body
        self.themes_scroll = ScrollArea(self.content, self.BG)
        self.themes_page = self.themes_scroll.body
        self.pages = {"dashboard": self.status_scroll, "bhop": self.bhop_scroll, "activity": self.activity_scroll, "socials": self.socials_scroll, "updates": self.updates_scroll, "themes": self.themes_scroll, "settings": self.settings_scroll}
        self.start_buttons = []
        self.stop_buttons = []
        self.toggle_buttons = []
        self.status_labels = []
        self._build_dashboard_page()
        self._build_bhop_page()
        self._build_activity_page()
        self._build_socials_page()
        self._build_updates_page()
        self._build_themes_page()
        self._build_settings_page()
        self.show_tab(getattr(self, "active_tab", "dashboard"))
        if announce:
            self._log("Ready. Start CS2, then click Start & Attach.")
            self._log(f"Session log: {self.session_log.name}")

    def _draw_logo(self, canvas):
        canvas.delete("all")
        canvas.configure(bg=self.LOGO_BG)
        canvas.create_polygon(17, 1, 33, 17, 17, 33, 1, 17, outline=self.LOGO_PRIMARY, fill="", width=1)
        canvas.create_line(17, 8, 17, 26, fill=self.LOGO_PRIMARY, width=1)
        canvas.create_line(8, 17, 26, 17, fill=self.LOGO_PRIMARY, width=1)
        canvas.create_oval(15, 15, 19, 19, fill=self.LOGO_ACCENT, outline="")

    def _asset_path(self, filename):
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(Path(sys._MEIPASS) / filename)
        source_directory = Path(__file__).resolve().parent
        candidates.append(source_directory / filename)
        candidates.append(source_directory.parent / "assets" / filename)
        candidates.append(Path(sys.executable).resolve().parent / filename)
        return next((path for path in candidates if path.exists()), None)

    def _configure_wallpaper(self, shell):
        if not self.BACKGROUND:
            if self.wallpaper_canvas and self.wallpaper_canvas.winfo_exists():
                self.wallpaper_canvas.place_forget()
            return
        asset = self._asset_path(self.BACKGROUND)
        if not asset:
            return
        if self.wallpaper_canvas is None or not self.wallpaper_canvas.winfo_exists():
            self.wallpaper_canvas = tk.Canvas(self, bg=self.BG, bd=0, highlightthickness=0)
            self.wallpaper_canvas.bind("<Configure>", lambda _event: self._resize_wallpaper())
        if self.wallpaper_source is None:
            try:
                self.wallpaper_source = tk.PhotoImage(file=str(asset))
                self.wallpaper_cache = {1: self.wallpaper_source}
            except tk.TclError:
                self.wallpaper_source = None
                return
        self.wallpaper_canvas.configure(bg=self.BG)
        self.wallpaper_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        # Canvas.lower() searches canvas tags; use the native window command
        # here so the wallpaper is placed beneath the shell without Tk
        # parsing the Frame name as a boolean tag expression.
        self.tk.call("lower", self.wallpaper_canvas._w, shell._w)
        self._resize_wallpaper()

    def _resize_wallpaper(self):
        if not self.wallpaper_canvas or not self.wallpaper_source or not self.wallpaper_canvas.winfo_exists():
            return
        width = max(self.wallpaper_canvas.winfo_width(), 1)
        height = max(self.wallpaper_canvas.winfo_height(), 1)
        scale = max(width / self.wallpaper_source.width(), height / self.wallpaper_source.height())
        zoom = max(1, math.ceil(scale))
        image = self.wallpaper_cache.get(zoom)
        if image is None:
            image = self.wallpaper_source.zoom(zoom, zoom)
            self.wallpaper_cache[zoom] = image
        self.wallpaper_image = image
        if self.wallpaper_image_id is None:
            self.wallpaper_image_id = self.wallpaper_canvas.create_image(width // 2, height // 2, image=image, anchor="center")
        else:
            self.wallpaper_canvas.itemconfigure(self.wallpaper_image_id, image=image)
            self.wallpaper_canvas.coords(self.wallpaper_image_id, width // 2, height // 2)

    def _add_nav_item(self, parent, tab, symbol, label, hint):
        item = tk.Frame(parent, bg=self.SIDEBAR, height=52, cursor="hand2")
        item.pack(fill="x", padx=8, pady=2)
        item.pack_propagate(False)
        indicator = tk.Frame(item, bg=self.SIDEBAR, width=2)
        indicator.pack(side="left", fill="y")
        icon = tk.Label(item, text=symbol, bg=self.SIDEBAR, fg=self.MUTED, font=("Segoe UI", 17), width=3)
        icon.pack(side="left", padx=(2, 0))
        copy = tk.Frame(item, bg=self.SIDEBAR)
        copy.pack(side="left", pady=8)
        text = tk.Label(copy, text=label, bg=self.SIDEBAR, fg=self.MUTED, font=("Segoe UI Semibold", 10))
        text.pack(anchor="w")
        subtext = tk.Label(copy, text=hint, bg=self.SIDEBAR, fg=self.SUBTLE, font=("Segoe UI", 7))
        subtext.pack(anchor="w", pady=(2, 0))
        for widget in (item, icon, copy, text, subtext):
            widget.bind("<Button-1>", lambda _event, key=tab: self.show_tab(key))
            widget.bind("<Enter>", lambda _event, key=tab: self._nav_hover(key, True))
            widget.bind("<Leave>", lambda _event, key=tab: self._nav_hover(key, False))
        self.nav_items[tab] = (item, icon, text, subtext, indicator)

    def _nav_hover(self, tab, hovered):
        if tab == getattr(self, "active_tab", None):
            return
        item, icon, text, subtext, indicator = self.nav_items[tab]
        background = self.NAV_HOVER if hovered else self.SIDEBAR
        for widget in (item, icon, text.master, text, subtext, indicator):
            widget.configure(bg=background)

    def show_tab(self, tab):
        self.active_tab = tab
        for page in self.pages.values():
            page.pack_forget()
        page = self.pages[tab]
        page.pack(fill="both", expand=True)
        labels = {"dashboard": ("CONTROL CENTER  /  OVERVIEW", "DragiWare is ready."), "bhop": ("WORKSPACE  /  BHOP", "Control deck."), "activity": ("WORKSPACE  /  ACTIVITY", "Activity stream."), "socials": ("NETWORK  /  SOCIALS", "Stay in the loop."), "updates": ("RELEASE CHANNEL  /  UPDATES", "Stay current."), "themes": ("APPEARANCE  /  THEMES", "Make DragiWare yours."), "settings": ("APPLICATION  /  SETTINGS", "Fine tune your setup.")}
        eyebrow, title = labels[tab]
        self.page_eyebrow.configure(text=eyebrow)
        self.page_title.configure(text=title)
        for key, (item, icon, text, subtext, indicator) in self.nav_items.items():
            selected = key == tab
            background = self.NAV_ACTIVE if selected else self.SIDEBAR
            foreground = self.RED if selected else self.MUTED
            item.configure(bg=background)
            indicator.configure(bg=self.RED if selected else background)
            icon.configure(bg=background, fg=foreground)
            text.configure(bg=background, fg=foreground)
            text.master.configure(bg=background)
            subtext.configure(bg=background, fg=self.ACCENT if selected else self.SUBTLE)
        current = self.status_labels[0].cget("text") if self.status_labels else "Ready to start"
        self.top_status.configure(text=f"●  {('ATTACHED' if current.startswith('Attached') else 'SEARCHING' if current.startswith('Waiting') else 'ERROR' if 'error' in current.lower() or 'lost' in current.lower() else 'READY')}")

    def _eyebrow(self, parent, text, bg=None):
        bg = bg or self.PANEL
        row = tk.Frame(parent, bg=bg)
        tk.Frame(row, bg=self.RED, width=18, height=1).pack(side="left", padx=(0, 8))
        tk.Label(row, text=text, bg=bg, fg=self.MUTED, font=("Segoe UI Semibold", 8)).pack(side="left")
        return row

    def _card_heading(self, parent, eyebrow, title, code=None):
        header = tk.Frame(parent, bg=self.PANEL)
        header.pack(fill="x")
        left = tk.Frame(header, bg=self.PANEL)
        left.pack(side="left")
        self._eyebrow(left, eyebrow).pack(anchor="w")
        tk.Label(left, text=title, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(6, 0))
        if code:
            tk.Label(header, text=code, bg=self.PANEL, fg=self.SUBTLE, font=("Cascadia Mono", 8)).pack(side="right", anchor="n")
        return header

    def _signal_row(self, parent, label, detail, value, color=None):
        row = tk.Frame(parent, bg=self.PANEL, height=44)
        row.pack(fill="x")
        row.pack_propagate(False)
        copy = tk.Frame(row, bg=self.PANEL)
        copy.pack(side="left", pady=6)
        tk.Label(copy, text=label, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(copy, text=detail, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(3, 0))
        dot_color = color or self.SUBTLE
        value_box = tk.Frame(row, bg=self.PANEL)
        value_box.pack(side="right", pady=12)
        tk.Label(value_box, text="●", bg=self.PANEL, fg=dot_color, font=("Segoe UI", 8)).pack(side="left", padx=(0, 5))
        tk.Label(value_box, text=value, bg=self.PANEL, fg=dot_color, font=("Segoe UI Semibold", 8)).pack(side="left")
        tk.Frame(parent, bg=self.LINE, height=1).pack(fill="x")

    def _state_accent(self):
        values = self.settings.snapshot()
        if self.running:
            return self.GREEN if self.status.cget("text").startswith("Attached") else self.WARNING
        return self.RED if values["enabled"] else self.MUTED

    def _build_dashboard_page(self):
        page = self.status_page
        intro = tk.Frame(page, bg=self.BG)
        intro.pack(fill="x", pady=(8, 19))
        tk.Label(intro, text="LIVE OVERVIEW", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(intro, text="A quiet command center for your next session.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 0))
        row = tk.Frame(page, bg=self.BG)
        row.pack(fill="x")
        row.grid_columnconfigure(0, weight=1, uniform="dashboard_cards")
        row.grid_columnconfigure(1, weight=1, uniform="dashboard_cards")

        hero = RoundedPanel(row, self.PANEL, height=320, radius=18, inset=20)
        hero.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        hero_top = tk.Frame(hero.body, bg=self.PANEL)
        hero_top.pack(fill="x")
        tk.Label(hero_top, text="CORE STATUS", bg=self.PANEL, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(side="left")
        self.dashboard_live = tk.Label(hero_top, text="●  IDLE", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI Semibold", 8))
        self.dashboard_live.pack(side="right")
        self.dashboard_ring = StatusRing(hero.body, width=320, height=112, colors={"accent": self.RED, "text": self.TEXT, "detail": self.MUTED})
        self.dashboard_ring.pack(fill="x", expand=True, pady=(18, 0))
        self.dashboard_ring.pulse()
        bottom = tk.Frame(hero.body, bg=self.PANEL)
        bottom.pack(fill="x", pady=(3, 0))
        self.dashboard_summary = tk.Frame(bottom, bg=self.PANEL)
        self.dashboard_summary.pack(side="left", fill="both", expand=True)
        self.dashboard_summary_stacked = None
        self.dashboard_bhop_value = tk.Label(self.dashboard_summary, text="BHOP  •  ENABLED", bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 8))
        self.dashboard_bhop_value.pack(side="left")
        self.dashboard_connection_value = tk.Label(self.dashboard_summary, text="CONNECTION  •  OFFLINE", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI Semibold", 8))
        self.dashboard_connection_value.pack(side="left", padx=(23, 0))
        self.start_button = RoundedButton(bottom, "Start & Attach", self.start, self.BLUE, self.TEXT, width=132, height=36)
        self.start_button.pack(side="right")
        bottom.bind("<Configure>", self._fit_dashboard_summary)
        self.start_buttons.append(self.start_button)

        telemetry = RoundedPanel(row, self.PANEL, height=320, radius=18, inset=20)
        telemetry.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._card_heading(telemetry.body, "TELEMETRY", "Signal check", "V / 02")
        signal_box = tk.Frame(telemetry.body, bg=self.PANEL)
        signal_box.pack(fill="x", pady=(19, 0))
        self._signal_row(signal_box, "PROCESS", "cs2.exe", "IDLE")
        self._signal_row(signal_box, "MODULE", "client.dll", "IDLE")
        self._signal_row(signal_box, "FOCUS", "Counter-Strike 2", "STANDBY")
        status_line = tk.Frame(telemetry.body, bg=self.PANEL)
        status_line.pack(fill="x", pady=(18, 0))
        tk.Label(status_line, text="›", bg=self.PANEL, fg=self.RED, font=("Cascadia Mono", 12)).pack(side="left", padx=(0, 8))
        self.dashboard_status = tk.Label(status_line, text="Ready to start", bg=self.PANEL, fg=self.MUTED, font=("Cascadia Mono", 8), anchor="w")
        self.dashboard_status.pack(side="left", fill="x", expand=True)
        self.status = self.dashboard_status
        self.status_labels.append(self.dashboard_status)

        lower = tk.Frame(page, bg=self.BG)
        lower.pack(fill="x", pady=(14, 0))
        lower.grid_columnconfigure(0, weight=1, uniform="dashboard_lower_cards")
        lower.grid_columnconfigure(1, weight=1, uniform="dashboard_lower_cards")
        command = RoundedPanel(lower, self.PANEL, height=132, radius=18, inset=20)
        command.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._card_heading(command.body, "QUICK COMMAND", "Session control")
        command_line = tk.Frame(command.body, bg=self.PANEL)
        command_line.pack(fill="x", pady=(21, 0))
        self.command_state_label = tk.Label(command_line, text="Bunny hop  /  ARMED", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI Semibold", 10))
        self.command_state_label.pack(side="left")
        self.toggle_button = ToggleSwitch(command_line, self.toggle, initial=self.settings.snapshot()["enabled"], colors={"track_on": self.RED, "track_off": self.BUTTON, "hover_on": self.ACCENT_HOVER, "hover_off": self.BUTTON_HOVER, "thumb": self.TEXT, "disabled": self.LINE})
        self.toggle_button.pack(side="right")
        self.toggle_buttons.append(self.toggle_button)
        self.toggle_hint = tk.Label(command.body, text="Use INSERT to toggle instantly.", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8))
        self.toggle_hint.pack(anchor="w", pady=(8, 0))
        self.state = tk.Label(command.body, text="BHOP  ON", bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 1))
        self.state.pack_forget()

        activity = RoundedPanel(lower, self.PANEL, height=132, radius=18, inset=20)
        activity.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._card_heading(activity.body, "LAST SIGNALS", "Activity")
        self.dashboard_log_preview = tk.Label(activity.body, text="Ready. Start CS2, then click Start & Attach.", bg=self.PANEL, fg=self.MUTED, font=("Cascadia Mono", 8), justify="left", anchor="w", wraplength=300)
        self.dashboard_log_preview.pack(fill="x", pady=(21, 0))
        tk.Label(activity.body, text="Open Activity for the complete session ledger  →", bg=self.PANEL, fg=self.SUBTLE, font=("Segoe UI", 8)).pack(anchor="w", pady=(12, 0))

    def _fit_dashboard_summary(self, _event=None):
        if not self.dashboard_summary.winfo_exists():
            return
        stacked = self.dashboard_summary.winfo_width() < 190
        if stacked == self.dashboard_summary_stacked:
            return
        self.dashboard_bhop_value.pack_forget()
        self.dashboard_connection_value.pack_forget()
        if stacked:
            self.dashboard_bhop_value.pack(anchor="w")
            self.dashboard_connection_value.pack(anchor="w", pady=(4, 0))
        else:
            self.dashboard_bhop_value.pack(side="left")
            self.dashboard_connection_value.pack(side="left", padx=(23, 0))
        self.dashboard_summary_stacked = stacked

    def _build_bhop_page(self):
        page = self.bhop_page
        intro = tk.Frame(page, bg=self.BG)
        intro.pack(fill="x", pady=(8, 18))
        tk.Label(intro, text="PRIMARY CONTROL", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(intro, text="Attach once CS2 is running, then hold SPACE in game.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 0))
        row = tk.Frame(page, bg=self.BG)
        row.pack(fill="x")
        row.grid_columnconfigure(0, weight=1, uniform="bhop_cards")
        row.grid_columnconfigure(1, weight=1, uniform="bhop_cards")
        control = RoundedPanel(row, self.PANEL, height=272, radius=18, inset=20)
        control.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        top = tk.Frame(control.body, bg=self.PANEL)
        top.pack(fill="x")
        tk.Label(top, text="BHOP  /  01", bg=self.PANEL, fg=self.SUBTLE, font=("Cascadia Mono", 8)).pack(side="left")
        self.bhop_engine_state = tk.Label(top, text="●  ARMED", bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 8))
        self.bhop_engine_state.pack(side="right")
        center = tk.Frame(control.body, bg=self.PANEL)
        center.pack(fill="both", expand=True, pady=(20, 14))
        self.bhop_center = center
        self.bhop_status = tk.Label(center, text="BHOP ENGINE\nON", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 22), justify="left")
        self.bhop_status.pack(side="left", anchor="center", padx=(6, 24))
        copy = tk.Frame(center, bg=self.PANEL)
        copy.pack(side="left", anchor="center")
        self.bhop_center_copy = copy
        self.bhop_center_stacked = None
        tk.Label(copy, text="AUTOMATION", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(copy, text="The existing V2 worker owns the session.", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8), justify="left", wraplength=168).pack(anchor="w", pady=(8, 0))
        center.bind("<Configure>", self._fit_bhop_center)
        actions = tk.Frame(control.body, bg=self.PANEL)
        actions.pack(fill="x")
        self.bhop_start_button = RoundedButton(actions, "Start & Attach", self.start, self.BLUE, self.TEXT, width=112, height=36)
        self.bhop_start_button.pack(side="left")
        self.start_buttons.append(self.bhop_start_button)
        self.bhop_toggle_button = ToggleSwitch(actions, self.toggle, initial=self.settings.snapshot()["enabled"], width=50, height=28, colors={"track_on": self.RED, "track_off": self.BUTTON, "hover_on": self.ACCENT_HOVER, "hover_off": self.BUTTON_HOVER, "thumb": self.TEXT, "disabled": self.LINE})
        self.bhop_toggle_button.pack(side="left", padx=6)
        self.toggle_buttons.append(self.bhop_toggle_button)
        self.stop_button = RoundedButton(actions, "Stop", self.stop, self.BUTTON, self.TEXT, width=64, height=36)
        self.stop_button.pack(side="left", padx=(0, 6))
        self.stop_buttons.append(self.stop_button)
        self.exit_button = RoundedButton(actions, "Exit", self.shutdown, self.BUTTON, self.TEXT, width=60, height=36)
        self.exit_button.pack(side="left")
        self.stop_button.set_enabled(False)
        connection = RoundedPanel(row, self.PANEL, height=272, radius=18, inset=20)
        connection.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._card_heading(connection.body, "SESSION STATE", "Connection", "02")
        connection_state = tk.Frame(connection.body, bg=self.PANEL)
        connection_state.pack(fill="x", pady=(30, 20))
        self.bhop_connection_dot = tk.Label(connection_state, text="●", bg=self.PANEL, fg=self.SUBTLE, font=("Segoe UI", 16))
        self.bhop_connection_dot.pack(side="left", padx=(0, 10))
        connection_copy = tk.Frame(connection_state, bg=self.PANEL)
        connection_copy.pack(side="left")
        self.bhop_connection_label = tk.Label(connection_copy, text="Ready to start", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 13))
        self.bhop_connection_label.pack(anchor="w")
        tk.Label(connection_copy, text="No active game connection", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 0))
        self._signal_row(connection.body, "PROCESS", "cs2.exe", "WAITING")
        self._signal_row(connection.body, "MODULE", "client.dll", "WAITING")

        binds = RoundedPanel(page, self.PANEL, height=132, radius=18, inset=20, autosize="height")
        binds.pack(fill="x", pady=(14, 0))
        self._card_heading(binds.body, "INPUT MAP", "Keybinds")
        key_row = tk.Frame(binds.body, bg=self.PANEL)
        key_row.pack(fill="x", pady=(18, 0))
        space = tk.Frame(key_row, bg=self.ALT, highlightthickness=1, highlightbackground=self.LINE)
        space.pack(side="left", padx=(0, 9), ipadx=14, ipady=10)
        tk.Label(space, text="SPACE", bg=self.ALT, fg=self.TEXT, font=("Cascadia Mono", 9)).pack(anchor="w")
        tk.Label(space, text="hold to jump", bg=self.ALT, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))
        toggle = tk.Frame(key_row, bg=self.ALT, highlightthickness=1, highlightbackground=self.LINE)
        toggle.pack(side="left", padx=(0, 9), ipadx=12, ipady=8)
        tk.Label(toggle, text="TOGGLE BHOP", bg=self.ALT, fg=self.MUTED, font=("Segoe UI Semibold", 7)).pack(anchor="w")
        toggle_line = tk.Frame(toggle, bg=self.ALT)
        toggle_line.pack(anchor="w", pady=(5, 0))
        self.toggle_key_label = tk.Label(toggle_line, text=self._key_name(self.toggle_key), bg=self.ALT, fg=self.TEXT, font=("Cascadia Mono", 9))
        self.toggle_key_label.pack(side="left")
        self.toggle_change_button = RoundedButton(toggle_line, "Change", lambda: self._begin_key_capture("toggle"), self.INPUT, self.TEXT, width=68, height=26)
        self.toggle_change_button.pack(side="left", padx=(11, 0))
        exit_box = tk.Frame(key_row, bg=self.ALT, highlightthickness=1, highlightbackground=self.LINE)
        exit_box.pack(side="left", ipadx=12, ipady=8)
        tk.Label(exit_box, text="EXIT SCRIPT", bg=self.ALT, fg=self.MUTED, font=("Segoe UI Semibold", 7)).pack(anchor="w")
        exit_line = tk.Frame(exit_box, bg=self.ALT)
        exit_line.pack(anchor="w", pady=(5, 0))
        self.exit_key_label = tk.Label(exit_line, text=self._key_name(self.exit_key), bg=self.ALT, fg=self.TEXT, font=("Cascadia Mono", 9))
        self.exit_key_label.pack(side="left")
        self.exit_change_button = RoundedButton(exit_line, "Change", lambda: self._begin_key_capture("exit"), self.INPUT, self.TEXT, width=68, height=26)
        self.exit_change_button.pack(side="left", padx=(11, 0))
        ToolTip(self.toggle_change_button, "Click, then press the key that should toggle bunny hop.")
        ToolTip(self.exit_change_button, "Click, then press the key that should close the script.")

    def _fit_bhop_center(self, _event=None):
        if not self.bhop_center.winfo_exists():
            return
        stacked = self.bhop_center.winfo_width() < 390
        if stacked == self.bhop_center_stacked:
            return
        self.bhop_status.pack_forget()
        self.bhop_center_copy.pack_forget()
        if stacked:
            self.bhop_status.pack(side="top", anchor="w")
            self.bhop_center_copy.pack(side="top", anchor="w", pady=(8, 0))
        else:
            self.bhop_status.pack(side="left", anchor="center", padx=(6, 24))
            self.bhop_center_copy.pack(side="left", anchor="center")
        self.bhop_center_stacked = stacked

    def _build_activity_page(self):
        page = self.activity_page
        intro = tk.Frame(page, bg=self.BG)
        intro.pack(fill="x", pady=(8, 18))
        tk.Label(intro, text="SESSION LEDGER", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(intro, text="Connection state, focus checks, and errors are saved after each launch.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 0))
        logs = RoundedPanel(page, self.PANEL, height=425, radius=18, inset=20)
        logs.pack(fill="both", expand=True)
        header = tk.Frame(logs.body, bg=self.PANEL)
        header.pack(fill="x", pady=(0, 14))
        self._eyebrow(header, "LIVE DIAGNOSTICS").pack(side="left")
        copy_button = RoundedButton(header, "Copy", self._copy_activity_log, self.BUTTON, self.TEXT, width=64, height=28)
        copy_button.pack(side="right")
        clear_button = RoundedButton(header, "Clear", self._clear_activity_log, self.INPUT, self.TEXT, width=64, height=28)
        clear_button.pack(side="right", padx=(0, 8))
        self.activity_log_status = tk.Label(header, text="0 EVENTS  •  SAVED", bg=self.PANEL, fg=self.MUTED, font=("Cascadia Mono", 8))
        self.activity_log_status.pack(side="right", padx=(0, 12))
        log_body = RoundedPanel(logs.body, self.ALT, height=322, radius=13, inset=12)
        log_body.pack(fill="both", expand=True)
        self.log = tk.Text(log_body.body, height=15, bg=self.ALT, fg=self.TEXT, insertbackground=self.TEXT, relief="flat", borderwidth=0, highlightthickness=0, font=("Cascadia Mono", 9), padx=2, pady=2, state="disabled", wrap="word")
        log_scrollbar = tk.Scrollbar(log_body.body, command=self.log.yview, bg=self.ALT, activebackground=self.PANEL, troughcolor=self.ALT, relief="flat", width=10)
        self.log.configure(yscrollcommand=log_scrollbar.set)
        self.log.tag_configure("info", foreground=self.TEXT)
        self.log.tag_configure("warning", foreground=self.WARNING)
        self.log.tag_configure("error", foreground=self.RED)
        self.log.tag_configure("success", foreground=self.GREEN)
        self.log.pack(side="left", fill="both", expand=True)
        log_scrollbar.pack(side="right", fill="y", padx=(8, 0))
        self._activity_line_count = 0
        changes = RoundedPanel(page, self.PANEL, height=100, radius=18, inset=20, autosize="height")
        changes.pack(fill="x", pady=(14, 0))
        self._eyebrow(changes.body, f"RELEASE NOTES  •  V{APP_DISPLAY_VERSION}").pack(anchor="w")
        tk.Label(changes.body, text=CHANGELOG[0][1], bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 9), justify="left", wraplength=700).pack(anchor="w", pady=(10, 0))
        tk.Label(changes.body, text="Previous: " + "  •  ".join(version for version, _note in CHANGELOG[1:]), bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 0))

    def _build_socials_page(self):
        page = self.socials_page
        intro = tk.Frame(page, bg=self.BG)
        intro.pack(fill="x", pady=(8, 24))
        tk.Label(intro, text="NETWORK", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(intro, text="Open the official project page or report an issue.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 0))
        cards = tk.Frame(page, bg=self.BG)
        cards.pack(fill="x")
        self._social_card(cards, "GH", "SOURCE / RELEASES", "GitHub", "View the official source and published releases.", "github.com/yxzroot/cs2-bhop", "https://github.com/yxzroot/cs2-bhop").pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._social_card(cards, "!", "SUPPORT / ISSUES", "Report an issue", "Include a copied Activity log when reporting a problem.", "github.com/yxzroot/cs2-bhop/issues", "https://github.com/yxzroot/cs2-bhop/issues").pack(side="left", fill="both", expand=True, padx=(8, 0))
        note = RoundedPanel(page, self.PANEL, height=84, radius=16, inset=18, autosize="height")
        note.pack(fill="x", pady=(18, 0))
        tk.Label(note.body, text="●", bg=self.PANEL, fg=self.GREEN, font=("Segoe UI", 12)).pack(side="left", padx=(0, 10))
        copy = tk.Frame(note.body, bg=self.PANEL)
        copy.pack(side="left")
        tk.Label(copy, text="Need help?", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(copy, text="Share the latest Activity log when reporting an issue.", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 0))

    def _social_card(self, parent, mark, eyebrow, title, description, url_text, url):
        card = tk.Frame(parent, bg=self.PANEL, highlightthickness=1, highlightbackground=self.LINE, cursor="hand2", height=245)
        card.pack_propagate(False)
        top = tk.Frame(card, bg=self.PANEL)
        top.pack(fill="x", padx=20, pady=(20, 0))
        logo = tk.Label(top, text=mark, bg=self.ALT, fg=self.RED if mark == "GH" else self.BLUE, font=("Segoe UI Semibold", 10), width=4, height=2)
        logo.pack(side="left")
        tk.Label(top, text="↗", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 13)).pack(side="right")
        copy = tk.Frame(card, bg=self.PANEL)
        copy.pack(fill="both", expand=True, padx=20, pady=(28, 0))
        tk.Label(copy, text=eyebrow, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI Semibold", 7)).pack(anchor="w")
        tk.Label(copy, text=title, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 22)).pack(anchor="w", pady=(8, 0))
        tk.Label(copy, text=description, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8), justify="left", wraplength=260).pack(anchor="w", pady=(8, 0))
        link = tk.Label(card, text=url_text + "   →", bg=self.PANEL, fg=self.RED, font=("Cascadia Mono", 8), anchor="w")
        link.pack(fill="x", padx=20, pady=(0, 18))
        for widget in (card, top, logo, copy, link):
            widget.bind("<Button-1>", lambda _event, address=url: webbrowser.open(address))
            widget.bind("<Enter>", lambda _event, target=card: target.configure(highlightbackground=self.RED))
            widget.bind("<Leave>", lambda _event, target=card: target.configure(highlightbackground=self.LINE))
        return card

    def _build_updates_page(self):
        page = self.updates_page
        intro = tk.Frame(page, bg=self.BG)
        intro.pack(fill="x", pady=(8, 18))
        tk.Label(intro, text="RELEASE CHANNEL", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(intro, text="Updates are checked from the official release source and always require your approval.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 0))

        row = tk.Frame(page, bg=self.BG)
        row.pack(fill="x")
        row.grid_columnconfigure(0, weight=3, uniform="update_cards")
        row.grid_columnconfigure(1, weight=2, uniform="update_cards")

        updates = RoundedPanel(row, self.PANEL, height=220, radius=18, inset=20)
        updates.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._card_heading(updates.body, "UPDATE CHANNEL", "Stable releases", "SAFE")
        update_meta = tk.Frame(updates.body, bg=self.PANEL)
        update_meta.pack(fill="x", pady=(16, 0))
        self.update_installed_label = tk.Label(update_meta, text=f"Installed  /  v{APP_DISPLAY_VERSION}", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 9))
        self.update_installed_label.pack(side="left")
        self.update_latest_label = tk.Label(update_meta, text="Latest stable  /  checking…", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8))
        self.update_latest_label.pack(side="right")
        self.update_status_label = tk.Label(updates.body, text="●  Stable channel only", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8), anchor="w")
        self.update_status_label.pack(fill="x", pady=(12, 0))
        update_controls = tk.Frame(updates.body, bg=self.PANEL)
        update_controls.pack(fill="x", pady=(14, 0))
        update_options = tk.Frame(update_controls, bg=self.PANEL)
        update_options.pack(side="left")
        self.auto_check_var = tk.BooleanVar(value=self.settings.snapshot().get("auto_check_updates", True))
        tk.Checkbutton(update_options, text="Check automatically", variable=self.auto_check_var, command=self._toggle_auto_check, bg=self.PANEL, fg=self.TEXT, selectcolor=self.INPUT, activebackground=self.PANEL, activeforeground=self.TEXT, font=("Segoe UI", 8), relief="flat", highlightthickness=0).pack(anchor="w")
        self.update_button = RoundedButton(update_controls, "Check for Updates", self._update_action, self.BUTTON, self.TEXT, width=140, height=32)
        self.update_button.pack(side="right")

        channel = RoundedPanel(row, self.PANEL, height=220, radius=18, inset=20)
        channel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._card_heading(channel.body, "CURRENT BUILD", "DragiWare channel", BUILD_CHANNEL.upper())
        tk.Label(channel.body, text=f"v{APP_DISPLAY_VERSION}", bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 26)).pack(anchor="w", pady=(24, 0))
        tk.Label(channel.body, text=f"{BUILD_CHANNEL} channel  /  {BUILD_TYPE} build", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(3, 0))
        tk.Label(channel.body, text=f"Build identity: {BUILD_FLAVOR}\nDownloads and restarts happen only after you confirm them.", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8), justify="left", wraplength=260).pack(anchor="w", pady=(12, 0))

    def _build_themes_page_legacy(self):
        page = self.themes_page
        page.configure(bg=self.BG)
        self._card_heading(page, "THEME LIBRARY", "Choose your DragiWare look.", "LIVE PREVIEW").pack(fill="x", pady=(8, 18))
        preview = tk.Frame(page, bg=self.PANEL, highlightthickness=1, highlightbackground=self.LINE)
        preview.pack(fill="x", pady=(0, 18))
        preview_left = tk.Frame(preview, bg=self.PANEL)
        preview_left.pack(side="left", fill="both", expand=True, padx=18, pady=16)
        tk.Label(preview_left, text="LIVE PREVIEW", bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(preview_left, text=f"{self.theme_name} theme", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 13)).pack(anchor="w", pady=(4, 10))
        preview_ui = tk.Frame(preview_left, bg=self.BG, highlightthickness=1, highlightbackground=self.LINE)
        preview_ui.pack(fill="x")
        header = tk.Frame(preview_ui, bg=self.SIDEBAR, height=34)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="dragiware", bg=self.SIDEBAR, fg=self.LOGO_PRIMARY, font=("Segoe UI Semibold", 10)).pack(side="left", padx=12)
        tk.Label(header, text="THEMES  •  SETTINGS  •  BHOP", bg=self.SIDEBAR, fg=self.MUTED, font=("Segoe UI", 8)).pack(side="right", padx=12)
        preview_body = tk.Frame(preview_ui, bg=self.BG)
        preview_body.pack(fill="x", padx=10, pady=10)
        panel = tk.Frame(preview_body, bg=self.PANEL, highlightthickness=1, highlightbackground=self.LINE)
        panel.pack(fill="x")
        tk.Label(panel, text="Session control", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=12, pady=(10, 2))
        tk.Label(panel, text="Ready to start  •  live theme rendering", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=12)
        actions = tk.Frame(panel, bg=self.PANEL)
        actions.pack(anchor="w", padx=12, pady=10)
        tk.Button(actions, text="ACTIVE", bg=self.RED, fg=self.TEXT, activebackground=self.ACCENT_HOVER, relief="flat", bd=0, padx=12, pady=4).pack(side="left")
        tk.Button(actions, text="Secondary", bg=self.BUTTON, fg=self.TEXT, activebackground=self.BUTTON_HOVER, relief="flat", bd=0, padx=12, pady=4).pack(side="left", padx=8)
        tk.Label(actions, text="Accent / selected / border states", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(side="left")
        tk.Label(page, text="THEMES", bg=self.BG, fg=self.MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(0, 8))
        grid = tk.Frame(page, bg=self.BG)
        grid.pack(fill="x")
        for index, name in enumerate(THEME_NAMES):
            palette = THEMES[name]
            card = tk.Frame(grid, bg=self.PANEL, highlightthickness=2 if name == self.theme_name else 1,
                            highlightbackground=self.RED if name == self.theme_name else self.LINE, cursor="hand2")
            card.grid(row=index // 4, column=index % 4, sticky="nsew", padx=(0 if index % 4 == 0 else 6, 6), pady=(0, 10))
            grid.grid_columnconfigure(index % 4, weight=1)
            title = f"{name}  ✓" if name == self.theme_name else name
            tk.Label(card, text=title, bg=self.PANEL, fg=self.RED if name == self.theme_name else self.TEXT,
                     font=("Segoe UI Semibold", 10), anchor="w").pack(fill="x", padx=10, pady=(10, 7))
            swatches = tk.Frame(card, bg=self.PANEL)
            swatches.pack(fill="x", padx=10)
            for key, label in (("accent", "A"), ("blue", "S"), ("bg", "B"), ("text", "T")):
                swatch = tk.Frame(swatches, bg=palette[key], width=22, height=22,
                                  highlightthickness=1, highlightbackground=palette["line"])
                swatch.pack(side="left", padx=(0, 5))
                swatch.pack_propagate(False)
                tk.Label(swatch, text=label, bg=palette[key], fg=palette["text"], font=("Segoe UI", 7)).pack(expand=True)
            tk.Label(card, text="Accent  •  Secondary  •  Background  •  Text", bg=self.PANEL,
                     fg=self.MUTED, font=("Segoe UI", 7), anchor="w").pack(fill="x", padx=10, pady=(7, 10))
            self._bind_theme_card(card, name)
        background = tk.Frame(page, bg=self.PANEL, highlightthickness=1, highlightbackground=self.LINE)
        background.pack(fill="x", pady=(8, 16))
        tk.Label(background, text="BACKGROUND", bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=16, pady=(14, 3))
        background_name = THEMES[self.theme_name].get("background")
        message = f"Artwork: {background_name}" if background_name else "No background artwork is enabled for this theme."
        tk.Label(background, text=message, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(0, 14))

    def _bind_theme_card(self, widget, name):
        widget.bind("<Button-1>", lambda _event, theme=name: self._apply_theme(theme))
        for child in widget.winfo_children():
            self._bind_theme_card(child, name)

    def _build_themes_page(self):
        page = self.themes_page
        page.configure(bg=self.BG)
        self._theme_preview_canvases = []
        intro = tk.Frame(page, bg=self.BG)
        intro.pack(fill="x", pady=(8, 16))
        intro_header = tk.Frame(intro, bg=self.BG)
        intro_header.pack(fill="x")
        tk.Label(intro_header, text="APPEARANCE", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(side="left")
        tk.Label(
            intro_header,
            text="these are vibecoded dont be mad at me!!!",
            bg=self.BG,
            fg=self.MUTED,
            font=("Segoe UI", 8),
        ).pack(side="right")
        tk.Label(intro, text="Themes", bg=self.BG, fg=self.TEXT, font=("Segoe UI", 22)).pack(anchor="w", pady=(4, 0))
        tk.Label(intro, text="Pick a palette and see it in context before you apply it.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(5, 0))

        preview = RoundedPanel(page, self.PANEL, height=174, radius=18, inset=18)
        preview.pack(fill="x", pady=(0, 20))
        self._card_heading(preview.body, "PREVIEW", f"{self.theme_name} theme", "LIVE").pack(fill="x")
        preview_ui = tk.Canvas(preview.body, height=82, bg=self.BG, bd=0, highlightthickness=0)
        preview_ui.pack(fill="x", pady=(14, 0))
        self._theme_preview_canvases.append(preview_ui)
        preview_ui.bind("<Configure>", lambda _event: self._draw_theme_preview(preview_ui))
        self._draw_theme_preview(preview_ui)

        section = tk.Frame(page, bg=self.BG)
        section.pack(fill="x", pady=(0, 10))
        tk.Label(section, text="THEMES", bg=self.BG, fg=self.TEXT, font=("Segoe UI Semibold", 11)).pack(side="left")
        tk.Label(section, text=f"{len(THEME_NAMES)} palettes", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 8)).pack(side="right", pady=2)
        grid = tk.Frame(page, bg=self.BG)
        grid.pack(fill="x")
        for index, name in enumerate(THEME_NAMES):
            palette = THEMES[name]
            card = ThemeCard(grid, name, palette, name == self.theme_name, self._apply_theme)
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=(0 if index % 3 == 0 else 6, 6), pady=(0, 12))
            grid.grid_columnconfigure(index % 3, weight=1)
            title = f"{name}  •  ACTIVE" if name == self.theme_name else name
            card_body_bg = palette["alt"] if name == self.theme_name else palette["panel"]
            tk.Label(card.body, text=title, bg=card_body_bg, fg=palette["accent"] if name == self.theme_name else palette["text"], font=("Segoe UI Semibold", 10), anchor="w").pack(fill="x")
            mini = tk.Canvas(card.body, height=58, bg=card_body_bg, bd=0, highlightthickness=0)
            mini.pack(fill="x", pady=(9, 8))
            mini.bind("<Configure>", lambda _event, canvas=mini, colors=palette: self._draw_mini_theme(canvas, colors))
            self._draw_mini_theme(mini, palette)
            tk.Label(card.body, text="Accent  ·  Secondary  ·  Background  ·  Text", bg=card_body_bg, fg=palette["muted"], font=("Segoe UI", 7), anchor="w").pack(fill="x")
            self._bind_theme_card(card, name)

        background = RoundedPanel(page, self.PANEL, height=76, radius=18, inset=18, autosize="height")
        background.pack(fill="x", pady=(8, 18))
        self._card_heading(background.body, "BACKGROUND", "Theme artwork", "OPTIONAL").pack(fill="x")
        background_name = THEMES[self.theme_name].get("background")
        message = f"{background_name} is active for this theme." if background_name else "This theme uses a clean solid background."
        tk.Label(background.body, text=message, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(10, 0))

    def _draw_mini_theme(self, canvas, palette):
        if not canvas.winfo_exists():
            return
        width, height = max(canvas.winfo_width(), 2), max(canvas.winfo_height(), 2)
        canvas.delete("all")
        canvas.create_rectangle(1, 1, width - 1, height - 1, fill=palette["bg"], outline=palette["line"])
        canvas.create_rectangle(9, 9, width - 9, height - 9, fill=palette["panel"], outline="")
        canvas.create_rectangle(18, 18, max(19, width * 0.42), 22, fill=palette["accent"], outline="")
        canvas.create_rectangle(18, 29, max(19, width * 0.68), 32, fill=palette["muted"], outline="")
        canvas.create_rectangle(width - 64, height - 21, width - 18, height - 12, fill=palette["accent"], outline="")

    def _draw_theme_preview(self, canvas):
        if not canvas.winfo_exists():
            return
        palette = getattr(self, "_display_palette", THEMES[self.theme_name])
        width, height = max(canvas.winfo_width(), 2), max(canvas.winfo_height(), 2)
        canvas.delete("all")
        canvas.create_rectangle(1, 1, width - 1, height - 1, fill=palette["bg"], outline=palette["line"])
        canvas.create_rectangle(12, 12, width - 12, height - 12, fill=palette["panel"], outline="")
        canvas.create_rectangle(25, 24, max(26, width * 0.28), 29, fill=palette["logo_primary"], outline="")
        canvas.create_rectangle(25, 40, max(26, width * 0.52), 44, fill=palette["text"], outline="")
        canvas.create_rectangle(25, 52, max(26, width * 0.43), 55, fill=palette["muted"], outline="")
        canvas.create_rectangle(width - 128, 31, width - 76, 49, fill=palette["button"], outline="")
        canvas.create_rectangle(width - 68, 31, width - 22, 49, fill=palette["accent"], outline="")

    def _build_settings_page(self):
        page = self.settings_page
        intro = tk.Frame(page, bg=self.BG)
        intro.pack(fill="x", pady=(8, 18))
        tk.Label(intro, text="APPLICATION PREFERENCES", bg=self.BG, fg=self.SUBTLE, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(intro, text="Only the preferences supported by the original V2 runtime live here.", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 0))
        panel = RoundedPanel(page, self.PANEL, height=315, radius=18, inset=22)
        panel.pack(fill="x")
        self._card_heading(panel.body, "BEHAVIOR", "Runtime options", "V2")
        menu_row = tk.Frame(panel.body, bg=self.PANEL)
        menu_row.pack(fill="x", pady=(21, 0))
        menu_copy = tk.Frame(menu_row, bg=self.PANEL)
        menu_copy.pack(side="left")
        tk.Label(menu_copy, text="Menu Toggle Keybind", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(menu_copy, text="Open or hide the menu without stopping DragiWare.", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 0))
        self.menu_toggle_key_label = tk.Label(menu_row, text=self._key_name(self.menu_toggle_key, self.menu_toggle_modifiers), bg=self.INPUT, fg=self.RED, font=("Cascadia Mono", 9), padx=12, pady=6)
        self.menu_toggle_key_label.pack(side="left", padx=(0, 8))
        self.menu_toggle_change_button = RoundedButton(menu_row, "Change", lambda: self._begin_key_capture("menu"), self.INPUT, self.TEXT, width=68, height=28)
        self.menu_toggle_change_button.pack(side="right")
        ToolTip(self.menu_toggle_change_button, "Click, then press a keyboard key or combination; mouse buttons are not supported.")
        tk.Frame(panel.body, bg=self.LINE, height=1).pack(fill="x", pady=(18, 0))
        options = tk.Frame(panel.body, bg=self.PANEL)
        options.pack(fill="x", pady=(17, 0))
        preset_copy = tk.Frame(options, bg=self.PANEL)
        preset_copy.pack(side="left")
        tk.Label(preset_copy, text="Keybind preset", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(preset_copy, text="Apply a known V2 keyboard layout.", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 0))
        self.preset_var = tk.StringVar(value=self.settings.snapshot()["preset"])
        preset_menu = tk.OptionMenu(options, self.preset_var, "Custom", *PRESETS.keys(), command=self._apply_preset)
        preset_menu.configure(bg=self.INPUT, fg=self.TEXT, activebackground=self.BUTTON_HOVER, activeforeground=self.TEXT, highlightthickness=0, bd=0, font=("Segoe UI Semibold", 9), width=14)
        preset_menu["menu"].configure(bg=self.PANEL, fg=self.TEXT, activebackground=self.NAV_ACTIVE, activeforeground=self.TEXT, font=("Segoe UI", 9))
        preset_menu.pack(side="right")
        tk.Frame(panel.body, bg=self.LINE, height=1).pack(fill="x", pady=(20, 0))
        debug_row = tk.Frame(panel.body, bg=self.PANEL)
        debug_row.pack(fill="x", pady=(14, 0))
        tk.Label(debug_row, text="Debug logging", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 10)).pack(side="left")
        self.debug_button = ToggleSwitch(debug_row, self._toggle_debug_mode, initial=self.settings.snapshot()["debug_mode"], colors={"track_on": self.RED, "track_off": self.BUTTON, "hover_on": self.ACCENT_HOVER, "hover_off": self.BUTTON_HOVER, "thumb": self.TEXT, "disabled": self.LINE})
        self.debug_button.pack(side="right")
        self.reset_button = RoundedButton(panel.body, "Reset defaults", self._reset_defaults, self.BUTTON, self.TEXT, width=124, height=30)
        self.reset_button.pack(anchor="w", pady=(19, 0))
        ToolTip(self.debug_button, "Write additional focus and error details to the Activity log.")
        ToolTip(self.reset_button, "Restore the original Insert / End keybinds and standard settings.")

        appearance = RoundedPanel(page, self.PANEL, height=112, radius=18, inset=20, autosize="height")
        appearance.pack(fill="x", pady=(14, 0))
        self._card_heading(appearance.body, "BACKGROUND", "Theme artwork", "OPTIONAL")
        background_name = THEMES[self.theme_name].get("background")
        background_text = f"{background_name} is active for this theme." if background_name else "This theme uses a clean solid background."
        tk.Label(appearance.body, text=background_text, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(16, 0))

        guide = RoundedPanel(page, self.PANEL, height=132, radius=18, inset=20, autosize="height")
        guide.pack(fill="x", pady=(14, 0))
        self._card_heading(guide.body, "INPUT MAP", "Keybinds live on the control deck")
        tk.Label(guide.body, text="SPACE  /  hold to jump\nINSERT  /  toggle bhop\nEND  /  exit script", bg=self.PANEL, fg=self.MUTED, font=("Cascadia Mono", 9), justify="left").pack(anchor="w", pady=(16, 0))

    def _startup_update_check(self):
        if self.closing or not self.settings.snapshot().get("auto_check_updates", True):
            return
        self._check_for_updates(silent=True)

    def _check_for_updates(self, silent=False):
        if self.update_check_running:
            return
        self.update_check_running = True
        if hasattr(self, "update_status_label"):
            self.update_status_label.configure(text="●  Checking official stable releases…", fg=self.MUTED)
            self.update_button.set_enabled(False)
        self.update_thread = threading.Thread(target=self._check_updates_worker, args=(silent,), name="DragiWareUpdater", daemon=True)
        self.update_thread.start()

    def _check_updates_worker(self, silent):
        error = None
        release = None
        try:
            release = self.updater.check_latest()
        except Exception as exc:
            error = str(exc)
        try:
            self.after(0, self._finish_update_check, release, error, silent)
        except tk.TclError:
            pass

    def _finish_update_check(self, release, error, silent=False):
        self.update_check_running = False
        if error:
            if hasattr(self, "update_status_label"):
                self.update_status_label.configure(text="●  Update server unavailable — your install is safe.", fg=self.WARNING)
                self.update_button.set_enabled(True)
            self._log(f"Stable update check failed: {error}")
            return
        self.update_info = release
        if not release:
            if hasattr(self, "update_status_label"):
                self.update_status_label.configure(text="●  You're up to date", fg=self.GREEN)
                self.update_latest_label.configure(text="Latest stable  /  no newer release")
                self.update_button.set_enabled(True)
                self.update_button.set_text("Check Again")
            if not silent:
                self._log("No newer stable release found.")
            return
        self.update_latest_label.configure(text=f"Latest stable  /  v{release.version}")
        self.update_status_label.configure(text="●  Stable update available", fg=self.ACCENT_HOVER)
        self.update_button.set_enabled(True)
        self.update_button.set_text("Download Update")
        self._log(f"Stable update available: v{release.version}.")
        if not getattr(sys, "frozen", False):
            self.update_status_label.configure(text="●  Stable update available — packaged release required", fg=self.WARNING)
            self._log("Update download skipped while running from source.")
            return
        self._show_update_available_dialog(release)

    def _download_update(self):
        if not self.update_info or (self.update_thread and self.update_thread.is_alive()):
            return
        self.update_button.set_enabled(False)
        self.update_button.set_text("Downloading…")
        self.update_thread = threading.Thread(target=self._download_update_worker, args=(self.update_info,), name="DragiWareUpdateDownload", daemon=True)
        self.update_thread.start()

    def _download_update_worker(self, release):
        error = None
        path = None
        try:
            path = self.updater.download(release, lambda received, total: self._queue_update_progress(received, total))
        except Exception as exc:
            error = str(exc)
        try:
            self.after(0, self._finish_update_download, path, error)
        except tk.TclError:
            pass

    def _queue_update_progress(self, received, total):
        try:
            self.after(0, self._update_progress, received, total)
        except tk.TclError:
            pass

    def _update_progress(self, received, total):
        if not hasattr(self, "update_status_label"):
            return
        if total:
            self.update_status_label.configure(text=f"●  Downloading stable update… {received / total:.0%}", fg=self.ACCENT_HOVER)
        else:
            self.update_status_label.configure(text="●  Downloading stable update…", fg=self.ACCENT_HOVER)

    def _finish_update_download(self, path, error):
        if error:
            self.update_downloaded = None
            self.update_status_label.configure(text="●  Update failed — current installation is unchanged.", fg=self.DANGER)
            self.update_button.set_enabled(True)
            self.update_button.set_text("Retry Download")
            self._log(f"Stable update download failed: {error}")
            return
        self.update_downloaded = path
        self.update_status_label.configure(text="●  Stable update downloaded and verified", fg=self.GREEN)
        self.update_button.set_enabled(True)
        self.update_button.set_text("Install Update")
        self._log(f"Stable update v{self.update_info.version} downloaded and verified.")
        self._show_update_install_dialog()

    def _update_action(self):
        if self.update_check_running:
            return
        if self.update_downloaded:
            if not getattr(sys, "frozen", False):
                messagebox.showinfo("Packaged release required", "Updates can be installed from the packaged DragiWare.exe release.", parent=self)
                self._log("Update install skipped while running from source.")
                return
            self._show_update_install_dialog()
            return
        if self.update_info:
            self._show_update_available_dialog(self.update_info)
        else:
            self._check_for_updates()

    def _show_update_available_dialog(self, release):
        self._show_update_dialog(
            eyebrow="BUILD UPDATED!",
            title="A new version is available.",
            detail=f"Version {release.version} is ready from the official release channel. Would you like to download it?",
            confirm_text="Yes",
            cancel_text="No",
            on_confirm=self._download_update,
            on_cancel=lambda: self._decline_update_download(release),
        )

    def _show_update_install_dialog(self):
        self._show_update_dialog(
            eyebrow="DOWNLOAD VERIFIED",
            title="Install the update now?",
            detail="DragiWare will close, replace its packaged executable, and restart only if you confirm.",
            confirm_text="Yes",
            cancel_text="No",
            on_confirm=self._install_downloaded_update,
            on_cancel=lambda: self._log("Verified update kept for manual installation later."),
        )

    def _show_update_dialog(self, eyebrow, title, detail, confirm_text, cancel_text, on_confirm, on_cancel):
        if self.update_dialog and self.update_dialog.winfo_exists():
            self.update_dialog.lift()
            self.update_dialog.focus_force()
            return

        dialog = tk.Toplevel(self)
        self.update_dialog = dialog
        dialog.title("DragiWare update")
        dialog.configure(bg=self.BG)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.geometry("440x228")

        panel = RoundedPanel(dialog, self.PANEL, height=228, radius=18, inset=22)
        panel.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(panel.body, text=eyebrow, bg=self.PANEL, fg=self.RED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(panel.body, text=title, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 17)).pack(anchor="w", pady=(8, 0))
        tk.Label(panel.body, text=detail, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9), justify="left", anchor="w", wraplength=385).pack(anchor="w", pady=(10, 0))
        actions = tk.Frame(panel.body, bg=self.PANEL)
        actions.pack(anchor="e", pady=(20, 0))

        def dismiss(callback):
            if dialog.winfo_exists():
                dialog.grab_release()
                dialog.destroy()
            self.update_dialog = None
            callback()

        no_button = RoundedButton(actions, cancel_text, lambda: dismiss(on_cancel), self.BUTTON, self.TEXT, width=84, height=32)
        no_button.pack(side="right")
        yes_button = RoundedButton(actions, confirm_text, lambda: dismiss(on_confirm), self.RED, self.TEXT, width=84, height=32)
        yes_button.pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", lambda: dismiss(on_cancel))
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        no_button.focus_set()

    def _decline_update_download(self, release):
        self.update_status_label.configure(text="●  Update available — download skipped", fg=self.MUTED)
        self.update_button.set_text("Download Update")
        self._log(f"Stable update v{release.version} was not downloaded.")

    def _install_downloaded_update(self):
        try:
            self.updater.install_after_exit(self.update_downloaded, self.shutdown)
        except Exception as error:
            self.update_status_label.configure(text="●  Update failed — current installation is unchanged.", fg=self.DANGER)
            self._log(f"Stable update install failed: {error}")

    def _toggle_auto_check(self):
        enabled = bool(self.auto_check_var.get())
        self.settings.update(auto_check_updates=enabled)
        self._log(f"Automatic stable update checks turned {'on' if enabled else 'off'}.")

    @staticmethod
    def _key_name(key, modifiers=0):
        common_names = {0x2D: "INSERT", 0x23: "END", 0x24: "HOME", 0x2E: "DELETE", 0x20: "SPACE"}
        if key in common_names:
            name = common_names[key]
        elif 0x70 <= key <= 0x87:
            name = f"F{key - 0x6F}"
        elif 0x30 <= key <= 0x39 or 0x41 <= key <= 0x5A:
            name = chr(key)
        else:
            name = f"KEY {key}"
        prefixes = []
        if modifiers & MOD_CONTROL:
            prefixes.append("CTRL")
        if modifiers & MOD_ALT:
            prefixes.append("ALT")
        if modifiers & MOD_SHIFT:
            prefixes.append("SHIFT")
        if modifiers & MOD_WIN:
            prefixes.append("WIN")
        return "+".join(prefixes + [name])

    @staticmethod
    def _event_modifiers(event):
        modifiers = 0
        if event.state & 0x0004:
            modifiers |= MOD_CONTROL
        if event.state & 0x0008:
            modifiers |= MOD_ALT
        if event.state & 0x0001:
            modifiers |= MOD_SHIFT
        if event.state & 0x0040:
            modifiers |= MOD_WIN
        return modifiers

    def _begin_key_capture(self, setting):
        self.capturing_keybind = setting
        button = {
            "toggle": self.toggle_change_button,
            "exit": self.exit_change_button,
            "menu": self.menu_toggle_change_button,
        }[setting]
        button.set_text("Press key")
        self.focus_force()

    def _capture_keybind(self, event):
        if not self.capturing_keybind:
            return
        setting = self.capturing_keybind
        button = {
            "toggle": self.toggle_change_button,
            "exit": self.exit_change_button,
            "menu": self.menu_toggle_change_button,
        }[setting]
        if event.keysym == "Escape":
            button.set_text("Change")
            self.capturing_keybind = None
            return "break"
        if event.keysym in {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Win_L", "Win_R"}:
            return "break"
        key = event.keycode
        if not key:
            return "break"
        modifiers = self._event_modifiers(event)
        values = self.settings.snapshot()
        if setting == "menu" and key in {values["toggle_key"], values["exit_key"]}:
            self._log("Menu key conflicts with an existing BHOP or exit key.")
            return "break"
        if setting != "menu" and key == values["menu_toggle_key"]:
            self._log("BHOP or exit key conflicts with the menu toggle key.")
            return "break"
        if setting == "toggle":
            self.settings.update(toggle_key=key, preset="Custom")
            self._log(f"Toggle key changed to {self._key_name(key)}.")
        elif setting == "exit":
            self.settings.update(exit_key=key, preset="Custom")
            self._log(f"Exit key changed to {self._key_name(key)}.")
        else:
            self.settings.update(menu_toggle_key=key, menu_toggle_modifiers=modifiers, preset="Custom")
            self.menu_toggle_key, self.menu_toggle_modifiers = key, modifiers
            self._restart_menu_hotkey()
            self._log(f"Menu toggle key changed to {self._key_name(key, modifiers)}.")
        self._sync_settings_to_ui()
        button.set_text("Change")
        self.capturing_keybind = None
        return "break"

    def _sync_settings_to_ui(self):
        values = self.settings.snapshot()
        self.toggle_key, self.exit_key = values["toggle_key"], values["exit_key"]
        self.menu_toggle_key, self.menu_toggle_modifiers = values["menu_toggle_key"], values["menu_toggle_modifiers"]
        self.toggle_key_label.configure(text=self._key_name(self.toggle_key))
        self.exit_key_label.configure(text=self._key_name(self.exit_key))
        self.menu_toggle_key_label.configure(text=self._key_name(self.menu_toggle_key, self.menu_toggle_modifiers))
        self.menu_hint.configure(text=f"{self._key_name(self.menu_toggle_key, self.menu_toggle_modifiers)}  /  MENU")
        self.toggle_hint.configure(text=f"Use {self._key_name(self.toggle_key)} to toggle instantly.")
        self._refresh_enabled(values["enabled"])
        self.debug_button.set_text("Debug: On" if values["debug_mode"] else "Debug: Off")
        preset = values["preset"] if values["preset"] in PRESETS or values["preset"] == "Custom" else "Custom"
        self.preset_var.set(preset)
        if hasattr(self, "theme_var"):
            self.theme_var.set(self.theme_name)
        if hasattr(self, "auto_check_var"):
            self.auto_check_var.set(values.get("auto_check_updates", True))

    def _apply_preset(self, name):
        if name == "Custom":
            self.settings.update(preset="Custom")
            return
        try:
            self.settings.apply_preset(name)
            self._sync_settings_to_ui()
            self._log(f"Applied {name} keybind preset.")
        except (OSError, ValueError) as error:
            self._log(f"Could not apply preset: {error}")

    def _toggle_debug_mode(self):
        enabled = not self.settings.snapshot()["debug_mode"]
        self.settings.update(debug_mode=enabled)
        self._sync_settings_to_ui()
        self._log(f"Debug logging turned {'on' if enabled else 'off'}.")

    def _reset_defaults(self):
        values = self.settings.reset()
        self.theme_manager = ThemeManager(values.get("theme", "Dark"))
        self._apply_palette()
        self._rebuild_for_theme()
        self._log("Settings reset to defaults.")

    def _copy_activity_log(self):
        content = self.log.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(content)
        self._log("[CONSOLE] Diagnostics copied to the clipboard.")

    def _clear_activity_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._activity_line_count = 0
        self.activity_log_status.configure(text="0 EVENTS  •  SAVED")
        self._log("[CONSOLE] Live view cleared; the session log was retained.")

    def _log_style(self, message):
        upper = str(message).upper()
        if "[FATAL]" in upper or "[TRACE]" in upper or "[ACTION]" in upper or "[LIVENESS]" in upper or "ERROR" in upper:
            return "error"
        if "[FOCUS]" in upper or "[ATTACH]" in upper or "[MODULE]" in upper:
            return "success"
        if "[IDLE]" in upper or "WAITING" in upper or "SKIPPED" in upper:
            return "warning"
        return "info"

    def _log(self, message):
        line = f"[{time.strftime('%H:%M:%S')}]  {message}"
        self.log.configure(state="normal")
        self.log.insert("end", f"{line}\n", self._log_style(message))
        self._activity_line_count += 1
        while self._activity_line_count > 600:
            self.log.delete("1.0", "2.0")
            self._activity_line_count -= 1
        self.log.see("end")
        self.log.configure(state="disabled")
        if hasattr(self, "activity_log_status"):
            self.activity_log_status.configure(text=f"{self._activity_line_count} EVENTS  •  SAVED")
        try:
            with self.session_log.open("a", encoding="utf-8") as log_file:
                log_file.write(f"{line}\n")
        except OSError:
            pass
        if hasattr(self, "dashboard_log_preview"):
            self.dashboard_log_preview.configure(text=message)

    def _set_status(self, text, color):
        colors = {"success": self.GREEN, "waiting": self.WARNING, "error": self.RED, "idle": self.MUTED}
        color = colors.get(color, color)
        for label in getattr(self, "status_labels", []):
            label.configure(text=text, foreground=color)
        self.top_status.configure(text=f"●  {'ATTACHED' if text.startswith('Attached') else 'SEARCHING' if text.startswith('Waiting') else 'ERROR' if color == self.RED else 'READY'}", fg=color)
        if hasattr(self, "dashboard_live"):
            self.dashboard_live.configure(text=f"●  {'ACTIVE' if text.startswith('Attached') else 'SEARCHING' if text.startswith('Waiting') else 'ERROR' if color == self.RED else 'IDLE'}", fg=color)
        if hasattr(self, "dashboard_connection_value"):
            self.dashboard_connection_value.configure(text=f"CONNECTION  •  {'CONNECTED' if text.startswith('Attached') else 'PENDING' if text.startswith('Waiting') else 'OFFLINE'}", fg=self.GREEN if text.startswith('Attached') else self.MUTED)
        if hasattr(self, "dashboard_ring"):
            label = "LIVE" if text.startswith("Attached") else "SYNC" if text.startswith("Waiting") else "READY"
            self.dashboard_ring.set_state(label, text, color)
        if hasattr(self, "bhop_connection_label"):
            self.bhop_connection_label.configure(text=text, fg=self.GREEN if text.startswith("Attached") else color)
        if hasattr(self, "sidebar_state_label"):
            self.sidebar_state_label.configure(text="ATTACHED" if text.startswith("Attached") else "SEARCHING" if text.startswith("Waiting") else "STANDBY", fg=color)
            self.sidebar_state_hint.configure(text="cs2.exe connected" if text.startswith("Attached") else "Looking for the game" if text.startswith("Waiting") else "Awaiting session")

    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        self.running = True
        for button in self.start_buttons:
            button.set_enabled(False)
            button.set_text("Attaching…")
        for button in self.stop_buttons:
            button.set_enabled(True)
        self._set_status("Waiting for cs2.exe", "waiting")
        self._log("Looking for cs2.exe…")
        engine = BhopWorker(self.events, self.stop_event, self.settings)
        self.worker = threading.Thread(target=engine.run, name="BhopWorker", daemon=False)
        self.worker.start()

    def stop(self):
        if not self.running:
            return
        self._log("Stop requested — ending the current session.")
        self.stop_event.set()
        self._set_status("Stopping session", "waiting")
        for button in self.stop_buttons:
            button.set_enabled(False)

    def toggle(self):
        enabled = self.settings.toggle_enabled()
        self._refresh_enabled(enabled)
        self._log(f"Bhop turned {'on' if enabled else 'off'}.")

    def _refresh_enabled(self, enabled=None):
        if enabled is None:
            enabled = self.settings.snapshot()["enabled"]
        self.state.configure(text=f"BHOP  {'ON' if enabled else 'OFF'}", foreground=self.GREEN if enabled else self.RED)
        for button in self.toggle_buttons:
            button.set_text("Turn Off" if enabled else "Turn On")
        self.dashboard_bhop_value.configure(text=f"BHOP  •  {'ENABLED' if enabled else 'DISABLED'}", fg=self.RED if enabled else self.MUTED)
        self.command_state_label.configure(text=f"Bunny hop  •  {'ARMED' if enabled else 'PAUSED'}", fg=self.MUTED if enabled else self.MUTED)
        self.bhop_engine_state.configure(text=f"●  {'ARMED' if enabled else 'PAUSED'}", fg=self.RED if enabled else self.MUTED)
        self.bhop_status.configure(text=f"BHOP ENGINE\n{'ON' if enabled else 'OFF'}", fg=self.TEXT if enabled else self.MUTED)

    def _consume_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log": self._log(value)
                elif kind == "status": self._set_status(*value)
                elif kind == "enabled": self._refresh_enabled(value)
                elif kind == "attached":
                    for button in self.start_buttons:
                        button.set_text("Attached")
                elif kind == "stopped":
                    self.running = False
                    for button in self.start_buttons:
                        button.set_enabled(True)
                        button.set_text("Start & Attach")
                    for button in self.stop_buttons:
                        button.set_enabled(False)
                    self._set_status("Ready to start", self.MUTED)
                elif kind == "close":
                    self.shutdown()
                    return
        except queue.Empty:
            pass
        if self.winfo_exists(): self.after(80, self._consume_events)

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        for after_id in tuple(self._boot_after_ids):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        self._boot_after_ids.clear()
        self._stop_menu_hotkey()
        self._log("Clean shutdown requested.")
        self.stop_event.set()
        self.shutdown_deadline = time.monotonic() + 2.0
        self._finish_shutdown()

    def _finish_shutdown(self):
        if self.worker and self.worker.is_alive() and time.monotonic() < self.shutdown_deadline:
            self.after(30, self._finish_shutdown)
            return
        if self.worker and self.worker.is_alive():
            self._log("Worker did not stop in time; closing the interface.")
        else:
            self._log("Session ended cleanly.")
        self.destroy()


if __name__ == "__main__":
    crash_reporter = install_crash_reporter(APP_NAME, APP_VERSION)
    ctypes.set_last_error(0)
    instance_mutex = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        if instance_mutex:
            kernel32.CloseHandle(instance_mutex)

        already_open = tk.Tk()
        already_open.title("DragiWare")
        already_open.configure(bg=BhopApp.BG)
        already_open.resizable(False, False)
        width, height = 390, 190
        x = (already_open.winfo_screenwidth() - width) // 2
        y = (already_open.winfo_screenheight() - height) // 2
        already_open.geometry(f"{width}x{height}+{x}+{y}")
        card = RoundedPanel(already_open, BhopApp.PANEL, height=150, radius=24)
        card.pack(fill="both", expand=True, padx=14, pady=14)
        tk.Label(card.body, text="Software already open", bg=BhopApp.PANEL, fg=BhopApp.TEXT, font=("Segoe UI Semibold", 16)).pack(anchor="w", pady=(5, 4))
        tk.Label(card.body, text="DragiWare is already running on this PC.", bg=BhopApp.PANEL, fg=BhopApp.MUTED, font=("Segoe UI", 10)).pack(anchor="w")
        RoundedButton(card.body, "Okay", already_open.destroy, BhopApp.BLUE, "#ffffff", width=88).pack(anchor="e", pady=(13, 0))
        already_open.mainloop()
    else:
        app = BhopApp()
        app.report_callback_exception = crash_reporter.report_tk_exception
        app.mainloop()
        if instance_mutex:
            kernel32.CloseHandle(instance_mutex)
