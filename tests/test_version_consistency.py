"""版本号一致性（M28-补丁1 C 组）。

事实源契约：插件版本号只有 metadata.yaml 一处事实源；README 的版本徽章
必须与它一致（徽章形如 `![version](https://img.shields.io/badge/version-X.Y.Z-blue)`）。
注意：panel_layout.json 里的 "version" 是布局结构版本（M25-补丁1），语义
不同，本测试不读它，它也不得随插件版本变动。

修前必红：本测试先于 README 徽章存在——README 还没有徽章时必须失败。
"""

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
METADATA_PATH = PLUGIN_ROOT / "metadata.yaml"
README_PATH = PLUGIN_ROOT / "README.md"

_BADGE_RE = re.compile(
    r"img\.shields\.io/badge/version-([0-9]+\.[0-9]+\.[0-9]+)-"
)


def _metadata_version() -> str:
    """从 metadata.yaml 读 version（只做行级解析，不引 yaml 依赖）。"""
    for line in METADATA_PATH.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^version:\s*(\S+)\s*$", line)
        if m:
            return m.group(1)
    raise AssertionError("metadata.yaml 里没有 version 行")


def _readme_badge_version() -> str | None:
    """从 README.md 的版本徽章里提取版本号；无徽章返回 None。"""
    m = _BADGE_RE.search(README_PATH.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def test_readme_has_version_badge():
    assert _readme_badge_version() is not None, (
        "README.md 缺少版本徽章（img.shields.io/badge/version-…）"
    )


def test_readme_badge_matches_metadata_version():
    meta = _metadata_version()
    badge = _readme_badge_version()
    assert badge == meta, (
        f"README 版本徽章（{badge}）与 metadata.yaml 的 version（{meta}）不一致"
    )


def test_metadata_version_is_single_source_semver():
    # 版本号语义守卫：metadata.yaml 的 version 必须是 x.y.z 形态，
    # panel_layout.json 的 "version"（布局结构版本）不参与本契约。
    meta = _metadata_version()
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", meta), (
        f"metadata.yaml version 不是 x.y.z 形态：{meta!r}"
    )
    layout = (PLUGIN_ROOT / "panel_layout.json").read_text(encoding="utf-8")
    m = re.search(r'"version"\s*:\s*([0-9]+)', layout)
    assert m, "panel_layout.json 缺少布局结构版本（不应被移除）"
