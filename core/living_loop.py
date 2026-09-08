"""LivingLoop——主循环：心跳 → 闸门 → 活动周期 → 记忆 → （过闸门）分享。

安静纪律（任务书 M1 约束 6）：
  - 心跳间隔默认 45 分钟，且闸门不过就什么都不做；
  - 活动产出默认只写记忆；只有配置了 output_gate.target_sessions 才可能
    真正发消息，且每条都过消息闸门（上限/间隔/静默时段）；
  - 日志：关键生命周期用 INFO，判定与"本可发送"一律 DEBUG。

异常哲学：活动失败 ≠ 进程崩溃。活动周期整体 try/except，任何异常只记
日志，主循环必须活到下一轮心跳。

M3 双事件（任务书 A1）：睡眠可被两个事件打断——
  - config_event：配置变更 → **只重置定时器，不触发判定**（频繁改配置
    = 定时器反复重置，零判定零活动零 LLM 调用）；
  - wake_event：手动/吵醒唤醒 → 触发一次 force 判定（仍过闸门其余约束）。
"""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import suppress
from datetime import datetime
from typing import Any, Callable

from astrbot.api import logger

from .activities import Activity, ActivityContext, ActivityOutcome, default_activities
from .ghost_event import build_ghost_event
from .llm_failover import looks_like_llm_error_output

