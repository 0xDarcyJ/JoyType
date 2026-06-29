"""Normalized controller state.

HID parsing is transport-specific (Bluetooth vs USB differ); everything
downstream of this module speaks in terms of :class:`Button` / stick floats
only. Two responsibilities:

1. Provide stable, human-readable names for inputs (used verbatim in
   ``config.yaml``).
2. Apply input shaping (dead-zone, response curve) and edge detection
   (button down/up transitions, stick motion deltas).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional


class Button(Enum):
    """All readable Nintendo controller buttons used by JoyType.

    Names are the exact tokens used as keys in ``config.yaml`` profiles
    (uppercased). New buttons must be appended, never renumbered - that would
    silently rebind every config out there.
    """

    # Face / diamond
    A = "A"
    B = "B"
    X = "X"
    Y = "Y"
    # Shoulder triggers (digital on Joy-Con).
    L = "L"
    R = "R"
    ZL = "ZL"
    ZR = "ZR"
    # Center cluster
    MINUS = "MINUS"
    PLUS = "PLUS"
    HOME = "HOME"
    CAPTURE = "CAPTURE"
    # Stick clicks
    L3 = "L3"
    R3 = "R3"
    # D-pad
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    # Joy-Con side-rail buttons (SL/SR).
    # LEFT_SL/SR live on a Joy-Con L; RIGHT_SL/SR on a Joy-Con R.
    LEFT_SL = "LEFT_SL"
    LEFT_SR = "LEFT_SR"
    RIGHT_SL = "RIGHT_SL"
    RIGHT_SR = "RIGHT_SR"


# Config files use upper-case tokens; accept them case-insensitively.
_BUTTON_BY_TOKEN = {b.value.upper(): b for b in Button}


def parse_button(token: str) -> Button:
    """Resolve a config-file token to a :class:`Button`.

    Raises :class:`KeyError` with a helpful message on unknown names so a typo
    in ``config.yaml`` fails loudly instead of silently binding nothing.
    """
    try:
        return _BUTTON_BY_TOKEN[token.upper()]
    except KeyError as exc:
        known = ", ".join(sorted(b.value for b in Button))
        raise KeyError(
            f"Unknown button '{token}'. Known buttons: {known}"
        ) from exc


@dataclass(frozen=True)
class StickState:
    """A single analog stick, centered at (0.0, 0.0), range [-1, 1].

    ``y`` is screen-oriented: positive = up (raw HID y-up is flipped to match
    mouse semantics so consumers never have to think about it).
    """

    x: float = 0.0
    y: float = 0.0

    @property
    def magnitude(self) -> float:
        """Radial deflection 0..~1.1; use for dead-zone and speed scaling."""
        return math.hypot(self.x, self.y)


@dataclass(frozen=True)
class ControllerState:
    """Full snapshot of the controller at one instant.

    Immutable on purpose: the binder compares consecutive snapshots to derive
    edge events rather than trusting the device to send clean transitions.
    """

    buttons: frozenset[Button] = frozenset()
    left: StickState = StickState()
    right: StickState = StickState()
    # Triggers are digital on a Joy-Con; kept as 0/1 for a uniform API.
    zl: float = 0.0
    zr: float = 0.0
    # Battery: coarse level 0..4 (4=full), -1 = unknown; plus charging flag.
    battery: int = -1
    charging: bool = False

    def is_pressed(self, b: Button) -> bool:
        return b in self.buttons


# ----------------------------------------------------------------------------
# Input shaping helpers
# ----------------------------------------------------------------------------


def shape_stick(x: float, y: float, deadzone: float, curve: str) -> StickState:
    """Apply dead-zone + response curve to raw stick coordinates.

    ``curve`` is ``"linear"`` or ``"exponential"`` (smoother near center,
    sharper near the rim - usually what you want for pointer control).
    """
    # Radial dead-zone: gate the whole vector on its magnitude, not per-axis,
    # so diagonal motion doesn't get clipped into a square.
    mag = math.hypot(x, y)
    if mag < deadzone:
        return StickState(0.0, 0.0)

    scale = (mag - deadzone) / (mag * (1.0 - deadzone))
    sx, sy = x * scale, y * scale

    if curve == "exponential":
        def _shape(v: float) -> float:
            # Square the magnitude for fine control near center, but KEEP the
            # sign. The old `else -(-v * v)` branch flipped negatives positive,
            # so the cursor only ever moved up/right - copysign handles both
            # signs correctly in one expression.
            return math.copysign(v * v, v)
        sx, sy = _shape(sx), _shape(sy)

    return StickState(sx, sy)


# ----------------------------------------------------------------------------
# Edge detection
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ButtonEvent:
    """A button state transition between two snapshots."""

    button: Button
    pressed: bool  # True = just went down, False = just released


@dataclass(frozen=True)
class StickFrame:
    """A shaped stick sample ready for consumption.

    Carries the delta since the previous frame so mouse movers can integrate
    position without re-deriving it.
    """

    side: str  # "left" | "right"
    state: StickState


def diff_states(
    prev: Optional[ControllerState], curr: ControllerState
) -> Iterator[ButtonEvent]:
    """Yield every button edge between ``prev`` and ``curr``.

    ``prev=None`` is treated as "everything released" - the first frame after
    connect therefore synthesizes no spurious presses, only releases of
    whatever the device reports as held (rare, but the contract is clean).
    """
    prev_set = prev.buttons if prev is not None else frozenset()
    curr_set = curr.buttons

    for b in curr_set - prev_set:
        yield ButtonEvent(b, pressed=True)
    for b in prev_set - curr_set:
        yield ButtonEvent(b, pressed=False)
