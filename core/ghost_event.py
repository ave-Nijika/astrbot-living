"""幽灵事件构造：R0 风险验证（2026-09-07）确定的生产路径。

R0 结论（详见 docs/m0_report.md 与 scripts/r0_result.json）：
  - tool_loop_agent(event=None) 被 AstrAgentContext 的 pydantic 校验拒绝；
  - 绕过校验（object.__new__）后，FunctionToolExecutor._execute_local 仍会
    抛 "Event must be provided for local function tools"，本地工具无法执行；
  - 唯一全链路可行的方案：构造一个仅满足类型校验的 AstrMessageEvent 子类
    实例（"幽灵事件"）。它不来自任何平台、不进入消息管道。
"""

from __future__ import annotations

from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

GHOST_PLATFORM_ID = "living_ghost"
"""幽灵平台 id。出现在 unified_msg_origin 首段即代表自主活动来源。"""


class GhostEvent(AstrMessageEvent):
    """仅满足 AstrAgentContext 类型校验的最小事件。"""


def build_ghost_event(session_id: str = "living_autonomous") -> AstrMessageEvent:
    """构造自主活动用的幽灵事件。

    Args:
        session_id: 逻辑会话名（用于日志辨识，非真实会话）。
    """
    platform_meta = PlatformMetadata(
        name="living",
        description="astrbot-living 自主活动幽灵平台（非真实连接）",
        id=GHOST_PLATFORM_ID,
    )
    message_obj = AstrBotMessage()
    message_obj.type = MessageType.FRIEND_MESSAGE
    message_obj.self_id = f"{GHOST_PLATFORM_ID}_bot"
    message_obj.session_id = session_id
    message_obj.message_id = f"living_{session_id}"
    message_obj.sender = None
    message_obj.message = []
    message_obj.message_str = "(astrbot-living 自主活动，非用户消息)"
    message_obj.raw_message = None

    return GhostEvent(
        message_str=message_obj.message_str,
        message_obj=message_obj,
        platform_meta=platform_meta,
        session_id=session_id,
    )
