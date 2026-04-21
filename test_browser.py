#!/usr/bin/env python3
"""
Test script for Daytona Playwright MCP Server

Tests browser_start, navigation, and screenshots by directly using
the underlying implementation (bypassing the MCP tool wrappers).
"""

import os
import sys
import json
import time
import urllib.request
from urllib.parse import urlparse

# Ensure we have the API key
API_KEY = os.environ.get("DAYTONA_API_KEY")
if not API_KEY:
    print("Error: DAYTONA_API_KEY environment variable not set")
    sys.exit(1)

print("API key found. Starting tests...")

# Import dependencies
from daytona_sdk import Daytona, DaytonaConfig, SessionExecuteRequest
from patchright.sync_api import sync_playwright

# Constants
CDP_PORT = 9222
PROXY_PORT = 9223  # We use a Python TCP proxy to expose CDP on 0.0.0.0
_SESSION_ID = "enable-browser"
_LAUNCHER_PATH = "/tmp/_enable_browser_launcher.py"
_PROXY_PATH = "/tmp/_tcp_proxy.py"

# TCP proxy script (forwards 0.0.0.0:9223 -> 127.0.0.1:9222)
_PROXY_SCRIPT = '''
import socket
import threading
import sys

LOCAL_PORT = 9222
PROXY_PORT = 9223

def forward(source, destination):
    try:
        while True:
            data = source.recv(4096)
            if not data:
                break
            destination.sendall(data)
    except:
        pass
    finally:
        source.close()
        destination.close()

def handle_client(client_socket):
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.connect(("127.0.0.1", LOCAL_PORT))
        t1 = threading.Thread(target=forward, args=(client_socket, server))
        t2 = threading.Thread(target=forward, args=(server, client_socket))
        t1.daemon = True
        t2.daemon = True
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        client_socket.close()

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PROXY_PORT))
    server.listen(5)
    print(f"TCP proxy listening on 0.0.0.0:{PROXY_PORT}", file=sys.stderr)
    while True:
        client, addr = server.accept()
        t = threading.Thread(target=handle_client, args=(client,))
        t.daemon = True
        t.start()

if __name__ == "__main__":
    main()
'''

# Launcher script
_LAUNCHER_SCRIPT = f'''
import signal
import subprocess
import os
import sys
import time

os.makedirs("/home/daytona/.browser-profile", exist_ok=True)

chromium_args = [
    "chromium",
    "--user-data-dir=/home/daytona/.browser-profile",
    "--remote-debugging-port={CDP_PORT}",
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

time.sleep(5)

print("Starting TCP proxy...", file=sys.stderr)
proxy_proc = subprocess.Popen(["python", "{_PROXY_PATH}"], stdout=subprocess.DEVNULL, stderr=sys.stderr)
print(f"Proxy PID: {{proxy_proc.pid}}", file=sys.stderr)

signal.pause()
'''


def resolve_cdp_ws_url(preview_url: str) -> str:
    """Fetch /json/version and rebuild the WS URL against the Daytona proxy."""
    probe = preview_url.rstrip("/") + "/json/version"
    print(f"    Probing: {probe}")
    with urllib.request.urlopen(probe, timeout=10) as r:
        data = json.load(r)
        print(f"    CDP Version: {data.get('Browser', 'unknown')}")
        path = urlparse(data["webSocketDebuggerUrl"]).path
        host = urlparse(preview_url).netloc
        return f"wss://{host}{path}"


