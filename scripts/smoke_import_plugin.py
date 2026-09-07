"""冒烟验证：以包上下文真实 import 插件 main.py。

AstrBot 加载插件时，main.py 位于 data.plugins.<name> 包内，相对导入
`.core.xxx` 依赖父包。本脚本用合成包名模拟这一上下文，验证 main.py
及其全部依赖可以干净导入（不实例化、不连网、不碰 AstrBot 运行时）。
"""

import importlib
import sys
import types
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = Path(r"D:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot")

sys.path.insert(0, str(WORKDIR))
sys.path.insert(0, str(ASTRBOT_ROOT))

import os  # noqa: E402

os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))

PKG = "living_plugin_under_test"
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(WORKDIR)]
sys.modules[PKG] = pkg

main_mod = importlib.import_module(f"{PKG}.main")

assert hasattr(main_mod, "LivingPlugin"), "缺少插件主类"
assert hasattr(main_mod, "build_ghost_event"), "缺少幽灵事件构造"
sig_names = main_mod.LivingPlugin.__init__.__code__.co_varnames[:3]
assert "context" in sig_names and "config" in sig_names, (
    f"插件 __init__ 签名不符: {sig_names}"
)
assert callable(getattr(main_mod.LivingPlugin, "initialize", None))
assert callable(getattr(main_mod.LivingPlugin, "terminate", None))

print("SMOKE OK: main.py 在包上下文中导入成功")
print("  类:", main_mod.LivingPlugin.__name__)
print("  __init__ 参数:", sig_names)
