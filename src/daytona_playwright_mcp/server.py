"""
Daytona Playwright MCP Server

An MCP server that provides tools to control a Playwright browser running inside
a Daytona sandbox. Supports navigation, clicking, typing, screenshots, and more.

Uses async Playwright API to avoid threading issues with FastMCP's worker pool.
"""

import asyncio
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

# Conditionally import Daytona SDK - allows running without it for testing
try:
    from daytona_sdk import Daytona, DaytonaConfig
    DAYTONA_AVAILABLE = True
except ImportError:
    DAYTONA_AVAILABLE = False

try:
    from patchright.async_api import async_playwright, Browser, Page, BrowserContext
    PATCHRIGHT_AVAILABLE = True
except ImportError:
    PATCHRIGHT_AVAILABLE = False


# ============================================================================
# Daytona Browser Configuration
# ============================================================================

# The default Daytona sandbox has chromium and Xvfb installed.
CDP_PORT = 9222

_LAUNCHER_PATH = "/tmp/_enable_browser_launcher.py"
_SESSION_ID = "enable-browser"

# Launcher script that starts chromium with CDP.
_LAUNCHER_SCRIPT = f'''
import signal
import subprocess
import os
import sys

os.makedirs("/home/daytona/.browser-profile", exist_ok=True)

chromium_args = [
    "chromium",
    "--user-data-dir=/home/daytona/.browser-profile",
    "--remote-debugging-port={CDP_PORT}",
    "--remote-debugging-address=0.0.0.0",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-client-side-phishing-detection",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-hang-monitor",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--no-sandbox",
    "--safebrowsing-disable-auto-update",
    "--disable-dev-shm-usage",
]

print("Starting chromium...", file=sys.stderr)
chromium_proc = subprocess.Popen(chromium_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"Chromium PID: {{chromium_proc.pid}}", file=sys.stderr)

signal.pause()
'''


# ============================================================================
# Browser Session Manager
# ============================================================================

@dataclass
class BrowserSession:
    """Manages a browser session inside a Daytona sandbox."""
    sandbox: object = None
    browser: object = None
    page: object = None
    playwright: object = None
    _signed_url: str = ""
    _vnc_url: str = ""

    def is_connected(self) -> bool:
        """Check if browser is connected and usable."""
        return self.browser is not None and self.page is not None


# Global session state
_session: BrowserSession = BrowserSession()


def _resolve_cdp_ws_url(preview_url: str) -> str:
    """
    Fetch /json/version and rebuild the WS URL against the Daytona proxy.
    """
    probe = preview_url.rstrip("/") + "/json/version"
    with urllib.request.urlopen(probe, timeout=10) as r:
        data = json.load(r)
        path = urlparse(data["webSocketDebuggerUrl"]).path
        host = urlparse(preview_url).netloc
        return f"wss://{host}{path}"


def _tail_launcher_logs(sandbox, cmd) -> str:
    """Best-effort fetch of the session command's stdout/stderr for diagnosis."""
    cmd_id = getattr(cmd, "cmd_id", None) or getattr(cmd, "id", None)
    if cmd_id is None:
        return "<no command id available>"
    try:
        log = sandbox.process.get_session_command_logs(_SESSION_ID, cmd_id)
    except Exception as e:
        return f"<failed to fetch logs: {e}>"
    return str(log) if log else "<empty>"


# ============================================================================
# MCP Server Definition
# ============================================================================

mcp = FastMCP(
    name="Daytona Playwright",
    instructions="""
    This MCP server controls a Playwright browser running inside a Daytona sandbox.

    Workflow:
    1. First call `browser_start` to create a sandbox and launch the browser
    2. Use navigation tools (`browser_navigate`, `browser_click`, `browser_type`, etc.)
    3. Use `browser_screenshot` to see what's on the page
    4. When done, call `browser_stop` to clean up

    The browser runs in a secure cloud sandbox with full Chrome capabilities.
    Screenshots are returned as base64-encoded images.
    """
)