def main():
    print("=" * 60)
    print("Daytona Playwright MCP Server Test")
    print("=" * 60)

    sandbox = None
    browser = None
    pw = None

    try:
        # Test 1: Create Daytona sandbox
        print("\n[Test 1] Creating Daytona sandbox...")
        config = DaytonaConfig(api_key=API_KEY)
        daytona = Daytona(config)

        sandbox = daytona.create(timeout=120)
        print(f"✓ Sandbox created: {sandbox.id}")

        # Test 2: Upload scripts and start browser
        print("\n[Test 2] Uploading scripts and starting browser...")
        sandbox.fs.upload_file(_PROXY_SCRIPT.encode(), _PROXY_PATH)
        sandbox.fs.upload_file(_LAUNCHER_SCRIPT.encode(), _LAUNCHER_PATH)
        sandbox.process.create_session(_SESSION_ID)

        cmd = sandbox.process.execute_session_command(
            _SESSION_ID,
            SessionExecuteRequest(
                command=f"Xvfb :99 -screen 0 1920x1080x24 & export DISPLAY=:99 && sleep 2 && python {_LAUNCHER_PATH}",
                run_async=True,
            ),
        )
        print("✓ Browser launcher started")

        # Test 3: Get signed preview URL and connect
        print("\n[Test 3] Getting signed preview URL and connecting via CDP...")

        # Wait for browser and proxy to start
        print("    Waiting 15 seconds for browser to start...")
        time.sleep(15)

        # Use create_signed_preview_url instead of get_preview_link
        signed = sandbox.create_signed_preview_url(PROXY_PORT)
        signed_url = signed.url
        print(f"    Signed URL: {signed_url}")

        pw = sync_playwright().start()

        # Connect with retry
        deadline = time.monotonic() + 60
        last_err = None

        while time.monotonic() < deadline:
            try:
                ws_url = resolve_cdp_ws_url(signed_url)
                print(f"    WebSocket URL: {ws_url}")
                browser = pw.chromium.connect_over_cdp(ws_url)
                print("✓ Connected to browser via CDP!")
                break
            except Exception as e:
                last_err = e
                print(f"    Waiting... ({type(e).__name__})")
                time.sleep(3)

        if not browser:
            print(f"✗ Failed to connect: {last_err}")
            return 1

        # Get a page
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("✓ Got browser page")

        # Test 4: Navigate to example.com
        print("\n[Test 4] Navigating to example.com...")
        page.goto("https://example.com", wait_until="load")
        print(f"✓ Navigated to: {page.url}")
        print(f"  Page title: {page.title()}")

        # Test 5: Take a screenshot
        print("\n[Test 5] Taking screenshot...")
        os.makedirs("/tmp/logs", exist_ok=True)
        screenshot_bytes = page.screenshot()
        screenshot_path = "/tmp/logs/screenshot_example.png"
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)
        print(f"✓ Screenshot saved to: {screenshot_path}")
        print(f"  Size: {len(screenshot_bytes)} bytes")

        # Test 6: Get page text
        print("\n[Test 6] Getting page text...")
        text = page.inner_text("body")
        print(f"✓ Page text (first 200 chars):\n{text[:200]}...")

        # Test 7: Navigate to another site
        print("\n[Test 7] Navigating to httpbin.org...")
        page.goto("https://httpbin.org", wait_until="load")
        print(f"✓ Navigated to: {page.url}")

        # Test 8: Take another screenshot
        print("\n[Test 8] Taking screenshot of httpbin.org...")
        screenshot_bytes = page.screenshot()
        screenshot_path = "/tmp/logs/screenshot_httpbin.png"
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)
        print(f"✓ Screenshot saved to: {screenshot_path}")
        print(f"  Size: {len(screenshot_bytes)} bytes")

        # Test 9: Full page screenshot
        print("\n[Test 9] Taking full-page screenshot...")
        screenshot_bytes = page.screenshot(full_page=True)
        screenshot_path = "/tmp/logs/screenshot_fullpage.png"
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)
        print(f"✓ Full-page screenshot saved to: {screenshot_path}")
        print(f"  Size: {len(screenshot_bytes)} bytes")

        print("\n" + "=" * 60)
        print("All tests PASSED!")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        # Cleanup
        print("\n[Cleanup] Stopping browser and deleting sandbox...")
        if browser:
            try:
                browser.close()
                print("  Browser closed")
            except Exception as e:
                print(f"  Error closing browser: {e}")

        if pw:
            try:
                pw.stop()
                print("  Playwright stopped")
            except Exception as e:
                print(f"  Error stopping playwright: {e}")

        if sandbox:
            try:
                sandbox.delete()
                print("  Sandbox deleted")
            except Exception as e:
                print(f"  Error deleting sandbox: {e}")


if __name__ == "__main__":
    sys.exit(main())
