"""M38-补丁1 测试：协商档导入修复 + 绝对自导入守护 + 活动池展示修正。

第一批（协商档从未生效的根因）：
main.py 协商器惰性导入曾写成绝对导入 ``from core.negotiator import ...``。
生产以包形态加载插件（data.plugins.<插件目录>.main），顶层 ``core`` 模块
不存在 → 首次进入 negotiate 档即 ModuleNotFoundError，且异常被钩子最外层
``except Exception`` 吞成 DEBUG——INFO 级日志一个字不落，输出侧质检整条
静默。测试环境 conftest 把插件目录塞进 sys.path，绝对导入照样通——
test/prod 加载形态分歧导致全绿漏网。本文件守护：
1. 插件源码不得存在指向自身包的绝对导入（AST 扫描，B1）；
2. 扫描器对反例样本必须能报出 文件:行号（防"永远绿的摆设"，B2）；
3. 包形态加载真实 main.py 后 _negotiator() 可用（验收 1）；
4. 钩子外层异常升 WARNING 带类型名、回复不受影响（验收 5）；
5. CancelledError 仍原样上抛（验收 6）。

第二批（活动池展示修正）：
runnableActivities 曾把 agent_activities 当活动池数据源 → 卡片少报
peek / reminisce（它们不勾选也照常跑）；多选框列 6 项但勾 peek /
reminisce 无任何效果。修后数据源换成真实活动池（六常驻）+ 形态标注，
多选框收窄为真正支持自主的四个，残留值展示时忽略、保存时保留。
"""

import ast
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
LAYOUT = json.loads((WORKDIR / "panel_layout.json").read_text(encoding="utf-8"))

HARNESS = WORKDIR / "tests" / "js" / "status_engine_harness.mjs"
HAS_NODE = shutil.which("node") is not None
NODE_SKIP = pytest.mark.skipif(not HAS_NODE, reason="node 不可用（JS 行为/DOM 测试）")

DEFAULT_AGENT_ACTIVITIES = ["surf", "read", "game", "free"]


def run_js(op: str, **kwargs):
    """node 桥：直调 status-engine.js 的纯函数（断言留在 Python 侧）。"""
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"op": op, **kwargs}),
        capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 桥失败: {proc.stderr[-800:]}"
    return json.loads(proc.stdout)


def run_harness(req: dict, timeout: int = 60):
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps(req),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 桥失败: {proc.stderr[-800:]}"
    return json.loads(proc.stdout)


def build_payload(**overrides):
    payload = {
        "knobs": {},
        "advanced": {},
        "schema": {
            "preset": {"items": SCHEMA["preset"]["items"]},
            "advanced": {"items": SCHEMA["advanced"]["items"]},
        },
        "layout": LAYOUT,
        "providers": ["p-chat"],
        "agent_tools": ["recall_long_term_memory", "web_search", "fetch_page"],
    }
    payload.update(overrides)
    return payload


def pool_names(**kw):
    return [a["name"] for a in run_js("runnableActivities", **kw)]


# ===========================================================================
# 第一批 B1/B2：绝对自导入守护（扫描器 + 反例验证）
# ===========================================================================
SELF_TOP_MODULES = {"core", "main"}


