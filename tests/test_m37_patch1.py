"""M37-补丁1 回归：判断记录「重启后首次写入覆盖历史」修复。

根因：OutputJudge._add_record 不触发从盘加载，新实例（模拟重启）第一次
写记录时内存只有这 1 条，_save_records 以 "w" 整份覆盖把盘上历史冲掉。
修法：_add_record 写前补一次 _load_records_from_disk（复用既有
_records_loaded 标志——只加载一次；复用其全套异常兜底）。

本文件锁：
- 验收 1：重启后直接写不丢（实例 A 写 → 实例 B 直接 _add_record）
- 验收 2：同实例连续写的条数与顺序（test_e1 语义之外补同款）
- 验收 3：加载只发生一次（计数 + 行为两种断言）
- 验收 4：records() 先 add 后查不重复读回（内存权威语义不变）
- 验收 5：record_limit 裁剪（含重启后新写入参与裁剪）
- 验收 6：盘上无文件/损坏 JSON/字段形态
- 验收 7：落盘失败静默（不影响判断主流程）
- 红线：JSON 字段名一个不改；records()/_save_records 语义不动
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.judge import OutputJudge

NOW = datetime(2026, 10, 11, 9, 0, 0)


def _mk(path, limit: int = 50) -> OutputJudge:
    cfg = {"judge": {"mode": "api", "provider_id": "p", "record_limit": limit}}
    return OutputJudge(
        config_getter=lambda: cfg,
        llm_call=None,
        records_path=path,
        clock=lambda: NOW,
    )


def _disk_rows(path) -> list:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 验收 1（核心）：重启后直接写不丢
# ---------------------------------------------------------------------------
def test_v1_restart_direct_write_keeps_history(tmp_path):
    """实例 A 写 2 条 → 实例 B（同 path，模拟重启）直接 _add_record 1 条
    → 盘上共 3 条、旧的 2 条内容仍在、新的在最前。"""
    p = tmp_path / "judge_records.json"
    a = _mk(p)
    a._add_record(
        side="input", input_summary="旧消息一", verdict="chat/normal/plain",
        injected=True,
    )
    a._add_record(
        side="output", input_summary="旧消息二", verdict="ok: 无问题",
        injected=False,
    )
    assert len(_disk_rows(p)) == 2

    b = _mk(p)  # 新实例：不走 records()，直接写（缺陷路径）
    b._add_record(
        side="input", input_summary="重启后第一条", verdict="chat/short/plain",
        injected=True,
    )

    rows = _disk_rows(p)
    assert len(rows) == 3, f"重启后直接写丢了历史：盘上只剩 {rows}"
    summaries = [r["input_summary"] for r in rows]
    assert summaries == ["重启后第一条", "旧消息二", "旧消息一"]
    # 旧条目内容逐字段仍在
    assert rows[2]["verdict"] == "chat/normal/plain"
    assert rows[2]["injected"] is True
    assert rows[1]["side"] == "output"
    # 新实例内存视图同样完整（面板经 records() 读到全部 3 条）
    assert len(b.records()) == 3


# ---------------------------------------------------------------------------
# 验收 2：同实例连续写
# ---------------------------------------------------------------------------
def test_v2_same_instance_sequential_writes(tmp_path):
    """连续 _add_record 多条：条数正确、新的在前（与 test_e1 同语义）。"""
    p = tmp_path / "judge_records.json"
    j = _mk(p)
    for i in range(4):
        j._add_record(
            side="input", input_summary=f"消息{i}", verdict="chat", injected=False
        )
    rows = _disk_rows(p)
    assert len(rows) == 4
    assert [r["input_summary"] for r in rows] == ["消息3", "消息2", "消息1", "消息0"]


# ---------------------------------------------------------------------------
# 验收 3：加载只发生一次（计数 + 行为两种断言）
# ---------------------------------------------------------------------------
def test_v3_load_only_once_by_count(tmp_path):
    """多次 _add_record 只触发一次从盘加载（_records_loaded 标志有效）。"""
    p = tmp_path / "judge_records.json"
    a = _mk(p)
    a._add_record(side="input", input_summary="种子", verdict="chat", injected=False)

    b = _mk(p)
    calls = {"n": 0}
    real_load = b._load_records_from_disk

    def counting_load():
        calls["n"] += 1
        real_load()

    b._load_records_from_disk = counting_load
    for i in range(3):
        b._add_record(
            side="input", input_summary=f"新{i}", verdict="chat", injected=False
        )
    assert calls["n"] == 1, f"加载了 {calls['n']} 次（应只在首次写前加载一次）"
    assert len(_disk_rows(p)) == 4


def test_v3_load_only_once_by_behavior(tmp_path):
    """行为断言：首次写后外部修改盘文件，后续写入不会把它读回来。"""
    p = tmp_path / "judge_records.json"
    a = _mk(p)
    a._add_record(side="input", input_summary="种子", verdict="chat", injected=False)

    b = _mk(p)
    b._add_record(side="input", input_summary="第一条", verdict="chat", injected=False)
    # 外部（另一进程/手工）往盘上塞一条伪造记录
    rows = _disk_rows(p)
    rows.append(
        {"ts": "2026-10-11 00:00:00", "side": "input", "input_summary": "外部伪造",
         "verdict": "chat", "injected": False, "rewrote": False}
    )
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    # 同实例再写——若再次读盘会把伪造条目带进内存并落盘
    b._add_record(side="input", input_summary="第二条", verdict="chat", injected=False)
    summaries = [r["input_summary"] for r in _disk_rows(p)]
    assert "外部伪造" not in summaries, "内存是权威：写后不应再从盘读回外部改动"
    assert summaries == ["第二条", "第一条", "种子"]


# ---------------------------------------------------------------------------
# 验收 4：records() 语义不变（先 add 后查不重复读回）
# ---------------------------------------------------------------------------
def test_v4_records_after_add_no_duplicate_readback(tmp_path):
    """先 _add_record 再 records()：同一条不被读成两份（内存权威）。"""
    p = tmp_path / "judge_records.json"
    j = _mk(p)
    j._add_record(side="input", input_summary="唯一一条", verdict="chat", injected=True)
    rows = j.records()
    assert len(rows) == 1
    assert rows[0]["input_summary"] == "唯一一条"
    # 再写再查，总数 = 累计数（不因读盘翻倍）
    j._add_record(side="input", input_summary="第二条", verdict="chat", injected=True)
    assert len(j.records()) == 2


def test_v4_records_on_empty_still_loads(tmp_path):
    """盘上有历史、内存空、未加载 → records() 仍读盘（既有行为不变）。"""
    p = tmp_path / "judge_records.json"
    seed = [
        {"ts": "2026-10-10 14:46:00", "side": "input", "input_summary": "历史",
         "verdict": "chat", "injected": True, "rewrote": False}
    ]
    p.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    j = _mk(p)
    assert j.records()[0]["input_summary"] == "历史"


# ---------------------------------------------------------------------------
# 验收 5：record_limit 裁剪（含重启后新写入参与裁剪）
# ---------------------------------------------------------------------------
def test_v5_record_limit_trims_on_disk(tmp_path):
    """同实例写超限：盘上保留上限条、新的在前。"""
    p = tmp_path / "judge_records.json"
    j = _mk(p, limit=3)
    for i in range(5):
        j._add_record(
            side="input", input_summary=f"消息{i}", verdict="chat", injected=False
        )
    rows = _disk_rows(p)
    assert len(rows) == 3
    assert [r["input_summary"] for r in rows] == ["消息4", "消息3", "消息2"]


def test_v5_limit_applies_across_restart(tmp_path):
    """重启后新实例直接写：裁剪照样生效（旧的超限条目被新的顶出）。"""
    p = tmp_path / "judge_records.json"
    a = _mk(p, limit=3)
    for i in range(3):
        a._add_record(
            side="input", input_summary=f"旧{i}", verdict="chat", injected=False
        )
    b = _mk(p, limit=3)
    b._add_record(side="input", input_summary="新条目", verdict="chat", injected=False)
    rows = _disk_rows(p)
    assert len(rows) == 3
    assert [r["input_summary"] for r in rows] == ["新条目", "旧2", "旧1"]


# ---------------------------------------------------------------------------
# 验收 6：盘上无文件 / 损坏 / 字段形态
# ---------------------------------------------------------------------------
def test_v6_missing_file_write_ok(tmp_path):
    """记录文件不存在：直接写正常建文件、不抛异常。"""
    p = tmp_path / "sub" / "judge_records.json"  # 连父目录都不存在
    j = _mk(p)
    j._add_record(side="input", input_summary="首条", verdict="chat", injected=False)
    assert len(_disk_rows(p)) == 1


def test_v6_corrupt_file_write_ok(tmp_path):
    """盘上是非法 JSON：按空记录继续，写入后盘上是合法 JSON。"""
    p = tmp_path / "judge_records.json"
    p.write_text("{这不是 JSON", encoding="utf-8")
    j = _mk(p)
    j._add_record(side="input", input_summary="修复后首条", verdict="chat",
                  injected=False)
    rows = _disk_rows(p)
    assert len(rows) == 1
    assert rows[0]["input_summary"] == "修复后首条"


def test_v6_fields_shape_unchanged(tmp_path):
    """红线：JSON 字段名一个不改（ts/side/input_summary/verdict/injected/
    rewrote——面板与历史数据依赖）。"""
    p = tmp_path / "judge_records.json"
    j = _mk(p)
    j._add_record(side="rewrite", input_summary="某回复", verdict="rewrote: 啰嗦",
                  injected=False, rewrote=True)
    (row,) = _disk_rows(p)
    assert set(row.keys()) == {
        "ts", "side", "input_summary", "verdict", "injected", "rewrote"
    }
    assert row["ts"] == "2026-10-11 09:00:00"
    assert row["rewrote"] is True


# ---------------------------------------------------------------------------
# 验收 7：落盘失败静默
# ---------------------------------------------------------------------------
def test_v7_unwritable_path_silent(tmp_path):
    """records_path 指向目录（open "w" 必败）：_add_record 不抛异常、
    主流程不受影响（记录绝不影响判断——红线 2）。"""
    p = tmp_path / "occupied"
    p.mkdir()
    j = _mk(p)
    j._add_record(side="input", input_summary="这条落不了盘", verdict="chat",
                  injected=False)  # 不抛即通过
    j._add_record(side="input", input_summary="第二条也不抛", verdict="chat",
                  injected=False)
    assert len(j.records()) == 2  # 内存照常累积


# ---------------------------------------------------------------------------
# 红线自查：源码形态（本批只补"写前加载"，其余语义不动）
# ---------------------------------------------------------------------------
def test_redline_fix_shape_and_neighbors_untouched():
    source = (Path(__file__).resolve().parent.parent / "core" / "judge.py").read_text(
        encoding="utf-8"
    )
    add_body = source.split("def _add_record(", 1)[1].split("def records(", 1)[0]
    # 修法本体：写前加载（且在 appendleft 之前）
    assert "if not self._records_loaded:" in add_body
    assert add_body.index("_load_records_from_disk") < add_body.index(
        "_records.appendleft"
    )
    # 只加载一次的锚点在加载函数本体（置位语义不变）
    load_body = source.split("def _load_records_from_disk(", 1)[1].split(
        "def _save_records(", 1
    )[0]
    assert load_body.index("self._records_loaded = True") < load_body.index("try:")
    # records() 的加载条件逐字不变（内存空且未加载过才读）
    records_body = source.split("def records(", 1)[1].split(
        "def _load_records_from_disk(", 1
    )[0]
    assert "if not self._records and not self._records_loaded:" in records_body
    # _save_records 仍是整份覆盖写（红线 3：不顺手重构）
    save_body = source.split("def _save_records(", 1)[1]
    assert 'open(self._records_path, "w", encoding="utf-8")' in save_body
    # JSON 字段名零改动（红线 1）
    assert '"ts": self._clock()' in add_body
