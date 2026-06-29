"""Joy-Con connection diagnostics for Windows Bluetooth/HID issues.

Run this while the controller is in the "JoyType cannot connect" state:

    python -m joytype.tools.connection_diag --seconds 8

The command separates three layers:

- Windows still has a stale Joy-Con pairing, but no HID controller node.
- HID sees the Joy-Con, but hidapi cannot open it.
- HID opens and standard 0x30 input reports are streaming.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Iterable

from joytype import hid_reader


@dataclass(frozen=True)
class ProbeStats:
    devices_found: int
    open_attempted: bool = False
    open_ok: bool = False
    init_ok: bool = False
    haptic_attempted: bool = False
    haptic_ok: bool = False
    haptic_error: str = ""
    total_reports: int = 0
    standard_reports: int = 0
    parsed_reports: int = 0
    report_id_counts: tuple[tuple[int, int], ...] = ()
    sample_reports: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class Diagnosis:
    code: str
    message: str
    next_step: str


@dataclass(frozen=True)
class PnpSnapshot:
    has_joycon_pairing: bool = False
    has_hid_game_controller: bool = False


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "mbcs", "latin-1"):
            try:
                return value.decode(encoding)
            except Exception:
                pass
        return repr(value)
    return str(value)


def _short(value: Any, *, max_len: int = 120) -> str:
    text = _text(value).replace("\r", "").replace("\n", " ")
    if len(text) <= max_len:
        return text
    head = max_len // 2 - 2
    tail = max_len - head - 5
    return f"{text[:head]} ... {text[-tail:]}"


def format_hid_device(info: dict[str, Any], *, index: int) -> str:
    """Return one compact line for a hid.enumerate() device dictionary."""
    product_id = int(info.get("product_id") or 0)
    kind = hid_reader.controller_kind(product_id) if product_id else "Nintendo HID"
    fields = [
        f"{index}. {kind}",
        f"pid=0x{product_id:04x}",
    ]
    for key, label in (
        ("manufacturer_string", "manufacturer"),
        ("product_string", "product"),
        ("serial_number", "serial"),
        ("interface_number", "interface"),
        ("usage_page", "usage_page"),
        ("usage", "usage"),
        ("path", "path"),
    ):
        text = _short(info.get(key))
        if text:
            fields.append(f"{label}={text}")
    return " | ".join(fields)


def format_report_id_counts(counts: Iterable[tuple[int, int]]) -> str:
    items = tuple(counts)
    if not items:
        return "-"
    return ", ".join(
        f"0x{report_id:02x}:{count}" for report_id, count in items
    )


def _format_report_sample(data: bytes, *, max_bytes: int = 16) -> str:
    if not data:
        return "len=0 id=- data="
    prefix = data[:max_bytes].hex(" ")
    suffix = " ..." if len(data) > max_bytes else ""
    return f"len={len(data)} id=0x{data[0]:02x} data={prefix}{suffix}"


def collect_hid_devices() -> list[dict[str, Any]]:
    """Enumerate supported Nintendo Joy-Con HID devices."""
    hid = hid_reader._open_hid_module()  # noqa: SLF001 - diagnostics surface
    devices: list[dict[str, Any]] = []
    for product_id in hid_reader.NINTENDO_PRODUCT_IDS:
        devices.extend(hid.enumerate(hid_reader.NINTENDO_VENDOR_ID, product_id))
    return devices


def analyze_pnp_lines(lines: Iterable[str]) -> PnpSnapshot:
    """Detect whether Windows remembers Joy-Con but lacks the HID node."""
    has_joycon_pairing = False
    has_hid_game_controller = False
    for line in lines:
        lower = line.lower()
        upper = line.upper()
        mentions_joycon = (
            "joy-con" in lower
            or "nintendo" in lower
            or "VID&0002057E_PID&2006" in upper
            or "VID&0002057E_PID&2007" in upper
        )
        if not mentions_joycon:
            continue
        if "BTHENUM" in upper:
            has_joycon_pairing = True
        if "HID-COMPLIANT GAME CONTROLLER" in upper or (
            "HID\\" in upper and "VID&0002057E_PID&" in upper
        ):
            has_hid_game_controller = True
    return PnpSnapshot(
        has_joycon_pairing=has_joycon_pairing,
        has_hid_game_controller=has_hid_game_controller,
    )


def classify_probe(
    stats: ProbeStats, pnp: PnpSnapshot | None = None
) -> Diagnosis:
    """Turn raw probe counters into a field-debuggable verdict."""
    if (
        stats.devices_found == 0
        and pnp is not None
        and pnp.has_joycon_pairing
        and not pnp.has_hid_game_controller
    ):
        return Diagnosis(
            code="stale_bluetooth_pairing",
            message=(
                "Windows still has a Joy-Con pairing record, but it did not "
                "create the HID game controller node JoyType needs."
            ),
            next_step=(
                "Remove the Joy-Con in Windows Bluetooth settings, then pair "
                "it again. This can happen after connecting the controller to "
                "another host."
            ),
        )
    if stats.devices_found == 0:
        return Diagnosis(
            code="no_hid_device",
            message=(
                "Windows is not exposing an awake Joy-Con as a supported HID "
                "device to JoyType."
            ),
            next_step=(
                "Wake the Joy-Con. If Windows still shows it as paired but not "
                "connected, remove it in Windows Bluetooth settings and pair "
                "it again."
            ),
        )
    if stats.open_attempted and not stats.open_ok:
        detail = f" Error: {stats.error}" if stats.error else ""
        return Diagnosis(
            code="open_failed",
            message=(
                "A supported Joy-Con HID device is visible, but JoyType/hidapi "
                f"could not open it.{detail}"
            ),
            next_step=(
                "Close other controller tools, stop/start the JoyType daemon, "
                "then rerun the probe before re-pairing."
            ),
        )
    if stats.open_ok and stats.parsed_reports > 0:
        return Diagnosis(
            code="streaming",
            message=(
                "JoyType is receiving Joy-Con input reports from Windows HID."
            ),
            next_step=(
                "If the app still says disconnected, focus on the JoyType daemon "
                "or GUI status path rather than Bluetooth pairing."
            ),
        )
    if stats.open_ok and stats.standard_reports > 0:
        return Diagnosis(
            code="unparsed_standard_reports",
            message=(
                "The Joy-Con is sending standard 0x30 reports, but JoyType did "
                "not parse them into controller states."
            ),
            next_step=(
                "Save this probe output; the parser/product-id path is the next "
                "thing to inspect."
            ),
        )
    if stats.open_ok and stats.total_reports > 0:
        return Diagnosis(
            code="nonstandard_reports",
            message=(
                "The HID device opened and sent reports, but not Joy-Con 0x30 "
                "standard input reports."
            ),
            next_step=(
                "This usually means initialization/report-mode negotiation did "
                "not take; rerun with the controller freshly awake."
            ),
        )
    if stats.open_ok and not stats.init_ok and stats.error:
        return Diagnosis(
            code="init_failed",
            message=(
                "The HID device opened, but JoyType could not send the Joy-Con "
                f"initialization handshake. Error: {stats.error}"
            ),
            next_step=(
                "This points below the app UI and above Windows pairing: HID "
                "write/init is failing."
            ),
        )
    return Diagnosis(
        code="opened_no_reports",
        message=(
            "The HID device opened, but no Joy-Con input reports arrived during "
            "the probe window."
        ),
        next_step=(
            "Press a Joy-Con button during the probe; if it stays silent, treat "
            "this as a Windows Bluetooth/HID reconnect problem."
        ),
    )


def probe_first_device(
    seconds: float,
    *,
    init: bool = True,
    haptic_click: bool = False,
) -> ProbeStats:
    """Open the first visible Joy-Con and count reports for a short window."""
    hid = hid_reader._open_hid_module()  # noqa: SLF001 - diagnostics surface
    devices = collect_hid_devices()
    if not devices:
        return ProbeStats(devices_found=0)

    info = devices[0]
    stats = ProbeStats(devices_found=len(devices), open_attempted=True)
    device = hid.device()
    init_error = ""
    try:
        device.open_path(info["path"])
        device.set_nonblocking(True)
        if init:
            try:
                hid_reader.initialize_controller(device, enable_imu=True)
                stats = ProbeStats(
                    devices_found=stats.devices_found,
                    open_attempted=True,
                    open_ok=True,
                    init_ok=True,
                )
            except Exception as exc:  # noqa: BLE001 - diagnostic must continue
                init_error = str(exc)
                stats = ProbeStats(
                    devices_found=stats.devices_found,
                    open_attempted=True,
                    open_ok=True,
                    init_ok=False,
                    error=init_error,
                )
        else:
            stats = ProbeStats(
                devices_found=stats.devices_found,
                open_attempted=True,
                open_ok=True,
                init_ok=True,
            )

        haptic_attempted = False
        haptic_ok = False
        haptic_error = ""
        if haptic_click:
            haptic_attempted = True
            try:
                hid_reader.send_rumble_click(device)
                haptic_ok = True
            except Exception as exc:  # noqa: BLE001 - diagnostic must continue
                haptic_error = str(exc)

        product_id = int(info.get("product_id") or hid_reader.JOYCON_L_PRODUCT_ID)
        total_reports = 0
        standard_reports = 0
        parsed_reports = 0
        report_id_counts: dict[int, int] = {}
        sample_reports: list[str] = []
        deadline = time.perf_counter() + max(seconds, 0.1)
        while time.perf_counter() < deadline:
            try:
                raw = device.read(hid_reader.JOYCON_REPORT_LEN)
            except Exception as exc:  # noqa: BLE001 - diagnostic output
                return ProbeStats(
                    devices_found=len(devices),
                    open_attempted=True,
                    open_ok=True,
                    init_ok=stats.init_ok,
                    haptic_attempted=haptic_attempted,
                    haptic_ok=haptic_ok,
                    haptic_error=haptic_error,
                    total_reports=total_reports,
                    standard_reports=standard_reports,
                    parsed_reports=parsed_reports,
                    report_id_counts=tuple(sorted(report_id_counts.items())),
                    sample_reports=tuple(sample_reports),
                    error=str(exc),
                )
            if raw:
                total_reports += 1
                data = bytes(raw)
                if data:
                    report_id_counts[data[0]] = report_id_counts.get(data[0], 0) + 1
                    if len(sample_reports) < 6:
                        sample_reports.append(_format_report_sample(data))
                if data and data[0] == hid_reader.INPUT_REPORT_STANDARD:
                    standard_reports += 1
                if hid_reader.parse_report(data, product_id=product_id) is not None:
                    parsed_reports += 1
            else:
                time.sleep(0.01)

        return ProbeStats(
            devices_found=len(devices),
            open_attempted=True,
            open_ok=True,
            init_ok=stats.init_ok,
            haptic_attempted=haptic_attempted,
            haptic_ok=haptic_ok,
            haptic_error=haptic_error,
            total_reports=total_reports,
            standard_reports=standard_reports,
            parsed_reports=parsed_reports,
            report_id_counts=tuple(sorted(report_id_counts.items())),
            sample_reports=tuple(sample_reports),
            error=init_error,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic output
        return ProbeStats(
            devices_found=len(devices),
            open_attempted=True,
            open_ok=False,
            error=str(exc),
        )
    finally:
        try:
            device.close()
        except Exception:
            pass


def _windows_pnp_lines() -> list[str]:
    if os.name != "nt":
        return []
    command = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
Get-PnpDevice -PresentOnly |
  Where-Object {
    $_.InstanceId -match 'VID_057E|057E' -or
    $_.FriendlyName -match 'Joy-Con|Nintendo'
  } |
  Select-Object -First 30 Class,Status,FriendlyName,InstanceId |
  Format-Table -AutoSize |
  Out-String -Width 220
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic output
        return [f"Windows PnP query failed: {exc}"]
    output = _decode_process_output(result.stdout).strip()
    if not output:
        return ["No matching present Windows PnP devices found."]
    return output.splitlines()


def _decode_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return str(value)
    if not value:
        return ""
    encodings = ["utf-8-sig"]
    if b"\x00" in value[:80]:
        encodings.append("utf-16-le")
    encodings.extend(["mbcs", "latin-1"])
    for encoding in encodings:
        try:
            return value.decode(encoding)
        except Exception:
            pass
    return value.decode("utf-8", errors="replace")


def _print_section(title: str, lines: Iterable[str]) -> None:
    print(f"\n== {title} ==")
    for line in lines:
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds",
        type=float,
        default=6.0,
        help="How long to watch for input reports after opening the device.",
    )
    parser.add_argument(
        "--no-init",
        action="store_true",
        help="Skip the Joy-Con initialization handshake.",
    )
    parser.add_argument(
        "--skip-pnp",
        action="store_true",
        help="Skip the Windows PnP snapshot.",
    )
    parser.add_argument(
        "--haptic-click",
        action="store_true",
        help="Send one conservative Joy-Con click vibration during the probe.",
    )
    args = parser.parse_args(argv)

    print("JoyType Joy-Con connection diagnostic")
    print(f"Probe window: {args.seconds:.1f}s")

    try:
        devices = collect_hid_devices()
    except RuntimeError as exc:
        _print_section("HID enumeration", [str(exc)])
        return 2

    if devices:
        _print_section(
            "HID enumeration",
            [format_hid_device(info, index=i + 1) for i, info in enumerate(devices)],
        )
    else:
        _print_section("HID enumeration", ["No supported Joy-Con HID devices found."])

    stats = probe_first_device(
        args.seconds,
        init=not args.no_init,
        haptic_click=args.haptic_click,
    )
    pnp_lines = [] if args.skip_pnp else _windows_pnp_lines()
    diagnosis = classify_probe(
        stats,
        None if args.skip_pnp else analyze_pnp_lines(pnp_lines),
    )
    probe_lines = [
        f"devices_found={stats.devices_found}",
        f"open_attempted={stats.open_attempted}",
        f"open_ok={stats.open_ok}",
        f"init_ok={stats.init_ok}",
        f"haptic_attempted={stats.haptic_attempted}",
        f"haptic_ok={stats.haptic_ok}",
        f"haptic_error={stats.haptic_error or '-'}",
        f"total_reports={stats.total_reports}",
        f"standard_reports={stats.standard_reports}",
        f"parsed_reports={stats.parsed_reports}",
        f"report_ids={format_report_id_counts(stats.report_id_counts)}",
    ]
    if stats.sample_reports:
        probe_lines.append("sample_reports:")
        probe_lines.extend(f"  {sample}" for sample in stats.sample_reports)
    else:
        probe_lines.append("sample_reports=-")
    probe_lines.extend(
        [
            f"error={stats.error or '-'}",
            f"diagnosis={diagnosis.code}",
            diagnosis.message,
            f"Next: {diagnosis.next_step}",
        ]
    )
    _print_section(
        "Probe result",
        probe_lines,
    )

    if not args.skip_pnp:
        _print_section("Windows PnP snapshot", pnp_lines)

    return 0 if diagnosis.code == "streaming" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
