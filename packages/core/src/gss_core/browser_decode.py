"""Optional Playwright/Chromium decode layer for Route C.

Default anonymous mode never receives the account's ChatGPT cookies.  It only
collects the browser's native environment and public risk cookies; callers merge
those into the order-proxy client afterwards.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .browser_context import BrowserContext

logger = logging.getLogger("gss.browser_decode")


class BrowserDecodeUnavailable(RuntimeError):
    """Playwright/browser failure.  Do not include command lines or cookies."""


class BrowserDecoder(Protocol):
    async def decode(self, *, target_url: str, timeout_ms: int = 30000) -> BrowserContext:
        ...


@dataclass
class FakeBrowserDecoder:
    context: BrowserContext

    async def decode(self, *, target_url: str, timeout_ms: int = 30000) -> BrowserContext:
        return self.context


@dataclass
class NullBrowserDecoder:
    async def decode(self, *, target_url: str, timeout_ms: int = 30000) -> BrowserContext:
        raise BrowserDecodeUnavailable("browser_decode_unavailable")


def _brands_js() -> str:
    return """() => {
      const d = navigator.userAgentData;
      const brands = d && Array.isArray(d.brands) ? d.brands : [];
      return {
        ua: navigator.userAgent,
        language: navigator.language || 'en-US',
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'America/New_York',
        platform: (d && d.platform) || '',
        sec_ch_ua: brands.map(b => `"${b.brand}";v="${b.version}"`).join(', '),
        w: window.innerWidth || 1440,
        h: window.innerHeight || 900,
        dpr: window.devicePixelRatio || 1
      };
    }"""


@dataclass
class PlaywrightBrowserDecoder:
    headless: bool = True
    timeout_ms: int = 30000
    target_url: str = "https://chatgpt.com/"
    auth: bool = False
    user_data_dir: str | None = None

    async def decode(self, *, target_url: str = "", timeout_ms: int | None = None) -> BrowserContext:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # noqa: BLE001
            raise BrowserDecodeUnavailable("browser_decode_unavailable") from exc
        timeout = int(timeout_ms or self.timeout_ms)
        url = str(target_url or self.target_url or "https://chatgpt.com/")
        browser = None
        context = None
        pw = None
        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()
            await page.goto(url, wait_until="commit", timeout=timeout)
            try:
                await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
            data = await page.evaluate(_brands_js())
            ua = str(data.get("ua") or "")
            sec = str(data.get("sec_ch_ua") or "")
            if "HeadlessChrome" in ua:
                ua = ua.replace("HeadlessChrome", "Chrome")
            if "HeadlessChrome" in sec:
                sec = sec.replace("HeadlessChrome", "Google Chrome")
            # Playwright's unbranded Chromium reports only Chromium in the
            # client hints while the UA says Chrome/<major>. Align the pair.
            match = re.search(r"Chrome/(\d+)", ua)
            if match and "Google Chrome" not in sec and "HeadlessChrome" not in ua:
                sec = (sec + ', "Google Chrome";v="' + match.group(1) + '"').strip(", ")
            cookies = {c["name"]: c["value"] for c in await context.cookies() if c.get("name") and c.get("value") is not None}
            return BrowserContext(
                user_agent=ua,
                sec_ch_ua=sec,
                sec_ch_ua_platform=str(data.get("platform") or ""),
                locale=str(data.get("language") or "en-US"),
                timezone_id=str(data.get("timezone") or "America/New_York"),
                viewport_width=int(data.get("w") or 1440),
                viewport_height=int(data.get("h") or 900),
                device_scale_factor=float(data.get("dpr") or 1.0),
                cookies=cookies,
                source="playwright",
            )
        except BrowserDecodeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser_decode_failed type=%s", type(exc).__name__)
            raise BrowserDecodeUnavailable("browser_decode_unavailable") from exc
        finally:
            for item in (context, browser):
                if item is not None:
                    try:
                        await item.close()
                    except Exception:  # noqa: BLE001
                        pass
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:  # noqa: BLE001
                    pass


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "BrowserContext",
    "BrowserDecoder",
    "BrowserDecodeUnavailable",
    "FakeBrowserDecoder",
    "NullBrowserDecoder",
    "PlaywrightBrowserDecoder",
    "playwright_available",
]
