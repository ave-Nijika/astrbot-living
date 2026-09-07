"""MoodState——心境状态机：它是谁、现在什么心情、对什么感兴趣。

M1 的活动选择是纯随机；M2 要让随机变成"有倾向的随机"。心境用三个
经典维度刻画（valence/arousal 取自情绪的二维模型，energy 单列）：
  - mood_valence: -1.0~1.0 心情积极/消极（默认 0.2，略偏乐观地来到世上）
  - mood_arousal:  0.0~1.0 情绪唤醒度（默认 0.5；M3 与休眠系统联动，
                    M2 只持久化与暴露，不发明额外动力学）
  - energy:        0.0~1.0 精力（默认 0.8）
  - interests:     {主题: 0.0~1.0}，随活动累积，每日衰减 ×0.9

持久化用独立 mood.db（与 living_state.db 同目录不同文件）：闸门状态
"该不该动"与心境"是什么状态"生命周期不同，分表分文件互不干扰。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from astrbot.api import logger

# 更新规则常量（任务书 M2-A）：数值集中一处，方便凛调参
DELTA_VALENCE_OK = 0.05
DELTA_VALENCE_FAIL = -0.08
DELTA_ENERGY_OK = -0.1
DELTA_ENERGY_FAIL = -0.05
DELTA_INTEREST_TOPIC = 0.15
DELTA_INTEREST_REMINISCE = 0.1
DAILY_INTEREST_DECAY = 0.9

INTEREST_TOPIC_ACTIVITIES = ("surf", "read", "game")
REMINISCE_INTEREST_KEY = "记忆"  # reminisce 活动累积的兴趣维度
NEUTRAL_INTEREST = 0.3  # interest_weight 对无记录主题的中性值

VALENCE_MIN, VALENCE_MAX = -1.0, 1.0
UNIT_MIN, UNIT_MAX = 0.0, 1.0
FATIGUE_MIN, FATIGUE_MAX = 0.0, 100.0
# 每日恢复量：一夜安睡大致抵掉大半疲惫，但睡眠债高的人醒来仍带倦意
DAILY_FATIGUE_RECOVERY = 60.0
DELTA_AROUSAL_OK = 0.05  # 活动成功的兴奋值（M2 挂点兑现）
AROUSAL_SLEEP_SETTLE = 0.6  # 睡一夜后 arousal 自然回落系数
# 睡眠债每晚消退量（覆盖默认值；配置项 sleep.sleep_debt_decay_per_day 由
# 调用方在结算时传入覆盖——load 里用常量兜底）
DAILY_SLEEP_DEBT_DECAY = 30.0

DELTA_GROUCHY_VALENCE = -0.15  # 被吵醒且触发起床气时的心境惩罚
DELTA_GROUCHY_ENERGY = -0.1


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_float(raw: Any, default: float) -> float:
    """脏数据兜底：库里的值解析不了就回默认（心境不能因为坏值崩掉）。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class MoodState:
    """心境状态。load 后在内存中读写，save 落 SQLite 键值表。"""

    def __init__(self, db_path: str, now_provider=None) -> None:
        self._db_path = db_path
        # 可注入时钟：测试里控制"今天"是哪天，验证每日衰减
        self._now = now_provider or datetime.now
        self._db: Any = None

        self.valence: float = 0.2
        self.arousal: float = 0.5
        self.energy: float = 0.8
        self.interests: dict[str, float] = {}
        # M3：疲惫与睡眠债（0~100）。fatigue 随活动累积，sleep_debt 由
        # 被吵醒产生——两者都让"它"第二天真的带着昨晚的痕迹醒来
        self.fatigue: float = 0.0
        self.sleep_debt: float = 0.0

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    async def _get_db(self) -> Any:
        if self._db is None:
            import aiosqlite

            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS mood ("
                "key TEXT PRIMARY KEY, value TEXT)"
            )
            await self._db.commit()
        return self._db

    async def _get_raw(self, key: str) -> str | None:
        db = await self._get_db()
        async with db.execute(
            "SELECT value FROM mood WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def _set_raw(self, key: str, value: str) -> None:
        db = await self._get_db()
        await db.execute(
            "INSERT INTO mood (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()

    async def load(self, sleep_debt_decay_per_day: float = DAILY_SLEEP_DEBT_DECAY) -> None:
        """读状态；跨日时做一次"隔夜结算"：兴趣衰减、疲惫恢复、睡眠债消退、
        arousal 回落（衰减节奏是"每天一次"，锚在 load）。"""
        self.valence = _clamp(
            _to_float(await self._get_raw("mood_valence"), 0.2),
            VALENCE_MIN,
            VALENCE_MAX,
        )
        self.arousal = _clamp(
            _to_float(await self._get_raw("mood_arousal"), 0.5), UNIT_MIN, UNIT_MAX
        )
        # sleep_debt 先于 energy 解析：energy 上限被睡眠债压制
        self.sleep_debt = _clamp(
            _to_float(await self._get_raw("sleep_debt"), 0.0),
            FATIGUE_MIN,
            FATIGUE_MAX,
        )
        self.energy = _clamp(
            _to_float(await self._get_raw("energy"), 0.8), UNIT_MIN, self._energy_cap()
        )
        self.fatigue = _clamp(
            _to_float(await self._get_raw("fatigue"), 0.0), FATIGUE_MIN, FATIGUE_MAX
        )
        raw_interests = await self._get_raw("interests")
        try:
            loaded = json.loads(raw_interests) if raw_interests else {}
        except (TypeError, ValueError):
            loaded = {}
        interests = {}
        for key, value in loaded.items() if isinstance(loaded, dict) else []:
            parsed = _to_float(value, None)
            if parsed is not None:
                interests[str(key)] = _clamp(parsed, UNIT_MIN, UNIT_MAX)
        self.interests = interests

        today = self._now().date().isoformat()
        stored_date = await self._get_raw("date")
        if stored_date is not None and stored_date != today:
            # 隔夜结算只在"确实跨了天"时触发：首次加载（无 stored_date）应当
            # 保持出厂默认——刚来到世界上的第一刻不算"睡了一夜"
            # 为什么只在日期翻转时结算：兴趣的消退、疲惫的恢复、睡眠债的
            # 消退都是"隔夜"尺度的事，逐次活动结算会让高频活动立刻磨掉
            # 自己刚养起来的状态
            self.decay_interests(DAILY_INTEREST_DECAY)
            self.arousal = _clamp(
                self.arousal * AROUSAL_SLEEP_SETTLE, UNIT_MIN, UNIT_MAX
            )
            self.fatigue = _clamp(
                self.fatigue - DAILY_FATIGUE_RECOVERY, FATIGUE_MIN, FATIGUE_MAX
            )
            self.sleep_debt = _clamp(
                self.sleep_debt - max(sleep_debt_decay_per_day, 0.0),
                FATIGUE_MIN,
                FATIGUE_MAX,
            )
            await self._set_raw("date", today)
            await self.save()

    async def save(self) -> None:
        # date 随状态一起落库：否则"上次活跃日"丢失，隔夜结算永远不触发
        await self._set_raw("date", self._now().date().isoformat())
        await self._set_raw("mood_valence", repr(self.valence))
        await self._set_raw("mood_arousal", repr(self.arousal))
        await self._set_raw("energy", repr(self.energy))
        await self._set_raw("fatigue", repr(self.fatigue))
        await self._set_raw("sleep_debt", repr(self.sleep_debt))
        await self._set_raw("interests", json.dumps(self.interests, ensure_ascii=False))

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    async def record_activity(
        self, activity_name: str, ok: bool, topic: str | None = None,
        duration_seconds: float = 0.0, fatigue_rate_per_hour: float = 4.0,
    ) -> None:
        """活动结束后由 LivingLoop 调用：心情/精力/兴趣/疲惫按规则演化。"""
        if ok:
            self.valence = _clamp(self.valence + DELTA_VALENCE_OK, VALENCE_MIN, VALENCE_MAX)
            self.energy = _clamp(self.energy + DELTA_ENERGY_OK, UNIT_MIN, self._energy_cap())
            self.arousal = _clamp(self.arousal + DELTA_AROUSAL_OK, UNIT_MIN, UNIT_MAX)
        else:
            self.valence = _clamp(
                self.valence + DELTA_VALENCE_FAIL, VALENCE_MIN, VALENCE_MAX
            )
            self.energy = _clamp(
                self.energy + DELTA_ENERGY_FAIL, UNIT_MIN, self._energy_cap()
            )

        if ok:
            if activity_name in INTEREST_TOPIC_ACTIVITIES and topic:
                self.bump_interest(topic, DELTA_INTEREST_TOPIC)
            elif activity_name == "reminisce":
                # 翻旧记忆这件事本身也是兴趣：它爱回味，才会常翻
                self.bump_interest(REMINISCE_INTEREST_KEY, DELTA_INTEREST_REMINISCE)

        # 疲惫按活动实际耗时折算（任务书 B1：fatigue_rate_per_hour 配置由
        # 调用方传入，mood 不读配置）
        hours = max(duration_seconds, 0.0) / 3600.0
        self.fatigue = _clamp(
            self.fatigue + hours * fatigue_rate_per_hour, FATIGUE_MIN, FATIGUE_MAX
        )

        await self.save()

    def _energy_cap(self) -> float:
        """精力上限受睡眠债压制：债满(100)时上限只有 0.5——没睡好的觉，
        第二天做什么都提不起十足的劲。"""
        return _clamp(1.0 - self.sleep_debt / 200.0, 0.5, 1.0)

    def apply_grouchiness(self, enabled: bool) -> bool:
        """被吵醒的起床气（任务书 B3）：命中概率时 valence/energy 双降。

        Returns:
            是否真的起了床气（供日志与记忆语气参考）。
        """
        if not enabled:
            return False
        self.valence = _clamp(
            self.valence + DELTA_GROUCHY_VALENCE, VALENCE_MIN, VALENCE_MAX
        )
        self.energy = _clamp(
            self.energy + DELTA_GROUCHY_ENERGY, UNIT_MIN, self._energy_cap()
        )
        return True

    def add_sleep_debt(self, amount: float) -> None:
        """吵醒按剩余睡眠比例累积睡眠债（任务书 B3），次日结算时消退。"""
        self.sleep_debt = _clamp(
            self.sleep_debt + max(amount, 0.0), FATIGUE_MIN, FATIGUE_MAX
        )

    def add_fatigue(self, amount: float) -> None:
        self.fatigue = _clamp(
            self.fatigue + max(amount, 0.0), FATIGUE_MIN, FATIGUE_MAX
        )

    def bump_interest(self, topic: str, delta: float) -> None:
        current = self.interests.get(topic, 0.0)
        self.interests[topic] = _clamp(current + delta, UNIT_MIN, UNIT_MAX)

    def decay_interests(self, rate: float) -> None:
        """全体兴趣乘以 rate（0<rate<=1）。"""
        self.interests = {
            k: _clamp(v * rate, UNIT_MIN, UNIT_MAX) for k, v in self.interests.items()
        }

    def get_interests(self) -> dict[str, float]:
        return dict(self.interests)

    def interest_weight(self, topic: str) -> float:
        """兴趣度权重：无记录返回 0.3 中性——没接触过的东西也值得一试。"""
        return self.interests.get(topic, NEUTRAL_INTEREST)

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def digest(self) -> str:
        """心境摘要：给决策 LLM 的一段短描述（人话，不是数字转储）。"""
        if self.valence >= 0.3:
            mood_word = "心情不错"
        elif self.valence >= -0.1:
            mood_word = "心情平静"
        else:
            mood_word = "有点低落"
        if self.energy >= 0.6:
            energy_word = "精力充沛"
        elif self.energy >= 0.3:
            energy_word = "精力一般"
        else:
            energy_word = "有点累了"
        parts = [f"{mood_word}（valence={self.valence:.2f}）", energy_word]
        if self.fatigue >= 60:
            parts.append("身体有些疲惫")
        if self.sleep_debt >= 40:
            parts.append("最近没睡好，欠了点觉")
        top = sorted(self.interests.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if top:
            liked = "、".join(f"{k}({v:.2f})" for k, v in top)
            parts.append(f"最近对这些有兴趣：{liked}")
        return "；".join(parts)
