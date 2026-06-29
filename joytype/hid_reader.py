"""Joy-Con HID reader (Bluetooth transport).

Reads raw HID reports from a Bluetooth-connected Nintendo Joy-Con (vendor
0x057E; Joy-Con L = 0x2006, Joy-Con R = 0x2007) and turns each report into a
normalized :class:`~joytype.state.ControllerState`.

Protocol notes
--------------
Over Bluetooth a Joy-Con emits **input report 0x30** - a 49-byte "standard"
report (offset 0 = the report-id byte):

    [0]      report id      = 0x30
    [1]      timer byte
    [2]      battery / connection info
    [3:6]    button_status  (3 bytes, little-endian bitfield)
    [6:9]    left stick     x(12) + y(12)   (the Joy-Con L's analog stick)
    [9:12]   right stick    x(12) + y(12)   (the Joy-Con R's analog stick)
    [13:]    IMU tails (ignored by JoyType)

Bit positions and the 12-bit stick packing follow the Linux kernel driver
``hid-nintendo.c`` (``JC_BUTTON_*`` macros). Buttons are active-high; sticks
are centered near 0x800 over a 0x000..0xFFF range. A lone Joy-Con reports its
single stick in its own slot (L = left, R = right); the other slot is unused.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

from .state import Button, ControllerState, StickState

log = logging.getLogger(__name__)

# Nintendo Joy-Con USB Vendor/Product IDs.
NINTENDO_VENDOR_ID = 0x057E
JOYCON_L_PRODUCT_ID = 0x2006
JOYCON_R_PRODUCT_ID = 0x2007
NINTENDO_PRODUCT_IDS = (JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID)

# Report id the controller streams over Bluetooth.
INPUT_REPORT_STANDARD = 0x30
# Minimum usable length of a standard input report (we need the first 12 data
# bytes; the rest is IMU tails we ignore).
STANDARD_REPORT_MIN_LEN = 12
# Joy-Cons send 49-byte reports over Bluetooth. hidapi's read() MUST use this
# length or it returns misaligned data where button bytes appear stuck at zero.
JOYCON_REPORT_LEN = 49

# Offsets within the report (offset 0 = report-id byte).
OFFSET_BUTTONS = 3
OFFSET_LEFT_STICK = 6
OFFSET_RIGHT_STICK = 9

# --- Button bit masks -------------------------------------------------------
# Bit positions inside the 3-byte little-endian button_status field, as
# (byte_index, bit_mask) relative to OFFSET_BUTTONS. Verified against a real
# Joy-Con L and matching Chromium nintendo_controller.cc + pyjoycon.
#   byte 0 (report[3]) - Joy-Con R cluster + face buttons
#   byte 1 (report[4]) - shared center cluster
#   byte 2 (report[5]) - Joy-Con L cluster
_BUTTON_BITS: dict[Button, tuple[int, int]] = {
    Button.Y:        (0, 0x01),
    Button.X:        (0, 0x02),
    Button.B:        (0, 0x04),
    Button.A:        (0, 0x08),
    Button.RIGHT_SR: (0, 0x10),
    Button.RIGHT_SL: (0, 0x20),
    Button.R:        (0, 0x40),
    Button.ZR:       (0, 0x80),
    Button.MINUS:    (1, 0x01),
    Button.PLUS:     (1, 0x02),
    Button.R3:       (1, 0x04),
    Button.L3:       (1, 0x08),
    Button.HOME:     (1, 0x10),
    Button.CAPTURE:  (1, 0x20),
    Button.DOWN:     (2, 0x01),
    Button.UP:       (2, 0x02),
    Button.RIGHT:    (2, 0x04),
    Button.LEFT:     (2, 0x08),
    Button.LEFT_SR:  (2, 0x10),
    Button.LEFT_SL:  (2, 0x20),
    Button.L:        (2, 0x40),
    Button.ZL:       (2, 0x80),
}

# Stick: each axis is a 12-bit unsigned value, center ~= 0x800.
_STICK_CENTER = 0x800
_STICK_RANGE = 0x800  # half of 12-bit range; treat as ±1.0


def _decode_stick(b0: int, b1: int, b2: int) -> tuple[float, float]:
    """Decode a 3-byte stick packet into (x, y) floats in [-1, 1].

    Layout (per hid-nintendo.c): x = b0 | ((b1 & 0x0F) << 8),
    y = (b1 >> 4) | (b2 << 4). Nintendo reports higher raw y for "stick up",
    which maps to screen-up = positive y; mouse movers re-invert for Windows.
    """
    x = b0 | ((b1 & 0x0F) << 8)
    y = (b1 >> 4) | (b2 << 4)
    fx = (x - _STICK_CENTER) / _STICK_RANGE
    fy = (y - _STICK_CENTER) / _STICK_RANGE
    return fx, fy


def _extract_buttons(report: bytes) -> frozenset[Button]:
    """Decode the 3-byte button_status field into a set of pressed buttons."""
    pressed: set[Button] = set()
    for btn, (byte_idx, mask) in _BUTTON_BITS.items():
        if report[OFFSET_BUTTONS + byte_idx] & mask:
            pressed.add(btn)
    return frozenset(pressed)


def parse_report(
    report: bytes, *, product_id: int = JOYCON_L_PRODUCT_ID
) -> Optional[ControllerState]:
    """Parse one raw HID report into a :class:`ControllerState`.

    A lone Joy-Con reports its single analog stick in its own slot: the
    Joy-Con L in the left slot (bytes 6-8), the Joy-Con R in the right slot
    (bytes 9-11). The unused slot is zeroed.

    Returns ``None`` for reports that aren't standard input reports
    (subcommand acks, etc.) so the caller can silently skip them.
    """
    if len(report) < STANDARD_REPORT_MIN_LEN:
        return None
    if report[0] != INPUT_REPORT_STANDARD:
        return None

    buttons = _extract_buttons(report)

    if product_id == JOYCON_R_PRODUCT_ID:
        rx, ry = _decode_stick(
            report[OFFSET_RIGHT_STICK], report[OFFSET_RIGHT_STICK + 1],
            report[OFFSET_RIGHT_STICK + 2],
        )
        lx = ly = 0.0
    else:
        # Joy-Con L (default): analog stick in the left slot.
        lx, ly = _decode_stick(
            report[OFFSET_LEFT_STICK], report[OFFSET_LEFT_STICK + 1],
            report[OFFSET_LEFT_STICK + 2],
        )
        rx = ry = 0.0

    # Trigger values are digital; expose them as 0/1 alongside the button set.
    zl = 1.0 if Button.ZL in buttons else 0.0
    zr = 1.0 if Button.ZR in buttons else 0.0

    # Byte 2 = battery + connection. In the high nibble the LSB is the charging
    # flag and the even part (8/6/4/2/0 = full/high/mid/low/empty) is the coarse
    # level; we normalize to 0..4 (4 = full).
    nib = report[2] >> 4
    charging = bool(nib & 0x01)
    battery = (nib & 0x0E) // 2

    return ControllerState(
        buttons=buttons,
        left=StickState(lx, ly),
        right=StickState(rx, ry),
        zl=zl,
        zr=zr,
        battery=battery,
        charging=charging,
    )


# ----------------------------------------------------------------------------
# Device lifecycle
# ----------------------------------------------------------------------------


def _open_hid_module():
    """Import the ``hid`` package lazily with a friendly error if missing."""
    try:
        import hid  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "The 'hid' module provided by the 'hidapi' Python package is not "
            "installed. Run: pip install hidapi."
        ) from exc
    return hid


def find_controller():
    """Return the ``hid`` device info for the connected Joy-Con.

    Tries Joy-Con L first, then Joy-Con R. Returns ``None`` when nothing is
    paired or awake; the caller is expected to retry.
    """
    hid = _open_hid_module()
    for pid in (JOYCON_L_PRODUCT_ID, JOYCON_R_PRODUCT_ID):
        for info in hid.enumerate(NINTENDO_VENDOR_ID, pid):
            return info
    return None


def controller_kind(product_id: int) -> str:
    """Human-readable name for a Joy-Con PID."""
    return {
        JOYCON_L_PRODUCT_ID: "Joy-Con (L)",
        JOYCON_R_PRODUCT_ID: "Joy-Con (R)",
    }.get(product_id, f"Nintendo 0x{product_id:04x}")


def report_length_for(product_id: int) -> int:
    """HID read length for a Joy-Con (always the 49-byte standard report)."""
    return JOYCON_REPORT_LEN


# --- Output reports / subcommands (switch the controller into full mode) -----
#
# Out of the box a Joy-Con paired over Bluetooth ships a "simple" input report
# in which the button bytes are always zero. The Linux hid-nintendo driver
# fixes this by sending SET_REPORT_MODE (subcommand 0x03, arg 0x30) inside an
# output report 0x01 (RUMBLE_AND_SUBCMD); Joy-Cons also need ENABLE_IMU before
# they stream full reports. We replicate both here.
#
# Output report 0x01 layout (49 bytes total):
#   [0]    output id = 0x01
#   [1]    packet number (we just tick it)
#   [2:10] rumble data (8 bytes; zeros = no rumble)
#   [10]   subcommand id
#   [11:]  subcommand data
OUTPUT_REPORT_RUMBLE_AND_SUBCMD = 0x01
SUBCMD_SET_REPORT_MODE = 0x03
SUBCMD_ARG_FULL_REPORT = 0x30  # stream full 0x30 input reports
SUBCMD_ENABLE_IMU = 0x40       # arg 0x01 enables accel + gyro
SUBCMD_ENABLE_VIBRATION = 0x48  # arg 0x01 enables Joy-Con vibration
# Player indicator LEDs (subcommand id 0x30 - distinct from the 0x30 ARG above).
# Arg bitfield: low nibble = solid LEDs (bit0=LED1..bit3=LED4), high nibble =
# flashing. 0x01 = LED 1 solid ("player 1"); without this the Joy-Con cycles
# all 4 LEDs forever (the "unassigned / connecting" animation).
SUBCMD_SET_PLAYER_LIGHTS = 0x30
SUBCMD_ARG_PLAYER_1 = 0x01
OUTPUT_REPORT_LEN = 49
RUMBLE_NEUTRAL = bytes([0x00, 0x01, 0x40, 0x40] * 2)
RUMBLE_CLICK_PULSES = {
    "light": bytes([0x00, 0x12, 0x40, 0x44] * 2),
    "medium": bytes([0x00, 0x1D, 0x40, 0x47] * 2),
    "strong": bytes([0x00, 0x30, 0x40, 0x4A] * 2),
}
RUMBLE_CLICK_PULSE = RUMBLE_CLICK_PULSES["medium"]
SUBCOMMAND_SETTLE_S = 0.02


def _send_subcommand(device, subcmd_id: int, subcmd_arg: int,
                     packet_num: int = 0) -> int:
    """Write one output report 0x01 with the given subcommand.

    Returns the byte count reported by hidapi. Wraps OS write errors as
    :class:`OSError`.
    """
    buf = bytearray(OUTPUT_REPORT_LEN)
    buf[0] = OUTPUT_REPORT_RUMBLE_AND_SUBCMD
    buf[1] = packet_num & 0x0F
    # bytes [2:10] rumble already zeroed by bytearray()
    buf[10] = subcmd_id
    buf[11] = subcmd_arg
    written = device.write(bytes(buf))
    if written <= 0:
        raise OSError(
            f"Controller write returned {written} for subcmd 0x{subcmd_id:02x}"
        )
    return written


def initialize_controller(device, *, packet_num: int = 0,
                          enable_imu: bool = True) -> bytes:
    """Send the init handshake so the Joy-Con streams full button reports.

    Init sequence:
      1. ENABLE_IMU (subcmd 0x40, arg 0x01) - Joy-Cons ship with IMU off and
         refuse to send full button reports until it's enabled. Skipped when
         ``enable_imu`` is false.
      2. ENABLE_VIBRATION (subcmd 0x48, arg 0x01) - allow rumble output
         reports to actuate haptics. Non-fatal so buttons still work if this
         write fails.
      3. SET_REPORT_MODE (subcmd 0x03, arg 0x30) - request full 0x30 reports
         containing the button bytes (without this, buttons read as zero).

    Idempotent: re-sending is harmless. Wraps OS write errors as
    :class:`OSError`. Set ``enable_imu=False`` to skip step 1 (rarely needed).
    """
    next_packet_num = packet_num & 0x0F

    def send(subcmd_id: int, subcmd_arg: int) -> tuple[int, int]:
        nonlocal next_packet_num
        used_packet_num = next_packet_num
        written_count = _send_subcommand(
            device, subcmd_id, subcmd_arg, used_packet_num
        )
        next_packet_num = (next_packet_num + 1) & 0x0F
        return written_count, used_packet_num

    if enable_imu:
        send(SUBCMD_ENABLE_IMU, 0x01)
        # Brief settle before changing report mode (pyjoycon sleeps 20ms here).
        time.sleep(SUBCOMMAND_SETTLE_S)
        log.debug("Sent ENABLE_IMU (subcmd 0x40)")

    try:
        send(SUBCMD_ENABLE_VIBRATION, 0x01)
        time.sleep(SUBCOMMAND_SETTLE_S)
        log.debug("Sent ENABLE_VIBRATION (subcmd 0x48)")
    except OSError:
        log.debug("ENABLE_VIBRATION failed (non-fatal)", exc_info=True)

    written, report_mode_packet_num = send(
        SUBCMD_SET_REPORT_MODE, SUBCMD_ARG_FULL_REPORT
    )
    log.debug("Sent SET_REPORT_MODE (wrote %d bytes)", written)

    # Fix a steady player-1 LED so the controller stops cycling all 4 LEDs.
    # Cosmetic, so a failure here must not break init.
    time.sleep(SUBCOMMAND_SETTLE_S)
    try:
        send(SUBCMD_SET_PLAYER_LIGHTS, SUBCMD_ARG_PLAYER_1)
        log.debug("Sent SET_PLAYER_LIGHTS (LED 1 solid)")
    except OSError:
        log.debug("SET_PLAYER_LIGHTS failed (non-fatal)", exc_info=True)

    buf = bytearray(OUTPUT_REPORT_LEN)
    buf[0] = OUTPUT_REPORT_RUMBLE_AND_SUBCMD
    buf[1] = report_mode_packet_num
    buf[10] = SUBCMD_SET_REPORT_MODE
    buf[11] = SUBCMD_ARG_FULL_REPORT
    return bytes(buf)


def send_keep_alive(device) -> None:
    """Poke the controller so it doesn't idle-sleep (re-assert full report mode).

    Re-sends SET_REPORT_MODE: idempotent, and the host->device traffic keeps the
    link/controller from dropping into a low-power state that makes the next
    button press laggy. Raises :class:`OSError` on write failure.
    """
    _send_subcommand(device, SUBCMD_SET_REPORT_MODE, SUBCMD_ARG_FULL_REPORT)


def _write_rumble(device, rumble_data: bytes, *, packet_num: int = 0) -> None:
    if len(rumble_data) != 8:
        raise ValueError("Joy-Con rumble payload must be exactly 8 bytes")
    buf = bytearray(OUTPUT_REPORT_LEN)
    buf[0] = OUTPUT_REPORT_RUMBLE_AND_SUBCMD
    buf[1] = packet_num & 0x0F
    buf[2:10] = rumble_data
    written = device.write(bytes(buf))
    if written <= 0:
        raise OSError(f"Controller rumble write returned {written}")


def send_rumble_click(
    device,
    packet_num: int = 0,
    duration_s: float = 0.035,
    strength: str = "medium",
) -> None:
    """Send a short conservative haptic click, then return rumble to neutral."""
    try:
        pulse = RUMBLE_CLICK_PULSES[strength]
    except KeyError as exc:
        raise ValueError(f"Unsupported haptic strength: {strength}") from exc
    _write_rumble(device, pulse, packet_num=packet_num)
    time.sleep(duration_s)
    _write_rumble(device, RUMBLE_NEUTRAL, packet_num=packet_num)


def open_controller(*, init: bool = True):
    """Open the connected Joy-Con for reading; return ``(device, product_id)``.

    Auto-detects Joy-Con L or R. When ``init`` is True (default) sends the
    init handshake so button events and optional vibration feedback are ready.
    Caller must ``close()`` on shutdown. Raises :class:`RuntimeError` if no
    Joy-Con is visible.
    """
    hid = _open_hid_module()
    info = find_controller()
    if info is None:
        raise RuntimeError(
            "No Joy-Con found. Make sure a Joy-Con is paired via Bluetooth "
            "and turned on."
        )
    product_id = info["product_id"]
    device = hid.device()
    device.open_path(info["path"])
    device.set_nonblocking(False)  # blocking read, no timeout
    log.info(
        "Opened %s: manufacturer=%r product=%r",
        controller_kind(product_id),
        info.get("manufacturer_string"),
        info.get("product_string"),
    )

    if init:
        try:
            initialize_controller(device, enable_imu=True)
        except OSError:
            log.warning(
                "Could not send init subcommand; button reports may stay "
                "empty. Continuing anyway - retry happens on next connect.",
                exc_info=True,
            )

    return device, product_id


def read_states(
    device,
    *,
    report_len: int = JOYCON_REPORT_LEN,
    product_id: int = JOYCON_L_PRODUCT_ID,
    deadzone: float = 0.15,
    curve: str = "exponential",
) -> Iterator[ControllerState]:
    """Yield parsed :class:`ControllerState` objects forever.

    Runs until the device is closed or a read fails. Stick shaping (dead-zone
    + curve) and auto-center calibration are applied here so downstream code
    always sees clean, drift-free values.

    ``report_len`` MUST be the 49-byte Joy-Con report length; get it from
    :func:`report_length_for`.
    """
    # Auto-center calibration. A Joy-Con stick rarely rests exactly at the
    # 0x800 center we assume, so a fixed center makes the mouse drift. We
    # average the resting position over the first CAL_FRAMES reports and
    # subtract it thereafter. Re-runs on every reconnect (generator restarts).
    from .state import shape_stick

    CAL_FRAMES = 12
    cal_l: list[tuple[float, float]] = []
    cal_r: list[tuple[float, float]] = []
    center_l = (0.0, 0.0)
    center_r = (0.0, 0.0)
    calibrated = False

    while True:
        try:
            raw = device.read(report_len)
        except OSError as exc:
            raise ConnectionError(f"Controller read failed: {exc}") from exc
        if not raw:
            continue
        state = parse_report(bytes(raw), product_id=product_id)
        if state is None:
            continue

        if not calibrated:
            # Collect resting samples; treat the stick as centered meanwhile.
            cal_l.append((state.left.x, state.left.y))
            cal_r.append((state.right.x, state.right.y))
            if len(cal_l) >= CAL_FRAMES:
                center_l = (
                    sum(x for x, _ in cal_l) / len(cal_l),
                    sum(y for _, y in cal_l) / len(cal_l),
                )
                center_r = (
                    sum(x for x, _ in cal_r) / len(cal_r),
                    sum(y for _, y in cal_r) / len(cal_r),
                )
                calibrated = True
            left = shape_stick(0.0, 0.0, deadzone, curve)
            right = shape_stick(0.0, 0.0, deadzone, curve)
        else:
            left = shape_stick(
                state.left.x - center_l[0], state.left.y - center_l[1],
                deadzone, curve,
            )
            right = shape_stick(
                state.right.x - center_r[0], state.right.y - center_r[1],
                deadzone, curve,
            )

        yield ControllerState(
            buttons=state.buttons,
            left=left,
            right=right,
            zl=state.zl,
            zr=state.zr,
            battery=state.battery,
            charging=state.charging,
        )


# Convenience for ad-hoc testing: pair, run `python -m joytype.hid_reader`,
# and watch parsed events stream to the console.
def _main() -> None:  # pragma: no cover - manual test entry point
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("Opening Joy-Con for live event dump...")
    device, product_id = open_controller()
    report_len = report_length_for(product_id)
    prev: Optional[ControllerState] = None
    try:
        for state in read_states(
            device, report_len=report_len, product_id=product_id,
            deadzone=0.15, curve="exponential",
        ):
            from .state import diff_states

            for ev in diff_states(prev, state):
                log.info("BUTTON %s %s", ev.button.value, "DOWN" if ev.pressed else "UP")
            if state.left.magnitude > 0.05 or state.right.magnitude > 0.05:
                log.info(
                    "L(%+.2f,%+.2f) R(%+.2f,%+.2f)",
                    state.left.x, state.left.y, state.right.x, state.right.y,
                )
            prev = state
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        device.close()


if __name__ == "__main__":  # pragma: no cover
    _main()