# ============================================================================
# Browser Lifecycle Tools
# ============================================================================

@mcp.tool
async def browser_start(
    timeout: Annotated[int, "Timeout in seconds to wait for browser to be ready"] = 60
) -> str:
    """
    Start a new browser session in a Daytona sandbox.

    This creates a cloud sandbox with Chrome installed, launches the browser,
    and establishes a connection for remote control. Must be called before
    using any other browser tools.
    """
    global _session

    if not DAYTONA_AVAILABLE:
        return "Error: daytona-sdk is not installed. Please install it with: pip install daytona-sdk"

    if not PATCHRIGHT_AVAILABLE:
        return "Error: patchright is not installed. Please install it with: pip install patchright"

    if _session.is_connected():
        return "Browser is already running. Use browser_stop first if you want to restart."

    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        return "Error: DAYTONA_API_KEY environment variable is not set."

    api_url = os.environ.get("DAYTONA_API_URL", os.environ.get("DAYTONA_SERVER_URL"))

    try:
        # Initialize Daytona client (blocking SDK calls run in thread pool)
        config = DaytonaConfig(api_key=api_key, api_url=api_url) if api_url else DaytonaConfig(api_key=api_key)
        daytona = Daytona(config)

        # Create sandbox using the default Python sandbox (has chromium + Xvfb)
        sandbox = await asyncio.to_thread(daytona.create, timeout=timeout)

        _session.sandbox = sandbox

        await asyncio.to_thread(sandbox.fs.upload_file, _LAUNCHER_SCRIPT.encode(), _LAUNCHER_PATH)
        await asyncio.to_thread(sandbox.process.create_session, _SESSION_ID)

        # Start the desktop first — this is what VNC on 6080 streams. Chromium
        # then launches into the same display (:0) so its window is visible.
        # Previously chromium rendered to a separate Xvfb on :99, invisible to VNC.
        await asyncio.to_thread(sandbox.computer_use.start)
        vnc_preview = await asyncio.to_thread(sandbox.create_signed_preview_url, 6080)
        _session._vnc_url = vnc_preview.url

        from daytona_sdk import SessionExecuteRequest
        cmd = await asyncio.to_thread(
            sandbox.process.execute_session_command,
            _SESSION_ID,
            SessionExecuteRequest(
                command=f"DISPLAY=:0 python {_LAUNCHER_PATH}",
                run_async=True,
            ),
        )

        # Wait for chromium to bind the CDP port.
        await asyncio.sleep(15)

        signed_preview = await asyncio.to_thread(sandbox.create_signed_preview_url, CDP_PORT)
        signed = signed_preview.url
        _session._signed_url = signed

        # Connect via CDP using async playwright
        pw = await async_playwright().start()
        _session.playwright = pw

        deadline = asyncio.get_event_loop().time() + timeout
        last_err = None

        while asyncio.get_event_loop().time() < deadline:
            try:
                ws_url = await asyncio.to_thread(_resolve_cdp_ws_url, signed)
                browser = await pw.chromium.connect_over_cdp(ws_url)
                _session.browser = browser

                # Get or create a page
                ctx = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                _session.page = page

                return f"Browser started successfully in Daytona sandbox. Ready for commands.\n\nLive view: {_session._vnc_url}"

            except Exception as e:
                last_err = e
                await asyncio.sleep(2)

        # Timeout - try to get logs
        launcher_logs = await asyncio.to_thread(_tail_launcher_logs, sandbox, cmd)
        return f"Error: Browser failed to start within {timeout}s. Last error: {last_err}\nLauncher logs:\n{launcher_logs}"

    except Exception as e:
        # Clean up on failure
        if _session.sandbox:
            try:
                await asyncio.to_thread(_session.sandbox.delete)
            except:
                pass
        _session = BrowserSession()
        return f"Error starting browser: {str(e)}"


