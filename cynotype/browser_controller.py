"""
Playwright / Chrome DevTools Protocol (CDP) browser controller for CynoType MCP.

Designed for user-authorized browser automation, web IDEs, remote VDI consoles,
QA workflows, and web apps where direct clipboard paste is disabled or unreliable.
Enforces an explicit domain allowlist and refuses password inputs by default.
"""

import asyncio
import fnmatch
import os
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

from .engine import TypingEngine


DEFAULT_ALLOWED_DOMAINS: Set[str] = {
    "localhost",
    "127.0.0.1",
    "::1",
    "*.localhost",
}

MAX_CHARS_PER_JOB = int(os.environ.get("CYNOTYPE_MCP_MAX_CHARS", "50000"))

_INSPECT_INPUTS_JS = r"""
() => {
  const candidates = Array.from(document.querySelectorAll(
    'textarea, input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"]):not([type="file"]), [contenteditable="true"], [role="textbox"], .monaco-editor textarea.inputarea, .cm-content'
  ));
  const seen = new Set();
  const results = [];

  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.getAttribute('data-testid')) {
      return `[data-testid="${el.getAttribute('data-testid').replace(/"/g, '\\"')}"]`;
    }
    if (el.name) {
      return `${el.tagName.toLowerCase()}[name="${el.name.replace(/"/g, '\\"')}"]`;
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 5) {
      let sel = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift('#' + CSS.escape(cur.id));
        break;
      }
      const parent = cur.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (siblings.length > 1) {
          sel += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
        }
      }
      parts.unshift(sel);
      cur = parent;
    }
    return parts.join(' > ');
  }

  candidates.forEach((el) => {
    if (seen.has(el)) return;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    const isMonacoTextarea = el.classList.contains('inputarea') && !!el.closest('.monaco-editor');
    const visible = isMonacoTextarea || (rect.width > 0 && rect.height > 0);
    if (!visible) return;

    const tag = el.tagName.toLowerCase();
    const inputType = (el.getAttribute('type') || '').toLowerCase();
    const isPassword = tag === 'input' && inputType === 'password';
    const editorKind = isMonacoTextarea
      ? 'monaco'
      : el.classList.contains('cm-content')
      ? 'codemirror'
      : el.isContentEditable
      ? 'contenteditable'
      : tag;

    const label =
      el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') ||
      (el.labels && el.labels[0] ? el.labels[0].innerText.trim() : '') ||
      el.getAttribute('name') ||
      el.id ||
      '';

    results.push({
      index: results.length,
      tag,
      editor_kind: editorKind,
      type: inputType || null,
      is_password: isPassword,
      focused: document.activeElement === el,
      label: label.slice(0, 120),
      selector: cssPath(el),
      paste_blocked_attr: Boolean(el.getAttribute('onpaste')),
    });
  });

  return {
    url: location.href,
    title: document.title,
    active_tag: document.activeElement ? document.activeElement.tagName.toLowerCase() : null,
    inputs: results,
  };
}
"""


@dataclass
class BrowserPolicy:
    """Explicit domain allowlist & input safety policy."""

    allowed_domains: Set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_DOMAINS))
    allow_file_urls: bool = False
    block_password_fields: bool = True
    max_chars_per_job: int = MAX_CHARS_PER_JOB

    def __post_init__(self):
        env_domains = os.environ.get("CYNOTYPE_MCP_ALLOWED_DOMAINS", "").strip()
        if env_domains:
            for item in env_domains.split(","):
                host = item.strip().lower()
                if host:
                    self.allowed_domains.add(host)
        if os.environ.get("CYNOTYPE_MCP_ALLOW_FILE_URLS", "").lower() in ("1", "true", "yes"):
            self.allow_file_urls = True

    def allow_domain(self, domain: str) -> str:
        cleaned = domain.strip().lower()
        if "://" in cleaned:
            cleaned = (urlparse(cleaned).hostname or "").lower()
        cleaned = cleaned.split("/")[0].split(":")[0]
        if not cleaned:
            raise ValueError("Domain cannot be empty.")
        self.allowed_domains.add(cleaned)
        return cleaned

    def is_url_allowed(self, url: str) -> bool:
        if not url or url in ("about:blank",):
            return True
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme == "file":
            return self.allow_file_urls
        if scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        for pattern in self.allowed_domains:
            if pattern == "*" or host == pattern or fnmatch.fnmatch(host, pattern):
                return True
        return False

    def assert_url_allowed(self, url: str) -> None:
        if not self.is_url_allowed(url):
            host = urlparse(url).hostname or url
            raise PermissionError(
                f"Domain '{host}' is not in CynoType MCP's allowlist. "
                f"Approve it first via browser_allow_domain('{host}') or set "
                f"CYNOTYPE_MCP_ALLOWED_DOMAINS."
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_domains": sorted(self.allowed_domains),
            "allow_file_urls": self.allow_file_urls,
            "block_password_fields": self.block_password_fields,
            "max_chars_per_job": self.max_chars_per_job,
        }


