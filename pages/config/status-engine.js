/* M25-补丁1 面板状态引擎（纯函数，无 DOM / 无 bridge 依赖）。
 *
 * 四态语义（任务书 C4）：
 *   active  绿点  开关打开且前置满足，真的在生效
 *   off     灰点  显式关闭（本节点自己的开关）
 *   blocked 黄点  开关打开但前置不满足（如 api 档没选 provider）
 *   dead    红点  被上游显式关闭级联（如风格总闸关着）
 * 另有两个展示态：
 *   unimpl       配套 e：judge.mode=local 之类的"未实现占位"，绝不能显示成开
 *   unknown      运行时数据（browser_status 等）未就绪——宁可标未知也不猜
 *
 * 本模块被 pages/config/app.js import；pytest 通过 node 桥
 * （tests/js/status_engine_harness.mjs）直接行为级测试，改动时两侧同源。
 * 求值器 op 集合与任务书一致：truthy / eq / nonEmpty / empty / anyOf / allOf。
 */

/* core/activities.py:40 SEARCH_DEPENDENT_ACTIVITIES —— 搜索关闭时被摘除的
 * 活动（前端镜像，验收 4 的展示口径；锚点测试锁定两侧同值）。 */
export const SEARCH_DEPENDENT_ACTIVITIES = ["surf", "read"];

const CIRCLED = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"];
function circled(i) {
  return CIRCLED[i] || `(${i + 1})`;
}

/* ---------------- 值读取 ---------------- */

/* keyPath 形如 "style_learning.enabled"（advanced 组.键）或 "preset_model"
 * （无点 = 新手旋钮）；"runtime.xxx" 是运行时谓词（复用既有端点，零新增轮询）：
 *   runtime.browser_installed      ← browser_status 端点
 *   runtime.workspace_ready        ← workspace_status 端点
 *   runtime.provider_exists:<id>   ← config payload 的 providers 清单 */
export function getConfValue(ctx, keyPath) {
  const path = String(keyPath || "");
  if (path.startsWith("runtime.")) {
    const rest = path.slice("runtime.".length);
    if (rest === "browser_installed") return ctx.runtime?.browser_installed;
    if (rest === "workspace_ready") return ctx.runtime?.workspace_ready;
    if (rest.startsWith("provider_exists:")) {
      const pid = rest.slice("provider_exists:".length);
      const providers = ctx.runtime?.providers;
      return Array.isArray(providers) ? providers.includes(pid) : undefined;
    }
    return undefined;
  }
  const dot = path.indexOf(".");
  if (dot < 0) return ctx.values?.knobs?.[path];
  const group = path.slice(0, dot);
  const key = path.slice(dot + 1);
  return ctx.values?.advanced?.[group]?.[key];
}

/* ---------------- 条件求值（单环） ----------------
 * 返回 { pass, explicitOff, unknown, detail }：
 * - explicitOff：失败源于"布尔显式 false"——用于区分 哑（dead，被显式关闭
 *   级联）与 卡着（blocked，前置缺失）；
 * - unknown：失败涉及缺失的运行时数据（不猜，宁可标未知）。 */
export function evalCond(cond, ctx) {
  if (!cond || typeof cond !== "object") {
    return { pass: true, explicitOff: false, unknown: false, detail: "" };
  }
  const op = cond.op;
  if (op === "anyOf" || op === "allOf") {
    const subs = (cond.of || []).map((c) => evalCond(c, ctx));
    const pass = op === "anyOf" ? subs.some((s) => s.pass) : subs.every((s) => s.pass);
    const failing = subs.find((s) => !s.pass);
    // unknown：任一失败子环是运行时数据缺失（且无显式关闭可归因）
    const anyUnknown = subs.some((s) => !s.pass && s.unknown);
    const anyExplicit = subs.some((s) => !s.pass && s.explicitOff);
    return {
      pass,
      explicitOff: !pass && anyExplicit,
      unknown: !pass && anyUnknown && !anyExplicit,
      detail: pass ? "满足" : (failing ? failing.detail : "不满足"),
    };
  }
  const value = getConfValue(ctx, cond.key);
  const shown = (v) => (v === undefined || v === null ? "（未设置）" : String(v));
  if (op === "truthy") {
    // JSON 桥传参没有 undefined：null 同样按"运行时数据缺失"处理（不猜）。
    // runtime 谓词的 false 是环境状态（如 Chromium 未安装）而不是显式关闭
    // ——归"卡着"（前置缺失），绝不归"哑"（被显式关闭级联）。
    const isRuntime = String(cond.key).startsWith("runtime.");
    const runtimeMissing = isRuntime && (value === undefined || value === null);
    if (value) return { pass: true, explicitOff: false, unknown: false, detail: "开" };
    return {
      pass: false,
      explicitOff: value === false && !isRuntime,
      unknown: runtimeMissing,
      detail: runtimeMissing ? "状态未知" : (value === false ? "关" : `未生效（${shown(value)}）`),
    };
  }
  if (op === "eq") {
    const pass = value === cond.value;
    return {
      pass,
      explicitOff: false,
      unknown: value === undefined,
      detail: pass ? `是 ${shown(cond.value)}` : `当前 ${shown(value)}`,
    };
  }
  if (op === "nonEmpty") {
    const empty = value === undefined || value === null || String(value).trim() === "";
    return {
      pass: !empty,
      explicitOff: false,
      unknown: value === undefined,
      detail: empty ? "空" : `已配（${String(value).trim()}）`,
    };
  }
  if (op === "empty") {
    const empty = value === undefined || value === null || String(value).trim() === "";
    return {
      pass: empty,
      explicitOff: false,
      unknown: value === undefined,
      detail: empty ? "空" : `已填（${String(value).trim()}）`,
    };
  }
  // 未知 op：按通过处理（宁可少标也不误报）
  return { pass: true, explicitOff: false, unknown: false, detail: "" };
}

