/* M25-补丁1 node 桥：pytest 通过本脚本行为级测试 status-engine.js。
 * 协议：stdin 收一行 JSON {op, ...args}，stdout 回一行 JSON 结果。
 * 不 import 任何 DOM / bridge 依赖——status-engine.js 本身就是纯模块。
 * M26-补丁1 新增 renderPanel / renderPanelFlat：装最小 DOM 桩后动态
 * import 真实 app.js 全链路渲染，输出键行/组摘要供 DOM 装配级断言。 */
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
} from "./dom_stub.mjs";

const req = JSON.parse(await new Promise((resolve, reject) => {
  let buf = "";
  process.stdin.setEncoding("utf-8");
  process.stdin.on("data", (d) => (buf += d));
  process.stdin.on("end", () => resolve(buf || "{}"));
  process.stdin.on("error", reject);
}));

let out;

/* M26-补丁1：渲染面板（真实 app.js + DOM 桩）。payload 由 Python 侧按
 * build_config_payload 同构构造；layout 为 null 时走 renderExpertFlat
 * 回退路径（配套 c）。返回键行/组/全局状态行的装配摘要。 */
async function renderPanel(req) {
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
    apiPost: async () => ({ status: "ok" }),
  };
  await import("../../pages/config/app.js"); // 顶层 boot() 自动跑 load→renderNovice+renderExpert+renderGlobalStatus
  await flush();
  const host = io.byId["expert-groups"];
  const keyRows = findAll(host, "key-row", "style-admin").map((row) => {
    const label = findFirst(row, "key-label");
    const nameEl = label && findFirst(label, "key-name");
    const codeEl = label && findFirst(label, "key-code");
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
    };
  });
  const groups = findAll(host, "expert-group").map((g) => {
    const summaryNodes = findAll(g, "group-summary");
    return {
      title: textOf(findFirst(g, "group-head")),
      hasSummary: summaryNodes.length > 0,
      summaryNodeCount: summaryNodes.length,
      summaryText: textOf(summaryNodes[0]),
      keyCount: findAll(g, "key-row", "style-admin").length,
    };
  });
  const gs = io.byId["global-status"];
  return {
    keyRows,
    groups,
    globalStatus: gs.textContent,
    noviceKnobCards: findAll(io.byId["novice-cards"], "knob-card").length,
    expertSections: findAll(host, "expert-section").length,
    loadError: io.byId["load-error"].textContent,
  };
}

if (req.op === "renderPanel" || req.op === "renderPanelFlat") {
  out = await renderPanel(req);
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
