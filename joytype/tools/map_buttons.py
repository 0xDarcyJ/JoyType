"""Button-bit calibration tool.

Asks the user to press each button in turn while the controller is open in
raw mode, and prints the byte offset + bit mask that fired for each. Use this
to derive the real _BUTTON_BITS table for your specific controller instead of
trusting a third-party spec.

Run:  python -m joytype.tools.map_buttons
"""

from __future__ import annotations

import sys
import time

# Allow `python -m joytype.tools.map_buttons` from a sibling checkout.
if __package__ in (None, ""):
    sys.path.insert(0, ".")

from joytype import hid_reader


# The 18 buttons to map, in the order the user will be prompted.
BUTTONS_TO_MAP = [
    "A", "B", "X", "Y", "L", "R", "ZL", "ZR",
    "UP", "DOWN", "LEFT", "RIGHT",
    "MINUS", "PLUS", "HOME", "CAPTURE", "L3", "R3",
]


def _bits_set(byte: int) -> list[int]:
    return [b for b in range(8) if byte & (1 << b)]


def _read_for_seconds(device, seconds: float) -> tuple[list[bytes], list[list[int]]]:
    """Read reports for `seconds`, returning (samples, per-byte bit unions)."""
    deadline = time.time() + seconds
    n_bytes = 78
    byte_union = [0] * n_bytes
    byte_idle = [0xFF] * n_bytes
    samples = 0
    while time.time() < deadline:
        raw = device.read(78)
        if not raw:
            continue
        b = bytes(raw)
        if b[0] != 0x30:
            continue
        samples += 1
        for i in range(n_bytes):
            byte_union[i] |= b[i]
            byte_idle[i] &= b[i]
    return byte_union, byte_idle, samples


def _find_changed_bits(byte_union, byte_idle, search_range=range(3, 6)):
    """Return list of (offset, bitmask) for bits that were sometimes set."""
    changes = []
    for i in search_range:
        toggled = byte_union[i] & ~byte_idle[i]
        for bit in range(8):
            mask = 1 << bit
            if toggled & mask:
                changes.append((i, mask))
    return changes


def main() -> int:
    print("Opening Joy-Con and enabling full report mode...")
    device, _ = hid_reader.open_controller(init=True)
    # Discard a few startup frames so readings settle.
    time.sleep(0.3)
    try:
        print()
        print("Each prompt will give you a few seconds. PRESS AND HOLD the")
        print("named button the whole time, then release.")
        print()

        results = {}
        for name in BUTTONS_TO_MAP:
            input(f"Press ENTER, then immediately HOLD {name} for 3 seconds... ")
            # Flush any reports queued before the hold started.
            device.set_nonblocking(True)
            while device.read(78):
                pass
            device.set_nonblocking(False)

            union, idle, n = _read_for_seconds(device, 3.0)
            # Find bits that toggled relative to baseline idle.
            changed = _find_changed_bits(union, idle)
            if not changed:
                print(f"  !! No button change detected for {name}. Try again.")
                results[name] = None
                continue
            desc = ", ".join(f"byte[{i}] bit 0x{m:02x}" for i, m in changed)
            print(f"  -> {name}: {desc}  ({n} samples)")
            results[name] = changed

        print()
        print("=== Mapping summary ===")
        print("# Paste these into hid_reader.py _BUTTON_BITS if they look right.")
        for name in BUTTONS_TO_MAP:
            r = results.get(name)
            if r:
                for (offset, mask) in r:
                    byte_idx = offset - 3  # convert to index within button_status[0..2]
                    print(f'    Button.{name:7s}: ({byte_idx}, 0x{mask:02x}),')
            else:
                print(f"    Button.{name}: <not detected>")
    finally:
        device.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
