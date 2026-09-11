"""astrbot_plugin_living——让 AstrBot 在无消息时拥有自己的生活。

M3：休眠系统（疲惫/睡眠债/吵醒/静默回复/梦）+ 双事件热生效 + agent 循环
（token 硬闸保护）。设计详见 docs/项目总纲.md；前情报告见 docs/archive/。

R0 风险验证结论（2026-09-07 实测，详见 docs/archive/m0_report.md）：
  自主 agent 循环必须携带一个"幽灵 AstrMessageEvent"——event=None 会被
  AstrAgentContext 的 pydantic 校验拒绝，即使绕过校验，FunctionToolExecutor
  也会拒绝执行本地工具。见 core/ghost_event.py。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.activities import default_activities
from .core.agent_loop import LivingAgentLoop
from .core.decider import ActivityDecider
from .core.fetcher import WebFetcher
from .core.ghost_event import build_ghost_event
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
from .core.search import BochaSearcher
from .core.sender import Sender
from .core.sleep import SleepManager

PLUGIN_NAME = "astrbot_plugin_living"

# 闸门拦截原因 → 给主人看的一句话（/living_wake 反馈用）
_WAKE_REASON_TEXT = {
    "sleeping": "我在睡觉呢（休眠窗内），不忍心叫就别叫我啦",
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

        logger.info(f"[{PLUGIN_NAME}] M3 加载完成（心境+休眠+agent 循环）")

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
        self.sleep_manager = SleepManager(
            config_getter=lambda: self.config,
            gate=self.gate,
            mood=self.mood,
        )
        agent_loop = LivingAgentLoop(
            context=self.context,
            config_getter=lambda: self.config,
            tools=build_living_tools(
                searcher=self.searcher,
                fetcher=self.fetcher,
                sandbox=self.sandbox,
                memory_getter=self._get_memory,
            ),
            persona_getter=self._persona_prompt,
            life_extra_getter=lambda: str(self._cfg("persona", "life_extra", "") or ""),
            mood=self.mood,
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
            sleep_manager=self.sleep_manager,
            agent_loop=agent_loop,
            dream_llm_call=self._decision_llm_call,
            persona_id_getter=self._persona_id,
        )
        await self.loop.start()

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
            window = str((self.config.get("sleep", {}) or {}).get(
                "sleep_window", "") or "未配置")
            lines.append(
                f"休眠窗：{window}"
                f"（当前{'在内' if self.gate.in_sleep_window() else '在外'}）"
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
        if self.gate.in_sleep_window():
            lines.append("现在在休眠窗内，下次心跳会回到睡眠。")
        else:
            lines.append("当前不在休眠窗内，照常待机。")
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
        decision_mode = str(
            (self.config.get("decision", {}) or {}).get("decision_mode", "hybrid")
        )
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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """所有消息的旁路监听：待机刷新 / 吵醒计数 / 静默拦截（B3/B4 + 补丁 II）。

        顺序敏感：
          1. 待机期内（awake_until 未过期）→ 刷新待机时长（滑动窗）→
             不计数、不拦（醒着聊天，AstrBot 正常回复）；
          2. 否则走吵醒计数——达到阈值的那条消息不拦（被吵醒了就该回应），
             并请求主循环唤醒；
          3. 其余休眠窗内消息按 sleep_mute_replies 拦截。本插件命令不拦。
        """
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
            logger.debug(f"[Living] 待机刷新异常（按非待机继续）: {e}")

        try:
            # register_message 是同步方法（纯内存滑动窗），不要 await
            wake_triggered, _count = self.sleep_manager.register_message(
                now, sender_id, session=session
            )
        except Exception as e:
            logger.debug(f"[Living] 消息计数异常（跳过）: {e}")
            return
        if wake_triggered:
            logger.info("[Living] 睡眠中被连续消息吵醒，请求主循环唤醒")
            if self.loop is not None:
                self.loop.request_wake()
            return  # 触发吵醒的这条不拦
        try:
            message_str = event.message_str
        except Exception:
            message_str = ""
        if self.sleep_manager.should_mute_message(now, message_str):
            # 拦截 = 事件不再向后续插件 handler 与 LLM 回复管线传播
            #（scheduler 逐阶段检查 is_stopped）——主人定稿的"真正休息"
            event.stop_event()
            logger.debug("[Living] 睡眠期消息已拦截（sleep_mute_replies=true）")

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
