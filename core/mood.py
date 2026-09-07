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

    async def load(self) -> None:
        """读状态；跨日时做一次兴趣衰减（衰减节奏是"每天一次"，锚在 load）。"""
        self.valence = _clamp(
            _to_float(await self._get_raw("mood_valence"), 0.2),
            VALENCE_MIN,
            VALENCE_MAX,
        )
        self.arousal = _clamp(
            _to_float(await self._get_raw("mood_arousal"), 0.5), UNIT_MIN, UNIT_MAX
        )
        self.energy = _clamp(
            _to_float(await self._get_raw("energy"), 0.8), UNIT_MIN, UNIT_MAX
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
        if stored_date != today:
            # 为什么只在日期翻转时衰减：兴趣的消退是"隔夜"尺度的事，
            # 逐次活动衰减会让高频活动把自己刚养起来的兴趣立刻磨掉
            self.decay_interests(DAILY_INTEREST_DECAY)
            await self._set_raw("date", today)
            await self.save()

    async def save(self) -> None:
        await self._set_raw("mood_valence", repr(self.valence))
        await self._set_raw("mood_arousal", repr(self.arousal))
        await self._set_raw("energy", repr(self.energy))
        await self._set_raw("interests", json.dumps(self.interests, ensure_ascii=False))

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    async def record_activity(
        self, activity_name: str, ok: bool, topic: str | None = None
    ) -> None:
        """活动结束后由 LivingLoop 调用：心情/精力/兴趣按规则演化。"""
        if ok:
            self.valence = _clamp(self.valence + DELTA_VALENCE_OK, VALENCE_MIN, VALENCE_MAX)
            self.energy = _clamp(self.energy + DELTA_ENERGY_OK, UNIT_MIN, UNIT_MAX)
        else:
            self.valence = _clamp(
                self.valence + DELTA_VALENCE_FAIL, VALENCE_MIN, VALENCE_MAX
            )
            self.energy = _clamp(self.energy + DELTA_ENERGY_FAIL, UNIT_MIN, UNIT_MAX)

        if ok:
            if activity_name in INTEREST_TOPIC_ACTIVITIES and topic:
                self.bump_interest(topic, DELTA_INTEREST_TOPIC)
            elif activity_name == "reminisce":
                # 翻旧记忆这件事本身也是兴趣：它爱回味，才会常翻
                self.bump_interest(REMINISCE_INTEREST_KEY, DELTA_INTEREST_REMINISCE)

        await self.save()

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
        top = sorted(self.interests.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if top:
            liked = "、".join(f"{k}({v:.2f})" for k, v in top)
            parts.append(f"最近对这些有兴趣：{liked}")
        return "；".join(parts)
