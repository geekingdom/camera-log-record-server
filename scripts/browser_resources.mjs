// 设备资源浏览器验收：在页面层 mock API，覆盖认证失效、资源保存与任务的资源绑定。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-ui-token"));
const resources = [
  { id: "serial-fixture", name: "串口服务器 A", kind: "SERIAL_SERVER", ip: "192.0.2.40", version: 1 },
];
const mutations = [];
let authenticationCall = 0;
let releaseAuthentication;
let authenticationStarted;
const authenticationStartedPromise = new Promise(resolve => { authenticationStarted = resolve; });
let releaseSave;
let saveStarted;
const saveStartedPromise = new Promise(resolve => { saveStarted = resolve; });
async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
await context.route("**/api/v1/**", async route => {
  const request = route.request(); const url = new URL(request.url()); const path = url.pathname; const method = request.method();
  const payload = request.postDataJSON?.() ?? {};
  if (method === "GET" && path === "/api/v1/resources") return json(route, { items: resources, total: resources.length, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/tasks") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/command-templates") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/nodes") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "POST" && path === "/api/v1/resources/authenticate") {
    authenticationCall += 1;
    if (authenticationCall === 1) {
      authenticationStarted();
      await new Promise(resolve => { releaseAuthentication = resolve; });
    }
    return json(route, { model: "DS-2CD", subSerialNumber: "SN-fixture", softwareVersion: "V5.7" });
  }
  if (method === "POST" && path === "/api/v1/resources") {
    mutations.push({ method, path, payload });
    if (mutations.filter(item => item.path === path).length === 1) {
      saveStarted();
      await new Promise(resolve => { releaseSave = resolve; });
    }
    const created = { id: "network-fixture", ...payload, model: "DS-2CD", subSerialNumber: "SN-fixture", softwareVersion: "V5.7", version: 1 };
    delete created.password; resources.unshift(created); return json(route, created, 201);
  }
  if (method === "POST" && path === "/api/v1/tasks") {
    mutations.push({ method, path, payload }); return json(route, { id: "task-fixture", ...payload, status: "STOPPED", desiredState: "STOPPED", version: 1 }, 201);
  }
  throw new Error(`未模拟的 API 请求：${method} ${path}`);
});
const page = await context.newPage(); page.setDefaultTimeout(10000);
const errors = []; page.on("pageerror", error => errors.push(error.message)); page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
async function confirmSaveIfNeeded() {
  const dialog = page.locator(".el-message-box");
  if (!await dialog.isVisible().catch(() => false)) return;
  await dialog.locator(".el-button--primary").click();
}
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByLabel("资源名称", { exact: true }).fill("网络设备 A");
  await page.getByLabel("IP 地址", { exact: true }).fill("192.0.2.20");
  await page.getByLabel("用户名", { exact: true }).fill("http-user");
  await page.getByLabel("密码", { exact: true }).fill("http-password");
  await page.waitForTimeout(5200);
  assert.equal(await page.getByLabel("资源名称", { exact: true }).inputValue(), "网络设备 A", "轮询不得销毁资源编辑草稿");
  await page.getByRole("button", { name: "点击认证", exact: true }).click();
  await authenticationStartedPromise;
  await page.getByLabel("IP 地址", { exact: true }).fill("192.0.2.21");
  releaseAuthentication();
  await page.waitForTimeout(200);
  assert.equal(await page.getByRole("button", { name: "保存资源", exact: true }).isDisabled(), true);
  assert.equal(mutations.length, 0, "认证失效后不得保存资源");
  await page.getByRole("button", { name: "点击认证", exact: true }).click();
  await page.getByText("DS-2CD", { exact: true }).waitFor();
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
  await confirmSaveIfNeeded();
  await saveStartedPromise;
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByLabel("资源名称", { exact: true }).fill("关闭后重开资源");
  releaseSave();
  await page.waitForTimeout(200);
  assert.equal(await page.getByLabel("资源名称", { exact: true }).inputValue(), "关闭后重开资源", "过期保存响应不得关闭新抽屉");
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByLabel("资源名称", { exact: true }).fill("网络设备 B");
  await page.getByLabel("IP 地址", { exact: true }).fill("192.0.2.21");
  await page.getByLabel("用户名", { exact: true }).fill("http-user");
  await page.getByLabel("密码", { exact: true }).fill("http-password");
  await page.getByRole("button", { name: "点击认证", exact: true }).click();
  await page.getByRole("button", { name: "保存资源", exact: true }).click();
  await confirmSaveIfNeeded();
  await page.getByText("资源已保存", { exact: true }).waitFor();
  assert.equal(mutations[1].payload.password, "http-password");
  await page.getByRole("button", { name: "查看任务", exact: true }).filter({ hasText: "查看任务" }).first().click();
  await page.getByRole("heading", { name: "网络设备 B · 采集任务", exact: true }).waitFor();
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await page.getByLabel("任务名称", { exact: true }).fill("资源绑定 SSH 任务");
  await page.getByLabel("用户名", { exact: true }).fill("ssh-user");
  await page.getByLabel("密码", { exact: true }).fill("ssh-password");
  const networkTaskRequest = page.waitForRequest(request => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/tasks");
  await page.getByRole("button", { name: "保存任务", exact: true }).click();
  await confirmSaveIfNeeded();
  await networkTaskRequest;
  await page.getByText("任务已保存", { exact: true }).last().waitFor();
  assert.equal(mutations[2].payload.resourceId, "network-fixture");
  assert.equal(mutations[2].payload.ip, "192.0.2.21");
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await page.locator(".el-form-item").filter({ hasText: "连接协议" }).locator(".el-select").click();
  await page.getByRole("option", { name: "Telnet 串口", exact: true }).click();
  await page.getByText("已有串口服务器", { exact: true }).click();
  await page.locator(".el-form-item").filter({ hasText: "串口服务器资源" }).locator(".el-select").click();
  await page.getByRole("option", { name: /串口服务器 A/ }).click();
  assert.equal(await page.getByLabel("串口服务器 IP", { exact: true }).evaluate(input => input.hasAttribute("disabled")), true);
  await page.getByText("自定义地址", { exact: true }).click();
  assert.equal(await page.getByLabel("串口服务器 IP", { exact: true }).evaluate(input => input.hasAttribute("disabled")), false);
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("tab", { name: "设备资源", exact: true }).click();
  const serialRow = page.getByRole("row").filter({ hasText: "串口服务器 A" });
  await serialRow.getByRole("button", { name: "查看任务", exact: true }).click();
  await page.getByRole("heading", { name: "串口服务器 A · 采集任务", exact: true }).waitFor();
  await page.getByRole("button", { name: "新建任务", exact: true }).click();
  await page.getByLabel("任务名称", { exact: true }).fill("串口资源任务");
  assert.equal(await page.getByLabel("串口服务器 IP", { exact: true }).inputValue(), "192.0.2.40");
  assert.equal(await page.getByLabel("串口服务器 IP", { exact: true }).evaluate(input => input.hasAttribute("disabled")), true);
  assert.equal(await page.getByLabel("用户名", { exact: true }).count(), 1, "Telnet 串口应允许填写采集登录用户名");
  await page.getByLabel("用户名", { exact: true }).fill("serial-user");
  await page.getByLabel("密码", { exact: true }).fill("serial-password");
  await page.getByLabel("端口", { exact: true }).fill("10002");
  const serialTaskRequest = page.waitForRequest(request => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/tasks");
  await page.getByRole("button", { name: "保存任务", exact: true }).click();
  await confirmSaveIfNeeded();
  await serialTaskRequest;
  await page.getByText("任务已保存", { exact: true }).last().waitFor();
  assert.equal(mutations[3].payload.resourceId, "serial-fixture");
  assert.equal(mutations[3].payload.serialServerResourceId, null);
  assert.equal(mutations[3].payload.ip, "192.0.2.40");
  assert.equal(mutations[3].payload.port, 10002);
  assert.equal(mutations[3].payload.username, "serial-user");
  assert.equal(mutations[3].payload.password, "serial-password");
  await page.getByRole("tab", { name: "设备资源", exact: true }).click();
  await page.waitForTimeout(3200);
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.getByRole("button", { name: "新建资源", exact: true }).click();
    await page.locator(".el-drawer.open").waitFor();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow, false, `${width}px 资源抽屉不得造成页面横向溢出`);
    await page.screenshot({ path: `output/playwright/resources-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "关闭", exact: true }).click();
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  assert.equal(overflow, false); assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, resourceMutations: 2, taskMutations: 2, screenshots: "output/playwright/resources-{1440,390,320}.png", consoleErrors: 0 }));
} finally { await browser.close(); }
