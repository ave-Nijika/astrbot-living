"""活动池——五种能力的"闲时用法"（M1 写死在代码里）。

为什么 M1 不做自由涌现：总纲 D1 的"活动=LLM 现场发明"依赖决策 prompt（M2）
与 agent 循环（M3），M1 先用硬编码活动池验证"冲动→活动→记忆→分享"的管道。
活动必须是"有生活气息"的闲时用法，而不是死板轮询。

M3 起活动有两条执行路径（任务书 C1）：
  - agent 模式：LLM 拿着生活工具集（搜索/抓取/沙箱/记忆）自己决定"怎么玩"，
    决策层（decider）决定"玩什么"（intent 里带上 params）；
  - 脚本模式（M1 原路径）：保底，agent 异常/无产出时回退，绝不空转。
每个活动声明 supports_agent；decision.agent_activities 配置在 Loop 层控制
哪些活动实际注入 agent 通道。

每个活动返回 ActivityOutcome；失败直接抛异常，由 LivingLoop 统一兜底
（记 ERROR + 写失败记忆），活动自身不吞异常。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot.api import logger

# 固定候选主题池（M2 起决策 LLM 可用 params.topic 覆盖）
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

    event 是幽灵事件（core/ghost_event.py，M0-R0 结论）：活动直接调用能力
    方法用不到它，agent 模式下由 LivingAgentLoop 自行构造，这里保留作占位。
    params 是决策层给活动的执行参数；agent 是 agent 模式入口
    （async (intent) -> AgentRunResult），None = 本活动走脚本模式。
    """

    searcher: Any
    fetcher: Any
    sandbox: Any
    memory: Any  # MemoryBackend
    gate: Any  # LivingGate（"看评价"空走闸门用）
    event: Any  # 幽灵 AstrMessageEvent
    rng: Any  # random.Random 实例
    now: datetime = field(default_factory=datetime.now)
    params: dict = field(default_factory=dict)
    agent: Any = None

    def date_prefix(self) -> str:
        # 不用 strftime 的 %-m：Windows 平台不支持该转义
        return f"{self.now.month}月{self.now.day}日"

    def pick_topic(self) -> str:
        """主题词来源：决策参数优先，否则固定候选池随机。"""
        topic = (self.params or {}).get("topic")
        return (
            str(topic).strip()
            if topic and str(topic).strip()
            else self.rng.choice(TOPIC_POOL)
        )


@dataclass
class ActivityOutcome:
    """活动产出。summary 是"想说的话"（候选发送），memory_content 是要写的记忆。"""

    name: str
    summary: str | None = None
    memory_content: str | None = None
    importance: float = 0.5
    agent_mode: bool = False


class Activity(ABC):
    """活动基类。name 用于日志与"避免连续两次同活动"；description 给决策
    LLM 的一句话介绍（llm 档要知道"有哪些可选"）。"""

    name: str = "activity"
    description: str = "做一件小事"
    supports_agent: bool = False

    async def run(self, ctx: ActivityContext) -> ActivityOutcome:
        """agent 模式优先（supports_agent 且 Loop 注入了通道）；
        agent 异常/无产出回退脚本模式——保底不空转（任务书 C1）。"""
        if self.supports_agent and getattr(ctx, "agent", None) is not None:
            outcome = await self._try_agent_mode(ctx)
            if outcome is not None:
                return outcome
        return await self._run_script(ctx)

    @abstractmethod
    async def _run_script(self, ctx: ActivityContext) -> ActivityOutcome:
        """M1 脚本模式（保底路径）。"""

    def agent_intent(self, ctx: ActivityContext) -> str:
        """agent 模式的任务描述（子类覆写）；params 是决策层给的偏好。"""
        return self.description

    def _params_hint(self, ctx: ActivityContext, key: str = "topic") -> str:
        value = str((ctx.params or {}).get(key, "") or "").strip()
        return value

    async def _try_agent_mode(self, ctx: ActivityContext) -> ActivityOutcome | None:
        """执行 agent 模式。返回 None = 回退脚本模式。"""
        intent = self.agent_intent(ctx)
        try:
            result = await ctx.agent(intent)
        except Exception as e:
            logger.debug(f"[{self.name}] agent 模式异常，回退脚本模式: {e}")
            return None
        if result is None:
            return None

        text = str(getattr(result, "text", "") or "").strip()
        if getattr(result, "budget_exceeded", False):
            # 超预算中断（任务书 C2）：不回退脚本二次消费（那会重复花钱），
            # 把半程当一段经历记下来——记忆照写，不算 crash
            partial = text[:80] if text else ""
            memory = (
                f"{ctx.date_prefix()}我{self.description}，"
                f"玩到一半被 token 预算叫停了。{partial}"
            )
            logger.info(
                f"[{self.name}] agent 循环触达 token 预算"
                f"（{getattr(result, 'tokens_used', '?')} tokens），按半程经历记录"
            )
            return ActivityOutcome(
                name=self.name,
                summary=partial or "玩到一半被 token 预算叫停",
                memory_content=memory,
                importance=0.3,
                agent_mode=True,
            )

        if not getattr(result, "ok", False) or not text:
            logger.debug(
                f"[{self.name}] agent 无产出（{getattr(result, 'error', '?')}），回退脚本模式"
            )
            return None

        memory = f"{ctx.date_prefix()}我{self.description}，过程里：{text[:80]}"
        if getattr(result, "capped_at_max_steps", False):
            # 自然跑满步数（任务书 C2）：统计进记忆——"这次玩了很久"
            memory += "（这次玩了很久）"
        logger.debug(
            f"[{self.name}] agent 模式完成，"
            f"tokens={getattr(result, 'tokens_used', 0)} steps={getattr(result, 'steps_used', 0)}"
        )
        return ActivityOutcome(
            name=self.name,
            summary=text[:80],
            memory_content=memory,
            importance=0.5,
            agent_mode=True,
        )


