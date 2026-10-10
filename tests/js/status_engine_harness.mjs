/* M25-补丁1 node 桥：pytest 通过本脚本行为级测试 status-engine.js。
 * 协议：stdin 收一行 JSON {op, ...args}，stdout 回一行 JSON 结果。
 * 不 import 任何 DOM / bridge 依赖——status-engine.js 本身就是纯模块。
 * M26-补丁1 新增 renderPanel / renderPanelFlat：装最小 DOM 桩后动态
 * import 真实 app.js 全链路渲染，输出键行/组摘要供 DOM 装配级断言。
 * M27-补丁1 新增：chain-panel 可达性/点击探针（renderPanel 输出扩展）、
 * statusRecomputeProbe（7.2 状态点实时重算）、toolsProbe（7.3 多选 +
 * 保存契约）、siblingProbe / selectorThrowProbe（dom_stub 选择器）。 */
import {
  SEARCH_DEPENDENT_ACTIVITIES,
  getConfValue,
  evalCond,
  computeNodeStatus,
  computeKeyStatus,
  renderChain,
  computeCounts,
  computeExitLines,
  runnableActivities,
  novicePlan,
} from "../../pages/config/status-engine.js";
import {
  installDomStub,
  flush,
  findAll,
  findFirst,
  textOf,
  walk,
  StubElement,
  StubTextNode,
} from "./dom_stub.mjs";
/* M28-补丁1：说明书渲染冒烟要往同一份 LIVING_HELP 实例塞转义样例——
 * import 拿到的是与 app.js 共享的同一模块实例（ES module 缓存）。 */
import { LIVING_HELP } from "../../pages/config/help-content.js";

const req = JSON.parse(await new Promise((resolve, reject) => {
  let buf = "";
  process.stdin.setEncoding("utf-8");
  process.stdin.on("data", (d) => (buf += d));
  process.stdin.on("end", () => resolve(buf || "{}"));
  process.stdin.on("error", reject);
}));

let out;

/* 渲染公共核（M27-补丁1 抽出）：装桩 → 注入 bridge 桩 → 动态 import 真实
 * app.js（顶层 boot 自动 load→renderNovice+renderExpert+renderGlobalStatus）
 * → flush 收尾。返回 io 供各 op 自行做装配摘要。 */
async function bootPanel(req, postCapture) {
  const io = installDomStub();
  const payload = req.payload;
  const runtime = req.runtime || {};
  io.window.AstrBotPluginPage = {
    ready: async () => {},
    apiGet: async (endpoint) => {
      if (endpoint === "config") return { status: "ok", data: payload };
      if (endpoint === "browser_status") {
        return { status: "ok", data: { installed: runtime.browser_installed !== false } };
      }
      if (endpoint === "workspace_status") {
        return { status: "ok", data: { state: "ready", path: "C:\\ws" } };
      }
      if (endpoint === "style_data") {
        return { status: "ok", data: { corpus: [], materials: [], features: [], usage: [], meta: {} } };
      }
      if (endpoint === "judge_records") return { status: "ok", data: { records: [] } };
      return { status: "error", message: `harness 未实现的端点 ${endpoint}` };
    },
    apiPost: async (endpoint, body) => {
      if (postCapture) postCapture.push({ endpoint, body });
      return { status: "ok" };
    },
  };
  await import("../../pages/config/app.js");
  await flush();
  return io;
}

/* 键行装配摘要（M26 形态，原样保留） */
function collectKeyRows(host) {
  return findAll(host, "key-row", "style-admin").map((row) => {
    const label = findFirst(row, "key-label");
    const nameEl = label && findFirst(label, "key-name");
    const codeEl = label && findFirst(label, "key-code");
    const ctrl = findFirst(row, "key-control");
    return {
      code: textOf(codeEl),
      name: textOf(nameEl),
      hint: textOf(label && findFirst(label, "hint")),
      hasLabel: !!label,
      hasSide: !!findFirst(row, "key-side"),
      dangerRow: row.classList.contains("danger-row"),
      dimmed: row.classList.contains("dimmed"),
      hasDangerChip: !!(label && findFirst(label, "danger-chip")),
      hasMappedChip: !!(label && findFirst(label, "mapped-chip")),
      childClasses: row.children
        .map((c) => (c.kind === "#text" ? "#text" : c.className))
        .filter((s) => s !== ""),
      control: ctrl
        ? {
            cls: ctrl.className,
            text: textOf(ctrl),
            childCls: ctrl.children
              .filter((c) => c.kind === "element")
              .map((c) => c.className),
          }
        : null,
    };
  });
}

