"""Playwright Chromium 浏览器池与跨平台中文字体定位。"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from pathlib import Path

from .config import TEMPLATE_DIR
from .logging_util import logger

FONT_WEIGHTS = {
    "Regular": 400,
    "Medium": 500,
    "Semibold": 600,
    "Bold": 700,
    "Heavy": 900,
}

SYSTEM_CJK_FALLBACKS = [
    # Linux 常见中文字体
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    # Windows 常见中文字体
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    # macOS 常见中文字体
    Path("/System/Library/Fonts/PingFang.ttc"),
]

SYSTEM_CJK_BOLD_FALLBACKS = [
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
]


def _find_font(weight: str, font_dir: str) -> Path | None:
    search_dirs: list[Path] = []
    if font_dir:
        search_dirs.append(Path(font_dir))
    search_dirs.append(TEMPLATE_DIR / "fonts")

    for directory in search_dirs:
        candidate = directory / f"MiSans-{weight}.ttf"
        if candidate.is_file():
            return candidate

    if weight in ("Semibold", "Bold", "Heavy"):
        for candidate in SYSTEM_CJK_BOLD_FALLBACKS:
            if candidate.is_file():
                return candidate

    for candidate in SYSTEM_CJK_FALLBACKS:
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=8)
def build_font_css(font_dir: str = "") -> str:
    rules: list[str] = []
    for weight_name, weight_num in FONT_WEIGHTS.items():
        found = _find_font(weight_name, font_dir)
        if found is not None:
            rules.append(
                f"@font-face{{font-family:Haku;src:url('{found.resolve().as_uri()}');font-weight:{weight_num};}}"
            )
    return "\n".join(rules)


def _chrome_user_agent(version: str) -> str:
    # 默认 UA 带 HeadlessChrome，部分 CDN/Cloudflare 会拦下模板里的远程图片
    major = version.split(".")[0]
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


class BrowserPool:
    def __init__(self, max_contexts: int = 5, idle_seconds: int = 300):
        self._slots = asyncio.Semaphore(max_contexts)
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._idle: asyncio.Task | None = None
        self._active = 0
        self._idle_seconds = idle_seconds
        self._browsers_path: str = ""

    def set_browsers_path(self, path: str) -> None:
        self._browsers_path = path.strip()

    @asynccontextmanager
    async def page(self, *, device_scale_factor: float = 2, **options) -> AsyncIterator:
        from playwright.async_api import async_playwright

        async with self._slots:
            context = None
            acquired = False
            try:
                async with self._lock:
                    if self._idle is not None:
                        self._idle.cancel()
                        self._idle = None
                    if self._browser is None or not self._browser.is_connected():
                        if self._playwright is None:
                            if self._browsers_path:
                                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self._browsers_path
                            self._playwright = await async_playwright().start()
                        self._browser = await self._playwright.chromium.launch(
                            headless=True,
                            args=["--no-sandbox", "--disable-setuid-sandbox"],
                        )
                    self._active += 1
                    acquired = True
                    browser = self._browser
                context = await browser.new_context(
                    device_scale_factor=device_scale_factor,
                    **{"user_agent": _chrome_user_agent(browser.version), **options},
                )
                yield await context.new_page()
            finally:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    if acquired:
                        async with self._lock:
                            self._active -= 1
                            if self._active == 0:
                                self._idle = asyncio.create_task(self._close_idle())

    async def _close_idle(self) -> None:
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(self._idle_seconds)
            async with self._lock:
                if self._active == 0:
                    await self._close_resources()

    async def _close_resources(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception as exc:
            logger.debug(f"[cs_stats] 关闭浏览器异常: {exc}")
        finally:
            self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.debug(f"[cs_stats] 停止 Playwright 异常: {exc}")
                self._playwright = None

    async def close(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            await asyncio.gather(self._idle, return_exceptions=True)
            self._idle = None
        async with self._lock:
            await self._close_resources()
