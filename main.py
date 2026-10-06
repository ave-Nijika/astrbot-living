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
import os
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
from .core.activities import web_search_enabled as _web_search_enabled
from .core.fetcher import WebFetcher
from .core.ghost_event import GHOST_PLATFORM_ID, build_ghost_event
from .core.initiative import InitiativeEngine
from .core.judge import OutputJudge
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
from .core.style_learning import StyleLearner

PLUGIN_NAME = "astrbot_plugin_living"

# 闸门拦截原因 → 给主人看的一句话（/living_wake 反馈用）
_WAKE_REASON_TEXT = {
    "sleeping": "我在睡觉呢（自主作息），不忍心叫就别叫我啦",
    "daily_limit": "今天已经玩够了（每日活动上限）",
    "cooldown": "刚忙完，还在歇着（冷却中）",
    "rolled_off": "想了想暂时不想动（概率掷点落空，再叫一次就好）",
}


def _merge_config_defaults(refer: dict, conf: dict) -> dict:
    """按 default 树递归合并（M9-补丁2 A2）：磁盘值优先，缺键/None 补默认。

    语义对齐 AstrBotConfig.check_config_integrity（缺键插入 schema default、
    None 视同缺失）；磁盘上 schema 没有的键原样保留（对齐其 update(conf)
    行为）。refer 分支深拷贝：default 树按实例缓存，防止调用方 mutate
    合并结果时污染缓存。"""
    out: dict = dict(conf)
    for key, value in refer.items():
        if key not in conf or conf[key] is None:
            out[key] = json.loads(json.dumps(value))  # 深拷贝（纯 JSON 树）
        elif isinstance(value, dict) and isinstance(conf[key], dict):
            out[key] = _merge_config_defaults(value, conf[key])
    return out


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
        # M9-补丁3：存量身份回填后台任务（引用存实例防 GC，并发去重）
        self._backfill_task: asyncio.Task | None = None
        # M19-补丁1：判断模型引擎（构造轻量，initialize 装配）；输出侧
        # log_only 的后台检查任务引用集合（防 GC）
        self._judge: OutputJudge | None = None
        self._judge_tasks: set[asyncio.Task] = set()

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

    def _plugin_config_path(self) -> str:
        """插件配置文件的磁盘路径（AstrBot 侧命名规则：插件目录名_config.json，
        见 star_manager 的插件配置构造）。"""
        import os

        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        return os.path.join(get_astrbot_config_path(), f"{PLUGIN_NAME}_config.json")

    def _schema_defaults(self) -> dict:
        """schema 默认树（preset/advanced 同构），按实例缓存。

        schema 随插件部署不变（热重载重建实例即刷新）；缓存的是默认树
        而非配置——配置本体在 _effective_config 里每次现读，无缓存。"""
        cached = getattr(self, "_schema_defaults_cache", None)
        if cached is None:
            from .core.panel_api import default_tree, load_schema

            cached = default_tree(load_schema(Path(__file__).resolve().parent))
            self._schema_defaults_cache = cached
        return cached

    def _effective_config(self) -> dict:
        """运行时配置的磁盘直读（M9-补丁2 A1）——装配处 config_getter 的实现。

        为什么不直接用 self.config：面板保存插件配置时 AstrBot 只写磁盘并
        更新 dashboard 侧实例，运行中插件持有的 self.config 不同步
        （v4.28.0-beta.1 实测）→ 所有热读拿到的都是启动时旧值。改为每次
        现读磁盘文件（json + schema 缺键补默认，A2），面板保存即热生效。

        容错（A3）：文件不存在（AstrBot 尚未持久化过插件配置）/JSON 损坏/
        结构异常 → 回落 self.config 并 WARNING，心跳不因读取失败而挂。
        """
        path = self._plugin_config_path()
        try:
            with open(path, encoding="utf-8-sig") as f:
                conf = json.load(f)
            if not isinstance(conf, dict):
                raise ValueError("配置文件顶层不是 JSON 对象")
        except FileNotFoundError:
            return self.config
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] 配置文件读取失败（回落运行时配置）: {e}"
            )
            return self.config
        return _merge_config_defaults(self._schema_defaults(), conf)

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
    async def _decision_llm_call(
        self,
        prompt: str,
        system_prompt: str | None,
        contexts: list | None = None,
    ):
        """决策 LLM 调用（decider/梦/分享改写共用），带模型故障转移链。

        provider 选择（任务书 M3-补丁 问题 1）：fallback_chain 配置链在前，
        全部已启用 chat provider 兜底；只有 404/429/超时/连接类错误才切换，
        401 等换模型解决不了的直接放弃（返回 None，由决策层静默回退）。

        M12-补丁1 B2：新增可选 contexts（分享改写的真实聊天历史，dict
        列表 role/content 形态——与 AstrBot 真实聊天链路
        `req.contexts = json.loads(conversation.history)` 同构）。decider/
        梦的既有双参调用零变化。
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
                    # M12-补丁1 B2：分享改写的真实聊天上下文透传（None 时
                    # 不改变请求形态——decider/梦的既有调用零变化）
                    contexts=contexts or None,
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

    # ------------------------------------------------------------------
    # 判断模型（M19-补丁1 A/B/C/E 组）
    # ------------------------------------------------------------------
    async def _judge_llm_call(
        self, prompt: str, system_prompt: str | None
    ) -> str | None:
        """判断模型的 LLM 调用：固定走 judge.provider_id（A4，不与决策链
        混用——判断模型应该又小又快，跟决策模型不是一个东西）。provider
        未选/不存在 → 记 WARNING（60s 节流防刷屏）并返回 None，按"本轮
        不判断"处理——不得报错、不得阻断聊天。"""
        try:
            pid = str(conf_group(self._effective_config(), "judge").get("provider_id") or "").strip()
        except Exception:
            pid = ""
        if not pid:
            self._judge_warn_throttled("provider 未配置")
            return None
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=pid,
                prompt=prompt,
                system_prompt=system_prompt or None,
            )
        except Exception as e:
            self._judge_warn_throttled(f"provider {pid!r} 调用失败: {summarize_provider_error(e)}")
            return None
        text = getattr(resp, "completion_text", None)
        if not text:
            try:
                components = getattr(getattr(resp, "result_chain", None), "chain", None) or []
                if components:
                    text = getattr(components[0], "text", None)
            except Exception:
                text = None
        text = str(text or "").strip()
        if not text or looks_like_llm_error_output(text):
            return None
        return text

    def _judge_warn_throttled(self, reason: str) -> None:
        """provider 配置类 WARNING 的节流（60s 最多一条，其余 DEBUG）——
        输出侧每条回复都可能触发，不节流会刷爆日志。"""
        now = datetime.now()
        last = getattr(self, "_judge_warn_last", None)
        if last is None or (now - last).total_seconds() >= 60:
            self._judge_warn_last = now
            logger.warning(f"[Judge] {reason}（按'本轮不判断'处理）")
        else:
            logger.debug(f"[Judge] {reason}")

    def _judge_provider_ids(self) -> list[str]:
        """已启用 chat provider 的 id 清单（面板下拉数据源，F2/E3）。

        context.get_all_providers() 只返回 chat_completion 类型且已启用的
        provider（embedding/STT 不在内，与 build_provider_chain 同款口径）；
        任何异常回落空列表（面板显示"无可用 provider"，不崩）。"""
        try:
            providers = self.context.get_all_providers() or []
            ids = []
            for provider in providers:
                try:
                    pid = str(provider.meta().id)
                except Exception:
                    continue
                if pid:
                    ids.append(pid)
            return ids
        except Exception:
            return []

    def _wake_prefixes(self) -> list[str]:
        """全局唤醒/命令前缀（B1 命令跳过规则的判定依据）。读不到按
        ["/"]（本体默认）处理。"""
        try:
            get_config = getattr(self.context, "get_config", None)
            cfg = get_config() if callable(get_config) else {}
            raw = (cfg or {}).get("provider_settings", {}).get("wake_prefix")
            if isinstance(raw, (list, tuple)) and raw:
                return [str(p) for p in raw if str(p)]
            if isinstance(raw, str) and raw:
                return [raw]
        except Exception:
            pass
        return ["/"]

    def _judge_record_task(self, coro) -> None:
        """fire-and-forget 任务登记（输出侧 log_only 检查不阻塞回复送达；
        引用存集合防 GC，完成后自动清理）。"""
        task = asyncio.create_task(coro)
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)

    @filter.on_llm_request()
    async def judge_input_on_llm_request(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """B 组：输入侧判断（主人发消息时给建议，追加到请求末尾）。

        红线逐条：
        - 默认 off → 第一行即返回（零调用、零注入、零行为变化）；
        - local → 一次性 WARNING 明确提示未实现，不注入不降级（A3）；
        - 只追加 extra_user_content_parts（TextPart.mark_as_temp()，不留痕
          不写会话历史）——与 M17-补丁1 语气注入同一通道（红线 4）；
        - 限频/极短/命令前缀跳过（B1）；超时/异常只 DEBUG，主回复照常
          （B2/B3/红线 6）；
        - 判断结果用完即弃：本钩子不写任何存储（红线 1）。"""
        try:
            judge = self._judge
            if judge is None:
                return
            mode = judge.mode()
            if mode == "local":
                judge.warn_local_once()
                return
            if mode != "api":
                return
            message_text = str(getattr(event, "message_str", "") or "")
            stripped = message_text.strip()
            if not stripped:
                return
            # B1 命令跳过：命中全局唤醒/命令前缀的消息不判断
            for prefix in self._wake_prefixes():
                if prefix and stripped.startswith(prefix):
                    return
            skip = judge.should_skip_input(message_text)
            if skip:
                logger.debug(f"[Judge] 输入判断跳过（{skip}）")
                return
            context_lines = self._judge_context_lines(req)
            verdict = await judge.judge_input(message_text, context_lines)
            if verdict is None:
                return
            injection = judge.build_input_injection(verdict)
            if not injection:
                return
            from astrbot.core.agent.message import TextPart

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                return  # 本体形态有变时安全退出（不注入、不报错）
            parts.append(TextPart(text=injection).mark_as_temp())
            judge.record_input_injected(message_text, verdict)
            logger.debug("[Judge] 输入判断建议已注入（用完即弃）")
        except Exception as e:
            logger.debug(f"[Judge] 输入判断失败（不影响聊天）: {e}")

    def _judge_context_lines(self, req: Any) -> list[str]:
        """输入/输出判断的上文材料：只取 role/content 两个字段（安全红线：
        其余元数据一律不带入 prompt 链），尾部 N 条（judge.context_messages）。"""
        lines: list[str] = []
        try:
            from .core.conf_path import conf_group

            limit = self._int_from_config("judge", "context_messages", 6, 12)
            contexts = getattr(req, "contexts", None) or []
            for message in list(contexts)[-limit:]:
                if isinstance(message, dict):
                    role = str(message.get("role") or "")
                    content = message.get("content")
                else:
                    role = str(getattr(message, "role", "") or "")
                    content = getattr(message, "content", None)
                if role == "system":  # system 提示不进判断材料（只看对话）
                    continue
                if isinstance(content, list):  # 多模态 parts → 取文本部分
                    content = " ".join(
                        str(getattr(part, "text", "") or "")
                        for part in content
                    )
                content = str(content or "").strip().replace("\n", " ")
                if not content:
                    continue
                who = "主人" if role == "user" else "助手"
                lines.append(f"{who}：{content[:80]}")
        except Exception as e:
            logger.debug(f"[Judge] 上文材料整理失败（按无上文继续）: {e}")
        return lines

    def _int_from_config(self, group: str, key: str, default: int, cap: int) -> int:
        try:
            value = int(conf_group(self._effective_config(), group).get(key, default))
        except Exception:
            return default
        return min(max(value, 0), cap)

    @filter.on_llm_response()
    async def judge_output_on_llm_response(
        self, event: AstrMessageEvent, response: Any
    ) -> None:
        """C 组：输出侧检查（聊天模型回复后过一遍）。

        - log_only（默认）：后台任务检查+记录，回复照常送达（不等判断）；
        - rewrite：同步打回（钩子内 await），最多重写 1 次 + 超时，失败
          放行原回复——修改走 response.completion_text setter（同步更新
          result_chain，与本体消费同一形态）；
        - 流式 chunk（is_chunk）不判（每 chunk 一判既烧钱又没法整体改）；
        - 任何异常只 DEBUG，绝不影响回复送达（红线 6）。"""
        try:
            judge = self._judge
            if judge is None or not judge.enabled():
                return
            if bool(getattr(response, "is_chunk", False)):
                return
            reply = str(getattr(response, "completion_text", "") or "").strip()
            if len(reply) < 8:  # 极短回复没有"惯性"可言，不烧判断
                return
            action = judge.output_action()
            context_lines = []  # 输出侧暂不带会话上下文（req 已不可得）
            if action == "rewrite":
                fixed = await judge.rewrite_output(reply, context_lines)
                if fixed:
                    response.completion_text = fixed
                    logger.info("[Judge] 输出检查打回重写（轻量修正已应用）")
            else:
                # log_only：后台检查，不阻塞回复送达
                self._judge_record_task(judge.check_output(reply, context_lines))
        except Exception as e:
            logger.debug(f"[Judge] 输出检查失败（不影响回复）: {e}")

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
            # M9-补丁3：身份首次可用 → 后台回填存量无身份的 living 记忆
            # （幂等；绝不能阻塞消息链路，故 create_task 且不等待）
            self._schedule_ghost_backfill()
        except Exception as e:
            logger.debug(f"[Living] 自身身份采集失败（忽略）: {e}")

    def _schedule_ghost_backfill(self) -> None:
        """存量身份回填的后台触发（M9-补丁3）。

        同一时刻只允许一个回填任务在跑/排队（并发去重）；任务引用存实例
        属性防止被垃圾回收（asyncio 官方建议）。触发本身绝不抛异常——
        身份采集在消息链路上，回填只是它的副产品。
        """
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # 非事件循环上下文（同步调用）：跳过，不留悬空协程
            prev = getattr(self, "_backfill_task", None)
            if prev is not None and not prev.done():
                return
            self._backfill_task = asyncio.create_task(
                self._run_ghost_backfill_once()
            )
        except Exception as e:
            logger.debug(f"[Living] 存量回填触发失败（忽略）: {e}")

    async def _run_ghost_backfill_once(self) -> None:
        """存量回填执行体：身份可用 + LivingMemory 就绪才动手（M9-补丁3）。

        与 _run_identity_selfheal_with_retry 的差异：那个等引擎就绪（启动
        期最多 10 分钟），这个由身份缓存成功事件触发（引擎通常已就绪），
        探测一次不就绪就放弃——下一条真实消息会再次触发，不必在这里等。
        """
        try:
            from .core.memory_backend import LivingMemoryBackend
            from .core.selfheal import run_ghost_identity_backfill

            identity = await self._bot_identity()
            if not identity or self._identity_is_polluted(
                identity.get("identity_key")
            ):
                return
            backend = await self._get_memory()
            if not isinstance(backend, LivingMemoryBackend):
                return  # Simple 后端无 documents/图谱，无事可做
            await run_ghost_identity_backfill(
                getattr(backend, "engine", None), identity
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[SelfHeal] 存量身份回填异常（不影响服务）: {e}")

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

    async def _livingmemory_conversation_manager(self):
        """livingmemory 插件的会话管理器（M13-补丁1 B1）。

        只经插件注册表拿公开属性（与 memory_backend.probe 同款软依赖，
        绝不 import 其源码）：star_cls.initializer.conversation_manager。
        每次现取——插件异步初始化完成前/重载期间拿不到就返回 None，
        调用方按"不可用"跳过该落点。任何失败都吞掉（DEBUG 留底）。
        """
        try:
            get_star = getattr(self.context, "get_registered_star", None)
            if not callable(get_star):
                return None
            meta = get_star("astrbot_plugin_livingmemory")
            if meta is None or not getattr(meta, "activated", False):
                return None
            initializer = getattr(getattr(meta, "star_cls", None), "initializer", None)
            mgr = getattr(initializer, "conversation_manager", None)
            if mgr is not None and callable(getattr(mgr, "add_message", None)):
                return mgr
        except Exception as e:
            logger.debug(f"[Living] livingmemory 会话管理器探测失败（跳过）: {e}")
        return None

    # ------------------------------------------------------------------
    # 主动搭话念头系统接线（M14-补丁1）：引擎的会话/上下文/双写回调
    # 都转发给 loop 的既有实现——三段会话优先级与双存储落库不复制第二份
    # ------------------------------------------------------------------
    def _initiative_session(self) -> str | None:
        """念头目标会话（I3）：复用 loop 的三段优先级解析，取第一个。"""
        if self.loop is None:
            return None
        sessions, _source = self.loop._resolve_target_sessions()
        return sessions[0] if sessions else None

    async def _initiative_chat_contexts(self):
        """open_topic 的话题材料（B2）：复用 loop 的真实聊天上下文读取。"""
        if self.loop is None:
            return None
        sessions, _source = self.loop._resolve_target_sessions()
        return await self.loop._load_chat_contexts(sessions)

    async def _initiative_speech_write(self, text: str, dedup_key: str) -> None:
        """念头台词双写（E2）：复用 M13-补丁1 的共享落库；占位与日志
        标签在这一层固定。"""
        if self.loop is None:
            return
        await self.loop._write_speech_to_stores(
            text, dedup_key, "(主动搭话)", label="主动搭话"
        )

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
    # 风格注入（M17-补丁1 A5）：四路说话入口共用的低调用料
    # ------------------------------------------------------------------
    def _style_hint_silent(self) -> str:
        """同步风格注入块 getter（ShareRewriter 挂载点）。

        库空/关闭/异常一律空串——注入是锦上添花，任何故障都静默跳过
        （红线 1/5），绝不影响分享改写主链路。"""
        learner = getattr(self, "_style_learner", None)
        if learner is None:
            return ""
        try:
            return str(learner.inject_block() or "").strip()
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 风格提示生成失败（跳过）: {e}")
            return ""

    async def _persona_with_style(self) -> str | None:
        """主动搭话的人格 + 风格注入（M17-补丁1 A5）。

        InitiativeEngine 会把返回值当 persona 截前 500 字（红线 7 不改
        initiative.py），所以这里先预截人格再接风格块，保证两段都完整
        进入 system prompt：风格块硬控 120 字（主动搭话是"一句话"场景，
        提示宜短），人格保底 200 字、上限 500-块长。"""
        try:
            persona = await self._persona_prompt()
        except Exception:
            persona = None
        persona = str(persona or "").strip()
        style = self._style_hint_silent()[:120]
        if not style:
            return persona or None
        budget = max(500 - len(style) - 2, 200)
        persona = persona[:budget]
        combined = f"{persona}\n\n{style}" if persona else style
        return combined or None

    @filter.on_llm_request()
    async def inject_style_on_llm_request(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """正常对话的语气注入（M17-补丁1 A5，living 第一次介入正常对话）。

        红线 6 的三条：
        - 只追加：注入走 req.extra_user_content_parts 追加型通道，绝不
          覆盖/重排既有 system_prompt 与 contexts——astrbot_plugin_
          prompt_preset 也挂本钩子且会整体替换 system_prompt，追加通道
          与它正交（先替换后追加，两者都生效，T16 验证此场景）；
        - 不留痕：TextPart.mark_as_temp() 使注入只面向本轮 provider，
          不写进会话历史存储；
        - 稳：钩子内任何异常一律吞掉，绝不让主人的正常聊天失败。"""
        try:
            learner = getattr(self, "_style_learner", None)
            if learner is None or not learner.enabled():
                return
            hint = str(learner.inject_block() or "").strip()
            if not hint:
                return
            from astrbot.core.agent.message import TextPart

            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                return  # 本体形态有变时安全退出（不注入、不报错）
            parts.append(TextPart(text=hint).mark_as_temp())
            logger.debug("[living] 已在正常对话注入语气参考（风格学习）")
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 语气注入失败（不影响聊天）: {e}")

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
        """按当前 autonomy 配置组装生活工具集（M15-补丁1 起支持 async——
        D 组 persona 档要 await persona_manager 取本体筛选结果）。

        由 LivingAgentLoop 在每次活动前调用（补丁 XIII-P1）——
        档位/写层级配置热读，改配置下个活动周期即生效，无需重启插件。
        """
        return self._build_agent_tools_async()

    async def _build_agent_tools_async(self):
        # M18-补丁1 C2：同一函数统一运行时同源——原先 tier/write_level 取
        # self.config（面板保存后不同步的启动时旧值），web_search_enabled
        # 却取 _effective_config（磁盘直读），同函数两来源。现统一磁盘直读。
        config = self._effective_config()
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
            # M15-补丁1 E2：博查搜索独立开关（关 = web_search 不挂载）
            web_search_enabled=_web_search_enabled(config),
            # M15-补丁1 C0：截图"能看"三路（模态探针 + 本体转述闭包）
            image_probe=self._activity_image_probe(),
            image_captioner=self._caption_screenshot,
        )
        # M15-补丁1 D 组：按 agent_tools_mode 追加本体工具（含 MCP）。
        # living 自带四件套与档位工具始终保留（她的核心生活能力，不随
        # 本体工具开关变动，红线 5）；同名冲突以 living 自带优先（D3）。
        await self._append_agent_tools(tools)
        # 补丁 XV 清单2：档位日志改用 build_tool_manifest（"预期清单"），
        # 与"实际挂载"并排——两者不一致即装配有缺，一眼可查。
        # M15-补丁1 D5：追加结果天然反映在"实际挂载"清单里。
        manifest = build_tool_manifest(
            tier,
            write_level,
            # M15-补丁2 A2 闭环：fail-closed 挂载后须同口径——未装 Chromium
            # 时实际不挂五件套，manifest 也必须同步不列，"清单 vs 实际挂载"
            # 的一致性日志才有意义（否则天天假报不一致）。
            has_browser=browser_session is not None
            and self._chromium_ready(),
            has_workspace=bool(self._living_workspace()),
            # M15-补丁3 A3 顺手：搜索关闭时实际不挂 web_search，清单同口径
            has_search=_web_search_enabled(config),
        )
        logger.info(
            f"[{PLUGIN_NAME}] 档位={tier}({TIER_NAMES.get(tier, '?')}) "
            f"写层级={write_level}({WRITE_LEVEL_NAMES.get(write_level, '?')}) "
            f"清单={manifest} 实际挂载={[t.name for t in tools.tools]}"
        )
        return tools

    def _activity_image_probe(self):
        """C0-2/C0-3 的活动模型模态探针：读 agent_loop 本轮 provider。

        返回闭包（None 结果 = 探针不可用），工具侧把"未知"按支持处理，
        交本体 runner 的模态检查做最终裁决——现状行为，不误杀。
        """
        loop = getattr(self, "_agent_loop", None)
        if loop is None:
            return None

        def probe():
            from .core.living_tools import provider_supports_image

            provider = getattr(loop, "current_provider", None)
            if provider is None:
                return None
            return provider_supports_image(provider)

        return probe

    async def _caption_screenshot(self, image_path: str) -> str | None:
        """C0-3-①：复用本体 _ensure_img_caption 转述截图（同款语义：
        压缩→转述→图片移除，失败走本体内部占位处理）。

        未配置 default_image_caption_provider_id 返回 None（调用方按
        "图片移除 + DEBUG" 兜底）——不自造第二套转述逻辑。
        """
        try:
            from astrbot.core.astr_main_agent import _ensure_img_caption
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 本体转述函数不可用（跳过转述）: {e}")
            return None
        try:
            from astrbot.core.provider.entities import ProviderRequest

            umo = build_ghost_event().unified_msg_origin
            getter = getattr(self.context, "get_config", None)
            cfg = {}
            if callable(getter):
                cfg = getter(umo=umo).get("provider_settings", {}) or {}
            provider_id = str(
                cfg.get("default_image_caption_provider_id") or ""
            ).strip()
            if not provider_id:
                return None
            req = ProviderRequest(
                prompt="Please describe the image.", image_urls=[image_path]
            )
            await _ensure_img_caption(
                build_ghost_event(), req, cfg, self.context, provider_id
            )
            parts = []
            for part in getattr(req, "extra_user_content_parts", None) or []:
                text = getattr(part, "text", None)
                if text:
                    parts.append(str(text))
            text = "\n".join(parts).strip()
            if not text or "[Image Captioning Failed]" in text:
                return None
            # 本体包了 <image_caption> 标签——剥出内文，工具层统一包装
            if text.startswith("<image_caption>") and text.endswith(
                "</image_caption>"
            ):
                text = text[len("<image_caption>"):-len("</image_caption>")]
            return text.strip() or None
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 截图转述失败（按未转述处理）: {e}")
            return None

    async def _append_agent_tools(self, tools) -> None:
        """D1：agent_tools_mode 三档——off 逐字现状；persona 按当前人格的
        tools 筛选本体工具集（复用本体筛选语义，不另造界面）；custom 按
        白名单。D3：与 living 自带同名时以自带优先（她的 surf/read 依赖
        自建 searcher/fetcher 的注入与脱敏语义），冲突逐条记 INFO（D3
        报告清单的数据源）。"""
        from astrbot.core.agent.tool import ToolSet

        mode = str(
            self._cfg("capabilities", "agent_tools_mode", "off") or "off"
        ).strip().lower()
        if mode not in ("persona", "custom"):
            return
        if mode == "custom":
            whitelist = [
                s.strip()
                for s in str(
                    self._cfg("capabilities", "agent_tools", "") or ""
                ).split(",")
                if s.strip()
            ]
            extra = self._whitelisted_toolset(whitelist, ToolSet)
        else:
            extra = await self._persona_filtered_toolset(ToolSet)
        added: list[str] = []
        skipped: list[str] = []
        for tool in list(extra):
            name = getattr(tool, "name", "")
            if not name:
                continue
            if tools.get_tool(name) is not None:
                skipped.append(name)
                continue
            tools.add_tool(tool)
            added.append(name)
        if added:
            logger.info(f"[{PLUGIN_NAME}] 已追加本体工具 {len(added)} 个: {added}")
        if skipped:
            logger.info(
                f"[{PLUGIN_NAME}] 本体工具与 living 自带同名，以 living 自带优先: "
                f"{skipped}"
            )

    def _llm_tool_manager(self):
        """本体工具注册中心（get_full_tool_set 含 MCP 工具）；取不到回 None。"""
        getter = getattr(self.context, "get_llm_tool_manager", None)
        try:
            return getter() if callable(getter) else None
        except Exception:
            return None

    def _whitelisted_toolset(self, whitelist: list[str], toolset_cls) -> Any:
        """custom 档：按白名单从工具管理器取工具（本体 get_func 同款）。"""
        mgr = self._llm_tool_manager()
        toolset = toolset_cls()
        for name in whitelist:
            try:
                tool = mgr.get_func(name) if mgr is not None else None
            except Exception:
                tool = None
            if tool is not None and getattr(tool, "active", True):
                toolset.add_tool(tool)
            else:
                logger.debug(f"[{PLUGIN_NAME}] 白名单工具不可用，跳过: {name}")
        return toolset

    async def _persona_filtered_toolset(self, toolset_cls) -> Any:
        """persona 档：复用本体 astr_main_agent 的筛选语义（逐分支对齐）——
        persona 存在且 tools 为 None（或无人格）→ 全量工具集去 inactive；
        tools=[] → 空集（人格里明确禁用）；tools 列表 → 白名单逐个 get_func。"""
        mgr = self._llm_tool_manager()
        if mgr is None:
            return toolset_cls()
        persona = None
        try:
            pm = getattr(self.context, "persona_manager", None)
            getter = getattr(pm, "get_default_persona_v3", None)
            if callable(getter):
                persona = await getter(build_ghost_event().unified_msg_origin)
        except Exception:
            persona = None
        if isinstance(persona, dict):
            tools_cfg = persona.get("tools")
        elif persona is not None:
            tools_cfg = getattr(persona, "tools", None)
        else:
            tools_cfg = None
        if (persona and tools_cfg is None) or not persona:
            toolset = mgr.get_full_tool_set()
            for tool in list(toolset):
                if not getattr(tool, "active", True):
                    toolset.remove_tool(tool.name)
            return toolset
        toolset = toolset_cls()
        if tools_cfg:
            for name in tools_cfg:
                try:
                    tool = mgr.get_func(str(name))
                except Exception:
                    tool = None
                if tool is not None and getattr(tool, "active", True):
                    toolset.add_tool(tool)
        return toolset

    @staticmethod
    def _chromium_ready() -> bool:
        """M15-补丁2 A2：Chromium 可用性（与 living_tools 挂载判定同口径）。

        fail-closed：探测返回 False/None（未装/探测失败）都视为不可用。
        """
        try:
            # 与 living_tools 挂载判定同源取符号——测试对
            # `core.living_tools.chromium_installed` 的 monkeypatch 同时
            # 作用于"挂载"与"manifest"两处，口径天然一致。
            from .core.living_tools import chromium_installed

            return chromium_installed() is True
        except Exception:
            return False

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
            config_getter=self._effective_config,
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
            config_getter=self._effective_config,
            gate=self.gate,
            llm_call=self._decision_llm_call,
        )
        self.sleep_manager = SleepManager(
            config_getter=self._effective_config,
            gate=self.gate,
            mood=self.mood,
            schedule=self._schedule,
            # M9-补丁1：主人身份自动认领——owner_id 未手填时派生自管理员
            global_config_getter=lambda: self.context.astrbot_config,
        )
        # M17-补丁1 C1/C2：重启时若仍在睡，恢复入睡时抽定的吵醒阈值与
        # 睡眠期未回消息留档（取不到/解析失败静默按默认继续）
        try:
            await self.sleep_manager.restore_wake_threshold()
            await self.sleep_manager.load_pending_messages()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 睡眠侧状态恢复失败（按默认继续）: {e}")
        # M17-补丁1 A 组：风格学习引擎——素材库 style_pool.json 在插件
        # 数据目录，与记忆/图谱/会话存储完全分开（红线 2，不参与记忆召回）；
        # 学习材料来自 fetcher 的最近抓取留档（read/surf 脚本与 agent 两种
        # 执行形态统一覆盖，A7）
        self._style_learner = StyleLearner(
            config_getter=self._effective_config,
            llm_call=self._decision_llm_call,
            pool_path=os.path.join(self._plugin_data_dir(), "style_pool.json"),
            sample_getter=self.fetcher.recent_samples,
            search_enabled_getter=lambda: _web_search_enabled(
                self._effective_config()
            ),
        )
        agent_loop = LivingAgentLoop(
            context=self.context,
            config_getter=self._effective_config,
            persona_getter=self._persona_prompt,
            life_extra_getter=lambda: str(self._preset("life_extra", "") or ""),
            mood=self.mood,
            tool_builder=self._build_agent_tools,
        )
        # M15-补丁1 C0：模态探针经 self._agent_loop 读"本轮 provider"；
        # tool_builder 在 _run_with_provider 内调用时该值已就位
        self._agent_loop = agent_loop
        decider = ActivityDecider(
            # 补丁 XV 清单3：free 开关在构造期先滤一次（decider/loop 内部
            # 还会按配置现读，双保险保热生效）
            activities=default_activities(
                enabled_free=self._free_activity_enabled()
            ),
            config_getter=self._effective_config,
            llm_call=self._decision_llm_call,
            mood=self.mood,
            persona_getter=self._persona_prompt,
            life_extra_getter=lambda: str(self._preset("life_extra", "") or ""),
            memory_getter=self._get_memory,
        )
        self.loop = LivingLoop(
            gate=self.gate,
            memory_getter=self._get_memory,
            config_getter=self._effective_config,
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
                config_getter=self._effective_config,
                persona_getter=self._persona_prompt,
                life_extra_getter=lambda: str(
                    self._preset("life_extra", "") or ""
                ),
                mood=self.mood,
                # M17-补丁1 A5：学到的说话语气参考（同步 getter，空串跳过）
                style_hint_getter=self._style_hint_silent,
            ),
            bot_identity_getter=self._bot_identity,
            # M13-补丁1 B1：livingmemory 会话管理器动态探测——活动自述写进
            # 它的会话存储，MemoryReflection 才能把活动当对话总结进图谱
            lm_conversation_manager_getter=self._livingmemory_conversation_manager,
            # M14-补丁1 A1/I 组：主动搭话念头引擎（主动出口第二条通路）——
            # 会话解析/话题材料/双写落库经下方三个 _initiative_* 方法转发
            # 给 loop 既有实现，不复制第二份；任何失败在引擎内部静默降级
            initiative=InitiativeEngine(
                config_getter=self._effective_config,
                gate=self.gate,
                llm_call=self._decision_llm_call,
                mood=self.mood,
                sender=self.sender,
                # M17-补丁1 A5：人格 + 风格注入（包装版 persona getter，
                # initiative.py 本体零改动——红线 7）
                persona_getter=self._persona_with_style,
                session_getter=self._initiative_session,
                contexts_getter=self._initiative_chat_contexts,
                speech_writer=self._initiative_speech_write,
            ),
            # M9-补丁1：主人身份自动认领——target_sessions 未手填时派生
            # 全部管理员的私聊会话（与 _bot_identity_getter 同款注入先例）
            global_config_getter=lambda: self.context.astrbot_config,
            # M12-补丁1：真实聊天历史（ConversationManager 公开 API）——
            # 分享改写的完整上下文来源；取不到时 loop 内部静默按无上下文处理
            conversation_manager=getattr(self.context, "conversation_manager", None),
            # M15-补丁1 A3：晚安 LLM 档的人格 system prompt（复用主人格读取）
            persona_getter=self._persona_prompt,
            # M17-补丁1 A 组：风格学习引擎（A7 学习触发 / A5 梦话注入）
            style_learner=self._style_learner,
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
        # M19-补丁1：判断模型引擎（A/B/C/E 组）——记录文件在插件数据目录，
        # 与记忆/图谱/会话存储完全分开（红线 1 的物理隔离）；llm_call 固定
        # 走 judge.provider_id（不与决策链混用，A4）
        self._judge = OutputJudge(
            config_getter=self._effective_config,
            llm_call=self._judge_llm_call,
            records_path=os.path.join(self._plugin_data_dir(), "judge_records.json"),
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
            (f"{prefix}/browser_status", self._api_browser_status_get, ["GET"], "浏览器能力状态"),
            (f"{prefix}/judge_records", self._api_judge_records_get, ["GET"], "判断记录读取"),
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
            # M18-补丁1 C1：以运行时同源为准（_effective_config 磁盘直读
            # + schema 缺键补默认）——手改 JSON 后面板显示的也是运行时
            # 真实使用的值；保存链路不变（仍写 self.config + save_config）
            # M19-补丁1 F2/E3：providers 下拉数据源（已启用 chat provider id）
            payload = build_config_payload(
                self._effective_config(),
                self._panel_schema(),
                providers=self._judge_provider_ids(),
            )
            return {"status": "ok", "data": payload}
        except PanelApiError as e:
            return {"status": "error", "message": str(e)}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 面板读取失败")
            return {"status": "error", "message": "内部错误"}

    async def _api_judge_records_get(self):
        """E1：判断记录读取（最近 N 条，新的在前）。"""
        try:
            judge = self._judge
            if judge is None:
                return {"status": "ok", "data": {"records": []}}
            return {"status": "ok", "data": {"records": judge.records()}}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 判断记录读取失败")
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
            # M18-补丁1 B1：基线随默认值一并归位——监视器若仍持旧基线，
            # 会把"回到默认"当成"用户改了档位"，下个周期按映射把非默认
            # 值写回（面板说恢复了，实际没有）。本段与上面的内存重写之间
            # 无 await，对旋钮监视任务是原子的：它要么整体前跑（旧值 vs
            # 旧基线，无差异），要么整体后跑（默认值 vs 默认基线，无差异）。
            try:
                knobs = getattr(self, "_knobs", None)
                if knobs is not None:
                    knobs.reset_baseline()
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] 旋钮基线归位失败（不影响）: {e}")
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

    async def _api_browser_status_get(self):
        """浏览器能力状态（M15-补丁1 C3，可选加分项）：Chromium 二进制
        可用性只读探测。探测可能起一次 Playwright driver 子进程（数百毫秒），
        放线程池跑避免卡事件循环；结果只读不缓存——面板每次打开都是实况。
        任何异常按"未安装"反馈（安装指引在面板说明块与 README）。"""
        from .core.browser_tools import chromium_installed

        try:
            installed = await asyncio.to_thread(chromium_installed)
            return {"status": "ok", "data": {"installed": bool(installed)}}
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 浏览器状态探测失败")
            return {"status": "ok", "data": {"installed": False}}

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
        # M9-补丁3 B1：身份可用（路径 0/1 任一命中）时顺路回填存量——
        # 覆盖"重启期间没有新消息触发 _remember_self_identity"的场景；
        # 幂等由扫描条件保证（已有身份即跳过），重复运行零副作用
        try:
            from .core.selfheal import run_ghost_identity_backfill

            await run_ghost_identity_backfill(
                getattr(backend, "engine", None), identity
            )
        except Exception as e:
            logger.warning(f"[SelfHeal] 存量身份回填异常（不影响服务）: {e}")

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

    # M18-补丁1 A3：白名单与 schema 的 advanced 组一一对应——原名单里
    # persona/misc 从未在 schema 存在过（删），autonomy 漏了（导致
    # tier/write_level 改不了，补）
    _CONFIG_ALLOWED_GROUPS = (
        "autonomy", "capabilities", "decision", "initiative",
        "memory", "model", "output_gate", "sleep", "style_learning",
    )

    # A3：取值有约束的键 → 合法值提示（写入前校验，与 read_tier 的
    # clamp 范围一致）
    _CONFIG_VALUE_CONSTRAINTS = {
        ("autonomy", "tier"): "0=仅自带工具 1=+浏览器只读 2=+工作区写 3=全权",
        ("autonomy", "write_level"):
            "0=只读 1=浏览交互 2=轻写入（评论/点赞） 3=全权",
    }

    def _config_schema_item(self, group: str, key: str) -> dict | None:
        """schema 里该键的定义（与面板同源的校验依据）；读不到返回 None。"""
        try:
            items = self._panel_schema().get("advanced", {}).get("items", {})
            item = items.get(group, {}).get("items", {}).get(key)
            return item if isinstance(item, dict) else None
        except Exception:
            return None

    def _config_group_keys(self, group: str) -> tuple[str, ...]:
        """组内可写的键名（schema 定义优先；schema 不可用退回运行时组）。"""
        try:
            items = self._panel_schema().get("advanced", {}).get("items", {})
            group_items = items.get(group, {}).get("items", {})
            if isinstance(group_items, dict) and group_items:
                return tuple(group_items)
        except Exception:
            pass
        try:
            return tuple(conf_group(self.config, group))
        except Exception:
            return ()

    @staticmethod
    def _convert_command_value(item: dict, raw: str) -> tuple[Any, str | None]:
        """按 schema 类型转换命令输入（M18-补丁1 A1：与面板同源校验）。

        返回 (值, None) 或 (None, 错误文本)。string/text 原样返回。"""
        t = item.get("type")
        if t == "bool":
            lower = str(raw).strip().lower()
            if lower in ("true", "1", "on"):
                return True, None
            if lower in ("false", "0", "off"):
                return False, None
            return None, "需要 true/false"
        if t == "int":
            try:
                return int(str(raw).strip()), None
            except ValueError:
                return None, "需要整数"
        if t == "float":
            try:
                return float(str(raw).strip()), None
            except ValueError:
                return None, "需要数字"
        if t in ("list", "object"):
            try:
                return json.loads(raw), None
            except ValueError:
                return None, (
                    f"需要合法 JSON（{'数组' if t == 'list' else '对象'}），"
                    "建议在面板编辑"
                )
        return str(raw), None

    async def _living_config_lines(self, key: str, value: str) -> list[str]:
        """M18-补丁1 A 组：命令写入与运行时读取同源。

        原实现写顶层 group——分层后运行时读 advanced 嵌套（conf_group
        嵌套优先），顶层写入被完全忽略，回复却称"热生效"（假生效）。现
        经 apply_panel_save 写 advanced.<group>.<key>（与面板保存完全同
        源，同一套 schema 校验），落盘后用 _effective_config() 回读验证，
        验证通过才允许说"已生效"（A2/A4）。"""
        if not key or not value:
            return [
                "用法：/living config <group>.<key> <value>",
                f"允许的组：{'/'.join(self._CONFIG_ALLOWED_GROUPS)}",
                "例：/living config decision.daily_impulse_limit 5",
                "例：/living config autonomy.tier 2（0=仅自带 1=+浏览 2=+写 3=全权）",
            ]
        if "." in key:
            group, _, k = key.partition(".")
        else:
            # 裸键名：在允许组的 schema 定义里找唯一匹配
            hits = [
                (g, kk) for g in self._CONFIG_ALLOWED_GROUPS
                for kk in self._config_group_keys(g)
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
        item = self._config_schema_item(group, k)
        if item is None:
            return [f"未知配置键 {group}.{k!r}（以面板可编辑项为准）。"]

        converted, conv_error = self._convert_command_value(item, value)
        if conv_error:
            return [f"{group}.{k} {conv_error}，收到 {value!r}。"]
        constraint = self._CONFIG_VALUE_CONSTRAINTS.get((group, k))
        if constraint is not None and (
            not isinstance(converted, int) or isinstance(converted, bool)
            or not 0 <= converted <= 3
        ):
            return [f"{group}.{k} 取值 0-3（{constraint}）。"]

        # 回读展示的旧值取运行时真实值（磁盘直读），不是内存旧值
        try:
            old_value = conf_group(self._effective_config(), group).get(k)
        except Exception:
            old_value = None

        from .core.panel_api import PanelApiError, apply_panel_save

        try:
            # A1：与面板保存完全同源（同一套类型校验 + 写 advanced.<group>）
            apply_panel_save(
                self.config, self._panel_schema(),
                {"advanced": {group: {k: converted}}},
            )
        except PanelApiError as e:
            return [f"设置被拒绝：{e}"]
        except Exception as e:
            return [f"写入失败：{e}"]

        # A5：旧版本曾把值写到顶层 group（读取侧永远读不到的孤儿键）——
        # 写入新位置的同时清掉同名残留（幂等；group 是白名单组名，
        # 不可能是 preset/advanced，无误伤面）
        try:
            if isinstance(self.config, dict):
                self.config.pop(group, None)
        except Exception as e:
            logger.debug(f"[/living config] 顶层残留清理失败（不影响）: {e}")

        save_error = ""
        try:
            saver = getattr(self.config, "save_config_async", None)
            if callable(saver):
                await saver()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            save_error = str(e)
            logger.warning(f"[/living config] 配置落盘失败: {e}")

        # A2：运行时同源回读——验证通过才允许说"已生效"
        verified, read_back = False, None
        try:
            read_back = conf_group(self._effective_config(), group).get(k)
            verified = read_back == converted
        except Exception:
            verified = False
        if not verified:
            detail = f"（落盘异常：{save_error}）" if save_error else ""
            return [
                f"设置未生效：{group}.{k} 写入后回读不符"
                f"（期望 {converted!r}，读到 {read_back!r}）{detail}。"
                "请检查配置文件后重试。"
            ]
        return [f"已设置 {group}.{k}：{old_value!r} → {converted!r}（已生效）"]

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
            "/living config <group>.<key> <value> — 改配置（写入并回读验证后生效）",
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
    async def _extract_schedule_safe(self, event: AstrMessageEvent) -> None:
        """约定提取的后台包装：任何异常只 debug，绝不影响消息主链路。

        M9-补丁1 交付后线上实测发现原签名 (self, schedule, text) 不可用：
        AstrBot 的 handler 参数注入只认 event 等内置名，schedule/text 不会被
        注入，导致每条用户消息触发一次 TypeError、约定提取自上线起从未
        工作（2026-09-23 凛核验定位）。现改为从 event 取文本、schedule 走
        实例属性。"""
        try:
            text = str(getattr(event, "message_str", "") or "")
            await self._schedule.maybe_extract(text, datetime.now())
        except Exception as e:
            logger.debug(f"[Schedule] 约定提取失败（忽略）: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """所有消息的旁路监听：身份采集 / 待机刷新 / 吵醒计数 / 静默拦截
        （B3/B4 + 补丁 II + 补丁 XVI）。

        M9-补丁3 回归修复：M5-补丁4 在本方法上方插入 _extract_schedule_safe
        时把这行装饰器"抢走"了——本方法自此失去注册，身份采集、待机刷新、
        吵醒计数、静默拦截在线上整体失效（身份链断裂的直接根因）。
        约定提取不在这里做：它由 _extract_schedule_safe 自己的装饰器路径
        覆盖（本方法体内的 create_task 调用块随本补丁移除——其传参与凛
        1ecef9c 修好的签名失配，恢复注册后每条消息都会 TypeError）。

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
        # M14-补丁1 I2：念头系统的回应记账（F2）——主人消息到达即清零
        # 未回应收敛计数。只认念头目标会话（主人的私聊会话）来的消息，
        # 群聊里别人说话不算"回应她"。任何失败只 DEBUG，绝不影响消息链路
        initiative = (
            getattr(self.loop, "initiative", None) if self.loop is not None else None
        )
        if initiative is not None:
            try:
                target = self._initiative_session()
                try:
                    msg_session = event.unified_msg_origin
                except Exception:
                    msg_session = None
                if target and msg_session and msg_session == target:
                    await initiative.note_owner_message(datetime.now())
            except Exception as e:
                logger.debug(f"[Initiative] 回应记账失败（忽略）: {e}")
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
            threshold_fn = getattr(self.sleep_manager, "current_wake_threshold", None)
            if callable(threshold_fn):
                # M17-补丁1 C1：显示入睡时抽定的阈值（未抽定回落固定值）
                threshold = threshold_fn()
            else:
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
        # M17-补丁1 C2：睡眠期收到的消息留档（醒来后由 LLM 一次判断
        # 回不回）。触发吵醒的那条在上面已 return（她马上正常回应，
        # 不算"错过"）；记录条件（开关/在睡/命令豁免/有界）在
        # record_pending_message 内自查，失败只 DEBUG 不影响拦截链路
        try:
            recorder = getattr(self.sleep_manager, "record_pending_message", None)
            if callable(recorder):
                await recorder(session, message_str, now)
        except Exception as e:
            logger.debug(f"[Living] 未回消息留档失败（忽略）: {e}")
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
            "_selfheal_task", "_interest_cooldown_task", "_knobs_task",
            "_backfill_task",
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