class SurfActivity(Activity):
    """冲浪：挑个主题搜一搜，看一眼标题们。"""

    name = "surf"
    description = "上网冲浪：挑个感兴趣的主题搜一搜，看看有什么新东西"
    supports_agent = True

    def agent_intent(self, ctx: ActivityContext) -> str:
        hint = self._params_hint(ctx)
        topic_line = f"主题方向：{hint}。" if hint else "主题你自己挑。"
        return (
            f"你现在打算上网冲浪。{topic_line}"
            "用 web_search 搜一搜，挑一两条结果看看，"
            "最后用几句话汇报你看到了什么、有什么想法。"
        )

    async def _run_script(self, ctx: ActivityContext) -> ActivityOutcome:
        topic = ctx.pick_topic()
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
    """读文章：挑个主题，真的点进去读一篇正文。"""

    name = "read"
    description = "读文章：搜一个主题，挑一条结果认真读正文"
    supports_agent = True

    def agent_intent(self, ctx: ActivityContext) -> str:
        hint = self._params_hint(ctx)
        topic_line = f"主题方向：{hint}。" if hint else "主题你自己挑。"
        return (
            f"你现在打算读一篇文章。{topic_line}"
            "用 web_search 搜索，挑一条你最想读的，用 fetch_page 认真读完，"
            "然后用自己的话总结要点，再说一点你的感想。"
        )

    async def _run_script(self, ctx: ActivityContext) -> ActivityOutcome:
        topic = ctx.pick_topic()
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
    """玩小游戏：脚本模式跑内置模板；agent 模式让 LLM 现场写一个。"""

    name = "game"
    description = "写个小游戏自己玩：写一段秒级小游戏代码丢进沙箱试玩"
    supports_agent = True

    def agent_intent(self, ctx: ActivityContext) -> str:
        hint = self._params_hint(ctx, "style") or self._params_hint(ctx, "topic")
        style_line = f"风格想法：{hint}。" if hint else "玩法你自己发挥。"
        return (
            f"你现在打算写个小游戏自己玩。{style_line}"
            "用 run_python 现场写一个秒级能跑完的小游戏"
            "（只能用 random/math/time/datetime/json/re/itertools/collections，"
            "记得 print 出结果），跑一跑，说说结果和你的心得。"
        )

    def _pick_template(self, ctx: ActivityContext) -> tuple[str, str]:
        """小游戏模板选择：决策参数 style 做模糊匹配，不中则随机。"""
        style = self._params_hint(ctx, "style")
        if style:
            for game_name, code in _GAME_TEMPLATES:
                if style in game_name or game_name in style:
                    return game_name, code
        return ctx.rng.choice(_GAME_TEMPLATES)

    async def _run_script(self, ctx: ActivityContext) -> ActivityOutcome:
        game_name, code = self._pick_template(ctx)
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
    """看评价（弱触发）：只空走消息闸门验证链路，不真正发送。

    这个活动只调 should_send_message 看看"现在能不能说话"，把结果留在
    DEBUG 日志里；真正的发送由 LivingLoop._maybe_share 统一管理。
    保持脚本模式（没有可 agent 化的部分）。
    """

    name = "peek"
    description = "看看有没有人给我留了话（不发言，只是看一眼）"

    async def _run_script(self, ctx: ActivityContext) -> ActivityOutcome:
        allow, reason = await ctx.gate.should_send_message(ctx.now)
        logger.debug(f"[PeekFeedback] 空走消息闸门 allow={allow} reason={reason}")
        return ActivityOutcome(name=self.name)


class MemoryBrowsingActivity(Activity):
    """整理：随机捞一段旧记忆翻一翻，像人翻旧相册。保持脚本模式。"""

    name = "reminisce"
    description = "翻翻自己的旧记忆，回味一下最近经历过的事"

    async def _run_script(self, ctx: ActivityContext) -> ActivityOutcome:
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
    """M3 活动池（顺序即 Pool；选择随机性由 decider/Loop 处理）。"""
    return [
        SurfActivity(),
        ReadArticleActivity(),
        MiniGameActivity(),
        PeekFeedbackActivity(),
        MemoryBrowsingActivity(),
    ]
