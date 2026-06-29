"""JoyType GUI host (QWebEngine + QWebChannel proof-of-concept).

Loads webui/index.html in a QWebEngineView and bridges the existing controller
daemon to the web UI via QWebChannel. This proves the architecture end-to-end
(web rendering + fonts + JS<->Python bridge + live device status) before the
full design is built.

Run:  python joytype_gui.py
"""
from __future__ import annotations

import json
import sys

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

from joytype import __version__
from joytype.daemon import ControllerDaemon, DaemonStatus
from joytype import actions as jactions
from joytype import config_writer
from joytype import paths as jpaths
from joytype.tray_lifecycle import CloseToTrayController
from joytype.state import Button

# Joy-Con L buttons shown/editable in the UI (the stick is handled separately).
JOYCON_L_BUTTONS = [
    "ZL", "L", "UP", "DOWN", "LEFT", "RIGHT",
    "MINUS", "CAPTURE", "L3", "LEFT_SL", "LEFT_SR",
]


def _action_label(a) -> str:
    """Short human label for a bound action (for the UI)."""
    if a is None:
        return "—"
    if hasattr(a, "press") and hasattr(a, "double") and hasattr(a, "hold"):
        return _action_label(a.press)
    if isinstance(a, jactions.VoiceAction):
        return "PUSH-TO-TALK" if a.mode == "hold" else "DICTATION"
    if isinstance(a, jactions.ChordAction):
        return "+".join(a.chord.tokens).upper()
    if isinstance(a, jactions.KeyAction):
        return a.key.upper()
    return type(a).__name__


# Resolve assets for both source and packaged runs. Bundled web assets
# live under _MEIPASS; the writable runtime config is resolved by joytype.paths.
BASE = jpaths.bundle_dir()
CONFIG = jpaths.ensure_runtime_config_path()
WEBUI = BASE / "webui" / "index.html"
ICON = BASE / "assets" / "joytype.ico"


