"""起床约定（M5-补丁4）：提取、存储、压力查询。

设计哲学（任务书零节）：约定不是定时器，是**心理压力**——它只以权重
形式进入睡意公式（schedule_pressure 加项）与长睡锚定（早睡锚、min 语义），
不存在任何确定性闹钟语义（无"到点强制醒"、无"时长截断"）。

成本硬约束（红线 3）：预筛（hit_trigger_words）是本地正则，未命中词表时
maybe_extract 直接返回——零 LLM 调用、零额外开销。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from typing import Any, Callable

from astrbot.api import logger

DEFAULT_TRIGGER_WORDS: list[str] = [
    "起床", "早起", "叫我", "叫醒", "唤醒", "早点叫", "早八",
    "wake", "alarm",
]

# LLM 确认 prompt（报告需附全文）
PROMPT_TEMPLATE = (
    "你负责判断一条消息是否表达了\"起床约定\"——说话人要求（自己或对方）"
    "在某个具体时刻起床、醒来或被叫醒。\n"
    "消息：{text}\n"
    "当前时间：{now}\n"
    "只输出 JSON：{{\"is_wake_commitment\": true或false, "
    "\"target_time\": \"YYYY-MM-DDTHH:MM\", \"confidence\": 0到1}}\n"
    "规则：仅在明确提到具体起床/醒来/叫醒时刻时 is_wake_commitment 为 true；"
    "target_time 推断为该时刻的绝对时间（无法确定日期时取最近的未来时刻）；"
    "confidence 表示你对判断的确信度；闲聊、抱怨或没有具体时刻一律 false。"
)


def hit_trigger_words(text: str, words: list[str] | None = None) -> bool:
    """预筛（A1，本地正则零成本）：消息命中任一触发词。"""
    text = str(text or "").lower()
    if not text:
        return False
    for word in (words if words else DEFAULT_TRIGGER_WORDS):
        if word and str(word).lower() in text:
            return True
    return False


def build_extract_prompt(text: str, now: datetime) -> str:
    return PROMPT_TEMPLATE.format(text=text, now=now.isoformat(timespec="minutes"))


def _extract_json(text: Any) -> dict | None:
    """从 LLM 回复抠第一个 JSON 对象（宽容解析，失败按非约定处理）。"""
    if not text:
        return None
    cleaned = str(text).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_target(value: Any, now: datetime) -> datetime | None:
    try:
        target = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if target <= now:
        return None  # 过去时刻无意义（LLM 幻觉或时区错误）→ 丢弃
    return target


def _expires_at(target: datetime, created: datetime) -> datetime:
    """默认过期 = 约定当日 23:59 或 created+24h 取先到（A3）。"""
    end_of_day = datetime.combine(target.date(), time(23, 59))
    return min(end_of_day, created + timedelta(hours=24))


class ScheduleManager:
    """约定的提取与查询。所有查询惰性清除过期项（A3）。"""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        gate: Any,
        llm_call: Callable[..., Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._gate = gate
        self._llm_call = llm_call  # async (prompt, system) -> str | None
        self._now = now_provider or datetime.now

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _sleep_cfg(self) -> dict:
        from .conf_path import conf_group

        try:
            return conf_group(self._config_getter() or {}, "sleep")
        except Exception:
            return {}

    def enabled(self) -> bool:
        return bool(self._sleep_cfg().get("schedule_reminder_enabled", True))

    def _strictness(self) -> float:
        try:
            return max(min(float(self._sleep_cfg().get(
                "schedule_extract_strictness", 0.7)), 1.0), 0.0)
        except (TypeError, ValueError):
            return 0.7

    def _trigger_words(self) -> list[str]:
        raw = self._sleep_cfg().get("schedule_trigger_words")
        return [str(w) for w in raw] if isinstance(raw, list) else list(
            DEFAULT_TRIGGER_WORDS
        )

    # ------------------------------------------------------------------
    # 提取（A1 预筛 → A2 LLM 确认 → A3/A4 存储）
    # ------------------------------------------------------------------
    async def maybe_extract(self, text: str, now: datetime | None = None) -> dict | None:
        """消息入口：命中词表才走 LLM；确认且过严格度门槛才记录。"""
        if not self.enabled():
            return None
        text = str(text or "").strip()
        if not text or not hit_trigger_words(text, self._trigger_words()):
            return None  # A1：未命中 → 整条链路零 LLM 调用
        if self._llm_call is None:
            return None
        now = now or self._now()
        try:
            raw = await self._llm_call(
                build_extract_prompt(text[:200], now), None
            )
        except Exception as e:
            logger.debug(f"[Schedule] 约定确认 LLM 调用失败（按非约定）: {e}")
            return None
        data = _extract_json(raw)
        if not data or not data.get("is_wake_commitment"):
            return None  # 判非约定 → 静默丢弃
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            return None
        if confidence < self._strictness():
            logger.debug(
                f"[Schedule] 约定置信度 {confidence} < 门槛，丢弃"
            )
            return None
        target = _parse_target(data.get("target_time"), now)
        if target is None:
            return None
        return await self.record(target, text, now)

    async def record(self, target: datetime, content: str, now: datetime) -> dict:
        """写入约定（A4 幂等：同 target_time 更新而非新增）。"""
        item = {
            "type": "wake",
            "target_time": target.isoformat(timespec="minutes"),
            "content": str(content)[:100],
            "created_at": now.isoformat(timespec="minutes"),
            "expires_at": _expires_at(target, now).isoformat(timespec="minutes"),
        }
        await self._gate.add_commitment(item)
        logger.info(f"[Schedule] 记录起床约定：{item['target_time']}（{item['content'][:40]}）")
        return item

    # ------------------------------------------------------------------
    # 查询（全部先过总开关，再惰性清过期）
    # ------------------------------------------------------------------
    async def active_wake_commitments(
        self, now: datetime | None = None, within_hours: float = 12.0
    ) -> list[dict]:
        if not self.enabled():
            return []
        now = now or self._now()
        items = await self._gate.get_commitments(now)
        horizon = now + timedelta(hours=within_hours)
        return [
            item
            for item in items
            if item.get("type") == "wake"
            and self._target_dt(item) is not None
            and now < self._target_dt(item) <= horizon
        ]

    async def earliest_future_wake(self, now: datetime | None = None) -> dict | None:
        items = await self.active_wake_commitments(now)
        if not items:
            return None
        return min(items, key=lambda item: self._target_dt(item))

    async def schedule_pressure(self, now: datetime | None = None) -> float:
        """约定压力 ∈ [0,1]（B1）：约定前 3h 起随临近线性上升。"""
        now = now or self._now()
        commitment = await self.earliest_future_wake(now)
        if commitment is None:
            return 0.0
        hours = (self._target_dt(commitment) - now).total_seconds() / 3600.0
        if hours <= 0:
            return 1.0  # 已到约定时刻还没醒——压力满格
        if hours >= 3.0:
            return 0.0
        return 1.0 - hours / 3.0

    async def overdue_unfulfilled(self, now: datetime | None = None) -> dict | None:
        """已过期未兑现的 wake 约定（C2 催醒起床气加倍用）。"""
        if not self.enabled():
            return None
        now = now or self._now()
        for item in await self._gate.get_commitments(now):
            target = self._target_dt(item)
            if item.get("type") == "wake" and target is not None and target <= now:
                return item
        return None

    async def consume_due_wake(self, now: datetime | None = None) -> dict | None:
        """取走 target 已到的约定并从存储清除（C1/C3：醒来结算消费）。"""
        if not self.enabled():
            return None
        now = now or self._now()
        for item in await self._gate.get_commitments(now):
            target = self._target_dt(item)
            if item.get("type") == "wake" and target is not None and target <= now:
                await self._gate.remove_commitment(item.get("target_time"))
                return item
        return None

    @staticmethod
    def _target_dt(item: dict) -> datetime | None:
        try:
            return datetime.fromisoformat(str(item.get("target_time")))
        except (TypeError, ValueError):
            return None