/* ---------------- 节点四态判定（computeNodeStatus，任务书 C4） ----------------
 * decl：组/键声明 { switch?, offValue?, unimplemented?, requires?, chainNote? }
 * 返回 { status, chain, failing }；status ∈ active/off/blocked/dead/unimpl/unknown。
 * 判定顺序：
 *   1. switch 值命中 unimplemented.value → unimpl（配套 e，绝不算开）
 *   2. switch 显式关（false 或 offValue）→ off
 *   3. requires 逐环：soft 环只展示不阻断；首个失败环决定
 *      unknown / dead（上游显式关闭级联）/ blocked（前置缺失）
 *   4. 全过 → active */
export function computeNodeStatus(decl, ctx) {
  const chain = [];
  const requires = (decl && decl.requires) || [];
  const switchKey = decl && decl.switch;

  if (switchKey) {
    const switchVal = getConfValue(ctx, switchKey);
    const unimp = decl.unimplemented;
    if (unimp && switchVal === unimp.value) {
      return {
        status: "unimpl",
        chain,
        failing: null,
        statusLabel: unimp.label || "未实现",
        hint: unimp.hint || "",
      };
    }
    const isOff =
      switchVal === false ||
      (decl.offValue !== undefined && switchVal === decl.offValue);
    if (isOff) {
      return { status: "off", chain, failing: null, statusLabel: "关" };
    }
  }

  let failing = null;
  (requires || []).forEach((req, i) => {
    const soft = !!req.soft;
    const cond = req.op ? req : (req.cond || {});
    const result = evalCond(cond, ctx);
    const pass = soft ? true : result.pass;
    let detail = result.detail;
    if (soft && !result.pass && req.fallbackNote) detail = req.fallbackNote;
    chain.push({
      index: circled(i),
      label: req.label || cond.key || "",
      pass,
      soft,
      fail: !pass,
      detail,
      failHint: req.failHint || "",
      anchor: req.anchor || "",
    });
    if (!pass && !failing) failing = { req, result, index: i };
  });

  if (failing) {
    // req.explicit：布局声明者标注"该环的失败等于上游显式关闭"（如判断
    // 档位选了 off/local——是显式选择，级联置灰为哑，而不是"卡着"）
    if (failing.result.unknown) {
      return { status: "unknown", chain, failing, statusLabel: "未知" };
    }
    if (failing.result.explicitOff || failing.req.explicit) {
      return { status: "dead", chain, failing, statusLabel: "哑" };
    }
    return { status: "blocked", chain, failing, statusLabel: "卡着" };
  }
  return { status: "active", chain, failing: null, statusLabel: "开" };
}

/* ---------------- 键级状态（组状态投射 + 键自身 requires） ---------------- */
export function computeKeyStatus(groupStatus, keyDecl, ctx) {
  if (groupStatus !== "active") {
    // 组不生效时键跟随组；但组自己的 switch 键显示"关"本身（它是因不是果）
    if (keyDecl && keyDecl.isGroupSwitch && groupStatus === "off") return "off";
    return groupStatus;
  }
  const own = keyDecl && keyDecl.requires && keyDecl.requires.length
    ? computeNodeStatus(keyDecl, ctx) : null;
  return own ? own.status : "active";
}

/* ---------------- 生效链文本（任务书 C5，范例风格逐字对齐） ----------------
 * 返回 { title, lines: ["① 风格学习总闸 ● 关", ...], conclusion, note }
 * lines 每行带环序号、状态点与原因；conclusion 说清哪步断了、断了什么后果。 */
export function renderChain(title, decl, ctx) {
  const result = computeNodeStatus(decl, ctx);
  const dotOf = (ring) => (ring.fail ? "▲" : "●");
  const lines = result.chain.map((ring) =>
    `${ring.index} ${ring.label} ${dotOf(ring)} ${ring.detail}`);
  let conclusion;
  if (result.status === "active") {
    conclusion = "⇒ 当前：各环全通，这条功能正在生效。";
  } else if (result.status === "off") {
    conclusion = "⇒ 当前：开关已关闭（显式关闭，不是故障）。";
  } else if (result.status === "unimpl") {
    conclusion = `⇒ 当前：${result.statusLabel}——${result.hint || "该档位尚未实现。"}`;
  } else if (result.status === "unknown") {
    conclusion = "⇒ 当前：运行时状态未知（数据未就绪），刷新面板后再看。";
  } else {
    const ring = result.chain[result.failing.index];
    conclusion = `⇒ 当前：${ring.index}未通。${ring.failHint || "前置不满足。"}`;
  }
  return {
    title: title || "生效链",
    lines,
    conclusion,
    note: (decl && decl.chainNote) || "",
    status: result.status,
  };
}

