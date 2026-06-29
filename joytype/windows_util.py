"""Win32 input injection and foreground-window inspection.

All input injection goes through :func:`SendInput` (the modern, UIPI-friendly
API); legacy ``keybd_event`` / ``mouse_event`` are deliberately avoided.
Everything here is Windows-only by design - this module is the only place
that knows about ``ctypes`` and the Win32 API surface.

Key naming
----------
Config files use lowercase logical key names (``enter``, ``ctrl``, ``left``).
The :data:`KEY_NAMES` table maps those to Win32 virtual-key codes (VK_*).
Names follow the `pynput <https://pynput.readthedocs.io>`_ convention so
users coming from other Python automation tools feel at home.
"""

from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)

# High-resolution timer. Windows' default timer granularity is ~15.6ms, which
# makes a fixed-rate mouse thread sleep unevenly (visible as cursor stutter).
# timeBeginPeriod(1) drops it to ~1ms for steady ticks; always pair with
# end_high_res_timer().
winmm.timeBeginPeriod.argtypes = (wintypes.UINT,)
winmm.timeBeginPeriod.restype = wintypes.UINT
winmm.timeEndPeriod.argtypes = (wintypes.UINT,)
winmm.timeEndPeriod.restype = wintypes.UINT


def begin_high_res_timer(period_ms: int = 1) -> None:
    """Raise the system timer resolution around a timing-critical loop."""
    try:
        winmm.timeBeginPeriod(period_ms)
    except Exception:
        pass


def end_high_res_timer(period_ms: int = 1) -> None:
    """Restore the resolution raised by :func:`begin_high_res_timer`."""
    try:
        winmm.timeEndPeriod(period_ms)
    except Exception:
        pass

# SendInput structures
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

KEYEVENTF_KEYDOWN = 0x0000  # implicit; keydown when this flag absent
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001  # required for Right-side modifiers, arrows, Numpad

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

WHEEL_DELTA = 120


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _INPUT_UNION(ctypes.Union):
    _fields_ = (
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
    )


class _INPUT(ctypes.Structure):
    class _Anonymous(ctypes.Union):
        _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT))

    _anonymous_ = ("u",)
    _fields_ = (
        ("type", wintypes.DWORD),
        ("u", _Anonymous),
    )


# SendInput signature
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

# GetForegroundWindow + GetWindowThreadProcessId
user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

# Process name lookup
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL

# QueryFullProcessImageNameW (Vista+) - reliable, no WOW64 surprises
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.BOOL,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

# Window title
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int

# Cursor position (we set absolute moves relative to current)
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = (wintypes.INT, wintypes.INT)
user32.SetCursorPos.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ---------------------------------------------------------------------------
# Key name -> virtual-key code
# ---------------------------------------------------------------------------

# Win32 virtual-key codes for the modifiers + most-used keys.
VK = {
    # Modifiers (generic - matches whichever physical side is pressed)
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "win": 0x5B, "cmd": 0x5B, "super": 0x5B, "meta": 0x5B,
    # Side-specific modifiers. Some apps (dictation tools in particular) check
    # the physical key via scancode, so exposing lshift/rshift etc. matters.
    "lshift": 0xA0, "rshift": 0xA1,
    "lctrl": 0xA2, "lcontrol": 0xA2, "rctrl": 0xA3, "rcontrol": 0xA3,
    "lalt": 0xA4, "lmenu": 0xA4, "ralt": 0xA5, "rmenu": 0xA5,
    "lwin": 0x5B, "rwin": 0x5C,
    # Whitespace / editing
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "back": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D, "esc": 0x1B, "escape": 0x1B,
    # Navigation
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    # Function keys
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    # Letters and digits use ASCII (uppercase). The resolver handles those.
}

# Resolve any name the user might write. Unknown names raise KeyError.
def resolve_vk(name: str) -> int:
    """Map a config key name to a Win32 virtual-key code.

    Accepts lowercase names (``enter``, ``ctrl``), single characters
    (``a``, ``5``), and accepts unknown names with a clear error.
    """
    key = name.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        # Letters / digits / punctuation: use the ASCII code as the VK.
        return ord(key.upper())
    raise KeyError(
        f"Unknown key name '{name}'. Use a single character or one of: "
        + ", ".join(sorted(VK))
    )


