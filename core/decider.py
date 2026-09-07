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
            cfg = self._config_getter() or {}
            mode = str((cfg.get("decision") or {}).get("decision_mode", "hybrid"))
            return mode if mode in VALID_MODES else "hybrid"
        except Exception:
            return "hybrid"

    # ------------------------------------------------------------------
    # rules 档
    # ------------------------------------------------------------------
    def rules_pick(self) -> Activity:
        """加权随机 + 避免连续重复。心境只是"倾向"，不是"规则"——权重×2
        足够让选择有性格，又不至于变成可预测的循环。"""
        pool = [
            a for a in self._activities if a.name != self._last_name
        ] or self._activities
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
    # hybrid 档
    # ------------------------------------------------------------------
    async def _params_for(self, activity: Activity) -> dict:
        if activity.name not in PARAMETERIZABLE or self._llm_call is None:
            return {}
        mood_block = self._mood.digest() if self._mood is not None else "心情平静，精力一般"
        if activity.name == "game":
            prompt = (
                f"你现在打算写个小游戏自己玩。你现在的状态：{mood_block}。\n"
                '顺着状态选一个具体的小游戏风格。只输出 JSON，格式：{"style": "…"}'
            )
        else:
            action = "上网冲浪（搜索）" if activity.name == "surf" else "读一篇文章"
            prompt = (
                f"你现在打算{action}。你现在的状态：{mood_block}。\n"
                "顺着状态选一个具体、有生活气息的主题词。"
                '只输出 JSON，格式：{"topic": "…"}'
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
        return params

    # ------------------------------------------------------------------
    # llm 档
    # ------------------------------------------------------------------
    async def _llm_decide(self) -> Decision | None:
        if self._llm_call is None:
            return None
        memories = await self._recent_memories()
        activity_lines = "\n".join(
            f"- {a.name}: {a.description}" for a in self._activities
        )
        memory_block = "\n".join(f"- {m}" for m in memories) if memories else "（还没什么记忆）"
        mood_block = self._mood.digest() if self._mood is not None else "心情平静，精力一般"
        prompt = (
            "现在是你的独处时间，没有人在找你，可以自己决定干点什么。\n\n"
            f"你现在的状态：{mood_block}\n\n"
            f"最近记得的事：\n{memory_block}\n\n"
            f"可以做的活动：\n{activity_lines}\n\n"
            "请选一个你现在最想做的活动，并给它合适参数（topic 为主题词，"
            'style 为小游戏风格，peek 和 reminisce 不需要参数）。\n'
            '只输出 JSON，格式：{"activity": "…", "params": {"topic": "…"}}'
        )
        system_prompt = await self._system_prompt()
        raw = await self._safe_llm(prompt, system_prompt)
        data = extract_json_object(raw)
        if not data:
            return None
        name = str(data.get("activity", "")).strip()
        activity = next((a for a in self._activities if a.name == name), None)
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
                    parts.append(f"你的生活补充设定：\n{life_extra}")
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
