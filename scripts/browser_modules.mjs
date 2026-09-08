// 模块化工作区浏览器验收：全部 API 都在浏览器层 mock，绝不使用真实凭据或设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const output = "output/playwright";
const timestamp = "2026-09-08T08:00:00.000Z";
const task = {
  id: "task-fixture", name: "模拟日志任务", protocol: "TELNET_SERIAL", ip: "192.0.2.10", port: 2001,
  status: "STOPPED", desiredState: "STOPPED", initialCommands: [], scheduledCommands: [], updatedAt: timestamp,
};
const state = {
  platform: { retentionDays: 7, version: 1, updatedAt: timestamp },
  nodes: [{
    id: "edge-fixture", url: "https://edge-fixture.example.test:8443",
    reportedUrl: "https://edge-fixture.example.test:8443", capacity: 8, accepting: true,
    version: 0, registered: false, online: true, reportedAt: timestamp, activeTasks: 1,
  }],
  tokens: [{
    id: "token-fixture", name: "既有服务账号", scopes: ["tasks:read"], taskIds: null,
    expiresAt: "2030-01-01T00:00:00.000Z", createdAt: timestamp, revoked: false,
  }],
  mutations: [],
};

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-ui-token"));

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

await context.route("**/api/v1/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const method = request.method();
  const path = url.pathname;
  const payload = request.postDataJSON?.() ?? {};
  if (method !== "GET") state.mutations.push({ method, path, payload });

  if (method === "GET" && path === "/api/v1/tasks")
    return json(route, { items: [task], total: 1, page: 1, pageSize: 100 });
  if (method === "GET" && path === "/api/v1/command-templates")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (method === "GET" && path === "/api/v1/nodes")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (method === "GET" && path === "/api/v1/platform-settings") return json(route, state.platform);
  if (method === "PATCH" && path === "/api/v1/platform-settings") {
    state.platform = { ...state.platform, retentionDays: payload.retentionDays, version: state.platform.version + 1 };
    return json(route, state.platform);
  }
  if (method === "GET" && path === "/api/v1/admin/nodes") return json(route, { items: state.nodes });
  if (method === "POST" && path === "/api/v1/admin/nodes") {
    const node = { ...payload, registered: true, version: 1, online: true, reportedAt: timestamp, reportedUrl: payload.url };
    state.nodes = state.nodes.map((item) => item.id === node.id ? node : item);
    return json(route, node, 201);
  }
  if (method === "PATCH" && path.startsWith("/api/v1/admin/nodes/")) {
    const identifier = decodeURIComponent(path.split("/").at(-1));
    const index = state.nodes.findIndex((node) => node.id === identifier);
    state.nodes[index] = { ...state.nodes[index], ...payload, version: state.nodes[index].version + 1 };
    return json(route, state.nodes[index]);
  }
  if (method === "GET" && path === "/api/v1/service-tokens")
    return json(route, { items: state.tokens, total: state.tokens.length, page: 1, pageSize: 20 });
  if (method === "POST" && path === "/api/v1/service-tokens") {
    const token = {
      id: "created-token", name: payload.name, scopes: payload.scopes, taskIds: payload.taskIds ?? null,
      expiresAt: "2030-02-01T00:00:00.000Z", createdAt: timestamp, revoked: false,
    };
    state.tokens.unshift(token);
    return json(route, { ...token, token: "synthetic-one-time-token" }, 201);
  }
  if (method === "DELETE" && path.startsWith("/api/v1/service-tokens/")) {
    const identifier = decodeURIComponent(path.split("/").at(-1));
    const token = state.tokens.find((item) => item.id === identifier);
    if (token) token.revoked = true;
    return route.fulfill({ status: 204 });
  }
  if (method === "GET" && path === "/api/v1/audit-events") return json(route, {
    items: [{ id: "audit-fixture", action: "update_platform_settings", actor: "bootstrap", targetId: "platform", createdAt: timestamp }],
    total: 1, page: 1, pageSize: 50,
  });
  if (method === "GET" && path === "/api/v1/runtime-events") return json(route, {
    items: [{ id: "event-fixture", type: "DISK_PRESSURE_CHANGED", nodeId: "edge-fixture", createdAt: timestamp }],
    total: 1, page: 1, pageSize: 50,
  });
  throw new Error(`未模拟的 API 请求：${method} ${path}`);
});

const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });

async function assertNoHorizontalOverflow(label) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  assert.equal(overflow, false, `${label} 存在水平溢出`);
}

async function navigate(label) {
  await page.getByRole("tab", { name: label, exact: true }).click();
  await page.locator("h1").filter({ hasText: label }).waitFor();
}

