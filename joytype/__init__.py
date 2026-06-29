"""Core runtime package for JoyType.

JoyType uses this package for controller HID input, foreground-window profile
matching, Win32 keyboard/mouse output, config parsing/writing, and daemon
lifecycle management.

Public surface:
    joytype.config - configuration loader
    joytype.state  - normalized controller state + button/stick enums
    joytype.hid_reader - controller HID report parser
    joytype.windows_util - Win32 SendInput / foreground-window helpers
    joytype.actions - executable action types
    joytype.binder - the core router (foreground window -> profile -> action)
"""

__version__ = "0.0.2"
__all__ = [
    "__version__",
]
