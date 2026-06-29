"""Configuration loading and validation.

``config.yaml`` is the contract between the user and the daemon. This module
turns that YAML into typed dataclasses and validates it eagerly so a typo
fails at load time with a clear message rather than silently at runtime when
a button is pressed.

The on-disk schema mirrors what's documented in ``config.yaml``; see that
file for prose explanations of each field.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .desktop import KeyChord
from .state import Button, parse_button

log = logging.getLogger(__name__)

HAPTIC_STRENGTHS = ("light", "medium", "strong")
PROFILE_LAYER_SEPARATOR = "::"


# ---------------------------------------------------------------------------
# Dataclasses - typed views of the YAML
# ---------------------------------------------------------------------------

@dataclass
class MouseConfig:
    left_stick: str = "move"
    right_stick: str = "scroll"
    speed: int = 1200
    scroll_speed: int = 8
    acceleration: float = 1.4
    click_button: Optional[Button] = Button.L3


@dataclass
class VoiceConfig:
    # ``hold_hotkey`` is held while a {voice: true} button is pressed (push-to-
    # talk); ``toggle_hotkey`` is tapped by {voice_toggle: true} buttons. Both
    # must match the hotkeys configured in the dictation tool.
    hold_hotkey: KeyChord = field(default_factory=KeyChord)
    toggle_hotkey: KeyChord = field(default_factory=KeyChord)


@dataclass
class HapticsConfig:
    click: tuple[Button, ...] = ()
    strength: str = "medium"


@dataclass
class MatchRule:
    """How a profile decides it owns the foreground window."""

    process: list[str] = field(default_factory=list)  # stems to compare against
    process_icase: bool = True
    title_regex: Optional[re.Pattern] = None


@dataclass
class ProfileConfig:
    """One key-binding table. ``bindings`` maps Button -> raw action dict."""

    name: str
    match: Optional[MatchRule] = None  # None means "the default profile"
    bindings: dict[Button, dict] = field(default_factory=dict)
    base: Optional[str] = None
    display_name: Optional[str] = None


@dataclass
class Config:
    deadzone: float = 0.15
    stick_curve: str = "exponential"
    poll_hz: int = 0
    # Seconds between keep-alive pokes to the controller (0 = off). Set a small
    # value (e.g. 1.0) to fight idle-sleep wake-up lag, at some battery cost.
    keep_alive_s: float = 0.0

    mouse: MouseConfig = field(default_factory=MouseConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    haptics: HapticsConfig = field(default_factory=HapticsConfig)

    profiles: dict[str, ProfileConfig] = field(default_factory=dict)
    # Insertion order = match priority. "default" is always last (fallback).

    # Path the config was loaded from; used for reloads.
    source: Optional[Path] = None
    # File mtime at load time, for change detection.
    _loaded_mtime: float = 0.0


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """Raised when the YAML is structurally wrong or violates the schema."""


def _require(condition: bool, msg: str) -> None:
    if not condition:
        raise ConfigError(msg)


def _parse_hotkey(spec: Any, where: str) -> KeyChord:
    try:
        return KeyChord.parse(spec)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _parse_match(raw: dict, where: str) -> MatchRule:
    _require(isinstance(raw, dict), f"{where}: match must be a mapping")
    process = raw.get("process", [])
    if isinstance(process, str):
        process = [process]
    _require(isinstance(process, list), f"{where}: match.process must be str or list")
    title_regex = None
    if "title_regex" in raw and raw["title_regex"]:
        try:
            title_regex = re.compile(raw["title_regex"])
        except re.error as exc:
            raise ConfigError(f"{where}: bad title_regex: {exc}") from exc
    _require(
        bool(process) or title_regex is not None,
        f"{where}: match must define process or title_regex",
    )
    return MatchRule(
        process=[p.lower() for p in process],
        process_icase=bool(raw.get("process_icase", True)),
        title_regex=title_regex,
    )


def _parse_bindings(raw: dict, where: str) -> dict[Button, dict]:
    bindings: dict[Button, dict] = {}
    _require(isinstance(raw, dict), f"{where}: bindings must be a mapping")
    for token, action in raw.items():
        # Reserved non-button keys inside a profile section.
        if token.lower() in ("match", "bindings", "overrides", "display_name"):
            continue
        try:
            btn = parse_button(token)
        except KeyError as exc:
            raise ConfigError(f"{where}: {exc}") from exc
        _require(
            isinstance(action, dict),
            f"{where}: action for {token} must be a mapping, got {type(action)}",
        )
        bindings[btn] = action
    return bindings


def _is_nested_profile_body(body: dict) -> bool:
    return "bindings" in body or "overrides" in body


def _parse_profile_bindings(body: dict, where: str) -> dict[Button, dict]:
    if _is_nested_profile_body(body):
        bindings_raw = body.get("bindings", {})
        _require(isinstance(bindings_raw, dict), f"{where}.bindings must be a mapping")
        return _parse_bindings(bindings_raw, f"{where}.bindings")
    return _parse_bindings(body, where)


def _layer_id(base: str, override: str) -> str:
    return f"{base}{PROFILE_LAYER_SEPARATOR}{override}"


def _parse_button_tuple(raw: Any, where: str) -> tuple[Button, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        tokens = [raw]
    else:
        _require(isinstance(raw, list), f"{where}: must be a string or list of strings")
        tokens = raw

    buttons: list[Button] = []
    for token in tokens:
        _require(isinstance(token, str), f"{where}: button names must be strings")
        try:
            buttons.append(parse_button(token))
        except KeyError as exc:
            raise ConfigError(f"{where}: {exc}") from exc
    return tuple(buttons)


# ---------------------------------------------------------------------------
# Top-level loader
# ---------------------------------------------------------------------------

def parse_config(data: dict, source: Optional[Path] = None) -> Config:
    """Validate a raw dict (already YAML-loaded) into a :class:`Config`."""
    _require(isinstance(data, dict), "Top-level config must be a mapping")

    cfg = Config(
        deadzone=float(data.get("deadzone", 0.15)),
        stick_curve=str(data.get("stick_curve", "exponential")).lower(),
        poll_hz=int(data.get("poll_hz", 0)),
        keep_alive_s=float(data.get("keep_alive_s", 0.0)),
        source=source,
    )
    if cfg.stick_curve not in ("linear", "exponential"):
        raise ConfigError(
            f"stick_curve must be 'linear' or 'exponential', got {cfg.stick_curve!r}"
        )

    # Mouse
    if mouse_raw := data.get("mouse"):
        _require(isinstance(mouse_raw, dict), "mouse must be a mapping")
        click_tok = mouse_raw.get("click_button", "L3")
        click_btn = None
        if click_tok and str(click_tok).lower() not in ("none", "off"):
            click_btn = parse_button(str(click_tok))
        cfg.mouse = MouseConfig(
            left_stick=str(mouse_raw.get("left_stick", "move")).lower(),
            right_stick=str(mouse_raw.get("right_stick", "scroll")).lower(),
            speed=int(mouse_raw.get("speed", 1200)),
            scroll_speed=int(mouse_raw.get("scroll_speed", 8)),
            acceleration=float(mouse_raw.get("acceleration", 1.4)),
            click_button=click_btn,
        )

    # Voice
    if voice_raw := data.get("voice"):
        _require(isinstance(voice_raw, dict), "voice must be a mapping")
        cfg.voice = VoiceConfig(
            hold_hotkey=_parse_hotkey(
                voice_raw.get("hold_hotkey"), "voice.hold_hotkey"
            ),
            toggle_hotkey=_parse_hotkey(
                voice_raw.get("toggle_hotkey"), "voice.toggle_hotkey"
            ),
        )

    # Haptics
    if "haptics" in data:
        haptics_raw = data["haptics"]
        _require(isinstance(haptics_raw, dict), "haptics must be a mapping")
        haptic_strength = str(haptics_raw.get("strength", "medium")).lower()
        if haptic_strength not in HAPTIC_STRENGTHS:
            raise ConfigError("haptics.strength: must be light, medium, or strong")
        cfg.haptics = HapticsConfig(
            click=_parse_button_tuple(haptics_raw.get("click"), "haptics.click"),
            strength=haptic_strength,
        )

    # Profiles
    profiles_raw = data.get("profiles", {})
    _require(isinstance(profiles_raw, dict), "profiles must be a mapping")
    default_profile: Optional[ProfileConfig] = None
    base_profiles: list[ProfileConfig] = []
    named: list[ProfileConfig] = []
    for name, body in profiles_raw.items():
        where = f"profiles.{name}"
        _require(isinstance(body, dict), f"{where}: profile body must be a mapping")
        if _is_nested_profile_body(body):
            if "match" in body:
                raise ConfigError(f"{where}: base profiles cannot define match")
            base = ProfileConfig(
                name=name,
                match=None,
                bindings=_parse_profile_bindings(body, where),
                base=None,
                display_name=str(body.get("display_name", name)),
            )
            base_profiles.append(base)
            if name == "default":
                default_profile = base
            overrides_raw = body.get("overrides", {})
            _require(isinstance(overrides_raw, dict), f"{where}.overrides must be a mapping")
            for override_name, override_body in overrides_raw.items():
                override_where = f"{where}.overrides.{override_name}"
                _require(
                    isinstance(override_body, dict),
                    f"{override_where}: override body must be a mapping",
                )
                _require(
                    "match" in override_body,
                    f"{override_where}: override must define match",
                )
                named.append(ProfileConfig(
                    name=_layer_id(name, override_name),
                    match=_parse_match(override_body["match"], override_where),
                    bindings=_parse_profile_bindings(override_body, override_where),
                    base=name,
                    display_name=str(override_body.get("display_name", override_name)),
                ))
            continue

        match = _parse_match(body["match"], where) if "match" in body else None
        bindings = _parse_profile_bindings(body, where)
        prof = ProfileConfig(
            name=name,
            match=match,
            bindings=bindings,
            base=None,
            display_name=str(body.get("display_name", name)),
        )
        if match is None:
            if default_profile is None or name == "default":
                default_profile = prof
            base_profiles.append(prof)
        else:
            named.append(prof)

    if default_profile is None:
        # Synthesize an empty default so the binder always has a fallback.
        if base_profiles:
            default_profile = base_profiles[0]
        else:
            default_profile = ProfileConfig(
                name="default",
                match=None,
                bindings={},
                display_name="default",
            )
            base_profiles.append(default_profile)
            log.warning("No 'default' profile in config; using an empty one.")

    for prof in named:
        if prof.base is None:
            prof.base = default_profile.name

    ordered: dict[str, ProfileConfig] = {}
    for base in base_profiles:
        ordered[base.name] = base
        for prof in named:
            if prof.base == base.name and PROFILE_LAYER_SEPARATOR in prof.name:
                ordered[prof.name] = prof
    for prof in named:
        if prof.name not in ordered:
            ordered[prof.name] = prof
    cfg.profiles = ordered

    if source is not None and source.exists():
        cfg._loaded_mtime = source.stat().st_mtime

    return cfg


def load_config(path: str | os.PathLike) -> Config:
    """Read ``path`` and return a validated :class:`Config`."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        data = {}
    return parse_config(data, source=p)


def config_changed_on_disk(cfg: Config) -> bool:
    """Has the source file been modified since we last loaded it?"""
    if cfg.source is None or not cfg.source.exists():
        return False
    return cfg.source.stat().st_mtime != cfg._loaded_mtime