function collectGroups(host) {
  return findAll(host, "expert-group").map((g) => {
    const summaryNodes = findAll(g, "group-summary");
    const head = findFirst(g, "group-head");
    const dot = head && findFirst(head, "status-dot");
    const count = head && findFirst(head, "count");
    const body = findFirst(g, "group-body");
    const cascade = findAll(g, "cascade-note").map((n) => textOf(n));
    return {
      title: textOf(head),
      dotText: dot ? textOf(dot) : null,
      countText: count ? textOf(count) : null,
      bodyHidden: body ? body.classList.contains("hidden") : null,
      cascadeNotes: cascade,
      hasCascadeNote: cascade.length > 0,
      dimmedKeyRows: findAll(g, "key-row", "style-admin")
        .filter((r) => r.classList.contains("dimmed")).length,
      hasSummary: summaryNodes.length > 0,
      summaryNodeCount: summaryNodes.length,
      summaryText: textOf(summaryNodes[0]),
      keyCount: findAll(g, "key-row", "style-admin").length,
    };
  });
}

/* M27-补丁1 验收 1/2/3：生效链面板可达性 + 点击交互探针 */
function chainPanelSummary(host) {
  const panels = findAll(host, "chain-panel").map((p) => {
    const parent = p.parentNode;
    return {
      hidden: p.classList.contains("hidden"),
      parentCls: parent && parent.kind === "element" ? parent.className : "",
      siblingToggle: !!(parent && (parent.children || []).some(
        (c) => c !== p && c.kind === "element" && c.classList.contains("chain-toggle")
      )),
    };
  });
  const btns = findAll(host, "chain-toggle");
  let probe = { toggles: btns.length };
  if (btns.length) {
    const btn = btns[0];
    const panel = (btn.parentNode.children || []).find(
      (c) => c !== btn && c.kind === "element" && c.classList.contains("chain-panel")
    );
    if (!panel) {
      // 修前形态（面板游离未挂载）：如实上报，不崩
      probe = { toggles: btns.length, mounted: false };
    } else {
      const before = panel.classList.contains("hidden");
      btn.click();
      const openHidden = panel.classList.contains("hidden");
      const openText = btn.textContent;
      btn.click();
      const closedHidden = panel.classList.contains("hidden");
      const closeText = btn.textContent;
      probe = {
        toggles: btns.length, mounted: true,
        before, openHidden, openText, closedHidden, closeText,
      };
    }
  }
  return { panels, probe };
}

/* M26/M27 renderPanel：全量渲染 + 键行/组/链面板/全局状态行摘要 */
async function renderPanel(req) {
  const io = await bootPanel(req);
  const host = io.byId["expert-groups"];
  const noviceCards = findAll(io.byId["novice-cards"], "knob-card");
  const { panels, probe } = chainPanelSummary(host);
  return {
    keyRows: collectKeyRows(host),
    groups: collectGroups(host),
    chainPanels: panels,
    chainProbe: probe,
    globalStatus: io.byId["global-status"].textContent,
    noviceKnobCards: noviceCards.length,
    noviceDetailCards: noviceCards.filter((c) => c.classList.contains("novice-detail")).length,
    expertSections: findAll(host, "expert-section").length,
    loadError: io.byId["load-error"].textContent,
  };
}

/* M34-补丁1：档位联动提示探针——全链路渲染真实 app.js 后摘要：
 * - 新手页每张旋钮卡的标题 / 是否有 knob-mismatch-note 提示及其文案 /
 *   preset_model 卡 provider-picker 的状态行文本（C 组：实际生效值）
 * - 专家页全部带 mapped-chip 的键行：code / chip 文本 / chip title（B 组：
 *   已脱离 vs 仍在档内） */
