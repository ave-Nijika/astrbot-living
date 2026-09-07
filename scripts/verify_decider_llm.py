"""M2 真实验证：决策层 LLM 调用端到端（hybrid / llm 两档 + persona + 心境）。

M0 的 verify_tool_loop.py 证明了"无事件 agent 循环"可行；本脚本证明 M2 的
"决策 LLM 调用"在真实 AstrBot 组件上可行：
  1. plugin._decision_llm_call —— 真实 context.llm_generate（provider 回退路径）
  2. plugin._persona_prompt   —— 真实 persona_manager.get_default_persona_v3
  3. hybrid 档决策            —— 真实 LLM 生成活动参数
  4. llm 档决策               —— 真实 LLM 全权选活动
  5. 用决策出的参数真实跑一次活动（博查搜索）

与运行中的 AstrBot 无冲突：不启动 dashboard/平台/插件系统，不写其数据库
（persona 初始化只读）。

运行（AstrBot venv python）：
  cd D:\\sandbox\\astrbot-living
  D:\\astrbot\\...\\AstrBot\\venv\\Scripts\\python.exe scripts\\verify_decider_llm.py
"""

import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ASTRBOT_ROOT = Path(r"D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot")
WORKDIR = Path(__file__).resolve().parents[1]
RESULT_JSON = Path(__file__).parent / "m2_llm_verify.json"

sys.path.insert(0, str(ASTRBOT_ROOT))
sys.path.insert(0, str(WORKDIR))
os.chdir(ASTRBOT_ROOT)
os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))

