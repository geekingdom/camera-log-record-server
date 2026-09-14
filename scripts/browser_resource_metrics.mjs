// 资源监控浏览器验收：全部 API 在页面路由层模拟，不连接真实设备或服务端采集器。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "resource-metrics-test-token"));
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const resources = [
  { id: "metrics-resource", name: "趋势设备", kind: "HIKVISION_NETWORK", ip: "192.0.2.30", username: "http", authType: "DIGEST", version: 1, enableResourceMonitor: true, healthStatus: "ONLINE" },
  { id: "disabled-resource", name: "未启用趋势", kind: "HIKVISION_NETWORK", ip: "192.0.2.31", version: 1, enableResourceMonitor: false },
  { id: "serial-resource", name: "串口资源", kind: "SERIAL_SERVER", ip: "192.0.2.32", version: 1, enableResourceMonitor: true },
];
let metricRequests = 0;
let failNextMetrics = false;
let delayNextMetrics = false;
let delayedMetricsStarted;
let releaseDelayedMetrics;
const metricUrls = [];
async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
await context.route("**/api/v1/**", async route => {
  const request = route.request(); const url = new URL(request.url()); const path = url.pathname; const method = request.method();
  if (method === "GET" && path === "/api/v1/auth/me") return json(route, { user: { id: "reader", username: "reader", displayName: "只读用户", scopes: ["tasks:read", "logs:read"], isAdmin: false, enabled: true, mustChangePassword: false } });
  if (method === "GET" && path === "/api/v1/resources") return json(route, { items: resources, total: resources.length, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/tasks") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/command-templates") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/nodes") return json(route, { items: [], total: 0, page: 1, pageSize: 20 });
  if (method === "GET" && path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1 });
  if (method === "GET" && path === "/api/v1/resources/metrics-resource/resource-metrics") {
    metricRequests += 1; metricUrls.push(url);
    if (delayNextMetrics) {
      delayNextMetrics = false; delayedMetricsStarted?.();
      await new Promise(resolve => { releaseDelayedMetrics = resolve; });
    }
    if (failNextMetrics) { failNextMetrics = false; return json(route, { message: "模拟监控读取失败" }, 500); }
    if (!url.searchParams.get("cursor")) return json(route, {
      items: [
        { sampledAt: "2026-09-11T00:02:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 21, unit: "%" }, { id: "process:dsp:99", name: "Dsp_Main", pid: 99, value: 2048, unit: "KB" }] },
        { sampledAt: "2026-09-11T00:01:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 18, unit: "%" }, { id: "proc", name: "Dsp_Main", pid: 12, value: 2010, unit: "KB" }] },
      ], nextCursor: "next-page",
    });
    return json(route, { items: [{ sampledAt: "2026-09-11T00:00:00.000Z", status: "TIMEOUT", errorCode: "COMMAND_TIMEOUT", values: [] }], nextCursor: null });
  }
  throw new Error(`未模拟的 API 请求：${method} ${path}`);
});
const page = await context.newPage(); page.setDefaultTimeout(12_000);
const errors = []; page.on("pageerror", error => errors.push(error.message)); page.on("console", message => {
  if (message.type() === "error" && !message.text().includes("Failed to load resource: the server responded with a status of 500")) errors.push(message.text());
});
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(await page.getByLabel("查看 CPU 与内存趋势").count(), 1, "仅启用的海康资源显示趋势入口");
  await page.getByLabel("查看 CPU 与内存趋势").click();
  try { await page.getByText(/内存（(KB|MB|GB)）/, { exact: true }).waitFor(); }
  catch (cause) { throw new Error(`趋势弹窗未渲染：${JSON.stringify({ dialogs: await page.getByRole("dialog").allTextContents(), errors })}`, { cause }); }
  await page.locator(".metrics-chart canvas").first().waitFor();
  assert.equal(await page.getByText("Dsp_Main 最新值", { exact: true }).count(), 1, "PID变化后仅有一个同名进程摘要");
  assert.equal(await page.getByText(/Dsp_Main \(PID/).count(), 0, "进程对象不显示PID后缀");
  await page.getByText("比率", { exact: true }).click();
  await page.getByRole("heading", { name: "内存比率（初始有效值 = 100%）" }).waitFor();
  await page.mouse.move(10, 10);
  await page.locator(".metrics-toolbar input:focus").evaluateAll(elements => elements.forEach(element => element.blur()));
  await page.waitForTimeout(300);
  await page.screenshot({ path: "output/playwright/resource-metrics-relative-1440.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(250);
  await page.getByText("比率", { exact: true }).hover();
  const rateHint = page.getByRole("tooltip").filter({ hasText: "首个有效采样" });
  await rateHint.waitFor();
  const hintBounds = await rateHint.boundingBox();
  assert.ok(hintBounds && hintBounds.x >= 0 && hintBounds.x + hintBounds.width <= 390, "比率说明不得超出窄屏");
  await page.mouse.move(10, 10);
  await page.locator(":focus").evaluateAll(elements => elements.forEach(element => element.blur()));
  await rateHint.waitFor({ state: "hidden" });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "比率模式窄屏不得横向溢出");
  await page.screenshot({ path: "output/playwright/resource-metrics-relative-390.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByText("原始数值", { exact: true }).click();
  assert.ok(await page.locator(".metrics-chart canvas").first().evaluate(canvas => canvas.width > 0 && canvas.height > 0), "CPU 图表 canvas 应可绘制");
  await page.getByText("CPU", { exact: true }).first().click();
  await page.getByText("CPU（%）", { exact: true }).waitFor();
  await page.screenshot({ path: "output/playwright/resource-metrics-cpu.png", fullPage: true });
  await page.getByText("内存", { exact: true }).first().click();
  await page.getByText(/内存（(KB|MB|GB)）/, { exact: true }).waitFor();
  assert.equal(metricUrls[0].searchParams.get("limit"), "2000");
  assert.ok(metricUrls[0].searchParams.get("start")); assert.ok(metricUrls[0].searchParams.get("end"));
  assert.equal(metricUrls.some(url => url.searchParams.get("cursor") === "next-page"), true, "应消费服务端游标");
  const delayed = new Promise(resolve => { delayedMetricsStarted = resolve; });
  delayNextMetrics = true;
  await page.getByRole("button", { name: "刷新趋势", exact: true }).click();
  await delayed;
  await page.locator(".resource-metrics-dialog .el-dialog__headerbtn").click();
  await page.getByLabel("查看 CPU 与内存趋势").click();
  releaseDelayedMetrics();
  await page.getByText(/内存（(KB|MB|GB)）/, { exact: true }).waitFor();
  assert.ok(metricRequests >= 4, "关闭后重新打开必须发起新请求，不能被旧 loading 状态卡住");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 CSV", exact: true }).click();
  const csv = await download;
  assert.match(await csv.suggestedFilename(), /resource-metrics\.csv$/);
  const beforePause = metricRequests;
  await page.getByRole("button", { name: "暂停自动刷新", exact: true }).click();
  await page.waitForTimeout(100);
  assert.equal(metricRequests, beforePause, "暂停后不得立即产生额外请求");
  failNextMetrics = true;
  await page.getByRole("button", { name: "刷新趋势", exact: true }).click();
  await page.getByText("模拟监控读取失败", { exact: true }).waitFor();
  await page.getByRole("button", { name: "刷新趋势", exact: true }).click();
  await page.getByText(/^内存（(?:KB|MB|GB)）$/).waitFor();
  await page.getByText("自定义", { exact: true }).click();
  const customStart = new Date(Date.now() - 3_600_000), customEnd = new Date(Date.now() - 60_000);
  const localTime = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:00`;
  await page.locator('.metrics-custom-range .el-range-input[placeholder="开始时间"]').fill(localTime(customStart));
  await page.locator('.metrics-custom-range .el-range-input[placeholder="结束时间"]').fill(localTime(customEnd));
  await page.locator('.metrics-custom-range .el-range-input[placeholder="结束时间"]').press("Tab");
  const beforeCustom = metricRequests;
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.metrics-commands .is-loading'));
  assert.ok(metricRequests > beforeCustom, "自定义查询必须真正发出新请求");
  assert.equal(Date.parse(metricUrls.at(-1).searchParams.get("start")), new Date(localTime(customStart)).getTime());
  assert.equal(Date.parse(metricUrls.at(-1).searchParams.get("end")), new Date(localTime(customEnd)).getTime());
  await page.getByText("最近 1 小时", { exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.metrics-commands .is-loading'));
  await page.mouse.move(10, 10);
  await page.locator("button:focus").evaluateAll(elements => elements.forEach(element => element.blur()));
  await page.waitForTimeout(500);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.waitForTimeout(250);
    await page.screenshot({ path: `output/playwright/resource-metrics-${width}.png`, fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `${width}px 不得横向溢出`);
  }
  await page.locator(".resource-metrics-dialog .el-dialog__headerbtn").click();
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, metricRequests, screenshots: "output/playwright/resource-metrics-{1440,390}.png", consoleErrors: 0 }));
} finally { await browser.close(); }
