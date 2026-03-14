"""
card_html_renderer.py — ATLAS HTML→PNG Card Renderer
═══════════════════════════════════════════════════════
Renders HTML card templates to PNG screenshots using Playwright (headless Chromium).
Produces browser-quality text rendering that stays crisp at Discord's display scale.

Uses a temp file + page.goto() instead of set_content() so that file:// URLs
for fonts and images load correctly.

Usage:
    from card_html_renderer import render_card_png

    png_bytes = await render_card_png(html_string, width=700)
    file = discord.File(io.BytesIO(png_bytes), filename="card.png")
"""

import asyncio
import os
import tempfile
from pathlib import Path

_BROWSER = None
_BROWSER_LOCK = asyncio.Lock()
_TEMP_DIR = Path(tempfile.gettempdir()) / "atlas_cards"


async def _get_browser():
    """Lazy-init a persistent browser instance. Reused across renders."""
    global _BROWSER
    async with _BROWSER_LOCK:
        if _BROWSER is None or not _BROWSER.is_connected():
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            _BROWSER = await pw.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
            )
    return _BROWSER


async def render_card_png(
    html: str,
    width: int = 700,
    selector: str = ".card",
    device_scale: float = 2.0,
) -> bytes:
    """
    Render an HTML string to PNG bytes by screenshotting a DOM element.

    Writes HTML to a temp file and navigates to it (instead of set_content)
    so that file:// URLs for @font-face and <img> sources load correctly.

    Args:
        html: Complete HTML document string (with <style>, fonts, etc.)
        width: Viewport width in CSS pixels
        selector: CSS selector of the element to screenshot
        device_scale: Device pixel ratio (2.0 = retina, crisp on Discord)

    Returns:
        PNG image as bytes
    """
    _TEMP_DIR.mkdir(exist_ok=True)
    tmp_path = _TEMP_DIR / f"card_{os.getpid()}.html"
    tmp_path.write_text(html, encoding="utf-8")

    browser = await _get_browser()
    page = await browser.new_page(
        viewport={"width": width, "height": 1200},
        device_scale_factor=device_scale,
    )
    try:
        file_url = "file:///" + str(tmp_path.resolve()).replace("\\", "/")
        await page.goto(file_url, wait_until="networkidle")
        element = await page.query_selector(selector)
        if element is None:
            raise ValueError(f"Selector '{selector}' not found in HTML")
        png_bytes = await element.screenshot(type="png")
        return png_bytes
    finally:
        await page.close()
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def render_card_png_sync(html: str, **kwargs) -> bytes:
    """Synchronous wrapper for use in thread executors."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(render_card_png(html, **kwargs))
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(asyncio.run, render_card_png(html, **kwargs))
        return future.result(timeout=15)


async def cleanup_browser():
    """Call on bot shutdown to cleanly close the browser."""
    global _BROWSER
    if _BROWSER and _BROWSER.is_connected():
        await _BROWSER.close()
        _BROWSER = None
