"""Controller daemon - the runtime engine, decoupled from any UI.

The JoyType WebEngine host drives this :class:`ControllerDaemon` from the GUI
thread. The daemon owns:

- a worker thread that opens the controller, streams parsed states, and feeds
  them to the :class:`~joytype.binder.Binder`
- automatic reconnection (controllers sleep, Bluetooth drops)
- config hot-reload (file-watch every 2s)
- a callback surface (``on_event``, ``on_profile_change``, ``on_state_change``)
  that the WebEngine host subscribes to for live updates

Threading model
---------------
The worker runs in its own thread. Callbacks fire on that worker thread, so
UI hosts MUST marshal them onto their own thread (Qt: ``QMetaObject.invokeMethod``
or a ``Signal``; JoyType's host layer handles this). The daemon never imports
any UI framework on purpose.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import __version__
from .binder import Binder
from .config import ConfigError, config_changed_on_disk, load_config
from .desktop import DesktopAdapter, WindowsDesktopAdapter
from .device import HAPTIC_CLICK, DeviceAdapter, InputReadSettings, JoyConHidAdapter
from .state import ButtonEvent, ControllerState, diff_states

log = logging.getLogger("joytype")

# Reconnect backoff. Retry FAST right after a live connection drops, so an
# idle-sleeping Joy-Con that re-appears on a button press is re-grabbed almost
# immediately; back off to SLOW if it stays absent (saves CPU + avoids log spam).
RECONNECT_DELAY_FAST = 0.4
RECONNECT_DELAY_SLOW = 2.0
FAST_RECONNECT_WINDOW = 12.0   # seconds of fast retries after a drop
# How often to check whether config.yaml changed on disk.
CONFIG_CHECK_INTERVAL = 2.0
# Minimum spacing between click-feedback writes for the same physical control.
HAPTIC_MIN_INTERVAL_S = 0.08


# ---------------------------------------------------------------------------
# Callback protocols - UIs implement these, daemon calls them
# ---------------------------------------------------------------------------

@dataclass
class DaemonStatus:
    """Snapshot of daemon state, passed to ``on_status`` callbacks.

    Designed as a plain value object so it can be queued across threads
    safely (immutable after construction).
    """

    connected: bool
    device_name: str = ""
    profile: str = ""              # currently active profile name
    manual_profile: Optional[str] = None  # forced profile, or None=auto
    running: bool = True           # worker thread alive
    error: str = ""                # last error message, "" = none
    battery: int = -1              # coarse 0..4 (4=full), -1=unknown
    charging: bool = False
    controls: tuple[str, ...] = ()  # control tokens exposed by current device
    layout_id: str = ""            # UI/layout hint for current device


# Callback signatures. All fire on the worker thread.
StatusCallback = Callable[[DaemonStatus], None]
EventCallback = Callable[[ButtonEvent], None]
LogCallback = Callable[[str, str], None]  # (level, message)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class ControllerDaemon:
    """Runs the controller loop in a background thread.

    Lifecycle::

        d = ControllerDaemon("config.yaml")
        d.on_status.append(my_status_handler)
        d.on_button_event.append(my_event_handler)
        d.start()                  # spawns the worker thread
        ...
        d.stop()                   # graceful shutdown, joins the worker
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        device_adapter: DeviceAdapter | None = None,
        desktop: DesktopAdapter | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")

        self.device_adapter: DeviceAdapter = device_adapter or JoyConHidAdapter()
        self.desktop: DesktopAdapter = desktop or WindowsDesktopAdapter()

        self._lifecycle_lock = threading.RLock()
        self._binder_lock = threading.RLock()
        self._reload_deferred_lock = threading.Lock()
        self._reload_deferred_thread: Optional[threading.Thread] = None
        self._worker_generation = 0

        # Binder is built up-front so config errors surface immediately.
        self.binder: Binder = self._build_binder()
        self._last_status: DaemonStatus = DaemonStatus(
            connected=False, running=False
        )
        # Latest battery reading from the stream (coarse 0..4, -1=unknown).
        self._battery_level: int = -1
        self._charging: bool = False
        # Dedupe the "no controller found" log across a search episode.
        self._search_logged: bool = False
        self._last_haptic_at: dict[tuple[str, str], float] = {}

        # Callback lists - UIs append to these. Fires on worker thread.
        self.on_status: list[StatusCallback] = []
        self.on_button_event: list[EventCallback] = []
        self.on_log: list[LogCallback] = []

        # Thread control.
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        # Dedicated fixed-rate mouse mover (smooth cursor independent of HID
        # report jitter). Runs for the daemon's lifetime.
        self._mouse_worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _build_binder(self) -> Binder:
        cfg = load_config(self.config_path)
        return Binder(cfg, desktop=self.desktop)

    def _emit_status(self, **overrides) -> None:
        """Rebuild the status snapshot and fan out to subscribers."""
        connected = overrides.get("connected", self._last_status.connected)
        controls = overrides.get("controls", self._last_status.controls)
        layout_id = overrides.get("layout_id", self._last_status.layout_id)
        if not connected:
            controls = ()
            layout_id = ""
        with self._binder_lock:
            profile = self.binder.active_profile_name
            manual_profile = self.binder._manual_profile  # noqa: SLF001
        next_status = DaemonStatus(
            connected=connected,
            device_name=overrides.get("device_name", self._last_status.device_name),
            profile=overrides.get("profile", profile),
            manual_profile=overrides.get("manual_profile", manual_profile),
            running=overrides.get("running", self._last_status.running),
            error=overrides.get("error", self._last_status.error),
            battery=overrides.get("battery", self._battery_level),
            charging=overrides.get("charging", self._charging),
            controls=tuple(controls),
            layout_id=layout_id,
        )
        if next_status == self._last_status:
            return
        self._last_status = next_status
        for cb in list(self.on_status):
            try:
                cb(self._last_status)
            except Exception:
                log.exception("status callback raised")

    def _emit_log(self, level: str, message: str) -> None:
        for cb in list(self.on_log):
            try:
                cb(level, message)
            except Exception:
                log.exception("log callback raised")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker thread. Safe to call once."""
        with self._lifecycle_lock:
            if self._worker is not None:
                if self._worker.is_alive():
                    return
                self._worker = None
            if self._mouse_worker is not None:
                if self._mouse_worker.is_alive():
                    return
                self._mouse_worker = None

            self._worker_generation += 1
            generation = self._worker_generation
            self._stop.clear()
            # Self-heal: clear any modifier left stuck by a previous crash /
            # force-kill (e.g. Alt held by the window-cycler when the process
            # was killed mid-cycle), so a fresh launch never inherits a jammed
            # keyboard.
            try:
                self.desktop.release_all_modifiers()
            except Exception:
                pass
            self._worker = threading.Thread(
                target=self._run, args=(generation,),
                name="input-platform-worker",
                daemon=True,
            )
            self._worker.start()
            self._mouse_worker = threading.Thread(
                target=self._mouse_loop, args=(generation,),
                name="input-platform-mouse", daemon=True,
            )
            self._mouse_worker.start()
        self._emit_status(running=True, error="")

    def stop(self) -> None:
        """Signal the worker to stop and join it."""
        with self._lifecycle_lock:
            self._stop.set()
            worker = self._worker
            mouse_worker = self._mouse_worker
        if worker is not None:
            worker.join(timeout=5.0)
        if mouse_worker is not None:
            mouse_worker.join(timeout=1.0)
        with self._lifecycle_lock:
            worker_alive = worker is not None and worker.is_alive()
            mouse_alive = mouse_worker is not None and mouse_worker.is_alive()
            if self._worker is worker and not worker_alive:
                self._worker = None
            if self._mouse_worker is mouse_worker and not mouse_alive:
                self._mouse_worker = None
            running = worker_alive or mouse_alive
        # Belt-and-suspenders: lift any voice chord left held at shutdown.
        self._release_voice_safety()
        self._emit_status(running=running, connected=False)

    @property
    def status(self) -> DaemonStatus:
        return self._last_status

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _is_worker_current(self, generation: int) -> bool:
        with self._lifecycle_lock:
            return generation == self._worker_generation

    def _stream_is_current(self, generation: Optional[int]) -> bool:
        return generation is None or self._is_worker_current(generation)

    def _run(self, generation: int) -> None:
        """Top-level worker loop: connect, stream, reconnect, watch config."""
        self._emit_log("INFO", f"JoyType {__version__} worker started.")
        last_cfg_check = time.perf_counter()
        search_start = time.perf_counter()  # when the current search episode began

        while not self._stop.is_set() and self._is_worker_current(generation):
            # --- Periodic config hot-reload ---------------------------------
            now = time.perf_counter()
            if now - last_cfg_check > CONFIG_CHECK_INTERVAL:
                last_cfg_check = now
                with self._binder_lock:
                    cfg = self.binder.cfg
                if config_changed_on_disk(cfg):
                    self._emit_log("INFO", "Config file changed; reloading...")
                    try:
                        self._reload_config()
                    except ConfigError as exc:
                        self._emit_log("ERROR", f"Reload failed: {exc}")

            # --- Connect + stream -------------------------------------------
            connected = self._connect_and_stream(generation)

            if self._stop.is_set() or not self._is_worker_current(generation):
                break

            self._emit_status(connected=False, error="disconnected")
            # A fresh drop from a live connection starts the fast-retry window
            # so an idle-waking Joy-Con is re-grabbed quickly.
            if connected:
                search_start = time.perf_counter()
            fast = (time.perf_counter() - search_start) < FAST_RECONNECT_WINDOW
            delay = RECONNECT_DELAY_FAST if fast else RECONNECT_DELAY_SLOW
            # Sleep in small slices so stop() stays responsive.
            deadline = time.perf_counter() + delay
            while (time.perf_counter() < deadline
                   and not self._stop.is_set()
                   and self._is_worker_current(generation)):
                time.sleep(0.05)

        self._emit_log("INFO", "Worker stopped.")
        self._emit_status(running=False, connected=False)

    def _connect_and_stream(self, generation: Optional[int] = None) -> bool:
        """One connection attempt: open + stream until disconnect.

        Returns True if a controller was opened and streamed, False if none was
        found this attempt.
        """
        try:
            session = self.device_adapter.open()
        except RuntimeError as exc:
            # Not paired / asleep - log once per search episode, then go quiet.
            if not self._search_logged:
                self._emit_log("INFO", f"{exc} (will keep retrying)")
                self._search_logged = True
            return False
        except Exception as exc:
            self._emit_log("ERROR", f"open device failed: {exc}")
            return False
        self._search_logged = False

        name = session.info.display_name
        self._emit_status(
            connected=True,
            device_name=name,
            error="",
            controls=session.info.controls,
            layout_id=session.info.layout_id,
        )
        self._emit_log("INFO", f"Connected: {name}")
        self._emit_log(
            "INFO",
            "Device metadata: "
            f"layout={session.info.layout_id} "
            f"controls={','.join(session.info.controls) or '-'} "
            f"capabilities={','.join(session.info.capabilities) or '-'}",
        )

        # Snapshot the active profile once for the UI; profile changes during
        # streaming are reported via _on_profile_switched callback.
        self._emit_status()

        prev_t = time.perf_counter()
        prev_state: Optional[ControllerState] = None
        with self._binder_lock:
            cfg = self.binder.cfg
            keep_alive_s = cfg.keep_alive_s
            settings = InputReadSettings(
                deadzone=cfg.deadzone,
                curve=cfg.stick_curve,
            )
        last_keepalive = time.perf_counter()
        try:
            for state in session.read_states(settings):
                if self._stop.is_set() or not self._stream_is_current(generation):
                    break
                now = time.perf_counter()
                dt = max(now - prev_t, 1e-3)
                prev_t = now

                # Fire button-edge events to subscribers BEFORE the binder
                # acts on them, so the UI shows what the controller did
                # regardless of any action failures. Haptics uses the same
                # edge list, so compute it once per frame when either path
                # needs button transitions.
                with self._binder_lock:
                    needs_haptic_edges = bool(self.binder.cfg.haptics.click)
                events: list[ButtonEvent] = []
                if prev_state is not None and (
                    self.on_button_event or needs_haptic_edges
                ):
                    events = list(diff_states(prev_state, state))
                    for ev in events:
                        for cb in list(self.on_button_event):
                            try:
                                cb(ev)
                            except Exception:
                                log.exception("button-event callback raised")

                with self._binder_lock:
                    if (self._stop.is_set()
                            or not self._stream_is_current(generation)):
                        break
                    self.binder.update(state, dt=dt)

                for ev in events:
                    self._play_press_haptic(session, ev)

                # Battery changes rarely; push a status update only on change.
                if (state.battery != self._battery_level
                        or state.charging != self._charging):
                    self._battery_level = state.battery
                    self._charging = state.charging
                    self._emit_status()

                # Optional keep-alive: poke the controller so it doesn't idle-
                # sleep (which makes the next press laggy). Disabled when 0.
                if keep_alive_s > 0 and (
                    time.perf_counter() - last_keepalive
                ) >= keep_alive_s:
                    last_keepalive = time.perf_counter()
                    try:
                        session.keep_alive()
                    except Exception:
                        pass

                prev_state = state
        except ConnectionError as exc:
            self._emit_log("WARNING", f"Controller disconnected: {exc}")
        except Exception as exc:  # noqa: BLE001 - worker must survive any error
            self._emit_log("ERROR", f"Stream error: {exc}")
        finally:
            # Safety: if a push-to-talk press was still down when the controller
            # dropped, lift the held chord so modifier keys never stick down.
            self._release_voice_safety()
            # Stop the cursor: zero the cached deflection so the mouse thread
            # doesn't keep moving from the last-seen stick value.
            try:
                with self._binder_lock:
                    self.binder.reset_sticks()
            except Exception:
                pass
            try:
                session.close()
            except Exception:
                pass
            # Battery is unknown once disconnected.
            self._battery_level = -1
            self._charging = False
            self._emit_status(connected=False)
        return True

    def _play_press_haptic(self, session, ev: ButtonEvent) -> None:
        if not ev.pressed:
            return
        with self._binder_lock:
            click_buttons = self.binder.cfg.haptics.click
            strength = self.binder.cfg.haptics.strength
        if ev.button not in click_buttons:
            return
        if HAPTIC_CLICK not in session.info.capabilities:
            return
        if ev.button.value not in session.info.controls:
            return
        now = time.perf_counter()
        haptic_key = (session.info.id, ev.button.value)
        last = self._last_haptic_at.get(haptic_key)
        if last is not None and now - last < HAPTIC_MIN_INTERVAL_S:
            return
        self._last_haptic_at[haptic_key] = now
        try:
            session.play_feedback(
                HAPTIC_CLICK,
                ev.button.value,
                strength=strength,
            )
        except Exception as exc:
            self._emit_log(
                "WARNING",
                f"Feedback failed for {ev.button.value}: {exc}",
            )
            log.debug("feedback failed for %s", ev.button.value, exc_info=True)

    def _release_voice_safety(self) -> None:
        """Force-release any held keys (voice chord, window-cycle Alt, ...)."""
        try:
            with self._binder_lock:
                self.binder.panic_release_all()
        except Exception:
            log.exception("panic_release_all failed")
        # Belt-and-suspenders: lift every modifier in case a key-up was missed.
        try:
            self.desktop.release_all_modifiers()
        except Exception:
            pass

    def _mouse_loop(self, generation: Optional[int] = None) -> None:
        """Fixed-rate cursor mover, decoupled from HID report timing.

        Moves the cursor every MOUSE_TICK seconds from the latest stick
        deflection, so motion stays smooth even when Bluetooth reports arrive
        irregularly. Runs the daemon's whole lifetime; it emits nothing while
        the deflection is centered (disconnected or stick at rest).
        """
        MOUSE_TICK = 1.0 / 144.0   # ~144 Hz cursor updates
        MAX_DT = 0.05              # clamp so a scheduling hiccup can't jump far
        self.desktop.begin_high_res_timer(1)  # ~1ms sleeps instead of Windows' ~15ms
        try:
            last = time.perf_counter()
            while not self._stop.is_set() and self._stream_is_current(generation):
                now = time.perf_counter()
                dt = now - last
                last = now
                if dt > MAX_DT:
                    dt = MAX_DT
                try:
                    with self._binder_lock:
                        if (self._stop.is_set()
                                or not self._stream_is_current(generation)):
                            break
                        self.binder.tick_mouse(dt)
                except Exception:
                    log.exception("mouse tick failed")
                remaining = MOUSE_TICK - (time.perf_counter() - now)
                if remaining > 0:
                    self._stop.wait(remaining)
        finally:
            self.desktop.end_high_res_timer(1)

    # ------------------------------------------------------------------
    # UI-driven actions
    # ------------------------------------------------------------------

    def set_profile(self, name: str) -> None:
        """Force a profile (``__auto__`` to reset to auto-detect)."""
        with self._binder_lock:
            self.binder.set_profile(name)
        self._emit_status()

    def reload_config(self, *, max_block_s: float | None = None) -> bool:
        """Public reload entry point (e.g. GUI 'Reload' button)."""
        try:
            self._reload_config(lock_timeout=max_block_s)
            self._emit_log("INFO", "Config reloaded.")
            self._emit_status()
            return True
        except TimeoutError:
            self._emit_log("WARNING", "Config reload deferred; input loop is busy.")
            self._schedule_deferred_reload()
            return False
        except ConfigError as exc:
            self._emit_log("ERROR", f"Config error: {exc}")
            return False

    def _schedule_deferred_reload(self) -> None:
        with self._reload_deferred_lock:
            if (
                self._reload_deferred_thread is not None
                and self._reload_deferred_thread.is_alive()
            ):
                return
            thread = threading.Thread(
                target=self._run_deferred_reload,
                name="input-platform-reload",
                daemon=True,
            )
            self._reload_deferred_thread = thread
            thread.start()

    def _run_deferred_reload(self) -> None:
        try:
            self._reload_config()
            self._emit_log("INFO", "Config reloaded.")
            self._emit_status()
        except ConfigError as exc:
            self._emit_log("ERROR", f"Config error: {exc}")
        except Exception as exc:  # noqa: BLE001 - background reload must not die loudly
            self._emit_log("ERROR", f"Config reload failed: {exc}")
            log.exception("deferred config reload failed")
        finally:
            with self._reload_deferred_lock:
                current = threading.current_thread()
                if self._reload_deferred_thread is current:
                    self._reload_deferred_thread = None

    def _reload_config(self, *, lock_timeout: float | None = None) -> None:
        """Rebuild the binder from disk after safely releasing held actions."""
        fresh = self._build_binder()
        if lock_timeout is None:
            self._binder_lock.acquire()
        else:
            if not self._binder_lock.acquire(timeout=max(lock_timeout, 0.0)):
                raise TimeoutError("binder is busy")
        try:
            manual_profile = self.binder._manual_profile  # noqa: SLF001
            active_profile = self.binder._active_name  # noqa: SLF001

            # Release old stateful actions only after the new config compiles,
            # and while HID/mouse dispatch is paused. If reload is invalid, the
            # running binder and held state stay intact.
            self.binder.panic_release_all()

            if manual_profile in fresh._compiled:  # noqa: SLF001
                fresh._manual_profile = manual_profile  # noqa: SLF001
            if active_profile in fresh._compiled:  # noqa: SLF001
                fresh._active_name = active_profile  # noqa: SLF001
            self.binder = fresh
        finally:
            self._binder_lock.release()