# Canonical VK -> config name, used by the GUI hotkey-capture widget (the
# reverse of resolve_vk). Side-specific modifier names win because that's what
# dictation tools distinguish (lshift vs shift).
_VK_TO_NAME: dict[int, str] = {
    0xA0: "lshift", 0xA1: "rshift",
    0xA2: "lctrl", 0xA3: "rctrl",
    0xA4: "lalt", 0xA5: "ralt",
    0x10: "shift", 0x11: "ctrl", 0x12: "alt",
    0x5B: "lwin", 0x5C: "rwin",
    0x0D: "enter", 0x09: "tab", 0x20: "space",
    0x08: "backspace", 0x2E: "delete", 0x2D: "insert", 0x1B: "esc",
    0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right",
    0x24: "home", 0x23: "end", 0x21: "pageup", 0x22: "pagedown",
    **{0x6F + i: f"f{i}" for i in range(1, 13)},
}


def vk_to_name(vk: int) -> Optional[str]:
    """Map a Win32 virtual-key code back to a canonical config name.

    Used by the GUI hotkey-capture widget to turn a physical keypress into the
    token JoyType writes in config.yaml. Returns ``None`` for keys we have no
    token for (so the caller can ignore them during capture).
    """
    if vk in _VK_TO_NAME:
        return _VK_TO_NAME[vk]
    if 0x41 <= vk <= 0x5A:  # A-Z
        return chr(vk).lower()
    if 0x30 <= vk <= 0x39:  # 0-9 top row
        return chr(vk)
    return None


# ---------------------------------------------------------------------------
# SendInput wrappers
# ---------------------------------------------------------------------------

def _send(inputs: list[_INPUT]) -> None:
    """Submit a batch of INPUT structs, raising on total failure.

    SendInput requires a CONTIGUOUS array of INPUT structs. A Python list of
    ctypes Structure instances is NOT contiguous in memory, so passing
    ``inputs[0]`` with a count of ``len(inputs)`` makes SendInput read garbage
    for every element after the first. That silently corrupts multi-key chords
    (only the first key lands) and, worse, drops the key-UP half of releases,
    leaving modifiers stuck down. We must pack the structs into a real ctypes
    array before handing them to the API.
    """
    if not inputs:
        return
    n_in = len(inputs)
    arr = (_INPUT * n_in)(*inputs)
    n = user32.SendInput(n_in, arr, ctypes.sizeof(_INPUT))
    if n != n_in:
        err = ctypes.get_last_error()
        raise OSError(f"SendInput inserted {n}/{n_in} events (WinError {err})")


def _vk_to_scancode(vk: int) -> tuple[int, bool]:
    """Map a virtual-key code to (scancode, is_extended_key).

    Many global-hotkey apps (dictation tools especially) read raw scan codes
    via low-level keyboard hooks and ignore pure virtual-key SendInput
    events entirely. They also distinguish Left vs Right Shift/Ctrl/Alt ONLY
    by scancode, not by VK - so sending VK_RSHIFT without the right scancode
    looks like Left Shift to them. We therefore emit the scancode for every
    key, and set KEYEVENTF_EXTENDEDKEY for the right-side + Numpad + arrows
    group (which Windows requires to interpret their scancodes correctly).

    Returns (scancode, is_extended). (0, False) means "no known scancode,
    let Windows derive it from the VK".
    """
    # VK -> scancode map for the keys we care about. Scancodes are the
    # hardware-level key identifiers from the IBM PC AT keyboard spec.
    SC = {
        # Side-specific modifiers (these are what makes dictation hotkeys work).
        0xA0: 0x2A,  # LSHIFT
        0xA1: 0x36,  # RSHIFT  (extended)
        0xA2: 0x1D,  # LCTRL
        0xA3: 0x1D,  # RCTRL   (extended)
        0xA4: 0x38,  # LALT
        0xA5: 0x38,  # RALT    (extended)
        # Generic modifiers (set scancode so hooks see a real key).
        0x10: 0x2A,  # SHIFT (treat as left)
        0x11: 0x1D,  # CTRL  (treat as left)
        0x12: 0x38,  # ALT   (treat as left)
        # Whitespace / editing
        0x0D: 0x1C,  # ENTER
        0x09: 0x0F,  # TAB
        0x20: 0x39,  # SPACE
        0x08: 0x0E,  # BACKSPACE
        0x2E: 0x53,  # DELETE  (extended)
        0x2D: 0x52,  # INSERT  (extended)
        0x1B: 0x01,  # ESC
        # Navigation (all extended)
        0x26: 0x48,  # UP
        0x28: 0x50,  # DOWN
        0x25: 0x4B,  # LEFT
        0x27: 0x4D,  # RIGHT
        0x24: 0x47,  # HOME
        0x23: 0x4F,  # END
        0x21: 0x49,  # PAGEUP
        0x22: 0x51,  # PAGEDOWN
        # Win keys
        0x5B: 0x5B,  # LWIN
        0x5C: 0x5C,  # RWIN
        # Function keys F1-F12 (not extended). Dictation tools that read raw
        # scancodes need these or they see scancode 0 and ignore the key.
        0x70: 0x3B, 0x71: 0x3C, 0x72: 0x3D, 0x73: 0x3E,  # F1-F4
        0x74: 0x3F, 0x75: 0x40, 0x76: 0x41, 0x77: 0x42,  # F5-F8
        0x78: 0x43, 0x79: 0x44, 0x7A: 0x57, 0x7B: 0x58,  # F9-F12
    }
    # Keys whose scancodes require the EXTENDEDKEY flag.
    EXTENDED_VKS = {0xA1, 0xA3, 0xA5, 0x2E, 0x2D, 0x26, 0x28, 0x25, 0x27,
                    0x24, 0x23, 0x21, 0x22, 0x5C}
    sc = SC.get(vk, 0)
    if sc == 0:
        # Letters and digits: derive from the standard IBM AT scancode set.
        # A-Z maps to a fixed table; 0-9 (top row) too.
        if 0x41 <= vk <= 0x5A:  # A-Z
            LETTERS = "QWERTYUIOPASDFGHJKLZXCVBNM"
            ch = chr(vk)
            idx = LETTERS.find(ch)
            # Scancodes for the letters in QWERTY order:
            #  Q=10 W=11 E=12 R=13 T=14 Y=15 U=16 I=17 O=18 P=19
            #  A=30 S=31 D=32 F=33 G=34 H=35 J=36 K=37 L=38
            #  Z=44 X=45 C=46 V=47 B=48 N=49 M=50
            LETTER_SC = [0x10,0x11,0x12,0x13,0x14,0x15,0x16,0x17,0x18,0x19,
                         0x1E,0x1F,0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,
                         0x2C,0x2D,0x2E,0x2F,0x30,0x31]
            if idx >= 0:
                sc = LETTER_SC[idx]
        elif 0x30 <= vk <= 0x39:  # 0-9 top row
            # Row: 1=2, 2=3, ..., 0=0x0B
            DIGIT_SC = [0x0B,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0A]
            sc = DIGIT_SC[vk - 0x30]
    return sc, vk in EXTENDED_VKS


