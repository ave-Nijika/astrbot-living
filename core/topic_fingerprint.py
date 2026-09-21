"""主题指纹（M5-补丁2 C1）：把话题字符串归一到"主题"。

背景：LLM 产出的同一主题有无数变体（coffee extraction science / DIY home
cold brew optimization / v60 paper filter chemical…），字符串精确匹配的
重复惩罚换一个变体即绕过，表现为"每次都是新话题"，实质同一主题垄断。

设计：纯函数、可测、零依赖。
- 规则表：显式词簇优先（跨首词的家族，如 cold brew / v60 → coffee）；
- 兜底：归一化后取第一个非停用词（同前缀的变体自然同指纹）；
- 中文话题无分词能力，整串即指纹（中文变体少，可接受）。

词表是**可扩充的观察驱动配置**：出现新的垄断家族时在 _RULES 里加一行。
"""

from __future__ import annotations

import re

# 家族规则：指纹名 → 该家族的归一化关键词（子串匹配，命中即归入）
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "coffee",
        (
            "coffee", "cold brew", "coldbrew", "v60", "espresso", "caffeine",
            "latte", "barista", "aeropress", "french press", "drip",
            "咖啡", "手冲", "萃取",
        ),
    ),
    (
        "memory-technique",
        ("memory palace", "memory technique", "mnemonic", "method of loci",
         "记忆宫殿", "记忆术"),
    ),
)

# 兜底指纹提取时的通用停用词（修饰/学术套话——它们不构成主题差异）
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "for", "and", "or", "to",
    "with", "by", "from", "as", "is", "are",
    "home", "diy", "diys", "science", "sciences", "scientific",
    "techniques", "technique", "method", "methods", "guide", "guides",
    "tips", "best", "how", "why", "what", "when", "study", "studies",
    "research", "basics", "basic", "beginner", "beginners", "introduction",
    "intro", "review", "overview", "optimization", "variables", "chemistry",
    "入门", "指南", "技巧", "方法", "优化", "基础", "研究",
})

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


def topic_fingerprint(topic: str) -> str:
    """把话题字符串归一为主题指纹（纯函数）。

    例：'coffee extraction science' / 'DIY home cold brew optimization' /
    'v60 paper filter chemistry' → 都返回 'coffee'。
    无规则命中的话题取第一个非停用词作兜底指纹。
    """
    text = str(topic or "").strip().lower()
    if not text:
        return "unknown"
    text = _PUNCT.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "unknown"
    for fingerprint, keywords in _RULES:
        for keyword in keywords:
            if keyword in text:
                return fingerprint
    words = [w for w in text.split() if w not in _STOPWORDS]
    return words[0] if words else text
