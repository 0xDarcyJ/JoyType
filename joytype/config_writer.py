"""Comment-preserving writes to ``config.yaml`` for the GUI editor.

The daemon *reads* config with PyYAML (see :mod:`joytype.config`); this module
*writes* it with ruamel.yaml round-trip so the user's hand-written comments and
formatting survive GUI edits. Every mutation is validated by attempting a full
``parse_config`` + ``Binder`` compile on the candidate document BEFORE it
touches disk, so a bad edit can never brick the daemon - the write simply fails
and the caller surfaces the error.

Action dicts are emitted in flow style (``{key: enter}``) to match the compact
style already used throughout config.yaml.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .binder import Binder
from .config import PROFILE_LAYER_SEPARATOR, parse_config

_yaml = YAML()                 # round-trip mode (preserves comments)
_yaml.preserve_quotes = True
_yaml.width = 4096             # never line-wrap our flow maps / lists


def _load(path: Path) -> CommentedMap:
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)
    if data is None:
        raise ValueError("config.yaml is empty")
    if "profiles" not in data or not isinstance(data["profiles"], dict):
        raise ValueError("config.yaml has no 'profiles' mapping")
    return data


def _flow(value: Any) -> Any:
    """Deep-copy a plain dict/list into ruamel containers rendered in flow
    style, so an action writes as ``{hotkey: [ctrl, enter]}`` not a block."""
    if isinstance(value, dict):
        m = CommentedMap()
        for k, v in value.items():
            m[k] = _flow(v)
        m.fa.set_flow_style()
        return m
    if isinstance(value, (list, tuple)):
        s = CommentedSeq(_flow(v) for v in value)
        s.fa.set_flow_style()
        return s
    return value


def _valid_profile_name(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum()


def _split_layer_id(profile: str) -> tuple[str, str | None]:
    if PROFILE_LAYER_SEPARATOR in profile:
        base, override = profile.split(PROFILE_LAYER_SEPARATOR, 1)
        if not base or not override:
            raise ValueError(f"bad profile layer id {profile!r}")
        return base, override
    return profile, None


def _ensure_map(parent: CommentedMap, key: str) -> CommentedMap:
    value = parent.get(key)
    if value is None:
        value = CommentedMap()
        parent[key] = value
    if not isinstance(value, CommentedMap):
        raise ValueError(f"{key} must be a mapping")
    return value


def _ensure_nested_base(profs: CommentedMap, base: str) -> CommentedMap:
    if base not in profs:
        raise KeyError(f"unknown base profile {base!r}")
    body = profs[base]
    if body is None:
        body = CommentedMap()
        profs[base] = body
    if not isinstance(body, CommentedMap):
        raise ValueError(f"profile {base!r} must be a mapping")
    if "bindings" not in body and "overrides" not in body:
        bindings = CommentedMap()
        for key in list(body.keys()):
            if str(key).lower() == "match":
                raise ValueError("base profiles cannot define match")
            bindings[key] = body.pop(key)
        body["bindings"] = bindings
    _ensure_map(body, "bindings")
    _ensure_map(body, "overrides")
    return body


def _binding_map(data: CommentedMap, profile: str) -> CommentedMap:
    profs = data["profiles"]
    base, override = _split_layer_id(profile)
    if override is None:
        if base not in profs:
            raise KeyError(f"unknown profile {profile!r}")
        body = profs[base]
        if body is None:
            body = CommentedMap()
            profs[base] = body
        if not isinstance(body, CommentedMap):
            raise ValueError(f"profile {profile!r} must be a mapping")
        if "bindings" in body or "overrides" in body:
            return _ensure_map(body, "bindings")
        return body

    base_body = _ensure_nested_base(profs, base)
    overrides = _ensure_map(base_body, "overrides")
    if override not in overrides:
        raise KeyError(f"unknown profile {profile!r}")
    override_body = overrides[override]
    if override_body is None:
        override_body = CommentedMap()
        overrides[override] = override_body
    if not isinstance(override_body, CommentedMap):
        raise ValueError(f"profile {profile!r} must be a mapping")
    return _ensure_map(override_body, "bindings")


def _match_map(data: CommentedMap, profile: str) -> CommentedMap:
    profs = data["profiles"]
    base, override = _split_layer_id(profile)
    if override is None:
        if base not in profs:
            raise KeyError(f"unknown profile {profile!r}")
        body = profs[base]
        if not isinstance(body, CommentedMap):
            raise ValueError(f"profile {profile!r} must be a mapping")
        if "bindings" in body or "overrides" in body:
            raise ValueError("base profiles do not have app match rules")
        return body

    base_body = _ensure_nested_base(profs, base)
    overrides = _ensure_map(base_body, "overrides")
    if override not in overrides:
        raise KeyError(f"unknown profile {profile!r}")
    override_body = overrides[override]
    if not isinstance(override_body, CommentedMap):
        raise ValueError(f"profile {profile!r} must be a mapping")
    return override_body


def _dump(data: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _commit(path: Path, data: CommentedMap) -> None:
    """Validate the candidate document, then write it atomically."""
    text = _dump(data)
    # Full validation: the same path the daemon will take on reload. parse_config
    # raises on schema errors; Binder() compiles every action (catches bad keys,
    # unsupported action shapes, voice placeholders without a voice block, etc.).
    import yaml as _pyyaml
    cfg = parse_config(_pyyaml.safe_load(text))
    Binder(cfg)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Public mutations
# ---------------------------------------------------------------------------

def set_binding(path: str | Path, profile: str, button: str, action: dict) -> None:
    """Set (or override) ``button`` in ``profile`` to ``action``."""
    path = Path(path)
    data = _load(path)
    _binding_map(data, profile)[button] = _flow(action)
    _commit(path, data)


def clear_binding(path: str | Path, profile: str, button: str) -> None:
    """Remove ``button`` from ``profile`` (reverts to the inherited default)."""
    path = Path(path)
    data = _load(path)
    bindings = _binding_map(data, profile)
    if button in bindings:
        del bindings[button]
    _commit(path, data)


def set_match(path: str | Path, profile: str, match: dict) -> None:
    """Set the foreground-window match rule for a (non-default) profile."""
    path = Path(path)
    data = _load(path)
    _match_map(data, profile)["match"] = _flow(match)
    _commit(path, data)


def create_profile(path: str | Path, name: str, match: dict) -> None:
    """Create a new per-app override profile with an empty binding table."""
    path = Path(path)
    data = _load(path)
    profs = data["profiles"]
    if name in profs:
        raise ValueError(f"profile {name!r} already exists")
    if not _valid_profile_name(name):
        raise ValueError("profile name must be alphanumeric / underscores")
    m = CommentedMap()
    m["match"] = _flow(match)
    profs[name] = m  # parse_config always re-sorts 'default' last on load
    _commit(path, data)


def create_base_profile(path: str | Path, name: str) -> None:
    """Create a new base profile with an empty binding table."""
    path = Path(path)
    data = _load(path)
    profs = data["profiles"]
    if name in profs:
        raise ValueError(f"profile {name!r} already exists")
    if not _valid_profile_name(name):
        raise ValueError("profile name must be alphanumeric / underscores")
    m = CommentedMap()
    m["bindings"] = CommentedMap()
    m["overrides"] = CommentedMap()
    profs[name] = m
    _commit(path, data)


def create_override(path: str | Path, base: str, name: str, match: dict) -> None:
    """Create a per-app override under a base profile."""
    path = Path(path)
    data = _load(path)
    if not _valid_profile_name(name):
        raise ValueError("override name must be alphanumeric / underscores")
    base_body = _ensure_nested_base(data["profiles"], base)
    overrides = _ensure_map(base_body, "overrides")
    if name in overrides:
        raise ValueError(f"override {name!r} already exists under {base!r}")
    m = CommentedMap()
    m["match"] = _flow(match)
    m["bindings"] = CommentedMap()
    overrides[name] = m
    _commit(path, data)


def set_profile_display_name(path: str | Path, profile: str, display_name: str) -> None:
    """Set the user-visible profile name without changing the stable layer id."""
    path = Path(path)
    label = str(display_name or "").strip()
    if not label:
        raise ValueError("profile display name is required")
    data = _load(path)
    profs = data["profiles"]
    base, override = _split_layer_id(profile)
    if override is None:
        if base not in profs:
            raise KeyError(f"unknown profile {profile!r}")
        body = profs[base]
        if body is None:
            body = CommentedMap()
            profs[base] = body
        if not isinstance(body, CommentedMap):
            raise ValueError(f"profile {profile!r} must be a mapping")
        body["display_name"] = label
    else:
        base_body = _ensure_nested_base(profs, base)
        overrides = _ensure_map(base_body, "overrides")
        if override not in overrides:
            raise KeyError(f"unknown profile {profile!r}")
        body = overrides[override]
        if body is None:
            body = CommentedMap()
            overrides[override] = body
        if not isinstance(body, CommentedMap):
            raise ValueError(f"profile {profile!r} must be a mapping")
        body["display_name"] = label
    _commit(path, data)


def delete_profile(path: str | Path, name: str) -> None:
    """Delete a profile. The global ``default`` profile cannot be removed."""
    path = Path(path)
    data = _load(path)
    base, override = _split_layer_id(name)
    if override is None:
        if base == "default":
            raise ValueError("cannot delete the default profile")
        if base in data["profiles"]:
            del data["profiles"][base]
    else:
        base_body = _ensure_nested_base(data["profiles"], base)
        overrides = _ensure_map(base_body, "overrides")
        if override in overrides:
            del overrides[override]
    _commit(path, data)


def set_voice_hotkey(path: str | Path, mode: str, keys: list) -> None:
    """Set the dictation-IME hotkey JoyType fires for a voice mode.

    ``mode`` = 'hold' (push-to-talk) or 'toggle'. ``keys`` is a list of key-name
    tokens (e.g. ['lshift','lctrl','f8']). These must match the hotkeys set in
    the user's voice IME, so they need to be editable when the IME changes.
    """
    path = Path(path)
    field = "hold_hotkey" if mode == "hold" else "toggle_hotkey"
    data = _load(path)
    voice = data.get("voice")
    if voice is None:
        voice = CommentedMap()
        data["voice"] = voice
    voice[field] = _flow(list(keys))
    _commit(path, data)


def set_haptics(path: str | Path, click_buttons: list, strength: str) -> None:
    """Set click-feedback buttons and global haptic strength."""
    path = Path(path)
    data = _load(path)
    haptics = data.get("haptics")
    if haptics is None:
        haptics = CommentedMap()
        data["haptics"] = haptics
    haptics["click"] = _flow(list(click_buttons))
    haptics["strength"] = strength
    _commit(path, data)


def set_mouse_acceleration(path: str | Path, value: float) -> None:
    """Set the stick-to-mouse acceleration curve value."""
    path = Path(path)
    value = float(value)
    if value <= 0:
        raise ValueError("mouse acceleration must be > 0")
    data = _load(path)
    mouse = data.get("mouse")
    if mouse is None:
        mouse = CommentedMap()
        data["mouse"] = mouse
    mouse["acceleration"] = value
    _commit(path, data)
