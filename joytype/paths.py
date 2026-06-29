"""Path resolution that works both in development and when frozen.

When PyInstaller bundles the app (``--onedir``), the exe lives in
``dist/JoyType/JoyType.exe`` and ``config.yaml`` ships alongside it (NOT inside
the bundle), so users can edit it. This module finds that config regardless
of the current working directory.

Development mode (``python joytype_gui.py``): the checked-in release defaults
live in ``config.default.yaml``; the GUI writes to ignored ``config.local.yaml``
so local edits do not leak into release packages.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

DEFAULT_NAME = "config.yaml"
RELEASE_DEFAULT_NAME = "config.default.yaml"
LOCAL_NAME = "config.local.yaml"


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def exe_dir() -> Path:
    """Directory containing the running exe (frozen) or the repo root (dev).

    Frozen: the folder holding JoyType.exe. config.yaml lives here.
    Dev: the project root (parent of joytype/).
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def project_root() -> Path:
    """Repository root in development mode."""
    return Path(__file__).resolve().parent.parent


def _copy_if_missing(source: Path, target: Path) -> Path:
    if not target.exists() and source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return target


def ensure_runtime_config_path(root: Path | None = None) -> Path:
    """Return the writable config file used by the running app.

    Frozen builds use ``config.yaml`` beside ``JoyType.exe``. Development uses
    ignored ``config.local.yaml``, created from ``config.default.yaml`` on first
    run. This keeps the release template separate from local GUI edits.
    """
    if is_frozen():
        beside_exe = exe_dir() / DEFAULT_NAME
        if beside_exe.exists():
            return beside_exe
        bundled_default = bundle_dir() / RELEASE_DEFAULT_NAME
        if bundled_default.exists():
            return _copy_if_missing(bundled_default, beside_exe)
        bundled_legacy = bundle_dir() / DEFAULT_NAME
        if bundled_legacy.exists():
            return _copy_if_missing(bundled_legacy, beside_exe)
        return beside_exe

    root = Path(root).resolve() if root is not None else project_root()
    local = root / LOCAL_NAME
    if local.exists():
        return local

    release_default = root / RELEASE_DEFAULT_NAME
    if release_default.exists():
        return _copy_if_missing(release_default, local)

    legacy = root / DEFAULT_NAME
    if legacy.exists():
        return legacy
    return local


def default_config_path(name: str = DEFAULT_NAME) -> Path:
    """Resolve the default config path.

    Lookup order (frozen mode):
      1. Next to the exe (``dist/JoyType/config.yaml``) - the user-facing
         location, easy to find and edit.
      2. Copy bundled ``config.default.yaml`` there if the user-facing config
         is missing.
      3. Copy bundled legacy ``config.yaml`` there if present in an old build.

    Dev mode: ignored ``config.local.yaml`` copied from ``config.default.yaml``.
    """
    if name == DEFAULT_NAME:
        return ensure_runtime_config_path()

    if is_frozen():
        beside_exe = exe_dir() / name
        if beside_exe.exists():
            return beside_exe
        # PyInstaller 6+ onedir puts datas in <exe_dir>/_internal/
        in_bundle = exe_dir() / "_internal" / name
        if in_bundle.exists():
            return in_bundle
    return project_root() / name


def bundle_dir() -> Path:
    """The PyInstaller bundle root (_MEIPASS), or the repo root in dev.

    Useful for locating read-only assets that ARE bundled into the exe (icons,
    default templates, etc.). The writable config is deliberately NOT here.
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return project_root()
