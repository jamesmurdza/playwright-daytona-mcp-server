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

    Form Management:
    - Use `browser_form_list` to discover all forms on a page
    - Use `browser_form_get_fields` to see all fields in a form with their current values
    - Use `browser_form_fill` to fill multiple fields at once with a JSON mapping
    - Use `browser_form_submit` to submit a form
    - Use `browser_form_reset` to clear a form
    - Use `browser_checkbox_set` and `browser_radio_select` for checkboxes and radios
    - Use `browser_form_get_validation` to check HTML5 validation errors
    - Use `browser_form_focus` and `browser_form_blur` for focus management

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
# Form Management Tools
# ============================================================================

@mcp.tool
async def browser_form_list(
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    List all forms on the current page with their attributes and field counts.

    Returns information about each form including:
    - Form index (for use with other form tools)
    - Form id, name, action, method attributes
    - Number of input fields
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        forms_info = await _session.page.evaluate("""
            () => {
                const forms = document.querySelectorAll('form');
                return Array.from(forms).map((form, index) => {
                    const inputs = form.querySelectorAll('input, select, textarea');
                    return {
                        index: index,
                        id: form.id || null,
                        name: form.name || null,
                        action: form.action || null,
                        method: form.method || 'get',
                        fieldCount: inputs.length
                    };
                });
            }
        """)

        if not forms_info:
            return "No forms found on the page."

        result = []
        for form in forms_info:
            info = f"Form {form['index']}:"
            if form['id']:
                info += f" id='{form['id']}'"
            if form['name']:
                info += f" name='{form['name']}'"
            info += f" method={form['method']}"
            if form['action']:
                info += f" action='{form['action']}'"
            info += f" ({form['fieldCount']} fields)"
            result.append(info)

        return "\n".join(result)
    except Exception as e:
        return f"Error listing forms: {e}"


@mcp.tool
async def browser_form_get_fields(
    selector: Annotated[str | None, "CSS selector for a specific form. If not provided, gets fields from the first form."] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Get all fields from a form with their current values and attributes.

    Returns detailed information about each field including:
    - Field type (text, email, password, checkbox, radio, select, textarea, etc.)
    - Field name, id, placeholder
    - Current value
    - Required/disabled status
    - Options for select elements
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        form_selector = selector or "form"

        fields_info = await _session.page.evaluate(f"""
            (formSelector) => {{
                const form = document.querySelector(formSelector);
                if (!form) return null;

                const fields = form.querySelectorAll('input, select, textarea');
                return Array.from(fields).map(field => {{
                    const info = {{
                        tagName: field.tagName.toLowerCase(),
                        type: field.type || null,
                        name: field.name || null,
                        id: field.id || null,
                        placeholder: field.placeholder || null,
                        value: field.value || '',
                        required: field.required || false,
                        disabled: field.disabled || false,
                        readOnly: field.readOnly || false
                    }};

                    // Handle checkboxes and radios
                    if (field.type === 'checkbox' || field.type === 'radio') {{
                        info.checked = field.checked;
                    }}

                    // Handle select elements
                    if (field.tagName.toLowerCase() === 'select') {{
                        info.options = Array.from(field.options).map(opt => ({{
                            value: opt.value,
                            text: opt.text,
                            selected: opt.selected
                        }}));
                        info.multiple = field.multiple;
                    }}

                    return info;
                }});
            }}
        """, form_selector)

        if fields_info is None:
            return f"No form found matching selector: {form_selector}"

        if not fields_info:
            return "Form found but contains no fields."

        result = []
        for i, field in enumerate(fields_info):
            field_type = field.get('type') or field['tagName']
            identifier = field['name'] or field['id'] or f"[field {i}]"

            line = f"- {identifier} ({field_type})"

            if field.get('value'):
                line += f": '{field['value']}'"
            if field.get('checked'):
                line += " [checked]"
            if field.get('required'):
                line += " [required]"
            if field.get('disabled'):
                line += " [disabled]"
            if field.get('readOnly'):
                line += " [readonly]"
            if field.get('placeholder'):
                line += f" placeholder='{field['placeholder']}'"

            # Show select options
            if field.get('options'):
                opts = ", ".join([f"'{o['value']}'" + (" *" if o['selected'] else "") for o in field['options'][:5]])
                if len(field['options']) > 5:
                    opts += f" ... (+{len(field['options']) - 5} more)"
                line += f" options=[{opts}]"

            result.append(line)

        return "\n".join(result)
    except Exception as e:
        return f"Error getting form fields: {e}"


@mcp.tool
async def browser_form_fill(
    data: Annotated[str, "JSON object mapping field selectors or names to values. Example: {\"#email\": \"test@example.com\", \"password\": \"secret\"}"],
    selector: Annotated[str | None, "CSS selector for a specific form. If not provided, searches the entire page."] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Fill multiple form fields at once using a JSON mapping.

    The data parameter should be a JSON object where:
    - Keys can be CSS selectors (e.g., "#email", "[name='username']") or field names
    - Values are the text to fill, or for checkboxes/radios: true/false

    Examples:
    - '{"#email": "user@example.com", "#password": "secret123"}'
    - '{"username": "john", "remember_me": true}'
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        field_data = json.loads(data)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON data: {e}"

    if not isinstance(field_data, dict):
        return "Error: Data must be a JSON object (dictionary)"

    results = []
    errors = []

    for field_key, value in field_data.items():
        try:
            # Build the selector - if it looks like a selector, use it directly
            # Otherwise, try to find by name attribute within the form
            if field_key.startswith('#') or field_key.startswith('.') or field_key.startswith('[') or ' ' in field_key:
                field_selector = field_key
            else:
                # Try to find by name within the form context
                form_prefix = f"{selector} " if selector else ""
                field_selector = f"{form_prefix}[name='{field_key}']"

            # Get field info to determine how to fill it
            field_info = await _session.page.evaluate(f"""
                (sel) => {{
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    return {{
                        tagName: el.tagName.toLowerCase(),
                        type: el.type || null
                    }};
                }}
            """, field_selector)

            if field_info is None:
                errors.append(f"Field not found: {field_key}")
                continue

            field_type = field_info.get('type', '').lower()
            tag_name = field_info['tagName']

            # Handle different field types
            if field_type in ('checkbox', 'radio'):
                is_checked = await _session.page.is_checked(field_selector)
                should_check = bool(value)
                if should_check and not is_checked:
                    await _session.page.check(field_selector, timeout=timeout)
                    results.append(f"Checked: {field_key}")
                elif not should_check and is_checked:
                    await _session.page.uncheck(field_selector, timeout=timeout)
                    results.append(f"Unchecked: {field_key}")
                else:
                    results.append(f"Already {'checked' if is_checked else 'unchecked'}: {field_key}")
            elif tag_name == 'select':
                await _session.page.select_option(field_selector, value=str(value), timeout=timeout)
                results.append(f"Selected '{value}' in: {field_key}")
            else:
                await _session.page.fill(field_selector, str(value), timeout=timeout)
                results.append(f"Filled: {field_key}")

        except Exception as e:
            errors.append(f"Error with {field_key}: {str(e)}")

    output = []
    if results:
        output.append("Successfully filled:")
        output.extend(f"  {r}" for r in results)
    if errors:
        output.append("Errors:")
        output.extend(f"  {e}" for e in errors)

    return "\n".join(output) if output else "No fields processed"


@mcp.tool
async def browser_form_submit(
    selector: Annotated[str | None, "CSS selector for the form or submit button. If not provided, submits the first form."] = None,
    wait_for_navigation: Annotated[bool, "Whether to wait for page navigation after submit"] = True,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Submit a form by clicking its submit button or triggering form submission.

    This tool will:
    1. Find the submit button within the form and click it
    2. Or trigger the form's submit event if no button is found
    3. Optionally wait for navigation to complete
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        form_selector = selector or "form"

        # Try to find and click a submit button first
        submit_selectors = [
            f"{form_selector} button[type='submit']",
            f"{form_selector} input[type='submit']",
            f"{form_selector} button:not([type])",  # Buttons without type default to submit
        ]

        button_found = False
        for btn_selector in submit_selectors:
            try:
                count = await _session.page.locator(btn_selector).count()
                if count > 0:
                    if wait_for_navigation:
                        async with _session.page.expect_navigation(timeout=timeout):
                            await _session.page.click(btn_selector, timeout=timeout)
                    else:
                        await _session.page.click(btn_selector, timeout=timeout)
                    button_found = True
                    break
            except:
                continue

        if not button_found:
            # Fall back to programmatic form submission
            if wait_for_navigation:
                async with _session.page.expect_navigation(timeout=timeout):
                    await _session.page.evaluate(f"document.querySelector('{form_selector}').submit()")
            else:
                await _session.page.evaluate(f"document.querySelector('{form_selector}').submit()")

        url = _session.page.url
        return f"Form submitted successfully. Current URL: {url}"
    except Exception as e:
        return f"Error submitting form: {e}"


@mcp.tool
async def browser_form_reset(
    selector: Annotated[str | None, "CSS selector for the form to reset. If not provided, resets the first form."] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Reset a form to its initial values.

    This clears all user input and restores default values for all fields.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        form_selector = selector or "form"

        result = await _session.page.evaluate(f"""
            (formSelector) => {{
                const form = document.querySelector(formSelector);
                if (!form) return {{ success: false, error: 'Form not found' }};
                form.reset();
                return {{ success: true }};
            }}
        """, form_selector)

        if not result['success']:
            return f"Error: {result.get('error', 'Unknown error')}"

        return f"Form reset successfully: {form_selector}"
    except Exception as e:
        return f"Error resetting form: {e}"


@mcp.tool
async def browser_checkbox_set(
    selector: Annotated[str, "CSS selector for the checkbox element"],
    checked: Annotated[bool, "Whether the checkbox should be checked"] = True,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Set a checkbox to checked or unchecked state.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        if checked:
            await _session.page.check(selector, timeout=timeout)
            return f"Checkbox checked: {selector}"
        else:
            await _session.page.uncheck(selector, timeout=timeout)
            return f"Checkbox unchecked: {selector}"
    except Exception as e:
        return f"Error setting checkbox {selector}: {e}"


@mcp.tool
async def browser_radio_select(
    selector: Annotated[str, "CSS selector for the radio button to select"],
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Select a radio button option.

    Example selectors:
    - By value: "input[name='color'][value='red']"
    - By id: "#radio-option-1"
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.check(selector, timeout=timeout)
        return f"Radio button selected: {selector}"
    except Exception as e:
        return f"Error selecting radio button {selector}: {e}"


@mcp.tool
async def browser_form_get_validation(
    selector: Annotated[str | None, "CSS selector for a specific form. If not provided, checks the first form."] = None,
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Get HTML5 validation state and messages for all fields in a form.

    Returns validation status including:
    - Which fields are invalid
    - Validation error messages
    - Whether the form is valid overall
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        form_selector = selector or "form"

        validation_info = await _session.page.evaluate(f"""
            (formSelector) => {{
                const form = document.querySelector(formSelector);
                if (!form) return null;

                const fields = form.querySelectorAll('input, select, textarea');
                const fieldValidation = Array.from(fields).map(field => {{
                    const name = field.name || field.id || '[unnamed]';
                    return {{
                        name: name,
                        valid: field.validity.valid,
                        message: field.validationMessage || null,
                        valueMissing: field.validity.valueMissing,
                        typeMismatch: field.validity.typeMismatch,
                        patternMismatch: field.validity.patternMismatch,
                        tooShort: field.validity.tooShort,
                        tooLong: field.validity.tooLong,
                        rangeUnderflow: field.validity.rangeUnderflow,
                        rangeOverflow: field.validity.rangeOverflow
                    }};
                }}).filter(f => !f.valid);

                return {{
                    formValid: form.checkValidity(),
                    invalidFields: fieldValidation
                }};
            }}
        """, form_selector)

        if validation_info is None:
            return f"No form found matching selector: {form_selector}"

        if validation_info['formValid']:
            return "Form is valid. All fields pass validation."

        result = ["Form has validation errors:"]
        for field in validation_info['invalidFields']:
            msg = field['message'] or "Invalid"
            result.append(f"  - {field['name']}: {msg}")

        return "\n".join(result)
    except Exception as e:
        return f"Error checking form validation: {e}"


@mcp.tool
async def browser_form_focus(
    selector: Annotated[str, "CSS selector for the element to focus"],
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Focus on a specific form field.

    This is useful for triggering focus-related events or preparing for keyboard input.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.focus(selector, timeout=timeout)
        return f"Focused on element: {selector}"
    except Exception as e:
        return f"Error focusing on {selector}: {e}"


@mcp.tool
async def browser_form_blur(
    selector: Annotated[str, "CSS selector for the element to blur (unfocus)"],
    timeout: Annotated[int, "Timeout in milliseconds"] = 30000
) -> str:
    """
    Remove focus from a specific form field (blur).

    This is useful for triggering blur-related validation events.
    """
    if not _session.is_connected():
        return "Error: Browser is not running. Call browser_start first."

    try:
        await _session.page.evaluate(f"document.querySelector('{selector}').blur()")
        return f"Blurred element: {selector}"
    except Exception as e:
        return f"Error blurring {selector}: {e}"


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
