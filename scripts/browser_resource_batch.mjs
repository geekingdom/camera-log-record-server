// 批量资源浏览器回归：模拟认证失败、逐行修正、二次确认与原请求幂等重试。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const resources = [];
const batches = [];
const singles = [];
const calls = [];
let active = 0;
let peak = 0;
async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
await context.route("**/api/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const method = request.method();
  if (path === "/api/v1/auth/me") return json(route, { user: {
    id: "batch-admin", username: "batch-admin", displayName: "批量验收管理员", isAdmin: true,
    enabled: true, mustChangePassword: false, scopes: ["tasks:read", "resources:create", "resources:write"],
  } });
  if (method === "GET" && path === "/api/v1/resources")
    return json(route, { items: resources, total: resources.length, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/users/share-targets")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (method === "GET" && ["/api/v1/tasks", "/api/v1/nodes", "/api/v1/command-templates", "/api/v1/users/creators"].includes(path))
    return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  if (method === "POST" && path === "/api/v1/resources/authenticate") {
    const body = request.postDataJSON();
    calls.push(body);
    active += 1; peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 40));
    active -= 1;
    if (body.ip === "192.0.2.21" && body.password !== "row-correct-password")
      return json(route, { error: { code: "AUTH_FAILED", message: "设备用户名或密码错误" } }, 401);
    return json(route, { model: "DS-batch", subSerialNumber: `SN-${body.ip}`, softwareVersion: "V5.10" });
  }
  if (method === "POST" && path === "/api/v1/resources/batch") {
    const body = request.postDataJSON();
    batches.push({ body, key: request.headers()["idempotency-key"] });
    if (batches.length === 1)
      return json(route, { error: { code: "UNKNOWN_COMMIT", message: "创建提交结果未知，请使用相同幂等键重试" } }, 503);
    const items = body.items.map((item, index) => {
      const { password: _password, ...safe } = item;
      return { ...safe, id: `batch-resource-${index}`, version: 1, healthStatus: "ONLINE", createdBy: "batch-admin" };
    });
    resources.push(...items);
    return json(route, { items }, 201);
  }
  if (method === "POST" && path === "/api/v1/resources") {
    const body = request.postDataJSON(); singles.push(body);
    const { password: _password, ...safe } = body;
    const result = { ...safe, name: body.name?.trim() || body.ip, id: "single-resource", version: 1 };
    resources.push(result);
    return json(route, result, 201);
  }
  throw new Error(`未模拟接口：${method} ${path}`);
});
const page = await context.newPage();
page.setDefaultTimeout(12000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => {
  if (message.type() !== "error") return;
  const knownFailure = message.location().url.endsWith("/resources/authenticate") && message.text().includes("401")
    || message.location().url.endsWith("/resources/batch") && message.text().includes("503");
  if (!knownFailure) errors.push(message.text());
});
async function confirmIfShown() {
  const confirmation = page.locator(".el-message-box");
  if (await confirmation.isVisible()) {
    await confirmation.getByRole("button", { name: "确认", exact: true }).click();
    await confirmation.waitFor({ state: "hidden" });
  }
}
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto(`${baseUrl.replace(/\/$/, "")}/#resources`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByText("批量添加", { exact: true }).click();
  await page.getByLabel("起始 IP", { exact: true }).fill("192.0.2.20");
  await page.getByLabel("结束 IP", { exact: true }).fill("192.0.2.22");
  await page.getByLabel("批量用户名", { exact: true }).fill("batch-device-user");
  await page.getByLabel("批量密码", { exact: true }).fill("shared-password");
  await page.getByRole("button", { name: "批量认证", exact: true }).click();
  const add = page.getByRole("button", { name: "添加设备", exact: true });
  const rowPassword = page.getByLabel("密码 192.0.2.21", { exact: true });
  await rowPassword.waitFor();
  await page.getByText("设备用户名或密码错误", { exact: false }).first().waitFor();
  assert.equal(await add.isDisabled(), true, "存在认证失败时不得添加");
  assert.equal(batches.length, 0);
  await rowPassword.fill("row-correct-password");
  await page.getByRole("button", { name: "立即认证 192.0.2.21", exact: true }).click();
  await page.waitForFunction(() => [...document.querySelectorAll("button")].some(button => button.textContent?.trim() === "添加设备" && !button.disabled));
  await page.getByLabel("结束 IP", { exact: true }).fill("192.0.2.23");
  assert.equal(await add.isDisabled(), true, "范围草稿变更后不得提交旧设备列表");
  await page.getByRole("button", { name: "批量认证", exact: true }).click();
  await page.locator(".el-message-box").getByRole("button", { name: "取消", exact: true }).click();
  await page.locator(".el-message-box").waitFor({ state: "hidden" });
  assert.equal(await page.getByLabel("密码 192.0.2.21", { exact: true }).inputValue(), "row-correct-password", "取消范围替换必须保留单行凭据");
  await page.getByLabel("结束 IP", { exact: true }).fill("192.0.2.22");
  await page.getByLabel("批量用户名", { exact: true }).fill("changed-user");
  assert.equal(await add.isDisabled(), true, "共用凭据草稿变更后不得提交旧认证结果");
  await page.getByLabel("批量用户名", { exact: true }).press("Tab");
  await page.locator(".el-message-box").getByRole("button", { name: "取消", exact: true }).click();
  await page.locator(".el-message-box").waitFor({ state: "hidden" });
  assert.equal(await page.getByLabel("批量用户名", { exact: true }).inputValue(), "batch-device-user");
  await page.locator(".el-switch").filter({ has: page.getByLabel("批量启用 Coredump 监控", { exact: true }) }).click();
  await page.locator(".el-switch").filter({ has: page.getByLabel("批量启用 CPU 与内存监控", { exact: true }) }).click();
  await page.getByLabel("资源名称 192.0.2.20", { exact: true }).fill("入口测试设备");
  await page.getByRole("button", { name: "删除 192.0.2.22", exact: true }).click();
  await page.locator(".el-message-box").getByRole("button", { name: "取消", exact: true }).click();
  assert.equal(await page.getByLabel("资源名称 192.0.2.22", { exact: true }).count(), 1);
  await page.getByRole("button", { name: "删除 192.0.2.22", exact: true }).click();
  await confirmIfShown();
  await page.getByLabel("资源名称 192.0.2.22", { exact: true }).waitFor({ state: "detached" });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.locator(".batch-resource-table .el-scrollbar__wrap").evaluate(element => { element.scrollLeft = 0; });
    await add.scrollIntoViewIfNeeded();
    await page.waitForTimeout(350);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const box = await add.boundingBox();
    assert.ok(box && box.y >= 0 && box.y + box.height <= 900, "添加按钮须始终可达");
    await page.screenshot({ path: `output/playwright/resource-batch-${width}.png`, fullPage: true });
  }
  await add.click(); await confirmIfShown();
  await page.getByText("创建提交结果未知", { exact: false }).first().waitFor();
  await add.click(); await confirmIfShown();
  await page.getByRole("dialog", { name: "新建设备资源", exact: true }).waitFor({ state: "hidden" });
  assert.equal(batches.length, 2);
  assert.ok(batches[0].key);
  assert.equal(batches[0].key, batches[1].key, "未知提交原请求重试必须保留幂等键");
  assert.deepEqual(batches[0].body, batches[1].body);
  assert.equal(batches[1].body.items.length, 2);
  assert.equal(batches[1].body.items[0].name, "入口测试设备");
  assert.equal(batches[1].body.items[1].password, "row-correct-password");
  assert.ok(batches[1].body.items.every(item => item.enableCoredumpMonitor && item.enableResourceMonitor));
  assert.ok(peak <= 4);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByLabel("IP 地址", { exact: true }).fill("192.0.2.30");
  await page.locator(".el-form-item").filter({ hasText: "资源类型" }).locator(".el-select").click();
  await page.getByRole("option", { name: "串口服务器", exact: true }).click();
  assert.equal(await page.getByText("批量添加", { exact: true }).count(), 0, "串口服务器不得提供批量入口");
  await page.getByRole("button", { name: "保存资源", exact: true }).click(); await confirmIfShown();
  await page.getByRole("dialog", { name: "新建设备资源", exact: true }).waitFor({ state: "hidden" });
  assert.equal(singles.length, 1);
  assert.equal(resources.at(-1).name, "192.0.2.30");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, previewCalls: calls.length, batchRetries: batches.length, peak,
    screenshots: "output/playwright/resource-batch-{1440,390}.png" }));
} finally { await browser.close(); }
