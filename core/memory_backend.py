"""C4 记忆后端：抽象基类 + LivingMemory 运行时软依赖 + 内置 SQLite 降级。

设计约定（总纲 D3 / 任务书 R2-C4）：
  - LivingMemory 是**运行时软依赖**：只通过 AstrBot 插件注册表拿它的实例并
    调其公开方法，绝不 `import astrbot_plugin_livingmemory`（AGPL 隔离）。
    任何一步探测失败都记录"不可用原因"并降级。
  - 依赖的两个核心签名（2026-09-07 对着 LivingMemory 源码核实过）：
      await engine.add_memory(content, session_id=None, importance=0.5,
                              metadata=None, ...) -> int
      await engine.search_memories(query, k=5, session_id=None, ...)
                                    -> list[HybridResult]
    HybridResult 关键字段：doc_id / final_score / content / metadata。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

LIVINGMEMORY_STAR_NAME = "astrbot_plugin_livingmemory"


class MemoryBackend(ABC):
    """记忆后端抽象。所有方法异步。"""

    @abstractmethod
    async def add(
        self,
        content: str,
        importance: float = 0.5,
        metadata: dict | None = None,
    ) -> int:
        """写入一条记忆，返回其 id。"""

    @abstractmethod
    async def search(self, query: str, k: int = 5) -> list[dict]:
        """检索记忆，返回 [{content, score, id, metadata}, ...]。"""

    @abstractmethod
    async def close(self) -> None:
        """释放资源。"""


class LivingMemoryBackend(MemoryBackend):
    """LivingMemory 引擎的薄封装。实例只能通过 probe() 获得。"""

    def __init__(self, engine: Any, source: str = "livingmemory") -> None:
        self._engine = engine
        self.source = source

    @classmethod
    async def probe(cls, context: Any) -> tuple["LivingMemoryBackend | None", str]:
        """从 AstrBot 插件注册表探测 LivingMemory 引擎。

        Returns:
            (backend, reason)：成功时 backend 非 None、reason 为空；
            失败时 backend 为 None、reason 说明不可用原因。
        """
        if context is None:
            return None, "context 为空"

        get_star = getattr(context, "get_registered_star", None)
        if not callable(get_star):
            return None, "context.get_registered_star 不可用"

        try:
            meta = get_star(LIVINGMEMORY_STAR_NAME)
        except Exception as e:
            return None, f"get_registered_star 异常: {e}"
        if meta is None:
            return None, f"未安装插件 {LIVINGMEMORY_STAR_NAME}"

        activated = getattr(meta, "activated", None)
        if not activated:
            return None, f"插件 {LIVINGMEMORY_STAR_NAME} 未激活"

        star_cls = getattr(meta, "star_cls", None)
        if star_cls is None:
            return None, "StarMetadata.star_cls 为空（插件未完成实例化？）"

        initializer = getattr(star_cls, "initializer", None)
        if initializer is None:
            return None, "star_cls.initializer 不存在（版本不匹配？）"

        engine = getattr(initializer, "memory_engine", None)
        if engine is None:
            return None, "initializer.memory_engine 不存在"

        for method in ("add_memory", "search_memories"):
            if not callable(getattr(engine, method, None)):
                return None, f"engine.{method} 不可用（签名不匹配）"

        return cls(engine), ""

    async def add(
        self,
        content: str,
        importance: float = 0.5,
        metadata: dict | None = None,
    ) -> int:
        doc_id = await self._engine.add_memory(
            content,
            session_id=None,
            importance=importance,
            metadata=metadata,
        )
        return int(doc_id)

    async def search(self, query: str, k: int = 5) -> list[dict]:
        results = await self._engine.search_memories(query, k=k, session_id=None)
        out = []
        for r in results or []:
            out.append(
                {
                    "id": getattr(r, "doc_id", None),
                    "content": getattr(r, "content", ""),
                    "score": getattr(r, "final_score", 0.0),
                    "metadata": getattr(r, "metadata", {}) or {},
                }
            )
        return out

    async def close(self) -> None:
        # 引擎归 LivingMemory 插件所有，这里不代管其生命周期
        return None


class SimpleBackend(MemoryBackend):
    """内置 SQLite 简单记忆（aiosqlite 单表，LIKE 关键词检索，M0 够用）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Any = None

    async def _get_db(self) -> Any:
        if self._db is None:
            import aiosqlite

            self._db = await aiosqlite.connect(self.db_path)
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._db.commit()
        return self._db

    async def add(
        self,
        content: str,
        importance: float = 0.5,
        metadata: dict | None = None,
    ) -> int:
        db = await self._get_db()
        cur = await db.execute(
            "INSERT INTO memories (content, importance, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                content,
                float(importance),
                json.dumps(metadata or {}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
        return cur.lastrowid

    async def search(self, query: str, k: int = 5) -> list[dict]:
        db = await self._get_db()
        terms = [t for t in query.split() if t] or ([query] if query else [])
        if not terms:
            return []
        # 命中词数作为相关性分数（简单启发式），重要度做次级排序
        like_clauses = " OR ".join(["content LIKE ?"] * len(terms))
        params = [f"%{t}%" for t in terms]
        async with db.execute(
            f"SELECT id, content, importance, metadata_json FROM memories "
            f"WHERE {like_clauses} "
            f"ORDER BY importance DESC, id DESC LIMIT ?",
            [*params, int(k)],
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for row_id, content, importance, metadata_json in rows:
            score = sum(1 for t in terms if t.lower() in content.lower())
            out.append(
                {
                    "id": row_id,
                    "content": content,
                    "score": float(score),
                    "importance": float(importance),
                    "metadata": json.loads(metadata_json or "{}"),
                }
            )
        return out

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None


async def create_backend(
    context: Any = None,
    mode: str = "auto",
    simple_db_path: str | None = None,
) -> tuple[MemoryBackend, str]:
    """记忆后端工厂。

    Args:
        context: AstrBot Context（auto/livingmemory 模式需要）。
        mode: "auto" | "livingmemory" | "simple"（来自配置 memory.backend）。
        simple_db_path: SimpleBackend 的 SQLite 路径。

    Returns:
        (backend, note)：note 描述选择过程/降级原因，供日志与诊断。
    """
    if mode in ("auto", "livingmemory"):
        backend, reason = await LivingMemoryBackend.probe(context)
        if backend is not None:
            return backend, "使用 LivingMemory 引擎"
        if mode == "livingmemory":
            raise RuntimeError(f"强制 livingmemory 模式但不可用: {reason}")
        note = f"LivingMemory 不可用（{reason}），降级 SimpleBackend"
    else:
        note = "配置指定 SimpleBackend"

    if not simple_db_path:
        raise ValueError("simple_db_path 不能为空（simple 模式）")
    return SimpleBackend(simple_db_path), note
