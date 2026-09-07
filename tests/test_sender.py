"""C5 主动发消息组件测试。

M0 约定：只验证封装逻辑与 message_chain 构造正确性，不真发消息。
"""

import asyncio
import types

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from core.sender import Sender


def test_build_chain_structure():
    """message_chain 构造正确：链上第一个组件是携带原文的 Plain。"""
    chain = Sender.build_chain("你好，我今天学会了一首诗。")
    assert isinstance(chain, MessageChain)
    assert len(chain.chain) == 1
    comp = chain.chain[0]
    assert isinstance(comp, Plain)
    assert comp.text == "你好，我今天学会了一首诗。"


def test_send_invokes_context_api_correctly():
    """send() 应把 unified_msg_origin 字符串与 MessageChain 原样交给 context。"""
    captured = {}

    class FakeContext:
        async def send_message(self, session, message_chain):
            captured["session"] = session
            captured["chain"] = message_chain
            return True

    sender = Sender(FakeContext())
    ok = asyncio.run(sender.send("aiocqhttp:GroupMessage:123456", "晚间汇报"))

    assert ok is True
    assert captured["session"] == "aiocqhttp:GroupMessage:123456"
    assert isinstance(captured["chain"], MessageChain)
    assert captured["chain"].chain[0].text == "晚间汇报"


def test_send_returns_false_when_no_platform():
    """context.send_message 返回 False（无匹配平台）时 send() 返回 False。"""

    class FakeContext:
        async def send_message(self, session, message_chain):
            return False

    sender = Sender(FakeContext())
    assert asyncio.run(sender.send("aiocqhttp:GroupMessage:1", "hi")) is False


def test_send_swallows_exceptions():
    """API 抛异常时 send() 捕获并返回 False，不向上炸。"""

    class BoomContext:
        async def send_message(self, session, message_chain):
            raise RuntimeError("platform gone")

    sender = Sender(BoomContext())
    assert asyncio.run(sender.send("aiocqhttp:GroupMessage:1", "hi")) is False


def test_send_rejects_bad_session_format():
    """session 缺少':'（不可能是 unified_msg_origin）直接 False，不调 API。"""

    class FakeContext:
        def __init__(self):
            self.called = False

        async def send_message(self, session, message_chain):
            self.called = True
            return True

    ctx = FakeContext()
    sender = Sender(ctx)
    assert asyncio.run(sender.send("not-a-umo", "hi")) is False
    assert asyncio.run(sender.send("", "hi")) is False
    assert ctx.called is False
