"""补丁 XVI 数据修复：合并"两个独立图谱"的孤儿身份节点。

（文中 QQ 号为中性示例；实际部署时以当时的 bot self_id 为准。）

背景（2026-09-18 现场诊断）：
  - 原生侧 bot 身份 = aiocqhttp:10001（platform type + bot QQ）
  - living 侧因白名单只收 platform id，提取落空 → 兜底 cron:astrbot
  - 两者是同一实体却各自成团 → 图谱两个连通分量

本脚本（停 AstrBot 后运行）：
  1. 改写 documents.metadata 里 participant_identities 的 cron:astrbot
     → aiocqhttp:10001（阻止未来重建时再生孤儿）
  2. 合并图谱：旧节点(account:cron:astrbot) 的边/条目关联全部改指
     新节点(account:aiocqhttp:10001)，然后删除旧节点
  3. 打印修复前后连通分量数（验收依据）

安全：只动这两个身份相关的行；运行前由调用方先行备份 db。
"""

import io
import json
import os
import sqlite3
import sys
from collections import defaultdict, deque

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

D = r"C:\Users\windows10\Downloads\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot\data\plugin_data\astrbot_plugin_livingmemory"
DB = os.path.join(D, "livingmemory.db")
OLD_KEY = "cron:astrbot"
NEW_KEY = "aiocqhttp:10001"
OLD_CANON = "account:cron:astrbot"
NEW_CANON = "account:aiocqhttp:10001"


def components(cur):
    """返回图谱连通分量列表（按大小降序）。"""
    cur.execute("SELECT id FROM graph_nodes")
    nodes = [r[0] for r in cur.fetchall()]
    adj = defaultdict(set)
    cur.execute("SELECT source_node_id, target_node_id FROM graph_edges")
    for a, b in cur.fetchall():
        adj[a].add(b)
        adj[b].add(a)
    seen, comps = set(), []
    for n in nodes:
        if n in seen:
            continue
        q, comp = deque([n]), set()
        while q:
            x = q.popleft()
            if x in comp:
                continue
            comp.add(x)
            for y in adj.get(x, ()):
                if y not in comp:
                    q.append(y)
        seen |= comp
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


con = sqlite3.connect(DB)
cur = con.cursor()

print("=== 修复前 ===")
comps = components(cur)
print(f"连通分量: {len(comps)} 个（大小 {[len(c) for c in comps]}）")

# ---- 1. documents.metadata 改写 ----
cur.execute("SELECT id, metadata FROM documents")
fixed_docs = 0
for did, meta_s in cur.fetchall():
    if not meta_s or OLD_KEY not in str(meta_s):
        continue
    try:
        meta = json.loads(meta_s)
    except Exception:
        continue
    changed = False
    for part in meta.get("participant_identities") or []:
        if str((part or {}).get("identity_key", "")) == OLD_KEY:
            part["identity_key"] = NEW_KEY
            part["sender_id"] = "10001"
            part["platform"] = "aiocqhttp"
            part["display_name"] = "aiocqhttp"
            part["aliases"] = ["aiocqhttp"]
            changed = True
    if changed:
        cur.execute(
            "UPDATE documents SET metadata=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), did),
        )
        fixed_docs += 1
print(f"documents.metadata 改写: {fixed_docs} 条")

# ---- 2. 图谱节点合并 ----
cur.execute(
    "SELECT id, node_type, canonical_value FROM graph_nodes WHERE node_type='person'"
)
persons = cur.fetchall()
old_id = next((r[0] for r in persons if r[2] == OLD_CANON), None)
new_id = next((r[0] for r in persons if r[2] == NEW_CANON), None)
print(f"旧节点(account:cron:astrbot)={old_id}  新节点(account:aiocqhttp:10001)={new_id}")

merged_edges = 0
moved_entries = 0
if old_id is not None and new_id is not None:
    # 边改指（去重：若已存在同款边则删旧边）
    cur.execute(
        "SELECT id, source_node_id, target_node_id, relation_type FROM graph_edges "
        "WHERE source_node_id=? OR target_node_id=?",
        (old_id, old_id),
    )
    old_edges = cur.fetchall()
    cur.execute("SELECT source_node_id, target_node_id, relation_type FROM graph_edges")
    existing = {(s, t, r) for s, t, r in cur.fetchall()}
    for eid, s, t, r in old_edges:
        ns = new_id if s == old_id else s
        nt = new_id if t == old_id else t
        if (ns, nt, r) in existing and (ns, nt, r) != (s, t, r):
            cur.execute("DELETE FROM graph_edges WHERE id=?", (eid,))
        else:
            cur.execute(
                "UPDATE graph_edges SET source_node_id=?, target_node_id=? WHERE id=?",
                (ns, nt, eid),
            )
            existing.add((ns, nt, r))
            merged_edges += 1
    # 条目关联改指（去重）
    cur.execute(
        "SELECT entry_id FROM graph_entry_nodes WHERE node_id=?", (old_id,)
    )
    entry_ids = [r[0] for r in cur.fetchall()]
    for eid in entry_ids:
        cur.execute(
            "SELECT 1 FROM graph_entry_nodes WHERE entry_id=? AND node_id=?",
            (eid, new_id),
        )
        if cur.fetchone():
            cur.execute(
                "DELETE FROM graph_entry_nodes WHERE entry_id=? AND node_id=?",
                (eid, old_id),
            )
        else:
            cur.execute(
                "UPDATE graph_entry_nodes SET node_id=? WHERE entry_id=? AND node_id=?",
                (new_id, eid, old_id),
            )
            moved_entries += 1
    # 删除旧节点
    cur.execute("DELETE FROM graph_nodes WHERE id=?", (old_id,))
    print(f"边改指: {merged_edges} 条 | 条目关联改指: {moved_entries} 条 | 旧节点已删")
elif old_id is None:
    print("旧节点不存在（无需合并）")
else:
    print("⚠ 新节点不存在——拒绝删除旧节点（避免丢失连接）")

con.commit()

print("\n=== 修复后 ===")
comps = components(cur)
print(f"连通分量: {len(comps)} 个（大小 {[len(c) for c in comps]}）")
cur.execute("SELECT id, node_type, node_value, canonical_value FROM graph_nodes WHERE node_type='person'")
print("当前 person 节点:")
for r in cur.fetchall():
    print(f"  id={r[0]} value={r[2]!r} canon={r[3]!r}")
con.close()
print("\n✓ 数据修复完成")
