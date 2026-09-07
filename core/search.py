"""C1 搜索能力：复用 AstrBot 内置博查搜索的 key，直连博查 Web Search API。

优先方案说明：
  理想路径是运行时从 AstrBot 工具管理器拿内置 BochaWebSearchTool 直接调用，
  但该工具的 call() 需要 AstrAgentContext（含 event），与 M0 的无事件组件化
  使用方式不匹配；因此采用任务书允许的兜底方案——按博查官方 API 规格
  （POST https://api.bochaai.com/v1/web-search）直连 HTTP，key 从 AstrBot
  配置 provider_settings.websearch_bocha_key 读取。这里只做调用适配，
  不复制 AstrBot 内置工具的代码。
"""

from __future__ import annotations

from typing import Any

import httpx

BOCHA_SEARCH_URL = "https://api.bochaai.com/v1/web-search"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def resolve_bocha_key(context: Any = None, api_key: str | list | None = None) -> str:
    """解析博查 key：显式传入 > AstrBot 配置 > 空串。

    AstrBot 配置里 websearch_bocha_key 可能是 list（多 key 轮换）或 str，
    这里统一取第一个可用值。
    """
    if api_key:
        if isinstance(api_key, (list, tuple)):
            for k in api_key:
                if k:
                    return str(k)
            return ""
        return str(api_key)
    if context is not None:
        try:
            settings = context.get_config().get("provider_settings", {})
            return resolve_bocha_key(api_key=settings.get("websearch_bocha_key"))
        except Exception:
            return ""
    return ""


class BochaSearcher:
    """博查网页搜索。search() 返回 [{title, url, summary}, ...]。"""

    def __init__(
        self,
        context: Any = None,
        api_key: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._context = context
        self._api_key_override = api_key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": _DEFAULT_UA},
                trust_env=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def search(
        self,
        query: str,
        count: int = 5,
        summary: bool = True,
        freshness: str | None = None,
    ) -> list[dict]:
        """搜索并返回 [{title, url, summary}, ...]。

        失败时抛出异常，由调用方决定如何降级。
        """
        key = resolve_bocha_key(self._context, self._api_key_override)
        if not key:
            raise RuntimeError(
                "博查 key 不可用：请确认 AstrBot 配置 provider_settings."
                "websearch_bocha_key 已填写，或在 BochaSearcher(api_key=...) 传入。"
            )

        payload: dict[str, Any] = {"query": query, "count": count, "summary": summary}
        if freshness:
            payload["freshness"] = freshness

        resp = await self._get_client().post(
            BOCHA_SEARCH_URL,
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        resp.raise_for_status()
        data = resp.json()

        rows = (data.get("data") or {}).get("webPages") or {}
        items = rows.get("value") or []
        return [
            {
                "title": item.get("name", ""),
                "url": item.get("url", ""),
                # summary=True 时博查给更长的 summary，否则退回 snippet
                "summary": item.get("summary") or item.get("snippet", ""),
            }
            for item in items
        ]
