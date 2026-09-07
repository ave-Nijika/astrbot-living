"""LivingGate——状态闸门：决定"此刻该不该动"。

设计要点（任务书 M1-B）：
  - 判定链按序短路：休眠窗口 → 今日活动上限 → 冷却 → 概率；
  - 状态（今日计数/上次时间）持久化 SQLite，跨日自动清零；
  - 配置每次判定时热读（改配置即生效，不重启）；
  - 时间窗口支持跨午夜（如 00:30-08:00）。
为什么把"该不该动"独立成闸门：总纲 §3 要求闸门先于内容存在——
冲动再多，也必须先过闸门，这是"像人有节制"的底线。
"""

from __future__ import annotations

import random
from datetime import datetime, time as dt_time
from typing import Any, Callable

from astrbot.api import logger

# living_state 表的键名（SQLite 键值存储）
KEY_DATE = "date"
KEY_ACTIVITY_COUNT = "today_activity_count"
KEY_LAST_ACTIVITY_AT = "last_activity_at"
KEY_MESSAGE_COUNT = "today_message_count"
KEY_LAST_MESSAGE_AT = "last_message_at"


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_time_window(text: Any) -> tuple[dt_time, dt_time] | None:
    """解析 'HH:MM-HH:MM'。跨午夜（如 23:00-07:00）合法；无效配置返回 None。

    为什么宽容解析：配置写错不该让插件崩掉，安静依靠的是"判不出窗口就当
    没有窗口"+ 默认值兜底，错误配置只记 WARNING。
    """
    if not text or not isinstance(text, str) or "-" not in text:
        return None
    try:
        start_s, end_s = text.split("-", 1)
        start = dt_time.fromisoformat(start_s.strip())
        end = dt_time.fromisoformat(end_s.strip())
        return start, end
    except ValueError:
        logger.warning(f"[LivingGate] 时间窗口配置无法解析: {text!r}，按无窗口处理")
        return None


def in_time_window(now: datetime, window: tuple[dt_time, dt_time]) -> bool:
    """判断 now 是否落在 [start, end) 内；start > end 视为跨午夜窗口。"""
    start, end = window
    t = now.time()
    if start <= end:
        return start <= t < end
    # 跨午夜：如 23:00-07:00 => t>=23:00 或 t<07:00
    return t >= start or t < end