import astrbot.api  # noqa: F401,E402  先行导入解开循环导入（M0 经验）


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 最小真实 Context（与 M0 verify_tool_loop.py 同法）
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
    # acm.initialize() 在 main() 里调（本函数是同步的）
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
    from core.activities import ActivityContext, default_activities
    from core.decider import ActivityDecider
    from core.ghost_event import build_ghost_event
    from core.mood import MoodState
    from core.sandbox import Sandbox
    from core.search import BochaSearcher
    from core.memory_backend import SimpleBackend

    import importlib
    import types

    pkg_name = "living_plugin_under_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(WORKDIR)]
    sys.modules[pkg_name] = pkg
    main_module = importlib.import_module(f"{pkg_name}.main")

    evidence = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}

    ctx, provider_manager, persona_mgr, astrbot_config = build_context()
    await ctx.astrbot_config_mgr.initialize()  # 只读（幂等，get_conf 前置要求）
    # 加载全部 provider 并设默认（模拟生产状态，读配置无网络）
    for prov_cfg in astrbot_config["provider"]:
        try:
            await provider_manager.load_provider(prov_cfg)
        except Exception as e:
            log(f"  [bootstrap] 跳过 provider {prov_cfg.get('id')}: {e}")
    if provider_manager.provider_insts:
        provider_manager.curr_provider_inst = provider_manager.provider_insts[0]
    await persona_mgr.initialize()  # 只读 DB：装载真实 persona 列表

    # 走生产代码路径：直接用 main.py 里的真实方法（合成包加载）
    plugin = object.__new__(main_module.LivingPlugin)
    plugin.context = ctx
    plugin.config = {
        "model": {"provider_id": ""},  # 空 = 走默认聊天 provider 回退路径
        "decision": {"decision_mode": "hybrid"},
        "persona": {"life_extra": "# 生活补充设定\n- 深夜有精神，喜欢宇宙和咖啡"},
    }

    ghost_umo = build_ghost_event().unified_msg_origin
    provider_id = await ctx.get_current_chat_provider_id(ghost_umo)
    log(f"[1] provider 回退路径解析 -> {provider_id}")
    evidence["steps"].append({"step": "provider_fallback", "provider_id": provider_id})

    persona = await plugin._persona_prompt()
    persona_head = (persona or "")[:60]
    log(f"[2] 真实 persona 读取 -> {'命中(' + persona_head + '...)' if persona else '未命中(None)'}")
    evidence["steps"].append({"step": "persona", "found": bool(persona), "head": persona_head})

    mood = MoodState(db_path=str(WORKDIR / "scripts" / "_verify_mood.db"))
    await mood.load()
    mood.bump_interest("宇宙探索", 0.8)
    mood.bump_interest("咖啡", 0.6)
    mood.energy = 0.35
    log(f"[3] 心境摘要 -> {mood.digest()}")

    memory = SimpleBackend(str(WORKDIR / "scripts" / "_verify_memory.db"))
    for seed_text in (
        "9月7日我搜了「宇宙探索」，看到一篇讲系外行星的。",
        "9月6日我读了《咖啡的世界》，讲了烘焙曲线。",
        "9月5日我写了个猜数字小游戏，两轮就猜中了。",
    ):
        await memory.add(seed_text, 0.5)

    async def memory_getter():
        return memory

    decider = ActivityDecider(
        activities=default_activities(),
        config_getter=lambda: plugin.config,
        llm_call=plugin._decision_llm_call,
        mood=mood,
        persona_getter=plugin._persona_prompt,
        life_extra_getter=lambda: str(plugin._cfg("persona", "life_extra", "") or ""),
        memory_getter=memory_getter,
    )

    log("[4] hybrid 档真实决策（LLM 出活动参数）...")
    # rules 可能选中不可参数化的活动（peek/reminisce），重试几次确保
    # 验证到"LLM 出参数"这条链路本身
    d_hybrid = None
    for _ in range(8):
        d_hybrid = await decider.decide()
        if d_hybrid.activity.name in ("surf", "read", "game"):
            break
    log(f"    -> mode={d_hybrid.mode} activity={d_hybrid.activity.name} "
        f"params={d_hybrid.params} note={d_hybrid.note}")
    evidence["steps"].append({
        "step": "hybrid_decision", "mode": d_hybrid.mode,
        "activity": d_hybrid.activity.name, "params": d_hybrid.params,
    })

    plugin.config["decision"]["decision_mode"] = "llm"
    log("[5] llm 档真实决策（LLM 全权选活动）...")
    d_llm = await decider.decide()
    log(f"    -> mode={d_llm.mode} activity={d_llm.activity.name} "
        f"params={d_llm.params} note={d_llm.note}")
    evidence["steps"].append({
        "step": "llm_decision", "mode": d_llm.mode,
        "activity": d_llm.activity.name, "params": d_llm.params,
    })

    plugin.config["decision"]["decision_mode"] = "hybrid"

    log("[6] 用 hybrid 决策参数真实跑一次活动...")
    decision = d_hybrid if d_hybrid.activity.name in ("surf", "read", "game") else d_llm
    from core.fetcher import WebFetcher

    searcher = BochaSearcher(api_key=None, context=ctx)
    fetcher = WebFetcher()
    outcome = None
    error = None
    try:
        act_ctx = ActivityContext(
            searcher=searcher,
            fetcher=fetcher,
            sandbox=Sandbox(),
            memory=memory,
            gate=None,
            event=build_ghost_event(),
            rng=random.Random(),
            now=datetime.now(),
            params=decision.params,
        )
        # gate 只被 peek 用；peek 无产出，注入一个最小 gate 以防万一
        if decision.activity.name == "peek":
            class _G:
                async def should_send_message(self, now=None):
                    return False, "quiet_hours"
            act_ctx.gate = _G()
        outcome = await decision.activity.run(act_ctx)
        log(f"    -> summary: {outcome.summary}")
        log(f"    -> memory : {outcome.memory_content}")
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log(f"    -> 活动失败（回退路径，不算验证失败）: {error}")
    finally:
        await searcher.close()
        await fetcher.close()
    evidence["steps"].append({
        "step": "real_activity", "activity": decision.activity.name,
        "summary": outcome.summary if outcome else None,
        "memory": outcome.memory_content if outcome else None,
        "error": error,
    })

    await memory.close()
    await mood.close()

    evidence["passed"] = all([
        bool(provider_id),
        d_hybrid.mode == "hybrid" and bool(d_hybrid.params),
        d_llm.activity.name in {a.name for a in default_activities()},
        outcome is not None,
    ])
    RESULT_JSON.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("=" * 60)
    log(f"验证结论: {'通过' if evidence['passed'] else '部分通过（见 JSON）'}，"
        f"证据已写 {RESULT_JSON}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
