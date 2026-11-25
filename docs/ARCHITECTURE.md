# CynoType Architecture & Build Guide

This document describes the internal architecture, UI design system, cross-platform bridges, and packaging pipeline for contributors and maintainers.

---

## 1. Module Architecture

| Module | Responsibility |
|---|---|
| `cynotype/engine.py` | Platform-agnostic `TypingEngine`, Smart IDE indentation tracker (`_measure_indent_spaces`, `_is_block_opener`, `_type_indent_diff`), `analyze_indentation`, `KeystrokeDriver` protocol, `OSKeystrokeDriver` (`pynput` + clipboard fallback), `mac_check_permissions`, and `MacHotkeyListener` (Quartz `CGEventTap`). |
| `cynotype/browser_controller.py` | Async Playwright / CDP controller (`BrowserController`) and security policy (`BrowserPolicy`) for domain allowlisting, input inspection, focus, read-back, and CDP keystroke dispatch. |
| `cynotype/mcp_server.py` | MCP server built with `mcp.server.mcpserver.MCPServer` (v2) with `FastMCP` (v1) compatibility shim, async job registry, hardware kill-switch integration, and JSONL audit logging. |
| `auto_typer.py` | Desktop UI shell hosting `assets/ui.html` natively on macOS (`AppKit` + `NSVisualEffectView` + `WKWebView`) and Windows (`pywebview` / Edge WebView2), with a minimal Tkinter fallback if WebView2/pywebview is absent. |
| `assets/ui.html` | Single self-contained Apple Design Science interface shared across macOS and Windows. |
| `run_mcp.py` | Stdio entry point for MCP clients. |

---

## 2. UI Design System (`assets/ui.html`)

The interface follows a restrained Apple Design Science specification:
- **Typography**: System sans-serif (`-apple-system`, SF Pro / Segoe UI Variable) in 3 weights (`400`, `500`, `600`) plus monospace (`SF Mono` / `Cascadia Code` / `Consolas`) for code and tabular numeric counters.
- **4-Color Palette**:
  1. Neutral canvas (`transparent` over macOS `NSVisualEffectMaterialUnderWindowBackground`, `#F5F5F7` on Windows)
  2. Frosted glass surface (`rgba(255, 255, 255, 0.82)` with `backdrop-filter: blur(24px) saturate(180%)` + pure `#FFFFFF` editor well)
  3. Neutral ink (`#1D1D1F` with opacity scale for secondary text and hairlines)
  4. Restrained primary accent (`#0071E3` used strictly for primary action, active switches, slider fill, focus ring, and progress hairline)
- **Layout & Motion**: Centered `max-width: 780px` column on an `8px` spacing grid, `12px`/`8px` border radii, and damped ease-out motion (`cubic-bezier(0.22, 1, 0.36, 1)`).

### Cross-Platform JS ↔ Python Bridge
`assets/ui.html` detects the host bridge automatically:
- **macOS (`run_macos_app`)**: Loads Apple's `/System/Library/Frameworks/WebKit.framework` via `objc.loadBundle` (zero extra pip packages beyond PyObjC). JS calls `window.webkit.messageHandlers.bridge.postMessage(...)`; Python calls `webview.evaluateJavaScript_completionHandler_(...)` via `AppHelper.callAfter`.
- **Windows (`run_webview_app`)**: Uses `pywebview` with `gui="edgechromium"` (Microsoft Edge WebView2). JS calls `window.pywebview.api.dispatch(...)`; Python calls `window.evaluate_js(...)`.

---

## 3. Building Standalone Installers

### macOS (`dist/CynoType.app` & `dist/CynoType-Installer.dmg`)
```bash
./build_mac.sh
```
What `build_mac.sh` does:
1. Generates the Retina (`1320×840` @2x) DMG background (`assets/dmg_background.png`) via CoreGraphics / CoreText.
2. Packages `auto_typer.py`, `cynotype/engine.py`, and `assets/` into `dist/CynoType.app` with PyInstaller (`--windowed`, `--icon assets/CynoType.icns`), excluding `mcp` and `playwright` to keep the bundle lean (~17 MB compressed).
3. Injects bundle metadata and `NSAccessibilityUsageDescription` / `NSInputMonitoringUsageDescription` into `Info.plist`.
4. Ad-hoc signs the bundle (`codesign --force --deep --sign -`).
5. Builds a compressed UDZO read-only disk image `dist/CynoType-Installer.dmg` with custom Finder icon layout and `/Applications` drag-and-drop shortcut.

### Windows (`dist\CynoType.exe`)
```bat
build_win.bat
```
Packages `auto_typer.py`, `cynotype/engine.py`, `pywebview`, `pynput`, `pyperclip`, `assets\CynoType.ico`, and `assets\` into a single standalone `dist\CynoType.exe`.

### Automated CI/CD Builds (GitHub Actions)
[`.github/workflows/build-release.yml`](../.github/workflows/build-release.yml) runs on every push to `main`, manual workflow dispatch, and `v*` version tags:
- **`build-windows` (`windows-latest`)**: Runs `build_win.bat` and uploads **`CynoType.exe`**.
- **`build-macos` (`macos-latest`)**: Runs `./build_mac.sh` and uploads **`CynoType-Installer.dmg`**.
- **`release` (on `v*` tags)**: Automatically creates a GitHub Release attaching both installers.

To cut a new release:
```bash
git tag v2.0.0
git push origin v2.0.0
```
