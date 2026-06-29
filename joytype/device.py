"""Device adapter seam for input hardware."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Protocol

from . import hid_reader
from .state import Button, ControllerState

log = logging.getLogger(__name__)

HAPTIC_CLICK = "haptic.click"


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    display_name: str
    adapter_name: str
    layout_id: str
    controls: tuple[str, ...]
    capabilities: tuple[str, ...] = ()
    battery: int = -1
    charging: bool = False


@dataclass(frozen=True)
class InputReadSettings:
    deadzone: float = 0.15
    curve: str = "exponential"


class DeviceSession(Protocol):
    info: DeviceInfo

    def read_states(self, settings: InputReadSettings) -> Iterator[ControllerState]:
        ...

    def keep_alive(self) -> None:
        ...

    def play_feedback(
        self,
        effect: str,
        control: str | None = None,
        strength: str = "medium",
    ) -> None:
        ...

    def close(self) -> None:
        ...


class DeviceAdapter(Protocol):
    name: str

    def discover(self) -> list[DeviceInfo]:
        ...

    def open(self, device_id: str | None = None) -> DeviceSession:
        ...


JOYCON_L_CONTROLS = tuple(
    btn.value
    for btn in (
        Button.MINUS,
        Button.L3,
        Button.CAPTURE,
        Button.UP,
        Button.DOWN,
        Button.LEFT,
        Button.RIGHT,
        Button.LEFT_SL,
        Button.LEFT_SR,
        Button.L,
        Button.ZL,
    )
)

JOYCON_R_CONTROLS = tuple(
    btn.value
    for btn in (
        Button.A,
        Button.B,
        Button.X,
        Button.Y,
        Button.PLUS,
        Button.R3,
        Button.HOME,
        Button.RIGHT_SL,
        Button.RIGHT_SR,
        Button.R,
        Button.ZR,
    )
)


class JoyConHidSession:
    def __init__(self, device, product_id: int, info: DeviceInfo) -> None:
        self._device = device
        self._product_id = product_id
        self.info = info

    def read_states(self, settings: InputReadSettings) -> Iterator[ControllerState]:
        return hid_reader.read_states(
            self._device,
            report_len=hid_reader.report_length_for(self._product_id),
            product_id=self._product_id,
            deadzone=settings.deadzone,
            curve=settings.curve,
        )

    def keep_alive(self) -> None:
        hid_reader.send_keep_alive(self._device)

    def play_feedback(
        self,
        effect: str,
        control: str | None = None,
        strength: str = "medium",
    ) -> None:
        """Play device feedback.

        ``control`` is accepted for the generic DeviceSession contract, but
        Joy-Con click feedback is whole-device rumble, so it is not used yet.
        """
        if effect != HAPTIC_CLICK:
            raise ValueError(f"Unsupported feedback effect: {effect}")
        hid_reader.send_rumble_click(self._device, strength=strength)

    def close(self) -> None:
        self._device.close()


class JoyConHidAdapter:
    name = "joycon-hid"

    @staticmethod
    def info_for_product(product_id: int, device_id: str) -> DeviceInfo:
        if product_id == hid_reader.JOYCON_L_PRODUCT_ID:
            layout_id = "joycon-l"
            controls = JOYCON_L_CONTROLS
        elif product_id == hid_reader.JOYCON_R_PRODUCT_ID:
            layout_id = "joycon-r"
            controls = JOYCON_R_CONTROLS
        else:
            raise ValueError(f"Unsupported Joy-Con product ID: 0x{product_id:04x}")
        return DeviceInfo(
            id=device_id,
            display_name=hid_reader.controller_kind(product_id),
            adapter_name=JoyConHidAdapter.name,
            layout_id=layout_id,
            controls=controls,
            capabilities=(HAPTIC_CLICK,),
        )

    def discover(self) -> list[DeviceInfo]:
        hid = hid_reader._open_hid_module()  # noqa: SLF001 - adapter owns HID discovery
        devices: list[DeviceInfo] = []
        for product_id in hid_reader.NINTENDO_PRODUCT_IDS:
            for raw in hid.enumerate(hid_reader.NINTENDO_VENDOR_ID, product_id):
                devices.append(
                    self.info_for_product(
                        int(raw.get("product_id") or product_id),
                        device_id=self._device_id(raw.get("path")),
                    )
                )
        return devices

    def open(self, device_id: str | None = None) -> JoyConHidSession:
        hid = hid_reader._open_hid_module()  # noqa: SLF001 - adapter owns HID discovery
        if device_id is None:
            raw = self._first_raw_device(hid)
            if raw is None:
                raise RuntimeError(
                    "No Joy-Con found. Make sure a Joy-Con is paired via Bluetooth "
                    "and turned on."
                )
            return self._open_raw_device(hid, raw, self._device_id(raw.get("path")))

        for raw in self._raw_devices(hid):
            raw_device_id = self._device_id(raw.get("path"))
            if raw_device_id != device_id:
                continue
            return self._open_raw_device(hid, raw, raw_device_id)
        raise RuntimeError(f"Joy-Con device not found: {device_id}")

    @staticmethod
    def _raw_devices(hid) -> list[dict]:
        devices: list[dict] = []
        for product_id in hid_reader.NINTENDO_PRODUCT_IDS:
            for raw in hid.enumerate(hid_reader.NINTENDO_VENDOR_ID, product_id):
                normalized = dict(raw)
                if normalized.get("product_id") is None:
                    normalized["product_id"] = product_id
                devices.append(normalized)
        return devices

    def _first_raw_device(self, hid) -> dict | None:
        for raw in self._raw_devices(hid):
            return raw
        return None

    def _open_raw_device(
        self,
        hid,
        raw: dict,
        device_id: str,
    ) -> JoyConHidSession:
        product_id = int(raw["product_id"])
        device = hid.device()
        device.open_path(raw["path"])
        device.set_nonblocking(False)
        try:
            hid_reader.initialize_controller(device, enable_imu=True)
        except OSError:
            log.warning(
                "Could not send init subcommand; button reports may stay "
                "empty. Continuing anyway - retry happens on next connect.",
                exc_info=True,
            )
        return JoyConHidSession(
            device,
            product_id,
            self.info_for_product(product_id, device_id=device_id),
        )

    @staticmethod
    def _device_id(path) -> str:
        if isinstance(path, bytes):
            return path.hex()
        return str(path)
