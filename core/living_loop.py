"""LivingLoop——主循环：心跳 → 闸门 → 活动周期 → 记忆 → （过闸门）分享。

安静纪律（任务书 M1 约束 6）：
  - 心跳间隔默认 45 分钟，且闸门不过就什么都不做；
  - 活动产出默认只写记忆；只有配置了 output_gate.target_sessions 才可能
    真正发消息，且每条都过消息闸门（上限/间隔/静默时段）；
  - 日志：关键生命周期用 INFO，判定与"本可发送"一律 DEBUG。

异常哲学：活动失败 ≠ 进程崩溃。活动周期整体 try/except，任何异常只记
日志，主循环必须活到下一轮心跳。
"""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from datetime import datetime
from typing import Any, Callable

from astrbot.api import logger

from .activities import Activity, ActivityContext, default_activities
from .ghost_event import build_ghost_event

DEFAULT_CHECK_INTERVAL_MIN = 45.0
DEFAULT_MAX_RUN_SECONDS = 300.0
# 记忆写入单独限时：LivingMemory 引擎可能走嵌入 API，不能让它拖死活动周期
MEMORY_WRITE_TIMEOUT = 30.0


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
        self._task: asyncio.Task | None = None
        self._last_activity_name: str | None = None

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
        self._task = asyncio.create_task(self._run(), name="living-loop")
        logger.info("[LivingLoop] 主循环已启动")

    async def stop(self) -> None:
        """停止心跳任务。重复 stop 幂等。"""
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("[LivingLoop] 主循环已停止")

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
            # 先睡再查：插件刚加载不要立刻"活蹦乱跳"，等第一个心跳
            await self._sleep(interval_min * 60)
            try:
                await self.heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 心跳自身的意外异常不中断主循环
                logger.exception("[LivingLoop] 心跳异常（主循环继续）")

    # ------------------------------------------------------------------
    # 心跳与活动周期
    # ------------------------------------------------------------------
    async def heartbeat_once(self, now: datetime | None = None) -> bool:
        """一次冲动检查。返回是否真的进入了活动周期。"""
        allow, reason = await self._gate.should_wake(now)
        if not allow:
            return False
        await self.run_activity_cycle(now=now)
        return True

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
        activity = self._pick_activity()
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
        )

        outcome = None
        error_note: str | None = None
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

        # 记忆双路径：无论成败都写（任务书 D）
        await self._write_memory(activity, outcome, error_note, ctx)
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

    async def _write_memory(
        self,
        activity: Activity,
        outcome: Any,
        error_note: str | None,
        ctx: ActivityContext,
    ) -> None:
        if outcome is not None and outcome.memory_content:
            content = outcome.memory_content
            importance = outcome.importance
        else:
            # 失败也是生活的一部分：记一句"今天没干成什么"
            detail = f"（{error_note}）" if error_note else ""
            content = f"{ctx.date_prefix()}我想{activity.name}来着，没成{detail}。"
            importance = 0.2
        try:
            memory = await self._get_memory()
            await asyncio.wait_for(
                memory.add(content, importance=importance),
                timeout=MEMORY_WRITE_TIMEOUT,
            )
        except Exception as e:
            # 记忆失败只记 WARN：活动本身已经完成，不能因为记账失败翻脸
            logger.warning(f"[LivingLoop] 记忆写入失败（活动仍算完成）: {e}")

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
