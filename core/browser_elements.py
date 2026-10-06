"""浏览器可点元素清单（任务书 M20-补丁1 N 组）。

断点：browser_read 只返回正文文本，而 browser_click/browser_type 需要
CSS 选择器——她从画面/正文推不出选择器，"点进链接"实际做不到。本模块
让 browser_read 同时返回"页面上可交互元素清单"：每条含元素类型、给模型
看的文字、可直接用于 browser_click / browser_type 的选择器。

选择器生成策略（N3：稳定且可复用；优先级从高到低）：
1. ``#id``（有非空 id 且合法时——同一元素连续两次生成必然一致）；
2. ``a[href="..."]``（链接的 href 是页面自身语义，结构微调也不易失效）；
3. ``<tag>[name="..."]``（表单元素的 name）；
4. ``<tag>[placeholder="..."]``（输入框占位符）；
5. ``body>…>tag:nth-of-type(k)`` 路径（由浏览器端逐层算好返回——同一
   DOM 下逐字一致；页面结构变化会失效 → browser_click 返回明确错误，
   她据此重新 browser_read 获取最新清单，不会静默失败）。

安全与边界（N6）：只列**可见且可交互**的元素（不可见/disabled 一律
不列）；上限 50 条（N1：避免几百个链接塞爆上下文），超出时优先保留
正文区域（main/article 等容器内）的元素、剔除导航/页脚类；危险动作
（提交/评论等）仍由既有 action_kind + write_level 权限层把关——本清单
只是"看到"，不绕过任何权限。

实现说明：JS 只负责**采集原始信息**（tag/id/href/name/文本/路径/是否
正文区），选择器的挑选与转义全部在 Python 端完成——逻辑可脱离浏览器
单测（测试环境没有 Playwright）。
"""

from __future__ import annotations

import re
from typing import Any

# 采集上限（浏览器端），Python 端最终裁到 ELEMENTS_CAP
_COLLECT_LIMIT = 60

# 最终返回给模型的清单上限（N1 建议 ≤50）
ELEMENTS_CAP = 50

# 采集脚本：可见 + 可交互（链接/按钮/输入框/下拉），返回原始信息清单。
# nth-of-type 路径逐层计算，最多 8 层，超深元素放弃（避免不可用的超长选择器）。
ELEMENTS_JS = """
() => {
  const LIMIT = %LIMIT%;
  function visible(el) {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return el.getClientRects().length > 0;
  }
  function path(el) {
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.tagName && node.tagName.toLowerCase() !== 'body'
           && depth < 8) {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      let index = 1;
      if (parent) {
        let same = 0;
        for (const child of parent.children) {
          if (child.tagName === node.tagName) {
            same += 1;
            if (child === node) index = same;
          }
        }
        parts.unshift(tag + ':nth-of-type(' + index + ')');
      } else {
        parts.unshift(tag);
      }
      node = parent;
      depth += 1;
    }
    return 'body>' + parts.join('>');
  }
  const nodes = Array.from(document.querySelectorAll(
    'a[href], button, input, textarea, select, [role="button"], [role="link"]'
  ));
  const out = [];
  for (const el of nodes) {
    if (el.disabled) continue;
    if (!visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    let kind = 'button';
    let label = '';
    if (tag === 'a') {
      kind = 'link';
      label = (el.innerText || '').trim();
      const href = el.getAttribute('href') || '';
      if (!href || href.startsWith('javascript:')) continue;
    } else if (tag === 'input') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'hidden') continue;
      kind = (type === 'submit' || type === 'button') ? 'button' : 'input';
      label = el.placeholder || el.value || el.getAttribute('aria-label') || '';
      if (kind === 'input' && !label && !el.getAttribute('name')) continue;
    } else if (tag === 'textarea') {
      kind = 'input';
      label = el.placeholder || el.getAttribute('aria-label') || '';
    } else if (tag === 'select') {
      kind = 'select';
      label = el.getAttribute('aria-label') || el.getAttribute('name') || '';
    } else {
      kind = 'button';
      label = (el.innerText || el.getAttribute('aria-label') || '').trim();
    }
    if ((kind === 'link' || kind === 'button') && !label) continue;
    out.push({
      kind: kind,
      tag: tag,
      label: label.slice(0, 60),
      id: el.id || '',
      href: tag === 'a' ? (el.getAttribute('href') || '') : '',
      name: el.getAttribute('name') || '',
      placeholder: el.placeholder || '',
      path: path(el),
      inMain: !el.closest('nav, header, footer, aside'),
    });
  }
  // 正文区优先（N1：超出时优先正文区域）；Array.sort 稳定，组内保持 DOM 顺序
  out.sort((a, b) => (b.inMain ? 1 : 0) - (a.inMain ? 1 : 0));
  return out.slice(0, LIMIT);
}
""".replace("%LIMIT%", str(_COLLECT_LIMIT))

_KIND_LABELS = {
    "link": "链接",
    "button": "按钮",
    "input": "输入框",
    "select": "下拉框",
}

_ID_SAFE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _css_quote(value: str) -> str:
    """属性值转义（双引号与反斜杠）——保证生成的选择器语法合法。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_selector(info: dict) -> str:
    """由采集信息生成稳定 CSS 选择器（N3，优先级见模块 docstring）。"""
    tag = str(info.get("tag") or "a").lower()
    el_id = str(info.get("id") or "").strip()
    if el_id and _ID_SAFE_RE.match(el_id):
        return f"#{el_id}"
    if el_id:
        return f'{tag}[id="{_css_quote(el_id)}"]'
    href = str(info.get("href") or "").strip()
    if tag == "a" and href and not href.startswith(("#", "javascript:")):
        return f'a[href="{_css_quote(href)}"]'
    name = str(info.get("name") or "").strip()
    if name:
        return f'{tag}[name="{_css_quote(name)}"]'
    placeholder = str(info.get("placeholder") or "").strip()
    if placeholder and tag in ("input", "textarea"):
        return f'{tag}[placeholder="{_css_quote(placeholder)}"]'
    path = str(info.get("path") or "").strip()
    return path or tag


def format_elements_block(infos: list[dict]) -> str:
    """把元素信息清单格式化成模型可读文本（含可点选择器，N1/N2）。"""
    if not infos:
        return ""
    lines = ["可交互元素（选择器可直接用于 browser_click / browser_type）："]
    for i, info in enumerate(infos[:ELEMENTS_CAP], 1):
        selector = build_selector(info)
        kind = _KIND_LABELS.get(str(info.get("kind")), str(info.get("kind") or "元素"))
        label = str(info.get("label") or "").strip() or "（无文字）"
        lines.append(f"{i}. [{kind}] {label} → {selector}")
    return "\n".join(lines)


async def collect_page_elements(page: Any) -> str:
    """在当前页面上采集可交互元素并格式化（browser_read 调用）。

    采集失败返回空串——元素清单是锦上添花，绝不能让 browser_read 本身
    失败（红线：不拖慢不阻断）。"""
    try:
        raw = await page.evaluate(ELEMENTS_JS)
    except Exception:
        return ""
    if not isinstance(raw, list):
        return ""
    infos = [item for item in raw if isinstance(item, dict)]
    return format_elements_block(infos)