def _kbd_input(vk: int, flags: int) -> _INPUT:
    """Build a KEYBDINPUT with the correct scancode + extended-key flag.

    Always emits the scancode (via KEYEVENTF_SCANCODE would drop the VK; we
    send BOTH by setting wVk AND wScan, which is the documented way to make
    a synthesized event look like a physical keypress to low-level hooks).
    """
    sc, is_extended = _vk_to_scancode(vk)
    real_flags = flags
    if is_extended:
        real_flags |= KEYEVENTF_EXTENDEDKEY
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = sc
    inp.u.ki.dwFlags = real_flags
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = None
    return inp


def press_key(vk: int) -> None:
    """Send a single key-down (no release)."""
    _send([_kbd_input(vk, KEYEVENTF_KEYDOWN)])


def release_key(vk: int) -> None:
    """Send a single key-up."""
    _send([_kbd_input(vk, KEYEVENTF_KEYUP)])


def tap_key(vk: int) -> None:
    """Press and immediately release a key."""
    _send([_kbd_input(vk, KEYEVENTF_KEYDOWN), _kbd_input(vk, KEYEVENTF_KEYUP)])


def tap_chord(vks: Iterable[int]) -> None:
    """Hold all keys down, then release all in reverse order.

    Implements the classic Ctrl+Shift+P pattern: every key down first, every
    key up afterwards, so the OS sees a proper chord rather than independent
    taps that the focused app might race on.
    """
    keys = list(vks)
    inputs: list[_INPUT] = []
    for vk in keys:
        inputs.append(_kbd_input(vk, KEYEVENTF_KEYDOWN))
    for vk in reversed(keys):
        inputs.append(_kbd_input(vk, KEYEVENTF_KEYUP))
    _send(inputs)


def press_chord(vks: Iterable[int]) -> None:
    """Press all keys in a single atomic SendInput call (all-down).

    Use this instead of looping press_key() when you need a multi-key chord
    to be observed atomically by low-level hooks - some global-hotkey apps
    reject chords whose modifier keys arrive as separate SendInput calls
    because they look like typing rather than a held combo.
    """
    keys = list(vks)
    if not keys:
        return
    _send([_kbd_input(vk, KEYEVENTF_KEYDOWN) for vk in keys])


def release_all_modifiers() -> None:
    """Send key-up for every common modifier to clear any stuck-down state.

    Called on daemon startup to self-heal a modifier left stuck by a previous
    crash / force-kill (e.g. Alt held by the window-cycler when the process was
    hard-killed mid-cycle), and as a shutdown safety. Sending key-up for a key
    that isn't down is harmless.
    """
    for name in ("lshift", "rshift", "lctrl", "rctrl", "lalt", "ralt",
                 "shift", "ctrl", "alt", "lwin", "rwin"):
        try:
            release_key(resolve_vk(name))
        except Exception:
            pass


