"""LLM 故障转移：provider 链构建与错误分类（任务书 M3-补丁 问题 1）。

背景：VM 的默认聊天模型 404 失效，agent 活动直接报
"All chat models failed: NotFoundError"。修复思路：插件内按序尝试多个
provider，全部失败才算活动失败。

错误分类原则（任务书定稿）：只有"换个模型可能有用"的错误才切下一个——
404（模型不存在）/ 429（限流）/ 超时 / 连接错误。401（key 无效）这类换
模型也解决不了的错误不重试，直接失败——盲目轮换只会把坏 key 在每个
provider 上都撞一遍，还拖长失败路径。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from astrbot.api import logger

# 可重试（切下一个 provider）的错误特征，按小写子串匹配
_RETRYABLE_PATTERNS = (
    "404",
    "429",
    "notfounderror",
    "not found",
    "rate limit",
    "ratelimit",
    "timeout",
    "timed out",
    "connection",
    "apiconnectionerror",
    "unavailable",
    "503",
    "502",
)

# 明确不可重试的特征（优先级高于可重试：错误文本同时含两类时按不可重试处理）
_NON_RETRYABLE_PATTERNS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "authentication",
    "invalid api key",
    "incorrect api key",
    "400",
    "bad request",
    "invalid_request",
)


def is_retryable_llm_error(error: Any) -> bool:
    """判断一个异常/错误文本是否值得换 provider 重试。

    error 可传异常对象或文本；取 str() 后做小写子串匹配。
    """
    if not error:
        return False
    text = str(error).lower()
    for pattern in _NON_RETRYABLE_PATTERNS:
        if pattern in text:
            return False
    for pattern in _RETRYABLE_PATTERNS:
        if pattern in text:
            return True
    return False


def _provider_id(provider: Any) -> str:
    try:
        return str(provider.meta().id)
    except Exception:
        return f"<provider#{id(provider)}>"


async def build_provider_chain(
    context: Any,
    config_getter: Callable[[], Any],
) -> list[tuple[str, Any]]:
    """构建 provider 尝试顺序：专用 provider → 配置链 → 全部已启用 provider 兜底。

    - model.provider_id：自主活动专用模型（总纲：防烧聊天模型），链首；
    - model.fallback_chain（list）：用户手动指定的尝试顺序；
    - 链尾自动兜底：context.get_all_providers()——AstrBot 该 API 本身只返回
      chat_completion 类型且已启用的 provider（embedding/STT 不在内）；
    - 按 provider id 去重，保留首次出现位置（前面的不再被后面重复）。

    Returns:
        [(provider_id, provider实例), ...]；空列表 = 无可用 provider。
    """
    chain: list[tuple[str, Any]] = []
    seen: set[str] = set()

    def _usable(provider: Any) -> bool:
        return provider is not None and hasattr(provider, "text_chat")

    def _try_append(provider: Any) -> None:
        if not _usable(provider):
            return
        pid = _provider_id(provider)
        if pid in seen:
            return
        seen.add(pid)
        chain.append((pid, provider))

    try:
        model_cfg = (config_getter() or {}).get("model", {})
        manager = getattr(context, "provider_manager", None)

        async def _resolve(pid: str) -> None:
            pid = str(pid).strip()
            if not pid or not manager:
                return
            try:
                provider = await manager.get_provider_by_id(pid)
                _try_append(provider)
            except Exception:
                # 配置里写了不存在的 provider：跳过，不让配置错误阻塞活动
                logger.debug(f"[Failover] 链里的 {pid!r} 无法解析，跳过")

        # 0. 专用 provider（M2 语义保留：链首）
        await _resolve(model_cfg.get("provider_id", ""))

        # 1. 用户配置的故障转移链（热读）
        fallback_ids = model_cfg.get("fallback_chain") or []
        if isinstance(fallback_ids, str):
            fallback_ids = [fallback_ids]
        for pid in fallback_ids:
            await _resolve(pid)
    except Exception as e:
        logger.debug(f"[Failover] 读取模型链配置失败（跳过配置部分）: {e}")

    # 2. 自动兜底：全部已启用 chat provider
    try:
        for provider in context.get_all_providers() or []:
            _try_append(provider)
    except Exception as e:
        logger.debug(f"[Failover] 枚举自动兜底 provider 失败: {e}")

    return chain


def summarize_provider_error(error: Any, limit: int = 160) -> str:
    """错误文本截断（进日志/记忆替换文案用，防长堆栈刷屏）。"""
    text = re.sub(r"\s+", " ", str(error or "")).strip()
    return text[:limit]


# LLM 层故障以"正常产出"形态出现的特征（VM 实测：404 时 agent 拿到的
# 最终文本就是 "All chat models failed: NotFoundError..."）——这种文本
# 一旦当成果写进记忆就会污染记忆库（任务书问题 2）
_LLM_ERROR_OUTPUT_PATTERNS = (
    "all chat models failed",
    "notfounderror",
    "error code: 4",
    "http 5",
    "http 4",
)


def looks_like_llm_error_output(text: Any) -> bool:
    """判断一段"产出"文本是否其实是 LLM 错误信息（任务书问题 2 的过滤依据）。"""
    if not text:
        return False
    lowered = str(text).lower()
    return any(pattern in lowered for pattern in _LLM_ERROR_OUTPUT_PATTERNS)
