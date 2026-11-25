"""
CynoType MCP Server (Model Context Protocol via FastMCP).

Gives AI hosts controlled browser inspection + human/Smart-IDE keystroke typing
for user-authorized web apps, web IDEs, VDI consoles, and QA workflows where
direct clipboard paste is disabled or unsupported.

Safety & governance built in:
- Explicit domain allowlist (defaults to localhost / 127.0.0.1; approve others via
  CYNOTYPE_MCP_ALLOWED_DOMAINS or browser_allow_domain).
- Blocks password fields by default.
- Global hardware kill switch (Esc / F8 on macOS, F8 on Windows/Linux).
- Local JSONL audit log at ~/.cynotype/mcp_audit.jsonl.
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from mcp.server.mcpserver import MCPServer as _MCPServer
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
except ImportError:
    from mcp.server.fastmcp import FastMCP as _MCPServer

    class _ToolError(RuntimeError):
        pass

from .browser_controller import BrowserController, BrowserPolicy
from .engine import (
    IS_MAC,
    MacHotkeyListener,
    TypingEngine,
    analyze_indentation,
)


AUDIT_LOG_PATH = Path.home() / ".cynotype" / "mcp_audit.jsonl"


def _write_audit(entry: Dict[str, Any]) -> None:
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **entry,
        }
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


@dataclass
class TypingJob:
    job_id: str
    backend: str  # "cdp" | "os"
    mode: str  # "code" | "text"
    target: str
    total_chars: int
    typed_chars: int = 0
    progress_pct: float = 0.0
    status: str = "running"  # running | finished | stopped | error
    message: str = "Starting..."
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CynoTypeMCPState:
    """Holds browser controller, OS typing engine, active jobs, and global kill switch."""

    def __init__(self):
        self.policy = BrowserPolicy()
        self.browser = BrowserController(self.policy)
        self.jobs: Dict[str, TypingJob] = {}
        self.active_job_id: Optional[str] = None

        self._cdp_stop_event: Optional[asyncio.Event] = None
        self._cdp_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._os_engine: Optional[TypingEngine] = None
        self._os_done_event: Optional[asyncio.Event] = None
        self._hotkey_listener = None

    def ensure_hotkey_listener(self) -> None:
        if self._hotkey_listener is not None:
            return
        if IS_MAC:
            listener = MacHotkeyListener(self._on_global_stop_hotkey)
            listener.start()
            self._hotkey_listener = listener
        else:
            try:
                from pynput import keyboard
                from pynput.keyboard import Key

                listener = keyboard.Listener(
                    on_press=lambda k: self._on_global_stop_hotkey("F8")
                    if k == Key.f8
                    else None
                )
                listener.daemon = True
                listener.start()
                self._hotkey_listener = listener
            except Exception:
                pass

    def _on_global_stop_hotkey(self, key_name: str) -> None:
        if self._os_engine is not None and self._os_engine.is_running:
            self._os_engine.handle_emergency_hotkey(key_name)
        if self._cdp_stop_event is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._cdp_stop_event.set)
            if self.active_job_id and self.active_job_id in self.jobs:
                job = self.jobs[self.active_job_id]
                if job.status == "running":
                    job.message = f"Stop requested via hardware hotkey ({key_name})"

    def has_running_job(self) -> bool:
        if self.active_job_id and self.active_job_id in self.jobs:
            return self.jobs[self.active_job_id].status == "running"
        return False

    def stop_active(self, reason: str = "Stopped by MCP stop_typing()") -> Dict[str, Any]:
        stopped_any = False
        if self._os_engine is not None and self._os_engine.is_running:
            self._os_engine.stop(reason)
            stopped_any = True
        if self._cdp_stop_event is not None:
            self._cdp_stop_event.set()
            stopped_any = True
        if self.active_job_id and self.active_job_id in self.jobs:
            job = self.jobs[self.active_job_id]
            if job.status == "running":
                job.status = "stopped"
                job.message = reason
                job.finished_at = time.time()
        return {"stopped": stopped_any, "active_job_id": self.active_job_id}

    def get_os_engine(self, job: TypingJob, loop: asyncio.AbstractEventLoop) -> TypingEngine:
        def on_status(msg: str, _active: bool):
            job.message = msg

        def on_progress(pct: float, msg: str):
            job.progress_pct = round(pct, 1)
            job.typed_chars = int(round((pct / 100.0) * job.total_chars))
            job.message = msg

        def on_finished(total_chars: int, _am: bool):
            job.typed_chars = total_chars
            job.progress_pct = 100.0
            job.status = "finished"
            job.message = f"Finished typing {total_chars:,} characters."
            job.finished_at = time.time()
            _write_audit(
                {
                    "job_id": job.job_id,
                    "backend": "os",
                    "mode": job.mode,
                    "target": job.target,
                    "chars": total_chars,
                    "status": "finished",
                }
            )
            if self._os_done_event is not None:
                loop.call_soon_threadsafe(self._os_done_event.set)

        def on_stopped(reason: str, _am: bool):
            job.status = "stopped"
            job.message = f"Stopped ({reason})"
            job.finished_at = time.time()
            _write_audit(
                {
                    "job_id": job.job_id,
                    "backend": "os",
                    "mode": job.mode,
                    "target": job.target,
                    "chars": job.typed_chars,
                    "status": "stopped",
                    "reason": reason,
                }
            )
            if self._os_done_event is not None:
                loop.call_soon_threadsafe(self._os_done_event.set)

        engine = TypingEngine(
            on_status=on_status,
            on_progress=on_progress,
            on_finished=on_finished,
            on_stopped=on_stopped,
            on_minimize=lambda: None,
        )
        self._os_engine = engine
        return engine


# ==============================================================================
# FastMCP Server Definition
# ==============================================================================
mcp = _MCPServer(
    "CynoType",
    instructions=(
        "CynoType MCP controls a user-authorized browser session (via CDP/Playwright) "
        "and types text or code using realistic keystrokes and Smart IDE indentation. "
        "Always inspect tabs/inputs first, ensure the target domain is allowlisted, "
        "and respect the user's Esc/F8 emergency stop."
    ),
)

_state = CynoTypeMCPState()


@mcp.tool()
async def browser_get_policy() -> Dict[str, Any]:
    """Return the active domain allowlist, password-field guardrail, and character cap."""
    return _state.policy.to_dict()


@mcp.tool()
async def browser_allow_domain(domain: str) -> Dict[str, Any]:
    """
    Explicitly approve a domain or host pattern (e.g. 'codesandbox.io' or '*.internal.corp')
    for browser inspection and typing in this MCP session.
    """
    try:
        added = _state.policy.allow_domain(domain)
    except Exception as err:
        raise _ToolError(str(err)) from err
    _write_audit({"event": "allow_domain", "domain": added})
    return {"allowed": added, "policy": _state.policy.to_dict()}


@mcp.tool()
async def browser_connect(
    cdp_url: str = "http://127.0.0.1:9222",
    launch_if_needed: bool = True,
    use_system_chrome: bool = True,
) -> Dict[str, Any]:
    """
    Connect to a user-launched Chrome debugging session (recommended: launch Chrome with
    '--remote-debugging-port=9222'), or open a fresh visible browser window if launch_if_needed=True.
    """
    _state.ensure_hotkey_listener()
    try:
        return await _state.browser.connect(
            cdp_url=cdp_url,
            launch_if_needed=launch_if_needed,
            use_system_chrome=use_system_chrome,
            headless=False,
        )
    except Exception as err:
        raise _ToolError(str(err)) from err


@mcp.tool()
async def browser_disconnect() -> Dict[str, Any]:
    """Disconnect from the browser session (closes browser only if CynoType launched it)."""
    _state.stop_active("Browser disconnected")
    await _state.browser.disconnect()
    return {"disconnected": True}


@mcp.tool()
async def browser_list_tabs() -> Dict[str, Any]:
    """List open tabs with tab_id, title, url, and whether each tab's domain is allowlisted."""
    try:
        tabs = await _state.browser.list_tabs()
        return {"tabs": tabs}
    except Exception as err:
        raise _ToolError(str(err)) from err


