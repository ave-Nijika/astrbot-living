"""M13-补丁1：活动经历进对话上下文（双存储落点）。

A 组：活动结束 → AstrBot 对话上下文（主人当前对话）追加一对 OpenAI 格式
消息：占位 user "(自主活动：{活动名})"（maxlen 50）+ 第一人称自述
assistant（maxlen 400）。写入挂在"活动结束"，与是否分享无关（A6）；
先写后分享（A4）；失败路径 DEBUG 降级不影响分享（A3/D2）。
B 组：同文本写入 livingmemory 会话消息存储（session_id=主人真实 umo，
role=assistant、is_bot_message=True）——MemoryReflection 的总结数据源；
绝不写 ghost 会话（D3）。
C 组：直塞 LivingMemory 的 memory.add 从活动路径消失——活动周期零 add
调用即零 embeddings（D5 可观测断言）。
B5/D4：开关 activity_context_write 关闭 → 两处写入全部跳过。
"""

import asyncio
import copy
from datetime import datetime

from core.activities import ActivityOutcome
from core.living_loop import LivingLoop

NOW = datetime(2026, 9, 29, 15, 0, 0)
# 假号（测试先例 10001），非真实 QQ 号
MASTER_UMO = "aiocqhttp:FriendMessage:10001"

BASE_CONFIG = {
    "decision": {"daily_impulse_limit": 3, "activity_probability": 1.0,
                 "impulse_check_interval_minutes": 5, "max_run_seconds": 300,
                 "decision_mode": "rules"},
    "capabilities": {"cooldown_between_activities_hours": 2.0},
    "sleep": {"sleep_window": "", "fatigue_rate_per_hour": 4.0,
              "dream_probability": 0.0},
    "output_gate": {"daily_message_limit": 10, "message_min_interval_minutes": 30,
                    "target_sessions": MASTER_UMO, "quiet_hours": ""},
}

NARRATION = "9月29日我读了媒介理论的文章，聊到时间循环结构"


class FakeGate:
    def __init__(self, allow_message=True):
        self.allow_message = allow_message
        self.started = 0
        self.finished = 0
        self.message_sends = 0

    async def should_wake(self, now=None, force=False):
        return True, "ok"

    def in_sleep_window(self, now=None):
        return False

    def awake_standby_active(self, now=None):
        return False

    async def consume_standby_expiry(self, now=None):
        return False

    async def should_send_message(self, now=None):
        return self.allow_message, "ok" if self.allow_message else "blocked"

    async def note_activity_started(self, now=None):
        self.started += 1

    async def note_activity_finished(self, now=None):
        self.finished += 1

    async def note_message_sent(self, now=None):
        self.message_sends += 1

    async def close(self):
        pass


class FakeSender:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.sent = []

    async def send(self, session, text):
        self.sent.append((session, text))
        self.events.append(("share", text))
        return True


class ZeroMemory:
    """直塞观测点（D5）：活动路径对它 add 一次都不该发生。"""

    def __init__(self):
        self.added = []

    async def add(self, content, **kwargs):
        self.added.append(content)
        return len(self.added)

    async def search(self, query, k=5):
        return []

    async def close(self):
        pass


class FakeCtxMgr:
    """AstrBot ConversationManager 替身：记录写入对，可注入故障。"""

    def __init__(self, cid="cid-1", has_curr=True, error=None, events=None):
        self.cid = cid
        self.has_curr = has_curr
        self.error = error
        self.pairs = []  # (cid, user_msg_dict, assistant_msg_dict)
        self.new_conversations = []
        self.events = events if events is not None else []

    async def get_curr_conversation_id(self, umo):
        return self.cid if self.has_curr else None

    async def new_conversation(self, umo):
        self.new_conversations.append(umo)
        return self.cid

    async def add_message_pair(self, cid, user_msg, assistant_msg):
        if self.error:
            raise self.error
        self.pairs.append((cid, user_msg, assistant_msg))
        self.events.append(
            ("ctx", user_msg["content"], assistant_msg["content"])
        )


class FakeLmMgr:
    """livingmemory ConversationManager 替身：只记录 add_message。"""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def add_message(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)


class ScriptedActivity:
    def __init__(self, name="surf", outcome=None, error=None):
        self.name = name
        self.description = f"{name} 的描述"
        self.outcome = outcome
        self.error = error
        self.runs = 0

    async def run(self, ctx):
        self.runs += 1
        if self.error:
            raise self.error
        return self.outcome


