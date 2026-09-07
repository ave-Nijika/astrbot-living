"""C2 抓取组件测试。

注意：httpx.AsyncClient 绑定创建时的事件循环，因此每个用例把
"创建→使用→关闭"放在同一次 asyncio.run 里，避免跨循环清理报错。
"""

import asyncio
import time

from core.fetcher import WebFetcher, extract_text


def test_extract_text_strips_script_and_compresses():
    html = """
    <html><head><title>测试页面</title><style>body{color:red}</style></head>
    <body>
    <nav>导航 导航</nav>
    <script>alert("x")</script>
    <p>第一段正文。</p>
    <div>第二段正文。</div>
    <p></p>
    <p>   </p>
    <p>第三段正文。</p>
    </body></html>
    """
    text = extract_text(html)
    assert "alert" not in text
    assert "导航" not in text
    assert "body{color:red}" not in text
    assert "第一段正文。" in text and "第三段正文。" in text
    # 压缩空行：正文行之间不应出现连续空行
    assert "\n\n" not in text


def test_fetch_example_com():
    """抓稳定页面：title 非空、正文含关键词、status=200。"""

    async def flow():
        fetcher = WebFetcher()
        try:
            return await fetcher.fetch("https://example.com/")
        finally:
            await fetcher.close()

    result = asyncio.run(flow())
    assert result["status"] == 200
    assert result["title"].strip() != ""
    assert "example" in result["text"].lower()
    assert "domain" in result["text"].lower() or "illustrative" in result["text"].lower()


def test_fetch_timeout_is_bounded():
    """不可路由地址应在超时上限附近失败而不是挂死。"""

    async def flow():
        fetcher = WebFetcher(timeout=3.0)
        try:
            try:
                await fetcher.fetch("http://10.255.255.1/")  # 不可路由地址
                return False, time.time()
            except Exception:
                return True, time.time()
        finally:
            await fetcher.close()

    start = time.time()
    raised, _ = asyncio.run(flow())
    elapsed = time.time() - start
    assert raised, "不可达地址应当抛异常"
    assert elapsed < 30, f"超时未生效，耗时 {elapsed:.1f}s"
