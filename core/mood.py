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
# 精力保底（任务书 M3 补丁 IV-B3）：energy 允许被消耗但绝不触底为 0——
# 0 意味着"永远动不了"的死锁；保底相当于"再累也还剩一口气"
ENERGY_FLOOR = 0.05
# 兴趣清理阈值（任务书 M3 补丁 IV-B4）：每日衰减后低于它的条目直接删除，
# 防止 interests 字典随时间无限膨胀
INTEREST_PRUNE_THRESHOLD = 0.01
# 近期主题追踪窗口（任务书 M3 补丁 VII 需求 2）：记录最近 N 次活动的实际
# 主题，供重复惩罚与探索配额使用；默认值可被配置 recent_topic_window 覆盖
RECENT_TOPIC_WINDOW_DEFAULT = 6
# 重复惩罚表（补丁 VII 需求 2）：最近窗口内出现 1/2/>=3 次的权重乘数，
# 可被配置 recent_topic_penalty 覆盖
RECENT_TOPIC_PENALTY_DEFAULT = (0.5, 0.3, 0.15)
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
        # 近期主题追踪（补丁 VII 需求 2）：按时间顺序记录最近 N 次活动
        # 实际使用的 topic——重复惩罚与探索配额的数据源
        self.recent_topics: list[str] = []

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

    async def load(
        self,
        sleep_debt_decay_per_day: float = DAILY_SLEEP_DEBT_DECAY,
        interest_daily_decay: float = DAILY_INTEREST_DECAY,
    ) -> None:
        """读状态；跨日时做一次"隔夜结算"：兴趣衰减、疲惫恢复、睡眠债消退、
        arousal 回落（衰减节奏是"每天一次"，锚在 load）。

        interest_daily_decay 可配置（补丁 VII 需求 1：衰减系数是偏执循环
        的成因之一，提为可调项留调参口）。"""
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
        self.energy = self._clamp_energy(
            _to_float(await self._get_raw("energy"), 0.8)
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

        raw_recent = await self._get_raw("recent_topics")
        try:
            loaded_recent = json.loads(raw_recent) if raw_recent else []
        except (TypeError, ValueError):
            loaded_recent = []
        self.recent_topics = [
            str(t) for t in loaded_recent if str(t).strip()
        ][-RECENT_TOPIC_WINDOW_DEFAULT:]

        today = self._now().date().isoformat()
        stored_date = await self._get_raw("date")
        if stored_date is not None and stored_date != today:
            # 隔夜结算只在"确实跨了天"时触发：首次加载（无 stored_date）应当
            # 保持出厂默认——刚来到世界上的第一刻不算"睡了一夜"
            # 为什么只在日期翻转时结算：兴趣的消退、疲惫的恢复、睡眠债的
            # 消退都是"隔夜"尺度的事，逐次活动结算会让高频活动立刻磨掉
            # 自己刚养起来的状态
            self.decay_interests(interest_daily_decay)
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
        await self._set_raw(
            "recent_topics", json.dumps(self.recent_topics, ensure_ascii=False)
        )

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
            self.energy = self._clamp_energy(self.energy + DELTA_ENERGY_OK)
            self.arousal = _clamp(self.arousal + DELTA_AROUSAL_OK, UNIT_MIN, UNIT_MAX)
        else:
            self.valence = _clamp(
                self.valence + DELTA_VALENCE_FAIL, VALENCE_MIN, VALENCE_MAX
            )
            self.energy = self._clamp_energy(self.energy + DELTA_ENERGY_FAIL)

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

    def _clamp_energy(self, value: float) -> float:
        """energy 的统一钳制：上限受睡眠债压制，下限是保底值而非 0。"""
        return _clamp(value, ENERGY_FLOOR, self._energy_cap())

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
        self.energy = self._clamp_energy(self.energy + DELTA_GROUCHY_ENERGY)
        return True

    def add_sleep_debt(self, amount: float) -> None:
        """吵醒按剩余睡眠比例累积睡眠债（任务书 B3），次日结算时消退。"""
        self.sleep_debt = _clamp(
            self.sleep_debt + max(amount, 0.0), FATIGUE_MIN, FATIGUE_MAX
        )

    def bump_interest(self, topic: str, delta: float) -> None:
        """兴趣增益带饱和曲线（任务书 M3 补丁 VII 需求 1）。

        effective_delta = delta * (1 - current)：新主题全额增益，0.5 时
        减半，1.0 时增益归零——兴趣天然收敛到上限而不是撞死在 1.0。
        这是去"偏执循环"的第一道闸：读得越多涨得越少的边际递减，
        让别的主题有机会起量。
        """
        current = self.interests.get(topic, 0.0)
        effective_delta = delta * (1.0 - current)
        self.interests[topic] = _clamp(
            current + effective_delta, UNIT_MIN, UNIT_MAX
        )

    def decay_interests(self, rate: float) -> None:
        """全体兴趣乘以 rate（0<rate<=1）；衰减后低于阈值的条目直接删除。

        为什么删除而不是留着：低于 0.01 的兴趣对决策权重毫无影响，留着只
        会让字典随时间无限膨胀（任务书 M3 补丁 IV-B4）。
        """
        pruned = {}
        for key, value in self.interests.items():
            decayed = _clamp(value * rate, UNIT_MIN, UNIT_MAX)
            if decayed >= INTEREST_PRUNE_THRESHOLD:
                pruned[key] = decayed
        self.interests = pruned

    def get_interests(self) -> dict[str, float]:
        return dict(self.interests)

    def interest_weight(
        self,
        topic: str,
        repeat_count: int = 0,
        penalty_table: tuple[float, ...] = RECENT_TOPIC_PENALTY_DEFAULT,
    ) -> float:
        """兴趣度权重：无记录返回 0.3 中性——没接触过的东西也值得一试。

        repeat_count：该主题在近期窗口内的出现次数（补丁 VII 需求 2）。
        重复会按 penalty_table 乘衰减系数（1 次 x0.5、2 次 x0.3、>=3 次
        x0.15）——"新鲜感递减就换"，打破偏执循环的第二道闸。
        """
        weight = self.interests.get(topic, NEUTRAL_INTEREST)
        if repeat_count <= 0:
            return weight
        table = penalty_table or RECENT_TOPIC_PENALTY_DEFAULT
        index = min(max(repeat_count, 1), len(table)) - 1
        return weight * table[index]

    def record_recent_topics(self, topics: list[str], window: int = RECENT_TOPIC_WINDOW_DEFAULT) -> None:
        """记录活动实际使用的 topic（最新在尾部），窗口滑动截断。"""
        for topic in topics or []:
            text = str(topic).strip()
            if text:
                self.recent_topics.append(text)
        self.recent_topics = self.recent_topics[-max(window, 1):]

    def recent_topics_list(self) -> list[str]:
        return list(self.recent_topics)

    def recent_topic_count(self, topic: str) -> int:
        return self.recent_topics.count(topic)

    def cooldown_hot_interests(self, threshold: float, factor: float) -> list[str]:
        """一次性降温（补丁 VII 需求 5）：兴趣 >= threshold 的条目乘 factor。

        返回被降温的主题列表（调用方记日志/幂等标记）。历史污染数据
        （memory palace = 1.0）不降温的话新机制要连跑数天才能自然稀释。
        """
        cooled = []
        for key, value in self.interests.items():
            if value >= threshold:
                self.interests[key] = _clamp(value * factor, UNIT_MIN, UNIT_MAX)
                cooled.append(key)
        return cooled

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def digest(self) -> str:
        """心境摘要：给决策 LLM 的一段短描述（人话，不是数字转储）。

        补丁 XVII L1-b：兴趣行弱化——只报 top2、去掉数值，措辞从"对这些
        有兴趣"降为"偶尔在琢磨"——原来的写法会被 LLM 当作"继续做这个"的
        指令，与近期话题惩罚机制对着干（兴趣回环放大器，补丁 XVII 根因 b）。
        """
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
        parts = [mood_word, energy_word]
        if self.fatigue >= 60:
            parts.append("身体有些疲惫")
        if self.sleep_debt >= 40:
            parts.append("最近没睡好，欠了点觉")
        top = sorted(self.interests.items(), key=lambda kv: kv[1], reverse=True)[:2]
        if top:
            liked = "、".join(k for k, _ in top)
            parts.append(f"最近偶尔在琢磨的方向：{liked}（浅尝过，未必延续）")
        return "；".join(parts)


# ---------------------------------------------------------------------------
# 自主作息（任务书 M3 补丁 X）：醒来恢复与白天小睡
# ---------------------------------------------------------------------------

async def restore_after_sleep(mood, actual_hours: float, planned_hours: float) -> dict:
    """长睡醒来结算：energy 恢复到 0.85~1.0，睡眠债按实睡比例保留。

    睡满预计时长 → 债清零；早醒 → 按实睡/预计比例保留残余债
    （"睡到中午"与"八九点就起来"的差别在这里体现）。
    返回结算明细供日志。
    """
    ratio = 1.0
    if planned_hours > 0:
        ratio = max(0.0, min(1.0, actual_hours / planned_hours))
    mood.sleep_debt = _clamp(mood.sleep_debt * (1.0 - ratio), FATIGUE_MIN, FATIGUE_MAX)
    restore = 0.85 + 0.15 * ratio  # 睡得越足恢复越高（0.85~1.0）
    mood.energy = mood._clamp_energy(max(mood.energy, restore))
    mood.arousal = _clamp(mood.arousal * AROUSAL_SLEEP_SETTLE, UNIT_MIN, UNIT_MAX)
    await mood.save()
    return {
        "debt_remaining": mood.sleep_debt,
        "energy": mood.energy,
        "ratio": ratio,
    }


async def apply_nap_effects(mood, nap_minutes: float) -> dict:
    """白天小睡结束结算：energy +0.3、sleep_debt -20（下限 0）。"""
    mood.energy = mood._clamp_energy(mood.energy + 0.3)
    mood.sleep_debt = _clamp(
        mood.sleep_debt - 20.0 * (nap_minutes / 60.0) * 2, FATIGUE_MIN, FATIGUE_MAX
    )
    await mood.save()
    return {"energy": mood.energy, "debt": mood.sleep_debt}
