"""CynoType core package: typing engine, browser controller, and MCP server."""

from .engine import (
    APP_NAME,
    APP_VERSION,
    IS_MAC,
    IS_WIN,
    KeystrokeDriver,
    MacHotkeyListener,
    OSKeystrokeDriver,
    TypingEngine,
    analyze_indentation,
    mac_check_permissions,
)

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "IS_MAC",
    "IS_WIN",
    "KeystrokeDriver",
    "MacHotkeyListener",
    "OSKeystrokeDriver",
    "TypingEngine",
    "analyze_indentation",
    "mac_check_permissions",
]