async function knobMismatchProbe(req) {
  const io = await bootPanel(req);
  const titleOf = (card) => {
    const h = (card.children || []).find(
      (c) => c.kind === "element" && c.tagName === "H3"
    );
    return h ? textOf(h) : "";
  };
  const novice = findAll(io.byId["novice-cards"], "knob-card").map((card) => {
    const note = findFirst(card, "knob-mismatch-note");
    const picker = findFirst(card, "provider-picker");
    const status = picker && findFirst(picker, "control-status");
    const jstatus = findFirst(card, "judge-status"); // M34 验收 7：judge 卡状态行不受影响
    return {
      title: titleOf(card),
      mismatch: !!note,
      noteText: note ? textOf(note) : null,
      providerStatus: status ? textOf(status) : null,
      judgeStatus: jstatus ? textOf(jstatus) : null,
    };
  });
  const host = io.byId["expert-groups"];
  const mappedChips = findAll(host, "key-row", "style-admin").flatMap((row) => {
    const label = findFirst(row, "key-label");
    const code = label && findFirst(label, "key-code");
    const chip = label && findFirst(label, "mapped-chip");
    if (!chip) return [];
    return [{ code: textOf(code), chipText: textOf(chip), chipTitle: chip.title }];
  });
  return {
    novice,
    mappedChips,
    loadError: io.byId["load-error"].textContent,
  };
}

/* M27-补丁1 7.2：状态点实时重算探针——改 judge.mode / judge.provider_id
 * 的下拉值（dispatch change），逐步抓 A2/A3 组头状态点、生效计数、
 * 展开态保持与滚动位置恢复。 */
async function statusRecomputeProbe(req) {
  const scrolls = [];
  const io = await bootPanel(req);
  // 滚动探针：先伪造当前位置，重渲染后应被 scrollTo 恢复
  io.window.scrollY = 4321;
  io.window.scrollTo = (x, y) => scrolls.push(y);
  const host = io.byId["expert-groups"];
  const findGroup = (needle) => findAll(host, "expert-group").find((g) => {
    const head = findFirst(g, "group-head");
    return head && textOf(head).includes(needle);
  });
  const snap = (needle) => {
    const g = findGroup(needle);
    if (!g) return null;
    return collectGroups(host).find((x) => x.title.includes(needle));
  };
  const setSelect = (code, value) => {
    const row = findAll(host, "key-row", "style-admin").find((r) => {
      const c = findFirst(r, "key-code");
      return c && textOf(c) === code;
    });
    if (!row) throw new Error(`probe: 找不到键行 ${code}`);
    const sel = findFirst(findFirst(row, "key-control"), "enum-select");
    if (!sel) throw new Error(`probe: ${code} 的控件里没有下拉`);
    sel.value = value;
    sel.dispatch("change");
  };
  const steps = [];
  steps.push({ step: "初始(off)", a2: snap("把关用的小大脑"), a3: snap("把关怎么把守") });
  // 先展开 A2（真实点击组头），验证重渲染不丢展开态
  const a2g = findGroup("把关用的小大脑");
  findFirst(a2g, "group-head").click();
  steps.push({ step: "点击展开A2后", a2BodyHidden: snap("把关用的小大脑").bodyHidden });
  setSelect("judge.mode", "api");
  steps.push({ step: "mode→api(provider空)", a2: snap("把关用的小大脑"), a3: snap("把关怎么把守") });
  setSelect("judge.provider_id", "p-chat");
  steps.push({ step: "provider→p-chat", a2: snap("把关用的小大脑"), a3: snap("把关怎么把守") });
  setSelect("judge.mode", "off");
  steps.push({ step: "mode→off", a2: snap("把关用的小大脑"), a3: snap("把关怎么把守") });
  // 无关键（预算数字，不在状态键集合）变化 → 不触发重渲染
  const budgetRow = findAll(host, "key-row", "style-admin").find((r) => {
    const c = findFirst(r, "key-code");
    return c && textOf(c) === "decision.single_run_token_budget";
  });
  const ctrlEl = budgetRow && findFirst(budgetRow, "key-control");
  const numInput = ctrlEl && ctrlEl.children.find(
    (c) => c.kind === "element" && c.tagName === "INPUT"
  );
  const relevantScrolls = scrolls.length;
  if (numInput) {
    numInput.value = "123";
    numInput.dispatch("change");
  }
  steps.push({
    step: "无关键变化(预算数字)",
    rerendered: scrolls.length > relevantScrolls,
  });
  return {
    steps,
    scrolls,
    loadError: io.byId["load-error"].textContent,
  };
}

/* M27-补丁1 7.3：本体工具白名单多选探针——渲染后读各行文本与勾选态，
 * 勾选"注册表里没有"的工具后触发保存，抓 POST 保存契约（仍逗号分隔串）。 */
