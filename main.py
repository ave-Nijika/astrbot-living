"""astrbot_plugin_living——让 AstrBot 在无消息时拥有自己的生活。

M1 最小自主闭环：主循环（心跳→闸门→活动→记忆→分享）+ 五能力。
设计详见 docs/项目总纲.md；M0 地基（幽灵事件/五能力组件）见 docs/m0_report.md。

R0 风险验证结论（2026-09-07 实测，详见 docs/m0_report.md）：
  自主 agent 循环必须携带一个"幽灵 AstrMessageEvent"——event=None 会被
  AstrAgentContext 的 pydantic 校验拒绝，即使绕过校验，FunctionToolExecutor
  也会拒绝执行本地工具。见 core/ghost_event.py。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star

from .core.fetcher import WebFetcher
from .core.ghost_event import build_ghost_event  # noqa: F401（M2 agent 循环入口）
from .core.lazy_memory import LazyMemory
from .core.living_loop import LivingLoop
from .core.living_state import LivingGate
from .core.sandbox import Sandbox
from .core.search import BochaSearcher
from .core.sender import Sender

PLUGIN_NAME = "astrbot_plugin_living"


class LivingPlugin(Star):
    """插件主类：五能力 + 状态闸门 + 主循环。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # 五能力（构造均为轻量同步操作）
        self.searcher = BochaSearcher(context)
        self.fetcher = WebFetcher()
        self.sandbox = Sandbox(
            timeout=int(self._cfg("capabilities", "sandbox_timeout_seconds", 10) or 10),
        )
        self.sender = Sender(context)

        # 记忆后端懒加载（需求 A）：插件加载序可能早于 LivingMemory，
        # 加载期探测必然扑空，所以只在真正要用时才探测
        self._lazy_memory = LazyMemory(
            context=context,
            mode_getter=lambda: self._cfg("memory", "backend", "auto"),
            db_path_getter=self._memory_db_path,
        )
        self.memory_note = "尚未初始化"

        self.gate: LivingGate | None = None
        self.loop: LivingLoop | None = None

        logger.info(f"[{PLUGIN_NAME}] M1 加载完成（闭环组件就绪）")

    # ------------------------------------------------------------------
    # 配置与路径
    # ------------------------------------------------------------------
    def _cfg(self, group: str, key: str, default: Any = None) -> Any:
        """读配置（AstrBotConfig 是 dict 子类；缺失/空值回默认）。"""
        try:
            group_cfg = self.config.get(group, {})
            val = group_cfg.get(key, default)
            return default if val in ("", None) and default is not None else val
        except Exception:
            return default

    def _plugin_data_dir(self) -> str:
        import os

        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        # 只创建我们插件自己的数据目录，不碰 AstrBot 本体与其他插件
        path = get_astrbot_plugin_data_path()
        os.makedirs(path, exist_ok=True)
        plugin_dir = os.path.join(path, PLUGIN_NAME)
        os.makedirs(plugin_dir, exist_ok=True)
        return plugin_dir

    def _memory_db_path(self) -> str:
        import os

        return os.path.join(self._plugin_data_dir(), "living_memory_simple.db")

    def _gate_db_path(self) -> str:
        import os

        return os.path.join(self._plugin_data_dir(), "living_state.db")

    # ------------------------------------------------------------------
    # 记忆懒加载（需求 A，实现在 core/lazy_memory.py）
    # ------------------------------------------------------------------
    async def _get_memory(self):
        """首次使用时才探测；成功后永久缓存；失败降级 Simple 且择机重试。"""
        backend = await self._lazy_memory.get()
        self.memory_note = self._lazy_memory.note
        return backend

    # ------------------------------------------------------------------
    # 生命周期（需求 F：接线 + 热重载安全）
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """AstrBot 在插件实例化后自动调用；热重载可能重复进入，需幂等。"""
        # 上一个循环实例若因异常没停干净，先停（stop 本身幂等）
        if self.loop is not None:
            await self.loop.stop()

        self.gate = LivingGate(
            config_getter=lambda: self.config,
            db_path=self._gate_db_path(),
        )
        self.loop = LivingLoop(
            gate=self.gate,
            memory_getter=self._get_memory,
            config_getter=lambda: self.config,
            abilities={
                "searcher": self.searcher,
                "fetcher": self.fetcher,
                "sandbox": self.sandbox,
            },
            sender=self.sender,
        )
        await self.loop.start()

    async def terminate(self) -> None:
        """插件卸载/停用时由 AstrBot 调用；重复调用安全。"""
        if self.loop is not None:
            await self.loop.stop()
            self.loop = None
        if self.gate is not None:
            await self.gate.close()
            self.gate = None
        for closer in (
            self.searcher.close(),
            self.fetcher.close(),
            self._lazy_memory.close(),
        ):
            try:
                await closer
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] terminate 清理异常")
        logger.info(f"[{PLUGIN_NAME}] 已卸载")
