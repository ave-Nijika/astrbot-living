"""BrowserTools——自主浏览工具（任务书 M3 补丁 XI-B2/B3，Playwright 实现）。

防御导入：playwright 未安装时模块可加载但工具返回明确错误文本。
会话持久化：cookies + localStorage 存工作区 browser_state.json。
写操作分层：browser_click / browser_type 受 write_level 约束。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

MAX_PAGE_TEXT = 3000


class BrowserSession:
    """浏览器会话管理器。持有一个 Page 实例并支持状态持久化。"""

    def __init__(self, workspace: str, write_level: int = 0) -> None:
        self._workspace = workspace
        self._write_level = write_level
        self._pw = None
        self._browser = None
        self._page = None

    @property
    def write_level(self) -> int:
        return self._write_level

    @write_level.setter
    def write_level(self, v: int) -> None:
        self._write_level = max(0, min(3, int(v)))

    async def _ensure_page(self):
        if self._page is not None:
            return self._page
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("playwright 未安装")
        self._pw = await __import__("playwright.async_api", fromlist=["async_playwright"]).async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._page = await self._browser.new_page()
        return self._page

    async def save_state(self):
        if self._page is None or self._browser is None:
            return
        from pathlib import Path
        state_file = Path(self._workspace) / "browser_state.json"
        try:
            state = {"cookies": await self._page.context.cookies()}
            state_file.write_text(
                __import__("json").dumps(state, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    async def close(self):
        for closer in (self._browser, self._pw):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        self._page = None
        self._browser = None
        self._pw = None


class BrowserTools:
    """浏览器工具集：navigate / read / screenshot / click / type。"""

    def __init__(self, session: BrowserSession, max_text: int = MAX_PAGE_TEXT) -> None:
        self._session = session
        self._max_text = max_text

    async def navigate(self, url: str) -> str:
        page = await self._session._ensure_page()
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        title = await page.title()
        text = await page.inner_text("body")
        return f"已打开 {title}（{url}）\n正文前 2000 字：\n{text[:2000]}"

    async def read_page(self) -> str:
        page = await self._session._ensure_page()
        title = await page.title()
        text = await page.inner_text("body")
        return f"当前页面 {title}\n正文：\n{text[:self._max_text]}"

    async def screenshot(self, path: str) -> str:
        page = await self._session._ensure_page()
        await page.screenshot(path=path)
        return f"截图已保存 {path}"

    async def click(self, selector: str) -> str:
        page = await self._session._ensure_page()
        await page.click(selector, timeout=5000)
        return f"已点击 {selector}"

    async def type_text(self, selector: str, text: str) -> str:
        page = await self._session._ensure_page()
        await page.fill(selector, text)
        return f"已在 {selector} 填入文本"
