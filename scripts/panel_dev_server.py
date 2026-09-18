"""配置面板浏览器实测用本地服务器（M5 补丁 1 开发工具，非生产组件）。

作用：在无 AstrBot 运行时的环境下实测面板前端——
- 静态服务 pages/config/（index.html/app.js/style.css）；
- /api/plugin/page/bridge-sdk.js 返回一个 mock bridge（apiGet/apiPost 打到
  /mock/api/*）；
- /mock/api/* 直接调用插件真实的 core.panel_api 逻辑（同一套校验与写入），
  内存 config 初始为 schema 默认树。

用法：
    python scripts/panel_dev_server.py [端口，默认 8791]
    浏览器打开 http://127.0.0.1:<端口>/

注意：仅监听 127.0.0.1；配置只存内存，刷新即回默认后的当前值。
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKDIR))
# core.* 依赖 astrbot（logger 等）——测试外的运行环境需手动指 AstrBot 源码根
ASTRBOT_ROOT = Path(r"D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot")
sys.path.insert(0, str(ASTRBOT_ROOT))
os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))

from core.panel_api import (  # noqa: E402
    apply_panel_reset,
    apply_panel_save,
    build_config_payload,
    default_tree,
    load_schema,
)

PAGE_DIR = WORKDIR / "pages" / "config"
SCHEMA = load_schema(WORKDIR)
CONFIG = default_tree(SCHEMA)  # {"preset": {...}, "advanced": {...}}

MOCK_BRIDGE_JS = """
window.AstrBotPluginPage = {
  async ready() { return true; },
  async apiGet(path) {
    const resp = await fetch('/mock/api/' + path, { method: 'GET' });
    return await resp.json();
  },
  async apiPost(path, body) {
    const resp = await fetch('/mock/api/' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return await resp.json();
  },
};
"""

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   CONTENT_TYPES[".json"])

    def _send_file(self, name: str) -> None:
        path = PAGE_DIR / name
        if not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, path.read_bytes(),
                   CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send_file("index.html")
        elif path == "/app.js":
            self._send_file("app.js")
        elif path == "/style.css":
            self._send_file("style.css")
        elif path == "/api/plugin/page/bridge-sdk.js":
            self._send(200, MOCK_BRIDGE_JS.encode("utf-8"), CONTENT_TYPES[".js"])
        elif path == "/mock/api/config":
            self._send_json({"status": "ok", "data": build_config_payload(CONFIG, SCHEMA)})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"status": "error", "message": "请求体不是合法 JSON"})
            return
        if path == "/mock/api/config":
            try:
                summary = apply_panel_save(CONFIG, SCHEMA, payload)
                self._send_json({
                    "status": "ok",
                    "message": f"已保存 {summary['count']} 项",
                    "data": {"changed": summary["changed"]},
                })
            except Exception as e:  # PanelApiError 及其他，全部明文反馈
                self._send_json({"status": "error", "message": str(e)})
            return
        if path == "/mock/api/config/reset":
            summary = apply_panel_reset(CONFIG, SCHEMA)
            self._send_json({
                "status": "ok",
                "message": "已恢复默认值",
                "data": summary,
            })
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"面板实测服务器: http://127.0.0.1:{port}/  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