def scan_absolute_self_imports(root: Path) -> list[str]:
    """AST 扫描 root 下 main.py 与 core/**/*.py，收集指向插件自身的
    绝对导入（ImportFrom level==0 或 import 语句的顶层模块名命中
    core / main / 与插件目录同名者）。返回 ["文件:行号: 语句", ...]。

    只看真 import 节点——docstring/注释里的文字（如本文件的缺陷
    复述）不会被误报。
    """
    files = [root / "main.py"] + sorted((root / "core").rglob("*.py"))
    self_names = SELF_TOP_MODULES | {root.name}
    hits: list[str] = []
    for py in files:
        tree = ast.parse(py.read_text(encoding="utf-8"))
        rel = py.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[0] in self_names:
                    names = ", ".join(a.name for a in node.names)
                    hits.append(f"{rel}:{node.lineno}: from {node.module} import {names}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in self_names:
                        hits.append(f"{rel}:{node.lineno}: import {alias.name}")
    return hits


def test_b1_no_absolute_self_imports_in_plugin():
    """B1（验收 2/3）：当前仓库源码中指向插件自身包的绝对导入 = 0 处。"""
    hits = scan_absolute_self_imports(WORKDIR)
    assert hits == [], f"插件源码出现绝对自导入（生产包形态下必失败）: {hits}"


def test_b2_scanner_catches_absolute_import_sample(tmp_path):
    """B2（验收 4）：同一扫描器对含绝对导入的样本必须报出 文件:行号——
    证明守护测试不是永远绿的摆设。"""
    (tmp_path / "core").mkdir()
    (tmp_path / "main.py").write_text(
        "from core.negotiator import Negotiator\n", encoding="utf-8")
    (tmp_path / "core" / "x.py").write_text(
        "import core.judge\n"
        "from main import LivingPlugin\n"
        "from .peer import ok\n"
        "import json\n",
        encoding="utf-8",
    )
    hits = scan_absolute_self_imports(tmp_path)
    assert any(h.startswith("main.py:1:") and "core.negotiator" in h for h in hits), (
        f"样本 main.py:1 的绝对导入未被报出: {hits}"
    )
    assert any(h.startswith("core/x.py:1:") and "core.judge" in h for h in hits)
    assert any(h.startswith("core/x.py:2:") and "from main" in h for h in hits)
    # 相对导入与标准库不误报
    assert not any(".peer" in h for h in hits)
    assert not any(h.endswith("import json") for h in hits)


# ===========================================================================
# 第一批验收 1：包形态加载后 _negotiator() 不抛（子进程模拟生产）
# ===========================================================================
def test_v1_negotiator_works_under_package_form(tmp_path):
    """验收 1：模拟 AstrBot 生产加载形态（data.plugins.<插件目录>.main）
    加载真实 main.py——_negotiator() 惰性构造成功且走缓存；同环境下
    旧写法（绝对导入 core）必 ModuleNotFoundError（修前形态的对照）。"""
    pkg_root = tmp_path / "data" / "plugins"
    dest = pkg_root / "astrbot_plugin_living"
    dest.mkdir(parents=True)
    shutil.copy2(WORKDIR / "main.py", dest / "main.py")
    shutil.copytree(
        WORKDIR / "core", dest / "core",
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    # 子进程 sys.path：保留 astrbot 源码树与解释器默认路径，剔除插件
    # 工作目录——后者正是测试形态与生产形态的分歧点。插入 tmp 根
    # （data 的父目录）：AstrBot 的 data/ 本就是 namespace package，
    # 与这里的 tmp/data 合并解析，与生产加载形态一致
    clean_path = [
        p for p in sys.path
        if p and Path(p).resolve() != WORKDIR.resolve()
    ]
    probe = (
        "import sys\n"
        f"sys.path[:] = {clean_path!r}\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "import data.plugins.astrbot_plugin_living.main as mod\n"
        "p = object.__new__(mod.LivingPlugin)\n"
        "p._judge = None\n"
        "p._negotiator_chat_call = None\n"
        "p._chat_prefix_for_umo = lambda umo: ''\n"
        "n1 = p._negotiator()\n"
        "n2 = p._negotiator()\n"
        "assert n1 is n2, '二次调用必须走缓存（惰性构造语义不变）'\n"
        "print('NEGOTIATOR_OK:' + type(n1).__module__ + '.' + type(n1).__name__)\n"
        "try:\n"
        "    import core.negotiator\n"
        "except ModuleNotFoundError as e:\n"
        "    print('ABS_IMPORT_FAILS:' + str(e.name))\n"
        "else:\n"
        "    print('ABS_IMPORT_UNEXPECTEDLY_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120, encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"包形态加载失败（协商器导入仍坏？）:\n{proc.stderr[-1500:]}"
    )
    lines = [
        ln for ln in proc.stdout.splitlines()
        if ln.startswith(("NEGOTIATOR_OK:", "ABS_IMPORT_"))
    ]
    # 相对导入在包形态下解析到插件自己的 negotiator（任务书 §2.2 复现
    # 的成功输出同形态）
    assert any(
        ln == "NEGOTIATOR_OK:data.plugins.astrbot_plugin_living.core.negotiator.Negotiator"
        for ln in lines
    ), f"协商器包形态构造输出不符: {lines}"
    # 对照：旧绝对写法在无 sys.path 特权的同一环境必失败
    assert "ABS_IMPORT_FAILS:core" in lines, (
        f"绝对导入在包形态环境竟然可用（对照失效）: {lines}"
    )


# ===========================================================================
# 第一批 B3/B4（验收 5/6）：钩子外层异常分级 + 取消语义
# ===========================================================================
REPLY = "这是一条用于输出侧检查测试的普通回复，长度超过八字。"


class _FakeResponse:
    def __init__(self, text):
        self.completion_text = text
        self.is_chunk = False


def _hook_plugin(tmp_path):
    """negotiate 档钩子测试替身：真实 LivingPlugin 实例 + api 档 judge。"""
    import importlib

    pkg_name = "living_plugin_under_test_m38"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(WORKDIR)]
        sys.modules[pkg_name] = pkg
        import core as core_pkg

        sys.modules[f"{pkg_name}.core"] = core_pkg
        for name, mod in list(sys.modules.items()):
            if name == "core" or name.startswith("core."):
                sys.modules.setdefault(f"{pkg_name}.{name}", mod)
    main_module = importlib.import_module(f"{pkg_name}.main")

    from core.judge import OutputJudge

    plugin = object.__new__(main_module.LivingPlugin)
    seed = {"preset": {}, "advanced": {}}
    cfg_path = tmp_path / "astrbot_plugin_living_config.json"
    cfg_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    plugin._plugin_config_path = lambda: str(cfg_path)
    plugin.config = seed
    plugin.context = types.SimpleNamespace()
    jcfg = {"mode": "api", "provider_id": "p-judge", "output_action": "negotiate"}
    async def _no_llm(prompt, system_prompt=None):
        return '{"ok": true}'  # 仅让 enabled() 成立，本文件不真调模型

    plugin._judge = OutputJudge(
        config_getter=lambda: {"advanced": {"judge": jcfg}},
        llm_call=_no_llm,
        records_path=tmp_path / "judge_records.json",
    )
    plugin._judge_tasks = set()
    plugin._chat_prefix_cache = {}
    plugin.sender = types.SimpleNamespace(send=None)
    plugin.loop = None
    plugin.gate = None
    plugin.sleep_manager = None
    event = types.SimpleNamespace(unified_msg_origin="aiocqhttp:FriendMessage:10001")
    return plugin, event


class _CaptureHandler(logging.Handler):
    """直接挂到 astrbot logger 的收集器。

    astrbot 根 logger propagate=False（自带 handler 输出），且 pytest
    caplog 对非传播 logger 的挂载依赖时序（loggerDict 快照晚于 astrbot
    运行时初始化时抓不到）——挂在本层最稳：无论 _PluginContextLogger
    解析到 astrbot 本身还是其子 logger，记录沿途都会调用本层 handler。
    """

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_v5_output_qc_failure_escalates_to_warning(tmp_path):
    """验收 5：_negotiator() 抛非 Timeout 异常 → WARNING 级 + 异常类型名；
    回复内容不被改动、钩子不抛出。"""
    plugin, event = _hook_plugin(tmp_path)

    def _boom():
        raise RuntimeError("模拟配置/部署级硬错")

    plugin._negotiator = _boom  # 实例属性遮蔽，模拟惰性构造抛错
    resp = _FakeResponse(REPLY)
    cap = _CaptureHandler()
    astrbot_logger = logging.getLogger("astrbot")
    astrbot_logger.addHandler(cap)
    try:
        asyncio.run(plugin.judge_output_on_llm_response(event, resp))
    finally:
        astrbot_logger.removeHandler(cap)
    assert resp.completion_text == REPLY  # 回复原样
    records = [r for r in cap.records if "输出检查失败" in r.getMessage()]
    assert records, "外层异常未留下日志"
    rec = records[-1]
    assert rec.levelname == "WARNING", f"应为 WARNING，实际 {rec.levelname}"
    assert "RuntimeError" in rec.getMessage(), "日志须带异常类型名"


def test_v6_cancelled_error_propagates(tmp_path):
    """验收 6（B4）：asyncio.CancelledError 继承 BaseException，不经
    except Exception 分支——原样上抛，不被吞。"""
    plugin, event = _hook_plugin(tmp_path)

    def _cancel():
        raise asyncio.CancelledError()

    plugin._negotiator = _cancel
    resp = _FakeResponse(REPLY)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin.judge_output_on_llm_response(event, resp))


