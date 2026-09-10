// 确认对话框浏览器验收：所有 API 在路由层模拟，逐项校验取消不写入、确认只写入一次。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const timestamp = "2026-09-08T08:00:00.000Z";
const administrator = {
  id: "admin", username: "admin", displayName: "模拟管理员", isAdmin: true,
  scopes: ["*"], resourceIds: null, enabled: true, mustChangePassword: false,
  builtin: true, version: 1,
};
const tasks = [
  ["start-task", "待启动任务", "STOPPED", "STOPPED"],
  ["stop-task", "运行中任务", "COLLECTING", "RUNNING"],
  ["pause-task", "可暂停任务", "COLLECTING", "RUNNING"],
  ["resume-task", "已暂停任务", "PAUSED", "PAUSED"],
].map(([id, name, status, desiredState]) => ({
  id, name, status, desiredState, protocol: "SSH", ip: "192.0.2.10", port: 22,
  username: "fixture", resourceId: "network-resource", initialCommands: [], scheduledCommands: [], version: 1,
}));
const state = {
  platform: { retentionDays: 7, version: 1, updatedAt: timestamp },
  nodes: [{ id: "edge-fixture", url: "https://edge.example.test", reportedUrl: "https://edge.example.test", capacity: 8, accepting: true, version: 1, registered: true, online: true, reportedAt: timestamp }],
  templates: [{
    id: "template-fixture", name: "既有模板", description: "浏览器夹具", version: 1,
    initialCommands: [
      { command: "first", newline: "\n", delaySeconds: 0, prompt: null, timeoutSeconds: 30 },
      { command: "second", newline: "\n", delaySeconds: 0, prompt: null, timeoutSeconds: 30 },
    ],
    scheduledCommands: [{ command: "logread", totalExecutions: 1, intervalSeconds: 60 }],
  }],
  mutations: [],
  downloadCancelled: false,
};
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-ui-token"));

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
function page(items, pageSize = 100) { return { items, total: items.length, page: 1, pageSize }; }
await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const method = request.method();
  const path = new URL(request.url()).pathname;
  const payload = request.postDataJSON?.() ?? {};
  if (method !== "GET") state.mutations.push({ method, path, payload });
  if (method === "GET" && path === "/api/v1/auth/me") return json(route, { user: administrator });
  if (method === "GET" && path === "/api/v1/tasks") return json(route, page(tasks));
  if (method === "GET" && path.startsWith("/api/v1/tasks/") && path.endsWith("/log-hours")) return json(route, page([{ hourId: "hour-fixture", hour: "2026-09-09T18:00:00Z", status: "READY", integrity: "VERIFIED", bytes: 10, archiveBytes: 5, files: [] }], 24));
  if (method === "GET" && path.startsWith("/api/v1/tasks/")) return json(route, tasks.find(task => path.endsWith(task.id)) ?? tasks[0]);
  if (method === "POST" && /^\/api\/v1\/tasks\/[^/]+\/(start|stop|pause|resume)$/.test(path)) return json(route, {});
  if (method === "PATCH" && path.startsWith("/api/v1/tasks/")) return json(route, { ...tasks.find(task => path.endsWith(task.id)), ...payload });
  if (method === "GET" && path === "/api/v1/resources") return json(route, page([{ id: "network-resource", name: "模拟网络设备", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", model: "DS-2CD", subSerialNumber: "SN-fixture", version: 1 }]));
  if (method === "GET" && path === "/api/v1/resources/network-resource/coredump-monitor") return json(route, { active: false, ownerTask: null, mountStatus: null });
  if (method === "GET" && path === "/api/v1/command-templates") return json(route, page(state.templates));
  if (method === "GET" && path === "/api/v1/users/share-targets") return json(route, page([]));
  if (method === "GET" && path.startsWith("/api/v1/command-templates/")) return json(route, state.templates[0]);
  if (method === "PATCH" && path.startsWith("/api/v1/command-templates/")) return json(route, { ...state.templates[0], ...payload });
  if (method === "GET" && path === "/api/v1/nodes") return json(route, page([]));
  if (method === "GET" && path === "/api/v1/platform-settings") return json(route, state.platform);
  if (method === "PATCH" && path === "/api/v1/platform-settings") { state.platform = { ...state.platform, ...payload, version: state.platform.version + 1 }; return json(route, state.platform); }
  if (method === "GET" && path === "/api/v1/admin/nodes") return json(route, { items: state.nodes });
  if (method === "POST" && path === "/api/v1/admin/nodes") return json(route, { ...payload, version: 1, registered: true, online: true, reportedAt: timestamp }, 201);
  if (method === "PATCH" && path.startsWith("/api/v1/admin/nodes/")) return json(route, { ...state.nodes[0], ...payload, version: state.nodes[0].version + 1 });
  if (method === "DELETE" && path.startsWith("/api/v1/admin/nodes/")) {
    assert.equal(new URL(request.url()).searchParams.get("version"), "1");
    state.nodes = state.nodes.filter(node => !path.endsWith(node.id));
    return route.fulfill({ status: 204 });
  }
  if (method === "POST" && path === "/api/v1/downloads") return json(route, { id: "download-fixture" }, 201);
  if (method === "GET" && path === "/api/v1/downloads/download-fixture") return json(route, { id: "download-fixture", status: state.downloadCancelled ? "CANCELLED" : "RUNNING", progress: 30 });
  if (method === "DELETE" && path === "/api/v1/downloads/download-fixture") { state.downloadCancelled = true; return route.fulfill({ status: 204 }); }
  throw new Error(`未模拟的 API 请求：${method} ${path}`);
});

const ui = await context.newPage();
ui.setDefaultTimeout(10000);
const errors = [];
ui.on("pageerror", error => errors.push(error.message));
ui.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
const mutations = () => state.mutations.length;
const changedOnce = (before, label) => assert.equal(mutations(), before + 1, `${label} 确认后必须恰好发出一次修改请求`);
async function dismissConfirmation() {
  const dialog = ui.locator(".el-message-box");
  await dialog.waitFor({ state: "visible" });
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
}
async function acceptConfirmation() {
  const dialog = ui.locator(".el-message-box");
  await dialog.waitFor({ state: "visible" });
  await dialog.getByRole("button", { name: "确认", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
}
async function cancelThenConfirm(click, label) {
  const before = mutations();
  await click(); await dismissConfirmation();
  assert.equal(mutations(), before, `${label} 取消后不得发送修改请求`);
  await click(); await acceptConfirmation();
  changedOnce(before, label);
}
async function cancelThenRemove(click, remaining, label) {
  const before = mutations();
  await click(); await dismissConfirmation();
  assert.equal(mutations(), before, `${label} 取消后不得发出保存请求`);
  await click(); await acceptConfirmation();
  await remaining();
  assert.equal(mutations(), before, `${label} 删除草稿前不得发出保存请求`);
}
async function navigate(label) {
  await ui.getByRole("tab", { name: label, exact: true }).click();
  await ui.locator("h1").filter({ hasText: label }).waitFor();
}

try {
  await ui.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await navigate("采集任务");
  for (const [name, action] of [["待启动任务", "启动任务"], ["运行中任务", "停止任务"], ["可暂停任务", "暂停任务"], ["已暂停任务", "继续任务"]]) {
    const row = ui.locator(".el-table__body tr").filter({ hasText: name });
    await cancelThenConfirm(() => row.getByRole("button", { name: action, exact: true }).click(), `${name}${action}`);
  }

  const taskRow = ui.locator(".el-table__body tr").filter({ hasText: "待启动任务" });
  await taskRow.getByRole("button", { name: "编辑任务", exact: true }).click();
  const taskEditor = ui.getByRole("dialog", { name: "任务 · 待启动任务" });
  await taskEditor.waitFor();
  await cancelThenConfirm(() => ui.getByRole("button", { name: "保存任务", exact: true }).click(), "编辑任务保存");
  await taskEditor.waitFor({ state: "hidden" });

  await navigate("命令模板");
  await ui.getByRole("row").filter({ hasText: "既有模板" }).locator("button").first().click();
  const templateEditor = ui.getByRole("dialog", { name: "编辑命令模板" });
  await templateEditor.waitFor();
  await cancelThenRemove(
    () => templateEditor.getByLabel("删除初始化命令").nth(1).click(),
    () => templateEditor.locator(".initial-row").count().then(count => assert.equal(count, 1, "确认后应只删除一条初始化命令")),
    "初始化命令删除",
  );
  await cancelThenRemove(
    () => templateEditor.getByLabel("删除定时命令").click(),
    () => templateEditor.locator(".schedule-edit-row").count().then(count => assert.equal(count, 0, "确认后应删除定时命令")),
    "定时命令删除",
  );
  await mkdir("output/playwright", { recursive: true });
  await templateEditor.screenshot({ path: "output/playwright/command-confirmations-1440.png" });
  await ui.setViewportSize({ width: 390, height: 844 });
  await templateEditor.screenshot({ path: "output/playwright/command-confirmations-390.png" });
  const widths = await templateEditor.locator("*").evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().right));
  assert.ok(Math.max(...widths) <= 390, "移动端命令编辑器不应横向溢出");
  await ui.setViewportSize({ width: 1440, height: 1000 });
  await cancelThenConfirm(() => ui.getByRole("button", { name: "保存模板", exact: true }).click(), "编辑模板保存");
  await templateEditor.waitFor({ state: "hidden" });

  await navigate("后台配置");
  await ui.getByLabel("日志保存天数", { exact: true }).fill("14");
  await cancelThenConfirm(() => ui.getByRole("button", { name: "保存", exact: true }).click(), "日志保留期保存");
  const nodeRow = ui.getByRole("row").filter({ hasText: "edge-fixture" });
  await nodeRow.getByLabel("编辑节点配置").click();
  const nodeEditor = ui.getByRole("dialog", { name: "编辑节点配置" });
  await nodeEditor.waitFor();
  await cancelThenConfirm(() => ui.getByRole("button", { name: "保存配置", exact: true }).click(), "节点配置保存");
  await nodeEditor.waitFor({ state: "hidden" });
  await ui.screenshot({ path: "output/playwright/node-settings-1440.png" });
  await ui.setViewportSize({ width: 390, height: 844 });
  await ui.screenshot({ path: "output/playwright/node-settings-390.png" });
  await ui.setViewportSize({ width: 1440, height: 1000 });
  await cancelThenConfirm(() => nodeRow.getByLabel("删除节点 edge-fixture").click(), "节点删除");
  await nodeRow.waitFor({ state: "hidden" });
  await ui.getByRole("button", { name: "登记节点", exact: true }).click();
  const registration = ui.getByRole("dialog", { name: "登记节点" });
  await registration.getByLabel("节点 ID", { exact: true }).fill("new-edge");
  await registration.getByLabel("Worker 服务地址", { exact: true }).fill("https://new-edge.example.test");
  await cancelThenConfirm(() => registration.getByRole("button", { name: "保存配置", exact: true }).click(), "节点登记");
  await registration.waitFor({ state: "hidden" });

  await navigate("日志工作台");
  await ui.getByRole("combobox", { name: "选择日志任务", exact: true }).click();
  await ui.getByRole("option", { name: /待启动任务/ }).click();
  await ui.getByRole("tab", { name: "小时归档与检索", exact: true }).click();
  await ui.getByText("小时（北京时间 UTC+8）", { exact: true }).waitFor();
  await ui.getByText("2026/9/10 02:00:00", { exact: true }).waitFor();
  await ui.getByText("hour-fixture", { exact: false }).waitFor().catch(() => ui.locator(".el-table__body tr").first().waitFor());
  await ui.locator(".el-table__body tr").first().locator(".el-checkbox").click();
  await ui.getByRole("button", { name: "下载选中小时", exact: true }).click();
  await ui.getByRole("button", { name: "取消作业", exact: true }).waitFor();
  await cancelThenConfirm(() => ui.getByRole("button", { name: "取消作业", exact: true }).click(), "日志作业取消");
  await ui.getByRole("button", { name: "取消作业", exact: true }).waitFor({ state: "hidden" });

  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, mutations: mutations(), consoleErrors: 0 }));
} finally {
  await browser.close();
}