@mcp.tool
async def browser_stop() -> str:
    """
    Stop the browser and clean up the Daytona sandbox.

    Call this when you're done using the browser to free up resources.
    """
    global _session

    errors = []

    if _session.browser:
        try:
            await _session.browser.close()
        except Exception as e:
            errors.append(f"Error closing browser: {e}")

    if _session.playwright:
        try:
            await _session.playwright.stop()
        except Exception as e:
            errors.append(f"Error stopping playwright: {e}")

    if _session.sandbox:
        try:
            await asyncio.to_thread(_session.sandbox.delete)
        except Exception as e:
            errors.append(f"Error deleting sandbox: {e}")

    _session = BrowserSession()

    if errors:
        return "Browser stopped with errors: " + "; ".join(errors)
    return "Browser stopped and sandbox deleted successfully."


@mcp.tool
async def browser_status() -> str:
    """
    Check the current status of the browser session.
    """
    if not _session.is_connected():
        return "Browser is not running. Call browser_start to begin."

    try:
        url = _session.page.url
        title = await _session.page.title()
        status = f"Browser is running.\nCurrent URL: {url}\nPage title: {title}"
        if _session._vnc_url:
            status += f"\n\nLive view: {_session._vnc_url}"
        return status
    except Exception as e:
        return f"Browser session exists but may be disconnected: {e}"


# ============================================================================
# Navigation Tools
# ============================================================================

