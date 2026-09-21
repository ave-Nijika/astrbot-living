"""存量数据修复：清嵌套睡前回顾 + interests 指纹归一（M5-补丁2 D3）。

背景（VM 实测）：睡前回顾用日期字符串检索命中了历次回顾自身，一天嵌套
4 层；interests 里同一主题（咖啡家族）有 5 个变体 key 靠变体轮换绕过惩罚。

处理（停机后由凛在 VM 执行）：
1. 睡前回顾：删除"回声"记忆——正文里"睡前想了想今天"出现 >= 2 次即视为
   嵌套产物；只出现 1 次的（含唯一含真实内容的一条）保留。规则对重复
   执行幂等：清完后剩余记忆的该短语均 <= 1 次，再跑无删除。
2. interests 归一：mood.db 的 interests 按 topic_fingerprint 分桶，同桶
   变体合并为一条（保留权重最大者）——interests 的存储 key 结构不变，
   只是去掉冗余变体。

安全性：
- 执行前自动备份两个库文件（<路径>.bak_m5p2）；
- 只触碰本插件的 mood.db 与**参数指定**的 livingmemory.db，不碰 AstrBot
  本体数据；
- 幂等：重复执行无副作用（第二次运行 0 删除 / 0 归一）。

用法（AstrBot 停机后）：
    python scripts/fix_sleep_topic_data.py <mood.db 路径> <livingmemory.db 路径>
    例：
    python scripts/fix_sleep_topic_data.py \\
        data/plugin_data/astrbot_plugin_living/mood.db \\
        data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

WORKDIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKDIR))

from core.topic_fingerprint import topic_fingerprint  # noqa: E402

# 嵌套回声特征：回顾前缀短语出现 >= NEST_ECHO_THRESHOLD 次即为嵌套产物
NEST_PHRASE = "睡前想了想今天"
NEST_ECHO_THRESHOLD = 2


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    target = path.with_name(path.name + ".bak_m5p2")
    if not target.exists():  # 幂等：保留第一次的备份
        shutil.copy2(path, target)
    return target


def clean_nested_reviews(lm_db_path: Path) -> tuple[int, int]:
    """清 livingmemory.db 里的嵌套回顾。返回 (删除数, 保留数)。"""
    conn = sqlite3.connect(str(lm_db_path))
    try:
        rows = conn.execute(
            "SELECT id, content FROM documents WHERE content LIKE '%睡前想了想今天%'"
        ).fetchall()
        to_delete = [
            (row_id, content)
            for row_id, content in rows
            if content.count(NEST_PHRASE) >= NEST_ECHO_THRESHOLD
        ]
        for row_id, _content in to_delete:
            conn.execute("DELETE FROM documents WHERE id = ?", (row_id,))
        conn.commit()
        return len(to_delete), len(rows) - len(to_delete)
    finally:
        conn.close()


def normalize_interests(mood_db_path: Path) -> tuple[int, int]:
    """mood.db interests 按指纹归一。返回 (归并删除的变体数, 保留主题数)。"""
    conn = sqlite3.connect(str(mood_db_path))
    try:
        row = conn.execute(
            "SELECT value FROM mood WHERE key = 'interests'"
        ).fetchone()
        if not row:
            return 0, 0
        try:
            interests = json.loads(row[0])
        except (TypeError, ValueError):
            print("interests 不是合法 JSON，跳过归一")
            return 0, 0
        if not isinstance(interests, dict):
            return 0, 0

        # 按指纹分桶，桶内保留权重最大的 key（变体其余删除）
        buckets: dict[str, list[str]] = {}
        for key in interests:
            buckets.setdefault(topic_fingerprint(key), []).append(key)
        merged = 0
        for _fp, keys in buckets.items():
            if len(keys) <= 1:
                continue
            keeper = max(keys, key=lambda k: float(interests.get(k, 0.0)))
            for extra in keys:
                if extra != keeper:
                    interests.pop(extra, None)
                    merged += 1
        if merged:
            conn.execute(
                "UPDATE mood SET value = ? WHERE key = 'interests'",
                (json.dumps(interests, ensure_ascii=False),),
            )
            conn.commit()
        return merged, len(interests)
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    mood_path = Path(argv[1])
    lm_path = Path(argv[2])
    if not mood_path.exists():
        print(f"找不到 mood.db：{mood_path}")
        return 1
    if not lm_path.exists():
        print(f"找不到 livingmemory.db：{lm_path}")
        return 1

    for path in (mood_path, lm_path):
        target = backup(path)
        print(f"已备份：{path} → {target}" if target else f"跳过备份（不存在）：{path}")

    try:
        deleted, kept = clean_nested_reviews(lm_path)
        print(f"嵌套回顾：删除 {deleted} 条，保留 {kept} 条（非回声回顾不动）")
    except sqlite3.OperationalError as e:
        print(f"livingmemory.db 结构不符（表名/字段缺失），跳过回顾清理：{e}")

    if "mood" not in _kv_tables(mood_path):
        print("mood.db 无 mood 表，跳过 interests 归一")
        return 0
    merged, kept_topics = normalize_interests(mood_path)
    print(f"interests 归一：合并变体 {merged} 个，保留主题 {kept_topics} 个")
    print("完成。可重复执行验证幂等（第二次应全部为 0）。")
    return 0


def _kv_tables(mood_db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(mood_db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {name for (name,) in rows}
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
