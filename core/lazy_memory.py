"""LazyMemory——记忆后端懒加载器（任务书 M1-A）。

为什么懒：AstrBot 按目录序加载插件，本插件排在 LivingMemory 之前时，
加载期注册表里还没有它——加载时主动探测必然降级且（在 M0 实现里）不再重试，
主人在另一台机器上已复现此问题。

为什么不用 create_backend（auto 模式）：它内部降级后返回 SimpleBackend，
无法区分"探测成功"与"降级顶替"，会让懒加载器误判为成功并永久缓存。
所以这里直接调 LivingMemoryBackend.probe()，把两种结局分开处理：
  - 探测成功 → 缓存，之后不再探测；
  - 探测失败 → 返回可复用的 SimpleBackend（实例缓存，避免反复建连），
    但保留重试机会：下次 get() 仍会尝试探测，等 LivingMemory 晚加载就绪。
"""

from __future__ import annotations

from typing import Any, Callable

from astrbot.api import logger

from .memory_backend import LivingMemoryBackend, MemoryBackend, SimpleBackend


class LazyMemory:
    """记忆后端的懒加载代理。get() 永远返回可用后端（可能为降级 Simple）。"""

    def __init__(
        self,
        context: Any,
        mode_getter: Callable[[], str],
        db_path_getter: Callable[[], str],
    ) -> None:
        self._context = context
        self._mode_getter = mode_getter
        self._db_path_getter = db_path_getter
        self._backend: MemoryBackend | None = None
        self._fallback: SimpleBackend | None = None
        self.note: str = "尚未初始化"

    async def get(self) -> MemoryBackend:
        if self._backend is not None:
            return self._backend

        mode = self._mode_getter()
        note = ""
        if mode in ("auto", "livingmemory"):
            backend, reason = await LivingMemoryBackend.probe(self._context)
            if backend is not None:
                self._backend = backend
                self.note = "使用 LivingMemory 引擎"
                logger.info(f"[astrbot_plugin_living] 记忆后端: {self.note}")
                return self._backend
            if mode == "livingmemory":
                # 强制模式但引擎不可用：插件可靠性优先，降级并说明原因
                note = f"强制 livingmemory 但不可用（{reason}），降级 Simple"
            else:
                note = f"LivingMemory 不可用（{reason}），降级 SimpleBackend"
        else:
            note = "配置指定 SimpleBackend"

        self.note = note
        if self._fallback is None:
            self._fallback = SimpleBackend(self._db_path_getter())
            logger.info(
                f"[astrbot_plugin_living] 记忆后端降级 Simple（{note}），将择机重试"
            )
        return self._fallback

    async def close(self) -> None:
        for backend in (self._backend, self._fallback):
            if backend is not None:
                try:
                    await backend.close()
                except Exception:
                    logger.exception("[astrbot_plugin_living] 记忆后端关闭异常")
        self._backend = None
        self._fallback = None