class BrowserController:
    """Async Playwright/CDP controller with domain guardrails and human/IDE keystroke dispatch."""

    def __init__(self, policy: Optional[BrowserPolicy] = None):
        self.policy = policy or BrowserPolicy()
        self._pw = None
        self._browser = None
        self._owns_browser = False
        self._mode: Optional[str] = None  # "cdp" | "launched"

    @property
    def is_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def connect(
        self,
        cdp_url: str = "http://127.0.0.1:9222",
        launch_if_needed: bool = True,
        use_system_chrome: bool = True,
        headless: bool = False,
    ) -> Dict[str, Any]:
        """
        Attach to an existing Chrome/Chromium DevTools endpoint (recommended),
        or launch a visible user-controlled Chromium window if requested.
        """
        if self.is_connected:
            return {
                "connected": True,
                "connection_mode": self._mode,
                "tabs": await self.list_tabs(),
            }

        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()

        # 1. Try attaching to user's Chrome CDP session first
        if cdp_url:
            try:
                self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
                self._owns_browser = False
                self._mode = "cdp"
                return {
                    "connected": True,
                    "connection_mode": "cdp",
                    "cdp_url": cdp_url,
                    "tabs": await self.list_tabs(),
                }
            except Exception as cdp_err:
                if not launch_if_needed:
                    await self.disconnect()
                    raise RuntimeError(
                        f"Could not attach to Chrome CDP at {cdp_url}: {cdp_err}. "
                        f"Launch Chrome with '--remote-debugging-port=9222' or set launch_if_needed=True."
                    ) from cdp_err

        # 2. Launch a visible browser window controlled by the user
        launch_errors = []
        if use_system_chrome:
            try:
                self._browser = await self._pw.chromium.launch(
                    channel="chrome", headless=headless
                )
            except Exception as err:
                launch_errors.append(f"system chrome: {err}")

        if self._browser is None:
            try:
                self._browser = await self._pw.chromium.launch(headless=headless)
            except Exception as err:
                launch_errors.append(f"playwright chromium: {err}")
                await self.disconnect()
                raise RuntimeError(
                    "Could not launch Chromium ("
                    + "; ".join(launch_errors)
                    + "). Install Playwright browsers with '.venv/bin/python -m playwright install chromium' "
                    "or launch Chrome with '--remote-debugging-port=9222'."
                ) from err

        self._owns_browser = True
        self._mode = "launched"
        ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        if not ctx.pages:
            await ctx.new_page()

        return {
            "connected": True,
            "connection_mode": "launched",
            "tabs": await self.list_tabs(),
        }

    async def disconnect(self) -> None:
        if self._browser is not None:
            try:
                if self._owns_browser:
                    await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._owns_browser = False
        self._mode = None

    def _all_pages(self) -> List[Any]:
        if not self.is_connected:
            raise RuntimeError(
                "Browser is not connected. Call browser_connect() first."
            )
        pages = []
        for ctx in self._browser.contexts:
            pages.extend(ctx.pages)
        return pages

    def _get_page(self, tab_id: int = 0) -> Any:
        pages = self._all_pages()
        if not pages:
            raise RuntimeError("No open tabs found in browser.")
        if tab_id < 0 or tab_id >= len(pages):
            raise IndexError(f"Invalid tab_id {tab_id}. Open tabs: 0..{len(pages) - 1}.")
        return pages[tab_id]

    async def list_tabs(self) -> List[Dict[str, Any]]:
        pages = self._all_pages()
        out = []
        for idx, page in enumerate(pages):
            url = page.url
            try:
                title = await page.title()
            except Exception:
                title = ""
            out.append(
                {
                    "tab_id": idx,
                    "title": title,
                    "url": url,
                    "allowed": self.policy.is_url_allowed(url),
                }
            )
        return out

    async def navigate(self, url: str, tab_id: int = 0) -> Dict[str, Any]:
        self.policy.assert_url_allowed(url)
        page = self._get_page(tab_id)
        await page.goto(url, wait_until="domcontentloaded")
        return {
            "tab_id": tab_id,
            "url": page.url,
            "title": await page.title(),
        }

    async def inspect_inputs(self, tab_id: int = 0) -> Dict[str, Any]:
        page = self._get_page(tab_id)
        self.policy.assert_url_allowed(page.url)
        data = await page.evaluate(_INSPECT_INPUTS_JS)
        data["tab_id"] = tab_id
        return data

    async def focus_input(
        self,
        tab_id: int = 0,
        selector: Optional[str] = None,
        index: Optional[int] = None,
    ) -> Dict[str, Any]:
        page = self._get_page(tab_id)
        self.policy.assert_url_allowed(page.url)
        await page.bring_to_front()

        target_selector = selector
        if target_selector is None and index is not None:
            info = await self.inspect_inputs(tab_id)
            inputs = info.get("inputs", [])
            if index < 0 or index >= len(inputs):
                raise IndexError(
                    f"Input index {index} out of range (found {len(inputs)} editable elements)."
                )
            chosen = inputs[index]
            if self.policy.block_password_fields and chosen.get("is_password"):
                raise PermissionError("Refusing to focus or type into a password field.")
            target_selector = chosen["selector"]

        if target_selector:
            loc = page.locator(target_selector).first
            is_pwd = await loc.evaluate(
                "el => el.tagName.toLowerCase() === 'input' && (el.type || '').toLowerCase() === 'password'"
            )
            if self.policy.block_password_fields and is_pwd:
                raise PermissionError("Refusing to focus or type into a password field.")

            # Monaco editor uses a hidden textarea inside .monaco-editor; click container when appropriate
            is_monaco = await loc.evaluate(
                "el => el.classList.contains('inputarea') && !!el.closest('.monaco-editor')"
            )
            if is_monaco:
                await loc.evaluate("el => el.focus()")
            else:
                await loc.scroll_into_view_if_needed()
                await loc.click()

        return {
            "tab_id": tab_id,
            "url": page.url,
            "focused_selector": target_selector or "(active element)",
        }

    async def read_input_value(
        self,
        tab_id: int = 0,
        selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read current text from a non-password input/textarea/contenteditable to verify typed output."""
        page = self._get_page(tab_id)
        self.policy.assert_url_allowed(page.url)
        if selector:
            loc = page.locator(selector).first
            is_pwd = await loc.evaluate(
                "el => el.tagName.toLowerCase() === 'input' && (el.type || '').toLowerCase() === 'password'"
            )
            if self.policy.block_password_fields and is_pwd:
                raise PermissionError("Refusing to read a password field.")
            value = await loc.evaluate(
                "el => (typeof el.value === 'string' ? el.value : (el.innerText || el.textContent || ''))"
            )
        else:
            info = await page.evaluate(
                "() => { const el = document.activeElement; if (!el) return {pwd:false, val:''}; const pwd = el.tagName.toLowerCase()==='input' && (el.type||'').toLowerCase()==='password'; return {pwd, val: typeof el.value === 'string' ? el.value : (el.innerText || el.textContent || '')}; }"
            )
            if self.policy.block_password_fields and info.get("pwd"):
                raise PermissionError("Refusing to read a password field.")
            value = info.get("val", "")
        return {
            "tab_id": tab_id,
            "selector": selector or "(active element)",
            "length": len(value),
            "value": value,
        }

    async def type_text(
        self,
        text: str,
        tab_id: int = 0,
        selector: Optional[str] = None,
        index: Optional[int] = None,
        code_mode: bool = True,
        speed_ms: float = 45.0,
        humanize: bool = True,
        use_tab_key: bool = True,
        spaces_per_tab: int = 4,
        dismiss_autocomplete: bool = True,
        stop_event: Optional[asyncio.Event] = None,
        on_progress: Optional[Callable[[int, int, float, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Dispatch trusted CDP keyboard events into the browser tab using CynoType's
        Smart IDE indentation tracking and natural human rhythm.
        Works even when pages block clipboard paste events.
        """
        if not text:
            raise ValueError("Text cannot be empty.")
        if len(text) > self.policy.max_chars_per_job:
            raise ValueError(
                f"Text length ({len(text):,}) exceeds max_chars_per_job ({self.policy.max_chars_per_job:,})."
            )

        page = self._get_page(tab_id)
        self.policy.assert_url_allowed(page.url)

        if selector is not None or index is not None:
            await self.focus_input(tab_id=tab_id, selector=selector, index=index)
        else:
            # Verify active element isn't a password field
            active_is_pwd = await page.evaluate(
                "() => !!document.activeElement && document.activeElement.tagName.toLowerCase() === 'input' && (document.activeElement.type || '').toLowerCase() === 'password'"
            )
            if self.policy.block_password_fields and active_is_pwd:
                raise PermissionError("Refusing to type into a password field.")

        base_delay = max(0.005, float(speed_ms) / 1000.0)
        spaces_per_tab = max(1, min(16, int(spaces_per_tab)))
        clean_text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = clean_text.split("\n")
        total_lines = len(lines)
        total_chars = max(1, len(clean_text))
        typed_count = 0
        update_interval = 1 if total_chars < 200 else max(1, total_chars // 100)
        kb = page.keyboard

        async def wait_interruptible(seconds: float) -> bool:
            if stop_event is None:
                await asyncio.sleep(seconds)
                return False
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=seconds)
                return True
            except asyncio.TimeoutError:
                return False

        async def send_char(ch: str) -> None:
            if ch == "\r":
                return
            if ch == "\n":
                await kb.press("Enter")
                return
            if ch == "\t":
                await kb.press("Tab")
                return
            if ch == " ":
                await kb.press("Space")
                return
            # Single-char keyboard.type dispatches full CDP keyDown -> char -> keyUp
            await kb.type(ch)

        if not code_mode:
            for line_idx, line in enumerate(lines):
                if stop_event and stop_event.is_set():
                    return {
                        "status": "stopped",
                        "typed_chars": typed_count,
                        "total_chars": len(clean_text),
                        "line": line_idx + 1,
                    }
                if line_idx > 0:
                    await kb.press("Enter")
                    typed_count += 1
                    enter_pause = max(0.05, base_delay * 1.3)
                    if humanize:
                        enter_pause += random.uniform(0.02, 0.08)
                    if await wait_interruptible(enter_pause):
                        return {
                            "status": "stopped",
                            "typed_chars": typed_count,
                            "total_chars": len(clean_text),
                            "line": line_idx + 1,
                        }

                for ch in line:
                    if stop_event and stop_event.is_set():
                        return {
                            "status": "stopped",
                            "typed_chars": typed_count,
                            "total_chars": len(clean_text),
                            "line": line_idx + 1,
                        }
                    await send_char(ch)
                    typed_count += 1
                    if on_progress and (
                        typed_count % update_interval == 0 or typed_count >= total_chars
                    ):
                        pct = min(100.0, (typed_count / total_chars) * 100.0)
                        on_progress(
                            typed_count,
                            total_chars,
                            pct,
                            f"Typing line {line_idx + 1} of {total_lines} ({pct:.0f}%)",
                        )
                    delay = TypingEngine.calculate_delay(ch, base_delay, humanize)
                    if await wait_interruptible(delay):
                        return {
                            "status": "stopped",
                            "typed_chars": typed_count,
                            "total_chars": len(clean_text),
                            "line": line_idx + 1,
                        }
        else:
            current_indent = 0
            last_block_opener = False

            for line_idx, line in enumerate(lines):
                if stop_event and stop_event.is_set():
                    return {
                        "status": "stopped",
                        "typed_chars": typed_count,
                        "total_chars": len(clean_text),
                        "line": line_idx + 1,
                    }

                if line_idx > 0:
                    if dismiss_autocomplete:
                        await kb.press("Escape")
                        await asyncio.sleep(0.015)

                    await kb.press("Enter")
                    typed_count += 1

                    enter_pause = max(0.06, base_delay * 1.3)
                    if humanize:
                        enter_pause += random.uniform(0.02, 0.08)
                    if await wait_interruptible(enter_pause):
                        return {
                            "status": "stopped",
                            "typed_chars": typed_count,
                            "total_chars": len(clean_text),
                            "line": line_idx + 1,
                        }

                    if last_block_opener:
                        current_indent += spaces_per_tab
                        last_block_opener = False

                if not line:
                    continue

                target_indent = TypingEngine._measure_indent_spaces(line, spaces_per_tab)
                content = line.lstrip(" \t")
                delta = target_indent - current_indent

                if delta > 0:
                    step_delay = max(0.015, base_delay * 0.7)
                    if use_tab_key and spaces_per_tab > 0:
                        tabs = delta // spaces_per_tab
                        rem = delta % spaces_per_tab
                        for _ in range(tabs):
                            if stop_event and stop_event.is_set():
                                return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                            await kb.press("Tab")
                            typed_count += spaces_per_tab
                            if await wait_interruptible(step_delay):
                                return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                        for _ in range(rem):
                            if stop_event and stop_event.is_set():
                                return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                            await kb.press("Space")
                            typed_count += 1
                            if await wait_interruptible(step_delay):
                                return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                    else:
                        for _ in range(delta):
                            if stop_event and stop_event.is_set():
                                return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                            await kb.press("Space")
                            typed_count += 1
                            if await wait_interruptible(step_delay):
                                return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                    current_indent = target_indent

                elif delta < 0:
                    unindent = abs(delta)
                    num_bs = max(1, (unindent + spaces_per_tab - 1) // spaces_per_tab)
                    bs_delay = max(0.02, base_delay * 0.6)
                    for _ in range(num_bs):
                        if stop_event and stop_event.is_set():
                            return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                        await kb.press("Backspace")
                        if await wait_interruptible(bs_delay):
                            return {"status": "stopped", "typed_chars": typed_count, "total_chars": len(clean_text)}
                    current_indent = target_indent

                for ch in content:
                    if stop_event and stop_event.is_set():
                        return {
                            "status": "stopped",
                            "typed_chars": typed_count,
                            "total_chars": len(clean_text),
                            "line": line_idx + 1,
                        }
                    await send_char(ch)
                    typed_count += 1
                    if on_progress and (
                        typed_count % update_interval == 0 or typed_count >= total_chars
                    ):
                        pct = min(100.0, (typed_count / total_chars) * 100.0)
                        on_progress(
                            typed_count,
                            total_chars,
                            pct,
                            f"Typing line {line_idx + 1} of {total_lines} ({pct:.0f}%)",
                        )
                    delay = TypingEngine.calculate_delay(ch, base_delay, humanize)
                    if await wait_interruptible(delay):
                        return {
                            "status": "stopped",
                            "typed_chars": typed_count,
                            "total_chars": len(clean_text),
                            "line": line_idx + 1,
                        }

                last_block_opener = TypingEngine._is_block_opener(line)

        return {
            "status": "finished",
            "typed_chars": len(clean_text),
            "total_chars": len(clean_text),
            "url": page.url,
        }
