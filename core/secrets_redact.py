"""敏感信息脱敏（任务书 M3 补丁 IV 独立审计项 4）。

为什么需要：记忆库的内容会进入后续决策 prompt——一旦错误详情里的
API key / 连接串被写进记忆，泄漏就变成"长期记忆"，每次决策都可能
被复述出去。写入口统一脱敏比在各处小心谨慎可靠得多。

模式覆盖（小写不敏感、按子串/正则）：
- sk- 开头的密钥形态（OpenAI/DeepSeek/自建网关等通用）
- Bearer <token>
- api_key=<...> / apikey=<...> / key=<...> 查询串
- 长十六进制串（32/64 位，常见于 token/secret）
"""

from __future__ import annotations

import re

# 各类密钥形态的正则（按优先级依次替换）
_REDACT_RULES = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[=:]\s*[\"']?[A-Za-z0-9._-]{8,}"),
    re.compile(r"\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{64}\b"),
)

_REPLACEMENT = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """脱敏一段即将写入记忆/日志的文本。"""
    if not text:
        return text
    result = text
    for rule in _REDACT_RULES:
        result = rule.sub(_REPLACEMENT, result)
    return result
