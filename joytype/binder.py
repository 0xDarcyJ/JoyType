"""The core router: controller events -> actions.

This is the heart of JoyType. It consumes a stream of
:class:`~joytype.state.ControllerState` snapshots and decides, for each one:

1. Which profile is active? (Auto-detected from the foreground window, with
   an optional manual override.)
2. For each button edge, which action (if any) fires?
3. For each stick frame, what mouse motion (if any) should be applied?

Mouse integration runs on a fixed timebase so motion is framerate-
independent: the main loop calls :meth:`Binder.tick` once per HID frame with
the elapsed time, and the binder scales stick deflection by ``dt`` to produce
pixels-per-second motion that feels the same regardless of how fast the
controller reports.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from inspect import Parameter, signature
from typing import Optional

from .actions import (
    Action,
    ActionContext,
    VoiceAction,
    build_action,
    make_voice_action,
)
from .config import Config, ConfigError, ProfileConfig
from .desktop import DesktopAdapter, ForegroundWindow, WindowsDesktopAdapter
from .state import Button, ButtonEvent, ControllerState, StickState, diff_states

log = logging.getLogger(__name__)

# Sentinel profile name meaning "stop overriding, go back to auto-detect".
AUTO_PROFILE = "__auto__"

# How many consecutive frames a button must STAY released before we fire its
# on_release event. At ~60-120Hz HID this is 25-50ms - imperceptible to the
# user but long enough to swallow single-frame Bluetooth jitter. Set to 0 to
# disable debouncing entirely (useful for tests).
RELEASE_DEBOUNCE_FRAMES = 3

# How often (seconds) to re-query the foreground window to pick the active
# profile. The query is comparatively expensive, so we keep it off the
# per-frame mouse path; 0.12s is well under human window-switch reaction time.
PROFILE_RECHECK_INTERVAL = 0.12

# Trigger gesture defaults. These only apply to bindings that opt into
# `triggers`; plain one-action bindings still fire immediately.
DEFAULT_DOUBLE_MS = 280
DEFAULT_HOLD_MS = 350


# ---------------------------------------------------------------------------
# Compiled profile: buttons -> Action objects, ready to fire
# ---------------------------------------------------------------------------

@dataclass
class CompiledProfile:
    """A profile whose raw YAML actions have been turned into Action objects."""

    name: str
    bindings: dict[Button, "TriggerBinding"] = field(default_factory=dict)


@dataclass
class HoldTrigger:
    """Compiled hold gesture: threshold plus action."""

    action: Action
    after_s: float = DEFAULT_HOLD_MS / 1000.0
    on_release: bool = False


@dataclass
class TriggerBinding:
    """All gestures assigned to one physical button."""

    press: Optional[Action] = None
    double: Optional[Action] = None
    hold: Optional[HoldTrigger] = None
    double_s: float = DEFAULT_DOUBLE_MS / 1000.0

    @property
    def fires_press_immediately(self) -> bool:
        return self.press is not None and self.double is None and self.hold is None


@dataclass
class ButtonTriggerRuntime:
    """In-flight gesture state for one button."""

    binding: TriggerBinding
    pressed: bool = False
    pressed_at: float = 0.0
    pending_deadline: Optional[float] = None
    pending_press: Optional[Action] = None
    active_action: Optional[Action] = None
    hold_ready: bool = False
    hold_fired: bool = False


class Binder:
    """Routes controller state to actions.

    Lifecycle:
        binder = Binder(cfg)
        for state in hid_stream:
            binder.update(state, dt)
    """

    def __init__(self, cfg: Config, *, desktop: DesktopAdapter | None = None) -> None:
        self.cfg = cfg
        self.desktop: DesktopAdapter = desktop or WindowsDesktopAdapter()

        # Last seen snapshot - used for edge detection.
        self._prev: Optional[ControllerState] = None
        # Timestamp of the previous update; for dt computation on mouse motion.
        self._last_t: float = 0.0

        # Button-release debounce. HID streams occasionally report a button
        # as released for one frame even while it's physically held (Bluetooth
        # packet loss, sensor noise). For stateful actions like voice
        # hold-to-talk, that one-frame release fires on_release prematurely,
        # confusing the dictation tool. We hold each "release" event for
        # RELEASE_DEBOUNCE_FRAMES additional frames; if the button is back to
        # pressed by then, we drop the release entirely.
        self._release_debounce: dict[Button, int] = {}
        # Monotonic gesture clock advanced by HID frame dt. Tests can pass dt
        # directly; production uses the daemon's measured frame interval.
        self._trigger_clock: float = 0.0
        self._button_triggers: dict[Button, ButtonTriggerRuntime] = {}

        # Currently active profile name. ``None`` means "use auto-detect".
        # Manual override can be driven by the GUI / API.
        self._manual_profile: Optional[str] = None
        self._active_name: str = self._default_name
        # Throttle the foreground-window query that selects the profile. Doing
        # it every HID frame stalls the mouse loop (the query can spike a few
        # ms), inflating the next frame's dt and making the cursor jump. We
        # re-check at most every PROFILE_RECHECK_INTERVAL seconds and reuse the
        # cached profile in between.
        self._profile_cache_t: float = 0.0
        # Sub-pixel mouse accumulator: carry fractional pixels across frames so
        # slow / fine stick motion moves smoothly instead of truncating to 0.
        self._mouse_accum_x: float = 0.0
        self._mouse_accum_y: float = 0.0
        # Latest shaped stick deflection, written by update() (HID thread) and
        # read by tick_mouse() (the fixed-rate mouse thread). Decoupling the two
        # keeps cursor motion smooth despite irregular Bluetooth report timing.
        self._latest_left: StickState = StickState()
        self._latest_right: StickState = StickState()

        # Action context handed to every fire().
        self._ctx = ActionContext(desktop=self.desktop)

        # Pre-built voice actions, shared by every profile.
        #   {voice: true}        -> hold-to-talk (holds hotkey while pressed)
        #   {voice_toggle: true} -> tap-to-toggle (toggles hotkey on each press)
        # Both are built once and reused so a press never constructs them.
        # MUST be built before _compile_profiles(), which references them.
        self._voice_hold_action: Optional[VoiceAction] = None
        self._voice_toggle_action: Optional[VoiceAction] = None
        # Two distinct dictation hotkeys, distinct gestures:
        #   hold_hotkey   -> {voice: true}        genuine press-and-hold (PTT)
        #   toggle_hotkey -> {voice_toggle: true} tap-to-toggle (latching)
        if cfg.voice.hold_hotkey:
            self._voice_hold_action = make_voice_action(
                cfg.voice.hold_hotkey,
                "hold",
                "voice.hold_hotkey",
                desktop=self.desktop,
            )
        if cfg.voice.toggle_hotkey:
            self._voice_toggle_action = make_voice_action(
                cfg.voice.toggle_hotkey,
                "toggle",
                "voice.toggle_hotkey",
                desktop=self.desktop,
            )

        # Compile profiles last - it references the voice actions above.
        self._compile_profiles()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @property
    def _default_name(self) -> str:
        """Name of the fallback profile (the one with no `match`)."""
        prof = self.cfg.profiles.get("default")
        if prof is not None and prof.match is None:
            return "default"
        for name, prof in self.cfg.profiles.items():
            if prof.match is None:
                return name
        return "default"

    def _compile_profiles(self) -> None:
        """Pre-build Action objects for every profile.

        Compilation happens up front so a button press never pays the cost
        of resolving key names or constructing chord lists at fire time; it
        also surfaces all config errors immediately on startup.
        """
        self._compiled: dict[str, CompiledProfile] = {}
        for name, prof in self.cfg.profiles.items():
            bindings: dict[Button, TriggerBinding] = {}
            for btn, raw in prof.bindings.items():
                where = f"profiles.{name}.{btn.value}"
                bindings[btn] = self._compile_binding(raw, where)
            self._compiled[name] = CompiledProfile(name=name, bindings=bindings)

    def _compile_binding(self, raw, where: str) -> TriggerBinding:
        """Compile either a legacy action or a `{triggers: ...}` binding."""
        if isinstance(raw, dict) and "triggers" in raw:
            triggers = raw["triggers"]
            if not isinstance(triggers, dict):
                raise ConfigError(f"{where}.triggers: must be a mapping")
            self._reject_push_to_talk_trigger(triggers, where)
            double_ms = int(triggers.get("double_ms", DEFAULT_DOUBLE_MS))
            if double_ms <= 0:
                raise ConfigError(f"{where}.triggers.double_ms: must be > 0")
            binding = TriggerBinding(double_s=double_ms / 1000.0)
            if "press" in triggers and triggers["press"] is not None:
                binding.press = self._compile_action(
                    triggers["press"], f"{where}.triggers.press"
                )
            if "double" in triggers and triggers["double"] is not None:
                binding.double = self._compile_action(
                    triggers["double"], f"{where}.triggers.double"
                )
            if "hold" in triggers and triggers["hold"] is not None:
                binding.hold = self._compile_hold_trigger(
                    triggers["hold"], f"{where}.triggers.hold"
                )
            if binding.press is None and binding.double is None and binding.hold is None:
                raise ConfigError(f"{where}.triggers: at least one trigger is required")
            return binding
        return TriggerBinding(press=self._compile_action(raw, where))

    def _reject_push_to_talk_trigger(self, triggers: dict, where: str) -> None:
        for trigger_name in ("press", "double"):
            if self._is_voice_hold_placeholder(triggers.get(trigger_name)):
                raise ConfigError(
                    f"{where}.triggers.{trigger_name}: PUSH TO TALK must be a "
                    "direct {voice: true} binding, not a gesture trigger"
                )
        hold = triggers.get("hold")
        hold_action = hold.get("action") if isinstance(hold, dict) else hold
        if self._is_voice_hold_placeholder(hold_action):
            raise ConfigError(
                f"{where}.triggers.hold: PUSH TO TALK must be a direct "
                "{voice: true} binding, not a gesture trigger"
            )

    def _compile_hold_trigger(self, raw, where: str) -> HoldTrigger:
        if isinstance(raw, dict) and "action" in raw:
            action_raw = raw["action"]
            after_ms = int(raw.get("after_ms", DEFAULT_HOLD_MS))
            on_release = bool(raw.get("on_release", False))
        else:
            action_raw = raw
            after_ms = DEFAULT_HOLD_MS
            on_release = False
        if after_ms <= 0:
            raise ConfigError(f"{where}.after_ms: must be > 0")
        return HoldTrigger(
            action=self._compile_action(action_raw, f"{where}.action"),
            after_s=after_ms / 1000.0,
            on_release=on_release,
        )

    def _compile_action(self, raw, where: str) -> Action:
        # Detect {voice: true} / {voice_toggle: true} placeholders BEFORE
        # build_action, which would otherwise raise. The presence of these
        # placeholders is only an error when no voice.* block exists.
        if self._is_voice_hold_placeholder(raw):
            if self._voice_hold_action is None:
                raise ConfigError(
                    f"{where}: {{voice: true}} but no voice.hold_hotkey configured"
                )
            return self._voice_hold_action
        if self._is_voice_toggle_placeholder(raw):
            if self._voice_toggle_action is None:
                raise ConfigError(
                    f"{where}: {{voice_toggle: true}} but no voice.toggle_hotkey "
                    f"configured"
                )
            return self._voice_toggle_action
        return build_action(raw, where, desktop=self.desktop)

    @staticmethod
    def _is_voice_hold_placeholder(raw) -> bool:
        return (
            isinstance(raw, dict)
            and raw.get("voice") is True
            and len(raw) == 1
        )

    @staticmethod
    def _is_voice_toggle_placeholder(raw) -> bool:
        return (
            isinstance(raw, dict)
            and raw.get("voice_toggle") is True
            and len(raw) == 1
        )

    def panic_release_all(self) -> None:
        """Force-release every stateful action that may hold keys down.

        Called by the daemon on disconnect / shutdown so a held push-to-talk
        voice chord can never strand keys down and jam the keyboard. Safe to
        call repeatedly.
        """
        seen: set[int] = set()
        actions: list = [self._voice_hold_action, self._voice_toggle_action]
        for prof in self._compiled.values():
            for binding in prof.bindings.values():
                actions.extend(self._iter_binding_actions(binding))
        for rt in self._button_triggers.values():
            if rt.active_action is not None:
                actions.append(rt.active_action)
        for act in actions:
            if act is None or id(act) in seen:
                continue
            seen.add(id(act))
            panic = getattr(act, "panic_release", None)
            if panic is not None:
                try:
                    self._call_panic_release(panic)
                except Exception:
                    log.exception("panic_release failed for %r", act)
        self._button_triggers.clear()

    def _call_panic_release(self, panic) -> None:
        try:
            sig = signature(panic)
        except (TypeError, ValueError):
            panic(self._ctx)
            return
        params = sig.parameters.values()
        accepts_arg = any(
            p.kind == Parameter.VAR_POSITIONAL
            or p.kind in (
                Parameter.POSITIONAL_ONLY,
                Parameter.POSITIONAL_OR_KEYWORD,
                Parameter.KEYWORD_ONLY,
            )
            for p in params
        )
        if accepts_arg:
            panic(self._ctx)
        else:
            panic()

    @staticmethod
    def _iter_binding_actions(binding: TriggerBinding) -> list[Action]:
        actions: list[Action] = []
        if binding.press is not None:
            actions.append(binding.press)
        if binding.double is not None:
            actions.append(binding.double)
        if binding.hold is not None:
            actions.append(binding.hold.action)
        return actions

    # ------------------------------------------------------------------
    # Profile selection
    # ------------------------------------------------------------------

    def set_profile(self, name: str) -> None:
        """Switch to a named profile, or back to auto-detect with AUTO_PROFILE.

        Unknown profile names are warned and ignored - the user keeps typing
        in config and we don't want a half-edited file to brick the daemon.
        """
        if name == AUTO_PROFILE:
            self._manual_profile = None
            log.info("Profile override cleared; back to auto-detect.")
            return
        if name not in self._compiled:
            log.warning("Unknown profile %r; ignoring switch.", name)
            return
        self._manual_profile = name
        log.info("Profile manually set to %r.", name)

    def _auto_select_profile(self) -> str:
        """Pick a profile by matching the current foreground window."""
        fg = self.desktop.get_foreground_window()
        if fg is None:
            return self._default_name

        for name, prof in self.cfg.profiles.items():
            if prof.match is None:
                continue  # default profile handled last
            if self._match(fg, prof):
                return name
        return self._default_name

    @staticmethod
    def _match(fg: ForegroundWindow, prof: ProfileConfig) -> bool:
        rule = prof.match
        assert rule is not None
        # Process match: compare against either the full name (with .exe) or
        # the stem. We lower-case both sides when process_icase is set.
        if rule.process:
            stem = fg.process_stem.lower() if rule.process_icase else fg.process_stem
            full = fg.process_name.lower() if rule.process_icase else fg.process_name
            wanted = [p.lower() for p in rule.process] if rule.process_icase else rule.process
            if stem not in wanted and full not in wanted:
                return False
        if rule.title_regex is not None and not rule.title_regex.search(fg.title):
            return False
        return True

    def _current_profile(self) -> CompiledProfile:
        """Resolve the actually-active compiled profile for this frame.

        Auto-detect is throttled to PROFILE_RECHECK_INTERVAL so the expensive
        foreground-window query stays off the per-frame mouse path; between
        checks we reuse the last-selected profile.
        """
        if self._manual_profile is not None:
            return self._compiled[self._manual_profile]
        now = time.perf_counter()
        if now - self._profile_cache_t >= PROFILE_RECHECK_INTERVAL:
            self._profile_cache_t = now
            name = self._auto_select_profile()
            if name != self._active_name:
                log.debug("Auto profile: %s -> %s", self._active_name, name)
                self._active_name = name
        return self._compiled[self._active_name]

    @property
    def active_profile_name(self) -> str:
        return self._manual_profile or self._active_name

    # ------------------------------------------------------------------
    # Main entry: ingest one HID frame
    # ------------------------------------------------------------------

    def update(self, state: ControllerState, dt: Optional[float] = None) -> None:
        """Process one parsed controller snapshot.

        ``dt`` is seconds since the previous call. When omitted we derive it
        from the wall clock; pass an explicit value in tests for determinism.
        """
        now = time.perf_counter()
        if dt is None:
            dt = max(now - self._last_t, 1e-3) if self._last_t else 1 / 120
        self._last_t = now
        self._trigger_clock += dt

        profile = self._current_profile()
        self._process_trigger_timers()

        # --- Button edges -> bound actions (with release debouncing) -------
        # We fire presses immediately (latency-sensitive) but delay releases.
        # A "release" only counts if the button stays released across multiple
        # consecutive frames; this swallows single-frame Bluetooth jitter that
        # would otherwise fire on_release prematurely mid-hold on a stateful
        # action like voice hold-to-talk.
        was_held = self._prev.buttons if self._prev is not None else frozenset()
        is_held = state.buttons

        # Cancel pending release-debounce for any button that's held again.
        for btn in list(self._release_debounce.keys()):
            if btn in is_held:
                self._release_debounce.pop(btn, None)

        # Presses: any button newly held (wasn't in prev, is now).
        for btn in is_held - was_held:
            self._release_debounce.pop(btn, None)
            self._fire(ButtonEvent(btn, pressed=True), profile)

        # Releases: any button newly released (was in prev, isn't now).
        # But don't fire immediately - increment its debounce counter and only
        # fire once it's stayed released past RELEASE_DEBOUNCE_FRAMES.
        for btn in was_held - is_held:
            count = self._release_debounce.get(btn, 0) + 1
            if count > RELEASE_DEBOUNCE_FRAMES:
                self._release_debounce.pop(btn, None)
                self._fire(ButtonEvent(btn, pressed=False), profile)
            else:
                self._release_debounce[btn] = count

        # Now handle buttons that are STILL released (in debounce countdown).
        # These don't appear in the was/is diff above (no transition this
        # frame), but each frame they stay released increments their counter.
        for btn in list(self._release_debounce.keys()):
            if btn not in was_held and btn not in is_held:
                # Was already released last frame, still released now.
                # (count was incremented when the transition happened, so just
                # continue counting from there.)
                count = self._release_debounce[btn] + 1
                if count > RELEASE_DEBOUNCE_FRAMES:
                    self._release_debounce.pop(btn, None)
                    self._fire(ButtonEvent(btn, pressed=False), profile)
                else:
                    self._release_debounce[btn] = count

        # Stash the latest deflection for the fixed-rate mouse thread (see
        # tick_mouse), and handle the edge-based click here on the HID thread.
        self._latest_left = state.left
        self._latest_right = state.right
        self._handle_mouse_click(state)

        self._prev = state

    def _fire(self, ev, profile: CompiledProfile) -> None:
        """Dispatch one button event to its bound trigger binding.

        Profile INHERITANCE: a button not bound in the active profile falls back
        to the ``default`` profile. So a per-app profile only lists the buttons
        it OVERRIDES (e.g. MINUS = send key) and inherits everything else -
        voice, keyboard shortcuts, arrows - from default.
        """
        if ev.pressed:
            self._on_button_press(ev.button, profile)
        else:
            self._on_button_release(ev.button)

    def _binding_for(
        self, btn: Button, profile: CompiledProfile
    ) -> Optional[TriggerBinding]:
        binding = profile.bindings.get(btn)
        if binding is None:
            base_name = self.cfg.profiles[profile.name].base
            if base_name is None and profile.name != self._default_name:
                base_name = self._default_name
            if base_name is not None and base_name != profile.name:
                base = self._compiled.get(base_name)
                if base is not None:
                    binding = base.bindings.get(btn)
        return binding

    def _on_button_press(self, btn: Button, profile: CompiledProfile) -> None:
        now = self._trigger_clock
        rt = self._button_triggers.get(btn)
        if (
            rt is not None
            and rt.pending_deadline is not None
            and now <= rt.pending_deadline
            and rt.binding.double is not None
        ):
            rt.pending_deadline = None
            rt.pending_press = None
            rt.pressed = True
            rt.pressed_at = now
            rt.hold_ready = False
            rt.hold_fired = False
            rt.active_action = rt.binding.double
            self._press_action(btn, rt.binding.double)
            return

        binding = self._binding_for(btn, profile)
        if binding is None:
            self._button_triggers.pop(btn, None)
            return

        rt = ButtonTriggerRuntime(binding=binding, pressed=True, pressed_at=now)
        self._button_triggers[btn] = rt
        if binding.fires_press_immediately:
            rt.active_action = binding.press
            self._press_action(btn, binding.press)

    def _on_button_release(self, btn: Button) -> None:
        rt = self._button_triggers.get(btn)
        if rt is None:
            return
        now = self._trigger_clock
        rt.pressed = False

        if rt.active_action is not None:
            self._release_action(btn, rt.active_action)
            self._button_triggers.pop(btn, None)
            return

        hold = rt.binding.hold
        held_long_enough = (
            hold is not None
            and (rt.hold_ready or (now - rt.pressed_at) >= hold.after_s)
        )
        if held_long_enough:
            if hold.on_release:
                self._tap_action(btn, hold.action)
            self._button_triggers.pop(btn, None)
            return

        if rt.binding.double is not None:
            rt.pending_deadline = now + rt.binding.double_s
            rt.pending_press = rt.binding.press
            return

        if rt.binding.press is not None:
            self._tap_action(btn, rt.binding.press)
        self._button_triggers.pop(btn, None)

    def _process_trigger_timers(self) -> None:
        now = self._trigger_clock
        for btn, rt in list(self._button_triggers.items()):
            if rt.pending_deadline is not None and now >= rt.pending_deadline:
                if rt.pending_press is not None:
                    self._tap_action(btn, rt.pending_press)
                self._button_triggers.pop(btn, None)
                continue

            hold = rt.binding.hold
            if (
                rt.pressed
                and hold is not None
                and not rt.hold_fired
                and not rt.hold_ready
                and (now - rt.pressed_at) >= hold.after_s
            ):
                if hold.on_release:
                    rt.hold_ready = True
                else:
                    rt.active_action = hold.action
                    rt.hold_fired = True
                    self._press_action(btn, hold.action)

    def _press_action(self, btn: Button, action: Optional[Action]) -> None:
        if action is None:
            return
        try:
            action.on_press(self._ctx)
        except Exception:
            log.exception("Action %r for %s press failed", action, btn.value)

    def _release_action(self, btn: Button, action: Optional[Action]) -> None:
        if action is None:
            return
        try:
            action.on_release(self._ctx)
        except Exception:
            log.exception("Action %r for %s release failed", action, btn.value)

    def _tap_action(self, btn: Button, action: Optional[Action]) -> None:
        if action is None:
            return
        self._press_action(btn, action)
        self._release_action(btn, action)

    # ------------------------------------------------------------------
    # Mouse integration
    # ------------------------------------------------------------------

    def stick_snapshot(self) -> dict[str, dict[str, float]]:
        """Latest shaped stick deflection for GUI instrumentation.

        The mouse loop already reads these cached values, so the GUI preview
        should observe the same source of truth instead of animating its own
        decorative state.
        """
        def pack(stick: StickState) -> dict[str, float]:
            return {
                "x": stick.x,
                "y": stick.y,
                "magnitude": min(stick.magnitude, 1.0),
            }

        return {"left": pack(self._latest_left), "right": pack(self._latest_right)}

    def tick_mouse(self, dt: float) -> None:
        """Move/scroll from the LATEST stick deflection on a fixed timebase.

        Called by the daemon's dedicated mouse thread (not the HID loop), so
        cursor motion is smooth regardless of how irregularly Bluetooth reports
        arrive: between reports the deflection is constant, so this emits steady
        constant-velocity motion. Sub-pixel remainder carries across ticks.
        """
        mc = self.cfg.mouse
        fdx = fdy = 0.0
        if mc.left_stick == "move":
            ddx, ddy = self._stick_to_delta(self._latest_left, mc.speed, mc.acceleration, dt)
            fdx += ddx
            fdy += ddy
        if mc.right_stick == "move":
            ddx, ddy = self._stick_to_delta(self._latest_right, mc.speed, mc.acceleration, dt)
            fdx += ddx
            fdy += ddy
        if fdx or fdy:
            self._mouse_accum_x += fdx
            self._mouse_accum_y += fdy
            idx = int(self._mouse_accum_x)
            idy = int(self._mouse_accum_y)
            if idx or idy:
                self._mouse_accum_x -= idx
                self._mouse_accum_y -= idy
                self.desktop.move_mouse_relative(idx, idy)

        if mc.right_stick == "scroll":
            clicks = self._stick_to_scroll(self._latest_right, mc.scroll_speed, dt)
            if clicks:
                self.desktop.scroll_wheel(clicks)

    def reset_sticks(self) -> None:
        """Zero the cached deflection so the cursor stops (call on disconnect)."""
        self._latest_left = StickState()
        self._latest_right = StickState()
        self._mouse_accum_x = 0.0
        self._mouse_accum_y = 0.0

    def _handle_mouse_click(self, state: ControllerState) -> None:
        """Fire a left click on the click-button's rising edge (HID thread)."""
        mc = self.cfg.mouse
        if mc.click_button is not None:
            prev_held = self._prev is not None and self._prev.is_pressed(mc.click_button)
            now_held = state.is_pressed(mc.click_button)
            if now_held and not prev_held:
                self.desktop.click_mouse("left")

    @staticmethod
    def _stick_to_delta(
        stick: StickState, speed: int, accel: float, dt: float
    ) -> tuple[float, float]:
        """Deflection (already dead-zoned) -> sub-pixel delta for this frame.

        Returns floats; the caller accumulates the fractional remainder across
        frames so slow motion stays smooth. The acceleration curve maps
        magnitude through ``mag ** (1 / accel)`` so a full push (1.0) stays at
        1.0 while larger acceleration values make partial deflections faster.
        """
        mag = min(stick.magnitude, 1.0)
        if mag == 0.0:
            return 0.0, 0.0
        accel = max(accel, 0.1)
        scaled = mag ** (1.0 / accel)
        # Per-axis contribution: scale x/y by their share of the magnitude.
        if stick.magnitude > 0:
            ux, uy = stick.x / stick.magnitude, stick.y / stick.magnitude
        else:
            ux, uy = 0.0, 0.0
        pixels = speed * scaled * dt
        return ux * pixels, -uy * pixels  # invert y for screen-down

    @staticmethod
    def _stick_to_scroll(stick: StickState, speed: int, dt: float) -> int:
        """Vertical deflection -> integer wheel clicks for this frame."""
        # Only vertical matters for scroll; use a soft threshold so a centered
        # stick doesn't trickle-scroll.
        v = stick.y
        if abs(v) < 0.1:
            return 0
        return int(v * speed * dt)
