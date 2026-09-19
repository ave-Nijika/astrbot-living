"""数据修复：睡前回顾孤岛 + cron:astrbot 身份残留（2026-09-19）。

两个问题（均由本次线上诊断发现）：

1. 睡前回顾（documents #10 / graph 节点 277-278）：
   写入时未注入 participant_identities（补丁 IV 漏了该路径，已在补丁 XX 修代码），
   导致它在图谱里没有 person 节点、成为孤立分量。
   修复：documents.metadata 补 participant_identities；
        图谱加 mentioned_in 边（bot 身份 → 该 fact）。

2. cron:astrbot 身份残留（graph 节点 268）：
   补丁 XVI 的身份合并未覆盖全（documents 里仍有 1 条带该身份的记录），
   LivingMemory 重建图谱时又生成了该 person 节点，它连着 5 条 09-18 的老对话记忆。
   修复：documents 里该身份改写为 aiocqhttp:<bot_qq>；
        图谱把 268 的边改指 241（aiocqhttp 身份）后删除 268。

用法（AstrBot 停机后执行）：
    python fix_islands_and_stale_identity.py
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
OLD_CANON = "account:cron:astrbot"


def components(cur):
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

# ---- 定位关键节点 ----
cur.execute("SELECT id, node_type, canonical_value FROM graph_nodes WHERE node_type='person'")
persons = cur.fetchall()
bot_node = next((r[0] for r in persons if str(r[2] or "").endswith("3410132338")), None)
stale_node = next((r[0] for r in persons if r[2] == OLD_CANON), None)
print(f"bot 身份节点(aiocqhttp)={bot_node}  陈旧节点(cron:astrbot)={stale_node}")

cur.execute(
    "SELECT id, node_type, node_value FROM graph_nodes "
    "WHERE node_type='fact' AND node_value LIKE '%睡前%'"
)
review_fact = cur.fetchone()
print(f"睡前回顾 fact 节点={review_fact[0] if review_fact else None}")

# ---- 1. documents：睡前回顾补身份；陈旧身份改写 ----
cur.execute("SELECT id, metadata FROM documents")
fixed_review = fixed_stale = 0
bot_identity = None
if bot_node is not None:
    cur.execute("SELECT node_type, node_value, canonical_value, metadata FROM graph_nodes WHERE id=?", (bot_node,))
    _, val, canon, meta_s = cur.fetchone()
    try:
        meta = json.loads(meta_s or "{}")
    except Exception:
        meta = {}
    meta["canonical_value"] = canon
    bot_identity = {
        "identity_key": meta.get("identity_key") or str(canon or "").replace("account:", ""),
        "sender_id": meta.get("sender_id") or "",
        "platform": meta.get("platform") or "",
        "display_name": meta.get("aliases", [val])[0] if meta.get("aliases") else val,
        "aliases": meta.get("aliases") or [val],
        "is_bot": True,
    }
    print(f"取到的 bot 身份: {bot_identity['identity_key']}")

cur.execute("SELECT id, metadata FROM documents")
for did, meta_s in cur.fetchall():
    if not meta_s:
        continue
    try:
        m = json.loads(meta_s)
    except Exception:
        continue
    changed = False
    parts = m.get("participant_identities") or []
    # (a) 睡前回顾：无身份 → 补 bot 身份
    if "睡前" in str(m.get("topics") or "") and not parts and bot_identity:
        m["participant_identities"] = [bot_identity]
        changed = True
    # (b) 陈旧身份改写
    for p in parts:
        if str((p or {}).get("identity_key", "")) == OLD_KEY and bot_identity:
            p["identity_key"] = bot_identity["identity_key"]
            p["sender_id"] = bot_identity["sender_id"]
            p["platform"] = bot_identity["platform"]
            p["display_name"] = bot_identity["display_name"]
            p["aliases"] = bot_identity["aliases"]
            changed = True
    if changed:
        cur.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps(m, ensure_ascii=False), did))
        if "睡前" in str(m.get("topics") or ""):
            fixed_review += 1
        else:
            fixed_stale += 1
print(f"documents 修改: 睡前回顾 {fixed_review} 条 | 陈旧身份 {fixed_stale} 条")

# ---- 2. 图谱：给睡前回顾 fact 加 mentioned_in 边 ----
if review_fact and bot_node:
    fid = review_fact[0]
    cur.execute(
        "SELECT 1 FROM graph_edges WHERE source_node_id=? AND target_node_id=? AND relation_type='mentioned_in'",
        (bot_node, fid),
    )
    if not cur.fetchone():
        # source_memory_id 为 NOT NULL：取该 fact 关联条目的来源记忆 id
        cur.execute(
            "SELECT e.source_memory_id FROM graph_entries e "
            "JOIN graph_entry_nodes en ON e.id = en.entry_id "
            "WHERE en.node_id = ? LIMIT 1",
            (fid,),
        )
        row = cur.fetchone()
        smid = row[0] if row else 10
        cur.execute(
            "INSERT INTO graph_edges (edge_key, source_node_id, target_node_id, relation_type, "
            "source_memory_id, weight, confidence, status, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, 'mentioned_in', ?, 1.0, 1.0, 'active', '{}', datetime('now'), datetime('now'))",
            (f"repair-review-{fid}", bot_node, fid, smid),
        )
        print(f"✓ 已补边: {bot_node} -> {fid} (mentioned_in)")

# ---- 3. 图谱：268 的边改指 bot 节点后删除 ----
if stale_node and bot_node and stale_node != bot_node:
    cur.execute(
        "SELECT id, source_node_id, target_node_id, relation_type FROM graph_edges "
        "WHERE source_node_id=? OR target_node_id=?",
        (stale_node, stale_node),
    )
    stale_edges = cur.fetchall()
    cur.execute("SELECT source_node_id, target_node_id, relation_type FROM graph_edges")
    existing = {(s, t, r) for s, t, r in cur.fetchall()}
    moved = dropped = 0
    for eid, s, t, r in stale_edges:
        ns = bot_node if s == stale_node else s
        nt = bot_node if t == stale_node else t
        if (ns, nt, r) in existing and (ns, nt, r) != (s, t, r):
            cur.execute("DELETE FROM graph_edges WHERE id=?", (eid,))
            dropped += 1
        else:
            cur.execute(
                "UPDATE graph_edges SET source_node_id=?, target_node_id=? WHERE id=?",
                (ns, nt, eid),
            )
            existing.add((ns, nt, r))
            moved += 1
    # entry_nodes 关联
    cur.execute("SELECT entry_id FROM graph_entry_nodes WHERE node_id=?", (stale_node,))
    for (eid,) in cur.fetchall():
        cur.execute(
            "SELECT 1 FROM graph_entry_nodes WHERE entry_id=? AND node_id=?", (eid, bot_node)
        )
        if cur.fetchone():
            cur.execute(
                "DELETE FROM graph_entry_nodes WHERE entry_id=? AND node_id=?", (eid, stale_node)
            )
        else:
            cur.execute(
                "UPDATE graph_entry_nodes SET node_id=? WHERE entry_id=? AND node_id=?",
                (bot_node, eid, stale_node),
            )
    cur.execute("DELETE FROM graph_nodes WHERE id=?", (stale_node,))
    print(f"✓ 陈旧节点已合并: 边改指 {moved} / 丢弃重复 {dropped} / 节点已删")

con.commit()

print("\n=== 修复后 ===")
comps = components(cur)
print(f"连通分量: {len(comps)} 个（大小 {[len(c) for c in comps]}）")
cur.execute("SELECT id, node_type, node_value, canonical_value FROM graph_nodes WHERE node_type='person'")
for r in cur.fetchall():
    print(f"  person id={r[0]} value={r[2]!r} canon={r[3]!r}")
con.close()
print("\n✓ 数据修复完成")
