// 后台监控折叠、常用命令与工作台补全的隔离浏览器验收。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, permissions: ["clipboard-read", "clipboard-write"] });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "fixture-token"));
const common = Array.from({ length: 101 }, (_, index) => ({ id: `common-${index + 1}`, command: index === 0 ? "logread -f" : `command-${String(index + 1).padStart(3, "0")}`, description: index === 0 ? "持续查看设备日志" : `描述 ${index + 1}`, enabled: index !== 1, version: 1, createdAt: "2026-10-19T00:00:00Z", updatedAt: "2026-10-19T00:00:00Z" }));
const monitor = { intervalSeconds: 60, retentionDays: 90, items: [{ id: "cpu", name: "CPU 使用率", command: "top", pattern: "(\\d+)", unit: "%", enabled: true }], processDiscoveryCommand: "ps", processRules: [{ id: "proc", name: "主进程", pattern: "camera", enabled: true }], processStatusCommand: "cat /proc/{pid}/status", processValuePattern: "VmRSS\\s+(\\d+)" };
const platform = { retentionDays: 7, clusterCapacity: 500, liveLogBufferMiB: 10, serviceLogRetentionDays: 7, version: 1, updatedAt: "2026-10-19T00:00:00Z", resourceMonitor: monitor, recordRetention: { auditDays: 90, eventDays: 90, runDays: 90 } };
const task = { id: "task-1", name: "验收任务", protocol: "SSH", ip: "192.0.2.1", port: 22, status: "COLLECTING", desiredState: "RUNNING", initialCommands: [], scheduledCommands: [], version: 1 };
const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const pageOf = (items, page = 1, pageSize = 100) => ({ items, total: items.length, page, pageSize });
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const delayedAdminPages = [];
const delayedUpdates = [];
const delayedCommonPages = [];
await context.route("**/api/v1/**", async route => {
  const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
  if (path === "/api/v1/auth/me") return json(route, { user: { id: "admin", username: "admin", displayName: "管理员", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } });
  if (path === "/api/v1/platform-settings" && method === "GET") return json(route, platform);
  if (path === "/api/v1/admin/nodes") return json(route, pageOf([]));
  if (path === "/api/v1/admin/common-commands") {
    const page = Number(url.searchParams.get("page") || 1), size = Number(url.searchParams.get("pageSize") || 100);
    assert.ok(Number.isInteger(page) && page >= 1, "管理列表必须发送有效页码");
    if (method === "GET") {
      const delayed = delayedAdminPages.shift();
      if (delayed) return delayed.promise.then(body => json(route, body));
      return json(route, { ...pageOf(common.slice((page - 1) * size, page * size), page, size), total: common.length });
    }
    if (method === "POST") { const body = request.postDataJSON(); common.unshift({ id: "common-new", version: 1, createdAt: "2026-10-19T00:00:00Z", updatedAt: "2026-10-19T00:00:00Z", ...body }); return json(route, common[0], 201); }
  }
  if (path.startsWith("/api/v1/admin/common-commands/") && method === "PATCH") {
    const row = common.find(item => item.id === path.split("/").at(-1));
    Object.assign(row, request.postDataJSON(), { version: (row?.version ?? 0) + 1 });
    const delayed = delayedUpdates.shift();
    if (delayed) return delayed.promise.then(body => json(route, body));
    return json(route, row);
  }
  if (path.startsWith("/api/v1/admin/common-commands/") && method === "DELETE") { common.splice(common.findIndex(item => item.id === path.split("/").at(-1)), 1); return route.fulfill({ status: 204 }); }
  if (path === "/api/v1/common-commands") {
    const delayed = delayedCommonPages.shift();
    if (delayed) return delayed.promise.then(body => json(route, body));
    const enabled = common.filter(item => item.enabled);
    const page = Number(url.searchParams.get("page") || 1), size = Number(url.searchParams.get("pageSize") || 100);
    return json(route, { ...pageOf(enabled.slice((page - 1) * size, page * size), page, size), total: enabled.length });
  }
  if (path === "/api/v1/tasks") return json(route, pageOf([task], 1, 20));
  if (path === "/api/v1/tasks/task-1") return json(route, task);
  if (path === "/api/v1/display-settings") return json(route, { liveLogBufferMiB: 10 });
  if (path === "/api/v1/command-templates" || path === "/api/v1/nodes" || path === "/api/v1/service-tokens" || path === "/api/v1/audit-events" || path === "/api/v1/runtime-events") return json(route, pageOf([]));
  if (path.endsWith("/search-index-status")) return json(route, { enabled: false, status: "DISABLED", totalFiles: 0, coveredFiles: 0, coverageComplete: false, latestIndexedHour: null, contiguousCoveredThrough: null, truncated: false, fallback: "RAW_SCAN" });
  if (path.endsWith("/log-hours")) return json(route, pageOf([], 1, 24));
  if (path.endsWith("/log-days")) return json(route, { days: [] });
  throw new Error(`未模拟请求 ${method} ${path}`);
});
await context.routeWebSocket("**/api/v1/tasks/*/logs", socket => socket.onMessage(() => {}));
const page = await context.newPage(); page.setDefaultTimeout(10_000); await mkdir("output/playwright", { recursive: true });
try {
  await page.goto(`${(process.env.BASE_URL || "http://127.0.0.1:5173").replace(/\/$/, "")}/#settings`, { waitUntil: "networkidle" });
  await page.getByText("资源监控", { exact: true }).click();
  await page.getByText("CPU 使用率", { exact: true }).click();
  await page.getByRole("button", { name: "新增指标" }).click();
  await page.getByLabel("指标名称 2").waitFor();
  await page.waitForTimeout(400);
  assert.equal(await page.getByLabel("指标名称 2").evaluate(element => document.activeElement === element), true, "新增指标必须展开并聚焦");
  await page.screenshot({ path: "output/playwright/settings-monitor-1440.png", fullPage: true });
  await page.getByText("常用命令", { exact: true }).click();
  await page.getByRole("button", { name: "刷新常用命令", exact: true }).click();
  await page.getByText("logread -f", { exact: true }).waitFor();
  await page.getByText("command-101", { exact: true }).count();
  await page.locator(".common-commands .el-pagination .btn-next").click();
  await page.getByText("command-101", { exact: true }).waitFor();
  await page.locator(".common-commands .el-pagination .btn-prev").click();
  await page.getByText("logread -f", { exact: true }).waitFor();
  await page.getByRole("button", { name: "禁用常用命令 logread -f" }).click();
  await page.getByText("常用命令已禁用", { exact: true }).waitFor();
  await page.getByRole("button", { name: "新增命令" }).click(); await page.getByRole("dialog", { name: "新增常用命令" }).getByLabel("常用命令", { exact: true }).fill("ifconfig"); await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByText("常用命令已新增", { exact: true }).waitFor();

  // 旧分页晚于新分页返回时，表格和序号必须保留最新目标页的数据。
  const slowSecondPage = deferred(), freshFirstPage = deferred();
  delayedAdminPages.push(slowSecondPage, freshFirstPage);
  await page.locator(".common-commands .el-pagination .btn-next").click();
  await page.waitForTimeout(100);
  await page.locator(".common-commands .el-pagination .btn-prev").click();
  await page.waitForTimeout(100);
  const raceRows = [
    { ...common[0], id: "race-a", command: "fresh-page-a", description: "当前页 A" },
    { ...common[1], id: "race-b", command: "fresh-page-b", description: "当前页 B" },
  ];
  common.unshift(...raceRows);
  freshFirstPage.resolve({ ...pageOf(raceRows, 1, 100), total: 101 });
  await page.getByText("fresh-page-a", { exact: true }).waitFor();
  slowSecondPage.resolve({ ...pageOf([{ ...common[100], command: "stale-page-two" }], 2, 100), total: 101 });
  await page.waitForTimeout(100);
  assert.equal(await page.getByText("stale-page-two", { exact: true }).count(), 0, "旧页响应不得覆盖新分页");
  assert.equal(await page.getByText("fresh-page-a", { exact: true }).count(), 1, "最新分页必须保留对应行");

  // 保存 A 时打开 B，A 的迟到成功响应不得关闭或改写 B 的编辑会话。
  const slowUpdate = deferred();
  delayedUpdates.push(slowUpdate);
  await page.getByRole("button", { name: "编辑常用命令 fresh-page-a" }).click();
  const dialog = page.getByRole("dialog", { name: "编辑常用命令" });
  await dialog.getByLabel("常用命令", { exact: true }).fill("fresh-page-a-updated");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "编辑常用命令 fresh-page-b" }).click();
  await dialog.getByLabel("常用命令", { exact: true }).fill("fresh-page-b-draft");
  slowUpdate.resolve({ ...raceRows[0], command: "fresh-page-a-updated", version: 2 });
  await page.waitForTimeout(100);
  assert.equal(await dialog.isVisible(), true, "旧保存成功不得关闭后续编辑框");
  assert.equal(await dialog.getByLabel("常用命令", { exact: true }).inputValue(), "fresh-page-b-draft", "旧保存成功不得覆盖后续编辑内容");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await page.screenshot({ path: "output/playwright/settings-common-1440.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 }); await page.getByLabel("后台配置模块").getByText("常用命令", { exact: true }).click(); await page.waitForTimeout(500); await page.screenshot({ path: "output/playwright/settings-common-390.png", fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "移动端不得产生页面横向溢出");
  const staleFirstCommonPage = deferred(), staleSecondCommonPage = deferred(), freshCommonPage = deferred();
  delayedCommonPages.push(staleFirstCommonPage, staleSecondCommonPage);
  await page.getByRole("tab", { name: "采集任务", exact: true }).click(); await page.getByRole("button", { name: "查看实时打印 · 验收任务" }).click();
  staleFirstCommonPage.resolve({
    items: Array.from({ length: 100 }, (_, index) => ({ ...common[0], id: `stale-${index}`, command: `stale-${index}` })),
    total: 101, page: 1, pageSize: 100,
  });
  await page.waitForTimeout(150);
  delayedCommonPages.push(freshCommonPage);
  const input = page.getByLabel("输入手工命令");
  await input.focus();
  await page.waitForTimeout(100);
  freshCommonPage.resolve({ items: [{ ...common[0], id: "fresh-enabled", command: "fresh-enabled", description: "最新启用命令" }], total: 1, page: 1, pageSize: 100 });
  await input.fill("fresh-enabled");
  await page.getByText("最新启用命令", { exact: true }).waitFor();
  staleSecondCommonPage.resolve({ items: [{ ...common[0], id: "stale-disabled", command: "stale-disabled", description: "已禁用旧命令" }], total: 101, page: 2, pageSize: 100 });
  await page.waitForTimeout(100);
  await input.fill("stale-disabled");
  assert.equal(await page.getByText("已禁用旧命令", { exact: true }).count(), 0, "旧分页不得重新加入已禁用命令");
  await input.fill("fresh-enabled"); await page.getByText("最新启用命令", { exact: true }).click();
  await page.waitForTimeout(500); await page.screenshot({ path: "output/playwright/workbench-command-suggestion-390.png", fullPage: true });
  assert.equal(await input.inputValue(), "fresh-enabled", "点击候选只能填入命令，不自动发送");
  console.log("settings common commands browser verification passed");
} finally { await browser.close(); }
