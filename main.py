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
from .core.ghost_event import build_ghost_event  # noqa: F401（M3 agent 循环入口）
from .core.activities import default_activities
from .core.decider import ActivityDecider
from .core.lazy_memory import LazyMemory
from .core.living_loop import LivingLoop
from .core.living_state import LivingGate
from .core.mood import MoodState
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

        # 心境状态机（M2-A）：构造轻量，load 在 initialize 里做
        self.mood = MoodState(db_path=self._mood_db_path())

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

    def _mood_db_path(self) -> str:
        import os

        return os.path.join(self._plugin_data_dir(), "mood.db")

    # ------------------------------------------------------------------
    # 记忆懒加载（需求 A，实现在 core/lazy_memory.py）
    # ------------------------------------------------------------------
    async def _get_memory(self):
        """首次使用时才探测；成功后永久缓存；失败降级 Simple 且择机重试。"""
        backend = await self._lazy_memory.get()
        self.memory_note = self._lazy_memory.note
        return backend

    # ------------------------------------------------------------------
    # 决策层支持（M2：LLM 调用 + persona 读取）
    # ------------------------------------------------------------------
    async def _decision_llm_call(self, prompt: str, system_prompt: str | None):
        """决策 LLM 调用（decider 注入用）。

        provider 选择：model.provider_id 配置优先（总纲：自主活动专用模型，
        防烧聊天模型）；留空回退当前默认聊天 provider。任何失败返回 None
        由 decider 静默回退——默认配置下没配专用 provider 也能跑。
        """
        provider_id = str(self._cfg("model", "provider_id", "") or "")
        if not provider_id:
            try:
                umo = build_ghost_event().unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception:
                return None
        if not provider_id:
            return None
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt or None,
            )
        except Exception:
            return None
        text = getattr(resp, "completion_text", None)
        if not text:
            # completion_text 已过时但仍在；兜底从 result_chain 取首个文本组件
            try:
                chain = getattr(resp, "result_chain", None)
                components = getattr(chain, "chain", None) or []
                if components:
                    text = getattr(components[0], "text", None)
            except Exception:
                text = None
        return text or None

    async def _persona_prompt(self) -> str | None:
        """读取当前生效 persona 的 system_prompt（总纲 D4：主人格复用）。

        C1 约定：任何一步失败都静默返回 None——决策没有性格引导也能跑，
        只是少了点"它是谁"的味道。
        """
        try:
            persona_manager = getattr(self.context, "persona_manager", None)
            if persona_manager is None:
                return None
            getter = getattr(persona_manager, "get_default_persona_v3", None)
            if not callable(getter):
                return None
            umo = build_ghost_event().unified_msg_origin
            persona = await getter(umo)
        except Exception:
            return None
        # Personality 是 TypedDict（prompt/name 字段），防御性兼容属性式对象
        if isinstance(persona, dict):
            prompt = persona.get("prompt")
        else:
            prompt = getattr(persona, "prompt", None)
        if prompt is None:
            return None
        text = str(prompt).strip()
        return text or None

    # ------------------------------------------------------------------
    # 生命周期（需求 F：接线 + 热重载安全）
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """AstrBot 在插件实例化后自动调用；热重载可能重复进入，需幂等。"""
        # 上一个循环实例若因异常没停干净，先停（stop 本身幂等）
        if self.loop is not None:
            await self.loop.stop()

        try:
            await self.mood.load()  # 内含跨日兴趣衰减
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 心境加载失败（用默认心境继续）: {e}")

        self.gate = LivingGate(
            config_getter=lambda: self.config,
            db_path=self._gate_db_path(),
        )
        decider = ActivityDecider(
            activities=default_activities(),
            config_getter=lambda: self.config,
            llm_call=self._decision_llm_call,
            mood=self.mood,
            persona_getter=self._persona_prompt,
            life_extra_getter=lambda: str(self._cfg("persona", "life_extra", "") or ""),
            memory_getter=self._get_memory,
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
            mood=self.mood,
            decider=decider,
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
            self.mood.close(),
        ):
            try:
                await closer
            except Exception:
                logger.exception(f"[{PLUGIN_NAME}] terminate 清理异常")
        logger.info(f"[{PLUGIN_NAME}] 已卸载")