class Bridge(QObject):
    """JS-facing object. Lives on the GUI thread; daemon callbacks fire on the
    worker thread and emit Qt signals, which Qt queues onto the GUI thread for
    QWebChannel to forward to JavaScript."""

    statusChanged = Signal(str)  # JSON string

    def __init__(self, daemon: ControllerDaemon) -> None:
        super().__init__()
        self._d = daemon
        self._window = None  # set by main() so JS can drive the frameless window
        daemon.on_status.append(self._on_status)

    @staticmethod
    def _status_dict(status: DaemonStatus) -> dict:
        return {
            "connected": status.connected,
            "device": status.device_name,
            "battery": status.battery,
            "charging": status.charging,
            "profile": status.manual_profile or status.profile,
            "running": status.running,
            "error": status.error,
            "controls": list(status.controls),
            "layout_id": status.layout_id,
        }

    def _current_controls(self) -> tuple[str, ...]:
        controls = tuple(getattr(self._d.status, "controls", ()) or ())
        return controls or tuple(JOYCON_L_BUTTONS)

    def _current_stick_mode(self) -> str:
        layout_id = getattr(self._d.status, "layout_id", "")
        if layout_id == "joycon-r":
            return self._d.binder.cfg.mouse.right_stick
        return self._d.binder.cfg.mouse.left_stick

    def _on_status(self, status: DaemonStatus) -> None:
        self.statusChanged.emit(json.dumps(self._status_dict(status)))

    @Slot(result=str)
    def getStatus(self) -> str:
        """Current status, so JS can PULL it on connect (avoids the race where
        the daemon's first push fires before the web channel is ready)."""
        return json.dumps(self._status_dict(self._d.status))

    @Slot(result=str)
    def getAppVersion(self) -> str:
        """Expose the package version to the web UI so release text has one source."""
        return __version__

    @Slot(result=str)
    def getProfiles(self) -> str:
        return json.dumps(list(self._d.binder.cfg.profiles.keys()))

    @Slot(str, result=str)
    def getBindings(self, profile: str) -> str:
        """Effective bindings for the current device, inheriting from its base."""
        b = self._d.binder
        default = b._compiled.get(b._default_name)        # noqa: SLF001
        comp = b._compiled.get(profile, default)          # noqa: SLF001
        prof_cfg = b.cfg.profiles.get(profile)
        base_name = prof_cfg.base if prof_cfg is not None else None
        base = b._compiled.get(base_name) if base_name else default  # noqa: SLF001
        out = {}
        controls = self._current_controls()
        for name in controls:
            try:
                btn = Button[name]
            except KeyError:
                continue
            act = comp.bindings.get(btn)
            if act is None and base is not None and comp is not base:
                act = base.bindings.get(btn)
            out[name] = _action_label(act)
        out["STICK"] = "MOUSE" if self._current_stick_mode() == "move" else "—"
        if Button.L3.value in controls and b.cfg.mouse.click_button == Button.L3:
            out["L3"] = "MOUSE L"
        return json.dumps(out)

    @Slot(str)
    def setProfile(self, name: str) -> None:
        self._d.set_profile(name)

    # --- profile / binding editing (writes the runtime config, reloads the daemon) ---
    @Slot(str, result=str)
    def getProfileDetail(self, profile: str) -> str:
        """Raw bindings + match rule for a profile, so the editor can pre-fill.
        Returns the profile's OWN bindings (overrides); inheritance from default
        is resolved on the JS side by also fetching the default profile."""
        prof = self._d.binder.cfg.profiles.get(profile)
        if prof is None:
            return json.dumps({"error": f"unknown profile {profile}"})
        match = None
        if prof.match is not None:
            m = prof.match
            match = {
                "process": list(m.process),
                "process_icase": m.process_icase,
                "title_regex": m.title_regex.pattern if m.title_regex else "",
            }
        bindings = {btn.name: raw for btn, raw in prof.bindings.items()}
        display_name = prof.display_name or prof.name
        is_base = prof.base is None
        return json.dumps({
            "name": profile,
            "displayName": display_name,
            "base": prof.base,
            "isBase": is_base,
            "isOverride": not is_base,
            "isDefault": profile == self._d.binder._default_name,  # noqa: SLF001
            "match": match,
            "bindings": bindings,
        })

    def _write(self, fn, *args) -> str:
        """Run a config_writer mutation, hot-reload the daemon, report result."""
        try:
            fn(self._d.config_path, *args)
        except Exception as exc:  # validation / IO error: config left intact
            return json.dumps({"ok": False, "error": str(exc)})
        applied = self._d.reload_config(max_block_s=0.2)
        return json.dumps({"ok": True, "reload": "applied" if applied else "deferred"})

    @Slot(str, str, str, result=str)
    def setBinding(self, profile: str, button: str, action_json: str) -> str:
        try:
            action = json.loads(action_json)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"bad action: {exc}"})
        return self._write(config_writer.set_binding, profile, button, action)

    @Slot(str, str, result=str)
    def clearBinding(self, profile: str, button: str) -> str:
        return self._write(config_writer.clear_binding, profile, button)

    @Slot(str, str, result=str)
    def setMatch(self, profile: str, match_json: str) -> str:
        try:
            match = json.loads(match_json)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"bad match: {exc}"})
        return self._write(config_writer.set_match, profile, match)

    @Slot(str, str, result=str)
    def createProfile(self, name: str, match_json: str) -> str:
        try:
            match = json.loads(match_json)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"bad match: {exc}"})
        return self._write(config_writer.create_profile, name, match)

    @Slot(str, result=str)
    def createBaseProfile(self, name: str) -> str:
        return self._write(config_writer.create_base_profile, name)

    @Slot(str, str, str, result=str)
    def createOverride(self, base: str, name: str, match_json: str) -> str:
        try:
            match = json.loads(match_json)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"bad match: {exc}"})
        return self._write(config_writer.create_override, base, name, match)

    @Slot(str, str, result=str)
    def setProfileDisplayName(self, profile: str, display_name: str) -> str:
        return self._write(config_writer.set_profile_display_name, profile, display_name)

    @Slot(str, result=str)
    def deleteProfile(self, name: str) -> str:
        return self._write(config_writer.delete_profile, name)

    @Slot(result=str)
    def getVoiceConfig(self) -> str:
        """The two dictation-IME hotkeys, as editable key-name tokens."""
        v = self._d.binder.cfg.voice
        return json.dumps({
            "hold": list(v.hold_hotkey.tokens),
            "toggle": list(v.toggle_hotkey.tokens),
        })

    @Slot(str, str, result=str)
    def setVoiceHotkey(self, mode: str, keys_json: str) -> str:
        try:
            keys = json.loads(keys_json)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"bad keys: {exc}"})
        return self._write(config_writer.set_voice_hotkey, mode, keys)

    @Slot(result=str)
    def getHapticsConfig(self) -> str:
        h = self._d.binder.cfg.haptics
        return json.dumps({
            "click": [button.value for button in h.click],
            "strength": h.strength,
        })

    @Slot(str, result=str)
    def setHapticsConfig(self, config_json: str) -> str:
        try:
            haptics = json.loads(config_json)
            if not isinstance(haptics, dict):
                raise ValueError("must be an object")
            click = haptics.get("click", [])
            strength = haptics.get("strength", "medium")
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"bad haptics: {exc}"})
        return self._write(config_writer.set_haptics, click, strength)

    @Slot(result=str)
    def getMouseConfig(self) -> str:
        m = self._d.binder.cfg.mouse
        return json.dumps({
            "left_stick": m.left_stick,
            "right_stick": m.right_stick,
            "speed": m.speed,
            "scroll_speed": m.scroll_speed,
            "acceleration": m.acceleration,
        })

    @Slot(result=str)
    def getStickState(self) -> str:
        """Latest shaped stick deflection for the GUI live pointer preview."""
        return json.dumps(self._d.binder.stick_snapshot())

    @Slot(float, result=str)
    def setMouseAcceleration(self, value: float) -> str:
        return self._write(config_writer.set_mouse_acceleration, value)

    @Slot(result=str)
    def pickExe(self) -> str:
        """Open a file dialog so the user picks an .exe; return its process
        stem (e.g. C:\\...\\Code.exe -> 'code'), which is what match uses."""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self._window, "Pick the application's .exe", "",
            "Programs (*.exe);;All files (*.*)")
        if not path:
            return ""
        import os
        return os.path.splitext(os.path.basename(path))[0].lower()

    # --- frameless window controls (the web header replaces the OS title bar) ---
    @Slot()
    def minimizeWindow(self) -> None:
        if self._window is not None:
            self._window.showMinimized()

    @Slot()
    def closeWindow(self) -> None:
        if self._window is not None:
            self._window.close()

    @Slot()
    def dragWindow(self) -> None:
        """Begin a native window move (called on header mousedown)."""
        if self._window is not None:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    @Slot()
    def start(self) -> None:
        self._d.start()

    @Slot()
    def stop(self) -> None:
        self._d.stop()


