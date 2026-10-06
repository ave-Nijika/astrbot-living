"""一次性工具（M20-补丁1）：把 core/style_learning.py 与 core/style_review.py
里的复盘/归纳提示词常量注入 _conf_schema.json 的 schema default。

M19-补丁1 的 gen_schema_m19.py 同款先例：多行长文本手工转录必错，从代码
常量导入生成，另有守护测试断言 schema 默认与代码常量逐字一致。

用法（必须用 AstrBot venv python，因为常量模块 import astrbot.api）：
    ASTRBOT venv python scripts/gen_schema_m20.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.style_learning import (  # noqa: E402
    DEFAULT_PROMPT_DISTILL,
    DEFAULT_PROMPT_REVIEW,
)
from core.style_review import DEFAULT_PROMPT_INDUCT  # noqa: E402


def text_key(description: str, hint: str, default: str) -> dict:
    return {
        "description": description,
        "hint": hint,
        "type": "text",
        "default": default,
        "invisible": True,
    }


def main() -> None:
    schema_path = ROOT / "_conf_schema.json"
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    items = schema["advanced"]["items"]["style_learning"]["items"]
    items["prompt_distill"]["default"] = DEFAULT_PROMPT_DISTILL
    items["prompt_review"]["default"] = DEFAULT_PROMPT_REVIEW
    items["prompt_induct"]["default"] = DEFAULT_PROMPT_INDUCT
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=4)
        f.write("\n")
    print("schema defaults injected:", [k for k in ("prompt_distill", "prompt_review", "prompt_induct")])


if __name__ == "__main__":
    main()
