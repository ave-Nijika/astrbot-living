"""配置结构迁移：平铺 → preset/advanced 分层（补丁 XVIII 部署辅助）。

背景：补丁 XVIII 把 _conf_schema.json 改为两层结构后，AstrBot 的
check_config_integrity 会把旧平铺键（config["sleep"]=… 等）当作
"schema 外的键"静默清除，并整棵插入默认值树——**用户存量调优会丢**。
本脚本在停机状态下把旧配置文件原地迁移到新结构，避免重置。

用法（AstrBot 停机后执行）：
    python scripts/migrate_config_to_advanced.py data/config/astrbot_plugin_living_config.json

行为：
- 原文件先备份为 <路径>.bak_pre18；
- 各平铺组（autonomy/decision/output_gate/sleep/capabilities/memory/model）
  原值搬进 advanced 下（键名与值都不改）；
- 旧 persona.life_extra 搬进 preset（组容器改名，值不改）；
- 新增 preset 组若旧配置里没有（不可能有），交由 AstrBot 按默认值补齐；
- 已删除的死键（补丁 XVII 清理项）丢弃并打印提示；
- 迁移是幂等的：已经是新结构的文件原样跳过。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

GROUPS = [
    "autonomy", "decision", "output_gate", "sleep",
    "capabilities", "memory", "model",
]
# 补丁 XVII 已删除的死键：迁移时丢弃（新版 schema 不认它们）
DEAD_KEYS = {
    "enable_search", "enable_fetch", "enable_sandbox", "enable_send",
    "sandbox_max_memory_mb", "fatigue_threshold", "default_importance",
    "recall_count", "log_level",
}


def migrate(config_path: str) -> int:
    path = Path(config_path)
    if not path.exists():
        print(f"找不到配置文件：{path}")
        return 1
    with open(path, encoding="utf-8-sig") as f:
        config = json.load(f)

    if "advanced" in config:
        print("已经是新结构（存在 advanced 组），无需迁移。")
        return 0

    advanced: dict = {}
    preset: dict = {}
    dropped: list[str] = []

    persona = config.get("persona")
    if isinstance(persona, dict) and "life_extra" in persona:
        preset["life_extra"] = persona["life_extra"]

    for group in GROUPS:
        value = config.get(group)
        if value is None:
            continue  # 旧配置里本就没有，交给 AstrBot 补默认值
        if not isinstance(value, dict):
            print(f"警告：{group} 组不是对象（{type(value).__name__}），原样跳过")
            continue
        cleaned = {}
        for key, val in value.items():
            if key in DEAD_KEYS:
                dropped.append(f"{group}.{key}")
                continue
            cleaned[key] = val
        advanced[group] = cleaned

    new_config = dict(config)
    for group in GROUPS + ["persona"]:
        new_config.pop(group, None)
    if preset:
        new_config["preset"] = preset
    new_config["advanced"] = advanced

    backup = path.with_name(path.name + ".bak_pre18")
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(new_config, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"迁移完成。原文件备份于 {backup}")
    print(f"advanced 组：{', '.join(sorted(advanced)) or '（空，全部用默认值）'}")
    print(f"preset 组：{', '.join(sorted(preset)) or '（空，全部用默认值）'}")
    if dropped:
        print(f"丢弃死键：{', '.join(dropped)}")
    print("提示：旋钮键（preset_*）按默认值回显，与底层键当前值可能不一致——"
          "旋钮只在你下次改动它时才写入。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(migrate(sys.argv[1]))