class InputPlatformWindow(QWebEngineView):
    """WebEngine host window that hides to tray on ordinary close."""

    def __init__(self, close_lifecycle: CloseToTrayController) -> None:
        super().__init__()
        self._close_lifecycle = close_lifecycle

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_lifecycle.handle_close(event, self.hide)


class TrayPresenter(QObject):
    """Keeps tray tooltip/menu state in sync on the GUI thread."""

    def __init__(self, tray: QSystemTrayIcon, start_action, stop_action) -> None:
        super().__init__()
        self._tray = tray
        self._start_action = start_action
        self._stop_action = stop_action

    @Slot(str)
    def apply_status_json(self, status_json: str) -> None:
        try:
            status = json.loads(status_json)
        except Exception:
            return
        profile = status.get("profile") or "(auto)"
        if status.get("error"):
            state = f"error: {status['error']}"
        elif not status.get("running"):
            state = "stopped"
        elif status.get("connected"):
            state = "connected"
        else:
            state = "searching"
        self._tray.setToolTip(f"JoyType - {state} - profile: {profile}")
        self._start_action.setEnabled(not bool(status.get("running")))
        self._stop_action.setEnabled(bool(status.get("running")))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("JoyType")
    app.setQuitOnLastWindowClosed(False)
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))

    daemon = ControllerDaemon(CONFIG)
    bridge = Bridge(daemon)

    icon = QIcon(str(ICON)) if ICON.exists() else QIcon()
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("JoyType")
    menu = QMenu()
    act_show = menu.addAction("Show window")
    act_hide = menu.addAction("Hide to tray")
    menu.addSeparator()
    act_start = menu.addAction("Start daemon")
    act_stop = menu.addAction("Stop daemon")
    act_reload = menu.addAction("Reload config")
    menu.addSeparator()
    act_quit = menu.addAction("Quit JoyType")
    tray.setContextMenu(menu)
    tray.setVisible(True)

    close_lifecycle = CloseToTrayController(
        "JoyType",
        on_real_close=daemon.stop,
    )

    view = InputPlatformWindow(close_lifecycle)
    view.setWindowTitle("JoyType")
    if ICON.exists():
        view.setWindowIcon(icon)
    view.setWindowFlag(Qt.FramelessWindowHint, True)  # web header is the title bar
    view.setStyleSheet("background: #e7eeee;")
    view.page().setBackgroundColor(QColor("#e7eeee"))
    # Size to fit the current screen (the 1920x1080 canvas scales to fill the
    # window). Cap at the design size on big monitors; shrink to ~90% of the
    # available area on small ones so it never overflows. Keep the 16:9 aspect.
    avail = app.primaryScreen().availableGeometry()
    design_aspect = 16 / 9
    w = min(1500, int(avail.width() * 0.9))
    h = int(w / design_aspect)
    if h > avail.height() * 0.9:
        h = int(avail.height() * 0.9)
        w = int(h * design_aspect)
    view.setFixedSize(w, h)
    view.move(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2)
    bridge._window = view
    channel = QWebChannel()
    channel.registerObject("bridge", bridge)
    view.page().setWebChannel(channel)
    view.setUrl(QUrl.fromLocalFile(str(WEBUI)))
    view.show()

    def show_and_raise() -> None:
        view.showNormal() if view.isMinimized() else view.show()
        view.raise_()
        view.activateWindow()

    def quit_from_tray() -> None:
        close_lifecycle.request_real_close()
        tray.hide()
        view.close()
        app.quit()

    def on_tray_activated(reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            show_and_raise()

    act_show.triggered.connect(show_and_raise)
    act_hide.triggered.connect(view.hide)
    act_start.triggered.connect(daemon.start)
    act_stop.triggered.connect(daemon.stop)
    act_reload.triggered.connect(daemon.reload_config)
    act_quit.triggered.connect(quit_from_tray)
    tray.activated.connect(on_tray_activated)

    tray_presenter = TrayPresenter(tray, act_start, act_stop)
    bridge.statusChanged.connect(tray_presenter.apply_status_json)
    tray_presenter.apply_status_json(bridge.getStatus())

    daemon.start()
    try:
        return app.exec()
    finally:
        daemon.stop()


if __name__ == "__main__":
    sys.exit(main())
