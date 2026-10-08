"""M28-补丁1 测试：面板内置说明书 + README 重写 + 版本号启更。

断言方式与 M26/M27 同源：node 桥 + 最小 DOM 桩让**真实 app.js**完成
说明书的渲染与开关路径（非源码正则）；源码锚点与文档覆盖断言为补充。
版本一致性在 test_version_consistency.py（本文件不重复）。
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

WORKDIR = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((WORKDIR / "_conf_schema.json").read_text(encoding="utf-8"))
LAYOUT = json.loads((WORKDIR / "panel_layout.json").read_text(encoding="utf-8"))
APP_JS = (WORKDIR / "pages" / "config" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (WORKDIR / "pages" / "config" / "index.html").read_text(encoding="utf-8")
HELP_JS = (WORKDIR / "pages" / "config" / "help-content.js").read_text(encoding="utf-8")
STYLE_CSS = (WORKDIR / "pages" / "config" / "style.css").read_text(encoding="utf-8")
README = (WORKDIR / "README.md").read_text(encoding="utf-8")
README_EN = (WORKDIR / "README_EN.md").read_text(encoding="utf-8")
CHANGELOG = (WORKDIR / "CHANGELOG.md").read_text(encoding="utf-8")
METADATA = (WORKDIR / "metadata.yaml").read_text(encoding="utf-8")

HARNESS = WORKDIR / "tests" / "js" / "status_engine_harness.mjs"
HAS_NODE = __import__("shutil").which("node") is not None
NODE_SKIP = pytest.mark.skipif(not HAS_NODE, reason="node 不可用（DOM 桩渲染测试）")


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


def run_harness(req, timeout=60):
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps(req),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8",
    )
    assert proc.returncode == 0, f"node 桥失败: {proc.stderr[-800:]}"
    return json.loads(proc.stdout)


_HELP_DATA = None


def help_probe():
    """helpProbe 只跑一次（冒烟/关闭路径/转义共用同一份结果）。"""
    global _HELP_DATA
    if _HELP_DATA is None:
        _HELP_DATA = run_harness({"op": "helpProbe", "payload": build_payload()})
    return _HELP_DATA


# ---------------------------------------------------------------------------
# 验收 2：说明书渲染冒烟（#help-body 有内容；.help-section 数 == LIVING_HELP 节数）
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc2_help_modal_renders_all_sections():
    data = help_probe()
    assert data["loadError"] == ""
    assert data["initial"]["hidden"] is True, "弹层默认应收起"
    assert data["initial"]["bodyChildren"] == 0, "未打开时正文应为空（惰性渲染）"
    after = data["afterOpen"]
    assert after["hidden"] is False, "点「📖 说明书」后弹层应展开"
    assert after["bodyChildren"] > 0, "#help-body 渲染后必须有子节点"
    assert after["sections"] == after["livingHelpLength"] > 0, (
        f".help-section 数（{after['sections']}）应等于 LIVING_HELP 节数"
        f"（{after['livingHelpLength']}）"
    )
    # 2.1.3 的 10 个主题全部在（可合并不得缺主题）
    titles = "\n".join(after["titles"])
    for theme in [
        "这是什么", "三分钟上手", "它会自己做什么", "它什么时候会找你说话",
        "你最可能改的开关", "它能碰到什么", "它学你说话", "小大脑",
        "出问题先看这里", "名词小抄",
    ]:
        assert theme in titles, f"说明书缺主题：{theme}"


# ---------------------------------------------------------------------------
# 验收 2（续）+ 配套 c：三种关闭方式（关闭按钮 / 遮罩本体 / Escape 仅开着时）
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc2_help_modal_three_close_paths():
    data = help_probe()
    assert data["afterCloseBtn"]["hidden"] is True, "#help-close 点击应关闭"
    # 点在卡片内容器上不关（e.target !== e.currentTarget 守卫）
    assert data["maskClickOnCard"]["hidden"] is False, "点卡片不应关闭"
    # 点在遮罩本体上关
    assert data["maskClickOnMask"]["hidden"] is True, "点遮罩本体应关闭"
    # Escape：开着时关；已关时保持关（守卫生效、不抛错）
    assert data["escapeWhenOpen"]["hidden"] is True
    assert data["escapeWhenClosed"]["hidden"] is True


# ---------------------------------------------------------------------------
# 验收 3：转义——含 <script>/& 的样例节以文本节点字面出现，无可执行标签
# ---------------------------------------------------------------------------
@NODE_SKIP
def test_acc3_help_content_is_escaped():
    data = help_probe()
    esc = data["escape"]
    assert esc["scriptTags"] == 0, "渲染结果里不得出现 SCRIPT 元素"
    assert esc["asTextNode"] is True, "样例文本应以文本节点形式出现"
    assert esc["literalText"] is True, "含 <script>/& 的文本必须按字面显示"
    assert esc["sectionsAfterPush"] == data["afterOpen"]["sections"] + 1


# ---------------------------------------------------------------------------
# 验收 4：源码锚点（补充——DOM 行为断言已在上方完成）
# ---------------------------------------------------------------------------
def test_acc4_source_anchors():
    assert 'id="btn-help"' in INDEX_HTML
    assert 'id="help-mask"' in INDEX_HTML
    assert 'id="help-close"' in INDEX_HTML
    assert 'id="help-body"' in INDEX_HTML
    assert ">📖 说明书</button>" in INDEX_HTML
    assert 'role="dialog"' in INDEX_HTML and 'aria-modal="true"' in INDEX_HTML
    # app.js：开关绑定与 Escape 处理
    assert 'import { LIVING_HELP } from "./help-content.js"' in APP_JS
    assert "function renderHelpModal()" in APP_JS
    assert "function openHelpModal()" in APP_JS
    assert "function closeHelpModal()" in APP_JS
    assert '$("#btn-help").addEventListener("click", openHelpModal)' in APP_JS
    assert '$("#help-close").addEventListener("click", closeHelpModal)' in APP_JS
    assert "e.target === e.currentTarget" in APP_JS
    assert 'document.addEventListener("keydown"' in APP_JS
    assert '"Escape"' in APP_JS
    # help-content.js：纯数据导出
    assert "export const LIVING_HELP" in HELP_JS
    # 样式：变量全部取自 living :root（配套 d）
    for var in ("--panel", "--panel-2", "--border", "--text", "--muted",
                "--accent", "--radius"):
        assert var in STYLE_CSS, f"style.css 缺少 {var}（不应引用不存在的变量）"
    root_block = STYLE_CSS.split("}", 1)[0]
    for use in re.findall(r"var\((--[a-z0-9-]+)", STYLE_CSS):
        assert use in root_block, f".help-* 引用了 :root 未定义的变量 {use}"


# ---------------------------------------------------------------------------
# 红线 4：中性化——说明书 / README / README_EN / CHANGELOG 无禁用词、无性别代词
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", [
    "pages/config/help-content.js", "README.md", "README_EN.md", "CHANGELOG.md",
])
def test_docs_no_forbidden_words(name):
    text = (WORKDIR / name).read_text(encoding="utf-8")
    for word in ["主人", "凛", "zcode", "女仆", "妹妹"]:
        assert word not in text, f"{name} 出现禁用词：{word}"
    assert "她" not in text, f"{name} 出现性别代词「她」"


# ---------------------------------------------------------------------------
# C 组：版本启更（与 test_version_consistency.py 互补：这里钉 1.0.0 下限、
# CHANGELOG 结构、panel_layout 的结构版本语义不被混改）
# ---------------------------------------------------------------------------
def test_metadata_version_is_at_least_1_0_0():
    m = re.search(r"^version:\s*(\S+)", METADATA, re.M)
    assert m, "metadata.yaml 缺 version"
    parts = tuple(int(p) for p in m.group(1).split("."))
    assert parts >= (1, 0, 0), f"版本号应 ≥ 1.0.0（本批启更），实际 {m.group(1)}"


def test_changelog_has_1_0_0_entry_with_categories():
    assert "## [1.0.0]" in CHANGELOG
    for cat in ("Added", "Changed", "Fixed", "Docs"):
        assert f"### {cat}" in CHANGELOG, f"CHANGELOG 缺 {cat} 分类"
    # Keep a Changelog 风格头
    assert "Keep a Changelog" in CHANGELOG


def test_panel_layout_version_stays_layout_semantics():
    # 布局结构版本是独立语义（M25-补丁1），本批启更不得混改
    assert LAYOUT["version"] == 1


# ---------------------------------------------------------------------------
# 验收 5：README 覆盖 2.2.1 的 15 个主题（弱化为标题/关键词断言）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("theme", [
    "这是什么", "快速开始", "它怎么运作", "能力档位与写权限", "风格学习",
    "判断模型", "模型配置与缓存保护", "命令速查", "配置说明", "浏览器能力",
    "工作区", "面板说明", "已知限制", "开发", "版本与更新日志",
])
def test_acc5_readme_covers_all_themes(theme):
    assert theme in README, f"README 缺主题：{theme}"


def test_readme_content_truthfulness_anchors():
    # 关键功能断言必须与代码事实一致（防止重写时臆造）
    assert "0.3.0" not in README, "README 不应残留旧版本号"
    for kw in ["看留言", "写权限", "生效链", "前缀对齐", "playwright install chromium",
               "living_wake", "judge.provider_id", "prefix_cache_ttl_minutes"]:
        assert kw in README, f"README 缺关键事实：{kw}"


# ---------------------------------------------------------------------------
# 配套 a：README_EN 同结构（标题数对齐 + 覆盖功能与安装 + 滞后声明允许）
# ---------------------------------------------------------------------------
def test_readme_en_same_structure():
    zh_heads = re.findall(r"^## .+$", README, re.M)
    en_heads = re.findall(r"^## .+$", README_EN, re.M)
    assert len(zh_heads) == len(en_heads) >= 12, (
        f"中英版二级标题数应一致：zh={len(zh_heads)} en={len(en_heads)}"
    )
    for kw in ("Install", "Configuration", "Command", "License"):
        assert kw.lower() in README_EN.lower(), f"README_EN 缺章节：{kw}"


# ---------------------------------------------------------------------------
# D 组：主链路零改动的守卫（这些符号必须原样在位——说明书不与渲染/保存交集）
# ---------------------------------------------------------------------------
def test_main_pipeline_untouched_guards():
    for anchor in [
        "function buildSavePayload", "function diffSection",
        "KNOB_MAPPED_KEYS", "function renderNovice", "function renderExpert",
        "async function save", "async function load",
    ]:
        assert anchor in APP_JS, f"app.js 主链路锚点缺失：{anchor}"
