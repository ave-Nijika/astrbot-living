"""M3 真实验证：agent 循环（LLM 现场用工具）+ token 硬闸。

任务书 C3 要求的真实验证：让 LLM 拿着生活工具集（web_search/fetch_page/
run_python/remember）真实执行一次活动，并验证 token 预算闸会中断超支循环。

与运行中的 AstrBot 无冲突（最小 Context，不启动 dashboard/平台/插件系统）。
运行（AstrBot venv python）：
  cd D:\\sandbox\\astrbot-living
  D:\\astrbot\\...\\AstrBot\\venv\\Scripts\\python.exe scripts\\verify_agent_loop.py
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ASTRBOT_ROOT = Path(r"D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot")
WORKDIR = Path(__file__).resolve().parents[1]
RESULT_JSON = Path(__file__).parent / "m3_agent_verify.json"

sys.path.insert(0, str(ASTRBOT_ROOT))
sys.path.insert(0, str(WORKDIR))
os.chdir(ASTRBOT_ROOT)
os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))

import astrbot.api  # noqa: F401,E402


def log(msg):
    print(msg, flush=True)


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
    ctx = Context(
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
    )
    return ctx, provider_manager, persona_mgr, astrbot_config


async def main() -> int:
    from core.agent_loop import LivingAgentLoop
    from core.lazy_memory import LazyMemory
    from core.living_tools import build_living_tools
    from core.memory_backend import SimpleBackend
    from core.sandbox import Sandbox
    from core.search import BochaSearcher
    from core.fetcher import WebFetcher

    ctx, provider_manager, persona_mgr, astrbot_config = build_context()
    await ctx.astrbot_config_mgr.initialize()
    for prov_cfg in astrbot_config["provider"]:
        try:
            await provider_manager.load_provider(prov_cfg)
        except Exception as e:
            log(f"  [bootstrap] 跳过 provider {prov_cfg.get('id')}: {e}")
    if provider_manager.provider_insts:
        provider_manager.curr_provider_inst = provider_manager.provider_insts[0]
    await persona_mgr.initialize()

    memory = SimpleBackend(str(WORKDIR / "scripts" / "_verify_agent_memory.db"))
    async def memory_getter():
        return memory

    searcher = BochaSearcher(context=ctx)
    agent_loop = LivingAgentLoop(
        context=ctx,
        config_getter=lambda: {
            "decision": {"single_run_token_budget": 20000, "max_tool_rounds": 8},
            "model": {"provider_id": ""},  # 空 = 默认聊天 provider
        },
        tools=build_living_tools(
            searcher=searcher,
            fetcher=WebFetcher(),
            sandbox=Sandbox(),
            memory_getter=memory_getter,
        ),
        persona_getter=None,  # persona 链路 M2 已验证过，这里省 DB 读
        life_extra_getter=lambda: "对宇宙和咖啡感兴趣",
        mood=None,
    )

    evidence = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": []}

    # ---- 场景 1：正常预算，LLM 现场用工具写个小游戏 ----
    log("[1] agent 模式（预算 20000）：让 LLM 现场写个小游戏并用 run_python 玩一次")
    result = await agent_loop.run(
        "你现在打算写个小游戏自己玩。用 run_python 工具现场写一个猜数字或掷骰子"
        "小游戏（秒级能跑完，print 出结果），跑一次，然后用一句话告诉我结果和你"
        "的心情。"
    )
    log(f"    ok={result.ok} tokens={result.tokens_used} steps={result.steps_used}"
        f"/{result.max_steps} budget_exceeded={result.budget_exceeded}")
    log(f"    text: {result.text[:120]}")
    evidence["runs"].append({
        "scenario": "normal", "ok": result.ok, "tokens": result.tokens_used,
        "steps": result.steps_used, "text": result.text[:300],
        "error": result.error,
    })
    normal_ok = result.ok and result.tokens_used > 0

    # ---- 场景 2：极小预算（300）→ 硬闸应中断 ----
    log("[2] token 硬闸（预算 300）：应快速触闸中断")
    agent_loop_tight = LivingAgentLoop(
        context=ctx,
        config_getter=lambda: {
            "decision": {"single_run_token_budget": 300, "max_tool_rounds": 8},
            "model": {"provider_id": ""},
        },
        tools=build_living_tools(searcher=searcher, memory_getter=memory_getter),
    )
    result2 = await agent_loop_tight.run(
        "用 web_search 搜一搜「独立游戏开发」最近有什么新作品，再读一篇文章。"
    )
    log(f"    ok={result2.ok} tokens={result2.tokens_used} steps={result2.steps_used}"
        f" budget_exceeded={result2.budget_exceeded}")
    evidence["runs"].append({
        "scenario": "tight_budget", "ok": result2.ok, "tokens": result2.tokens_used,
        "steps": result2.steps_used, "budget_exceeded": result2.budget_exceeded,
        "error": result2.error,
    })
    gate_ok = result2.budget_exceeded or result2.tokens_used <= 300

    # remember 工具的写入检查（agent 循环中 LLM 若调用了 remember，记忆里应有）
    rows = await memory.search("", k=5)
    evidence["memory_rows_after"] = len(rows)
    await memory.close()
    await searcher.close()

    evidence["passed"] = bool(normal_ok and gate_ok)
    RESULT_JSON.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("=" * 60)
    log(f"验证结论: {'通过' if evidence['passed'] else '未通过（见 JSON）'}，"
        f"证据已写 {RESULT_JSON}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