/* ---------------- 生效计数（任务书 C7） ----------------
 * entries: [{status}] → {active, total}；组状态投射后逐键计数。 */
export function computeCounts(statuses) {
  const list = statuses || [];
  return {
    active: list.filter((s) => s === "active").length,
    total: list.length,
  };
}

/* ---------------- 四态 → 展示文案 ---------------- */
export const STATUS_META = {
  active: { label: "开", dot: "●", cls: "st-active" },
  on: { label: "开", dot: "●", cls: "st-active" }, // 出口行的"on"别名（computeExitLines）
  off: { label: "关", dot: "●", cls: "st-off" },
  blocked: { label: "卡着", dot: "▲", cls: "st-blocked" },
  dead: { label: "哑", dot: "●", cls: "st-dead" },
  unimpl: { label: "未实现", dot: "○", cls: "st-unimpl" },
  unknown: { label: "未知", dot: "○", cls: "st-unknown" },
};

/* ---------------- 七条出口（任务书 C8 全局状态行） ----------------
 * exitsDecl：panel_layout.json 的 exits.items。判定纯前端：
 *   cond 失败 → off；cond 过但 requires 失败 → blocked（带 failHint）；
 *   全过 → on。4 条过闸门 + 3 条直发的 note 逐字来自布局声明。 */
export function computeExitLines(exitsDecl, ctx) {
  return (exitsDecl || []).map((item) => {
    let status = "on";
    let detail = item.note || "";
    if (item.cond && !evalCond(item.cond, ctx).pass) {
      status = "off";
      detail = "已关闭";
    } else if (item.requires && item.requires.length) {
      const result = computeNodeStatus({ requires: item.requires }, ctx);
      if (result.status === "blocked" || result.status === "dead"
        || result.status === "unknown") {
        status = "blocked";
        const ring = result.chain[result.failing.index];
        detail = `卡着——${ring.failHint || ring.label + "不满足"}`;
      }
    }
    return { id: item.id, label: item.label, status, note: item.note || "", detail };
  });
}

/* ---------------- C3 活动池：当前可跑的活动（验收 4 展示口径） ----------------
 * 镜像 core/activities.py:106-120 activities_excluding_search +
 * decision.free_activity_enabled（:707 附近，调用方据此裁掉 free）。 */
export function runnableActivities(agentActivities, freeEnabled, webSearchEnabled) {
  let list = Array.isArray(agentActivities) ? [...agentActivities] : [];
  if (!webSearchEnabled) {
    list = list.filter((a) => !SEARCH_DEPENDENT_ACTIVITIES.includes(a));
  }
  if (freeEnabled === false) {
    list = list.filter((a) => a !== "free");
  }
  return list;
}

/* ---------------- 新手面板归组（配套 a） ----------------
 * 第一层：8 张旋钮（按 preset 键 section 一级 id 归组，组序随 sections）+
 * life_extra（app.js 固定块）。折叠区：13 张功能卡按 layout.novice.cardGroups
 * 归组。返回 { knobGroups, cardGroups, firstTierKnobCount }。 */
export function novicePlan(layout, presetSchema) {
  const sections = (layout && layout.sections) || [];
  const preset = presetSchema || {};
  const bySec = new Map();
  for (const [name, item] of Object.entries(preset)) {
    // life_extra 有专属渲染块（#novice-life，"性格与兴趣底色"文本框），
    // 不算旋钮卡——配套 a 的"首屏旋钮数 = 8"不含它
    if (name === "life_extra") continue;
    const sec = item && item.section;
    if (!Array.isArray(sec) || sec.length < 2) continue;
    const [sid, , order] = sec;
    if (!bySec.has(sid)) bySec.set(sid, []);
    bySec.get(sid).push({ name, order: typeof order === "number" ? order : 0 });
  }
  const knobGroups = [];
  for (const sec of sections) {
    const entries = bySec.get(sec.id);
    if (!entries || !entries.length) continue;
    entries.sort((a, b) => a.order - b.order);
    knobGroups.push({
      id: sec.id,
      title: sec.title,
      knobs: entries.map((e) => e.name),
    });
  }
  const cardGroups = (((layout || {}).novice || {}).cardGroups || []).map((g) => {
    const sec = sections.find((s) => s.id === g.section);
    return { id: g.section, title: sec ? sec.title : g.section, cards: g.cards || [] };
  });
  const firstTierKnobCount = knobGroups.reduce((n, g) => n + g.knobs.length, 0);
  return { knobGroups, cardGroups, firstTierKnobCount };
}
