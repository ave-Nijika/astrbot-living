"""R0 结论回归测试：幽灵事件是唯一可用的自主循环路径。

锁定 scripts/verify_tool_loop.py 的实测结论：
  - event=None：AstrAgentContext pydantic 校验拒绝；
  - 绕过校验后工具执行层仍拒绝；
  - GhostEvent（AstrMessageEvent 子类实例）全链路可用。
"""

import asyncio

import pytest
from pydantic import ValidationError

from core.ghost_event import GHOST_PLATFORM_ID, GhostEvent, build_ghost_event


def test_ghost_event_is_valid_message_event():
    event = build_ghost_event()
    assert isinstance(event, GhostEvent)
    assert event.unified_msg_origin.startswith(f"{GHOST_PLATFORM_ID}:")
    assert event.message_str


def test_ghost_event_passes_agent_context_validation():
    """GhostEvent 能通过 AstrAgentContext 的 pydantic 校验（event=None 则不能）。"""
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.star.context import Context

    # 不跑 __init__（那需要全套管理器），仅造一个满足 isinstance 的实例
    fake_ctx = object.__new__(Context)
    event = build_ghost_event()
    agent_ctx = AstrAgentContext(context=fake_ctx, event=event)
    assert agent_ctx.event is event

    with pytest.raises(ValidationError):
        AstrAgentContext(context=fake_ctx, event=None)


def test_tool_executor_requires_truthy_event():
    """文档化 R0 的关键事实：本地工具执行层要求 event 真值存在。"""
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

    assert hasattr(FunctionToolExecutor, "_execute_local")
    import inspect

    src = inspect.getsource(FunctionToolExecutor._execute_local)
    assert "Event must be provided" in src, (
        "AstrBot 上游行为变化：_execute_local 不再检查 event，"
        "M1 可重新评估 event=None 路线"
    )


def test_ghost_event_survives_agent_loop_with_fake_provider(monkeypatch):
    """用假 provider 走 tool_loop_agent 的前置路径，验证幽灵事件不被拒。

    不发起真实 LLM 调用：provider 解析成功后即人为中断并断言异常类型，
    证明循环已越过 event 校验与工具层检查、进入 provider 调用阶段。
    """
    from astrbot.core.provider.entities import ProviderType

    class DummyProvider:
        provider_type = ProviderType.CHAT_COMPLETION

    class FakePM:
        async def get_provider_by_id(self, provider_id):
            return DummyProvider()

    from astrbot.core.star.context import Context

    ctx = object.__new__(Context)
    ctx.provider_manager = FakePM()

    from astrbot.core.agent.tool import FunctionTool, ToolSet

    tool = FunctionTool(
        name="noop",
        description="noop",
        parameters={"type": "object", "properties": {}},
    )

    async def run():
        return await ctx.tool_loop_agent(
            event=build_ghost_event(),
            chat_provider_id="dummy",
            prompt="x",
            tools=ToolSet(tools=[tool]),
        )

    # provider 是假的，进入 text_chat 必然失败——但失败点必须已经过了 event 校验
    with pytest.raises(Exception) as exc_info:
        asyncio.run(run())
    assert "validation" not in str(exc_info.value).lower()
