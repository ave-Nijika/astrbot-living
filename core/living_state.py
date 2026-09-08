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
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Callable

from astrbot.api import logger

# living_state 表的键名（SQLite 键值存储）
KEY_DATE = "date"
KEY_ACTIVITY_COUNT = "today_activity_count"
KEY_LAST_ACTIVITY_AT = "last_activity_at"
KEY_MESSAGE_COUNT = "today_message_count"
KEY_LAST_MESSAGE_AT = "last_message_at"


def _parse_iso(raw: str | None) -> datetime | None:
    """ISO 时间戳解析；空串/坏值返回 None（awake_until 的宽容读取）。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


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
        # 清醒待机（任务书 M3 补丁 II）：awake_until 的内存镜像为运行期权威，
        # SQLite 为持久化镜像（跨重启恢复）。为什么需要内存镜像：
        # should_mute_message / awake_standby_active 是同步方法，
        # 不能每次都 await 数据库
        self._awake_until: datetime | None = None

    # ------------------------------------------------------------------
    # 清醒待机（任务书 M3 补丁 II 一）
    # ------------------------------------------------------------------
    async def load_state(self) -> None:
        """启动时恢复待机状态（跨重启：AstrBot 重启时若仍在待机期内则延续）。"""
        raw = await self._get_raw("awake_until")
        self._awake_until = _parse_iso(raw)

    def awake_standby_active(self, now: datetime | None = None) -> bool:
        """当前是否处于清醒待机期（同步；供消息路径的同步判定用）。"""
        now = now or datetime.now()
        return self._awake_until is not None and now < self._awake_until

    async def refresh_awake_until(
        self, minutes: float, now: datetime | None = None
    ) -> None:
        """设置/刷新待机截止时间（now + minutes）。"""
        now = now or datetime.now()
        self._awake_until = now + timedelta(minutes=max(minutes, 0.0))
        await self._set_raw("awake_until", self._awake_until.isoformat())

    async def clear_awake_until(self) -> None:
        """清除待机状态（自然回落时调用）。"""
        self._awake_until = None
        # 写空串而非删键：UPSERT 简单一致，读取端把空串解析为 None
        await self._set_raw("awake_until", "")

    async def consume_standby_expiry(self, now: datetime | None = None) -> bool:
        """待机"刚过期"检测：已设置且 now 已越过截止 → 清除并返回 True。

        供主循环心跳做"恢复睡眠/告别消息"的状态切换；从未设置或仍在
        待机期内返回 False。
        """
        now = now or datetime.now()
        if self._awake_until is None or now < self._awake_until:
            return False
        await self.clear_awake_until()
        return True

    def _awake_standby_skip_sleeping(self, now: datetime) -> bool:
        """待机期内跳过 sleeping 判定（判定链首位，任务书定稿语义）。"""
        return self.awake_standby_active(now)

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
    async def should_wake(
        self, now: datetime | None = None, force: bool = False
    ) -> tuple[bool, str]:
        """此刻是否允许进入一次活动周期。判定链按序短路（任务书 M1-B）。

        force=True（手动/吵醒唤醒，任务书 M3-A2）：
          - 豁免概率掷点；
          - 休眠窗内不再直接拒绝，而是返回 (True, "woken_from_sleep")——
            由调用方执行吵醒流程（起床气/睡眠债）后再进活动周期；
          - 每日上限与冷却**仍然生效**：手动唤醒不是无限豁免。

        清醒待机（任务书 M3 补丁 II）：判定链最前面先查 awake_until——
        待机期内跳过 sleeping 判定（心跳可正常触发活动），force 也不会
        再进 woken_from_sleep 分支（人已经醒了，不存在"吵醒"）。
        """
        now = now or datetime.now()
        config = self._config_getter() or {}
        state = await self.get_state(now)

        allow, reason = self._evaluate_wake(
            now, config, state, force=force,
            standby_active=self._awake_standby_skip_sleeping(now),
        )
        last = state["last_activity_at"]
        hours_since = (
            f"{(now - last).total_seconds() / 3600:.1f}h" if last else "无记录"
        )
        limit = max(
            _to_int(_conf_group(config, "decision").get("daily_impulse_limit"), 3), 0
        )
        logger.debug(
            f"[LivingGate] 判定 reason={reason} allow={allow} force={force}"
            f"（今日活动 {state['activity_count']}"
            f"{'/%d' % limit if limit > 0 else '/∞'}，距上次活动 {hours_since}）"
        )
        return allow, reason

    def _evaluate_wake(
        self, now: datetime, config: Any, state: dict, force: bool = False,
        standby_active: bool = False,
    ) -> tuple[bool, str]:
        decision = _conf_group(config, "decision")
        capabilities = _conf_group(config, "capabilities")

        # 0. 清醒待机（补丁 II）：跳过 sleeping 判定，其余链照常——
        #    待机期内心跳可正常触发活动，force 也不触发吵醒结算
        if standby_active:
            if force:
                return True, "ok"
            # 落到下面的上限/冷却/概率链：待机期是否"再干一件事"仍受约束
        else:
            # 1. 休眠窗口（M3：force 触发吵醒流程而非拒绝，M1 仅做时间窗判定）
            window = parse_time_window(
                _conf_group(config, "sleep").get("sleep_window")
            )
            if window and in_time_window(now, window):
                if force:
                    return True, "woken_from_sleep"
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

        # 4. 概率掷点：让"动不动"带点随机，不像闹钟。手动唤醒豁免——
        #    主人都来叫了，还掷骰子就太不识趣了
        if force:
            return True, "ok"
        probability = _to_float(decision.get("activity_probability"), 0.8)
        if self._rng() < probability:
            return True, "ok"
        return False, "rolled_off"

    def in_sleep_window(self, now: datetime | None = None) -> bool:
        """此刻是否在休眠窗内（供吵醒计数/静默拦截等调用方判断）。"""
        now = now or datetime.now()
        window = parse_time_window(
            _conf_group(self._config_getter() or {}, "sleep").get("sleep_window")
        )
        return bool(window and in_time_window(now, window))

    def sleep_window_span(self, now: datetime | None = None) -> tuple[float, float] | None:
        """休眠窗信息：(总时长分钟, 距自然醒点的剩余分钟)。

        窗内返回数值；不在窗内或未配置窗口返回 None。跨午夜窗口
        （如 23:00-07:00）的剩余时间按"先到窗尾"方向计算。
        """
        now = now or datetime.now()
        window = parse_time_window(
            _conf_group(self._config_getter() or {}, "sleep").get("sleep_window")
        )
        if not window or not in_time_window(now, window):
            return None
        start, end = window
        start_dt = datetime.combine(now.date(), start)
        end_dt = datetime.combine(now.date(), end)
        if end <= start:
            # 跨午夜：窗尾在"明天"（如 23:00-07:00，23:30 时窗尾是明早 07:00）
            if now.time() >= start:
                end_dt = datetime.combine(now.date(), end) + timedelta(days=1)
            else:  # 凌晨段：窗头在"昨天"
                start_dt = datetime.combine(now.date(), start) - timedelta(days=1)
        total_minutes = (end_dt - start_dt).total_seconds() / 60.0
        remaining_minutes = max((end_dt - now).total_seconds() / 60.0, 0.0)
        return total_minutes, remaining_minutes

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
