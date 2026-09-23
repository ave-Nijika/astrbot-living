"""历史污染数据自愈（任务书 M3 补丁 VI 需求 2）+ 存量身份回填（M9-补丁3）。

背景：补丁 IV 时期的 _bot_identity 提取过 default:hash 污染身份并随活动
记忆入库（凛 SQL 查实 2 个污染 person 节点、12 条记忆）。本模块在插件
启动后一次性扫描自身记忆，把 participant_identities 里的污染身份替换为
修正身份（cron:{dashboard_username} 桥接身份），并触发 LivingMemory
的图谱重建。

安全边界（任务书硬性要求）：
- 只处理 participant_identities 里含 default: 污染身份的记忆——该身份
  只有 living 直写记忆才可能有，原生记忆（aiocqhttp:/cron: 开头）天然
  不命中，绝不触碰；
- 幂等：已处理的记忆 id 记录在 selfheal_state.json，重复启动不重复处理；
- LivingMemory 数据重建以 LivingMemory 自己的机制为准，living 只负责
  metadata 修正与重建触发。

引擎接口不确定性处理（LivingMemory 是外部插件，签名可能随版本变化）：
修正落库按可用性依次尝试三条路径——
  1. engine.update_memory / update_metadata（直接改主表 metadata）
  2. engine.graph_memory_manager.index_memory（以新 metadata 重建图谱条目）
  3. engine.delete_memory + add_memory（删除重写，最后手段）
实际可用者记录在返回值与日志里。

M9-补丁3 新增 run_ghost_identity_backfill：身份可用时扫描 living 直写
（session_id 以 living_ghost 开头）但 participant_identities 为空的存量
记忆并回填 bot 身份。与污染自愈的记账差异：回填**不用** state 文件按
id 记账——主人清空记忆库后 documents id 从 1 重排，按 id 记账会把新
记忆误判为已处理；"已有身份即跳过"的扫描条件本身天然幂等（2026-09-23
对 LivingMemory 源码核实：engine.update_memory(id, updates) 的
updates={"metadata": ...} 走三库同步，且 metadata 键触及
participant_identities 时自动触发 graph_memory_manager.index_memory
图谱重建——身份回填与图谱联动单次调用完成）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

# probe 词与提取链（main._bot_identity）保持一致——霸榜的记忆也要扫到
PROBE_WORDS = ("我", "今天", "记忆", "文章", "冲浪", "的")
PROBE_K = 20
POLLUTED_PREFIX = "default:"


def _is_polluted(identity_key: Any) -> bool:
    return str(identity_key or "").startswith(POLLUTED_PREFIX)


async def scan_polluted_memories(
    backend: Any,
    probe_words: tuple[str, ...] = PROBE_WORDS,
    k: int = PROBE_K,
) -> list[dict]:
    """扫描自身可查询的记忆，返回携带污染身份的条目。

    判定依据：participant_identities 里存在 default: 前缀身份。该身份
    只有 living 直写记忆才可能有——这个过滤同时就是"原生记忆绝不触碰"
    的保证（原生记忆的身份是 aiocqhttp:/cron: 开头，天然不命中）。
    """
    seen: set[Any] = set()
    found: list[dict] = []
    for word in probe_words:
        try:
            rows = await backend.search(word, k=k)
        except Exception as e:
            logger.debug(f"[SelfHeal] probe {word!r} 搜索失败（跳过）: {e}")
            continue
        for row in rows or []:
            rid = row.get("id")
            if rid is None or rid in seen:
                continue
            seen.add(rid)
            metadata = row.get("metadata") or {}
            participants = metadata.get("participant_identities") or []
            if any(_is_polluted(p.get("identity_key")) for p in participants):
                found.append(
                    {
                        "id": rid,
                        "content": str(row.get("content", "")),
                        "metadata": metadata,
                    }
                )
    return found


def build_corrected_identity(bot_identity: dict) -> dict:
    """把需求 1 提取的 bot 身份补成修正条目的完整结构（任务书示例带 aliases）。"""
    corrected = dict(bot_identity or {})
    display = str(corrected.get("display_name") or "astrbot")
    corrected["display_name"] = display
    corrected.setdefault("aliases", [display])
    corrected["is_bot"] = True
    return corrected


def corrected_metadata(metadata: dict, corrected_identity: dict) -> dict:
    """污染 metadata 的修正版：participant_identities 整体替换（结构保持）。"""
    new_metadata = dict(metadata or {})
    new_metadata["participant_identities"] = [dict(corrected_identity)]
    return new_metadata


async def _try_call(engine: Any, names: tuple[str, ...], *args, **kwargs) -> bool:
    """防御式调用引擎方法：按名字依次探测，可用即调用并返回 True。

    兼容 sync/async 两种实现；全部不可用返回 False。
    """
    for name in names:
        method = getattr(engine, name, None)
        if not callable(method):
            continue
        try:
            result = method(*args, **kwargs)
            if asyncio.iscoroutine(result):
                await result
            return True
        except TypeError:
            # 签名不匹配（参数形态不同）：试下一个名字
            continue
        except Exception as e:
            # 接口存在但执行失败：如实上抛给上层记录
            logger.warning(f"[SelfHeal] 引擎 {name} 调用失败: {e}")
            raise
    return False


async def _reapply_memory(engine: Any, item: dict, new_metadata: dict) -> str | None:
    """修正落库：按可用性依次尝试三条路径，返回实际采用的路径名。"""
    # 路径 1：直接更新主表 metadata
    if await _try_call(
        engine, ("update_memory", "update_metadata"), item["id"], metadata=new_metadata
    ):
        return "update_memory"

    # 路径 2：graph_memory_manager.index_memory——以新 metadata 重建图谱条目
    gmm = getattr(engine, "graph_memory_manager", None)
    if gmm is not None and callable(getattr(gmm, "index_memory", None)):
        result = gmm.index_memory(item["id"], item["content"], new_metadata)
        if asyncio.iscoroutine(result):
            await result
        return "index_memory"

    # 路径 3：删除重写（最后手段：id 会变，图谱条目靠 add 后的提取器重建）
    deleted = await _try_call(engine, ("delete_memory", "remove_memory"), item["id"])
    if deleted:
        await engine.add_memory(
            item["content"],
            importance=0.5,
            metadata=new_metadata,
            session_id=None,
            persona_id=None,
        )
        return "delete_and_readd"
    return None


async def run_identity_selfheal(
    backend: Any,
    corrected_identity: dict,
    state_path: str | Path,
    engine: Any = None,
    probe_words: tuple[str, ...] = PROBE_WORDS,
) -> dict:
    """启动时一次性自愈：检测 → 修正 → 触发重建 → 幂等记账。

    Args:
        backend: MemoryBackend（search 用于扫描；engine 走 backend.engine）。
        corrected_identity: 修正身份（需求 1 提取产物）。
        state_path: 幂等状态文件路径。
        engine: 引擎实例（不传则从 backend.engine 属性取）。

    Returns:
        {"found": 命中数, "fixed": 修正数, "skipped": 已处理跳过数,
         "paths": 实际采用的落库路径集合}
    """
    if engine is None:
        engine = getattr(backend, "engine", None)

    state = load_state(state_path)
    healed: dict[str, str] = state.get("healed", {})

    polluted = await scan_polluted_memories(backend, probe_words)
    fixed = 0
    skipped = 0
    paths: set[str] = set()

    for item in polluted:
        rid = str(item["id"])
        if rid in healed:
            skipped += 1
            continue
        new_metadata = corrected_metadata(item["metadata"], corrected_identity)
        try:
            path = await _reapply_memory(engine, item, new_metadata)
        except Exception as e:
            logger.warning(
                f"[SelfHeal] 记忆 {rid} 修正失败（跳过，下次启动重试）: {e}"
            )
            continue
        if path is None:
            logger.warning(
                f"[SelfHeal] 记忆 {rid} 无可用修正接口"
                "（update/index/delete 均不可用），跳过"
            )
            continue
        paths.add(path)
        healed[rid] = datetime.now().isoformat(timespec="seconds")
        fixed += 1

    state["healed"] = healed
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state_path, state)

    summary = {
        "found": len(polluted),
        "fixed": fixed,
        "skipped": skipped,
        "paths": sorted(paths),
    }
    logger.info(
        f"[SelfHeal] 身份自愈完成：命中 {summary['found']} 条，"
        f"修正 {fixed} 条（路径 {summary['paths'] or '无'}），"
        f"已处理跳过 {skipped} 条"
    )
    return summary


def load_state(state_path: str | Path) -> dict:
    path = Path(state_path)
    if not path.exists():
        return {"healed": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {"healed": {}}
    except (ValueError, OSError):
        return {"healed": {}}


# ---------------------------------------------------------------------------
# M9-补丁3：存量身份回填（living 直写且无参与者身份的记忆并回主图谱）
# ---------------------------------------------------------------------------

GHOST_SESSION_PREFIX = "living_ghost"
"""living 直写记忆的会话前缀（幽灵 uwo 首段 = platform_meta.id）。"""


def _parse_metadata(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def scan_ghost_memories_without_identity(engine: Any) -> list[dict]:
    """扫描 living 直写但 participant_identities 为空的存量记忆。

    扫描条件（任务书 B1/B4，三者同时满足才命中）：
      - metadata.session_id 以 living_ghost 开头——living 自主活动直写的
        会话（原生对话记忆是 aiocqhttp: 等真实平台会话，天然不命中）；
      - participant_identities 为空（已有身份的记忆不动——这同时是幂等
        机制：回填成功后下次扫描不再命中）；
      - 正文非空（跳过占位/测试残留）。

    实现说明：LivingMemory 无"按前缀列会话"的公开 API（get_session_memories
    只接受精确 session_id），与它自身的会话查询同款走 documents 表的
    json_extract（schema 依据 memory_engine_crud.get_session_memories 源码
    核实）；SQL 或表结构变化时返回空列表并 WARNING，由调用方记录。
    """
    conn = getattr(engine, "db_connection", None)
    if conn is None:
        logger.warning(
            "[SelfHeal] 存量回填：引擎无 db_connection（接口变化？），跳过扫描"
        )
        return []
    try:
        cursor = await conn.execute(
            """
            SELECT id, text, metadata
            FROM documents
            WHERE json_extract(metadata, '$.session_id') LIKE ?
            ORDER BY id
            """,
            (GHOST_SESSION_PREFIX + "%",),
        )
        rows = await cursor.fetchall()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[SelfHeal] 存量回填扫描失败（跳过）: {e}")
        return []

    found: list[dict] = []
    for row in rows:
        metadata = _parse_metadata(row["metadata"])
        if metadata.get("participant_identities"):
            continue  # 已有身份：幂等跳过
        content = str(row["text"] or "")
        if not content.strip():
            continue  # 占位/测试残留
        found.append(
            {"id": int(row["id"]), "content": content, "metadata": metadata}
        )
    return found


async def _backfill_one(engine: Any, item: dict, identity: dict) -> bool:
    """单条回填：metadata 补丁式更新（其余键保留），返回是否成功。

    调用对齐 2026-09-23 实测的 LivingMemory 签名
    update_memory(memory_id, updates)（memory_engine_crud.py:612）：
    updates={"metadata": {...}} 走 hybrid_retriever 三库同步，且键触及
    participant_identities 时引擎自动 index_memory 重建图谱（bot 身份 →
    fact 的 mentioned_in 边在这里产生，任务书 B3）。
    """
    updates = {"metadata": {"participant_identities": [dict(identity)]}}
    method = getattr(engine, "update_memory", None)
    if not callable(method):
        logger.warning(
            "[SelfHeal] 存量回填：engine.update_memory 不可用（接口变化？）"
        )
        return False
    try:
        ok = method(item["id"], updates)
        if asyncio.iscoroutine(ok):
            ok = await ok
    except TypeError as e:
        logger.warning(
            f"[SelfHeal] 记忆 {item['id']} 回填签名失配（跳过）: {e}"
        )
        return False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 单条失败只记日志不中断（任务书 B3：回填与后续机制解耦）
        logger.warning(f"[SelfHeal] 记忆 {item['id']} 回填失败（跳过）: {e}")
        return False
    if not ok:
        logger.warning(f"[SelfHeal] 记忆 {item['id']} 回填被引擎拒绝")
    return bool(ok)


async def run_ghost_identity_backfill(engine: Any, bot_identity: dict) -> dict:
    """身份可用时的一次性存量回填：扫描 → 回填 → 图谱联动 → 统计。

    Args:
        engine: LivingMemory 引擎（backend.engine）。
        bot_identity: main._bot_identity() 产物（identity_key/sender_id/
            platform/display_name/aliases/is_bot——与正常写入规范同构）。

    Returns:
        {"scanned": SQL 命中 living_ghost 的行数, "backfilled": 成功回填数,
         "skipped": 已有身份/空正文/失败跳过数}
    """
    rows = await scan_ghost_memories_without_identity(engine)
    scanned = 0
    try:
        conn = getattr(engine, "db_connection", None)
        if conn is not None:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) FROM documents
                WHERE json_extract(metadata, '$.session_id') LIKE ?
                """,
                (GHOST_SESSION_PREFIX + "%",),
            )
            row = await cursor.fetchone()
            scanned = int(row[0]) if row else 0
    except asyncio.CancelledError:
        raise
    except Exception:
        scanned = len(rows)  # 计数失败时退化为可回填集合大小

    backfilled = 0
    for item in rows:
        if not isinstance(bot_identity, dict) or not bot_identity:
            break  # 身份不可用：宁可全部不回填也不写空身份
        if await _backfill_one(engine, item, bot_identity):
            backfilled += 1

    summary = {
        "scanned": scanned,
        "backfilled": backfilled,
        "skipped": scanned - backfilled,
    }
    logger.info(
        f"[SelfHeal] 存量身份回填完成：扫描 {summary['scanned']} 条，"
        f"回填 {backfilled} 条，跳过 {summary['skipped']} 条"
    )
    return summary