# ===========================================================================
# 第二批 A 组（验收 1-4、6）：runnableActivities 换真实活动池
# ===========================================================================
@NODE_SKIP
def test_pool1_default_six_activities_including_peek_reminisce():
    """验收 1：默认配置（搜索开、free 开）→ 六个活动，peek/reminisce 在。"""
    assert pool_names(
        agentActivities=DEFAULT_AGENT_ACTIVITIES,
        freeEnabled=True, webSearchEnabled=True,
    ) == ["surf", "read", "game", "peek", "reminisce", "free"]


@NODE_SKIP
def test_pool2_search_off_keeps_peek_reminisce():
    """验收 2：搜索关闭 → 摘掉 surf/read，剩 4 个，peek/reminisce 仍在。"""
    assert pool_names(
        agentActivities=DEFAULT_AGENT_ACTIVITIES,
        freeEnabled=True, webSearchEnabled=False,
    ) == ["game", "peek", "reminisce", "free"]


@NODE_SKIP
def test_pool3_free_off_keeps_peek_reminisce():
    """验收 3：free 关 → 摘掉 free，peek/reminisce 仍在。"""
    assert pool_names(
        agentActivities=DEFAULT_AGENT_ACTIVITIES,
        freeEnabled=False, webSearchEnabled=True,
    ) == ["surf", "read", "game", "peek", "reminisce"]


