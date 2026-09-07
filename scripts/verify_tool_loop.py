"""M0-R0 风险验证：脱离 message event 自主发起 agent 循环。

任务书 R0（最高优先）：不经过任何消息事件，直接以代码发起一次带工具调用的
LLM agent 循环，验证三件事：
  1. 循环能起
  2. 工具被真实调用
  3. 能拿到最终 LLMResponse

尝试顺序（任务书指定，按序记录每条路结论）：
  A. 直接 event=None 调 tool_loop_agent
  B. 自构 AstrAgentContext(context=ctx, event=None) 传入 agent_context
  B2. 绕过 pydantic dataclass 校验构造 event=None 的 AstrAgentContext
  C. 构造"幽灵 AstrMessageEvent"（仅填必需字段）

运行方式（必须用 AstrBot venv 的 python）：
  cd D:\\sandbox\\astrbot-living
  D:\\astrbot\\AstrBotLauncher-0.3.0\\AstrBotLauncher-0.3.0\\AstrBot\\venv\\Scripts\\python.exe scripts\\verify_tool_loop.py [provider_id]

注意：
- 本脚本只读取 AstrBot 配置，不写其数据库（不调用 db.initialize()）。
- 不启动 dashboard / 平台适配器 / 插件系统，与正在运行的 AstrBot 实例无端口冲突。
- 会真实调用一次 LLM API（产生少量 token 消耗），这是本验证的必要代价。
"""

import asyncio
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# 引导：把 AstrBot 根目录放进 sys.path 并切到该目录（astrbot 以 cwd 解析 data/）
# ---------------------------------------------------------------------------
ASTRBOT_ROOT = Path(r"D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot")
PROVIDER_ID_DEFAULT = "Jasper/glm-5.3-flash"
RESULT_JSON = Path(__file__).parent / "r0_result.json"

sys.path.insert(0, str(ASTRBOT_ROOT))
os.chdir(ASTRBOT_ROOT)
os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))

# 先行导入 astrbot.api 以解开循环导入：
# star.context → kb_mgr → provider.manager → persona_mgr → astrbot.api → star
# astrbot.api 第 4 行先定义 sp，第 8 行才触发 star 链，以它为入口可解。
import astrbot.api  # noqa: F401,E402


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 假工具：掷骰子。记录每次真实调用，用于验证"工具被真实调用"
# ---------------------------------------------------------------------------
DICE_CALLS: list[dict] = []


def make_dice_tool():
    from pydantic import Field
    from pydantic.dataclasses import dataclass as pydantic_dataclass

    from astrbot.core.agent.tool import FunctionTool, ToolExecResult

    @pydantic_dataclass
    class DiceTool(FunctionTool):
        """与 AstrBot 内置工具（BochaWebSearchTool）相同的定义模式。"""

        name: str = "roll_dice"
        description: str = "掷一个六面骰子，返回 1-6 的随机点数。"
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "sides": {
                        "type": "integer",
                        "description": "骰子面数，可选，默认 6。",
                    }
                },
            }
        )

        async def call(self, context, **kwargs) -> ToolExecResult:
            sides = kwargs.get("sides", 6)
            result = random.randint(1, sides)
            DICE_CALLS.append({"sides": sides, "result": result})
            log(f"    [DiceTool] 被真实调用！sides={sides} -> {result}")
            return f"骰子结果: {result} (d{sides})"

    return DiceTool()


# ---------------------------------------------------------------------------
# 构造最小可运行的 Context（组件全部真实，仅不启动生命周期）
# ---------------------------------------------------------------------------
def build_context():
    from asyncio import Queue

    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.cron.manager import CronJobManager
    from astrbot.core.knowledge_base.kb_mgr import KnowledgeBaseManager
    from astrbot.core.persona_mgr import PersonaManager
    from astrbot.core.platform.manager import PlatformManager
    from astrbot.core.platform_message_history_mgr import PlatformMessageHistoryManager
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.star.context import Context
    from astrbot.core.umop_config_router import UmopConfigRouter

    acm = AstrBotConfigManager(astrbot_config, UmopConfigRouter(sp), sp)
    persona_mgr = PersonaManager(db_helper, acm)
    provider_manager = ProviderManager(acm, db_helper, persona_mgr)

    return (
        Context(
            event_queue=Queue(),
            config=astrbot_config,
            db=db_helper,
            provider_manager=provider_manager,
            platform_manager=PlatformManager(astrbot_config, Queue()),
            conversation_manager=ConversationManager(db_helper),
            message_history_manager=PlatformMessageHistoryManager(db_helper),
            persona_manager=persona_mgr,
            astrbot_config_mgr=acm,
            knowledge_base_manager=KnowledgeBaseManager(provider_manager),
            cron_manager=CronJobManager(db_helper),
        ),
        provider_manager,
        astrbot_config,
    )