try {
  await mkdir(output, { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "采集任务", exact: true }).waitFor();

  await navigate("后台配置");
  await page.getByLabel("日志保存天数", { exact: true }).fill("14");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByText("日志保留期已保存", { exact: true }).waitFor();
  assert.equal(state.platform.retentionDays, 14);
  await page.getByRole("button", { name: "登记此节点", exact: true }).click();
  const registerDialog = page.getByRole("dialog", { name: "登记节点" });
  await registerDialog.getByLabel("节点 ID", { exact: true }).waitFor();
  await registerDialog.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByText("节点已登记", { exact: true }).waitFor();
  await page.getByText("已登记", { exact: true }).waitFor();
  await page.getByRole("button", { name: "编辑节点配置", exact: true }).click();
  const editDialog = page.getByRole("dialog", { name: "编辑节点配置" });
  await editDialog.locator(".el-switch").click();
  await editDialog.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByText("节点配置已保存", { exact: true }).waitFor();
  assert.equal(state.nodes[0].accepting, false);
  await page.waitForTimeout(3200);
  await page.screenshot({ path: `${output}/modules-settings-desktop.png`, fullPage: true });
  await assertNoHorizontalOverflow("桌面后台配置");

  await navigate("服务账号");
  await page.getByRole("button", { name: "新建服务账号", exact: true }).click();
  const accountDialog = page.getByRole("dialog", { name: "新建第三方服务账号" });
  await accountDialog.getByLabel("账号名称", { exact: true }).fill("浏览器验收账号");
  await accountDialog.locator(".el-select").click();
  await page.getByRole("option", { name: "tasks:read", exact: true }).click();
  await page.keyboard.press("Escape");
  await accountDialog.getByRole("button", { name: "创建并显示口令", exact: true }).click();
  const secretDialog = page.getByRole("dialog", { name: "请立即保存服务账号口令" });
  await secretDialog.getByLabel("一次性服务账号口令", { exact: true }).waitFor();
  await secretDialog.getByRole("button", { name: "我已保存口令", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "浏览器验收账号" }).getByRole("button", { name: "撤销服务账号" }).click();
  await page.getByRole("button", { name: "撤销账号", exact: true }).click();
  await page.getByText("服务账号已撤销", { exact: true }).waitFor();
  assert.equal(state.tokens.find((item) => item.id === "created-token")?.revoked, true);
  await page.waitForTimeout(3200);
  await page.screenshot({ path: `${output}/modules-access-desktop.png`, fullPage: true });
  await assertNoHorizontalOverflow("桌面服务账号");

  await navigate("审计与事件");
  await page.getByRole("cell", { name: "update_platform_settings", exact: true }).click();
  await page.locator(".event-details").getByText("update_platform_settings", { exact: false }).waitFor();
  await page.screenshot({ path: `${output}/modules-audit-desktop.png`, fullPage: true });
  await assertNoHorizontalOverflow("桌面审计");

  await navigate("日志工作台");
  await page.getByRole("combobox", { name: "选择日志任务", exact: true }).waitFor();
  await page.screenshot({ path: `${output}/modules-logs-desktop.png`, fullPage: true });
  await assertNoHorizontalOverflow("桌面日志工作台");

  await page.setViewportSize({ width: 390, height: 844 });
  await navigate("后台配置");
  await page.screenshot({ path: `${output}/modules-settings-mobile.png`, fullPage: true });
  await assertNoHorizontalOverflow("移动后台配置");
  await navigate("服务账号");
  await page.screenshot({ path: `${output}/modules-access-mobile.png`, fullPage: true });
  await assertNoHorizontalOverflow("移动服务账号");
  await navigate("审计与事件");
  await page.screenshot({ path: `${output}/modules-audit-mobile.png`, fullPage: true });
  await assertNoHorizontalOverflow("移动审计");
  await navigate("日志工作台");
  await page.screenshot({ path: `${output}/modules-logs-mobile.png`, fullPage: true });
  await assertNoHorizontalOverflow("移动日志工作台");

  assert.deepEqual(errors, []);
  assert.deepEqual(state.mutations.map((item) => `${item.method} ${item.path}`), [
    "PATCH /api/v1/platform-settings", "POST /api/v1/admin/nodes", "PATCH /api/v1/admin/nodes/edge-fixture",
    "POST /api/v1/service-tokens", "DELETE /api/v1/service-tokens/created-token",
  ]);
  console.log(JSON.stringify({ passed: true, screenshots: output, mutations: state.mutations.length, consoleErrors: 0 }));
} finally {
  await browser.close();
}
