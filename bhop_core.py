"""Runtime, settings, and process-attachment code for DragiWare.

The UI lives in ``cs2_bhop.py``. Keeping non-visual behavior here makes the
application easier to maintain and test without opening a Tk window.
"""

from __future__ import annotations

import ctypes
import json
import queue
import threading
import time
import traceback
from pathlib import Path

CHANGELOG = (
    ("2.2.0", "A dedicated updates workspace, live themes, and safer stable update delivery."),
    ("2.0.0", "Dashboard refresh, saved settings, presets, debug logging, and keybind controls."),
    ("1.3.0", "Added single-instance protection and per-session logs."),
    ("1.0.0", "Initial desktop release."),
)

# Current client.dll offsets — sourced from a2x/cs2-dumper, Oct 5 2026.
# dwLocalPlayerPawn: direct pointer to the local player's C_CSPlayerPawn.
OFFSET_LOCAL_PLAYER_PAWN = 0x2560698
# CInButtonState for the jump action (from buttons.json "jump", Oct 5 2026).
# Each button is a CInButtonState struct with three uint64 fields:
#   +0x00  nCurrentState  — write here to press / release the button
#   +0x08  nPreviousState — clear this to force CS2 edge-detection to fire
#   +0x10  nHandledState  — engine marks bits here when it processes a press
# Writing 65537 / 0 to +0 / +8 is the confirmed-working bhop pattern.
OFFSET_JUMP_BUTTON  = 0x22304E0   # nCurrentState (uint64)
OFFSET_JUMP_HANDLED = 0x22304E8   # nPreviousState = OFFSET_JUMP_BUTTON + 0x08
# m_fFlags lives at +0x3F4 inside C_BaseEntity — unchanged.
OFFSET_FLAGS = 0x3F4
FL_ONGROUND = 1
BHOP_KEY = 0x20  # Space
OFFSET_SOURCE_BASE = "https://raw.githubusercontent.com/a2x/cs2-dumper/main/output/"
OFFSET_REQUEST_TIMEOUT_SECONDS = 4
MAX_OFFSET_DOCUMENT_BYTES = 1024 * 1024
MIN_REASONABLE_OFFSET = 0x1000
MAX_REASONABLE_OFFSET = 0x7FFF_FFFF

DEFAULT_SETTINGS = {
    "toggle_key": 0x2D,  # Insert
    "exit_key": 0x23,  # End
    "menu_toggle_key": 0x74,  # F5
    "menu_toggle_modifiers": 0,
    "enabled": True,
    "debug_mode": False,
    "preset": "Default",
    "theme": "Dark",
    "auto_check_updates": True,
}