# ---------------------------------------------------------------------------
# 幽灵事件（路线 C 备用）
# ---------------------------------------------------------------------------
def make_ghost_event():
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    platform_meta = PlatformMetadata(
        name="ghost",
        description="M0 R0 验证用幽灵平台，不存在任何真实连接",
        id="m0_ghost",
    )
    message_obj = AstrBotMessage()
    message_obj.type = MessageType.FRIEND_MESSAGE
    message_obj.self_id = "m0_ghost_bot"
    message_obj.session_id = "m0_verify"
    message_obj.message_id = "m0_ghost_001"
    message_obj.sender = None
    message_obj.message = []
    message_obj.message_str = "(m0 幽灵事件，非真实消息)"
    message_obj.raw_message = None

    class GhostEvent(AstrMessageEvent):
        """仅满足类型校验的最小事件，从不经过任何平台/管道。"""

    return GhostEvent(
        message_str=message_obj.message_str,
        message_obj=message_obj,
        platform_meta=platform_meta,
        session_id="m0_verify",
    )


# ---------------------------------------------------------------------------
# 各尝试路线
# ---------------------------------------------------------------------------
async def attempt_a(ctx, provider_id: str, tools) -> dict:
    """路线 A：直接 event=None。"""
    log("\n[路线 A] tool_loop_agent(event=None, ...) 直接调用")
    resp = await ctx.tool_loop_agent(
        event=None,
        chat_provider_id=provider_id,
        prompt="请调用 roll_dice 工具掷一次骰子，然后把结果告诉我。",
        tools=tools,
        system_prompt="你是测试助手，必须通过工具完成任务。",
        max_steps=5,
    )
    return {"resp": resp}


async def attempt_b(ctx, provider_id: str, tools) -> dict:
    """路线 B：自构 AstrAgentContext(context=ctx, event=None)。"""
    from astrbot.core.astr_agent_context import AstrAgentContext

    log("\n[路线 B] agent_context=AstrAgentContext(context=ctx, event=None)")
    agent_context = AstrAgentContext(context=ctx, event=None)
    resp = await ctx.tool_loop_agent(
        event=None,
        chat_provider_id=provider_id,
        prompt="请调用 roll_dice 工具掷一次骰子，然后把结果告诉我。",
        tools=tools,
        system_prompt="你是测试助手，必须通过工具完成任务。",
        max_steps=5,
        agent_context=agent_context,
    )
    return {"resp": resp, "agent_context": agent_context}


async def attempt_b2(ctx, provider_id: str, tools) -> dict:
    """路线 B2：绕过 pydantic dataclass 初始化校验构造 event=None 的上下文。"""
    from astrbot.core.astr_agent_context import AstrAgentContext

    log("\n[路线 B2] object.__new__ 绕过校验构造 AstrAgentContext(event=None)")
    agent_context = object.__new__(AstrAgentContext)
    agent_context.context = ctx
    agent_context.event = None
    agent_context.extra = {}
    resp = await ctx.tool_loop_agent(
        event=None,
        chat_provider_id=provider_id,
        prompt="请调用 roll_dice 工具掷一次骰子，然后把结果告诉我。",
        tools=tools,
        system_prompt="你是测试助手，必须通过工具完成任务。",
        max_steps=5,
        agent_context=agent_context,
    )
    return {"resp": resp, "agent_context": agent_context}


async def attempt_c(ctx, provider_id: str, tools) -> dict:
    """路线 C：幽灵事件。"""
    from astrbot.core.astr_agent_context import AstrAgentContext

    log("\n[路线 C] 幽灵 AstrMessageEvent（仅填必需字段）")
    ghost = make_ghost_event()
    log(f"    ghost.unified_msg_origin = {ghost.unified_msg_origin!r}")
    agent_context = AstrAgentContext(context=ctx, event=ghost)
    resp = await ctx.tool_loop_agent(
        event=ghost,
        chat_provider_id=provider_id,
        prompt="请调用 roll_dice 工具掷一次骰子，然后把结果告诉我。",
        tools=tools,
        system_prompt="你是测试助手，必须通过工具完成任务。",
        max_steps=5,
        agent_context=agent_context,
    )
    return {"resp": resp, "agent_context": agent_context, "event": ghost}