def _conf_group(config: Any, group: str) -> dict:
    """安全取配置分组。配置热读失败时返回空 dict（后续用默认值兜底）。"""
    try:
        value = config.get(group, {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


class LivingGate:
    """状态闸门。所有判定方法接受注入的 now（可测试），默认取本地时间。"""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        db_path: str,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._db_path = db_path
        # 概率掷点可注入：测试需要确定性的"骰子"
        self._rng = rng or random.random
        self._db: Any = None

    # ------------------------------------------------------------------
    # 状态存取（aiosqlite 键值表，惰性连接）
    # ------------------------------------------------------------------
    async def _get_db(self) -> Any:
        if self._db is None:
            import aiosqlite

            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS living_state ("
                "key TEXT PRIMARY KEY, value TEXT)"
            )
            await self._db.commit()
        return self._db

    async def _get_raw(self, key: str) -> str | None:
        db = await self._get_db()
        async with db.execute(
            "SELECT value FROM living_state WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def _set_raw(self, key: str, value: str) -> None:
        db = await self._get_db()
        await db.execute(
            "INSERT INTO living_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()

    async def get_state(self, now: datetime | None = None) -> dict:
        """读完整状态；跨日时清零今日计数（保留绝对时间戳——冷却跨日仍有效）。"""
        now = now or datetime.now()
        today = now.date().isoformat()
        stored_date = await self._get_raw(KEY_DATE)
        if stored_date != today:
            # 为什么只清计数不清时间戳：daily_limit 管的是"今天干了多少"，
            # 冷却管的是"距上次干了多久"，两者生命周期不同。
            await self._set_raw(KEY_DATE, today)
            await self._set_raw(KEY_ACTIVITY_COUNT, "0")
            await self._set_raw(KEY_MESSAGE_COUNT, "0")

        raw_values = {
            KEY_ACTIVITY_COUNT: await self._get_raw(KEY_ACTIVITY_COUNT),
            KEY_LAST_ACTIVITY_AT: await self._get_raw(KEY_LAST_ACTIVITY_AT),
            KEY_MESSAGE_COUNT: await self._get_raw(KEY_MESSAGE_COUNT),
            KEY_LAST_MESSAGE_AT: await self._get_raw(KEY_LAST_MESSAGE_AT),
        }

        def _ts(key: str) -> datetime | None:
            raw = raw_values.get(key)
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None

        return {
            "date": today,
            "activity_count": _to_int(raw_values[KEY_ACTIVITY_COUNT], 0),
            "last_activity_at": _ts(KEY_LAST_ACTIVITY_AT),
            "message_count": _to_int(raw_values[KEY_MESSAGE_COUNT], 0),
            "last_message_at": _ts(KEY_LAST_MESSAGE_AT),
        }

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    async def should_wake(self, now: datetime | None = None) -> tuple[bool, str]:
        """此刻是否允许进入一次活动周期。判定链按序短路（任务书 M1-B）。"""
        now = now or datetime.now()
        config = self._config_getter() or {}
        state = await self.get_state(now)

        allow, reason = self._evaluate_wake(now, config, state)
        last = state["last_activity_at"]
        hours_since = (
            f"{(now - last).total_seconds() / 3600:.1f}h" if last else "无记录"
        )
        limit = max(
            _to_int(_conf_group(config, "decision").get("daily_impulse_limit"), 3), 0
        )
        logger.debug(
            f"[LivingGate] 判定 reason={reason} allow={allow}"
            f"（今日活动 {state['activity_count']}"
            f"{'/%d' % limit if limit > 0 else '/∞'}，距上次活动 {hours_since}）"
        )
        return allow, reason

    def _evaluate_wake(
        self, now: datetime, config: Any, state: dict
    ) -> tuple[bool, str]:
        decision = _conf_group(config, "decision")
        capabilities = _conf_group(config, "capabilities")

        # 1. 休眠窗口（M1 只做时间窗判定，疲惫度/睡眠债在 M3）
        window = parse_time_window(_conf_group(config, "sleep").get("sleep_window"))
        if window and in_time_window(now, window):
            return False, "sleeping"

        # 2. 今日活动上限（0 = 不限制）
        limit = _to_int(decision.get("daily_impulse_limit"), 3)
        if limit > 0 and state["activity_count"] >= limit:
            return False, "daily_limit"

        # 3. 冷却：刚忙完需要歇一会儿
        cooldown_hours = _to_float(
            capabilities.get("cooldown_between_activities_hours"), 2.0
        )
        last_activity = state["last_activity_at"]
        if last_activity is not None and cooldown_hours > 0:
            elapsed = (now - last_activity).total_seconds()
            if elapsed < cooldown_hours * 3600:
                return False, "cooldown"

        # 4. 概率掷点：让"动不动"带点随机，不像闹钟
        probability = _to_float(decision.get("activity_probability"), 0.8)
        if self._rng() < probability:
            return True, "ok"
        return False, "rolled_off"

    async def should_send_message(self, now: datetime | None = None) -> tuple[bool, str]:
        """此刻是否允许主动发消息（输出闸门，任务书 M1-E）。"""
        now = now or datetime.now()
        config = self._config_getter() or {}
        state = await self.get_state(now)
        output = _conf_group(config, "output_gate")

        # 1. 今日消息上限（0 = 不限制）
        limit = _to_int(output.get("daily_message_limit"), 10)
        if limit > 0 and state["message_count"] >= limit:
            return False, "msg_daily_limit"

        # 2. 两条消息最小间隔
        min_interval_min = _to_float(output.get("message_min_interval_minutes"), 30)
        last_message = state["last_message_at"]
        if last_message is not None and min_interval_min > 0:
            elapsed = (now - last_message).total_seconds()
            if elapsed < min_interval_min * 60:
                return False, "msg_interval"

        # 3. 静默时段（与休眠窗口独立）
        quiet = parse_time_window(output.get("quiet_hours"))
        if quiet and in_time_window(now, quiet):
            return False, "quiet_hours"

        return True, "ok"

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------
    async def note_activity_started(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        state = await self.get_state(now)
        # 计数在"开始"时就 +1：即使活动中途崩了，这次冲动也已经花掉了
        await self._set_raw(KEY_ACTIVITY_COUNT, str(state["activity_count"] + 1))
        await self._set_raw(KEY_LAST_ACTIVITY_AT, now.isoformat())

    async def note_activity_finished(self, now: datetime | None = None) -> None:
        # 结束时刷新时间戳：冷却从"干完活"起算，而不是"起念"起算
        now = now or datetime.now()
        await self._set_raw(KEY_LAST_ACTIVITY_AT, now.isoformat())

    async def note_message_sent(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        state = await self.get_state(now)
        await self._set_raw(KEY_MESSAGE_COUNT, str(state["message_count"] + 1))
        await self._set_raw(KEY_LAST_MESSAGE_AT, now.isoformat())

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
