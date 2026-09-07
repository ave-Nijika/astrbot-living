"""活动池——五种能力的"闲时用法"（M1 写死在代码里）。

为什么 M1 不做自由涌现：总纲 D1 的"活动=LLM 现场发明"依赖决策 prompt（M2）
与心境（M2/M3），M1 先用硬编码活动池验证"冲动→活动→记忆→分享"的管道本身。
活动必须是"有生活气息"的闲时用法，而不是死板轮询——每个活动都带随机主题、
随机小花样，产出第一人称、带日期感的一句话。

每个活动返回 ActivityOutcome；失败直接抛异常，由 LivingLoop 统一兜底
（记 ERROR + 写失败记忆），活动自身不吞异常。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot.api import logger

# 固定候选主题池（M1 不接 LLM 生成主题，那是 M2 决策层的事）
TOPIC_POOL = [
    "人工智能",
    "独立游戏开发",
    "效率工具",
    "宇宙探索",
    "咖啡",
    "城市摄影",
    "冷知识",
]


@dataclass
class ActivityContext:
    """一次活动的上下文。

    event 是幽灵事件（core/ghost_event.py，M0-R0 结论）：M1 活动直接调用
    能力方法用不到它，但保留在上下文里作为 M2 接入 tool_loop_agent 的
    统一入口——届时 agent 循环必须携带它。
    """

    searcher: Any
    fetcher: Any
    sandbox: Any
    memory: Any  # MemoryBackend
    gate: Any  # LivingGate（"看评价"空走闸门用）
    event: Any  # 幽灵 AstrMessageEvent
    rng: Any  # random.Random 实例
    now: datetime = field(default_factory=datetime.now)

    def date_prefix(self) -> str:
        # 不用 strftime 的 %-m：Windows 平台不支持该转义
        return f"{self.now.month}月{self.now.day}日"


@dataclass
class ActivityOutcome:
    """活动产出。summary 是"想说的话"（候选发送），memory_content 是要写的记忆。"""

    name: str
    summary: str | None = None
    memory_content: str | None = None
    importance: float = 0.5


class Activity(ABC):
    """活动基类。name 用于日志与"避免连续两次同活动"。"""

    name: str = "activity"

    @abstractmethod
    async def run(self, ctx: ActivityContext) -> ActivityOutcome:
        """执行活动。失败抛异常（LivingLoop 统一兜底）。"""


class SurfActivity(Activity):
    """冲浪：随机挑个主题搜一搜，看一眼标题们。"""

    name = "surf"

    async def run(self, ctx: ActivityContext) -> ActivityOutcome:
        topic = ctx.rng.choice(TOPIC_POOL)
        results = await ctx.searcher.search(topic, count=5)
        if not results:
            raise RuntimeError(f"搜「{topic}」没有任何结果")
        titles = [r.get("title", "") for r in results[:3] if r.get("title")]
        first = titles[0] if titles else "一些东西"
        return ActivityOutcome(
            name=self.name,
            summary=f"搜了「{topic}」，看到《{first}》等 {len(results)} 条结果",
            memory_content=(
                f"{ctx.date_prefix()}我搜了「{topic}」，看到《{first}》，"
                f"有点好奇后面讲了什么。"
            ),
        )


class ReadArticleActivity(Activity):
    """读文章：搜个主题，挑一条结果真的点进去读正文。"""

    name = "read"

    async def run(self, ctx: ActivityContext) -> ActivityOutcome:
        topic = ctx.rng.choice(TOPIC_POOL)
        results = await ctx.searcher.search(topic, count=5)
        target = next((r for r in results if r.get("url")), None)
        if target is None:
            raise RuntimeError(f"搜「{topic}」没有可读的链接")
        page = await ctx.fetcher.fetch(target["url"])
        title = page.get("title") or target.get("title") or "一篇无题文章"
        digest = (page.get("text") or "").strip().replace("\n", " ")[:60]
        return ActivityOutcome(
            name=self.name,
            summary=f"读了《{title}》",
            memory_content=(
                f"{ctx.date_prefix()}我读了《{title}》"
                f"（搜「{topic}」找到的），印象最深的是：{digest}……"
            ),
        )


# 小游戏模板：(名字, 源码)。源码只用白名单库（random），保证能过沙箱静态扫描。
_GAME_TEMPLATES: list[tuple[str, str]] = [
    (
        "二分猜数字",
        (
            "import random\n"
            "secret = random.randint(1, 100)\n"
            "lo, hi, tries = 1, 100, 0\n"
            "while lo <= hi:\n"
            "    guess = (lo + hi) // 2\n"
            "    tries += 1\n"
            "    if guess == secret:\n"
            "        break\n"
            "    elif guess < secret:\n"
            "        lo = guess + 1\n"
            "    else:\n"
            "        hi = guess - 1\n"
            "print(f'猜中了！答案是 {secret}，用了 {tries} 次')\n"
        ),
    ),
    (
        "掷骰子统计",
        (
            "import random\n"
            "counts = {}\n"
            "for _ in range(60):\n"
            "    n = random.randint(1, 6)\n"
            "    counts[n] = counts.get(n, 0) + 1\n"
            "best = max(counts, key=counts.get)\n"
            "print(f'掷了 60 次骰子，{best} 点出现最多（{counts[best]} 次）')\n"
        ),
    ),
]


class MiniGameActivity(Activity):
    """玩小游戏：写一段秒级小游戏脚本，丢进沙箱试玩。"""

    name = "game"

    async def run(self, ctx: ActivityContext) -> ActivityOutcome:
        game_name, code = ctx.rng.choice(_GAME_TEMPLATES)
        result = await ctx.sandbox.run(code, timeout=10)
        if not result.get("ok"):
            detail = result.get("refused_reason") or result.get("stderr") or "未知原因"
            raise RuntimeError(f"小游戏「{game_name}」没跑起来: {detail[:120]}")
        first_line = (result.get("stdout") or "").strip().splitlines()
        play_result = first_line[0] if first_line else "没有输出"
        return ActivityOutcome(
            name=self.name,
            summary=f"写了个{game_name}小游戏试玩：{play_result}",
            memory_content=(
                f"{ctx.date_prefix()}我写了个{game_name}的小游戏自己玩，"
                f"结果：{play_result}。"
            ),
        )


class PeekFeedbackActivity(Activity):
    """看评价（弱触发）：M1 只空走消息闸门验证链路，不真正发送。

    为什么存在：任务书要求 M1 验证"闸门放了才发"的链路，但默认不打扰主人——
    所以这个活动只调 should_send_message 看看"现在能不能说话"，把结果留在
    DEBUG 日志里；真正的发送由 LivingLoop._maybe_share 统一管理（同样过闸门）。
    """

    name = "peek"

    async def run(self, ctx: ActivityContext) -> ActivityOutcome:
        allow, reason = await ctx.gate.should_send_message(ctx.now)
        logger.debug(f"[PeekFeedback] 空走消息闸门 allow={allow} reason={reason}")
        # 不产出、不写记忆（任务书 D 表格明确）
        return ActivityOutcome(name=self.name)


class MemoryBrowsingActivity(Activity):
    """整理：随机捞一段旧记忆翻一翻，像人翻旧相册。"""

    name = "reminisce"

    async def run(self, ctx: ActivityContext) -> ActivityOutcome:
        rows = await ctx.memory.search("", k=5)
        if rows:
            picked = ctx.rng.choice(rows)
            snippet = (picked.get("content") or "").strip()[:50]
            return ActivityOutcome(
                name=self.name,
                summary=f"翻了翻记忆，想起：{snippet}",
                memory_content=(
                    f"{ctx.date_prefix()}我翻了翻以前的记忆，"
                    f"翻到一条：{snippet}"
                ),
                importance=0.4,
            )
        return ActivityOutcome(
            name=self.name,
            summary="翻了翻记忆，暂时一片空白",
            memory_content=f"{ctx.date_prefix()}我翻了翻自己的记忆，"
            f"发现还空得很，得多经历点事。",
            importance=0.3,
        )


def default_activities() -> list[Activity]:
    """M1 活动池（顺序即 Pool；选择随机性由 LivingLoop 处理）。"""
    return [
        SurfActivity(),
        ReadArticleActivity(),
        MiniGameActivity(),
        PeekFeedbackActivity(),
        MemoryBrowsingActivity(),
    ]