def _outcome(memory=NARRATION, summary="今天读了媒介理论，聊到时间循环"):
    return ActivityOutcome(name="surf", summary=summary,
                           memory_content=memory, importance=0.5)


_DEFAULT = object()


def make_loop(activity, mgr=_DEFAULT, lm=_DEFAULT, config=None, memory=None,
              gate=None, sender=None, identity=None):
    """构建被测循环；返回 (loop, mgr, lm, sender, memory)。

    mgr/lm 传 None = 未注入（该落点整体跳过）；传可调用 = 直接作为
    livingmemory 管理器 getter（测试探测失败路径）。
    """
    memory = memory if memory is not None else ZeroMemory()
    if mgr is _DEFAULT:
        mgr = FakeCtxMgr()
    if lm is _DEFAULT:
        lm = FakeLmMgr()
    if sender is None:
        sender = FakeSender()
    getter = None
    if lm is not None:
        getter = lm if callable(lm) else (
            lambda: asyncio.sleep(0, result=lm)
        )
    loop = LivingLoop(
        gate=gate or FakeGate(),
        memory_getter=lambda: asyncio.sleep(0, result=memory),
        config_getter=lambda: config if config is not None else BASE_CONFIG,
        activities=[activity],
        sender=sender,
        conversation_manager=mgr,
        lm_conversation_manager_getter=getter,
        bot_identity_getter=(
            lambda: asyncio.sleep(0, result=identity)
        ) if identity else None,
    )
    return loop, mgr, lm, sender, memory


# ---------------------------------------------------------------------------
# A 组：AstrBot 对话上下文写入
# ---------------------------------------------------------------------------
def test_success_experience_pair_written():
    """活动正常结束 → 当前对话末尾追加一对消息（占位 user + 自述 assistant）。"""
    act = ScriptedActivity(outcome=_outcome())
    loop, mgr, lm, sender, memory = make_loop(act)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert len(mgr.pairs) == 1
    cid, user_msg, asst_msg = mgr.pairs[0]
    assert cid == "cid-1"
    assert user_msg == {"role": "user", "content": "(自主活动：surf)"}
    assert asst_msg == {"role": "assistant", "content": NARRATION}
    assert memory.added == []  # C：直塞已移除（同一周期内观测）


def test_narration_truncated_to_400_placeholder_to_50():
    """A2 有界：自述截到 400 字，占位截到 50 字。"""
    act = ScriptedActivity(name="x" * 80,
                           outcome=_outcome(memory="经" * 600))
    loop, mgr, lm, sender, memory = make_loop(act)

    asyncio.run(loop.run_activity_cycle(NOW))
    user_msg = mgr.pairs[0][1]["content"]
    asst_msg = mgr.pairs[0][2]["content"]
    assert len(user_msg) == 50 and user_msg.startswith("(自主活动：")
    assert asst_msg == "经" * 400


def test_no_current_conversation_creates_one():
    """A3：无当前对话 → new_conversation 新建（并把新 id 用于写入）。"""
    act = ScriptedActivity(outcome=_outcome())
    mgr = FakeCtxMgr(cid="cid-new", has_curr=False)
    loop, mgr, lm, sender, memory = make_loop(act, mgr=mgr)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert mgr.new_conversations == [MASTER_UMO]
    assert mgr.pairs[0][0] == "cid-new"


def test_context_write_failure_share_still_sends():
    """D2：add_message_pair 抛异常 → DEBUG 降级，分享照常发送。"""
    act = ScriptedActivity(outcome=_outcome())
    mgr = FakeCtxMgr(error=RuntimeError("db gone"))
    loop, mgr, lm, sender, memory = make_loop(act, mgr=mgr)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert mgr.pairs == []
    assert sender.sent and sender.sent[0][0] == MASTER_UMO


def test_write_happens_before_share():
    """A4：先写上下文后发分享——主人看到分享时上下文已含自述。"""
    act = ScriptedActivity(outcome=_outcome())
    events = []
    mgr = FakeCtxMgr(events=events)
    sender = FakeSender(events=events)
    loop, mgr, lm, sender, memory = make_loop(act, mgr=mgr, sender=sender)

    asyncio.run(loop.run_activity_cycle(NOW))
    kinds = [e[0] for e in events]
    assert "ctx" in kinds and "share" in kinds
    assert kinds.index("ctx") < kinds.index("share")


def test_share_blocked_still_writes_experience():
    """A6 写入与分享解耦：闸门拦下分享，上下文自述照写（无白活动）。"""
    act = ScriptedActivity(outcome=_outcome())
    gate = FakeGate(allow_message=False)
    loop, mgr, lm, sender, memory = make_loop(act, gate=gate)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert sender.sent == []
    assert len(mgr.pairs) == 1
    assert len(lm.calls) == 1


