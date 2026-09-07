"""C5 主动发消息能力：context.send_message 的薄封装。

已核实（AstrBot v4.27.5 astrbot/core/star/context.py）：
  await context.send_message(session: str | MessageSesion, message_chain) -> bool
  session 传 unified_msg_origin 字符串（"平台:消息类型:会话ID"）即可无事件发送，
  返回值表示是否找到匹配的平台。

M0 约定：只做封装与 message_chain 构造，真发消息属于 M1（避免 M0 乱发）。
"""

from __future__ import annotations

from typing import Any

from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain


class Sender:
    """主动消息发送器。send() 返回 bool（是否成功找到平台并送出）。"""

    def __init__(self, context: Any) -> None:
        self._context = context

    @staticmethod
    def build_chain(text: str) -> MessageChain:
        """把纯文本组标准 MessageChain（便于测试与后续扩展多组件）。"""
        return MessageChain(chain=[Plain(text=text)])

    async def send(self, session: str, text: str) -> bool:
        """向 unified_msg_origin 会话发送一条纯文本消息。

        Returns:
            True=平台匹配且已送出；False=发送失败（异常被吞掉并记日志）。
            注意：该 API 返回 False 也可能意味着"没有匹配平台"，调用方
            无法区分"对方没收到"与"平台不存在"，M1 的闸门层需自行统计。
        """
        if not session or ":" not in session:
            return False
        try:
            chain = self.build_chain(text)
            return bool(await self._context.send_message(session, chain))
        except Exception:
            from astrbot.api import logger

            logger.exception(f"主动发送消息失败: session={session!r}")
            return False