@NODE_SKIP
def test_pool4_search_off_and_free_off():
    """验收 4：搜索关 + free 关 → 只剩 game / peek / reminisce。"""
    assert pool_names(
        agentActivities=DEFAULT_AGENT_ACTIVITIES,
        freeEnabled=False, webSearchEnabled=False,
    ) == ["game", "peek", "reminisce"]


@NODE_SKIP
def test_pool6_mode_annotation():
    """验收 6：形态标注——勾选的 surf 可自主；未勾的 game 固定脚本；
    peek / reminisce 无论是否勾选永远固定脚本。"""
    chosen = ["surf"]
    got = {
        a["name"]: a["agent"]
        for a in run_js("runnableActivities",
                        agentActivities=chosen, freeEnabled=True,
                        webSearchEnabled=True)
    }
    assert got["surf"] is True, "勾选且支持自主 → 可自主发挥"
    assert got["game"] is False, "支持自主但未勾选 → 固定脚本"
    assert got["free"] is False
    # 勾选 peek / reminisce 也不会标成自主（不支持自主模式）
    forced = {
        a["name"]: a["agent"]
        for a in run_js("runnableActivities",
                        agentActivities=["peek", "reminisce"],
                        freeEnabled=True, webSearchEnabled=True)
    }
    assert forced["peek"] is False and forced["reminisce"] is False


@NODE_SKIP
def test_pool_mirrors_backend_activities():
    """验收 8（同源锚点）：前端活动池/可自主名单与 core/activities.py 的
    default_activities / supports_agent 声明逐一对齐。"""
    from core.activities import default_activities

    pool = default_activities()
    assert run_js("activityPool") == [a.name for a in pool]
    capable = sorted(a.name for a in pool if a.supports_agent)
    assert capable == ["free", "game", "read", "surf"], (
        f"supports_agent 声明被改动（应为四 True）: {capable}"
    )
    all_on = run_js(
        "runnableActivities",
        agentActivities=[a.name for a in pool],
        freeEnabled=True, webSearchEnabled=True,
    )
    assert sorted(x["name"] for x in all_on if x["agent"]) == capable