# ---------------------------------------------------------------------------
async def main() -> int:
    provider_id = sys.argv[1] if len(sys.argv) > 1 else PROVIDER_ID_DEFAULT
    attempts = [
        ("A_event_none", attempt_a),
        ("B_agent_context_none", attempt_b),
        ("B2_agent_context_bypass", attempt_b2),
        ("C_ghost_event", attempt_c),
    ]
    results: list[dict] = []
    winner: dict | None = None

    log("=" * 70)
    log("M0-R0 风险验证：无事件自主 agent 循环")
    log(f"AstrBot 根目录: {ASTRBOT_ROOT}")
    log(f"目标 provider: {provider_id}")
    log("=" * 70)

    ctx, provider_manager, astrbot_config = build_context()
    log("\n[bootstrap] Context 构造成功（真实组件，未启动生命周期）")

    prov_cfg = next(
        (p for p in astrbot_config["provider"] if p.get("id") == provider_id), None
    )
    if prov_cfg is None:
        log(f"[bootstrap] 找不到 provider 配置: {provider_id}")
        return 2
    await provider_manager.load_provider(prov_cfg)
    prov = await provider_manager.get_provider_by_id(provider_id)
    log(f"[bootstrap] provider 已加载: {type(prov).__name__}")
    if prov is None:
        log("[bootstrap] provider 实例为空，终止")
        return 2

    dice = make_dice_tool()
    from astrbot.core.agent.tool import ToolSet

    tools = ToolSet(tools=[dice])

    for name, fn in attempts:
        DICE_CALLS.clear()
        started = time.time()
        record: dict = {"path": name, "provider": provider_id}
        try:
            out = await fn(ctx, provider_id, tools)
            took = time.time() - started
            resp = out["resp"]
            content = getattr(resp, "result_chain", None) or getattr(resp, "content", None)
            record.update(
                {
                    "status": "PASS",
                    "seconds": round(took, 2),
                    "tool_calls": list(DICE_CALLS),
                    "final_text": str(content)[:500] if content else "(空响应)",
                    "resp_type": type(resp).__name__,
                }
            )
            if not DICE_CALLS:
                # 循环能起、LLM 能回，但工具没被真实执行（如 event=None 时
                # FunctionToolExecutor._execute_local 直接拒绝），不算通过。
                record["status"] = "FAIL"
                record["reason"] = (
                    "loop_started_but_tool_not_called: 循环与 LLM 调用正常，"
                    "但本地工具执行层拒绝（event=None），工具从未被真实调用。"
                    f"LLM 收到的错误可从 final_text 看到: {record['final_text'][:200]}"
                )
                log(f"  => 路线 {name} 名义跑通但工具未真实调用，判为失败")
                results.append(record)
                continue
            log(f"  => 路线 {name} 通过！耗时 {took:.1f}s")
            log(f"  => 工具真实调用记录: {DICE_CALLS}")
            log(f"  => 最终 LLMResponse 类型: {type(resp).__name__}")
            log(f"  => 最终文本: {record['final_text']}")
            results.append(record)
            winner = record
            break
        except Exception as e:
            took = time.time() - started
            tb = traceback.format_exc()
            record.update(
                {
                    "status": "FAIL",
                    "seconds": round(took, 2),
                    "error_type": type(e).__name__,
                    "error": str(e)[:500],
                    "traceback": tb[:3000],
                }
            )
            log(f"  => 路线 {name} 失败（{type(e).__name__}: {e}）")
            results.append(record)

    verdict = {
        "r0_passed": winner is not None,
        "winning_path": winner["path"] if winner else None,
        "tool_actually_called": bool(DICE_CALLS),
        "attempts": results,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "astrbot_version": "v4.27.5",
        "python": sys.version,
    }
    RESULT_JSON.write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("\n" + "=" * 70)
    if winner:
        log(f"R0 结论：通过（路线 {winner['path']}），结果已写入 {RESULT_JSON}")
    else:
        log(f"R0 结论：全部路线失败，结果已写入 {RESULT_JSON}")
    log("=" * 70)
    return 0 if winner else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
