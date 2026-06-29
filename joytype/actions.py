"""Executable actions bound to controller inputs.

Each action is a small object with a ``fire()`` method. The binder builds
these from raw ``config.yaml`` dicts; the executor (this module) knows how to
turn each one into Win32 calls. Keeping them as objects (rather than executing
immediately at bind time) lets us support hold/release semantics cleanly:
the binder calls :meth:`Action.on_press` / :meth:`Action.on_release`, and
stateful actions like VoiceAction remember whether they're "holding".
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from .config import ConfigError, _parse_hotkey  # noqa: F401 (re-exported helper)
from .desktop import DesktopAdapter, KeyChord, WindowsDesktopAdapter

log = logging.getLogger(__name__)

# Optional latency instrumentation. Set env INPUT_PLATFORM_TIMING=1 or
# JOYTYPE_TIMING=1 to print a
# millisecond-stamped line each time a voice hotkey is injected/released, so
# the end-to-end delay (controller -> JoyType -> dictation tool) can be measured
# against a screen recording. Off by default (no production noise).
_TIMING = bool(
    os.environ.get("INPUT_PLATFORM_TIMING") or os.environ.get("JOYTYPE_TIMING")
)


def _stamp() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


# ---------------------------------------------------------------------------
# Action base + simple key/chord actions
# ---------------------------------------------------------------------------

class Action:
    """Base class. Subclasses override on_press / on_release as needed."""

    def on_press(self, ctx: "ActionContext") -> None:  # noqa: D401 - thin base
        """Called once when the bound button transitions to pressed."""

    def on_release(self, ctx: "ActionContext") -> None:
        """Called once when the bound button transitions to released."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"{type(self).__name__}()"


@dataclass
class KeyAction(Action):
    """Tap a single virtual key."""

    key: str

    def on_press(self, ctx: "ActionContext") -> None:
        ctx.desktop.tap_key(ctx.desktop.resolve_key(self.key))


@dataclass
class ChordAction(Action):
    """Tap a modifier chord, e.g. Ctrl+Shift+P."""

    chord: KeyChord

    def on_press(self, ctx: "ActionContext") -> None:
        ctx.desktop.tap_chord(self.chord.native_keys(ctx.desktop))


@dataclass
class AltTabAction(Action):
    """Show the Windows Alt+Tab switcher and commit after input settles.

    The UI still exposes this as a plain KEYBOARD chord (`ALT+TAB` or
    `SHIFT+ALT+TAB`). Internally it needs Windows-switcher semantics: keep Alt
    held across repeated presses so the candidate window strip stays visible,
    tap Tab for each press, then release Alt shortly after the last press.
    """

    forward: bool = True
    commit_ms: int = 800
    alt_token: str = "alt"
    shift_token: str = "shift"
    tab_token: str = "tab"

    _alt_down: ClassVar[bool] = False
    _held_alt_token: ClassVar[Optional[str]] = None
    _held_desktop: ClassVar[Optional[DesktopAdapter]] = None
    _timer: ClassVar[Optional[threading.Timer]] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def on_press(self, ctx: "ActionContext") -> None:
        cls = AltTabAction
        with cls._lock:
            if not cls._alt_down:
                ctx.desktop.press_key(ctx.desktop.resolve_key(self.alt_token))
                cls._alt_down = True
                cls._held_alt_token = self.alt_token
                cls._held_desktop = ctx.desktop
            if self.forward:
                ctx.desktop.tap_key(ctx.desktop.resolve_key(self.tab_token))
            else:
                ctx.desktop.tap_chord([
                    ctx.desktop.resolve_key(self.shift_token),
                    ctx.desktop.resolve_key(self.tab_token),
                ])
            self._arm_commit(ctx.desktop)

    def _arm_commit(self, desktop: DesktopAdapter) -> None:
        cls = AltTabAction
        if cls._timer is not None:
            cls._timer.cancel()
        if cls._held_desktop is None:
            cls._held_desktop = desktop
        cls._timer = threading.Timer(self.commit_ms / 1000.0, AltTabAction._commit)
        cls._timer.daemon = True
        cls._timer.start()

    @staticmethod
    def _commit() -> None:
        cls = AltTabAction
        with cls._lock:
            cls._timer = None
            if cls._alt_down:
                desktop = cls._held_desktop or ActionContext().desktop
                token = cls._held_alt_token or "alt"
                try:
                    desktop.release_key(desktop.resolve_key(token))
                finally:
                    cls._alt_down = False
                    cls._held_alt_token = None
                    cls._held_desktop = None

    def panic_release(self, ctx: "ActionContext | None" = None) -> None:
        cls = AltTabAction
        with cls._lock:
            if cls._timer is not None:
                cls._timer.cancel()
                cls._timer = None
            if cls._alt_down:
                desktop = (
                    cls._held_desktop
                    or (ctx.desktop if ctx is not None else ActionContext().desktop)
                )
                token = cls._held_alt_token or self.alt_token
                try:
                    desktop.release_key(desktop.resolve_key(token))
                finally:
                    cls._alt_down = False
                    cls._held_alt_token = None
                    cls._held_desktop = None