def test_backend_semantics_untouched():
    """验收 8（红线）：运行逻辑零改动——活动池名单、free 摘除行为、
    schema 默认值逐一锚定。"""
    from core.activities import default_activities

    assert [a.name for a in default_activities()] == [
        "surf", "read", "game", "peek", "reminisce", "free",
    ]
    assert [a.name for a in default_activities(enabled_free=False)] == [
        "surf", "read", "game", "peek", "reminisce",
    ]
    item = SCHEMA["advanced"]["items"]["decision"]["items"]["agent_activities"]
    assert item["default"] == DEFAULT_AGENT_ACTIVITIES
    assert item["type"] == "list"


# ===========================================================================
# 第二批 B/C 组（验收 5、7）：多选收窄 + 残留值兼容（DOM 级全链路）
# ===========================================================================
@NODE_SKIP
def test_dom_choices_four_modes_note_and_residual_kept():
    """验收 5 + 7：渲染真实面板——选项只有 4 个（无 peek/reminisce）、
    语义说明行在位；配置残留 peek 不崩、不显示为选项，勾选保存时
    其它值与残留值都不被误删。"""
    payload = build_payload(advanced={
        "decision": {
            "agent_activities": ["surf", "game", "peek"],  # peek 为残留值
            "free_activity_enabled": True,
        },
        "capabilities": {"web_search_enabled": True},
    })
    data = run_harness({"op": "activityProbe", "payload": payload})
    assert data["found"] is True
    assert data["loadError"] == "", f"残留 peek 导致面板报错: {data['loadError']}"

    labels = data["labels"]
    assert len(labels) == 4, f"多选选项应为 4 个，实际: {labels}"
    assert not any("peek" in x for x in labels), "peek 不得作为选项出现"
    assert not any("reminisce" in x for x in labels), "reminisce 不得作为选项出现"
    assert data["checkedBefore"] == ["surf 上网冲浪", "game 写小游戏"]

    # B2：语义说明行讲清"不勾≠不做"
    assert data["note"] and "不勾" in data["note"] and "照常" in data["note"]

    # A3：C3 卡片六活动全列 + 形态标注（surf/game 勾选→可自主，其余脚本）
    note = data["poolNote"]
    assert "surf（可自主发挥）" in note
    assert "game（可自主发挥）" in note
    assert "read（固定脚本）" in note
    assert "peek（固定脚本）" in note
    assert "reminisce（固定脚本）" in note
    assert "free（固定脚本）" in note

    # C1：勾选 read 后保存——数组形态，surf/game/read 与残留 peek 全保留
    posts = [p for p in data["savePosts"] if p["endpoint"] == "config"]
    assert posts, "保存请求未被捕获"
    saved = posts[0]["body"]["advanced"]["decision"]["agent_activities"]
    assert saved == ["surf", "read", "game", "peek"], (
        f"保存契约被破坏（残留值应保留）: {saved!r}"
    )


@NODE_SKIP
def test_dom_pool_note_without_agent_config():
    """agent_activities 未配置（undefined）也不崩：六活动全固定脚本形态。"""
    payload = build_payload(advanced={
        "decision": {"free_activity_enabled": True},
        "capabilities": {"web_search_enabled": True},
    })
    data = run_harness({"op": "activityProbe", "payload": payload})
    assert data["found"] is True and data["loadError"] == ""
    note = data["poolNote"]
    for name in ("surf", "read", "game", "peek", "reminisce", "free"):
        assert f"{name}（固定脚本）" in note


def test_help_and_schema_copy_updated():
    """B3/C2 文案落位：schema hint 点明"不勾≠不做"；说明书补活动形态
    一句；C3 组摘要不再引用过时行号。"""
    item = SCHEMA["advanced"]["items"]["decision"]["items"]["agent_activities"]
    assert "照常进行" in item["hint"] and "固定流程" in item["hint"]
    assert "始终在活动池" in item["hint"]

    help_js = (WORKDIR / "pages" / "config" / "help-content.js").read_text(
        encoding="utf-8")
    assert "两种情况这个活动都会发生" in help_js
    assert "翻回忆和看留言只有固定做法" in help_js

    c3 = next(
        g for s in LAYOUT["sections"] if s["id"] == "C" for g in s["groups"]
        if g["id"] == "C3"
    )
    assert "任何情况下都在" in c3["summary"]
