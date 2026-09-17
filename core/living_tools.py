"""生活工具集——把五能力包装成 agent 循环可调用的 FunctionTool。

定义模式与 AstrBot 内置工具（web_search_tools.py 的 @pydantic_dataclass
子类化 FunctionTool、覆写 call()）一致：call(context, **kwargs) 返回字符串。
工具只做"能力的薄包装"，不做任何决策——决策在 decider，玩法在 LLM。
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

import tempfile

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool, ToolSet, ToolExecResult

FETCH_TEXT_CHARS = 1500  # 喂给 LLM 的正文上限：够读，不至于撑爆上下文


@pydantic_dataclass
class WebSearchTool(FunctionTool):
    """网页搜索（复用 C1 BochaSearcher）。"""

    name: str = "web_search"
    description: str = "搜索网络，返回若干条带标题、链接和摘要的结果。用于查资料、看新鲜事。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        }
    )

    def __post_init__(self) -> None:
        # pydantic dataclass 不接受额外字段的直接赋值以外的方式？直接存即可
        pass

    async def call(self, context, **kwargs) -> ToolExecResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return "错误：query 不能为空"
        results = await self._searcher.search(query, count=5)
        if not results:
            return f"搜索「{query}」没有结果"
        lines = [
            f"{i}. {r.get('title', '')} | {r.get('url', '')}\n   {r.get('summary', '')[:200]}"
            for i, r in enumerate(results, 1)
        ]
        return "搜索结果：\n" + "\n".join(lines)

    _searcher: Any = None

    def bind(self, searcher: Any) -> "WebSearchTool":
        self._searcher = searcher
        return self


@pydantic_dataclass
class FetchPageTool(FunctionTool):
    """网页抓取（复用 C2 WebFetcher），返回标题与正文前若干字。"""

    name: str = "fetch_page"
    description: str = (
        f"抓取一个网页，返回标题和正文前 {FETCH_TEXT_CHARS} 字。用于把搜索结果真的读一遍。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "网页 URL"},
            },
            "required": ["url"],
        }
    )

    _fetcher: Any = None

    def bind(self, fetcher: Any) -> "FetchPageTool":
        self._fetcher = fetcher
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        url = str(kwargs.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return "错误：需要完整的 http(s) URL"
        page = await self._fetcher.fetch(url)
        text = (page.get("text") or "").strip()
        if not text:
            return f"页面《{page.get('title', '')}》没有可读正文"
        return (
            f"《{page.get('title', '')}》\n"
            + text[:FETCH_TEXT_CHARS]
            + ("…（正文已截断）" if len(text) > FETCH_TEXT_CHARS else "")
        )


@pydantic_dataclass
class RunPythonTool(FunctionTool):
    """沙箱代码执行（复用 C3 Sandbox）——LLM 现场写小游戏/小工具自己玩。"""

    name: str = "run_python"
    description: str = (
        "在受限沙箱里运行一段 Python 代码（秒级完成），返回 stdout。"
        "可用的库只有 random/math/time/datetime/json/re/itertools/collections。"
        "适合写小游戏玩、算点东西、生成文字节目。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "完整的 Python 源码"},
            },
            "required": ["code"],
        }
    )

    _sandbox: Any = None

    def bind(self, sandbox: Any) -> "RunPythonTool":
        self._sandbox = sandbox
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        code = str(kwargs.get("code", ""))
        if not code.strip():
            return "错误：code 不能为空"
        result = await self._sandbox.run(code, timeout=10)
        if result.get("refused_reason"):
            return f"代码被沙箱拒绝：{result['refused_reason']}"
        parts = []
        if result.get("stdout"):
            parts.append(f"stdout:\n{result['stdout']}")
        if result.get("stderr"):
            parts.append(f"stderr:\n{result['stderr']}")
        if not parts:
            parts.append(f"代码执行完成，无输出（exit={result.get('exit_code')}）")
        return "\n".join(parts)


@pydantic_dataclass
class RememberTool(FunctionTool):
    """记忆写入（复用 MemoryBackend）——agent 自己把"今天的事"记下来。"""

    name: str = "remember"
    description: str = (
        "把一件今天发生的事或一个想法写进自己的长期记忆，用第一人称一句话。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "第一人称的一句话记忆"},
                "importance": {
                    "type": "number",
                    "description": "重要度 0~1，默认 0.5",
                },
            },
            "required": ["text"],
        }
    )

    _memory_getter: Callable[..., Any] | None = None

    def bind(self, memory_getter: Callable[..., Any]) -> "RememberTool":
        self._memory_getter = memory_getter
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        text = str(kwargs.get("text", "")).strip()
        if not text:
            return "错误：text 不能为空"
        try:
            importance = float(kwargs.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        memory = await self._memory_getter()
        doc_id = await memory.add(text, importance=importance)
        return f"已记住（id={doc_id}）：{text[:50]}"


def build_living_tools(
    searcher: Any = None,
    fetcher: Any = None,
    sandbox: Any = None,
    memory_getter: Callable[..., Any] | None = None,
    tier: int = 0,
    write_level: int = 0,
    workspace: str = "",
    browser_session: Any = None,
) -> ToolSet:
    """按能力档位装配 ToolSet（任务书 M3 补丁 XI-B1/B2）。

    tier 决定挂载哪些工具，write_level 决定写操作权限，
    workspace 限制文件操作目录。每次活动周期重建（配置热读）。

    tier 0: 仅自带 4 工具
    tier >= 1: + 浏览器只读工具（navigate/read/screenshot）
    tier >= 2: + 工作区写入工具
    """
    tools: list[FunctionTool] = []

    search_tool = WebSearchTool()
    if searcher is not None:
        search_tool.bind(searcher)
        tools.append(search_tool)

    fetch_tool = FetchPageTool()
    if fetcher is not None:
        fetch_tool.bind(fetcher)
        tools.append(fetch_tool)

    if sandbox is not None:
        sandbox_tool = RunPythonTool()
        sandbox_tool.bind(sandbox)
        tools.append(sandbox_tool)

    if memory_getter is not None:
        remember_tool = RememberTool()
        remember_tool.bind(memory_getter)
        tools.append(remember_tool)

    # tier >= 1: 浏览器工具（类定义在本文件后半部，运行时可直接引用）
    if tier >= 1 and browser_session is not None:
        try:
            tools.append(BrowserNavigateTool().bind_session(browser_session))
            tools.append(BrowserReadTool().bind_session(browser_session))
            tools.append(BrowserScreenshotTool().bind_session(browser_session))
            tools.append(BrowserClickTool().bind_session(browser_session, write_level))
            tools.append(BrowserTypeTool().bind_session(browser_session, write_level))
        except Exception as e:
            logger.warning(f"浏览器工具加载失败（不影响其他工具）: {e}", exc_info=True)

    return ToolSet(tools=tools)


# ---------------------------------------------------------------------------
# 浏览器 FunctionTool 包装（任务书 M3 补丁 XI-B2）
# ---------------------------------------------------------------------------

class BrowserSessionRef:
    """浏览器会话引用（跨工具共享同一 BrowserSession 实例）。"""
    def __init__(self, session):
        self.session = session
        self.write_level = 0


@pydantic_dataclass
class BrowserNavigateTool(FunctionTool):
    name: str = "browser_navigate"
    description: str = "打开网页，返回标题和正文摘要。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "目标 URL"}},
        "required": ["url"],
    })
    _session_ref: Any = None

    def bind_session(self, ref) -> "BrowserNavigateTool":
        self._session_ref = ref
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        url = str(kwargs.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return "错误：需要 http(s) URL"
        page = await self._session_ref.session._ensure_page()
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        title = await page.title()
        text = await page.inner_text("body")
        await self._session_ref.session.save_state()
        result = "已打开「{}」（{}）".format(title, url)
        body = text[:2000]
        return result + "\n" + body


@pydantic_dataclass
class BrowserReadTool(FunctionTool):
    name: str = "browser_read"
    description: str = "读取当前网页的标题和正文内容。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object", "properties": {},
    })
    _session_ref: Any = None

    def bind_session(self, ref) -> "BrowserReadTool":
        self._session_ref = ref
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        page = await self._session_ref.session._ensure_page()
        title = await page.title()
        text = await page.inner_text("body")
        return "「{}」\n{}".format(title, text[:self._max_text])


@pydantic_dataclass
class BrowserScreenshotTool(FunctionTool):
    name: str = "browser_screenshot"
    description: str = "截取当前网页的屏幕截图并保存。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object", "properties": {},
    })
    _session_ref: Any = None

    def bind_session(self, ref) -> "BrowserScreenshotTool":
        self._session_ref = ref
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        page = await self._session_ref.session._ensure_page()
        import os as _os
        path = _os.path.join(tempfile.gettempdir(), "living_screenshot.png")
        await page.screenshot(path=path)
        return f"截图已保存 {path}"


@pydantic_dataclass
class BrowserClickTool(FunctionTool):
    name: str = "browser_click"
    description: str = "点击网页上的元素（按钮/链接等）。需要 write_level >= 1。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {"selector": {"type": "string", "description": "CSS 选择器"}},
        "required": ["selector"],
    })
    _session_ref: Any = None

    def bind_session(self, ref, write_level: int = 0) -> "BrowserClickTool":
        self._session_ref = ref
        self._write_level = write_level
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        selector = str(kwargs.get("selector", "")).strip()
        if not selector:
            return "错误：selector 不能为空"
        page = await self._session_ref.session._ensure_page()
        await page.click(selector, timeout=5000)
        return f"已点击 {selector}"


@pydantic_dataclass
class BrowserTypeTool(FunctionTool):
    name: str = "browser_type"
    description: str = "在网页输入框中填入文本。需要 write_level >= 1。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "输入框 CSS 选择器"},
            "text": {"type": "string", "description": "要输入的文本"},
        },
        "required": ["selector", "text"],
    })
    _session_ref: Any = None

    def bind_session(self, ref, write_level: int = 0) -> "BrowserTypeTool":
        self._session_ref = ref
        self._write_level = write_level
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        selector = str(kwargs.get("selector", "")).strip()
        text = str(kwargs.get("text", ""))
        if not selector:
            return "错误：selector 不能为空"
        page = await self._session_ref.session._ensure_page()
        await page.fill(selector, text)
        return f"已在 {selector} 填入 {len(text)} 字"