# ---------------------------------------------------------------------------
# Stateful actions: voice (hold-to-talk)
# ---------------------------------------------------------------------------

@dataclass
class VoiceAction(Action):
    """Drive a dictation tool's hotkey for push-to-talk or toggle.

    Two modes:

    - ``mode="hold"`` (push-to-talk): genuinely hold the chord DOWN while the
      controller button is held, then release it when the button releases.
      This is what a dictation tool's "press and hold to talk" hotkey expects.
      The chord MUST be free of Alt - a held Alt activates Win32 menu access
      keys (Alt -> File etc.); configure a Shift/Ctrl-based hotkey instead.

    - ``mode="toggle"`` (latching): tap the chord once per press; the dictation
      tool flips itself on/off. ``on_release`` does nothing.

    Safety: ``_held`` tracks whether hold mode currently has the chord down.
    The release path runs through the configured desktop adapter's
    ``release_chord`` (the earlier ctypes bug used to drop the key-UP half and
    strand modifiers).
    As a belt-and-suspenders guard, :meth:`panic_release` lifts the chord
    unconditionally - the daemon calls it on disconnect / shutdown so a lost
    release edge can never leave modifiers stuck down.
    """

    chord: KeyChord
    mode: str = "hold"
    _held: bool = field(default=False, init=False)
    _held_desktop: Optional[DesktopAdapter] = field(default=None, init=False)

    def on_press(self, ctx: "ActionContext") -> None:
        if self.mode == "toggle":
            ctx.desktop.tap_chord(self.chord.native_keys(ctx.desktop))
            if _TIMING:
                log.info("[TIMING] voice TOGGLE  injected @ %s", _stamp())
            return
        # Push-to-talk: hold the chord down for the duration of the press.
        if self._held:
            return  # already down (jitter double-press) - ignore
        ctx.desktop.press_chord(self.chord.native_keys(ctx.desktop))
        self._held = True
        self._held_desktop = ctx.desktop
        if _TIMING:
            log.info("[TIMING] voice HOLD start injected @ %s", _stamp())

    def on_release(self, ctx: "ActionContext") -> None:
        if self.mode != "hold":
            return  # toggle mode latches on press only
        if not self._held:
            return  # spurious release while not held - jitter, ignore
        desktop = self._held_desktop or ctx.desktop
        desktop.release_chord(self.chord.native_keys(desktop))
        self._held = False
        self._held_desktop = None
        if _TIMING:
            log.info("[TIMING] voice HOLD stop  released @ %s", _stamp())

    def panic_release(self, ctx: "ActionContext | None" = None) -> None:
        """Unconditionally lift the chord if hold mode left it down.

        Called by the daemon on disconnect / shutdown so a missing release
        edge can never strand modifier keys in the down state.
        """
        if self._held:
            desktop = self._held_desktop or (
                ctx.desktop if ctx is not None else ActionContext().desktop
            )
            try:
                desktop.release_chord(self.chord.native_keys(desktop))
            finally:
                self._held = False
                self._held_desktop = None

# ---------------------------------------------------------------------------
# Execution context (passed to every fire())
# ---------------------------------------------------------------------------

@dataclass
class ActionContext:
    """Per-dispatch context object passed to every action call."""

    desktop: DesktopAdapter = field(default_factory=WindowsDesktopAdapter)


# ---------------------------------------------------------------------------
# Factory: turn a raw YAML action dict into an Action object
# ---------------------------------------------------------------------------

def _default_desktop(desktop: DesktopAdapter | None) -> DesktopAdapter:
    return desktop or WindowsDesktopAdapter()


def _normalize_key(token: Any) -> str:
    return str(token).strip().lower()


