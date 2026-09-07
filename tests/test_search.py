"""C1 搜索组件测试。"""

import asyncio

import pytest

from conftest import get_bocha_key, requires_bocha_key

from core.search import BochaSearcher, resolve_bocha_key


@requires_bocha_key
def test_search_real_nextjs():
    """任务书要求：真实调一次 'Next.js 16 新特性'，断言 ≥1 条且含 url 字段。"""

    async def flow():
        searcher = BochaSearcher(api_key=get_bocha_key())
        try:
            return await searcher.search("Next.js 16 新特性", count=5)
        finally:
            await searcher.close()

    results = asyncio.run(flow())
    assert len(results) >= 1
    for item in results:
        assert set(item) >= {"title", "url", "summary"}
        assert item["url"].startswith("http")
    assert any("Next" in item["title"] or "next" in item["url"] for item in results)


def test_search_without_key_raises():
    """无 key 时应给出可诊断的报错而非静默失败。"""

    async def flow():
        searcher = BochaSearcher(api_key="")
        try:
            await searcher.search("anything")
        finally:
            await searcher.close()

    with pytest.raises(RuntimeError, match="博查 key"):
        asyncio.run(flow())


def test_resolve_key_from_list_and_str():
    """AstrBot 配置里的 key 可能是 list（轮换），应取第一个可用值。"""
    assert resolve_bocha_key(api_key=["a", "b"]) == "a"
    assert resolve_bocha_key(api_key="solo") == "solo"
    assert resolve_bocha_key(api_key=[None, "", "c"]) == "c"
    assert resolve_bocha_key(api_key=None, context=None) == ""
