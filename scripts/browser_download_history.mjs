// 下载记录浏览器验收：路由层夹具验证分页、制品复用、过期禁用和任务切换竞态。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "download-history-token"));
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const tasks = [
  { id: "task-one", name: "下载历史任务一", protocol: "SSH", ip: "192.0.2.41", port: 22, status: "STOPPED", desiredState: "STOPPED", initialCommands: [], scheduledCommands: [], version: 1 },
  { id: "task-two", name: "下载历史任务二", protocol: "SSH", ip: "192.0.2.42", port: 22, status: "STOPPED", desiredState: "STOPPED", initialCommands: [], scheduledCommands: [], version: 1 },
];
const ready = { id: "ready-download", taskId: "task-one", status: "SUCCEEDED", createdAt: "2026-10-16T01:00:00Z", expiresAt: "2026-10-20T01:00:00Z", bytes: 3_145_728, filename: "ready.tar.gz", progress: 100, downloadAvailable: true };
const expired = { id: "expired-download", taskId: "task-one", status: "SUCCEEDED", createdAt: "2026-10-01T01:00:00Z", expiresAt: "2026-10-08T01:00:00Z", bytes: 2_048, filename: "expired.tar.gz", progress: 100, downloadAvailable: false };
const taskTwoRecords = Array.from({ length: 21 }, (_, index) => ({
  id: `task-two-${index + 1}`, taskId: "task-two", status: index === 0 ? "RUNNING" : "FAILED",
  createdAt: `2026-10-16T${String(1 + (index % 20)).padStart(2, "0")}:00:00Z`, expiresAt: undefined,
  bytes: index === 0 ? undefined : index * 100, progress: index === 0 ? 47 : undefined,
  error: index === 0 ? undefined : "归档缺少可用片段", downloadAvailable: false,
}));
let historyRequests = 0;
let delayedRefresh = false;
let delayedStarted;
let releaseDelayed;

