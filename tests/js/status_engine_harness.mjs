/* M25-补丁1 node 桥：pytest 通过本脚本行为级测试 status-engine.js。
 * 协议：stdin 收一行 JSON {op, ...args}，stdout 回一行 JSON 结果。
 * 不 import 任何 DOM / bridge 依赖——status-engine.js 本身就是纯模块。 */
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

const req = JSON.parse(await new Promise((resolve, reject) => {
  let buf = "";
  process.stdin.setEncoding("utf-8");
  process.stdin.on("data", (d) => (buf += d));
  process.stdin.on("end", () => resolve(buf || "{}"));
  process.stdin.on("error", reject);
}));

let out;
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
process.stdout.write(JSON.stringify(out));
