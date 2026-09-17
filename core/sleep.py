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
from datetime import datetime, timedelta
from typing import Any, Callable

from astrbot.api import logger

# 本插件的命令前缀：这些消息永远不拦（任务书 B4 例外）
# 本插件命令与紧急命令：休眠期拦截永远豁免（任务书 M3 补丁 IX 需求 2-5）
OWN_COMMAND_KEYWORDS = ("living_wake", "/stop")


# ---------------------------------------------------------------------------
# 自主作息（任务书 M3 补丁 X）：睡意动力学
# 设计说明：熬夜不是脚本，是涌现——玩得投入时 energy 消耗慢、jitter 不利，
# 睡意就上不来；欠债了 debt 项自然抬高，第二天就睡到中午。所有非规律作息
# 都来自同一套动力学，而不是规则表。
# ---------------------------------------------------------------------------

_CIRCADIAN_PEAK = 1.0
_CIRCADIAN_EDGE = 0.3
_CIRCADIAN_BASE = 0.1
_CIRCADIAN_EDGE_MINUTES = 60


def _parse_window_start_minutes(hint: str) -> int | None:
    """'23:00-07:00' → 起点分钟数（23*60）。解析失败返回 None。"""
    text = str(hint or "").strip().split("-")[0]
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def circadian_factor(now: datetime, hint: str) -> float:
    """昼夜节律因子：hint 窗口内 1.0，两侧边界 1 小时线性渐变到 0.3，其余 0.1。

    hint 形如 "23:00-07:00"（支持跨午夜）。它只提高睡意倾向（加权项），
    不强制入睡——入睡仍是 sleepiness 与 threshold 的比较。
    """
    start = _parse_window_start_minutes(hint)
    if start is None:
        return _CIRCADIAN_BASE
    minutes = now.hour * 60 + now.minute
    pos = (minutes - start) % 1440
    duration = 8 * 60  # hint 窗口按 8 小时理解（起点 + 8h = 尾部）
    if pos <= duration - _CIRCADIAN_EDGE_MINUTES:
        return _CIRCADIAN_PEAK
    if pos <= duration:
        edge = (duration - pos) / _CIRCADIAN_EDGE_MINUTES
        return _CIRCADIAN_EDGE + (_CIRCADIAN_PEAK - _CIRCADIAN_EDGE) * edge
    if pos <= 1440 - _CIRCADIAN_EDGE_MINUTES:
        return _CIRCADIAN_BASE
    edge_in = (pos - (1440 - _CIRCADIAN_EDGE_MINUTES)) / _CIRCADIAN_EDGE_MINUTES
    return _CIRCADIAN_BASE + (_CIRCADIAN_PEAK - _CIRCADIAN_BASE) * edge_in


def gate_minutes_since_wakeup(gate, now: datetime | None = None) -> float | None:
    """距上次醒来的分钟数（gate 持有 last_wakeup_at；从未醒来返回 None）。"""
    try:
        return gate.minutes_since_last_wakeup(now)
    except Exception:
        return None


