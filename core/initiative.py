"""InitiativeEngine——主动搭话念头系统（M14-补丁1）。

主动出口的第二条通路（与五条既有分享链路完全独立，红线 1）：念头台词
本身就是人格化输出，不走分享改写器；发送成功后复用 M13-补丁1 的双存储
落点进她的工作记忆（E2）。

设计来源（调研报告对照详见任务报告）：
  - private_companion 的"候选来源 → 审核 → 发送"：收敛为
    掷概率 → 选来源 → 生成（含人格终审）→ 闸门 → 发送 → 双写 → 记账；
  - proactive_chat 的"未回复收敛"：连续未回应 → 概率减半 → 阈值后安静；
  - living 自己的差异点——真实生活状态：睡眠期不评估（A2）、时段权重
    （D2）、心境调制（D3），这是两家参考插件都没有的。

F3 未回应口径（任务书钦定，报告有专节说明）：每个念头只结算一次——
发出后的第一次评估时，主人自发出后无任何消息 → 该念头计为未回应
（streak +1）；主人消息到达即时清零（F2，note_owner_message）。
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime
from typing import Any, Callable

from astrbot.api import logger

from .conf_path import conf_group
from .share_rewriter import _strip_wrapping_quotes

# C3：台词硬上限（prompt 要求 30 字，LLM 不听话时在这里兜住）
INITIATIVE_TEXT_MAX = 120
# G2：审计行里的台词预览截断（防日志泄漏长文本）
AUDIT_TEXT_PREVIEW = 20
# F1：发送记录有界保留
SENT_LOG_MAX = 20

# living_state 表的键名（经 gate.state_get/state_set 存取）
STATE_KEY_SENT_LOG = "initiative_sent_log"
STATE_KEY_LAST_SENT = "initiative_last_sent_at"
STATE_KEY_STREAK = "initiative_unanswered_streak"
STATE_KEY_PENDING_SETTLE = "initiative_pending_settle_at"
STATE_KEY_LAST_OWNER_MSG = "initiative_last_owner_msg_at"

# D2 时段权重内置表：[start, end) 小时区间，跨午夜段按 in_time_window 同款
# 语义（start > end）表达；没有任何段命中的小时按 0（保守，不主动）
DEFAULT_HOURLY_WEIGHTS: tuple[tuple[int, int, float], ...] = (
    (6, 9, 0.5),
    (9, 12, 0.9),
    (12, 14, 0.8),
    (14, 18, 0.6),
    (18, 23, 1.0),
    (23, 6, 0.0),
)

# B 组首批来源（H8 sources 的合法值；未知名忽略——配置手滑在审计里可见）
KNOWN_SOURCES = ("random_miss", "open_topic")

# C3：异常形态特征（防 OOC——主动搭话里不该有链接和代码块）
_OOC_MARKERS = ("http://", "https://", "www.", "```")


def parse_hourly_weights(raw: Any) -> tuple[tuple[int, int, float], ...] | None:
    """H5：解析 '06-09=0.5,09-12=0.9,...'。

    任何一段不合法（缺等号/缺横杠/非数字/权重负数）→ None，调用方回落
    内置表；空串/None 同样表示"用内置表"。跨午夜段（如 23-06=0）合法。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    segments: list[tuple[int, int, float]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part or "-" not in part:
            return None
        range_text, _, weight_text = part.partition("=")
        start_text, _, end_text = range_text.partition("-")
        try:
            start, end, weight = (
                int(start_text.strip()),
                int(end_text.strip()),
                float(weight_text.strip()),
            )
        except ValueError:
            return None
        if not (0 <= start <= 24 and 0 <= end <= 24) or weight < 0:
            return None
        segments.append((start, end, weight))
    return tuple(segments) if segments else None


def hour_weight(
    segments: tuple[tuple[int, int, float], ...], now: datetime
) -> float:
    """now.hour 落入的时段权重；无段命中 → 0.0（自定义表有缝隙时保守）。"""
    hour = now.hour
    for start, end, weight in segments:
        if start <= end:
            if start <= hour < end:
                return weight
        elif hour >= start or hour < end:  # 跨午夜段（如 23-06）
            return weight
    return 0.0


def mood_energy_factor(mood: Any) -> float:
    """D3 心境调制：energy ≥ 0.7 → ×1.2；energy ≤ 0.3 → ×0.5；其余 ×1.0。

    读 mood.energy 数值（与 mood.digest() 同一数据源）；mood 未注入或
    取不到数值 → ×1.0（不因观测失败改变行为）。
    """
    if mood is None:
        return 1.0
    try:
        energy = float(getattr(mood, "energy"))
    except (TypeError, ValueError, AttributeError):
        return 1.0
    if energy >= 0.7:
        return 1.2
    if energy <= 0.3:
        return 0.5
    return 1.0


def _looks_like_ooc(text: str) -> bool:
    """C3：URL/代码块等异常形态 → 不像她会说的话，直接 SKIP。"""
    lowered = text.lower()
    return any(marker in lowered for marker in _OOC_MARKERS)


class InitiativeEngine:
    """念头评估器：心跳 tick 驱动，全部依赖可注入可空（A1）。

    流程（A3）：掷概率 → 选候选来源 → 生成台词（含终审）→ 通用闸门 →
    发送 → 双写落库 → 记账；任何环节失败只 DEBUG，绝不抛出影响主循环。
    """

    def __init__(
        self,
        config_getter: Callable[[], Any],
        gate: Any,
        llm_call: Callable[..., Any] | None = None,
        mood: Any = None,
        sender: Any = None,
        session_getter: Callable[[], str | None] | None = None,
        contexts_getter: Callable[[], Any] | None = None,
        speech_writer: Callable[..., Any] | None = None,
        persona_getter: Callable[..., Any] | None = None,
        rng: random.Random | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._gate = gate
        self._llm_call = llm_call  # async (prompt, system) -> str | None
        self._mood = mood
        self._sender = sender
        self._session_getter = session_getter  # () -> 主人 umo | None
        self._contexts_getter = contexts_getter  # async () -> [dict] | None
        # async (text, dedup_key) -> None：双存储落库（E1/E2，main 接线到
        # LivingLoop._write_speech_to_stores，占位与标签在接线层固定）
        self._speech_writer = speech_writer
        self._persona_getter = persona_getter
        self._rng = rng or random.Random()
        self._now = now_provider or datetime.now
        # F 组状态：内存权威 + gate 状态库持久化镜像（跨重启恢复）
        self._sent_log: list[datetime] = []
        self._last_sent_at: datetime | None = None
        self._streak: int = 0
        self._pending_settle_at: datetime | None = None
        self._last_owner_msg_at: datetime | None = None
        self._loaded = False

    # ------------------------------------------------------------------
    # 配置（initiative 组，热读）
    # ------------------------------------------------------------------
    def _cfg(self) -> dict:
        try:
            return conf_group(self._config_getter() or {}, "initiative")
        except Exception:
            return {}

    def _conf_int(self, cfg: dict, key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _conf_float(self, cfg: dict, key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _conf_bool(self, cfg: dict, key: str, default: bool) -> bool:
        raw = cfg.get(key, default)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("false", "0", "off", "no")

    # ------------------------------------------------------------------
    # 持久化（F1/F3：gate 的 living_state 键值表；gate 不支持时仅内存）
    # ------------------------------------------------------------------
    async def _load_state(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        getter = getattr(self._gate, "state_get", None)
        if not callable(getter):
            return

        def _ts(raw: Any) -> datetime | None:
            if not raw:
                return None
            try:
                return datetime.fromisoformat(str(raw))
            except ValueError:
                return None

        try:
            log_raw = await getter(STATE_KEY_SENT_LOG)
            items = json.loads(log_raw) if log_raw else []
            stamps = []
            for item in items if isinstance(items, list) else []:
                parsed = _ts(item)
                if parsed is not None:
                    stamps.append(parsed)
            self._sent_log = stamps[-SENT_LOG_MAX:]
            self._last_sent_at = _ts(await getter(STATE_KEY_LAST_SENT))
            try:
                self._streak = max(int(await getter(STATE_KEY_STREAK) or 0), 0)
            except (TypeError, ValueError):
                self._streak = 0
            self._pending_settle_at = _ts(await getter(STATE_KEY_PENDING_SETTLE))
            self._last_owner_msg_at = _ts(await getter(STATE_KEY_LAST_OWNER_MSG))
        except Exception as e:
            logger.debug(f"[Initiative] 状态恢复失败（按空状态继续）: {e}")

    async def _persist(self, *keys: str) -> None:
        setter = getattr(self._gate, "state_set", None)
        if not callable(setter):
            return
        try:
            for key in keys:
                if key == STATE_KEY_SENT_LOG:
                    value = json.dumps(
                        [ts.isoformat() for ts in self._sent_log],
                        ensure_ascii=False,
                    )
                elif key == STATE_KEY_STREAK:
                    value = str(self._streak)
                else:
                    raw = {
                        STATE_KEY_LAST_SENT: self._last_sent_at,
                        STATE_KEY_PENDING_SETTLE: self._pending_settle_at,
                        STATE_KEY_LAST_OWNER_MSG: self._last_owner_msg_at,
                    }[key]
                    value = raw.isoformat() if raw else ""
                await setter(key, value)
        except Exception as e:
            logger.debug(f"[Initiative] 状态持久化失败（不影响主流程）: {e}")

    # ------------------------------------------------------------------
    # F2/F3：未回应收敛
    # ------------------------------------------------------------------
    async def note_owner_message(self, now: datetime | None = None) -> None:
        """F2：主人消息到达 → 视为已回应，连续未回应计数清零。

        由 on_any_message 旁路调用（I2，main 负责只对念头目标会话调用）。
        同步更新内存 + 异步落库；任何异常由调用方兜（不影响消息主链路）。
        """
        now = now or self._now()
        await self._load_state()
        self._last_owner_msg_at = now
        had_outstanding = self._pending_settle_at is not None or self._streak > 0
        self._pending_settle_at = None
        if self._streak > 0:
            logger.debug(
                f"[Initiative] 主人说话了，未回应计数清零（原 {self._streak}）"
            )
        self._streak = 0
        if had_outstanding:
            await self._persist(
                STATE_KEY_LAST_OWNER_MSG, STATE_KEY_STREAK, STATE_KEY_PENDING_SETTLE
            )
        else:
            await self._persist(STATE_KEY_LAST_OWNER_MSG)

    async def _settle_unanswered(self, now: datetime) -> None:
        """F3 结算：每个念头只结算一次（发出后的第一次评估时）。

        主人自发出后无任何消息 → streak +1（该念头计为未回应）；有消息
        （通常已被 note_owner_message 即时清零）→ 保持 0。口径细节见
        任务报告 F3 专节。
        """
        if self._pending_settle_at is None:
            return
        pending = self._pending_settle_at
        self._pending_settle_at = None
        answered = (
            self._last_owner_msg_at is not None and self._last_owner_msg_at > pending
        )
        if answered:
            self._streak = 0
            logger.debug("[Initiative] 上次念头已有回应（评估期兜底清零）")
        else:
            self._streak += 1
            logger.debug(f"[Initiative] 上次念头未获回应，收敛计数 → {self._streak}")
        await self._persist(STATE_KEY_STREAK, STATE_KEY_PENDING_SETTLE)

    # ------------------------------------------------------------------
    # D 组：概率
    # ------------------------------------------------------------------
    def _probability(self, cfg: dict, now: datetime) -> float:
        """D1-D4：基础概率 × 时段权重 × 心境调制 × 收敛倍率，夹在 [0,1]。"""
        base = max(self._conf_float(cfg, "base_probability", 0.18), 0.0)
        segments = parse_hourly_weights(cfg.get("hourly_weights"))
        if segments is None:
            segments = DEFAULT_HOURLY_WEIGHTS
        p = base * hour_weight(segments, now) * mood_energy_factor(self._mood)
        if self._conf_bool(cfg, "unanswered_backoff", True) and self._streak >= 3:
            p *= 0.5 ** (self._streak // 3)  # F4
        return max(0.0, min(p, 1.0))

    def _enabled_sources(self, cfg: dict) -> list[str]:
        raw = str(cfg.get("sources") or "").strip()
        if not raw:
            return list(KNOWN_SOURCES)
        wanted = [s.strip() for s in raw.split(",") if s.strip()]
        return [s for s in wanted if s in KNOWN_SOURCES]

    def _sent_today(self, now: datetime) -> int:
        today = now.date()
        return sum(1 for ts in self._sent_log if ts.date() == today)

    def _note_initiative_sent(self, now: datetime) -> None:
        """F1 记账 + F3 挂起结算标记（该念头等待下次评估定性）。"""
        self._sent_log.append(now)
        if len(self._sent_log) > SENT_LOG_MAX:
            self._sent_log = self._sent_log[-SENT_LOG_MAX:]
        self._last_sent_at = now
        self._pending_settle_at = now

    # ------------------------------------------------------------------
    # B/C 组：来源与台词
    # ------------------------------------------------------------------
    async def _extract_topic(self) -> str | None:
        """B2 open_topic：从最近聊天上下文提取"可自然接上的话题"。

        提取失败/无合适话题 → None（该来源本次不可用，不降级为随机想念）。
        """
        if self._llm_call is None or self._contexts_getter is None:
            return None
        try:
            contexts = self._contexts_getter()
            if asyncio.iscoroutine(contexts):
                contexts = await contexts
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Initiative] 聊天上下文读取失败（open_topic 不可用）: {e}")
            return None
        lines: list[str] = []
        for message in contexts or []:
            if not isinstance(message, dict):
                continue
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            who = "主人" if message.get("role") == "user" else "你"
            lines.append(f"{who}：{content[:60]}")
        if not lines:
            return None
        prompt = (
            "下面是你和主人最近的聊天记录（节选）：\n"
            + "\n".join(lines)
            + "\n\n从中找一个\"可以自然接上、继续聊下去\"的话题，"
            "用一句短语概括（20 字以内）。\n"
            "要求：必须是还没聊完的话题；不能是需要主人回答的追问；"
            "不要重复已经聊完了的话题。\n"
            "如果没有合适的话题，只输出 NONE。"
        )
        try:
            raw = await self._llm_call(prompt, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Initiative] 话题提取失败（open_topic 不可用）: {e}")
            return None
        topic = str(raw or "").strip().strip("。.！!")
        if not topic or topic.upper() == "NONE" or _looks_like_ooc(topic):
            return None
        return topic[:40]

    async def _system_prompt(self, mood_digest: str) -> str | None:
        """人格 + 心境（与 ShareRewriter._system_prompt 同源拼接惯例）。

        persona getter 兼容同步/异步两种注入形态（main 的 _persona_prompt
        是 async；与 living_loop 探测 livingmemory 管理器的 iscoroutine
        先例一致）。"""
        parts: list[str] = []
        if self._persona_getter is not None:
            try:
                persona = self._persona_getter()
                if asyncio.iscoroutine(persona):
                    persona = await persona
                persona = str(persona or "").strip()
            except Exception:
                persona = ""
            if persona:
                parts.append(f"你的人格设定：\n{persona[:500]}")
        if mood_digest:
            parts.append(f"你现在的状态：{mood_digest}")
        return "\n\n".join(parts) if parts else None

    async def _generate_line(
        self, cfg: dict, now: datetime, topic: str | None
    ) -> str | None:
        """C1/C4：单次 LLM 调用生成主动搭话台词（含人格终审）。

        返回 None = SKIP/空/异常/异常形态（本次不发，审计 llm_skip）。
        """
        if self._llm_call is None:
            return None
        mood_digest = ""
        if self._mood is not None:
            try:
                digest = getattr(self._mood, "digest", None)
                mood_digest = str(digest()) if callable(digest) else ""
            except Exception:
                mood_digest = ""
        material = (
            f"你们之前聊到过：{topic}——可以从它自然接上，也可以只字不提。"
            if topic
            else "没有什么特别的事由，就是忽然想找他说句话。"
        )
        skip_rule = (
            "\n- 如果此刻其实不该说话、或者这话不像你会说的，只输出 SKIP"
            if self._conf_bool(cfg, "final_review_enabled", True)
            else ""
        )
        prompt = (
            f"当前时间：{now.month}月{now.day}日 {now.hour}:{now.minute:02d}。\n"
            f"{material}\n\n"
            "写一句你主动发给主人的话。要求：\n"
            "- 用你自己的口吻，30 字以内\n"
            "- 这是主动搭话，不是回答他：不要\"你说\"\"发过来\"这类回应式措辞，"
            "不要问主人要任何东西，不要催促\n"
            "- 像朋友间随口聊天：不要标题、列表、Markdown、链接，"
            "只输出这句话本身" + skip_rule
        )
        try:
            system = await self._system_prompt(mood_digest)
            raw = await self._llm_call(prompt, system)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Initiative] 台词生成异常: {e}")
            return None
        line = _strip_wrapping_quotes(str(raw or "").strip())
        if not line or line.upper() == "SKIP":
            return None  # C2：终审不过/空输出 → 不发，不重试
        if _looks_like_ooc(line) or "\n" in line:
            return None  # C3：异常形态防 OOC
        return line[:INITIATIVE_TEXT_MAX]

    # ------------------------------------------------------------------
    # G 组：审计
    # ------------------------------------------------------------------
    def _audit(
        self,
        *,
        streak: int | None = None,
        prob: float | None = None,
        roll: float | None = None,
        source: str | None = None,
        gate: str | None = None,
        review: str | None = None,
        reason: str | None = None,
        text: str | None = None,
    ) -> dict:
        """G1：每次评估恰好一条 INFO 审计；G2：台词只留 20 字预览。

        返回 {"sent": bool, "reason": str}（测试观测用私有契约）。
        """
        parts: list[str] = []
        if roll is not None:
            parts.append(f"掷点={roll:.2f}")
        if prob is not None:
            parts.append(f"概率={prob:.2f}")
        if streak is not None:
            parts.append(f"streak={streak}")
        if source is not None:
            parts.append(f"来源={source}")
        if gate is not None:
            parts.append(f"闸门={gate}")
        if review is not None:
            parts.append(f"终审={review}")
        result: dict = {"sent": False, "reason": reason or ""}
        if text is not None:
            parts.append(f"→ 已发送({len(text)}字)：{text[:AUDIT_TEXT_PREVIEW]}")
            result["sent"] = True
            result["reason"] = "sent"
            result["text"] = text
        elif reason:
            parts.append(f"→ 未发送(reason={reason})")
        logger.info("[Initiative] 评估 " + " ".join(parts))
        return result

    # ------------------------------------------------------------------
    # A3：主流程
    # ------------------------------------------------------------------
    async def tick(self, now: datetime | None = None) -> dict:
        """一次念头评估。loop 心跳调用（I1，仅清醒分支），也可直接调用。"""
        now = now or self._now()
        await self._load_state()
        cfg = self._cfg()
        if not self._conf_bool(cfg, "enabled", True):
            # H1 关闭：系统整体静默，不产审计
            return {"sent": False, "reason": "disabled"}

        # A2：睡眠期静默（loop 只在清醒分支调用，这里是直接调用时的兜底）
        asleep_check = getattr(self._gate, "is_asleep_now", None)
        if callable(asleep_check):
            try:
                if asleep_check(now):
                    return self._audit(reason="sleeping")
            except Exception as e:
                logger.debug(f"[Initiative] 睡眠判定失败（按清醒继续）: {e}")

        # F3：结算上一个念头的"是否被回应"
        await self._settle_unanswered(now)
        streak = self._streak

        # F5：streak ≥ 6 → 懂事地安静下来（主人回应即恢复）
        if self._conf_bool(cfg, "unanswered_backoff", True) and streak >= 6:
            return self._audit(streak=streak, reason="backoff_silent")

        # D7：每日念头上限（独立计数）
        daily_max = max(self._conf_int(cfg, "daily_max", 3), 0)
        if daily_max > 0 and self._sent_today(now) >= daily_max:
            return self._audit(streak=streak, reason="daily_max")

        # D6：念头独立最小间隔（与通用闸门取更严者，这里先拦省 token）
        min_interval = max(self._conf_float(cfg, "min_interval_minutes", 150.0), 0.0)
        if (
            min_interval > 0
            and self._last_sent_at is not None
            and (now - self._last_sent_at).total_seconds() < min_interval * 60
        ):
            return self._audit(streak=streak, reason="initiative_interval")

        # D1-D4：概率掷点
        prob = self._probability(cfg, now)
        roll = self._rng.random() if hasattr(self._rng, "random") else self._rng()
        if roll >= prob:
            return self._audit(
                streak=streak, prob=prob, roll=roll, reason="no_roll"
            )

        # B3：来源选择（等权随机；open_topic 需提取成功，失败不降级）
        sources = self._enabled_sources(cfg)
        if not sources:
            return self._audit(
                streak=streak, prob=prob, roll=roll, reason="no_source"
            )
        source = (
            self._rng.choice(sources)
            if hasattr(self._rng, "choice")
            else sources[0]
        )
        topic: str | None = None
        if source == "open_topic":
            topic = await self._extract_topic()
            if not topic:
                return self._audit(
                    streak=streak, prob=prob, roll=roll, source=source,
                    reason="no_source",
                )

        # C：台词生成 + 人格终审（一次调用）
        line = await self._generate_line(cfg, now, topic)
        if line is None:
            return self._audit(
                streak=streak, prob=prob, roll=roll, source=source,
                reason="llm_skip",
            )

        # D5：通用闸门（每日上限/最小间隔/静默时段，默认值不改）
        try:
            allow, gate_reason = await self._gate.should_send_message(now)
        except Exception as e:
            logger.debug(f"[Initiative] 通用闸门判定失败（本次不发）: {e}")
            allow, gate_reason = False, "gate_error"
        if not allow:
            return self._audit(
                streak=streak, prob=prob, roll=roll, source=source,
                reason=f"gate:{gate_reason}",
            )

        # I3：发送（与分享同源 sender；目标会话取解析结果的第一个）
        session = None
        if self._session_getter is not None:
            try:
                session = self._session_getter()
            except Exception as e:
                logger.debug(f"[Initiative] 目标会话解析失败: {e}")
        if not session or self._sender is None:
            return self._audit(
                streak=streak, prob=prob, roll=roll, source=source,
                reason="no_target",
            )
        try:
            sent = bool(await self._sender.send(session, line))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[Initiative] 发送异常: {e}")
            sent = False
        if not sent:
            return self._audit(
                streak=streak, prob=prob, roll=roll, source=source,
                reason="send_failed",
            )

        # E2：双写落库（对话上下文 + livingmemory 会话，主人真实 umo；
        # 失败只 DEBUG，在 writer 内部处理，这里再兜一层）
        if self._speech_writer is not None:
            try:
                # '#' 前缀：共享幂等集合按字典序裁剪时念头键排在活动键之前，
                # 先被挤出——活动侧幂等键的保留窗口不受影响（红线 4）
                dedup_key = f"#initiative:{now.strftime('%Y%m%d_%H%M%S')}:{source}"
                await self._speech_writer(line, dedup_key)
            except Exception as e:
                logger.debug(f"[Initiative] 双写落库失败（不影响发送）: {e}")

        # 记账：通用闸门配额（D5 复用）+ 念头账本（F1）
        try:
            await self._gate.note_message_sent(now)
        except Exception as e:
            logger.debug(f"[Initiative] 通用闸门记账失败: {e}")
        self._note_initiative_sent(now)
        await self._persist(
            STATE_KEY_SENT_LOG, STATE_KEY_LAST_SENT, STATE_KEY_PENDING_SETTLE,
            STATE_KEY_STREAK,
        )
        return self._audit(
            streak=streak, prob=prob, roll=roll, source=source,
            gate="通过", review="通过", text=line,
        )