def test_multi_session_uses_first():
    """多目标会话取第一个（与分享主会话同源，M12 先例）。"""
    act = ScriptedActivity(outcome=_outcome())
    config = copy.deepcopy(BASE_CONFIG)
    config["output_gate"]["target_sessions"] = (
        MASTER_UMO + "\naiocqhttp:FriendMessage:10002"
    )
    loop, mgr, lm, sender, memory = make_loop(act, config=config)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert lm.calls[0]["session_id"] == MASTER_UMO


def test_idempotent_same_activity_written_once():
    """A5 幂等：同一活动（id+名）只写一次。"""
    act = ScriptedActivity(outcome=_outcome())
    loop, mgr, lm, sender, memory = make_loop(act)

    asyncio.run(loop._write_activity_experience(act, "自述", "20260929_150000"))
    asyncio.run(loop._write_activity_experience(act, "自述", "20260929_150000"))
    assert len(mgr.pairs) == 1
    assert len(lm.calls) == 1


def test_same_second_different_activities_both_written():
    """同秒两个不同活动是两次经历——幂等键含活动名，不互相顶掉。"""
    first = ScriptedActivity(name="surf", outcome=_outcome())
    second = ScriptedActivity(name="read", outcome=_outcome())
    loop, mgr, lm, sender, memory = make_loop(first)

    asyncio.run(loop.run_activity_cycle(NOW))
    loop._activities = [second]
    asyncio.run(loop.run_activity_cycle(NOW))
    assert len(mgr.pairs) == 2
    assert {p[1]["content"] for p in mgr.pairs} == {
        "(自主活动：surf)", "(自主活动：read)"
    }


# ---------------------------------------------------------------------------
# 失败/半程路径的自述形态（A1：正常 + 中断两种路径都要）
# ---------------------------------------------------------------------------
def test_halfway_memory_narrated_verbatim():
    """token 预算中断的半程经历原样成为自述（不改写、不加料）。"""
    half = "9月29日我surf来着，玩到一半被 token 预算叫停了。时间循环"
    act = ScriptedActivity(outcome=_outcome(memory=half))
    loop, mgr, lm, sender, memory = make_loop(act)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert mgr.pairs[0][2]["content"] == half
    assert lm.calls[0]["content"] == half


def test_model_failure_narration_replaces_error_text():
    """LLM 错误串不进上下文，替换为专属失败文案（原问题 2 语义平移）。"""
    act = ScriptedActivity(outcome=_outcome(
        memory="All chat models failed: NotFoundError", summary="x"
    ))
    loop, mgr, lm, sender, memory = make_loop(act)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is False
    narration = mgr.pairs[0][2]["content"]
    assert "脑子转不动" in narration and "模型全挂了" in narration
    assert "All chat models failed" not in narration


def test_exception_failure_narration_redacts_secrets():
    """异常串带密钥形态信息 → 自述脱敏（独立审计项 4 平移到新落点）。"""
    act = ScriptedActivity(error=RuntimeError(
        "request failed with api key sk-abcdef1234567890 at endpoint"
    ))
    loop, mgr, lm, sender, memory = make_loop(act)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is False
    narration = mgr.pairs[0][2]["content"]
    assert "没成" in narration
    assert "sk-abcdef1234567890" not in narration
    assert "[REDACTED]" in narration


# ---------------------------------------------------------------------------
# B 组：livingmemory 会话消息写入
# ---------------------------------------------------------------------------
def test_lm_message_uses_master_umo_and_assistant_role():
    """D3：session_id=主人真实 umo（绝不 ghost）、role=assistant、
    is_bot_message=True；身份注入时 sender 用 bot 身份。"""
    act = ScriptedActivity(outcome=_outcome())
    identity = {
        "identity_key": "aiocqhttp:10001",
        "sender_id": "10001",
        "platform": "aiocqhttp",
        "display_name": "小凛",
        "aliases": ["小凛"],
        "is_bot": True,
    }
    loop, mgr, lm, sender, memory = make_loop(act, identity=identity)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert len(lm.calls) == 1
    call = lm.calls[0]
    assert call["session_id"] == MASTER_UMO
    assert not call["session_id"].startswith("living_ghost")
    assert call["role"] == "assistant"
    assert call["is_bot_message"] is True
    assert call["content"] == NARRATION
    assert call["sender_id"] == "10001"
    assert call["sender_name"] == "小凛"
    # 与 A 落点同文本（单一事实来源：两处一致，图谱只从 reflection 来）
    assert call["content"] == mgr.pairs[0][2]["content"]


