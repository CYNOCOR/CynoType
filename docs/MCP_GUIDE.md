# CynoType MCP Server & Browser Control Guide

CynoType includes a **Model Context Protocol (MCP)** server (`run_mcp.py`) that gives AI hosts controlled browser inspection and realistic keystroke typing. It is built for user-authorized productivity, QA testing, web IDEs (Monaco / CodeMirror), VDI/remote consoles, and web apps where direct clipboard paste is disabled or unreliable.

> **Intended Use & Safety Scope**: CynoType MCP is strictly for user-authorized automation and accessibility workflows. It enforces an explicit domain allowlist, refuses password fields, logs all typing activity locally, and keeps a global hardware kill switch (`Esc` / `F8`) active at all times.

---

## Architecture

```text
AI Host (Claude Desktop / Cursor / VS Code / Antigravity)
        │  MCP over stdio
        ▼
┌──────────────────────────────────────────────────────────┐
│  CynoType MCP Server  (cynotype/mcp_server.py)           │
│  ├── BrowserPolicy          (allowlist, pwd block, caps) │
│  ├── BrowserController      (Playwright / Chrome CDP)    │
│  ├── TypingEngine           (Smart IDE + human rhythm)   │
│  └── Audit Log & KillSwitch (~/.cynotype/mcp_audit.jsonl)│
└──────────────────────────────────────────────────────────┘
        │                                    │
        ▼                                    ▼
Chrome DevTools Protocol (CDP)      OS Keystroke Driver (pynput)
(tab-scoped trusted keystrokes;     (canvas / VDI / native apps;
 works when onpaste is blocked)      global Esc / F8 kill switch)
```

---

## 1. Setup

Install the MCP and Playwright dependencies into your virtual environment:

```bash
# From the repository root
.venv/bin/python -m pip install -r requirements-mcp.txt
```

If you don't have Google Chrome installed locally and want Playwright's bundled Chromium as a fallback:
```bash
.venv/bin/python -m playwright install chromium
```

---

## 2. Connecting to Your Browser (Two Modes)

### Mode A: Attach to Your Existing Chrome Window (Recommended)
Launch Chrome with a local DevTools debugging port so CynoType MCP can inspect and type into tabs you already have open:

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/cynotype-chrome-profile

# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir=%TEMP%\cynotype-chrome-profile
```

Then the AI calls `browser_connect(cdp_url="http://127.0.0.1:9222")`.

### Mode B: Launch a Dedicated Visible Browser Window
If Chrome isn't running on port `9222`, calling `browser_connect(launch_if_needed=True)` opens a clean, visible Chrome/Chromium window managed for that session (and closes it when `browser_disconnect()` is called).

---

## 3. MCP Client Configuration

### Claude Desktop / Cursor / Antigravity / VS Code
Add `cynotype` to your client's MCP configuration file:

```json
{
  "mcpServers": {
    "cynotype": {
      "command": "/Users/YOUR_USER/path/to/Auto-Typer/.venv/bin/python",
      "args": ["/Users/YOUR_USER/path/to/Auto-Typer/run_mcp.py"],
      "env": {
        "CYNOTYPE_MCP_ALLOWED_DOMAINS": "localhost,127.0.0.1,codesandbox.io",
        "CYNOTYPE_MCP_MAX_CHARS": "50000"
      }
    }
  }
}
```

#### Environment Variables
| Variable | Default | Description |
|---|---|---|
| `CYNOTYPE_MCP_ALLOWED_DOMAINS` | `localhost,127.0.0.1,::1,*.localhost` | Comma-separated hostnames or glob patterns (e.g. `*.internal.corp`) pre-approved for browser tools |
| `CYNOTYPE_MCP_ALLOW_FILE_URLS` | `false` | Set to `true` to allow local `file://` pages |
| `CYNOTYPE_MCP_MAX_CHARS` | `50000` | Maximum characters allowed in a single typing job |

---

## 4. Tool Reference (14 Tools)