@mcp.tool()
async def browser_navigate(url: str, tab_id: int = 0) -> Dict[str, Any]:
    """Navigate an open browser tab to an allowlisted URL."""
    try:
        return await _state.browser.navigate(url=url, tab_id=tab_id)
    except Exception as err:
        raise _ToolError(str(err)) from err


@mcp.tool()
async def browser_inspect_inputs(tab_id: int = 0) -> Dict[str, Any]:
    """
    Inspect editable inputs, textareas, contenteditable blocks, Monaco, and CodeMirror
    editors in an allowlisted tab so you can choose a target selector or index.
    """
    try:
        return await _state.browser.inspect_inputs(tab_id=tab_id)
    except Exception as err:
        raise _ToolError(str(err)) from err


@mcp.tool()
async def browser_focus_input(
    tab_id: int = 0,
    selector: Optional[str] = None,
    index: Optional[int] = None,
) -> Dict[str, Any]:
    """Focus a specific editable element in an allowlisted browser tab by CSS selector or inspect index."""
    try:
        return await _state.browser.focus_input(
            tab_id=tab_id, selector=selector, index=index
        )
    except Exception as err:
        raise _ToolError(str(err)) from err


@mcp.tool()
async def browser_read_value(
    tab_id: int = 0,
    selector: Optional[str] = None,
) -> Dict[str, Any]:
    """Read text from a non-password input/textarea/editor in an allowlisted tab to verify typed output."""
    try:
        return await _state.browser.read_input_value(tab_id=tab_id, selector=selector)
    except Exception as err:
        raise _ToolError(str(err)) from err