async function toolsProbe(req) {
  const posts = [];
  const io = await bootPanel(req, posts);
  const host = io.byId["expert-groups"];
  const row = findAll(host, "key-row", "style-admin").find((r) => {
    const c = findFirst(r, "key-code");
    return c && textOf(c) === "capabilities.agent_tools";
  });
  if (!row) return { found: false, loadError: io.byId["load-error"].textContent };
  const readChoices = () => findAll(row, "multi-choice").map((m) => {
    const box = m.children.find((c) => c.kind === "element" && c.tagName === "INPUT");
    return { text: textOf(m), checked: !!(box && box.checked) };
  });
  const before = readChoices();
  // 勾选一个未勾的注册表工具（如 fetch_page），验证保存串拼入；同时
  // "注册表里没有"的已勾工具保持勾选（不静默丢弃）
  const target = readChoices().find((c) => !c.checked && !c.text.includes("当前注册表里没有"));
  if (target) {
    const m = findAll(row, "multi-choice").find((x) => textOf(x) === target.text);
    const box = m.children.find((c) => c.kind === "element" && c.tagName === "INPUT");
    box.checked = true;
    box.dispatch("change");
  }
  const after = readChoices();
  // 触发保存：抓 buildSavePayload 的实际提交体
  io.byId["btn-save"].click();
  await flush(200);
  return {
    found: true,
    labels: before.map((c) => c.text),
    checkedBefore: before.filter((c) => c.checked).map((c) => c.text),
    checkedAfter: after.filter((c) => c.checked).map((c) => c.text),
    savePosts: posts,
    loadError: io.byId["load-error"].textContent,
  };
}

/* M28-补丁1：内置说明书探针——渲染冒烟（节数与 LIVING_HELP 一致）+
 * 三种关闭方式（关闭按钮 / 遮罩本体点击 target===currentTarget /
 * Escape 仅开着时生效）+ 转义（塞含 <script>/& 的样例节 → 无可执行
 * 标签、文本按字面出现在文本节点里）。 */
async function helpProbe(req) {
  const io = await bootPanel(req);
  const mask = io.byId["help-mask"];
  const body = io.byId["help-body"];
  const titles = () => findAll(body, "help-section").map(
    (s) => textOf((s.children || []).find((c) => c.tagName === "H3"))
  );
  const out = {
    initial: {
      hidden: mask.classList.contains("hidden"),
      bodyChildren: body.children.length,
    },
  };
  io.byId["btn-help"].click();
  out.afterOpen = {
    hidden: mask.classList.contains("hidden"),
    sections: findAll(body, "help-section").length,
    bodyChildren: body.children.length,
    livingHelpLength: LIVING_HELP.length,
    titles: titles(),
  };
  io.byId["help-close"].click();
  out.afterCloseBtn = { hidden: mask.classList.contains("hidden") };
  // 点遮罩关闭的守卫：把真实监听器拿来喂自定义事件（桩 dispatch 只能
  // 造 target===currentTarget 的点击，这里两种情形都要验）
  io.byId["btn-help"].click(); // 开
  const clickLsn = mask._listeners.find(([t]) => t === "click")[1];
  clickLsn({ target: { kind: "element" }, currentTarget: mask }); // 点在卡片内
  out.maskClickOnCard = { hidden: mask.classList.contains("hidden") };
  clickLsn({ target: mask, currentTarget: mask }); // 点在遮罩本体
  out.maskClickOnMask = { hidden: mask.classList.contains("hidden") };
  // Escape：开着时关；已关时保持关（守卫生效、不抛错）
  io.byId["btn-help"].click();
  document.dispatch("keydown", { key: "Escape" });
  out.escapeWhenOpen = { hidden: mask.classList.contains("hidden") };
  document.dispatch("keydown", { key: "Escape" });
  out.escapeWhenClosed = { hidden: mask.classList.contains("hidden") };
  // 转义样例：含 <script> 与 & 的文本必须以文本节点字面出现，不得成标签
  LIVING_HELP.push({
    title: "转义样例",
    blocks: [{ t: "p", text: '<script>alert(1)</script> & "quotes"' }],
  });
  io.byId["btn-help"].click();
  let scriptTags = 0;
  let asTextNode = false;
  walk(body, (n) => {
    if (n.tagName === "SCRIPT") scriptTags += 1;
    for (const c of n.children || []) {
      if (c.kind === "#text" && c.text.includes("<script>")) asTextNode = true;
    }
  });
  out.escape = {
    scriptTags,
    asTextNode,
    literalText: body.textContent.includes('<script>alert(1)</script> & "quotes"'),
    sectionsAfterPush: findAll(body, "help-section").length,
  };
  LIVING_HELP.pop(); // 还原共享实例
  out.loadError = io.byId["load-error"].textContent;
  return out;
}