### Policy & Session
| Tool | Parameters | Purpose |
|---|---|---|
| `browser_get_policy` | *(none)* | Returns active domain allowlist, password-field rule, and character limit |
| `browser_allow_domain` | `domain: str` | Explicitly approves a domain or wildcard pattern for the current session |
| `browser_connect` | `cdp_url="http://127.0.0.1:9222"`, `launch_if_needed=True`, `use_system_chrome=True` | Attaches via CDP or launches a visible browser and returns open tabs |
| `browser_disconnect` | *(none)* | Stops active typing and disconnects (closes browser only if launched by CynoType) |

### Browser Inspection & Focus
| Tool | Parameters | Purpose |
|---|---|---|
| `browser_list_tabs` | *(none)* | Lists open tabs (`tab_id`, `title`, `url`, `allowed`) |
| `browser_navigate` | `url: str`, `tab_id=0` | Navigates an allowlisted tab to an allowlisted URL |
| `browser_inspect_inputs` | `tab_id=0` | Scans editable fields (`textarea`, `input`, `contenteditable`, Monaco, CodeMirror) returning index, selector, label, focus state, `is_password`, and `paste_blocked_attr` |
| `browser_focus_input` | `tab_id=0`, `selector=None`, `index=None` | Scrolls into view and focuses an editable element (refuses password fields) |
| `browser_read_value` | `tab_id=0`, `selector=None` | Reads current text from a non-password input/editor to verify typed output |

### Typing & Job Control
| Tool | Parameters | Purpose |
|---|---|---|
| `analyze_code_indent` | `text: str` | Detects whether a snippet uses Tabs, 2 spaces, or 4 spaces |
| `type_in_browser` | `text`, `tab_id=0`, `selector=None`, `index=None`, `mode="code"`, `speed_ms=45`, `humanize=True`, `use_tab_key=True`, `spaces_per_tab=None`, `dismiss_autocomplete=True`, `wait=False` | Dispatches trusted CDP keyboard events directly into the target browser tab using Smart IDE indentation or verbatim text mode |
| `type_os_window` | `text`, `mode="code"`, `speed_ms=45`, `humanize=True`, `start_delay=3`, `use_tab_key=True`, `spaces_per_tab=None`, `dismiss_autocomplete=True`, `wait=False` | Types into the currently focused desktop/VDI/canvas window via OS-level keystrokes after a countdown |
| `typing_status` | `job_id=None` | Returns progress (`typed_chars`, `total_chars`, `progress_pct`, `status`, `message`) for a job |
| `stop_typing` | *(none)* | Immediately stops any active browser or OS typing job |

---

## 5. Typical AI Workflow

1. **Connect & check policy**: `browser_connect()` → `browser_list_tabs()`
2. **Approve domain (if external)**: `browser_allow_domain("codesandbox.io")`
3. **Inspect editable fields**: `browser_inspect_inputs(tab_id=0)` → identifies e.g. `#codebox` (with `paste_blocked_attr: true`)
4. **Type via CDP**: `type_in_browser(text="...", tab_id=0, selector="#codebox", mode="code", speed_ms=35, wait=False)`
5. **Monitor or halt**: `typing_status(job_id)` / `stop_typing()` (or press physical `Esc` / `F8` at any moment)
6. **Verify result**: `browser_read_value(tab_id=0, selector="#codebox")`

---

## 6. Safety Guardrails & Audit Log

- **Domain Allowlist**: Navigation, inspection, reading, and typing all fail with an actionable error unless the tab's hostname matches the allowlist.
- **Password Protection**: Any attempt to focus, read, or type into `<input type="password">` is rejected.
- **Hardware Kill Switch**: Pressing **`Esc`** or **`F8`** (macOS) or **`F8`** (Windows) immediately signals cancellation to both CDP and OS typing jobs.
- **Audit Log**: Every domain approval and typing job (start/finish/stop/error, target URL, backend, character count) is appended as JSONL to `~/.cynotype/mcp_audit.jsonl`.
