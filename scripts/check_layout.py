# -*- coding: utf-8 -*-
"""M25-补丁1 验收 7：_layout 覆盖完整性比对脚本。

比对 _conf_schema.json 的全部扁平键（advanced 顶层 type 键 124 + preset 9，
子组 weights/source_weights 的子键不算独立键）与 section 元数据的落位：

  1. 每个 schema 键都有 section 且为 [一级id, 二级id, 序号] 三元组；
  2. section 指向的一级/二级 id 都存在于 panel_layout.json 的栏目树；
  3. 同一二级组内序号无重复（遗漏 0 / 重复 0）；
  4. panel_layout.json 无"幽灵组"（每个二级组都有键落入，fallback 区除外）；
  5. 键级 requires 的每环都有 label 与 anchor（源码锚点纪律）。

独立可跑（报告贴输出），也被 tests/test_m25_patch1.py 导入复用：
  python scripts/check_layout.py   # 全部通过 exit 0，否则非 0
"""
import json
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = WORKDIR / "_conf_schema.json"
LAYOUT_PATH = WORKDIR / "panel_layout.json"


def flat_keys(schema: dict) -> dict[str, dict]:
    """全部扁平键：{组名.键名: 键定义}（含 preset 无组前缀键）。"""
    keys: dict[str, dict] = {}
    for group, body in schema.get("advanced", {}).get("items", {}).items():
        for key, item in body.get("items", {}).items():
            if isinstance(item, dict) and "type" in item:
                keys[f"{group}.{key}"] = item
    for key, item in schema.get("preset", {}).get("items", {}).items():
        if isinstance(item, dict) and "type" in item:
            keys[key] = item
    return keys


def iter_requires(layout: dict, schema: dict) -> list[tuple[str, dict]]:
    """全部 requires 环：(出处描述, 环定义)。键级 + 组级。"""
    out: list[tuple[str, dict]] = []
    for path, item in flat_keys(schema).items():
        for req in item.get("requires", []) or []:
            out.append((f"键 {path}", req))
    for sec in layout.get("sections", []):
        for grp in sec.get("groups", []):
            for req in grp.get("requires", []) or []:
                out.append((f"组 {sec['id']}.{grp['id']}", req))
    return out


def check(schema: dict, layout: dict) -> list[str]:
    """执行全部校验，返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    keys = flat_keys(schema)

    # 1. 每键有 section 且结构正确
    no_section = [p for p, item in keys.items() if "section" not in item]
    if no_section:
        problems.append(f"缺少 section 的键（{len(no_section)}）: {no_section}")
    bad_shape = [
        p for p, item in keys.items()
        if "section" in item and (
            not isinstance(item["section"], list) or len(item["section"]) != 3
            or not all(isinstance(x, str) for x in item["section"][:2])
            or not isinstance(item["section"][2], int)
        )
    ]
    if bad_shape:
        problems.append(f"section 结构非法（需 [一级, 二级, 序号]）: {bad_shape}")

    # 栏目树索引
    sec_ids = {s["id"] for s in layout.get("sections", [])}
    grp_ids = {
        (s["id"], g["id"])
        for s in layout.get("sections", []) for g in s.get("groups", [])
    }

    # 2/3. 指向存在 + 组内序号唯一
    orders: dict[tuple, list] = {}
    for path, item in keys.items():
        sec = item.get("section")
        if not isinstance(sec, list) or len(sec) != 3:
            continue
        s, g, order = sec
        if s not in sec_ids:
            problems.append(f"{path}: section 一级 {s!r} 不在布局树")
        if (s, g) not in grp_ids:
            problems.append(f"{path}: section 二级 {s}.{g} 不在布局树")
        orders.setdefault((s, g), []).append((order, path))
    for (s, g), lst in orders.items():
        seen: dict = {}
        for order, path in sorted(lst):
            if order in seen:
                problems.append(
                    f"{s}.{g} 序号 {order} 重复: {seen[order]} 与 {path}"
                )
            seen[order] = path

    # 4. 幽灵组（fallback 兜底区除外）
    for s in layout.get("sections", []):
        for g in s.get("groups", []):
            if g.get("fallback") or g.get("id") == "Z1":
                continue
            if (s["id"], g["id"]) not in orders:
                problems.append(f"布局组 {s['id']}.{g['id']} 没有任何键落入（幽灵组）")

    # 5. requires 纪律：label + anchor 必填（任务书"提醒 2"：宁可标未知也不猜）
    for where, req in iter_requires(layout, schema):
        if req.get("op") == "anyOf":
            for sub in req.get("of", []):
                if not sub.get("label"):
                    problems.append(f"{where}: anyOf 子环缺 label: {sub}")
        if not req.get("label"):
            problems.append(f"{where}: requires 环缺 label: {req}")
        if not req.get("anchor"):
            problems.append(f"{where}: requires 环缺 anchor（源码锚点）: {req.get('label')}")

    return problems


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    layout = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    keys = flat_keys(schema)
    print(f"schema 扁平键: {len(keys)}（advanced "
          f"{sum(1 for k in keys if '.' in k)} + preset "
          f"{sum(1 for k in keys if '.' not in k)}）")
    problems = check(schema, layout)
    if problems:
        print(f"发现 {len(problems)} 个问题:")
        for p in problems:
            print(f"  - {p}")
        return 1
    grp_of: dict[tuple, int] = {}
    for item in keys.values():
        s, g, _ = item["section"]
        grp_of[(s, g)] = grp_of.get((s, g), 0) + 1
    print(f"覆盖: 遗漏 0 / 重复 0；{len(grp_of)} 个二级组全部有键落入，"
          f"一级区 {len({s for s, _ in grp_of})} 个")
    print("OK: 覆盖完整性校验全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
