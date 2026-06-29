"""Desktop adapter seam for OS-specific actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol


@dataclass(frozen=True)
class ForegroundWindow:
    process_name: str
    process_stem: str
    title: str


@dataclass(frozen=True)
class KeyChord:
    tokens: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.tokens)

    @classmethod
    def parse(cls, spec) -> "KeyChord":
        if spec is None:
            return cls(())
        if isinstance(spec, str):
            tokens = [t.strip().lower() for t in re.split(r"[+\s]+", spec) if t.strip()]
        elif isinstance(spec, list):
            tokens = [str(t).strip().lower() for t in spec if str(t).strip()]
        else:
            raise TypeError(f"hotkey must be a string or list, got {type(spec)}")
        return cls(tuple(tokens))

    def native_keys(self, desktop: "DesktopAdapter") -> list[int]:
        return [desktop.resolve_key(token) for token in self.tokens]


class DesktopAdapter(Protocol):
    def resolve_key(self, token: str) -> int:
        ...

    def key_name(self, native_key: int) -> str | None:
        ...

    def press_key(self, native_key: int) -> None:
        ...

    def release_key(self, native_key: int) -> None:
        ...

    def tap_key(self, native_key: int) -> None:
        ...

    def press_chord(self, native_keys: Iterable[int]) -> None:
        ...

    def release_chord(self, native_keys: Iterable[int]) -> None:
        ...

    def tap_chord(self, native_keys: Iterable[int]) -> None:
        ...

    def move_mouse_relative(self, dx: int, dy: int) -> None:
        ...

    def scroll_wheel(self, clicks: int) -> None:
        ...

    def click_mouse(self, button: str) -> None:
        ...

    def get_foreground_window(self) -> Optional[ForegroundWindow]:
        ...

    def release_all_modifiers(self) -> None:
        ...

    def begin_high_res_timer(self, period_ms: int = 1) -> None:
        ...

    def end_high_res_timer(self, period_ms: int = 1) -> None:
        ...


class WindowsDesktopAdapter:
    def resolve_key(self, token: str) -> int:
        from . import windows_util as wu
        return wu.resolve_vk(token)

    def key_name(self, native_key: int) -> str | None:
        from . import windows_util as wu
        return wu.vk_to_name(native_key)

    def press_key(self, native_key: int) -> None:
        from . import windows_util as wu
        wu.press_key(native_key)

    def release_key(self, native_key: int) -> None:
        from . import windows_util as wu
        wu.release_key(native_key)

    def tap_key(self, native_key: int) -> None:
        from . import windows_util as wu
        wu.tap_key(native_key)

    def press_chord(self, native_keys: Iterable[int]) -> None:
        from . import windows_util as wu
        wu.press_chord(native_keys)

    def release_chord(self, native_keys: Iterable[int]) -> None:
        from . import windows_util as wu
        wu.release_chord(native_keys)

    def tap_chord(self, native_keys: Iterable[int]) -> None:
        from . import windows_util as wu
        wu.tap_chord(native_keys)

    def move_mouse_relative(self, dx: int, dy: int) -> None:
        from . import windows_util as wu
        wu.move_mouse_relative(dx, dy)

    def scroll_wheel(self, clicks: int) -> None:
        from . import windows_util as wu
        wu.scroll_wheel(clicks)

    def click_mouse(self, button: str) -> None:
        from . import windows_util as wu
        wu.click_mouse(button)

    def get_foreground_window(self) -> Optional[ForegroundWindow]:
        from . import windows_util as wu
        fg = wu.get_foreground_window()
        if fg is None:
            return None
        return ForegroundWindow(
            process_name=fg.process_name,
            process_stem=fg.process_stem,
            title=fg.title,
        )

    def release_all_modifiers(self) -> None:
        from . import windows_util as wu
        wu.release_all_modifiers()

    def begin_high_res_timer(self, period_ms: int = 1) -> None:
        from . import windows_util as wu
        wu.begin_high_res_timer(period_ms)

    def end_high_res_timer(self, period_ms: int = 1) -> None:
        from . import windows_util as wu
        wu.end_high_res_timer(period_ms)
