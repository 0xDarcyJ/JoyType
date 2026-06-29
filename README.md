# JoyType

Use a Bluetooth Joy-Con as a tiny Windows controller for dictation, coding, and fast desktop actions.

![JoyType configuration UI](assets/joytype-config-ui.png)

JoyType maps a Joy-Con L to mouse movement, keyboard shortcuts, window switching, and external dictation hotkeys. It is built for hands-on coding workflows where you want push-to-talk, send/enter, arrow keys, and window switching close to your thumb.

## Why JoyType

- Drive dictation tools with Joy-Con buttons: hold to talk, or tap to toggle.
- Move the pointer with the analog stick and click by pressing the stick.
- Send common coding/chat actions like Enter, arrows, Escape, and window switching.
- Edit mappings in a visual Joy-Con UI instead of memorizing raw config.
- Keep app-specific profiles local; the release default starts clean with one global profile.

## Download

Download the latest package for your platform from [GitHub Releases](https://github.com/0xDarcyJ/JoyType/releases/latest).

Requirements for the current public preview:

- Windows 10/11
- A paired Joy-Con L
- An external dictation app if you use the voice actions

## Quick Start

1. Download the Windows zip, macOS dmg, or Linux AppImage for your platform.
2. Install or extract the package.
3. Pair your Joy-Con in your operating system's Bluetooth settings.
4. Run JoyType.
5. Open the Config view to adjust button actions, mouse feel, and dictation hotkeys.

## Default Controls

| Control | Action |
|---|---|
| Stick | Mouse move |
| L3 / stick press | Left click |
| ZL hold | Push-to-talk hotkey |
| L tap | Toggle dictation hotkey |
| D-pad | Arrow keys |
| MINUS | Enter / send |
| LEFT_SL | Window forward |
| LEFT_SR | Window reverse |

JoyType does not include speech recognition. It sends global hotkeys to your existing dictation tool. The default voice hotkeys are `LeftShift + LeftCtrl + F8` for push-to-talk and `LeftCtrl + LeftAlt + F8` for toggle dictation.

## Configuration

Packaged builds keep the user-editable config beside the app:

```text
JoyType/
  JoyType.exe
  config.yaml
```

Most settings can be edited from the UI. `config.yaml` is still there for advanced edits and backup.

## Joy-Con Diagnosis

Run this when Windows says the Joy-Con is paired but JoyType cannot connect:

```cmd
python -m joytype.tools.connection_diag --seconds 8
```

Useful diagnoses:

- `streaming`: Windows HID is healthy and JoyType can read reports.
- `stale_bluetooth_pairing`: Windows remembers the Joy-Con, but the HID game controller node is missing. Remove the Joy-Con in Windows Bluetooth settings, then pair it again.
- `open_failed`: Windows exposes the HID device, but hidapi cannot open it.
- `no_hid_device`: Windows is not exposing a supported Joy-Con HID device.

Joy-Con connections can require removing and re-pairing after the controller has connected to another host. `keep_alive_s` only helps after JoyType is already connected.

## Run From Source

Requirements:

- Python 3.10+
- Windows 10/11

```cmd
pip install -r requirements.txt
python joytype_gui.py
```

## License

MIT
