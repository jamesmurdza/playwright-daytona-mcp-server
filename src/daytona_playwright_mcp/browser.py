"""
Shared browser session setup for the MCP server and smoke test.

`create_browser_session()` provisions a Daytona sandbox using a declarative
image (xfce+VNC stack + patchright+Chrome), launches stealth Chrome via
patchright into DISPLAY=:0, and returns a `BrowserSession` with the connected
page, the VNC preview URL, and a `close()` coroutine for teardown.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from daytona import (
    CreateSandboxFromImageParams,
    Daytona,
    Image,
    Sandbox,
    SessionExecuteRequest,
)
from daytona_api_client.models.port_preview_url import PortPreviewUrl 
from patchright.async_api import Browser, Page, Playwright, async_playwright


# Declarative image: minimal slice of the default Daytona snapshot — the xfce
# + VNC stack that computer_use streams on 6080 for the live view, plus
# patchright with the Chrome channel for stealth.
browser_image = (
    Image.debian_slim("3.12")
    .run_commands(
        "apt-get update && apt-get install -y --no-install-recommends "
        "xvfb x11vnc novnc xfce4 xfce4-terminal dbus-x11 "
        "&& rm -rf /var/lib/apt/lists/*"
    )
    .pip_install(["patchright>=1.48"])
    .run_commands(
        "patchright install chrome",
        "patchright install-deps chrome",
    )
    .workdir("/home/daytona")
)

CDP_PORT = 9222
VNC_PORT = 6080

_LAUNCHER_PATH = "/tmp/_enable_browser_launcher.py"
_SESSION_ID = "enable-browser"

# Runs inside the sandbox. Patchright applies stealth patches on launch, so
# we must launch via its API (not a raw chrome subprocess) — connecting over
# CDP to an already-running browser skips those patches entirely.
#
# Drop --remote-debugging-pipe from Playwright's default args, or Chrome opens
# a pipe transport instead of binding CDP_PORT, and connect_over_cdp fails.
_LAUNCHER_SCRIPT = f'''
import signal
from patchright.sync_api import sync_playwright

p = sync_playwright().start()
p.chromium.launch_persistent_context(
    user_data_dir="/home/daytona/.browser-profile",
    channel="chrome",
    headless=False,
    no_viewport=True,
    ignore_default_args=["--remote-debugging-pipe"],
    args=[
        "--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=0.0.0.0",
        "--no-first-run",
        "--no-default-browser-check",
    ],
)
signal.pause()
'''


@dataclass
class BrowserSession:
    sandbox: Sandbox
    playwright: Playwright
    browser: Browser
    page: Page
    vnc_url: str

    async def close(self) -> None:
        """Tear down the browser, playwright, and sandbox. Swallows errors."""
        try:
            await self.browser.close()
        except Exception:
            pass
        try:
            await self.playwright.stop()
        except Exception:
            pass
        try:
            await asyncio.to_thread(self.sandbox.delete)
        except Exception:
            pass


_PREVIEW_TOKEN_HEADER = "x-daytona-preview-token"


def _resolve_cdp_ws_url(cdp_preview: PortPreviewUrl) -> str:
    """Fetch /json/version and rebuild the WS URL against the Daytona proxy.

    Chrome advertises ws://localhost:9222/devtools/browser/<id> regardless of
    how it's reached, so connect_over_cdp(http_url) would target an
    unreachable host. We keep just the /devtools/browser/<id> path and splice
    it onto the externally-reachable preview host.
    """
    probe = cdp_preview.url.rstrip("/") + "/json/version"
    req = urllib.request.Request(probe, headers={_PREVIEW_TOKEN_HEADER: cdp_preview.token})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    path = urlparse(data["webSocketDebuggerUrl"]).path
    host = urlparse(cdp_preview.url).netloc
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


async def create_browser_session(*, timeout: int = 60) -> BrowserSession:
    """Provision a Daytona sandbox, launch stealth Chrome, connect over CDP.

    Raises RuntimeError if DAYTONA_API_KEY is unset, or if CDP doesn't come up
    within `timeout` seconds (with the launcher's stdout/stderr embedded for
    diagnosis). On any failure the sandbox is deleted before the exception
    propagates.
    """
    if not os.environ.get("DAYTONA_API_KEY"):
        raise RuntimeError("DAYTONA_API_KEY environment variable is not set.")

    daytona = Daytona()
    # First-run builds take a few minutes — stream the snapshot logs to stderr
    # so operators see progress. Stdout is reserved for the stdio-transport
    # MCP protocol, so we cannot print there.
    sandbox = await asyncio.to_thread(
        daytona.create,
        CreateSandboxFromImageParams(image=browser_image),
        timeout=0,
        on_snapshot_create_logs=lambda line: print(line, file=sys.stderr, flush=True),
    )

    pw: Playwright | None = None
    try:
        await asyncio.to_thread(sandbox.fs.upload_file, _LAUNCHER_SCRIPT.encode(), _LAUNCHER_PATH)
        await asyncio.to_thread(sandbox.process.create_session, _SESSION_ID)

        # Start the VNC desktop first — this is what computer_use streams on
        # port 6080. Chrome then launches into the same DISPLAY=:0 so its
        # window is visible in the live view.
        await asyncio.to_thread(sandbox.computer_use.start)
        # Append /vnc.html — the preview URL's root just serves a directory
        # listing of the noVNC install; /vnc.html is the actual client page.
        vnc_preview = await asyncio.to_thread(
            sandbox.create_signed_preview_url, VNC_PORT, 3600
        )
        vnc_url = vnc_preview.url.rstrip("/") + "/vnc.html"

        cmd = await asyncio.to_thread(
            sandbox.process.execute_session_command,
            _SESSION_ID,
            SessionExecuteRequest(
                command=f"DISPLAY=:0 python {_LAUNCHER_PATH}",
                run_async=True,
            ),
        )

        # Wait for chrome to bind the CDP port.
        await asyncio.sleep(15)

        # Use a non-signed preview link so the token doesn't expire mid-session
        # — signed URLs default to 60s, which is fine for the immediate CDP
        # attach below but would break any later reconnect. The token rides in
        # the x-daytona-preview-token header for both the /json/version probe
        # and the CDP WebSocket.
        cdp_preview = await asyncio.to_thread(sandbox.get_preview_link, CDP_PORT)
        cdp_headers = {_PREVIEW_TOKEN_HEADER: cdp_preview.token}

        pw = await async_playwright().start()

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        last_err: Exception | None = None
        while loop.time() < deadline:
            try:
                ws_url = await asyncio.to_thread(_resolve_cdp_ws_url, cdp_preview)
                browser = await pw.chromium.connect_over_cdp(ws_url, headers=cdp_headers)
                ctx = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return BrowserSession(
                    sandbox=sandbox,
                    playwright=pw,
                    browser=browser,
                    page=page,
                    vnc_url=vnc_url,
                )
            except Exception as e:
                last_err = e
                await asyncio.sleep(2)

        launcher_logs = await asyncio.to_thread(_tail_launcher_logs, sandbox, cmd)
        raise RuntimeError(
            f"Browser failed to start within {timeout}s. Last error: {last_err}\n"
            f"Launcher logs:\n{launcher_logs}"
        )

    except Exception:
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
        try:
            await asyncio.to_thread(sandbox.delete)
        except Exception:
            pass
        raise
