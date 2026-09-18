"""生活工具集——把五能力包装成 agent 循环可调用的 FunctionTool。

定义模式与 AstrBot 内置工具（web_search_tools.py 的 @pydantic_dataclass
子类化 FunctionTool、覆写 call()）一致：call(context, **kwargs) 返回字符串。
工具只做"能力的薄包装"，不做任何决策——决策在 decider，玩法在 LLM。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

import asyncio
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool, ToolSet, ToolExecResult

from .autonomy import check_action_kind

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

    # tier >= 1: 浏览器工具
    if tier >= 1 and browser_session is not None:
        try:
            tools.append(BrowserNavigateTool().bind_session(browser_session))
            tools.append(BrowserReadTool().bind_session(browser_session))
            tools.append(BrowserScreenshotTool().bind_session(browser_session))
            tools.append(BrowserClickTool().bind_session(browser_session, write_level))
            tools.append(BrowserTypeTool().bind_session(browser_session, write_level))
        except Exception as e:
            logger.warning(f'浏览器工具加载失败（不影响其他工具）: {e}', exc_info=True)

    # tier >= 2: 工作区受限的文件工具（任务书 M3 补丁 XIV 2.1）
    if tier >= 2 and workspace:
        tools.append(WorkspaceReadTool().bind(workspace, write_level))
        tools.append(WorkspaceWriteTool().bind(workspace, write_level))
        tools.append(WorkspaceListTool().bind(workspace))

    # tier >= 3: 本机 shell（受限命令黑名单 + 红线路径拒绝）
    if tier >= 3:
        tools.append(LocalShellTool().bind(workspace, write_level))

    return ToolSet(tools=tools)


# ---------------------------------------------------------------------------
# 工作区工具（任务书 M3 补丁 XIV 2.1，tier 2 居家档）
# ---------------------------------------------------------------------------

def _resolve_inside(workspace: str, rel_path: str) -> str:
    """把相对路径解析到工作区内，防路径穿越（..）。"""
    base = str(Path(workspace).resolve())
    full = str(Path(workspace, rel_path).resolve())
    if not full.startswith(base):
        raise PermissionError(f"路径 {rel_path!r} 超出工作区")
    return full


@pydantic_dataclass
class WorkspaceReadTool(FunctionTool):
    """读工作区内文件（文本，限长）。"""

    name: str = "workspace_read"
    description: str = "读取工作区内的文本文件（限前 3000 字）。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对工作区的文件路径"}},
        "required": ["path"],
    })
    _workspace: str = ""
    _write_level: int = 0

    def bind(self, workspace: str, write_level: int = 0) -> "WorkspaceReadTool":
        self._workspace = workspace
        self._write_level = write_level
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        rel = str(kwargs.get("path", "")).strip()
        try:
            full = _resolve_inside(self._workspace, rel)
        except PermissionError:
            return f"拒绝：{rel!r} 超出工作区"
        p = Path(full)
        if not p.is_file():
            return f"文件不存在：{rel}"
        text = p.read_text(encoding="utf-8", errors="replace")[:3000]
        return f"{rel} 内容（前 3000 字）：\n{text}"


@pydantic_dataclass
class WorkspaceWriteTool(FunctionTool):
    """写工作区内文件。受 is_write_allowed + write_level >= 2 双重校验。"""

    name: str = "workspace_write"
    description: str = "在工作区内写一个文本文件。路径必须在工作区范围内。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "content": {"type": "string", "description": "要写入的文本内容"},
        },
        "required": ["path", "content"],
    })
    _workspace: str = ""
    _write_level: int = 0

    def bind(self, workspace: str, write_level: int = 0) -> "WorkspaceWriteTool":
        self._workspace = workspace
        self._write_level = write_level
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        rel = str(kwargs.get("path", "")).strip()
        text = str(kwargs.get("content", ""))
        full = str(Path(self._workspace, rel).resolve())
        if not is_write_allowed(full, self._workspace, self._write_level):
            logger.warning(f"[WorkspaceWrite] 写入被拒（红线路径或越界）: {full}")
            return f"拒绝：{rel!r} 不允许写入（工作区外或受保护路径）"
        p = Path(full)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return f"已写入 {rel}（{len(text)} 字）"


@pydantic_dataclass
class WorkspaceListTool(FunctionTool):
    """列工作区目录。"""

    name: str = "workspace_list"
    description: str = "列出工作区内指定目录的文件和子目录。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对工作区的目录路径，默认根"}},
    })
    _workspace: str = ""

    def bind(self, workspace: str) -> "WorkspaceListTool":
        self._workspace = workspace
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        rel = str(kwargs.get("path", "") or ".").strip()
        try:
            full = _resolve_inside(self._workspace, rel)
        except PermissionError:
            return f"拒绝：{rel!r} 超出工作区"
        p = Path(full)
        if not p.is_dir():
            return f"目录不存在：{rel}"
        entries = sorted(p.iterdir(), key=lambda f: f.name)[:50]
        lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries]
        return "\n".join(lines) if lines else "（空目录）"


# ---------------------------------------------------------------------------
# 本机 Shell 工具（任务书 M3 补丁 XIV 2.2 方案 B，tier 3 自由档）
# 选型理由（写进代码注释）：
#   AstrBot 内置计算机工具依赖 ComputerUseMixin + booter（local/cua/shipyard），
#   需要全局 provider_settings.computer_use_runtime 配置匹配才能激活，
#   且 @builtin_tool 条件门控依赖 pipeline 上下文。living 的 agent 循环
#   独立于 pipeline，直接挂载内置工具需要绕过多层门控，脆弱且难维护。
#   方案 B（自建受限 shell）更自洽：复用 asyncio.subprocess，保留命令
#   黑名单 + 红线路径拒绝 + 超时杀树，与 sandbox 风格一致。
# ---------------------------------------------------------------------------

# 系统级破坏命令黑名单（不可配置，硬编码安全底线）
_SHELL_BLACKLIST = re.compile(
    r"\b(rm\s+-rf|mkfs|shutdown|reboot|halt|poweroff|fdisk|format"
    r"|del\s+/[sqs]|rmdir\s+/[sq]|rd\s+/[sq]|taskkill\s+/f"
    r"|chmod\s+777|kill\s+-9\s+1\b|:\(\)\{.*\};:)\b",
    re.IGNORECASE,
)


@pydantic_dataclass
class LocalShellTool(FunctionTool):
    """本机 shell（tier 3 自由档）：受限执行，命令黑名单 + 超时杀树。"""

    name: str = "local_shell"
    description: str = (
        "在本机执行一条 shell 命令（秒级，限 30s 超时）。"
        "禁止破坏性命令（rm -rf / mkfs / shutdown 等自动拦截）。"
        "工作目录为你的专属工作区。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "shell 命令"}},
        "required": ["command"],
    })
    _workspace: str = ""
    _write_level: int = 0

    def bind(self, workspace: str, write_level: int = 0) -> "LocalShellTool":
        self._workspace = workspace
        self._write_level = write_level
        return self

    async def call(self, context, **kwargs) -> ToolExecResult:
        command = str(kwargs.get("command", "")).strip()
        if not command:
            return "错误：command 不能为空"
        if _SHELL_BLACKLIST.search(command):
            return f"拒绝：命令包含破坏性操作"
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self._workspace or None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            proc.kill()
            return "命令超时（30s），已终止"
        parts = []
        if stdout:
            parts.append(f"stdout:\n{stdout.decode('utf-8', errors='replace')[:2000]}")
        if stderr:
            parts.append(f"stderr:\n{stderr.decode('utf-8', errors='replace')[:1000]}")
        if not parts:
            parts.append(f"执行完成（exit={proc.returncode}）")
        result = "\n".join(parts)
        logger.info(f"[LocalShell] {command[:80]} → exit={proc.returncode}")
        return result


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
        # 补丁 XV 清单1：截图落工作区 screenshots/，不再写系统 temp
        try:
            workspace = str(
                getattr(self._session_ref.session, "_workspace", "") or ""
            ).strip() or str(Path.cwd())
            screenshot_dir = Path(workspace) / "screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            filename = "living_screenshot_" + datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            ) + ".png"
            path = str(screenshot_dir / filename)
            await page.screenshot(path=path)
        except Exception as e:
            # 失败保护：目录创建/写盘失败返回明确文本，不抛异常
            logger.warning(f"[browser_screenshot] 截图保存失败: {e}")
            return f"截图失败：无法写入截图目录（{e}）"
        return f"截图已保存 {path}"


@pydantic_dataclass
class BrowserClickTool(FunctionTool):
    name: str = "browser_click"
    description: str = (
        "点击网页上的元素（按钮/链接等）。需要 write_level >= 1；"
        "提交/评论/发帖等写入性质的动作必须用 action_kind 标明，"
        "标错或漏标会被权限层拒绝。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS 选择器"},
            "action_kind": {
                "type": "string",
                "description": (
                    "这次点击的操作性质：navigate(跳转)/fill(填表)/"
                    "submit_form(提交表单)/comment(评论、点赞)/post(发帖)/"
                    "message(私信)/purchase(下单)。漏标按 unknown 保守拒绝。"
                ),
            },
        },
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
        # 补丁 XV 清单4：写操作分级判定（拒绝时连页面都不碰）
        allowed, reason = check_action_kind(
            self._write_level, kwargs.get("action_kind")
        )
        if not allowed:
            logger.info(
                f"[browser_click] 写操作被拒 write_level={self._write_level} "
                f"action_kind={kwargs.get('action_kind')!r} selector={selector[:60]}"
            )
            return reason
        page = await self._session_ref.session._ensure_page()
        await page.click(selector, timeout=5000)
        return f"已点击 {selector}"


@pydantic_dataclass
class BrowserTypeTool(FunctionTool):
    name: str = "browser_type"
    description: str = (
        "在网页输入框中填入文本。需要 write_level >= 1；"
        "若这次输入是为发帖/私信等写入做准备，必须用 action_kind 标明。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "输入框 CSS 选择器"},
            "text": {"type": "string", "description": "要输入的文本"},
            "action_kind": {
                "type": "string",
                "description": (
                    "这次输入的操作性质：fill(普通填表)/comment(评论、点赞)/"
                    "post(发帖)/message(私信)/submit_form(提交表单)。"
                    "漏标按 unknown 保守拒绝。"
                ),
            },
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
        # 补丁 XV 清单4：写操作分级判定（拒绝时连页面都不碰）
        allowed, reason = check_action_kind(
            self._write_level, kwargs.get("action_kind")
        )
        if not allowed:
            logger.info(
                f"[browser_type] 写操作被拒 write_level={self._write_level} "
                f"action_kind={kwargs.get('action_kind')!r} selector={selector[:60]}"
            )
            return reason
        page = await self._session_ref.session._ensure_page()
        await page.fill(selector, text)
        return f"已在 {selector} 填入 {len(text)} 字"