DEFAULT_CHECK_INTERVAL_MIN = 45.0
DEFAULT_MAX_RUN_SECONDS = 300.0
# 记忆写入单独限时：LivingMemory 引擎可能走嵌入 API，不能让它拖死活动周期
MEMORY_WRITE_TIMEOUT = 30.0
CONFIG_POLL_SECONDS = 5.0
DEFAULT_AGENT_ACTIVITIES = ("surf", "read", "game")
DREAM_MAX_CHARS = 120


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _conf_group(config: Any, group: str) -> dict:
    try:
        value = config.get(group, {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


class LivingLoop:
    """自主生活主循环。start/stop 幂等，可安全应对插件热重载。"""

    def __init__(
        self,
        gate: Any,
        memory_getter: Callable[[], Any],
        config_getter: Callable[[], Any],
        abilities: dict | None = None,
        activities: list[Activity] | None = None,
        sender: Any = None,
        rng: random.Random | None = None,
        sleep_func: Callable[[float], Any] | None = None,
        mood: Any = None,
        decider: Any = None,
        sleep_manager: Any = None,
        agent_loop: Any = None,
        dream_llm_call: Callable[..., Any] | None = None,
        persona_id_getter: Callable[..., Any] | None = None,
    ) -> None:
        self._gate = gate
        self._get_memory = memory_getter
        self._config_getter = config_getter
        self._abilities = abilities or {}
        self._activities = activities if activities is not None else default_activities()
        self._sender = sender
        self._rng = rng or random.Random()
        # 可注入的 sleep：测试里换成即时返回，不用真等 45 分钟
        self._sleep = sleep_func or asyncio.sleep
        # M2：心境与决策层均可空——空则跳过相应逻辑（向后兼容）
        self._mood = mood
        self._decider = decider
        # M3：休眠结算器、agent 循环、梦生成 LLM，均可空
        self._sleep_manager = sleep_manager
        self._agent_loop = agent_loop
        self._dream_llm_call = dream_llm_call
        # M3 补丁：记忆写入时携带 persona id（问题 3，图谱参与者边的原料）
        self._persona_id_getter = persona_id_getter
        self._task: asyncio.Task | None = None
        self._watcher_task: asyncio.Task | None = None
        self._last_activity_name: str | None = None
        # 双事件（任务书 A1）：配置变更重置定时器；手动/吵醒唤醒触发判定
        self._config_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        # 睡眠状态跟踪（任务书 B2/B5）：入睡写回顾，醒来掷梦
        self._asleep: bool = False
        self._pending_dream: bool = False

    # ------------------------------------------------------------------
    # 对外事件入口（main.py 的命令/消息监听调用）
    # ------------------------------------------------------------------
    def notify_config_changed(self) -> None:
        """配置变更：只重置定时器（不触发判定）。"""
        self._config_event.set()

    def request_wake(self) -> None:
        """请求一次判定（force 语义仍受闸门其余约束）。"""
        self._wake_event.set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """启动心跳任务。重复 start 幂等（已在跑就直接返回）。"""
        if self.running:
            return
        # 初始哈希在 start 里同步采集：若留给 watcher 首帧采集，启动慢时
        # 第一次哈希可能已经落在配置修改之后，变更会被当成初始值漏检
        self._last_config_hash = self._config_hash()
        self._task = asyncio.create_task(self._run(), name="living-loop")
        self._watcher_task = asyncio.create_task(
            self._config_watcher(), name="living-config-watcher"
        )
        logger.info("[LivingLoop] 主循环已启动")

    async def stop(self) -> None:
        """停止心跳任务。重复 stop 幂等。"""
        for name in ("_task", "_watcher_task"):
            task = getattr(self, name, None)
            if task is None:
                continue
            setattr(self, name, None)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        logger.info("[LivingLoop] 主循环已停止")

    async def _config_watcher(self) -> None:
        """配置变更兜底检测（任务书 A4）：每 5 秒序列化比对一次。

        为什么用哈希快照而不是保存配置对象引用：AstrBotConfig 是原地修改
        对象，存引用永远等于自己；json 序列化哈希每次取的是当前状态。
        纯内存比对，零 LLM 零网络。若 AstrBot 未来提供配置变更钩子，
        main 可直接调 notify_config_changed()，本 watcher 留作兜底。
        """
        last = getattr(self, "_last_config_hash", None) or self._config_hash()
        while True:
            await asyncio.sleep(CONFIG_POLL_SECONDS)
            try:
                current = self._config_hash()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if current != last:
                last = current
                logger.debug("[LivingLoop] 检测到配置变更 → 重置定时器")
                self.notify_config_changed()

    def _config_hash(self) -> str:
        return json.dumps(
            self._config_getter() or {},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    async def _run(self) -> None:
        while True:
            interval_min = min(
                max(
                    _to_float(
                        _conf_group(self._config_getter(), "decision").get(
                            "impulse_check_interval_minutes"
                        ),
                        DEFAULT_CHECK_INTERVAL_MIN,
                    ),
                    1.0,
                ),
                1440.0,
            )
            # 双事件等待（任务书 A1）：任一事件或超时都会打断睡眠
            config_wait = asyncio.ensure_future(self._config_event.wait())
            wake_wait = asyncio.ensure_future(self._wake_event.wait())
            done, pending = await asyncio.wait(
                {config_wait, wake_wait},
                timeout=interval_min * 60,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if wake_wait in done:
                # 手动/吵醒唤醒：触发一次 force 判定（概率豁免、约束保留）
                self._wake_event.clear()
                logger.debug("[LivingLoop] 被唤醒（wake_event），执行 force 判定")
                try:
                    await self.heartbeat_once(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[LivingLoop] 强制心跳异常（主循环继续）")
                continue
            if config_wait in done:
                # 配置变更：只重置定时器，**不判定**（任务书 A1 定稿语义）
                self._config_event.clear()
                logger.debug("[LivingLoop] 配置变更，定时器已重置（本轮不判定）")
                continue
            # 超时 → 正常心跳
            try:
                await self.heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[LivingLoop] 心跳异常（主循环继续）")

    # ------------------------------------------------------------------
    # 心跳与活动周期
    # ------------------------------------------------------------------
    async def heartbeat_once(
        self, now: datetime | None = None, force: bool = False
    ) -> bool:
        """一次冲动检查。返回是否真的进入了活动周期。

        force=True（任务书 A2）：豁免概率掷点，仍受休眠窗/上限/冷却约束；
        休眠窗内的强制唤醒会触发吵醒结算（起床气/睡眠债）。
        """
        awake, _reason, _activity = await self.heartbeat_once_detailed(now, force)
        return awake

    async def heartbeat_once_detailed(
        self, now: datetime | None = None, force: bool = False
    ) -> tuple[bool, str, str | None]:
        """带详情的心跳：/living_wake 命令用反馈给主人（唤醒/拦截原因）。"""
        now = now or datetime.now()

        # 睡眠状态跟踪（任务书 B2/B5）：入睡写睡前回顾，醒来掷梦
        try:
            in_window = self._gate.in_sleep_window(now)
        except Exception:
            in_window = False
        if in_window and not self._asleep:
            self._asleep = True
            await self._write_bedtime_review(now)
        elif not in_window and self._asleep:
            self._asleep = False
            self._pending_dream = True  # 自然醒，醒来也许有梦

        allow, reason = await self._gate.should_wake(now, force=force)
        if not allow:
            return False, reason, None

        if reason == "woken_from_sleep":
            # 吵醒结算（任务书 B3）：起床气 + 睡眠债，然后带着情绪醒来
            if self._sleep_manager is not None:
                try:
                    await self._sleep_manager.apply_woken_in_sleep(now)
                except Exception as e:
                    logger.warning(f"[LivingLoop] 吵醒结算失败（不影响唤醒）: {e}")
            self._pending_dream = True

        result = await self.run_activity_cycle(now=now)
        activity_name = result.get("activity") if isinstance(result, dict) else None

        if self._pending_dream:
            self._pending_dream = False
            await self._maybe_dream(now)
        return True, reason, activity_name

    async def _write_bedtime_review(self, now: datetime) -> None:
        """睡前回顾（任务书 B2）：把今天的活动记忆聚成一句话存起来。

        用脚本聚合而非 LLM：回顾的价值在"记下来了"，不在辞藻——省下的
        token 留给梦。
        """
        try:
            memory = await self._get_memory()
        except Exception as e:
            logger.debug(f"[LivingLoop] 睡前回顾：记忆不可用，跳过（{e}）")
            return
        date_key = f"{now.month}月{now.day}日"
        try:
            rows = await memory.search(date_key, k=5)
        except Exception as e:
            logger.debug(f"[LivingLoop] 睡前回顾检索失败: {e}")
            rows = []
        contents = [str(r.get("content", "")).strip() for r in rows or []]
        contents = [c for c in contents if c][:3]
        if contents:
            review = f"{date_key}睡前想了想今天：{'；'.join(c[:40] for c in contents)}。该睡了，晚安。"
        else:
            review = (
                f"{date_key}是安静的一天，没做成什么事。该睡了，晚安。"
            )
        try:
            await memory.add(
                review,
                importance=0.6,
                session_id=self._session_id(None),
                persona_id=await self._persona_id(),
            )
            logger.info("[LivingLoop] 已写入睡前回顾")
        except Exception as e:
            logger.warning(f"[LivingLoop] 睡前回顾写入失败: {e}")

    async def _maybe_dream(self, now: datetime) -> None:
        """梦（任务书 B5）：醒来后的低概率彩蛋，任何失败都静默。"""
        if self._dream_llm_call is None:
            return
        try:
            from .living_state import _conf_group

            probability = _to_float(
                _conf_group(self._config_getter(), "sleep").get("dream_probability"),
                0.3,
            )
        except Exception:
            probability = 0.3
        if self._rng.random() >= max(probability, 0.0):
            return

        try:
            memory = await self._get_memory()
            rows = await memory.search("", k=3)
        except Exception as e:
            logger.debug(f"[LivingLoop] 梦的素材取不到，今晚不做梦: {e}")
            return
        fragments = [str(r.get("content", "")).strip()[:60] for r in rows or []]
        fragments = [f for f in fragments if f]
        if not fragments:
            return
        prompt = (
            "你刚从睡梦中醒来，还带着睡意。下面是你最近的记忆碎片：\n"
            + "\n".join(f"- {f}" for f in fragments)
            + "\n\n请说一句你刚才做的梦，80 字以内，第一人称，语气朦胧含糊，"
            "把碎片搅在一起也没关系，梦本来就是不讲道理的。只输出梦话本身。"
        )
        try:
            text = await self._dream_llm_call(prompt, None)
        except Exception as e:
            logger.debug(f"[LivingLoop] 梦生成失败（梦丢了就丢了）: {e}")
            return
        dream = str(text or "").strip()[:DREAM_MAX_CHARS]
        if not dream:
            return
        date_key = f"{now.month}月{now.day}日"
        try:
            await memory.add(
                f"{date_key}我做了个梦：{dream}",
                importance=0.2,
                session_id=self._session_id(None),
                persona_id=await self._persona_id(),
            )
        except Exception as e:
            logger.debug(f"[LivingLoop] 梦的记忆写入失败: {e}")
            return
        logger.info("[LivingLoop] 醒来做了个梦（已写入记忆）")
        await self._maybe_share(f"我好像做了个梦：{dream}", now)

    async def run_activity_cycle(self, now: datetime | None = None) -> dict:
        """一次完整活动周期：起念 → 活动 → 记忆（双路径）→ 收账 → 候选分享。"""
        now = now or datetime.now()
        activity_id = now.strftime("%Y%m%d_%H%M%S")
        # 幽灵事件（M0-R0 结论）：M1 活动直接调能力用不到它，但它是 M2 接入
        # tool_loop_agent 的唯一合法事件形态，构造好放进活动上下文。
        ghost_event = build_ghost_event(session_id=f"living_{activity_id}")

        # 记忆后端先就位：它挂了的话活动没法写记忆，这轮直接放弃（不耗配额）
        try:
            memory = await self._get_memory()
        except Exception as e:
            logger.error(f"[LivingLoop] 记忆后端不可用，本轮放弃: {e}")
            return {"activity": None, "ok": False, "error": "memory_unavailable"}

        await self._gate.note_activity_started(now)
        activity, params = await self._choose_activity(now)
        logger.info(f"[LivingLoop] 活动开始 name={activity.name} id={activity_id}")

        ctx = ActivityContext(
            searcher=self._abilities.get("searcher"),
            fetcher=self._abilities.get("fetcher"),
            sandbox=self._abilities.get("sandbox"),
            memory=memory,
            gate=self._gate,
            event=ghost_event,
            rng=self._rng,
            now=now,
            params=params,
            agent=self._agent_callable(activity.name),
        )

        outcome = None
        error_note: str | None = None
        real_start = datetime.now()  # 疲惫按真实耗时折算，不用注入的 now
        # 非正值/脏值回默认；上限 1h 防止配置手滑把一次活动拖成半天
        max_run = min(
            _to_float(
                _conf_group(self._config_getter(), "decision").get("max_run_seconds"),
                DEFAULT_MAX_RUN_SECONDS,
            ),
            3600.0,
        )
        if max_run <= 0:
            max_run = DEFAULT_MAX_RUN_SECONDS
        try:
            outcome = await asyncio.wait_for(
                activity.run(ctx), timeout=max_run
            )
        except asyncio.TimeoutError:
            error_note = f"活动 {activity.name} 超时（>{max_run:.0f}s），被强杀"
            logger.error(f"[LivingLoop] {error_note}")
        except asyncio.CancelledError:
            # 主循环停止：不吞（让 stop() 语义成立），但把账记平
            await self._gate.note_activity_finished()
            raise
        except Exception as e:
            error_note = f"活动 {activity.name} 失败: {e}"
            logger.error(f"[LivingLoop] {error_note}")

        # 任务书问题 2：LLM 错误文本不进记忆。VM 实测里 provider 全挂时
        # agent 的"产出"就是 "All chat models failed: ..." 这类错误串——
        # 当成果写进记忆会污染记忆库、拖垮后续决策 prompt 的质量。
        model_failure = False
        if outcome is not None and looks_like_llm_error_output(
            f"{outcome.summary or ''} {outcome.memory_content or ''}"
        ):
            logger.warning(
                f"[LivingLoop] 活动 {activity.name} 的产出是 LLM 错误信息，按失败处理"
            )
            model_failure = True
            error_note = "LLM 错误信息，已拦截不入记忆"
            outcome = None  # 失败路径：心境记失败、不分享、用专属失败文案

        # 心境演化与记忆重要度调节（M2-D；M3 起疲惫按活动耗时折算）
        duration_seconds = (datetime.now() - real_start).total_seconds()
        importance_adjust = await self._update_mood(
            activity, outcome, params, duration_seconds
        )
        # 记忆双路径：无论成败都写（任务书 D）
        failure_text = (
            f"{ctx.date_prefix()}我想做{activity.name}来着，"
            "但脑子转不动（模型全挂了）。"
            if model_failure
            else None
        )
        await self._write_memory(
            activity, outcome, error_note, ctx, importance_adjust,
            failure_text=failure_text,
        )
        await self._gate.note_activity_finished()
        logger.info(f"[LivingLoop] 活动结束 name={activity.name}")

        # 候选分享（内部过输出闸门）
        if outcome is not None and outcome.summary:
            await self._maybe_share(outcome.summary, now)
        return {
            "activity": activity.name,
            "ok": error_note is None,
            "error": error_note,
        }

    def _agent_callable(self, activity_name: str) -> Callable[..., Any] | None:
        """按配置 decision.agent_activities 决定该活动是否走 agent 模式
        （任务书 C1）。返回 None = 脚本模式。"""
        if self._agent_loop is None:
            return None
        try:
            enabled = (
                _conf_group(self._config_getter(), "decision").get("agent_activities")
                or list(DEFAULT_AGENT_ACTIVITIES)
            )
        except Exception:
            enabled = list(DEFAULT_AGENT_ACTIVITIES)
        if isinstance(enabled, str):
            enabled = [enabled]
        if activity_name not in list(enabled):
            return None
        return self._agent_loop.run

    async def _choose_activity(self, now: datetime) -> tuple[Activity, dict]:
        """M2：决策层优先（rules/hybrid/llm 三档），未接线时退回 M1 随机。"""
        if self._decider is not None:
            try:
                decision = await self._decider.decide(now)
                return decision.activity, dict(decision.params or {})
            except Exception as e:
                # 决策器自身抛异常：退回内部随机，生活照常
                logger.warning(f"[LivingLoop] 决策器异常，回退随机选择: {e}")
        return self._pick_activity(), {}

    async def _update_mood(
        self,
        activity: Activity,
        outcome: Any,
        params: dict | None,
        duration_seconds: float = 0.0,
    ) -> float:
        """活动结束后更新心境；返回记忆重要度调节量。

        低谷时的小确幸记得更牢：valence < 0 时成功活动的记忆重要度 +0.1
        （任务书 M2-D）。M3 起疲惫按活动实际耗时折算（fatigue_rate_per_hour
        配置热读）。心境更新失败不影响活动记账。
        """
        if self._mood is None:
            return 0.0
        ok = outcome is not None
        valence_before = self._mood.valence
        topic = (params or {}).get("topic")
        fatigue_rate = _to_float(
            _conf_group(self._config_getter(), "sleep").get("fatigue_rate_per_hour"),
            4.0,
        )
        try:
            await self._mood.record_activity(
                activity.name,
                ok,
                topic=topic if isinstance(topic, str) else None,
                duration_seconds=duration_seconds,
                fatigue_rate_per_hour=fatigue_rate,
            )
        except Exception as e:
            logger.warning(f"[LivingLoop] 心境更新失败（活动仍算完成）: {e}")
        if ok and valence_before < 0:
            return 0.1
        return 0.0

    async def _write_memory(
        self,
        activity: Activity,
        outcome: Any,
        error_note: str | None,
        ctx: ActivityContext,
        importance_adjust: float = 0.0,
        failure_text: str | None = None,
    ) -> None:
        if outcome is not None and outcome.memory_content:
            content = outcome.memory_content
            importance = outcome.importance
        else:
            # 失败也是生活的一部分；模型故障用专属文案（任务书问题 2 定稿）
            detail = f"（{error_note}）" if error_note else ""
            content = failure_text or (
                f"{ctx.date_prefix()}我想{activity.name}来着，没成{detail}。"
            )
            importance = 0.2
        # 心境调节后的重要度仍要钳在合理区间
        importance = max(0.0, min(1.0, importance + importance_adjust))
        try:
            memory = await self._get_memory()
            await asyncio.wait_for(
                memory.add(
                    content,
                    importance=importance,
                    # 任务书问题 3：带上会话与人格上下文——LivingMemory 的
                    # 图谱提取器靠它们生成参与者边，传 None 只会得到孤立节点。
                    # 幽灵事件的 uwo 是自主活动记忆在图谱里的"家"
                    session_id=self._session_id(ctx),
                    persona_id=await self._persona_id(),
                ),
                timeout=MEMORY_WRITE_TIMEOUT,
            )
        except Exception as e:
            # 记忆失败只记 WARN：活动本身已经完成，不能因为记账失败翻脸
            logger.warning(f"[LivingLoop] 记忆写入失败（活动仍算完成）: {e}")

    def _session_id(self, ctx: ActivityContext | None) -> str | None:
        """记忆归属会话：自主活动统一落在幽灵会话里（图谱上的自留地）。"""
        if ctx is not None and getattr(ctx, "event", None) is not None:
            try:
                return ctx.event.unified_msg_origin
            except Exception:
                pass
        try:
            return build_ghost_event().unified_msg_origin
        except Exception:
            return None

    async def _persona_id(self) -> str:
        """当前生效 persona 的 id；任何失败都回 "default"（任务书问题 3）。"""
        if self._persona_id_getter is None:
            return "default"
        try:
            pid = await self._persona_id_getter()
        except Exception:
            return "default"
        text = str(pid or "").strip()
        return text or "default"

    # ------------------------------------------------------------------
    # 分享（输出闸门链路，任务书 E）
    # ------------------------------------------------------------------
    async def _maybe_share(self, text: str, now: datetime) -> None:
        sessions = [
            s.strip()
            for s in str(
                _conf_group(self._config_getter(), "output_gate").get(
                    "target_sessions", ""
                )
            ).splitlines()
            if s.strip()
        ]
        if not sessions:
            # 默认安静：内容只在 DEBUG 里留底，不打扰任何人
            logger.debug(f"[LivingLoop] 本可发送的内容（未配置 target_sessions）：{text}")
            return
        allow, reason = await self._gate.should_send_message(now)
        if not allow:
            logger.debug(f"[LivingLoop] 想说话但被闸门拦下 reason={reason}：{text}")
            return
        if self._sender is None:
            logger.warning("[LivingLoop] 已配置 target_sessions 但 sender 未注入")
            return
        for session in sessions:
            try:
                sent = await self._sender.send(session, text)
            except Exception as e:
                logger.warning(f"[LivingLoop] 发送到 {session} 异常: {e}")
                continue
            if sent:
                # 只有真发出去才记账，失败的会话不消耗配额
                await self._gate.note_message_sent(now)

    def _pick_activity(self) -> Activity:
        """随机选活动，避免和上次相同（连着两回干一样的事就不像生活了）。"""
        pool = [
            a for a in self._activities if a.name != self._last_activity_name
        ] or self._activities
        chosen = self._rng.choice(pool)
        self._last_activity_name = chosen.name
        return chosen