PRESETS = {
    "Default": {"toggle_key": 0x2D, "exit_key": 0x23, "menu_toggle_key": 0x74, "menu_toggle_modifiers": 0},
    "Function Keys": {"toggle_key": 0x75, "exit_key": 0x76, "menu_toggle_key": 0x74, "menu_toggle_modifiers": 0},  # F6 / F7 / F5
    "Home + End": {"toggle_key": 0x24, "exit_key": 0x23, "menu_toggle_key": 0x74, "menu_toggle_modifiers": 0},
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
# Declare return types explicitly so ctypes does not truncate 64-bit handles
# to 32 bits on x86-64 Windows — an unprototyped call returns c_int (32-bit)
# which will be 0 when the real HWND value doesn't fit, making every focus
# check fail silently.
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
user32.GetWindowThreadProcessId.restype = ctypes.c_ulong


def key_held(key: int) -> bool:
    return (user32.GetAsyncKeyState(key) & 0x8000) != 0


def foreground_process_id() -> int | None:
    """Return the process ID that owns the foreground window, if any."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    process_id = ctypes.c_ulong()
    thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return process_id.value if thread_id and process_id.value else None


def _read_offset_json(url: str) -> dict:
    """Read one small HTTPS JSON document without persisting or executing it."""
    import urllib.request

    if not url.lower().startswith("https://"):
        raise ValueError("The offset source must use HTTPS.")
    request = urllib.request.Request(url, headers={"User-Agent": "DragiWare-offsets/1.0"})
    with urllib.request.urlopen(request, timeout=OFFSET_REQUEST_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        if not isinstance(final_url, str) or not final_url.lower().startswith("https://"):
            raise ValueError("The offset source did not use HTTPS.")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise ValueError("The offset source returned an invalid response size.") from error
            if not 0 <= declared_size <= MAX_OFFSET_DOCUMENT_BYTES:
                raise ValueError("The offset response exceeded the allowed size.")
        payload = response.read(MAX_OFFSET_DOCUMENT_BYTES + 1)
    if len(payload) > MAX_OFFSET_DOCUMENT_BYTES:
        raise ValueError("The offset response exceeded the allowed size.")
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("The offset response root must be a JSON object.")
    return decoded


def _validated_live_offsets(offset_document: object, button_document: object) -> tuple[int, int]:
    """Return the expected numeric offsets or reject the remote data entirely."""
    def value_from(document: object, field: str) -> int:
        if not isinstance(document, dict):
            raise ValueError("The offset response root must be a JSON object.")
        client = document.get("client.dll")
        if not isinstance(client, dict):
            raise ValueError("The offset response is missing the client.dll object.")
        value = client.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"The {field} offset must be an integer.")
        if not MIN_REASONABLE_OFFSET <= value <= MAX_REASONABLE_OFFSET:
            raise ValueError(f"The {field} offset is outside the accepted range.")
        return value

    pawn = value_from(offset_document, "dwLocalPlayerPawn")
    jump = value_from(button_document, "jump")
    if pawn == jump or jump > MAX_REASONABLE_OFFSET - 8:
        raise ValueError("The offset response contains invalid jump data.")
    return pawn, jump


class SettingsStore:
    """Thread-safe JSON-backed user preferences."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._values = DEFAULT_SETTINGS.copy()
        self._load()

    def _load(self):
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Settings root must be an object")
            for key, default in DEFAULT_SETTINGS.items():
                value = loaded.get(key, default)
                if key in ("toggle_key", "exit_key", "menu_toggle_key"):
                    if not isinstance(value, int) or not 1 <= value <= 255:
                        value = default
                elif key == "menu_toggle_modifiers":
                    if not isinstance(value, int) or value < 0 or value > 15:
                        value = default
                elif key in ("enabled", "debug_mode"):
                    value = bool(value)
                elif key == "preset" and not isinstance(value, str):
                    value = default
                elif key == "theme" and not isinstance(value, str):
                    value = default
                elif key == "auto_check_updates":
                    value = bool(value)
                self._values[key] = value
        except FileNotFoundError:
            self.save()
        except (OSError, ValueError, json.JSONDecodeError):
            # Keep safe defaults if a damaged settings file cannot be read.
            self.save()

    def save(self):
        """Write settings atomically so an interruption cannot corrupt them."""
        with self._lock:
            payload = json.dumps(self._values, indent=2, sort_keys=True)
            temporary = self.path.with_suffix(".tmp")
            try:
                temporary.write_text(payload + "\n", encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def snapshot(self) -> dict:
        with self._lock:
            return self._values.copy()

    def update(self, **changes):
        with self._lock:
            self._values.update(changes)
        self.save()

    def toggle_enabled(self) -> bool:
        with self._lock:
            self._values["enabled"] = not self._values["enabled"]
            enabled = self._values["enabled"]
        self.save()
        return enabled

    def reset(self):
        with self._lock:
            self._values = DEFAULT_SETTINGS.copy()
        self.save()
        return self.snapshot()

    def apply_preset(self, name: str):
        if name not in PRESETS:
            raise ValueError(f"Unknown preset: {name}")
        with self._lock:
            self._values.update(PRESETS[name])
            self._values["preset"] = name
        self.save()
        return self.snapshot()


class BhopWorker:
    """Attaches to CS2 and runs the low-CPU bunny-hop loop in a worker thread."""

    def __init__(self, events: queue.Queue, stop_event: threading.Event, settings: SettingsStore):
        self.events = events
        self.stop_event = stop_event
        self.settings = settings

    def _event(self, kind: str, value=None):
        self.events.put((kind, value))

    def _ensure_pymem(self):
        try:
            import pymem
            import pymem.process
            return pymem
        except ImportError:
            self._event("log", "Required component pymem is missing. Complete the in-app setup before attaching.")
            self._event("status", ("Dependency missing — open setup", "error"))
            return None

    def _fetch_offsets(self) -> None:
        """Fetch current offsets from a2x/cs2-dumper and update the module-level
        constants in-place only after validating both HTTPS JSON documents.
        Falls back to the hardcoded values when the source is unavailable or
        malformed; received data is never executed or written to disk."""
        global OFFSET_LOCAL_PLAYER_PAWN, OFFSET_JUMP_BUTTON, OFFSET_JUMP_HANDLED
        try:
            offsets = _read_offset_json(OFFSET_SOURCE_BASE + "offsets.json")
            buttons = _read_offset_json(OFFSET_SOURCE_BASE + "buttons.json")
            pawn, jump = _validated_live_offsets(offsets, buttons)

            OFFSET_LOCAL_PLAYER_PAWN = pawn
            OFFSET_JUMP_BUTTON       = jump
            OFFSET_JUMP_HANDLED      = jump + 8
            self._event("log",
                f"[OFFSETS] Refreshed from the HTTPS source: pawn={hex(pawn)} jump={hex(jump)}.")
        except Exception as err:
            self._event("log",
                f"[OFFSETS] Refresh rejected ({type(err).__name__}: {err}); using built-in values.")

    def run(self):
        pm = None
        client_base = None
        stage = "initializing"
        try:
            stage = "checking dependencies"
            pymem = self._ensure_pymem()
            if pymem is None:
                return
            # Fetch fresh offsets before doing anything so CS2 updates don't
            # require a new build.  Falls back to hardcoded values silently.
            stage = "refreshing offsets"
            self._fetch_offsets()
            stage = "waiting for cs2.exe"
            while not self.stop_event.is_set() and pm is None:
                try:
                    pm = pymem.Pymem("cs2.exe")
                except pymem.exception.ProcessNotFound:
                    self.stop_event.wait(1)
                except pymem.exception.CouldNotOpenProcess:
                    self._event("log", "Cannot open CS2. Run the launcher as Administrator.")
                    self._event("status", ("Administrator access required", "error"))
                    self.stop_event.wait(2)
            if pm is None:
                return

            cs2_pid = int(pm.process_id)
            self._event("log", f"[ATTACH] Connected to cs2.exe | pid={cs2_pid}.")
            self._event("status", ("Waiting for client.dll", "waiting"))
            stage = "waiting for client.dll"
            module_wait_started = time.monotonic()
            last_module_notice = 0.0
            while not self.stop_event.is_set() and client_base is None:

                try:
                    module = pymem.process.module_from_name(pm.process_handle, "client.dll")
                    if module:
                        client_base = module.lpBaseOfDll
                except Exception:
                    self._event("log", "CS2 closed before client.dll was ready. Shutting down DragiWare.")
                    self._event("close")
                    self.stop_event.set()
                    break
                if client_base is None:
                    elapsed = time.monotonic() - module_wait_started
                    if elapsed - last_module_notice >= 5.0:
                        self._event("log", f"[MODULE] Still waiting for client.dll ({elapsed:.0f}s after attach).")
                        last_module_notice = elapsed
                    self.stop_event.wait(0.5)
            if client_base is None:
                return

            self._event("log", f"[MODULE] client.dll ready | base={hex(client_base)} | wait={time.monotonic() - module_wait_started:.1f}s.")
            self._event("status", ("Attached — ready in game", "success"))
            self._event("attached")
            self._event("log",
                f"[OFFSETS] pawn={hex(OFFSET_LOCAL_PLAYER_PAWN)} "
                f"jump={hex(OFFSET_JUMP_BUTTON)} handled={hex(OFFSET_JUMP_HANDLED)}")

            stage = "active loop"
            toggle_pressed  = False
            last_check      = 0.0
            focused         = False
            previous_focus  = None
            _prev_space     = False   # previous Space state
            _prev_pawn      = 0       # previous pawn address
            _prev_ground    = None    # previous on-ground state
            _liveness_fails = 0       # consecutive liveness-check failures
            last_idle_reason = None
            last_idle_notice = 0.0
            current = self.settings.snapshot()
            self._event("log", f"[CONTROL] Space={hex(BHOP_KEY)} | toggle={hex(current['toggle_key'])} | exit={hex(current['exit_key'])} | enabled={current['enabled']}.")

            while not self.stop_event.is_set():
                now = time.perf_counter()
                if key_held(current["exit_key"]):
                    self._event("log", "Exit key pressed — closing cleanly.")
                    self._event("close")
                    self.stop_event.set()
                    break
                if key_held(current["toggle_key"]):
                    if not toggle_pressed:
                        enabled = self.settings.toggle_enabled()
                        self._event("enabled", enabled)
                        self._event("log", f"Bhop turned {'on' if enabled else 'off'} by keybind.")
                        toggle_pressed = True
                        current = self.settings.snapshot()
                else:
                    toggle_pressed = False

                # ── liveness + focus check every 200 ms ──────────────────────────
                if now - last_check > 0.20:
                    try:
                        pm.read_int(client_base)
                        _liveness_fails = 0
                    except Exception as error:
                        _liveness_fails += 1
                        self._event("log", f"[LIVENESS] failed {_liveness_fails}/10 | {type(error).__name__}: {error}")
                        if _liveness_fails >= 10:
                            self._event("log", "CS2 was closed or connection was lost.")
                            self._event("status", ("Connection lost", "error"))
                            self._event("close")
                            self.stop_event.set()
                            break
                    # PID-based focus keeps input inactive outside the CS2 window.
                    foreground_pid = foreground_process_id()
                    focused = (foreground_pid == cs2_pid)
                    if focused != previous_focus:
                        self._event("log", f"[FOCUS] CS2 is now {'FOCUSED' if focused else 'UNFOCUSED'} | foreground_pid={foreground_pid or 'none'} | cs2_pid={cs2_pid}.")
                    previous_focus, last_check = focused, now
                    current = self.settings.snapshot()

                # ── log Space key transitions ─────────────────────────────────────
                space_held = key_held(BHOP_KEY)
                if space_held != _prev_space:
                    self._event("log",
                        f"[KEY] Space {'PRESSED' if space_held else 'RELEASED'}. "
                        f"enabled={current['enabled']} focused={focused}")
                    _prev_space = space_held

                # ── main bhop path — direct port of bhop_simple.py ───────────────
                if current["enabled"] and focused and space_held:
                    try:
                        pawn = pm.read_longlong(client_base + OFFSET_LOCAL_PLAYER_PAWN)

                        if pawn != _prev_pawn:
                            self._event("log",
                                f"[PAWN] Address changed: {hex(_prev_pawn)} -> "
                                f"{'NULL' if not pawn else hex(pawn)}")
                            _prev_pawn = pawn

                        if pawn:
                            # Exactly as bhop_simple.py: read_bytes + struct.unpack
                            import struct as _struct
                            raw_vz    = pm.read_bytes(pawn + 0x3F8 + 8, 4)
                            vz        = _struct.unpack("<f", raw_vz)[0]
                            on_ground = abs(vz) < 64.0

                            if on_ground != _prev_ground:
                                self._event("log",
                                    f"[GROUND] {'ON GROUND' if on_ground else 'AIRBORNE'} "
                                    f"vz={vz:+.1f} pawn={hex(pawn)}")
                                _prev_ground = on_ground

                            if on_ground:
                                pm.write_longlong(client_base + OFFSET_JUMP_BUTTON,     65537)
                                pm.write_longlong(client_base + OFFSET_JUMP_BUTTON + 8, 0)
                            else:
                                pm.write_longlong(client_base + OFFSET_JUMP_BUTTON,     256)
                                pm.write_longlong(client_base + OFFSET_JUMP_BUTTON + 8, 65537)
                        else:
                            if _prev_pawn:
                                self._event("log", "[PAWN] NULL — not spawned or not in a match.")
                            _prev_pawn = 0
                            _prev_ground = None

                    except Exception as error:
                        self._event("log", f"[ACTION] {type(error).__name__}: {error}")

                    # 1ms sleep — same as bhop_simple.py
                    self.stop_event.wait(0.001)

                else:
                    if space_held:
                        reasons = []
                        if not current["enabled"]: reasons.append("bhop OFF")
                        if not focused:            reasons.append("CS2 not focused")
                        reason = ", ".join(reasons) or "unknown"
                        if reason != last_idle_reason or now - last_idle_notice >= 2.0:
                            self._event("log", f"[IDLE] Space held but blocked: {reason}.")
                            last_idle_reason, last_idle_notice = reason, now
                    else:
                        last_idle_reason = None
                    self.stop_event.wait(0.005)

        except Exception as error:
            self._event("log", f"[FATAL] stage={stage} | {type(error).__name__}: {error}")
            for detail in traceback.format_exc().strip().splitlines():
                self._event("log", f"[TRACE] {detail}")
            self._event("status", ("Error — see Activity", "error"))
        finally:
            if pm:
                try:
                    if client_base:
                        pm.write_longlong(client_base + OFFSET_JUMP_BUTTON,     256)
                        pm.write_longlong(client_base + OFFSET_JUMP_BUTTON + 8, 0)
                    pm.close_process()
                except Exception:
                    pass
            self._event("stopped")
