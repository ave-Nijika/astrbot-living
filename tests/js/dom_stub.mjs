/* M26-补丁1 最小 DOM 桩：让真实 pages/config/app.js 在 node 里完成主渲染
 * 路径（createElement/append/classList/textContent/事件注册），供 pytest 经
 * harness 做 DOM 装配级断言。零依赖（不引 jsdom）；只实现 app.js 主渲染
 * 用到的 API 面——点击处理器等交互路径不在桩上执行。 */

export class StubTextNode {
  constructor(text) {
    this.kind = "#text";
    this.text = String(text ?? "");
  }
}

export class StubElement {
  constructor(tagName) {
    this.kind = "element";
    this.tagName = String(tagName || "div").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._cls = new Set();
    this._text = "";
    this._html = "";
    this.dataset = {};
    this._listeners = [];
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.title = "";
  }

  get className() {
    return [...this._cls].join(" ");
  }

  set className(v) {
    this._cls = new Set(String(v ?? "").split(/\s+/).filter(Boolean));
  }

  get classList() {
    const self = this;
    return {
      add: (...cs) => cs.forEach((c) => self._cls.add(c)),
      remove: (...cs) => cs.forEach((c) => self._cls.delete(c)),
      toggle: (c, force) => {
        const want = force === undefined ? !self._cls.has(c) : !!force;
        if (want) self._cls.add(c); else self._cls.delete(c);
        return want;
      },
      contains: (c) => self._cls.has(c),
    };
  }

  get textContent() {
    // 与真 DOM 一致：递归拼接所有后代文本（textContent setter 创建文本子节点，
    // 之后 prepend 的徽章等元素会拼在前面——buildKeyRow 的危险/联动徽章即此形态）
    if (this.children.length) {
      return this.children.map((c) => c.kind === "#text" ? c.text : c.textContent).join("");
    }
    return this._text;
  }

  set textContent(v) {
    // 与真 DOM 一致：赋值创建文本子节点（不清成"裸 _text"——否则之后
    // prepend 的子元素会让 getter 丢掉这段文本）
    this.children = [];
    this._text = "";
    const t = new StubTextNode(String(v ?? ""));
    t.parentNode = this;
    this.children.push(t);
  }

  get innerHTML() {
    return this._html;
  }

  set innerHTML(v) {
    // 桩不解析 HTML：赋非空串（如 renderExpertFlat 的组头模板）只存原文并清空子树
    this.children = [];
    this._html = String(v ?? "");
    this._text = "";
  }

  appendChild(node) {
    this.children.push(node);
    if (node) node.parentNode = this;
    return node;
  }

  append(...nodes) {
    for (const n of nodes) this.appendChild(n);
  }

  prepend(...nodes) {
    for (const n of nodes.reverse()) {
      this.children.unshift(n);
      if (n) n.parentNode = this;
    }
  }

  insertBefore(newNode, ref) {
    const idx = ref ? this.children.indexOf(ref) : -1;
    if (idx < 0) this.children.push(newNode);
    else this.children.splice(idx, 0, newNode);
    if (newNode) newNode.parentNode = this;
    return newNode;
  }

  remove() {
    if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
    }
  }

  addEventListener(type, fn) {
    this._listeners.push([type, fn]); // 桩不触发事件：主渲染路径不需要
  }

  /* 有限选择器：仅支持 ".cls"（递归后代查找，不含自身）——主渲染路径
   * 的 querySelector(All) 都是单类查询（如 grid.querySelectorAll(".knob-card")） */
  _matchesSelector(sel) {
    const m = /^\.([A-Za-z0-9_-]+)$/.exec(String(sel || ""));
    return !!m && this._cls.has(m[1]);
  }

  querySelectorAll(sel) {
    const out = [];
    for (const c of this.children || []) {
      const sub = findAllIn(c, (n) => n._matchesSelector(sel));
      out.push(...sub);
    }
    return out;
  }

  querySelector(sel) {
    return this.querySelectorAll(sel)[0] ?? null;
  }

  focus() { /* no-op */ }
  blur() { /* no-op */ }
  removeAttribute() { /* no-op */ }
  setAttribute(name, v) { this[name] = v; }
}

/* 静态骨架：index.html 的固定 id 元素（$() 的查询目标） */
export function installDomStub() {
  const byId = {
    "view-novice": new StubElement("section"),
    "view-expert": new StubElement("section"),
    "novice-cards": new StubElement("div"),
    "novice-life": new StubElement("div"),
    "expert-groups": new StubElement("div"),
    "mood-section": new StubElement("div"),
    "global-status": new StubElement("div"),
    "save-state": new StubElement("span"),
    "btn-save": new StubElement("button"),
    "btn-reset": new StubElement("button"),
    "tab-novice": new StubElement("button"),
    "tab-expert": new StubElement("button"),
    "load-error": new StubElement("p"),
    "toast": new StubElement("div"),
  };
  byId["view-expert"].className = "view hidden"; // 初渲染为新手视图
  byId["view-novice"].className = "view";
  byId["expert-groups"].className = "drawer-list";
  byId["novice-cards"].className = "card-grid";

  const document = {
    createElement: (tag) => new StubElement(tag),
    createTextNode: (text) => new StubTextNode(text),
    querySelector: (sel) => {
      const m = /^#([A-Za-z0-9_-]+)$/.exec(String(sel || ""));
      return m ? (byId[m[1]] ?? null) : null;
    },
  };
  const localStorageStub = {
    _m: new Map(),
    getItem: (k) => (localStorageStub._m.has(k) ? localStorageStub._m.get(k) : null),
    setItem: (k, v) => localStorageStub._m.set(k, String(v)),
  };
  globalThis.document = document;
  globalThis.window = {
    AstrBotPluginPage: null, // harness 随后注入 bridge 桩
    localStorage: localStorageStub,
  };
  globalThis.localStorage = localStorageStub;
  const body = new StubElement("body");
  for (const el of Object.values(byId)) body.appendChild(el);
  return { document, window: globalThis.window, body, byId };
}

/* 等异步微/宏任务收尾（styleDataCard 的 style_data 拉取、
 * browser/workspace 状态刷新等 async 渲染尾巴） */
export function flush(ms = 120) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* ---- 树遍历与收集（harness 断言的数据源） ---- */

export function walk(node, fn) {
  if (!node || node.kind === "#text") return;
  fn(node);
  for (const c of node.children || []) walk(c, fn);
}

function findAllIn(node, pred) {
  const out = [];
  walk(node, (n) => {
    if (pred(n)) out.push(n);
  });
  return out;
}

export function findAll(root, cls, excludeCls) {
  const out = [];
  walk(root, (n) => {
    if (!n.classList?.contains(cls)) return;
    if (excludeCls && n.classList.contains(excludeCls)) return;
    out.push(n);
  });
  return out;
}

export function findFirst(root, cls) {
  let hit = null;
  walk(root, (n) => {
    if (!hit && n.classList?.contains(cls)) hit = n;
  });
  return hit;
}

export function textOf(n) {
  if (!n) return "";
  if (n.kind === "#text") return n.text;
  if (n.children?.length) return n.children.map(textOf).join("");
  return n._text ?? "";
}