@mcp.tool
async def browser_navigate(
    url: Annotated[str, "The URL to navigate to"],
    wait_until: Annotated[
        Literal["load", "domcontentloaded", "networkidle", "commit"],
        "When to consider navigation complete"
    ] = "load"
) -> str:
    """
    Navigate the browser to a URL.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.goto(url, wait_until=wait_until)
        title = await _session.page.title()
        return f"Navigated to {url}\nPage title: {title}"
    except Exception as e:
        return f"Error navigating to {url}: {e}"


@mcp.tool
async def browser_back() -> str:
    """Navigate back in browser history."""
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.go_back()
        return f"Navigated back. Current URL: {_session.page.url}"
    except Exception as e:
        return f"Error navigating back: {e}"


@mcp.tool
async def browser_forward() -> str:
    """Navigate forward in browser history."""
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.go_forward()
        return f"Navigated forward. Current URL: {_session.page.url}"
    except Exception as e:
        return f"Error navigating forward: {e}"


@mcp.tool
async def browser_refresh() -> str:
    """Refresh the current page."""
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.reload()
        return f"Page refreshed. Current URL: {_session.page.url}"
    except Exception as e:
        return f"Error refreshing page: {e}"


# ============================================================================
# Interaction Tools
# ============================================================================

@mcp.tool
async def browser_click(
    selector: Annotated[str, "CSS selector, XPath, or text to click (e.g., 'button.submit', '//button[@id=\"login\"]', 'text=Sign In')"],
    button: Annotated[Literal["left", "right", "middle"], "Mouse button to use"] = "left",
    click_count: Annotated[int, "Number of clicks (1 for single, 2 for double)"] = 1,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Click on an element on the page.

    Supports CSS selectors, XPath, and text selectors.
    Examples:
    - CSS: "button.primary", "#submit-btn", "[data-testid='login']"
    - XPath: "//button[@type='submit']"
    - Text: "text=Sign In", "text=Submit"
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.click(selector, button=button, click_count=click_count, timeout=timeout)
        return f"Clicked on element: {selector}"
    except Exception as e:
        return f"Error clicking on {selector}: {e}"


@mcp.tool
async def browser_type(
    selector: Annotated[str, "CSS selector or text selector for the input element"],
    text: Annotated[str, "Text to type into the element"],
    clear_first: Annotated[bool, "Whether to clear the field before typing"] = True,
    delay: Annotated[int, "Delay between key presses in milliseconds"] = 0,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Type text into an input field or editable element.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if clear_first:
            await _session.page.fill(selector, text, timeout=timeout)
        else:
            await _session.page.type(selector, text, delay=delay, timeout=timeout)
        return f"Typed text into element: {selector}"
    except Exception as e:
        return f"Error typing into {selector}: {e}"


@mcp.tool
async def browser_press(
    key: Annotated[str, "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown', 'Control+a')"],
    selector: Annotated[str | None, "Optional selector to focus before pressing key"] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Press a keyboard key, optionally on a specific element.

    Key examples: Enter, Tab, Escape, Backspace, Delete, ArrowUp, ArrowDown,
    Control+a, Control+c, Control+v, Shift+Tab, Alt+F4
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if selector:
            await _session.page.press(selector, key, timeout=timeout)
        else:
            await _session.page.keyboard.press(key)
        return f"Pressed key: {key}"
    except Exception as e:
        return f"Error pressing key {key}: {e}"


@mcp.tool
async def browser_hover(
    selector: Annotated[str, "CSS selector or text selector for the element to hover over"],
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Hover over an element on the page.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.hover(selector, timeout=timeout)
        return f"Hovering over element: {selector}"
    except Exception as e:
        return f"Error hovering over {selector}: {e}"


@mcp.tool
async def browser_select(
    selector: Annotated[str, "CSS selector for the <select> element"],
    value: Annotated[str | None, "Value attribute to select"] = None,
    label: Annotated[str | None, "Visible text label to select"] = None,
    index: Annotated[int | None, "Index of option to select (0-based)"] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Select an option from a dropdown (<select> element).

    Provide one of: value, label, or index.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if value:
            await _session.page.select_option(selector, value=value, timeout=timeout)
        elif label:
            await _session.page.select_option(selector, label=label, timeout=timeout)
        elif index is not None:
            await _session.page.select_option(selector, index=index, timeout=timeout)
        else:
            return "Error: Must provide value, label, or index to select."
        return f"Selected option in: {selector}"
    except Exception as e:
        return f"Error selecting option in {selector}: {e}"


@mcp.tool
async def browser_scroll(
    direction: Annotated[Literal["up", "down", "left", "right"], "Direction to scroll"] = "down",
    amount: Annotated[int, "Amount to scroll in pixels"] = 500,
    selector: Annotated[str | None, "Optional selector for a scrollable element"] = None
) -> str:
    """
    Scroll the page or a specific element.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if selector:
            element = _session.page.locator(selector)
            if direction == "down":
                await element.evaluate(f"el => el.scrollTop += {amount}")
            elif direction == "up":
                await element.evaluate(f"el => el.scrollTop -= {amount}")
            elif direction == "right":
                await element.evaluate(f"el => el.scrollLeft += {amount}")
            elif direction == "left":
                await element.evaluate(f"el => el.scrollLeft -= {amount}")
        else:
            if direction == "down":
                await _session.page.evaluate(f"window.scrollBy(0, {amount})")
            elif direction == "up":
                await _session.page.evaluate(f"window.scrollBy(0, -{amount})")
            elif direction == "right":
                await _session.page.evaluate(f"window.scrollBy({amount}, 0)")
            elif direction == "left":
                await _session.page.evaluate(f"window.scrollBy(-{amount}, 0)")

        return f"Scrolled {direction} by {amount}px"
    except Exception as e:
        return f"Error scrolling: {e}"


# ============================================================================
# Content Extraction Tools
# ============================================================================

@mcp.tool
async def browser_screenshot(
    full_page: Annotated[bool, "Whether to capture the full scrollable page"] = False,
    selector: Annotated[str | None, "Optional selector to screenshot a specific element"] = None
) -> Image:
    """
    Take a screenshot of the current page or a specific element.

    Returns the screenshot as an image that can be displayed.
    """
    if not _session.is_connected():
        raise ValueError("Browser is not running. Call browser_start first.")

    try:
        if selector:
            screenshot_bytes = await _session.page.locator(selector).screenshot()
        else:
            screenshot_bytes = await _session.page.screenshot(full_page=full_page)

        return Image(data=screenshot_bytes, format="png")
    except Exception as e:
        raise ValueError(f"Error taking screenshot: {e}")