def test_lm_without_identity_defaults_sender_fields():
    """未注入身份 → sender 留空（livingmemory 侧自行回退），平台取 umo 首段。"""
    act = ScriptedActivity(outcome=_outcome())
    loop, mgr, lm, sender, memory = make_loop(act)

    asyncio.run(loop.run_activity_cycle(NOW))
    call = lm.calls[0]
    assert call["sender_id"] is None and call["sender_name"] is None
    assert call["platform"] == "aiocqhttp"


def test_lm_unavailable_skips_only_that_sink():
    """livingmemory 管理器不可用（getter None）→ 只跳过 B 落点，A 照写。"""
    act = ScriptedActivity(outcome=_outcome())
    loop, mgr, lm, sender, memory = make_loop(act, lm=None)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert len(mgr.pairs) == 1
    assert sender.sent


def test_lm_probe_failure_and_add_failure_are_silent():
    """getter 抛异常 / add_message 抛异常 → DEBUG 降级，分享照发。"""
    act = ScriptedActivity(outcome=_outcome())

    def boom_getter():
        raise RuntimeError("plugin not ready")

    loop, mgr, lm, sender, memory = make_loop(act, lm=boom_getter)
    asyncio.run(loop.run_activity_cycle(NOW))
    assert len(mgr.pairs) == 1 and sender.sent

    broken = FakeLmMgr(error=RuntimeError("lm store gone"))
    loop2, mgr2, lm2, sender2, _ = make_loop(act, lm=broken)
    asyncio.run(loop2.run_activity_cycle(NOW))
    assert broken.calls == []
    assert len(mgr2.pairs) == 1 and sender2.sent


# ---------------------------------------------------------------------------
# 开关与直塞移除
# ---------------------------------------------------------------------------
def test_switch_off_skips_both_writes():
    """B5/D4：开关关闭 → 两处写入都不发生，分享照发（回到旧行为）。"""
    act = ScriptedActivity(outcome=_outcome())
    config = copy.deepcopy(BASE_CONFIG)
    config["decision"]["activity_context_write"] = False
    loop, mgr, lm, sender, memory = make_loop(act, config=config)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is True
    assert mgr.pairs == []
    assert lm.calls == []
    assert sender.sent


def test_switch_on_by_default_and_bad_config_falls_open():
    """未配置 → 默认开启；配置读取抛异常 → 回落开启（写不出去顶多少份
    沉淀，不因读取翻脸）。"""
    act = ScriptedActivity(outcome=_outcome())
    loop, mgr, lm, sender, memory = make_loop(act)
    assert loop._experience_write_enabled() is True

    class BrokenConfig:
        def __getitem__(self, key):
            raise RuntimeError("config gone")

    loop2, mgr2, lm2, _, _ = make_loop(act)
    loop2._config_getter = lambda: BrokenConfig()
    assert loop2._experience_write_enabled() is True


def test_zero_direct_memory_adds_in_full_cycle():
    """D5：活动周期对记忆后端零 add 调用（= 零 embeddings 的可观测边界）。"""
    act = ScriptedActivity(outcome=_outcome())
    memory = ZeroMemory()
    loop, mgr, lm, sender, _ = make_loop(act, memory=memory)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert act.runs == 1
    assert memory.added == []
    assert len(mgr.pairs) == 1 and len(lm.calls) == 1


def test_failed_cycle_also_writes_experience():
    """失败活动也有沉淀（A6：不存在白活动）——失败文案进两处落点。"""
    act = ScriptedActivity(error=RuntimeError("搜索挂了"))
    loop, mgr, lm, sender, memory = make_loop(act)

    result = asyncio.run(loop.run_activity_cycle(NOW))
    assert result["ok"] is False
    narration = mgr.pairs[0][2]["content"]
    assert "没成" in narration and "搜索挂了" in narration
    assert lm.calls[0]["content"] == narration


def test_whitespace_narration_writes_nothing():
    """自述剥完空白为空（如 memory_content 全空白）→ 不写空壳占位。
    （正常失败路径有专属文案兜底，只有这种脏输入才会落空。）"""
    bare = ActivityOutcome(name="surf", summary="有摘要", memory_content="   ")
    act = ScriptedActivity(outcome=bare)
    loop, mgr, lm, sender, memory = make_loop(act)

    asyncio.run(loop.run_activity_cycle(NOW))
    assert mgr.pairs == []
    assert lm.calls == []
