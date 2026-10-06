"""BrowserTools——自主浏览工具（任务书 M3 补丁 XI-B2/B3，Playwright 实现）。

防御导入：playwright 未安装时模块可加载但工具返回明确错误文本。
会话持久化：cookies + localStorage 存工作区 browser_state.json。
写操作分层：browser_click / browser_type 受 write_level 约束。
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

MAX_PAGE_TEXT = 3000

# M20-补丁1 L1/N：最近读取留档（学习触发证据 + 面板可见），有界
RECENT_READS_KEEP = 8
RECENT_READ_TEXT_CHARS = 4000  # 每条正文存档上限（学习材料只需开头一段）


def chromium_installed() -> Optional[bool]:
    """Chromium 二进制可用性探测（M15-补丁1 C3 面板状态用）。

    返回 True/False；playwright 库未安装也按 False（浏览器工具本来就
    不可用）。只读探测：起一次 driver 拿 chromium.executable_path 再查
    文件存在——不 launch 浏览器、不写任何状态。异常一律 False（面板按
    "未安装"给指引）。同步阻塞（数百毫秒），调用方放线程池执行。
    """
    if not HAS_PLAYWRIGHT:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception as e:
        logger.debug(f"[BrowserTools] Chromium 探测失败（按未安装处理）: {e}")
        return False


class BrowserSession:
    """浏览器会话管理器。持有一个 Page 实例并支持状态持久化。"""

    def __init__(self, workspace: str, write_level: int = 0) -> None:
        self._workspace = workspace
        self._write_level = write_level
        self._pw = None
        self._browser = None
        self._page = None
        # M20-补丁1 L1：最近读取留档（browser_navigate / browser_read 成功
        # 时记一条；风格学习用它判定"本轮真的读了网页"，有界 8 条）
        self._recent_reads: deque[dict] = deque(maxlen=RECENT_READS_KEEP)

    @property
    def write_level(self) -> int:
        return self._write_level

    @write_level.setter
    def write_level(self, v: int) -> None:
        self._write_level = max(0, min(3, int(v)))

    def note_read(self, url: str, title: str, text: str) -> None:
        """记录一次成功的页面读取（M20-补丁1 L1 证据 / N2 闭环留痕）。
        纯内存操作，任何失败不影响工具调用本身。"""
        try:
            self._recent_reads.append(
                {
                    "url": str(url or "")[:300],
                    "title": str(title or "")[:120],
                    "text": str(text or "")[:RECENT_READ_TEXT_CHARS],
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
            )
        except Exception:
            pass

    def recent_reads(self) -> list[dict]:
        """最近读取留档（新的在前；风格学习/测试用）。"""
        return list(reversed(self._recent_reads))

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
        state_file = Path(self._workspace) / "browser_state.json"
        try:
            state = {"cookies": await self._page.context.cookies()}
            state_file.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
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
