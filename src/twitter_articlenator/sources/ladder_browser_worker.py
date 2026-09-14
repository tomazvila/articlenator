"""One disposable browser process per 13ft fallback request."""

import json
import sys
import time
from pathlib import Path

from botasaurus_driver import Driver
from playwright.sync_api import sync_playwright

from .ladder_http import MAX_BYTES


def main():
    url, output, proxy = sys.argv[1:]
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
    driver = Driver(
        chrome_executable_path=executable,
        enable_xvfb_virtual_display=sys.platform.startswith("linux"),
        headless=False,
        profile=str(Path(output).parent / "profile"),
        wait_for_complete_page_load=False,
        arguments=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-quic",
            f"--proxy-server={proxy}",
            "--proxy-bypass-list=<-loopback>",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
    )
    try:
        driver.get(url, bypass_cloudflare=True, timeout=18)
        # Give client-rendered pages time to populate their article body.
        for _ in range(12):
            data = driver.run_js("""return {
                html: document.documentElement.outerHTML,
                url: location.href,
                ready: !!document.querySelector('article p, main p, [data-testid^="paragraph-"]')
            }""")
            if data and data["ready"]:
                break
            time.sleep(1)
        if data:
            encoded = json.dumps({"html": data["html"], "url": data["url"]})
            if len(encoded.encode()) <= MAX_BYTES:
                Path(output).write_text(encoded)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