def release_chord(vks: Iterable[int]) -> None:
    """Release all keys in a single atomic SendInput call (all-up).

    Mirror of :func:`press_chord`. Releasing as one batch avoids the
    "modifier still held" race that confuses some hotkey software.
    """
    keys = list(vks)
    if not keys:
        return
    # Release in reverse press order (standard convention; some apps care).
    _send([_kbd_input(vk, KEYEVENTF_KEYUP) for vk in reversed(keys)])


# --- Mouse -----------------------------------------------------------------

def _mouse_input(flags: int, data: int = 0) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi.dx = 0
    inp.u.mi.dy = 0
    inp.u.mi.mouseData = data
    inp.u.mi.dwFlags = flags
    inp.u.mi.time = 0
    inp.u.mi.dwExtraInfo = None
    return inp


def move_mouse_relative(dx: int, dy: int) -> None:
    """Move the cursor by (dx, dy) pixels from its current position.

    Relative motion avoids the screen-resolution math that ABSOLUTE moves
    require and is exactly what stick-driven motion wants.
    """
    if dx == 0 and dy == 0:
        return
    inp = _mouse_input(MOUSEEVENTF_MOVE)
    inp.u.mi.dx = dx
    inp.u.mi.dy = dy
    _send([inp])


def scroll_wheel(clicks: int) -> None:
    """Send wheel deltas. Positive = up, negative = down."""
    if clicks == 0:
        return
    amount = int(max(-1, min(1, clicks))) * WHEEL_DELTA * abs(int(clicks)) if clicks else 0
    # Simpler: one event per full delta, repeat for magnitude.
    direction = 1 if clicks > 0 else -1
    for _ in range(abs(int(clicks)) or 1):
        _send([_mouse_input(MOUSEEVENTF_WHEEL, direction * WHEEL_DELTA)])


def click_mouse(button: str = "left") -> None:
    """Press and release a mouse button (``left`` / ``right`` / ``middle``)."""
    button = button.lower()
    if button == "left":
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    elif button == "right":
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    elif button == "middle":
        down, up = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
    else:
        raise ValueError(f"Unknown mouse button '{button}'")
    _send([_mouse_input(down), _mouse_input(up)])


# ---------------------------------------------------------------------------
# Foreground window inspection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForegroundWindow:
    """Identity of the window that currently has keyboard focus.

    Both fields are normalized to lower-case so config matchers can compare
    case-insensitively without each caller repeating the dance.
    """

    process_name: str  # e.g. "code.exe"  (lower-case, includes .exe)
    process_stem: str  # e.g. "code"      (no extension)
    title: str         # window title verbatim


def _query_process_name(pid: int) -> str:
    """Return the owning process's image path filename, or '' on failure.

    Uses QueryFullProcessImageNameW so it works across UAC boundaries as long
    as JoyType itself isn't elevated-vs-unelevated with the target.
    """
    if pid == 0:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, False, buf, ctypes.byref(size)):
            # Full path -> take basename.
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _query_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_foreground_window() -> Optional[ForegroundWindow]:
    """Inspect whatever window currently has the keyboard focus."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = _query_process_name(pid.value)
    title = _query_window_title(hwnd)
    stem = name.rsplit(".", 1)[0] if name else ""
    return ForegroundWindow(process_name=name, process_stem=stem, title=title)


# ---------------------------------------------------------------------------
# Text injection (via clipboard + Ctrl+V)
# ---------------------------------------------------------------------------

CF_UNICODETEXT = 13

kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = ()
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
user32.CloseClipboard.argtypes = ()
user32.CloseClipboard.restype = wintypes.BOOL


def _set_clipboard_text(text: str) -> None:
    """Replace the clipboard with ``text`` (Unicode)."""
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        # Allocate global memory for the string (+NUL).
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(0x0042, len(encoded))  # GMEM_MOVEABLE|ZEROINIT
        if not h_mem:
            raise OSError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
    finally:
        user32.CloseClipboard()


def type_text(text: str) -> None:
    """Inject arbitrary text via clipboard-paste.

    Long pastes and Unicode (CJK, emoji) work reliably this way; SendInput
    per-character would mangle anything outside the current keyboard layout.
    Saves and restores nothing - if you need to preserve clipboard, do it at
    a higher layer.
    """
    _set_clipboard_text(text)
    time.sleep(0.02)  # let the OS settle the clipboard
    tap_chord([resolve_vk("ctrl"), resolve_vk("v")])
