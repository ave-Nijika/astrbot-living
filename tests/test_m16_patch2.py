"""M16-补丁2：落库上限同源 + 去重键 FIFO。

对应任务书 T1-T5（T6 全量基线由本地全量回归保证，不在本文件）。
测试不写 AstrBot 本体数据目录；真实身份一律用先例假号 10001。
"""

import asyncio
import copy
import types

from core.living_loop import LivingLoop

MASTER = "aiocqhttp:FriendMessage:10001"  # 先例假号，非真实身份

BASE_CONFIG = {
    "decision": {"activity_context_write": True},
    "output_gate": {"target_sessions": MASTER},
}


class FakeConvMgr:
    """AstrBot ConversationManager 替身：记录写入对。"""

    def __init__(self):
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return "conv-1"

    async def add_message_pair(self, cid, user, asst):
        self.pairs.append((cid, user, asst))


class FakeLM:
    """livingmemory 会话管理器替身：记录 add_message。"""

    def __init__(self):
        self.added = []

    async def add_message(self, **kwargs):
        self.added.append(kwargs)
        return len(self.added)


def make_loop(config=None, lm=False):
    cfg = copy.deepcopy(BASE_CONFIG)
    if config:
        for group, kv in config.items():
            cfg.setdefault(group, {}).update(kv)
    mgr = FakeConvMgr()
    fake_lm = FakeLM() if lm else None
    loop = LivingLoop(
        gate=types.SimpleNamespace(),
        memory_getter=lambda: asyncio.sleep(0, result=object()),
        config_getter=lambda: cfg, activities=[],
        conversation_manager=mgr,
        lm_conversation_manager_getter=(
            lambda: fake_lm) if fake_lm is not None else None,
    )

    async def _none():
        return None
    loop._bot_identity = _none
    loop._persona_id = _none
    loop._session_id = lambda event: "living_test"
    return loop, mgr, fake_lm


# ---------------------------------------------------------------------------
# T1：长文本不截断——share_max_length=500 时发出多少落多少
# ---------------------------------------------------------------------------
def test_long_text_not_truncated_when_share_max_length_500():
    loop, mgr, lm = make_loop({"output_gate": {"share_max_length": 500}},
                              lm=True)
    text = "键" * 450
    asyncio.run(loop._write_speech_to_stores(text, "#t1", "(分享)"))
    assert len(mgr.pairs) == 1
    assert mgr.pairs[0][2]["content"] == text  # A 落点：完整 450 字
    assert lm.added[0]["content"] == text      # B 落点：同源同长


# ---------------------------------------------------------------------------
# T2：同源生效——500→上限 500；未设置/非法→400（现状保底）
# ---------------------------------------------------------------------------
def test_limit_sources_from_share_max_length():
    loop, _, _ = make_loop({"output_gate": {"share_max_length": 500}})
    assert loop._speech_store_limit() == 500
    loop2, _, _ = make_loop()  # 未设置（默认 120）
    assert loop2._speech_store_limit() == 400
    loop3, _, _ = make_loop({"output_gate": {"share_max_length": "abc"}})
    assert loop3._speech_store_limit() == 400
    loop4, _, _ = make_loop({"output_gate": {"share_max_length": None}})
    assert loop4._speech_store_limit() == 400


def test_unconfigured_limit_keeps_current_400_behavior():
    """未设置 share_max_length → 450 字仍截 400（行为与补丁前一致）。"""
    loop, mgr, _ = make_loop()
    asyncio.run(loop._write_speech_to_stores("字" * 450, "#t2", "(分享)"))
    assert mgr.pairs[0][2]["content"] == "字" * 400


# ---------------------------------------------------------------------------
# T3（核心）：裁剪按插入序 FIFO，不是字典序
# ---------------------------------------------------------------------------
def test_trim_is_fifo_not_lexicographic():
    """130 个混合键族（#share:/#initiative:/#farewell: 轮转）写入 →
    淘汰的是最旧写入的键，与字典序无关。

    反证一：#share:000 是最早写入且字典序最大（原 set+sorted 实现下
    最不容易被裁）——现按"最旧"出局；
    反证二：#farewell:127 是最晚写入且字典序最小（原实现最先被裁）——
    现按"最新"保留。"""
    loop, mgr, _ = make_loop()

    def key(i):
        return {0: f"#share:{i:03d}",
                1: f"#initiative:{i:03d}",
                2: f"#farewell:{i:03d}"}[i % 3]

    for i in range(130):
        asyncio.run(loop._write_speech_to_stores("各不相同的键", key(i), "(t)"))

    # 第 129 次写入（len=129>128）触发裁剪保留 64，再加第 130 次 → 65
    written = list(loop._experience_written)
    assert len(written) == 65
    assert written[0] == key(65) and written[-1] == key(129)  # 插入序保留最近
    assert all(key(i) not in loop._experience_written for i in range(65))
    assert "#share:000" not in loop._experience_written   # 旧的出局（反证一）
    assert "#share:129" in loop._experience_written       # 新的保留
    assert "#farewell:128" in loop._experience_written    # 反证二（128%3==2）
    assert len(mgr.pairs) == 130  # 130 个不同键各写一次（互不吞并）


# ---------------------------------------------------------------------------
# T4：幂等不变——同一键第二次调用不重复写
# ---------------------------------------------------------------------------
def test_same_key_second_call_no_rewrite():
    loop, mgr, _ = make_loop()
    asyncio.run(loop._write_speech_to_stores("内容甲", "#k:1", "(t)"))
    asyncio.run(loop._write_speech_to_stores("内容乙", "#k:1", "(t)"))
    assert len(mgr.pairs) == 1
    assert mgr.pairs[0][2]["content"] == "内容甲"


# ---------------------------------------------------------------------------
# T5：类型与保序语义直接锁定（配套 test_m16_patch1 断言同步）
# ---------------------------------------------------------------------------
def test_experience_written_is_insertion_ordered_dict():
    loop, mgr, _ = make_loop()
    for i in range(3):
        asyncio.run(loop._write_speech_to_stores("c", f"#k:{i}", "(t)"))
    assert list(loop._experience_written) == ["#k:0", "#k:1", "#k:2"]
    assert len(mgr.pairs) == 3
