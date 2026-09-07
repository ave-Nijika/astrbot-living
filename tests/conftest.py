"""pytest 公共引导。

1. 把插件工作目录加进 sys.path，使 `import core.xxx` 可用；
2. 在导入 astrbot 之前设置 ASTRBOT_ROOT，使 astrbot 的路径解析指向本机
   AstrBot 实例（测试只读其配置，不写其数据库）。

运行方式（AstrBot venv python）：
  cd D:\\sandbox\\astrbot-living
  D:\\astrbot\\...\\AstrBot\\venv\\Scripts\\python.exe -m pytest tests/ -v
"""

import os
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = Path(r"D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot")

if str(WORKDIR) not in sys.path:
    sys.path.insert(0, str(WORKDIR))

# AstrBot 以源码树方式运行（venv 里没有 pip 安装 astrbot），需手动指根
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))

import pytest  # noqa: E402


def get_bocha_key() -> str:
    """从 AstrBot 配置读博查 key（该文件带 UTF-8 BOM，用 utf-8-sig 解析）。"""
    import json

    cfg_path = ASTRBOT_ROOT / "data" / "cmd_config.json"
    if not cfg_path.exists():
        return ""
    with open(cfg_path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    key = cfg.get("provider_settings", {}).get("websearch_bocha_key", [])
    if isinstance(key, (list, tuple)):
        key = next((k for k in key if k), "")
    return str(key or "")


requires_bocha_key = pytest.mark.skipif(
    not get_bocha_key(), reason="AstrBot 配置中没有博查 key"
)
