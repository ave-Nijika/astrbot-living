"""LivingAgentLoop——agent 循环接入 + token 硬闸（任务书 M3-C）。

⚠️ C2 调研结论（2026-09-08，对 v4.27.5 源码核实，为什么这样实现）：
  - 路线 1"BaseAgentRunHooks 抛异常中断"：**不可行**。runner 对四个钩子
    （on_agent_begin/on_tool_start/on_tool_end/on_agent_done）全部 try/except
    包裹（tool_loop_agent_runner.py:796/1191/1313/201），异常只进错误日志。
  - 路线 2"context.tool_loop_agent + 外部计时中断"：拿不到 runner 实例
    （方法内局部变量），无法优雅停止，只能整块 cancel（丢失结算信息）。
  - 路线 3（采用）：**自驱 ToolLoopAgentRunner**——按 context.tool_loop_agent
    的编排方式自己 reset / step_until_done / get_final_llm_resp（调用方式
    参照 AstrBot 源码，未复制其代码）。runner 暴露两个关键能力：
      * `stats.token_usage`（TokenUsage.input_other+input_cached+output）
        每步 LLM 响应后自动累计；
      * `request_stop()` 公开优雅中断（多个检查点响应 abort 信号）。
    `step_until_done` 是异步生成器，每步之间检查累计 token，超预算即
    request_stop()——这就是 token 硬闸，先于任何 agent 活动上线。

这里不用 BaseAgentRunHooks 做闸门，但保留 hooks 参数位（将来如需
"每步回调"语义——如步数遥测——仍走 hooks，它只是不能承担中断）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entities import ProviderRequest

from .ghost_event import build_ghost_event
from .llm_failover import (
    build_provider_chain,
    is_retryable_llm_error,
    looks_like_llm_error_output,
    summarize_provider_error,
)


@dataclass
class AgentRunResult:
    """一次 agent 循环的结算。ok 表示正常产出（不含预算截断）。"""

    ok: bool
    text: str = ""
    tokens_used: int = 0
    budget_exceeded: bool = False
    steps_used: int = 0
    max_steps: int = 0
    error: str | None = None
    tool_traces: list[dict] = field(default_factory=list)

    @property
    def capped_at_max_steps(self) -> bool:
        """自然跑满 max_steps（"这次玩了很久"的记忆素材）。"""
        return self.steps_used >= self.max_steps and not self.budget_exceeded


async def drive_agent_steps(runner: Any, budget: float, max_steps: int) -> tuple[int, bool, int]:
    """驱动 agent 步进，并在每步之间执行 token 硬闸检查（任务书 C2 核心）。

    返回 (yield 次数, 是否触达预算, LLM 调用轮数)。触达预算时调用
    runner.request_stop()——runner 在最近的检查点优雅退出并保留已产出的
    内容与统计。runner 只要求鸭子类型：step_until_done(max_steps) 异步
    生成器、stats.token_usage.total、request_stop()。

    为什么单独数 LLM 轮数：runner 的 yield 对应内部阶段而非 LLM 轮，
    "跑满 max_steps 轮"的判断必须以 usage 增量（每次 LLM 响应都会累计
    token）为准。
    """
    steps = 0
    exceeded = False
    llm_calls = 0
    last_total = 0
    async for _ in runner.step_until_done(max_steps):
        steps += 1
        used = runner.stats.token_usage.total
        if used > last_total:
            llm_calls += 1
            last_total = used
        logger.debug(f"[AgentLoop] step {steps}: 累计 token={used}/{budget}")
        if budget > 0 and used >= budget:
            exceeded = True
            runner.request_stop()
            logger.info(f"[AgentLoop] 触达 token 预算 {used}>={budget}，中断本轮")
    return steps, exceeded, llm_calls


class LivingAgentLoop:
    """受 token 硬闸保护的生活 agent 循环。

    依赖全部注入（context/tools/预算），可独立测试；provider 解析失败
    时 run() 直接返回失败结果，由活动层回退脚本模式。
    """

    def __init__(
        self,
        context: Any,
        config_getter: Callable[[], Any],
        tools: ToolSet,
        persona_getter: Callable[..., Any] | None = None,
        life_extra_getter: Callable[[], str] | None = None,
        mood: Any = None,
    ) -> None:
        self._context = context
        self._config_getter = config_getter
        self._tools = tools
        self._persona_getter = persona_getter
        self._life_extra_getter = life_extra_getter
        self._mood = mood

    # ------------------------------------------------------------------
    async def run(self, intent: str) -> AgentRunResult:
        """按故障转移链执行 agent 循环（任务书 M3-补丁 问题 1）。

        预算语义（红线：token 硬闸不变）：预算是**整个活动的**——链上多次
        尝试的累计消耗一起计数，剩余预算递减传给下一次尝试。这样"换 provider
        重试"不可能变成绕过预算闸的后门。
        """
        decision_cfg = self._group("decision")
        budget = self._number(decision_cfg.get("single_run_token_budget"), 20000)
        max_steps = int(self._number(decision_cfg.get("max_tool_rounds"), 8))
        max_steps = max(1, min(max_steps, 30))

        chain = await build_provider_chain(self._context, self._config_getter)
        if not chain:
            return AgentRunResult(
                ok=False, error="无可用聊天 provider", max_steps=max_steps
            )

        cumulative_tokens = 0
        last_result: AgentRunResult | None = None
        for index, (provider_id, provider) in enumerate(chain):
            remaining = budget - cumulative_tokens if budget > 0 else budget
            result = await self._run_with_provider(
                provider, provider_id, intent, remaining, max_steps
            )
            cumulative_tokens += result.tokens_used
            last_result = result

            # VM 现场形态防御：provider 可能把错误包成"正常文本产出"——
            # 统一归一化为失败，交给下面的切换逻辑
            if result.ok and looks_like_llm_error_output(result.text):
                logger.warning(
                    f"[AgentLoop] provider {provider_id} 返回错误文本，按失败处理"
                )
                result = AgentRunResult(
                    ok=False,
                    text=result.text,
                    tokens_used=result.tokens_used,
                    steps_used=result.steps_used,
                    max_steps=max_steps,
                    error=(
                        f"provider {provider_id} 返回错误文本: "
                        f"{summarize_provider_error(result.text)}"
                    ),
                )

            if result.ok or result.budget_exceeded:
                # 成功收工；或预算被整个活动耗尽——硬闸语义优先，不再换链
                return result

            error_text = result.error or ""
            if index < len(chain) - 1 and is_retryable_llm_error(error_text):
                # 只有"换模型可能有用"的错误才切下一个（任务书错误分类）
                logger.warning(
                    f"[AgentLoop] provider {provider_id} 失败"
                    f"（{summarize_provider_error(error_text)}），切换下一个"
                )
                continue
            # 不可重试（如 401）或已是最后一个：按失败收场
            return result

        return last_result or AgentRunResult(
            ok=False, error="故障转移链异常终止", max_steps=max_steps
        )

    async def _run_with_provider(
        self,
        provider: Any,
        provider_id: str,
        intent: str,
        budget: float,
        max_steps: int,
    ) -> AgentRunResult:
        """用指定 provider 跑一轮完整 agent 循环（单次尝试）。"""
        system_prompt = await self._system_prompt(intent)
        agent_context = AstrAgentContext(context=self._context, event=build_ghost_event())
        request = ProviderRequest(
            prompt=intent,
            func_tool=self._tools,
            system_prompt=system_prompt or "",
        )
        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider=provider,
            request=request,
            run_context=ContextWrapper(
                context=agent_context,
                tool_call_timeout=120,
            ),
            tool_executor=FunctionToolExecutor(),
            agent_hooks=BaseAgentRunHooksShim(),
            streaming=False,
        )

        try:
            steps, budget_exceeded, llm_calls = await drive_agent_steps(
                runner, budget, max_steps
            )
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            logger.warning(f"[AgentLoop] 循环异常（provider={provider_id}）: {error}")
            return AgentRunResult(
                ok=False,
                tokens_used=runner.stats.token_usage.total,
                steps_used=0,
                max_steps=max_steps,
                error=error,
            )

        tokens = runner.stats.token_usage.total
        resp = runner.get_final_llm_resp()
        text = ""
        if resp is not None:
            text = getattr(resp, "completion_text", None) or ""
            if not text:
                try:
                    chain = getattr(resp, "result_chain", None)
                    components = getattr(chain, "chain", None) or []
                    if components:
                        text = getattr(components[0], "text", "") or ""
                except Exception:
                    text = ""

        if budget_exceeded:
            # 超预算中断：不算 crash（记忆照写/心境照记），但 ok=False——
            # 产出可能被掐断在半截，不当作完整成果
            return AgentRunResult(
                ok=False,
                text=text,
                tokens_used=tokens,
                budget_exceeded=True,
                steps_used=llm_calls,
                max_steps=max_steps,
                error=f"token 预算 {budget} 已用尽（实际 {tokens}）",
            )

        if not text.strip():
            # LLM 层失败常以"空产出"形态出现
            return AgentRunResult(
                ok=False,
                text=text,
                tokens_used=tokens,
                steps_used=llm_calls,
                max_steps=max_steps,
                error=f"agent 没有产出文本（provider={provider_id}）",
            )

        if looks_like_llm_error_output(text):
            # VM 实测形态：provider 层把错误包装成"正常文本产出"——
            # 必须按失败处理，交给故障转移链切换（任务书问题 1 现场形态）
            return AgentRunResult(
                ok=False,
                text=text,
                tokens_used=tokens,
                steps_used=llm_calls,
                max_steps=max_steps,
                error=f"provider {provider_id} 返回了错误文本: {text[:160]}",
            )

        logger.debug(f"[LivingLoop] 使用 provider: {provider_id}")
        return AgentRunResult(
            ok=True,
            text=text,
            tokens_used=tokens,
            steps_used=llm_calls,
            max_steps=max_steps,
        )

    async def _system_prompt(self, intent: str) -> str | None:
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
                    parts.append(f"你的生活补充设定：\n{life_extra}")
            except Exception:
                pass
        if self._mood is not None:
            try:
                parts.append(f"你现在的状态：{self._mood.digest()}")
            except Exception:
                pass
        return "\n\n".join(parts) if parts else None

    def _group(self, name: str) -> dict:
        try:
            value = (self._config_getter() or {}).get(name, {})
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _number(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


class BaseAgentRunHooksShim:
    """占位 hooks：当前闸门不依赖钩子（见模块 docstring 调研结论）。

    保留一个行为兼容的对象传给 runner.reset(agent_hooks=...)，避免 runner
    内部对 hooks 属性访问出现意外；生命周期钩子留空。
    """

    async def on_agent_begin(self, run_context) -> None: ...
    async def on_tool_start(self, run_context, tool, tool_args) -> None: ...
    async def on_tool_end(self, run_context, tool, tool_args, tool_result) -> None: ...
    async def on_agent_done(self, run_context, llm_response) -> None: ...