def _validate_key(token: str, desktop: DesktopAdapter, where: str) -> None:
    try:
        desktop.resolve_key(token)
    except KeyError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _validate_chord(chord: KeyChord, desktop: DesktopAdapter, where: str) -> list[int]:
    if not chord:
        raise ConfigError(f"{where}: empty hotkey")
    try:
        return chord.native_keys(desktop)
    except KeyError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _resolve_supported(desktop: DesktopAdapter, tokens: tuple[str, ...]) -> set[int]:
    resolved: set[int] = set()
    for token in tokens:
        try:
            resolved.add(desktop.resolve_key(token))
        except KeyError:
            pass
    return resolved


def _action_from_dict(
    raw: dict, where: str, desktop: DesktopAdapter | None = None
) -> Action:
    """Build the right Action subclass from a parsed config dict."""
    desktop = _default_desktop(desktop)
    keys = set(raw.keys())

    if "key" in raw:
        key = _normalize_key(raw["key"])
        _validate_key(key, desktop, where)
        return KeyAction(key=key)

    if "hotkey" in raw:
        chord = _parse_hotkey(raw["hotkey"], where)
        native_keys = _validate_chord(chord, desktop, where)
        alt_tab = _alt_tab_action_from_hotkey(chord, native_keys, desktop)
        if alt_tab is not None:
            return alt_tab
        if len(chord.tokens) == 1:
            return KeyAction(key=chord.tokens[0])
        return ChordAction(chord=chord)

    if "voice" in raw and raw["voice"]:
        # Voice config is resolved at binder build time and attached by the
        # caller via `_with_voice`. Bare {voice: true} becomes a placeholder
        # we can't resolve here; the binder swaps it in.
        raise ConfigError(
            f"{where}: {{voice: true}} requires a voice.* config block"
        )

    if "voice_toggle" in raw and raw["voice_toggle"]:
        # Same dance as {voice: true}, but resolves to the toggle-hotkey
        # VoiceAction instead of the hold-hotkey one.
        raise ConfigError(
            f"{where}: {{voice_toggle: true}} requires a voice.* config block"
        )

    raise ConfigError(f"{where}: action dict has no recognized key. Got: {sorted(keys)}")


def _alt_tab_action_from_hotkey(
    chord: KeyChord, native_keys: list[int], desktop: DesktopAdapter
) -> Optional[AltTabAction]:
    """Map ALT+TAB chords to Windows switcher semantics."""
    try:
        tab_vk = desktop.resolve_key("tab")
    except KeyError:
        return None
    alt_vks = _resolve_supported(
        desktop, ("alt", "lalt", "ralt", "menu", "lmenu", "rmenu")
    )
    shift_vks = _resolve_supported(desktop, ("shift", "lshift", "rshift"))
    if native_keys.count(tab_vk) != 1:
        return None
    alts = [token for token, vk in zip(chord.tokens, native_keys) if vk in alt_vks]
    shifts = [token for token, vk in zip(chord.tokens, native_keys) if vk in shift_vks]
    allowed = alt_vks | shift_vks | {tab_vk}
    if len(alts) != 1 or any(vk not in allowed for vk in native_keys):
        return None
    if len(native_keys) == 2 and not shifts:
        return AltTabAction(forward=True, alt_token=alts[0])
    if len(native_keys) == 3 and len(shifts) == 1:
        return AltTabAction(forward=False, alt_token=alts[0], shift_token=shifts[0])
    return None


def build_action(
    raw: Any, where: str, *, desktop: DesktopAdapter | None = None
) -> Action:
    """Public entry point: any config action spec -> an :class:`Action`."""
    if isinstance(raw, str):
        # Shorthand: "B: escape"  ->  {key: escape}
        raw = {"key": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: action must be a string or mapping")
    return _action_from_dict(raw, where, desktop=desktop)


def make_voice_action(
    hotkey: KeyChord, mode: str, where: str, *, desktop: DesktopAdapter | None = None
) -> VoiceAction:
    """Construct a VoiceAction, validating that a hotkey was configured."""
    desktop = _default_desktop(desktop)
    chord = hotkey if isinstance(hotkey, KeyChord) else _parse_hotkey(hotkey, where)
    if not chord:
        raise ConfigError(f"{where}: voice action but no hotkey configured")
    try:
        chord.native_keys(desktop)
    except KeyError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    return VoiceAction(chord=chord, mode=mode)