@mcp.tool()
async def analyze_code_indent(text: str) -> Dict[str, Any]:
    """Analyze a code snippet and detect its indentation width (2 vs 4 spaces or Tabs)."""
    spaces, use_tab, msg = analyze_indentation(text)
    return {
        "spaces_per_tab": spaces,
        "use_tab_key": use_tab,
        "summary": msg,
    }


@mcp.tool()
async def type_in_browser(
    text: str,
    tab_id: int = 0,
    selector: Optional[str] = None,
    index: Optional[int] = None,
    mode: str = "code",
    speed_ms: float = 45.0,
    humanize: bool = True,
    use_tab_key: bool = True,
    spaces_per_tab: Optional[int] = None,
    dismiss_autocomplete: bool = True,
    wait: bool = False,
) -> Dict[str, Any]:
    """
    Type text or code directly into an allowlisted browser tab using trusted CDP keyboard
    events (works even when clipboard paste is disabled).

    Args:
        text: The exact code or text to type.
        tab_id: Tab index from browser_list_tabs() (default 0).
        selector: Optional CSS selector to focus before typing.
        index: Optional input index from browser_inspect_inputs() to focus before typing.
        mode: 'code' (Smart IDE indentation tracking) or 'text' (verbatim).
        speed_ms: Base delay per keystroke in milliseconds (10–250).
        humanize: Add natural human rhythm and punctuation pauses.
        use_tab_key: Use Tab key for indentation in code mode.
        spaces_per_tab: Spaces per indent level (auto-detected if omitted in code mode).
        dismiss_autocomplete: Send Esc before Enter in code mode to close popup suggestions.
        wait: If True, waits until typing finishes before returning; if False, returns job_id immediately.
    """
    if _state.has_running_job():
        return {
            "error": "Another typing job is already running. Call stop_typing() or check typing_status().",
            "active_job_id": _state.active_job_id,
        }

    code_mode = mode.strip().lower() != "text"
    if spaces_per_tab is None and code_mode:
        detected_sp, detected_tab, _ = analyze_indentation(text)
        spaces_per_tab = detected_sp
        if detected_tab is not None:
            use_tab_key = detected_tab
    spaces_per_tab = spaces_per_tab or 4

    # Validate connection & allowlist upfront before spawning job
    try:
        tabs = await _state.browser.list_tabs()
        if tab_id < 0 or tab_id >= len(tabs):
            raise IndexError(f"Invalid tab_id {tab_id}. Open tabs: 0..{len(tabs) - 1}.")
        target_url = tabs[tab_id]["url"]
        _state.policy.assert_url_allowed(target_url)
    except Exception as err:
        raise _ToolError(str(err)) from err

    _state.ensure_hotkey_listener()
    loop = asyncio.get_running_loop()
    _state._loop = loop
    stop_event = asyncio.Event()
    _state._cdp_stop_event = stop_event

    job = TypingJob(
        job_id=uuid.uuid4().hex[:10],
        backend="cdp",
        mode="code" if code_mode else "text",
        target=target_url,
        total_chars=len(text.replace("\r\n", "\n").replace("\r", "\n")),
    )
    _state.jobs[job.job_id] = job
    _state.active_job_id = job.job_id

    def on_progress(typed: int, total: int, pct: float, msg: str):
        job.typed_chars = typed
        job.progress_pct = round(pct, 1)
        job.message = msg

    async def _runner():
        try:
            res = await _state.browser.type_text(
                text=text,
                tab_id=tab_id,
                selector=selector,
                index=index,
                code_mode=code_mode,
                speed_ms=speed_ms,
                humanize=humanize,
                use_tab_key=use_tab_key,
                spaces_per_tab=spaces_per_tab,
                dismiss_autocomplete=dismiss_autocomplete,
                stop_event=stop_event,
                on_progress=on_progress,
            )
            job.status = res.get("status", "finished")
            job.typed_chars = res.get("typed_chars", job.typed_chars)
            if job.status == "finished":
                job.progress_pct = 100.0
                job.message = f"Finished typing {job.typed_chars:,} characters."
            else:
                job.message = f"Stopped at {job.typed_chars:,} / {job.total_chars:,} characters."
        except Exception as err:
            job.status = "error"
            job.message = str(err)
        finally:
            job.finished_at = time.time()
            _write_audit(
                {
                    "job_id": job.job_id,
                    "backend": "cdp",
                    "mode": job.mode,
                    "target": target_url,
                    "chars": job.typed_chars,
                    "status": job.status,
                    "message": job.message,
                }
            )

    task = asyncio.create_task(_runner())
    _state._cdp_task = task

    if wait:
        await task
        return job.to_dict()

    return {
        "job_id": job.job_id,
        "status": job.status,
        "target": target_url,
        "total_chars": job.total_chars,
        "message": "Typing started in browser. Press Esc/F8 or call stop_typing() anytime to halt.",
    }


