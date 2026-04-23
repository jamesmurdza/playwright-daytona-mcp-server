#!/usr/bin/env python3
"""
Smoke test for Daytona Playwright MCP Server.

Exercises create_browser_session() end-to-end — image build, sandbox boot,
chrome launch, CDP attach, then drives the returned page against a couple of
real sites. Useful for iterating on the sandbox setup without going through
an MCP client.
"""

import asyncio
import os
import sys

from daytona_playwright_mcp.browser import create_browser_session


async def main() -> int:
    print("=" * 60)
    print("Daytona Playwright MCP Server Test")
    print("=" * 60)

    print("\n[Test 1] Creating browser session (image build + chrome launch)...")
    try:
        session = await create_browser_session(timeout=120)
    except Exception as e:
        print(f"✗ Failed to create session: {e}")
        return 1
    print(f"✓ Sandbox: {session.sandbox.id}")
    print(f"  Live view: {session.vnc_url}")

    page = session.page
    os.makedirs("/tmp/logs", exist_ok=True)

    try:
        print("\n[Test 2] Navigating to example.com...")
        await page.goto("https://example.com", wait_until="load")
        print(f"✓ Navigated to: {page.url}")
        print(f"  Page title: {await page.title()}")

        print("\n[Test 3] Screenshot of example.com...")
        shot = await page.screenshot()
        path = "/tmp/logs/screenshot_example.png"
        with open(path, "wb") as f:
            f.write(shot)
        print(f"✓ Saved {path} ({len(shot)} bytes)")

        print("\n[Test 4] Inner text of body...")
        text = await page.inner_text("body")
        print(f"✓ First 200 chars:\n{text[:200]}...")

        print("\n[Test 5] Navigating to httpbin.org...")
        await page.goto("https://httpbin.org", wait_until="load")
        print(f"✓ Navigated to: {page.url}")

        print("\n[Test 6] Full-page screenshot of httpbin.org...")
        shot = await page.screenshot(full_page=True)
        path = "/tmp/logs/screenshot_httpbin_fullpage.png"
        with open(path, "wb") as f:
            f.write(shot)
        print(f"✓ Saved {path} ({len(shot)} bytes)")

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
        print("\n[Cleanup] Closing session...")
        await session.close()
        print("  Done")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
