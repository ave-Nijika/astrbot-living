"""SleepManager——休眠的消息侧：吵醒计数、起床气、睡眠债、静默拦截判定。

分工：LivingGate 管"此刻该不该动"（时间窗判定链），本模块管"消息怎么
影响睡眠"——两者只在"吵醒"时交汇（计数达阈值 → 唤醒主循环 → 本模块
做起床气/睡眠债结算）。

为什么滑动窗用内存队列而不是 SQLite：吵醒窗口只有几分钟，重启丢掉的
只是"刚才有几个人说话"，不值得为它建表。
"""

from __future__ import annotations

import random
from collections import deque
from datetime import datetime
from typing import Any, Callable

from astrbot.api import logger

# 本插件的命令前缀：这些消息永远不拦（任务书 B4 例外）
OWN_COMMAND_KEYWORDS = ("living_wake",)


class SleepManager:
    """休眠期的消息计数器与睡眠结算器。所有配置热读。"""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        gate: Any,
        mood: Any = None,
        rng: Callable[[], float] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._gate = gate  # 用它的 in_sleep_window / sleep_window_span
        self._mood = mood
        self._rng = rng or random.random
        self._now = now_provider or datetime.now
        # 滑动窗内的消息时间戳
        self._stamps: deque[datetime] = deque()
        # 上次触发吵醒的时刻：触发后冷却一个窗口时长，避免同一波聊天
        # 反复把主循环踹醒
        self._last_wake_trigger: datetime | None = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _group(self, name: str) -> dict:
        try:
            value = (self._config_getter() or {}).get(name, {})
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _f(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _i(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # 吵醒计数（任务书 B3）
    # ------------------------------------------------------------------
    def counts_toward_wake(self, sender_id: str | None) -> bool:
        """这条消息是否计入吵醒（wake_source 配置：all / owner_only）。"""
        source = str(self._group("sleep").get("wake_source", "all") or "all")
        if source != "owner_only":
            return True
        owner_id = str(self._group("sleep").get("owner_id", "") or "").strip()
        if not owner_id:
            # 配置了 owner_only 却没填 owner_id：退回 all 并记一次警告
            # （安静与否比"谁说的"更重要，配置残缺不该让吵醒失灵）
            logger.warning("[Sleep] wake_source=owner_only 但未配置 owner_id，按 all 计数")
            return True
        return str(sender_id or "").strip() == owner_id

    def register_message(
        self, now: datetime | None = None, sender_id: str | None = None
    ) -> tuple[bool, int]:
        """记录一条消息，返回 (是否触发吵醒, 窗内计数)。

        只有休眠窗内、且计入吵醒（wake_source 过滤后）的消息才进滑动窗——
        否则陌生消息会把主人的"3 条达标"时机垫早，吵醒语义就乱了。
        """
        now = now or self._now()
        window_minutes = max(
            self._f(self._group("sleep").get("wake_window_minutes"), 10), 1.0
        )
        threshold = max(self._i(self._group("sleep").get("wake_n_messages"), 3), 1)

        if not self._gate.in_sleep_window(now):
            return False, len(self._stamps)
        if not self.counts_toward_wake(sender_id):
            return False, len(self._stamps)

        self._stamps.append(now)
        while self._stamps and (now - self._stamps[0]).total_seconds() > window_minutes * 60:
            self._stamps.popleft()

        count = len(self._stamps)
        if count < threshold:
            return False, count

        # 冷却：刚踹醒过就不再连续触发（下一波聊天要隔一个窗口才算"又一批"）
        if (
            self._last_wake_trigger is not None
            and (now - self._last_wake_trigger).total_seconds() < window_minutes * 60
        ):
            return False, count

        self._last_wake_trigger = now
        self._stamps.clear()  # 这一波已经把人吵醒了，清窗重新计数
        return True, threshold

    # ------------------------------------------------------------------
    # 吵醒结算（任务书 B3：起床气 + 睡眠债）
    # ------------------------------------------------------------------
    async def apply_woken_in_sleep(self, now: datetime | None = None) -> dict:
        """主循环在休眠窗内被强制唤醒后的结算。

        - 起床气：按 grouchiness_percent 概率给 valence/energy 双降；
        - 睡眠债：按"距自然醒点的剩余时长占整个睡眠窗的比例"累积。
        """
        now = now or self._now()
        result = {"grouchy": False, "debt_added": 0.0, "remaining_minutes": 0.0}
        span = self._gate.sleep_window_span(now)
        if span is None:
            return result
        total_minutes, remaining_minutes = span
        result["remaining_minutes"] = round(remaining_minutes, 1)

        percent = max(self._f(self._group("sleep").get("grouchiness_percent"), 20), 0.0)
        grouchy = self._rng() < percent / 100.0
        if self._mood is not None:
            self._mood.apply_grouchiness(grouchy)
        result["grouchy"] = grouchy

        if total_minutes > 0:
            debt = 100.0 * (remaining_minutes / total_minutes)
            if self._mood is not None:
                self._mood.add_sleep_debt(debt)
            result["debt_added"] = round(debt, 1)

        if self._mood is not None:
            try:
                await self._mood.save()
            except Exception as e:
                logger.debug(f"[Sleep] 睡眠结算保存失败（不影响本次唤醒）: {e}")
        logger.debug(
            f"[Sleep] 吵醒结算 起床气={result['grouchy']} "
            f"睡眠债+{result['debt_added']}（剩余睡眠 {result['remaining_minutes']} 分钟）"
        )
        return result

    # ------------------------------------------------------------------
    # 静默拦截判定（任务书 B4）
    # ------------------------------------------------------------------
    def should_mute_message(self, now: datetime | None, message_str: str | None) -> bool:
        """这条消息是否应被拦截（不进入回复管线）。

        拦截条件全部满足才拦：配置开启 + 休眠窗内 + 不是本插件命令。
        达到吵醒阈值的那条消息由调用方（main 的消息 handler）先判 wake
        再判 mute——触发的消息不拦（被吵醒了就该回应）。
        """
        enabled = bool(self._group("sleep").get("sleep_mute_replies", True))
        if not enabled:
            return False
        if not self._gate.in_sleep_window(now):
            return False
        text = str(message_str or "")
        if any(keyword in text for keyword in OWN_COMMAND_KEYWORDS):
            return False
        return True