async function json(route, body, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
function page(items, current = 1, total = items.length) { return { items, total, page: current, pageSize: 20 }; }
await context.route("**/api/v1/**", async route => {
  const request = route.request(); const url = new URL(request.url()); const path = url.pathname; const method = request.method();
  if (method === "GET" && path === "/api/v1/auth/me") return json(route, { user: { id: "reader", username: "reader", displayName: "下载验收", scopes: ["tasks:read", "logs:read", "logs:download"], isAdmin: false, enabled: true, mustChangePassword: false } });
  if (method === "GET" && path === "/api/v1/tasks") return json(route, page(tasks));
  if (method === "GET" && /^\/api\/v1\/tasks\/[^/]+\/downloads$/.test(path)) {
    historyRequests += 1;
    const taskId = decodeURIComponent(path.split("/")[4]);
    if (taskId === "task-one" && delayedRefresh) {
      delayedRefresh = false; delayedStarted?.();
      await new Promise(resolve => { releaseDelayed = resolve; });
    }
    if (taskId === "task-one") return json(route, page([ready, expired]));
    const current = Number(url.searchParams.get("page") || "1");
    return json(route, page(taskTwoRecords.slice((current - 1) * 20, current * 20), current, taskTwoRecords.length));
  }
  if (method === "GET" && /^\/api\/v1\/tasks\/[^/]+\/search-index-status$/.test(path)) {
    const taskId = decodeURIComponent(path.split("/")[4]);
    return json(route, taskId === "task-one"
      ? { enabled: true, status: "AVAILABLE", totalFiles: 12, coveredFiles: 8, coverageComplete: false, latestIndexedHour: "2026-10-16T04:00:00Z", contiguousCoveredThrough: "2026-10-16T02:00:00Z", truncated: false, fallback: "RAW_SCAN" }
      : { enabled: false, status: "DISABLED", totalFiles: 0, coveredFiles: 0, coverageComplete: false, latestIndexedHour: null, contiguousCoveredThrough: null, truncated: false, fallback: "RAW_SCAN" });
  }
  if (method === "GET" && /^\/api\/v1\/tasks\/[^/]+\/(log-hours|log-days)$/.test(path)) return json(route, path.endsWith("log-days") ? { days: [] } : page([]));
  if (method === "GET" && /^\/api\/v1\/downloads\/[^/]+\/content$/.test(path)) return route.fulfill({ status: 200, headers: { "content-type": "application/gzip", "content-disposition": "attachment; filename=ready.tar.gz" }, body: "fixture" });
  if (method === "GET" && path === "/api/v1/common-commands") return json(route, page([]));
  if (method === "GET" && path.startsWith("/api/v1/tasks/")) return json(route, tasks.find(task => path.endsWith(task.id)) ?? tasks[0]);
  if (method === "GET" && path === "/api/v1/resources") return json(route, page([]));
  if (method === "GET" && path === "/api/v1/command-templates") return json(route, page([]));
  if (method === "GET" && path === "/api/v1/nodes") return json(route, page([]));
  if (method === "GET" && path === "/api/v1/display-settings") return json(route, { liveLogBufferMiB: 10 });
  throw new Error(`未模拟的 API 请求：${method} ${path}`);
});
for (const task of tasks) {
  await context.routeWebSocket(`**/api/v1/tasks/${task.id}/logs`, socket => {
    // 保持精确任务订阅的握手成功；下载历史验收不注入实时正文。
    socket.onMessage(() => {});
  });
}

const pageUi = await context.newPage();
pageUi.setDefaultTimeout(12_000);
const errors = [];
pageUi.on("pageerror", error => errors.push(error.message));
pageUi.on("console", message => { if (message.type() === "error") errors.push(message.text()); });

try {
  await mkdir("output/playwright", { recursive: true });
  await pageUi.goto(`${baseUrl.replace(/\/$/, "")}/#tasks`, { waitUntil: "networkidle" });
  await pageUi.getByRole("tab", { name: "采集任务", exact: true }).click();
  await pageUi.getByRole("button", { name: "查看实时打印 · 下载历史任务一" }).click();
  await pageUi.getByRole("tab", { name: "下载记录", exact: true }).click();
  await pageUi.getByText("制品可重新下载", { exact: true }).waitFor();
  assert.equal(await pageUi.getByText("已完成", { exact: true }).count(), 1, "成功制品必须显示已完成状态");
  assert.equal(await pageUi.getByText("已过期", { exact: true }).count(), 1, "成功但过期的制品必须明确标记为已过期");
  const expiredButton = pageUi.getByLabel("重新下载 expired-download");
  assert.equal(await expiredButton.isDisabled(), true, "过期制品必须禁用重复下载");
  await pageUi.getByLabel("重新下载 ready-download").click();
  await pageUi.waitForFunction(() => true);
  // 归档内容链接是公开附件地址，点击不能先请求短期浏览器票据或重建作业。
  assert.equal(await pageUi.evaluate(() => performance.getEntriesByType("resource").some(entry => entry.name.includes("browser-session"))), false, "重复下载不得请求浏览器票据");
  assert.equal(historyRequests, 1, "初始记录页只读取一次历史");
  await pageUi.screenshot({ path: "output/playwright/download-history-1440.png", fullPage: true });
  await pageUi.getByRole("tab", { name: "小时归档与检索", exact: true }).click();
  await pageUi.getByText("已覆盖 8/12 个分卷", { exact: true }).waitFor();
  await pageUi.getByText("连续覆盖至北京时间 2026/10/16 10:00:00。", { exact: true }).waitFor();
  await pageUi.getByText("最近已索引小时：北京时间 2026/10/16 12:00:00（不代表连续覆盖）。", { exact: true }).waitFor();
  await pageUi.getByText("未覆盖的分卷仍会按原文确认结果。", { exact: true }).waitFor();
  await pageUi.screenshot({ path: "output/playwright/search-index-coverage-1440.png", fullPage: true });
  await pageUi.getByRole("tab", { name: "下载记录", exact: true }).click();

  const delayed = new Promise(resolve => { delayedStarted = resolve; });
  delayedRefresh = true;
  await pageUi.getByRole("button", { name: "刷新下载记录" }).click();
  await delayed;
  await pageUi.getByRole("button", { name: "切换任务", exact: true }).click();
  await pageUi.getByRole("dialog", { name: "选择日志任务" }).getByText("下载历史任务二", { exact: true }).click();
  await pageUi.getByRole("button", { name: "确认切换", exact: true }).click();
  releaseDelayed();
  await pageUi.getByRole("tab", { name: "下载记录", exact: true }).click();
  await pageUi.getByText("归档缺少可用片段", { exact: true }).first().waitFor();
  assert.equal(await pageUi.getByText("ready.tar.gz", { exact: true }).count(), 0, "切换任务后旧任务迟到响应不得覆盖当前历史");
  assert.equal(await pageUi.getByText("47%", { exact: true }).count(), 1, "运行中作业必须展示服务端进度");
  await pageUi.getByRole("tab", { name: "小时归档与检索", exact: true }).click();
  await pageUi.getByText("服务未启用索引，本次检索将扫描原文。", { exact: true }).waitFor();
  await pageUi.getByText("未覆盖的分卷仍会按原文确认结果。", { exact: true }).waitFor();
  await pageUi.getByRole("tab", { name: "下载记录", exact: true }).click();
  await pageUi.locator(".el-pagination .btn-next").click();
  await pageUi.getByText("task-two-21", { exact: true }).waitFor().catch(() => pageUi.locator(".download-history-table .el-table__row").first().waitFor());
  assert.equal(await pageUi.getByText("归档缺少可用片段", { exact: true }).count(), 1, "第二页必须保留独立分页数据");
  await pageUi.setViewportSize({ width: 390, height: 844 });
  await pageUi.screenshot({ path: "output/playwright/download-history-390.png", fullPage: true });
  assert.equal(await pageUi.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "下载记录窄屏不得横向溢出");
  assert.ok(historyRequests >= 4, "刷新、切换任务与分页必须分别读取服务端历史");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, historyRequests, screenshots: "output/playwright/download-history-{1440,390}.png" }));
} finally {
  await browser.close();
}