@mcp.tool()
async def type_os_window(
    text: str,
    mode: str = "code",
    speed_ms: float = 45.0,
    humanize: bool = True,
    start_delay: int = 3,
    use_tab_key: bool = True,
    spaces_per_tab: Optional[int] = None,
    dismiss_autocomplete: bool = True,
    wait: bool = False,
) -> Dict[str, Any]:
    """
    Type text or code into the currently focused desktop/browser window using OS-level
    keystrokes (useful for canvas/WebGL editors, remote desktops/VDI, or native IDEs).
    Includes a countdown (default 3s) so the user can focus the intended window, and
    respects the global Esc/F8 emergency stop.
    """
    if _state.has_running_job():
        return {
            "error": "Another typing job is already running. Call stop_typing() or check typing_status().",
            "active_job_id": _state.active_job_id,
        }
    if not text:
        raise ValueError("Text cannot be empty.")
    if len(text) > _state.policy.max_chars_per_job:
        raise ValueError(
            f"Text length ({len(text):,}) exceeds max_chars_per_job ({_state.policy.max_chars_per_job:,})."
        )

    code_mode = mode.strip().lower() != "text"
    if spaces_per_tab is None and code_mode:
        detected_sp, detected_tab, _ = analyze_indentation(text)
        spaces_per_tab = detected_sp
        if detected_tab is not None:
            use_tab_key = detected_tab
    spaces_per_tab = spaces_per_tab or 4

    _state.ensure_hotkey_listener()
    loop = asyncio.get_running_loop()
    _state._loop = loop
    _state._os_done_event = asyncio.Event()

    job = TypingJob(
        job_id=uuid.uuid4().hex[:10],
        backend="os",
        mode="code" if code_mode else "text",
        target="active-os-window",
        total_chars=len(text.replace("\r\n", "\n").replace("\r", "\n")),
    )
    _state.jobs[job.job_id] = job
    _state.active_job_id = job.job_id

    engine = _state.get_os_engine(job, loop)
    engine.start(
        text=text,
        base_delay_ms=speed_ms,
        randomize=humanize,
        start_delay=start_delay,
        auto_minimize=False,
        code_mode=code_mode,
        dismiss_autocomplete=dismiss_autocomplete,
        use_tab_key=use_tab_key,
        spaces_per_tab=spaces_per_tab,
    )

    if wait:
        await _state._os_done_event.wait()
        return job.to_dict()

    return {
        "job_id": job.job_id,
        "status": job.status,
        "start_delay": start_delay,
        "total_chars": job.total_chars,
        "message": f"Countdown ({start_delay}s) started for OS window typing. Press Esc/F8 or call stop_typing() to halt.",
    }


@mcp.tool()
async def typing_status(job_id: Optional[str] = None) -> Dict[str, Any]:
    """Get progress and completion status for a specific job_id (or the most recent job)."""
    target_id = job_id or _state.active_job_id
    if not target_id or target_id not in _state.jobs:
        return {"status": "idle", "message": "No typing jobs recorded yet."}
    return _state.jobs[target_id].to_dict()


@mcp.tool()
async def stop_typing() -> Dict[str, Any]:
    """Immediately stop any active browser or OS typing job."""
    return _state.stop_active("Stopped via MCP stop_typing()")


def run_server() -> None:
    mcp.run()


if __name__ == "__main__":
    run_server()
