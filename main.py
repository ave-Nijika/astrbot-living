"""astrbot_plugin_living——让 AstrBot 在无消息时拥有自己的生活。

M0 地基版：只做五能力组件的初始化与插件骨架，不含决策层与主循环
（M1 实现）。设计详见 docs/项目总纲.md。

R0 风险验证结论（2026-09-07 实测，详见 docs/m0_report.md）：
  自主 agent 循环必须携带一个"幽灵 AstrMessageEvent"——event=None 会被
  AstrAgentContext 的 pydantic 校验拒绝，即使绕过校验，FunctionToolExecutor
  也会拒绝执行本地工具（"Event must be provided for local function tools"）。
  本插件的 build_ghost_event() 即 R0 验证出的生产路径。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star

from .core.fetcher import WebFetcher
from .core.ghost_event import build_ghost_event
from .core.memory_backend import SimpleBackend, create_backend
from .core.sandbox import Sandbox
from .core.search import BochaSearcher
from .core.sender import Sender

PLUGIN_NAME = "astrbot_plugin_living"


class LivingPlugin(Star):
    """插件主类。M0：组件初始化；M1 将加入主循环与决策层。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # 五能力（构造均为轻量同步操作）
        self.searcher = BochaSearcher(context)
        self.fetcher = WebFetcher()
        self.sandbox = Sandbox(
            timeout=int(self._cfg("capabilities", "sandbox_timeout_seconds", 10)),
        )
        self.sender = Sender(context)
        # 记忆后端在 async initialize() 里探测/降级
        self.memory = None
        self.memory_note = "尚未初始化"

        logger.info(f"[{PLUGIN_NAME}] M0 骨架加载完成（五能力就绪，主循环待 M1）")

    def _cfg(self, group: str, key: str, default: Any = None) -> Any:
        """读配置（AstrBotConfig 为 dict 子类；无配置时用默认值兜底）。"""
        try:
            group_cfg = self.config.get(group, {})
            val = group_cfg.get(key, default)
            return default if val in ("", None) and default is not None else val
        except Exception:
            return default

    async def initialize(self) -> None:
        """AstrBot 在插件实例化后自动调用（v4.27.5 star_manager）。"""
        try:
            backend, note = await create_backend(
                context=self.context,
                mode=self._cfg("memory", "backend", "auto"),
                simple_db_path=self._simple_db_path(),
            )
            self.memory = backend
            self.memory_note = note
            logger.info(f"[{PLUGIN_NAME}] 记忆后端: {note}")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 记忆后端初始化失败: {e}")
            self.memory = SimpleBackend(self._simple_db_path())
            self.memory_note = f"初始化失败降级: {e}"

    @staticmethod
    def _simple_db_path() -> str:
        """SimpleBackend 的 SQLite 路径：AstrBot data/plugin_data/ 下。"""
        import os

        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        plugin_data = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        os.makedirs(plugin_data, exist_ok=True)
        return os.path.join(plugin_data, "living_memory_simple.db")

    async def terminate(self) -> None:
        """插件卸载/停用时由 AstrBot 调用，释放网络与数据库资源。"""
        for closer in (
            self.searcher.close(),
            self.fetcher.close(),
            self.memory.close() if self.memory is not None else None,
        ):
            if closer is None:
                continue
            try:
                await closer
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] terminate 清理异常")
        logger.info(f"[{PLUGIN_NAME}] 已卸载")