@mcp.tool
async def browser_get_text(
    selector: Annotated[str | None, "CSS selector to get text from specific element(s). If not provided, gets all visible text."] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Get text content from the page or specific elements.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if selector:
            elements = _session.page.locator(selector)
            count = await elements.count()
            if count == 0:
                return f"No elements found matching: {selector}"

            texts = []
            for i in range(min(count, 100)):  # Limit to first 100 elements
                texts.append(await elements.nth(i).inner_text(timeout=timeout))
            return "\n---\n".join(texts)
        else:
            return await _session.page.inner_text("body", timeout=timeout)
    except Exception as e:
        return f"Error getting text: {e}"


@mcp.tool
async def browser_get_html(
    selector: Annotated[str | None, "CSS selector to get HTML from specific element. If not provided, gets full page HTML."] = None,
    outer: Annotated[bool, "Whether to include the element itself (outer) or just its contents (inner)"] = False
) -> str:
    """
    Get HTML content from the page or a specific element.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if selector:
            element = _session.page.locator(selector).first
            if outer:
                return await element.evaluate("el => el.outerHTML")
            else:
                return await element.inner_html()
        else:
            return await _session.page.content()
    except Exception as e:
        return f"Error getting HTML: {e}"


@mcp.tool
async def browser_get_attribute(
    selector: Annotated[str, "CSS selector for the element"],
    attribute: Annotated[str, "Name of the attribute to get (e.g., 'href', 'src', 'class')"],
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Get an attribute value from an element.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        value = await _session.page.get_attribute(selector, attribute, timeout=timeout)
        if value is None:
            return f"Attribute '{attribute}' not found on element: {selector}"
        return value
    except Exception as e:
        return f"Error getting attribute: {e}"


@mcp.tool
async def browser_evaluate(
    script: Annotated[str, "JavaScript code to execute in the page context"],
) -> str:
    """
    Execute JavaScript in the page context and return the result.

    Examples:
    - "document.title"
    - "window.location.href"
    - "document.querySelectorAll('a').length"
    - "JSON.stringify(localStorage)"
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        result = await _session.page.evaluate(script)
        if result is None:
            return "null"
        if isinstance(result, (dict, list)):
            return json.dumps(result, indent=2)
        return str(result)
    except Exception as e:
        return f"Error executing script: {e}"


# ============================================================================
# Wait Tools
# ============================================================================

@mcp.tool
async def browser_wait_for_selector(
    selector: Annotated[str, "CSS selector to wait for"],
    state: Annotated[
        Literal["attached", "detached", "visible", "hidden"],
        "State to wait for"
    ] = "visible",
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Wait for an element to reach a specific state.

    States:
    - attached: Element is in the DOM
    - detached: Element is removed from the DOM
    - visible: Element is visible on the page
    - hidden: Element is hidden or removed
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.wait_for_selector(selector, state=state, timeout=timeout)
        return f"Element {selector} is now {state}"
    except Exception as e:
        return f"Error waiting for selector {selector}: {e}"


@mcp.tool
async def browser_wait_for_navigation(
    url: Annotated[str | None, "URL pattern to wait for (glob, regex, or exact)"] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Wait for a navigation to complete.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if url:
            await _session.page.wait_for_url(url, timeout=timeout)
        else:
            await _session.page.wait_for_load_state("load", timeout=timeout)
        return f"Navigation complete. Current URL: {_session.page.url}"
    except Exception as e:
        return f"Error waiting for navigation: {e}"


# ============================================================================
# Tab Management Tools
# ============================================================================

@mcp.tool
async def browser_new_tab(
    url: Annotated[str | None, "URL to open in the new tab"] = None
) -> str:
    """
    Open a new browser tab and switch to it.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        ctx = _session.browser.contexts[0]
        page = await ctx.new_page()
        _session.page = page

        if url:
            await page.goto(url)
            return f"Opened new tab and navigated to: {url}"
        return "Opened new blank tab"
    except Exception as e:
        return f"Error opening new tab: {e}"


@mcp.tool
async def browser_list_tabs() -> str:
    """
    List all open tabs with their URLs and titles.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        ctx = _session.browser.contexts[0]
        tabs = []
        for i, page in enumerate(ctx.pages):
            active = " (active)" if page == _session.page else ""
            title = await page.title()
            tabs.append(f"{i}: {title} - {page.url}{active}")
        return "\n".join(tabs) if tabs else "No tabs open"
    except Exception as e:
        return f"Error listing tabs: {e}"


@mcp.tool
async def browser_switch_tab(
    index: Annotated[int, "Index of the tab to switch to (0-based)"]
) -> str:
    """
    Switch to a different browser tab by index.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        ctx = _session.browser.contexts[0]
        pages = ctx.pages
        if index < 0 or index >= len(pages):
            return f"Error: Tab index {index} out of range. Available tabs: 0-{len(pages)-1}"

        _session.page = pages[index]
        await _session.page.bring_to_front()
        title = await _session.page.title()
        return f"Switched to tab {index}: {title} - {_session.page.url}"
    except Exception as e:
        return f"Error switching tab: {e}"


@mcp.tool
async def browser_close_tab(
    index: Annotated[int | None, "Index of the tab to close (defaults to current tab)"] = None
) -> str:
    """
    Close a browser tab.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        ctx = _session.browser.contexts[0]
        pages = ctx.pages

        if len(pages) <= 1:
            return "Error: Cannot close the last tab. Use browser_stop to end the session."

        if index is None:
            # Close current tab
            current_idx = pages.index(_session.page)
            await _session.page.close()
            # Switch to another tab
            remaining = ctx.pages
            _session.page = remaining[min(current_idx, len(remaining)-1)]
            return f"Closed current tab. Now on: {_session.page.url}"
        else:
            if index < 0 or index >= len(pages):
                return f"Error: Tab index {index} out of range"
            page_to_close = pages[index]
            await page_to_close.close()
            if page_to_close == _session.page:
                _session.page = ctx.pages[0]
            return f"Closed tab {index}"
    except Exception as e:
        return f"Error closing tab: {e}"


# ============================================================================
# File Operations
# ============================================================================

@mcp.tool
async def browser_upload_file(
    selector: Annotated[str, "CSS selector for the file input element"],
    file_path: Annotated[str, "Path to the file to upload (on your local machine)"],
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Upload a file to a file input element.

    Note: The file must exist on the machine running this MCP server.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.set_input_files(selector, file_path, timeout=timeout)
        return f"Uploaded file: {file_path}"
    except Exception as e:
        return f"Error uploading file: {e}"


@mcp.tool
async def browser_download_wait(
    timeout: Annotated[int, "Timeout in milliseconds to wait for download"] = 60000
) -> str:
    """
    Wait for a download to start and complete, returning the downloaded file path.

    Call this before triggering the download action.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    # Note: This is a simplified implementation. Full download handling
    # would require more complex async patterns.
    return "Download monitoring not yet implemented. Downloads will save to the sandbox's default location."


# ============================================================================
# Entry Point
# ============================================================================

def main():
    """Run the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="Daytona Playwright MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport protocol (default: stdio)"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to for SSE/HTTP transport (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for SSE/HTTP transport (default: 8765)"
    )

    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        print(f"Starting SSE server on {args.host}:{args.port}", file=sys.stderr)
        mcp.run(transport="sse", host=args.host, port=args.port)
    elif args.transport == "http":
        print(f"Starting HTTP server on {args.host}:{args.port}", file=sys.stderr)
        mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
