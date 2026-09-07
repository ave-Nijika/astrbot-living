"""C2 网页抓取能力：httpx 异步拉取 + 简单启发式正文提取。

约束（任务书 R2-C2）：
  - 超时 15s，响应大小上限 2MB，UA 伪装常规浏览器
  - 正文提取：去 script/style/nav 等标签、按行保留文本、压缩空行
  - 编码容错：charset-normalizer 检测（requests 同源方案），失败退回 UTF-8 替换
  - 不引重型依赖（不使用 BeautifulSoup 等）
"""

from __future__ import annotations

import html as html_mod
import re

import httpx

DEFAULT_TIMEOUT = 15.0
MAX_BYTES = 2 * 1024 * 1024  # 2MB 响应上限
MAX_TEXT_CHARS = 20000  # 提取后正文上限，防止单页占用过多 token

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# 需要整体剔除的标签块（导航、脚本、样式等对"阅读"无意义的内容）
_STRIP_BLOCKS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|iframe|svg)\b"
    r"[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# <br>/<p>/<li>/标题等块级标签视为换行
_BLOCK_BREAK_RE = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|blockquote|pre)\b[^>]*>",
    re.IGNORECASE,
)


def _decode_body(raw: bytes, http_charset: str | None) -> str:
    """解码响应体：HTTP 声明的编码 > charset-normalizer 检测 > UTF-8 替换。"""
    candidates = []
    if http_charset:
        candidates.append(http_charset)
    try:
        import charset_normalizer

        best = charset_normalizer.from_bytes(raw).best()
        if best and best.encoding:
            candidates.append(best.encoding)
    except Exception:
        pass  # charset-normalizer 未安装或检测失败，走兜底
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def extract_text(html: str) -> str:
    """启发式正文提取：剔除无用块 → 块级标签转换行 → 去标签 → 压缩空行。"""
    text = _STRIP_BLOCKS.sub(" ", html)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    # 压缩空行：全 empty 的行丢弃；保留有内容的行
    lines = [line for line in lines if line]
    full = "\n".join(lines)
    return full[:MAX_TEXT_CHARS]


class WebFetcher:
    """网页抓取。fetch() 返回 {title, text, status}。"""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_BYTES,
        user_agent: str = _DEFAULT_UA,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._client: httpx.AsyncClient | None = None
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers=self._headers,
                follow_redirects=True,
                trust_env=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def fetch(self, url: str) -> dict:
        """抓取网页并提取正文。

        Returns:
            {"title": str, "text": str, "status": int}

        Raises:
            httpx.HTTPError: 网络错误 / 超时 / 非 2xx 状态。
        """
        client = self._get_client()
        # 流式读取并在 max_bytes 处截断，避免超大响应撑爆内存
        buf = bytearray()
        status = 0
        async with client.stream("GET", url) as resp:
            status = resp.status_code
            resp.raise_for_status()
            http_charset = resp.charset_encoding
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) >= self._max_bytes:
                    break
        raw = bytes(buf[: self._max_bytes])
        html = _decode_body(raw, http_charset)

        title = ""
        m = _TITLE_RE.search(html)
        if m:
            title = html_mod.unescape(m.group(1)).strip()

        return {"title": title, "text": extract_text(html), "status": status}
