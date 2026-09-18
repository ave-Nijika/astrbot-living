"""ActivityDecider——决策层：rules / hybrid / llm 三档（总纲 D2）。

三档分工（M2 范围：不接入 tool_loop_agent，活动执行仍走 M1 能力调用）：
  - rules:  加权随机 + 避免重复。零成本兜底；有心境时按精力做简单倾向
            （累了想安静，精神好想玩游戏）。
  - hybrid: rules 选活动 + LLM 只出"怎么做"的参数（主题/风格）。
            LLM 挂了/解析失败 → 参数为空，活动内部回退随机主题，不阻塞。
  - llm:    LLM 拿着人设+心境+近期记忆+活动清单，自己选活动并给参数。
            任何失败回退 rules。

设计约定：LLM 调用通过注入的 llm_call 异步函数完成（main.py 用
context.llm_generate 实现），本模块不直接依赖 AstrBot provider——
测试可注入 mock，决策失败永远静默回退，绝不让"想一想"变成"卡住"。
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from astrbot.api import logger

from .activities import Activity

VALID_MODES = ("rules", "hybrid", "llm")
# hybrid 档 LLM 只给"怎么做"的参数；peek 没有可参数化的部分
PARAMETERIZABLE = ("surf", "read", "game")
_PARAM_MAX_LEN = 30  # 参数是主题词/风格，不是文章——截断防 LLM 跑题


def extract_json_object(text: Any) -> dict | None:
    """从 LLM 回复里抠出第一个 JSON 对象；剥代码围栏，失败返回 None。

    为什么宽容解析：要求 LLM"只输出 JSON"它也常裹一句客套话或 ```json
    围栏，解析必须对现实让步——抠不到就回退，绝不为格式重试烧 token。
    """
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


def _clean_param(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:_PARAM_MAX_LEN] if text else None


# 方向归并（补丁 XVII L2.5）：LLM 产出的方向词清洗上限
_DIRECTION_MAX_LEN = 24  # 单个方向词长度上限（"咖啡/发酵化学×2"这个量级）
_DIRECTION_MAX_ITEMS = 4  # 方向词数量上限（任务书：2-4 个）


def _clean_directions(raw: Any) -> list[str]:
    """清洗 LLM 顺带产出的方向归并结果。

    容错一切形态：非 list / 元素非字符串 / 空串 / 超长 / 重复都处理掉；
    清洗后为空即视为"没产出"（调用方保留旧缓存或回退）。
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        text = str(item or "").strip()[:_DIRECTION_MAX_LEN].strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:_DIRECTION_MAX_ITEMS]


@dataclass
class Decision:
    """一次决策结果。note 记录回退原因（诊断用，不进日志 INFO）。"""

    activity: Activity
    params: dict
    mode: str
    note: str | None = None


class ActivityDecider:
    """三档决策器。所有外部依赖（LLM/persona/记忆）均可注入、均可失效。"""

    def __init__(
        self,
        activities: list[Activity],
        config_getter: Callable[[], Any],
        rng: random.Random | None = None,
        llm_call: Callable[..., Any] | None = None,
        mood: Any = None,
        persona_getter: Callable[..., Any] | None = None,
        life_extra_getter: Callable[[], str] | None = None,
        memory_getter: Callable[..., Any] | None = None,
    ) -> None:
        self._activities = list(activities)
        self._config_getter = config_getter
        self._rng = rng or random.Random()
        self._llm_call = llm_call  # async (prompt, system_prompt) -> str | None
        self._mood = mood
        self._persona_getter = persona_getter  # async () -> str | None
        self._life_extra_getter = life_extra_getter  # () -> str
        self._memory_getter = memory_getter  # async () -> MemoryBackend
        self._last_name: str | None = None
        # 补丁 XVII L2.5：方向归并缓存 (topics_fingerprint, directions)。
        # 归并在决策 LLM 调用里顺带产出，同一批 recent_topics 不重复归并；
        # topics 变了指纹失配 → 自动回退原始清单注入（绝不阻塞决策）。
        self._direction_cache: "tuple[tuple[str, ...], list[str]] | None" = None

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def decide(self, now: datetime | None = None) -> Decision:
        mode = self._mode()
        try:
            if mode == "rules":
                return Decision(self.rules_pick(), {}, "rules")
            if mode == "llm":
                decision = await self._llm_decide()
                if decision is not None:
                    return decision
                return Decision(
                    self.rules_pick(), {}, "rules", note="llm_failed_fallback_rules"
                )
            # hybrid（默认）：规则选活动，LLM 细化做法
            activity = self.rules_pick()
            params = await self._params_for(activity)
            return Decision(activity, params, "hybrid")
        except Exception as e:
            # 决策层的任何意外都不允许打断生活——回退 rules 继续过日子
            logger.warning(f"[Decider] 决策异常，回退 rules: {e}")
            return Decision(self.rules_pick(), {}, "rules", note=f"error_fallback: {e}")

    def _mode(self) -> str:
        try:
            from .conf_path import conf_group

            cfg = conf_group(self._config_getter() or {}, "decision")
            mode = str(cfg.get("decision_mode", "hybrid"))
            return mode if mode in VALID_MODES else "hybrid"
        except Exception:
            return "hybrid"

    def _effective_activities(self) -> "list[Activity]":
        """决策池（补丁 XV 清单3）：decision.free_activity_enabled=false 时
        摘除 free。每次决策现读配置——开关热生效，改配置下个决策即回固定池。"""
        try:
            from .conf_path import conf_group

            raw = conf_group(self._config_getter() or {}, "decision").get(
                "free_activity_enabled"
            )
            if raw is not None and not bool(raw):
                return [a for a in self._activities if a.name != "free"]
        except Exception:
            pass
        return self._activities

    # ------------------------------------------------------------------
    # rules 档
    # ------------------------------------------------------------------
    def rules_pick(self) -> Activity:
        """加权随机 + 避免连续重复。心境只是"倾向"，不是"规则"——权重×2
        足够让选择有性格，又不至于变成可预测的循环。"""
        pool = [
            a for a in self._effective_activities() if a.name != self._last_name
        ] or self._effective_activities()
        weights = [self._weight_for(a) for a in pool]
        chosen = self._weighted_choice(pool, weights)
        self._last_name = chosen.name
        return chosen

    def _weight_for(self, activity: Activity) -> float:
        """rules 档的心境倾向（需求 D）：累了偏安静活动，精神好偏玩游戏。"""
        if self._mood is None:
            return 1.0
        if self._mood.energy < 0.3 and activity.name in ("reminisce", "surf"):
            return 2.0
        if self._mood.energy > 0.7 and activity.name == "game":
            return 2.0
        return 1.0

    def _weighted_choice(self, pool: list[Activity], weights: list[float]) -> Activity:
        if all(w == weights[0] for w in weights):
            return self._rng.choice(pool)  # 等权走 choice：兼容注入的脚本化 rng
        total = sum(weights)
        point = self._rng.random() * total
        cumulative = 0.0
        for activity, weight in zip(pool, weights):
            cumulative += weight
            if point < cumulative:
                return activity
        return pool[-1]

    # ------------------------------------------------------------------
    # 近期话题与探索配额（任务书 M3 补丁 VII 需求 2/3）
    # ------------------------------------------------------------------
    def _recent_topic_summary(self) -> str:
        """近期话题清单文案："memory palace techniques（3 次）、睡前回顾（2 次）"。

        空返回空串——prompt 拼接端跳过空段。数据来自 mood 的近期主题
        追踪（最近 N 次活动实际使用的 topic）。
        """
        if self._mood is None:
            return ""
        try:
            recent = self._mood.recent_topics_list()
        except Exception:
            return ""
        if not recent:
            return ""
        counts = Counter(recent)
        return "、".join(f"{topic}（{count} 次）" for topic, count in counts.most_common())

    # ------------------------------------------------------------------
    # 方向归并（补丁 XVII L2.5）：对付"字符串不重复但谱系重复"。
    # 归并本身并入决策 LLM 调用（零新增调用），这里只负责：取指纹、
    # 用缓存拼方向级注入、存新归并。LLM 没产出方向 → 缓存不更新，
    # 下轮自动回退原始清单注入——行为不劣于现状，绝不阻塞决策。
    # ------------------------------------------------------------------
    def _recent_topics_fingerprint(self) -> "tuple[str, ...]":
        try:
            return tuple(self._mood.recent_topics_list())
        except Exception:
            return ()

    def _direction_section(self, fingerprint: "tuple[str, ...]") -> str:
        """方向级注入文案（缓存命中时）；未命中返回空串。"""
        if not fingerprint or self._direction_cache is None:
            return ""
        cached_topics, directions = self._direction_cache
        if cached_topics != fingerprint or not directions:
            return ""
        return "你最近折腾过的方向：" + "、".join(directions) + "——这次挑一个完全不同的方向。"

    def _raw_topics_section(self, fingerprint: "tuple[str, ...]") -> str:
        """原始话题清单注入（方向缓存未命中时的回退形态）。"""
        if not fingerprint:
            return ""
        return "你最近已经折腾过这些话题（太多了会腻）：" + "、".join(fingerprint) + "。"

    def _store_directions(self, fingerprint: "tuple[str, ...]", raw: Any) -> bool:
        """清洗并缓存本次 LLM 顺带产出的方向归并；返回是否成功入库。"""
        directions = _clean_directions(raw)
        if not directions or not fingerprint:
            return False
        self._direction_cache = (fingerprint, directions)
        logger.info(f"[Decider] 方向归并（缓存更新）：{'、'.join(directions)}")
        return True

    def _decision_group(self) -> dict:
        try:
            from .conf_path import conf_group

            return conf_group(self._config_getter() or {}, "decision")
        except Exception:
            return {}

    def _int_setting(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _exploration_trigger(self) -> tuple[int, int]:
        """探索配额配置：(窗口, 触发次数)，默认最近 4 次内同话题 >=3 次。"""
        decision = self._decision_group()
        window = self._int_setting(decision.get("exploration_window"), 4)
        trigger = self._int_setting(decision.get("exploration_trigger"), 3)
        return max(window, 1), max(trigger, 1)

    def _exploration_needed(self) -> tuple[bool, str]:
        """偏执循环检测：最近窗口内同一 topic 出现达到触发次数 → 强制探索。

        返回 (是否触发, 热门话题)。锁死机制的特征就是同一主题反复出现，
        此时无论 rules 还是 LLM 都该被按头换方向。
        """
        if self._mood is None:
            return False, ""
        try:
            recent = self._mood.recent_topics_list()
        except Exception:
            return False, ""
        window, trigger = self._exploration_trigger()
        if len(recent) < trigger:
            return False, ""
        counts = Counter(recent[-window:])
        if not counts:
            return False, ""
        topic, count = counts.most_common(1)[0]
        if count >= trigger:
            return True, topic
        return False, ""

    def _exploration_directive(self) -> str:
        """探索强制指令（触发时拼进 LLM prompt）。"""
        _triggered, hot = self._exploration_needed()
        if _triggered:
            return (
                f"注意：你最近反复折腾「{hot}」，已经偏执了。"
                "这次必须选一个你从没接触过的新话题，从零开始了解它。"
            )
        return ""

    # ------------------------------------------------------------------
    # hybrid 档
    # ------------------------------------------------------------------
    async def _params_for(self, activity: Activity) -> dict:
        if activity.name not in PARAMETERIZABLE or self._llm_call is None:
            return {}
        mood_block = self._mood.digest() if self._mood is not None else "心情平静，精力一般"
        fingerprint: tuple[str, ...] = ()
        if activity.name == "game":
            prompt = (
                f"你现在打算写个小游戏自己玩。你现在的状态：{mood_block}。\n"
                '顺着状态选一个具体的小游戏风格。只输出 JSON，格式：{"style": "…"}'
            )
        else:
            # 补丁 XVII L2.5：近期方向注入 + 归并并入同一次调用（零新增调用）。
            # 方向缓存命中 → 方向级表述；未命中 → 原始清单（回退形态）。
            action = "上网冲浪（搜索）" if activity.name == "surf" else "读一篇文章"
            fingerprint = self._recent_topics_fingerprint()
            recent_block = self._direction_section(fingerprint) or (
                self._raw_topics_section(fingerprint)
            )
            recent_line = f"{recent_block}\n" if recent_block else ""
            # 无近期话题时归并指令是空指令，不拼（省 token 也防 LLM 编造）
            merge_line = (
                "顺带把你最近折腾过的话题归并成不超过 4 个方向。\n"
                if fingerprint
                else ""
            )
            json_spec = (
                '只输出 JSON，格式：{"topic": "…", '
                '"directions": ["方向×出现次数", "…"]}'
                if fingerprint
                else '只输出 JSON，格式：{"topic": "…"}'
            )
            prompt = (
                f"你现在打算{action}。你现在的状态：{mood_block}。\n"
                f"{recent_line}"
                "顺着状态选一个具体、有生活气息的主题词，"
                "选一个你最近没碰过的方向，越新鲜越好。\n"
                f"{merge_line}"
                f"{json_spec}"
            )
        system_prompt = await self._system_prompt()
        raw = await self._safe_llm(prompt, system_prompt)
        data = extract_json_object(raw)
        if not data:
            # 解析失败不阻塞活动：参数留空，活动内部回退随机主题
            return {}
        params: dict = {}
        for key in ("topic", "style"):
            value = _clean_param(data.get(key))
            if value:
                params[key] = value
        if fingerprint:
            self._store_directions(fingerprint, data.get("directions"))
        return params

    # ------------------------------------------------------------------
    # llm 档
    # ------------------------------------------------------------------
    async def _llm_decide(self) -> Decision | None:
        if self._llm_call is None:
            return None
        memories = await self._recent_memories()
        effective = self._effective_activities()
        activity_lines = "\n".join(
            f"- {a.name}: {a.description}" for a in effective
        )
        memory_block = "\n".join(f"- {m}" for m in memories) if memories else "（还没什么记忆）"
        mood_block = self._mood.digest() if self._mood is not None else "心情平静，精力一般"
        # 补丁 XVII L2.5：方向缓存命中 → 方向级表述；未命中 → 原有字符串清单
        fingerprint = self._recent_topics_fingerprint()
        direction_section = self._direction_section(fingerprint)
        if direction_section:
            recent_section = f"\n{direction_section}\n"
        else:
            recent_block = self._recent_topic_summary()
            recent_section = (
                f"\n你最近已经折腾过这些话题（太多了会腻）：{recent_block}。\n"
                if recent_block
                else "\n"
            )
        exploration_line = self._exploration_directive()
        if exploration_line:
            exploration_line = f"{exploration_line}\n"
        prompt = (
            "现在是你的独处时间，没有人在找你，可以自己决定干点什么。\n\n"
            f"你现在的状态：{mood_block}\n\n"
            f"最近记得的事：\n{memory_block}\n"
            f"{recent_section}\n"
            f"可以做的活动：\n{activity_lines}\n\n"
            f"{exploration_line}"
            "请选一个你现在最想做的活动，并给它合适参数（topic 为主题词，"
            'style 为小游戏风格，peek 和 reminisce 不需要参数）。'
            "如果上面列了你最近反复折腾的话题，这次避开它们。\n"
            + (
                "顺带把你最近折腾过的话题归并成不超过 4 个方向。\n"
                if fingerprint
                else ""
            )
            + (
                '只输出 JSON，格式：{"activity": "…", "params": {"topic": "…"}, '
                '"directions": ["方向×出现次数", "…"]}'
                if fingerprint
                else '只输出 JSON，格式：{"activity": "…", "params": {"topic": "…"}}'
            )
        )
        system_prompt = await self._system_prompt()
        raw = await self._safe_llm(prompt, system_prompt)
        data = extract_json_object(raw)
        if not data:
            return None
        if fingerprint:
            self._store_directions(fingerprint, data.get("directions"))
        name = str(data.get("activity", "")).strip()
        activity = next((a for a in effective if a.name == name), None)
        if activity is None:
            return None  # 选了不存在的活动：当它没说，回退 rules
        raw_params = data.get("params")
        params: dict = {}
        if isinstance(raw_params, dict):
            for key in ("topic", "style"):
                value = _clean_param(raw_params.get(key))
                if value:
                    params[key] = value
        self._last_name = activity.name
        return Decision(activity, params, "llm")

    # ------------------------------------------------------------------
    # prompt 素材（全部可失效：缺谁都能拼出一个能用的 prompt）
    # ------------------------------------------------------------------
    async def _system_prompt(self) -> str | None:
        """人设拼接（总纲 D4）：AstrBot persona 为主人格 + life_extra 补充。"""
        parts = []
        if self._persona_getter is not None:
            try:
                persona = await self._persona_getter()
                if persona:
                    parts.append(f"你的人格设定：\n{persona}")
            except Exception:
                pass  # C1：取不到 persona 就静默跳过，决策仍可用
        if self._life_extra_getter is not None:
            try:
                life_extra = self._life_extra_getter() or ""
                if str(life_extra).strip():
                    # 补丁 XVII L1-a：措辞从"身份设定"降为"背景参考"——
                    # 原写法会被 LLM 当身份定义严格执行，话题被人设点名
                    # 的方向（如咖啡）绑死（补丁 XVII 根因 a）
                    parts.append(
                        "你的生活背景参考（口味倾向，不是任务清单，"
                        f"不必围绕它选题）：\n{life_extra}"
                    )
            except Exception:
                pass
        return "\n\n".join(parts) if parts else None

    async def _recent_memories(self, k: int = 5) -> list[str]:
        if self._memory_getter is None:
            return []
        try:
            backend = await self._memory_getter()
            rows = await backend.search("", k=k)
            return [
                str(r.get("content", "")).strip()[:80]
                for r in rows
                if str(r.get("content", "")).strip()
            ]
        except Exception:
            return []

    async def _safe_llm(self, prompt: str, system_prompt: str | None) -> str | None:
        try:
            return await self._llm_call(prompt, system_prompt)
        except Exception as e:
            logger.debug(f"[Decider] LLM 调用失败（静默回退）: {e}")
            return None