class SleepManagerAutonomous:
    """自主作息动力学（任务书 M3 补丁 X）——挂到 SleepManager 上的混入。

    职责：睡意评估 → 入睡决策 → 睡眠时长 → 醒来/被吵醒结算 → 白天小睡。
    状态（在睡/到点）由 LivingGate 持有（in_sleep_window 已融合自主睡眠），
    静默拦截、唤醒计数、起床气、待机等既有机制零改动自动生效。
    """

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _cfg_group(self) -> dict:
        try:
            value = (self._config_getter() or {}).get("sleep", {})
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _f(self, value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _weights(self) -> dict:
        raw = self._cfg_group().get("weights") or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "energy": self._f(raw.get("energy"), 0.35),
            "debt": self._f(raw.get("debt"), 0.35),
            "circadian": self._f(raw.get("circadian"), 0.30),
        }

    # ------------------------------------------------------------------
    # 睡意模型
    # ------------------------------------------------------------------
    async def sleepiness(self, mood, now: datetime | None = None) -> tuple[float, dict]:
        """睡意值 ∈ [0,1] 与分解明细（日志可读性是验收重点）。

        sleepiness = w_energy*(1-energy) + w_debt*(debt/100)
                   + w_circa*circadian_factor(now) + jitter
        jitter = ±sleepiness_jitter 均匀随机——同样状态下不必然同时刻入睡。
        """
        now = now or self._now()
        cfg = self._cfg_group()
        weights = self._weights()
        jitter_amp = max(self._f(cfg.get("sleepiness_jitter"), 0.15), 0.0)

        e_term = weights["energy"] * (1.0 - mood.energy)
        d_term = weights["debt"] * (mood.sleep_debt / 100.0)
        c_raw = circadian_factor(now, cfg.get("circadian_hint", "23:00-07:00"))
        c_term = weights["circadian"] * c_raw
        jitter = (self._rng_float() * 2.0 - 1.0) * jitter_amp
        value = max(0.0, min(1.0, e_term + d_term + c_term + jitter))
        detail = {
            "energy": mood.energy,
            "e_term": round(e_term, 3),
            "debt": mood.sleep_debt,
            "d_term": round(d_term, 3),
            "circadian": round(c_raw, 3),
            "c_term": round(c_term, 3),
            "jitter": round(jitter, 3),
        }
        return value, detail

    async def should_fall_asleep(self, mood, now: datetime | None = None) -> tuple[bool, float, dict]:
        """入睡判定：睡意 >= threshold 且距上次醒来 >= min_awake_minutes。"""
        now = now or self._now()
        cfg = self._cfg_group()
        threshold = max(self._f(cfg.get("sleepiness_threshold"), 0.6), 0.0)
        min_awake = max(self._f(cfg.get("min_awake_minutes"), 240), 0.0)
        value, detail = await self.sleepiness(mood, now)
        since_wakeup = gate_minutes_since_wakeup(self._gate, now)
        if since_wakeup is not None and since_wakeup < min_awake:
            detail["min_awake_wait"] = round(min_awake - since_wakeup, 1)
            return False, value, detail
        return value >= threshold, value, detail

    def sleep_duration_hours(self, mood, now: datetime | None = None) -> float:
        """睡眠时长：债务越高睡得越久 + ±0.5h 抖动（任务书 3.3）。"""
        cfg = self._cfg_group()
        min_h = max(self._f(cfg.get("min_sleep_hours"), 5.0), 1.0)
        max_h = max(self._f(cfg.get("max_sleep_hours"), 11.0), min_h)
        debt_ratio = max(0.0, min(mood.sleep_debt / 100.0, 1.0))
        jitter_h = (self._rng_float() * 2.0 - 1.0) * 0.5
        return max(min_h + (max_h - min_h) * debt_ratio + jitter_h, min_h)

    def nap_duration_minutes(self) -> float:
        cfg = self._cfg_group()
        nap_enabled = bool(cfg.get("nap_enabled", True))
        if not nap_enabled:
            return 0.0
        lo = max(self._f(cfg.get("nap_min_minutes"), 20), 1.0)
        hi = max(self._f(cfg.get("nap_max_minutes"), 90), lo)
        # 不用 uniform：rng 可能是只暴露 random() 的注入对象
        return lo + (hi - lo) * self._rng_float()

    # ------------------------------------------------------------------
    # 入睡 / 醒来 / 小睡 / 被吵醒结算
    # ------------------------------------------------------------------
    async def begin_autonomous_sleep(self, mood, now: datetime | None = None) -> dict:
        """入睡：评估睡意 → 达标则进自主睡眠（写 gate 状态），返回决策信息。"""
        now = now or self._now()
        asleep, value, detail = await self.should_fall_asleep(mood, now)
        if not asleep:
            logger.info(
                f"[Sleep] 睡意评估 {value:.2f}（精力 {detail['energy']:.2f}→"
                f"x{self._weights()['energy']:.2f}={detail['e_term']:.3f} | "
                f"债务 {detail['debt']:.0f}→x{self._weights()['debt']:.2f}="
                f"{detail['d_term']:.3f} | 昼夜 {detail['circadian']:.1f}→"
                f"x{self._weights()['circadian']:.2f}={detail['c_term']:.3f} | "
                f"抖动 {detail['jitter']:+.3f}）→ 不睡（阈值 "
                f"{self._f(self._cfg_group().get('sleepiness_threshold'), 0.6):.2f}）"
            )
            return {"asleep": False, "value": value, "detail": detail}

        duration_h = self.sleep_duration_hours(mood, now)
        until = now + timedelta(hours=duration_h)
        await self._gate.enter_autonomous_sleep(until, "long", now)
        logger.info(
            f"[Sleep] 睡意评估 {value:.2f}（精力 {detail['energy']:.2f}→"
            f"x{self._weights()['energy']:.2f}={detail['e_term']:.3f} | "
            f"债务 {detail['debt']:.0f}→x{self._weights()['debt']:.2f}="
            f"{detail['d_term']:.3f} | 昼夜 {detail['circadian']:.1f}→"
            f"x{self._weights()['circadian']:.2f}={detail['c_term']:.3f} | "
            f"抖动 {detail['jitter']:+.3f}）→ 入睡，预计 {duration_h:.1f} 小时后自然醒"
        )
        return {"asleep": True, "value": value, "duration_h": duration_h,
                "until": until, "detail": detail}

    def should_nap(self, mood, now: datetime | None = None) -> tuple[bool, float]:
        """白天小睡判定：nap_enabled 且 energy < 0.25 且距上次醒来 >= 300 分钟。"""
        cfg = self._cfg_group()
        if not bool(cfg.get("nap_enabled", True)):
            return False, 0.0
        if mood.energy >= 0.25:
            return False, 0.0
        since_wakeup = gate_minutes_since_wakeup(self._gate, now)
        if since_wakeup is not None and since_wakeup < 300:
            return False, 0.0
        return True, self.nap_duration_minutes()

    async def apply_woken_from_autonomous(
        self, mood, actual_hours: float, planned_hours: float,
        now: datetime | None = None,
    ) -> dict:
        """自主长睡被吵醒的结算：起床气照常 + 睡眠债按实睡/预计比例保留
        （替代 fixed 窗口的 apply_woken_in_sleep——自主模式没有固定窗）。
        \"被叫醒了就不睡了\"：gate.exit_autonomous_sleep 由调用方负责。"""
        result = {"grouchy": False, "debt_added": 0.0}
        percent = max(self._f(self._cfg_group().get("grouchiness_percent"), 20), 0.0)
        grouchy = self._rng_float() < percent / 100.0
        if mood is not None:
            mood.apply_grouchiness(grouchy)
        result["grouchy"] = grouchy
        if planned_hours > 0:
            debt = 100.0 * max(0.0, min(1.0 - actual_hours / planned_hours, 1.0))
            if mood is not None:
                mood.add_sleep_debt(debt)
            result["debt_added"] = round(debt, 1)
        return result


