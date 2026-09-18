"""ShareRewriter——主动分享角色化改写（任务书 M3 补丁 VIII）。

背景（生产实锤）：agent loop 的产出是"面向工作报告的 LLM 输出"（Markdown
标题/加粗/列表），原样发给主人像在交报告，不像聊天。本模块在发送前做一次
轻量 LLM 改写：把活动汇报转成 1-2 句朋友间随口聊天的口吻。

边界（任务书需求 3）：
- 改写调用是单次轻量调用，不在 agent loop 内，不受 token 硬闸约束；
  频率由每日分享上限（daily_message_limit）自然限流；
- 改写失败/超时/空输出 → 返回 None，调用方降级发送原始汇报；
- 开关关闭（share_rewrite_enabled=false）→ 返回 None 且不调 LLM，
  行为与补丁 VIII 之前完全一致（向后兼容）。
"""

from __future__ import annotations

from typing import Any, Callable

from astrbot.api import logger

# 句末标点（长度截断的句子边界依据，中英皆收）
_SENTENCE_ENDINGS = "。！？!？;；."

DEFAULT_PROMPT = (
    "把下面这份\u201c今天的活动记录\u201d改写成你对主人说的一两句话。要求：\n"
    "- 像朋友间随口聊天，不是汇报；不要标题、不要列表、不要 Markdown 格式\n"
    "- 用你自己的口吻，可以带一点当天心情\n"
    "- 只输出要说的话本身，不要任何前缀和引号"
)


def truncate_at_sentence(text: str, max_length: int) -> str:
    """按句子边界截断到 max_length 以内。

    在上限内找最后一个句末标点，截到标点处（含标点）；整段没有句末
    标点（或标点在首个字符前）就按上限硬截——有内容总比没有强。
    """
    if len(text) <= max_length:
        return text
    window = text[:max_length]
    for i in range(len(window) - 1, -1, -1):
        if window[i] in _SENTENCE_ENDINGS:
            truncated = window[: i + 1].strip()
            if truncated:
                return truncated
    return window.strip()


def _strip_wrapping_quotes(text: str) -> str:
    """剥掉 LLM 爱加的包裹引号（提示词禁止了，但它有时不听话）。"""
    cleaned = text.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'“”‘’「」":
        cleaned = cleaned[1:-1].strip()
    return cleaned


class ShareRewriter:
    """把活动汇报改写成角色口吻的聊天式分享。全程可配置、可失效。"""

    def __init__(
        self,
        llm_call: Callable[..., Any],
        config_getter: Callable[[], Any],
        persona_getter: Callable[..., Any] | None = None,
        life_extra_getter: Callable[..., Any] | None = None,
        mood: Any = None,
    ) -> None:
        self._llm_call = llm_call  # async (prompt, system_prompt) -> str | None
        self._config_getter = config_getter
        self._persona_getter = persona_getter
        self._life_extra_getter = life_extra_getter
        self._mood = mood

    # ------------------------------------------------------------------
    def _group(self, name: str) -> dict:
        try:
            value = (self._config_getter() or {}).get(name, {})
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _enabled(self) -> bool:
        return bool(self._group("output_gate").get("share_rewrite_enabled", True))

    def _prompt_template(self) -> str:
        raw = self._group("output_gate").get("share_rewrite_prompt")
        return str(raw) if raw else DEFAULT_PROMPT

    def _max_length(self) -> int:
        try:
            return max(int(self._group("output_gate").get("share_max_length", 120)), 1)
        except (TypeError, ValueError):
            return 120

    async def _system_prompt(self, mood_digest: str) -> str | None:
        """与 decider._system_prompt 同源的人设拼接（人格一致性），
        额外带心境——改写语气随心情变化。"""
        parts = []
        if self._persona_getter is not None:
            try:
                persona = await self._persona_getter()
                if persona:
                    parts.append(f"你的人格设定：\n{persona}")
            except Exception:
                pass
        if self._life_extra_getter is not None:
            try:
                life_extra = self._life_extra_getter() or ""
                if str(life_extra).strip():
                    # 补丁 XVII L1-a：措辞与 decider._system_prompt 保持一致——
                    # "身份设定"降为"背景参考"，活动执行阶段同样不应被人设绑死方向
                    parts.append(
                        "你的生活背景参考（口味倾向，不是任务清单，"
                        f"不必围绕它选题）：\n{life_extra}"
                    )
            except Exception:
                pass
        if mood_digest:
            parts.append(f"你现在的状态：{mood_digest}")
        return "\n\n".join(parts) if parts else None

    # ------------------------------------------------------------------
    async def rewrite(self, report: str, mood_digest: str = "") -> str | None:
        """把活动汇报改写成聊天式分享。

        Returns:
            改写后的文本；None = 不改写/改写失败（调用方降级发送原文）。
        """
        report = str(report or "").strip()
        if not report:
            return None
        if not self._enabled():
            return None

        template = self._prompt_template()
        prompt = template.replace("{report}", report).replace(
            "{mood}", mood_digest or "心情平静"
        )
        system_prompt = await self._system_prompt(mood_digest)
        try:
            raw = await self._llm_call(prompt, system_prompt)
        except Exception as e:
            logger.warning(f"[ShareRewrite] 改写调用失败，降级原文: {e}")
            return None
        text = _strip_wrapping_quotes(str(raw or "").strip())
        if not text:
            logger.warning("[ShareRewrite] 改写输出为空，降级原文")
            return None

        # LLM 偷懒原样返回汇报：照发不阻塞（任务书需求 3）
        if text == report:
            return report

        max_length = self._max_length()
        if len(text) > max_length:
            text = truncate_at_sentence(text, max_length)
        return text
