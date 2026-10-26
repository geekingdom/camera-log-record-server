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
const dateRequests = [];
let failDates = false;
let metricRequests = 0;
const deletedAt = "2026-09-11T00:03:00.000Z";
let deletedRequests = 0;
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
  if (method === "GET" && path.endsWith("/resource-metric-dates")) {
    dateRequests.push(url);
    if (failDates) return json(route, {detail: "模拟日期查询失败"}, 500);
    const month = url.searchParams.get("month");
    return json(route, {days: [`${month}-11`]});
  }
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
        { sampledAt: "2026-09-11T00:02:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 21.123456, unit: "%" }, { id: "mem-available", name: "MemAvailable", value: 504824, unit: "KB" }, { id: "slab", name: "Slab", value: 43911, unit: "KB" }, { id: "process:dsp:99", name: "Dsp_Main", pid: 99, value: 56504, unit: "KB" }, { id: "process:zero", name: "真实零内存", value: 0, unit: "KB" }] },
        { sampledAt: "2026-09-11T00:01:00.000Z", status: "OK", values: [{ id: "cpu", name: "CPU", value: 18, unit: "%" }, { id: "proc", name: "Dsp_Main", pid: 12, value: 56000, unit: "KB" }, { id: "process:bll", name: "facesnap_bll", value: 4096, unit: "KB" }, { id: "process:zero", name: "真实零内存", value: 0, unit: "KB" }] },
      ], nextCursor: "next-page",
    });
    return json(route, { items: [{ sampledAt: "2026-09-11T00:00:00.000Z", status: "TIMEOUT", errorCode: "COMMAND_TIMEOUT", values: [] }], nextCursor: null });
  }
  if (method === "GET" && path === "/api/v1/resources/deleted-resource/resource-metrics") {
    deletedRequests += 1;
    assert.equal(url.searchParams.get("end"), deletedAt);
    assert.equal(url.searchParams.get("start"), "2026-09-10T23:03:00.000Z");
    return json(route, {items: [{sampledAt: "2026-09-11T00:02:00.000Z", status: "OK", values: [{id: "cpu", name: "CPU", unit: "%", value: 21}, {id: "mem", name: "内存", unit: "KB", value: 2048}]}], nextCursor: null, owner: null});
  }
  throw new Error(`未模拟的 API 请求：${method} ${path}`);
});
const page = await context.newPage(); page.setDefaultTimeout(12_000);
const errors = []; page.on("pageerror", error => errors.push(error.message)); page.on("console", message => {
  if (message.type() === "error" && !message.text().includes("Failed to load resource: the server responded with a status of 500")) errors.push(message.text());
});
// ECharts 通过 ResizeObserver 异步应用视口变化；等待画布实际匹配容器后再检查页面宽度。
async function waitForChartLayout(viewportWidth) {
  await page.waitForFunction(() => {
    const chart = document.querySelector(".resource-metrics-dialog .metrics-chart");
    const canvas = chart?.querySelector("canvas");
    if (!chart || !canvas) return false;
    const chartBounds = chart.getBoundingClientRect();
    const canvasBounds = canvas.getBoundingClientRect();
    return chartBounds.width > 0
      && Math.abs(canvasBounds.width - chartBounds.width) <= 1
      && document.documentElement.scrollWidth <= window.innerWidth;
  });
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth),
    false,
    `${viewportWidth}px 趋势面板不得横向溢出`,
  );
}
try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto(`${baseUrl.replace(/\/$/, "")}/#resources`, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "设备资源", exact: true }).waitFor();
  assert.equal(await page.getByLabel("查看 CPU 与内存趋势").count(), 1, "仅启用的海康资源显示趋势入口");
  await page.getByLabel("查看 CPU 与内存趋势").click();
  try { await page.getByText(/内存（(KB|MB|GB)）/, { exact: true }).waitFor(); }
  catch (cause) { throw new Error(`趋势弹窗未渲染：${JSON.stringify({ dialogs: await page.getByRole("dialog").allTextContents(), errors })}`, { cause }); }
  await page.locator(".metrics-chart canvas").first().waitFor();
  assert.equal(await page.getByText("Dsp_Main 最新值", { exact: true }).count(), 1, "PID变化后仅有一个同名进程摘要");
  assert.equal(await page.getByText(/Dsp_Main \(PID/).count(), 0, "进程对象不显示PID后缀");
  assert.equal(await page.locator(".metrics-summary strong").filter({ hasText: /^504,824 KB$/ }).count(), 1, "摘要保留原始 KB 数值");
  assert.equal(await page.locator(".metrics-summary strong").filter({ hasText: /^43,911 KB$/ }).count(), 1, "摘要保留原始 Slab KB 数值");
  const missingSummary = page.locator(".metrics-summary > div").filter({ hasText: "facesnap_bll 最新值" });
  assert.equal(await missingSummary.getByText("暂无数据", { exact: true }).count(), 1, "缺失进程不得显示为 0 或沿用旧值");
  const zeroSummary = page.locator(".metrics-summary > div").filter({ hasText: "真实零内存 最新值" });
  assert.equal(await zeroSummary.locator("strong").filter({ hasText: /^0 KB$/ }).count(), 1, "真实零值必须保留，不能显示为缺失");
  await page.locator(".metrics-chart").scrollIntoViewIfNeeded();
  const chartCanvas = page.locator(".metrics-chart canvas").first();
  const chartBounds = await chartCanvas.boundingBox();
  assert.ok(chartBounds, "内存图必须有可悬停的绘制区域");
  // canvas 出现不代表加载遮罩已退场；locator.hover 等待稳定与命中测试通过，
  // 避免一次性页面坐标移动被遮罩吞掉后一直等不到 tooltip。落点留在绘图区内。
  await chartCanvas.hover({ position: { x: chartBounds.width - 40, y: chartBounds.height / 2 } });
  const metricTooltip = page.locator(".resource-metrics-chart-tooltip");
  await metricTooltip.waitFor({ state: "visible" });
  const tooltipText = await metricTooltip.innerText();
  assert.match(tooltipText, /MemAvailable[\s\S]*492\.99 MB/, "tooltip 内存值最多两位小数");
  assert.match(tooltipText, /Slab[\s\S]*42\.88 MB/, "tooltip Slab 值最多两位小数");
  assert.match(tooltipText, /真实零内存[\s\S]*0 MB/, "tooltip 必须保留真实零值");
  assert.match(tooltipText, /facesnap_bll[\s\S]*暂无数据/, "tooltip 缺失进程必须显示暂无数据");
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
    await waitForChartLayout(width);
    await page.screenshot({ path: `output/playwright/resource-metrics-${width}.png`, fullPage: true });
  }
  await page.locator(".resource-metrics-dialog .el-dialog__headerbtn").click();
  resources.push({id: "deleted-resource", name: "已删除趋势设备", kind: "HIKVISION_NETWORK", ip: "192.0.2.33", version: 2, deletedAt, enableResourceMonitor: false});
  await page.reload({waitUntil: "networkidle"});
  const deletedRow = page.getByRole("row").filter({hasText: "已删除趋势设备"});
  await deletedRow.getByLabel("查看 CPU 与内存趋势").click();
  await page.getByText("删除前 1 小时", {exact: true}).waitFor();
  await page.locator(".metrics-chart canvas").first().waitFor();
  assert.equal(deletedRequests, 1);
  assert.equal(await page.getByRole("button", {name: "恢复自动刷新", exact:true}).count(), 0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 844});
    await waitForChartLayout(width);
    await page.screenshot({path: `output/playwright/deleted-resource-metrics-${width}.png`, fullPage: true});
  }
  await page.getByText("CPU", {exact:true}).click();
  await page.getByRole("heading", {name:"CPU（%）",exact:true}).waitFor();
  await page.getByText("自定义", {exact:true}).click();
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('.metrics-custom-range .el-range-input[placeholder="开始时间"]').click();
  await page.locator('.metrics-calendar-cell[title="2026-09-11 有指标采样"] i').waitFor();
  assert.ok(dateRequests.some(url => url.pathname.includes("deleted-resource") && url.searchParams.get("month") === "2026-09"));
  assert.equal(await page.locator('.metrics-calendar-cell[title="2026-09-12"] i').count(), 0);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    await page.screenshot({path:`output/playwright/resource-metric-dates-${width}.png`,fullPage:false});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "日期面板不得横向溢出");
    assert.equal(await page.locator(".metrics-calendar-popper:visible .el-picker-panel__body").evaluate(element => element.scrollWidth > element.clientWidth), false, "日期面板内部不能裁切日期列");
  }
  await page.setViewportSize({width:1440,height:1000});
  const panel = page.locator(".metrics-calendar-popper:visible");
  await panel.locator(".el-picker-panel__icon-btn.arrow-right").last().click();
  await panel.locator('[title="2026-11-11 有指标采样"] i').waitFor();
  assert.ok(dateRequests.some(url => url.searchParams.get("month") === "2026-12"), "翻月覆盖右侧面板及相邻月");
  failDates = true;
  await panel.locator(".el-picker-panel__icon-btn.arrow-right").last().click();
  await page.getByText("日期标记加载失败，重新打开日历可重试", {exact:true}).waitFor();
  await page.locator('.metrics-custom-range .el-range-input[placeholder="开始时间"]').press("Escape");
  await panel.waitFor({state:"hidden"});
  failDates = false;
  await page.locator('.metrics-custom-range .el-range-input[placeholder="开始时间"]').click();
  await page.getByText("日期标记加载失败，重新打开日历可重试", {exact:true}).waitFor({state:"hidden"});
  await page.locator('.metrics-calendar-cell[title="2026-09-11 有指标采样"] i').waitFor();
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, metricRequests, screenshots: "output/playwright/resource-metrics-{1440,390}.png", consoleErrors: 0 }));
} finally { await browser.close(); }