class SleepManager(SleepManagerAutonomous):
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
        # rng 统一包装：无论注入 Random 实例、bound method 还是简单
        # callable（lambda: 0.5 等），sleepiness/duration/nap 统一通过
        # self._rng_float() 取 [0,1) 随机值
        if rng is None:
            rng_obj = random.Random()
            self._rng_float = rng_obj.random
        elif isinstance(rng, random.Random):
            self._rng_float = rng.random
        elif callable(rng):
            self._rng_float = rng
        else:
            obj = random.Random()
            self._rng_float = obj.random
        self._rng = self._rng_float  # 向后兼容旧名
        self._now = now_provider or datetime.now
        # 滑动窗内的消息时间戳
        self._stamps: deque[datetime] = deque()
        # 上次触发吵醒的时刻：触发后冷却一个窗口时长，避免同一波聊天
        # 反复把主循环踹醒
        self._last_wake_trigger: datetime | None = None
        # 会话追踪（任务书 M3 补丁 II 二/三）：确认消息发给"吵醒我们的
        # 最后一个会话"，告别消息发给"待机期里最后活跃的会话"
        self.last_wake_session: str | None = None
        self.last_active_session: str | None = None
        # 最近一次吵醒计数（休眠窗内），供 describe_mute 报进度
        self.last_window_count: int = 0

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
        self,
        now: datetime | None = None,
        sender_id: str | None = None,
        session: str | None = None,
    ) -> tuple[bool, int]:
        """记录一条消息，返回 (是否触发吵醒, 窗内计数)。

        只有休眠窗内、且计入吵醒（wake_source 过滤后）的消息才进滑动窗——
        否则陌生消息会把主人的"3 条达标"时机垫早，吵醒语义就乱了。
        触发吵醒时记录来源会话（唤醒确认消息的发往地）。
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
        self.last_window_count = count
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
        if session:
            self.last_wake_session = session
        self.last_window_count = threshold
        return True, threshold

    def reset_wake_state(self) -> None:
        """紧急唤醒后清空吵醒计数与冷却（从干净状态开始，
        任务书 M3 补丁 IX 需求 2-2）。"""
        self._stamps.clear()
        self._last_wake_trigger = None
        self.last_window_count = 0

    # ------------------------------------------------------------------
    # 清醒待机（任务书 M3 补丁 II 一）
    # ------------------------------------------------------------------
    def standby_minutes(self) -> float:
        return max(
            self._f(self._group("sleep").get("awake_standby_minutes"), 30), 0.0
        )

    async def refresh_standby(
        self, now: datetime | None = None, session: str | None = None
    ) -> bool:
        """若处于待机期，按滑动窗口语义刷新待机时长。

        Returns:
            True = 当前在待机期（调用方应跳过吵醒计数与静默拦截——
            待机期的消息是"醒着聊天"，不是"吵"）；
            False = 不在待机期，调用方走原有吵醒计数逻辑。
        """
        now = now or self._now()
        if not self._gate.awake_standby_active(now):
            return False
        if session:
            self.last_active_session = session
        await self._gate.refresh_awake_until(self.standby_minutes(), now)
        logger.debug(
            f"[Sleep] 待机期消息，待机刷新 {self.standby_minutes():.0f} 分钟"
        )
        return True

    async def begin_standby(self, now: datetime | None = None) -> float:
        """被吵醒后进入清醒待机。返回待机分钟数（供日志）。"""
        minutes = self.standby_minutes()
        await self._gate.refresh_awake_until(minutes, now)
        return minutes

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
        grouchy = self._rng_float() < percent / 100.0
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
                logger.warning(f"[Sleep] 睡眠结算保存失败（不影响本次唤醒）: {e}")
        logger.debug(
            f"[Sleep] 吵醒结算 起床气={result['grouchy']} "
            f"睡眠债+{result['debt_added']}（剩余睡眠 {result['remaining_minutes']} 分钟）"
        )
        return result

    # ------------------------------------------------------------------
    # 静默拦截判定（任务书 B4）
    # ------------------------------------------------------------------
    def describe_mute(self, now: datetime | None = None, count: int | None = None) -> str:
        """拦截上下文文案（任务书 M3 补丁 IX 需求 1）：让主人在日志里一眼
        看懂"为什么没回复"以及"怎么唤醒我"。

        形如：正在休眠（00:30-08:00），窗内第 1 条；再发 2 条可唤醒
        （10 分钟窗口内）；紧急联系可发 living_wake_now
        """
        now = now or self._now()
        window_raw = str(
            self._group("sleep").get("sleep_window", "") or "未配置"
        )
        window_minutes = max(
            self._f(self._group("sleep").get("wake_window_minutes"), 10), 1.0
        )
        threshold = max(self._i(self._group("sleep").get("wake_n_messages"), 3), 1)
        count = self.last_window_count if count is None else count
        remaining = max(threshold - count, 0)
        return (
            f"正在休眠（{window_raw}），窗内第 {count} 条；"
            f"再发 {remaining} 条可唤醒（{window_minutes:.0f} 分钟窗口内）；"
            f"紧急联系可发 living_wake_now"
        )

    def should_mute_message(self, now: datetime | None, message_str: str | None) -> bool:
        """这条消息是否应被拦截（不进入回复管线）。

        拦截条件全部满足才拦：配置开启 + 休眠窗内 + 不在清醒待机期
        + 不是本插件命令。待机期是"醒着聊天"，拦了就自相矛盾。
        达到吵醒阈值的那条消息由调用方（main 的消息 handler）先判 wake
        再判 mute——触发的消息不拦（被吵醒了就该回应）。
        """
        enabled = bool(self._group("sleep").get("sleep_mute_replies", True))
        if not enabled:
            return False
        if self._gate.awake_standby_active(now):
            return False
        if self._gate.force_awake_active(now):
            return False  # 紧急唤醒后的强醒期：不拦（本次休眠已结束）
        if not self._gate.in_sleep_window(now):
            return False
        text = str(message_str or "")
        if any(keyword in text for keyword in OWN_COMMAND_KEYWORDS):
            return False
        return True

