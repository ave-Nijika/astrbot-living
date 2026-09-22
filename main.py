"""astrbot_plugin_living——让 AstrBot 在无消息时拥有自己的生活。

M3：休眠系统（疲惫/睡眠债/吵醒/静默回复/梦）+ 双事件热生效 + agent 循环
（token 硬闸保护）。设计详见 docs/项目总纲.md；前情报告见 docs/archive/。

R0 风险验证结论（2026-09-07 实测，详见 docs/archive/m0_report.md）：
  自主 agent 循环必须携带一个"幽灵 AstrMessageEvent"——event=None 会被
  AstrAgentContext 的 pydantic 校验拒绝，即使绕过校验，FunctionToolExecutor
  也会拒绝执行本地工具。见 core/ghost_event.py。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.activities import default_activities
from .core.agent_loop import LivingAgentLoop
from .core.autonomy import (
    TIER_NAMES,
    WRITE_LEVEL_NAMES,
    build_tool_manifest,
    read_tier,
    read_write_level,
)
from .core.config_knobs import ConfigKnobs
from .core.conf_path import conf_group
from .core.decider import ActivityDecider
from .core.fetcher import WebFetcher
from .core.ghost_event import GHOST_PLATFORM_ID, build_ghost_event
from .core.lazy_memory import LazyMemory
from .core.living_loop import LivingLoop
from .core.living_state import LivingGate
from .core.living_tools import build_living_tools
from .core.llm_failover import (
    build_provider_chain,
    is_retryable_llm_error,
    looks_like_llm_error_output,
    summarize_provider_error,
)
from .core.mood import MoodState
from .core.sandbox import Sandbox
from .core.schedule import ScheduleManager
from .core.share_rewriter import ShareRewriter
from .core.selfheal import run_identity_selfheal
from .core.search import BochaSearcher
from .core.sender import Sender
from .core.sleep import SleepManager

PLUGIN_NAME = "astrbot_plugin_living"

# 闸门拦截原因 → 给主人看的一句话（/living_wake 反馈用）
_WAKE_REASON_TEXT = {
    "sleeping": "我在睡觉呢（自主作息），不忍心叫就别叫我啦",
    "daily_limit": "今天已经玩够了（每日活动上限）",
    "cooldown": "刚忙完，还在歇着（冷却中）",
    "rolled_off": "想了想暂时不想动（概率掷点落空，再叫一次就好）",
}


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
        self.sleep_manager: SleepManager | None = None
        self._selfheal_task: asyncio.Task | None = None
        self._interest_cooldown_task: asyncio.Task | None = None
        # 补丁 XVIII：新手旋钮监视
        self._knobs: Any = None
        self._knobs_task: asyncio.Task | None = None
        # M5-补丁4：起床约定管理器
        self._schedule: Any = None
        # 补丁 XIII：浏览器会话（惰性创建，跨活动复用 → 登录态保持）
        self._browser_session: Any = None
        # 补丁 XVI：bot 自身身份缓存（来自真实消息事件，与原生侧同源）
        self._self_identity: dict | None = None

        logger.info(f"[{PLUGIN_NAME}] M3 加载完成（心境+休眠+agent 循环）")

    # ------------------------------------------------------------------
    # 配置与路径
    # ------------------------------------------------------------------
    def _cfg(self, group: str, key: str, default: Any = None) -> Any:
        """读配置（AstrBotConfig 是 dict 子类；缺失/空值回默认）。

        补丁 XVIII 起底层键收拢在 advanced 组下，经 conf_group 统一读取
        （嵌套优先、平铺兜底）；组名与键名不变。
        """
        try:
            from .core.conf_path import conf_group

            group_cfg = conf_group(self.config, group)
            val = group_cfg.get(key, default)
            return default if val in ("", None) and default is not None else val
        except Exception:
            return default

    def _preset(self, key: str, default: Any = None) -> Any:
        """读新手设置组的键（旋钮与 life_extra，补丁 XVIII）。"""
        try:
            from .core.conf_path import preset_value

            return preset_value(self.config, key, default)
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
        """决策 LLM 调用（decider/梦共用），带模型故障转移链。

        provider 选择（任务书 M3-补丁 问题 1）：fallback_chain 配置链在前，
        全部已启用 chat provider 兜底；只有 404/429/超时/连接类错误才切换，
        401 等换模型解决不了的直接放弃（返回 None，由决策层静默回退）。
        """
        chain = await build_provider_chain(self.context, lambda: self.config)
        if not chain:
            return None
        for provider_id, _provider in chain:
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt or None,
                )
            except Exception as e:
                if is_retryable_llm_error(e):
                    logger.warning(
                        f"[Failover] 决策调用 provider {provider_id} 失败，"
                        f"尝试下一个: {summarize_provider_error(e)}"
                    )
                    continue
                logger.debug(f"[Failover] 不可重试错误，放弃决策调用: {e}")
                return None
            text = getattr(resp, "completion_text", None)
            if not text:
                # completion_text 已过时但仍在；兜底从 result_chain 取首个文本组件
                try:
                    chain_components = getattr(resp, "result_chain", None)
                    components = getattr(chain_components, "chain", None) or []
                    if components:
                        text = getattr(components[0], "text", None)
                except Exception:
                    text = None
            if not text:
                continue  # 空产出：换下一个 provider
            if looks_like_llm_error_output(text):
                # VM 实测形态：错误被包成正常产出（"All chat models failed"）
                logger.warning(
                    f"[Failover] provider {provider_id} 返回错误文本，尝试下一个"
                )
                continue
            logger.debug(f"[LivingLoop] 使用 provider: {provider_id}")
            return text
        return None

    def _platform_prefixes(self) -> set[str]:
        """合法身份前缀集合（补丁 VI 需求 1-2；补丁 XVI 修正：补 type 维度）。

        AstrBot 的 platform 条目有 **id**（用户可自定义，实测为 "default"）
        与 **type**（平台类型，实测 "aiocqhttp"）两个维度。原生侧
        LivingMemory 给 bot 建节点用的是 **type**（实测 aiocqhttp:10001），
        而补丁 VI 只收了 id —— 白名单永远匹配不上原生身份，提取链必然
        落空、退到兜底 cron:{username}，形成孤儿节点（两团根因）。

        现在两个维度都收；"cron" 恒在集合内。
        """
        prefixes = {"cron"}
        try:
            get_config = getattr(self.context, "get_config", None)
            if callable(get_config):
                cfg = get_config() or {}
                for platform in cfg.get("platform", []) or []:
                    entry = platform or {}
                    for field in ("id", "type"):
                        value = str(entry.get(field, "") or "").strip()
                        if value:
                            prefixes.add(value)
        except Exception:
            pass
        return prefixes

    @staticmethod
    def _identity_is_polluted(identity_key: str) -> bool:
        """污染身份判定：default: 前缀是 ghost 会话 fallback 的历史产物，
        绝不采信（任务书 M3 补丁 VI 需求 1-1）。"""
        return str(identity_key or "").startswith("default:")

    def _identity_whitelisted(self, identity_key: str, prefixes: set[str]) -> bool:
        """白名单校验：identity_key 必须以真实平台 id + ':' 开头。"""
        text = str(identity_key or "")
        return any(text.startswith(f"{prefix}:") for prefix in prefixes)

    def _remember_self_identity(self, event: Any) -> None:
        """从真实消息事件缓存 bot 自身身份（补丁 XVI）。

        身份格式与原生侧 LivingMemory 一致：``{platform_name}:{self_id}``
        （实测 ``aiocqhttp:10001``）——原生图谱给 bot 建节点用的就是
        这个值，living 采信同源身份才能与原生记忆连通。

        幽灵事件（伪造 self_id）与不在平台白名单内的组合一律不采信。
        任何异常都静默吞掉：身份采集绝不能影响消息处理主链路。
        """
        try:
            self_id = str(event.get_self_id() or "").strip()
            platform = str(event.get_platform_name() or "").strip()
            if not self_id or not platform:
                return
            if platform.startswith(GHOST_PLATFORM_ID):
                return
            key = f"{platform}:{self_id}"
            if not self._identity_whitelisted(key, self._platform_prefixes()):
                return
            cached = getattr(self, "_self_identity", None)
            if cached and cached.get("identity_key") == key:
                return
            self._self_identity = {
                "identity_key": key,
                "sender_id": self_id,
                "platform": platform,
                "display_name": platform,
                "aliases": [platform],
                "is_bot": True,
            }
            logger.info(f"[Living] bot 自身身份已记录（与原生同源）: {key}")
        except Exception as e:
            logger.debug(f"[Living] 自身身份采集失败（忽略）: {e}")

    def _normalize_bot_identity(self, participant: dict) -> dict:
        """把采信的原生参与者条目规范成统一的 bot 身份结构（补 aliases）。"""
        identity_key = str(participant.get("identity_key", ""))
        display_name = str(participant.get("display_name") or "astrbot")
        return {
            "identity_key": identity_key,
            "sender_id": str(
                participant.get("sender_id") or identity_key.partition(":")[-1]
            ),
            "platform": str(participant.get("platform") or identity_key.partition(":")[0]),
            "display_name": display_name,
            "aliases": [display_name],
            "is_bot": True,
        }

    async def _bot_identity(self) -> dict | None:
        """bot 自己的身份（participant_identities 原料）。

        M3 补丁 VI 需求 1：本方法在生产环境提取到过被污染的身份——probe
        词（"我"开头）召回的活动记忆霸榜 top-k，而这些记忆携带的正是
        ghost fallback 的 default:hash 污染身份，取到即缓存后污染自我延续。
        现在的提取链：
          1. 从 LivingMemory 已有记忆里找参与者身份（probe 词扩充、k=20、
             逐行全扫）；
          2. 每个候选过两道校验：default: 前缀一律跳过（污染过滤）；
             必须以真实平台 id/cron 开头（白名单）；
          3. 通过校验才采信并缓存；缓存里已有污染身份（历史遗留）→ 丢弃
             重新提取；
          4. 全部落空 → 兜底 cron:{dashboard_username}（原生侧真实存在
             该节点，是合法桥梁身份，逻辑不变）。
        """
        prefixes = self._platform_prefixes()

        # 路径 0（补丁 XVI）：真实消息事件缓存的自身身份——最权威来源，
        # 与原生侧 LivingMemory 建节点用的身份同源（{platform_name}:{self_id}）。
        # 它优先于任何缓存：哪怕缓存里是旧的兜底身份（cron:xxx）也被覆盖，
        # 这是"两团"问题不再复发的前提。
        own = getattr(self, "_self_identity", None)
        if own:
            self._bot_identity_cache = own
            return own

        # 缓存防污染：历史污染缓存（default: 前缀）丢弃重新提取
        cached = getattr(self, "_bot_identity_cache", None)
        if cached:
            if self._identity_is_polluted(cached.get("identity_key")):
                logger.warning(
                    "[Living] 检测到缓存的 bot 身份被污染（"
                    f"{cached.get('identity_key')}），丢弃并重新提取"
                )
                self._bot_identity_cache = None
            else:
                return cached

        # 路径 1：从 LivingMemory 已有记忆的参与者里找通过校验的身份
        try:
            backend = await self._get_memory()
            from .core.memory_backend import LivingMemoryBackend

            if isinstance(backend, LivingMemoryBackend):
                for probe in ("我", "今天", "记忆", "文章", "冲浪", "的"):
                    rows = await backend.search(probe, k=20)
                    for row in rows:
                        metadata = row.get("metadata") or {}
                        for participant in (
                            metadata.get("participant_identities") or []
                        ):
                            key = str(
                                (participant or {}).get("identity_key", "") or ""
                            )
                            if not key or self._identity_is_polluted(key):
                                continue  # 污染过滤
                            if not self._identity_whitelisted(key, prefixes):
                                continue  # 白名单校验
                            identity = self._normalize_bot_identity(participant)
                            self._bot_identity_cache = identity
                            logger.debug(
                                f"[Living] bot 身份采信（过白名单）: {key}"
                            )
                            return identity
        except Exception as e:
            logger.debug(f"[Living] 从记忆提取 bot 身份失败（走兜底）: {e}")

        # 兜底（补丁 XVI 修正）：不再用 cron:{dashboard_username} 造身份。
        # 补丁 VI 假设"原生侧真实存在 cron:{username} 节点、是合法桥梁"，
        # 但环境重置后该节点并不存在——写入它只会得到孤儿 person 节点，
        # 正是"两个独立图谱"的直接成因。
        # 改为返回 None：本次记忆暂不挂 bot 身份（图谱少一个节点，无害），
        # 等第一条真实消息事件到达（路径 0 生效）后，后续记忆即与原生同源。
        logger.warning(
            "[Living] 暂无可用的 bot 自身身份（等待真实消息事件）——"
            "本次记忆不注入参与者身份"
        )
        return None

    async def _persona_id(self) -> str:
        """当前生效 persona 的 id（问题 3：记忆图谱的参与者边原料）。

        v3 Personality 字典里 name 字段存的就是 persona_id（源码核实）；
        任何失败回 "default"——图谱归属不能因为取不到 id 而阻塞写入。
        """
        try:
            persona_manager = getattr(self.context, "persona_manager", None)
            if persona_manager is None:
                return "default"
            getter = getattr(persona_manager, "get_default_persona_v3", None)
            if not callable(getter):
                return "default"
            umo = build_ghost_event().unified_msg_origin
            persona = await getter(umo)
        except Exception:
            return "default"
        if isinstance(persona, dict):
            pid = persona.get("persona_id") or persona.get("name")
        else:
            pid = getattr(persona, "persona_id", None) or getattr(
                persona, "name", None
            )
        text = str(pid or "").strip()
        return text or "default"

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
    # 自主能力接线（补丁 XIII：档位配置热读 + 浏览器会话复用）
    # ------------------------------------------------------------------
    def _living_workspace(self) -> str:
        """"它的家"：自主活动工作区目录（配置优先，缺省用插件数据目录）。"""
        configured = str(self._cfg("autonomy", "workspace_dir", "") or "").strip()
        if configured:
            return configured
        return str(Path("data") / "plugin_data" / f"{PLUGIN_NAME}_home")

    def _get_browser_session(self, write_level: int):
        """浏览器会话复用（同一会话跨活动共享 → 登录态保持）。

        会话对象惰性创建；Playwright 未安装时返回 None（工具不注册，
        不影响其他能力）。write_level 每次同步，确保分层实时生效。
        """
        if self._browser_session is None:
            try:
                from .core.browser_tools import BrowserSession

                self._browser_session = BrowserSession(
                    self._living_workspace(), write_level
                )
                logger.info(f"[{PLUGIN_NAME}] 浏览器会话已创建（写层级={write_level}）")
            except Exception as e:
                logger.warning(
                    f"[{PLUGIN_NAME}] 浏览器会话创建失败（本次降级为无浏览能力）: {e}",
                    exc_info=True,
                )
                return None
        else:
            self._browser_session.write_level = write_level
        return self._browser_session

    def _build_agent_tools(self):
        """按当前 autonomy 配置组装生活工具集。

        由 LivingAgentLoop 在每次活动前调用（补丁 XIII-P1）——
        档位/写层级配置热读，改配置下个活动周期即生效，无需重启插件。
        """
        config = self.config if isinstance(self.config, dict) else {}
        tier = read_tier(config)
        write_level = read_write_level(config)
        browser_session = self._get_browser_session(write_level) if tier >= 1 else None
        tools = build_living_tools(
            searcher=self.searcher,
            fetcher=self.fetcher,
            sandbox=self.sandbox,
            memory_getter=self._get_memory,
            # 补丁 XX：remember 工具需带 bot 身份，否则写入的记忆成图谱孤岛
            bot_identity_getter=self._bot_identity,
            tier=tier,
            write_level=write_level,
            workspace=self._living_workspace(),
            browser_session=browser_session,
        )
        # 补丁 XV 清单2：档位日志改用 build_tool_manifest（"预期清单"），
        # 与"实际挂载"并排——两者不一致即装配有缺，一眼可查
        manifest = build_tool_manifest(
            tier,
            write_level,
            has_browser=browser_session is not None,
            has_workspace=bool(self._living_workspace()),
        )
        logger.info(
            f"[{PLUGIN_NAME}] 档位={tier}({TIER_NAMES.get(tier, '?')}) "
            f"写层级={write_level}({WRITE_LEVEL_NAMES.get(write_level, '?')}) "
            f"清单={manifest} 实际挂载={[t.name for t in tools.tools]}"
        )
        return tools

    def _free_activity_enabled(self) -> bool:
        """decision.free_activity_enabled（补丁 XV 清单3）：False 时 free
        活动退出活动池，回到固定池。"""
        raw = self._cfg("decision", "free_activity_enabled", True)
        return True if raw is None else bool(raw)

    # ------------------------------------------------------------------
    # 生命周期（需求 F：接线 + 热重载安全）
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """AstrBot 在插件实例化后自动调用；热重载可能重复进入，需幂等。"""
        # 上一个循环实例若因异常没停干净，先停（stop 本身幂等）
        if self.loop is not None:
            await self.loop.stop()

        try:
            # 跨日结算用配置的睡眠债消退速率（热读，取自当前配置）
            await self.mood.load(
                sleep_debt_decay_per_day=float(
                    self._cfg("sleep", "sleep_debt_decay_per_day", 30.0) or 30.0
                )
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 心境加载失败（用默认心境继续）: {e}")

        self.gate = LivingGate(
            config_getter=lambda: self.config,
            db_path=self._gate_db_path(),
        )
        # 补丁 II：重启时若仍在清醒待机期内则延续待机状态（持久化恢复）
        try:
            await self.gate.load_state()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 待机状态恢复失败（按无待机继续）: {e}")
        # M5-补丁4：起床约定（ScheduleManager）——提取/存储/压力查询，
        # 复用决策 LLM 装配（与 _dream_llm_call 同一注入模式）
        self._schedule = ScheduleManager(
            config_getter=lambda: self.config,
            gate=self.gate,
            llm_call=self._decision_llm_call,
        )
        self.sleep_manager = SleepManager(
            config_getter=lambda: self.config,
            gate=self.gate,
            mood=self.mood,
            schedule=self._schedule,
            # M9-补丁1：主人身份自动认领——owner_id 未手填时派生自管理员
            global_config_getter=lambda: self.context.astrbot_config,
        )
        agent_loop = LivingAgentLoop(
            context=self.context,
            config_getter=lambda: self.config,
            persona_getter=self._persona_prompt,
            life_extra_getter=lambda: str(self._preset("life_extra", "") or ""),
            mood=self.mood,
            tool_builder=self._build_agent_tools,
        )
        decider = ActivityDecider(
            # 补丁 XV 清单3：free 开关在构造期先滤一次（decider/loop 内部
            # 还会按配置现读，双保险保热生效）
            activities=default_activities(
                enabled_free=self._free_activity_enabled()
            ),
            config_getter=lambda: self.config,
            llm_call=self._decision_llm_call,
            mood=self.mood,
            persona_getter=self._persona_prompt,
            life_extra_getter=lambda: str(self._preset("life_extra", "") or ""),
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
            # 补丁 XV 清单3：活动池显式传入（原先 loop 自建全量池，与
            # decider 池来源不一致；现同源，free 开关在两处一致）
            activities=default_activities(
                enabled_free=self._free_activity_enabled()
            ),
            decider=decider,
            sleep_manager=self.sleep_manager,
            agent_loop=agent_loop,
            dream_llm_call=self._decision_llm_call,
            persona_id_getter=self._persona_id,
            schedule=self._schedule,
            share_rewriter=ShareRewriter(
                llm_call=self._decision_llm_call,
                config_getter=lambda: self.config,
                persona_getter=self._persona_prompt,
                life_extra_getter=lambda: str(
                    self._preset("life_extra", "") or ""
                ),
                mood=self.mood,
            ),
            bot_identity_getter=self._bot_identity,
            # M9-补丁1：主人身份自动认领——target_sessions 未手填时派生
            # 全部管理员的私聊会话（与 _bot_identity_getter 同款注入先例）
            global_config_getter=lambda: self.context.astrbot_config,
        )
        await self.loop.start()

        # M3 补丁 VI 需求 2：历史污染数据自愈（后台一次性，不阻塞加载）。
        # 污染身份会让 LivingMemory 反思/总结持续自我延续——早一天清掉，
        # 图谱就少一天脏数据
        self._selfheal_task = asyncio.create_task(
            self._run_identity_selfheal_with_retry(), name="living-selfheal"
        )
        # M3 补丁 VII 需求 5：兴趣数据一次性降温（独立小任务，幂等）
        self._interest_cooldown_task = asyncio.create_task(
            self._run_interest_cooldown_once(), name="living-interest-cooldown"
        )
        # 补丁 XVIII：新手旋钮监视（批量预设器，独立周期任务）
        # 补丁 XIX：基线持久化——旋钮改动经热重载生效，内存基线会在重载时
        # 被"当前值"重置导致永不写入；落盘后 arm() 优先读旧基线才能比出差异
        self._knobs = ConfigKnobs(
            lambda: self.config,
            save_config=self._save_config_async,
            state_path=self._knobs_state_path(),
        )
        self._knobs_task = asyncio.create_task(
            self._run_knob_loop(), name="living-config-knobs"
        )
        # M5 补丁 1：自带配置面板的 REST API（pages/config/ 前端调用）
        self._register_dashboard_routes()

    async def _save_config_async(self) -> None:
        """把内存中的配置变更持久化（AstrBotConfig 提供 async save）。"""
        save = getattr(self.config, "save_config_async", None)
        if callable(save):
            await save()

    # ------------------------------------------------------------------
    # 自带配置面板 API（M5 补丁 1）：pages/config/ 前端的唯一后端
    # ------------------------------------------------------------------
    def _register_dashboard_routes(self) -> None:
        """注册面板 REST API（构造后调用一次；重复注册会被同名替换，幂等）。

        route 带插件名前缀（dashboard 按 /api/plug/<route> 挂载）；
        Pages bridge 只提供 GET/POST。业务逻辑全部在 core/panel_api.py，
        handler 只做"读 body → 调逻辑 → 包装响应"，便于与 mock 实测共用。
        """
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                f"[{PLUGIN_NAME}] context.register_web_api 不可用，配置面板 API 未注册"
            )
            return
        prefix = f"/{PLUGIN_NAME}"
        routes = [
            (f"{prefix}/config", self._api_config_get, ["GET"], "面板配置读取"),
            (f"{prefix}/config", self._api_config_post, ["POST"], "面板配置保存"),
            (f"{prefix}/config/reset", self._api_config_reset, ["POST"], "恢复默认值"),
            (f"{prefix}/mood", self._api_mood_get, ["GET"], "心境快照读取"),
            (f"{prefix}/mood/interests", self._api_mood_interests_post, ["POST"], "兴趣权重编辑"),
        ]
        for route, handler, methods, desc in routes:
            register(route, handler, methods, desc)
        logger.info(
            f"[{PLUGIN_NAME}] 配置面板 API 已注册（{len(routes)} 条路由）"
        )

    def _panel_schema(self) -> dict:
        from .core.panel_api import load_schema

        return load_schema(Path(__file__).resolve().parent)

    def _panel_save_config(self) -> None:
        """面板保存后的落盘：走 AstrBot 原生保存路径（dict 环境静默跳过）。"""
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    async def _api_config_get(self):
        from .core.panel_api import PanelApiError, build_config_payload

        try:
            payload = build_config_payload(self.config, self._panel_schema())
            return {"status": "ok", "data": payload}
        except PanelApiError as e:
            return {"status": "error", "message": str(e)}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 面板读取失败")
            return {"status": "error", "message": "内部错误"}

    async def _api_config_post(self):
        from astrbot.api.web import request as web_request

        from .core.panel_api import (
            PanelApiError,
            apply_panel_save,
            build_config_payload,
        )

        try:
            payload = await web_request.json(default={})
            summary = apply_panel_save(self.config, self._panel_schema(), payload)
            self._panel_save_config()
            logger.info(
                f"[{PLUGIN_NAME}] 面板保存 {summary['count']} 项: "
                f"{'; '.join(summary['changed'])}"
            )
            return {
                "status": "ok",
                "message": f"已保存 {summary['count']} 项",
                "data": {"changed": summary["changed"]},
            }
        except PanelApiError as e:
            return {"status": "error", "message": str(e)}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 面板保存失败")
            return {"status": "error", "message": "内部错误"}

    async def _api_config_reset(self):
        from .core.panel_api import PanelApiError, apply_panel_reset

        try:
            schema = self._panel_schema()
            # 先把旧值备份到日志（任务书 2.3：reset 前留痕）
            try:
                old = json.dumps(
                    {
                        "preset": dict(self.config.get("preset", {}) or {}),
                        "advanced": dict(self.config.get("advanced", {}) or {}),
                    },
                    ensure_ascii=False,
                    default=str,
                )
                logger.info(f"[{PLUGIN_NAME}] 面板恢复默认值，旧值备份: {old[:2000]}")
            except Exception:
                pass
            summary = apply_panel_reset(self.config, schema)
            self._panel_save_config()
            logger.info(
                f"[{PLUGIN_NAME}] 已恢复默认值（preset {summary['preset_keys']} 项 / "
                f"advanced {summary['advanced_keys']} 项）"
            )
            return {
                "status": "ok",
                "message": "已恢复默认值",
                "data": summary,
            }
        except PanelApiError as e:
            return {"status": "error", "message": str(e)}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 恢复默认值失败")
            return {"status": "error", "message": "内部错误"}

    async def _api_mood_get(self):
        """心境快照读取（M9-补丁1 B1）：五项只读状态 + interests。"""
        from .core.panel_api import PanelApiError, build_mood_snapshot

        try:
            if self.mood is None:
                return {"status": "error", "message": "心境模块未初始化"}
            return {"status": "ok", "data": build_mood_snapshot(self.mood)}
        except PanelApiError as e:
            return {"status": "error", "message": str(e)}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 心境读取失败")
            return {"status": "error", "message": "内部错误"}

    async def _api_mood_interests_post(self):
        """兴趣权重编辑（M9-补丁1 B2-B4）：set/delete/clear，写后立即
        持久化（运行中实例热生效）。校验失败走 error 响应（Pages bridge
        的响应体恒 200，400 语义以 body.status=error 表达，与 config
        端点一致）。"""
        from astrbot.api.web import request as web_request

        from .core.panel_api import PanelApiError, apply_mood_interests

        try:
            payload = await web_request.json(default={})
            data = await apply_mood_interests(self.mood, payload)
            logger.info(
                f"[{PLUGIN_NAME}] 面板兴趣写入: {payload.get('action')} "
                f"{payload.get('topic', '')}"
            )
            return {"status": "ok", "message": "已更新兴趣", "data": data}
        except PanelApiError as e:
            return {"status": "error", "message": str(e)}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 兴趣写入失败")
            return {"status": "error", "message": "内部错误"}

    async def _run_knob_loop(self) -> None:
        """旋钮监视循环：先记基线（不写入），之后每 5s 处理增量。

        与 LivingLoop 的配置 watcher 同节奏——纯内存比对，零 LLM 零网络。
        apply_changes 内部吞一切异常，这里只兜底任务级意外。
        """
        try:
            self._knobs.arm()
            while True:
                await asyncio.sleep(5)
                await self._knobs.apply_changes()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 旋钮监视任务退出（不影响服务）: {e}")

    async def _run_interest_cooldown_once(self) -> None:
        """历史偏执数据降温（补丁 VII 需求 5）：单主题兴趣 >= 阈值时乘系数
        一次性稀释——饱和曲线+衰减要连跑数天才能自然稀释，不降温的话新
        机制上线头几天行为仍然偏执。幂等：state 文件标记后不再执行。"""
        try:
            if self.mood is None:
                return
            from .core.selfheal import cooldown_interests_once

            cooled = await cooldown_interests_once(
                self.mood,
                state_path=self._selfheal_state_path(),
                threshold=float(
                    self._cfg("decision", "interest_cooldown_threshold", 0.85)
                    or 0.85
                ),
                factor=float(
                    self._cfg("decision", "interest_cooldown_factor", 0.4) or 0.4
                ),
            )
            if cooled:
                await self.mood.save()
                logger.info(
                    f"[SelfHeal] 兴趣数据降温完成：{cooled}（历史偏执稀释）"
                )
        except Exception as e:
            logger.warning(f"[SelfHeal] 兴趣降温任务异常（不影响服务）: {e}")

    async def _run_identity_selfheal_with_retry(self) -> None:
        """历史污染数据自愈（任务书 M3 补丁 VI 需求 2；凛热修补时序）。

        只处理 living 直写记忆（participant_identities 含 default: 污染
        身份的条目），原生记忆绝不触碰；幂等状态落 selfheal_state.json。
        全流程异常只记 WARNING——自愈失败绝不影响插件正常服务。

        凛热修（2026-09-15）：AstrBot 按目录序加载插件，living 排在
        livingmemory 之前——本任务触发时引擎往往尚未就绪（lazy_memory
        探测失败降级 Simple），原"一次性执行"版本会在这个窗口被跳过且
        不再重试。改为轮询等待：探测到 LivingMemory 后端才开始自愈，
        最长 10 分钟，超时放弃（WARNING，不影响插件服务）。
        """
        from .core.memory_backend import LivingMemoryBackend

        backend = None
        max_attempts = 20  # 20 × 30s = 10 分钟
        for attempt in range(1, max_attempts + 1):
            try:
                backend = await self._get_memory()
            except Exception as e:
                logger.warning(
                    f"[SelfHeal] 记忆后端获取失败（第 {attempt}/{max_attempts} 次）: {e}"
                )
                backend = None
            if isinstance(backend, LivingMemoryBackend):
                break
            if attempt == 1:
                logger.info(
                    "[SelfHeal] LivingMemory 引擎尚未就绪（本插件加载顺序早于它），"
                    "30 秒后重试…"
                )
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise  # 插件终止时会 cancel 本任务，让取消正常传播

        if not isinstance(backend, LivingMemoryBackend):
            logger.warning(
                f"[SelfHeal] 等待 LivingMemory 就绪超时（{max_attempts} 次探测），放弃自愈"
            )
            return
        identity = await self._bot_identity()
        if not identity or self._identity_is_polluted(identity.get("identity_key")):
            # 连修正身份本身都被污染（理论上不会发生）——宁可不清也不清错
            logger.warning("[SelfHeal] 修正身份不可用或已污染，放弃本次自愈")
            return
        corrected = dict(identity)
        corrected.setdefault("aliases", [corrected.get("display_name", "astrbot")])
        try:
            summary = await run_identity_selfheal(
                backend,
                corrected,
                state_path=self._selfheal_state_path(),
                engine=getattr(backend, "engine", None),
            )
            if summary["found"]:
                logger.info(
                    f"[SelfHeal] 自愈结果：修正 {summary['fixed']}/"
                    f"{summary['found']} 条（路径 {summary['paths']}）"
                )
        except Exception as e:
            logger.warning(f"[SelfHeal] 自愈流程异常（不影响插件服务）: {e}")

    def _selfheal_state_path(self) -> str:
        import os

        return os.path.join(self._plugin_data_dir(), "selfheal_state.json")

    def _knobs_state_path(self) -> str:
        """旋钮基线文件（补丁 XIX）：与 selfheal_state.json 同目录，便于运维。"""
        import os

        return os.path.join(self._plugin_data_dir(), "knobs_state.json")

    # ------------------------------------------------------------------
    # M3：手动唤醒命令 + 消息监听（吵醒计数 / 睡眠期静默拦截）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # M3 补丁 III-B：/living 命令体系（子命令手工解析，同 /preset 模式）
    # ------------------------------------------------------------------
    @filter.command("living")
    async def living(self, event: AstrMessageEvent):
        """astrbot-living 状态与控制入口。

        子命令：status / mood / memories [n] / pause / resume / wake /
        sleep / do <activity> [topic] / config <key> <value> / debug。
        无参数 = 简要状态。全部手工解析 message_str（过滤器对多余参数
        直接忽略，见 star/filter/command.py 的 validate 逻辑）。
        """
        async for line in self._living_dispatch(event):
            yield event.plain_result(line)

    async def _living_dispatch(self, event: AstrMessageEvent):
        raw = str(getattr(event, "message_str", "") or "").strip()
        tokens = raw.split()
        # 找到指令标记（可能带不同配置前缀），其后为子命令与参数
        idx = next((i for i, t in enumerate(tokens) if "living" in t.lower()), -1)
        rest = tokens[idx + 1:] if idx >= 0 else []
        sub = rest[0].lower() if rest else ""
        args = rest[1:]

        if sub == "":
            # 任务书定稿：/living 无参数 = 简要状态（帮助只在显式 help/未知时给）
            for line in await self._living_root_lines():
                yield line
        elif sub in ("help", "帮助"):
            for line in self._living_help_lines():
                yield line
        elif sub in ("status", "状态"):
            for line in await self._living_status_lines():
                yield line
        elif sub == "mood":
            for line in await self._living_mood_lines():
                yield line
        elif sub in ("memories", "记忆"):
            n = int(args[0]) if args and args[0].isdigit() else 5
            for line in await self._living_memories_lines(n):
                yield line
        elif sub == "pause":
            for line in await self._living_pause_lines():
                yield line
        elif sub in ("resume", "继续"):
            for line in await self._living_resume_lines():
                yield line
        elif sub == "wake":
            for line in await self._wake_flow_lines():
                yield line
        elif sub == "sleep":
            for line in await self._living_sleep_lines():
                yield line
        elif sub == "do":
            activity = args[0].lower() if args else ""
            topic = args[1] if len(args) > 1 else None
            for line in await self._living_do_lines(activity, topic):
                yield line
        elif sub == "config":
            key = args[0] if args else ""
            value = args[1] if len(args) > 1 else ""
            for line in await self._living_config_lines(key, value):
                yield line
        elif sub == "debug":
            for line in await self._living_debug_lines():
                yield line
        else:
            yield f"未知子命令：{sub}"
            for line in self._living_help_lines():
                yield line

    # ---- 组件守护：命令在组件缺失时也要给出人话反馈 ----
    def _loop_or_hint(self) -> tuple[Any, str | None]:
        if self.loop is None:
            return None, "主循环未启动（检查启动日志）"
        return self.loop, None

    async def _recent_memory_lines(self, n: int, title: str) -> list[str]:
        lines = [title]
        try:
            memory = await self._get_memory()
            rows = await memory.search("", k=n)
        except Exception as e:
            return lines + [f"（记忆读取失败：{e}）"]
        rows = rows or []
        if not rows:
            lines.append("（还没有任何记忆）")
            return lines
        for row in rows[:n]:
            content = str(row.get("content", "")).strip()[:40]
            lines.append(f"- {content}")
        return lines

    async def _living_root_lines(self) -> list[str]:
        lines = ["[astrbot-living] 状态一览"]
        if self.loop is None:
            lines.append("主循环：未启动")
        else:
            state = "已暂停" if self.loop.paused else (
                "运行中" if self.loop.running else "已停止"
            )
            lines.append(f"主循环：{state}")
        if self.mood is not None:
            lines.append(f"心境：{self.mood.digest()}")
        try:
            reached, count, limit = await self.gate.daily_limit_info()
            limit_text = f"{count}/{limit}" if limit > 0 else f"{count}/∞"
            lines.append(f"今日活动：{limit_text}" + ("（已满）" if reached else ""))
        except Exception:
            lines.append("今日活动：未知")
        if self.gate is not None and self.gate.awake_standby_active():
            lines.append("状态：清醒待机中（可以聊天）")
        for line in await self._recent_memory_lines(3, "最近："):
            lines.append(line)
        return lines

    async def _living_status_lines(self) -> list[str]:
        lines = ["[astrbot-living] 详细状态"]
        if self.loop is None:
            lines.append("主循环：未启动")
        else:
            state = "已暂停" if self.loop.paused else (
                "运行中" if self.loop.running else "已停止"
            )
            lines.append(f"主循环：{state}")
        if self.mood is not None:
            lines.append(
                f"心境：valence={self.mood.valence:.2f} "
                f"arousal={self.mood.arousal:.2f} energy={self.mood.energy:.2f}"
            )
            lines.append(f"疲惫：{self.mood.fatigue:.0f}/100")
            lines.append(f"睡眠债：{self.mood.sleep_debt:.0f}/100")
            interests = self.mood.get_interests()
            if interests:
                liked = "、".join(
                    f"{k}({v:.2f})" for k, v in sorted(
                        interests.items(), key=lambda kv: kv[1], reverse=True
                    )
                )
                lines.append(f"兴趣：{liked}")
        if self.gate is not None:
            state = await self.gate.get_state()
            reached, count, limit = await self.gate.daily_limit_info()
            lines.append(
                f"今日活动：{count}/{limit if limit > 0 else '∞'}"
                + ("（已满）" if reached else "")
            )
            last_act = state.get("last_activity_at")
            lines.append(
                f"上次活动：{last_act.strftime('%H:%M') if last_act else '无记录'}"
            )
            lines.append(f"今日主动消息：{state.get('message_count', 0)} 条")
            lines.append(
                f"睡眠：{'在睡' if self.gate.is_asleep_now() else '醒着'}"
                "（自主作息，入睡时机由睡意动力学决定）"
            )
            if self.gate.awake_standby_active():
                gate_until = self.gate._awake_until
                until = (
                    gate_until.isoformat(timespec="minutes") if gate_until else "?"
                )
                lines.append(f"清醒待机：生效中（至 {until}）")
            else:
                lines.append("清醒待机：无")
        for line in await self._recent_memory_lines(3, "最近记忆："):
            lines.append(line)
        return lines

    async def _living_mood_lines(self) -> list[str]:
        if self.mood is None:
            return ["[astrbot-living] 心境尚未初始化"]
        lines = ["[astrbot-living] 心境详情", self.mood.digest()]
        lines.append(
            f"valence={self.mood.valence:.2f}（积极↔消极） "
            f"arousal={self.mood.arousal:.2f}（唤醒度）"
        )
        energy_cap = 1.0 - self.mood.sleep_debt / 200.0
        lines.append(
            f"energy={self.mood.energy:.2f}（精力，上限 {energy_cap:.2f}，"
            f"受睡眠债压制） 疲惫={self.mood.fatigue:.0f}/100 "
            f"睡眠债={self.mood.sleep_debt:.0f}/100"
        )
        interests = self.mood.get_interests()
        if interests:
            for k, v in sorted(interests.items(), key=lambda kv: kv[1], reverse=True):
                lines.append(f"- 兴趣 {k}: {v:.2f}")
        else:
            lines.append("（还没有累积出兴趣偏好）")
        return lines

    async def _living_memories_lines(self, n: int) -> list[str]:
        n = max(1, min(n, 20))
        return await self._recent_memory_lines(n, f"[astrbot-living] 最近 {n} 条记忆")

    async def _living_pause_lines(self) -> list[str]:
        loop, hint = self._loop_or_hint()
        if hint:
            return [hint]
        if loop.paused:
            return ["已经在暂停中了（/living resume 恢复）"]
        await loop.pause()
        return ["已暂停自主活动（心跳保持，闸门状态不变；/living resume 恢复）"]

    async def _living_resume_lines(self) -> list[str]:
        loop, hint = self._loop_or_hint()
        if hint:
            return [hint]
        if not loop.paused:
            return ["本来就在运行中"]
        await loop.resume()
        return ["已恢复自主活动"]

    async def _wake_flow_lines(self) -> list[str]:
        """手动唤醒的共享流程（/living wake 与 /living_wake 同一份逻辑）。"""
        loop, hint = self._loop_or_hint()
        if hint:
            return [hint]
        try:
            awake, reason, activity = await loop.heartbeat_once_detailed(force=True)
        except Exception as e:
            logger.exception("[/living wake] 唤醒判定异常")
            return [f"判定出了点岔子：{e}"]
        if awake:
            return [f"醒了！这就去{activity or '忙点什么'}。"]
        return [f"被拦下了：{_WAKE_REASON_TEXT.get(reason, reason)}"]

    async def _living_sleep_lines(self) -> list[str]:
        if self.gate is None:
            return ["闸门未初始化"]
        was_standby = self.gate.awake_standby_active()
        await self.gate.clear_awake_until()
        lines = ["已清除清醒待机。"]
        if self.gate.is_asleep_now():
            lines.append("现在在睡，下次心跳会继续休息。")
        else:
            lines.append("当前不在睡，照常待机。")
        # 告别消息（如配置了）发到待机期最后活跃会话——主人让它睡，它道个晚安
        if was_standby and self.loop is not None:
            try:
                await self.loop._send_sleep_farewell(datetime.now())
            except Exception as e:
                logger.debug(f"[/living sleep] 告别发送失败（不影响）: {e}")
        return lines

    async def _living_do_lines(self, activity: str, topic: str | None) -> list[str]:
        loop, hint = self._loop_or_hint()
        if hint:
            return [hint]
        if not activity:
            names = "/".join(loop.activity_names)
            return [f"用法：/living do <activity> [topic]。可选活动：{names}"]
        if activity not in loop.activity_names:
            names = "/".join(loop.activity_names)
            return [f"未知活动 {activity!r}。可选：{names}"]
        result = await loop.run_activity_cycle(
            force_activity=activity, force_topic=topic
        )
        if result.get("ok"):
            suffix = f"（主题：{topic}）" if topic else ""
            return [f"这就去{activity}了{suffix}。"]
        error = result.get("error", "")
        if error.startswith("daily_limit"):
            return [f"今日活动已达上限（{error}），明天再约。/living config 可调整。"]
        if "未知活动" in error:
            return [error]
        return [f"没做成：{error}"]

    _CONFIG_ALLOWED_GROUPS = (
        "decision", "capabilities", "output_gate", "model",
        "sleep", "memory", "persona", "misc",
    )

    async def _living_config_lines(self, key: str, value: str) -> list[str]:
        if not key or not value:
            return [
                "用法：/living config <group>.<key> <value>",
                f"允许的组：{'/'.join(self._CONFIG_ALLOWED_GROUPS)}",
                "例：/living config decision.daily_impulse_limit 5",
            ]
        if "." in key:
            group, _, k = key.partition(".")
        else:
            # 裸键名：在允许组里找唯一匹配
            hits = [
                (g, kk) for g in self._CONFIG_ALLOWED_GROUPS
                for kk in (self.config.get(g, {}) or {})
                if kk == key
            ]
            if not hits:
                return [f"找不到配置项 {key!r}，请用 组.键 形式。"]
            if len(hits) > 1:
                return [f"键 {key!r} 在多个组里出现，请用 组.键 消歧。"]
            group, k = hits[0]
        group = group.lower()
        if group not in self._CONFIG_ALLOWED_GROUPS:
            return [
                f"不允许修改组 {group!r}。允许：{'/'.join(self._CONFIG_ALLOWED_GROUPS)}"
            ]

        # 类型自动转换：bool → int → float → str（配置热读，下一次判定生效）
        lower = value.lower()
        if lower in ("true", "false"):
            converted: Any = lower == "true"
        else:
            try:
                converted = int(value)
            except ValueError:
                try:
                    converted = float(value)
                except ValueError:
                    converted = value
        try:
            group_cfg = self.config.setdefault(group, {})
            old_value = group_cfg.get(k)
            group_cfg[k] = converted
        except Exception as e:
            return [f"写入失败：{e}"]
        try:
            saver = getattr(self.config, "save_config_async", None)
            if callable(saver):
                await saver()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[/living config] 配置落盘失败（内存已生效）: {e}")
        return [f"已设置 {group}.{k}：{old_value!r} → {converted!r}（热生效）"]

    async def _living_debug_lines(self) -> list[str]:
        lines = ["[astrbot-living] 调试信息"]
        if self.gate is None:
            return lines + ["闸门未初始化"]
        lines.append("— 闸门判定链（本次真实评估）—")
        try:
            for step, verdict in await self.gate.debug_wake_chain():
                lines.append(f"  {step}: {verdict}")
        except Exception as e:
            lines.append(f"  （判定链评估失败：{e}）")
        decision_mode = str(self._cfg("decision", "decision_mode", "hybrid"))
        lines.append(f"决策模式：{decision_mode}")
        try:
            chain = await build_provider_chain(self.context, lambda: self.config)
            ids = [pid for pid, _ in chain]
            lines.append(f"provider 链：{' → '.join(ids) if ids else '（空）'}")
        except Exception as e:
            lines.append(f"provider 链：解析失败（{e}）")
        if (
            self._agent_loop is not None
            and getattr(self._agent_loop, "last_result", None) is not None
        ):
            r = self._agent_loop.last_result
            lines.append(
                f"上次 agent 循环：tokens={r.tokens_used} steps={r.steps_used}"
                f"/{r.max_steps} budget_exceeded={r.budget_exceeded}"
            )
        else:
            lines.append("上次 agent 循环：尚无记录")
        lines.append(f"记忆后端：{self.memory_note}")
        return lines

    def _living_help_lines(self) -> list[str]:
        return [
            "[astrbot-living] 可用子命令：",
            "/living — 状态一览；/living status — 详细状态；/living mood — 心境",
            "/living memories [n] — 最近记忆；/living pause|resume — 暂停/恢复",
            "/living wake — 手动唤醒；/living sleep — 手动入睡",
            "/living do <activity> [topic] — 强制执行（每日上限内）",
            "/living config <group>.<key> <value> — 改配置（热生效）",
            "/living debug — 判定链与 token 统计",
        ]

    @filter.command("living_wake")

    async def living_wake(self, event: AstrMessageEvent):
        """手动唤醒：触发一次判定（豁免概率掷点，仍受休眠/上限/冷却约束）。"""
        yield event.plain_result("收到，判定中…")
        if self.loop is None:
            yield event.plain_result("主循环还没启动，稍后再试")
            return
        try:
            awake, reason, activity = await self.loop.heartbeat_once_detailed(force=True)
        except Exception as e:
            logger.exception("[living_wake] 唤醒判定异常")
            yield event.plain_result(f"判定出了点岔子：{e}")
            return
        if awake:
            yield event.plain_result(f"醒了！这就去{activity or '忙点什么'}。")
        else:
            yield event.plain_result(
                f"被拦下了：{_WAKE_REASON_TEXT.get(reason, reason)}"
            )

    @filter.command("living_wake_now")
    async def living_wake_now(self, event: AstrMessageEvent):
        """紧急唤醒：立即终止本次休眠，清空吵醒计数与待机，立即触发一次
        force 判定。下次入睡仍由睡意动力学决定。"""
        logger.info("[Living] 紧急唤醒：主人强制结束休眠")
        now = datetime.now()
        if self.gate is None or self.sleep_manager is None:
            yield event.plain_result("休眠组件未就绪，稍后再试")
            return
        # M3 补丁 XI-A.3：在自主睡眠中先退出（本次中断不回睡）
        if self.gate.asleep_in_autonomous(now):
            await self.gate.exit_autonomous_sleep(now)
            logger.info("[Living] 紧急唤醒：自主睡眠已终止")
        # 清空吵醒计数与待机状态（从干净状态开始）
        self.sleep_manager.reset_wake_state()
        await self.gate.clear_awake_until()
        yield event.plain_result(
            "已紧急唤醒，本次睡眠结束。下次入睡由睡意动力学决定。"
        )
        # 立即触发一次 force 判定（复用既有链路，照常回复判定结果）
        if self.loop is not None:
            try:
                awake, reason, activity = (
                    await self.loop.heartbeat_once_detailed(force=True, now=now)
                )
                if awake:
                    yield event.plain_result(
                        f"醒了！这就去{activity or '忙点什么'}。"
                    )
                elif reason == "sleeping":
                    # 理论不可达（强醒期内不判 sleeping）——防御提示
                    yield event.plain_result("状态异常，请查看日志")
                else:
                    yield event.plain_result(
                        f"被拦下了：{_WAKE_REASON_TEXT.get(reason, reason)}"
                    )
            except Exception as e:
                logger.exception("[living_wake_now] force 判定异常")
                yield event.plain_result(f"判定出了点岔子：{e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _extract_schedule_safe(self, schedule, text: str) -> None:
        """约定提取的后台包装：任何异常只 debug，绝不影响消息主链路。"""
        try:
            await schedule.maybe_extract(text, datetime.now())
        except Exception as e:
            logger.debug(f"[Schedule] 约定提取失败（忽略）: {e}")

    async def on_any_message(self, event: AstrMessageEvent):
        """所有消息的旁路监听：待机刷新 / 吵醒计数 / 静默拦截（B3/B4 + 补丁 II）。

        顺序敏感：
          1. 待机期内（awake_until 未过期）→ 刷新待机时长（滑动窗）→
             不计数、不拦（醒着聊天，AstrBot 正常回复）；
          2. 否则走吵醒计数——达到阈值的那条消息不拦（被吵醒了就该回应），
             并请求主循环唤醒；
          3. 其余休眠窗内消息按 sleep_mute_replies 拦截。本插件命令不拦。
        """
        # 补丁 XVI：任何真实消息都顺手记录 bot 自身身份（与原生侧同源）——
        # 这是"身份两团"不再复发的基石，必须先于一切早退分支执行。
        # （getattr 保护：便于局部 mock 的测试对象复用本方法）
        remember_identity = getattr(self, "_remember_self_identity", None)
        if callable(remember_identity):
            remember_identity(event)
        # M5-补丁4 A1/A2：起床约定提取——本地词表预筛（零 LLM 零开销），
        # 命中才以后台任务走一次轻量 LLM 确认（不阻塞消息处理链、静默
        # 不打扰对话）；总开关关闭时整条链路零生效
        schedule = getattr(self, "_schedule", None)
        if schedule is not None:
            text = str(getattr(event, "message_str", "") or "")
            if text.strip():
                asyncio.create_task(
                    self._extract_schedule_safe(schedule, text)
                )
        if self.sleep_manager is None:
            return
        now = datetime.now()
        try:
            sender_id = event.get_sender_id()
        except Exception:
            sender_id = None
        try:
            session = event.unified_msg_origin
        except Exception:
            session = None

        # 补丁 II 一：待机期消息只刷新待机（滑动窗），不重复扣睡眠债/
        # 起床气判定，也不拦截
        try:
            if await self.sleep_manager.refresh_standby(now, session=session):
                return
        except Exception as e:
            # 补丁 IX：异常不该静默——主人排查"为什么没反应"时日志要能给答案
            logger.warning(f"[Living] 待机刷新异常（按非待机继续）: {e}")

        try:
            # register_message 是同步方法（纯内存滑动窗），不要 await
            wake_triggered, window_count = self.sleep_manager.register_message(
                now, sender_id, session=session
            )
        except Exception as e:
            # 补丁 IX：计数异常直接影响吵醒功能，WARNING 级留痕
            logger.warning(f"[Living] 消息计数异常（跳过）: {e}")
            return
        if wake_triggered:
            logger.info("[Living] 睡眠中被连续消息吵醒，请求主循环唤醒")
            if self.loop is not None:
                self.loop.request_wake()
            return  # 触发吵醒的这条不拦
        # 补丁 IX 需求 1：窗内逐条消息的计数进度 INFO——主人能实时看到
        # "还差几条吵醒"（观测原则：影响响应行为的路径必须 INFO 可见）
        if self.sleep_manager.last_window_count:
            # 模块级 conf_group（非 self._cfg）：消息监听热路径上的局部
            # mock 对象只带 config 属性，不带完整插件方法
            threshold = self.sleep_manager._i(
                conf_group(self.config, "sleep").get("wake_n_messages"), 3
            )
            logger.info(
                f"[Living] 休眠计数 {window_count}/{threshold}"
            )
        try:
            message_str = event.message_str
        except Exception:
            message_str = ""
        if self.sleep_manager.should_mute_message(now, message_str):
            # 拦截 = 事件不再向后续插件 handler 与 LLM 回复管线传播
            #（scheduler 逐阶段检查 is_stopped）——主人定稿的"真正休息"。
            # 补丁 IX：INFO 级 + 带行动指引，杜绝"为什么没回复"的误判
            context_text = self.sleep_manager.describe_mute(now, window_count)
            event.stop_event()
            logger.info(f"[Living] 睡眠期消息已拦截：{context_text}")

    async def terminate(self) -> None:
        """插件卸载/停用时由 AstrBot 调用；重复调用安全。"""
        if self.loop is not None:
            await self.loop.stop()
            self.loop = None
        for stale_task_name in (
            "_selfheal_task", "_interest_cooldown_task", "_knobs_task"
        ):
            stale_task = getattr(self, stale_task_name, None)
            if stale_task is None or stale_task.done():
                setattr(self, stale_task_name, None)
                continue
            # 一次性后台任务：卸载时若还没跑完就取消（下次启动重跑，
            # 幂等状态保证不重复修正）
            stale_task.cancel()
            try:
                await stale_task
            except (asyncio.CancelledError, Exception):
                pass
            setattr(self, stale_task_name, None)
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