if (req.op === "renderPanel" || req.op === "renderPanelFlat") {
  out = await renderPanel(req);
} else if (req.op === "knobMismatchProbe") {
  out = await knobMismatchProbe(req);
} else if (req.op === "statusRecomputeProbe") {
  out = await statusRecomputeProbe(req);
} else if (req.op === "toolsProbe") {
  out = await toolsProbe(req);
} else if (req.op === "helpProbe") {
  out = await helpProbe(req);
} else if (req.op === "siblingProbe") {
  // M27-补丁1 验收 b：~ 一般兄弟选择器（真值集合、次序、文本节点与前置兄弟排除）
  installDomStub();
  const parent = new StubElement("div");
  const before = new StubElement("div");
  before.className = "knob-card";
  const toggle = new StubElement("div");
  toggle.className = "novice-detail-toggle";
  const txt = new StubTextNode("占位文本");
  const b1 = new StubElement("div");
  b1.className = "knob-card";
  const b2 = new StubElement("div");
  b2.className = "knob-card";
  parent.append(before, toggle, txt, b1, b2);
  const hits = parent.querySelectorAll(".novice-detail-toggle ~ .knob-card");
  // tag 选择器正例（styleCorpusList 编辑器的 querySelectorAll("button") 同形态）
  const tagParent = new StubElement("div");
  const label = new StubElement("label");
  const btnEl = new StubElement("button");
  label.appendChild(btnEl);
  const span = new StubElement("span");
  tagParent.append(label, span);
  out = {
    count: hits.length,
    hits: hits.map((n) => (n === before ? "before" : n === b1 ? "b1" : n === b2 ? "b2" : "?")),
    tagButtonsInLabel: label.querySelectorAll("button").length,
    tagButtonsInParent: tagParent.querySelectorAll("button").length,
    noSiblingBefore: !parent
      .querySelectorAll(".novice-detail-toggle ~ .knob-card").includes(before),
  };
} else if (req.op === "selectorThrowProbe") {
  // M27-补丁1 验收 c：不支持的选择器必须抛错（元素级 + document 级）
  installDomStub();
  const parent = new StubElement("div");
  const child = new StubElement("div");
  child.className = "a";
  parent.appendChild(child);
  const results = {};
  for (const sel of req.selectors || []) {
    try {
      parent.querySelectorAll(sel);
      results[sel] = "no-throw";
    } catch (e) {
      results[sel] = "threw";
    }
  }
  try {
    document.querySelector(".nope");
    results["document:.nope"] = "no-throw";
  } catch (e) {
    results["document:.nope"] = "threw";
  }
  out = results;
} else {
  switch (req.op) {
  case "getConfValue":
    out = getConfValue(req.ctx, req.keyPath);
    break;
  case "evalCond":
    out = evalCond(req.cond, req.ctx);
    break;
  case "computeNodeStatus":
    out = computeNodeStatus(req.decl, req.ctx);
    break;
  case "computeKeyStatus":
    out = computeKeyStatus(req.groupStatus, req.keyDecl, req.ctx);
    break;
  case "renderChain":
    out = renderChain(req.title, req.decl, req.ctx);
    break;
  case "computeCounts":
    out = computeCounts(req.statuses);
    break;
  case "computeExitLines":
    out = computeExitLines(req.exitsDecl, req.ctx);
    break;
  case "runnableActivities":
    out = runnableActivities(req.agentActivities, req.freeEnabled, req.webSearchEnabled);
    break;
  case "novicePlan":
    out = novicePlan(req.layout, req.presetSchema);
    break;
  case "searchDependent":
    out = SEARCH_DEPENDENT_ACTIVITIES;
    break;
  default:
    console.error(JSON.stringify({ error: `unknown op ${req.op}` }));
    process.exit(2);
  }
}
process.stdout.write(JSON.stringify(out));