def save_state(state_path: str | Path, state: dict) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )



async def cooldown_interests_once(
    mood: Any,
    state_path: str | Path,
    threshold: float = 0.85,
    factor: float = 0.4,
) -> list[str]:
    """数据降温一次性自愈（任务书 M3 补丁 VII 需求 5，幂等）。

    历史偏执循环把单主题兴趣顶到 1.0——不降温的话饱和曲线+衰减要连跑
    数天才能自然稀释。启动时检查：任一 topic 兴趣 >= threshold → 乘
    factor（1.0 -> 0.4）。幂等：state 文件标记已执行后不再重复。
    返回被降温的主题列表（空 = 未触发或已执行过）。
    """
    state = load_state(state_path)
    if state.get("interest_cooldown_done"):
        return []
    try:
        cooled = mood.cooldown_hot_interests(threshold, factor)
    except Exception as e:
        logger.warning(f"[SelfHeal] 兴趣降温失败（跳过）: {e}")
        return []
    state["interest_cooldown_done"] = True
    state["interest_cooldown_at"] = datetime.now().isoformat(timespec="seconds")
    state["interest_cooldown_topics"] = cooled
    save_state(state_path, state)
    if cooled:
        logger.info(
            f"[SelfHeal] 兴趣降温：{cooled} 已乘 {factor}"
            "（偏执循环历史数据一次性稀释）"
        )
    return cooled
