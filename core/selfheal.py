"""历史污染数据自愈（任务书 M3 补丁 VI 需求 2）。

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

    state = _load_state(state_path)
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
    _save_state(state_path, state)

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


def _load_state(state_path: str | Path) -> dict:
    path = Path(state_path)
    if not path.exists():
        return {"healed": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {"healed": {}}
    except (ValueError, OSError):
        return {"healed": {}}


def _save_state(state_path: str | Path, state: dict) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

