// 旧模板字段与工作区切换回归：浏览器层模拟 API，验证实际组件不因历史文档缺字段而残留。
import assert from "node:assert/strict";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const timestamp = "2026-09-09T12:00:00.000Z";
const administrator = {
  id: "legacy-template-admin", username: "legacy-template-admin", displayName: "历史模板验收管理员",
  isAdmin: true, scopes: ["*"], enabled: true, mustChangePassword: false, version: 1,
};
const resource = {
  id: "legacy-resource", name: "旧模板切换验证资源", kind: "SERIAL_SERVER", ip: "192.0.2.33", version: 1,
};
const task = {
  id: "legacy-task", name: "旧模板切换验证任务", resourceId: resource.id, protocol: "TELNET_SERIAL",
  ip: resource.ip, port: 2001, status: "STOPPED", desiredState: "STOPPED", initialCommands: [], scheduledCommands: [], version: 1,
};
// 该对象刻意保持升级前形状，不能补填 sharedWith、sharedWithAll 或创建者字段。
const legacyTemplate = { id: "legacy-template", name: "历史命令模板", description: "缺少共享字段", initialCommands: [], scheduledCommands: [], version: 1 };
const catalog = {
  version: "fixture", guides: [{ title: "鉴权", text: "浏览器验收夹具" }], schemas: {}, errorExample: { error: { code: "400" } },
  operations: [{ id: "tasks-list", method: "GET", path: "/api/v1/tasks", title: "查询采集任务", group: "任务", description: "读取任务列表", permission: "tasks:read", headers: {}, parameters: [], requestExample: null, responseStatus: 200, responseExample: { items: [] } }],
};

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });

async function json(route, body) {
  await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
}

await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  if (request.method() !== "GET") throw new Error(`不应发出写请求：${request.method()} ${path}`);
  if (path === "/api/v1/auth/me") return json(route, { user: administrator });
  if (path === "/api/v1/tasks") return json(route, { items: [task], total: 1, page: 1, pageSize: 20 });
  if (path === "/api/v1/resources") return json(route, { items: [resource], total: 1, page: 1, pageSize: 20 });
  if (path === "/api/v1/command-templates") return json(route, { items: [legacyTemplate], total: 1, page: 1, pageSize: 100 });
  if (path === "/api/v1/nodes" || path === "/api/v1/admin/nodes") return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1, updatedAt: timestamp });
  if (path === "/api/v1/api-reference") return json(route, catalog);
  throw new Error(`未模拟的读取请求：${path}`);
});

const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => {
  if (message.type() === "error") errors.push(message.text());
});

async function switchTo(label, locator) {
  await page.getByRole("tab", { name: label, exact: true }).click();
  await locator.waitFor();
  assert.equal(await page.locator(".data-table").filter({ hasText: "历史命令模板" }).count(), 0,
    `${label} 不能残留命令模板表格`);
}

async function enterTemplates() {
  await page.getByRole("tab", { name: "命令模板", exact: true }).click();
  await page.getByRole("cell", { name: "历史命令模板", exact: true }).waitFor();
  await page.getByText("仅创建者", { exact: true }).waitFor();
}

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();

  await enterTemplates();

  for (let iteration = 0; iteration < 3; iteration += 1) {
    if (iteration) await enterTemplates();
    await switchTo("设备资源", page.getByRole("heading", { name: "设备资源目录", exact: true }));
    await page.getByText(resource.name, { exact: true }).waitFor();
    await switchTo("日志工作台", page.getByText("未选择采集任务", { exact: true }));
    await switchTo("API 文档", page.locator(".api-reference-grid"));
    await page.locator(".api-operation-list > button").getByText("查询采集任务", { exact: true }).waitFor();
    await switchTo("后台配置", page.getByLabel("日志保存天数", { exact: true }));
  }

  for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 760 }]) {
    await page.setViewportSize(viewport);
    await enterTemplates();
    await switchTo("设备资源", page.getByRole("heading", { name: "设备资源目录", exact: true }));
    await switchTo("日志工作台", page.getByText("未选择采集任务", { exact: true }));
    await switchTo("API 文档", page.locator(".api-reference-grid"));
    await switchTo("后台配置", page.getByLabel("日志保存天数", { exact: true }));
  }

  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, legacyTemplateFields: "missing", switchCycles: 3, consoleErrors: 0 }));
} finally {
  await context.close();
  await browser.close();
}
